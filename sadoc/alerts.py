"""What Sadoc tells you, and how.

Every alert becomes an email: a subject line in his voice, a body rendered from
blocks, and a plain-text twin for clients that prefer it.

The GitHub side of the identity is handled separately - see `persona.py`.
"""

from __future__ import annotations

import time

from . import email_template as et
from .email_template import Block
from .persona import Persona

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
RELEASE_DUE = "release_due"
BRANCH_FIXED = "branch_fixed"
FAILURE = "failure"
NIGHTLY_SUMMARY = "nightly_summary"

# Kinds that are worth waking up for, versus a daily digest.
URGENT = {CI_FAILED, REVIEW_UNADDRESSED, BLOCKED, REVIEW_RECEIVED, AUTO_MERGED, FAILURE}

# Accent colour per kind, so the header bar says something at a glance.
_TONES = {
    CI_FAILED: ("#b45309", "#fffbeb"),
    REVIEW_UNADDRESSED: ("#b45309", "#fffbeb"),
    BLOCKED: ("#b91c1c", "#fef2f2"),
    FAILURE: ("#b91c1c", "#fef2f2"),
    AUTO_MERGED: ("#047857", "#ecfdf5"),
    MERGED: ("#047857", "#ecfdf5"),
    READY_TO_MERGE: ("#047857", "#ecfdf5"),
    NIGHTLY_SUMMARY: ("#1d4ed8", "#eff4ff"),
}


class Alert:
    """One thing to tell the human about."""

    def __init__(
        self,
        kind: str,
        subject: str,
        blocks: list[Block],
        *,
        repo: str | None = None,
        pr_number: int | None = None,
        dedupe_key: str = "",
        heading: str = "",
        footer: str = "",
        persona: Persona | None = None,
    ):
        self.kind = kind
        self.subject = subject
        self.blocks = blocks
        self.repo = repo
        self.pr_number = pr_number
        self.dedupe_key = dedupe_key
        self.heading = heading
        self.persona = persona or Persona()
        self.footer = footer or self.persona.signature()

    # ---------------------------------------------------------------- text
    @property
    def text(self) -> str:
        return et.render_text(self.subject, self.blocks, self.footer)

    @property
    def html(self) -> str:
        accent, soft = _TONES.get(self.kind, (et.ACCENT, et.ACCENT_SOFT))
        return et.render_html(
            self.subject,
            self.blocks,
            heading=self.heading,
            accent=accent,
            accent_soft=soft,
            footer=self.footer,
            preheader=self.blocks[0].value if self.blocks else "",
            accent_label=self.persona.name,
        )

    def signature(self, extra: str = "") -> str:
        """The footer of the email."""
        parts = [self.persona.tagline]
        if extra:
            parts.append(extra)
        return " &middot; ".join(parts) if parts else ""


def _rows(*pairs) -> list[Block]:
    return [Block(kind="row", label=label, value=value) for label, value in pairs]


def pr_opened(
    repo: str,
    number: int,
    url: str,
    tier: str,
    n_updates: int,
    titles: list[str],
    notes: str = "",
    persona: Persona | None = None,
) -> Alert:
    p = persona or Persona()
    tier_text = {
        "A": "patch and minor updates",
        "B": "major version updates",
        "C": "security updates",
    }.get(tier, tier)
    blocks = _rows(
        ("Repository", repo),
        ("Pull request", f"#{number}"),
        ("Link", url),
        ("Updates", f"{n_updates} ({tier_text})"),
    )
    if titles:
        blocks.append(Block(kind="list", items=titles))
    if notes:
        blocks.append(Block(kind="paragraph", value=notes))
    if tier == "A":
        blocks.append(
            Block(
                kind="note",
                tone="ok",
                value="A routine haul. It will merge on its own once the checks are green.",
            )
        )
    elif tier == "C":
        blocks.append(
            Block(
                kind="note",
                tone="danger",
                value="This one closes a published security advisory, so it waits for you.",
            )
        )
    else:
        blocks.append(
            Block(
                kind="note",
                tone="warn",
                value="This one carries a breaking change, so it waits for your hand.",
            )
        )
    return Alert(
        PR_OPENED,
        p.subject(f"a pull request for {repo}"),
        blocks,
        repo=repo,
        pr_number=number,
        dedupe_key=f"{PR_OPENED}:{repo}#{number}",
        heading="A pull request is ready for you",
        persona=p,
    )


def ci_failed(
    repo: str, number: int, url: str, tier: str, detail: str, persona: Persona | None = None
) -> Alert:
    p = persona or Persona()
    blocks = _rows(
        ("Repository", repo),
        ("Pull request", f"#{number}"),
        ("Link", url),
    )
    if detail:
        blocks.append(Block(kind="paragraph", value=detail))
    blocks.append(
        Block(
            kind="note",
            tone="warn",
            value="The checks are red and I could not make them green. It needs a hand.",
        )
    )
    return Alert(
        CI_FAILED,
        p.subject(f"the checks are failing on {repo}"),
        blocks,
        repo=repo,
        pr_number=number,
        dedupe_key=f"{CI_FAILED}:{repo}#{number}",
        heading="Red sails",
        persona=p,
    )


def review_received(
    repo: str,
    number: int,
    url: str,
    author: str,
    body: str,
    addressed: bool,
    persona: Persona | None = None,
) -> Alert:
    p = persona or Persona()
    blocks = _rows(
        ("Repository", repo),
        ("Pull request", f"#{number}"),
        ("Link", url),
        ("From", f"@{author}"),
    )
    if body:
        blocks.append(Block(kind="paragraph", value=body[:1500]))
    blocks.append(
        Block(
            kind="note",
            tone="ok" if addressed else "warn",
            value="I have dealt with the comment."
            if addressed
            else "I could not deal with this one - it needs you.",
        )
    )
    return Alert(
        REVIEW_RECEIVED,
        p.subject(f"a review on {repo}"),
        blocks,
        repo=repo,
        pr_number=number,
        dedupe_key=f"{REVIEW_RECEIVED}:{repo}#{number}:{author}",
        heading="Word from the shore",
        persona=p,
    )


def ready_to_merge(
    repo: str, number: int, url: str, tier: str, persona: Persona | None = None
) -> Alert:
    p = persona or Persona()
    blocks = _rows(
        ("Repository", repo),
        ("Pull request", f"#{number}"),
        ("Link", url),
    )
    blocks.append(
        Block(
            kind="note",
            tone="ok",
            value="Green and waiting. It was not merged automatically - take a look and "
            "merge it when you are content.",
        )
    )
    return Alert(
        READY_TO_MERGE,
        p.subject(f"{repo} is green and waiting for you"),
        blocks,
        repo=repo,
        pr_number=number,
        dedupe_key=f"{READY_TO_MERGE}:{repo}#{number}",
        heading="Ready to come ashore",
        persona=p,
    )


def reminder_review(
    repo: str, number: int, url: str, tier: str, age_hours: int, persona: Persona | None = None
) -> Alert:
    p = persona or Persona()
    blocks = _rows(
        ("Repository", repo),
        ("Pull request", f"#{number}"),
        ("Link", url),
        ("Waiting for", f"{age_hours} hours"),
    )
    blocks.append(
        Block(
            kind="note",
            tone="warn",
            value="Still no word from you. Review it and merge it, or close it if the "
            "update is not wanted.",
        )
    )
    return Alert(
        REMINDER_REVIEW,
        p.subject(f"{repo} is still waiting for your review"),
        blocks,
        repo=repo,
        pr_number=number,
        dedupe_key=f"{REMINDER_REVIEW}:{repo}#{number}:{int(time.time() // 86400)}",
        heading="A reminder from the west",
        persona=p,
    )


def reminder_merge(
    repo: str, number: int, url: str, tier: str, age_hours: int, persona: Persona | None = None
) -> Alert:
    p = persona or Persona()
    blocks = _rows(
        ("Repository", repo),
        ("Pull request", f"#{number}"),
        ("Link", url),
        ("Green for", f"{age_hours} hours"),
    )
    blocks.append(
        Block(
            kind="note",
            tone="warn",
            value="The checks have been green for a while. Merge it or close it.",
        )
    )
    return Alert(
        REMINDER_MERGE,
        p.subject(f"{repo} is green but still unmerged"),
        blocks,
        repo=repo,
        pr_number=number,
        dedupe_key=f"{REMINDER_MERGE}:{repo}#{number}:{int(time.time() // 86400)}",
        heading="A reminder from the west",
        persona=p,
    )


def blocked(repo: str, reason: str, url: str = "", persona: Persona | None = None) -> Alert:
    p = persona or Persona()
    blocks = _rows(("Repository", repo))
    if url:
        blocks.append(Block(kind="row", label="Pull request", value=url))
    blocks.append(Block(kind="paragraph", value=reason))
    blocks.append(
        Block(
            kind="note",
            tone="danger",
            value="I have given up on this one. It needs a hand.",
        )
    )
    return Alert(
        BLOCKED,
        p.subject(f"I have given up on {repo}"),
        blocks,
        repo=repo,
        dedupe_key=f"{BLOCKED}:{repo}:{int(time.time() // 86400)}",
        heading="Blocked",
        persona=p,
    )


def auto_merged(
    repo: str, number: int, url: str, tier: str, persona: Persona | None = None
) -> Alert:
    p = persona or Persona()
    blocks = _rows(
        ("Repository", repo),
        ("Pull request", f"#{number}"),
        ("Link", url),
    )
    blocks.append(
        Block(
            kind="note",
            tone="ok",
            value="Merged on my own: patch and minor updates, checks green, no review outstanding.",
        )
    )
    return Alert(
        AUTO_MERGED,
        p.subject(f"merged {repo} for you"),
        blocks,
        repo=repo,
        pr_number=number,
        dedupe_key=f"{AUTO_MERGED}:{repo}#{number}",
        heading="Merged and gone",
        persona=p,
    )


def merged_by_human(
    repo: str, number: int, url: str, tier: str, persona: Persona | None = None
) -> Alert:
    p = persona or Persona()
    blocks = _rows(
        ("Repository", repo),
        ("Pull request", f"#{number}"),
        ("Link", url),
    )
    blocks.append(
        Block(kind="note", tone="ok", value="You merged this one yourself. Good to know.")
    )
    return Alert(
        MERGED,
        p.subject(f"{repo} was merged"),
        blocks,
        repo=repo,
        pr_number=number,
        dedupe_key=f"{MERGED}:{repo}#{number}",
        heading="Merged and gone",
        persona=p,
    )


def failure(repo: str, stage: str, detail: str, persona: Persona | None = None) -> Alert:
    p = persona or Persona()
    blocks = _rows(
        ("Repository", repo),
        ("Stage", stage),
    )
    if detail:
        blocks.append(Block(kind="code", value=detail[:2000]))
    blocks.append(
        Block(
            kind="note",
            tone="danger",
            value="Something went wrong and I could not finish. It needs a hand.",
        )
    )
    return Alert(
        FAILURE,
        p.subject(f"I could not finish {repo}"),
        blocks,
        repo=repo,
        dedupe_key=f"{FAILURE}:{repo}:{stage}:{int(time.time() // 86400)}",
        heading="Something went wrong",
        persona=p,
    )


def branch_fixed(
    repo: str, url: str, job: str, cause: str, persona: Persona | None = None
) -> Alert:
    """The default branch was broken and a fix has been offered."""
    p = persona or Persona()
    blocks = _rows(
        ("Repository", repo),
        ("Failed job", job or "the build"),
        ("Fix", url),
    )
    if cause:
        blocks.append(Block(kind="paragraph", value=cause))
    blocks.append(
        Block(
            kind="note",
            tone="ok",
            value="The default branch was red. I have opened a pull request with the fix.",
        )
    )
    return Alert(
        BRANCH_FIXED,
        p.subject(f"the default branch of {repo} was broken - I have a fix"),
        blocks,
        repo=repo,
        dedupe_key=f"{BRANCH_FIXED}:{repo}:{url}",
        heading="A broken branch, found and mended",
        footer=p.signature(),
        persona=p,
    )


def release_due(
    repo: str,
    version: str,
    published: str,
    kinds: list[str],
    note: str,
    persona: Persona | None = None,
) -> Alert:
    """The version has moved on and the project may owe a release."""
    p = persona or Persona()
    blocks = _rows(
        ("Repository", repo),
        ("Version now", version),
        ("Last published", published or "nothing yet"),
        ("What a release means here", ", ".join(kinds) if kinds else "not worked out"),
    )
    if note:
        blocks.append(Block(kind="paragraph", value=note))
    blocks.append(
        Block(
            kind="note",
            tone="warn",
            value="The version has moved past what has been published. A release would "
            "put it out in the world.",
        )
    )
    return Alert(
        RELEASE_DUE,
        p.subject(f"{repo} is ready for a release"),
        blocks,
        repo=repo,
        dedupe_key=f"{RELEASE_DUE}:{repo}:{version}",
        heading="Ready to be let go",
        footer=p.signature(),
        persona=p,
    )


def nightly_summary(
    repos: list[str],
    prs_opened: int,
    prs_merged: int,
    failures: list[str],
    persona: Persona | None = None,
) -> Alert:
    p = persona or Persona()
    blocks = _rows(
        ("Repositories with outdated dependencies", str(len(repos))),
        ("Pull requests opened tonight", str(prs_opened)),
        ("Pull requests merged", str(prs_merged)),
    )
    if failures:
        blocks.append(Block(kind="paragraph", value="Needing a hand: " + ", ".join(failures[:10])))
    return Alert(
        NIGHTLY_SUMMARY,
        p.subject("the night's report"),
        blocks,
        dedupe_key=f"{NIGHTLY_SUMMARY}:{time.strftime('%Y-%m-%d')}",
        heading="The night's report",
        persona=p,
    )
