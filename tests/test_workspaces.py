"""Disposable checkout ownership, including failure and recovery paths."""

import subprocess
import sys
from unittest.mock import Mock

import pytest

from sadoc.config import Config
from sadoc.gitops import Git, GitError
from sadoc.pipeline import Worker
from sadoc.state import State


@pytest.fixture
def worker(tmp_path, monkeypatch):
    cfg = Config(workdir=tmp_path / "work", agent_dir=tmp_path / "agent")
    state = State(tmp_path / "state.db")
    gh = Mock()
    gh.default_branch.return_value = "main"
    gh.pr_list.return_value = []
    worker = Worker(cfg, gh, state)

    def clone(git, url, branch):
        git.path.mkdir(parents=True)
        (git.path / "node_modules").mkdir()
        (git.path / "node_modules" / "large-build-artifact").write_text("build output")

    monkeypatch.setattr(Git, "clone", clone)
    yield worker
    state.close()


@pytest.mark.parametrize("error", [None, RuntimeError("build failed"), KeyboardInterrupt()])
def test_clone_is_removed_after_its_scope(worker, error):
    path = None
    try:
        with worker.prepare_clone("alice", "repo", "main") as git:
            path = git.path
            assert (path / "node_modules" / "large-build-artifact").is_file()
            if error:
                raise error
    except BaseException as exc:
        assert exc is error
    assert path is not None
    assert not path.exists()


def test_partial_clone_is_removed(worker, monkeypatch):
    paths = []

    def fail_clone(git, *args):
        git.path.mkdir(parents=True)
        (git.path / "partial").write_text("incomplete download")
        paths.append(git.path)
        raise GitError("clone timed out")

    monkeypatch.setattr(Git, "clone", fail_clone)
    with (
        pytest.raises(GitError, match="clone timed out"),
        worker.prepare_clone("alice", "repo", "main"),
    ):
        pytest.fail("a failed clone cannot be used")
    assert paths and not paths[0].exists()


def test_process_repo_cleans_on_build_setup_early_return(worker, monkeypatch):
    monkeypatch.setattr(
        worker, "ensure_build_setup", lambda *a: {"status": "blocked", "notes": "no tool"}
    )
    result = worker.process_repo("alice", "repo", "A", [])
    assert result.status == "failed"
    assert not list(worker.cfg.workdir.rglob("large-build-artifact"))


def test_recovery_removes_a_killed_process_checkout(worker):
    from sadoc import workspaces

    script = """
import os
import sys
from pathlib import Path
from sadoc.workspaces import checkout
with checkout(Path(sys.argv[1]), "alice", "abandoned") as path:
    path.mkdir()
    (path / "partial").write_text("left by killed clone")
    os._exit(0)
"""
    result = subprocess.run([sys.executable, "-c", script, str(worker.cfg.workdir)], timeout=10)
    assert result.returncode == 0
    assert list(worker.cfg.workdir.rglob("partial"))
    workspaces.recover(worker.cfg.workdir)
    assert not list(worker.cfg.workdir.rglob("partial"))


def test_recovery_refuses_an_active_checkout(worker):
    from sadoc import workspaces

    with worker.prepare_clone("alice", "repo", "main") as git:
        with pytest.raises(workspaces.WorkspaceError, match="in use"):
            workspaces.recover(worker.cfg.workdir)
        assert git.path.is_dir()


def test_recovery_only_removes_recognizable_legacy_clones(worker, tmp_path):
    from sadoc import workspaces

    workdir = worker.cfg.workdir
    for name, url in (
        ("alice__old", "https://github.com/alice/old.git"),
        ("alice__other", "https://github.com/someone/other.git"),
        ("my-checkout", "https://github.com/alice/my-checkout.git"),
    ):
        gitdir = workdir / name / ".git"
        gitdir.mkdir(parents=True)
        (gitdir / "config").write_text(f'[remote "origin"]\n\turl = {url}\n')
    (workdir / "alice__unknown").mkdir()
    linked = workdir / "alice__worktree"
    linked.mkdir()
    (linked / ".git").write_text("gitdir: /some/other/worktree")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep").write_text("user data")
    (workdir / "alice__symlink").symlink_to(outside, target_is_directory=True)
    managed = workdir / ".sadoc-checkouts"
    managed.mkdir()
    (managed / "link").symlink_to(outside, target_is_directory=True)
    workspaces.recover(workdir)
    assert not (workdir / "alice__old").exists()
    for name in (
        "alice__other",
        "my-checkout",
        "alice__unknown",
        "alice__worktree",
        "alice__symlink",
    ):
        assert (workdir / name).exists()
    assert (outside / "keep").read_text() == "user data"


def test_recovery_rejects_symlinked_managed_root(worker, tmp_path):
    from sadoc import workspaces

    worker.cfg.workdir.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep").write_text("user data")
    (worker.cfg.workdir / ".sadoc-checkouts").symlink_to(outside, target_is_directory=True)
    with pytest.raises(workspaces.WorkspaceError, match="symlink"):
        workspaces.recover(worker.cfg.workdir)
    assert (outside / "keep").exists()


@pytest.mark.parametrize(
    "owner,repo", [("../outside", "repo"), ("alice", "../repo"), ("alice", "..")]
)
def test_checkout_rejects_path_traversal(worker, owner, repo):
    from sadoc import workspaces

    with (
        pytest.raises(workspaces.WorkspaceError),
        workspaces.checkout(worker.cfg.workdir, owner, repo),
    ):
        pytest.fail("invalid repo path")


def test_cleanup_failure_is_visible_and_retried(worker, monkeypatch):
    from sadoc import workspaces

    real_remove = workspaces.shutil.rmtree

    def fail_remove(path):
        raise PermissionError("cannot delete build output")

    with monkeypatch.context() as patch:
        patch.setattr(workspaces.shutil, "rmtree", fail_remove)
        with (
            pytest.raises(workspaces.WorkspaceError, match="cleanup failed"),
            worker.prepare_clone("alice", "repo", "main") as git,
        ):
            path = git.path
    assert path.exists()
    assert real_remove is workspaces.shutil.rmtree
    workspaces.recover(worker.cfg.workdir)
    assert not path.exists()


def test_cleanup_failure_does_not_mask_primary_error(worker, monkeypatch, caplog):
    from sadoc import workspaces

    primary = RuntimeError("the original build failure")

    def fail_remove(path):
        raise PermissionError("cannot delete build output")

    monkeypatch.setattr(workspaces.shutil, "rmtree", fail_remove)
    with pytest.raises(RuntimeError) as raised, worker.prepare_clone("alice", "repo", "main"):
        raise primary
    assert raised.value is primary
    assert "cleanup failed" in "".join(raised.value.__notes__)
    assert "cannot delete build output" in caplog.text


@pytest.mark.parametrize("command", ["run", "monitor", "doctor"])
def test_cli_recovers_even_without_work_to_do(worker, monkeypatch, command):
    from sadoc import cli

    leftover = worker.cfg.workdir / ".sadoc-checkouts" / "alice__inactive"
    leftover.mkdir(parents=True)
    (leftover / "old-build").write_text("retained by a killed job")
    monkeypatch.setattr(sys, "argv", ["sadoc", command])
    monkeypatch.setattr(cli.config_mod, "load", lambda _: worker.cfg)
    monkeypatch.setattr(cli, "Gh", lambda: worker.gh)
    monkeypatch.setattr(cli.detect, "resolve_scope", lambda *args: [])
    monkeypatch.setattr(cli, "_check_models", lambda *args: False)
    cli.main()
    assert not leftover.exists()


def test_doctor_cleans_if_branch_creation_fails(worker, monkeypatch):
    from sadoc import doctor

    monkeypatch.setattr(
        doctor, "diagnose", lambda *a: doctor.Diagnosis(repo="alice/repo", state=doctor.FIXABLE)
    )
    monkeypatch.setattr(Git, "create_branch", Mock(side_effect=GitError("cannot create branch")))
    with pytest.raises(GitError, match="cannot create branch"):
        worker.doctor_repo("alice", "repo")
    assert not list(worker.cfg.workdir.rglob("large-build-artifact"))


def test_conflict_repair_cleans_on_blocked_build_setup(worker, monkeypatch):
    monkeypatch.setattr(worker, "ensure_build_setup", lambda *a: {"status": "blocked"})
    worker.state.record_pr_details("alice/repo", 1, updates='[{"name": "lib", "latest": "2.0"}]')
    result = worker.repair_conflicts(
        {"repo": "alice/repo", "number": 1, "branch": "deps/update"},
        {"baseRefName": "main"},
        dry_run=True,
    )
    assert "blocked" in result
    assert not list(worker.cfg.workdir.rglob("large-build-artifact"))


def test_monitor_cleans_if_pr_branch_has_disappeared(worker, monkeypatch):
    worker.gh.pr_view.return_value = {"state": "OPEN"}
    monkeypatch.setattr(worker, "_current_checks", lambda *a: [])
    monkeypatch.setattr(worker, "collect_review_comments", lambda *a: [{"id": 123}])
    monkeypatch.setattr(Git, "fetch_branch", lambda *a: False)
    result = worker.monitor_pr({"repo": "alice/repo", "number": 1, "branch": "deps/update"})
    assert "not found on the remote" in result
    assert not list(worker.cfg.workdir.rglob("large-build-artifact"))


@pytest.mark.parametrize("borrowed", [False, True])
def test_discovery_only_removes_checkouts_it_owns(worker, monkeypatch, borrowed):
    from sadoc.piworker import PiError

    session = Mock()
    session.start.side_effect = PiError("model offline")
    session.diagnostics.return_value = "{}"
    monkeypatch.setattr("sadoc.pipeline.PiSession", lambda **kw: session)
    monkeypatch.setattr(Git, "commit_sha", lambda *a: "original-commit")
    monkeypatch.setattr(Git, "clean", lambda *a, **kw: None)
    if borrowed:
        with worker.prepare_clone("alice", "repo", "main") as git:
            result = worker.discover_verify_command("alice", "repo", "main", git=git)
            assert git.path.is_dir()
    else:
        result = worker.discover_verify_command("alice", "repo", "main")
    assert result["status"] == "agent_failed"
    assert "model offline" in result["notes"]
    assert not list(worker.cfg.workdir.rglob("large-build-artifact"))


@pytest.mark.parametrize("agent_failed", [True, False])
def test_owned_discovery_retains_its_report_when_directory_removal_fails(
    worker, monkeypatch, agent_failed
):
    from sadoc import workspaces
    from sadoc.piworker import PiError, TurnResult

    session = Mock()
    session.diagnostics.return_value = "{}"
    if agent_failed:
        session.start.side_effect = PiError("model offline")
    session.prompt.return_value = TurnResult(
        text="STATUS: ready\nSETUP: true\nVERIFY: true\nPREREQUISITES: none\nCI_ONLY: none\nNOTES: validated recipe",
        settled=True,
    )
    session.bash.side_effect = [
        {"success": True, "data": {"exitCode": 0, "output": ""}},
        {"success": True, "data": {"exitCode": 0, "output": "checks passed"}},
        {"success": True, "data": {"exitCode": 0, "output": "checks passed"}},
    ]
    monkeypatch.setattr("sadoc.pipeline.PiSession", lambda **kw: session)
    monkeypatch.setattr(Git, "commit_sha", lambda *a: "original-commit")
    monkeypatch.setattr(Git, "clean", lambda *a, **kw: None)

    def fail_remove(path):
        raise PermissionError("cannot delete build output")

    monkeypatch.setattr(workspaces.shutil, "rmtree", fail_remove)
    result = worker.discover_verify_command("alice", "repo", "main")
    assert result["status"] == ("agent_failed" if agent_failed else "cleanup_failed")
    assert "cannot delete build output" in result["notes"]
    if agent_failed:
        assert "model offline" in result["notes"]
    else:
        assert result["verify_command"] == "true"
        assert "checks passed" in result["verify_output"]
