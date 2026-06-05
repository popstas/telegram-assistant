"""Dataclass records returned by the persistence layer.

These are decoupled from SQLite row tuples so callers (HTTP handlers, CLI,
worker) never have to know the storage format.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any


class OperationStatus(StrEnum):
    PENDING = "pending"
    COMPLETED = "completed"
    FAILED = "failed"
    NEEDS_REVIEW = "needs_review"


TERMINAL_STATUSES: frozenset[OperationStatus] = frozenset(
    {
        OperationStatus.COMPLETED,
        OperationStatus.FAILED,
        OperationStatus.NEEDS_REVIEW,
    }
)


@dataclass(frozen=True)
class OperationRecord:
    """Top-level operation state. Mirrors the `operations` table."""

    id: str
    type: str
    status: OperationStatus
    idempotency_key: str
    request_payload: dict[str, Any]
    result_payload: dict[str, Any] | None
    error: str | None
    created_at: datetime
    updated_at: datetime

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["status"] = self.status.value
        d["created_at"] = self.created_at.isoformat()
        d["updated_at"] = self.updated_at.isoformat()
        return d


@dataclass(frozen=True)
class OperationItemRecord:
    """Per-item state for bulk operations. Mirrors the `operation_items` table."""

    id: str
    operation_id: str
    idempotency_key: str
    status: OperationStatus
    request_payload: dict[str, Any]
    result_payload: dict[str, Any] | None
    error: str | None
    created_at: datetime
    updated_at: datetime

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["status"] = self.status.value
        d["created_at"] = self.created_at.isoformat()
        d["updated_at"] = self.updated_at.isoformat()
        return d


@dataclass(frozen=True)
class BeginResult:
    """Outcome of `OperationStore.begin_operation`.

    `created` distinguishes a fresh insert from a replay against an existing
    record. The replay path is the whole point of the idempotency layer, so
    callers need a clean signal.
    """

    operation: OperationRecord
    created: bool
    items: list[OperationItemRecord] = field(default_factory=list)
