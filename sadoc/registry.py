"""Registry version lookups.

Every lookup goes to the upstream registry directly - never to an LLM. Results are
cached on disk (JSON, keyed by ecosystem/name) so a re-scan is cheap.
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from pathlib import Path

CACHE_DIR = Path.home() / ".cache" / "sadoc"
CACHE_TTL = 6 * 3600
UA = "sadoc/1.0"

# Ecosystems where the "latest" version is a moving tag we should not chase.
_SKIP_LATEST_TAGS = {"latest", "stable", "main", "master", "nightly", "edge", "canary"}


class RegistryError(RuntimeError):
    pass


def _cache_path(ecosystem: str, name: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", name)
    return CACHE_DIR / ecosystem / f"{safe}.json"


def _cache_get(ecosystem: str, name: str):
    p = _cache_path(ecosystem, name)
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    if time.time() - data.get("ts", 0) > CACHE_TTL:
        return None
    return data.get("value")


def _cache_put(ecosystem: str, name: str, value) -> None:
    p = _cache_path(ecosystem, name)
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        p.write_text(json.dumps({"ts": time.time(), "value": value}))
    except OSError:
        pass


def _fetch(url: str, timeout: int = 20, headers: dict | None = None) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


# ------------------------------------------------------------------ crates
def crates_io_latest(name: str) -> str | None:
    """Latest *stable* version of a crate (ignores prereleases)."""
    cached = _cache_get("crates", name)
    if cached is not None:
        return cached
    try:
        raw = _fetch(f"https://crates.io/api/v1/crates/{name}")
    except (urllib.error.URLError, OSError):
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    version = (data.get("crate") or {}).get("max_stable_version")
    if not version:
        versions = [
            v["num"]
            for v in data.get("versions", [])
            if not v.get("yanked") and "-" not in v["num"]
        ]
        version = versions[0] if versions else None
    if version:
        _cache_put("crates", name, version)
    return version


# --------------------------------------------------------------------- npm
def npm_latest(name: str) -> str | None:
    cached = _cache_get("npm", name)
    if cached is not None:
        return cached
    try:
        raw = _fetch(f"https://registry.npmjs.org/{name.replace('/', '%2F')}")
    except (urllib.error.URLError, OSError):
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    dist = data.get("dist-tags", {}).get("latest")
    if not dist:
        return None
    _cache_put("npm", name, dist)
    return dist


# -------------------------------------------------------------------- pypi
def pypi_latest(name: str) -> str | None:
    cached = _cache_get("pypi", name)
    if cached is not None:
        return cached
    try:
        raw = _fetch(f"https://pypi.org/pypi/{name}/json")
    except (urllib.error.URLError, OSError):
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    version = (data.get("info") or {}).get("version")
    if version:
        _cache_put("pypi", name, version)
    return version


# ---------------------------------------------------------------------- go
def go_latest(module: str) -> str | None:
    cached = _cache_get("go", module)
    if cached is not None:
        return cached
    escaped = re.sub(r"([A-Z])", lambda m: "!" + m.group(1).lower(), module)
    try:
        raw = _fetch(f"https://proxy.golang.org/{escaped}/@latest")
    except (urllib.error.URLError, OSError):
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    version = data.get("Version")
    if version:
        _cache_put("go", module, version)
    return version


# -------------------------------------------------------------- gh actions
def action_latest(action: str, gh=None) -> str | None:
    """Latest release tag of a GitHub Action, e.g. "actions/checkout" -> "v5.0.0"."""
    cached = _cache_get("action", action)
    if cached is not None:
        return cached
    version = None
    if gh is not None:
        owner, _, repo = action.partition("/")
        version = gh.action_latest_release(owner, repo)
    if not version:
        try:
            raw = _fetch(f"https://api.github.com/repos/{action}/releases/latest")
            data = json.loads(raw)
            version = data.get("tag_name")
        except (urllib.error.URLError, OSError, json.JSONDecodeError):
            return None
    if version:
        _cache_put("action", action, version)
    return version


# ------------------------------------------------------------------ docker
_TAG_RE = re.compile(
    r"^v?(?P<major>\d+)(?:\.(?P<minor>\d+))?(?:\.(?P<patch>\d+))?"
    r"(?P<suffix>[-.+].*)?$"
)


def _tag_parts(tag: str):
    """(major, minor, patch) for a numeric tag, or None."""
    match = _TAG_RE.match(tag.strip())
    if not match:
        return None
    return (
        int(match.group("major")),
        int(match.group("minor") or 0),
        int(match.group("patch") or 0),
    )


def docker_latest(image: str, current_tag: str | None = None) -> str | None:
    """Latest tag for a Docker Hub image (best effort, no auth).

    Docker tags are ambiguous: "latest" may be a date, a distro suffix, or a
    completely different product line. The rule here is deliberately narrow -
    only a tag from **the same version series** is offered, so "3.21" becomes
    "3.21.10" and "20-bullseye-slim" becomes "20.20-bullseye-slim". A jump to a
    different minor or major is a decision, not an update, so it is left to a
    human.
    """
    if not current_tag:
        return None
    match = _TAG_RE.match(current_tag.strip())
    if not match:
        return None
    major = match.group("major")
    minor = match.group("minor")
    suffix = match.group("suffix") or ""
    series = f"{major}.{minor}" if minor else major
    current_parts = _tag_parts(current_tag)

    repo = image if "/" in image else f"library/{image}"
    # The registry's name filter matches the whole tag, so the suffix has to be
    # part of the query: "20-bullseye-slim" finds the bullseye variants only.
    try:
        raw = _fetch(
            f"https://hub.docker.com/v2/repositories/{repo}/tags"
            f"?page_size=100&name={series}{suffix}"
        )
        data = json.loads(raw)
    except (urllib.error.URLError, OSError, json.JSONDecodeError):
        return None

    best = None
    best_key = None
    for entry in data.get("results") or []:
        tag = entry.get("name")
        if not tag or tag in _SKIP_LATEST_TAGS:
            continue
        candidate = _TAG_RE.match(tag.strip())
        if not candidate:
            continue
        # The same suffix: a different base distribution is not an update.
        if (candidate.group("suffix") or "") != suffix:
            continue
        # The same series.
        if candidate.group("major") != major:
            continue
        if minor and candidate.group("minor") != minor:
            continue
        parts = _tag_parts(tag)
        if parts is None or current_parts is None:
            continue
        # Strictly newer - never propose a downgrade.
        if parts <= current_parts:
            continue
        if best_key is None or parts > best_key:
            best, best_key = tag, parts
    return best


# ---------------------------------------------------------------- dispatch
def ecosystem_for(dep) -> str:
    """Which registry should we ask for this dependency?"""
    if dep.kind == "action":
        return "action"
    manifest = dep.manifest
    base = manifest.rsplit("/", 1)[-1]
    if base == "Cargo.toml":
        return "crates"
    if base == "package.json":
        return "npm"
    if base == "go.mod":
        return "go"
    if base == "Dockerfile":
        return "docker"
    if base in ("requirements.txt", "pyproject.toml") or base.startswith("requirements"):
        return "pypi"
    return "unknown"


def latest_for(dep, gh=None) -> str | None:
    eco = ecosystem_for(dep)
    if eco == "crates":
        return crates_io_latest(dep.name)
    if eco == "npm":
        return npm_latest(dep.name)
    if eco == "pypi":
        return pypi_latest(dep.name)
    if eco == "go":
        return go_latest(dep.name)
    if eco == "action":
        return action_latest(dep.name, gh=gh)
    if eco == "docker":
        return docker_latest(dep.name, dep.current)
    return None
