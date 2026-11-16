"""The nightly runner.

It is a shell script, so the only way to be sure it works is to run it. The
`sadoc` it calls is replaced here by one that records how it was called.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
NIGHTLY = ROOT / "bin" / "sadoc-nightly"

# The options that have to come before the subcommand.
GLOBAL_OPTIONS = {"--config", "--db", "--dry-run"}


@pytest.fixture
def runner(tmp_path):
    """A copy of the nightly runner whose `sadoc` only records its arguments."""
    home = tmp_path / "sadoc"
    (home / "bin").mkdir(parents=True)
    script = home / "bin" / "sadoc-nightly"
    shutil.copy(NIGHTLY, script)
    script.chmod(0o755)

    calls = home / "calls.txt"
    fake = home / "bin" / "sadoc"
    fake.write_text('#!/usr/bin/env bash\nprintf "%s\\n" "$*" >> ' + str(calls) + "\nexit 0\n")
    fake.chmod(0o755)

    env = {
        **os.environ,
        "SADOC_LOG_DIR": str(tmp_path / "logs"),
        "SADOC_REPORT_DIR": str(tmp_path / "reports"),
        "SADOC_DB": str(tmp_path / "state.db"),
        "SADOC_SELF_UPDATE": "0",
    }
    result = subprocess.run(
        [str(script)],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    invocations = [line.split() for line in calls.read_text().splitlines() if line.strip()]
    return result, invocations


def test_the_nightly_runner_succeeds(runner):
    result, _ = runner
    assert result.returncode == 0, result.stdout + result.stderr


def test_every_step_is_run(runner):
    _, invocations = runner
    commands = [" ".join(args) for args in invocations]
    for step in ("scan", "run", "doctor", "monitor", "releases", "report"):
        assert any(step in command for command in commands), f"{step} was not run"


def test_the_global_options_come_before_the_subcommand(runner):
    """The options belong to sadoc itself, not to the subcommand."""
    _, invocations = runner
    for args in invocations:
        subcommand = None
        for arg in args:
            if arg in GLOBAL_OPTIONS:
                assert subcommand is None, f"{' '.join(args)}: {arg} comes after the subcommand"
                continue
            if not arg.startswith("-"):
                subcommand = arg
                break


def test_the_report_step_writes_the_digest(runner):
    _, invocations = runner
    report = [args for args in invocations if "report" in args]
    assert report, "the report step did not run"
    assert "--out" in report[0]
    assert "--note" in report[0]


def test_the_runner_does_not_claim_a_report_it_did_not_write(tmp_path):
    """A failing step must not be reported as a success."""
    home = tmp_path / "sadoc"
    (home / "bin").mkdir(parents=True)
    script = home / "bin" / "sadoc-nightly"
    shutil.copy(NIGHTLY, script)
    script.chmod(0o755)
    fake = home / "bin" / "sadoc"
    fake.write_text(
        "#!/usr/bin/env bash\n"
        'for arg in "$@"; do\n'
        '  if [ "$arg" = "report" ]; then exit 1; fi\n'
        "done\n"
        "exit 0\n"
    )
    fake.chmod(0o755)

    env = {
        **os.environ,
        "SADOC_LOG_DIR": str(tmp_path / "logs"),
        "SADOC_REPORT_DIR": str(tmp_path / "reports"),
        "SADOC_DB": str(tmp_path / "state.db"),
        "SADOC_SELF_UPDATE": "0",
    }
    result = subprocess.run([str(script)], env=env, capture_output=True, text=True, timeout=120)
    assert "report written to" not in result.stdout
    assert "the report was not written" in result.stdout


def test_monitor_runs_before_scan_and_new_updates(runner):
    _, invocations = runner
    steps = [
        next(a for a in args if a in {"monitor", "scan", "run"})
        for args in invocations
        if any(a in {"monitor", "scan", "run"} for a in args)
    ]
    assert steps.index("monitor") < steps.index("scan") < steps.index("run")


@pytest.mark.parametrize(
    "now,window,remaining",
    [
        ("0100", "01:00-07:00", 360),
        ("0137", "01:00-07:00", 323),
        ("2345", "23:00-02:00", 135),
    ],
)
def test_window_uses_minutes_without_the_hour_digit(tmp_path, now, window, remaining):
    home = tmp_path / "install"
    (home / "bin").mkdir(parents=True)
    script = home / "bin" / "sadoc-nightly"
    shutil.copy(NIGHTLY, script)
    fake = home / "bin" / "sadoc"
    fake.write_text("#!/bin/sh\nexit 0\n")
    fake.chmod(0o755)
    config = tmp_path / "config.toml"
    config.write_text(f'[schedule]\nwindow = "{window}"\n')
    bindir = tmp_path / "fake-bin"
    bindir.mkdir()
    date = bindir / "date"
    date.write_text(
        f'#!/bin/sh\nif [ "$1" = "+%H%M" ]; then echo {now}; else exec /usr/bin/date "$@"; fi\n'
    )
    date.chmod(0o755)
    env = {
        **os.environ,
        "PATH": str(bindir) + ":" + os.environ["PATH"],
        "SADOC_CONFIG": str(config),
        "SADOC_HOME": str(home),
        "SADOC_LOG_DIR": str(tmp_path / "logs"),
        "SADOC_REPORT_DIR": str(tmp_path / "reports"),
        "SADOC_SELF_UPDATE": "0",
    }
    result = subprocess.run(["bash", str(script)], env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert f"{remaining} min remaining" in result.stdout
