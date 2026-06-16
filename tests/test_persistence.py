"""Tests for the SQLite persistence + idempotency layer (Task 4)."""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

import pytest

from telegram_assistant.persistence import (
    SCHEMA_VERSION,
    BeginResult,
    IdempotencyConflictError,
    OperationNotFoundError,
    OperationStatus,
    OperationStore,
    bootstrap,
    idempotency,
)

# ---------------------------------------------------------------------------
# Schema bootstrap
# ---------------------------------------------------------------------------


def test_bootstrap_creates_tables(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    bootstrap(db)
    with sqlite3.connect(str(db)) as conn:
        rows = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    assert {"operations", "operation_items", "idempotency_index", "schema_meta"} <= rows


def test_bootstrap_is_idempotent(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    bootstrap(db)
    bootstrap(db)  # Second call must not raise
    with sqlite3.connect(str(db)) as conn:
        version = conn.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'"
        ).fetchone()
    assert version[0] == str(SCHEMA_VERSION)


def test_bootstrap_creates_parent_directory(tmp_path: Path) -> None:
    db = tmp_path / "nested" / "deep" / "state.db"
    bootstrap(db)
    assert db.exists()


# ---------------------------------------------------------------------------
# Idempotency key derivation
# ---------------------------------------------------------------------------


def test_group_create_key_uses_external_ref_when_present() -> None:
    assert (
        idempotency.group_create_key(external_ref=42, title="anything")
        == "group_create:external_ref=42"
    )


def test_group_create_key_falls_back_to_title() -> None:
    assert (
        idempotency.group_create_key(external_ref=None, title="  Client A  ")
        == "group_create:title=Client A"
    )


def test_group_create_key_requires_one_of_inputs() -> None:
    with pytest.raises(ValueError):
        idempotency.group_create_key(external_ref=None, title=None)
    with pytest.raises(ValueError):
        idempotency.group_create_key(external_ref="", title="")


def test_topic_create_key_uses_external_ref() -> None:
    assert (
        idempotency.topic_create_key(
            external_ref="t-77", telegram_chat_id=10, topic_name="ignored"
        )
        == "topic_create:external_ref=t-77"
    )


def test_topic_create_key_falls_back_to_chat_plus_name() -> None:
    assert (
        idempotency.topic_create_key(
            external_ref=None, telegram_chat_id=-100123, topic_name="Q1 planning"
        )
        == "topic_create:chat=-100123:name=Q1 planning"
    )


def test_topic_create_key_requires_chat_and_name_when_no_external_ref() -> None:
    with pytest.raises(ValueError):
        idempotency.topic_create_key(
            external_ref=None, telegram_chat_id=None, topic_name="x"
        )
    with pytest.raises(ValueError):
        idempotency.topic_create_key(
            external_ref=None, telegram_chat_id=1, topic_name=None
        )


def test_group_rename_key_shape_and_stability() -> None:
    key = idempotency.group_rename_key(telegram_chat_id=-100123, new_title="  New Name  ")
    assert key == "group_rename:chat=-100123:title=New Name"
    # stable for identical inputs
    assert key == idempotency.group_rename_key(
        telegram_chat_id=-100123, new_title="New Name"
    )
    # distinct for a different title
    assert key != idempotency.group_rename_key(
        telegram_chat_id=-100123, new_title="Other Name"
    )


def test_group_rename_key_requires_inputs() -> None:
    with pytest.raises(ValueError):
        idempotency.group_rename_key(telegram_chat_id="", new_title="x")
    with pytest.raises(ValueError):
        idempotency.group_rename_key(telegram_chat_id=10, new_title="   ")


def test_topic_rename_key_shape_and_stability() -> None:
    key = idempotency.topic_rename_key(
        telegram_chat_id=-100123, telegram_topic_id=5, new_title="  Renamed  "
    )
    assert key == "topic_rename:chat=-100123:topic=5:title=Renamed"
    # stable for identical inputs
    assert key == idempotency.topic_rename_key(
        telegram_chat_id=-100123, telegram_topic_id=5, new_title="Renamed"
    )
    # distinct for a different title
    assert key != idempotency.topic_rename_key(
        telegram_chat_id=-100123, telegram_topic_id=5, new_title="Other"
    )


def test_topic_rename_key_requires_inputs() -> None:
    with pytest.raises(ValueError):
        idempotency.topic_rename_key(
            telegram_chat_id="", telegram_topic_id=5, new_title="x"
        )
    with pytest.raises(ValueError):
        idempotency.topic_rename_key(
            telegram_chat_id=10, telegram_topic_id="", new_title="x"
        )
    with pytest.raises(ValueError):
        idempotency.topic_rename_key(
            telegram_chat_id=10, telegram_topic_id=5, new_title="  "
        )


def test_member_keys_shape() -> None:
    assert (
        idempotency.member_add_key(telegram_chat_id=42, user="@alice")
        == "member_bulk_add:chat=42:user=@alice"
    )
    assert (
        idempotency.member_remove_key(telegram_chat_id=42, user="@bob")
        == "member_bulk_remove:chat=42:user=@bob"
    )


def test_bulk_item_key_requires_both_parts() -> None:
    with pytest.raises(ValueError):
        idempotency.bulk_item_key(operation_id="", per_item_key="x")
    with pytest.raises(ValueError):
        idempotency.bulk_item_key(operation_id="x", per_item_key="")
    assert idempotency.bulk_item_key(operation_id="op-1", per_item_key="user=@a") == (
        "op-1:user=@a"
    )


# ---------------------------------------------------------------------------
# OperationStore basics
# ---------------------------------------------------------------------------


@pytest.fixture()
def store(tmp_path: Path) -> OperationStore:
    return OperationStore(tmp_path / "state.db")


def test_begin_operation_creates_pending_row(store: OperationStore) -> None:
    result = store.begin_operation(
        operation_type=idempotency.GROUP_CREATE,
        idempotency_key="group_create:title=Acme",
        request_payload={"title": "Acme"},
    )
    assert isinstance(result, BeginResult)
    assert result.created is True
    assert result.operation.status is OperationStatus.PENDING
    assert result.operation.type == idempotency.GROUP_CREATE
    assert result.operation.request_payload == {"title": "Acme"}
    assert result.operation.result_payload is None
    assert result.operation.error is None
    assert result.items == []


def test_begin_operation_rejects_empty_key(store: OperationStore) -> None:
    with pytest.raises(ValueError):
        store.begin_operation(
            operation_type="x", idempotency_key="", request_payload={}
        )


def test_begin_operation_replays_completed(store: OperationStore) -> None:
    key = "group_create:title=Acme"
    first = store.begin_operation(
        operation_type=idempotency.GROUP_CREATE,
        idempotency_key=key,
        request_payload={"title": "Acme"},
    )
    completed = store.complete_operation(
        first.operation.id, {"telegram_chat_id": -100}
    )
    assert completed.status is OperationStatus.COMPLETED

    replay = store.begin_operation(
        operation_type=idempotency.GROUP_CREATE,
        idempotency_key=key,
        request_payload={"title": "Acme"},
    )
    assert replay.created is False
    assert replay.operation.id == first.operation.id
    assert replay.operation.status is OperationStatus.COMPLETED
    assert replay.operation.result_payload == {"telegram_chat_id": -100}


def test_begin_operation_replays_failed(store: OperationStore) -> None:
    key = "topic_close:chat=-100:topic=5"
    first = store.begin_operation(
        operation_type=idempotency.TOPIC_CLOSE,
        idempotency_key=key,
        request_payload={"chat_id": -100, "topic_id": 5},
    )
    store.fail_operation(first.operation.id, "telethon: chat not found")

    replay = store.begin_operation(
        operation_type=idempotency.TOPIC_CLOSE,
        idempotency_key=key,
        request_payload={"chat_id": -100, "topic_id": 5},
    )
    assert replay.created is False
    assert replay.operation.status is OperationStatus.FAILED
    assert replay.operation.error == "telethon: chat not found"


def test_begin_operation_replays_needs_review(store: OperationStore) -> None:
    key = "topic_create:planfix_task_id=99"
    first = store.begin_operation(
        operation_type=idempotency.TOPIC_CREATE,
        idempotency_key=key,
        request_payload={"planfix_task_id": 99},
    )
    store.mark_needs_review(first.operation.id, "timeout: undefined outcome")

    replay = store.begin_operation(
        operation_type=idempotency.TOPIC_CREATE,
        idempotency_key=key,
        request_payload={"planfix_task_id": 99},
    )
    assert replay.created is False
    assert replay.operation.status is OperationStatus.NEEDS_REVIEW
    assert replay.operation.error == "timeout: undefined outcome"


def test_begin_operation_replays_pending_returns_same_id(store: OperationStore) -> None:
    key = "group_create:title=Beta"
    first = store.begin_operation(
        operation_type=idempotency.GROUP_CREATE,
        idempotency_key=key,
        request_payload={"title": "Beta"},
    )
    replay = store.begin_operation(
        operation_type=idempotency.GROUP_CREATE,
        idempotency_key=key,
        request_payload={"title": "Beta"},
    )
    assert replay.created is False
    assert replay.operation.id == first.operation.id
    assert replay.operation.status is OperationStatus.PENDING


def test_duplicate_key_with_different_type_raises(store: OperationStore) -> None:
    key = "shared-key"
    store.begin_operation(
        operation_type=idempotency.GROUP_CREATE,
        idempotency_key=key,
        request_payload={},
    )
    with pytest.raises(IdempotencyConflictError):
        store.begin_operation(
            operation_type=idempotency.TOPIC_CREATE,
            idempotency_key=key,
            request_payload={},
        )


def test_complete_after_complete_is_idempotent(store: OperationStore) -> None:
    first = store.begin_operation(
        operation_type="x", idempotency_key="k1", request_payload={}
    )
    completed = store.complete_operation(first.operation.id, {"ok": True})
    # Attempting to re-fail a completed operation must NOT rewrite the outcome.
    after = store.fail_operation(first.operation.id, "should be ignored")
    assert after.status is OperationStatus.COMPLETED
    assert after.result_payload == {"ok": True}
    assert after.error is None
    assert completed.id == after.id


def test_needs_review_is_not_overwritten_by_failure(store: OperationStore) -> None:
    first = store.begin_operation(
        operation_type="x", idempotency_key="k-needs", request_payload={}
    )
    store.mark_needs_review(first.operation.id, "timeout")
    after = store.fail_operation(first.operation.id, "later error")
    assert after.status is OperationStatus.NEEDS_REVIEW
    assert after.error == "timeout"


def test_get_operation_missing_raises(store: OperationStore) -> None:
    with pytest.raises(OperationNotFoundError):
        store.get_operation("does-not-exist")


def test_find_by_idempotency_key(store: OperationStore) -> None:
    assert store.find_by_idempotency_key("missing") is None
    created = store.begin_operation(
        operation_type="x", idempotency_key="found", request_payload={}
    )
    found = store.find_by_idempotency_key("found")
    assert found is not None
    assert found.id == created.operation.id


# ---------------------------------------------------------------------------
# wait_for_completion
# ---------------------------------------------------------------------------


def test_wait_for_completion_returns_immediately_when_terminal(
    store: OperationStore,
) -> None:
    op = store.begin_operation(
        operation_type="x", idempotency_key="t1", request_payload={}
    )
    store.complete_operation(op.operation.id, {"ok": True})
    started = time.monotonic()
    result = store.wait_for_completion(op.operation.id, timeout_seconds=5.0)
    assert result.status is OperationStatus.COMPLETED
    assert time.monotonic() - started < 0.5


def test_wait_for_completion_times_out_on_pending(store: OperationStore) -> None:
    op = store.begin_operation(
        operation_type="x", idempotency_key="t2", request_payload={}
    )
    started = time.monotonic()
    result = store.wait_for_completion(
        op.operation.id, timeout_seconds=0.15, poll_interval_seconds=0.02
    )
    elapsed = time.monotonic() - started
    assert result.status is OperationStatus.PENDING
    assert 0.1 <= elapsed < 1.0


def test_wait_for_completion_unblocks_on_background_transition(
    store: OperationStore,
) -> None:
    op = store.begin_operation(
        operation_type="x", idempotency_key="t3", request_payload={}
    )

    def _complete_later() -> None:
        time.sleep(0.05)
        store.complete_operation(op.operation.id, {"ok": True})

    worker = threading.Thread(target=_complete_later)
    worker.start()
    try:
        result = store.wait_for_completion(
            op.operation.id, timeout_seconds=2.0, poll_interval_seconds=0.02
        )
        assert result.status is OperationStatus.COMPLETED
    finally:
        worker.join()


# ---------------------------------------------------------------------------
# Per-item bulk idempotency
# ---------------------------------------------------------------------------


def test_begin_item_inserts_and_replays(store: OperationStore) -> None:
    parent = store.begin_operation(
        operation_type=idempotency.TOPIC_BULK_CREATE,
        idempotency_key="topic_bulk_create:batch-1",
        request_payload={},
    )
    op_id = parent.operation.id

    item_key = idempotency.bulk_item_key(operation_id=op_id, per_item_key="task=10")
    item, created = store.begin_item(
        operation_id=op_id,
        idempotency_key=item_key,
        request_payload={"planfix_task_id": 10},
    )
    assert created is True
    assert item.status is OperationStatus.PENDING

    again, created_again = store.begin_item(
        operation_id=op_id,
        idempotency_key=item_key,
        request_payload={"planfix_task_id": 10},
    )
    assert created_again is False
    assert again.id == item.id


def test_item_state_transitions(store: OperationStore) -> None:
    parent = store.begin_operation(
        operation_type=idempotency.TOPIC_BULK_CREATE,
        idempotency_key="topic_bulk_create:batch-2",
        request_payload={},
    )
    item_a, _ = store.begin_item(
        operation_id=parent.operation.id,
        idempotency_key="a",
        request_payload={"i": 1},
    )
    item_b, _ = store.begin_item(
        operation_id=parent.operation.id,
        idempotency_key="b",
        request_payload={"i": 2},
    )
    item_c, _ = store.begin_item(
        operation_id=parent.operation.id,
        idempotency_key="c",
        request_payload={"i": 3},
    )

    completed = store.complete_item(item_a.id, {"telegram_topic_id": 1})
    failed = store.fail_item(item_b.id, "privacy restricted")
    needs = store.mark_item_needs_review(item_c.id, "undefined outcome")

    assert completed.status is OperationStatus.COMPLETED
    assert completed.result_payload == {"telegram_topic_id": 1}
    assert failed.status is OperationStatus.FAILED
    assert failed.error == "privacy restricted"
    assert needs.status is OperationStatus.NEEDS_REVIEW
    assert needs.error == "undefined outcome"

    items = store.list_items(parent.operation.id)
    assert {it.idempotency_key for it in items} == {"a", "b", "c"}


def test_iter_pending_items_filters_to_pending(store: OperationStore) -> None:
    parent = store.begin_operation(
        operation_type=idempotency.TOPIC_BULK_CREATE,
        idempotency_key="topic_bulk_create:batch-3",
        request_payload={},
    )
    p, _ = store.begin_item(
        operation_id=parent.operation.id,
        idempotency_key="pending-1",
        request_payload={},
    )
    done, _ = store.begin_item(
        operation_id=parent.operation.id,
        idempotency_key="done-1",
        request_payload={},
    )
    store.complete_item(done.id, {"ok": True})

    pending = list(store.iter_pending_items(parent.operation.id))
    assert [it.id for it in pending] == [p.id]


def test_per_item_idempotency_isolated_by_operation(store: OperationStore) -> None:
    op_a = store.begin_operation(
        operation_type=idempotency.TOPIC_BULK_CREATE,
        idempotency_key="topic_bulk_create:A",
        request_payload={},
    )
    op_b = store.begin_operation(
        operation_type=idempotency.TOPIC_BULK_CREATE,
        idempotency_key="topic_bulk_create:B",
        request_payload={},
    )
    # Same per-item idempotency key under two different parents must produce
    # two distinct rows — the key only collides within the parent operation.
    item_a, created_a = store.begin_item(
        operation_id=op_a.operation.id,
        idempotency_key="duplicate",
        request_payload={},
    )
    item_b, created_b = store.begin_item(
        operation_id=op_b.operation.id,
        idempotency_key="duplicate",
        request_payload={},
    )
    assert created_a and created_b
    assert item_a.id != item_b.id


def test_begin_result_replay_includes_items(store: OperationStore) -> None:
    parent = store.begin_operation(
        operation_type=idempotency.TOPIC_BULK_CREATE,
        idempotency_key="topic_bulk_create:with-items",
        request_payload={},
    )
    store.begin_item(
        operation_id=parent.operation.id,
        idempotency_key="x",
        request_payload={},
    )

    replay = store.begin_operation(
        operation_type=idempotency.TOPIC_BULK_CREATE,
        idempotency_key="topic_bulk_create:with-items",
        request_payload={},
    )
    assert replay.created is False
    assert [it.idempotency_key for it in replay.items] == ["x"]
