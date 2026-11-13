"""Acknowledgements belong on the original comment, without noisy PR replies."""

import subprocess
from types import SimpleNamespace
from unittest.mock import Mock, call

import pytest

from sadoc.config import Config
from sadoc.gh import Gh, GhError
from sadoc.gitops import Git
from sadoc.pipeline import Worker
from sadoc.state import State


@pytest.fixture
def review(tmp_path, monkeypatch):
    path = tmp_path / "repo"
    path.mkdir()
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    git = Git(path)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=path, check=True)
    (path / "README.md").write_text("Before review\n")
    git.commit("Initial commit")
    monkeypatch.setattr(git, "fetch_branch", lambda branch: True)
    monkeypatch.setattr(git, "create_branch", lambda *args, **kwargs: None)
    monkeypatch.setattr(git, "push", Mock())

    client = Gh.__new__(Gh)
    client.run = Mock(return_value="{}")
    client.pr_view = Mock(return_value={"state": "OPEN"})
    client.pr_checks = Mock(return_value=[])
    client.default_branch = Mock(return_value="main")
    client.pr_review_comments = Mock(
        return_value=[
            {
                "id": 101,
                "body": "Please check this.",
                "path": "README.md",
                "line": 1,
                "user": {"login": "reviewer"},
                "created_at": "2026-11-13T10:00:00Z",
            }
        ]
    )
    client.pr_issue_comments = Mock(
        return_value=[
            {
                "id": 202,
                "body": "Thanks!",
                "user": {"login": "reviewer"},
                "created_at": "2026-11-13T10:01:00Z",
            }
        ]
    )
    client.pr_review_threads = Mock(
        return_value=[
            {
                "id": "thread-1",
                "isResolved": False,
                "comments": {"nodes": [{"body": "Please check this."}]},
            }
        ]
    )
    client.resolve_review_thread = Mock()
    client.pr_comment = Mock()
    session = Mock()
    session.prompt.return_value = SimpleNamespace(text="Acknowledged.")
    monkeypatch.setattr("sadoc.pipeline.PiSession", lambda **kwargs: session)
    state = State(tmp_path / "state.db")
    worker = Worker(Config(), client, state)
    monkeypatch.setattr(worker, "prepare_clone", lambda *args: git)
    row = {"repo": "o/r", "number": 7, "branch": "deps/update", "tier": "A"}
    state.record_pr("o/r", 7, branch="deps/update", tier="A", state="open")
    yield SimpleNamespace(worker=worker, gh=client, git=git, session=session, row=row)
    state.close()


def test_no_changes_reacts_to_each_original_comment_without_reply_or_resolution(review):
    review.worker.monitor_pr(review.row)

    review.gh.pr_comment.assert_not_called()
    review.gh.resolve_review_thread.assert_not_called()
    review.git.push.assert_not_called()
    assert [c for c in review.gh.run.call_args_list if "/reactions" in c.args[1]] == [
        call(
            "api",
            "repos/o/r/pulls/comments/101/reactions",
            "--method",
            "POST",
            "--raw-field",
            "content=+1",
            check=True,
        ),
        call(
            "api",
            "repos/o/r/issues/comments/202/reactions",
            "--method",
            "POST",
            "--raw-field",
            "content=+1",
            check=True,
        ),
    ]
    assert review.worker.state.known_comment_ids("o/r", 7) == {"101", "202"}

    review.gh.run.reset_mock()
    review.session.prompt.reset_mock()
    review.worker.monitor_pr(review.row)
    review.session.prompt.assert_not_called()
    assert not any("/reactions" in c.args[1] for c in review.gh.run.call_args_list)


def test_pushed_changes_still_get_a_substantive_reply(review):
    def edit(prompt):
        (review.git.path / "README.md").write_text("After review\n")
        return SimpleNamespace(text="Corrected the documentation example.")

    review.session.prompt.side_effect = edit
    review.worker.monitor_pr(review.row)

    review.git.push.assert_called_once_with("deps/update")
    review.gh.resolve_review_thread.assert_called_once_with("o", "r", "thread-1")
    review.gh.pr_comment.assert_called_once()
    assert "Corrected the documentation example." in review.gh.pr_comment.call_args.args[3]
    assert not any("/reactions" in c.args[1] for c in review.gh.run.call_args_list)


def test_failed_reaction_does_not_fall_back_to_a_comment_or_skip_other_comments(review):
    def run(*args, **kwargs):
        if args[1] == "repos/o/r/pulls/comments/101/reactions":
            raise GhError("Reaction rejected")
        return "{}"

    review.gh.run.side_effect = run
    review.worker.monitor_pr(review.row)

    review.gh.pr_comment.assert_not_called()
    assert any(
        c.args[1] == "repos/o/r/issues/comments/202/reactions" for c in review.gh.run.call_args_list
    )


def test_dry_run_does_not_acknowledge_comments(review):
    review.worker.monitor_pr(review.row, dry_run=True)

    review.gh.pr_comment.assert_not_called()
    review.gh.resolve_review_thread.assert_not_called()
    assert not any("/reactions" in c.args[1] for c in review.gh.run.call_args_list)
    assert review.worker.state.known_comment_ids("o/r", 7) == set()
