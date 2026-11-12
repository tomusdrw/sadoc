"""Fingerprint recipe inputs, not application source, from the committed tree.

Conventional build files are always watched, including newly added files. The
agent supplies additional files/directories for unconventional project layouts.
Missing declarations deliberately retain whole-tree invalidation.
"""

from __future__ import annotations

import hashlib
import json
from collections import deque
from fnmatch import fnmatchcase
from pathlib import PurePosixPath

from .gitops import Git

_NAMES = {
    "package.json",
    "package-lock.json",
    "npm-shrinkwrap.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "pnpm-workspace.yaml",
    "bun.lock",
    "bun.lockb",
    "cargo.toml",
    "cargo.lock",
    "build.rs",
    "rustfmt.toml",
    "clippy.toml",
    "rust-toolchain",
    "rust-toolchain.toml",
    "go.mod",
    "go.sum",
    "go.work",
    "go.work.sum",
    "pyproject.toml",
    "poetry.lock",
    "uv.lock",
    "pipfile",
    "pipfile.lock",
    "setup.py",
    "setup.cfg",
    "tox.ini",
    "pytest.ini",
    "ruff.toml",
    "mypy.ini",
    "noxfile.py",
    "makefile",
    "configure",
    "configure.ac",
    "configure.in",
    "gnumakefile",
    "justfile",
    "taskfile",
    "taskfile.yml",
    "taskfile.yaml",
    "cmakelists.txt",
    "cmakepresets.json",
    "meson.build",
    "meson_options.txt",
    "jenkinsfile",
    "procfile",
    "agents.md",
    "gemfile",
    "gemfile.lock",
    "composer.json",
    "composer.lock",
    "pom.xml",
    "build.gradle",
    "build.gradle.kts",
    "settings.gradle",
    "settings.gradle.kts",
    "gradle.properties",
    "gradlew",
    "gradlew.bat",
}
_PATTERNS = (
    "readme*",
    "contributing*",
    "requirements*.txt",
    "requirements*.in",
    "constraints*.in",
    "makefile.*",
    "*.mk",
    "*.cmake",
    "constraints*.txt",
    "tsconfig*.json",
    "*.config.*",
    "dockerfile*",
    "compose*.yml",
    "compose*.yaml",
    "docker-compose*.yml",
    "docker-compose*.yaml",
    "*.sh",
    "*.bash",
    "*.ps1",
)
_DIRECTORIES = {".github", ".circleci", "scripts", "tools", "ci", "config", "configs", "gradle"}


def parse_inputs(value: str | None) -> list[str] | None:
    """Normalize a JSON list of relative files/directories; None means unknown."""
    if value is None:
        return None
    try:
        paths = json.loads(value)
    except (ValueError, TypeError) as exc:
        raise ValueError("INPUTS must be a JSON array of repository-relative paths") from exc
    if not isinstance(paths, list):
        raise ValueError("INPUTS must be a JSON array of repository-relative paths")
    normalized = set()
    for path in paths:
        if (
            not isinstance(path, str)
            or not path
            or any(char in path for char in "*?[]\\")
            or any(ord(char) < 32 for char in path)
        ):
            raise ValueError("INPUTS must contain literal repository-relative paths, without globs")
        relative = PurePosixPath(path)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("INPUTS paths must stay inside the repository")
        normalized.add(str(relative))
    return sorted(normalized)


def _conventional(path: str) -> bool:
    relative = PurePosixPath(path)
    name = relative.name.lower()
    return (
        name.startswith(".")
        or name in _NAMES
        or any(fnmatchcase(name, pattern) for pattern in _PATTERNS)
        or any(part.startswith(".") or part.lower() in _DIRECTORIES for part in relative.parts)
    )


def _resolve_link(git: Git, entries: dict[str, str], path: str) -> tuple[str | None, set[str]]:
    """Resolve components in filesystem order using only committed Git objects."""
    pending = deque(path.split("/"))
    prefix: list[str] = []
    followed: set[str] = set()
    hops = 0
    while pending:
        part = pending.popleft()
        if part in ("", "."):
            continue
        if part == "..":
            if not prefix:
                return None, followed
            prefix.pop()
            continue
        candidate = "/".join([*prefix, part])
        if entries.get(candidate, "").startswith("120000 "):
            followed.add(candidate)
            hops += 1
            if hops > 40:
                return None, followed
            target = git._run("show", f"HEAD:{candidate}")
            if not target or target.startswith("/"):
                return None, followed
            pending.extendleft(reversed(target.split("/")))
        else:
            prefix.append(part)
    return "/".join(prefix) or ".", followed


def fingerprint(git: Git, inputs: list[str] | None, *, allow_network: bool) -> str:
    """Hash tracked input paths, modes and object IDs, including additions/removals."""
    if inputs is None:
        content = git.commit_sha("HEAD^{tree}")
    else:
        entries = {}
        for entry in git._run("ls-tree", "-rz", "--full-tree", "HEAD").split("\0"):
            if entry:
                metadata, path = entry.split("\t", 1)
                entries[path] = metadata
        selected = {
            path
            for path, meta in entries.items()
            if _conventional(path)
            or meta.startswith("160000 ")
            or any(root == "." or path == root or path.startswith(root + "/") for root in inputs)
            or (meta.startswith("120000 ") and any(root.startswith(path + "/") for root in inputs))
        }
        pending = [path for path in selected if entries[path].startswith("120000 ")]
        while pending:
            path = pending.pop()
            resolved, followed = _resolve_link(git, entries, path)
            if resolved is None:
                # Loops and external targets cannot safely narrow the input set.
                return fingerprint(git, None, allow_network=allow_network)
            selected.update(followed)
            targets = (
                [resolved]
                if resolved in entries
                else [
                    candidate
                    for candidate in entries
                    if resolved == "."
                    or candidate.startswith(resolved + "/")
                    or (
                        entries[candidate].startswith("120000 ")
                        and resolved.startswith(candidate + "/")
                    )
                ]
            )
            for candidate in targets:
                if candidate not in selected:
                    selected.add(candidate)
                    if entries[candidate].startswith("120000 "):
                        pending.append(candidate)
        content = [(path, entries[path]) for path in sorted(selected)]
    payload = json.dumps(
        {"inputs": inputs, "allow_network": allow_network, "content": content},
        sort_keys=True,
        separators=(",", ":"),
    )
    return "recipe-v2:" + hashlib.sha256(payload.encode()).hexdigest()
