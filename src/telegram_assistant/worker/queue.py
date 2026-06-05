"""Async worker queue with bounded parallelism and FLOOD_WAIT handling.

The queue is the single place where Telegram operations get rate-limited and
where bulk operations persist per-item progress. HTTP and CLI surfaces submit
handlers here; the queue takes care of:

  - bounding the number of concurrent Telegram calls
    (`queue.max_parallel_telegram_ops`)
  - pausing on `FLOOD_WAIT` for the requested seconds + a safety margin instead
    of failing the whole operation
  - persisting per-item state for bulk runs so a process restart resumes from
    the last incomplete item rather than redoing completed work
  - leaving `needs_review` outcomes untouched on resume — those are quarantined
    until an operator runs `operations retry`
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any

from telegram_assistant.persistence.models import (
    OperationItemRecord,
    OperationStatus,
)
from telegram_assistant.persistence.store import OperationStore


class FloodWaitError(RuntimeError):
    """Raised by a handler when Telegram returns FLOOD_WAIT.

    The queue catches this, sleeps for `seconds + safety_margin`, and retries
    the same handler invocation. A handler should raise this only when the
    upstream Telethon call surfaced a `FloodWaitError`; other exceptions are
    treated as terminal.
    """

    def __init__(self, seconds: float) -> None:
        super().__init__(f"FLOOD_WAIT {seconds}s")
        self.seconds = float(seconds)


class NeedsReviewError(RuntimeError):
    """Raised by a handler when the outcome is undefined and must not be retried."""


@dataclass(frozen=True)
class BulkItemSpec:
    """Input to a bulk handler — the queue persists `payload` per item."""

    idempotency_key: str
    payload: dict[str, Any]


OperationHandler = Callable[[], Awaitable[dict[str, Any]]]
ItemHandler = Callable[[BulkItemSpec], Awaitable[dict[str, Any]]]
SleepFn = Callable[[float], Awaitable[None]]


class WorkerQueue:
    """Bounded-parallelism async queue for Telegram operations."""

    def __init__(
        self,
        store: OperationStore,
        *,
        max_parallel: int = 1,
        flood_wait_safety_margin_seconds: float = 5.0,
        default_retry_delay_seconds: float = 30.0,
        max_flood_wait_retries: int = 6,
        sleep: SleepFn | None = None,
    ) -> None:
        if max_parallel < 1:
            raise ValueError("max_parallel must be >= 1")
        if max_flood_wait_retries < 1:
            raise ValueError("max_flood_wait_retries must be >= 1")
        self._store = store
        self._semaphore = asyncio.Semaphore(max_parallel)
        self._fw_margin = float(flood_wait_safety_margin_seconds)
        self._retry_delay = float(default_retry_delay_seconds)
        self._max_fw_retries = int(max_flood_wait_retries)
        self._sleep: SleepFn = sleep if sleep is not None else asyncio.sleep

    @property
    def store(self) -> OperationStore:
        return self._store

    async def run_operation(
        self,
        operation_id: str,
        handler: OperationHandler,
    ) -> OperationItemRecord | Any:
        """Run a single-shot operation under the rate limit.

        Returns the final OperationRecord. FLOOD_WAIT triggers a pause-and-retry;
        any other exception transitions the operation to `failed`. A
        `NeedsReviewError` marks it `needs_review` (no auto-retry). After
        ``max_flood_wait_retries`` consecutive FLOOD_WAIT pauses, the operation
        is moved to `needs_review` so it does not pin the request forever.
        """
        fw_attempts = 0
        while True:
            async with self._semaphore:
                try:
                    result = await handler()
                except FloodWaitError as fw:
                    # Release the semaphore so other queued operations can
                    # proceed while this one waits out the FLOOD_WAIT window.
                    pause = max(fw.seconds, 0.0) + self._fw_margin
                    fw_attempts += 1
                    if fw_attempts >= self._max_fw_retries:
                        return self._store.mark_needs_review(
                            operation_id,
                            (
                                f"FLOOD_WAIT exceeded retry budget "
                                f"({self._max_fw_retries} attempts, last "
                                f"pause={fw.seconds:.0f}s)"
                            ),
                        )
                except NeedsReviewError as exc:
                    return self._store.mark_needs_review(operation_id, str(exc))
                except Exception as exc:
                    return self._store.fail_operation(operation_id, str(exc))
                else:
                    return self._store.complete_operation(operation_id, result)
            await self._sleep(pause)

    async def run_bulk(
        self,
        operation_id: str,
        items: Sequence[BulkItemSpec],
        item_handler: ItemHandler,
        *,
        stop_on_failure: bool = False,
    ) -> list[OperationItemRecord]:
        """Register and run each item, persisting progress for restart-safety.

        Items already in a terminal state (completed/failed/needs_review) are
        not re-run — they're returned as-is. Only `pending` items execute. The
        same applies on restart: calling `run_bulk` again with the same items
        will pick up exactly where the previous run left off because
        `begin_item` is idempotent on `(operation_id, idempotency_key)`.

        When ``stop_on_failure`` is true, the loop breaks the first time an
        item ends in a non-completed terminal state (``failed`` or
        ``needs_review``). Items not yet started are left untouched so a later
        resume can finish them.
        """
        results: list[OperationItemRecord] = []
        for spec in items:
            item, _ = self._store.begin_item(
                operation_id=operation_id,
                idempotency_key=spec.idempotency_key,
                request_payload=spec.payload,
            )
            if item.status is not OperationStatus.PENDING:
                results.append(item)
                if stop_on_failure and item.status is not OperationStatus.COMPLETED:
                    break
                continue
            final = await self._run_item(item.id, spec, item_handler)
            results.append(final)
            if stop_on_failure and final.status is not OperationStatus.COMPLETED:
                break
        return results

    async def _run_item(
        self,
        item_id: str,
        spec: BulkItemSpec,
        handler: ItemHandler,
    ) -> OperationItemRecord:
        fw_attempts = 0
        while True:
            async with self._semaphore:
                try:
                    result = await handler(spec)
                except FloodWaitError as fw:
                    # Release the semaphore so concurrent items can keep
                    # progressing while this one sleeps off the FLOOD_WAIT.
                    pause = max(fw.seconds, 0.0) + self._fw_margin
                    fw_attempts += 1
                    if fw_attempts >= self._max_fw_retries:
                        return self._store.mark_item_needs_review(
                            item_id,
                            (
                                f"FLOOD_WAIT exceeded retry budget "
                                f"({self._max_fw_retries} attempts, last "
                                f"pause={fw.seconds:.0f}s)"
                            ),
                        )
                except NeedsReviewError as exc:
                    return self._store.mark_item_needs_review(item_id, str(exc))
                except Exception as exc:
                    return self._store.fail_item(item_id, str(exc))
                else:
                    return self._store.complete_item(item_id, result)
            await self._sleep(pause)

    async def resume_bulk(
        self,
        operation_id: str,
        item_handler: ItemHandler,
    ) -> list[OperationItemRecord]:
        """Resume a bulk operation from its persisted pending items.

        Reads pending items from storage (so the caller doesn't need to retain
        the original `items` list across restarts) and runs each through the
        same FLOOD_WAIT-aware path as `run_bulk`.
        """
        results: list[OperationItemRecord] = []
        for item in list(self._store.iter_pending_items(operation_id)):
            spec = BulkItemSpec(
                idempotency_key=item.idempotency_key,
                payload=item.request_payload,
            )
            final = await self._run_item(item.id, spec, item_handler)
            results.append(final)
        return results
