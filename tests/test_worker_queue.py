"""Tests for the worker queue (Task 5).

Covers:
  - FLOOD_WAIT retry with safety margin
  - bounded parallelism
  - restart-resumption of bulk operations
  - failure / needs_review transitions
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from telegram_assistant.persistence import (
    OperationStatus,
    OperationStore,
    idempotency,
)
from telegram_assistant.worker import (
    BulkItemSpec,
    FloodWaitError,
    NeedsReviewError,
    WorkerQueue,
)


@pytest.fixture()
def store(tmp_path: Path) -> OperationStore:
    return OperationStore(tmp_path / "state.db")


def _make_queue(
    store: OperationStore,
    *,
    max_parallel: int = 1,
    flood_margin: float = 0.0,
    sleeps: list[float] | None = None,
    max_flood_wait_retries: int = 6,
) -> WorkerQueue:
    """Build a queue with a recording, no-real-sleep sleep function."""
    record = sleeps if sleeps is not None else []

    async def fake_sleep(seconds: float) -> None:
        record.append(seconds)

    return WorkerQueue(
        store,
        max_parallel=max_parallel,
        flood_wait_safety_margin_seconds=flood_margin,
        max_flood_wait_retries=max_flood_wait_retries,
        sleep=fake_sleep,
    )


# ---------------------------------------------------------------------------
# single-operation runs
# ---------------------------------------------------------------------------


async def test_run_operation_completes_and_persists_result(store: OperationStore) -> None:
    op = store.begin_operation(
        operation_type="x", idempotency_key="k1", request_payload={}
    )
    queue = _make_queue(store)

    async def handler() -> dict[str, object]:
        return {"ok": True, "chat_id": -100}

    record = await queue.run_operation(op.operation.id, handler)
    assert record.status is OperationStatus.COMPLETED
    assert record.result_payload == {"ok": True, "chat_id": -100}


async def test_run_operation_failure_marks_failed(store: OperationStore) -> None:
    op = store.begin_operation(
        operation_type="x", idempotency_key="k-fail", request_payload={}
    )
    queue = _make_queue(store)

    async def handler() -> dict[str, object]:
        raise RuntimeError("user not found")

    record = await queue.run_operation(op.operation.id, handler)
    assert record.status is OperationStatus.FAILED
    assert record.error == "user not found"


async def test_run_operation_needs_review_does_not_auto_retry(
    store: OperationStore,
) -> None:
    op = store.begin_operation(
        operation_type="x", idempotency_key="k-needs", request_payload={}
    )
    queue = _make_queue(store)
    calls = 0

    async def handler() -> dict[str, object]:
        nonlocal calls
        calls += 1
        raise NeedsReviewError("undefined outcome")

    record = await queue.run_operation(op.operation.id, handler)
    assert record.status is OperationStatus.NEEDS_REVIEW
    assert record.error == "undefined outcome"
    assert calls == 1


async def test_run_operation_flood_wait_retries_are_bounded(
    store: OperationStore,
) -> None:
    op = store.begin_operation(
        operation_type="x", idempotency_key="k-fw-bound", request_payload={}
    )
    queue = _make_queue(store, flood_margin=0.0, max_flood_wait_retries=3)

    calls = 0

    async def handler() -> dict[str, object]:
        nonlocal calls
        calls += 1
        raise FloodWaitError(2.0)

    record = await queue.run_operation(op.operation.id, handler)
    assert record.status is OperationStatus.NEEDS_REVIEW
    assert calls == 3
    assert "FLOOD_WAIT exceeded retry budget" in (record.error or "")


async def test_run_operation_pauses_on_flood_wait_then_succeeds(
    store: OperationStore,
) -> None:
    op = store.begin_operation(
        operation_type="x", idempotency_key="k-fw", request_payload={}
    )
    sleeps: list[float] = []
    queue = _make_queue(store, flood_margin=2.0, sleeps=sleeps)
    calls = 0

    async def handler() -> dict[str, object]:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise FloodWaitError(7.0)
        return {"ok": True}

    record = await queue.run_operation(op.operation.id, handler)
    assert record.status is OperationStatus.COMPLETED
    assert calls == 2
    # 7s requested + 2s margin
    assert sleeps == [9.0]


# ---------------------------------------------------------------------------
# bulk runs
# ---------------------------------------------------------------------------


def _bulk_parent(store: OperationStore, key: str) -> str:
    parent = store.begin_operation(
        operation_type=idempotency.TOPIC_BULK_CREATE,
        idempotency_key=key,
        request_payload={},
    )
    return parent.operation.id


async def test_run_bulk_persists_per_item_results(store: OperationStore) -> None:
    op_id = _bulk_parent(store, "topic_bulk_create:b1")
    queue = _make_queue(store)
    items = [
        BulkItemSpec(idempotency_key="a", payload={"i": 1}),
        BulkItemSpec(idempotency_key="b", payload={"i": 2}),
    ]

    async def handler(spec: BulkItemSpec) -> dict[str, object]:
        return {"echo": spec.payload["i"]}

    results = await queue.run_bulk(op_id, items, handler)
    assert [r.status for r in results] == [
        OperationStatus.COMPLETED,
        OperationStatus.COMPLETED,
    ]
    assert [r.result_payload for r in results] == [{"echo": 1}, {"echo": 2}]


async def test_run_bulk_continues_after_per_item_failure(
    store: OperationStore,
) -> None:
    op_id = _bulk_parent(store, "topic_bulk_create:b-fail")
    queue = _make_queue(store)
    items = [
        BulkItemSpec(idempotency_key="ok", payload={"i": 1}),
        BulkItemSpec(idempotency_key="bad", payload={"i": 2}),
        BulkItemSpec(idempotency_key="review", payload={"i": 3}),
    ]

    async def handler(spec: BulkItemSpec) -> dict[str, object]:
        if spec.idempotency_key == "bad":
            raise RuntimeError("privacy restricted")
        if spec.idempotency_key == "review":
            raise NeedsReviewError("timeout")
        return {"ok": True}

    results = await queue.run_bulk(op_id, items, handler)
    statuses = [r.status for r in results]
    assert statuses == [
        OperationStatus.COMPLETED,
        OperationStatus.FAILED,
        OperationStatus.NEEDS_REVIEW,
    ]
    assert results[1].error == "privacy restricted"
    assert results[2].error == "timeout"


async def test_run_bulk_skips_already_completed_items(store: OperationStore) -> None:
    """Re-running run_bulk after a partial completion must not re-execute the
    already-completed items — that's the contract that makes restart safe."""
    op_id = _bulk_parent(store, "topic_bulk_create:resume")
    queue = _make_queue(store)
    items = [
        BulkItemSpec(idempotency_key="done", payload={"i": 1}),
        BulkItemSpec(idempotency_key="pending", payload={"i": 2}),
    ]
    # Pre-complete the first item by hand to simulate a previous partial run.
    pre, _ = store.begin_item(
        operation_id=op_id,
        idempotency_key="done",
        request_payload={"i": 1},
    )
    store.complete_item(pre.id, {"prefilled": True})

    invocations: list[str] = []

    async def handler(spec: BulkItemSpec) -> dict[str, object]:
        invocations.append(spec.idempotency_key)
        return {"fresh": True}

    results = await queue.run_bulk(op_id, items, handler)
    # Only the still-pending item ran.
    assert invocations == ["pending"]
    # First item's saved result was returned, not the new handler output.
    assert results[0].result_payload == {"prefilled": True}
    assert results[1].result_payload == {"fresh": True}


async def test_resume_bulk_picks_up_pending_items(store: OperationStore) -> None:
    op_id = _bulk_parent(store, "topic_bulk_create:resume2")
    # Pretend a previous worker registered three items but only finished one.
    a, _ = store.begin_item(operation_id=op_id, idempotency_key="a", request_payload={"i": 1})
    store.complete_item(a.id, {"done": "a"})
    store.begin_item(operation_id=op_id, idempotency_key="b", request_payload={"i": 2})
    store.begin_item(operation_id=op_id, idempotency_key="c", request_payload={"i": 3})

    queue = _make_queue(store)

    async def handler(spec: BulkItemSpec) -> dict[str, object]:
        return {"resumed": spec.payload["i"]}

    resumed = await queue.resume_bulk(op_id, handler)
    keys = sorted(r.idempotency_key for r in resumed)
    assert keys == ["b", "c"]
    assert all(r.status is OperationStatus.COMPLETED for r in resumed)
    # The completed item was untouched on resume — its saved result stands.
    items = {it.idempotency_key: it for it in store.list_items(op_id)}
    assert items["a"].result_payload == {"done": "a"}


async def test_run_bulk_flood_wait_retries_just_failing_item(
    store: OperationStore,
) -> None:
    op_id = _bulk_parent(store, "topic_bulk_create:fw")
    sleeps: list[float] = []
    queue = _make_queue(store, flood_margin=1.0, sleeps=sleeps)
    items = [BulkItemSpec(idempotency_key="x", payload={"i": 1})]
    calls = 0

    async def handler(spec: BulkItemSpec) -> dict[str, object]:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise FloodWaitError(3.0)
        return {"ok": True}

    results = await queue.run_bulk(op_id, items, handler)
    assert results[0].status is OperationStatus.COMPLETED
    assert calls == 2
    assert sleeps == [4.0]


# ---------------------------------------------------------------------------
# bounded parallelism
# ---------------------------------------------------------------------------


async def test_max_parallel_bounds_concurrent_handlers(store: OperationStore) -> None:
    queue = _make_queue(store, max_parallel=2)

    in_flight = 0
    peak = 0

    async def handler(spec: BulkItemSpec) -> dict[str, object]:
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        # Yield twice so several handlers overlap before any returns.
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        in_flight -= 1
        return {"i": spec.payload["i"]}

    # run_bulk processes items serially within a single call, so we drive
    # several bulks concurrently to exercise the semaphore.
    parents = [_bulk_parent(store, f"topic_bulk_create:par-{n}") for n in range(5)]

    async def run_one(pid: str) -> None:
        await queue.run_bulk(
            pid,
            [BulkItemSpec(idempotency_key=f"{pid}-x", payload={"i": 1})],
            handler,
        )

    await asyncio.gather(*(run_one(pid) for pid in parents))
    # Lower bound checks the semaphore actually permits parallelism (otherwise
    # a regression that collapsed it to 1 would silently pass this test).
    assert peak == 2


# ---------------------------------------------------------------------------
# retry helpers on the store
# ---------------------------------------------------------------------------


def test_reset_operation_for_retry_clears_failed(store: OperationStore) -> None:
    op = store.begin_operation(
        operation_type="x", idempotency_key="retry-1", request_payload={}
    )
    store.fail_operation(op.operation.id, "boom")
    reset = store.reset_operation_for_retry(op.operation.id)
    assert reset.status is OperationStatus.PENDING
    assert reset.error is None


def test_reset_operation_for_retry_clears_needs_review(
    store: OperationStore,
) -> None:
    op = store.begin_operation(
        operation_type="x", idempotency_key="retry-nr", request_payload={}
    )
    store.mark_needs_review(op.operation.id, "timeout")
    reset = store.reset_operation_for_retry(op.operation.id)
    assert reset.status is OperationStatus.PENDING
    assert reset.error is None


def test_reset_operation_for_retry_refuses_completed(
    store: OperationStore,
) -> None:
    op = store.begin_operation(
        operation_type="x", idempotency_key="retry-done", request_payload={}
    )
    store.complete_operation(op.operation.id, {"ok": True})
    with pytest.raises(ValueError):
        store.reset_operation_for_retry(op.operation.id)


def test_reset_items_for_retry_only_touches_non_completed(
    store: OperationStore,
) -> None:
    op_id = _bulk_parent(store, "topic_bulk_create:reset-items")
    done, _ = store.begin_item(operation_id=op_id, idempotency_key="d", request_payload={})
    fail, _ = store.begin_item(operation_id=op_id, idempotency_key="f", request_payload={})
    nr, _ = store.begin_item(operation_id=op_id, idempotency_key="n", request_payload={})
    pending, _ = store.begin_item(operation_id=op_id, idempotency_key="p", request_payload={})

    store.complete_item(done.id, {"ok": True})
    store.fail_item(fail.id, "boom")
    store.mark_item_needs_review(nr.id, "timeout")

    reset = store.reset_items_for_retry(op_id)
    reset_keys = {r.idempotency_key for r in reset}
    assert reset_keys == {"f", "n"}
    items = {it.idempotency_key: it for it in store.list_items(op_id)}
    assert items["d"].status is OperationStatus.COMPLETED
    assert items["d"].result_payload == {"ok": True}
    assert items["f"].status is OperationStatus.PENDING
    assert items["f"].error is None
    assert items["n"].status is OperationStatus.PENDING
    assert items["n"].error is None
    assert items["p"].status is OperationStatus.PENDING
    del pending
