"""Notification mute/unmute domain shared by HTTP and CLI."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Protocol

from telegram_assistant.access.service import AccessLevel, Authorizer


class NotificationBackend(Protocol):
    """Telegram-facing operations needed to update chat notification settings."""

    async def mute_chat(
        self, *, chat_id: int, mute_until: datetime | None = None
    ) -> None:
        ...

    async def unmute_chat(self, *, chat_id: int) -> None:
        ...


@dataclass(frozen=True)
class MuteRequest:
    """Request to mute or unmute one resolved Telegram chat."""

    telegram_chat_id: int
    duration: timedelta | None = None

    def __post_init__(self) -> None:
        if self.telegram_chat_id == 0:
            raise ValueError("telegram_chat_id must be non-zero")
        if self.duration is not None and self.duration.total_seconds() <= 0:
            raise ValueError("duration must be positive")


@dataclass(frozen=True)
class MuteResult:
    """Serializable result for notification changes."""

    telegram_chat_id: int
    muted: bool
    mute_until: datetime | None = None
    muted_forever: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "telegram_chat_id": self.telegram_chat_id,
            "muted": self.muted,
            "mute_until": (
                self.mute_until.isoformat() if self.mute_until is not None else None
            ),
            "muted_forever": self.muted_forever,
        }


async def mute_chat(
    *,
    backend: NotificationBackend,
    request: MuteRequest,
    authorizer: Authorizer | None = None,
    now: datetime | None = None,
) -> MuteResult:
    """Mute one chat after WRITE access is granted."""
    if authorizer is not None:
        await authorizer.require(request.telegram_chat_id, AccessLevel.WRITE)
    mute_until = None
    if request.duration is not None:
        mute_until = (now or datetime.now()) + request.duration
    await backend.mute_chat(
        chat_id=request.telegram_chat_id,
        mute_until=mute_until,
    )
    return MuteResult(
        telegram_chat_id=request.telegram_chat_id,
        muted=True,
        mute_until=mute_until,
        muted_forever=mute_until is None,
    )


async def unmute_chat(
    *,
    backend: NotificationBackend,
    request: MuteRequest,
    authorizer: Authorizer | None = None,
) -> MuteResult:
    """Unmute one chat after WRITE access is granted."""
    if authorizer is not None:
        await authorizer.require(request.telegram_chat_id, AccessLevel.WRITE)
    await backend.unmute_chat(chat_id=request.telegram_chat_id)
    return MuteResult(telegram_chat_id=request.telegram_chat_id, muted=False)
