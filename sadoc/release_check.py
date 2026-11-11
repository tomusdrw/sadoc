"""Working out whether a repository owes a release, and to whom.

After a dependency pull request is merged the project version may have moved on,
and the release that goes with it may be somebody's job. What counts as a release
differs per repository - crates.io, npm, PyPI, a GitHub release, or nothing at all
because the project deploys straight from the default branch - so the mechanism is
worked out from the repository itself, and can be stated in the README.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from . import releases
from .gh import Gh

# The files worth reading to work out the release mechanism.
_INTERESTING = re.compile(
    r"^(Cargo\.toml|package\.json|pyproject\.toml|README\.md|readme\.md|"
    r"VERSION|version\.txt|\.sadoc\.toml|sadoc\.toml)$"
)


@dataclass
class ReleaseStatus:
    repo: str
    kind: str = releases.NONE
    kinds: list[str] = None
    version: str = ""
    published: str = ""
    due: bool = False
    command: str = ""
    source: str = ""
    note: str = ""
    # One entry per package the repository publishes.
    packages: list[tuple[str, str]] = None

    def __post_init__(self):
        if self.kinds is None:
            self.kinds = []
        if self.packages is None:
            self.packages = []

    def summary(self) -> str:
        """One line about the version and what has been published."""
        if not self.packages:
            return ""
        if len(self.packages) == 1:
            return f"{self.packages[0][1]} (published {self.published or 'nothing yet'})"
        listed = ", ".join(f"{name} {version}" for name, version in self.packages)
        return f"{listed} (published {self.published or 'nothing yet'})"


def _read_files(gh: Gh, owner: str, repo: str, branch: str) -> dict[str, str]:
    """The files that matter for deciding how the repository releases."""
    files: dict[str, str] = {}
    for name in (
        "Cargo.toml",
        "package.json",
        "pyproject.toml",
        "README.md",
        "readme.md",
        "VERSION",
        "version.txt",
        ".sadoc.toml",
        "sadoc.toml",
    ):
        try:
            files[name] = gh.file_text(owner, repo, name, ref=branch)
        except Exception:
            continue
    try:
        tree = gh.repo_tree(owner, repo, branch)
    except Exception:
        tree = []
    for entry in tree:
        path = entry.get("path", "")
        if not path.startswith(".github/workflows/"):
            continue
        if not path.endswith((".yml", ".yaml")):
            continue
        try:
            files[path] = gh.file_text(owner, repo, path, ref=branch)
        except Exception:
            continue
    # The crates of a Cargo workspace each carry their own version.
    for entry in tree:
        path = entry.get("path", "")
        if not path.endswith("Cargo.toml") or path.count("/") > 2:
            continue
        try:
            files[path] = gh.file_text(owner, repo, path, ref=branch)
        except Exception:
            continue
    return files


def latest_published_version(
    gh: Gh, owner: str, repo: str, packages: list[tuple[str, str]] | None = None
) -> str:
    """The newest published version.

    Tags and releases only tell part of the story: a crate published to
    crates.io may never have had a git tag, and an npm package is published
    without one either. So the registries are asked as well.
    """
    best = ""
    for candidate in _tag_versions(gh, owner, repo):
        if not best or _newer(candidate, best):
            best = candidate
    for name, _version in packages or []:
        published = _registry_version(name)
        if published and (not best or _newer(published, best)):
            best = published
    return best


def _newer(candidate: str, current: str) -> bool:
    from . import semver

    a = semver.parse(candidate)
    b = semver.parse(current)
    if a is None:
        return False
    if b is None:
        return True
    return semver.compare(a, b) > 0


def _registry_version(package: str) -> str:
    """The newest version of a published package, from its registry."""
    from . import registry

    kind, _, name = package.partition(":")
    try:
        if kind == "cargo":
            return registry.crates_io_latest(name) or ""
        if kind == "npm":
            return registry.npm_latest(name) or ""
    except Exception:  # noqa: BLE001
        return ""
    return ""


def _tag_versions(gh: Gh, owner: str, repo: str) -> list[str]:
    """The versions that have been tagged or released."""

    found: list[str] = []
    try:
        releases_list = gh.api_paginate(f"repos/{owner}/{repo}/releases", limit=50)
    except Exception:
        releases_list = []
    for release in releases_list:
        if release.get("draft"):
            continue
        tag = release.get("tag_name") or ""
        version = _version_from_tag(tag)
        if version:
            found.append(version)
    try:
        tags = gh.api_paginate(f"repos/{owner}/{repo}/tags", limit=50)
    except Exception:
        tags = []
    for tag in tags:
        version = _version_from_tag(tag.get("name") or "")
        if version:
            found.append(version)
    return found


def _version_from_tag(tag: str) -> str:
    """The version in a tag name, if the tag names one at all."""
    from . import semver

    candidate = re.sub(r"^[vV]", "", tag.split("-")[0].split("+")[0])
    if semver.parse(candidate) is None:
        return ""
    return candidate


def check(gh: Gh, owner: str, repo: str, branch: str) -> ReleaseStatus:
    """Work out whether this repository owes a release."""
    status = ReleaseStatus(repo=f"{owner}/{repo}")
    files = _read_files(gh, owner, repo, branch)
    if not files:
        status.note = "no manifest or README found"
        return status

    status.packages = releases.project_versions(files)
    status.version = status.packages[0][1] if status.packages else ""
    kinds, command, source = releases.detect_kinds(files)
    status.kinds = kinds
    status.command = command
    status.source = source
    status.kind = kinds[0] if kinds else releases.NONE
    status.published = latest_published_version(gh, owner, repo, status.packages)
    # A release is only owed if we know what releasing means here. A repository
    # that deploys from its default branch owes nothing, and one with no
    # release process at all is a question, not a debt.
    releasable = [k for k in status.kinds if k not in (releases.AUTO, releases.NONE)]
    status.due = bool(releasable) and any(
        releases.release_due(version, status.published) for _, version in status.packages
    )
    status.note = releases.describe(status)
    return status


def nudge_text(status: ReleaseStatus) -> str:
    """What to say about a release that is due."""
    lines = []
    if status.version:
        lines.append(f"The project is at {status.version}.")
    if status.published:
        lines.append(f"The last published version is {status.published}.")
    lines.append(status.note)
    if status.command:
        lines.append(f"Command: {status.command}")
    return "\n".join(lines)
