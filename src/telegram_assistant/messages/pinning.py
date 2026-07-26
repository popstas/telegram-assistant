"""Message pin/unpin domain shared by HTTP, CLI, and the worker.

Two entry points, :func:`pin_message` and :func:`unpin_message`, pin or unpin a
message in a resolved chat. Both change chat state — WRITE operations: when an
:class:`Authorizer` is supplied it must grant ``WRITE`` on the target chat or
:class:`AccessDenied` is raised before any Telegram call.

Kept in its own module so :mod:`service` (already large) stays focused on the
send/mass-send flow. Following the project's service/backend split, the domain
depends on the narrow :class:`PinBackend` protocol; the production Telethon
adapter lives in :mod:`telethon_backend` and tests inject fakes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from telegram_assistant.access.service import AccessLevel, Authorizer
from telegram_assistant.messages.pacing import Pacer, pin_pacing_key


class PinBackend(Protocol):
    """Telethon-facing surface needed to pin/unpin a message.

    ``pin_message`` pins one message; ``silent`` suppresses the service
    notification and ``pm_oneside`` pins only on the current side in a private
    chat. ``unpin_message`` unpins a single message id, or **all** pinned
    messages when ``message_id`` is ``None``.
    """

    async def pin_message(
        self, *, chat_id: int, message_id: int, silent: bool, pm_oneside: bool
    ) -> None:
        ...

    async def unpin_message(
        self, *, chat_id: int, message_id: int | None
    ) -> None:
        ...


@dataclass(frozen=True)
class PinMessageRequest:
    """Input to :func:`pin_message`.

    ``telegram_chat_id`` is the resolved numeric chat id and ``message_id`` the
    target message. ``silent`` suppresses the "pinned a message" service
    notification; ``pm_oneside`` pins only on the acting side of a private chat.
    ``dry_run`` resolves + authorizes but does not pin. ``chat_name`` is carried
    through for logging only.
    """

    telegram_chat_id: int
    message_id: int
    silent: bool = False
    pm_oneside: bool = False
    dry_run: bool = False
    chat_name: str | None = None


@dataclass(frozen=True)
class PinMessageResult:
    """Result of a pin operation.

    ``dry_run`` is ``True`` when nothing was actually pinned.
    """

    telegram_chat_id: int
    telegram_message_id: int
    silent: bool
    pm_oneside: bool
    dry_run: bool
    chat_name: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "telegram_chat_id": self.telegram_chat_id,
            "telegram_message_id": self.telegram_message_id,
            "silent": self.silent,
            "pm_oneside": self.pm_oneside,
            "dry_run": self.dry_run,
            "chat_name": self.chat_name,
        }


@dataclass(frozen=True)
class UnpinMessageRequest:
    """Input to :func:`unpin_message`.

    ``telegram_chat_id`` is the resolved numeric chat id. ``message_id`` is the
    target message to unpin, or ``None`` to unpin **all** pinned messages.
    ``dry_run`` resolves + authorizes but does not unpin. ``chat_name`` is
    carried through for logging only.
    """

    telegram_chat_id: int
    message_id: int | None = None
    dry_run: bool = False
    chat_name: str | None = None


@dataclass(frozen=True)
class UnpinMessageResult:
    """Result of an unpin operation.

    ``telegram_message_id`` echoes the unpinned id, or ``None`` for unpin-all.
    ``unpinned_all`` is ``True`` when every pinned message was removed.
    ``dry_run`` is ``True`` when nothing was actually unpinned.
    """

    telegram_chat_id: int
    telegram_message_id: int | None
    unpinned_all: bool
    dry_run: bool
    chat_name: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "telegram_chat_id": self.telegram_chat_id,
            "telegram_message_id": self.telegram_message_id,
            "unpinned_all": self.unpinned_all,
            "dry_run": self.dry_run,
            "chat_name": self.chat_name,
        }


async def pin_message(
    backend: PinBackend,
    *,
    request: PinMessageRequest,
    authorizer: Authorizer | None = None,
    pacer: Pacer | None = None,
) -> PinMessageResult:
    """Pin ``request.message_id`` in the resolved chat.

    Validation: ``message_id`` must be a positive integer.

    Pinning changes chat state — a WRITE op. When an ``authorizer`` is supplied
    it must grant ``WRITE`` on the target chat or :class:`AccessDenied` is raised
    before the backend is touched. ``dry_run`` runs the access check but returns
    without calling the backend.

    A ``pacer`` spreads rapid pins over a shared minimum interval and absorbs
    ``FLOOD_WAIT`` with bounded sleep-and-retry; without one the backend is
    called directly (pre-pacing behaviour). ``dry_run`` never paces — nothing
    reaches Telegram, so there is no rate limit to respect.
    """
    if request.message_id <= 0:
        raise ValueError("message_id must be a positive integer")

    if authorizer is not None:
        await authorizer.require(request.telegram_chat_id, AccessLevel.WRITE)

    if request.dry_run:
        return PinMessageResult(
            telegram_chat_id=request.telegram_chat_id,
            telegram_message_id=request.message_id,
            silent=request.silent,
            pm_oneside=request.pm_oneside,
            dry_run=True,
            chat_name=request.chat_name,
        )

    async def _call() -> None:
        await backend.pin_message(
            chat_id=request.telegram_chat_id,
            message_id=request.message_id,
            silent=request.silent,
            pm_oneside=request.pm_oneside,
        )

    if pacer is not None:
        await pacer.run(pin_pacing_key(request.telegram_chat_id), _call)
    else:
        await _call()
    return PinMessageResult(
        telegram_chat_id=request.telegram_chat_id,
        telegram_message_id=request.message_id,
        silent=request.silent,
        pm_oneside=request.pm_oneside,
        dry_run=False,
        chat_name=request.chat_name,
    )


async def unpin_message(
    backend: PinBackend,
    *,
    request: UnpinMessageRequest,
    authorizer: Authorizer | None = None,
    pacer: Pacer | None = None,
) -> UnpinMessageResult:
    """Unpin ``request.message_id`` (or all pinned messages) in the chat.

    Validation: when a ``message_id`` is given it must be a positive integer;
    ``None`` means unpin every pinned message.

    Unpinning changes chat state — a WRITE op. When an ``authorizer`` is supplied
    it must grant ``WRITE`` on the target chat or :class:`AccessDenied` is raised
    before the backend is touched. ``dry_run`` runs the access check but returns
    without calling the backend.

    Unpins share the pin pacing gate (same per-chat Telegram limit), so a
    ``pacer`` throttles them together and retries bounded ``FLOOD_WAIT`` pauses.
    """
    unpin_all = request.message_id is None
    if not unpin_all and request.message_id <= 0:
        raise ValueError("message_id must be a positive integer")

    if authorizer is not None:
        await authorizer.require(request.telegram_chat_id, AccessLevel.WRITE)

    if request.dry_run:
        return UnpinMessageResult(
            telegram_chat_id=request.telegram_chat_id,
            telegram_message_id=request.message_id,
            unpinned_all=unpin_all,
            dry_run=True,
            chat_name=request.chat_name,
        )

    async def _call() -> None:
        await backend.unpin_message(
            chat_id=request.telegram_chat_id,
            message_id=request.message_id,
        )

    if pacer is not None:
        await pacer.run(pin_pacing_key(request.telegram_chat_id), _call)
    else:
        await _call()
    return UnpinMessageResult(
        telegram_chat_id=request.telegram_chat_id,
        telegram_message_id=request.message_id,
        unpinned_all=unpin_all,
        dry_run=False,
        chat_name=request.chat_name,
    )


__all__ = [
    "PinBackend",
    "PinMessageRequest",
    "PinMessageResult",
    "UnpinMessageRequest",
    "UnpinMessageResult",
    "pin_message",
    "unpin_message",
]
