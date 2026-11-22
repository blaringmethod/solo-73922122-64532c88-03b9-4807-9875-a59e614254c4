"""SQLite schema and connection management.

A single monotonic ``state_id`` identifies every committed state. Any
successful boundary replacement or household-count report opens a write
transaction and bumps the state; a failed write is rolled back so the
previously committed data stays intact.
"""
from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from collections.abc import Iterator

DB_PATH = os.environ.get("DB_PATH", "/data/households.db")

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS blocks (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    year          INTEGER NOT NULL,
    block_no      TEXT NOT NULL,
    x1            INTEGER NOT NULL,
    y1            INTEGER NOT NULL,
    x2            INTEGER NOT NULL,
    y2            INTEGER NOT NULL,
    boundary_rev  INTEGER NOT NULL,
    households    INTEGER,
    UNIQUE(year, block_no)
);

CREATE TABLE IF NOT EXISTS state_log (
    state_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    change     TEXT NOT NULL,
    detail     TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Range (geometry-only) comparison cache, reused when only counts change.
CREATE TABLE IF NOT EXISTS geometry_match_cache (
    cache_key   TEXT PRIMARY KEY,
    year_a      INTEGER NOT NULL,
    year_b      INTEGER NOT NULL,
    payload     TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Immutable snapshots returned by each YoY run.
CREATE TABLE IF NOT EXISTS comparisons (
    comparison_id INTEGER PRIMARY KEY AUTOINCREMENT,
    state_id      INTEGER NOT NULL,
    year_a        INTEGER NOT NULL,
    year_b        INTEGER NOT NULL,
    reused_range  INTEGER NOT NULL,
    payload       TEXT NOT NULL,
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


def connect(db_path: str | None = None) -> sqlite3.Connection:
    path = db_path or DB_PATH
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    conn = sqlite3.connect(path, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    if conn.execute("SELECT value FROM meta WHERE key='state_id'").fetchone() is None:
        conn.execute("INSERT INTO meta(key, value) VALUES('state_id', '0')")


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Exclusive write transaction with rollback on any failure."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def current_state_id(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT value FROM meta WHERE key='state_id'").fetchone()
    return int(row["value"])


def bump_state(conn: sqlite3.Connection, change: str, detail: str) -> int:
    conn.execute(
        "INSERT INTO state_log(change, detail) VALUES(?, ?)", (change, detail)
    )
    new_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute("UPDATE meta SET value=? WHERE key='state_id'", (str(new_id),))
    return new_id
