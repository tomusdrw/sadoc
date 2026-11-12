"""Configuration loading, state persistence and the notification layer."""

from __future__ import annotations

import textwrap

from sadoc import alerts, notify
from sadoc import config as config_mod
from sadoc import state as state_mod


def write_config(tmp_path, body: str):
    path = tmp_path / "config.toml"
    path.write_text(textwrap.dedent(body))
    return path


def test_config_defaults(tmp_path):
    cfg = config_mod.load(tmp_path / "missing.toml")
    assert cfg.owner == ""
    assert cfg.notify["enabled"] is False
    assert cfg.auto_merge_tiers == ["A"]


def test_config_reads_sections(tmp_path):
    path = write_config(
        tmp_path,
        """
        owner = "someone"

        [scope]
        skip_forks = false
        exclude = ["a", "b"]

        [merge]
        auto_merge_tier = ["A", "B"]

        [notify]
        enabled = true
        backend = "smtp"
        smtp_host = "smtp.example.com"

        [models.migrate]
        model = "big-model"
        base_url = "http://localhost:9000/v1"
        """,
    )
    cfg = config_mod.load(path)
    assert cfg.owner == "someone"
    assert cfg.skip_forks is False
    assert cfg.exclude == ["a", "b"]
    assert cfg.auto_merge_tiers == ["A", "B"]
    assert cfg.notify["enabled"] is True
    assert cfg.notify["backend"] == "smtp"
    assert cfg.model_for("migrate").model == "big-model"
    assert cfg.model_for("bump").model == cfg.base_model.model


def test_state_records_and_queries(tmp_path):
    st = state_mod.State(tmp_path / "state.db")
    try:
        st.upsert_repo("owner", "repo", default_branch="main", is_fork=0)
        st.set_scope("owner", "repo", True, "in scope")
        assert [r["name"] for r in st.scoped_repos()] == ["repo"]

        st.record_dep(
            "owner/repo",
            {
                "manifest": "package.json",
                "ecosystem": "npm",
                "name": "react",
                "section": "dependencies",
                "kind": "runtime",
                "current": "1.0.0",
                "latest": "2.0.0",
                "bump_kind": "major",
                "constraint": "^1.0.0",
                "action": "manifest",
                "installed": None,
            },
        )
        deps = st.outdated_deps("owner/repo")
        assert len(deps) == 1
        assert deps[0]["bump_kind"] == "major"
    finally:
        st.close()


def test_state_deduplicates_notifications(tmp_path):
    st = state_mod.State(tmp_path / "state.db")
    try:
        assert not st.notification_sent("k")
        st.record_notification("k", "owner/repo", 1, "pr_opened", "subject", "id")
        assert st.notification_sent("k")
    finally:
        st.close()


def test_state_tracks_processed_comments(tmp_path):
    st = state_mod.State(tmp_path / "state.db")
    try:
        st.mark_comment_processed("owner/repo", 1, "42", "done")
        assert "42" in st.known_comment_ids("owner/repo", 1)
    finally:
        st.close()


def test_console_backend_prints(capsys):
    backend = notify.ConsoleBackend()
    backend.send(notify.Message(subject="s", text="t", to=["a@b.c"]))
    out = capsys.readouterr().out
    assert "Subject: s" in out


def test_resend_backend_requires_a_key(monkeypatch):
    monkeypatch.delenv("RESEND_API_KEY", raising=False)
    try:
        notify.build_backend({"backend": "resend"})
    except notify.NotifyError as exc:
        assert "RESEND_API_KEY" in str(exc)
    else:
        raise AssertionError("expected a NotifyError")


def test_resend_backend_builds_the_right_payload(monkeypatch):
    monkeypatch.setenv("RESEND_API_KEY", "re_test")
    backend = notify.build_backend(
        {"backend": "resend", "from": "bot@example.com", "to": ["you@example.com"]}
    )
    assert isinstance(backend, notify.ResendBackend)
    assert backend.api_key == "re_test"
    assert backend.from_addr == "bot@example.com"


def test_notifier_deduplicates(tmp_path):
    st = state_mod.State(tmp_path / "state.db")
    try:
        n = notify.Notifier(notify.ConsoleBackend(), state=st, to=["a@b.c"])
        alert = alerts.pr_opened("owner/repo", 1, "http://x", "A", 1, ["a: 1 -> 2"])
        assert n.send(alert) == "sent"
        assert n.send(alert) == "duplicate"
    finally:
        st.close()


def test_alert_body_has_no_internal_detail():
    """The published note must not name the tooling."""
    alert = alerts.pr_opened("owner/repo", 1, "http://x", "B", 2, ["a: 1 -> 2"])
    assert "model" not in alert.text.lower()
    assert "agent" not in alert.text.lower()
    assert "sadoc" not in alert.text.lower()
    assert alert.dedupe_key


def test_notify_accepts_a_logger_with_the_standard_signature():
    """The worker calls log(message, repo=..., level=..., data=...)."""
    from sadoc.pipeline import Worker

    calls = []

    def log(message, repo=None, level="info", data=None):
        calls.append((message, repo, level))

    worker = Worker.__new__(Worker)
    worker.log = log
    worker.notifier = notify.Notifier(notify.ConsoleBackend(), state=None, to=["a@b.c"])
    status = worker.notify(alerts.pr_opened("owner/repo", 1, "http://x", "A", 1, []))
    assert status == "sent"
    assert calls and calls[0][1] == "owner/repo"


def test_notify_survives_a_broken_logger():
    """A logging failure must never abort the pipeline."""
    from sadoc.pipeline import Worker

    def broken_log(*args, **kwargs):
        raise RuntimeError("logger exploded")

    worker = Worker.__new__(Worker)
    worker.log = broken_log
    worker.notifier = notify.Notifier(notify.ConsoleBackend(), state=None, to=["a@b.c"])
    assert worker.notify(alerts.pr_opened("owner/repo", 1, "http://x", "A", 1, [])) == "sent"


def test_notify_survives_a_logger_with_the_wrong_signature():
    from sadoc.pipeline import Worker

    worker = Worker.__new__(Worker)
    worker.log = lambda message: None  # no keyword arguments at all
    worker.notifier = notify.Notifier(notify.ConsoleBackend(), state=None, to=["a@b.c"])
    assert worker.notify(alerts.pr_opened("owner/repo", 1, "http://x", "A", 1, [])) == "sent"


def test_notify_returns_failed_when_the_backend_fails():
    from sadoc.pipeline import Worker

    class BrokenBackend(notify.Backend):
        def send(self, message):
            raise notify.NotifyError("nope")

    worker = Worker.__new__(Worker)
    worker.log = lambda *a, **k: None
    worker.notifier = notify.Notifier(BrokenBackend(), state=None, to=["a@b.c"])
    assert worker.notify(alerts.pr_opened("owner/repo", 1, "http://x", "A", 1, [])) == "failed"


class _FakeGh:
    """A gh stand-in that records what the pipeline asked it."""

    def __init__(self, checks):
        self.checks = checks

    def pr_checks(self, owner, repo, number):
        return self.checks


def test_failed_job_ids_parses_both_link_shapes():
    from sadoc.gh import Gh

    gh = Gh.__new__(Gh)
    gh.pr_checks = lambda o, r, n: [
        {"bucket": "fail", "link": "https://github.com/o/r/actions/runs/1/job/222"},
        {"bucket": "fail", "link": "https://github.com/o/r/actions/runs/1/jobs/333"},
        {"bucket": "pass", "link": "https://github.com/o/r/actions/runs/1/job/444"},
    ]
    assert gh.failed_job_ids("o", "r", 1) == ["222", "333"]


def test_pr_merge_uses_a_strategy_flag():
    """`gh pr merge` takes -s/--squash, not --merge-method."""
    from sadoc.gh import Gh

    recorded = {}

    class _Gh(Gh):
        def run(self, *args, **kwargs):
            recorded["args"] = args
            return ""

    gh = _Gh()
    gh.pr_merge("o", "r", 1)
    assert "--squash" in recorded["args"]
    assert "--merge-method" not in recorded["args"]


def test_a_dry_run_does_not_send_mail(tmp_path):
    """A test run must not email anyone."""
    from sadoc.notify import Notifier

    sent = []

    class RecordingBackend(notify.Backend):
        def send(self, message):
            sent.append(message)
            return "sent"

    state = state_mod.State(tmp_path / "state.db")
    try:
        notifier = Notifier(RecordingBackend(), state=state, dry_run=True, to=["a@b.c"])
        alert = alerts.pr_opened("owner/repo", 1, "http://x", "A", 1, [])
        assert notifier.send(alert) == "dry-run"
        assert sent == []
        # The alert is still recorded, so a later real run can send it.
        assert state.notification_sent(alert.dedupe_key)
    finally:
        state.close()
