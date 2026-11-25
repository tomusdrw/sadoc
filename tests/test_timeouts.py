"""Global pi timeout configuration and run startup."""

import sys
import time
from unittest.mock import Mock

import pytest

from sadoc import cli, config
from sadoc.pipeline import Worker

CUSTOM_TIMEOUTS = """
[timeouts]
prompt_wall_clock = 5400
prompt_inactivity = 900
note_generation = 1800
"""


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ('owner = "example"\n', (None, None, 900)),
        ("[timeouts]\n", (None, None, 900)),
        (CUSTOM_TIMEOUTS, (5400, 900, 1800)),
        ("[timeouts]\nprompt_inactivity = 900\n", (None, 900, 900)),
        ("[timeouts]\nnote_generation = 1200\n", (None, None, 1200)),
        ("[timeouts]\nprompt_wall_clock = 3600\n", (3600, None, 900)),
    ],
)
def test_load_timeouts(tmp_path, body, expected):
    path = tmp_path / "config.toml"
    path.write_text(body)
    limits = config.load(path).timeouts
    assert (limits.prompt_wall_clock, limits.prompt_inactivity, limits.note_generation) == expected


@pytest.mark.parametrize(
    "body",
    [
        "timeouts = 1",
        "[timeouts]\nprompt_wall_clock = 0",
        "[timeouts]\nprompt_inactivity = -1",
        "[timeouts]\nnote_generation = true",
        '[timeouts]\nprompt_wall_clock = "5400"',
        "[timeouts]\nprompt_inactivity = 900.0",
        "[timeouts]\nnote_generation = inf",
        "[timeouts]\nprompt_inactivity = 1800",
        "[timeouts]\nprompt_wall_clock = 3000000\nprompt_inactivity = 2147484",
        "[timeouts]\nprompt_wall_clock = 500",
        "[timeouts]\nprompt_wall_clock = 900\nprompt_inactivity = 900",
    ],
)
def test_invalid_timeouts(tmp_path, body):
    path = tmp_path / "config.toml"
    path.write_text(body)
    with pytest.raises(config.ConfigError, match="timeouts"):
        config.load(path)


def test_invalid_timeouts_abort_before_run(tmp_path, monkeypatch, capsys):
    path = tmp_path / "config.toml"
    path.write_text(
        f'workdir = "{tmp_path / "work"}"\nreport_dir = "{tmp_path / "report"}"\n'
        "[timeouts]\nprompt_inactivity = 1800"
    )
    db = tmp_path / "state" / "state.db"
    run = Mock(return_value=0)
    monkeypatch.setattr(cli, "cmd_run", run)
    monkeypatch.setattr(sys, "argv", ["sadoc", "--config", str(path), "--db", str(db), "run"])
    assert cli.main() == 1
    assert "timeouts.prompt_inactivity must be less than timeouts.prompt_wall_clock" in (
        capsys.readouterr().out
    )
    assert not db.parent.exists()
    run.assert_not_called()


@pytest.mark.parametrize(
    ("body", "deadline", "expected"),
    [
        ("", "07:00", "wall=1800s inactivity=600s note=900s"),
        (CUSTOM_TIMEOUTS, "07:00", "wall=5400s inactivity=900s note=1800s"),
        ("", "none", "wall=1800s inactivity=600s note=900s"),
        ("", "inf", "wall=1800s inactivity=600s note=900s"),
    ],
)
def test_run_logs_effective_limits_before_scope(
    tmp_path, monkeypatch, capsys, body, deadline, expected
):
    path = tmp_path / "config.toml"
    path.write_text(body)
    epoch = {
        "07:00": time.mktime((2026, 11, 25, 7, 0, 0, 0, 0, -1)),
        "none": None,
        "inf": float("inf"),
    }[deadline]
    args = cli.argparse.Namespace(repo=None, deadline=epoch, max_prs=None, json=False)

    def resolve_scope(*args):
        output = capsys.readouterr().out
        assert output.startswith("[")
        assert f"] limits: {expected} deadline={deadline}\n" in output
        return []

    monkeypatch.setattr(cli.detect, "resolve_scope", resolve_scope)
    monkeypatch.setattr(cli, "_check_models", lambda cfg: True)
    assert cli.cmd_run(args, config.load(path), Mock(), Mock()) == 0


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("", [{"timeout": 7200, "idle_timeout": 600}, {}, {"timeout": 900}]),
        (
            CUSTOM_TIMEOUTS,
            [
                {"timeout": 5400, "idle_timeout": 900},
                {"timeout": 5400, "idle_timeout": 900},
                {"timeout": 1800, "idle_timeout": 900},
            ],
        ),
        (
            "[timeouts]\nprompt_wall_clock = 3600",
            [{"timeout": 3600, "idle_timeout": 600}, {"timeout": 3600}, {"timeout": 900}],
        ),
    ],
)
def test_prompt_overrides_preserve_omitted_limits(tmp_path, body, expected):
    path = tmp_path / "config.toml"
    path.write_text(body)
    worker = Worker(config.load(path), Mock(), Mock())
    session = Mock()
    worker._prompt(session, "discovery", timeout=7200, idle_timeout=600)
    worker._prompt(session, "bump")
    worker._prompt(session, "note", note=True)
    assert [call.kwargs for call in session.prompt.call_args_list] == expected
