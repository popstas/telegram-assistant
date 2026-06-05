"""SQLite schema bootstrap.

A single `bootstrap` function is the only public entry point — it is safe to
call repeatedly (CREATE TABLE IF NOT EXISTS) so the worker, HTTP factory, and
tests can all invoke it without coordinating.
"""

from __future__ import annotations

import contextlib
import sqlite3
from pathlib import Path

SCHEMA_VERSION = 1

_OPERATIONS_DDL = """
CREATE TABLE IF NOT EXISTS operations (
    id TEXT PRIMARY KEY,
    type TEXT NOT NULL,
    status TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    request_payload TEXT NOT NULL,
    result_payload TEXT,
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
"""

_OPERATION_ITEMS_DDL = """
CREATE TABLE IF NOT EXISTS operation_items (
    id TEXT PRIMARY KEY,
    operation_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    status TEXT NOT NULL,
    request_payload TEXT NOT NULL,
    result_payload TEXT,
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (operation_id) REFERENCES operations(id),
    UNIQUE (operation_id, idempotency_key)
)
"""

_IDEMPOTENCY_INDEX_DDL = """
CREATE TABLE IF NOT EXISTS idempotency_index (
    key TEXT PRIMARY KEY,
    operation_id TEXT NOT NULL,
    FOREIGN KEY (operation_id) REFERENCES operations(id)
)
"""

_META_DDL = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
)
"""

_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_operations_status ON operations(status)",
    "CREATE INDEX IF NOT EXISTS idx_operations_type ON operations(type)",
    "CREATE INDEX IF NOT EXISTS idx_operation_items_operation ON operation_items(operation_id)",
    "CREATE INDEX IF NOT EXISTS idx_operation_items_status ON operation_items(status)",
]


def connect(database_path: Path) -> sqlite3.Connection:
    """Open a sqlite3 connection with sensible defaults for this service."""
    database_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(
        str(database_path),
        isolation_level=None,
        check_same_thread=False,
        timeout=30.0,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    # 30s busy timeout so CLI runs that briefly contend with the HTTP worker
    # for a write lock wait instead of erroring out with "database is locked".
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def bootstrap(database_path: Path) -> None:
    """Create the persistence schema if it does not already exist."""
    # contextlib.closing ensures the connection is actually closed on exit;
    # sqlite3.Connection's own context manager only manages the transaction.
    with contextlib.closing(connect(database_path)) as conn:
        conn.execute("BEGIN")
        conn.execute(_OPERATIONS_DDL)
        conn.execute(_OPERATION_ITEMS_DDL)
        conn.execute(_IDEMPOTENCY_INDEX_DDL)
        conn.execute(_META_DDL)
        for stmt in _INDEXES:
            conn.execute(stmt)
        conn.execute(
            "INSERT OR REPLACE INTO schema_meta(key, value) VALUES (?, ?)",
            ("schema_version", str(SCHEMA_VERSION)),
        )
        conn.execute("COMMIT")
