"""Pushing a branch whose deterministic name already exists on the remote.

`deps/<package>-<version>` is regenerated every run, so a branch of the same
name can already exist on the remote: an earlier run that pushed and died
before opening the PR, or a PR that was closed unmerged. The pipeline must
skip what a PR already covers and never fail the night over a leftover
branch it is not allowed to overwrite.
"""

import json
from types import SimpleNamespace
from unittest.mock import Mock, call

import pytest

from sadoc import semver
from sadoc.config import Config
from sadoc.detect import Update
from sadoc.gitops import Git, GitError
from sadoc.pipeline import PrGroup, Worker
from sadoc.piworker import TurnResult
from sadoc.state import State

BRANCH = "deps/widget-1.1.0"
REJECTED = (
    f"git push --set-upstream origin {BRANCH} failed: To https://github.com/o/r.git\n"
    f" ! [rejected]        {BRANCH} -> {BRANCH} (fetch first)\n"
    "error: failed to push some refs to 'https://github.com/o/r.git'"
)


def update(name="widget", current="1.0.0", latest="1.1.0"):
    return Update(
        repo="o/r",
        manifest="package.json",
        ecosystem="npm",
        name=name,
        section="dependencies",
        kind="runtime",
        current=current,
        latest=latest,
        bump_kind=semver.classify(current, latest),
        constraint=f"^{current}",
    )


@pytest.fixture
def project(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    git = Git(root)
    git._run("init", "-b", "main")
    git._run("config", "user.name", "Test")
    git._run("config", "user.email", "test@example.com")
    (root / "package.json").write_text('{"dependencies":{"widget":"^1.0.0"}}\n')
    git.commit("initial")
    monkeypatch.setattr(git, "push", Mock())
    monkeypatch.setattr(git, "has_remote_branch", Mock(return_value=False))
    state = State(tmp_path / "state.db")
    gh = Mock()
    gh.default_branch.return_value = "main"
    gh.pr_list.return_value = []
    gh.open_pr.return_value = {"url": "https://github.com/o/r/pull/7"}
    worker = Worker(Config(disable_dependabot=False), gh, state)
    monkeypatch.setattr(worker, "prepare_clone", lambda *a: git)
    monkeypatch.setattr(worker, "ensure_build_setup", lambda *a: {"status": "ok"})
    worker.run_verify = Mock(return_value=(True, "all checks passed"))
    worker.watch_ci = Mock(return_value="pass")
    worker.may_auto_merge = Mock(return_value=(False, "requires review"))

    class Session:
        events: list[str] = []
        transcript: list = []

        def __init__(self, **kwargs):
            pass

        def start(self):
            pass

        def prompt(self, text, **kwargs):
            if "## Updates to apply" in text:
                self.events.append("bump")
                (root / "package.json").write_text(
                    json.dumps({"dependencies": {"widget": "^1.1.0"}}) + "\n"
                )
                return TurnResult(text="NOTES: Updated widget.", settled=True)
            self.events.append("prnote")
            return TurnResult(text="Updated widget to the requested version.", settled=True)

        def diagnostics(self):
            return "{}"

        def close(self):
            pass

    monkeypatch.setattr("sadoc.pipeline.PiSession", Session)

    def run():
        Session.events = []
        group = PrGroup(tier="B", updates=[update()])
        result = worker.process_repo("o", "r", "B", group.updates, group=group)
        return result

    yield SimpleNamespace(worker=worker, git=git, gh=gh, session=Session, state=state, run=run)
    state.close()


def test_existing_deps_pr_matches_generated_branch_names(project):
    """The repo-level dedup must recognise the branch names sadoc generates."""
    project.gh.pr_list.return_value = [
        {"headRefName": "feature/x", "number": 1},
        {"headRefName": BRANCH, "number": 2},
    ]
    pr = project.worker.existing_deps_pr("o", "r")
    assert pr is not None and pr["number"] == 2

    project.gh.pr_list.return_value = [{"headRefName": "feature/x", "number": 3}]
    assert project.worker.existing_deps_pr("o", "r") is None


def test_skips_when_pr_already_open_for_the_branch(project):
    project.gh.pr_list.return_value = [{"headRefName": BRANCH, "number": 7}]
    result = project.run()
    assert result.status == "skipped"
    assert "7" in result.detail
    assert project.session.events == []  # no model work was spent
    project.git.push.assert_not_called()


def test_skips_when_a_pr_appeared_by_push_time(project):
    """The early check can race a PR into existence; the push must not fight it."""
    project.git.push.side_effect = GitError(REJECTED)
    project.gh.pr_list.side_effect = [[], [{"headRefName": BRANCH, "number": 9}]]
    result = project.run()
    assert result.status == "skipped"
    assert "9" in result.detail
    project.gh.open_pr.assert_not_called()
    project.git.has_remote_branch.assert_not_called()


def test_orphan_branch_falls_back_to_the_next_free_name(project):
    """A rejected push with no PR is a leftover branch; push under a free name."""
    project.git.push.side_effect = [GitError(REJECTED), ""]
    result = project.run()
    assert result.status == "pr_opened"
    assert project.git.push.call_args_list == [call(BRANCH), call(f"{BRANCH}-2")]
    assert project.gh.open_pr.call_args[0][4] == f"{BRANCH}-2"
    branches = [row["branch"] for row in project.state.open_prs()]
    assert branches == [f"{BRANCH}-2"]


def test_all_alternative_names_taken_fails_cleanly(project):
    project.git.push.side_effect = GitError(REJECTED)
    project.git.has_remote_branch.return_value = True
    result = project.run()
    assert result.status == "failed"
    assert "GitError" in result.detail
    project.gh.open_pr.assert_not_called()


def test_a_push_failure_that_is_not_a_rejection_is_not_retried(project):
    project.git.push.side_effect = GitError(
        "git push --set-upstream origin deps/widget-1.1.0 failed: could not read from remote repository"
    )
    result = project.run()
    assert result.status == "failed"
    project.git.has_remote_branch.assert_not_called()
    project.gh.open_pr.assert_not_called()
