"""Telethon notification settings adapter."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from telegram_assistant.telegram_client.errors import translate_flood_wait

FOREVER_MUTE_UNTIL = datetime(2038, 1, 19, 3, 14, 7, tzinfo=UTC)

# Telethon's ``InputPeerNotifySettings`` omits ``mute_until`` from the wire when
# it is ``None`` (the flag bit is never set), which leaves any existing mute in
# place. Passing the epoch (``0``) sets the flag and serializes a past instant,
# explicitly clearing the mute. See ``InputPeerNotifySettings._bytes``.
UNMUTE_MUTE_UNTIL = 0


class TelethonNotificationBackend:
    """Adapter from Telethon ``TelegramClient`` to ``NotificationBackend``."""

    def __init__(self, client: Any) -> None:
        self._client = client

    async def mute_chat(
        self, *, chat_id: int, mute_until: datetime | None = None
    ) -> None:
        await self._update(chat_id=chat_id, mute_until=mute_until or FOREVER_MUTE_UNTIL)

    async def unmute_chat(self, *, chat_id: int) -> None:
        await self._update(chat_id=chat_id, mute_until=UNMUTE_MUTE_UNTIL)

    async def _update(
        self, *, chat_id: int, mute_until: datetime | int | None = None
    ) -> None:
        from telethon.tl.functions.account import UpdateNotifySettingsRequest
        from telethon.tl.types import InputNotifyPeer, InputPeerNotifySettings

        try:
            input_peer = await self._client.get_input_entity(chat_id)
            await self._client(
                UpdateNotifySettingsRequest(
                    peer=InputNotifyPeer(input_peer),
                    settings=InputPeerNotifySettings(mute_until=mute_until),
                )
            )
        except Exception as exc:
            raise translate_flood_wait(exc) from exc
