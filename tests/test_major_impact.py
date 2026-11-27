"""Breaking updates carry a codebase-specific assessment even when checks pass."""

import json
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock
from urllib.error import URLError

import pytest

from sadoc import registry, sanitize, semver
from sadoc.config import Config, ModelSpec, load
from sadoc.detect import Update
from sadoc.gitops import Git
from sadoc.pipeline import Worker
from sadoc.piworker import PiError, PiPromptTimeout, TurnResult
from sadoc.report import build
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
    monkeypatch.setattr(worker, "prepare_clone", lambda *a: nullcontext(git))
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

        def __init__(self, **kwargs):
            self.allow_network = kwargs["allow_network"]
            self.model = kwargs["model"]
            self.transcript = []
            self.prompts["http_idle_timeout_s"] = kwargs["http_idle_timeout_s"]

        def start(self):
            pass

        def prompt(self, text, **kwargs):
            if "## Breaking updates to assess" in text:
                self.events.append("impact")
                self.transcript.append({"phase": "impact", "effort": self.model.reasoning_effort})
                self.prompts["impact"] = text
                self.prompts["impact_options"] = kwargs
                assert not git.changed_files()
                if isinstance(self.report, Exception):
                    raise self.report
                if self.assessment_edit:
                    (root / "unexpected.txt").write_text("not an assessment")
                return TurnResult(text=self.report, settled=True)
            if "## Updates to apply" in text:
                assert not kwargs.get("read_only")
                self.events.append("bump")
                self.transcript.append({"phase": "bump", "effort": self.model.reasoning_effort})
                self.prompts["bump_options"] = kwargs
                (root / "package.json").write_text(
                    json.dumps({"dependencies": {u.name: f"^{u.latest}" for u in self.updates}})
                    + "\n"
                )
                return TurnResult(text="NOTES: Updated widget.", settled=True)
            if "## Failure output" in text:
                self.events.append("migrate")
                self.transcript.append({"phase": "migrate", "effort": self.model.reasoning_effort})
                self.prompts["migrate"] = text
                return TurnResult(text="NOTES: Replaced connect_sync with connect.", settled=True)
            self.events.append("prnote")
            self.prompts["prnote"] = text
            self.prompts["prnote_options"] = kwargs
            return TurnResult(text="Updated widget to the requested version.", settled=True)

        def diagnostics(self):
            return "{}"

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
    assert project.session.prompts["impact_options"] == {
        "read_only": True,
        "timeout": 7200,
        "idle_timeout": 600,
    }
    body = project.gh.open_pr.call_args.args[3]
    assert ASSESSMENT.strip() in body
    assert "before changes" in body.lower()
    assert ASSESSMENT.strip() in project.state.pr_details("o/r", 7)["agent_notes"]
    assert current in project.session.prompts["impact"]
    assert latest in project.session.prompts["impact"]


@pytest.mark.parametrize("network, available", [(True, True), (True, False), (False, False)])
def test_docker_action_release_notes_reach_read_only_assessment(
    project, monkeypatch, network, available
):
    project.worker.cfg.allow_network = network
    action = update("docker/build-push-action", "v5", "v7")
    action.ecosystem = action.kind = "action"
    releases = [
        ("v8.0.0", "Unrelated future version"),
        ("v7.2.0", "Target-line patch"),
        ("v7.0.0", "New target runtime"),
        ("v6.1.0", "Intermediate minor release"),
        ("v6.0.0", "Intermediate breaking change"),
        ("v5.3.0", "Old major line"),
    ]
    fetch = Mock(
        return_value=json.dumps([{"tag_name": tag, "body": body} for tag, body in releases]),
        side_effect=None if available else URLError("unavailable"),
    )
    monkeypatch.setattr(registry, "_fetch", fetch)

    result = project.run("B", [action])

    assert result.status == "pr_opened", result.detail
    assert project.session.prompts["impact_options"]["read_only"]
    prompt = project.session.prompts["impact"]
    if network:
        assert fetch.call_count == 1
        url = fetch.call_args.args[0]
        assert url.startswith("https://api.github.com/repos/docker/build-push-action/releases?")
    else:
        fetch.assert_not_called()
    if available:
        assert "Intermediate breaking change" in prompt
        assert "New target runtime" in prompt
        assert "Target-line patch" in prompt
        assert "https://github.com/docker/build-push-action/releases/tag/v6.0.0" in prompt
        assert "Unrelated future version" not in prompt
        assert "Old major line" not in prompt
        assert "moving major pins" in prompt
    elif network:
        assert "Public GitHub release lookup failed" in prompt
    else:
        assert "Network access is disabled" in prompt


def test_configured_budgets_reach_update_and_note_sessions(project, tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(
        "[timeouts]\nprompt_wall_clock = 5400\nprompt_inactivity = 900\nnote_generation = 1800\n"
    )
    project.worker.cfg.timeouts = load(path).timeouts
    result = project.run("B", [update()])

    assert result.status == "pr_opened", result.detail
    assert project.session.prompts["http_idle_timeout_s"] == 900
    assert project.session.prompts["impact_options"] == {
        "read_only": True,
        "timeout": 5400,
        "idle_timeout": 900,
    }
    assert project.session.prompts["bump_options"] == {"timeout": 5400, "idle_timeout": 900}
    assert project.session.prompts["prnote_options"] == {"timeout": 1800, "idle_timeout": 900}


def test_mixed_groups_assess_only_breaking_members(project):
    project.run("B", [update(), update("minor-lib", "1.0.0", "1.1.0")])
    assert "`widget`" in project.session.prompts["impact"]
    assert "minor-lib" not in project.session.prompts["impact"]


def test_nonbreaking_updates_skip_assessment(project):
    result = project.run("A", [update(latest="1.1.0")])
    assert result.status == "pr_opened", result.detail
    assert project.session.events == ["bump", "prnote"]
    assert "Major-version impact" not in project.gh.open_pr.call_args.args[3]


def test_effort_changes_for_migration_and_ci_repair(project, monkeypatch):
    project.worker.cfg.default_model.reasoning_effort = "medium"
    project.worker.cfg.models["migrate"] = ModelSpec(reasoning_effort=0)
    project.worker.cfg.models["fixci"] = ModelSpec(reasoning_effort="low")
    project.worker.run_verify.side_effect = [(False, "needs migration"), (True, "fixed")]
    project.worker.watch_ci.side_effect = ["fail", "pass"]
    project.worker._failing_workflow = Mock(return_value=None)
    project.worker.base_branch_is_also_failing = Mock(return_value=(False, ""))
    original_prompt = project.session.prompt

    def prompt(session, text, **kwargs):
        if "## Failure output" in text:
            assert session.model.reasoning_effort == 0
        if "## Updates to apply" in text:
            assert session.model.reasoning_effort == "medium"
        return original_prompt(session, text, **kwargs)

    def fix_ci(owner, repo, number, branch, base, updates, git, session):
        assert session.model.reasoning_effort == "low"
        return "fixed"

    monkeypatch.setattr(project.session, "prompt", prompt)
    monkeypatch.setattr(project.worker, "fix_ci", fix_ci)
    result = project.run("A", [update(latest="1.1.0")])

    assert result.status == "pr_opened", result.detail
    assert result.ci_state == "pass"
    summary = next(line for line in build(project.state).splitlines() if "Reasoning effort" in line)
    assert "reasoning_effort=0 (task migrate)" in summary
    assert "reasoning_effort=medium (task bump)" in summary
    assert "reasoning_effort=low (task fixci)" in summary
    assert json.loads(project.state.pr_details("o/r", 7)["transcript"]) == [
        {"phase": "bump", "effort": "medium"},
        {"phase": "migrate", "effort": 0},
    ]


def test_breaking_update_uses_bump_effort_for_manifest_edit(project):
    project.worker.cfg.models["bump"] = ModelSpec(reasoning_effort="low")
    project.worker.cfg.models["migrate"] = ModelSpec(reasoning_effort="medium")

    result = project.run("B", [update()])

    assert result.status == "pr_opened", result.detail
    assert json.loads(project.state.pr_details("o/r", 7)["transcript"]) == [
        {"phase": "impact", "effort": "medium"},
        {"phase": "bump", "effort": "low"},
    ]


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


@pytest.mark.parametrize("error, attempts", [(PiError, 1), (PiPromptTimeout, 2)])
def test_failed_migration_keeps_local_diagnostics_and_verification(
    project, monkeypatch, error, attempts
):
    project.worker.run_verify.return_value = (False, "original build failure")
    original = project.session.prompt
    sessions = []

    def prompt(self, text, **kwargs):
        if "## Failure output" in text:
            sessions.append(self)
            raise error("migration did not finish")
        return original(self, text, **kwargs)

    monkeypatch.setattr(project.session, "prompt", prompt)
    monkeypatch.setattr(
        project.session,
        "diagnostics",
        lambda self: (
            '{"tools": [{"output": "last migration output"}]}' if self in sessions else "{}"
        ),
        raising=False,
    )
    result = project.run("B", [update()])
    assert result.status == "failed"
    assert len(set(sessions)) == len(sessions) == attempts
    assert project.worker.run_verify.call_count == attempts + (error is PiPromptTimeout)
    records = project.state.conn.execute(
        "select data from events where repo='o/r' and data is not null"
    ).fetchall()
    evidence = "\n".join(r[0] for r in records)
    assert "original build failure" in evidence
    assert evidence.count("last migration output") == attempts
    project.gh.open_pr.assert_not_called()


@pytest.mark.parametrize("recovery", ["unneeded", "settled", "timed_out"])
def test_migration_timeout_recovers_existing_edits(project, monkeypatch, recovery):
    first_session = None
    closed = []
    original = project.session.prompt

    def prompt(self, text, **kwargs):
        nonlocal first_session
        if "## Failure output" in text:
            if first_session is None:
                first_session = self
                (project.git.path / "compat.py").write_text("from widget import connect\n")
                raise PiPromptTimeout("pi prompt timed out")
            assert self is not first_session
            assert first_session in closed
            assert "documentation check failed" in text
            assert "original type error" not in text
            assert kwargs["timeout"] == 7200
            assert kwargs["idle_timeout"] == 600
            if recovery == "timed_out":
                (project.git.path / "docs.txt").write_text("Updated widget documentation\n")
                raise PiPromptTimeout("recovery timed out")
        return original(self, text, **kwargs)

    monkeypatch.setattr(project.session, "prompt", prompt)
    monkeypatch.setattr(project.session, "close", lambda self: closed.append(self))
    verification = [(False, "original type error")]
    if recovery != "unneeded":
        verification.append((False, "documentation check failed"))
    project.worker.run_verify.side_effect = [*verification, (True, "passed")]

    result = project.run("B", [update()])
    assert result.status == "pr_opened", result.detail
    assert "compat.py" in result.changes
    assert project.git._run("show", "HEAD:compat.py").strip() == "from widget import connect"
    assert project.session.events.count("migrate") == (recovery == "settled")
    assert len(closed) == (3 if recovery == "timed_out" else 2)
    if recovery == "timed_out":
        assert project.git._run("show", "HEAD:docs.txt").strip() == "Updated widget documentation"


def test_leak_check_includes_new_migration_files(project):
    (project.git.path / "compat.py").write_text('ORIGIN = "generated by the agent"\n')

    result = project.worker._check_for_leaks(project.git, project.git.changed_files())

    assert result["blocking"]


@pytest.mark.parametrize("failure", ["diagnostics", "persistence"])
def test_diagnostic_failure_does_not_replace_original_error(project, monkeypatch, failure):
    project.session.report = PiError("original prompt failure")
    if failure == "diagnostics":

        def diagnostics(self):
            raise RuntimeError("diagnostics unavailable")

        monkeypatch.setattr(project.session, "diagnostics", diagnostics)
    else:
        log = project.state.log

        def failing_log(message, **kwargs):
            if message == "pipeline session failed":
                raise RuntimeError("database temporarily locked")
            return log(message, **kwargs)

        monkeypatch.setattr(project.state, "log", failing_log)

    result = project.run("B", [update()])
    assert result.status == "failed"
    assert result.detail == "PiError: original prompt failure"


NPM_NODE_FAILURE = """exit 1
npm error code EBADENGINE
npm error engine Not compatible with your version of node/npm: jsdom@31.0.1
npm error notsup Required: {"node":"^22.22.2 || ^24.15.0 || >=26.0.0"}
npm error notsup Actual:   {"npm":"10.9.2","node":"v22.14.0"}
"""


@pytest.mark.parametrize("recovered", [True, False])
def test_node_engine_failure_attempts_recipe_recovery_before_migration(project, recovered):
    worker = project.worker
    project.state.record_setup("o/r", "npm test", setup_command="npm ci")
    worker.run_verify.return_value = (False, NPM_NODE_FAILURE)
    if recovered:
        worker.run_verify.side_effect = [(False, NPM_NODE_FAILURE), (True, "passed")]
    recovery = Mock(return_value=recovered)
    worker._recover_node_recipe = recovery

    result = project.run("B", [update()])

    recovery.assert_called_once()
    assert "migrate" not in project.session.events
    if recovered:
        assert result.status == "pr_opened"
        assert worker.run_verify.call_count == 2
    else:
        assert result.status == "failed"
        assert "jsdom@31.0.1" in result.detail
        assert "22.22.2" in result.detail
        assert "22.14.0" in result.detail
        assert project.state.setup("o/r")["status"] == "stale"


def test_npm_engine_error_with_two_requirements_does_not_select_node(project):
    failure = NPM_NODE_FAILURE.replace(
        '"node":"^22.22.2 || ^24.15.0 || >=26.0.0"', '"node":">=20","npm":">=100"'
    )
    project.state.record_setup("o/r", "npm test", setup_command="npm ci")
    project.worker.run_verify.return_value = (False, failure)

    result = project.run("B", [update()])

    assert result.status == "failed"
    assert "npm >=100 (actual 10.9.2)" in result.detail
    assert "Node >=20 (actual 22.14.0)" in result.detail
    assert project.worker.run_verify.call_count == 1
    assert "migrate" not in project.session.events
