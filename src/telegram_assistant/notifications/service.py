"""Notification-settings domain shared by HTTP, CLI, and the worker.

Two entry points mute or unmute a single resolved chat/contact:

* :func:`mute_chat` — silence notifications, either indefinitely (the default)
  or until a future time derived from a duration in hours.
* :func:`unmute_chat` — restore normal notifications.

Both are WRITE operations: when an :class:`Authorizer` is supplied it must
grant ``WRITE`` on the target chat or :class:`AccessDenied` is raised before any
Telegram call. The chat is resolved upstream in the surface layer (CLI/HTTP);
this module works on the resolved numeric ``telegram_chat_id`` only.

Following the project's service/backend split, the domain depends on the narrow
:class:`NotificationBackend` protocol; the production Telethon adapter lives in
:mod:`telethon_backend` and tests inject fakes.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from telegram_assistant.access.service import AccessLevel, Authorizer

_MAX_MUTE_UNTIL = datetime(2037, 12, 31, 23, 59, 59, tzinfo=UTC)


class NotificationBackend(Protocol):
    """Telethon-facing surface needed to change a chat's notify settings.

    ``mute_until`` is the time the mute expires; ``None`` means *mute forever*
    (the adapter translates that into Telegram's far-future sentinel). ``unmute``
    clears the mute regardless of any prior expiry.
    """

    async def mute_chat(
        self, *, chat_id: int, mute_until: datetime | None
    ) -> None:
        ...

    async def unmute_chat(self, *, chat_id: int) -> None:
        ...


@dataclass(frozen=True)
class MuteRequest:
    """Input to :func:`mute_chat` / :func:`unmute_chat`.

    ``telegram_chat_id`` is the resolved numeric chat/contact id. ``duration_hours``
    applies only to mute: a positive integer mutes until ``now + duration``;
    ``None`` mutes indefinitely. ``chat_name`` is carried through for logging.
    """

    telegram_chat_id: int
    duration_hours: int | None = None
    chat_name: str | None = None


@dataclass(frozen=True)
class MuteResult:
    """Result of a mute/unmute operation.

    ``muted`` is ``True`` for a mute and ``False`` for an unmute. ``mute_until``
    is the ISO-8601 expiry when the mute is time-bounded, ``None`` for a
    forever-mute or an unmute. ``duration_hours`` echoes the requested duration.
    """

    telegram_chat_id: int
    muted: bool
    duration_hours: int | None = None
    mute_until: str | None = None
    chat_name: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "telegram_chat_id": self.telegram_chat_id,
            "muted": self.muted,
            "duration_hours": self.duration_hours,
            "mute_until": self.mute_until,
            "chat_name": self.chat_name,
        }


async def mute_chat(
    backend: NotificationBackend,
    *,
    request: MuteRequest,
    authorizer: Authorizer | None = None,
    now: datetime | None = None,
) -> MuteResult:
    """Mute ``request.telegram_chat_id`` indefinitely or for ``duration_hours``.

    Muting changes the chat's notification settings — a WRITE op. When an
    ``authorizer`` is supplied it must grant WRITE on the target chat or
    :class:`AccessDenied` is raised before the backend is touched. A non-positive
    ``duration_hours`` is rejected with :class:`ValueError`.
    """
    if request.duration_hours is not None and request.duration_hours <= 0:
        raise ValueError("duration_hours must be a positive integer")

    if authorizer is not None:
        await authorizer.require(request.telegram_chat_id, AccessLevel.WRITE)

    mute_until: datetime | None = None
    if request.duration_hours is not None:
        base = now if now is not None else datetime.now(UTC)
        try:
            mute_until = base + timedelta(hours=request.duration_hours)
        except OverflowError as exc:
            raise ValueError("duration_hours is too large") from exc
        if mute_until > _MAX_MUTE_UNTIL:
            raise ValueError(
                "duration_hours is too large; omit it to mute indefinitely"
            )

    await backend.mute_chat(
        chat_id=request.telegram_chat_id, mute_until=mute_until
    )
    return MuteResult(
        telegram_chat_id=request.telegram_chat_id,
        muted=True,
        duration_hours=request.duration_hours,
        mute_until=mute_until.isoformat() if mute_until is not None else None,
        chat_name=request.chat_name,
    )


async def unmute_chat(
    backend: NotificationBackend,
    *,
    request: MuteRequest,
    authorizer: Authorizer | None = None,
) -> MuteResult:
    """Restore normal notifications for ``request.telegram_chat_id``.

    Unmuting is the inverse of :func:`mute_chat` and is likewise WRITE-gated.
    ``duration_hours`` on the request is ignored.
    """
    if authorizer is not None:
        await authorizer.require(request.telegram_chat_id, AccessLevel.WRITE)

    await backend.unmute_chat(chat_id=request.telegram_chat_id)
    return MuteResult(
        telegram_chat_id=request.telegram_chat_id,
        muted=False,
        duration_hours=None,
        mute_until=None,
        chat_name=request.chat_name,
    )


__all__ = [
    "MuteRequest",
    "MuteResult",
    "NotificationBackend",
    "mute_chat",
    "unmute_chat",
]
