"""Client for pi's RPC mode.

pi runs as a long-lived child process speaking JSONL on stdin/stdout. One instance
per repository keeps the model's context alive across the whole pipeline
(bump -> migrate -> fix-ci -> address-review), so it does not have to re-read the
repo on every stage.

Notes that cost real debugging time:
  * stdin must stay open for the life of the session. Closing it is pi's documented
    orderly-shutdown signal.
  * the `bash` tool is *not* re-registered; the sandbox extension mutates the command
    in a `tool_call` hook instead.
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SANDBOX_EXTENSION = REPO_ROOT / "extensions" / "sandbox.ts"

# Environment variables that must never reach the agent.
SECRET_PATTERNS = (
    "GH_TOKEN",
    "GITHUB_TOKEN",
    "GH_ENTERPRISE_TOKEN",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "AZURE_OPENAI_API_KEY",
    "GOOGLE_API_KEY",
    "OPENROUTER_API_KEY",
    "NPM_TOKEN",
    "PYPI_TOKEN",
    "RESEND_API_KEY",
    "SMTP_APP_PASSWORD",
    "SMTP_PASSWORD",
)


class PiError(RuntimeError):
    pass


@dataclass
class TurnResult:
    text: str = ""
    thinking: str = ""
    tool_calls: list[dict] = field(default_factory=list)
    tool_results: list[dict] = field(default_factory=list)
    stop_reason: str | None = None
    error: str | None = None
    usage: dict = field(default_factory=dict)
    duration_s: float = 0.0
    settled: bool = False


@dataclass
class PiModel:
    """A pi provider/model pairing backed by a local OpenAI-compatible endpoint."""

    name: str
    base_url: str
    model: str
    context: int = 262144
    max_tokens: int = 16384
    reasoning: bool = True


class PiSession:
    def __init__(
        self,
        cwd: str | Path,
        model: PiModel,
        agent_dir: str | Path,
        allow_network: bool = True,
        scrub_env: bool = True,
        timeout_s: int = 1800,
        extra_args: list[str] | None = None,
        extra_secrets: list[str] | None = None,
    ):
        self.cwd = str(cwd)
        self.model = model
        self.agent_dir = Path(agent_dir)
        self.timeout_s = timeout_s
        self._counter = 0
        self._proc: subprocess.Popen | None = None
        self._events: queue.Queue = queue.Queue()
        self._reader: threading.Thread | None = None
        self._stderr: list[str] = []
        self._allow_network = allow_network
        self._scrub_env = scrub_env
        self._extra_args = extra_args or []
        self._extra_secrets = list(extra_secrets or [])
        self.transcript: list[dict] = []

    # ------------------------------------------------------------- setup
    def _build_env(self) -> dict:
        env = dict(os.environ)
        if self._scrub_env:
            for key in list(env):
                if key in SECRET_PATTERNS or key.endswith("_API_KEY") or key in self._extra_secrets:
                    env.pop(key, None)
        env["PI_CODING_AGENT_DIR"] = str(self.agent_dir)
        env["DEPSBOT_WORKSPACE"] = self.cwd
        env["DEPSBOT_REAL_HOME"] = str(Path.home())
        env["DEPSBOT_ALLOW_NET"] = "1" if self._allow_network else "0"
        # A persistent cache for the package managers, so a run does not
        # re-download the whole registry.
        cache_dir = Path(
            os.environ.get("DEPSBOT_CACHE_DIR", Path.home() / ".cache" / "deps-bot" / "toolcache")
        )
        cache_dir.mkdir(parents=True, exist_ok=True)
        env["DEPSBOT_CACHE_DIR"] = str(cache_dir)
        return env

    def _write_models_json(self) -> None:
        """Generate an isolated models.json so we never touch the user's config."""
        self.agent_dir.mkdir(parents=True, exist_ok=True)
        base = self.model.base_url.rstrip("/")
        if not base.endswith("/v1"):
            base += "/v1"
        payload = {
            "providers": {
                self.model.name: {
                    "api": "openai-completions",
                    "apiKey": "none",
                    "baseUrl": base,
                    "models": [
                        {
                            "id": self.model.model,
                            "name": self.model.model,
                            "contextWindow": self.model.context,
                            "maxTokens": self.model.max_tokens,
                            "input": ["text"],
                            "reasoning": self.model.reasoning,
                        }
                    ],
                }
            }
        }
        (self.agent_dir / "models.json").write_text(json.dumps(payload, indent=2))
        (self.agent_dir / "settings.json").write_text(
            json.dumps(
                {
                    "defaultProvider": self.model.name,
                    "defaultModel": self.model.model,
                },
                indent=2,
            )
        )

    def start(self) -> None:
        self._write_models_json()
        cmd = [
            "pi",
            "--mode",
            "rpc",
            "--no-session",
            "--no-extensions",
            "-e",
            str(SANDBOX_EXTENSION),
            "--provider",
            self.model.name,
            "--model",
            self.model.model,
            *self._extra_args,
        ]
        self._proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=self.cwd,
            env=self._build_env(),
            text=True,
            bufsize=1,
        )
        self._reader = threading.Thread(target=self._pump_stdout, daemon=True)
        self._reader.start()
        threading.Thread(target=self._pump_stderr, daemon=True).start()

    def _pump_stdout(self) -> None:
        assert self._proc and self._proc.stdout
        for line in self._proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                self._events.put(json.loads(line))
            except json.JSONDecodeError:
                self._events.put({"type": "_raw", "line": line})

    def _pump_stderr(self) -> None:
        assert self._proc and self._proc.stderr
        for line in self._proc.stderr:
            self._stderr.append(line.rstrip())
            if len(self._stderr) > 400:
                del self._stderr[:200]

    # ---------------------------------------------------------- commands
    def _next_id(self) -> str:
        self._counter += 1
        return f"req-{self._counter}"

    def _send(self, payload: dict) -> None:
        assert self._proc and self._proc.stdin
        self._proc.stdin.write(json.dumps(payload) + "\n")
        self._proc.stdin.flush()

    def command(self, _cmd: str, timeout: int | None = None, **fields) -> dict:
        """Send a command and wait for its correlated response."""
        req_id = self._next_id()
        self._send({"id": req_id, "type": _cmd, **fields})
        deadline = time.time() + (timeout or self.timeout_s)
        while time.time() < deadline:
            try:
                event = self._events.get(timeout=1.0)
            except queue.Empty as exc:
                if self._proc and self._proc.poll() is not None:
                    raise PiError(f"pi exited (code {self._proc.returncode})") from exc
                continue
            if event.get("type") == "response" and event.get("id") == req_id:
                return event
        raise PiError(f"timeout waiting for '{_cmd}' response")

    # ------------------------------------------------------------ prompt
    def prompt(self, message: str, timeout: int | None = None) -> TurnResult:
        """Send a prompt and collect the turn's events until the agent settles."""
        result = TurnResult()
        started = time.time()
        req_id = self._next_id()
        self._send({"id": req_id, "type": "prompt", "message": message})

        deadline = time.time() + (timeout or self.timeout_s)
        while time.time() < deadline:
            try:
                event = self._events.get(timeout=2.0)
            except queue.Empty:
                if self._proc and self._proc.poll() is not None:
                    result.error = f"pi exited (code {self._proc.returncode})"
                    break
                continue

            etype = event.get("type")
            self.transcript.append(event)

            if etype == "message_update":
                ev = event.get("assistantMessageEvent", {})
                et = ev.get("type")
                if et == "text_delta":
                    result.text += ev.get("delta", "")
                elif et == "thinking_delta":
                    result.thinking += ev.get("delta", "")
                elif et == "tool_call":
                    result.tool_calls.append(
                        {
                            "id": ev.get("id"),
                            "name": ev.get("toolName"),
                            "args": ev.get("args"),
                        }
                    )
            elif etype == "tool_execution_end":
                # The shape of `result` varies: it can be a bare string, or a dict
                # whose `structuredContent` is either a dict or a string.
                raw_result = event.get("result")
                output = ""
                exit_code = None
                if isinstance(raw_result, str):
                    output = raw_result
                elif isinstance(raw_result, dict):
                    sc = raw_result.get("structuredContent")
                    if isinstance(sc, dict):
                        output = sc.get("output", "") or ""
                        exit_code = sc.get("exit_code")
                    elif isinstance(sc, str):
                        output = sc
                    else:
                        content = raw_result.get("content")
                        if isinstance(content, list):
                            output = "".join(
                                c.get("text", "")
                                for c in content
                                if isinstance(c, dict) and c.get("type") == "text"
                            )
                        elif isinstance(content, str):
                            output = content
                result.tool_results.append(
                    {
                        "tool": event.get("toolName"),
                        "output": output,
                        "exit_code": exit_code,
                        "is_error": event.get("isError"),
                    }
                )
            elif etype == "message_end":
                msg = event.get("message") or {}
                if msg.get("role") == "assistant":
                    result.stop_reason = msg.get("stopReason")
                    result.error = msg.get("errorMessage")
                    if msg.get("usage"):
                        result.usage = msg["usage"]
                    # Prefer the assembled text over the streamed deltas.
                    assembled = "".join(
                        c.get("text", "") for c in msg.get("content", []) if c.get("type") == "text"
                    )
                    if assembled:
                        result.text = assembled
                    # Tool calls are reported as content blocks on the message.
                    for c in msg.get("content", []):
                        if c.get("type") == "toolCall":
                            result.tool_calls.append(
                                {
                                    "id": c.get("id"),
                                    "name": c.get("name"),
                                    "args": c.get("arguments"),
                                }
                            )
            elif etype == "agent_settled":
                result.settled = True
                break

        result.duration_s = time.time() - started
        return result

    def bash(self, command: str, timeout: int = 600) -> dict:
        """Run a shell command in the session's context (also sandboxed)."""
        return self.command("bash", command=command, timeout=timeout)

    def get_state(self) -> dict:
        resp = self.command("get_state", timeout=30)
        return resp.get("data", {}) if resp.get("success") else {}

    # ---------------------------------------------------------- lifecycle
    def close(self) -> None:
        if not self._proc:
            return
        try:
            if self._proc.stdin:
                self._proc.stdin.close()
        except OSError:
            pass
        try:
            self._proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self._proc.kill()

    def stderr_tail(self, n: int = 40) -> str:
        return "\n".join(self._stderr[-n:])

    def __enter__(self) -> PiSession:
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.close()
