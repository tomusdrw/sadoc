"""Git operations.

The orchestrator owns all remote state. The agent never sees these functions -
it can only edit files inside the clone.
"""

from __future__ import annotations

import base64
import os
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory


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
    github_token: Callable[[], str] | None = None
    git_dir: Path | None = None
    commit_identity: Callable[[], tuple[str, str]] | None = None

    def _command(self, *args: str) -> list[str]:
        base = ["git", "-C", str(self.path)]
        if self.git_dir is not None:
            base += ["--git-dir", str(self.git_dir), "--work-tree", str(self.path)]
        return [*base, *args]

    def _network_env(self) -> dict[str, str] | None:
        if self.github_token is None:
            return None
        credentials = base64.b64encode(f"x-access-token:{self.github_token()}".encode()).decode()
        env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith(("GIT_TRACE", "GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_"))
            and key not in ("GIT_CONFIG_PARAMETERS", "GIT_CURL_VERBOSE")
        }
        env.update(
            GIT_TERMINAL_PROMPT="0",
            GIT_CONFIG_COUNT="4",
            GIT_CONFIG_KEY_0="credential.helper",
            GIT_CONFIG_VALUE_0="",
            GIT_CONFIG_KEY_1="http.followRedirects",
            GIT_CONFIG_VALUE_1="false",
            GIT_CONFIG_KEY_2="http.https://github.com/.extraHeader",
            GIT_CONFIG_VALUE_2=f"Authorization: Basic {credentials}",
            GIT_CONFIG_KEY_3="core.hooksPath",
            GIT_CONFIG_VALUE_3=os.devnull,
        )
        return env

    def _identity_env(self) -> dict[str, str] | None:
        if self.commit_identity is None:
            return None
        name, email = self.commit_identity()
        return {
            **os.environ,
            "GIT_AUTHOR_NAME": name,
            "GIT_AUTHOR_EMAIL": email,
            "GIT_COMMITTER_NAME": name,
            "GIT_COMMITTER_EMAIL": email,
        }

    def _run(
        self,
        *args,
        check: bool = True,
        timeout: int = 600,
        network: bool = False,
        env: dict[str, str] | None = None,
    ) -> str:
        if network and self.github_token:
            env = self._network_env()
        proc = subprocess.run(
            self._command(*args),
            capture_output=True,
            text=True,
            timeout=timeout,
            **({"env": env} if env is not None else {}),
        )
        if check and proc.returncode != 0:
            raise GitError(f"git {' '.join(args)} failed: {proc.stderr.strip()[:1500]}")
        return proc.stdout

    # -------------------------------------------------------------- setup
    def clone(self, repo_url: str, branch: str, depth: int = 50) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            shutil.rmtree(self.path)
        if self.git_dir is None:
            self._clone_to(self.path, repo_url, branch, depth)
            return

        # Clone and copy trusted metadata privately before opening the file
        # tree to the container worker. The checkout gets its own Git metadata.
        with TemporaryDirectory(prefix="sadoc-clone-", dir="/tmp") as staging:
            checkout = Path(staging) / "checkout"
            self._clone_to(checkout, repo_url, branch, depth)
            shutil.copytree(checkout / ".git", self.git_dir, dirs_exist_ok=True)
            shutil.copytree(checkout, self.path, symlinks=True)

    def _clone_to(self, destination: Path, repo_url: str, branch: str, depth: int) -> None:
        args = ["git", "clone", "--depth", str(depth), "--branch", branch]
        proc = subprocess.run(
            [*args, repo_url, str(destination)],
            capture_output=True,
            text=True,
            timeout=900,
            **({"env": self._network_env()} if self.github_token else {}),
        )
        if proc.returncode != 0:
            raise GitError(f"clone failed: {proc.stderr.strip()[:1500]}")

    def fetch(self) -> None:
        self._run("fetch", "--all", "--prune", timeout=300, network=True)

    def fetch_branch(self, branch: str) -> bool:
        """Fetch a single remote branch into refs/remotes/origin/<branch>.

        A plain `git fetch origin <branch>` only writes FETCH_HEAD, and in a
        shallow clone it does not create the remote-tracking ref at all, so the
        refspec is given explicitly.
        """
        proc = subprocess.run(
            self._command(
                "fetch",
                "--depth",
                "50",
                "origin",
                f"+refs/heads/{branch}:refs/remotes/origin/{branch}",
            ),
            capture_output=True,
            text=True,
            timeout=300,
            **({"env": self._network_env()} if self.github_token else {}),
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

    def has_remote_branch(self, branch: str) -> bool:
        """Does refs/heads/<branch> exist on the origin?

        ls-remote talks to the remote directly, so it works in a shallow clone
        that has never fetched the branch in question.
        """
        out = self._run(
            "ls-remote",
            "--heads",
            "origin",
            f"refs/heads/{branch}",
            check=False,
            timeout=120,
            network=True,
        )
        return bool(out.strip())

    def create_branch(self, branch: str, start_point: str | None = None) -> None:
        args = ["checkout", "-B", branch]
        if start_point:
            args.append(start_point)
        self._run(*args)

    def checkout(self, ref: str) -> None:
        self._run("checkout", ref)

    def merge_base_branch(self, branch: str) -> list[str]:
        """Start a non-rewriting merge; leave conflicting files for local editing."""
        ref = f"origin/{branch}"
        if not self._run("merge-base", "HEAD", ref, check=False).strip():
            if self._run("rev-parse", "--is-shallow-repository").strip() == "true":
                self._run("fetch", "--unshallow", "origin", timeout=900, network=True)
            # Never merge unrelated histories when shallow ancestry was insufficient.
            self._run("merge-base", "HEAD", ref)
        proc = subprocess.run(
            self._command("merge", "--no-commit", "--no-ff", ref),
            env=self._identity_env(),
            capture_output=True,
            text=True,
            timeout=300,
        )
        conflicts = self._run("diff", "--name-only", "--diff-filter=U", "-z").split("\0")
        conflicts = [path for path in conflicts if path]
        if proc.returncode and not conflicts:
            raise GitError(f"merge failed: {(proc.stdout + proc.stderr)[-1500:]}")
        return conflicts

    def clean(self, include_ignored: bool = False, ref: str = "HEAD") -> None:
        """Remove untracked files and reset tracked ones."""
        self._run("reset", "--hard", ref)
        if include_ignored and (self.path / ".gitmodules").is_file():
            self._run("submodule", "deinit", "--force", "--all")
        self._run("clean", "-ffdxq" if include_ignored else "-fdq")

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
                self._command("apply", "--reverse", "--unidiff-zero", "-"),
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
        env = self._identity_env()
        args = ["commit", "-m", message]
        if env is not None:
            args.append("--no-gpg-sign")
        if author:
            args += ["--author", author]
        return self._run(*args, timeout=180, env=env)

    def has_commits(self, branch: str) -> bool:
        out = self._run("log", "--oneline", "-1", branch, check=False)
        return bool(out.strip())

    # -------------------------------------------------------------- push
    def push(self, branch: str, force: bool = False) -> str:
        args = ["push", "--set-upstream", "origin", branch]
        if force:
            args.insert(1, "--force-with-lease")
        return self._run(*args, timeout=600, network=True)

    def commit_sha(self, ref: str = "HEAD") -> str:
        return self._run("rev-parse", ref).strip()

    def remote_url(self) -> str:
        return self._run("remote", "get-url", "origin").strip()
