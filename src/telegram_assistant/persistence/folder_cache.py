"""FolderMembershipCache — persistent single-row cache of the folder map.

Folder-scoped access rules need to know which chats belong to which folders.
Building that map means a Telegram round-trip (``GetDialogFiltersRequest``); the
CLI is one process per call, so without persistence every gated command pays for
the fetch again. This store persists the map (``{folder_name: [chat_id, ...]}``)
plus the epoch it was fetched at, so a fresh process can reuse a still-fresh map.

One Telegram account per deployment ⇒ a single row is enough (the schema CHECK
keeps ``id = 1``). The store only reports the stored map and its age; TTL policy
and stale-fallback decisions live in the authorizer so storage stays dumb.
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
import threading
from collections.abc import Iterator
from pathlib import Path

from telegram_assistant.persistence.schema import bootstrap, connect

# The map shape the authorizer works with: folder title -> bare chat ids.
MembershipMap = dict[str, set[int]]


class FolderMembershipCache:
    """Thin SQLite-backed store for the folder-membership map."""

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

    def load(self) -> tuple[MembershipMap, float] | None:
        """Return the cached ``(map, fetched_at)`` or ``None`` when empty."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT payload, fetched_at FROM folder_membership_cache WHERE id = 1"
            ).fetchone()
        if row is None:
            return None
        payload = json.loads(row["payload"])
        mapping: MembershipMap = {
            str(folder): {int(cid) for cid in chat_ids}
            for folder, chat_ids in payload.items()
        }
        return mapping, float(row["fetched_at"])

    def save(self, mapping: MembershipMap, fetched_at: float) -> None:
        """Persist ``mapping`` and the epoch it was fetched at (upsert row 1)."""
        payload = json.dumps(
            {folder: sorted(chat_ids) for folder, chat_ids in mapping.items()},
            sort_keys=True,
        )
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                INSERT INTO folder_membership_cache (id, payload, fetched_at)
                VALUES (1, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    payload = excluded.payload,
                    fetched_at = excluded.fetched_at
                """,
                (payload, float(fetched_at)),
            )
            conn.execute("COMMIT")

    def clear(self) -> None:
        """Drop the cached row so the next load misses (a no-op when empty)."""
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("DELETE FROM folder_membership_cache WHERE id = 1")
            conn.execute("COMMIT")
