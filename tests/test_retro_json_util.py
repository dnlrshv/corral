from __future__ import annotations

import json
import re

import pytest
from pathlib import Path
from typing import Any

from corral.retro.json_util import extract_json_payload


def test_exact_json_object() -> None:
    assert extract_json_payload('{"a": 1}') == {"a": 1}


def test_json_wrapped_in_prose_and_fences() -> None:
    reply = 'Here you go:\n```json\n{"rule": "x", "n": [1, 2]}\n```\nDone.'
    assert extract_json_payload(reply) == {"rule": "x", "n": [1, 2]}


def test_json_array_payload() -> None:
    assert extract_json_payload("result: [1, 2, 3]") == [1, 2, 3]


def test_invalid_or_missing_payload_returns_none() -> None:
    assert extract_json_payload("") is None
    assert extract_json_payload("no payload at all") is None
    assert extract_json_payload("{not valid json") is None


_REFERENCE_PATH = Path(__file__).parents[1] / "_import/scripts/_json_helper_reference.py"


@pytest.mark.skipif(
    not _REFERENCE_PATH.exists(),
    reason="porting-time differential check; reference source lives only in the untracked staging area",
)
def test_behavior_matches_read_only_reference_on_adversarial_corpus() -> None:
    namespace = {"json": json, "re": re, "Any": Any}
    reference_path = _REFERENCE_PATH
    exec(reference_path.read_text(encoding="utf-8"), namespace)
    reference = namespace["extract_json_payload"]

    corpus: list[Any] = [
        None,
        "",
        "  ",
        '{"a": 1}',
        '{"a": 1} trailing prose',
        'prefix ```json\n{"a": [1, 2]}\n``` suffix',
        "prefix [1, 2, 3] suffix",
        '{"first": 1} middle {"second": 2}',
        '{"partial": true',
        "[1, 2",
        ["not", "a", "string"],
        b'{"bytes": true}',
    ]

    def outcome(function: Any, value: Any) -> tuple[Any, ...]:
        try:
            return ("return", function(value))
        except Exception as exc:  # compare the reference's error path byte-for-byte
            return ("raise", type(exc), str(exc))

    for value in corpus:
        assert outcome(extract_json_payload, value) == outcome(reference, value)
