"""The policy that decides what the agent's shell may and may not do.

The rules live here, in one place, and are handed to the sandbox extension
through the environment. The extension enforces them; this module owns them, so
there is a single list to read, change and test.

The base rules - no `git push`, no `gh pr create`, no `sudo` - live in the
extension itself, because they are about the shell in general. The rules about
the container runtime live here, because the runtime is a second, inner
sandbox: a container inside the sandbox.
"""

from __future__ import annotations

import json
import re

# The container runtimes, the pattern that names each one, and whether the
# runtime may ever be enabled. Docker never may: its socket is root-equivalent,
# so reaching the daemon would be reaching root.
RUNTIME_PATTERNS: list[tuple[str, str, bool]] = [
    ("docker", r"\bdocker\b(?!\.)", False),
    ("podman", r"\bpodman\b", True),
]

# A container is only useful if it cannot reach further than the workspace. A
# rootless container already runs as the user's own uid; these rules stop it
# reaching the host namespaces, the runtime socket, or the machine.
#
# Each entry is a regular expression and the reason it is refused.
CONTAINER_FORBIDDEN: list[tuple[str, str]] = [
    # Global options can change storage/remote endpoints or hide the verb from
    # ownership tagging. Recipes use subcommand-first calls through the helper.
    (
        r"(?:^|\bpodman\s+)(?:container\s+)?-(?!-(?:help|version)(?:\s|$)|[hv](?:\s|$))",
        "global runtime options; put the subcommand first",
    ),
    (r"\bio\.sadoc\.session\b|--label-file\b", "overriding the container ownership label"),
    (r"--privileged\b", "a privileged container"),
    (r"--(?:net|network|network-mode)[= ]host\b", "the host network"),
    (r"--pid[= ]host\b", "the host pid namespace"),
    (r"--ipc[= ]host\b", "the host ipc namespace"),
    (r"--uts[= ]host\b", "the host uts namespace"),
    (r"--userns[= ]host\b", "the host user namespace"),
    (r"--cap-add[= ]?(?:ALL|SYS_ADMIN)\b", "extra capabilities"),
    (r"--security-opt[= ]seccomp[=:]unconfined\b", "an unconfined seccomp profile"),
    (r"--device\b", "a host device"),
    (r"-v\s+/run/user/", "the container runtime socket"),
    (r"-v\s+/var/run/docker\.sock", "the container runtime socket"),
    (r'-v\s+/(?:etc|root|usr|boot|sys|proc)(?:\s|:|/|")', "a host directory outside the workspace"),
    (r'-v\s+/:(?:\s|:|/|")', "the host root directory"),
    (
        r"--mount\b[^\n]*\bsource=/(?:etc|root|usr|boot|sys|proc)\b",
        "a host directory outside the workspace",
    ),
]

_COMPILED = [(re.compile(pattern, re.I), label) for pattern, label in CONTAINER_FORBIDDEN]


def violation(command: str) -> str | None:
    """The reason a container command is not allowed, or None if it is fine."""
    for pattern, label in _COMPILED:
        if pattern.search(command or ""):
            return label
    return None


def runtime_violation(command: str, allowed: str | None) -> str | None:
    """The reason a runtime may not be used, or None if it may.

    `allowed` is the one runtime that is permitted, if any. Every other
    runtime is refused - and a runtime that may never be enabled is refused
    even when it is asked for.
    """
    for name, pattern, may_enable in RUNTIME_PATTERNS:
        if not may_enable:
            if re.search(pattern, command or "", re.I):
                return name
            continue
        if allowed and name == allowed:
            continue
        if re.search(pattern, command or "", re.I):
            return name
    return None


def policy_json() -> str:
    """The rules as JSON, for the sandbox extension to read from the environment."""
    return json.dumps(
        {
            "container": [[pattern, label] for pattern, label in CONTAINER_FORBIDDEN],
            "runtimes": [
                [name, pattern, may_enable] for name, pattern, may_enable in RUNTIME_PATTERNS
            ],
        }
    )
