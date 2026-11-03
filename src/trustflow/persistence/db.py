"""SQLite plumbing for M3 persistence (docs/m3.md). Stdlib only.

One database file holds runs, ledger and audit tables. WAL mode plus
``busy_timeout`` lets separate processes (API + workers/tests) share
the file safely for the single-instance demo.
"""

import sqlite3
from pathlib import Path

_MIGRATIONS: dict[int, list[str]] = {
    1: [
        """CREATE TABLE IF NOT EXISTS runs (
            run_id TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            version INTEGER NOT NULL,
            run_json TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )""",
        """CREATE TABLE IF NOT EXISTS ledger_accounts (
            account_id TEXT PRIMARY KEY,
            owner TEXT NOT NULL,
            balance REAL NOT NULL,
            currency TEXT NOT NULL DEFAULT 'USD'
        )""",
        """CREATE TABLE IF NOT EXISTS ledger_meta (
            key TEXT PRIMARY KEY,
            value INTEGER NOT NULL
        )""",
        """CREATE TABLE IF NOT EXISTS ledger_transactions (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            tx_id TEXT NOT NULL UNIQUE,
            idempotency_key TEXT NOT NULL UNIQUE,
            from_account TEXT NOT NULL,
            to_account TEXT NOT NULL,
            amount REAL NOT NULL,
            currency TEXT NOT NULL DEFAULT 'USD'
        )""",
        """CREATE TABLE IF NOT EXISTS audit_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL,
            ts TEXT NOT NULL,
            actor TEXT NOT NULL,
            action TEXT NOT NULL,
            payload_hash TEXT NOT NULL,
            detail_json TEXT NOT NULL
        )""",
        "CREATE INDEX IF NOT EXISTS idx_audit_run ON audit_events(run_id, id)",
        "CREATE INDEX IF NOT EXISTS idx_tx_account"
        " ON ledger_transactions(from_account, to_account)",
    ],
}


def connect(db_path: str) -> sqlite3.Connection:
    """Open (creating parent dirs) and migrate a TrustFlow database."""
    path = Path(db_path)
    if str(path) != ":memory:" and path.parent and str(path.parent) not in ("", "."):
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30.0, check_same_thread=False,
                           isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    migrate(conn)
    return conn


def migrate(conn: sqlite3.Connection) -> list[int]:
    """Apply pending migrations idempotently; return applied versions."""
    conn.execute(
        """CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            applied_at TEXT NOT NULL DEFAULT (datetime('now'))
        )"""
    )
    done = {row[0] for row in conn.execute("SELECT version FROM schema_migrations")}
    applied: list[int] = []
    for version in sorted(_MIGRATIONS):
        if version in done:
            continue
        conn.execute("BEGIN IMMEDIATE")
        try:
            for statement in _MIGRATIONS[version]:
                conn.execute(statement)
            conn.execute(
                "INSERT INTO schema_migrations(version) VALUES (?)", (version,))
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        applied.append(version)
    return applied
