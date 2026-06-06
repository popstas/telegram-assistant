"""Telethon-backed message adapters.

Kept separate from :mod:`service` so the domain layer stays free of Telethon
imports. Two adapters live here:

* :class:`TelethonMessageReadBackend` — the get-recent read op, translating
  ``iter_messages`` into :class:`RecentMessage` rows.
* :class:`TelethonMessageBackend` — the write side (text, media, scheduled
  sends), implementing the :class:`MessageBackend` protocol.

``FloodWaitError`` is translated in both, never swallowed.
"""

from __future__ import annotations

from datetime import datetime
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


def _message_id(sent: Any) -> int:
    """Return the integer id of a single Telethon ``Message`` result."""
    raw_id = sent.id
    msg_id = int(raw_id)
    if msg_id <= 0:
        raise ValueError(f"Telethon returned invalid message id: {raw_id!r}")
    return msg_id


def _message_ids(sent: Any) -> int | list[int]:
    """Normalise a Telethon send result into one id or a list of ids.

    ``send_message`` returns a single ``Message``. ``send_file`` returns a
    single ``Message`` for one attachment and a ``list[Message]`` (an album)
    for several. The service layer turns a list into the album result shape.
    """
    if isinstance(sent, (list, tuple)):
        return [_message_id(m) for m in sent]
    return _message_id(sent)


def _forwarded_message_ids(sent: Any, source_message_ids: tuple[int, ...]) -> list[int]:
    """Normalise Telethon forward results and reject missing placeholders."""
    messages = list(sent) if isinstance(sent, (list, tuple)) else [sent]
    if len(messages) != len(source_message_ids):
        raise ValueError(
            "Telethon returned "
            f"{len(messages)} forwarded messages for "
            f"{len(source_message_ids)} source message ids"
        )

    forwarded: list[int] = []
    missing_sources: list[int] = []
    for source_id, message in zip(source_message_ids, messages, strict=True):
        if message is None:
            missing_sources.append(source_id)
            continue
        forwarded.append(_message_id(message))

    if missing_sources:
        raise ValueError(
            "source message ids without forwarded result: "
            f"{missing_sources}; forwarded target ids: {forwarded}"
        )
    return forwarded


class TelethonMessageBackend:
    """Adapter from the Telethon ``TelegramClient`` to :class:`MessageBackend`.

    Handles text-only sends via ``send_message`` and attachment sends (single
    file or album) via ``send_file``. ``schedule_at`` defers delivery; ``text``
    doubles as the caption when attachments are present and may be empty for a
    media-only send. ``FloodWaitError`` is translated so the worker queue can
    pause-and-retry instead of marking the operation as a generic failure.
    """

    def __init__(self, client: Any) -> None:
        self._client = client

    async def send_message(
        self,
        *,
        chat_id: int,
        text: str,
        topic_id: int | None = None,
        files: tuple[str, ...] = (),
        schedule_at: datetime | None = None,
    ) -> int | list[int]:
        files = tuple(files)
        try:
            if files:
                kwargs: dict[str, Any] = {
                    # An empty caption must be ``None`` so Telethon doesn't send
                    # a stray empty-text message alongside the media.
                    "caption": text or None,
                }
                if topic_id is not None:
                    kwargs["reply_to"] = topic_id
                if schedule_at is not None:
                    kwargs["schedule"] = schedule_at
                sent = await self._client.send_file(
                    chat_id,
                    list(files),
                    **kwargs,
                )
            else:
                kwargs = {}
                if topic_id is not None:
                    kwargs["reply_to"] = topic_id
                if schedule_at is not None:
                    kwargs["schedule"] = schedule_at
                sent = await self._client.send_message(chat_id, text, **kwargs)
        except Exception as exc:
            raise translate_flood_wait(exc) from exc
        return _message_ids(sent)


class TelethonReactionBackend:
    """Adapter from the Telethon ``TelegramClient`` to :class:`ReactionBackend`.

    Translates a set/clear into a ``messages.SendReaction`` RPC. Setting passes
    a single :class:`ReactionEmoji`; clearing passes an explicit empty reaction
    vector so Telegram removes any existing reaction. ``FloodWaitError`` is translated so
    the worker queue can pause-and-retry rather than mark a generic failure.
    """

    def __init__(self, client: Any) -> None:
        self._client = client

    async def set_reaction(
        self, *, chat_id: int, message_id: int, emoji: str | None
    ) -> None:
        from telethon.tl.functions.messages import SendReactionRequest
        from telethon.tl.types import ReactionEmoji

        reaction = [ReactionEmoji(emoticon=emoji)] if emoji is not None else []
        try:
            peer = await self._client.get_input_entity(chat_id)
            await self._client(
                SendReactionRequest(
                    peer=peer,
                    msg_id=message_id,
                    reaction=reaction,
                )
            )
        except Exception as exc:
            raise translate_flood_wait(exc) from exc


class TelethonForwardBackend:
    """Adapter from the Telethon ``TelegramClient`` to :class:`ForwardBackend`.

    Resolves both peers, then calls ``forward_messages(target, ids, from_peer)``.
    A single forwarded message comes back as one ``Message``; several come back
    as a ``list[Message]`` — both are normalised to a list of ids in request
    order. ``FloodWaitError`` is translated so the worker queue can
    pause-and-retry rather than mark a generic failure.
    """

    def __init__(self, client: Any) -> None:
        self._client = client

    async def forward_messages(
        self,
        *,
        from_chat_id: int,
        to_chat_id: int,
        message_ids: tuple[int, ...],
    ) -> list[int]:
        try:
            from_peer = await self._client.get_input_entity(from_chat_id)
            to_peer = await self._client.get_input_entity(to_chat_id)
            sent = await self._client.forward_messages(
                to_peer,
                list(message_ids),
                from_peer=from_peer,
            )
        except Exception as exc:
            raise translate_flood_wait(exc) from exc
        return _forwarded_message_ids(sent, message_ids)


__all__ = [
    "TelethonMessageReadBackend",
    "TelethonMessageBackend",
    "TelethonReactionBackend",
    "TelethonForwardBackend",
]
