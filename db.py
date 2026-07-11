"""
db.py — SQLite dedupe + history so you never see the same item twice.

One table, `seen`, keyed by a stable per-item id (arXiv id, HN id, or RSS guid).
The DB file is committed back to the repo after each run (see the GitHub Action),
so state persists across runs without any external database.
"""

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

DB_PATH = "digest.db"


@contextmanager
def _conn(path: str = DB_PATH):
    con = sqlite3.connect(path)
    try:
        yield con
        con.commit()
    finally:
        con.close()


def init(path: str = DB_PATH) -> None:
    """Create tables if they don't exist. Safe to call every run."""
    with _conn(path) as con:
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS seen (
                id         TEXT PRIMARY KEY,
                source     TEXT NOT NULL,
                title      TEXT,
                first_seen TEXT NOT NULL
            )
            """
        )
        # Log of what actually got sent, handy for debugging / "what did I get?"
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS sent (
                id      TEXT,
                run_at  TEXT NOT NULL,
                title   TEXT
            )
            """
        )


def filter_new(items: list[dict], path: str = DB_PATH) -> list[dict]:
    """Return only items whose id has never been seen before."""
    if not items:
        return []
    with _conn(path) as con:
        known = {row[0] for row in con.execute("SELECT id FROM seen")}
    return [it for it in items if it["id"] not in known]


def mark_seen(items: list[dict], path: str = DB_PATH) -> None:
    """Record items as seen so future runs skip them."""
    if not items:
        return
    now = datetime.now(timezone.utc).isoformat()
    rows = [(it["id"], it["source"], it.get("title", ""), now) for it in items]
    with _conn(path) as con:
        con.executemany(
            "INSERT OR IGNORE INTO seen (id, source, title, first_seen) "
            "VALUES (?, ?, ?, ?)",
            rows,
        )


def log_sent(items: list[dict], path: str = DB_PATH) -> None:
    """Record the items that were included in a digest (audit trail)."""
    if not items:
        return
    now = datetime.now(timezone.utc).isoformat()
    rows = [(it["id"], now, it.get("title", "")) for it in items]
    with _conn(path) as con:
        con.executemany(
            "INSERT INTO sent (id, run_at, title) VALUES (?, ?, ?)", rows
        )
