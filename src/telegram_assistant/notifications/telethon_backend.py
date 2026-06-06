"""Telethon-backed :class:`NotificationBackend` implementation.

Kept separate from :mod:`service` so the domain layer stays Telethon-free. The
adapter translates the two domain verbs (``mute_chat`` / ``unmute_chat``) into
``account.UpdateNotifySettings`` RPCs carrying an ``InputPeerNotifySettings``.

Telegram has no explicit "mute forever" flag — clients express it with a
far-future ``mute_until`` timestamp. We use :data:`_MUTE_FOREVER_UNTIL` (well
inside the 32-bit epoch range) for an indefinite mute; an unmute sets
``mute_until=0``. ``FloodWaitError`` is translated, never swallowed.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from telegram_assistant.telegram_client.errors import translate_flood_wait

# Telegram represents an indefinite mute with a far-future expiry rather than a
# dedicated flag. Year 2037 keeps the value inside the signed 32-bit epoch range
# Telegram uses for ``mute_until`` while being effectively permanent.
_MUTE_FOREVER_UNTIL = datetime(2037, 12, 31, 23, 59, 59, tzinfo=UTC)


class TelethonNotificationBackend:
    """Adapter from the Telethon ``TelegramClient`` to :class:`NotificationBackend`."""

    def __init__(self, client: Any) -> None:
        self._client = client

    async def mute_chat(
        self, *, chat_id: int, mute_until: datetime | None
    ) -> None:
        from telethon.tl.functions.account import UpdateNotifySettingsRequest
        from telethon.tl.types import InputNotifyPeer, InputPeerNotifySettings

        until = mute_until if mute_until is not None else _MUTE_FOREVER_UNTIL
        try:
            peer = await self._client.get_input_entity(chat_id)
            await self._client(
                UpdateNotifySettingsRequest(
                    peer=InputNotifyPeer(peer),
                    settings=InputPeerNotifySettings(mute_until=until),
                )
            )
        except Exception as exc:
            raise translate_flood_wait(exc) from exc

    async def unmute_chat(self, *, chat_id: int) -> None:
        from telethon.tl.functions.account import UpdateNotifySettingsRequest
        from telethon.tl.types import InputNotifyPeer, InputPeerNotifySettings

        try:
            peer = await self._client.get_input_entity(chat_id)
            await self._client(
                UpdateNotifySettingsRequest(
                    peer=InputNotifyPeer(peer),
                    settings=InputPeerNotifySettings(mute_until=0),
                )
            )
        except Exception as exc:
            raise translate_flood_wait(exc) from exc


__all__ = ["TelethonNotificationBackend"]
