"""Message-edit domain shared by HTTP, CLI, and the worker.

A single entry point, :func:`edit_message`, edits the text/caption of one
already-sent message in a resolved chat. Editing is a WRITE operation: when an
:class:`Authorizer` is supplied it must grant ``WRITE`` on the target chat or
:class:`AccessDenied` is raised before any Telegram call.

Kept in its own module so :mod:`service` (already large) stays focused on the
send/mass-send flow. Following the project's service/backend split, the domain
depends on the narrow :class:`EditBackend` protocol; the production Telethon
adapter lives in :mod:`telethon_backend` and tests inject fakes.

The ``edit_only_session_messages`` guard mirrors ``delete_only_session_messages``
exactly: when active, only messages this process recorded in a
:class:`SentMessageRegistry` may be edited; any other id raises
:class:`MessageEditForbidden` before the backend is touched.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from telegram_assistant.access.service import AccessLevel, Authorizer
from telegram_assistant.messages.sent_registry import SentMessageRegistry


class MessageEditForbidden(RuntimeError):
    """An edit was blocked by the session-limit guard.

    Raised when ``edit_only_session_messages`` is active and the requested
    message id was not recorded by this process's :class:`SentMessageRegistry`.
    This is distinct from :class:`AccessDenied` (a policy denial): the policy
    *does* grant ``write`` here, but the session-limit narrows it to messages
    this process sent.
    """

    def __init__(self, message_id: int, *, chat_id: int) -> None:
        self.message_id = message_id
        self.chat_id = chat_id
        super().__init__(
            f"edit blocked: message {message_id} in chat {chat_id} was not sent "
            "by this server process (edit_only_session_messages is enabled)"
        )


class MessageEditRejected(RuntimeError):
    """Telegram refused the edit for a message-level reason.

    Surfaced by the adapter for Telegram's edit restrictions — editing another
    user's message, the ~48h edit window having expired, or the new text being
    identical to the current text. Distinct from :class:`AccessDenied` (policy)
    and :class:`MessageEditForbidden` (session guard): here the request reached
    Telegram and Telegram itself rejected it. ``reason`` is a short slug
    (``not_own_message`` / ``edit_window_expired`` / ``not_modified`` /
    ``rejected``) for surfaces to map to an error code.
    """

    def __init__(self, message: str, *, reason: str) -> None:
        self.reason = reason
        super().__init__(message)


class EditBackend(Protocol):
    """Telethon-facing surface needed to edit a message's text/caption.

    Edits ``message_id`` in ``chat_id`` to ``text`` and returns the edited
    message id (Telegram keeps the id stable across an edit). Implementations
    translate Telegram's edit restrictions into :class:`MessageEditRejected` and
    ``FloodWaitError`` into the project's queue signal.
    """

    async def edit_message(
        self, *, chat_id: int, message_id: int, text: str
    ) -> int:
        ...


@dataclass(frozen=True)
class MessageEditRequest:
    """Input to :func:`edit_message`.

    ``telegram_chat_id`` is the resolved numeric chat id, ``message_id`` the
    target message, and ``text`` the new (non-empty) text/caption. ``dry_run``
    resolves + authorizes (and runs the session-limit check) but does not edit.
    ``chat_name`` is carried through for logging only.
    """

    telegram_chat_id: int
    message_id: int
    text: str
    dry_run: bool = False
    chat_name: str | None = None


@dataclass(frozen=True)
class MessageEditResult:
    """Result of an edit operation.

    ``text`` echoes the new text; ``dry_run`` is ``True`` when nothing was
    actually edited.
    """

    telegram_chat_id: int
    telegram_message_id: int
    text: str
    dry_run: bool
    chat_name: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "telegram_chat_id": self.telegram_chat_id,
            "telegram_message_id": self.telegram_message_id,
            "text": self.text,
            "dry_run": self.dry_run,
            "chat_name": self.chat_name,
        }


async def edit_message(
    backend: EditBackend,
    *,
    request: MessageEditRequest,
    authorizer: Authorizer | None = None,
    sent_registry: SentMessageRegistry | None = None,
    only_session_messages: bool = False,
) -> MessageEditResult:
    """Edit ``request.message_id`` in the resolved chat to ``request.text``.

    Validation:

    * ``message_id`` must be a positive integer;
    * ``text`` must be non-empty (after stripping whitespace).

    Editing changes message state — a WRITE op. When an ``authorizer`` is
    supplied it must grant ``WRITE`` on the target chat or :class:`AccessDenied`
    is raised before the backend is touched.

    When ``only_session_messages`` is true (the safe default driven by
    ``telegram.access.edit_only_session_messages``) the requested id must have
    been recorded in ``sent_registry`` by this process; otherwise
    :class:`MessageEditForbidden` is raised before the backend is touched. A
    missing registry under this mode treats the id as unrecorded.

    ``dry_run`` runs the access + session-limit checks but returns without
    calling the backend.
    """
    if request.message_id <= 0:
        raise ValueError("message_id must be a positive integer")
    if not request.text or not request.text.strip():
        raise ValueError("text must be a non-empty string")

    if authorizer is not None:
        await authorizer.require(request.telegram_chat_id, AccessLevel.WRITE)

    if only_session_messages and (
        sent_registry is None
        or not sent_registry.contains(request.telegram_chat_id, request.message_id)
    ):
        raise MessageEditForbidden(
            request.message_id, chat_id=request.telegram_chat_id
        )

    if request.dry_run:
        return MessageEditResult(
            telegram_chat_id=request.telegram_chat_id,
            telegram_message_id=request.message_id,
            text=request.text,
            dry_run=True,
            chat_name=request.chat_name,
        )

    edited_id = await backend.edit_message(
        chat_id=request.telegram_chat_id,
        message_id=request.message_id,
        text=request.text,
    )
    return MessageEditResult(
        telegram_chat_id=request.telegram_chat_id,
        telegram_message_id=int(edited_id),
        text=request.text,
        dry_run=False,
        chat_name=request.chat_name,
    )


__all__ = [
    "EditBackend",
    "MessageEditForbidden",
    "MessageEditRejected",
    "MessageEditRequest",
    "MessageEditResult",
    "edit_message",
]
