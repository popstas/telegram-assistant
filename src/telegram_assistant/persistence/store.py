"""OperationStore — state machine + replay backed by SQLite.

State machine: pending -> completed | failed | needs_review.

Replay semantics (driven entirely by the existing row's status):
  completed     -> caller returns the saved result_payload
  failed        -> caller returns the saved error
  needs_review  -> caller returns the saved error; never auto-retried
  pending       -> caller may wait_for_completion() up to a timeout, then act

The store itself only reports state — wait timing and 503 emission live in the
HTTP layer so policy stays out of storage.
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
import threading
import time
import uuid
from collections.abc import Iterable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from telegram_assistant.persistence.models import (
    BeginResult,
    OperationItemRecord,
    OperationRecord,
    OperationStatus,
)
from telegram_assistant.persistence.schema import bootstrap, connect


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _dumps(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, default=str)


def _loads(value: str | None) -> dict[str, Any] | None:
    if value is None:
        return None
    return json.loads(value)


class IdempotencyConflictError(RuntimeError):
    """Same idempotency key reused for a different operation type."""


class OperationNotFoundError(LookupError):
    """No row matches the given operation_id."""


class OperationStore:
    """Thin wrapper around the SQLite persistence schema."""

    def __init__(self, database_path: Path) -> None:
        self._database_path = Path(database_path)
        # A coarse process-wide lock keeps the state-machine transitions
        # atomic. SQLite-level locking handles cross-process safety; this is
        # an in-process belt to keep test threads from racing each other.
        self._lock = threading.RLock()
        bootstrap(self._database_path)

    # ------------------------------------------------------------------
    # connection helpers
    # ------------------------------------------------------------------

    @contextlib.contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        # sqlite3.Connection.__exit__ commits/rolls back but does not close the
        # connection, so naive `with sqlite3.connect(...) as conn:` leaks the
        # underlying file descriptor until the GC finalises it. We manage the
        # transaction explicitly elsewhere and only need to ensure the
        # connection is closed when the caller exits the block.
        conn = connect(self._database_path)
        try:
            yield conn
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # row -> record mapping
    # ------------------------------------------------------------------

    @staticmethod
    def _row_to_operation(row: sqlite3.Row) -> OperationRecord:
        return OperationRecord(
            id=row["id"],
            type=row["type"],
            status=OperationStatus(row["status"]),
            idempotency_key=row["idempotency_key"],
            request_payload=json.loads(row["request_payload"]),
            result_payload=_loads(row["result_payload"]),
            error=row["error"],
            created_at=_parse_iso(row["created_at"]),
            updated_at=_parse_iso(row["updated_at"]),
        )

    @staticmethod
    def _row_to_item(row: sqlite3.Row) -> OperationItemRecord:
        return OperationItemRecord(
            id=row["id"],
            operation_id=row["operation_id"],
            idempotency_key=row["idempotency_key"],
            status=OperationStatus(row["status"]),
            request_payload=json.loads(row["request_payload"]),
            result_payload=_loads(row["result_payload"]),
            error=row["error"],
            created_at=_parse_iso(row["created_at"]),
            updated_at=_parse_iso(row["updated_at"]),
        )

    # ------------------------------------------------------------------
    # public API — operations
    # ------------------------------------------------------------------

    def begin_operation(
        self,
        *,
        operation_type: str,
        idempotency_key: str,
        request_payload: dict[str, Any],
    ) -> BeginResult:
        """Insert a new `pending` row, or replay the existing one.

        The (idempotency_key) column is UNIQUE; on collision we read back the
        existing row instead of failing. If the existing row's `type` differs
        from `operation_type` we surface that as an `IdempotencyConflictError`
        rather than silently mixing semantics.
        """
        if not idempotency_key:
            raise ValueError("idempotency_key must be non-empty")

        now = _utcnow()
        op_id = uuid.uuid4().hex
        payload_blob = _dumps(request_payload)

        with self._lock, self._connect() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    """
                    INSERT INTO operations
                        (id, type, status, idempotency_key,
                         request_payload, result_payload, error,
                         created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, NULL, NULL, ?, ?)
                    """,
                    (
                        op_id,
                        operation_type,
                        OperationStatus.PENDING.value,
                        idempotency_key,
                        payload_blob,
                        _iso(now),
                        _iso(now),
                    ),
                )
                conn.execute(
                    "INSERT INTO idempotency_index(key, operation_id) VALUES (?, ?)",
                    (idempotency_key, op_id),
                )
                conn.execute("COMMIT")
                created = True
            except sqlite3.IntegrityError:
                conn.execute("ROLLBACK")
                created = False

            row = conn.execute(
                "SELECT * FROM operations WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()

        if row is None:
            # Extremely unlikely — would mean the insert succeeded but the
            # readback failed. Surface clearly rather than returning None.
            raise RuntimeError("operation lookup failed immediately after insert")

        operation = self._row_to_operation(row)
        if not created and operation.type != operation_type:
            raise IdempotencyConflictError(
                f"idempotency key {idempotency_key!r} already bound to "
                f"operation type {operation.type!r}, not {operation_type!r}"
            )

        items = self.list_items(operation.id) if not created else []
        return BeginResult(operation=operation, created=created, items=items)

    def get_operation(self, operation_id: str) -> OperationRecord:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM operations WHERE id = ?", (operation_id,)
            ).fetchone()
        if row is None:
            raise OperationNotFoundError(operation_id)
        return self._row_to_operation(row)

    def find_by_idempotency_key(self, idempotency_key: str) -> OperationRecord | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM operations WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
        if row is None:
            return None
        return self._row_to_operation(row)

    def _transition(
        self,
        operation_id: str,
        new_status: OperationStatus,
        *,
        result: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> OperationRecord:
        now = _utcnow()
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT status FROM operations WHERE id = ?", (operation_id,)
            ).fetchone()
            if row is None:
                conn.execute("ROLLBACK")
                raise OperationNotFoundError(operation_id)
            current = OperationStatus(row["status"])
            if current != OperationStatus.PENDING and current != new_status:
                # Terminal states are sticky. Returning the existing record
                # keeps the call idempotent rather than silently rewriting
                # outcomes — particularly important for needs_review.
                conn.execute("ROLLBACK")
                existing = conn.execute(
                    "SELECT * FROM operations WHERE id = ?", (operation_id,)
                ).fetchone()
                return self._row_to_operation(existing)

            conn.execute(
                """
                UPDATE operations
                   SET status = ?,
                       result_payload = ?,
                       error = ?,
                       updated_at = ?
                 WHERE id = ?
                """,
                (
                    new_status.value,
                    _dumps(result) if result is not None else None,
                    error,
                    _iso(now),
                    operation_id,
                ),
            )
            conn.execute("COMMIT")
            updated = conn.execute(
                "SELECT * FROM operations WHERE id = ?", (operation_id,)
            ).fetchone()
        return self._row_to_operation(updated)

    def complete_operation(
        self, operation_id: str, result: dict[str, Any]
    ) -> OperationRecord:
        return self._transition(
            operation_id, OperationStatus.COMPLETED, result=result
        )

    def fail_operation(self, operation_id: str, error: str) -> OperationRecord:
        return self._transition(operation_id, OperationStatus.FAILED, error=error)

    def mark_needs_review(self, operation_id: str, error: str) -> OperationRecord:
        return self._transition(
            operation_id, OperationStatus.NEEDS_REVIEW, error=error
        )

    def delete_operation(self, operation_id: str) -> None:
        """Remove an operation row, its idempotency index entry, and items.

        Used to drop a stale ``completed`` group_create whose Telegram chat was
        deleted out-of-band, so a fresh create can claim the same idempotency
        key. ``foreign_keys=ON`` requires children (``operation_items``) to go
        before the parent. A missing row is a no-op.
        """
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "DELETE FROM operation_items WHERE operation_id = ?",
                (operation_id,),
            )
            conn.execute(
                "DELETE FROM idempotency_index WHERE operation_id = ?",
                (operation_id,),
            )
            conn.execute(
                "DELETE FROM operations WHERE id = ?",
                (operation_id,),
            )
            conn.execute("COMMIT")

    def wait_for_completion(
        self,
        operation_id: str,
        *,
        timeout_seconds: float,
        poll_interval_seconds: float = 0.05,
    ) -> OperationRecord:
        """Block until the operation leaves `pending` or the timeout elapses.

        Returns the current record regardless of final status. The HTTP layer
        is responsible for turning a still-pending result into a 503.
        """
        deadline = time.monotonic() + max(timeout_seconds, 0.0)
        while True:
            op = self.get_operation(operation_id)
            if op.status != OperationStatus.PENDING:
                return op
            if time.monotonic() >= deadline:
                return op
            time.sleep(poll_interval_seconds)

    # ------------------------------------------------------------------
    # public API — items (bulk operations)
    # ------------------------------------------------------------------

    def begin_item(
        self,
        *,
        operation_id: str,
        idempotency_key: str,
        request_payload: dict[str, Any],
    ) -> tuple[OperationItemRecord, bool]:
        """Insert a new pending item, or return the existing one for replay."""
        now = _utcnow()
        item_id = uuid.uuid4().hex
        with self._lock, self._connect() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    """
                    INSERT INTO operation_items
                        (id, operation_id, idempotency_key, status,
                         request_payload, result_payload, error,
                         created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, NULL, NULL, ?, ?)
                    """,
                    (
                        item_id,
                        operation_id,
                        idempotency_key,
                        OperationStatus.PENDING.value,
                        _dumps(request_payload),
                        _iso(now),
                        _iso(now),
                    ),
                )
                conn.execute("COMMIT")
                created = True
            except sqlite3.IntegrityError:
                conn.execute("ROLLBACK")
                created = False
            row = conn.execute(
                """
                SELECT * FROM operation_items
                 WHERE operation_id = ? AND idempotency_key = ?
                """,
                (operation_id, idempotency_key),
            ).fetchone()
        if row is None:
            raise RuntimeError("operation item lookup failed immediately after insert")
        return self._row_to_item(row), created

    def list_items(self, operation_id: str) -> list[OperationItemRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM operation_items WHERE operation_id = ? ORDER BY created_at",
                (operation_id,),
            ).fetchall()
        return [self._row_to_item(r) for r in rows]

    def iter_pending_items(self, operation_id: str) -> Iterable[OperationItemRecord]:
        """Yield items still in pending — used by the worker to resume on restart."""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM operation_items
                 WHERE operation_id = ? AND status = ?
                 ORDER BY created_at
                """,
                (operation_id, OperationStatus.PENDING.value),
            ).fetchall()
        return [self._row_to_item(r) for r in rows]

    def _transition_item(
        self,
        item_id: str,
        new_status: OperationStatus,
        *,
        result: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> OperationItemRecord:
        now = _utcnow()
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT status FROM operation_items WHERE id = ?", (item_id,)
            ).fetchone()
            if row is None:
                conn.execute("ROLLBACK")
                raise OperationNotFoundError(item_id)
            current = OperationStatus(row["status"])
            if current != OperationStatus.PENDING and current != new_status:
                conn.execute("ROLLBACK")
                existing = conn.execute(
                    "SELECT * FROM operation_items WHERE id = ?", (item_id,)
                ).fetchone()
                return self._row_to_item(existing)

            conn.execute(
                """
                UPDATE operation_items
                   SET status = ?,
                       result_payload = ?,
                       error = ?,
                       updated_at = ?
                 WHERE id = ?
                """,
                (
                    new_status.value,
                    _dumps(result) if result is not None else None,
                    error,
                    _iso(now),
                    item_id,
                ),
            )
            conn.execute("COMMIT")
            updated = conn.execute(
                "SELECT * FROM operation_items WHERE id = ?", (item_id,)
            ).fetchone()
        return self._row_to_item(updated)

    def complete_item(self, item_id: str, result: dict[str, Any]) -> OperationItemRecord:
        return self._transition_item(item_id, OperationStatus.COMPLETED, result=result)

    def fail_item(self, item_id: str, error: str) -> OperationItemRecord:
        return self._transition_item(item_id, OperationStatus.FAILED, error=error)

    def mark_item_needs_review(self, item_id: str, error: str) -> OperationItemRecord:
        return self._transition_item(
            item_id, OperationStatus.NEEDS_REVIEW, error=error
        )

    # ------------------------------------------------------------------
    # public API — retry helpers
    # ------------------------------------------------------------------

    def reset_operation_for_retry(self, operation_id: str) -> OperationRecord:
        """Force a failed/needs_review operation back to pending.

        Refuses to reset a completed operation — retrying a successful call
        would be a footgun, and the idempotency layer already returns the
        saved result on replay. Pending operations are returned as-is.
        """
        now = _utcnow()
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM operations WHERE id = ?", (operation_id,)
            ).fetchone()
            if row is None:
                conn.execute("ROLLBACK")
                raise OperationNotFoundError(operation_id)
            current = OperationStatus(row["status"])
            if current is OperationStatus.COMPLETED:
                conn.execute("ROLLBACK")
                raise ValueError(
                    f"operation {operation_id} is completed; refusing to reset"
                )
            if current is OperationStatus.PENDING:
                conn.execute("ROLLBACK")
                return self._row_to_operation(row)
            conn.execute(
                """
                UPDATE operations
                   SET status = ?,
                       error = NULL,
                       updated_at = ?
                 WHERE id = ?
                """,
                (OperationStatus.PENDING.value, _iso(now), operation_id),
            )
            conn.execute("COMMIT")
            updated = conn.execute(
                "SELECT * FROM operations WHERE id = ?", (operation_id,)
            ).fetchone()
        return self._row_to_operation(updated)

    def reset_items_for_retry(self, operation_id: str) -> list[OperationItemRecord]:
        """Reset every non-completed item under `operation_id` to pending.

        Completed items keep their saved results — that's what makes retry safe
        to invoke on a partially-completed bulk operation.
        """
        now = _utcnow()
        reset_ids: list[str] = []
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                """
                SELECT id FROM operation_items
                 WHERE operation_id = ?
                   AND status IN (?, ?)
                """,
                (
                    operation_id,
                    OperationStatus.FAILED.value,
                    OperationStatus.NEEDS_REVIEW.value,
                ),
            ).fetchall()
            for r in rows:
                conn.execute(
                    """
                    UPDATE operation_items
                       SET status = ?,
                           error = NULL,
                           updated_at = ?
                     WHERE id = ?
                    """,
                    (OperationStatus.PENDING.value, _iso(now), r["id"]),
                )
                reset_ids.append(r["id"])
            conn.execute("COMMIT")
        if not reset_ids:
            return []
        with self._connect() as conn:
            placeholders = ",".join("?" for _ in reset_ids)
            updated = conn.execute(
                f"SELECT * FROM operation_items WHERE id IN ({placeholders})",
                reset_ids,
            ).fetchall()
        return [self._row_to_item(r) for r in updated]

    def items_by_status(
        self, operation_id: str, statuses: Iterable[OperationStatus]
    ) -> list[OperationItemRecord]:
        """Items belonging to `operation_id` whose status is in `statuses`."""
        wanted = [s.value for s in statuses]
        if not wanted:
            return []
        with self._connect() as conn:
            placeholders = ",".join("?" for _ in wanted)
            rows = conn.execute(
                f"""
                SELECT * FROM operation_items
                 WHERE operation_id = ?
                   AND status IN ({placeholders})
                 ORDER BY created_at
                """,
                (operation_id, *wanted),
            ).fetchall()
        return [self._row_to_item(r) for r in rows]
