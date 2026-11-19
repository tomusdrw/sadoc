"""Discovery + detection.

Walks the configured repos, finds every dependency manifest, parses it, asks the
upstream registry what the newest version is, and classifies the gap into
patch / minor / major.

No LLM is involved here on purpose - version numbers come from registries, and
semver arithmetic is done in code.
"""

from __future__ import annotations

import concurrent.futures
import fnmatch
import re
import time
from dataclasses import asdict, dataclass
from pathlib import PurePosixPath

from . import lockfiles, registry, semver, toolchains
from . import manifests as mf

# Files we treat as dependency manifests.
MANIFEST_NAMES = {
    "package.json",
    "Cargo.toml",
    "requirements.txt",
    "pyproject.toml",
    "go.mod",
    "Gemfile",
    "build.gradle",
    "build.gradle.kts",
    "pubspec.yaml",
    "mix.exs",
    "setup.py",
    "setup.cfg",
    "Pipfile",
    "composer.json",
    "Package.swift",
    "Package.resolved",
    "deno.json",
    "deno.jsonc",
}
WORKFLOW_DIR = ".github/workflows"

# Manifests we never touch.
SKIP_PATTERNS = (
    "*/node_modules/*",
    "*/vendor/*",
    "*/target/*",
    "*/dist/*",
    "*/build/*",
    "*/.venv/*",
    "*/venv/*",
    "*/site-packages/*",
    "*/__pycache__/*",
    "*/examples/*/node_modules/*",
    "*/testdata/*",
    "*/fixtures/*",
)


def is_manifest(path: str) -> bool:
    base = PurePosixPath(path).name
    if base.startswith(("requirements", "constraints")) and base.endswith(".txt"):
        return True
    return base in MANIFEST_NAMES or base.startswith("Dockerfile")


def is_workflow(path: str) -> bool:
    return path.startswith(WORKFLOW_DIR + "/") and path.endswith((".yml", ".yaml"))


def should_skip(path: str) -> bool:
    return any(fnmatch.fnmatch(path, pattern) for pattern in SKIP_PATTERNS)


@dataclass
class Update:
    repo: str
    manifest: str
    ecosystem: str
    name: str
    section: str
    kind: str
    current: str | None
    latest: str
    bump_kind: str
    constraint: str | None
    action: str = (
        "manifest"  # "manifest" = edit the manifest, "lockfile" = refresh the lockfile only
    )
    installed: str | None = None
    toolchain_plan: dict | None = None

    def as_dict(self) -> dict:
        return {
            "manifest": self.manifest,
            "ecosystem": self.ecosystem,
            "name": self.name,
            "section": self.section,
            "kind": self.kind,
            "current": self.current,
            "latest": self.latest,
            "bump_kind": self.bump_kind,
            "constraint": self.constraint,
            "action": self.action,
            "installed": self.installed,
            **({"toolchain_plan": self.toolchain_plan} if self.toolchain_plan else {}),
        }


# --------------------------------------------------------------------- scope
def resolve_scope(cfg, gh, state) -> list[dict]:
    """Work out which repos the bot is allowed to touch, and why.

    `cfg.owners` lists the GitHub accounts to sweep: an empty list means the
    authenticated user, otherwise each entry is passed to `gh repo list` (so
    organisations work the same way).
    """
    owners = [o for o in (getattr(cfg, "owners", None) or []) if o]
    if not owners:
        repos = gh.my_repos()
    else:
        repos = []
        for owner in owners:
            repos.extend(gh.my_repos(owner=owner))

    result = []
    for repo in repos:
        name = repo["name"]
        owner = repo.get("owner", {}).get("login", cfg.owner)
        is_fork = bool(repo.get("isFork"))
        archived = bool(repo.get("isArchived"))
        pushed = repo.get("pushedAt") or ""

        state.upsert_repo(
            owner,
            name,
            default_branch=(repo.get("defaultBranchRef") or {}).get("name") or "main",
            is_fork=int(is_fork),
            is_archived=int(archived),
            pushed_at=pushed,
        )

        reason = None
        if archived:
            reason = "archived"
        elif name in cfg.exclude:
            reason = "explicitly excluded"
        elif is_fork and cfg.skip_forks and name not in cfg.include:
            reason = "fork (skipped by policy)"
        elif name in cfg.include:
            reason = "whitelisted"
        elif cfg.owner and owner != cfg.owner and name not in cfg.include:
            reason = f"not owned by {cfg.owner} and not whitelisted"
        else:
            if cfg.max_inactive_days:
                try:
                    age_days = (
                        time.time() - time.mktime(time.strptime(pushed[:10], "%Y-%m-%d"))
                    ) / 86400
                except (ValueError, TypeError):
                    age_days = 0
                if age_days > cfg.max_inactive_days:
                    reason = f"inactive for {int(age_days)} days"

        in_scope = reason is None
        state.set_scope(owner, name, in_scope, reason or "in scope")
        if in_scope:
            result.append(
                {"owner": owner, "name": name, "repo": f"{owner}/{name}", "pushed_at": pushed}
            )
    return result


# ---------------------------------------------------------------- detection
def find_manifest_paths(
    gh, owner: str, repo: str, branch: str
) -> tuple[list[str], list[str], list[str]]:
    tree = gh.repo_tree(owner, repo, branch)
    manifests, workflows, locks = [], [], []
    for entry in tree:
        path = entry.get("path", "")
        if entry.get("type") != "blob" or should_skip(path):
            continue
        if is_manifest(path) or (toolchains.is_build_input(path) and not is_workflow(path)):
            manifests.append(path)
        elif is_workflow(path):
            workflows.append(path)
        elif lockfiles.is_lockfile(path) or toolchains.is_lockfile(path):
            locks.append(path)
    return sorted(manifests), sorted(workflows), sorted(locks)


def _fetch_lockfile(args) -> tuple[str, dict[str, str]]:
    gh, owner, repo, path, branch = args
    try:
        text = gh.file_text(owner, repo, path, ref=branch)
    except Exception:
        return path, {}
    return path, lockfiles.parse_lockfile(path, text)


def detect_repo(
    gh, owner: str, repo: str, branch: str, workers: int = 8, *, diagnostic=None
) -> list[Update]:
    """Detect outdated dependencies in one repo."""
    manifest_paths, workflow_paths, lock_paths = find_manifest_paths(gh, owner, repo, branch)
    if not manifest_paths and not workflow_paths:
        return []

    # Installed versions come from the lockfiles, not the manifests.
    installed: dict[str, str] = {}
    lockfiles_present: set[str] = set()
    deps: list[mf.Dep] = []
    build_files: dict[str, str] = {}
    unreadable: list[str] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        paths = manifest_paths + workflow_paths
        build_paths = {p for p in paths if toolchains.is_build_input(p)}
        futures = {pool.submit(gh.file_text, owner, repo, path, ref=branch): path for path in paths}
        lock_jobs = [(gh, owner, repo, p, branch) for p in lock_paths if lockfiles.is_lockfile(p)]
        lock_futures = [pool.submit(_fetch_lockfile, job) for job in lock_jobs]
        for fut in concurrent.futures.as_completed(futures):
            path = futures[fut]
            try:
                contents = fut.result()
            except Exception:
                if path in build_paths:
                    unreadable.append(path)
                continue
            if path in build_paths:
                build_files[path] = contents
            try:
                if is_workflow(path):
                    deps.extend(mf.parse_workflow(contents, path))
                elif PurePosixPath(path).name.startswith("Dockerfile"):
                    deps.extend(mf.parse_dockerfile(contents, path))
                else:
                    deps.extend(mf.parse_manifest(path, contents))
            except (AttributeError, TypeError, ValueError):
                continue
        for fut in concurrent.futures.as_completed(lock_futures):
            try:
                path, mapping = fut.result()
                if mapping:
                    lockfiles_present.add(PurePosixPath(path).name)
                for name, version in mapping.items():
                    installed.setdefault(name, version)
            except Exception:
                pass

    tracked = set(manifest_paths + workflow_paths + lock_paths)
    if ".sadoc-toolchains.toml" in build_files:
        try:
            for path in sorted(toolchains.declared_paths(build_files) - tracked):
                build_files[path] = gh.file_text(owner, repo, path, ref=branch)
        except Exception as exc:
            unreadable.append(f"custom toolchain input ({type(exc).__name__})")

    updates: list[Update] = []
    toolchain_images = {
        (pin.path, f"library/{pin.image}") for pin in toolchains.inventory(build_files) if pin.image
    }
    proposals, blockers = (
        toolchains.plan(build_files, lock_paths=tuple(lock_paths)) if not unreadable else ([], [])
    )
    if diagnostic:
        for detail in blockers + [f"unreadable build input: {path}" for path in sorted(unreadable)]:
            diagnostic(detail)
    for proposal in proposals:
        first = proposal.changes[0].pin
        updates.append(
            Update(
                repo=f"{owner}/{repo}",
                manifest=first.path,
                ecosystem="toolchain",
                name=proposal.tool,
                section="toolchain pins",
                kind="toolchain",
                current=first.version,
                latest=proposal.target,
                bump_kind=semver.classify(first.version, proposal.target) or "minor",
                constraint=None,
                action="toolchain",
                toolchain_plan={
                    **asdict(proposal),
                    "lockfiles": toolchains.related_lockfiles(proposal, lock_paths),
                },
            )
        )
    seen: set[tuple[str, str]] = set()

    for dep in deps:
        if (dep.manifest, dep.name.removeprefix("docker.io/")) in toolchain_images:
            continue
        eco = registry.ecosystem_for(dep)
        if eco == "unknown":
            continue
        key = (dep.manifest, dep.name)
        if key in seen:
            continue
        seen.add(key)

        latest = registry.latest_for(dep, gh=gh)
        if not latest:
            continue

        constraint = (dep.constraint or "").strip()
        # What is actually installed? The lockfile is authoritative; the manifest
        # value is only a fallback (and is often a range, not a version).
        installed_version = installed.get(dep.name)
        pinned = _clean_version(dep.current) if dep.current else None
        baseline = installed_version or pinned
        has_lockfile = _has_lockfile(eco, lockfiles_present)
        if eco == "action":
            # An action pinned as "v4" should become "v7", not "v7.0.1".
            latest = _match_pin_precision(baseline or pinned, latest)

        # Does the manifest constraint already permit the new version?
        permits = bool(constraint) and semver.allows(constraint, latest, eco)

        if permits:
            # The manifest does not need to change - only the lockfile does.
            # That is only meaningful if the project actually commits one.
            if not has_lockfile:
                continue
            if not baseline:
                continue
            bump = semver.classify(baseline, latest)
            if not bump:
                continue
            updates.append(
                Update(
                    repo=f"{owner}/{repo}",
                    manifest=dep.manifest,
                    ecosystem=eco,
                    name=dep.name,
                    section=dep.section,
                    kind=dep.kind,
                    current=baseline,
                    latest=latest,
                    bump_kind=bump,
                    constraint=constraint,
                    action="lockfile",
                    installed=installed_version,
                )
            )
            continue

        # The manifest has to change.
        if baseline:
            bump = semver.classify(baseline, latest)
        else:
            floor = _floor_version(constraint, latest)
            bump = semver.classify(floor, latest) if floor else None
        if not bump:
            continue
        updates.append(
            Update(
                repo=f"{owner}/{repo}",
                manifest=dep.manifest,
                ecosystem=eco,
                name=dep.name,
                section=dep.section,
                kind=dep.kind,
                current=baseline or pinned,
                latest=latest,
                bump_kind=bump,
                constraint=constraint,
                action="manifest",
                installed=installed_version,
            )
        )
    return updates


def _match_pin_precision(current: str | None, latest: str) -> str:
    """Keep an action pin in the style it was written in.

    `actions/checkout@v4` becomes `actions/checkout@v7`, not `@v7.0.1`.
    """
    if not current:
        return latest
    stripped = current.strip().lstrip("v")
    if "." in stripped:
        return latest  # the pin names a full version, keep the full version
    latest_major = latest.strip().lstrip("v").split(".")[0]
    if not latest_major.isdigit():
        return latest
    return ("v" + latest_major) if latest.startswith("v") else latest_major


def _clean_version(text: str | None) -> str | None:
    """Strip constraint operators from a version-ish string."""
    if not text:
        return None
    value = re.sub(r"^[\^~>=<!=\s]+", "", text.strip())
    if value.startswith("v"):
        value = value[1:]
    return value or None


# Which lockfile files make a lockfile-only refresh meaningful per ecosystem.
_ECOSYSTEM_LOCKFILES = {
    "crates": {"Cargo.lock"},
    "npm": {"package-lock.json", "pnpm-lock.yaml", "pnpm-lock.yml", "yarn.lock"},
    "pypi": {"poetry.lock", "uv.lock", "requirements.txt"},
    "go": {"go.sum"},
    "composer": {"composer.lock"},
}


def _has_lockfile(ecosystem: str, present: set[str]) -> bool:
    wanted = _ECOSYSTEM_LOCKFILES.get(ecosystem)
    if not wanted:
        return False
    return bool(wanted & present)


def _floor_version(constraint: str | None, latest: str) -> str | None:
    """Best guess at the currently-installed version for an unpinned dep."""
    if not constraint:
        return None
    digits = ""
    for ch in constraint:
        if ch.isdigit() or ch == ".":
            digits += ch
        else:
            break
    digits = digits.strip(".")
    if not digits:
        return None
    return digits
