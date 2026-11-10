"""Configuration loading, state persistence and the notification layer."""

from __future__ import annotations

import textwrap

from deps_bot import alerts, notify
from deps_bot import config as config_mod
from deps_bot import state as state_mod


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
    assert "deps-bot" not in alert.text.lower()
    assert alert.dedupe_key
