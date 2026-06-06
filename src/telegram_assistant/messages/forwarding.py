"""Message-forwarding domain shared by HTTP, CLI, and the worker.

A single entry point, :func:`forward_messages`, copies one or more messages from
a resolved *source* chat into a resolved *target* chat. Forwarding reads from the
source and writes to the target, so when an :class:`Authorizer` is supplied it
must grant ``READ`` on the source **and** ``WRITE`` on the target or
:class:`AccessDenied` is raised before any Telegram call.

Kept in its own module so :mod:`service` (already large) stays focused on the
send/mass-send flow. Following the project's service/backend split, the domain
depends on the narrow :class:`ForwardBackend` protocol; the production Telethon
adapter lives in :mod:`telethon_backend` and tests inject fakes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from telegram_assistant.access.service import AccessLevel, Authorizer


class ForwardBackend(Protocol):
    """Telethon-facing surface needed to forward messages between chats.

    Returns the ids the forwarded copies received in the target chat, in the
    same order as ``message_ids``.
    """

    async def forward_messages(
        self,
        *,
        from_chat_id: int,
        to_chat_id: int,
        message_ids: tuple[int, ...],
    ) -> list[int]:
        ...


@dataclass(frozen=True)
class ForwardMessagesRequest:
    """Input to :func:`forward_messages`.

    ``from_chat_id`` / ``to_chat_id`` are resolved numeric chat ids and
    ``message_ids`` the source message ids to forward (at least one, all
    positive). ``from_chat_name`` / ``to_chat_name`` are carried through for
    logging only.
    """

    from_chat_id: int
    to_chat_id: int
    message_ids: tuple[int, ...]
    from_chat_name: str | None = None
    to_chat_name: str | None = None


@dataclass(frozen=True)
class ForwardMessagesResult:
    """Result of a forward operation.

    ``source_message_ids`` echoes the requested ids; ``telegram_message_ids``
    are the ids the forwarded copies received in the target chat.
    """

    from_chat_id: int
    to_chat_id: int
    source_message_ids: list[int]
    telegram_message_ids: list[int]
    from_chat_name: str | None = None
    to_chat_name: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "from_chat_id": self.from_chat_id,
            "to_chat_id": self.to_chat_id,
            "source_message_ids": list(self.source_message_ids),
            "telegram_message_ids": list(self.telegram_message_ids),
            "from_chat_name": self.from_chat_name,
            "to_chat_name": self.to_chat_name,
        }


async def forward_messages(
    backend: ForwardBackend,
    *,
    request: ForwardMessagesRequest,
    authorizer: Authorizer | None = None,
) -> ForwardMessagesResult:
    """Forward ``request.message_ids`` from the source chat into the target.

    Validation:

    * at least one ``message_id`` is required;
    * every ``message_id`` must be a positive integer.

    Forwarding reads the source and writes the target. When an ``authorizer``
    is supplied it must grant READ on the source and WRITE on the target or
    :class:`AccessDenied` is raised before the backend is touched.
    """
    message_ids = tuple(request.message_ids)
    if not message_ids:
        raise ValueError("at least one message_id is required")
    if any(mid <= 0 for mid in message_ids):
        raise ValueError("every message_id must be a positive integer")

    if authorizer is not None:
        await authorizer.require(request.from_chat_id, AccessLevel.READ)
        await authorizer.require(request.to_chat_id, AccessLevel.WRITE)

    forwarded = await backend.forward_messages(
        from_chat_id=request.from_chat_id,
        to_chat_id=request.to_chat_id,
        message_ids=message_ids,
    )
    return ForwardMessagesResult(
        from_chat_id=request.from_chat_id,
        to_chat_id=request.to_chat_id,
        source_message_ids=list(message_ids),
        telegram_message_ids=list(forwarded),
        from_chat_name=request.from_chat_name,
        to_chat_name=request.to_chat_name,
    )


__all__ = [
    "ForwardBackend",
    "ForwardMessagesRequest",
    "ForwardMessagesResult",
    "forward_messages",
]
