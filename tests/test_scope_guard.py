"""The scope guard.

The model occasionally changes something that is not a dependency - the Go
language directive, a toolchain version, an engines field. Those are decisions,
not updates, and they break builds in ways local verification does not catch.
"""

from __future__ import annotations

import subprocess

import pytest

from deps_bot.detect import Update
from deps_bot.gitops import Git, _split_patch


def _update(name, manifest="go.mod"):
    return Update(
        repo="o/r",
        manifest=manifest,
        ecosystem="go",
        name=name,
        section="require",
        kind="runtime",
        current="1.0.0",
        latest="2.0.0",
        bump_kind="minor",
        constraint="1.0.0",
    )


@pytest.fixture
def repo(tmp_path):
    path = tmp_path / "repo"
    path.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.t"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=path, check=True)
    (path / "go.mod").write_text(
        "module nori\n\ngo 1.25.0\n\nrequire (\n"
        "\tgithub.com/a-h/templ v0.3.1020\n"
        "\tgithub.com/go-chi/chi/v5 v5.3.1\n)\n"
    )
    subprocess.run(["git", "add", "-A"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=path, check=True)
    return Git(path)


def test_split_patch():
    patch = "diff --git a/f b/f\n--- a/f\n+++ b/f\n@@ -1 +1 @@\n-a\n+b\n@@ -3 +3 @@\n-c\n+d\n"
    header, hunks = _split_patch(patch)
    assert header.startswith("diff --git")
    assert len(hunks) == 2


def test_out_of_scope_detects_a_language_directive(repo):
    (repo.path / "go.mod").write_text(
        "module nori\n\ngo 1.26.0\n\nrequire (\n"
        "\tgithub.com/a-h/templ v0.3.1100\n"
        "\tgithub.com/go-chi/chi/v5 v5.5.0\n)\n"
    )
    from deps_bot.pipeline import Worker

    worker = Worker.__new__(Worker)
    updates = [_update("github.com/a-h/templ"), _update("github.com/go-chi/chi/v5")]
    offenders = worker._out_of_scope_changes(repo, updates)
    assert offenders
    assert all("go 1." in o["line"] for o in offenders)


def test_out_of_scope_ignores_comments(repo):
    (repo.path / "go.mod").write_text(
        "module nori\n\ngo 1.25.0\n\n// a comment about deps\nrequire (\n"
        "\tgithub.com/a-h/templ v0.3.1100\n"
        "\tgithub.com/go-chi/chi/v5 v5.5.0\n)\n"
    )
    from deps_bot.pipeline import Worker

    worker = Worker.__new__(Worker)
    updates = [_update("github.com/a-h/templ"), _update("github.com/go-chi/chi/v5")]
    assert worker._out_of_scope_changes(repo, updates) == []


def test_revert_restores_the_directive_and_keeps_the_updates(repo):
    (repo.path / "go.mod").write_text(
        "module nori\n\ngo 1.26.0\n\nrequire (\n"
        "\tgithub.com/a-h/templ v0.3.1100\n"
        "\tgithub.com/go-chi/chi/v5 v5.5.0\n)\n"
    )
    from deps_bot.pipeline import Worker

    worker = Worker.__new__(Worker)
    updates = [_update("github.com/a-h/templ"), _update("github.com/go-chi/chi/v5")]
    offenders = worker._out_of_scope_changes(repo, updates)
    repo.revert_lines(offenders)
    text = (repo.path / "go.mod").read_text()
    assert "go 1.25.0" in text
    assert "go 1.26.0" not in text
    # The dependency updates survive.
    assert "v0.3.1100" in text
    assert "v5.5.0" in text
