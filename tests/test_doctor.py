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
