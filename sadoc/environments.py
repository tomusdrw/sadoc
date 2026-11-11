"""The environment inside a CI workflow.

A workflow pins more than its actions: it pins the runtime the job runs on.
`node-version: 20`, `python-version: "3.11"`, `go-version: "1.22"` - all of those
go stale, and a stale runtime is often exactly why a dependency update fails to
build.

These are detected separately from the action pins because they are a different
kind of change: the action pins are usually a patch, the runtime is usually a
major, and it needs a human to look at it.
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from . import semver

CACHE_DIR = Path.home() / ".cache" / "sadoc" / "environments"
CACHE_TTL = 12 * 3600
UA = "sadoc/1.0"

# key -> (ecosystem, regex)
_ENV_PATTERNS = {
    "node-version": ("node", re.compile(r"^\s*node-version\s*:\s*[\"']?([0-9][^\"'\s#]*)", re.M)),
    "python-version": (
        "python",
        re.compile(r"^\s*python-version\s*:\s*[\"']?([0-9][^\"'\s#]*)", re.M),
    ),
    "go-version": ("go", re.compile(r"^\s*go-version\s*:\s*[\"']?(go)?([0-9][^\"'\s#]*)", re.M)),
}


@dataclass
class EnvUpdate:
    repo: str
    workflow: str
    ecosystem: str
    key: str
    current: str
    latest: str
    bump_kind: str
    action: str = "manifest"

    def as_dict(self) -> dict:
        return {
            "manifest": self.workflow,
            "ecosystem": self.ecosystem,
            "name": self.key,
            "section": f"with.{self.key}",
            "kind": "runtime",
            "current": self.current,
            "latest": self.latest,
            "bump_kind": self.bump_kind,
            "constraint": self.current,
            "action": self.action,
            "installed": None,
        }


def _cache_get(name: str):
    path = CACHE_DIR / f"{name}.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    if time.time() - data.get("ts", 0) > CACHE_TTL:
        return None
    return data.get("value")


def _cache_put(name: str, value) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        (CACHE_DIR / f"{name}.json").write_text(json.dumps({"ts": time.time(), "value": value}))
    except OSError:
        pass


def _fetch(url: str, timeout: int = 20) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


# ------------------------------------------------------------------ latest
def latest_node() -> str | None:
    """The newest Node LTS release."""
    cached = _cache_get("node")
    if cached:
        return cached
    try:
        releases = json.loads(_fetch("https://nodejs.org/dist/index.json"))
    except (urllib.error.URLError, OSError, json.JSONDecodeError):
        return None
    for release in releases:
        if release.get("lts"):
            version = str(release.get("version", "")).lstrip("v")
            if version:
                _cache_put("node", version)
                return version
    return None


def latest_python() -> str | None:
    """The newest stable Python release."""
    cached = _cache_get("python")
    if cached:
        return cached
    try:
        releases = json.loads(_fetch("https://endoflife.date/api/python.json"))
    except (urllib.error.URLError, OSError, json.JSONDecodeError):
        return None
    for release in releases:
        if release.get("latest") and not release.get("eol"):
            version = str(release["latest"])
            _cache_put("python", version)
            return version
    return None


def latest_go() -> str | None:
    """The newest stable Go release."""
    cached = _cache_get("go")
    if cached:
        return cached
    try:
        releases = json.loads(_fetch("https://go.dev/dl/?mode=json"))
    except (urllib.error.URLError, OSError, json.JSONDecodeError):
        return None
    for release in releases:
        if release.get("stable"):
            version = str(release.get("version", "")).lstrip("go")
            if version:
                _cache_put("go", version)
                return version
    return None


LATEST = {
    "node": latest_node,
    "python": latest_python,
    "go": latest_go,
}


def latest_for(ecosystem: str) -> str | None:
    lookup = LATEST.get(ecosystem)
    return lookup() if lookup else None


# --------------------------------------------------------------- detection
def find_environment_versions(text: str) -> list[tuple[str, str, str]]:
    """The runtime versions pinned in a workflow.

    Returns (key, ecosystem, current) for each one that is pinned to a concrete
    version. A workflow that reads the version from a file (`go-version-file`,
    `node-version-file`) is left alone - it is already in step with the project.
    """
    found: list[tuple[str, str, str]] = []
    for key, (ecosystem, pattern) in _ENV_PATTERNS.items():
        for match in pattern.finditer(text):
            value = match.group(1)
            if ecosystem == "go":
                value = match.group(2)
            value = value.strip().strip("\"'")
            # "20.x", "3.12" and "1.22" are all fine; a file reference is not.
            if not value or value.startswith(".") or "${{" in value:
                continue
            if value.endswith(".x"):
                value = value[:-2]
            found.append((key, ecosystem, value))
            break
    # A workflow that reads the version from a file is managed elsewhere.
    for key in ("go-version-file", "node-version-file", "python-version-file"):
        if re.search(rf"^\s*{key}\s*:", text, re.M):
            found = [f for f in found if f[0] != key.removesuffix("-file")]
    return found


def detect_updates(repo: str, workflow: str, text: str) -> list[EnvUpdate]:
    """The runtime updates worth making in one workflow."""
    updates: list[EnvUpdate] = []
    for key, ecosystem, current in find_environment_versions(text):
        latest = latest_for(ecosystem)
        if not latest:
            continue
        current_v = semver.parse(current)
        latest_v = semver.parse(latest)
        if current_v is None or latest_v is None:
            continue
        if semver.compare(latest_v, current_v) <= 0:
            continue
        bump = semver.classify(current, latest) or "minor"
        updates.append(
            EnvUpdate(
                repo=repo,
                workflow=workflow,
                ecosystem=ecosystem,
                key=key,
                current=current,
                latest=latest,
                bump_kind=bump,
            )
        )
    return updates
