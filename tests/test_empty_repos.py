"""Repositories that are not worth looking after.

An empty repository, or one that has been deleted since it was listed, is not a
failure. Nothing should be said about it.
"""

from __future__ import annotations

from sadoc import gh


def _error(text: str) -> Exception:
    return gh.GhError(f"gh api ... -> exit 1\nstdout: {text}")


def test_an_empty_repository_is_recognised():
    error = _error('{"message":"Git Repository is empty.","status":"409"}')
    assert gh.is_empty_repository(error)
    assert gh.is_unusable_repository(error)


def test_a_deleted_repository_is_recognised():
    error = _error('{"message":"Not Found","status":"404"}')
    assert gh.is_missing_repository(error)
    assert gh.is_unusable_repository(error)


def test_a_real_failure_is_not():
    error = _error('{"message":"Server Error","status":"500"}')
    assert not gh.is_unusable_repository(error)
    assert not gh.is_empty_repository(error)
    assert not gh.is_missing_repository(error)


def test_a_missing_workflow_is_not_a_missing_repository():
    """A 404 for one file says nothing about the repository."""
    error = _error("gh api repos/o/r/contents/.github/workflows/x.yml -> exit 1")
    assert not gh.is_unusable_repository(error)


class _FakeGh:
    def __init__(self, tree):
        self.tree = tree

    def repo_tree(self, owner, repo, branch):
        if isinstance(self.tree, Exception):
            raise self.tree
        return self.tree


def test_a_vanished_repository_has_no_tree(monkeypatch):
    """A 404 on the commit lookup means the repository is gone."""
    calls = {}

    def fake_api(self, endpoint, **kwargs):
        calls["endpoint"] = endpoint
        raise gh.GhError('{"message":"Not Found","status":"404"}')

    monkeypatch.setattr(gh.Gh, "api", fake_api)
    client = gh.Gh.__new__(gh.Gh)
    assert client.repo_tree("o", "r", "main") == []
    assert "commits/" in calls["endpoint"]


def test_an_empty_repository_has_no_tree(monkeypatch):
    """The commit lookup fails, but the tree endpoint says it is empty."""
    calls = {}

    def fake_api(self, endpoint, **kwargs):
        calls["endpoint"] = endpoint
        if "commits/" in endpoint:
            raise gh.GhError('{"message":"Not Found","status":"404"}')
        raise gh.GhError('{"message":"Git Repository is empty.","status":"409"}')

    monkeypatch.setattr(gh.Gh, "api", fake_api)
    client = gh.Gh.__new__(gh.Gh)
    assert client.repo_tree("o", "r", "main") == []


def test_a_real_error_is_not_swallowed(monkeypatch):
    def fake_api(self, endpoint, **kwargs):
        raise gh.GhError('{"message":"Server Error","status":"500"}')

    monkeypatch.setattr(gh.Gh, "api", fake_api)
    client = gh.Gh.__new__(gh.Gh)
    try:
        client.repo_tree("o", "r", "main")
    except gh.GhError:
        return
    raise AssertionError("a server error should not be swallowed")
