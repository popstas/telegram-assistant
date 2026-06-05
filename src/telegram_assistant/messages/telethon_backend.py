"""Telethon-backed :class:`MessageReadBackend` implementation.

Kept separate from :mod:`service` so the domain layer stays free of Telethon
imports. Translates the get-recent read op into ``iter_messages`` and maps each
Telethon message onto a :class:`RecentMessage` (id, sender, date, reply_to,
text/media summary). ``FloodWaitError`` is translated, never swallowed.
"""

from __future__ import annotations

from typing import Any

from telegram_assistant.messages.service import RecentMessage
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


__all__ = ["TelethonMessageReadBackend"]
