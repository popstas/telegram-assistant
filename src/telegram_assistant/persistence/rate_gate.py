"""RateGateStore — cross-process pacing state for rate-limited Telegram calls.

Telegram punishes bursts: a handful of `pin` calls fired back to back answers
with `FLOOD_WAIT` instead of pinning. Pacing therefore has to be *shared* —
the HTTP server, an MCP tool call and a CLI one-shot process all talk to the
same account, so an in-memory timestamp in one process would not stop the next
one from bursting.

This store keeps one row per paced resource (`key` → `next_allowed_at`, an
epoch). :meth:`reserve` is the whole contract: inside a single write
transaction it reads the gate, computes how long the caller must wait, and
pushes the gate forward by the minimum interval — so two concurrent processes
reserving at once get *different* slots rather than the same one.

Storage stays dumb: it never sleeps and knows nothing about FLOOD_WAIT budgets.
The pacer in :mod:`telegram_assistant.messages.pacing` owns that policy.
"""

from __future__ import annotations

import contextlib
import sqlite3
import threading
from collections.abc import Iterator
from pathlib import Path

from telegram_assistant.persistence.schema import bootstrap, connect


class RateGateStore:
    """SQLite-backed slot reservation keyed by an opaque pacing key."""

    def __init__(self, database_path: Path) -> None:
        self._database_path = Path(database_path)
        # Belt-and-suspenders in-process lock, mirroring OperationStore: SQLite
        # handles cross-process safety, this keeps test threads from racing.
        self._lock = threading.RLock()
        bootstrap(self._database_path)

    @contextlib.contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = connect(self._database_path)
        try:
            yield conn
        finally:
            conn.close()

    def reserve(self, key: str, min_interval_seconds: float, now: float) -> float:
        """Claim the next slot for ``key`` and return the seconds to wait.

        The returned delay is ``next_allowed_at - now`` (``0.0`` when the gate
        is already open). The gate is then advanced to
        ``max(now, next_allowed_at) + min_interval_seconds`` so the *following*
        caller — in this or any other process — waits its own full interval.
        """
        interval = max(float(min_interval_seconds), 0.0)
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT next_allowed_at FROM rate_gate WHERE key = ?", (key,)
            ).fetchone()
            next_allowed_at = float(row["next_allowed_at"]) if row is not None else 0.0
            wait = max(next_allowed_at - float(now), 0.0)
            conn.execute(
                """
                INSERT INTO rate_gate (key, next_allowed_at) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET next_allowed_at = excluded.next_allowed_at
                """,
                (key, max(float(now), next_allowed_at) + interval),
            )
            conn.execute("COMMIT")
        return wait

    def block_until(self, key: str, next_allowed_at: float) -> None:
        """Push the gate for ``key`` out to ``next_allowed_at`` (never back).

        Called after a FLOOD_WAIT so every other process backs off too, not
        just the one that happened to trip the limit.
        """
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                INSERT INTO rate_gate (key, next_allowed_at) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    next_allowed_at = MAX(rate_gate.next_allowed_at, excluded.next_allowed_at)
                """,
                (key, float(next_allowed_at)),
            )
            conn.execute("COMMIT")

    def peek(self, key: str) -> float | None:
        """Return the stored ``next_allowed_at`` for ``key`` (``None`` if unset)."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT next_allowed_at FROM rate_gate WHERE key = ?", (key,)
            ).fetchone()
        return None if row is None else float(row["next_allowed_at"])

    def clear(self) -> None:
        """Drop every gate row (used by tests and operator resets)."""
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("DELETE FROM rate_gate")
            conn.execute("COMMIT")


__all__ = ["RateGateStore"]
