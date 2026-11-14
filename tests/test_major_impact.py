"""Breaking updates carry a codebase-specific assessment even when checks pass."""

import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from sadoc import sanitize, semver
from sadoc.config import Config
from sadoc.detect import Update
from sadoc.gitops import Git
from sadoc.pipeline import Worker
from sadoc.piworker import PiError, TurnResult
from sadoc.state import State

ASSESSMENT = """### widget 1.0.0 → 2.0.0
Sources: [Migration guide](https://example.org/widget/migrate-v2).
Impact: `src/client.py:connect` uses the removed `connect_sync` API.
Migration: replace it with `connect`; integration tests must check timeout behavior.
Uncertainty: unit tests do not exercise production timeouts.
"""


def update(name="widget", current="1.0.0", latest="2.0.0"):
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
    state = State(tmp_path / "state.db")
    gh = Mock()
    gh.default_branch.return_value = "main"
    gh.open_pr.return_value = {"url": "https://github.com/o/r/pull/7"}
    worker = Worker(Config(disable_dependabot=False), gh, state)
    monkeypatch.setattr(worker, "prepare_clone", lambda *a: git)
    monkeypatch.setattr(worker, "ensure_build_setup", lambda *a: {"status": "ok"})
    worker.run_verify = Mock(return_value=(True, "all checks passed"))
    worker.watch_ci = Mock(return_value="pass")
    worker.may_auto_merge = Mock(return_value=(False, "requires review"))

    class Session:
        report = ASSESSMENT
        updates = [update()]
        assessment_edit = False
        events = []
        prompts = {}
        transcript = []

        def __init__(self, **kwargs):
            self.allow_network = kwargs["allow_network"]

        def start(self):
            pass

        def prompt(self, text, **kwargs):
            if "## Breaking updates to assess" in text:
                self.events.append("impact")
                self.prompts["impact"] = text
                self.prompts["impact_read_only"] = kwargs.get("read_only", False)
                assert not git.changed_files()
                if isinstance(self.report, Exception):
                    raise self.report
                if self.assessment_edit:
                    (root / "unexpected.txt").write_text("not an assessment")
                return TurnResult(text=self.report, settled=True)
            if "## Updates to apply" in text:
                assert not kwargs.get("read_only")
                self.events.append("bump")
                (root / "package.json").write_text(
                    json.dumps({"dependencies": {u.name: f"^{u.latest}" for u in self.updates}})
                    + "\n"
                )
                return TurnResult(text="NOTES: Updated widget.", settled=True)
            if "## Failure output" in text:
                self.events.append("migrate")
                self.prompts["migrate"] = text
                return TurnResult(text="NOTES: Replaced connect_sync with connect.", settled=True)
            self.events.append("prnote")
            self.prompts["prnote"] = text
            return TurnResult(text="Updated widget to the requested version.", settled=True)

        def close(self):
            pass

    monkeypatch.setattr("sadoc.pipeline.PiSession", Session)

    def run(tier, updates):
        Session.updates = updates
        (root / "package.json").write_text(
            json.dumps({"dependencies": {u.name: f"^{u.current}" for u in updates}}) + "\n"
        )
        if git.changed_files():
            git.commit("set baseline versions")
        return worker.process_repo("o", "r", tier, updates)

    yield SimpleNamespace(worker=worker, git=git, gh=gh, session=Session, state=state, run=run)
    state.close()


@pytest.mark.parametrize(
    "tier,current,latest",
    [
        ("B", "1.0.0", "2.0.0"),
        ("C", "1.0.0", "2.0.0"),
        ("B", "0.1.0", "0.2.0"),
    ],
)
def test_breaking_updates_are_assessed_before_bump_and_published(project, tier, current, latest):
    result = project.run(tier, [update(current=current, latest=latest)])

    assert result.status == "pr_opened", result.detail
    assert project.session.events == ["impact", "bump", "prnote"]
    assert project.session.prompts["impact_read_only"] is True
    body = project.gh.open_pr.call_args.args[3]
    assert ASSESSMENT.strip() in body
    assert "before changes" in body.lower()
    assert ASSESSMENT.strip() in project.state.pr_details("o/r", 7)["agent_notes"]
    assert current in project.session.prompts["impact"]
    assert latest in project.session.prompts["impact"]


def test_mixed_groups_assess_only_breaking_members(project):
    project.run("B", [update(), update("minor-lib", "1.0.0", "1.1.0")])
    assert "`widget`" in project.session.prompts["impact"]
    assert "minor-lib" not in project.session.prompts["impact"]


def test_nonbreaking_updates_skip_assessment(project):
    result = project.run("A", [update(latest="1.1.0")])
    assert result.status == "pr_opened", result.detail
    assert project.session.events == ["bump", "prnote"]
    assert "Major-version impact" not in project.gh.open_pr.call_args.args[3]


@pytest.mark.parametrize("report", ["NONE", "The agent used sadoc to inspect the changelog."])
def test_missing_or_unsafe_assessment_is_explicit_in_pr(project, report):
    project.session.report = report
    result = project.run("B", [update()])
    assert result.status == "pr_opened", result.detail
    body = project.gh.open_pr.call_args.args[3]
    assert "assessment is unavailable" in body
    assert not sanitize.contains_leak(body)


def test_unavailable_sources_remain_visible_without_network(project):
    project.worker.cfg.allow_network = False
    project.session.report = "Changelog unavailable offline. Runtime compatibility is unverified."
    result = project.run("B", [update()])
    assert result.status == "pr_opened", result.detail
    assert project.session.report in project.gh.open_pr.call_args.args[3]
    assert "disabled" in project.session.prompts["impact"]


@pytest.mark.parametrize("error", ["assessment failed", "pi returned an empty response"])
def test_assessment_failure_stops_before_bump_and_publication(project, error):
    project.session.report = PiError(error)
    result = project.run("B", [update()])
    assert result.status == "failed"
    assert error in result.detail
    assert project.session.events == ["impact"]
    project.gh.open_pr.assert_not_called()
    project.git.push.assert_not_called()


def test_assessment_reaches_migration_and_migration_work_reaches_summary(project):
    project.worker.run_verify.side_effect = [(False, "connect_sync is missing"), (True, "passed")]
    result = project.run("B", [update()])
    assert result.status == "pr_opened", result.detail
    assert ASSESSMENT.strip() in project.session.prompts["migrate"]
    assert "Replaced connect_sync with connect" in project.session.prompts["prnote"]


def test_assessment_cannot_smuggle_edits_into_the_update(project):
    project.session.assessment_edit = True
    result = project.run("B", [update()])
    assert result.status == "failed"
    assert "assessment modified" in result.detail
    assert "unexpected.txt" in result.detail
    assert project.session.events == ["impact"]
    project.gh.open_pr.assert_not_called()
    project.git.push.assert_not_called()


def test_markdown_whitespace_does_not_discard_the_assessment(project):
    project.session.report = (
        "### widget\n\n- **Sources**:\n"
        "  - [Migration guide](https://example.org/widget/migrate-v2)\n\n"
        "- **Codebase impact**: `src/client.py:connect` uses a removed API.\n"
    )
    result = project.run("B", [update()])
    assert result.status == "pr_opened", result.detail
    body = project.gh.open_pr.call_args.args[3]
    assert "[Migration guide](https://example.org/widget/migrate-v2)" in body
    assert "`src/client.py:connect` uses a removed API." in body
    assert "assessment is unavailable" not in body
