"""Updating Sadoc from the latest main branch.

Sadoc runs from a git checkout, so it can pull the latest version of itself.
Two things make that safe:

* the working tree must be clean - local changes are never thrown away;
* the pull is a fast-forward only, so a divergent history is refused rather
  than merged.

The caller is expected to re-execute itself afterwards, so that the code that
runs is the code that was just fetched.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass
class UpdateResult:
    changed: bool = False
    ok: bool = True
    message: str = ""
    before: str = ""
    after: str = ""


def _git(path: Path, *args: str, timeout: int = 120) -> tuple[int, str]:
    proc = subprocess.run(
        ["git", "-C", str(path), *args],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def _is_checkout(path: Path) -> bool:
    if not (path / ".git").exists():
        return False
    code, _ = _git(path, "rev-parse", "--is-inside-work-tree")
    return code == 0


def update(path: str | Path, branch: str = "main") -> UpdateResult:
    """Bring the checkout up to date. Never throws."""
    root = Path(path).resolve()
    result = UpdateResult()

    if not _is_checkout(root):
        result.ok = False
        result.message = f"{root} is not a git checkout - nothing to update"
        return result

    code, output = _git(root, "status", "--porcelain")
    if code != 0:
        result.ok = False
        result.message = "could not read the git status"
        return result
    if output.strip():
        result.ok = False
        result.message = "the checkout has local changes - leaving it alone"
        return result

    code, output = _git(root, "rev-parse", "HEAD")
    if code != 0:
        result.ok = False
        result.message = "could not read HEAD"
        return result
    result.before = output.strip()

    code, output = _git(root, "remote")
    if code != 0 or "origin" not in output:
        result.ok = False
        result.message = "no origin remote - nothing to update"
        return result

    code, output = _git(root, "fetch", "origin", branch)
    if code != 0:
        result.ok = False
        result.message = f"could not fetch origin/{branch}: {output.strip()[:200]}"
        return result

    code, output = _git(root, "rev-parse", f"origin/{branch}")
    if code != 0:
        result.ok = False
        result.message = f"origin/{branch} is not there"
        return result
    remote_head = output.strip()

    if remote_head == result.before:
        result.message = "already up to date"
        return result

    # Fast-forward only: a divergent history is refused rather than merged.
    code, output = _git(root, "merge", "--ff-only", f"origin/{branch}")
    if code != 0:
        result.ok = False
        result.message = f"could not fast-forward to origin/{branch}: {output.strip()[:200]}"
        return result

    code, output = _git(root, "rev-parse", "HEAD")
    result.after = output.strip()
    result.changed = result.after != result.before
    if result.changed:
        short = result.after[:8]
        result.message = f"updated to {short}"
    else:
        result.message = "already up to date"
    return result
