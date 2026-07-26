"""FolderMembershipCache — persistent single-row cache of the folder map.

Folder-scoped access rules need to know which chats belong to which folders.
Building that map means a Telegram round-trip (``GetDialogFiltersRequest``); the
CLI is one process per call, so without persistence every gated command pays for
the fetch again. This store persists the map plus the epoch it was fetched at,
so a fresh process can reuse a still-fresh map.

The map is keyed by the folder's stable ``folder_id``, not its title: Telegram
allows two folders to share a title, and a title-keyed map would keep only one
of them — wrongly denying access to the chats of the shadowed folder. The title
is stored alongside so name-targeted access rules can union same-named folders.

The stored JSON carries a ``version`` marker (:data:`PAYLOAD_VERSION`); a row
written by an older version (the title-keyed shape) is reported as a cache
**miss** rather than parsed, so an upgrade quietly refetches instead of raising.

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

# The map shape the authorizer works with: folder id -> (folder title, bare
# chat ids). Deliberately plain builtins — the store stays free of domain
# imports (``telegram_assistant.folders`` reaches back into the persistence
# package through the worker, so importing it here would risk an import cycle).
MembershipMap = dict[int, tuple[str, set[int]]]

# Bumped whenever the stored JSON shape changes; older rows load as a miss.
PAYLOAD_VERSION = 2


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
        """Return the cached ``(map, fetched_at)``, or ``None`` on a miss.

        A miss is an empty table *or* a row written in an older payload shape —
        the caller then refetches, which is exactly what an upgraded deployment
        should do.
        """
        with self._connect() as conn:
            row = conn.execute(
                "SELECT payload, fetched_at FROM folder_membership_cache WHERE id = 1"
            ).fetchone()
        if row is None:
            return None
        try:
            payload = json.loads(row["payload"])
        except ValueError:
            return None
        if not isinstance(payload, dict) or payload.get("version") != PAYLOAD_VERSION:
            return None
        mapping: MembershipMap = {}
        for entry in payload.get("folders", []):
            mapping[int(entry["id"])] = (
                str(entry["name"]),
                {int(cid) for cid in entry.get("chat_ids", [])},
            )
        return mapping, float(row["fetched_at"])

    def save(self, mapping: MembershipMap, fetched_at: float) -> None:
        """Persist ``mapping`` and the epoch it was fetched at (upsert row 1)."""
        payload = json.dumps(
            {
                "version": PAYLOAD_VERSION,
                "folders": [
                    {
                        "id": folder_id,
                        "name": folder_name,
                        "chat_ids": sorted(chat_ids),
                    }
                    for folder_id, (folder_name, chat_ids) in sorted(mapping.items())
                ],
            },
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
