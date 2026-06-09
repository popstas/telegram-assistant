"""Topic-creation domain shared by HTTP, CLI, and worker.

The :func:`create_topic` function is the single source of truth for the
"create a single forum topic in an existing supergroup" workflow. It handles
topic creation, first-message logic, and idempotency.

Idempotency keys (per the plan's Technical Details):

* ``external_ref`` when present
* ``telegram_chat_id + topic_name`` otherwise

First-message logic, in order:

* ``message`` if provided, otherwise the duplicated topic name — this is the
  *surviving* first message
* when a plugin provides a service message (e.g. ``/task {external_ref}``), the
  plugin posts it as a *second* message via ``after_topic_create`` and, if
  configured, cleans it up — so the surviving first message above is what stays
  visible in the topic
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from telegram_assistant.access.service import AccessLevel, Authorizer
from telegram_assistant.observability.logging import get_logger
from telegram_assistant.persistence import idempotency
from telegram_assistant.persistence.models import (
    OperationItemRecord,
    OperationRecord,
    OperationStatus,
)
from telegram_assistant.persistence.store import OperationStore
from telegram_assistant.plugins import PluginRegistry
from telegram_assistant.worker.queue import (
    BulkItemSpec,
    FloodWaitError,
    WorkerQueue,
)

logger = get_logger(__name__)


class TopicError(RuntimeError):
    """Base class for topic-creation failures surfaced to callers."""


class TopicCreateFailed(TopicError):
    """A previous attempt with this idempotency key already failed."""


class TopicCreatePending(TopicError):
    """A concurrent attempt with this idempotency key is still in flight."""


class TopicCreateNeedsReview(TopicError):
    """A previous attempt resulted in ``needs_review`` and must not auto-retry."""


class BulkTopicCreateFailed(TopicError):
    """A previous bulk-create attempt with this ``operation_id`` already failed."""


class BulkTopicCreatePending(TopicError):
    """A concurrent bulk-create attempt with this ``operation_id`` is in flight."""


class TopicCloseFailed(TopicError):
    """A previous close attempt with this idempotency key already failed."""


class TopicClosePending(TopicError):
    """A concurrent close attempt with this idempotency key is in flight."""


class TopicCloseNeedsReview(TopicError):
    """A previous close attempt resulted in needs_review."""


class AmbiguousTopicNameError(TopicError):
    """More than one topic in the chat shares the requested name."""

    def __init__(self, *, topic_name: str, telegram_chat_id: int, matches: list[int]) -> None:
        super().__init__(
            f"topic name {topic_name!r} matches {len(matches)} topics in chat "
            f"{telegram_chat_id}: {matches!r}"
        )
        self.topic_name = topic_name
        self.telegram_chat_id = telegram_chat_id
        self.matches = list(matches)


class TopicNotFoundError(TopicError):
    """No topic with the requested name exists in the chat."""


class BulkTopicCreateNeedsReview(TopicError):
    """A previous bulk-create attempt ended in needs_review; operator must
    reset via ``operations retry`` to resume.
    """


@dataclass(frozen=True)
class TopicCreateRequest:
    """Input shape shared by HTTP, CLI, and tests."""

    telegram_chat_id: int
    topic_name: str
    external_ref: int | str | None = None
    message: str | None = None

    def to_payload(self) -> dict[str, Any]:
        return {
            "telegram_chat_id": self.telegram_chat_id,
            "topic_name": self.topic_name,
            "external_ref": self.external_ref,
            "message": self.message,
        }


@dataclass(frozen=True)
class TopicCreateResult:
    """Result returned by both the live execution and a replay."""

    telegram_chat_id: int
    telegram_topic_id: int
    topic_name: str
    external_ref: int | str | None
    first_message_kind: str
    first_message_text: str | None
    telegram_message_id: int | None
    replayed: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "telegram_chat_id": self.telegram_chat_id,
            "telegram_topic_id": self.telegram_topic_id,
            "topic_name": self.topic_name,
            "external_ref": self.external_ref,
            "first_message_kind": self.first_message_kind,
            "first_message_text": self.first_message_text,
            "telegram_message_id": self.telegram_message_id,
            "replayed": self.replayed,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> TopicCreateResult:
        return cls(
            telegram_chat_id=int(payload["telegram_chat_id"]),
            telegram_topic_id=int(payload["telegram_topic_id"]),
            topic_name=str(payload["topic_name"]),
            # Legacy records persisted ``planfix_task_id``; accept both on read.
            external_ref=payload.get("external_ref", payload.get("planfix_task_id")),
            first_message_kind=str(payload.get("first_message_kind", "")),
            first_message_text=payload.get("first_message_text"),
            telegram_message_id=(
                int(payload["telegram_message_id"])
                if payload.get("telegram_message_id") is not None
                else None
            ),
            replayed=True,
        )


@dataclass(frozen=True)
class TopicSummary:
    """One topic surfaced by :meth:`TopicBackend.list_topics`."""

    topic_id: int
    title: str
    closed: bool = False


class TopicBackend(Protocol):
    """Telethon-facing operations needed to create a forum topic.

    Tests inject a fake; production wires this to a Telethon adapter.
    """

    async def create_topic(self, *, chat_id: int, name: str) -> int:
        ...

    async def send_message(
        self, *, chat_id: int, text: str, topic_id: int | None = None
    ) -> int:
        ...

    async def close_topic(self, *, chat_id: int, topic_id: int) -> None:
        ...

    async def get_recent_messages(
        self, *, chat_id: int, limit: int, topic_id: int | None = None
    ) -> list[dict[str, Any]]:
        ...

    async def delete_messages(
        self, *, chat_id: int, message_ids: Sequence[int]
    ) -> None:
        ...

    async def list_topics(self, *, chat_id: int) -> list[TopicSummary]:
        ...


def _first_message(
    *,
    request: TopicCreateRequest,
    plugins: PluginRegistry,
) -> tuple[str, str, bool]:
    """Decide the *surviving* first message and return ``(kind, text, task_pending)``.

    The surviving first message is the explicit ``message`` if provided, else
    the duplicated topic name. ``task_pending`` is ``True`` when a plugin
    provides a service message (e.g. ``/task``) for this topic — in that case
    the plugin posts (and optionally cleans up) that service message as a
    *second* message via ``after_topic_create``, leaving the surviving message
    visible.

    * ``("message", <message>, task_pending)`` when an explicit ``message`` is provided
    * ``("topic_name", <topic_name>, task_pending)`` otherwise
    """
    task = plugins.topic_first_message(external_ref=request.external_ref)
    task_pending = task is not None
    if request.message is not None and request.message.strip():
        return "message", request.message, task_pending
    return "topic_name", request.topic_name, task_pending


async def _execute_create(
    *,
    backend: TopicBackend,
    request: TopicCreateRequest,
    plugins: PluginRegistry,
) -> TopicCreateResult:
    topic_id = await backend.create_topic(
        chat_id=request.telegram_chat_id, name=request.topic_name
    )

    kind, text, task_pending = _first_message(request=request, plugins=plugins)
    message_id: int | None = None
    try:
        # The surviving first message (topic name or explicit message). When a
        # plugin posts a `/task` message afterwards, this is what stays visible.
        message_id = await backend.send_message(
            chat_id=request.telegram_chat_id,
            text=text,
            topic_id=topic_id,
        )
    except FloodWaitError:
        # When this function runs under the queue's bulk handler, FLOOD_WAIT
        # must propagate so the queue can pause-and-retry the whole item
        # instead of silently producing a topic without its first message.
        raise
    except Exception:
        # First-message failure is non-fatal — the topic itself was created.
        # The caller can resend manually. We record the attempt without a
        # message id so the result still reflects which branch ran.
        message_id = None

    if task_pending:
        # Delegate the plugin service message (e.g. `/task <ref>`) and its
        # cleanup to the active plugins. Best-effort: the plugin swallows its
        # own failures into `skipped`, mirroring `after_group_create`.
        skipped: list[dict[str, Any]] = []
        await plugins.after_topic_create(
            backend=backend,
            chat_id=request.telegram_chat_id,
            topic_id=topic_id,
            external_ref=request.external_ref,
            skipped=skipped,
        )
        if skipped:
            logger.debug(
                "topic post-create skipped",
                chat_id=request.telegram_chat_id,
                topic_id=topic_id,
                items=skipped,
            )

    return TopicCreateResult(
        telegram_chat_id=request.telegram_chat_id,
        telegram_topic_id=topic_id,
        topic_name=request.topic_name,
        external_ref=request.external_ref,
        first_message_kind=kind,
        first_message_text=text,
        telegram_message_id=message_id,
    )


async def create_topic(
    *,
    backend: TopicBackend,
    store: OperationStore,
    request: TopicCreateRequest,
    plugins: PluginRegistry | None = None,
    authorizer: Authorizer | None = None,
) -> tuple[TopicCreateResult, OperationRecord]:
    """Create a forum topic, or replay the saved result for the same key.

    State machine transitions identical to :func:`create_group`:

    * `completed`     → return saved result with ``replayed=True``
    * `failed`        → raise :class:`TopicCreateFailed`
    * `needs_review`  → raise :class:`TopicCreateNeedsReview`
    * `pending`       → raise :class:`TopicCreatePending`
    """
    if plugins is None:
        plugins = PluginRegistry()
    if not request.topic_name.strip():
        raise ValueError("topic create requires non-empty topic_name")

    # Creating a topic is a WRITE op on the host chat.
    if authorizer is not None:
        await authorizer.require(request.telegram_chat_id, AccessLevel.WRITE)

    key = idempotency.topic_create_key(
        external_ref=request.external_ref,
        telegram_chat_id=request.telegram_chat_id,
        topic_name=request.topic_name,
    )
    begin = store.begin_operation(
        operation_type=idempotency.TOPIC_CREATE,
        idempotency_key=key,
        request_payload=request.to_payload(),
    )

    if not begin.created:
        op = begin.operation
        if op.status is OperationStatus.COMPLETED:
            payload = op.result_payload or {}
            return TopicCreateResult.from_dict(payload), op
        if op.status is OperationStatus.FAILED:
            raise TopicCreateFailed(op.error or "previous attempt failed")
        if op.status is OperationStatus.NEEDS_REVIEW:
            raise TopicCreateNeedsReview(
                op.error or "previous attempt needs review"
            )
        raise TopicCreatePending(
            f"operation {op.id} is still pending; retry via 'operations retry'"
        )

    operation_id = begin.operation.id
    try:
        result = await _execute_create(
            backend=backend, request=request, plugins=plugins
        )
    except FloodWaitError as exc:
        # FLOOD_WAIT is transient — don't burn the idempotency key on `failed`
        # (terminal) because the next caller would be stuck. Marking
        # needs_review lets an operator `operations retry` after the window.
        store.mark_needs_review(
            operation_id, f"FLOOD_WAIT during topic create: {exc}"
        )
        raise TopicCreateNeedsReview(str(exc)) from exc
    except TopicError:
        raise
    except Exception as exc:
        store.fail_operation(operation_id, str(exc))
        raise

    op = store.complete_operation(operation_id, result.to_dict())
    return result, op


# ---------------------------------------------------------------------------
# Bulk topic creation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BulkTopicItem:
    """One row inside a bulk topic-create request."""

    topic_name: str
    external_ref: int | str | None = None
    message: str | None = None

    def to_payload(self) -> dict[str, Any]:
        return {
            "topic_name": self.topic_name,
            "external_ref": self.external_ref,
            "message": self.message,
        }


@dataclass(frozen=True)
class BulkTopicCreateRequest:
    """Input to :func:`bulk_create_topics`."""

    telegram_chat_id: int
    items: Sequence[BulkTopicItem]
    continue_on_error: bool = True
    operation_id: str | None = None

    def to_payload(self) -> dict[str, Any]:
        return {
            "telegram_chat_id": self.telegram_chat_id,
            "items": [it.to_payload() for it in self.items],
            "continue_on_error": self.continue_on_error,
            "operation_id": self.operation_id,
        }


@dataclass(frozen=True)
class BulkTopicItemResult:
    """One row in the aggregated response.

    ``status`` is one of ``created`` | ``existed`` | ``failed`` |
    ``needs_review`` | ``skipped``. ``existed`` is a per-bulk replay (same
    ``operation_id`` re-issued) of a previously-completed item.
    """

    status: str
    topic_name: str
    external_ref: int | str | None
    telegram_topic_id: int | None = None
    first_message_kind: str | None = None
    first_message_text: str | None = None
    telegram_message_id: int | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "topic_name": self.topic_name,
            "external_ref": self.external_ref,
            "telegram_topic_id": self.telegram_topic_id,
            "first_message_kind": self.first_message_kind,
            "first_message_text": self.first_message_text,
            "telegram_message_id": self.telegram_message_id,
            "error": self.error,
        }


@dataclass(frozen=True)
class BulkTopicCreateResult:
    """Aggregated outcome of :func:`bulk_create_topics`."""

    operation_id: str
    telegram_chat_id: int
    created: int
    existed: int
    failed: int
    needs_review: int
    skipped: int
    items: list[BulkTopicItemResult]

    def to_dict(self) -> dict[str, Any]:
        return {
            "operation_id": self.operation_id,
            "telegram_chat_id": self.telegram_chat_id,
            "created": self.created,
            "existed": self.existed,
            "failed": self.failed,
            "needs_review": self.needs_review,
            "skipped": self.skipped,
            "items": [it.to_dict() for it in self.items],
        }


def _bulk_parent_key(operation_id: str | None) -> tuple[str, str]:
    """Return ``(key, effective_id)`` for the bulk parent row.

    Callers supplying an explicit ``operation_id`` get idempotent replay
    against that id; otherwise we mint a fresh one so each call is independent.
    """
    if operation_id is not None and str(operation_id).strip():
        oid = str(operation_id).strip()
    else:
        oid = uuid.uuid4().hex
    return f"{idempotency.TOPIC_BULK_CREATE}:id={oid}", oid


def _item_status(record: OperationItemRecord | None, was_existing: bool) -> str:
    """Map an item record to the response-side status string."""
    if record is None:
        return "skipped"
    if record.status is OperationStatus.COMPLETED:
        return "existed" if was_existing else "created"
    if record.status is OperationStatus.FAILED:
        return "failed"
    if record.status is OperationStatus.NEEDS_REVIEW:
        return "needs_review"
    return "skipped"


def _build_item_result(
    *,
    item_input: BulkTopicItem,
    record: OperationItemRecord | None,
    was_existing: bool,
) -> BulkTopicItemResult:
    status = _item_status(record, was_existing)
    if record is None:
        return BulkTopicItemResult(
            status=status,
            topic_name=item_input.topic_name,
            external_ref=item_input.external_ref,
        )
    if record.status is OperationStatus.COMPLETED:
        payload = record.result_payload or {}
        return BulkTopicItemResult(
            status=status,
            topic_name=item_input.topic_name,
            external_ref=item_input.external_ref,
            telegram_topic_id=(
                int(payload["telegram_topic_id"])
                if payload.get("telegram_topic_id") is not None
                else None
            ),
            first_message_kind=payload.get("first_message_kind"),
            first_message_text=payload.get("first_message_text"),
            telegram_message_id=(
                int(payload["telegram_message_id"])
                if payload.get("telegram_message_id") is not None
                else None
            ),
        )
    return BulkTopicItemResult(
        status=status,
        topic_name=item_input.topic_name,
        external_ref=item_input.external_ref,
        error=record.error,
    )


def _replay_bulk(
    *,
    parent_op: OperationRecord,
    specs: Sequence[BulkItemSpec],
    request: BulkTopicCreateRequest,
    store: OperationStore,
) -> BulkTopicCreateResult:
    """Reconstruct the aggregated response from saved item state.

    Used when a previous bulk with the same ``operation_id`` already
    completed — we never touch Telegram, just rebuild the response.
    """
    items_by_key = {
        rec.idempotency_key: rec for rec in store.list_items(parent_op.id)
    }
    item_results: list[BulkTopicItemResult] = []
    counts = {"created": 0, "existed": 0, "failed": 0, "needs_review": 0, "skipped": 0}
    for spec, item_input in zip(specs, request.items, strict=True):
        rec = items_by_key.get(spec.idempotency_key)
        # On replay everything that's completed is reported as `existed` so
        # the caller can tell the rerun apart from the original.
        built = _build_item_result(
            item_input=item_input, record=rec, was_existing=True
        )
        item_results.append(built)
        counts[built.status] = counts.get(built.status, 0) + 1
    return BulkTopicCreateResult(
        operation_id=parent_op.id,
        telegram_chat_id=request.telegram_chat_id,
        created=counts["created"],
        existed=counts["existed"],
        failed=counts["failed"],
        needs_review=counts["needs_review"],
        skipped=counts["skipped"],
        items=item_results,
    )


async def bulk_create_topics(
    *,
    backend: TopicBackend,
    store: OperationStore,
    queue: WorkerQueue,
    request: BulkTopicCreateRequest,
    plugins: PluginRegistry | None = None,
    authorizer: Authorizer | None = None,
) -> tuple[BulkTopicCreateResult, OperationRecord]:
    """Create many forum topics under one parent operation.

    The parent operation is keyed by ``request.operation_id`` if supplied (so
    the same bulk can be retried idempotently), otherwise a fresh UUID. Each
    item is persisted under ``operation_items`` keyed by
    ``bulk_item_key(parent_op.id, topic_create_key(...))`` so a restart
    resumes exactly where the previous run left off.

    Per-item first-message logic mirrors :func:`create_topic`: the surviving
    first message is the explicit ``message`` else the topic name; when a plugin
    applies it posts (and optionally cleans up) its ``/task <id>`` service
    message as a second message via ``after_topic_create``.
    """
    if plugins is None:
        plugins = PluginRegistry()
    if not request.items:
        raise ValueError("bulk topic create requires at least one item")
    for it in request.items:
        if not it.topic_name.strip():
            raise ValueError("bulk topic create requires non-empty topic_name per item")

    # The whole bulk targets one chat — gate it once with WRITE up front.
    if authorizer is not None:
        await authorizer.require(request.telegram_chat_id, AccessLevel.WRITE)

    parent_key, _ = _bulk_parent_key(request.operation_id)
    begin = store.begin_operation(
        operation_type=idempotency.TOPIC_BULK_CREATE,
        idempotency_key=parent_key,
        request_payload=request.to_payload(),
    )
    parent_op = begin.operation

    # Compute per-item specs once — needed both for replay reconstruction and
    # for the queue's run_bulk.
    specs: list[BulkItemSpec] = []
    for it in request.items:
        per_key = idempotency.topic_create_key(
            external_ref=it.external_ref,
            telegram_chat_id=request.telegram_chat_id,
            topic_name=it.topic_name,
        )
        bulk_key = idempotency.bulk_item_key(
            operation_id=parent_op.id, per_item_key=per_key
        )
        specs.append(
            BulkItemSpec(
                idempotency_key=bulk_key,
                payload={
                    "telegram_chat_id": request.telegram_chat_id,
                    "topic_name": it.topic_name,
                    "external_ref": it.external_ref,
                    "message": it.message,
                },
            )
        )

    if not begin.created:
        # Existing parent — mirror the single-topic state machine.
        if parent_op.status is OperationStatus.COMPLETED:
            return (
                _replay_bulk(
                    parent_op=parent_op,
                    specs=specs,
                    request=request,
                    store=store,
                ),
                parent_op,
            )
        if parent_op.status is OperationStatus.FAILED:
            raise BulkTopicCreateFailed(parent_op.error or "previous bulk failed")
        if parent_op.status is OperationStatus.NEEDS_REVIEW:
            raise BulkTopicCreateNeedsReview(
                parent_op.error or "previous bulk needs review"
            )
        # PENDING → fall through and resume work below.

    # Snapshot which items were already completed BEFORE this call, so we can
    # distinguish "existed" (replay from a prior run with the same
    # operation_id) from "created" (executed in this run).
    pre_existing_completed = {
        it.idempotency_key
        for it in store.list_items(parent_op.id)
        if it.status is OperationStatus.COMPLETED
    }

    async def _handler(spec: BulkItemSpec) -> dict[str, Any]:
        payload = spec.payload
        item_request = TopicCreateRequest(
            telegram_chat_id=int(payload["telegram_chat_id"]),
            topic_name=str(payload["topic_name"]),
            # Legacy persisted specs used ``planfix_task_id``; accept both.
            external_ref=payload.get("external_ref", payload.get("planfix_task_id")),
            message=payload.get("message"),
        )
        # Bypass create_topic() here: a FLOOD_WAIT from the backend must
        # propagate up to the queue, but create_topic()'s broad
        # `except Exception` would mark the per-topic operation `failed`
        # instead. Calling the executor directly keeps the queue in charge
        # of FLOOD_WAIT handling.
        result = await _execute_create(
            backend=backend, request=item_request, plugins=plugins
        )
        return result.to_dict()

    item_records = await queue.run_bulk(
        parent_op.id,
        specs,
        _handler,
        stop_on_failure=not request.continue_on_error,
    )

    # Build the aggregated response. run_bulk may have returned fewer records
    # than specs when stop_on_failure short-circuited the loop; for items it
    # never reached we report `skipped`.
    records_by_key: dict[str, OperationItemRecord] = {
        rec.idempotency_key: rec for rec in item_records
    }
    item_results: list[BulkTopicItemResult] = []
    counts = {"created": 0, "existed": 0, "failed": 0, "needs_review": 0, "skipped": 0}
    for spec, item_input in zip(specs, request.items, strict=True):
        rec = records_by_key.get(spec.idempotency_key)
        was_existing = spec.idempotency_key in pre_existing_completed
        built = _build_item_result(
            item_input=item_input, record=rec, was_existing=was_existing
        )
        item_results.append(built)
        counts[built.status] = counts.get(built.status, 0) + 1

    # Decide the parent operation's final status. ``completed`` only when no
    # item ended in failed/needs_review and nothing was skipped by
    # stop_on_failure. Otherwise ``needs_review`` so the operator inspects via
    # `operations status`.
    parent_payload = {
        "telegram_chat_id": request.telegram_chat_id,
        "counts": counts,
        "operation_id": parent_op.id,
    }
    if counts["failed"] == 0 and counts["needs_review"] == 0 and counts["skipped"] == 0:
        final_parent = store.complete_operation(parent_op.id, parent_payload)
    else:
        # Surface the situation to operators without auto-retry.
        final_parent = store.mark_needs_review(
            parent_op.id,
            f"bulk_topic_create finished with "
            f"failed={counts['failed']}, "
            f"needs_review={counts['needs_review']}, "
            f"skipped={counts['skipped']}",
        )

    aggregated = BulkTopicCreateResult(
        operation_id=parent_op.id,
        telegram_chat_id=request.telegram_chat_id,
        created=counts["created"],
        existed=counts["existed"],
        failed=counts["failed"],
        needs_review=counts["needs_review"],
        skipped=counts["skipped"],
        items=item_results,
    )
    return aggregated, final_parent


# ---------------------------------------------------------------------------
# Close topic
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TopicCloseRequest:
    """Input shape for :func:`close_topic`."""

    telegram_chat_id: int
    telegram_topic_id: int
    reason: str | None = None

    def to_payload(self) -> dict[str, Any]:
        return {
            "telegram_chat_id": self.telegram_chat_id,
            "telegram_topic_id": self.telegram_topic_id,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class TopicCloseResult:
    """Result returned by both the live execution and a replay."""

    telegram_chat_id: int
    telegram_topic_id: int
    status: str
    reason: str | None = None
    replayed: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "telegram_chat_id": self.telegram_chat_id,
            "telegram_topic_id": self.telegram_topic_id,
            "status": self.status,
            "reason": self.reason,
            "replayed": self.replayed,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> TopicCloseResult:
        return cls(
            telegram_chat_id=int(payload["telegram_chat_id"]),
            telegram_topic_id=int(payload["telegram_topic_id"]),
            status=str(payload.get("status", "closed")),
            reason=payload.get("reason"),
            replayed=True,
        )


async def resolve_topic_id_by_name(
    *,
    backend: TopicBackend,
    telegram_chat_id: int,
    topic_name: str,
) -> int:
    """Look up a single topic by exact title within a chat.

    Ambiguous matches raise :class:`AmbiguousTopicNameError` so the caller can
    disambiguate, rather than silently picking one. Missing topics raise
    :class:`TopicNotFoundError`.
    """
    if not topic_name.strip():
        raise ValueError("topic resolution requires non-empty topic_name")
    summaries = await backend.list_topics(chat_id=telegram_chat_id)
    matches = [t for t in summaries if t.title == topic_name]
    if not matches:
        raise TopicNotFoundError(
            f"topic {topic_name!r} not found in chat {telegram_chat_id}"
        )
    if len(matches) > 1:
        raise AmbiguousTopicNameError(
            topic_name=topic_name,
            telegram_chat_id=telegram_chat_id,
            matches=[t.topic_id for t in matches],
        )
    return matches[0].topic_id


async def close_topic(
    *,
    backend: TopicBackend,
    store: OperationStore,
    request: TopicCloseRequest,
    authorizer: Authorizer | None = None,
) -> tuple[TopicCloseResult, OperationRecord]:
    """Close a forum topic, or replay the saved result for the same key.

    Idempotency key: ``telegram_chat_id + telegram_topic_id`` (per the plan).
    Re-close returns ``status=closed`` with ``replayed=True`` without touching
    Telegram a second time. The topic itself and its history are never
    deleted — closing only flips the topic's ``closed`` flag.
    """
    if request.telegram_topic_id <= 0:
        raise ValueError("close_topic requires a positive telegram_topic_id")

    # Closing a topic is a WRITE op on the host chat.
    if authorizer is not None:
        await authorizer.require(request.telegram_chat_id, AccessLevel.WRITE)

    key = idempotency.topic_close_key(
        telegram_chat_id=request.telegram_chat_id,
        telegram_topic_id=request.telegram_topic_id,
    )
    begin = store.begin_operation(
        operation_type=idempotency.TOPIC_CLOSE,
        idempotency_key=key,
        request_payload=request.to_payload(),
    )

    if not begin.created:
        op = begin.operation
        if op.status is OperationStatus.COMPLETED:
            payload = op.result_payload or {}
            return TopicCloseResult.from_dict(payload), op
        if op.status is OperationStatus.FAILED:
            raise TopicCloseFailed(op.error or "previous attempt failed")
        if op.status is OperationStatus.NEEDS_REVIEW:
            raise TopicCloseNeedsReview(op.error or "previous attempt needs review")
        raise TopicClosePending(
            f"operation {op.id} is still pending; retry via 'operations retry'"
        )

    operation_id = begin.operation.id
    try:
        await backend.close_topic(
            chat_id=request.telegram_chat_id,
            topic_id=request.telegram_topic_id,
        )
    except FloodWaitError as exc:
        # Same reasoning as create_topic: FLOOD_WAIT must not lock the
        # idempotency key into a terminal failure. Promote to needs_review so
        # the operator can retry once the window expires.
        store.mark_needs_review(
            operation_id, f"FLOOD_WAIT during topic close: {exc}"
        )
        raise TopicCloseNeedsReview(str(exc)) from exc
    except TopicError:
        raise
    except Exception as exc:
        store.fail_operation(operation_id, str(exc))
        raise

    result = TopicCloseResult(
        telegram_chat_id=request.telegram_chat_id,
        telegram_topic_id=request.telegram_topic_id,
        status="closed",
        reason=request.reason,
    )
    op = store.complete_operation(operation_id, result.to_dict())
    return result, op
