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

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Protocol

from telegram_assistant.access.service import AccessDenied, AccessLevel, Authorizer
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


class ScheduleError(ValueError):
    """Invalid scheduling input — bad delay, conflicting modes, or a past time.

    Surface layers map this to CLI exit code 2 / HTTP 400. Kept distinct from
    plain :class:`ValueError` so callers can translate it without catching every
    validation error.
    """


_DELAY_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_delay(value: str) -> int:
    """Parse a relative delay like ``10m``/``2h``/``1d``/``30s`` into seconds.

    The magnitude must be a positive integer and the trailing unit one of
    ``s``/``m``/``h``/``d``. Anything else raises :class:`ScheduleError`.
    """
    text = (value or "").strip().lower()
    if not text:
        raise ScheduleError("delay must be a non-empty duration like '10m', '2h', '1d'")
    unit = text[-1]
    if unit not in _DELAY_UNITS:
        raise ScheduleError(
            f"invalid delay {value!r}: must end with one of s, m, h, d "
            "(e.g. '10m', '2h', '1d')"
        )
    magnitude = text[:-1]
    if not magnitude.isdigit():
        raise ScheduleError(
            f"invalid delay {value!r}: magnitude must be a positive integer"
        )
    amount = int(magnitude)
    if amount <= 0:
        raise ScheduleError(f"invalid delay {value!r}: must be a positive duration")
    return amount * _DELAY_UNITS[unit]


def parse_schedule_at(value: str) -> datetime:
    """Parse an ISO-8601 ``--schedule-at`` value into a :class:`datetime`.

    Invalid input raises :class:`ScheduleError`. Whether the time is in the
    future is checked separately by :func:`resolve_schedule_at`.
    """
    try:
        return datetime.fromisoformat((value or "").strip())
    except ValueError as exc:
        raise ScheduleError(
            f"invalid schedule_at {value!r}: expected an ISO-8601 datetime"
        ) from exc


def resolve_schedule_at(
    *,
    schedule_at: datetime | None = None,
    delay_seconds: int | None = None,
    now: datetime | None = None,
) -> datetime | None:
    """Resolve the two scheduling modes into a single future ``datetime``.

    Exactly one of ``schedule_at`` / ``delay_seconds`` may be supplied. A delay
    is added to ``now`` (defaulting to the current time, matching the tz of an
    aware reference); an absolute ``schedule_at`` must be strictly in the future.
    Returns ``None`` when no scheduling was requested.
    """
    if schedule_at is not None and delay_seconds is not None:
        raise ScheduleError("provide only one of schedule_at or delay")
    if delay_seconds is not None:
        if delay_seconds <= 0:
            raise ScheduleError("delay must be a positive duration")
        base = now if now is not None else datetime.now()
        return base + timedelta(seconds=delay_seconds)
    if schedule_at is not None:
        reference = now
        if reference is None:
            reference = (
                datetime.now(schedule_at.tzinfo)
                if schedule_at.tzinfo is not None
                else datetime.now()
            )
        if schedule_at <= reference:
            raise ScheduleError("schedule_at must be in the future")
        return schedule_at
    return None


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


def _validate_attachment_refs(refs: tuple[str, ...], *, kind: str) -> None:
    """Reject empty/blank attachment references.

    Only checks that each reference is a non-empty string; existence and
    URL-scheme validation belong to the surface layer (CLI/HTTP) which has the
    filesystem and request context the pure domain deliberately avoids.
    """
    for ref in refs:
        if not ref or not str(ref).strip():
            raise ValueError(
                f"send_message {kind} entries must be non-empty references"
            )


def _normalize_message_ids(
    raw: int | list[int] | tuple[int, ...] | None,
) -> tuple[int | None, tuple[int, ...] | None]:
    """Split a backend send result into (primary_id, all_ids).

    A scalar id (plain/single send) yields ``(id, None)``. A sequence (album)
    yields ``(first, full_tuple)``. ``None``/empty yields ``(None, None)``.
    """
    if raw is None:
        return None, None
    if isinstance(raw, (list, tuple)):
        ids = tuple(int(x) for x in raw)
        if not ids:
            return None, None
        if len(ids) == 1:
            return ids[0], None
        return ids[0], ids
    return int(raw), None


class MessageBackend(Protocol):
    """Telethon-facing surface needed to send a message.

    Production wires this to the Telethon message adapter; tests inject a fake.
    The base shape mirrors :class:`TopicBackend.send_message` and stays
    backward-compatible: ``files`` and ``schedule_at`` are optional, so a
    text-only send is called exactly as before.

    Return value is a single message id for a plain/text send and a list of
    ids for an album (multiple attachments). The service normalises both into
    :class:`SendMessageResult`.
    """

    async def send_message(
        self,
        *,
        chat_id: int,
        text: str,
        topic_id: int | None = None,
        files: tuple[str, ...] = (),
        schedule_at: datetime | None = None,
    ) -> int | list[int]:
        ...


@dataclass(frozen=True)
class RecentMessage:
    """One message returned by the get-recent read op.

    ``sender`` is the sender's ``@username`` when known (``None`` otherwise);
    ``date`` is an ISO-8601 timestamp string (``None`` when the backend can't
    supply one); ``reply_to`` is the replied-to message id; ``text`` is the
    message body or, for media-only messages, a short media summary.
    """

    id: int
    sender: str | None
    date: str | None
    reply_to: int | None
    text: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "sender": self.sender,
            "date": self.date,
            "reply_to": self.reply_to,
            "text": self.text,
        }


class MessageReadBackend(Protocol):
    """Telethon-facing surface needed to read recent messages.

    Production wires this to a Telethon adapter; tests inject a fake. The op
    that consumes it (:func:`get_recent_messages`) is the canonical READ-level
    operation the access gate protects.
    """

    async def get_recent_messages(
        self, *, chat_id: int, limit: int = 5
    ) -> list[RecentMessage]:
        ...


@dataclass(frozen=True)
class SendMessageRequest:
    """Input to :func:`send_message`.

    Either ``telegram_chat_id`` is set (targeted send) or the caller resolved
    the chat upstream and passed the resolved id. ``operation_id`` anchors
    idempotency — supply the same one to replay the saved result without
    re-sending.

    Attachments are optional. ``files`` are server-side paths (or anything
    Telethon accepts as a local file) and ``file_urls`` are ``http``/``https``
    URLs handed to Telethon as-is. Both are combined into one attachment list
    in declaration order (``files`` then ``file_urls``); supplying more than
    one attachment sends an album. ``schedule_at`` defers delivery to a future
    time. ``text`` doubles as the caption when attachments are present and may
    be empty for a media-only send.
    """

    telegram_chat_id: int
    text: str
    telegram_topic_id: int | None = None
    operation_id: str | None = None
    chat_name: str | None = None
    topic_name: str | None = None
    files: tuple[str, ...] = field(default_factory=tuple)
    file_urls: tuple[str, ...] = field(default_factory=tuple)
    schedule_at: datetime | None = None

    @property
    def attachment_refs(self) -> tuple[str, ...]:
        """All attachments as one ordered list (``files`` then ``file_urls``)."""
        return tuple(self.files) + tuple(self.file_urls)

    @property
    def has_attachments(self) -> bool:
        return bool(self.files or self.file_urls)

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
            # Only attachment *references* are persisted, never file contents.
            "files": list(self.files),
            "file_urls": list(self.file_urls),
            "schedule_at": (
                self.schedule_at.isoformat()
                if self.schedule_at is not None
                else None
            ),
        }


@dataclass(frozen=True)
class SendMessageResult:
    """Result returned by :func:`send_message`.

    ``telegram_message_id`` is the primary id (the first message of an album).
    ``telegram_message_ids`` carries the full ordered list when the send
    produced more than one message (album); it is ``None`` for single sends.
    ``scheduled`` is ``True`` when the message was deferred via ``schedule_at``.
    """

    telegram_chat_id: int
    telegram_topic_id: int | None
    telegram_message_id: int | None
    is_service_command: bool
    chat_name: str | None = None
    topic_name: str | None = None
    replayed: bool = False
    telegram_message_ids: tuple[int, ...] | None = None
    scheduled: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "telegram_chat_id": self.telegram_chat_id,
            "telegram_topic_id": self.telegram_topic_id,
            "telegram_message_id": self.telegram_message_id,
            "is_service_command": self.is_service_command,
            "chat_name": self.chat_name,
            "topic_name": self.topic_name,
            "replayed": self.replayed,
            "telegram_message_ids": (
                list(self.telegram_message_ids)
                if self.telegram_message_ids is not None
                else None
            ),
            "scheduled": self.scheduled,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> SendMessageResult:
        raw_ids = payload.get("telegram_message_ids")
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
            telegram_message_ids=(
                tuple(int(x) for x in raw_ids) if raw_ids is not None else None
            ),
            scheduled=bool(payload.get("scheduled", False)),
        )


async def send_message(
    *,
    backend: MessageBackend,
    store: OperationStore,
    request: SendMessageRequest,
    authorizer: Authorizer | None = None,
) -> tuple[SendMessageResult, OperationRecord]:
    """Send a single message (or service command), or replay the saved result.

    State machine matches the other domain functions:

    * ``completed``    → return saved result with ``replayed=True``
    * ``failed``       → raise :class:`MessageSendFailed`
    * ``needs_review`` → raise :class:`MessageSendNeedsReview`
    * ``pending``      → raise :class:`MessageSendPending`

    Sending is a WRITE op: when an ``authorizer`` is supplied it must grant
    WRITE on the target chat or :class:`AccessDenied` is raised before any
    operation row is created.

    A send needs either non-empty ``text`` or at least one attachment. Media
    sends pass ``files``/``schedule_at`` through to the backend; a plain text
    send calls the backend exactly as before for backward compatibility.
    """
    has_text = bool(request.text and request.text.strip())
    if not has_text and not request.has_attachments:
        raise ValueError(
            "send_message requires non-empty text or at least one attachment"
        )
    _validate_attachment_refs(request.files, kind="files")
    _validate_attachment_refs(request.file_urls, kind="file_urls")

    if authorizer is not None:
        await authorizer.require(request.telegram_chat_id, AccessLevel.WRITE)

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
    # Only pass the media/schedule kwargs when set so a plain text send hits
    # the backend with the exact original signature (backward compatible with
    # text-only backends that predate attachments).
    extra: dict[str, Any] = {}
    if request.attachment_refs:
        extra["files"] = request.attachment_refs
    if request.schedule_at is not None:
        extra["schedule_at"] = request.schedule_at
    try:
        raw_id = await backend.send_message(
            chat_id=request.telegram_chat_id,
            text=request.text,
            topic_id=request.telegram_topic_id,
            **extra,
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

    primary_id, all_ids = _normalize_message_ids(raw_id)
    result = SendMessageResult(
        telegram_chat_id=request.telegram_chat_id,
        telegram_topic_id=request.telegram_topic_id,
        telegram_message_id=primary_id,
        is_service_command=is_service_command(request.text),
        chat_name=request.chat_name,
        topic_name=request.topic_name,
        telegram_message_ids=all_ids,
        scheduled=request.schedule_at is not None,
    )
    op = store.complete_operation(operation_id, result.to_dict())
    return result, op


# ---------------------------------------------------------------------------
# Get recent (read op)
# ---------------------------------------------------------------------------


async def get_recent_messages(
    *,
    backend: MessageReadBackend,
    chat_id: int,
    limit: int = 5,
    authorizer: Authorizer | None = None,
) -> list[RecentMessage]:
    """Return up to ``limit`` recent messages for ``chat_id``, newest first.

    This is a READ op: when an ``authorizer`` is supplied it must grant READ on
    the target chat or :class:`AccessDenied` is raised before any Telegram call.
    ``limit`` defaults to 5 and must be positive.
    """
    if limit <= 0:
        raise ValueError("get_recent_messages requires a positive limit")

    if authorizer is not None:
        await authorizer.require(chat_id, AccessLevel.READ)

    return await backend.get_recent_messages(chat_id=chat_id, limit=limit)


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
    authorizer: Authorizer | None = None,
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
        # Mass send is WRITE per resolved chat. Chats the policy doesn't permit
        # to write are recorded as skipped with reason=access_denied rather than
        # aborting the whole run, so a partial allowlist still delivers to the
        # chats it covers.
        if authorizer is not None:
            try:
                await authorizer.require(
                    chat.chat_id,
                    AccessLevel.WRITE,
                    folder_memberships=[snapshot.folder_name],
                )
            except AccessDenied:
                skipped += 1
                items.append(
                    MassSendItemResult(
                        status="skipped",
                        telegram_chat_id=chat.chat_id,
                        chat_name=chat.title,
                        topic_name=request.topic_name,
                        reason="access_denied",
                    )
                )
                continue

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
    "MessageReadBackend",
    "MessageSendFailed",
    "MessageSendNeedsReview",
    "MessageSendPending",
    "RecentMessage",
    "ScheduleError",
    "SendMessageRequest",
    "SendMessageResult",
    "get_recent_messages",
    "is_service_command",
    "mass_send_message",
    "parse_delay",
    "parse_schedule_at",
    "redact_message_text",
    "resolve_schedule_at",
    "send_message",
]
