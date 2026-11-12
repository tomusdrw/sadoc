"""Fingerprint recipe inputs, not application source, from the committed tree.

Conventional build files are always watched, including newly added files. The
agent supplies additional files/directories for unconventional project layouts.
Missing declarations deliberately retain whole-tree invalidation.

A fingerprint answers one question: *could the build recipe have changed?* A
dependency version bump does not change how a project is built - it changes
what is installed - so the versions are masked out before hashing. Without
that, every pull request Sadoc opens would invalidate the recipe it just
proved, and the next night would have to work out how to build the project all
over again.

Lockfiles are masked out entirely. A lockfile records what was resolved, not
how to build, and it churns on every bump.

Masking is never silent: the recipe is always replayed on the real tree
before a pull request is opened, so a recipe that has gone stale fails loudly
and is discarded rather than trusted.
"""

from __future__ import annotations

import hashlib
import json
import re
import tomllib
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

# A lockfile records what was resolved, not how to build. Its content changes
# on every version bump, so it takes no part in the fingerprint. Its *presence*
# still counts: adding or removing a lockfile changes the build.
_LOCKFILES = {
    "package-lock.json",
    "npm-shrinkwrap.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "bun.lock",
    "bun.lockb",
    "cargo.lock",
    "poetry.lock",
    "uv.lock",
    "gemfile.lock",
    "composer.lock",
    "pipfile.lock",
    "go.sum",
}

# Manifest keys whose values are version constraints.
_JSON_DEPENDENCY_KEYS = {
    "dependencies",
    "devDependencies",
    "peerDependencies",
    "optionalDependencies",
    "bundleDependencies",
    "bundledDependencies",
    "overrides",
    "resolutions",
    "require",
    "require-dev",
    "conflicts",
    "replaces",
    "provides",
    "suggests",
}

# Manifest keys whose values are lists of requirement strings.
_REQUIREMENT_LIST_KEYS = {
    "dependencies",
    "optional-dependencies",
    "requires",
    "dependency-groups",
}

# TOML tables whose values are dependency declarations.
_TOML_DEPENDENCY_TABLES = {
    "dependencies",
    "dev-dependencies",
    "build-dependencies",
    "optional-dependencies",
    "dependency-groups",
    "requires",
}

# A version constraint on a requirement line: `flask>=2.0`, `~> 7.0`, `==1.2`.
_VERSION_SPEC = re.compile(r"(===|==|>=|<=|~=|!=|~>|<|>|\^|=)\s*v?\d[0-9A-Za-z.\-+*]*")

# A bare version on a requirement line: `github.com/x/y v1.2.3`.
_BARE_VERSION = re.compile(r"(?<![\w.])v\d+\.\d+[0-9A-Za-z.\-+*]*")

# Files whose requirements are one per line.
_LINE_FILES = {
    "go.mod",
    "go.work",
    "gemfile",
    "pipfile",
}
_LINE_PATTERNS = ("requirements*.txt", "requirements*.in", "constraints*.txt")


def _requirement_name(requirement: str) -> str:
    """The package name out of a requirement string, without its version."""
    text = requirement.strip()
    # PEP 508 and npm-style requirements, with extras and environment markers.
    text = re.split(r"[;<\[(]", text, maxsplit=1)[0].strip()
    text = re.split(r"[<>=!~^]", text, maxsplit=1)[0].strip()
    return text


def _mask_json(text: str) -> str | None:
    """The manifest with its dependency versions replaced by the dependency names."""
    try:
        data = json.loads(text)
    except ValueError:
        return None

    def walk(value, key=None):
        if isinstance(value, dict):
            if key in _JSON_DEPENDENCY_KEYS:
                return {"__dependencies__": sorted(str(k) for k in value)}
            return {k: walk(v, k) for k, v in value.items()}
        if isinstance(value, list):
            if key in _JSON_DEPENDENCY_KEYS or key in _REQUIREMENT_LIST_KEYS:
                return sorted(_requirement_name(v) for v in value if isinstance(v, str))
            return [walk(v) for v in value]
        return value

    return json.dumps(walk(data), sort_keys=True, separators=(",", ":"))


def _mask_toml(text: str) -> str | None:
    """The manifest with its dependency versions replaced by the dependency names."""
    try:
        data = tomllib.loads(text)
    except (ValueError, tomllib.TOMLDecodeError):
        return None

    def walk(value, name=None):
        if isinstance(value, dict):
            if name in _TOML_DEPENDENCY_TABLES:
                if isinstance(value, list):
                    return sorted(_requirement_name(v) for v in value if isinstance(v, str))
                return {"__dependencies__": sorted(str(k) for k in value)}
            return {k: walk(v, k) for k, v in value.items()}
        if isinstance(value, list):
            if name in _REQUIREMENT_LIST_KEYS:
                return sorted(_requirement_name(v) for v in value if isinstance(v, str))
            return [walk(v) for v in value]
        return value

    return json.dumps(walk(data), sort_keys=True, separators=(",", ":"))


def _mask_lines(text: str) -> str:
    """Requirements one per line, with the versions taken out."""
    masked = []
    for line in text.splitlines():
        line = _BARE_VERSION.sub("v*", line)
        masked.append(_VERSION_SPEC.sub(r"\1*", line))
    return "\n".join(masked)


def _is_line_file(path: str) -> bool:
    name = PurePosixPath(path).name.lower()
    return name in _LINE_FILES or any(fnmatchcase(name, pat) for pat in _LINE_PATTERNS)


def masked_content(path: str, text: str) -> str | None:
    """The content of a manifest as the fingerprint should see it.

    None means "this file is not a manifest we know how to mask", so its
    content is hashed as it is.
    """
    name = PurePosixPath(path).name.lower()
    if name.endswith(".json"):
        return _mask_json(text)
    if name.endswith(".toml"):
        return _mask_toml(text)
    if _is_line_file(path):
        return _mask_lines(text)
    return None


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


def _read_blob(git: Git, path: str) -> str | None:
    """The content of a tracked file, or None if it cannot be read as text."""
    try:
        return git._run("show", f"HEAD:{path}")
    except Exception:  # noqa: BLE001 - binary files and odd trees are not fatal
        return None


def _digest(path: str, metadata: str, text: str | None) -> str:
    """What the fingerprint sees of one file.

    A lockfile contributes only its presence. A manifest contributes its
    content with the dependency versions taken out. Anything else contributes
    its content as it is.
    """
    parts = metadata.split()
    mode = parts[0] if parts else ""
    kind = parts[1] if len(parts) > 1 else ""
    sha = parts[2] if len(parts) > 2 else ""
    name = PurePosixPath(path).name.lower()
    if name in _LOCKFILES:
        return f"{mode}:lock"
    if kind == "blob" and text is not None:
        masked = masked_content(path, text)
        payload = masked if masked is not None else text
        return f"{mode}:{hashlib.sha256(payload.encode()).hexdigest()}"
    return f"{mode}:{sha}"


def fingerprint(git: Git, inputs: list[str] | None, *, allow_network: bool) -> str:
    """Hash the tracked input paths, modes and content, ignoring version churn."""
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
        content = []
        for path in sorted(selected):
            metadata = entries[path]
            text = _read_blob(git, path) if metadata.split()[1:2] == ["blob"] else None
            content.append((path, _digest(path, metadata, text)))
    payload = json.dumps(
        {"inputs": inputs, "allow_network": allow_network, "content": content},
        sort_keys=True,
        separators=(",", ":"),
    )
    return "recipe-v3:" + hashlib.sha256(payload.encode()).hexdigest()
