"""Exercise the RPC boundary without a model server."""

from pathlib import Path
from unittest.mock import Mock

import pytest

from sadoc.piworker import PiError, PiModel, PiSession


def session_with_events(tmp_path, *events):
    session = PiSession(tmp_path, PiModel("test", "http://localhost:1", "test"), tmp_path)
    session._proc = Mock()
    session._proc.poll.return_value = None
    for event in events:
        session._events.put(event)
    return session


def test_provider_error_is_not_an_empty_success(tmp_path):
    session = session_with_events(
        tmp_path,
        {
            "type": "message_end",
            "message": {
                "role": "assistant",
                "content": [],
                "stopReason": "error",
                "errorMessage": "Connection refused",
            },
        },
        {"type": "agent_settled"},
    )
    with pytest.raises(PiError, match="Connection refused"):
        session.prompt("discover")


def test_rejected_prompt_fails_immediately(tmp_path):
    session = session_with_events(
        tmp_path,
        {
            "type": "response",
            "id": "req-1",
            "success": False,
            "error": "Model not found",
        },
    )
    with pytest.raises(PiError, match="Model not found"):
        session.prompt("discover", timeout=0.01)


def test_timeout_does_not_accept_partial_recipe(tmp_path):
    session = session_with_events(
        tmp_path,
        {
            "type": "message_update",
            "assistantMessageEvent": {"type": "text_delta", "delta": "VERIFY: true"},
        },
    )
    with pytest.raises(PiError, match="timed out"):
        session.prompt("discover", timeout=0.01)


def test_empty_settled_turn_is_an_error(tmp_path):
    session = session_with_events(tmp_path, {"type": "agent_settled"})
    with pytest.raises(PiError, match="empty"):
        session.prompt("discover")


def test_successful_retry_can_recover_provider_error(tmp_path):
    session = session_with_events(
        tmp_path,
        {
            "type": "message_end",
            "message": {
                "role": "assistant",
                "content": [],
                "stopReason": "error",
                "errorMessage": "busy",
            },
        },
        {
            "type": "message_end",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "VERIFY: make check"}],
                "stopReason": "stop",
            },
        },
        {"type": "agent_settled"},
    )
    assert session.prompt("discover").text == "VERIFY: make check"


@pytest.mark.parametrize(
    "mode,marker", [("report_only", "SADOC_REPORT_ONLY"), ("read_only", "SADOC_READ_ONLY")]
)
def test_restricted_prompt_uses_the_extension_marker(tmp_path, mode, marker):
    import json

    session = session_with_events(
        tmp_path,
        {
            "type": "message_end",
            "message": {
                "role": "assistant",
                "stopReason": "stop",
                "content": [{"type": "text", "text": "STATUS: blocked"}],
            },
        },
        {"type": "agent_settled"},
    )
    assert session.prompt("Reformat", timeout=120, **{mode: True}).text == "STATUS: blocked"
    payload = json.loads(session._proc.stdin.write.call_args.args[0])
    assert payload["message"] == f"[{marker}]\nReformat"


def test_rpc_bash_uses_the_sandbox_and_scrubs_credentials(tmp_path, monkeypatch):
    import shutil

    if not shutil.which("pi") or not shutil.which("bwrap"):
        pytest.skip("pi and bubblewrap are needed for the RPC sandbox smoke test")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("GH_TOKEN", "test-token-must-not-reach-pi")
    session = PiSession(
        workspace, PiModel("test", "http://localhost:1", "test"), tmp_path / "agent"
    )
    try:
        session.start()
        response = session.bash('printf "%s:%s" "$SADOC_SANDBOX" "${GH_TOKEN-unset}"', timeout=20)
        assert response["data"]["exitCode"] == 0
        assert response["data"]["output"] == "1:unset"
        outside = tmp_path / "outside"
        response = session.bash(f"printf escaped > {outside}", timeout=20)
        # /tmp is writable inside the sandbox, but must not touch the host.
        assert not outside.exists()
        response = session.bash('printf "%s" "$PLAYWRIGHT_BROWSERS_PATH"', timeout=20)
        assert response["data"]["output"] == str(
            session._build_env()["SADOC_CACHE_DIR"] + "/ms-playwright"
        )
        response = session.bash("git push", timeout=20)
        assert response["data"]["exitCode"] == 1
        assert "Blocked by sadoc policy" in response["data"]["output"]
    finally:
        session.close()


def test_package_manager_data_stays_in_writable_persistent_cache(tmp_path, monkeypatch):
    import json
    import shutil

    if not shutil.which("pi") or not shutil.which("bwrap"):
        pytest.skip("pi and bubblewrap are needed for the RPC sandbox smoke test")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    cache = tmp_path / "tool cache"
    monkeypatch.setenv("SADOC_CACHE_DIR", str(cache))
    # Host settings must not send installs back to the read-only real home.
    for name in ("XDG_DATA_HOME", "PNPM_HOME", "npm_config_store_dir", "COREPACK_HOME"):
        monkeypatch.setenv(name, f"/usr/share/host-only/{name}")
    session = PiSession(
        workspace, PiModel("test", "http://localhost:1", "test"), tmp_path / "agent"
    )
    try:
        session.start()
        response = session.bash(
            "python3 - <<'PY'\n"
            "import os,json\n"
            "from pathlib import Path\n"
            "paths = {key: os.environ[key] for key in "
            "['XDG_DATA_HOME', 'PNPM_HOME', 'npm_config_store_dir', 'COREPACK_HOME']}\n"
            "print(json.dumps(paths))\n"
            "for path in paths.values():\n"
            " p = Path(path); p.mkdir(parents=True, exist_ok=True)\n"
            " (p / 'cached-evidence').write_text('retained')\n"
            "PY",
            timeout=20,
        )
        assert response["data"]["exitCode"] == 0, response["data"]["output"]
        paths = json.loads(response["data"]["output"])
        for path in paths.values():
            assert Path(path).is_relative_to(cache)
            assert (Path(path) / "cached-evidence").read_text() == "retained"
        # Each command gets a fresh /tmp; package-manager data must survive it.
        response = session.bash('cat "$npm_config_store_dir/cached-evidence"', timeout=20)
        assert response["data"]["exitCode"] == 0
        assert response["data"]["output"] == "retained"
    finally:
        session.close()


@pytest.mark.parametrize("reason", ["length", "toolUse"])
def test_incomplete_final_response_cannot_supply_a_recipe(tmp_path, reason):
    session = session_with_events(
        tmp_path,
        {
            "type": "message_end",
            "message": {
                "role": "assistant",
                "stopReason": reason,
                "content": [{"type": "text", "text": "STATUS: ready\nVERIFY: make build"}],
            },
        },
        {"type": "agent_settled"},
    )
    with pytest.raises(PiError, match=reason):
        session.prompt("discover")


def test_empty_final_answer_cannot_reuse_an_intermediate_recipe(tmp_path):
    session = session_with_events(
        tmp_path,
        {
            "type": "message_end",
            "message": {
                "role": "assistant",
                "stopReason": "toolUse",
                "content": [{"type": "text", "text": "VERIFY: make build"}],
            },
        },
        {
            "type": "message_end",
            "message": {
                "role": "assistant",
                "stopReason": "stop",
                "content": [],
            },
        },
        {"type": "agent_settled"},
    )
    with pytest.raises(PiError, match="empty"):
        session.prompt("discover")


def test_timeout_identifies_active_command_and_preserves_partial_output(tmp_path):
    import json

    session = session_with_events(
        tmp_path,
        {
            "type": "tool_execution_start",
            "toolCallId": "slow",
            "toolName": "bash",
            "args": {"command": "pnpm test"},
        },
        {
            "type": "tool_execution_update",
            "toolCallId": "slow",
            "toolName": "bash",
            "partialResult": {"content": [{"type": "text", "text": "waiting for database"}]},
        },
    )
    with pytest.raises(PiError, match="pnpm test"):
        session.prompt("discover", timeout=0.01)
    diagnostic = json.loads(session.diagnostics())
    assert diagnostic["active_tools"][0]["command"] == "pnpm test"
    assert diagnostic["active_tools"][0]["output"] == "waiting for database"
    assert diagnostic["active_tools"][0]["elapsed_s"] >= 0


def test_diagnostics_bound_output_and_keep_completed_tool_timing(tmp_path):
    import json

    session = session_with_events(
        tmp_path,
        {
            "type": "tool_execution_start",
            "toolCallId": "a",
            "toolName": "bash",
            "args": {"command": "make check"},
        },
        {
            "type": "tool_execution_end",
            "toolCallId": "a",
            "toolName": "bash",
            "isError": True,
            "result": {"content": [{"type": "text", "text": "x" * 100000 + "failure"}]},
        },
        {"type": "agent_settled"},
    )
    with pytest.raises(PiError):
        session.prompt("discover")
    raw = session.diagnostics()
    assert len(raw) < 10000
    diagnostic = json.loads(raw)
    assert diagnostic["active_tools"] == []
    tool = diagnostic["tools"][-1]
    assert tool["output"].endswith("failure")
    assert tool["command"] == "make check"
    assert tool["is_error"] is True
    assert tool["duration_s"] >= 0


def test_session_environments_have_unique_container_ownership(tmp_path):
    a = PiSession(tmp_path, PiModel("test", "http://localhost:1", "test"), tmp_path)
    b = PiSession(tmp_path, a.model, tmp_path)
    assert a._build_env()["SADOC_SESSION_ID"] != b._build_env()["SADOC_SESSION_ID"]


def test_forced_close_removes_only_owned_containers(tmp_path, monkeypatch):
    import subprocess

    session = PiSession(
        tmp_path, PiModel("test", "http://localhost:1", "test"), tmp_path, allow_containers=True
    )
    session._proc = Mock()
    session._proc.wait.side_effect = [subprocess.TimeoutExpired("pi", 20), None]
    calls = []

    def runtime(argv, **kwargs):
        calls.append(argv)
        if "ps" in argv:
            assert f"label=io.sadoc.session={session._build_env()['SADOC_SESSION_ID']}" in argv
            return subprocess.CompletedProcess(argv, 0, "abc123\n", "")
        assert argv == ["podman", "rm", "--force", "--volumes", "abc123"]
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr("sadoc.piworker.subprocess.run", runtime)
    session.close()
    assert len(calls) == 2


def test_container_cleanup_failure_is_reported(tmp_path, monkeypatch):
    import subprocess

    session = PiSession(
        tmp_path, PiModel("test", "http://localhost:1", "test"), tmp_path, allow_containers=True
    )
    session._proc = Mock()
    monkeypatch.setattr(
        "sadoc.piworker.subprocess.run",
        lambda argv, **kw: subprocess.CompletedProcess(argv, 1, "", "runtime unavailable"),
    )
    with pytest.raises(PiError, match="container cleanup"):
        session.close()


def test_rpc_user_message_string_does_not_break_diagnostics(tmp_path):
    session = session_with_events(
        tmp_path,
        {"type": "message_end", "message": {"role": "user", "content": "discover"}},
        {
            "type": "message_end",
            "message": {
                "role": "assistant",
                "stopReason": "stop",
                "content": [{"type": "text", "text": "ready"}],
            },
        },
        {"type": "agent_settled"},
    )
    assert session.prompt("discover").text == "ready"


def test_rpc_recipe_timeout_retains_command_and_streamed_output(tmp_path):
    import json

    session = session_with_events(
        tmp_path,
        {"type": "bash_execution_update", "id": "req-1", "delta": "waiting for database\n"},
    )
    with pytest.raises(PiError, match="make check"):
        session.bash("make check", timeout=0.01)
    active = json.loads(session.diagnostics())["active_tools"]
    assert active[0]["command"] == "make check"
    assert active[0]["output"].endswith("waiting for database\n")


def test_rpc_recipe_completion_is_timed_and_bounds_output(tmp_path):
    import json

    session = session_with_events(
        tmp_path,
        {
            "type": "response",
            "id": "req-1",
            "success": True,
            "data": {"output": "x" * 100000 + "done", "exitCode": 1},
        },
    )
    session.bash("make check")
    diagnostics = json.loads(session.diagnostics())
    assert not diagnostics["active_tools"]
    assert diagnostics["tools"][-1]["duration_s"] >= 0
    assert diagnostics["tools"][-1]["is_error"]
    assert diagnostics["tools"][-1]["output"].endswith("done")
    assert len(diagnostics["tools"][-1]["output"]) <= 4000


def test_close_kills_helper_descendants_before_container_cleanup(tmp_path):
    import os
    import subprocess
    import sys
    import time

    marker = tmp_path / "late-container"
    script = (
        "import os,sys,time\n"
        "if os.fork() == 0:\n"
        " time.sleep(0.5)\n"
        ' open(sys.argv[1], "w").write("late create")\n'
        " os._exit(0)\n"
        'print("started", flush=True)\n'
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", script, str(marker)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        start_new_session=True,
        text=True,
    )
    session = PiSession(tmp_path, PiModel("test", "http://localhost:1", "test"), tmp_path)
    session._proc = proc
    session._process_group = proc.pid
    try:
        assert proc.stdout.readline().strip() == "started"
        session.close()
        time.sleep(0.7)
        assert not marker.exists()
    finally:
        try:
            os.killpg(proc.pid, 9)
        except ProcessLookupError:
            pass
        proc.wait()
