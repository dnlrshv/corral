from corral.execution.pr_fence import require_active_review_owner
from corral.execution.store import Store, digest


def _export(store):
    record = {"repository": "fixture/repo", "pr_number": 7, "head": "a" * 40}
    record = {"export_id": digest(record), **record}
    store.put_once("trusted_export", record["export_id"], record)
    return record


def test_trusted_export_requires_exact_active_service_pr_fence(tmp_path):
    store = Store(tmp_path / "state.sqlite")
    export = _export(store)
    epoch = store.acquire("pr:fixture/repo#7", "corral")
    spec = {"trusted_export_id": export["export_id"], "pr_owner": "corral",
            "pr_owner_epoch": epoch, "repo": "caller/ignored"}
    require_active_review_owner(store, spec)
    store.transition_owner("pr:fixture/repo#7", "corral", epoch, "released")
    try:
        require_active_review_owner(store, spec)
    except PermissionError as error:
        assert "stale" in str(error)
    else:
        raise AssertionError("released PR owner reached review launch")


def test_checkout_review_has_no_pr_cohort_fence(tmp_path):
    require_active_review_owner(Store(tmp_path / "state.sqlite"), {"repo": "fixture/repo"})
