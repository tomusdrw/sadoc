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

import json
import re
import tomllib
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
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
    (
        "PyPI",
        re.compile(
            r"\b(twine\s+upload|uv\s+publish|poetry\s+publish|pypa/gh-action-pypi-publish)\b"
        ),
    ),
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
    for name in (".sadoc.toml", "sadoc.toml"):
        config = _toml(repo_files.get(name, "")).get("release", {})
        if not isinstance(config, dict):
            continue
        kinds = _parse_kinds(str(config.get("kind", "")))
        if kinds:
            return kinds, str(config.get("command", ""))
    for name in ("README.md", "readme.md"):
        text = _without_markdown_code(repo_files.get(name, ""))
        for match in re.finditer(r"<!--\s*sadoc:\s*release\s*=\s*([^\n>]+?)-->", text, re.I):
            kinds = _parse_kinds(match.group(1))
            if kinds:
                return kinds, ""
    return [], ""


def _toml(text: str) -> dict:
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return {}


def _json(text: str) -> dict:
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, TypeError):
        return {}


def _without_markdown_code(text: str) -> str:
    lines = []
    fence = ""
    for line in text.splitlines():
        match = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", line)
        if fence:
            if (
                match
                and match[1][0] == fence[0]
                and len(match[1]) >= len(fence)
                and not match[2].strip()
            ):
                fence = ""
            continue
        if match:
            fence = match[1]
        elif not line.startswith(("    ", "\t")):
            lines.append(re.sub(r"(`+).*?\1", "", line))
    return "\n".join(lines)


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


def project_versions(
    repo_files: dict[str, str], *, publishable_only: bool = False
) -> list[tuple[str, str]]:
    """Package identities and versions, read from their actual manifest sections.

    Private packages can have GitHub releases but must not be queried in public
    registries. Cargo workspace-inherited versions use the workspace value.
    """
    out = []
    workspace = _toml(repo_files.get("Cargo.toml", "")).get("workspace", {}).get("package", {})
    for path, text in repo_files.items():
        kind = ""
        data = {}
        private = False
        if path == "Cargo.toml" or path.endswith("/Cargo.toml"):
            kind = "cargo"
            data = _toml(text).get("package", {})
            data = {
                key: workspace.get(key)
                if isinstance(value, dict) and value.get("workspace")
                else value
                for key, value in data.items()
            }
            private = (
                data.get("publish") is False
                or isinstance(data.get("publish"), list)
                and "crates-io" not in data["publish"]
            )
        elif path == "package.json" or path.endswith("/package.json"):
            kind = "npm"
            data = _json(text)
            private = data.get("private") is True
        elif path == "pyproject.toml":
            kind = "pypi"
            config = _toml(text)
            data = config.get("project") or config.get("tool", {}).get("poetry", {})
        version = data.get("version")
        name = data.get("name")
        if kind and isinstance(version, str) and version:
            if publishable_only and (private or not name):
                continue
            out.append((f"{kind}:{name}" if name else kind, version))
    if not publishable_only:
        for name in ("VERSION", "version.txt"):
            text = repo_files.get(name, "").strip()
            if text:
                out.append(("version", text.splitlines()[0].strip()))
    return out


def project_version(repo_files: dict[str, str]) -> str:
    """The single version of the project, if there is only one."""
    versions = project_versions(repo_files)
    return versions[0][1] if len(versions) == 1 else ""


def _yaml_block(text: str, key: str) -> str:
    """Read a simple YAML key's indented block; not a general YAML parser.

    Only the workflow's top-level `on` block is passed to event detection, so
    step names, scripts, and comments cannot masquerade as event declarations.
    """
    lines = text.splitlines()
    for index, line in enumerate(lines):
        match = re.match(r"^( *)[\"']?" + re.escape(key) + r"[\"']?:\s*(.*)$", line)
        if not match:
            continue
        indent = len(match[1])
        body = [match[2].split(" #", 1)[0]]
        for child in lines[index + 1 :]:
            if child.strip() and not child.lstrip().startswith("#"):
                if len(child) - len(child.lstrip()) <= indent:
                    break
                body.append(child)
        return "\n".join(body)
    # Flow-style trigger/filter maps, e.g. on: {push: {tags: ["v*"]}}.
    match = re.search(r"[{,]\s*[\"']?" + re.escape(key) + r"[\"']?:\s*(\[[^\]]*\]|[^,}]+)", text)
    return match[1] if match else ""


def _event(on: str, name: str) -> bool:
    """Look only at event keys, never nested filters or dispatch inputs."""
    value = on.strip()
    if value.startswith("{"):
        tokens = re.findall(r"\"(?:\\.|[^\"])*\"|'(?:''|[^'])*'|[{}\[\],:]|[^\s{}\[\],:]+", value)
        depth = 0
        for index, token in enumerate(tokens):
            if token in ("{", "["):
                depth += 1
            elif token in ("}", "]"):
                depth -= 1
            elif (
                depth == 1 and token.strip("\"'") == name and tokens[index + 1 : index + 2] == [":"]
            ):
                return True
        return False
    if value.startswith("["):
        return name in [item.strip().strip("\"'") for item in value.strip("[]").split(",")]
    if ":" not in value:
        return value.strip("\"'") == name
    lines = [line for line in on.splitlines() if line.strip() and not line.lstrip().startswith("#")]
    indent = min((len(line) - len(line.lstrip()) for line in lines), default=0)
    return any(
        re.match(r"^ {" + str(indent) + r"}[\"']?" + name + r"[\"']?:", line) for line in lines
    )


def _branch_matches(branch: str, filters: str, default: bool) -> bool:
    matched = default
    for pattern in re.findall(r"[\w*/!.-]+", filters):
        negative = pattern.startswith("!")
        if fnmatchcase(branch, pattern.lstrip("!")):
            matched = not negative
    return matched


def _workflow_commands(text: str) -> str:
    """Only executable steps/actions count as publishing evidence."""
    commands = []
    lines = text.splitlines()
    for index, line in enumerate(lines):
        match = re.match(r"^( *)(?:-\s+)?(?:run|uses):\s*(.*)$", line)
        if not match:
            continue
        value = match[2]
        if value.startswith(("|", ">")):
            indent = len(match[1]) + (2 if line.lstrip().startswith("-") else 0)
            for child in lines[index + 1 :]:
                if child.strip() and len(child) - len(child.lstrip()) <= indent:
                    break
                if not child.lstrip().startswith("#"):
                    commands.append(child)
        else:
            commands.append(value.split(" #", 1)[0])
    return "\n".join(commands)


def detect_kinds(
    repo_files: dict[str, str],
    default_branch: str = "main",
    published_kinds: tuple[str, ...] = (),
) -> tuple[list[str], str, str]:
    """Infer destinations and the initiating action, keeping automation intact."""
    kinds, command = read_annotations(repo_files)
    if kinds:
        return kinds, command, "annotation"

    found: list[str] = []
    commands: list[str] = []
    automatic = False
    automatic_kinds: set[str] = set()
    for name, text in repo_files.items():
        if not name.startswith(".github/workflows/") or not name.endswith((".yml", ".yaml")):
            continue
        on = _yaml_block(text, "on")
        steps = _workflow_commands(text)
        publishes = [kind for kind, pattern in _PUBLISH_PATTERNS if pattern.search(steps)]
        release_event = _event(on, "release")
        tags = _yaml_block(on, "tags")
        push = _event(on, "push")
        branches = _yaml_block(on, "branches")
        ignored = _yaml_block(on, "branches-ignore")
        on_default = (
            push
            and not tags
            and _branch_matches(default_branch, branches, default=not branches)
            and not _branch_matches(default_branch, ignored, default=False)
        )
        if (
            on_default
            and not release_event
            and "refs/tags/" not in text
            and (publishes or _DEPLOY_PATTERNS.search(steps))
        ):
            automatic = True
            automatic_kinds.update(publishes)
            continue
        if release_event and steps:
            if GITHUB not in publishes:
                publishes.append(GITHUB)
            action = "gh release create"
        elif push and tags and publishes:
            action = "Create and push a tag matching " + " ".join(tags.split())
        elif _event(on, "workflow_dispatch") and publishes:
            action = f"gh workflow run {name.rsplit('/', 1)[-1]}"
        else:
            action = ""
        for kind in publishes:
            if kind not in found:
                found.append(kind)
        if action and action not in commands:
            commands.append(action)
    source = "workflow" if found else "publication history"
    for kind in published_kinds:
        if kind not in found and kind not in automatic_kinds:
            found.append(kind)
    if found and source == "workflow":
        return found, " or ".join(commands), source
    scripts = _json(repo_files.get("package.json", "")).get("scripts", {})
    if isinstance(scripts, dict):
        for name, script in scripts.items():
            if not isinstance(script, str):
                continue
            kinds = [
                kind
                for kind, pattern in _PUBLISH_PATTERNS
                if kind not in automatic_kinds and pattern.search(script)
            ]
            if kinds:
                return list(dict.fromkeys([*found, *kinds])), f"npm run {name}", "script"
    if found:
        return found, "gh release create" if found == [GITHUB] else "", source
    if automatic:
        return [AUTO], "", "workflow"
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
        return not published
    return semver.compare(current, latest) > 0


def describe(info: ReleaseInfo) -> str:
    """A sentence about what a release would mean here."""
    if info.kind == AUTO:
        return (
            "Publishing/deployment is automated from the default branch; "
            "no manual release action is needed."
        )
    if info.kind == NONE or not info.kinds:
        return (
            "No release process was found. If this project is published "
            "somewhere, say how in the README (for example "
            "`<!-- sadoc: release = npm -->`)."
        )
    if info.command:
        return f"Release entry point: {info.command}."
    if info.source != "annotation":
        return (
            "Release destinations: "
            + ", ".join(info.kinds)
            + ". No initiating command was identified."
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
