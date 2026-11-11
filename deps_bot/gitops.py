"""Git operations.

The orchestrator owns all remote state. The agent never sees these functions -
it can only edit files inside the clone.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


class GitError(RuntimeError):
    pass


def _split_patch(patch: str) -> tuple[str, list[str]]:
    """Split a unified diff into its header and its hunks."""
    header: list[str] = []
    hunks: list[str] = []
    current: list[str] | None = None
    for line in patch.splitlines(keepends=True):
        if line.startswith("@@"):
            if current:
                hunks.append("".join(current))
            current = [line]
        elif current is None:
            header.append(line)
        else:
            current.append(line)
    if current:
        hunks.append("".join(current))
    return "".join(header), hunks


@dataclass
class Git:
    path: Path

    def _run(self, *args, check: bool = True, timeout: int = 600) -> str:
        proc = subprocess.run(
            ["git", "-C", str(self.path), *args],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if check and proc.returncode != 0:
            raise GitError(f"git {' '.join(args)} failed: {proc.stderr.strip()[:1500]}")
        return proc.stdout

    # -------------------------------------------------------------- setup
    def clone(self, repo_url: str, branch: str, depth: int = 50) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            shutil.rmtree(self.path)
        proc = subprocess.run(
            ["git", "clone", "--depth", str(depth), "--branch", branch, repo_url, str(self.path)],
            capture_output=True,
            text=True,
            timeout=900,
        )
        if proc.returncode != 0:
            raise GitError(f"clone failed: {proc.stderr.strip()[:1500]}")

    def fetch(self) -> None:
        self._run("fetch", "--all", "--prune", timeout=300)

    def fetch_branch(self, branch: str) -> bool:
        """Fetch a single remote branch into refs/remotes/origin/<branch>.

        A plain `git fetch origin <branch>` only writes FETCH_HEAD, and in a
        shallow clone it does not create the remote-tracking ref at all, so the
        refspec is given explicitly.
        """
        proc = subprocess.run(
            [
                "git",
                "-C",
                str(self.path),
                "fetch",
                "--depth",
                "50",
                "origin",
                f"+refs/heads/{branch}:refs/remotes/origin/{branch}",
            ],
            capture_output=True,
            text=True,
            timeout=300,
        )
        if proc.returncode != 0:
            return False
        return self.has_branch(f"origin/{branch}")

    # ------------------------------------------------------------ branches
    def current_branch(self) -> str:
        return self._run("rev-parse", "--abbrev-ref", "HEAD").strip()

    def has_branch(self, branch: str) -> bool:
        out = self._run("rev-parse", "--verify", branch, check=False)
        return out.strip() != ""

    def create_branch(self, branch: str, start_point: str | None = None) -> None:
        args = ["checkout", "-B", branch]
        if start_point:
            args.append(start_point)
        self._run(*args)

    def checkout(self, ref: str) -> None:
        self._run("checkout", ref)

    def clean(self) -> None:
        """Remove untracked files and reset tracked ones."""
        self._run("reset", "--hard", "HEAD")
        self._run("clean", "-fdq")

    def revert_lines(self, offenders: list[dict]) -> list[str]:
        """Put individual changed lines back the way they were.

        `offenders` is a list of {"file": path, "line": content}. The diff is
        taken with zero context, so every hunk covers one contiguous change
        and the offending ones can be reversed on their own.
        """
        by_file: dict[str, set[str]] = {}
        for item in offenders:
            by_file.setdefault(item["file"], set()).add(item["line"].strip())
        reverted: list[str] = []
        for path, lines in by_file.items():
            patch = self._run("diff", "-U0", "--", path, check=False)
            if not patch.strip():
                continue
            header, hunks = _split_patch(patch)
            bad = []
            for hunk in hunks:
                changed = {
                    ln[1:].strip()
                    for ln in hunk.splitlines()
                    if ln.startswith(("+", "-")) and not ln.startswith(("+++", "---"))
                }
                if changed & lines:
                    bad.append(hunk)
            if not bad:
                continue
            reverse_patch = header + "".join(bad)
            proc = subprocess.run(
                ["git", "-C", str(self.path), "apply", "--reverse", "--unidiff-zero", "-"],
                input=reverse_patch,
                capture_output=True,
                text=True,
                timeout=60,
            )
            if proc.returncode != 0:
                continue
            reverted.extend(sorted(lines))
        self.stage_all()
        return reverted

    # -------------------------------------------------------------- state
    def status(self) -> str:
        return self._run("status", "--porcelain")

    def changed_files(self) -> list[str]:
        out = self._run("status", "--porcelain", "--untracked-files=all")
        files = []
        for line in out.splitlines():
            if not line.strip():
                continue
            path = line[3:].strip()
            if " -> " in path:
                path = path.split(" -> ", 1)[1]
            files.append(path.strip('"'))
        return files

    def diff(self, staged: bool = False) -> str:
        args = ["diff"]
        if staged:
            args.append("--cached")
        return self._run(*args, timeout=120)

    def diff_for(self, path: str) -> str:
        """The diff of a single file."""
        return self._run("diff", "--", path, timeout=60)

    def diff_stat(self) -> str:
        return self._run("diff", "--stat", timeout=120)

    # ------------------------------------------------------------ commits
    def stage_all(self) -> None:
        self._run("add", "-A")

    def commit(self, message: str, author: str | None = None) -> str:
        self.stage_all()
        args = ["commit", "-m", message]
        if author:
            args += ["--author", author]
        return self._run(*args, timeout=180)

    def has_commits(self, branch: str) -> bool:
        out = self._run("log", "--oneline", "-1", branch, check=False)
        return bool(out.strip())

    # -------------------------------------------------------------- push
    def push(self, branch: str, force: bool = False) -> str:
        args = ["push", "--set-upstream", "origin", branch]
        if force:
            args.insert(1, "--force-with-lease")
        return self._run(*args, timeout=600)

    def commit_sha(self, ref: str = "HEAD") -> str:
        return self._run("rev-parse", ref).strip()

    def remote_url(self) -> str:
        return self._run("remote", "get-url", "origin").strip()
