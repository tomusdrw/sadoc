"""What the bot tells you about, and when.

GitHub does not email you about activity on your own pull requests, so the bot
emails instead. Every alert has a dedupe key so the same thing is not sent twice
(e.g. the same reminder on consecutive nights).
"""

from __future__ import annotations

import html
import time
from dataclasses import dataclass

# Alert kinds
PR_OPENED = "pr_opened"
CI_FAILED = "ci_failed"
REVIEW_RECEIVED = "review_received"
REVIEW_UNADDRESSED = "review_unaddressed"
READY_TO_MERGE = "ready_to_merge"
REMINDER_REVIEW = "reminder_review"
REMINDER_MERGE = "reminder_merge"
BLOCKED = "blocked"
AUTO_MERGED = "auto_merged"
MERGED = "merged"
FAILURE = "failure"
NIGHTLY_SUMMARY = "nightly_summary"

# Kinds that are worth waking up for, versus a daily digest.
URGENT = {CI_FAILED, REVIEW_UNADDRESSED, BLOCKED, REVIEW_RECEIVED, AUTO_MERGED, FAILURE}


@dataclass
class Alert:
    kind: str
    subject: str
    text: str
    html: str | None = None
    repo: str | None = None
    pr_number: int | None = None
    dedupe_key: str = ""
    headers: dict | None = None


def _body(title: str, rows: list[tuple[str, str]], footer: str = "") -> str:
    lines = [title, ""]
    for key, value in rows:
        lines.append(f"{key}: {value}")
    if footer:
        lines += ["", footer]
    text = "\n".join(lines)

    h = [f"<h2>{html.escape(title)}</h2>", "<table>"]
    for key, value in rows:
        h.append(
            f"<tr><td style='padding:2px 12px 2px 0;color:#666'>{html.escape(key)}</td>"
            f"<td>{html.escape(str(value))}</td></tr>"
        )
    h.append("</table>")
    if footer:
        h.append(f"<p style='color:#666'>{html.escape(footer)}</p>")
    return text, "\n".join(h)


def pr_opened(
    repo: str, number: int, url: str, tier: str, n_updates: int, titles: list[str], notes: str = ""
) -> Alert:
    text, hbody = _body(
        "Deps bot opened a pull request",
        [
            ("Repository", repo),
            ("Pull request", f"#{number} {url}"),
            (
                "Tier",
                f"{tier} ({'patch/minor' if tier == 'A' else 'major' if tier == 'B' else 'security'})",
            ),
            ("Dependencies updated", str(n_updates)),
        ],
        "This PR needs your review - the bot cannot merge tier B or C on its own."
        if tier != "A"
        else "The bot will merge this automatically once CI is green and no review is outstanding.",
    )
    if titles:
        text += "\n\nUpdated:\n" + "\n".join(f"  - {t}" for t in titles)
    if notes:
        text += f"\n\nAgent notes:\n{notes}"
    return Alert(
        PR_OPENED,
        f"[deps-bot] PR opened: {repo}#{number}",
        text,
        html=hbody,
        repo=repo,
        pr_number=number,
        dedupe_key=f"{PR_OPENED}:{repo}#{number}",
    )


def ci_failed(repo: str, number: int, url: str, tier: str, detail: str) -> Alert:
    text, hbody = _body(
        "Deps bot could not get CI green",
        [("Repository", repo), ("Pull request", f"#{number} {url}"), ("Tier", tier)],
        detail or "The bot exhausted its fix attempts. This one needs a human.",
    )
    return Alert(
        CI_FAILED,
        f"[deps-bot] CI needs a human: {repo}#{number}",
        text,
        html=hbody,
        repo=repo,
        pr_number=number,
        dedupe_key=f"{CI_FAILED}:{repo}#{number}",
    )


def review_received(
    repo: str, number: int, url: str, author: str, body: str, addressed: bool
) -> Alert:
    text, hbody = _body(
        "New review comment on a deps-bot pull request",
        [
            ("Repository", repo),
            ("Pull request", f"#{number} {url}"),
            ("Comment by", f"@{author}"),
            ("Bot handled it", "yes" if addressed else "no - needs a human"),
        ],
        (body or "")[:1500],
    )
    return Alert(
        REVIEW_RECEIVED,
        f"[deps-bot] Review on {repo}#{number} from @{author}",
        text,
        html=hbody,
        repo=repo,
        pr_number=number,
        dedupe_key=f"{REVIEW_RECEIVED}:{repo}#{number}:{author}",
    )


def ready_to_merge(repo: str, number: int, url: str, tier: str) -> Alert:
    text, hbody = _body(
        "Pull request is green and waiting for you",
        [("Repository", repo), ("Pull request", f"#{number} {url}"), ("Tier", tier)],
        "CI passed and the PR is mergeable. It was not auto-merged - merge it when you are happy with it.",
    )
    return Alert(
        READY_TO_MERGE,
        f"[deps-bot] Ready to merge: {repo}#{number}",
        text,
        html=hbody,
        repo=repo,
        pr_number=number,
        dedupe_key=f"{READY_TO_MERGE}:{repo}#{number}",
    )


def reminder_review(repo: str, number: int, url: str, tier: str, age_hours: int) -> Alert:
    text, hbody = _body(
        "Reminder: pull request is still waiting for your review",
        [
            ("Repository", repo),
            ("Pull request", f"#{number} {url}"),
            ("Tier", tier),
            ("Waiting for", f"{age_hours} hours"),
        ],
        "Review and merge it, or close it if you do not want the update.",
    )
    return Alert(
        REMINDER_REVIEW,
        f"[deps-bot] Still waiting: {repo}#{number}",
        text,
        html=hbody,
        repo=repo,
        pr_number=number,
        dedupe_key=f"{REMINDER_REVIEW}:{repo}#{number}:{int(time.time() // 86400)}",
    )


def reminder_merge(repo: str, number: int, url: str, tier: str, age_hours: int) -> Alert:
    text, hbody = _body(
        "Reminder: green pull request is still not merged",
        [
            ("Repository", repo),
            ("Pull request", f"#{number} {url}"),
            ("Tier", tier),
            ("Green for", f"{age_hours} hours"),
        ],
        "CI is green and the PR is mergeable. Merge it or close it.",
    )
    return Alert(
        REMINDER_MERGE,
        f"[deps-bot] Not merged yet: {repo}#{number}",
        text,
        html=hbody,
        repo=repo,
        pr_number=number,
        dedupe_key=f"{REMINDER_MERGE}:{repo}#{number}:{int(time.time() // 86400)}",
    )


def blocked(repo: str, reason: str, url: str = "") -> Alert:
    text, hbody = _body(
        "Deps bot gave up on a repository",
        [("Repository", repo)] + ([("Pull request", url)] if url else []),
        reason,
    )
    return Alert(
        BLOCKED,
        f"[deps-bot] Blocked: {repo}",
        text,
        html=hbody,
        repo=repo,
        dedupe_key=f"{BLOCKED}:{repo}:{int(time.time() // 86400)}",
    )


def auto_merged(repo: str, number: int, url: str, tier: str) -> Alert:
    text, hbody = _body(
        "Pull request was auto-merged",
        [("Repository", repo), ("Pull request", f"#{number} {url}"), ("Tier", tier)],
        "Tier A only: patch and minor updates, CI green, no review outstanding.",
    )
    return Alert(
        AUTO_MERGED,
        f"[deps-bot] Auto-merged: {repo}#{number}",
        text,
        html=hbody,
        repo=repo,
        pr_number=number,
        dedupe_key=f"{AUTO_MERGED}:{repo}#{number}",
    )


def merged_by_human(repo: str, number: int, url: str, tier: str) -> Alert:
    """The pull request was merged by somebody else, not by the bot."""
    text, hbody = _body(
        "Pull request was merged",
        [("Repository", repo), ("Pull request", f"#{number} {url}"), ("Tier", tier)],
        "This one was merged outside the bot.",
    )
    return Alert(
        MERGED,
        f"[deps-bot] Merged: {repo}#{number}",
        text,
        html=hbody,
        repo=repo,
        pr_number=number,
        dedupe_key=f"{MERGED}:{repo}#{number}",
    )


def failure(repo: str, stage: str, detail: str) -> Alert:
    """Something went wrong during a run and a human needs to know."""
    text, hbody = _body(
        "Deps bot could not finish",
        [("Repository", repo), ("Stage", stage)],
        (detail or "no detail")[:2000],
    )
    return Alert(
        FAILURE,
        f"[deps-bot] Failed on {repo} ({stage})",
        text,
        html=hbody,
        repo=repo,
        dedupe_key=f"{FAILURE}:{repo}:{stage}:{int(time.time() // 86400)}",
    )


def nightly_summary(
    repos: list[str], prs_opened: int, prs_merged: int, failures: list[str]
) -> Alert:
    rows = [
        ("Repositories with outdated dependencies", str(len(repos))),
        ("Pull requests opened tonight", str(prs_opened)),
        ("Pull requests auto-merged", str(prs_merged)),
    ]
    if failures:
        rows.append(("Needs attention", ", ".join(failures[:10])))
    text, hbody = _body(
        "Nightly dependency update summary",
        rows,
        "Full report: ~/.local/share/deps-bot/reports/latest.md",
    )
    return Alert(
        NIGHTLY_SUMMARY,
        "[deps-bot] Nightly summary",
        text,
        html=hbody,
        dedupe_key=f"{NIGHTLY_SUMMARY}:{time.strftime('%Y-%m-%d')}",
    )
