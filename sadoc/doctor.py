"""The doctor: keeping the default branch green.

A repository's default branch can go red on its own - a new toolchain release
brings a new lint, a flaky test starts failing, an action stops working. Nothing
is watching it, so it stays red until somebody happens to look.

This looks at the latest run on the default branch. If it is failing, the failed
logs go to the model, the model works out what broke and fixes it, and the fix is
offered as a pull request.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .gh import Gh

# Failures that are not worth a pull request.
_TRANSIENT = (
    "network is unreachable",
    "connection reset by peer",
    "temporary failure in name resolution",
    "could not resolve host",
    "the operation was canceled",
    "timed out after",
    "i/o timeout",
    "job was canceled because the",
    "no space left on device",
    "rate limit exceeded",
)

# The kinds of failure the doctor handles.
FIXABLE = "fixable"
TRANSIENT_FAILURE = "transient"
ALREADY_OPEN = "already_open"
NOT_BROKEN = "not_broken"
UNKNOWN = "unknown"


@dataclass
class Diagnosis:
    repo: str
    state: str = UNKNOWN
    branch: str = ""
    detail: str = ""
    failed_job: str = ""
    url: str = ""
    logs: str = ""
    transient_reasons: list[str] = field(default_factory=list)

    @property
    def worth_fixing(self) -> bool:
        return self.state == FIXABLE


def _is_transient(logs: str) -> list[str]:
    """The transient failures in a log, if any."""
    found = []
    lowered = (logs or "").lower()
    for pattern in _TRANSIENT:
        if pattern in lowered:
            found.append(pattern)
    return found


def latest_run_on_branch(gh: Gh, owner: str, repo: str, branch: str) -> dict | None:
    """The most recent completed run on a branch."""
    run = gh.latest_run(owner, repo, branch)
    if not run:
        return None
    if run.get("status") != "completed":
        return None
    return run


def failing_jobs(gh: Gh, owner: str, repo: str, run_id: str) -> list[dict]:
    return [j for j in gh.run_jobs(owner, repo, str(run_id)) if j.get("conclusion") == "failure"]


def diagnose(gh: Gh, owner: str, repo: str, branch: str) -> Diagnosis:
    """Work out why the default branch is red."""
    diagnosis = Diagnosis(repo=f"{owner}/{repo}", branch=branch)
    run = latest_run_on_branch(gh, owner, repo, branch)
    if not run:
        diagnosis.state = NOT_BROKEN
        diagnosis.detail = "no completed run on the default branch"
        return diagnosis
    if run.get("conclusion") != "failure":
        diagnosis.state = NOT_BROKEN
        diagnosis.detail = f"the last run was {run.get('conclusion')}"
        return diagnosis

    jobs = failing_jobs(gh, owner, repo, run["databaseId"])
    if not jobs:
        diagnosis.state = NOT_BROKEN
        diagnosis.detail = "the run failed but no job is marked as failed"
        return diagnosis

    job = jobs[0]
    diagnosis.failed_job = job.get("name", "")
    diagnosis.url = job.get("html_url", "")
    logs = gh.job_logs(owner, repo, str(job.get("id", "")))
    diagnosis.logs = logs[-30000:]
    transient = _is_transient(diagnosis.logs)
    if transient:
        diagnosis.state = TRANSIENT_FAILURE
        diagnosis.transient_reasons = transient
        diagnosis.detail = "the failure looks transient: " + ", ".join(transient)
        return diagnosis
    diagnosis.state = FIXABLE
    diagnosis.detail = f"the {diagnosis.failed_job} job failed"
    return diagnosis


def summarise(diagnosis: Diagnosis, limit: int = 12000) -> str:
    """The part of the log worth showing the model."""
    lines = []
    for line in (diagnosis.logs or "").splitlines():
        if re.search(r"error|Error|ERROR|failed|Failed|FAILED|panic", line):
            lines.append(line.strip())
    if lines:
        return "\n".join(lines[-200:])[-limit:]
    return (diagnosis.logs or "")[-limit:]
