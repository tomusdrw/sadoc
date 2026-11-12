"""The doctor: keeping the default branch green."""

from __future__ import annotations

from sadoc import doctor


def test_transient_failures_are_recognised():
    logs = "error: failed to fetch https://registry.npmjs.org\nnetwork is unreachable"
    assert doctor._is_transient(logs)


def test_a_real_failure_is_not_transient():
    logs = "error[E0308]: mismatched types\n  --> src/main.rs:4:9"
    assert doctor._is_transient(logs) == []


def test_summarise_picks_the_interesting_lines():
    logs = "\n".join(
        [
            "step 1",
            "   Compiling foo v0.1.0",
            "error: cannot find value `x`",
            "   at src/lib.rs:10",
            "step 2",
        ]
    )
    summary = doctor.summarise(doctor.Diagnosis(repo="o/r", logs=logs))
    assert "cannot find value" in summary


def test_summarise_falls_back_to_the_raw_log():
    logs = "nothing interesting here at all"
    summary = doctor.summarise(doctor.Diagnosis(repo="o/r", logs=logs))
    assert "nothing interesting" in summary


def test_a_green_branch_needs_nothing():
    diagnosis = doctor.Diagnosis(repo="o/r")
    assert diagnosis.worth_fixing is False


def test_doctor_receives_job_branch_and_logs():
    from sadoc.pipeline import load_prompt

    prompt = load_prompt("doctor", JOB="generation", BRANCH="main", LOGS="failure: missing asset")
    assert "failure: missing asset" in prompt
    assert "`generation` on `main`" in prompt
    assert "{LOGS}" not in prompt


def test_doctor_reports_agent_failure_without_claiming_a_failed_repair(tmp_path, monkeypatch):
    from unittest.mock import Mock

    from sadoc.config import Config
    from sadoc.pipeline import Worker
    from sadoc.piworker import PiError
    from sadoc.state import State

    gh = Mock()
    gh.default_branch.return_value = "main"
    gh.pr_list.return_value = []
    monkeypatch.setattr(
        doctor,
        "diagnose",
        lambda *a: doctor.Diagnosis(
            repo="o/r",
            state=doctor.FIXABLE,
            detail="generation failed",
            failed_job="generation",
            logs="error: missing asset",
        ),
    )
    session = Mock()
    session.prompt.side_effect = PiError("Connection refused")
    monkeypatch.setattr("sadoc.pipeline.PiSession", lambda **kwargs: session)
    st = State(tmp_path / "state.db")
    try:
        worker = Worker(Config(), gh, st)
        worker.prepare_clone = Mock(return_value=Mock(path=tmp_path))
        result = worker.doctor_repo("o", "r")
        assert result["state"] == "agent_failed"
        assert "Connection refused" in result["detail"]
        gh.open_pr.assert_not_called()
    finally:
        st.close()
