"""Markdown digest of a deps-bot run."""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from . import config as config_mod
from .state import State


def build(state: State) -> str:
    now = time.strftime("%Y-%m-%d %H:%M")
    lines = [f"# deps-bot report - {now}", ""]

    rows = state.conn.execute(
        "SELECT repo, COUNT(*) n, "
        "SUM(CASE WHEN bump_kind='major' THEN 1 ELSE 0 END) majors, "
        "SUM(CASE WHEN bump_kind='minor' THEN 1 ELSE 0 END) minors, "
        "SUM(CASE WHEN bump_kind='patch' THEN 1 ELSE 0 END) patches "
        "FROM deps WHERE bump_kind IS NOT NULL GROUP BY repo "
        "ORDER BY (majors*10 + n) DESC"
    ).fetchall()
    total = sum(r["n"] for r in rows)
    lines += [f"**{total} outdated dependencies** across {len(rows)} repositories.", ""]

    if rows:
        lines += ["| Repository | Outdated | major | minor | patch |", "|---|---:|---:|---:|---:|"]
        for r in rows:
            lines.append(
                f"| `{r['repo']}` | {r['n']} | {r['majors'] or 0} | "
                f"{r['minors'] or 0} | {r['patches'] or 0} |"
            )
        lines.append("")

    prs = state.open_prs()
    lines += ["## Open pull requests", ""]
    if prs:
        lines += ["| PR | Tier | Title | CI | Mergeable |", "|---|---|---|---|---|"]
        for p in prs:
            lines.append(
                f"| [{p['repo']}#{p['number']}]({p['url']}) | {p['tier']} | "
                f"{p['title']} | {p['checks_state'] or '?'} | {p['mergeable'] or '?'} |"
            )
    else:
        lines.append("_none_")
    lines.append("")

    merged = list(
        state.conn.execute(
            "SELECT repo, number, title, url FROM prs WHERE auto_merged=1 "
            "ORDER BY updated_at DESC LIMIT 20"
        )
    )
    if merged:
        lines += ["## Auto-merged", ""]
        for p in merged:
            lines.append(f"- [{p['repo']}#{p['number']}]({p['url']}) - {p['title']}")
        lines.append("")

    failures = list(
        state.conn.execute(
            "SELECT repo, stage, status, detail FROM attempts "
            "WHERE status IN ('failed','skipped') AND started_at > ? "
            "ORDER BY id DESC LIMIT 20",
            (time.time() - 86400,),
        )
    )
    if failures:
        lines += ["## Needs attention (last 24h)", ""]
        for f in failures:
            detail = (f["detail"] or "").splitlines()[0][:300]
            lines.append(f"- `{f['repo']}` ({f['stage']}, {f['status']}): {detail}")
        lines.append("")

    scope = list(
        state.conn.execute(
            "SELECT owner, name, scope_reason FROM repos WHERE in_scope=0 "
            "AND scope_reason NOT LIKE 'archived%' ORDER BY name"
        )
    )
    if scope:
        lines += ["## Excluded", ""]
        for s in scope:
            lines.append(f"- `{s['owner']}/{s['name']}` - {s['scope_reason']}")
        lines.append("")

    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Write a markdown digest of a deps-bot run.")
    parser.add_argument("--config", default=None)
    parser.add_argument("--db", default=None)
    parser.add_argument("--out", default=None)
    args = parser.parse_args(argv)

    cfg = config_mod.load(args.config)
    if args.db:
        cfg.state_db = Path(args.db)
    state = State(cfg.state_db)
    try:
        text = build(state)
    finally:
        state.close()
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text)
        latest = cfg.report_dir / "latest.md"
        latest.parent.mkdir(parents=True, exist_ok=True)
        latest.write_text(text)
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
