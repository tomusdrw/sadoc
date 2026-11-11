"""What a release means for a repository.

There is no single answer. A Rust library publishes to crates.io, a Node package to
npm, a Python one to PyPI, some projects cut a GitHub release, and some deploy
straight from `main` when it is green - in which case there is nothing to release
at all.

This module works out which of those a repository does, and whether a release is
due: the version in the manifest compared with the latest tag or release.

The answer can also be stated explicitly. A repository can say what it does in
its README:

    <!-- sadoc: release = crates.io -->
    <!-- sadoc: release = npm, github -->
    <!-- sadoc: release = none -->

or in `.sadoc.toml`:

    [release]
    kind = "crates.io"
    command = "cargo publish"
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from . import semver

# The kinds of release we know about.
CRATES = "crates.io"
NPM = "npm"
PYPI = "PyPI"
GITHUB = "github"
AUTO = "auto-deploy"
NONE = "none"

ALL_KINDS = (CRATES, NPM, PYPI, GITHUB, AUTO, NONE)

# Commands that publish.
_PUBLISH_PATTERNS = (
    ("crates.io", re.compile(r"\bcargo\s+publish\b")),
    ("npm", re.compile(r"\bnpm\s+publish\b")),
    ("PyPI", re.compile(r"\b(twine|uv\s+publish|poetry\s+publish|python\s+-m\s+build)\b")),
    ("github", re.compile(r"\bgh\s+release\s+create\b|softprops/action-gh-release")),
)

# A workflow that deploys straight from the default branch.
_DEPLOY_PATTERNS = re.compile(
    r"\b(docker/build-push-action|deploy-pages|npm\s+publish|cargo\s+publish|"
    r"docker\s+push|flyctl\s+deploy|railway\s+up|vercel|wrangler\s+(deploy|publish))\b"
)


@dataclass
class ReleaseInfo:
    repo: str
    kind: str = NONE
    version: str = ""
    latest_published: str = ""
    due: bool = False
    command: str = ""
    source: str = ""  # where the answer came from
    notes: str = ""
    kinds: list[str] = field(default_factory=list)


def read_annotations(repo_files: dict[str, str]) -> tuple[list[str], str]:
    """Read an explicit release declaration from the README or a config file.

    Returns (kinds, command).
    """
    for name in ("README.md", "readme.md", ".sadoc.toml", "sadoc.toml"):
        text = repo_files.get(name)
        if not text:
            continue
        # <!-- sadoc: release = crates.io -->
        for match in re.finditer(r"<!--\s*sadoc:\s*release\s*=\s*([^\n>]+?)-->", text, re.I):
            kinds = _parse_kinds(match.group(1))
            if kinds:
                return kinds, ""
        # [release] kind = "crates.io"  (in .sadoc.toml)
        match = re.search(r"^\s*kind\s*=\s*[\"']([^\"']+)[\"']", text, re.M)
        if match:
            kinds = _parse_kinds(match.group(1))
            command = ""
            cmd = re.search(r"^\s*command\s*=\s*[\"']([^\"']+)[\"']", text, re.M)
            if cmd:
                command = cmd.group(1)
            if kinds:
                return kinds, command
    return [], ""


def _parse_kinds(text: str) -> list[str]:
    out = []
    for part in re.split(r"[,\s]+", text.strip().lower()):
        part = part.strip()
        if not part:
            continue
        if part in ("crates.io", "crates", "cargo"):
            out.append(CRATES)
        elif part in ("npm", "node", "js"):
            out.append(NPM)
        elif part in ("pypi", "python", "pip"):
            out.append(PYPI)
        elif part in ("github", "gh"):
            out.append(GITHUB)
        elif part in ("auto", "auto-deploy", "autodeploy"):
            out.append(AUTO)
        elif part in ("none", "no", "false"):
            out.append(NONE)
    return out


def project_versions(repo_files: dict[str, str]) -> list[tuple[str, str]]:
    """The versions the project is currently at.

    A repository can publish more than one package - a Rust crate and an npm
    package with different versions - so this returns one entry per package.
    """
    out: list[tuple[str, str]] = []
    cargo = repo_files.get("Cargo.toml")
    if cargo:
        match = re.search(r"^\s*version\s*=\s*\"([^\"]+)\"", cargo, re.M)
        if match:
            out.append(("cargo", match.group(1)))
    # The crates of a workspace each carry their own version.
    for path, text in repo_files.items():
        if not path.endswith("Cargo.toml") or path == "Cargo.toml":
            continue
        match = re.search(r"^\s*version\s*=\s*\"([^\"]+)\"", text, re.M)
        if not match:
            continue
        crate = re.search(r"^\s*name\s*=\s*\"([^\"]+)\"", text, re.M)
        if crate:
            crate_name = crate.group(1)
        else:
            crate_name = path.rsplit("/", 1)[0].rsplit("/", 1)[-1]
        out.append((f"cargo:{crate_name}", match.group(1)))
    package = repo_files.get("package.json")
    if package:
        import json

        try:
            version = str(json.loads(package).get("version", ""))
        except (json.JSONDecodeError, TypeError):
            version = ""
        if version:
            out.append(("npm", version))
    pyproject = repo_files.get("pyproject.toml")
    if pyproject:
        match = re.search(r"^\s*version\s*=\s*\"([^\"]+)\"", pyproject, re.M)
        if match:
            out.append(("pypi", match.group(1)))
    for name in ("VERSION", "version.txt"):
        text = repo_files.get(name)
        if text and text.strip():
            out.append(("version", text.strip().splitlines()[0].strip()))
    return out


def project_version(repo_files: dict[str, str]) -> str:
    """The single version of the project, if there is only one."""
    versions = project_versions(repo_files)
    return versions[0][1] if len(versions) == 1 else ""


def detect_kinds(repo_files: dict[str, str]) -> tuple[list[str], str, str]:
    """Work out how the repository releases.

    Returns (kinds, command, source).
    """
    kinds, command = read_annotations(repo_files)
    if kinds:
        return kinds, command, "annotation"

    workflows = {
        name: text
        for name, text in repo_files.items()
        if name.startswith(".github/workflows/") and name.endswith((".yml", ".yaml"))
    }
    found: list[str] = []
    publishes_on_main = False
    for _name, text in workflows.items():
        for kind, pattern in _PUBLISH_PATTERNS:
            if pattern.search(text) and kind not in found:
                found.append(kind)
        # A workflow that publishes or deploys straight from the default branch.
        if _DEPLOY_PATTERNS.search(text) and re.search(r"^\s*(push|pull_request):", text, re.M):
            publishes_on_main = True

    if not found and publishes_on_main:
        return [AUTO], "", "workflow"
    if found:
        # A repository that publishes somewhere has a real release, even if it
        # also deploys from the default branch.
        return found, "", "workflow"
    return [], "", ""


def release_due(version: str, published: str) -> bool:
    """Is the project ahead of what has been published?"""
    if not version:
        return False
    current = semver.parse(version)
    latest = semver.parse(published) if published else None
    if current is None:
        return False
    # A placeholder version means the project has never been released.
    if current.major == 0 and current.minor == 0 and current.patch == 0:
        return False
    if latest is None:
        return True
    return semver.compare(current, latest) > 0


def describe(info: ReleaseInfo) -> str:
    """A sentence about what a release would mean here."""
    if info.kind == AUTO:
        return (
            "This repository deploys from the default branch, so there is nothing "
            "to publish - the version bump is enough."
        )
    if info.kind == NONE or not info.kinds:
        return (
            "No release process was found. If this project is published "
            "somewhere, say how in the README (for example "
            "`<!-- sadoc: release = npm -->`)."
        )
    commands = {
        CRATES: "cargo publish",
        NPM: "npm publish",
        PYPI: "python -m build && twine upload dist/*",
        GITHUB: "gh release create",
    }
    steps = [commands.get(k, k) for k in info.kinds if k != AUTO]
    return "A release here means: " + ", ".join(steps) + "."


def load_config(path: str | Path) -> dict:
    """Read a `.sadoc.toml` if there is one."""
    path = Path(path)
    if not path.exists():
        return {}
    import tomllib

    with path.open("rb") as fh:
        data = tomllib.load(fh)
    return data.get("release", {}) or {}
