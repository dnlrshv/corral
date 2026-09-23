"""Second controller-side PR ownership fence for trusted inspection exports."""
from __future__ import annotations

from typing import Any

from .store import digest


def require_active_review_owner(store: Any, spec: dict[str, Any]) -> None:
    """Require the service-bound Corral PR lane immediately before review launch.

    A trusted export determines the repository and PR.  Request ``repo`` or PR fields
    are deliberately not used for the fence, so a caller cannot redirect the check.
    Checkout reviews do not have an export and keep their ordinary workspace contract.
    """
    export_id = spec.get("trusted_export_id")
    if export_id is None:
        return
    export = store.get("trusted_export", export_id)
    if (not isinstance(export_id, str) or not isinstance(export, dict)
            or export.get("export_id") != export_id
            or digest({key: value for key, value in export.items() if key != "export_id"}) != export_id):
        raise PermissionError("trusted export is unavailable for PR ownership fence")
    repo, number = export.get("repository"), export.get("pr_number")
    if (not isinstance(repo, str) or not repo or isinstance(number, bool)
            or not isinstance(number, int) or number <= 0):
        raise PermissionError("trusted export has no valid PR identity")
    expected_owner, expected_epoch = spec.get("pr_owner"), spec.get("pr_owner_epoch")
    if (expected_owner != "corral" or isinstance(expected_epoch, bool)
            or not isinstance(expected_epoch, int) or expected_epoch <= 0):
        raise PermissionError("trusted-export review requires service-bound PR owner and epoch")
    resource = f"pr:{repo}#{number}"
    if store.ownership(resource) != (expected_owner, expected_epoch, "active"):
        raise PermissionError("trusted-export review PR ownership fence is stale")
