"""Preparation claims remain fenced while process identity is uncertain."""
from corral.execution import service_prepare
from corral.execution.store import Store


def test_prepare_claim_reclaims_only_conclusively_dead_owner(tmp_path, monkeypatch):
    store = Store(tmp_path / "state")
    monkeypatch.setattr(service_prepare, "observe", lambda: {"pid": 10})

    assert service_prepare.claim(store, "event", "task", "transfer")

    monkeypatch.setattr(service_prepare, "process_status", lambda _identity: "unknown")
    assert not service_prepare.claim(store, "event", "task", "transfer")
    monkeypatch.setattr(service_prepare, "process_status", lambda _identity: "alive")
    assert not service_prepare.claim(store, "event", "task", "transfer")
    monkeypatch.setattr(service_prepare, "process_status", lambda _identity: "dead")
    assert service_prepare.claim(store, "event", "task", "transfer")


def test_prepare_claim_rejects_changed_transfer_identity(tmp_path, monkeypatch):
    store = Store(tmp_path / "state")
    monkeypatch.setattr(service_prepare, "observe", lambda: {"pid": 10})
    assert service_prepare.claim(store, "event", "task", "transfer")

    try:
        service_prepare.claim(store, "event", "task", "other")
    except ValueError as exc:
        assert "identity changed" in str(exc)
    else:
        raise AssertionError("changed preparation identity accepted")
