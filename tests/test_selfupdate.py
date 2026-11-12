"""Updating Sadoc from the latest main branch."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from sadoc import selfupdate


def _git(path: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(path), *args], capture_output=True, text=True, timeout=60
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


@pytest.fixture
def checkout(tmp_path):
    """A repository with an origin, one commit ahead of the clone."""
    origin = tmp_path / "origin"
    origin.mkdir()
    _git(origin, "init", "-q", "--initial-branch=main")
    _git(origin, "config", "user.email", "t@t.t")
    _git(origin, "config", "user.name", "t")
    (origin / "README.md").write_text("one\n")
    _git(origin, "add", "-A")
    _git(origin, "commit", "-qm", "first")

    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", str(origin), str(clone)], check=True, timeout=60)
    _git(clone, "config", "user.email", "t@t.t")
    _git(clone, "config", "user.name", "t")
    return clone, origin


def test_a_fresh_checkout_is_already_up_to_date(checkout):
    clone, _origin = checkout
    result = selfupdate.update(clone)
    assert result.ok
    assert not result.changed
    assert "up to date" in result.message


def test_a_new_commit_is_pulled(checkout):
    clone, origin = checkout
    (origin / "README.md").write_text("two\n")
    _git(origin, "add", "-A")
    _git(origin, "commit", "-qm", "second")

    result = selfupdate.update(clone)
    assert result.ok
    assert result.changed
    assert (clone / "README.md").read_text() == "two\n"


def test_local_changes_are_never_thrown_away(checkout):
    clone, origin = checkout
    (clone / "README.md").write_text("mine\n")
    (origin / "README.md").write_text("two\n")
    _git(origin, "add", "-A")
    _git(origin, "commit", "-qm", "second")

    result = selfupdate.update(clone)
    assert not result.ok
    assert not result.changed
    assert (clone / "README.md").read_text() == "mine\n"


def test_a_divergent_history_is_refused(checkout):
    clone, origin = checkout
    # A commit on the clone that is not on the origin.
    (clone / "README.md").write_text("mine\n")
    _git(clone, "add", "-A")
    _git(clone, "commit", "-qm", "local")
    (origin / "README.md").write_text("two\n")
    _git(origin, "add", "-A")
    _git(origin, "commit", "-qm", "second")

    result = selfupdate.update(clone)
    assert not result.ok
    assert "fast-forward" in result.message


def test_a_directory_that_is_not_a_checkout(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    result = selfupdate.update(plain)
    assert not result.ok
    assert "not a git checkout" in result.message
