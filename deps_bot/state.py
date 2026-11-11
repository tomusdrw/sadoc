"""Persistent state for deps-bot (SQLite).

Everything the nightly run needs to resume lives here: which repos are in scope,
which dependency updates were detected, which PRs are open, and what happened in
each attempt. A crashed run resumes from this without re-doing work or
double-opening PRs.
"""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

DEFAULT_DB = Path.home() / ".local" / "share" / "deps-bot" / "state.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS repos (
    owner TEXT NOT NULL,
    name  TEXT NOT NULL,
    default_branch TEXT,
    is_fork INTEGER DEFAULT 0,
    is_archived INTEGER DEFAULT 0,
    pushed_at TEXT,
    in_scope INTEGER DEFAULT 0,
    scope_reason TEXT,
    last_scan_at REAL,
    PRIMARY KEY (owner, name)
);

CREATE TABLE IF NOT EXISTS deps (
    repo TEXT NOT NULL,
    manifest TEXT NOT NULL,
    ecosystem TEXT NOT NULL,
    name TEXT NOT NULL,
    section TEXT,
    dep_kind TEXT,
    current TEXT,
    latest TEXT,
    bump_kind TEXT,
    action TEXT DEFAULT 'manifest',
    installed TEXT,
    constraint_text TEXT,
    first_seen REAL,
    last_seen REAL,
    PRIMARY KEY (repo, manifest, name)
);

CREATE TABLE IF NOT EXISTS prs (
    repo TEXT NOT NULL,
    number INTEGER NOT NULL,
    tier TEXT,
    title TEXT,
    url TEXT,
    branch TEXT,
    state TEXT DEFAULT 'open',
    created_at REAL,
    updated_at REAL,
    last_checked REAL,
    mergeable TEXT,
    review_decision TEXT,
    checks_state TEXT,
    auto_merged INTEGER DEFAULT 0,
    ci_attempts INTEGER DEFAULT 0,
    PRIMARY KEY (repo, number)
);

CREATE TABLE IF NOT EXISTS attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    repo TEXT,
    pr_number INTEGER,
    stage TEXT,
    attempt INTEGER,
    started_at REAL,
    ended_at REAL,
    status TEXT,
    detail TEXT
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL,
    repo TEXT,
    level TEXT,
    message TEXT,
    data TEXT
);

CREATE TABLE IF NOT EXISTS pr_comments (
    repo TEXT NOT NULL,
    pr_number INTEGER NOT NULL,
    comment_id TEXT NOT NULL,
    created_at REAL,
    processed_at REAL,
    outcome TEXT,
    PRIMARY KEY (repo, pr_number, comment_id)
);

CREATE TABLE IF NOT EXISTS repo_setup (
    repo TEXT NOT NULL,
    verify_command TEXT,
    prerequisites TEXT,
    notes TEXT,
    status TEXT,
    created_at REAL,
    updated_at REAL,
    PRIMARY KEY (repo)
);

CREATE TABLE IF NOT EXISTS pr_details (
    repo TEXT NOT NULL,
    number INTEGER NOT NULL,
    ref_id TEXT,
    tier TEXT,
    model TEXT,
    base_url TEXT,
    branch TEXT,
    updates TEXT,
    agent_notes TEXT,
    verify_output TEXT,
    transcript TEXT,
    created_at REAL,
    PRIMARY KEY (repo, number)
);

CREATE TABLE IF NOT EXISTS notifications (
    dedupe_key TEXT PRIMARY KEY,
    ts REAL,
    repo TEXT,
    pr_number INTEGER,
    kind TEXT,
    subject TEXT,
    sent_id TEXT,
    ok INTEGER DEFAULT 1
);

CREATE INDEX IF NOT EXISTS idx_deps_repo ON deps(repo);
CREATE INDEX IF NOT EXISTS idx_prs_state ON prs(state);
CREATE INDEX IF NOT EXISTS idx_events_repo ON events(repo, ts);
"""


class State:
    # Columns added after the first release. An existing database is migrated in
    # place, so an upgrade does not lose the history.
    MIGRATIONS = {
        "prs": {
            "ci_attempts": "INTEGER DEFAULT 0",
        },
    }

    def __init__(self, path: str | Path = DEFAULT_DB):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path), timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.commit()

    def _migrate(self) -> None:
        for table, columns in self.MIGRATIONS.items():
            existing = {row["name"] for row in self.conn.execute(f"PRAGMA table_info({table})")}
            for column, definition in columns.items():
                if column not in existing:
                    self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    @contextmanager
    def tx(self):
        try:
            yield self.conn
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    # ------------------------------------------------------------- repos
    def upsert_repo(self, owner: str, name: str, **fields) -> None:
        cols = ["owner", "name"] + list(fields)
        placeholders = ",".join("?" for _ in cols)
        updates = ",".join(f"{c}=excluded.{c}" for c in cols[2:])
        with self.tx() as c:
            c.execute(
                f"INSERT INTO repos ({','.join(cols)}) VALUES ({placeholders}) "
                f"ON CONFLICT(owner,name) DO UPDATE SET {updates}",
                [owner, name] + list(fields.values()),
            )

    def set_scope(self, owner: str, name: str, in_scope: bool, reason: str) -> None:
        with self.tx() as c:
            c.execute(
                "UPDATE repos SET in_scope=?, scope_reason=? WHERE owner=? AND name=?",
                (int(in_scope), reason, owner, name),
            )

    def scoped_repos(self) -> list[sqlite3.Row]:
        return list(self.conn.execute("SELECT * FROM repos WHERE in_scope=1 ORDER BY name"))

    # -------------------------------------------------------------- deps
    def record_dep(self, repo: str, dep: dict) -> None:
        now = time.time()
        with self.tx() as c:
            c.execute(
                "INSERT INTO deps (repo,manifest,ecosystem,name,section,dep_kind,"
                "current,latest,bump_kind,action,installed,constraint_text,first_seen,last_seen) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(repo,manifest,name) DO UPDATE SET "
                "ecosystem=excluded.ecosystem, section=excluded.section,"
                "dep_kind=excluded.dep_kind, current=excluded.current,"
                "latest=excluded.latest, bump_kind=excluded.bump_kind,"
                "action=excluded.action, installed=excluded.installed, "
                "constraint_text=excluded.constraint_text, "
                "last_seen=excluded.last_seen",
                (
                    repo,
                    dep["manifest"],
                    dep["ecosystem"],
                    dep["name"],
                    dep.get("section"),
                    dep.get("kind"),
                    dep.get("current"),
                    dep.get("latest"),
                    dep.get("bump_kind"),
                    dep.get("action", "manifest"),
                    dep.get("installed"),
                    dep.get("constraint"),
                    now,
                    now,
                ),
            )

    def clear_missing_deps(self, repo: str, seen: set[tuple[str, str]]) -> None:
        """Drop deps that no longer exist (they were fixed or removed upstream)."""
        with self.tx() as c:
            rows = c.execute("SELECT manifest,name FROM deps WHERE repo=?", (repo,)).fetchall()
            for r in rows:
                if (r["manifest"], r["name"]) not in seen:
                    c.execute(
                        "DELETE FROM deps WHERE repo=? AND manifest=? AND name=?",
                        (repo, r["manifest"], r["name"]),
                    )

    def outdated_deps(self, repo: str | None = None) -> list[sqlite3.Row]:
        if repo:
            return list(
                self.conn.execute(
                    "SELECT * FROM deps WHERE repo=? AND bump_kind IS NOT NULL "
                    "ORDER BY repo, manifest, name",
                    (repo,),
                )
            )
        return list(
            self.conn.execute(
                "SELECT * FROM deps WHERE bump_kind IS NOT NULL ORDER BY repo, manifest, name"
            )
        )

    # --------------------------------------------------------------- prs
    def record_pr(self, repo: str, number: int, **fields) -> None:
        now = time.time()
        fields.setdefault("created_at", now)
        fields.setdefault("updated_at", now)
        cols = ["repo", "number"] + list(fields)
        placeholders = ",".join("?" for _ in cols)
        updates = ",".join(f"{c}=excluded.{c}" for c in cols[2:])
        with self.tx() as c:
            c.execute(
                f"INSERT INTO prs ({','.join(cols)}) VALUES ({placeholders}) "
                f"ON CONFLICT(repo,number) DO UPDATE SET {updates}",
                [repo, number] + list(fields.values()),
            )

    def open_prs(self, repo: str | None = None) -> list[sqlite3.Row]:
        if repo:
            return list(
                self.conn.execute(
                    "SELECT * FROM prs WHERE repo=? AND state='open' ORDER BY updated_at DESC",
                    (repo,),
                )
            )
        return list(
            self.conn.execute("SELECT * FROM prs WHERE state='open' ORDER BY updated_at DESC")
        )

    def pr_for_branch(self, repo: str, branch: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM prs WHERE repo=? AND branch=? AND state='open'", (repo, branch)
        ).fetchone()

    def bump_ci_attempts(self, repo: str, number: int) -> int:
        """Count how many times the bot has tried to fix a PR's CI."""
        with self.tx() as c:
            c.execute(
                "UPDATE prs SET ci_attempts = COALESCE(ci_attempts, 0) + 1 "
                "WHERE repo=? AND number=?",
                (repo, number),
            )
            row = c.execute(
                "SELECT ci_attempts FROM prs WHERE repo=? AND number=?", (repo, number)
            ).fetchone()
        return row["ci_attempts"] if row else 0

    # ------------------------------------------------------- repo setup
    def record_setup(
        self,
        repo: str,
        verify_command: str | None,
        prerequisites: str = "",
        notes: str = "",
        status: str = "ok",
    ) -> None:
        """Remember how to build a repository, so it only has to be worked out once."""
        now = time.time()
        with self.tx() as c:
            c.execute(
                "INSERT INTO repo_setup (repo,verify_command,prerequisites,notes,status,"
                "created_at,updated_at) VALUES (?,?,?,?,?,?,?) "
                "ON CONFLICT(repo) DO UPDATE SET verify_command=excluded.verify_command,"
                "prerequisites=excluded.prerequisites, notes=excluded.notes,"
                "status=excluded.status, updated_at=excluded.updated_at",
                (repo, verify_command, prerequisites[:1000], notes[:2000], status, now, now),
            )

    def setup(self, repo: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM repo_setup WHERE repo=?", (repo,)).fetchone()

    # ------------------------------------------------------- pr details
    def record_pr_details(self, repo: str, number: int, **fields) -> None:
        """Store everything about a PR that must NOT appear in the PR itself."""
        fields["created_at"] = time.time()
        cols = ["repo", "number"] + list(fields)
        placeholders = ",".join("?" for _ in cols)
        updates = ",".join(f"{c}=excluded.{c}" for c in cols[2:])
        with self.tx() as c:
            c.execute(
                f"INSERT INTO pr_details ({','.join(cols)}) VALUES ({placeholders}) "
                f"ON CONFLICT(repo,number) DO UPDATE SET {updates}",
                [repo, number] + [fields[k] for k in fields],
            )

    def pr_details(self, repo: str, number: int) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM pr_details WHERE repo=? AND number=?", (repo, number)
        ).fetchone()

    def pr_by_ref(self, ref_id: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM pr_details WHERE ref_id=?", (ref_id,)).fetchone()

    # ------------------------------------------------------- notifications
    def notification_sent(self, dedupe_key: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM notifications WHERE dedupe_key=?", (dedupe_key,)
        ).fetchone()
        return row is not None

    def record_notification(
        self,
        dedupe_key: str,
        repo: str | None,
        pr_number: int | None,
        kind: str,
        subject: str,
        sent_id: str = "",
        ok: bool = True,
    ) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT INTO notifications (dedupe_key,ts,repo,pr_number,kind,subject,sent_id,ok) "
                "VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(dedupe_key) DO UPDATE SET "
                "ts=excluded.ts, sent_id=excluded.sent_id, ok=excluded.ok",
                (dedupe_key, time.time(), repo, pr_number, kind, subject[:300], sent_id, int(ok)),
            )

    def recent_notifications(self, limit: int = 50) -> list[sqlite3.Row]:
        return list(
            self.conn.execute("SELECT * FROM notifications ORDER BY ts DESC LIMIT ?", (limit,))
        )

    # ---------------------------------------------------------- attempts
    def unprocessed_comments(self, repo: str, pr_number: int) -> list[sqlite3.Row]:
        """Review comments that have not been handled yet."""
        return list(
            self.conn.execute(
                "SELECT * FROM pr_comments WHERE repo=? AND pr_number=? "
                "AND processed_at IS NULL ORDER BY created_at",
                (repo, pr_number),
            )
        )

    def mark_comment_processed(
        self, repo: str, pr_number: int, comment_id: str, outcome: str = ""
    ) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT INTO pr_comments (repo,pr_number,comment_id,created_at,processed_at,outcome) "
                "VALUES (?,?,?,?,?,?) "
                "ON CONFLICT(repo,pr_number,comment_id) DO UPDATE SET "
                "processed_at=excluded.processed_at, outcome=excluded.outcome",
                (repo, pr_number, str(comment_id), time.time(), time.time(), outcome[:500]),
            )

    def known_comment_ids(self, repo: str, pr_number: int) -> set[str]:
        return {
            r["comment_id"]
            for r in self.conn.execute(
                "SELECT comment_id FROM pr_comments WHERE repo=? AND pr_number=?", (repo, pr_number)
            )
        }

    def start_attempt(
        self, repo: str, stage: str, pr_number: int | None = None, attempt: int = 1
    ) -> int:
        with self.tx() as c:
            cur = c.execute(
                "INSERT INTO attempts (repo,pr_number,stage,attempt,started_at,status) "
                "VALUES (?,?,?,?,?,'running')",
                (repo, pr_number, stage, attempt, time.time()),
            )
            return cur.lastrowid

    def end_attempt(self, attempt_id: int, status: str, detail: str = "") -> None:
        with self.tx() as c:
            c.execute(
                "UPDATE attempts SET ended_at=?, status=?, detail=? WHERE id=?",
                (time.time(), status, detail[:4000], attempt_id),
            )

    # ------------------------------------------------------------ events
    def log(
        self, message: str, repo: str | None = None, level: str = "info", data: dict | None = None
    ) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT INTO events (ts,repo,level,message,data) VALUES (?,?,?,?,?)",
                (time.time(), repo, level, message, json.dumps(data) if data else None),
            )

    def recent_events(self, limit: int = 50, repo: str | None = None) -> list[sqlite3.Row]:
        if repo:
            return list(
                self.conn.execute(
                    "SELECT * FROM events WHERE repo=? ORDER BY ts DESC LIMIT ?", (repo, limit)
                )
            )
        return list(self.conn.execute("SELECT * FROM events ORDER BY ts DESC LIMIT ?", (limit,)))

    def close(self) -> None:
        self.conn.close()
