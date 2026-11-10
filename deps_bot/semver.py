"""Minimal semver parsing/classification, no third-party dependencies.

Handles the shapes we actually meet in manifests:
  1.2.3            1.2.3-alpha.1    1.2.3+build     0.4.0-rc.1
  1.2              1                 2024.1.2 (calendar)   v1.2.3
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# name, major, minor, patch, prerelease tuple, build
_VERSION_RE = re.compile(
    r"^\s*v?(\d+)(?:\.(\d+))?(?:\.(\d+))?"
    r"(?:-([0-9A-Za-z.\-]+))?"
    r"(?:\+([0-9A-Za-z.\-]+))?\s*$"
)


@dataclass(frozen=True)
class Version:
    major: int
    minor: int
    patch: int
    prerelease: tuple
    build: str = ""
    raw: str = ""

    @property
    def is_prerelease(self) -> bool:
        return bool(self.prerelease)

    def __str__(self) -> str:
        return self.raw or f"{self.major}.{self.minor}.{self.patch}"


def parse(text: str) -> Version | None:
    """Parse a version string. Returns None if it is not version-like."""
    if text is None:
        return None
    text = text.strip()
    if not text or text in {"*", "latest", "x", "X"}:
        return None
    m = _VERSION_RE.match(text)
    if not m:
        return None
    major, minor, patch, pre, build = m.groups()
    pre_parts: tuple = ()
    if pre:
        parts = []
        for p in pre.split("."):
            parts.append((0, int(p)) if p.isdigit() else (1, p))
        pre_parts = tuple(parts)
    return Version(
        major=int(major),
        minor=int(minor) if minor is not None else 0,
        patch=int(patch) if patch is not None else 0,
        prerelease=pre_parts,
        build=build or "",
        raw=text,
    )


def _cmp(a, b) -> int:
    if a < b:
        return -1
    if a > b:
        return 1
    return 0


def compare(a: Version, b: Version) -> int:
    """Return -1 if a < b, 0 if equal, 1 if a > b."""
    for x, y in ((a.major, b.major), (a.minor, b.minor), (a.patch, b.patch)):
        if x != y:
            return -1 if x < y else 1
    # A prerelease sorts before its release.
    if a.prerelease and not b.prerelease:
        return -1
    if b.prerelease and not a.prerelease:
        return 1
    if a.prerelease and b.prerelease:
        for pa, pb in zip(a.prerelease, b.prerelease, strict=False):
            c = _cmp(pa, pb)
            if c:
                return c
        return _cmp(len(a.prerelease), len(b.prerelease))
    return 0


def classify(current: str, latest: str) -> str | None:
    """Classify the bump current -> latest.

    Returns "major" | "minor" | "patch" | None (not an upgrade).
    Prereleases are ignored (never auto-bump to a prerelease).

    0.x versions follow Cargo/npm convention: for major version 0, a minor bump is
    a *breaking* change, so it is reported as "major".
    """
    current_v = parse(current)
    latest_v = parse(latest)
    if current_v is None or latest_v is None:
        return None
    if latest_v.is_prerelease and not current_v.is_prerelease:
        return None
    if compare(latest_v, current_v) <= 0:
        return None
    if latest_v.major != current_v.major:
        return "major"
    if latest_v.minor != current_v.minor:
        # 0.4 -> 0.6 breaks the API for every ecosystem that treats 0.x minor
        # bumps as breaking.
        if current_v.major == 0:
            return "major"
        return "minor"
    return "patch"


def bump_version(current: str, latest: str) -> str:
    """Return `latest` normalised to the precision of `current`.

    "1.2" -> "1.3" stays two-part; "1.2.3" -> "2.0.0" stays three-part.
    """
    current_v = parse(current)
    latest_v = parse(latest)
    if current_v is None or latest_v is None:
        return latest
    parts = current.strip().lstrip("v")
    precision = parts.count(".") + 1 if parts[0].isdigit() else 3
    if precision >= 3:
        base = f"{latest_v.major}.{latest_v.minor}.{latest_v.patch}"
    elif precision == 2:
        base = f"{latest_v.major}.{latest_v.minor}"
    else:
        base = f"{latest_v.major}"
    if latest_v.prerelease:
        base += "-" + ".".join(str(p[1]) for p in latest_v.prerelease)
    return base


# --------------------------------------------------------------------------
# Constraint handling - only the forms that appear in real manifests.
# --------------------------------------------------------------------------

_CONSTRAINT_OPS = ("^", "~", ">=", "<=", ">", "<", "=", "==", "v")


def constraint_kind(constraint: str, ecosystem: str | None = None) -> str:
    """Classify a manifest constraint string.

    Returns one of: exact, caret, tilde, gte, gt, lte, lt, any, wildcard, range

    The ecosystem matters: Cargo treats a bare "1.0" as "^1.0" (>=1.0, <2.0),
    while npm treats a bare "1.0" as exactly 1.0.
    """
    c = (constraint or "").strip()
    if not c:
        return "any"
    if c in {"*", "x", "X", "latest"}:
        return "any"
    # Compound ranges (">=1.0, <2.0") are treated as a range.
    if any(op in c for op in (",", " ")) and "||" not in c:
        return "range"
    if "||" in c:
        return "range"
    for op in _CONSTRAINT_OPS:
        if c.startswith(op):
            rest = c[len(op) :].strip()
            if op in {"^", "~", ">=", "<=", ">", "<"}:
                return {"^": "caret", "~": "tilde", ">=": "gte", "<=": "lte", ">": "gt", "<": "lt"}[
                    op
                ]
            if op in {"=", "==", "v"}:
                return "exact"
            if "*" in rest or "x" in rest.lower():
                return "wildcard"
    if "*" in c or "x" in c.lower():
        return "wildcard"
    # Bare version.
    if ecosystem == "crates":
        # Cargo: "1.0" means ^1.0; "0.4" means >=0.4, <0.5
        return "caret"
    if ecosystem in {"npm", "actions"}:
        return "exact"
    if ecosystem == "pypi":
        return "any"
    if ecosystem == "go":
        return "exact"
    return "exact"


def allows(constraint: str, version: str, ecosystem: str | None = None) -> bool:
    """Does `constraint` permit `version`? Conservative - used to avoid proposing
    updates that would violate the manifest's own policy."""
    kind = constraint_kind(constraint, ecosystem)
    v = parse(version)
    if v is None:
        return False
    c = (constraint or "").strip()
    if kind == "any":
        return True
    if kind == "exact":
        target = parse(c.lstrip("=v").strip())
        return target is None or compare(v, target) == 0
    if kind == "caret":
        target = parse(c.lstrip("^").strip())
        if target is None:
            return False
        if target.major > 0:
            return v.major == target.major
        if target.minor > 0 or target.major > 0:
            return v.major == 0 and v.minor == target.minor
        return v.major == 0 and v.minor == 0 and v.patch == target.patch
    if kind == "tilde":
        target = parse(c.lstrip("~").strip())
        if target is None:
            return False
        return v.major == target.major and v.minor == target.minor
    if kind == "gte":
        target = parse(c.lstrip(">=").strip())
        return target is not None and compare(v, target) >= 0
    if kind == "gt":
        target = parse(c.lstrip(">").strip())
        return target is not None and compare(v, target) > 0
    if kind == "lte":
        target = parse(c.lstrip("<=").strip())
        return target is not None and compare(v, target) <= 0
    if kind == "lt":
        target = parse(c.lstrip("<").strip())
        return target is not None and compare(v, target) < 0
    if kind == "wildcard":
        target = parse(c.replace("*", "0").replace("x", "0").replace("X", "0"))
        if target is None:
            return False
        return v.major == target.major and v.minor == target.minor
    return True
