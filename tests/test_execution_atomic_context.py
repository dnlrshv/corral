"""Live readers never see an empty or partial instruction amendment."""
import json
import os
import threading

import pytest

from corral.execution.atomic_io import write_json
from corral.execution.adapter import write_native_usage


def test_failed_replace_preserves_previous_context(tmp_path, monkeypatch):
    path = tmp_path / "context.json"
    write_json(path, {"objective": "original"})

    def failed_replace(source, destination):
        assert json.loads(path.read_text()) == {"objective": "original"}
        assert json.loads(source.read_text()) == {"objective": "amended"}
        raise OSError("injected replace failure")

    monkeypatch.setattr(os, "replace", failed_replace)
    with pytest.raises(OSError, match="injected"):
        write_json(path, {"objective": "amended"})
    assert json.loads(path.read_text()) == {"objective": "original"}
    assert list(tmp_path.iterdir()) == [path]


def test_amendments_are_complete_for_concurrent_worker(tmp_path):
    path = tmp_path / "context.json"
    write_json(path, {"sequence": 0, "objective": "0" * 10000})
    stop = threading.Event()
    failures = []
    observed = []

    def read_context():
        while not stop.is_set():
            try:
                value = json.loads(path.read_text())
                assert value["objective"] == str(value["sequence"]) * 10000
                observed.append(value["sequence"])
            except (OSError, ValueError, AssertionError, KeyError) as error:
                failures.append(error)
                break

    reader = threading.Thread(target=read_context)
    reader.start()
    try:
        for sequence in range(1, 50):
            write_json(path, {"sequence": sequence, "objective": str(sequence) * 10000})
    finally:
        stop.set()
        reader.join()
    assert observed and not failures
    assert json.loads(path.read_text())["sequence"] == 49


def test_usage_append_is_atomic_for_controller_reader(tmp_path, monkeypatch):
    path = tmp_path / "native-usage.json"
    original = {"id": "first", "counters": {"input_tokens": 10}}
    added = {"id": "second", "counters": {"input_tokens": 20}}
    write_native_usage(path, [original])
    replace = os.replace
    observed = []

    def observe_before_replace(source, destination):
        observed.append(json.loads(path.read_text()))
        assert json.loads(source.read_text()) == [original, added]
        replace(source, destination)

    monkeypatch.setattr(os, "replace", observe_before_replace)
    write_native_usage(path, [added])
    assert observed == [[original]]
    assert json.loads(path.read_text()) == [original, added]
