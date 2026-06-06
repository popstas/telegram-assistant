"""Message-reaction domain shared by HTTP, CLI, and the worker.

A single entry point, :func:`set_message_reaction`, sets or clears an emoji
reaction on one message in a resolved chat. Setting a reaction is a WRITE
operation: when an :class:`Authorizer` is supplied it must grant ``WRITE`` on
the target chat or :class:`AccessDenied` is raised before any Telegram call.

Kept in its own module so :mod:`service` (already large) stays focused on the
send/mass-send flow. Following the project's service/backend split, the domain
depends on the narrow :class:`ReactionBackend` protocol; the production
Telethon adapter lives in :mod:`telethon_backend` and tests inject fakes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from telegram_assistant.access.service import AccessLevel, Authorizer


class ReactionBackend(Protocol):
    """Telethon-facing surface needed to set/clear a message reaction.

    ``emoji`` is the reaction emoticon to set; ``None`` clears any existing
    reaction. The service decides which based on the request shape, so the
    adapter only has to translate one call.
    """

    async def set_reaction(
        self, *, chat_id: int, message_id: int, emoji: str | None
    ) -> None:
        ...


@dataclass(frozen=True)
class SendReactionRequest:
    """Input to :func:`set_message_reaction`.

    ``telegram_chat_id`` is the resolved numeric chat id and ``message_id`` the
    target message. Provide exactly one of ``emoji`` (set that reaction) or
    ``clear=True`` (remove the existing reaction). ``chat_name`` is carried
    through for logging only.
    """

    telegram_chat_id: int
    message_id: int
    emoji: str | None = None
    clear: bool = False
    chat_name: str | None = None


@dataclass(frozen=True)
class SendReactionResult:
    """Result of a set/clear reaction operation.

    ``emoji`` echoes the reaction that was set (``None`` for a clear) and
    ``cleared`` is ``True`` when the reaction was removed.
    """

    telegram_chat_id: int
    telegram_message_id: int
    emoji: str | None
    cleared: bool
    chat_name: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "telegram_chat_id": self.telegram_chat_id,
            "telegram_message_id": self.telegram_message_id,
            "emoji": self.emoji,
            "cleared": self.cleared,
            "chat_name": self.chat_name,
        }


async def set_message_reaction(
    backend: ReactionBackend,
    *,
    request: SendReactionRequest,
    authorizer: Authorizer | None = None,
) -> SendReactionResult:
    """Set or clear the reaction on ``request.message_id`` in the target chat.

    Validation:

    * ``message_id`` must be a positive integer;
    * provide exactly one of a non-empty ``emoji`` or ``clear=True`` — never
      both, never neither.

    Setting a reaction changes message state — a WRITE op. When an
    ``authorizer`` is supplied it must grant WRITE on the target chat or
    :class:`AccessDenied` is raised before the backend is touched.
    """
    if request.message_id <= 0:
        raise ValueError("message_id must be a positive integer")

    has_emoji = bool(request.emoji and request.emoji.strip())
    if has_emoji and request.clear:
        raise ValueError("provide either emoji or clear=True, not both")
    if not has_emoji and not request.clear:
        raise ValueError("provide either an emoji to set or clear=True")

    if authorizer is not None:
        await authorizer.require(request.telegram_chat_id, AccessLevel.WRITE)

    emoji = None if request.clear else request.emoji
    await backend.set_reaction(
        chat_id=request.telegram_chat_id,
        message_id=request.message_id,
        emoji=emoji,
    )
    return SendReactionResult(
        telegram_chat_id=request.telegram_chat_id,
        telegram_message_id=request.message_id,
        emoji=emoji,
        cleared=request.clear,
        chat_name=request.chat_name,
    )


__all__ = [
    "ReactionBackend",
    "SendReactionRequest",
    "SendReactionResult",
    "set_message_reaction",
]
