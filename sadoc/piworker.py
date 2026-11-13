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
import signal
import subprocess
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

from . import policy

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
        allow_containers: bool = False,
        discovery: bool = False,
        container_runtime: str = "podman",
    ):
        self.cwd = str(cwd)
        self.model = model
        self.agent_dir = Path(agent_dir)
        self.timeout_s = timeout_s
        self._counter = 0
        self._proc: subprocess.Popen | None = None
        self._process_group: int | None = None
        self._events: queue.Queue = queue.Queue()
        self._reader: threading.Thread | None = None
        self._stderr: list[str] = []
        self._allow_network = allow_network
        self._scrub_env = scrub_env
        self._extra_args = extra_args or []
        self._extra_secrets = list(extra_secrets or [])
        self._allow_containers = allow_containers
        self._container_runtime = container_runtime
        self._discovery = discovery
        self._session_id = uuid.uuid4().hex[:12]
        self.transcript: list[dict] = []
        self._tool_history: deque[dict] = deque(maxlen=40)
        self._active_tools: dict[str, dict] = {}
        self._original_commands: dict[str, str] = {}
        self._last_event = ""

    @staticmethod
    def _output_tail(result: object) -> str:
        if isinstance(result, str):
            return result[-4000:]
        if not isinstance(result, dict):
            return ""
        structured = result.get("structuredContent")
        if isinstance(structured, str):
            return structured[-4000:]
        if isinstance(structured, dict):
            return str(structured.get("output", ""))[-4000:]
        content = result.get("content", [])
        if isinstance(content, str):
            return content[-4000:]
        if not isinstance(content, list):
            return ""
        return "".join(
            str(c.get("text", ""))
            for c in content
            if isinstance(c, dict) and c.get("type") == "text"
        )[-4000:]

    def _record_diagnostic(self, event: dict) -> None:
        kind = event.get("type", "")
        self._last_event = kind
        now = time.monotonic()
        message = event.get("message") or {}
        if kind == "message_end" and message.get("role") == "assistant":
            for block in message.get("content") or []:
                if isinstance(block, dict) and block.get("type") == "toolCall":
                    self._original_commands[block.get("id")] = str(
                        (block.get("arguments") or {}).get("command", "")
                    )[:2000]
            # Only the most recent assistant message can launch new tools.
            if len(self._original_commands) > 100:
                self._original_commands.clear()
        key = event.get("toolCallId", "")
        if kind == "tool_execution_start":
            args = event.get("args") or {}
            self._active_tools[key] = {
                "id": key,
                "tool": event.get("toolName"),
                "started_at": time.time(),
                "started": now,
                "command": self._original_commands.pop(key, "")
                or str(args.get("command", ""))[:2000],
                "output": "",
            }
        elif kind == "tool_execution_update" and key in self._active_tools:
            self._active_tools[key]["output"] = self._output_tail(event.get("partialResult"))
        elif kind == "tool_execution_end":
            record = self._active_tools.pop(
                key, {"id": key, "tool": event.get("toolName"), "started": now}
            )
            record["duration_s"] = round(now - record.pop("started"), 3)
            record["output"] = self._output_tail(event.get("result"))
            record["is_error"] = bool(event.get("isError"))
            self._tool_history.append(record)

    def diagnostics(self) -> str:
        """Bounded local evidence, including a tool that never returned."""
        now = time.monotonic()
        active = [
            {
                **{k: v for k, v in record.items() if k != "started"},
                "elapsed_s": round(now - record["started"], 3),
            }
            for record in self._active_tools.values()
        ]
        return json.dumps(
            {
                "last_event": self._last_event,
                "active_tools": active,
                "tools": list(self._tool_history),
                "stderr": self.stderr_tail()[-8000:],
            }
        )

    # ------------------------------------------------------------- setup
    def _build_env(self) -> dict:
        env = dict(os.environ)
        if self._scrub_env:
            for key in list(env):
                if key in SECRET_PATTERNS or key.endswith("_API_KEY") or key in self._extra_secrets:
                    env.pop(key, None)
        env["PI_CODING_AGENT_DIR"] = str(self.agent_dir)
        env["SADOC_WORKSPACE"] = self.cwd
        env["SADOC_PLAYWRIGHT_RUNNER"] = str(REPO_ROOT / "extensions" / "playwright-runner.mjs")
        env["SADOC_SESSION_ID"] = self._session_id
        env["SADOC_DISCOVERY"] = "1" if self._discovery else "0"
        env["SADOC_REAL_HOME"] = str(Path.home())
        env["SADOC_ALLOW_NET"] = "1" if self._allow_network else "0"
        # A persistent cache for the package managers, so a run does not
        # re-download the whole registry.
        cache_dir = Path(
            os.environ.get("SADOC_CACHE_DIR", Path.home() / ".cache" / "sadoc" / "toolcache")
        )
        cache_dir.mkdir(parents=True, exist_ok=True)
        env["SADOC_CACHE_DIR"] = str(cache_dir)
        env["SADOC_ALLOW_CONTAINERS"] = "1" if self._allow_containers else "0"
        env["SADOC_CONTAINER_RUNTIME"] = self._container_runtime
        # The container rules are owned by sadoc.policy and handed over here, so
        # there is one list of them rather than one in Python and one in the
        # extension.
        if self._allow_containers:
            env["SADOC_CONTAINER_POLICY"] = policy.policy_json()
        else:
            env.pop("SADOC_CONTAINER_POLICY", None)
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
            start_new_session=True,
        )
        self._process_group = self._proc.pid
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
        if _cmd == "bash":
            self._record_diagnostic(
                {
                    "type": "tool_execution_start",
                    "toolCallId": req_id,
                    "toolName": "rpc_bash",
                    "args": fields,
                }
            )
        deadline = time.monotonic() + (timeout or self.timeout_s)
        while time.monotonic() < deadline:
            try:
                event = self._events.get(timeout=min(1.0, max(0.001, deadline - time.monotonic())))
            except queue.Empty as exc:
                if self._proc and self._proc.poll() is not None:
                    raise PiError(f"pi exited (code {self._proc.returncode})") from exc
                continue
            if (
                _cmd == "bash"
                and event.get("type") == "bash_execution_update"
                and event.get("id") in (None, req_id)
            ):
                record = self._active_tools[req_id]
                record["output"] = (record["output"] + str(event.get("delta", "")))[-4000:]
            if event.get("type") == "response" and event.get("id") == req_id:
                if _cmd == "bash":
                    data = event.get("data") or {}
                    self._record_diagnostic(
                        {
                            "type": "tool_execution_end",
                            "toolCallId": req_id,
                            "toolName": "rpc_bash",
                            "isError": not event.get("success")
                            or data.get("exitCode") != 0
                            or data.get("cancelled", False),
                            "result": data.get("output")
                            or event.get("error")
                            or self._active_tools[req_id]["output"],
                        }
                    )
                return event
        detail = f": {str(fields.get('command', ''))[:320]}" if _cmd == "bash" else ""
        raise PiError(f"timeout waiting for '{_cmd}' response{detail}")

    # ------------------------------------------------------------ prompt
    def prompt(
        self, message: str, timeout: int | None = None, *, report_only: bool = False
    ) -> TurnResult:
        """Send a prompt and collect the turn's events until the agent settles."""
        result = TurnResult()
        started = time.monotonic()
        if report_only:
            message = "[SADOC_REPORT_ONLY]\n" + message
        req_id = self._next_id()
        self._send({"id": req_id, "type": "prompt", "message": message})

        deadline = started + (timeout or self.timeout_s)
        while time.monotonic() < deadline:
            try:
                event = self._events.get(timeout=min(2.0, max(0.001, deadline - time.monotonic())))
            except queue.Empty:
                if self._proc and self._proc.poll() is not None:
                    result.error = f"pi exited (code {self._proc.returncode})"
                    break
                continue

            etype = event.get("type")
            self._record_diagnostic(event)
            if etype not in ("message_update", "tool_execution_update"):
                self.transcript.append(event)
                del self.transcript[:-200]

            if etype == "response" and event.get("id") == req_id:
                if not event.get("success"):
                    raise PiError(event.get("error") or "pi rejected the prompt")
                if (event.get("data") or {}).get("disposition") == "handled":
                    raise PiError("pi handled the prompt without starting an agent run")
            elif etype == "message_update":
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
                    # This message is authoritative, even when empty: an earlier
                    # tool-call preamble or retry must not become the final report.
                    assembled = "".join(
                        c.get("text", "") for c in msg.get("content", []) if c.get("type") == "text"
                    )
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

        result.duration_s = time.monotonic() - started
        if result.error:
            raise PiError(result.error)
        if not result.settled:
            active = "; ".join(
                f"{r['tool']}: {r.get('command', '')[:320]} (running {time.monotonic() - r['started']:.1f}s)"
                for r in self._active_tools.values()
            )
            detail = (
                f"; active tool: {active}"
                if active
                else f"; last event: {self._last_event or 'none'}"
            )
            raise PiError(f"pi prompt timed out after {result.duration_s:.1f}s{detail}")
        if not result.text.strip():
            raise PiError("pi returned an empty response")
        if result.stop_reason != "stop":
            raise PiError(f"pi prompt did not complete (stop reason: {result.stop_reason})")
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
                self._proc.wait(timeout=5)
        # External runtime commands are outside bubblewrap. Stop any remaining
        # helper descendants before enumerating containers, so they cannot create
        # another container after cleanup has already inspected the label.
        if self._process_group is not None:
            try:
                os.killpg(self._process_group, signal.SIGKILL)
            except ProcessLookupError:
                pass
            self._process_group = None
        if self._allow_containers:
            self._cleanup_containers()

    def _cleanup_containers(self) -> None:
        env = self._build_env()
        containers = Path(env["SADOC_CACHE_DIR"]) / "containers"
        env.update(
            XDG_DATA_HOME=str(containers / "data"),
            XDG_RUNTIME_DIR=str(containers / "run"),
            TMPDIR="/tmp",
        )
        runtime = self._container_runtime
        try:
            found = subprocess.run(
                [
                    runtime,
                    "ps",
                    "--all",
                    "--quiet",
                    "--filter",
                    f"label=io.sadoc.session={self._session_id}",
                ],
                env=env,
                capture_output=True,
                text=True,
                timeout=15,
            )
            if found.returncode:
                raise PiError("container cleanup: " + found.stderr[-1000:])
            ids = found.stdout.split()
            if ids:
                removed = subprocess.run(
                    [runtime, "rm", "--force", "--volumes", *ids],
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=20,
                )
                if removed.returncode:
                    raise PiError("container cleanup: " + removed.stderr[-1000:])
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise PiError(f"container cleanup: {exc}") from exc
        (containers / "run" / f"{self._session_id}.sock").unlink(missing_ok=True)

    def stderr_tail(self, n: int = 40) -> str:
        return "\n".join(self._stderr[-n:])

    def __enter__(self) -> PiSession:
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.close()
