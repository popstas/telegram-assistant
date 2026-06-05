"""Message-send domain shared by HTTP, CLI, and worker.

Two entry points:

* :func:`send_message` — targeted send to a single ``telegram_chat_id``
  (optionally inside a forum ``telegram_topic_id``).
* :func:`mass_send_message` — folder + topic-name mass mode. Iterates every
  chat in the resolved folder, looks up the named topic in each, and sends to
  matches. Chats without the topic are reported as ``skipped`` with
  ``reason=topic_not_found``.

Service commands (``/task 12345`` and friends) are recognized via
:func:`is_service_command`; their text is redacted in saved log/return
payloads via :func:`redact_message_text` so the body never leaks the
underlying id beyond the log line that carries the matching ``operation_id``.

Idempotency is per-send and anchored on the caller-supplied ``operation_id``
(plan's Technical Details: messages need an operator-supplied anchor since
they are not naturally pinned to a Planfix task id).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from telegram_assistant.folders import (
    FolderBackend,
    resolve_folder,
)
from telegram_assistant.persistence import idempotency
from telegram_assistant.persistence.models import (
    OperationRecord,
    OperationStatus,
)
from telegram_assistant.persistence.store import OperationStore
from telegram_assistant.topics import TopicBackend, TopicSummary
from telegram_assistant.worker.queue import FloodWaitError


class MessageSendFailed(RuntimeError):
    """A previous send attempt with this idempotency key already failed."""


class MessageSendPending(RuntimeError):
    """A concurrent send attempt with this idempotency key is in flight."""


class MessageSendNeedsReview(RuntimeError):
    """A previous send attempt resulted in ``needs_review``."""


def is_service_command(text: str) -> bool:
    """Return True for slash-prefixed service commands like ``/task 12345``.

    Used by callers to know they should redact ``text`` before logging.
    """
    if not text:
        return False
    stripped = text.lstrip()
    return stripped.startswith("/")


def redact_message_text(text: str) -> str:
    """Return a log-safe rendering of ``text``.

    For service commands like ``/task 12345`` the trailing argument is masked
    so the saved request payload + log line don't expose Planfix ids without
    operator context. Non-command messages are returned unchanged.
    """
    if not text:
        return text
    stripped = text.lstrip()
    if not stripped.startswith("/"):
        return text
    parts = stripped.split(None, 1)
    if len(parts) == 1:
        return text
    cmd, rest = parts
    if not rest:
        return text
    return f"{cmd} [redacted]"


class MessageBackend(Protocol):
    """Telethon-facing surface needed to send a message.

    Production wires this to the same Telethon adapter used by topics; tests
    inject a fake. The shape mirrors :class:`TopicBackend.send_message`.
    """

    async def send_message(
        self, *, chat_id: int, text: str, topic_id: int | None = None
    ) -> int:
        ...


@dataclass(frozen=True)
class SendMessageRequest:
    """Input to :func:`send_message`.

    Either ``telegram_chat_id`` is set (targeted send) or the caller resolved
    the chat upstream and passed the resolved id. ``operation_id`` anchors
    idempotency — supply the same one to replay the saved result without
    re-sending.
    """

    telegram_chat_id: int
    text: str
    telegram_topic_id: int | None = None
    operation_id: str | None = None
    chat_name: str | None = None
    topic_name: str | None = None

    def to_payload(self) -> dict[str, Any]:
        redacted = (
            redact_message_text(self.text)
            if is_service_command(self.text)
            else self.text
        )
        return {
            "telegram_chat_id": self.telegram_chat_id,
            "telegram_topic_id": self.telegram_topic_id,
            "text": redacted,
            "is_service_command": is_service_command(self.text),
            "operation_id": self.operation_id,
            "chat_name": self.chat_name,
            "topic_name": self.topic_name,
        }


@dataclass(frozen=True)
class SendMessageResult:
    """Result returned by :func:`send_message`."""

    telegram_chat_id: int
    telegram_topic_id: int | None
    telegram_message_id: int | None
    is_service_command: bool
    chat_name: str | None = None
    topic_name: str | None = None
    replayed: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "telegram_chat_id": self.telegram_chat_id,
            "telegram_topic_id": self.telegram_topic_id,
            "telegram_message_id": self.telegram_message_id,
            "is_service_command": self.is_service_command,
            "chat_name": self.chat_name,
            "topic_name": self.topic_name,
            "replayed": self.replayed,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> SendMessageResult:
        return cls(
            telegram_chat_id=int(payload["telegram_chat_id"]),
            telegram_topic_id=(
                int(payload["telegram_topic_id"])
                if payload.get("telegram_topic_id") is not None
                else None
            ),
            telegram_message_id=(
                int(payload["telegram_message_id"])
                if payload.get("telegram_message_id") is not None
                else None
            ),
            is_service_command=bool(payload.get("is_service_command", False)),
            chat_name=payload.get("chat_name"),
            topic_name=payload.get("topic_name"),
            replayed=True,
        )


async def send_message(
    *,
    backend: MessageBackend,
    store: OperationStore,
    request: SendMessageRequest,
) -> tuple[SendMessageResult, OperationRecord]:
    """Send a single message (or service command), or replay the saved result.

    State machine matches the other domain functions:

    * ``completed``    → return saved result with ``replayed=True``
    * ``failed``       → raise :class:`MessageSendFailed`
    * ``needs_review`` → raise :class:`MessageSendNeedsReview`
    * ``pending``      → raise :class:`MessageSendPending`
    """
    if not request.text or not request.text.strip():
        raise ValueError("send_message requires non-empty text")

    key = idempotency.message_send_key(
        telegram_chat_id=request.telegram_chat_id,
        telegram_topic_id=request.telegram_topic_id,
        operation_id=request.operation_id,
    )
    begin = store.begin_operation(
        operation_type=idempotency.MESSAGE_SEND,
        idempotency_key=key,
        request_payload=request.to_payload(),
    )

    if not begin.created:
        op = begin.operation
        if op.status is OperationStatus.COMPLETED:
            payload = op.result_payload or {}
            return SendMessageResult.from_dict(payload), op
        if op.status is OperationStatus.FAILED:
            raise MessageSendFailed(op.error or "previous attempt failed")
        if op.status is OperationStatus.NEEDS_REVIEW:
            raise MessageSendNeedsReview(
                op.error or "previous attempt needs review"
            )
        raise MessageSendPending(
            f"operation {op.id} is still pending; retry via 'operations retry'"
        )

    operation_id = begin.operation.id
    try:
        message_id = await backend.send_message(
            chat_id=request.telegram_chat_id,
            text=request.text,
            topic_id=request.telegram_topic_id,
        )
    except FloodWaitError as exc:
        # FLOOD_WAIT is transient — marking the op `failed` would lock the
        # idempotency key on a terminal state and the retry would surface
        # MessageSendFailed forever. Mark needs_review so an operator can
        # `operations retry` to reopen the slot.
        store.mark_needs_review(
            operation_id, f"FLOOD_WAIT during message send: {exc}"
        )
        raise MessageSendNeedsReview(str(exc)) from exc
    except Exception as exc:
        store.fail_operation(operation_id, str(exc))
        raise

    result = SendMessageResult(
        telegram_chat_id=request.telegram_chat_id,
        telegram_topic_id=request.telegram_topic_id,
        telegram_message_id=int(message_id) if message_id is not None else None,
        is_service_command=is_service_command(request.text),
        chat_name=request.chat_name,
        topic_name=request.topic_name,
    )
    op = store.complete_operation(operation_id, result.to_dict())
    return result, op


# ---------------------------------------------------------------------------
# Mass send (folder + topic_name)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MassSendRequest:
    """Input to :func:`mass_send_message`.

    ``folder_name`` + ``topic_name`` resolve to all chats in the folder that
    have a matching topic. Chats without the topic are reported as
    ``skipped`` with ``reason=topic_not_found`` rather than failing.
    """

    folder_name: str
    topic_name: str
    text: str
    folder_id: int | None = None
    operation_id: str | None = None


@dataclass(frozen=True)
class MassSendItemResult:
    """One row in the aggregated mass-send response.

    ``status`` is one of:

    * ``sent`` — message delivered this run
    * ``existed`` — replay of a previously-completed send under the same
      ``operation_id``
    * ``skipped`` — folder chat had no matching topic (``reason=topic_not_found``)
    * ``failed`` — terminal failure for this chat
    """

    status: str
    telegram_chat_id: int
    chat_name: str
    topic_name: str
    telegram_topic_id: int | None = None
    telegram_message_id: int | None = None
    reason: str | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "telegram_chat_id": self.telegram_chat_id,
            "chat_name": self.chat_name,
            "topic_name": self.topic_name,
            "telegram_topic_id": self.telegram_topic_id,
            "telegram_message_id": self.telegram_message_id,
            "reason": self.reason,
            "error": self.error,
        }


@dataclass(frozen=True)
class MassSendResult:
    """Aggregated outcome of :func:`mass_send_message`."""

    folder_name: str
    topic_name: str
    sent: int
    existed: int
    skipped: int
    failed: int
    items: list[MassSendItemResult]
    is_service_command: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "folder_name": self.folder_name,
            "topic_name": self.topic_name,
            "sent": self.sent,
            "existed": self.existed,
            "skipped": self.skipped,
            "failed": self.failed,
            "is_service_command": self.is_service_command,
            "items": [it.to_dict() for it in self.items],
        }


async def _match_topic(
    *,
    topic_backend: TopicBackend,
    telegram_chat_id: int,
    topic_name: str,
) -> TopicSummary | None:
    """Return the unique topic matching ``topic_name`` in the chat, or None.

    Multiple matches are treated as no match — the operator can't tell which
    one to use, so we surface that as ``skipped`` with a clear reason.
    ``FloodWaitError`` is re-raised so the caller can mark the chat as
    ``failed`` rather than silently misreporting a throttle as
    ``topic_not_found``.
    """
    topics = await topic_backend.list_topics(chat_id=telegram_chat_id)
    matches = [t for t in topics if t.title == topic_name]
    if len(matches) != 1:
        return None
    return matches[0]


async def mass_send_message(
    *,
    message_backend: MessageBackend,
    topic_backend: TopicBackend,
    folder_backend: FolderBackend,
    store: OperationStore,
    request: MassSendRequest,
) -> MassSendResult:
    """Send ``request.text`` to every chat in ``folder_name`` that has the
    matching ``topic_name``.

    Per-chat idempotency: each individual send is anchored on
    ``operation_id`` so re-running with the same ``operation_id`` replays
    rather than re-sending. Chats without the topic are reported as
    ``skipped``; ambiguous topic matches are also skipped (the operator can
    rename one of the duplicates and retry).
    """
    if not request.text or not request.text.strip():
        raise ValueError("mass send requires non-empty text")
    if not request.topic_name.strip():
        raise ValueError("mass send requires non-empty topic_name")

    snapshot = await resolve_folder(
        folder_backend,
        folder_name=request.folder_name,
        folder_id=request.folder_id,
    )

    items: list[MassSendItemResult] = []
    sent = existed = skipped = failed = 0
    service = is_service_command(request.text)

    for chat in snapshot.chats:
        try:
            match = await _match_topic(
                topic_backend=topic_backend,
                telegram_chat_id=chat.chat_id,
                topic_name=request.topic_name,
            )
        except FloodWaitError as exc:
            # A throttle on list_topics must not be reported as
            # ``topic_not_found`` — the caller would treat a transient pause
            # as a permanent skip and leave the chat unmessaged.
            failed += 1
            items.append(
                MassSendItemResult(
                    status="failed",
                    telegram_chat_id=chat.chat_id,
                    chat_name=chat.title,
                    topic_name=request.topic_name,
                    error=f"list_topics FLOOD_WAIT: {exc}",
                    reason="list_topics_flood_wait",
                )
            )
            continue
        except Exception as exc:
            failed += 1
            items.append(
                MassSendItemResult(
                    status="failed",
                    telegram_chat_id=chat.chat_id,
                    chat_name=chat.title,
                    topic_name=request.topic_name,
                    error=f"list_topics failed: {exc}",
                    reason="list_topics_failed",
                )
            )
            continue
        if match is None:
            skipped += 1
            items.append(
                MassSendItemResult(
                    status="skipped",
                    telegram_chat_id=chat.chat_id,
                    chat_name=chat.title,
                    topic_name=request.topic_name,
                    reason="topic_not_found",
                )
            )
            continue

        # Each chat gets its own idempotency anchor so a mass send is
        # restart-safe: a partial run resumes by replaying the chats already
        # sent and re-attempting the rest.
        per_chat_op_id = (
            f"{request.operation_id}:{chat.chat_id}:{match.topic_id}"
            if request.operation_id
            else None
        )
        send_req = SendMessageRequest(
            telegram_chat_id=chat.chat_id,
            text=request.text,
            telegram_topic_id=match.topic_id,
            operation_id=per_chat_op_id,
            chat_name=chat.title,
            topic_name=match.title,
        )
        try:
            result, _ = await send_message(
                backend=message_backend,
                store=store,
                request=send_req,
            )
        except (MessageSendFailed, MessageSendPending, MessageSendNeedsReview) as exc:
            failed += 1
            items.append(
                MassSendItemResult(
                    status="failed",
                    telegram_chat_id=chat.chat_id,
                    chat_name=chat.title,
                    topic_name=request.topic_name,
                    telegram_topic_id=match.topic_id,
                    error=str(exc),
                )
            )
            continue
        except Exception as exc:
            failed += 1
            items.append(
                MassSendItemResult(
                    status="failed",
                    telegram_chat_id=chat.chat_id,
                    chat_name=chat.title,
                    topic_name=request.topic_name,
                    telegram_topic_id=match.topic_id,
                    error=str(exc),
                )
            )
            continue

        if result.replayed:
            existed += 1
            status_str = "existed"
        else:
            sent += 1
            status_str = "sent"
        items.append(
            MassSendItemResult(
                status=status_str,
                telegram_chat_id=chat.chat_id,
                chat_name=chat.title,
                topic_name=match.title,
                telegram_topic_id=match.topic_id,
                telegram_message_id=result.telegram_message_id,
            )
        )

    return MassSendResult(
        folder_name=snapshot.folder_name,
        topic_name=request.topic_name,
        sent=sent,
        existed=existed,
        skipped=skipped,
        failed=failed,
        items=items,
        is_service_command=service,
    )


__all__ = [
    "MassSendItemResult",
    "MassSendRequest",
    "MassSendResult",
    "MessageBackend",
    "MessageSendFailed",
    "MessageSendNeedsReview",
    "MessageSendPending",
    "SendMessageRequest",
    "SendMessageResult",
    "is_service_command",
    "mass_send_message",
    "redact_message_text",
    "send_message",
]
