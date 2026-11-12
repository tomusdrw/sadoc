"""Exercise the RPC boundary without a model server."""

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
        response = session.bash("git push", timeout=20)
        assert response["data"]["exitCode"] == 1
        assert "Blocked by sadoc policy" in response["data"]["output"]
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
