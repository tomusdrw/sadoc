"""Thin, typed wrapper around the `gh` CLI.

Everything goes through `gh api` / `gh <cmd> --json` so we get structured data
without re-implementing GitHub's REST surface.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass


class GhError(RuntimeError):
    pass


@dataclass
class Gh:
    binary: str = "gh"

    def __post_init__(self) -> None:
        if shutil.which(self.binary) is None:
            raise GhError(f"{self.binary} not found on PATH")

    # ---------------------------------------------------------------- raw
    def run(self, *args: str, check: bool = True, timeout: int = 300) -> str:
        proc = subprocess.run(
            [self.binary, *args],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if check and proc.returncode != 0:
            raise GhError(
                f"gh {' '.join(args)} -> exit {proc.returncode}\n"
                f"stdout: {proc.stdout[:2000]}\nstderr: {proc.stderr[:2000]}"
            )
        return proc.stdout

    def api(self, endpoint: str, method: str = "GET", check: bool = True, **params) -> object:
        """Call the GitHub REST or GraphQL API. `params` become query params for
        GET and body fields otherwise.

        Numbers and booleans use `gh api --field` so they are typed correctly
        (GraphQL rejects `"1"` for an Int variable); everything else uses
        `--raw-field` so strings stay strings.
        """
        args = ["api", endpoint]
        # `gh api` silently switches GET -> POST as soon as a field is added, so
        # the method has to be stated explicitly for reads.
        args += ["--method", method.upper()]
        for key, value in params.items():
            if value is None:
                continue
            if isinstance(value, bool):
                args += ["--field", f"{key}={str(value).lower()}"]
            elif isinstance(value, (int, float)):
                args += ["--field", f"{key}={value}"]
            elif isinstance(value, (list, dict)):
                args += ["--raw-field", f"{key}={json.dumps(value)}"]
            else:
                args += ["--raw-field", f"{key}={value}"]
        out = self.run(*args, check=check)
        if not out.strip():
            return {}
        try:
            return json.loads(out)
        except json.JSONDecodeError:
            return out

    def api_paginate(self, endpoint: str, limit: int = 1000, **params) -> list:
        """GET a list endpoint, following pagination up to `limit` items."""
        items: list = []
        page = 1
        while len(items) < limit:
            params = dict(params)
            params["per_page"] = min(100, limit - len(items))
            params["page"] = page
            batch = self.api(endpoint, **params)
            if not isinstance(batch, list) or not batch:
                break
            items.extend(batch)
            if len(batch) < params["per_page"]:
                break
            page += 1
        return items[:limit]

    def api_json(self, endpoint: str, **params) -> object:
        """GET with Accept: application/vnd.github+json (needed for some previews)."""
        out = self.run(
            "api",
            endpoint,
            "-H",
            "Accept: application/vnd.github+json",
            *[f"{k}={v}" for k, v in params.items() if v is not None],
        )
        return json.loads(out) if out.strip() else {}

    # ------------------------------------------------------------- repos
    def my_repos(self, limit: int = 500, owner: str | None = None) -> list[dict]:
        """Repos visible to the authenticated user, or an owner's repos."""
        args = ["repo", "list"]
        if owner:
            args.append(owner)
        args += [
            "--limit",
            str(limit),
            "--json",
            "name,isArchived,isFork,visibility,pushedAt,updatedAt,"
            "defaultBranchRef,primaryLanguage,owner,url",
        ]
        out = self.run(*args)
        return json.loads(out)

    def repo_info(self, owner: str, repo: str) -> dict:
        return self.api(f"repos/{owner}/{repo}")

    def repo_tree(self, owner: str, repo: str, branch: str) -> list[dict]:
        data = self.api(f"repos/{owner}/{repo}/git/trees/{branch}?recursive=1")
        return data.get("tree", []) if isinstance(data, dict) else []

    def file_text(self, owner: str, repo: str, path: str, ref: str | None = None) -> str:
        endpoint = f"repos/{owner}/{repo}/contents/{path}"
        if ref:
            endpoint += f"?ref={ref}"
        data = self.api(endpoint)
        if not isinstance(data, dict) or "content" not in data:
            raise GhError(f"cannot read {owner}/{repo}:{path}")
        import base64

        return base64.b64decode(data["content"]).decode("utf-8", "replace")

    def default_branch(self, owner: str, repo: str) -> str:
        info = self.repo_info(owner, repo)
        return info.get("default_branch") or "main"

    # -------------------------------------------------------------- prs
    def open_pr(
        self,
        owner: str,
        repo: str,
        title: str,
        body: str,
        head: str,
        base: str,
        draft: bool = False,
        labels: list[str] | None = None,
    ) -> dict:
        args = [
            "pr",
            "create",
            "--repo",
            f"{owner}/{repo}",
            "--title",
            title,
            "--body",
            body,
            "--head",
            head,
            "--base",
            base,
        ]
        if draft:
            args.append("--draft")
        for label in labels or []:
            args += ["--label", label]
        out = self.run(*args, timeout=180)
        url = next((line.strip() for line in out.splitlines() if "http" in line), "")
        return {"url": url, "raw": out}

    def pr_view(self, owner: str, repo: str, number: int) -> dict:
        out = self.run(
            "pr",
            "view",
            str(number),
            "--repo",
            f"{owner}/{repo}",
            "--json",
            "number,title,url,state,isDraft,mergeable,mergeStateStatus,"
            "reviewDecision,statusCheckRollup,headRefName,baseRefName,"
            "labels,author,createdAt,updatedAt",
        )
        return json.loads(out)

    def pr_list(self, owner: str, repo: str, state: str = "open") -> list[dict]:
        out = self.run(
            "pr",
            "list",
            "--repo",
            f"{owner}/{repo}",
            "--state",
            state,
            "--limit",
            "100",
            "--json",
            "number,title,url,headRefName,isDraft,labels,mergeable,createdAt",
        )
        return json.loads(out)

    def pr_checks(self, owner: str, repo: str, number: int) -> list[dict]:
        out = self.run(
            "pr",
            "checks",
            str(number),
            "--repo",
            f"{owner}/{repo}",
            "--json",
            "name,state,bucket,link,workflow",
            check=False,
        )
        try:
            return json.loads(out or "[]")
        except json.JSONDecodeError:
            return []

    def pr_checks_for_commit(self, owner: str, repo: str, sha: str) -> list[dict]:
        """The checks that ran for one specific commit.

        `gh pr checks` reports the checks of the *pull request*, which after a
        new push still include the previous (failed) run. Reading those would
        make the bot act on stale results.
        """
        data = self.api(f"repos/{owner}/{repo}/commits/{sha}/check-runs", per_page=100)
        if not isinstance(data, dict):
            return []
        out = []
        for run in data.get("check_runs", []):
            conclusion = (run.get("conclusion") or "").lower()
            status = (run.get("status") or "").lower()
            if conclusion == "success":
                bucket = "pass"
            elif conclusion == "failure":
                bucket = "fail"
            elif conclusion == "cancelled":
                bucket = "cancel"
            elif conclusion in ("skipped", "neutral"):
                bucket = "skipping"
            else:
                bucket = "pending"
            out.append(
                {
                    "name": run.get("name"),
                    "bucket": bucket,
                    "link": run.get("details_url"),
                    "workflow": (run.get("app") or {}).get("slug"),
                    "state": conclusion or status,
                }
            )
        return out

    def pr_checks_watch(
        self, owner: str, repo: str, number: int, interval: int = 20, fail_fast: bool = True
    ) -> tuple[int, str]:
        args = [
            "pr",
            "checks",
            str(number),
            "--repo",
            f"{owner}/{repo}",
            "--watch",
            "--interval",
            str(interval),
        ]
        if fail_fast:
            args.append("--fail-fast")
        proc = subprocess.run([self.binary, *args], capture_output=True, text=True)
        return proc.returncode, (proc.stdout + proc.stderr)

    def pr_merge(
        self, owner: str, repo: str, number: int, method: str = "squash", delete_branch: bool = True
    ) -> str:
        """Merge a pull request.

        `gh pr merge` takes a strategy flag (-s/--squash, -m/--merge,
        -r/--rebase), not a --merge-method option.
        """
        flag = {"squash": "--squash", "merge": "--merge", "rebase": "--rebase"}.get(
            method, "--squash"
        )
        args = ["pr", "merge", str(number), "--repo", f"{owner}/{repo}", flag]
        if delete_branch:
            args.append("--delete-branch")
        return self.run(*args, timeout=300)

    # ---------------------------------------------------------- reviews
    def pr_review_comments(self, owner: str, repo: str, number: int) -> list[dict]:
        return self.api_paginate(f"repos/{owner}/{repo}/pulls/{number}/comments")

    def pr_issue_comments(self, owner: str, repo: str, number: int) -> list[dict]:
        return self.api_paginate(f"repos/{owner}/{repo}/issues/{number}/comments")

    def pr_reviews(self, owner: str, repo: str, number: int) -> list[dict]:
        return self.api_paginate(f"repos/{owner}/{repo}/pulls/{number}/reviews")

    def pr_review_threads(self, owner: str, repo: str, number: int) -> list[dict]:
        """GraphQL review threads with resolution state (needed to know whether
        a comment is still outstanding)."""
        query = """
        query($owner:String!, $repo:String!, $number:Int!) {
          repository(owner:$owner, name:$repo) {
            pullRequest(number:$number) {
              reviewThreads(first:100) {
                nodes {
                  id isResolved isOutdated
                  comments(first:20) { nodes { body path line author { login } } }
                }
              }
            }
          }
        }"""
        data = self.api(
            "graphql",
            method="POST",
            query=query,
            owner=owner,
            repo=repo,
            number=number,
        )
        try:
            nodes = data["data"]["repository"]["pullRequest"]["reviewThreads"]["nodes"]
        except (KeyError, TypeError):
            return []
        return nodes

    def resolve_review_thread(self, owner: str, repo: str, thread_id: str) -> bool:
        mutation = """
        mutation($threadId:ID!) {
          resolveReviewThread(input:{threadId:$threadId}) {
            thread { id isResolved }
          }
        }"""
        data = self.api("graphql", method="POST", query=mutation, threadId=thread_id)
        thread = (data.get("data") or {}).get("resolveReviewThread") or {}
        return bool((thread.get("thread") or {}).get("isResolved"))

    def add_labels(self, owner: str, repo: str, number: int, labels: list[str]) -> None:
        if not labels:
            return
        # The endpoint wants an array, so the body has to be JSON.
        payload = json.dumps({"labels": list(labels)})
        proc = subprocess.run(
            [
                self.binary,
                "api",
                f"repos/{owner}/{repo}/issues/{number}/labels",
                "--method",
                "POST",
                "--input",
                "-",
            ],
            input=payload,
            capture_output=True,
            text=True,
            timeout=60,
        )
        if proc.returncode != 0:
            raise GhError(f"could not add labels: {proc.stderr.strip()[:300]}")

    def create_label(
        self, owner: str, repo: str, name: str, color: str = "ededed", description: str = ""
    ) -> None:
        self.api(
            f"repos/{owner}/{repo}/labels",
            method="POST",
            check=False,
            name=name,
            color=color,
            description=description,
        )

    # ------------------------------------------------------------- runs
    def latest_run(
        self, owner: str, repo: str, branch: str, workflow: str | None = None
    ) -> dict | None:
        """The most recent workflow run on a branch, optionally for one workflow."""
        args = ["run", "list", "--repo", f"{owner}/{repo}", "--branch", branch, "--limit", "10"]
        if workflow:
            args += ["--workflow", workflow]
        args += [
            "--json",
            "databaseId,conclusion,status,workflowName,headBranch,url,createdAt",
        ]
        out = self.run(*args, check=False)
        try:
            runs = json.loads(out or "[]")
        except json.JSONDecodeError:
            return None
        if not runs:
            return None
        # Only a completed run tells us anything.
        for run in runs:
            if run.get("status") == "completed":
                return run
        return runs[0]

    def run_jobs(self, owner: str, repo: str, run_id: str) -> list[dict]:
        """The jobs of a workflow run, with their conclusions."""
        data = self.api(f"repos/{owner}/{repo}/actions/runs/{run_id}/jobs")
        if not isinstance(data, dict):
            return []
        return data.get("jobs", [])

    def run_logs_failed(self, owner: str, repo: str, run_id: str, max_bytes: int = 40000) -> str:
        proc = subprocess.run(
            [self.binary, "run", "view", run_id, "--repo", f"{owner}/{repo}", "--log-failed"],
            capture_output=True,
            text=True,
        )
        out = proc.stdout or ""
        if len(out) > max_bytes:
            out = "...(truncated)...\n" + out[-max_bytes:]
        return out

    def pr_run_ids(self, owner: str, repo: str, number: int) -> list[str]:
        checks = self.pr_checks(owner, repo, number)
        ids = []
        for c in checks:
            link = c.get("link") or ""
            if "/runs/" in link:
                ids.append(link.split("/runs/")[1].split("/")[0])
        return ids

    def failed_job_ids(self, owner: str, repo: str, number: int) -> list[str]:
        """The ids of the failed jobs on a pull request."""
        ids = []
        for check in self.pr_checks(owner, repo, number):
            if check.get("bucket") not in ("fail", "cancel"):
                continue
            link = check.get("link") or ""
            for marker in ("/jobs/", "/job/"):
                if marker in link:
                    ids.append(link.split(marker, 1)[1].split("/")[0].split("?")[0])
                    break
        return ids

    def job_logs(self, owner: str, repo: str, job_id: str) -> str:
        """The logs of a single job."""
        proc = subprocess.run(
            [
                self.binary,
                "api",
                f"repos/{owner}/{repo}/actions/jobs/{job_id}/logs",
                "--allow-escape-sequences",
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if proc.returncode != 0:
            return ""
        # Strip the ANSI escape sequences GitHub puts in the log.
        return re.sub(r"\x1b\[[0-9;]*m", "", proc.stdout)

    # ---------------------------------------------------------- workflow
    def action_latest_release(self, owner: str, repo: str) -> str | None:
        data = self.api(f"repos/{owner}/{repo}/releases/latest", check=False)
        if isinstance(data, dict) and data.get("tag_name"):
            return data["tag_name"]
        return None

    def rate_limit(self) -> dict:
        return self.api("rate_limit")
