"""SQLite persistence and idempotency layer."""

from telegram_assistant.persistence import idempotency
from telegram_assistant.persistence.folder_cache import FolderMembershipCache
from telegram_assistant.persistence.models import (
    TERMINAL_STATUSES,
    BeginResult,
    OperationItemRecord,
    OperationRecord,
    OperationStatus,
)
from telegram_assistant.persistence.schema import (
    SCHEMA_VERSION,
    bootstrap,
    connect,
)
from telegram_assistant.persistence.store import (
    IdempotencyConflictError,
    OperationNotFoundError,
    OperationStore,
)

__all__ = [
    "SCHEMA_VERSION",
    "TERMINAL_STATUSES",
    "BeginResult",
    "FolderMembershipCache",
    "IdempotencyConflictError",
    "OperationItemRecord",
    "OperationNotFoundError",
    "OperationRecord",
    "OperationStatus",
    "OperationStore",
    "bootstrap",
    "connect",
    "idempotency",
]
