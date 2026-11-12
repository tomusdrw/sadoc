#!/usr/bin/env python3
"""Command line interface for sadoc.

Usage:
    sadoc scan                 discover repos, detect outdated dependencies
    sadoc scope                show which repos are in scope and why
    sadoc run [--repo NAME]    run the pipeline (bump -> verify -> PR)
    sadoc monitor              watch existing PRs: CI + reviews + mergeability
    sadoc remind               email about pull requests left unattended
    sadoc report               print a summary of the current state
    sadoc status               show the queue and open pull requests
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

if __package__ in (None, ""):  # running as a script
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sadoc import alerts as alerts_mod  # noqa: E402
from sadoc import config as config_mod  # noqa: E402
from sadoc import detect, ecosystems, pipeline, release_check  # noqa: E402
from sadoc.config import ModelSpec  # noqa: E402
from sadoc.gh import Gh, GhError  # noqa: E402
from sadoc.notify import Notifier, NotifyError, build_backend  # noqa: E402
from sadoc.state import State  # noqa: E402


def _log(msg: str, repo: str | None = None, level: str = "info", data: dict | None = None) -> None:
    prefix = f"[{time.strftime('%H:%M:%S')}]"
    if repo:
        prefix += f" ({repo})"
    if level not in ("info",):
        prefix += f" [{level}]"
    print(f"{prefix} {msg}", flush=True)


def _make_notifier(cfg, state, dry_run: bool):
    """Build the notifier. Returns None when notifications are off."""
    ncfg = cfg.notify
    if not ncfg.get("enabled", False):
        return None
    backend_name = ncfg.get("backend", "none")
    if dry_run and backend_name in ("resend", "smtp"):
        backend_name = "console"
    try:
        backend = build_backend({**ncfg, "backend": backend_name})
    except NotifyError as exc:
        _log(f"notifications disabled: {exc}")
        return None
    to = ncfg.get("to") or []
    if isinstance(to, str):
        to = [to]
    return Notifier(backend, state=state, dry_run=dry_run, to=to, from_addr=ncfg.get("from"))


def name_of(entry: dict) -> str:
    return entry["name"]


def _report_unsupported(worker, gh, owner: str, name: str, repo: str, state, args) -> None:
    """Tell the human when a repository uses an ecosystem Sadoc cannot update."""
    try:
        branch = gh.default_branch(owner, name)
        tree = gh.repo_tree(owner, name, branch)
    except Exception:  # noqa: BLE001
        return
    paths = [e.get("path", "") for e in tree]
    hits = ecosystems.unsupported(paths)
    if not hits:
        return
    supported = [h for h in ecosystems.detect(paths) if h.supported]
    languages = sorted({h.ecosystem.label for h in hits})
    manifests = sorted({h.manifest for h in hits})
    _log(f"  {repo}: {', '.join(languages)} is not supported yet")
    worker.notify(
        alerts_mod.unsupported_ecosystem(
            repo,
            languages,
            manifests,
            ecosystems.describe(hits),
            also_supported=bool(supported),
            persona=worker.persona,
        )
    )


def _model_reachable(base_url: str) -> bool:
    """Is the model endpoint answering?

    A model that is not running makes every repository fail, and one email per
    repository. It is much better to say so once.
    """
    import urllib.error
    import urllib.request

    url = base_url.rstrip("/")
    if not url.endswith("/v1"):
        url += "/v1"
    try:
        with urllib.request.urlopen(f"{url}/models", timeout=10) as resp:
            return resp.status == 200
    except (urllib.error.URLError, OSError, ValueError):
        return False


def _check_models(cfg) -> bool:
    """Every model the run might use must be reachable."""
    seen: dict[str, ModelSpec] = {}
    for task in ("bump", "migrate", "prepare", "fixci", "review"):
        model = cfg.model_for(task)
        seen[model.base_url] = model
    for base_url, model in seen.items():
        if not _model_reachable(base_url):
            _log(f"the model {model.model} at {base_url} is not answering")
            _log("start it, or point the config at one that is running")
            return False
    return True


# ------------------------------------------------------------------- scan
def cmd_scan(args, cfg, gh, state) -> int:
    _log("resolving scope ...")
    scoped = detect.resolve_scope(cfg, gh, state)
    _log(f"{len(scoped)} repos in scope")
    worker = pipeline.Worker(
        cfg, gh, state, log=_log, notifier=_make_notifier(cfg, state, cfg.dry_run)
    )

    total = 0
    for entry in scoped:
        owner, name = entry["owner"], name_of(entry)
        repo = entry["repo"]
        # Say so when a repository uses something Sadoc cannot update, rather
        # than passing over it in silence.
        _report_unsupported(worker, gh, owner, name, repo, state, args)
        try:
            branch = gh.default_branch(owner, name)
            updates = pipeline.Worker(cfg, gh, state, log=_log).detect_repo(owner, name, branch)
        except GhError as exc:
            if gh.is_unusable_repository(exc):
                # An empty repository, or one that has been deleted since it
                # was listed, is not a problem - there is nothing to look after.
                _log(f"  {repo}: nothing to look after, skipping")
                state.log("nothing to look after, skipped", repo=repo)
                continue
            _log(f"  {repo}: scan failed: {exc}")
            state.log(f"scan failed: {exc}", repo=repo, level="error")
            worker.notify(alerts_mod.failure(repo, "detection", str(exc)))
            continue
        seen = set()
        for u in updates:
            state.record_dep(repo, u.as_dict())
            seen.add((u.manifest, u.name))
        state.clear_missing_deps(repo, seen)
        state.conn.execute(
            "UPDATE repos SET last_scan_at=? WHERE owner=? AND name=?", (time.time(), owner, name)
        )
        state.conn.commit()
        total += len(updates)
        marker = ""
        if updates:
            kinds = {}
            for u in updates:
                kinds[u.bump_kind] = kinds.get(u.bump_kind, 0) + 1
            marker = "  " + ", ".join(f"{v} {k}" for k, v in sorted(kinds.items()))
        _log(f"  {repo}: {len(updates)} outdated{marker}")
    _log(f"total outdated dependencies: {total}")
    return 0


# ------------------------------------------------------------------ scope
def cmd_scope(args, cfg, gh, state) -> int:
    detect.resolve_scope(cfg, gh, state)
    rows = state.conn.execute(
        "SELECT owner,name,is_fork,is_archived,in_scope,scope_reason,pushed_at "
        "FROM repos ORDER BY in_scope DESC, name"
    ).fetchall()
    in_scope = [r for r in rows if r["in_scope"]]
    out = [r for r in rows if not r["in_scope"]]
    _log(f"in scope: {len(in_scope)}   excluded: {len(out)}")
    for r in in_scope:
        _log(f"  IN  {r['owner']}/{r['name']}  (pushed {r['pushed_at'][:10]})")
    if args.verbose:
        for r in out:
            _log(f"  OUT {r['owner']}/{r['name']}  - {r['scope_reason']}")
    return 0


# -------------------------------------------------------------------- run
def cmd_run(args, cfg, gh, state) -> int:
    worker = pipeline.Worker(
        cfg, gh, state, log=_log, notifier=_make_notifier(cfg, state, cfg.dry_run)
    )
    scoped = detect.resolve_scope(cfg, gh, state)
    if args.repo:
        scoped = [s for s in scoped if s["name"] == args.repo]
        if not scoped:
            _log(f"repo '{args.repo}' is not in scope")
            return 1

    # Rank: security first, then how much is outdated, then staleness.
    candidates = []
    for entry in scoped:
        try:
            updates = worker.detect_repo(
                entry["owner"], entry["name"], gh.default_branch(entry["owner"], entry["name"])
            )
        except Exception as exc:  # noqa: BLE001
            detail = f"{type(exc).__name__}: {exc}"
            if gh.is_unusable_repository(exc):
                _log(f"{entry['repo']}: nothing to look after, skipping")
                state.log("nothing to look after, skipped", repo=entry["repo"])
                continue
            _log(f"{entry['repo']}: scan failed: {detail}")
            state.log(f"scan failed: {detail}", repo=entry["repo"], level="error")
            worker.notify(alerts_mod.failure(entry["repo"], "detection", detail))
            continue
        if not updates:
            continue
        groups = worker.build_groups(updates)
        priority = sum(
            len(g.updates)
            * (10 if g.tier == pipeline.TIER_C else 3 if g.tier == pipeline.TIER_B else 1)
            for g in groups
        )
        candidates.append((priority, entry, groups))
    candidates.sort(key=lambda x: -x[0])
    _log(f"{len(candidates)} repos have outdated dependencies")

    if not _check_models(cfg):
        _log("not starting: the model is not answering")
        return 1

    budget = args.max_prs if args.max_prs is not None else cfg.max_prs_per_night
    deadline = getattr(args, "deadline", None)
    opened = 0
    results = []
    for priority, entry, groups in candidates:
        if opened >= budget:
            _log("PR budget reached, stopping")
            break
        if deadline and time.time() >= deadline:
            _log("past the deadline for tonight, stopping")
            break
        repo = entry["repo"]
        _log(f"{repo}: priority {priority}, {len(groups)} group(s)")
        existing = worker.existing_deps_pr(entry["owner"], entry["name"])
        if existing:
            _log(f"{repo}: skipping, PR #{existing.get('number')} is already open")
            continue
        for group in groups:
            if opened >= budget:
                break
            if group.tier == pipeline.TIER_B and not cfg.tier_b_major:
                continue
            if group.tier == pipeline.TIER_C and not cfg.tier_c_security:
                continue
            _log(
                f"--- {repo} tier {group.tier}: {len(group.updates)} updates "
                f"({', '.join(group.manifests[:4])})"
            )
            res = worker.process_repo(
                entry["owner"], entry["name"], group.tier, group.updates, group=group
            )
            results.append(res)
            _log(f"    -> {res.status} {res.detail or res.pr_url}")
            if res.status == "pr_opened":
                opened += 1
    _log(f"opened {opened} PR(s)")
    if args.json:
        print(json.dumps([r.__dict__ for r in results], indent=2, default=str))
    return 0


# ---------------------------------------------------------------- monitor
def cmd_monitor(args, cfg, gh, state) -> int:
    worker = pipeline.Worker(
        cfg, gh, state, log=_log, notifier=_make_notifier(cfg, state, cfg.dry_run)
    )
    prs = state.open_prs()
    if not prs:
        _log("no open PRs tracked")
        return 0
    deadline = getattr(args, "deadline", None)
    for row in prs:
        if deadline and time.time() >= deadline:
            _log("past the deadline, stopping the monitor pass")
            break
        repo = row["repo"]
        owner, _, name = repo.partition("/")
        number = row["number"]
        _log(f"--- {repo}#{number} (tier {row['tier']})")
        try:
            note = worker.monitor_pr(row)
            _log(f"    {note}")
        except Exception as exc:  # noqa: BLE001
            _log(f"    monitor failed: {type(exc).__name__}: {exc}")
            state.log(f"monitor failed: {exc}", repo=repo, level="error")
        if args.json:
            print(json.dumps({"repo": repo, "number": number}, default=str))
    return 0


# ---------------------------------------------------------------- remind
def cmd_remind(args, cfg, gh, state) -> int:
    """Email about pull requests that have been left unattended."""
    worker = pipeline.Worker(
        cfg, gh, state, log=_log, notifier=_make_notifier(cfg, state, cfg.dry_run)
    )
    if not worker.notifier:
        _log("notifications are not enabled - see [notify] in the config")
        return 1
    review_h = int(cfg.notify.get("review_reminder_hours", 24))
    merge_h = int(cfg.notify.get("merge_reminder_hours", 48))
    _log(f"sweeping reminders (review after {review_h}h, merge after {merge_h}h)")
    sent = worker.sweep_reminders(review_after_h=review_h, merge_after_h=merge_h)
    if sent:
        _log(f"sent {len(sent)} reminder(s): {', '.join(sent)}")
    else:
        _log("nothing to remind about")
    return 0


# ----------------------------------------------------------------- report
def cmd_report(args, cfg, gh, state) -> int:
    from sadoc import report as report_mod

    text = report_mod.build(state, note=getattr(args, "note", "") or "")
    if getattr(args, "out", None):
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text)
        latest = cfg.report_dir / "latest.md"
        latest.parent.mkdir(parents=True, exist_ok=True)
        latest.write_text(text)
        _log(f"report written to {out}")
    else:
        print(text)
    return 0


# ---------------------------------------------------------------- doctor
def cmd_doctor(args, cfg, gh, state) -> int:
    """Look at the default branch of every repository, and fix what is broken."""
    worker = pipeline.Worker(
        cfg, gh, state, log=_log, notifier=_make_notifier(cfg, state, cfg.dry_run)
    )
    scoped = detect.resolve_scope(cfg, gh, state)
    if args.repo:
        scoped = [s for s in scoped if s["name"] == args.repo]
        if not scoped:
            _log(f"repo '{args.repo}' is not in scope")
            return 1
    broken = 0
    for entry in scoped:
        result = worker.doctor_repo(entry["owner"], entry["name"])
        if result["state"] in ("fixable", "transient", "agent_failed"):
            broken += 1
            _log(f"  {entry['repo']}: {result['state']} - {result['detail']}")
            if result.get("pr"):
                _log(f"    {result['pr']}")
        elif result["state"] == "already_open":
            _log(f"  {entry['repo']}: a fix is already open ({result['pr']})")
        if args.json:
            print(json.dumps(result, default=str))
    _log(f"{broken} repository branch(es) need attention")
    return 0


# -------------------------------------------------------------- releases
def cmd_releases(args, cfg, gh, state) -> int:
    """Report which repositories owe a release."""
    worker = pipeline.Worker(
        cfg, gh, state, log=_log, notifier=_make_notifier(cfg, state, cfg.dry_run)
    )
    scoped = detect.resolve_scope(cfg, gh, state)
    if args.repo:
        scoped = [s for s in scoped if s["name"] == args.repo]
    due = 0
    for entry in scoped:
        owner, name = entry["owner"], entry["name"]
        try:
            branch = gh.default_branch(owner, name)
            status = release_check.check(gh, owner, name, branch)
        except Exception as exc:  # noqa: BLE001
            _log(f"  {owner}/{name}: could not check - {exc}")
            continue
        if status.due:
            due += 1
            _log(f"  {owner}/{name}: a release is due - {status.summary()}")
            _log(f"    {status.note}")
            if args.notify:
                worker.notify(
                    alerts_mod.release_due(
                        f"{owner}/{name}",
                        status.packages[0][1] if status.packages else "",
                        status.published,
                        status.kinds,
                        status.note,
                        persona=worker.persona,
                    )
                )
        elif args.verbose:
            _log(f"  {owner}/{name}: {status.summary()} ({status.kinds or 'no release process'})")
        if args.json:
            print(
                json.dumps(
                    {
                        "repo": f"{owner}/{name}",
                        "due": status.due,
                        "version": status.version,
                        "published": status.published,
                        "kinds": status.kinds,
                        "note": status.note,
                    },
                    default=str,
                )
            )
    _log(f"{due} repository/ies owe a release")
    return 0


# ------------------------------------------------------------- selfupdate
def cmd_selfupdate(args, cfg, gh, state) -> int:
    """Bring Sadoc up to date from the latest main branch.

    Exit codes: 0 - already up to date, 10 - updated, 1 - could not update.
    """
    from sadoc import selfupdate

    root = Path(__file__).resolve().parent.parent
    result = selfupdate.update(root, branch=args.branch)
    _log(result.message)
    if not result.ok:
        return 1
    return 10 if result.changed else 0


# ----------------------------------------------------------------- status
def cmd_status(args, cfg, gh, state) -> int:
    rows = state.conn.execute(
        "SELECT repo, COUNT(*) n FROM deps WHERE bump_kind IS NOT NULL "
        "GROUP BY repo ORDER BY n DESC"
    ).fetchall()
    _log("outdated dependencies per repo:")
    for r in rows:
        _log(f"  {r['repo']}: {r['n']}")
    prs = state.open_prs()
    _log(f"open PRs: {len(prs)}")
    for p in prs:
        _log(f"  {p['repo']}#{p['number']} [{p['tier']}] {p['title']} -> {p['url']}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="sadoc",
        description="Sadoc - a nightly dependency update agent for your own GitHub repositories.",
        epilog="Run 'sadoc <command> --help' for command specific options.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    parser.add_argument("--config", default=None)
    parser.add_argument("--db", default=None)
    parser.add_argument("--dry-run", action="store_true", help="never push or open PRs")
    sub = parser.add_subparsers(dest="command", required=True)

    p_scan = sub.add_parser("scan", help="detect outdated dependencies")
    p_scan.set_defaults(func=cmd_scan)

    p_scope = sub.add_parser("scope", help="show scope decisions")
    p_scope.add_argument("--verbose", action="store_true")
    p_scope.set_defaults(func=cmd_scope)

    p_run = sub.add_parser("run", help="run the pipeline")
    p_run.add_argument("--repo", default=None)
    p_run.add_argument("--max-prs", type=int, default=None)
    p_run.add_argument(
        "--deadline",
        type=float,
        default=None,
        help="epoch seconds after which no new repository is started",
    )
    p_run.add_argument("--json", action="store_true")
    p_run.set_defaults(func=cmd_run)

    p_mon = sub.add_parser("monitor", help="watch open PRs")
    p_mon.add_argument("--json", action="store_true")
    p_mon.add_argument(
        "--deadline",
        type=float,
        default=None,
        help="epoch seconds after which no further PR is examined",
    )
    p_mon.set_defaults(func=cmd_monitor)

    p_rem = sub.add_parser("remind", help="email about unattended PRs")
    p_rem.add_argument("--json", action="store_true")
    p_rem.set_defaults(func=cmd_remind)

    p_doc = sub.add_parser("doctor", help="check the default branch and fix what is broken")
    p_doc.add_argument("--repo", default=None)
    p_doc.add_argument("--json", action="store_true")
    p_doc.set_defaults(func=cmd_doctor)

    p_rel = sub.add_parser("releases", help="report which repositories owe a release")
    p_rel.add_argument("--repo", default=None)
    p_rel.add_argument("--notify", action="store_true", help="send the nudge by email")
    p_rel.add_argument("--verbose", action="store_true")
    p_rel.add_argument("--json", action="store_true")
    p_rel.set_defaults(func=cmd_releases)

    p_up = sub.add_parser("selfupdate", help="update Sadoc from the latest main branch")
    p_up.add_argument("--branch", default="main")
    p_up.set_defaults(func=cmd_selfupdate)

    p_rep = sub.add_parser("report", help="write a markdown digest")
    p_rep.add_argument("--out", default=None)
    p_rep.add_argument("--note", default="", help="a line to put at the top of the digest")
    p_rep.set_defaults(func=cmd_report)

    sub.add_parser("status", help="queue and PR status").set_defaults(func=cmd_status)

    args = parser.parse_args()

    try:
        cfg = config_mod.load(args.config)
    except config_mod.ConfigError as exc:
        _log(f"the configuration is wrong: {exc}")
        return 1
    if args.db:
        cfg.state_db = Path(args.db)
    if args.dry_run:
        cfg.dry_run = True

    cfg.state_db.parent.mkdir(parents=True, exist_ok=True)
    cfg.workdir.mkdir(parents=True, exist_ok=True)
    cfg.report_dir.mkdir(parents=True, exist_ok=True)

    try:
        gh = Gh()
    except GhError as exc:
        _log(str(exc))
        return 1
    state = State(cfg.state_db)
    try:
        return args.func(args, cfg, gh, state)
    finally:
        state.close()


if __name__ == "__main__":
    sys.exit(main())
