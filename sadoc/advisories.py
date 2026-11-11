"""Security advisories, via the GitHub Advisory Database.

A dependency is "security-relevant" when its current version falls inside a
published advisory's vulnerable range. That promotes it to its own PR tier so
security fixes are never blocked behind a risky major bump.
"""

from __future__ import annotations

import re
import time

from . import semver

# GitHub ecosystem names -> ours
ECOSYSTEM_MAP = {
    "npm": "npm",
    "crates": "crates",
    "pip": "pypi",
    "pypi": "pypi",
    "go": "go",
    "rubygems": "rubygems",
    "maven": "maven",
    "nuget": "nuget",
    "packagist": "packagist",
    "composer": "composer",
    "swift": "swift",
    "actions": "action",
}

_RANGE_PART = re.compile(r"^\s*(>=|<=|=|>|<)?\s*([0-9][^\s,]*)\s*$")


def _normalise_range(text: str) -> list[tuple[str, str]]:
    parts = []
    for chunk in re.split(r",", text or ""):
        chunk = chunk.strip()
        if not chunk:
            continue
        m = _RANGE_PART.match(chunk)
        if not m:
            continue
        op = m.group(1) or "="
        version = m.group(2).lstrip("v")
        parts.append((op, version))
    return parts


def version_in_range(version: str, range_text: str) -> bool:
    """Is `version` inside `range_text`?

    Supports the comparison-list form GitHub uses ("< 1.2.3, >= 1.0.0") and the
    single-comparison form ("< 1.2.3"). An empty or unparsable range is treated
    as "not vulnerable" - we never want to block a repo on a parse failure.
    """
    v = semver.parse(version)
    if v is None:
        return False
    parts = _normalise_range(range_text)
    if not parts:
        return False
    for op, target_text in parts:
        target = semver.parse(target_text)
        if target is None:
            continue
        cmp = semver.compare(v, target)
        ok = {
            "<": cmp < 0,
            "<=": cmp <= 0,
            "=": cmp == 0,
            ">=": cmp >= 0,
            ">": cmp > 0,
        }[op]
        if not ok:
            return False
    return True


class AdvisoryCache:
    """Per-package advisory lookups.

    The list endpoint returns advisories newest-first, so indexing a page of them
    would silently miss advisories for older packages. Querying by `affects`
    targets the package directly, which is both correct and far cheaper.
    """

    def __init__(self, gh):
        self.gh = gh
        self._cache: dict[tuple[str, str], list[dict]] = {}
        self._ts: dict[tuple[str, str], float] = {}

    def advisories_for(self, ecosystem: str, package: str) -> list[dict]:
        """Advisories affecting `package`. Cached per (ecosystem, package) for a day."""
        gh_eco = next((k for k, v in ECOSYSTEM_MAP.items() if v == ecosystem), None)
        if gh_eco is None or not package:
            return []
        key = (gh_eco, package.lower())
        now = time.time()
        if key in self._cache and now - self._ts.get(key, 0) < 86400:
            return self._cache[key]
        try:
            rows = self.gh.api_paginate(
                f"advisories?ecosystem={gh_eco}&affects={package}",
                limit=200,
                per_page=100,
            )
        except Exception:
            rows = []
        out: list[dict] = []
        for row in rows:
            for vuln in row.get("vulnerabilities") or []:
                pkg = (vuln.get("package") or {}).get("name")
                if not pkg or pkg.lower() != package.lower():
                    continue
                out.append(
                    {
                        "ghsa": row.get("ghsa_id"),
                        "severity": row.get("severity"),
                        "summary": row.get("summary"),
                        "range": vuln.get("vulnerable_version_range", ""),
                        "patched": vuln.get("first_patched_version"),
                    }
                )
        self._cache[key] = out
        self._ts[key] = now
        return out


def is_vulnerable(current: str, advisories: list[dict]) -> dict | None:
    """Return the first advisory whose vulnerable range contains `current`."""
    for adv in advisories:
        if version_in_range(current, adv.get("range", "")):
            return adv
    return None
