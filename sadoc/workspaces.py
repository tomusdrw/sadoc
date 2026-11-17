"""Ownership and recovery of Sadoc's disposable repository checkouts."""

from __future__ import annotations

import configparser
import fcntl
import logging
import os
import re
import shutil
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from .gitops import GitError

_MANAGED = ".sadoc-checkouts"
_NAME = re.compile(r"([A-Za-z0-9][A-Za-z0-9-]*)__([A-Za-z0-9_.-]+)")


class WorkspaceError(GitError):
    pass


@contextmanager
def _locked(workdir: Path) -> Iterator[Path]:
    workdir.mkdir(parents=True, exist_ok=True)
    # Never unlink the lock: another process could still have that inode open.
    fd = os.open(workdir / ".sadoc-work.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise WorkspaceError(f"workdir is in use by another Sadoc job: {workdir}") from exc
        root = workdir / _MANAGED
        if root.is_symlink():
            raise WorkspaceError(f"managed checkout directory must not be a symlink: {root}")
        root.mkdir(exist_ok=True)
        yield root


def _remove(path: Path) -> None:
    if path.is_symlink():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)
    elif path.exists():
        path.unlink()


def _legacy_clone(path: Path) -> bool:
    """Recognize the exact flat clone layout used before scoped checkouts."""
    match = _NAME.fullmatch(path.name)
    if not match or path.is_symlink():
        return False
    owner, repo = match.groups()
    if repo in (".", ".."):
        return False
    gitdir = path / ".git"
    config = gitdir / "config"
    # A .git file belongs to a worktree; links cannot establish ownership.
    if gitdir.is_symlink() or not gitdir.is_dir() or config.is_symlink():
        return False
    parser = configparser.RawConfigParser()
    try:
        with config.open() as source:
            parser.read_file(source)
        return parser.get('remote "origin"', "url", fallback="") == (
            f"https://github.com/{owner}/{repo}.git"
        )
    except (OSError, configparser.Error, UnicodeError):
        return False


def _recover(workdir: Path, root: Path) -> None:
    # This reserved directory contains only checkouts, including incomplete
    # clones with no .git metadata. Symlinks are unlinked, never traversed.
    for path in root.iterdir():
        _remove(path)
    for path in workdir.iterdir():
        if _legacy_clone(path):
            _remove(path)


def recover(workdir: Path) -> None:
    """Reclaim abandoned checkouts, refusing to interfere with an active job."""
    with _locked(workdir) as root:
        _recover(workdir, root)


@contextmanager
def checkout(workdir: Path, owner: str, repo: str) -> Iterator[Path]:
    """Lease one disposable path, cleaning it even if cloning itself fails."""
    name = f"{owner}__{repo}"
    match = _NAME.fullmatch(name)
    if not match or match.groups() != (owner, repo) or repo in (".", ".."):
        raise WorkspaceError(f"invalid repository name: {owner}/{repo}")
    with _locked(workdir) as root:
        _recover(workdir, root)
        path = root / name
        error = None
        try:
            yield path
        except BaseException as exc:
            error = exc
            raise
        finally:
            try:
                _remove(path)
            except OSError as exc:
                detail = f"checkout cleanup failed for {path}: {exc}"
                if error is None:
                    raise WorkspaceError(detail) from exc
                error.add_note(detail)
                logging.getLogger(__name__).error(detail)
