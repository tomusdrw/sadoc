"""Lockfile parsing.

A manifest states a *constraint* ("1.0" in Cargo means ">=1.0, <2.0"); the lockfile
states what is actually *installed*. Both are needed to decide what a dependency
update really requires:

  constraint permits latest, installed < latest  ->  lockfile-only refresh
  constraint excludes latest                       ->  the manifest has to change

Getting this wrong makes the bot rewrite manifests that did not need it.
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path


def parse_cargo_lock(text: str) -> dict[str, str]:
    try:
        data = tomllib.loads(text)
    except Exception:
        return {}
    out = {}
    for pkg in data.get("package", []) or []:
        name, version = pkg.get("name"), pkg.get("version")
        if name and version:
            out.setdefault(name, version)
    return out


def parse_poetry_lock(text: str) -> dict[str, str]:
    try:
        data = tomllib.loads(text)
    except Exception:
        return {}
    out = {}
    for pkg in data.get("package", []) or []:
        name, version = pkg.get("name"), pkg.get("version")
        if name and version:
            out.setdefault(name, version)
    return out


def parse_uv_lock(text: str) -> dict[str, str]:
    try:
        data = tomllib.loads(text)
    except Exception:
        return {}
    out = {}
    for pkg in data.get("package", []) or []:
        name, version = pkg.get("name"), pkg.get("version")
        if name and version:
            out.setdefault(name, version)
    return out


def parse_package_lock(text: str) -> dict[str, str]:
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return {}
    out: dict[str, str] = {}
    packages = data.get("packages")
    if isinstance(packages, dict):
        # A package can appear several times (nested node_modules). The version
        # that matters is the hoisted one - the shallowest path - so keep the
        # entry with the fewest path segments.
        depth: dict[str, int] = {}
        for path, meta in packages.items():
            if not isinstance(meta, dict):
                continue
            version = meta.get("version")
            if not version:
                continue
            name = meta.get("name")
            if not name:
                # npm v2/v3 lockfiles key by path: "node_modules/@scope/pkg"
                marker = "node_modules/"
                if marker in path:
                    name = path.split(marker, 1)[1]
                else:
                    continue
            levels = path.count("node_modules/")
            if name in depth and depth[name] <= levels:
                continue
            depth[name] = levels
            out[name] = version
    deps = data.get("dependencies")
    if isinstance(deps, dict):
        for name, meta in deps.items():
            if isinstance(meta, dict) and meta.get("version"):
                out.setdefault(name, meta["version"])
    return out


def parse_pnpm_lock(text: str) -> dict[str, str]:
    """pnpm-lock.yaml: packages keyed as "/name@1.2.3" or "name@1.2.3".

    Parsed line by line rather than with a YAML library, so the project keeps
    its promise of having no runtime dependencies.
    """
    out: dict[str, str] = {}
    in_packages = False
    for raw in text.splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue
        if not line.startswith(" "):
            in_packages = line.startswith("packages:")
            continue
        if not in_packages:
            continue
        stripped = line.strip()
        if not stripped.endswith(":") or stripped.startswith("-"):
            continue
        spec = stripped[:-1].lstrip("/")
        if "@" not in spec:
            continue
        name, _, version = spec.rpartition("@")
        if not name or version.startswith("("):
            continue
        out.setdefault(name, version)
    return out


def parse_yarn_lock(text: str) -> dict[str, str]:
    """Classic yarn.lock v1: entries like `name@^1.0.0:\n  version "1.2.3"`."""
    out: dict[str, str] = {}
    name = None
    for line in text.splitlines():
        if not line.strip():
            continue
        if not line.startswith(" ") and line.rstrip().endswith(":"):
            header = line.rstrip()[:-1]
            first = header.split(",")[0].strip().strip('"')
            name = first.rsplit("@", 1)[0] if "@" in first[1:] else first
            continue
        stripped = line.strip()
        if name and stripped.startswith("version"):
            parts = stripped.split(None, 1)
            if len(parts) == 2:
                out.setdefault(name, parts[1].strip().strip('"'))
            name = None
    return out


def parse_composer_lock(text: str) -> dict[str, str]:
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return {}
    out = {}
    for section in ("packages", "packages-dev"):
        for pkg in data.get(section, []) or []:
            name, version = pkg.get("name"), pkg.get("version")
            if name and version:
                out.setdefault(name, version.lstrip("v"))
    return out


PARSERS = {
    "Cargo.lock": parse_cargo_lock,
    "poetry.lock": parse_poetry_lock,
    "uv.lock": parse_uv_lock,
    "package-lock.json": parse_package_lock,
    "pnpm-lock.yaml": parse_pnpm_lock,
    "pnpm-lock.yml": parse_pnpm_lock,
    "yarn.lock": parse_yarn_lock,
    "composer.lock": parse_composer_lock,
}


def parse_lockfile(path: str, text: str) -> dict[str, str]:
    base = Path(path).name
    parser = PARSERS.get(base)
    if parser is None:
        return {}
    return parser(text)


def is_lockfile(path: str) -> bool:
    return Path(path).name in PARSERS
