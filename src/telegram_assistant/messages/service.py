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

import os
import shutil
from collections.abc import Iterable
from contextlib import suppress
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from telegram_assistant.access.service import AccessDenied, AccessLevel, Authorizer
from telegram_assistant.folders import (
    FolderBackend,
    resolve_folder,
)
from telegram_assistant.messages import media_probe
from telegram_assistant.messages.attachments import (
    DEFAULT_MAX_BASE64_BYTES,
    Base64Attachment,
    decode_base64_attachment,
    materialize_base64_attachments,
)
from telegram_assistant.messages.downloads import Downloader
from telegram_assistant.messages.rich_markdown import (
    DEFAULT_MEDIA_GROUP_MODE,
    MAX_RICH_MEDIA,
    RICH_FILE_ID_RE,
    RICH_FILE_SCHEMES,
    MediaGroupChoice,
    RichFile,
    normalize_rich_markdown,
)
from telegram_assistant.messages.sent_registry import SentMessageRegistry
from telegram_assistant.persistence import idempotency
from telegram_assistant.persistence.models import (
    OperationRecord,
    OperationStatus,
)
from telegram_assistant.persistence.store import OperationStore
from telegram_assistant.topics import TopicBackend, TopicSummary
from telegram_assistant.worker.queue import FloodWaitError

#: Server-side ceiling for a rich message's markdown source, in characters.
#: Telegram accepts exactly this many (verified in the Task 1 spike:
#: 32 768 was delivered, 32 769 answered ``RICH_MESSAGE_TEXT_TOO_LONG``), so
#: the bound is inclusive. Everything else about the markdown — block count,
#: nesting, table width — is left to the server, whose errors are more
#: authoritative than any local lint.
MAX_RICH_MARKDOWN_CHARS = 32_768


def spaced_paragraphs_default(config: Any) -> bool:
    """Read ``telegram.defaults.rich_markdown_spaced_paragraphs`` from ``config``.

    Every surface builds a :class:`SendMessageRequest` from its own plumbing, so
    the config lookup lives here rather than three times over. Missing config
    (or a config predating the knob) means the built-in default, ``True`` — the
    knob only lets an operator turn spacing off globally; a per-call flag still
    wins over it.
    """
    defaults = getattr(getattr(config, "telegram", None), "defaults", None)
    value = getattr(defaults, "rich_markdown_spaced_paragraphs", None)
    return True if value is None else bool(value)


def line_breaks_default(config: Any) -> bool:
    """Read ``telegram.defaults.rich_markdown_line_breaks`` from ``config``.

    The twin of :func:`spaced_paragraphs_default`, with the same tolerance for a
    config predating the knob: the built-in default is ``True``, and a per-call
    flag still wins over it.
    """
    defaults = getattr(getattr(config, "telegram", None), "defaults", None)
    value = getattr(defaults, "rich_markdown_line_breaks", None)
    return True if value is None else bool(value)


def media_grouping_default(config: Any) -> str:
    """Read ``telegram.defaults.rich_markdown_grouping`` from ``config``.

    The twin of :func:`spaced_paragraphs_default`: missing config (or a config
    predating the knob) means :data:`DEFAULT_MEDIA_GROUP_MODE`. A per-group
    override still wins over whatever this returns.
    """
    defaults = getattr(getattr(config, "telegram", None), "defaults", None)
    value = getattr(defaults, "rich_markdown_grouping", None)
    return DEFAULT_MEDIA_GROUP_MODE if value is None else str(value)


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
        base = now if now is not None else datetime.now(UTC)
        try:
            return base + timedelta(seconds=delay_seconds)
        except OverflowError as exc:
            raise ScheduleError("delay is too large") from exc
    if schedule_at is not None:
        if schedule_at.tzinfo is None:
            schedule_at = schedule_at.replace(tzinfo=UTC)
        reference = now
        if reference is None:
            reference = datetime.now(schedule_at.tzinfo)
        elif reference.tzinfo is None and schedule_at.tzinfo is not None:
            reference = reference.replace(tzinfo=schedule_at.tzinfo)
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


class RichMessageUnsupported(RuntimeError):
    """The installed Telethon cannot build a rich-message body.

    A deployment error, not an idempotency state — nothing was attempted before.
    Reporting it as :class:`MessageSendFailed` would travel the
    ``previous_attempt_failed`` taxonomy (HTTP/MCP 409, CLI exit 2), telling the
    caller a *previous* attempt is at fault and hiding the actual fix: upgrade
    to telethon >= 1.44 (layer 227).
    """


class RichMediaForbidden(ValueError):
    """The chat forbids the media the rich message carries.

    A rich send is all-or-nothing — the article's media blocks are part of its
    body, so there is no media-less half to retry — and the caller decides what
    to do next, exactly like a server-rejected article. Deliberately a
    ``ValueError`` so every surface's existing 400 / exit-2 path carries the
    message naming the chat, instead of the bare 500 an unmapped ``RuntimeError``
    would produce.
    """


class MessageSendUnconfirmed(RuntimeError):
    """A send left no readable message id, so delivery is uncertain.

    Raised by a backend that issued the request without error but could not find
    the new id in the response envelope. :func:`send_message` turns it into
    ``needs_review`` rather than ``failed``: the message may well have been
    delivered, and a terminal ``failed`` tells the caller nothing happened —
    which invites a re-send under a fresh key and duplicates the message.
    """


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


def _validate_rich_files(
    rich_files: tuple[RichFile, ...], *, has_rich_markdown: bool
) -> None:
    """Reject rich-message uploads the send could not possibly complete.

    Unlike plain attachments, these *are* checked against the filesystem here:
    the ids are already written into the markdown, so a missing file would send
    an article whose media reference points at nothing. Failing before the
    operation row is opened keeps the idempotency key free for the fixed retry.

    The ``.gif`` check belongs here rather than in the backend for the same
    reason: the conversion is what makes the file attachable at all, so a box
    with no ffmpeg must fail before the operation row exists.
    """

    if not rich_files:
        return
    if not has_rich_markdown:
        raise ValueError("rich_files requires rich_markdown")
    if len(rich_files) > MAX_RICH_MEDIA:
        raise ValueError(
            f"rich_files exceeds Telegram's {MAX_RICH_MEDIA} media attachments "
            f"({len(rich_files)} given)"
        )
    seen: set[str] = set()
    for rich_file in rich_files:
        # ``fullmatch``: the pattern's ``$`` also matches before a trailing
        # newline, so ``match`` would pass an id Telegram rejects with
        # ``RICH_MESSAGE_FILE_ID_INVALID`` after the operation row is opened.
        if not rich_file.id or not RICH_FILE_ID_RE.fullmatch(rich_file.id):
            raise ValueError(
                f"rich_files id must match {RICH_FILE_ID_RE.pattern} "
                f"({rich_file.id!r} given)"
            )
        if rich_file.id in seen:
            raise ValueError(f"duplicate rich_files id: {rich_file.id}")
        seen.add(rich_file.id)
        if rich_file.kind not in RICH_FILE_SCHEMES:
            raise ValueError(
                f"rich_files kind must be one of "
                f"{', '.join(sorted(RICH_FILE_SCHEMES))} ({rich_file.kind!r} given)"
            )
        if not os.path.isfile(rich_file.path):
            raise ValueError(f"rich_files entry is not a file: {rich_file.path}")
        if not os.access(rich_file.path, os.R_OK):
            raise ValueError(f"rich_files entry is not readable: {rich_file.path}")
        if (
            os.path.splitext(rich_file.path)[1].lower() == ".gif"
            and not media_probe.ffmpeg_available()
        ):
            raise ValueError(
                f"rich_files entry needs ffmpeg: {rich_file.path} is a GIF, and "
                f"Telegram only attaches one to an article as an mp4 — install "
                f"ffmpeg or convert the file to mp4 yourself"
            )


def _coerce_message_id(raw: Any) -> int:
    msg_id = int(raw)
    if msg_id <= 0:
        raise ValueError(f"backend returned invalid message id: {raw!r}")
    return msg_id


def _normalize_message_ids(
    raw: int | list[int] | tuple[int, ...] | None,
) -> tuple[int | None, tuple[int, ...] | None]:
    """Split a backend send result into (primary_id, all_ids).

    A scalar id (plain/single send) yields ``(id, None)``. A sequence (album)
    yields ``(first, full_tuple)``. Missing, empty, or non-positive ids are
    invalid backend results and fail the operation.
    """
    if raw is None:
        raise ValueError("backend returned no message id")
    if isinstance(raw, (list, tuple)):
        ids = tuple(_coerce_message_id(x) for x in raw)
        if not ids:
            raise ValueError("backend returned no message ids")
        if len(ids) == 1:
            return ids[0], None
        return ids[0], ids
    return _coerce_message_id(raw), None


class MessageBackend(Protocol):
    """Telethon-facing surface needed to send a message.

    Production wires this to the Telethon message adapter; tests inject a fake.
    The base shape mirrors :class:`TopicBackend.send_message` and stays
    backward-compatible: ``files`` and ``schedule_at`` are optional, so a
    text-only send is called exactly as before.

    Return value is a single message id for a plain/text send and a list of
    ids for an album (multiple attachments). The service normalises both into
    :class:`SendMessageResult`.

    ``rich_markdown`` sends a Telegram *rich message* (article) whose markdown
    source the server parses itself; it is mutually exclusive with ``text`` and
    ``files``, and — like the other optional kwargs — the service only passes it
    when set, so backends predating rich sends keep working. ``rich_files`` are
    the local files that markdown names through ``tg://photo?id=…`` and friends;
    it is likewise only passed when non-empty.
    """

    async def send_message(
        self,
        *,
        chat_id: int,
        text: str,
        topic_id: int | None = None,
        files: tuple[str, ...] = (),
        schedule_at: datetime | None = None,
        reply_to_message_id: int | None = None,
        rich_markdown: str | None = None,
        rich_files: tuple[RichFile, ...] = (),
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
    be empty for a media-only send. ``reply_to_message_id`` threads the send as
    a reply to an existing message; in a forum it takes precedence over the
    topic root for the Telethon ``reply_to`` (replying to a message inside a
    topic keeps the reply in that topic).

    ``base64_files`` are small inline attachments supplied as base64 content
    (``{filename, mime, content_b64}``); each is decoded to a temp file under
    ``base64_max_bytes`` (default 1 MB) and cleaned up after the send. They are
    appended to the attachment list after ``files`` and ``file_urls``.

    ``rich_markdown`` turns the send into a Telegram *rich message* (article):
    the markdown source is handed to the server, which parses it into the
    article blocks (headings, tables, quotes, code, media by public URL). It is
    mutually exclusive with ``text`` and every attachment kind, and is bounded
    by :data:`MAX_RICH_MARKDOWN_CHARS`; targeting, ``topic_id``,
    ``reply_to_message_id`` and ``schedule_at`` all apply as usual.

    ``spaced_paragraphs`` (default ``True``, config default
    ``telegram.defaults.rich_markdown_spaced_paragraphs``) applies only to a
    rich send: the markdown is run through
    :func:`~telegram_assistant.messages.rich_markdown.normalize_rich_markdown`,
    which inserts a spacer paragraph where Telegram would otherwise render two
    paragraphs tight against each other. ``False`` sends the source
    byte-for-byte.

    ``line_breaks`` (default ``True``, config default
    ``telegram.defaults.rich_markdown_line_breaks``) is the same pass's third
    half: each line of a top-level paragraph becomes its own paragraph, so the
    single newlines an author wrote survive instead of being folded into
    spaces by the server's markdown parser.

    ``media_grouping`` (config default ``telegram.defaults.rich_markdown_grouping``)
    is the same pass's other half: a run of two or more consecutive media
    blocks is wrapped in ``<tg-collage>``/``<tg-slideshow>``, or left alone
    with ``none``. ``media_groups`` overrides individual runs by index (see
    :attr:`~telegram_assistant.messages.rich_markdown.RichMarkdownNormalization.groups`);
    an index naming no run raises
    :class:`~telegram_assistant.messages.rich_markdown.MediaGroupError`.

    ``rich_files`` are local files the ``rich_markdown`` body names through the
    ``tg://photo?id=…``/``tg://video?id=…``/``tg://audio?id=…`` references that
    :func:`~telegram_assistant.messages.rich_markdown.scan_media` writes. They
    are uploaded by the backend and bound to those ids; the field is only valid
    alongside ``rich_markdown`` and is bounded by
    :data:`~telegram_assistant.messages.rich_markdown.MAX_RICH_MEDIA`.
    """

    telegram_chat_id: int
    text: str
    telegram_topic_id: int | None = None
    operation_id: str | None = None
    chat_name: str | None = None
    topic_name: str | None = None
    files: tuple[str, ...] = field(default_factory=tuple)
    file_urls: tuple[str, ...] = field(default_factory=tuple)
    base64_files: tuple[Base64Attachment, ...] = field(default_factory=tuple)
    base64_max_bytes: int = DEFAULT_MAX_BASE64_BYTES
    schedule_at: datetime | None = None
    reply_to_message_id: int | None = None
    rich_markdown: str | None = None
    spaced_paragraphs: bool = True
    line_breaks: bool = True
    media_grouping: str = DEFAULT_MEDIA_GROUP_MODE
    media_groups: tuple[MediaGroupChoice, ...] = field(default_factory=tuple)
    rich_files: tuple[RichFile, ...] = field(default_factory=tuple)

    @property
    def attachment_refs(self) -> tuple[str, ...]:
        """All ref-style attachments in order (``files`` then ``file_urls``).

        Base64 attachments carry content, not a reference, so they are not
        included here; the send path materialises them to temp files separately.
        """
        return tuple(self.files) + tuple(self.file_urls)

    @property
    def has_attachments(self) -> bool:
        return bool(self.files or self.file_urls or self.base64_files)

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
            # Base64 attachments: persist filename/mime metadata only, never the
            # base64 content itself.
            "base64_files": [
                {"filename": att.filename, "mime": att.mime}
                for att in self.base64_files
            ],
            "schedule_at": (
                self.schedule_at.isoformat()
                if self.schedule_at is not None
                else None
            ),
            "reply_to_message_id": self.reply_to_message_id,
            # The markdown source is persisted like ``text`` (same redaction
            # rule) so the audit trail shows what a replayed op actually sent.
            # ``send_message`` normalises the markdown *before* building this
            # payload, so what is recorded is what went to Telegram, spacers
            # included.
            "rich_markdown": (
                None
                if self.rich_markdown is None
                else (
                    redact_message_text(self.rich_markdown)
                    if is_service_command(self.rich_markdown)
                    else self.rich_markdown
                )
            ),
            "spaced_paragraphs": self.spaced_paragraphs,
            "line_breaks": self.line_breaks,
            "media_grouping": self.media_grouping,
            "media_groups": [
                {"index": choice.index, "mode": choice.mode}
                for choice in self.media_groups
            ],
            # Like ``files``: only the *reference* to each upload is persisted
            # (id, path, kind), never the file contents. The caption is already
            # in the recorded markdown, so it is not duplicated here.
            "rich_files": [
                {"id": rf.id, "path": rf.path, "kind": rf.kind}
                for rf in self.rich_files
            ],
        }


@dataclass(frozen=True)
class SendMessageResult:
    """Result returned by :func:`send_message`.

    ``telegram_message_id`` is the primary id (the first message of an album).
    ``telegram_message_ids`` carries the full ordered list when the send
    produced more than one message (album); it is ``None`` for single sends.
    ``scheduled`` is ``True`` when the message was deferred via ``schedule_at``.
    ``warnings`` are non-fatal notes about the send — currently the
    rich-markdown normalization's ones (block/media budget, spacing rolled
    back). They never mean the send failed.
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
    schedule_at: str | None = None
    warnings: tuple[str, ...] = ()

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
            "schedule_at": self.schedule_at,
            "warnings": list(self.warnings),
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
            schedule_at=payload.get("schedule_at"),
            # Rows written before warnings existed simply carry none.
            warnings=tuple(payload.get("warnings") or ()),
        )


async def send_message(
    *,
    backend: MessageBackend,
    store: OperationStore,
    request: SendMessageRequest,
    authorizer: Authorizer | None = None,
    sent_registry: SentMessageRegistry | None = None,
    downloader: Downloader | None = None,
) -> tuple[SendMessageResult, OperationRecord]:
    """Send a single message (or service command), or replay the saved result.

    State machine matches the other domain functions:

    * ``completed``    → return saved result with ``replayed=True``
    * ``failed``       → raise :class:`MessageSendFailed`
    * ``needs_review`` → raise :class:`MessageSendNeedsReview`
    * ``pending``      → raise :class:`MessageSendPending`

    A backend :class:`MessageSendUnconfirmed` is quarantined as ``needs_review``
    rather than ``failed``: the message may have been delivered, so the caller
    must not be told nothing happened.

    Sending is a WRITE op: when an ``authorizer`` is supplied it must grant
    WRITE on the target chat or :class:`AccessDenied` is raised before any
    operation row is created.

    When a ``sent_registry`` is supplied, the id(s) of a freshly-sent message
    are recorded so the session-limited delete op can later recognise them.
    Recording is best-effort and only happens for fresh sends, never replays.

    When a ``downloader`` is supplied, each ``file_urls`` entry is downloaded to
    a local temp file (bounded by size + time) and the local path — not the
    URL — is handed to the backend; the temp files are removed in a ``finally``
    after the send. A download failure marks the operation ``failed`` and
    propagates. Without a ``downloader`` the URLs are passed through to the
    backend unchanged (backward compatible).

    ``base64_files`` are decoded and validated up front (a bad/oversize payload
    raises before any operation row is created), then materialised to temp files
    after the operation begins and cleaned up in the ``finally``.

    A send needs either non-empty ``text`` or at least one attachment. Media
    sends pass ``files``/``schedule_at`` through to the backend; a plain text
    send calls the backend exactly as before for backward compatibility.
    """
    has_text = bool(request.text and request.text.strip())
    warnings: tuple[str, ...] = ()
    if request.rich_markdown is not None:
        # A rich message carries its whole body in the markdown source; a
        # caption or attachment alongside it would be silently dropped by the
        # server, so reject the combination instead of half-sending it.
        if has_text or request.has_attachments:
            raise ValueError(
                "rich_markdown cannot be combined with text or attachments"
            )
        if not request.rich_markdown.strip():
            raise ValueError("rich_markdown must be non-empty")
        # Bound the *source* before normalising it. Both passes only ever grow
        # the text, so a source already over the limit can never come back
        # under it — and normalisation is a full line-by-line scan of caller
        # input that runs on the event loop, before the WRITE gate. Without
        # this, any token holder could hand a multi-megabyte string to
        # HTTP/MCP (neither bounds the field) and block the loop for seconds
        # on a send it was never authorized to make.
        if len(request.rich_markdown) > MAX_RICH_MARKDOWN_CHARS:
            raise ValueError(
                f"rich_markdown exceeds {MAX_RICH_MARKDOWN_CHARS} characters "
                f"({len(request.rich_markdown)} given)"
            )
        # Normalise *before* the length check: spacer insertion grows the
        # source, and MAX_RICH_MARKDOWN_CHARS must bound what actually reaches
        # Telegram — not what the caller happened to hand in. From here on
        # ``request`` carries the normalised markdown, so the operation payload,
        # the backend kwarg and the length check all agree on one text.
        normalization = normalize_rich_markdown(
            request.rich_markdown,
            spaced_paragraphs=request.spaced_paragraphs,
            line_breaks=request.line_breaks,
            grouping=request.media_grouping,
            media_groups=request.media_groups,
        )
        warnings = normalization.warnings
        if normalization.markdown is not request.rich_markdown:
            request = replace(request, rich_markdown=normalization.markdown)
        if len(normalization.markdown) > MAX_RICH_MARKDOWN_CHARS:
            given = f"{len(normalization.markdown)} given"
            # Name the pass(es) that grew it, so a source that was already too
            # long is not blamed on normalisation.
            grew_by = [
                name
                for name, applied in (
                    ("paragraph spacing", normalization.spacers_added),
                    ("line splitting", normalization.lines_split),
                    ("media grouping", normalization.grouped),
                )
                if applied
            ]
            if grew_by:
                given += " after " + " and ".join(grew_by)
            raise ValueError(
                f"rich_markdown exceeds {MAX_RICH_MARKDOWN_CHARS} characters "
                f"({given})"
            )
    elif not has_text and not request.has_attachments:
        raise ValueError(
            "send_message requires non-empty text or at least one attachment"
        )
    _validate_rich_files(request.rich_files, has_rich_markdown=request.rich_markdown is not None)
    _validate_attachment_refs(request.files, kind="files")
    _validate_attachment_refs(request.file_urls, kind="file_urls")
    # Validate base64 attachments before opening the operation so malformed or
    # oversize input surfaces as a clean error without poisoning the idempotency
    # key. The bytes are materialised to temp files only after the op begins.
    for att in request.base64_files:
        decode_base64_attachment(
            filename=att.filename,
            content_b64=att.content_b64,
            mime=att.mime,
            max_bytes=request.base64_max_bytes,
        )

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
    # When a downloader is supplied, ``file_urls`` are fetched to local temp
    # files first and the local paths replace the URLs in the attachment list;
    # base64 attachments are decoded into a temp dir. Both are cleaned up in the
    # ``finally`` once the send is done.
    downloaded_paths: list[str] = []
    base64_tmpdir: str | None = None
    try:
        try:
            if request.file_urls and downloader is not None:
                for url in request.file_urls:
                    downloaded_paths.append(await downloader(url))
                url_refs = tuple(downloaded_paths)
            else:
                url_refs = tuple(request.file_urls)
            base64_tmpdir, base64_paths = materialize_base64_attachments(
                request.base64_files,
                max_bytes=request.base64_max_bytes,
            )
            attachment_refs = (
                tuple(request.files) + url_refs + tuple(base64_paths)
            )
            # Only pass the media/schedule kwargs when set so a plain text send
            # hits the backend with the exact original signature (backward
            # compatible with text-only backends that predate attachments).
            extra: dict[str, Any] = {}
            if attachment_refs:
                extra["files"] = attachment_refs
            if request.schedule_at is not None:
                extra["schedule_at"] = request.schedule_at
            if request.reply_to_message_id is not None:
                extra["reply_to_message_id"] = request.reply_to_message_id
            if request.rich_markdown is not None:
                extra["rich_markdown"] = request.rich_markdown
            if request.rich_files:
                extra["rich_files"] = request.rich_files
            raw_id = await backend.send_message(
                chat_id=request.telegram_chat_id,
                text=request.text,
                topic_id=request.telegram_topic_id,
                **extra,
            )
            primary_id, all_ids = _normalize_message_ids(raw_id)
        except FloodWaitError as exc:
            # FLOOD_WAIT is transient — marking the op `failed` would lock the
            # idempotency key on a terminal state and the retry would surface
            # MessageSendFailed forever. Mark needs_review so an operator can
            # `operations retry` to reopen the slot.
            store.mark_needs_review(
                operation_id, f"FLOOD_WAIT during message send: {exc}"
            )
            raise MessageSendNeedsReview(str(exc)) from exc
        except MessageSendUnconfirmed as exc:
            # The request reached Telegram without error — only its id was
            # unreadable — so the message may already be delivered. `failed`
            # would claim nothing happened and invite a duplicate re-send, so
            # quarantine it for an operator to check the chat first.
            store.mark_needs_review(
                operation_id, f"unconfirmed message send: {exc}"
            )
            raise MessageSendNeedsReview(str(exc)) from exc
        except RichMessageUnsupported:
            # The backend refuses before issuing any RPC, so nothing reached
            # Telegram. Neither terminal state fits: `failed` would answer the
            # next attempt — the one made after the Telethon upgrade, with the
            # same operation_id — with previous_attempt_failed, and a leftover
            # `pending` row would answer it with MessageSendPending. Drop the row
            # so the key stays free for the retry that will actually work.
            store.delete_operation(operation_id)
            raise
        except Exception as exc:
            # Includes MediaConversionError (ffmpeg present but a .gif fails to
            # convert — corrupt input or the 120s timeout): deliberately
            # `failed`, not `needs_review`/dropped, because nothing reached
            # Telegram and the input itself is what needs fixing before a
            # retry. That is an intentional asymmetry with the ffmpeg-missing
            # gate in `_validate_rich_files`, which runs before the operation
            # row is opened specifically to keep the idempotency key free —
            # this exception fires after the row exists, so an explicit
            # `--operation-id` retry replays `previous_attempt_failed` here.
            store.fail_operation(operation_id, str(exc))
            raise
    finally:
        for path in downloaded_paths:
            with suppress(OSError):
                os.unlink(path)
        if base64_tmpdir is not None:
            shutil.rmtree(base64_tmpdir, ignore_errors=True)

    result = SendMessageResult(
        telegram_chat_id=request.telegram_chat_id,
        telegram_topic_id=request.telegram_topic_id,
        telegram_message_id=primary_id,
        is_service_command=is_service_command(request.text),
        chat_name=request.chat_name,
        topic_name=request.topic_name,
        telegram_message_ids=all_ids,
        scheduled=request.schedule_at is not None,
        schedule_at=(
            request.schedule_at.isoformat()
            if request.schedule_at is not None
            else None
        ),
        warnings=warnings,
    )
    op = store.complete_operation(operation_id, result.to_dict())
    # Record every id this process just sent (single send or album) so the
    # session-limited delete op (Tasks 6/7) recognises them. Only fresh sends
    # are recorded — a replay of a previously-completed op (handled above)
    # belongs to whatever process originally sent it, not this one.
    if sent_registry is not None:
        ids = all_ids if all_ids is not None else (primary_id,)
        for message_id in ids:
            if message_id is not None:
                sent_registry.record(request.telegram_chat_id, message_id)
    return result, op


# ---------------------------------------------------------------------------
# Get recent (read op)
# ---------------------------------------------------------------------------


async def get_recent_messages(
    *,
    backend: MessageReadBackend,
    chat_id: int,
    limit: int = 5,
    minutes: int | None = None,
    authorizer: Authorizer | None = None,
    now: datetime | None = None,
) -> list[RecentMessage]:
    """Return up to ``limit`` recent messages for ``chat_id``, newest first.

    This is a READ op: when an ``authorizer`` is supplied it must grant READ on
    the target chat or :class:`AccessDenied` is raised before any Telegram call.
    ``limit`` defaults to 5 and must be positive.

    ``minutes`` optionally narrows the result to messages newer than
    ``now - minutes`` (default ``now`` is the current UTC time). It composes with
    ``limit``: the backend returns the newest ``limit`` messages and the window
    then drops any that fall outside it, so the result may be shorter than
    ``limit``. Messages whose ``date`` the backend could not supply are excluded
    when a window is active (their age is unknown). ``minutes`` must be positive
    when given.
    """
    if limit <= 0:
        raise ValueError("get_recent_messages requires a positive limit")
    if minutes is not None and minutes <= 0:
        raise ValueError("get_recent_messages requires a positive minutes window")

    if authorizer is not None:
        await authorizer.require(chat_id, AccessLevel.READ)

    messages = await backend.get_recent_messages(chat_id=chat_id, limit=limit)
    if minutes is None:
        return messages

    reference = now if now is not None else datetime.now(UTC)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=UTC)
    cutoff = reference - timedelta(minutes=minutes)

    filtered: list[RecentMessage] = []
    for message in messages:
        if message.date is None:
            continue
        try:
            stamp = datetime.fromisoformat(message.date)
        except ValueError:
            continue
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=UTC)
        if stamp >= cutoff:
            filtered.append(message)
    return filtered


# ---------------------------------------------------------------------------
# Delete (delete op)
# ---------------------------------------------------------------------------


class MessageDeleteForbidden(RuntimeError):
    """A delete was blocked by the session-limit guard.

    Raised when ``delete_only_session_messages`` is active and one or more
    requested message ids were not recorded by this process's
    :class:`SentMessageRegistry`. ``message_ids`` lists the offending ids so a
    surface can report exactly which deletes were refused. This is distinct from
    :class:`AccessDenied` (a policy denial): the policy *does* grant ``delete``
    here, but the session-limit narrows it to messages this process sent.
    """

    def __init__(self, message_ids: Iterable[int], *, chat_id: int) -> None:
        self.message_ids = list(message_ids)
        self.chat_id = chat_id
        super().__init__(
            f"delete blocked: messages {self.message_ids} in chat {chat_id} "
            "were not sent by this server process "
            "(delete_only_session_messages is enabled)"
        )


class DeleteBackend(Protocol):
    """Telethon-facing surface needed to delete messages from a chat.

    ``revoke=True`` deletes for everyone (the default); ``revoke=False`` deletes
    only the technical account's local copy. Returns the number of messages the
    delete affected.
    """

    async def delete_messages(
        self, *, chat_id: int, message_ids: tuple[int, ...], revoke: bool = True
    ) -> int:
        ...


@dataclass(frozen=True)
class DeleteMessagesRequest:
    """Input to :func:`delete_messages`.

    ``telegram_chat_id`` is the resolved numeric chat id and ``message_ids`` the
    target message ids (at least one, all positive). ``revoke`` defaults to
    ``True`` (delete for everyone). ``dry_run`` resolves + authorizes (and runs
    the session-limit check) but does not delete. ``force`` is carried through
    for surface consistency with the project's ``--force`` convention; message
    delete has no protected-chat registry today, so it currently has no gating
    effect. ``chat_name`` is carried through for logging only.
    """

    telegram_chat_id: int
    message_ids: tuple[int, ...]
    revoke: bool = True
    dry_run: bool = False
    force: bool = False
    chat_name: str | None = None


@dataclass(frozen=True)
class DeleteMessagesResult:
    """Result of a delete operation.

    ``message_ids`` echoes the requested ids; ``deleted`` is how many the
    backend reported affected (``0`` on a dry run). ``dry_run`` is ``True`` when
    nothing was actually deleted.
    """

    telegram_chat_id: int
    message_ids: list[int]
    revoke: bool
    deleted: int
    dry_run: bool
    chat_name: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "telegram_chat_id": self.telegram_chat_id,
            "message_ids": list(self.message_ids),
            "revoke": self.revoke,
            "deleted": self.deleted,
            "dry_run": self.dry_run,
            "chat_name": self.chat_name,
        }


async def delete_messages(
    backend: DeleteBackend,
    *,
    request: DeleteMessagesRequest,
    authorizer: Authorizer | None = None,
    sent_registry: SentMessageRegistry | None = None,
    only_session_messages: bool = False,
) -> DeleteMessagesResult:
    """Delete ``request.message_ids`` from the resolved chat.

    Validation:

    * at least one ``message_id`` is required;
    * every ``message_id`` must be a positive integer.

    Deleting is a DELETE op: when an ``authorizer`` is supplied it must grant
    ``DELETE`` on the target chat or :class:`AccessDenied` is raised before any
    Telegram call.

    When ``only_session_messages`` is true (the safe default driven by
    ``telegram.access.delete_only_session_messages``) every requested id must
    have been recorded in ``sent_registry`` by this process; any unrecorded id
    raises :class:`MessageDeleteForbidden` before the backend is touched. A
    missing registry under this mode treats every id as unrecorded.

    ``dry_run`` runs the access + session-limit checks but returns without
    calling the backend (``deleted=0``).
    """
    message_ids = tuple(request.message_ids)
    if not message_ids:
        raise ValueError("at least one message_id is required")
    if any(mid <= 0 for mid in message_ids):
        raise ValueError("every message_id must be a positive integer")

    if authorizer is not None:
        await authorizer.require(request.telegram_chat_id, AccessLevel.DELETE)

    if only_session_messages:
        unknown = [
            mid
            for mid in message_ids
            if sent_registry is None
            or not sent_registry.contains(request.telegram_chat_id, mid)
        ]
        if unknown:
            raise MessageDeleteForbidden(unknown, chat_id=request.telegram_chat_id)

    if request.dry_run:
        return DeleteMessagesResult(
            telegram_chat_id=request.telegram_chat_id,
            message_ids=list(message_ids),
            revoke=request.revoke,
            deleted=0,
            dry_run=True,
            chat_name=request.chat_name,
        )

    deleted = await backend.delete_messages(
        chat_id=request.telegram_chat_id,
        message_ids=message_ids,
        revoke=request.revoke,
    )
    return DeleteMessagesResult(
        telegram_chat_id=request.telegram_chat_id,
        message_ids=list(message_ids),
        revoke=request.revoke,
        deleted=int(deleted),
        dry_run=False,
        chat_name=request.chat_name,
    )


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
    sent_registry: SentMessageRegistry | None = None,
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
                    # Pass the resolved folder's *identity* (id + name), not a
                    # bare name: a `folder_id:` rule can only match on the id,
                    # so a name-only membership would deny every chat here while
                    # a single send to the same chat succeeds.
                    folder_memberships=[(snapshot.folder_id, snapshot.folder_name)],
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
                sent_registry=sent_registry,
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
    "DeleteBackend",
    "DeleteMessagesRequest",
    "DeleteMessagesResult",
    "MassSendItemResult",
    "MassSendRequest",
    "MassSendResult",
    "MessageBackend",
    "MessageDeleteForbidden",
    "MessageReadBackend",
    "MessageSendFailed",
    "MessageSendNeedsReview",
    "MessageSendPending",
    "MessageSendUnconfirmed",
    "RecentMessage",
    "RichMessageUnsupported",
    "RichMediaForbidden",
    "ScheduleError",
    "SendMessageRequest",
    "SendMessageResult",
    "delete_messages",
    "get_recent_messages",
    "is_service_command",
    "mass_send_message",
    "parse_delay",
    "parse_schedule_at",
    "redact_message_text",
    "resolve_schedule_at",
    "send_message",
]
