"""Publisher identity is explicitly authorized and authenticated, never personal defaults."""
import pytest

from corral.execution.github_advisory import GitHubAdvisoryTransport


def _user(login):
    return lambda *_args, **_kwargs: {"login": login, "id": 1001}


def test_user_publisher_requires_explicit_authorization():
    with pytest.raises(PermissionError):
        GitHubAdvisoryTransport(http_client=_user("review-publisher"),
                               bridge_actor="review-publisher")
    transport = GitHubAdvisoryTransport(
        http_client=_user("review-publisher"), bridge_actor="review-publisher",
        authorized_bridge_actors=frozenset({"review-publisher"}))
    assert transport.validated_bridge_actor == "review-publisher"


def test_empty_authorized_publishers_does_not_restore_defaults():
    with pytest.raises(PermissionError):
        GitHubAdvisoryTransport(http_client=_user("github-actions[bot]"),
                               authorized_bridge_actors=frozenset())


def test_authorized_actor_cannot_impersonate_authenticated_user():
    with pytest.raises(PermissionError):
        GitHubAdvisoryTransport(
            http_client=_user("different-user"), bridge_actor="review-publisher",
            authorized_bridge_actors=frozenset({"review-publisher"}))
