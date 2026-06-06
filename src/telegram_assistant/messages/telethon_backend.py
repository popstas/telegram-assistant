"""Telethon-backed message adapter implementations.

Kept separate from :mod:`service` so the domain layer stays free of Telethon
imports. ``FloodWaitError`` is translated, never swallowed.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from telegram_assistant.messages.service import (
    RecentMessage,
    SendMessageBackendResult,
)
from telegram_assistant.telegram_client.errors import translate_flood_wait


def _media_summary(media: Any) -> str:
    """Return a short ``[type]`` summary for a media-only message."""
    if media is None:
        return ""
    name = type(media).__name__
    # Strip the common Telethon ``MessageMedia`` prefix for a tidy label
    # (``MessageMediaPhoto`` → ``photo``); fall back to the raw class name.
    if name.startswith("MessageMedia"):
        name = name[len("MessageMedia") :]
    return f"[{name.lower() or 'media'}]"


def _message_ids(sent: Any) -> tuple[int, ...]:
    """Extract Telegram message ids from a single message or album result."""
    if isinstance(sent, Sequence) and not isinstance(sent, (str, bytes, bytearray)):
        return tuple(int(getattr(item, "id", 0)) for item in sent)
    return (int(getattr(sent, "id", 0)),)


class TelethonMessageBackend:
    """Adapter from the Telethon ``TelegramClient`` to :class:`MessageBackend`."""

    def __init__(self, client: Any) -> None:
        self._client = client

    async def send_message(
        self,
        *,
        chat_id: int,
        text: str,
        topic_id: int | None = None,
        files: tuple[str, ...] = (),
        schedule_at: Any | None = None,
    ) -> SendMessageBackendResult:
        kwargs: dict[str, Any] = {}
        if topic_id is not None:
            kwargs["reply_to"] = topic_id
        if schedule_at is not None:
            kwargs["schedule"] = schedule_at
        try:
            if files:
                sent = await self._client.send_file(
                    chat_id,
                    files,
                    caption=text or None,
                    **kwargs,
                )
            else:
                sent = await self._client.send_message(chat_id, text, **kwargs)
        except Exception as exc:
            raise translate_flood_wait(exc) from exc

        ids = _message_ids(sent)
        return SendMessageBackendResult(
            telegram_message_id=ids[0] if ids else None,
            telegram_message_ids=ids,
        )

    async def set_message_reaction(
        self,
        *,
        chat_id: int,
        message_id: int,
        emoji: str | None,
    ) -> None:
        from telethon.tl.functions.messages import SendReactionRequest
        from telethon.tl.types import ReactionEmoji

        try:
            input_peer = await self._client.get_input_entity(chat_id)
            reaction = [] if emoji is None else [ReactionEmoji(emoticon=emoji)]
            await self._client(
                SendReactionRequest(
                    peer=input_peer,
                    msg_id=message_id,
                    reaction=reaction,
                )
            )
        except Exception as exc:
            raise translate_flood_wait(exc) from exc

    async def forward_messages(
        self,
        *,
        source_chat_id: int,
        target_chat_id: int,
        message_ids: tuple[int, ...],
    ) -> tuple[int, ...]:
        try:
            sent = await self._client.forward_messages(
                target_chat_id,
                message_ids,
                from_peer=source_chat_id,
            )
        except Exception as exc:
            raise translate_flood_wait(exc) from exc
        return _message_ids(sent)


class TelethonMessageReadBackend:
    """Adapter from the Telethon ``TelegramClient`` to :class:`MessageReadBackend`."""

    def __init__(self, client: Any) -> None:
        self._client = client

    async def get_recent_messages(
        self, *, chat_id: int, limit: int = 5
    ) -> list[RecentMessage]:
        try:
            channel = await self._client.get_input_entity(chat_id)
            out: list[RecentMessage] = []
            async for msg in self._client.iter_messages(channel, limit=limit):
                sender = getattr(msg, "sender", None)
                username = (
                    getattr(sender, "username", None) if sender is not None else None
                )
                reply_to = getattr(msg, "reply_to_msg_id", None)
                date = getattr(msg, "date", None)
                text = getattr(msg, "message", "") or ""
                if not text:
                    media = getattr(msg, "media", None)
                    if media is not None:
                        text = _media_summary(media)
                out.append(
                    RecentMessage(
                        id=int(getattr(msg, "id", 0)),
                        sender=username,
                        date=date.isoformat() if date is not None else None,
                        reply_to=int(reply_to) if reply_to else None,
                        text=text,
                    )
                )
        except Exception as exc:
            raise translate_flood_wait(exc) from exc
        return out


__all__ = ["TelethonMessageBackend", "TelethonMessageReadBackend"]
