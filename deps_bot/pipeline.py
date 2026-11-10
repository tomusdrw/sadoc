"""The deps-bot pipeline.

One repo at a time:
    detect -> bump (pi) -> verify (local) -> migrate (pi) -> commit/push -> PR
          -> watch CI -> fixci (pi) -> ... -> monitor (reviews, mergeability)

The orchestrator owns git and GitHub. The model only ever edits files inside a
sandboxed clone.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from datetime import UTC

from . import advisories, alerts, detect, sanitize
from .detect import Update
from .gitops import Git, GitError
from .piworker import PiModel, PiSession

TIER_A = "A"  # patch + minor
TIER_B = "B"  # major
TIER_C = "C"  # security

PR_TITLES = {
    TIER_A: "chore(deps): update {n} dependencies",
    TIER_B: "feat(deps)!: update {n} dependencies to major versions",
    TIER_C: "fix(deps): update {n} vulnerable dependencies",
}

PR_LABELS = {TIER_A: "dependencies", TIER_B: "dependencies", TIER_C: "dependencies"}


@dataclass
class RepoJob:
    owner: str
    name: str
    branch: str
    tier: str
    updates: list[Update] = field(default_factory=list)


@dataclass
class PrGroup:
    """One pull request's worth of updates.

    Updates are grouped by the files they would touch, *not* purely by tier: a
    major bump and a patch bump to the same package.json have to live in the
    same PR or the two branches conflict. The group takes the highest tier
    present, which also decides the merge policy.
    """

    tier: str
    updates: list[Update] = field(default_factory=list)
    touch: set[str] = field(default_factory=set)

    @property
    def manifests(self) -> list[str]:
        return sorted({u.manifest for u in self.updates})


_TIER_ORDER = {TIER_C: 3, TIER_B: 2, TIER_A: 1}

# Which lockfile a manifest's ecosystem implies.
_LOCKFILE_FOR = {
    "crates": "Cargo.lock",
    "npm": "package-lock.json",
    "pypi": "poetry.lock",
    "go": "go.sum",
}


def _touch_set(update: Update) -> set[str]:
    """The files an update would modify."""
    files = {update.manifest}
    lock = _LOCKFILE_FOR.get(update.ecosystem)
    if lock:
        files.add(lock)
    return files


def group_updates(updates: list[Update], security: dict | None = None) -> list[PrGroup]:
    """Cluster updates into PR-sized groups.

    Two updates end up in the same PR when they would touch the same file.
    The tier of a group is the highest tier among its members.
    """
    security = security or {}
    # Union-find over the updates, linked by shared files.
    parent = list(range(len(updates)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    by_file: dict[str, list[int]] = {}
    for idx, update in enumerate(updates):
        for f in _touch_set(update):
            by_file.setdefault(f, []).append(idx)
    for indices in by_file.values():
        for other in indices[1:]:
            union(indices[0], other)

    clusters: dict[int, list[Update]] = {}
    for idx, update in enumerate(updates):
        clusters.setdefault(find(idx), []).append(update)

    groups: list[PrGroup] = []
    for members in clusters.values():
        tier = TIER_A
        for m in members:
            t = (
                TIER_C
                if security.get((m.manifest, m.name))
                else (TIER_B if m.bump_kind == "major" else TIER_A)
            )
            if _TIER_ORDER[t] > _TIER_ORDER[tier]:
                tier = t
        touch: set[str] = set()
        for m in members:
            touch |= _touch_set(m)
        groups.append(PrGroup(tier=tier, updates=members, touch=touch))

    groups.sort(key=lambda g: (-_TIER_ORDER[g.tier], g.manifests[0] if g.manifests else ""))
    return groups


@dataclass
class JobResult:
    repo: str
    tier: str
    status: str = "pending"  # pr_opened | skipped | failed | no_updates
    pr_number: int | None = None
    pr_url: str = ""
    detail: str = ""
    changes: list[str] = field(default_factory=list)
    ci_state: str = ""


# ------------------------------------------------------------------ helpers
def tier_for(update: Update, security: bool) -> str:
    if security:
        return TIER_C
    return TIER_B if update.bump_kind == "major" else TIER_A


def render_updates(updates: list[Update]) -> str:
    rows = [
        "| Manifest | Package | Ecosystem | Current | Target | Kind | Section |",
        "|---|---|---|---|---|---|---|",
    ]
    for u in updates:
        rows.append(
            f"| `{u.manifest}` | `{u.name}` | {u.ecosystem} | "
            f"{u.current or '(unpinned)'} | **{u.latest}** | {u.bump_kind} | {u.section} |"
        )
    return "\n".join(rows)


def render_update_list(updates: list[Update]) -> str:
    return "\n".join(
        f"- {u.name}: {u.current or '(unpinned)'} -> {u.latest}  "
        f"[{u.ecosystem}/{u.bump_kind}] in {u.manifest}"
        for u in updates
    )


def load_prompt(name: str, **kwargs) -> str:
    from .config import REPO_ROOT

    text = (REPO_ROOT / "prompts" / f"{name}.md").read_text()
    for key, value in kwargs.items():
        text = text.replace("{{" + key + "}}", str(value))
    return text


# ------------------------------------------------------------------- worker
class Worker:
    def __init__(self, cfg, gh, state, log=print, notifier=None):
        self.cfg = cfg
        self.gh = gh
        self.state = state
        self.log = log
        self.notifier = notifier
        self.adv_cache = advisories.AdvisoryCache(gh)

    def _notify_secret_names(self) -> list[str]:
        """Environment variables that hold notification secrets."""
        ncfg = self.cfg.notify or {}
        names = [ncfg.get("resend_api_key_env"), ncfg.get("smtp_password_env")]
        return [n for n in names if n]

    def notify(self, alert) -> str:
        """Send an alert, if notifications are enabled."""
        if not self.notifier:
            return "disabled"
        try:
            status = self.notifier.send(alert)
            if status == "sent":
                self.log(f"notified: {alert.subject}", repo=alert.repo)
            return status
        except Exception as exc:  # noqa: BLE001
            self.log(f"notification failed: {exc}", repo=alert.repo, level="error")
            return "failed"

    # ------------------------------------------------------------- detect
    def detect_repo(self, owner: str, repo: str, branch: str) -> list[Update]:
        updates = detect.detect_repo(self.gh, owner, repo, branch)
        for u in updates:
            u.bump_kind = u.bump_kind
        return updates

    def security_advisory_for(self, update: Update) -> dict | None:
        if not update.current:
            return None
        advs = self.adv_cache.advisories_for(update.ecosystem, update.name)
        return advisories.is_vulnerable(update.current, advs)

    def group_by_tier(self, updates: list[Update]) -> dict[str, list[Update]]:
        groups: dict[str, list[Update]] = {TIER_A: [], TIER_B: [], TIER_C: []}
        for u in updates:
            adv = self.security_advisory_for(u)
            tier = tier_for(u, adv is not None)
            groups[tier].append(u)
        return {t: v for t, v in groups.items() if v}

    def build_groups(self, updates: list[Update]) -> list[PrGroup]:
        """Cluster updates into PR-sized, conflict-free groups."""
        security = {}
        for u in updates:
            if self.security_advisory_for(u):
                security[(u.manifest, u.name)] = True
        return group_updates(updates, security)

    # -------------------------------------------------------------- clone
    def existing_deps_pr(self, owner: str, repo: str) -> dict | None:
        """An open PR that deps-bot already opened for this repo."""
        try:
            prs = self.gh.pr_list(owner, repo, state="open")
        except Exception:
            return None
        for pr in prs:
            branch = pr.get("headRefName") or ""
            if branch.startswith(f"deps/{repo}/"):
                return pr
        return None

    def prepare_clone(self, owner: str, repo: str, branch: str) -> Git:
        dest = self.cfg.workdir / f"{owner}__{repo}"
        git = Git(dest)
        git.clone(f"https://github.com/{owner}/{repo}.git", branch)
        return git

    # ------------------------------------------------------------- verify
    def verify_command(self, repo: str) -> str | None:
        override = (self.cfg.repos.get(repo) or {}).get("verify")
        return override

    def run_verify(self, git: Git, repo: str, session: PiSession | None = None) -> tuple[bool, str]:
        cmd = self.verify_command(repo)
        if not cmd:
            return True, "no local verify configured"
        if session is not None:
            resp = session.bash(cmd, timeout=1800)
            data = resp.get("data") or {}
            output = data.get("output", "")
            code = data.get("exitCode")
        else:
            import subprocess

            proc = subprocess.run(
                cmd, shell=True, cwd=str(git.path), capture_output=True, text=True, timeout=1800
            )
            output = (proc.stdout or "") + (proc.stderr or "")
            code = proc.returncode
        return code == 0, (output or "")[-20000:]

    # --------------------------------------------------------------- PR
    def pr_body(self, updates: list[Update], repo: str, tier: str, note: str, ref_id: str) -> str:
        """A pull request body that reads like a normal dependency update.

        Nothing about the tooling goes in here - the model, the transcript and
        the verification output stay in the local database, and the PR only
        carries a short reference id.
        """
        lines = ["Updates outdated dependencies in this repository.", ""]
        lines.append("| Package | From | To |")
        lines.append("|---|---|---|")
        for u in updates:
            lines.append(f"| `{u.name}` | {u.current or '(unpinned)'} | {u.latest} |")
        lines.append("")
        if note:
            lines += [note.strip(), ""]
        if tier == TIER_B:
            lines += [
                "> These updates include major version changes and may require code adjustments.",
                "",
            ]
        elif tier == TIER_C:
            lines += ["> These updates address published security advisories.", ""]
        lines += [
            "<sub>",
            f"ref {ref_id}",
            "</sub>",
        ]
        return "\n".join(lines)

    def commit_message(self, tier: str, updates: list[Update], note: str = "") -> str:
        """A conventional-commit style message, like any other dependency bump."""
        if tier == TIER_C:
            head = "fix(deps): update vulnerable dependencies"
        elif tier == TIER_B:
            head = "feat(deps)!: update dependencies to major versions"
        else:
            head = "chore(deps): update dependencies"
        body = "\n".join(f"- {u.name}: {u.current or '(unpinned)'} -> {u.latest}" for u in updates)
        message = f"{head}\n\n{body}"
        if note:
            message += f"\n\n{sanitize.scrub_text(note)}"
        return message

    def branch_name(self, repo: str, tier: str, group: PrGroup | None) -> str:
        """A branch name that looks like any other dependency bump branch."""
        package = "dependencies"
        version = ""
        if group and group.updates:
            first = sorted(group.updates, key=lambda u: u.name)[0]
            package = re.sub(r"[^A-Za-z0-9.]+", "-", first.name).strip("-").lower()
            version = re.sub(r"[^A-Za-z0-9.]+", "-", first.latest).strip("-")
        if version:
            return f"deps/{package}-{version}"
        return f"deps/{package}"

    # ---------------------------------------------------------- leak check
    def _public_note(self, report: str) -> str:
        """Turn the agent's internal report into a short, human-facing note.

        The report is structured (`KEY: value` lines). Only the parts that make
        sense to a reviewer are kept, and any mention of the tooling is removed.
        """
        if not report:
            return ""
        fields: dict[str, str] = {}
        for line in report.splitlines():
            m = re.match(r"^\s*([A-Z][A-Z_]{2,}):\s*(.*)$", line)
            if m:
                fields[m.group(1)] = m.group(2).strip()
            elif fields:
                last = list(fields)[-1]
                fields[last] = (fields[last] + " " + line.strip()).strip()

        note = fields.get("NOTES", "")
        skipped = fields.get("SKIPPED", "")
        if skipped and skipped.lower() not in ("none", "n/a", "-"):
            extra = f"Skipped: {skipped}."
            note = f"{note} {extra}".strip()
        note = sanitize.scrub_text(note)
        # If anything leaky survived the scrub, publish nothing rather than
        # something garbled.
        if sanitize.contains_leak(note):
            return ""
        # Drop the note entirely if scrubbing left nothing meaningful.
        if len(note) < 8:
            return ""
        return note

    def _check_for_leaks(self, git: Git, changed: list[str]) -> dict:
        """Look for tooling references in the files the agent edited.

        Comment lines that mention the tooling are removed; anything in real code
        blocks the PR, because that needs a human to look at.
        """
        diff = git.diff()
        hits = sanitize.scan_diff(diff)
        if not hits:
            return {"hits": [], "blocking": False, "summary": ""}

        # Try to remove offending comment lines.
        files: dict[str, list[str]] = {}
        for path in changed:
            full = git.path / path
            if not full.is_file() or full.stat().st_size > 2_000_000:
                continue
            try:
                files[path] = full.read_text(errors="replace").splitlines()
            except OSError:
                continue
        removed = sanitize.strip_comment_hits(hits, files)
        if removed:
            for path, lines in files.items():
                (git.path / path).write_text("\n".join(lines) + "\n")
            git.stage_all()
            hits = [h for h in hits if h not in removed]

        blocking = [h for h in hits if not h["is_comment"]]
        summary = ""
        if blocking:
            summary = "tooling references found in code: " + "; ".join(
                f"{h['file']}: {h['marker']}" for h in blocking[:5]
            )
        elif removed:
            summary = f"removed {len(removed)} comment line(s) mentioning tooling"
        return {"hits": hits, "removed": removed, "blocking": bool(blocking), "summary": summary}

    # ------------------------------------------------------------ process
    def process_repo(
        self,
        owner: str,
        repo: str,
        tier: str,
        updates: list[Update],
        dry_run: bool | None = None,
        group: PrGroup | None = None,
    ) -> JobResult:
        dry_run = self.cfg.dry_run if dry_run is None else dry_run
        result = JobResult(repo=f"{owner}/{repo}", tier=tier)
        default_branch = self.gh.default_branch(owner, repo)
        branch = self.branch_name(repo, tier, group)
        updates = group.updates if group else updates

        attempt_id = self.state.start_attempt(f"{owner}/{repo}", f"tier_{tier.lower()}")
        self.state.log(
            f"processing {owner}/{repo} tier {tier} ({len(updates)} updates)",
            repo=f"{owner}/{repo}",
        )

        try:
            git = self.prepare_clone(owner, repo, default_branch)
            git.create_branch(branch)
            result.changes = []

            model = self.cfg.model_for("migrate" if tier == TIER_B else "bump")
            spec = PiModel(
                name=f"depsbot-{model.provider}",
                base_url=model.base_url,
                model=model.model,
                context=model.context,
                max_tokens=model.max_tokens,
                reasoning=model.reasoning,
            )
            session = PiSession(
                cwd=git.path,
                model=spec,
                agent_dir=self.cfg.agent_dir,
                allow_network=self.cfg.allow_network,
                scrub_env=self.cfg.scrub_env,
                extra_secrets=self._notify_secret_names(),
            )
            try:
                session.start()
                # --- bump -------------------------------------------------
                bump_prompt = load_prompt(
                    "bump",
                    UPDATES=render_updates(updates),
                    MANIFESTS=", ".join(sorted({u.manifest for u in updates})),
                )
                bump = session.prompt(bump_prompt)
                self.state.log(f"bump done: {bump.text[:400]}", repo=f"{owner}/{repo}")
                result.changes = git.changed_files()
                if not result.changes:
                    result.status = "skipped"
                    result.detail = "model made no changes"
                    self.state.end_attempt(attempt_id, "skipped", result.detail)
                    return result

                # --- local verify ----------------------------------------
                ok, output = self.run_verify(git, repo, session)
                if not ok:
                    migrate_prompt = load_prompt(
                        "migrate",
                        UPDATES=render_update_list(updates),
                        ERROR=output[-15000:],
                    )
                    mig = session.prompt(migrate_prompt)
                    self.state.log(f"migrate done: {mig.text[:400]}", repo=f"{owner}/{repo}")
                    ok, output = self.run_verify(git, repo, session)
                if not ok and self.verify_command(repo):
                    result.status = "failed"
                    result.detail = "local verification still failing after migration"
                    self.state.end_attempt(attempt_id, "failed", result.detail)
                    return result

                if dry_run:
                    result.status = "skipped"
                    result.detail = (
                        f"dry run - would open PR with {len(result.changes)} changed files"
                    )
                    self.state.log(result.detail, repo=f"{owner}/{repo}")
                    self.state.end_attempt(attempt_id, "dry-run", result.detail)
                    return result

                # --- leak check ------------------------------------------
                leak = self._check_for_leaks(git, result.changes)
                if leak["blocking"]:
                    result.status = "failed"
                    result.detail = "blocked: " + leak["summary"]
                    self.state.log(result.detail, repo=f"{owner}/{repo}", level="error", data=leak)
                    self.state.end_attempt(attempt_id, "failed", result.detail)
                    return result

                # --- reviewer-facing note ---------------------------------
                public_note = self._public_note(bump.text)
                try:
                    note_prompt = load_prompt(
                        "prnote",
                        UPDATES=render_update_list(updates),
                        WORK=sanitize.scrub_text(bump.text)[:4000],
                    )
                    note_result = session.prompt(note_prompt, timeout=900)
                    candidate = sanitize.scrub_text(note_result.text.strip())
                    if candidate.upper().startswith("NONE"):
                        candidate = ""
                    if len(candidate) > 20:
                        public_note = candidate
                except Exception as exc:  # noqa: BLE001
                    self.log(f"note generation failed: {exc}", repo=f"{owner}/{repo}")
                    public_note = public_note or ""

                # --- commit + push --------------------------------------
                message = self.commit_message(tier, updates, public_note)
                git.commit(message)
                git.push(branch)

                # --- open PR --------------------------------------------
                ref_id = _make_ref_id()
                title = PR_TITLES[tier].format(n=len(updates))
                body = self.pr_body(updates, f"{owner}/{repo}", tier, public_note, ref_id)
                pr = self.gh.open_pr(owner, repo, title, body, branch, default_branch)
                url = pr.get("url", "")
                number = self._pr_number(url)
                result.status = "pr_opened"
                result.pr_number = number
                result.pr_url = url
                self.state.record_pr(
                    f"{owner}/{repo}",
                    number or 0,
                    tier=tier,
                    title=title,
                    url=url,
                    branch=branch,
                    state="open",
                )
                # Everything that must not appear in the PR lives here.
                self.state.record_pr_details(
                    f"{owner}/{repo}",
                    number or 0,
                    ref_id=ref_id,
                    tier=tier,
                    model=self.cfg.model_for("migrate" if tier == TIER_B else "bump").model,
                    base_url=self.cfg.model_for("migrate" if tier == TIER_B else "bump").base_url,
                    branch=branch,
                    updates=json.dumps([u.as_dict() for u in updates]),
                    agent_notes=bump.text,
                    verify_output=output[-20000:],
                    transcript=json.dumps(session.transcript[-200:])[-40000:],
                )
                self.state.log(f"opened PR {url} (ref {ref_id})", repo=f"{owner}/{repo}")
                self.state.end_attempt(attempt_id, "pr_opened", url)

                self.notify(
                    alerts.pr_opened(
                        f"{owner}/{repo}",
                        number or 0,
                        url,
                        tier,
                        len(updates),
                        [f"{u.name}: {u.current or '(unpinned)'} -> {u.latest}" for u in updates],
                        bump.text[:800],
                    )
                )

                if not number:
                    return result

                # --- drive CI to green ----------------------------------
                ci_state = self.watch_ci(owner, repo, number)
                result.ci_state = ci_state
                self.state.log(f"CI state after push: {ci_state}", repo=f"{owner}/{repo}")
                for _ in range(max(0, self.cfg.max_attempts - 1)):
                    if ci_state != "fail":
                        break
                    self.state.log(
                        "CI failed - feeding logs back to the model", repo=f"{owner}/{repo}"
                    )
                    note = self.fix_ci(
                        owner, repo, number, branch, default_branch, updates, git, session
                    )
                    if "no changes" in note:
                        break
                    ci_state = self.watch_ci(owner, repo, number)
                    result.ci_state = ci_state
                    self.state.log(f"CI state after fix: {ci_state}", repo=f"{owner}/{repo}")

                # --- auto-merge (tier A only) ----------------------------
                if not dry_run:
                    ok, why = self.may_auto_merge(owner, repo, number, tier)
                    if ok:
                        self.gh.pr_merge(owner, repo, number)
                        self.state.record_pr(
                            f"{owner}/{repo}",
                            number,
                            state="merged",
                            auto_merged=1,
                            updated_at=time.time(),
                        )
                        self.state.log(f"auto-merged PR #{number}", repo=f"{owner}/{repo}")
                        result.detail = "auto-merged"
                        self.notify(alerts.auto_merged(f"{owner}/{repo}", number, url, tier))
                    else:
                        self.state.log(f"not auto-merged: {why}", repo=f"{owner}/{repo}")
                        result.detail = why
                        if tier == "A" and ci_state == "pass":
                            self.notify(alerts.ready_to_merge(f"{owner}/{repo}", number, url, tier))
                elif ci_state == "fail":
                    self.notify(
                        alerts.ci_failed(
                            f"{owner}/{repo}",
                            number or 0,
                            url,
                            tier,
                            "CI failed and the bot could not fix it.",
                        )
                    )
                return result
            finally:
                session.close()
        except (GitError, Exception) as exc:  # noqa: BLE001 - surfaced to the caller
            import traceback

            result.status = "failed"
            result.detail = f"{type(exc).__name__}: {exc}"[:500]
            detail = result.detail + "\n" + traceback.format_exc()
            self.state.log(
                result.detail,
                repo=f"{owner}/{repo}",
                level="error",
                data={"traceback": detail[-4000:]},
            )
            self.state.end_attempt(attempt_id, "failed", detail)
            return result

    def _pr_number(self, url: str) -> int | None:
        m = re.search(r"/pull/(\d+)", url or "")
        return int(m.group(1)) if m else None

    # ---------------------------------------------------------------- CI
    def watch_ci(self, owner: str, repo: str, number: int, timeout_s: int = 2400) -> str:
        """Wait for the PR checks to settle. Returns pass / fail / pending / none.

        GitHub needs a moment to register the checks after a push, so an empty
        check list is not immediately "no CI" - we wait for them to appear
        before giving up.
        """
        deadline = time.time() + timeout_s
        empty_since: float | None = None
        while time.time() < deadline:
            checks = self.gh.pr_checks(owner, repo, number)
            if not checks:
                # No checks yet. Give them a couple of minutes to show up.
                if empty_since is None:
                    empty_since = time.time()
                elif time.time() - empty_since > 180:
                    return "none"
                time.sleep(15)
                continue
            empty_since = None
            buckets = [c.get("bucket") for c in checks]
            if any(b in ("fail", "cancel") for b in buckets):
                return "fail"
            if all(b in ("pass", "skipping") for b in buckets):
                return "pass"
            time.sleep(20)
        return "pending"

    def failed_ci_logs(self, owner: str, repo: str, number: int) -> str:
        run_ids = self.gh.pr_run_ids(owner, repo, number)
        chunks = []
        for run_id in run_ids[:4]:
            logs = self.gh.run_logs_failed(owner, repo, run_id, max_bytes=20000)
            if logs.strip():
                chunks.append(f"--- run {run_id} ---\n{logs}")
        return "\n".join(chunks)[-40000:]

    def fix_ci(
        self,
        owner: str,
        repo: str,
        number: int,
        branch: str,
        default_branch: str,
        updates: list[Update],
        git: Git,
        session: PiSession,
    ) -> str:
        """Feed the failed CI logs back to the model and push a fix."""
        logs = self.failed_ci_logs(owner, repo, number)
        if not logs.strip():
            return "no failed logs found"
        prompt = load_prompt(
            "fixci",
            UPDATES=render_update_list(updates),
            ERROR=logs,
        )
        result = session.prompt(prompt)
        self.state.log(f"fixci: {result.text[:400]}", repo=f"{owner}/{repo}")
        if not git.changed_files():
            return "model made no changes"
        git.commit(self.commit_message(TIER_A, updates, "Fixes a failing CI check."))
        git.push(branch)
        return result.text[:500]

    # ------------------------------------------------------------ reviews
    def collect_review_comments(self, owner: str, repo: str, number: int) -> list[dict]:
        """Inline review comments and top-level issue comments, newest first."""
        out = []
        for c in self.gh.pr_review_comments(owner, repo, number):
            body = (c.get("body") or "").strip()
            if not body or body.startswith("deps-bot"):
                continue
            out.append(
                {
                    "kind": "inline",
                    "path": c.get("path"),
                    "line": c.get("line") or c.get("original_line"),
                    "author": (c.get("user") or {}).get("login"),
                    "body": body,
                    "created_at": c.get("created_at"),
                    "id": c.get("id"),
                }
            )
        for c in self.gh.pr_issue_comments(owner, repo, number):
            body = (c.get("body") or "").strip()
            if not body or body.startswith("deps-bot"):
                continue
            out.append(
                {
                    "kind": "issue",
                    "path": None,
                    "line": None,
                    "author": (c.get("user") or {}).get("login"),
                    "body": body,
                    "created_at": c.get("created_at"),
                    "id": c.get("id"),
                }
            )
        out.sort(key=lambda c: c.get("created_at") or "")
        return out

    def format_comments(self, comments: list[dict]) -> str:
        lines = []
        for i, c in enumerate(comments, 1):
            where = f"`{c['path']}:{c['line']}`" if c.get("path") else "general"
            lines.append(
                f"### Comment {i} ({c['kind']}, {where}) from @{c['author']}\n\n{c['body']}\n"
            )
        return "\n".join(lines) if lines else "_(no comments)_\n"

    def address_reviews(
        self,
        owner: str,
        repo: str,
        number: int,
        branch: str,
        default_branch: str,
        updates: list[Update],
        git: Git,
        session: PiSession,
        comments: list[dict],
    ) -> str:
        if not comments:
            return "no comments"
        prompt = load_prompt(
            "review",
            UPDATES=render_update_list(updates),
            COMMENTS=self.format_comments(comments),
        )
        result = session.prompt(prompt)
        self.state.log(f"review: {result.text[:400]}", repo=f"{owner}/{repo}")
        if not git.changed_files():
            return "model made no changes"
        git.commit("chore(deps): address review feedback\n\nGenerated by deps-bot.")
        git.push(branch)
        return result.text[:500]

    # -------------------------------------------------------- auto-merge
    def may_auto_merge(self, owner: str, repo: str, number: int, tier: str) -> tuple[bool, str]:
        """Is this PR safe to merge without a human? Decision #2: tier A only."""
        if tier not in self.cfg.auto_merge_tiers:
            return False, f"tier {tier} is not auto-mergeable"
        view = self.gh.pr_view(owner, repo, number)
        if view.get("isDraft"):
            return False, "PR is a draft"
        if view.get("mergeable") != "MERGEABLE":
            return False, f"mergeable={view.get('mergeable')}"
        if view.get("mergeStateStatus") in ("BLOCKED", "DIRTY"):
            return False, f"mergeStateStatus={view.get('mergeStateStatus')}"
        if self.cfg.require_checks:
            checks = self.gh.pr_checks(owner, repo, number)
            if not checks:
                return False, "no checks reported"
            if any(c.get("bucket") not in ("pass", "skipping") for c in checks):
                return False, "some checks are not passing"
        if (
            self.cfg.require_no_review_requests
            and view.get("reviewDecision") == "CHANGES_REQUESTED"
        ):
            return False, "changes requested"
        if self.cfg.require_no_unresolved_threads:
            threads = self.gh.pr_review_threads(owner, repo, number)
            if any(not t.get("isResolved") for t in threads):
                return False, "unresolved review threads"

        return True, "all auto-merge conditions met"

    # ------------------------------------------------------------ monitor
    def sweep_reminders(self, review_after_h: int = 24, merge_after_h: int = 48) -> list[str]:
        """Email about PRs the human has not dealt with.

        - a PR nobody has reviewed after `review_after_h`
        - a green, mergeable PR nobody has merged after `merge_after_h`

        Returns the list of reminder kinds that were sent.
        """
        sent: list[str] = []
        for row in self.state.open_prs():
            repo = row["repo"]
            owner, _, name = repo.partition("/")
            number = row_get(row, "number")
            branch = row_get(row, "branch")
            tier = row_get(row, "tier") or TIER_A
            if not number or not branch:
                continue
            try:
                view = self.gh.pr_view(owner, name, number)
            except Exception as exc:  # noqa: BLE001
                self.log(f"reminder: view failed for {repo}#{number}: {exc}", repo=repo)
                continue
            if view.get("state") != "OPEN":
                continue
            url = view.get("url") or ""

            created = view.get("createdAt") or ""
            age_h = _age_hours(created)
            review_decision = view.get("reviewDecision")

            checks = self.gh.pr_checks(owner, name, number)
            buckets = [c.get("bucket") for c in checks]
            ci_green = bool(checks) and all(b in ("pass", "skipping") for b in buckets)
            ci_failed = any(b in ("fail", "cancel") for b in buckets)
            mergeable = view.get("mergeable") == "MERGEABLE"

            # 1. CI is red and the bot could not fix it -> needs a human now.
            if ci_failed:
                if (
                    self.notify(
                        alerts.ci_failed(
                            repo,
                            number,
                            url,
                            tier,
                            "CI is failing on this pull request and the bot could not fix it.",
                        )
                    )
                    == "sent"
                ):
                    sent.append("ci_failed")
                continue

            # 2. Nobody has reviewed it yet.
            if not review_decision and age_h >= review_after_h:
                if (
                    self.notify(alerts.reminder_review(repo, number, url, tier, int(age_h)))
                    == "sent"
                ):
                    sent.append("reminder_review")
                continue

            # 3. It is green and mergeable but still open.
            if (
                ci_green
                and mergeable
                and age_h >= merge_after_h
                and self.notify(alerts.reminder_merge(repo, number, url, tier, int(age_h)))
                == "sent"
            ):
                sent.append("reminder_merge")
        return sent

    def monitor_pr(self, repo_row, dry_run: bool | None = None) -> str:
        """One monitoring pass over an open PR: CI, reviews, mergeability.

        Returns a short status string.
        """
        dry_run = self.cfg.dry_run if dry_run is None else dry_run
        repo = repo_row["repo"]
        owner, _, name = repo.partition("/")
        number = row_get(repo_row, "number")
        branch = row_get(repo_row, "branch")
        tier = row_get(repo_row, "tier") or TIER_A
        if not number or not branch:
            return "missing PR metadata"

        view = self.gh.pr_view(owner, name, number)
        pr_state = (view.get("state") or "").upper()
        if pr_state != "OPEN":
            new_state = "merged" if pr_state == "MERGED" else "closed"
            self.state.record_pr(repo, number, state=new_state, updated_at=time.time())
            self.state.log(f"PR is {pr_state.lower()}", repo=repo)
            if new_state == "merged" and not row_get(repo_row, "auto_merged"):
                self.notify(alerts.auto_merged(repo, number, view.get("url") or "", tier))
            return f"PR is {pr_state.lower()}"

        checks = self.gh.pr_checks(owner, name, number)
        buckets = [c.get("bucket") for c in checks]
        ci_state = "none"
        if any(b in ("fail", "cancel") for b in buckets):
            ci_state = "fail"
        elif any(b == "pending" for b in buckets):
            ci_state = "pending"
        elif checks:
            ci_state = "pass"
        self.state.record_pr(
            repo,
            number,
            mergeable=view.get("mergeable"),
            review_decision=view.get("reviewDecision"),
            checks_state=ci_state,
            updated_at=time.time(),
            last_checked=time.time(),
        )

        comments = self.collect_review_comments(owner, name, number)
        known = self.state.known_comment_ids(repo, number)
        fresh = [c for c in comments if str(c.get("id")) not in known]
        if not fresh and ci_state != "fail":
            return f"ci={ci_state} no new comments"

        default_branch = self.gh.default_branch(owner, name)
        git = self.prepare_clone(owner, name, default_branch)
        # The deps branch only exists on the remote - the clone is shallow and
        # fetched the default branch only.
        if not git.fetch_branch(branch):
            return f"branch {branch} not found on the remote"
        git.create_branch(branch, start_point=f"origin/{branch}")

        model = self.cfg.model_for("fixci" if ci_state == "fail" else "review")
        spec = PiModel(
            name=f"depsbot-{model.provider}",
            base_url=model.base_url,
            model=model.model,
            context=model.context,
            max_tokens=model.max_tokens,
            reasoning=model.reasoning,
        )
        updates = self._updates_for_pr(repo, tier)
        session = PiSession(
            cwd=git.path,
            model=spec,
            agent_dir=self.cfg.agent_dir,
            allow_network=self.cfg.allow_network,
            scrub_env=self.cfg.scrub_env,
        )
        try:
            session.start()
            if ci_state == "fail":
                note = self.fix_ci(
                    owner, name, number, branch, default_branch, updates, git, session
                )
                if dry_run:
                    return f"ci=fail (dry run) {note[:120]}"
                new_state = self.watch_ci(owner, name, number, timeout_s=1800)
                self.state.record_pr(repo, number, checks_state=new_state, updated_at=time.time())
                return f"ci was fail, now {new_state}"
            if comments:
                note = self.address_reviews(
                    owner, name, number, branch, default_branch, updates, git, session, fresh
                )
                for c in fresh:
                    self.state.mark_comment_processed(repo, number, str(c.get("id")), note[:200])
                self._resolve_threads(owner, name, number, fresh)
                return f"addressed {len(fresh)} comment(s): {note[:160]}"
            return f"ci={ci_state}"
        finally:
            session.close()

    def _resolve_threads(self, owner: str, repo: str, number: int, comments: list[dict]) -> None:
        """Mark the review threads we acted on as resolved."""
        try:
            threads = self.gh.pr_review_threads(owner, repo, number)
        except Exception:
            return
        if not threads:
            return
        bodies = [c.get("body", "") for c in comments]
        for thread in threads:
            if thread.get("isResolved"):
                continue
            thread_comments = thread.get("comments", {}).get("nodes", []) or []
            for node in thread_comments:
                body = (node.get("body") or "").strip()
                if any(body and body[:120] == b[:120] for b in bodies):
                    try:
                        self.gh.resolve_review_thread(owner, repo, thread["id"])
                        self.state.log(
                            f"resolved review thread {thread['id'][:8]}", repo=f"{owner}/{repo}"
                        )
                    except Exception:
                        pass
                    break

    def _updates_for_pr(self, repo: str, tier: str) -> list[Update]:
        """Rebuild Update records for a PR from the persisted state."""
        rows = self.state.conn.execute("SELECT * FROM deps WHERE repo=?", (repo,)).fetchall()
        out = []
        for r in rows:
            out.append(
                Update(
                    repo=repo,
                    manifest=r["manifest"],
                    ecosystem=r["ecosystem"],
                    name=r["name"],
                    section=r["section"] or "",
                    kind=r["dep_kind"] or "",
                    current=r["current"],
                    latest=r["latest"] or "",
                    bump_kind=r["bump_kind"] or "patch",
                    constraint=r["constraint_text"],
                    action=r["action"] or "manifest",
                    installed=r["installed"],
                )
            )
        return out


def row_get(row, key, default=None):
    """sqlite3.Row accessor that tolerates missing keys."""
    try:
        value = row[key]
    except (IndexError, KeyError):
        return default
    return value if value is not None else default


def _age_hours(created_at: str) -> float:
    """Age of an ISO-8601 timestamp in hours (0 if unparseable)."""
    if not created_at:
        return 0.0
    try:
        from datetime import datetime

        stamp = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        return max(0.0, (datetime.now(UTC) - stamp).total_seconds() / 3600)
    except (ValueError, TypeError):
        return 0.0


def _make_ref_id() -> str:
    """A short opaque id that ties a published PR to its local record."""
    import secrets

    return secrets.token_hex(4)
