"""Exercise the run command with local checkouts and a fake pi provider."""

import json
import sys
from contextlib import closing
from io import BytesIO
from pathlib import Path
from threading import Barrier, Event, Lock
from unittest.mock import Mock

import pytest

from sadoc import cli, report, workspaces
from sadoc.config import ConfigError, load
from sadoc.detect import Update
from sadoc.gitops import Git
from sadoc.pipeline import JobResult, Worker
from sadoc.piworker import PiSession, TurnResult
from sadoc.state import State


@pytest.mark.parametrize(
    "section,workers,strict",
    [
        ("", 1, False),
        ("workers = 1", 1, False),
        ("workers = 2\nrequire_slots = true", 2, True),
        ("workers = 3", 3, False),
    ],
)
def test_run_options(tmp_path, section, workers, strict):
    path = tmp_path / "config.toml"
    path.write_text("[run]\n" + section)
    cfg = load(path)
    assert (cfg.run_workers, cfg.run_require_slots) == (workers, strict)


@pytest.mark.parametrize(
    "section",
    [
        "run = 2",
        "[run]\nworkers = 0",
        "[run]\nworkers = 4",
        "[run]\nworkers = true",
        "[run]\nworkers = 2.0",
        '[run]\nworkers = "2"',
        '[run]\nrequire_slots = "yes"',
    ],
)
def test_invalid_run_options(tmp_path, section):
    path = tmp_path / "config.toml"
    path.write_text(section)
    with pytest.raises(ConfigError, match="run"):
        load(path)


@pytest.fixture
def run_command(tmp_path, monkeypatch, capsys):
    gh = Mock()
    gh.default_branch.return_value = "main"
    gh.pr_list.return_value = []
    gh.api_paginate.return_value = []
    monkeypatch.setattr(cli, "Gh", lambda: gh)
    monkeypatch.setattr(cli, "_model_reachable", lambda _: True)
    monkeypatch.setattr(cli, "_model_slots", lambda _: 3)
    config = tmp_path / "config.toml"
    db = tmp_path / "state.db"
    workdir = tmp_path / "work"
    agent_dir = tmp_path / "agent"

    def run(repos, *, settings="[run]\nworkers = 2", dry_run=False):
        entries = [{"owner": "o", "name": name, "repo": f"o/{name}"} for name in repos]
        monkeypatch.setattr(cli.detect, "resolve_scope", lambda *_: entries)

        def detect(_self, owner, repo, _branch):
            return [
                Update(
                    repo=f"{owner}/{repo}",
                    ecosystem="pypi" if name == "two" else "npm",
                    name=name,
                    section="dependencies",
                    kind="runtime",
                    manifest="two/requirements.txt" if name == "two" else "one/package.json",
                    current="1.0.0",
                    latest="1.1.0",
                    bump_kind="minor",
                    constraint="^1.0.0",
                )
                for name in repos[repo]
            ]

        monkeypatch.setattr(Worker, "detect_repo", detect)
        config.write_text(
            f'workdir = "{workdir}"\nagent_dir = "{agent_dir}"\n'
            f'report_dir = "{tmp_path / "reports"}"\ndisable_dependabot = false\n'
            f"{settings}\n"
            '[models.bump]\nmodel = "mock-bump"\n'
            + "\n".join(f'[repos."o/{name}"]\nverify = "true"' for name in repos)
        )
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "sadoc",
                "--config",
                str(config),
                "--db",
                str(db),
                *(["--dry-run"] if dry_run else []),
                "run",
                "--json",
            ],
        )
        rc = cli.main()
        output = capsys.readouterr().out
        logs, separator, results = output.partition("\n[\n")
        return rc, logs, json.loads(separator + results) if separator else []

    return run, gh, db, workdir


def test_pool_dry_run_priority_repo_exclusion_and_failure(run_command, monkeypatch):
    run, gh, db, workdir = run_command
    first_pair = Barrier(2, timeout=10)
    second_in_verify = Event()
    third_in_pi = Event()
    lock = Lock()
    active = set()
    sessions = {}

    def clone(git, url, branch):
        repo = url.removesuffix(".git").split("/")[-1]
        git.path.mkdir()
        git._run("init", "-b", branch)
        git._run("config", "user.name", "Test")
        git._run("config", "user.email", "test@example.com")
        (git.path / "one").mkdir()
        (git.path / "one/package.json").write_text('{"dependencies":{"one":"^1.0.0"}}\n')
        (git.path / "two").mkdir()
        (git.path / "two/requirements.txt").write_text("two==1.0.0\n")
        git.commit(f"initial {repo}")

    class FakePi(PiSession):
        def start(self):
            self._write_models_json()

        def prompt(self, text, **kwargs):
            repo = self.cwd.rsplit("__", 1)[1]
            [name] = [name for name in ("one", "two") if f"| `{name}` |" in text]
            self.job = (repo, name)
            with lock:
                assert repo not in active
                active.add(repo)
                sessions[self.job] = (self._session_id, self.agent_dir, self.model.model)
            if self.job in {("a", "one"), ("b", "one")}:
                first_pair.wait()
            if self.job == ("a", "one"):
                assert second_in_verify.wait(timeout=10)
                with pytest.raises(workspaces.WorkspaceError, match="in use"):
                    workspaces.recover(workdir)
                raise TimeoutError("mock provider timed out")
            if self.job == ("a", "two"):
                third_in_pi.set()
            path = Path(self.cwd) / (
                "one/package.json" if name == "one" else "two/requirements.txt"
            )
            path.write_text(path.read_text().replace("1.0.0", "1.1.0"))
            return TurnResult(text="updated", settled=True)

        def bash(self, command, timeout):
            if self.job == ("b", "one"):
                second_in_verify.set()
                assert third_in_pi.wait(timeout=10)
            return {"success": True, "data": {"exitCode": 0}}

        def close(self):
            with lock:
                active.remove(self.job[0])

    monkeypatch.setattr(Git, "clone", clone)
    monkeypatch.setattr("sadoc.pipeline.PiSession", FakePi)
    rc, logs, results = run({"b": ["one"], "a": ["one", "two"], "c": ["one"]}, dry_run=True)
    assert rc == 0
    assert [(r["repo"], r["status"]) for r in results] == [
        ("o/a", "failed"),
        ("o/a", "skipped"),
        ("o/b", "skipped"),
        ("o/c", "skipped"),
    ], results
    assert sessions["a", "one"][0].endswith("-w1")
    assert sessions["a", "two"][0].endswith("-w1")
    assert sessions["b", "one"][0].endswith("-w2")
    assert len({session for session, _, _ in sessions.values()}) == 4
    assert sessions["a", "one"][1] != sessions["b", "one"][1]
    assert {model for _, _, model in sessions.values()} == {"mock-bump"}
    assert "[w1] --- o/a tier A" in logs
    assert "[w2] --- o/b tier A" in logs
    assert "opened 0 PR(s)" in logs
    gh.open_pr.assert_not_called()
    with closing(State(db)) as state:
        assert sorted(row[0] for row in state.conn.execute("SELECT status FROM attempts")) == [
            "dry-run",
            "dry-run",
            "dry-run",
            "failed",
        ]
        assert "mock provider timed out" in report.build(state)


def test_pool_budget_counts_submission_even_if_the_job_raises(run_command, monkeypatch):
    run, _, _, _ = run_command
    lock = Lock()
    started = []
    first_pair = Barrier(2, timeout=5)

    def process(worker, owner, repo, tier, updates, **kwargs):
        with lock:
            started.append(repo)
        if repo in ("b", "c"):
            first_pair.wait()
        if repo == "a":
            raise TimeoutError("failed before submitting a PR")
        worker._on_pr_submit()
        if repo == "b":
            raise TimeoutError("GitHub response lost")
        return JobResult(repo=f"{owner}/{repo}", tier=tier, status="pr_opened")

    monkeypatch.setattr(Worker, "process_repo", process)
    rc, logs, results = run({name: ["one"] for name in "abcdefgh"})
    assert rc == 0
    assert sorted(started) == list("abcdef")
    assert [result["status"] for result in results] == ["failed", "failed"] + ["pr_opened"] * 4
    assert "PR budget reached, stopping" in logs
    assert "opened 4 PR(s)" in logs


def test_one_worker_uses_legacy_order_and_logs(run_command, monkeypatch):
    run, _, _, _ = run_command
    monkeypatch.setattr(cli, "time", Mock(strftime=lambda *args: "01:00:00"))
    probe = Mock(side_effect=AssertionError("sequential runs do not probe slots"))
    monkeypatch.setattr(cli, "_model_slots", probe)

    def process(worker, owner, repo, tier, updates, **kwargs):
        return JobResult(
            repo=f"{owner}/{repo}", tier=tier, status="skipped", detail=updates[0].name
        )

    monkeypatch.setattr(Worker, "process_repo", process)
    repos = {"b": ["one"], "a": ["one", "two"]}
    explicit = run(repos, settings="[run]\nworkers = 1")
    assert explicit == run(repos, settings="")
    rc, logs, results = explicit
    assert rc == 0
    assert [r["detail"] for r in results] == ["one", "two", "one"]
    assert logs.splitlines() == [
        "[01:00:00] 2 repos have outdated dependencies",
        "[01:00:00] o/a: priority 2, 2 group(s)",
        "[01:00:00] --- o/a tier A: 1 updates (one/package.json)",
        "[01:00:00]     -> skipped one",
        "[01:00:00] --- o/a tier A: 1 updates (two/requirements.txt)",
        "[01:00:00]     -> skipped two",
        "[01:00:00] o/b: priority 1, 1 group(s)",
        "[01:00:00] --- o/b tier A: 1 updates (one/package.json)",
        "[01:00:00]     -> skipped one",
        "[01:00:00] opened 0 PR(s)",
    ]
    probe.assert_not_called()


@pytest.mark.parametrize(
    "slots,strict,expected", [(1, False, 0), (1, True, 1), (None, True, 1), (2, True, 0)]
)
def test_parallel_slot_check(run_command, monkeypatch, slots, strict, expected):
    run, _, _, _ = run_command
    probe = Mock(return_value=slots)
    monkeypatch.setattr(cli, "_model_slots", probe)
    rc, logs, _ = run({}, settings=f"[run]\nworkers = 2\nrequire_slots = {str(strict).lower()}")
    assert rc == expected
    assert ("[warning]" in logs) == (slots != 2 and not strict)
    probe.assert_called_once()


@pytest.mark.parametrize(
    "props,slots",
    [
        ({"total_slots": 4}, 4),
        ({"total_slots": True}, None),
        ({"total_slots": 0}, None),
        ({}, None),
    ],
)
def test_model_slots_reads_llama_props(monkeypatch, props, slots):
    request = Mock(return_value=BytesIO(json.dumps(props).encode()))
    monkeypatch.setattr("urllib.request.urlopen", request)
    assert cli._model_slots("http://localhost:8094/v1/") == slots
    request.assert_called_once_with("http://localhost:8094/props", timeout=10)
