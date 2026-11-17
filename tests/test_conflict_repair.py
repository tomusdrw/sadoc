import json
import subprocess
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from sadoc.config import Config
from sadoc.gitops import Git
from sadoc.pipeline import Worker
from sadoc.piworker import PiError, TurnResult
from sadoc.state import State


@pytest.fixture
def project(tmp_path, monkeypatch):
    path = tmp_path / "repo"
    path.mkdir()
    git = Git(path)
    git._run("init", "-b", "main")
    git._run("config", "user.name", "Test")
    git._run("config", "user.email", "test@example.com")
    (path / "conflict.txt").write_text("original\n")
    git.commit("initial")
    git.create_branch("deps/update")
    (path / "conflict.txt").write_text("update\n")
    git.commit("dependency update")
    head = git.commit_sha()
    git.checkout("main")
    (path / "conflict.txt").write_text("base\n")
    (path / "main-only.txt").write_text("preserve this\n")
    git.commit("main changes")
    base = git.commit_sha()
    remote = tmp_path / "remote.git"
    subprocess.run(
        ["git", "clone", "--bare", str(path), str(remote)], check=True, capture_output=True
    )
    git._run("remote", "add", "origin", str(remote))
    gh = Mock()
    gh.pr_view.return_value = {
        "state": "OPEN",
        "mergeable": "CONFLICTING",
        "headRefName": "deps/update",
        "baseRefName": "main",
    }
    gh.default_branch.return_value = "main"
    gh.pr_review_comments.return_value = []
    gh.pr_issue_comments.return_value = []
    gh.pr_checks.return_value = []
    state = State(tmp_path / "state.db")
    state.record_pr("o/r", 7, branch="deps/update", tier="B", state="open")
    state.record_pr_details(
        "o/r",
        7,
        updates=json.dumps(
            [
                {
                    "name": "widget",
                    "current": "1.0.0",
                    "latest": "2.0.0",
                    "manifest": "package.json",
                    "ecosystem": "npm",
                }
            ]
        ),
    )
    cfg = Config(
        repos={"o/r": {"verify": 'test "$(cat conflict.txt)" = resolved && test -f main-only.txt'}}
    )
    worker = Worker(cfg, gh, state)
    monkeypatch.setattr(worker, "prepare_clone", lambda *a: nullcontext(git))
    worker._current_checks = Mock(return_value=[])
    worker.base_branch_is_also_failing = Mock(return_value=(False, ""))
    worker.fix_ci = Mock(side_effect=AssertionError("must not repair stale CI before merging"))

    class Session:
        resolution = "resolved\n"
        prompts = []

        def __init__(self, **kwargs):
            pass

        def start(self):
            pass

        def prompt(self, text, **kwargs):
            self.prompts.append(text)
            (path / "conflict.txt").write_text(self.resolution)
            return TurnResult(text="RESOLVED: yes", settled=True)

        def bash(self, command, **kwargs):
            result = subprocess.run(command, shell=True, cwd=path, capture_output=True, text=True)
            return {
                "success": True,
                "data": {"exitCode": result.returncode, "output": result.stdout + result.stderr},
            }

        def close(self):
            pass

        def diagnostics(self):
            return "{}"

    monkeypatch.setattr("sadoc.pipeline.PiSession", Session)
    yield SimpleNamespace(
        worker=worker,
        git=git,
        state=state,
        session=Session,
        remote=Git(remote),
        head=head,
        base=base,
    )
    state.close()


@pytest.mark.parametrize("checks", [[], [{"bucket": "pass"}], [{"bucket": "fail"}]])
def test_conflicts_are_resolved_verified_and_pushed_before_old_ci(project, checks):
    p = project
    p.worker._current_checks.return_value = checks
    result = p.worker.monitor_pr(p.state.open_prs()[0])
    merged = p.remote.commit_sha("deps/update")
    assert merged != p.head, result
    assert p.git._run("rev-list", "--parents", "-1", merged).strip().split()[1:] == [p.head, p.base]
    assert p.remote._run("show", "deps/update:main-only.txt") == "preserve this\n"
    assert p.remote._run("show", "deps/update:conflict.txt") == "resolved\n"
    assert "2.0.0" in p.session.prompts[0]
    assert p.state.open_prs()[0]["checks_state"] == "pending"


@pytest.mark.parametrize(
    "resolution", ["wrong\n", "<<<<<<< HEAD\nupdate\n=======\nbase\n>>>>>>> main\n"]
)
def test_invalid_resolution_is_not_pushed_and_merge_is_cleaned(project, resolution):
    p = project
    p.session.resolution = resolution
    with pytest.raises((PiError, RuntimeError)):
        p.worker.monitor_pr(p.state.open_prs()[0])
    assert p.remote.commit_sha("deps/update") == p.head
    assert p.git.commit_sha() == p.head
    assert not p.git.changed_files()
    assert not p.git._run("rev-parse", "-q", "--verify", "MERGE_HEAD", check=False).strip()
    assert p.state.open_prs()[0]["conflict_attempts"] == 1


def test_dry_run_never_pushes_conflict_resolution(project):
    p = project
    p.worker.monitor_pr(p.state.open_prs()[0], dry_run=True)
    assert p.remote.commit_sha("deps/update") == p.head
    assert p.git.commit_sha() == p.head
    assert p.state.open_prs()[0]["conflict_attempts"] == 0


def test_conflict_attempts_are_bounded_separately_from_ci(project):
    p = project
    p.state.record_pr("o/r", 7, conflict_attempts=3)
    result = p.worker.monitor_pr(p.state.open_prs()[0])
    assert "already tried" in result
    assert p.remote.commit_sha("deps/update") == p.head
    assert not p.session.prompts


def test_resolution_cannot_edit_unrelated_base_files(project, monkeypatch):
    p = project
    original = p.session.prompt

    def edit(self, text, **kwargs):
        result = original(self, text, **kwargs)
        (p.git.path / "main-only.txt").write_text("lost base content\n")
        return result

    monkeypatch.setattr(p.session, "prompt", edit)
    with pytest.raises(PiError, match="outside the conflict set"):
        p.worker.monitor_pr(p.state.open_prs()[0])
    assert p.remote.commit_sha("deps/update") == p.head


def test_stale_conflicting_status_can_merge_without_model_edits(project):
    p = project
    (p.git.path / "conflict.txt").write_text("original\n")
    p.git.commit("base restores original text")
    p.git.push("main")
    p.base = p.git.commit_sha()
    p.worker.cfg.repos["o/r"]["verify"] = (
        'test "$(cat conflict.txt)" = update && test -f main-only.txt'
    )
    p.worker.monitor_pr(p.state.open_prs()[0])
    assert p.remote.commit_sha("deps/update") != p.head
    assert not p.session.prompts
    assert p.remote._run("show", "deps/update:conflict.txt") == "update\n"


def test_shallow_checkout_recovers_common_history_before_merging(project, tmp_path):
    p = project
    shallow = Git(tmp_path / "shallow")
    shallow.clone(p.remote.path.as_uri(), "main", depth=1)
    shallow._run("config", "user.name", "Test")
    shallow._run("config", "user.email", "test@example.com")
    assert shallow.fetch_branch("deps/update")
    shallow.create_branch("deps/update", "origin/deps/update")
    assert shallow.merge_base_branch("main") == ["conflict.txt"]
    assert "<<<<<<<" in (shallow.path / "conflict.txt").read_text()


def test_exhausted_ci_budget_does_not_block_conflict_repair(project):
    p = project
    p.state.record_pr("o/r", 7, ci_attempts=3)
    p.worker.monitor_pr(p.state.open_prs()[0])
    assert p.remote.commit_sha("deps/update") != p.head
    assert p.state.open_prs()[0]["ci_attempts"] == 3


def test_verification_cannot_stage_changes_into_the_merge(project, monkeypatch):
    p = project
    original = p.session.bash

    def bash(self, command, **kwargs):
        result = original(self, command, **kwargs)
        (p.git.path / "main-only.txt").write_text("changed after verification\n")
        p.git.stage_all()
        return result

    monkeypatch.setattr(p.session, "bash", bash)
    with pytest.raises(PiError, match="verification modified"):
        p.worker.monitor_pr(p.state.open_prs()[0])
    assert p.remote.commit_sha("deps/update") == p.head


def test_existing_base_whitespace_does_not_block_conflict_repair(project):
    p = project
    (p.git.path / "main-only.txt").write_text("preserve this  \n")
    p.git.commit("base contains trailing spaces")
    p.git.push("main")
    p.worker.monitor_pr(p.state.open_prs()[0])
    assert p.remote._run("show", "deps/update:main-only.txt") == "preserve this  \n"


def test_model_cannot_change_merge_ancestry(project, monkeypatch):
    p = project
    original = p.session.prompt

    def edit(self, text, **kwargs):
        reply = original(self, text, **kwargs)
        (p.git.path / ".git" / "MERGE_HEAD").write_text(p.head + "\n")
        return reply

    monkeypatch.setattr(p.session, "prompt", edit)
    with pytest.raises(PiError, match="merge ancestry"):
        p.worker.monitor_pr(p.state.open_prs()[0])
    assert p.remote.commit_sha("deps/update") == p.head
