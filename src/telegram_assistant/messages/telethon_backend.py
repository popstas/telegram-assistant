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
from typing import TYPE_CHECKING, Any

from telegram_assistant.messages.service import RecentMessage
from telegram_assistant.telegram_client.errors import translate_flood_wait

if TYPE_CHECKING:
    from telegram_assistant.messages.media_download import (
        DownloadedMedia,
        MediaInfo,
    )


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
        reply_to_message_id: int | None = None,
    ) -> int | list[int]:
        files = tuple(files)
        # ``reply_to`` carries either an explicit reply target or, in a forum,
        # the topic root. An explicit ``reply_to_message_id`` wins: replying to
        # a message inside a topic keeps the reply threaded in that topic.
        reply_to = (
            reply_to_message_id if reply_to_message_id is not None else topic_id
        )
        try:
            if files:
                kwargs: dict[str, Any] = {
                    # An empty caption must be ``None`` so Telethon doesn't send
                    # a stray empty-text message alongside the media.
                    "caption": text or None,
                }
                if reply_to is not None:
                    kwargs["reply_to"] = reply_to
                if schedule_at is not None:
                    kwargs["schedule"] = schedule_at
                sent = await self._client.send_file(
                    chat_id,
                    list(files),
                    **kwargs,
                )
            else:
                kwargs = {}
                if reply_to is not None:
                    kwargs["reply_to"] = reply_to
                if schedule_at is not None:
                    kwargs["schedule"] = schedule_at
                sent = await self._client.send_message(chat_id, text, **kwargs)
        except Exception as exc:
            raise translate_flood_wait(exc) from exc
        return _message_ids(sent)


class TelethonDeleteBackend:
    """Adapter from the Telethon ``TelegramClient`` to :class:`DeleteBackend`.

    Resolves the peer then calls ``delete_messages(entity, ids, revoke=...)``.
    ``revoke=True`` (the default) deletes for everyone; ``revoke=False`` removes
    only the technical account's local copy. ``FloodWaitError`` is translated so
    the worker queue can pause-and-retry rather than mark a generic failure.
    Returns the count of requested ids (Telegram does not report a per-id
    success vector, so a non-erroring call is treated as all-affected).
    """

    def __init__(self, client: Any) -> None:
        self._client = client

    async def delete_messages(
        self, *, chat_id: int, message_ids: tuple[int, ...], revoke: bool = True
    ) -> int:
        message_ids = tuple(message_ids)
        try:
            entity = await self._client.get_input_entity(chat_id)
            await self._client.delete_messages(
                entity, list(message_ids), revoke=revoke
            )
        except Exception as exc:
            raise translate_flood_wait(exc) from exc
        return len(message_ids)


_EDIT_REJECTION_REASONS = {
    "MessageAuthorRequiredError": "not_own_message",
    "MessageEditTimeExpiredError": "edit_window_expired",
    "MessageNotModifiedError": "not_modified",
}


class TelethonEditBackend:
    """Adapter from the Telethon ``TelegramClient`` to :class:`EditBackend`.

    Resolves the peer then calls ``edit_message(entity, message_id, text)``.
    Telegram's edit restrictions — editing another user's message
    (``MessageAuthorRequiredError``), the ~48h edit window having expired
    (``MessageEditTimeExpiredError``), or the text being unchanged
    (``MessageNotModifiedError``) — are translated into
    :class:`MessageEditRejected` so surfaces map them to 4xx rather than 500.
    ``FloodWaitError`` is translated so the worker queue can pause-and-retry.
    Returns the (stable) edited message id.
    """

    def __init__(self, client: Any) -> None:
        self._client = client

    async def edit_message(
        self, *, chat_id: int, message_id: int, text: str
    ) -> int:
        from telegram_assistant.messages.editing import MessageEditRejected

        try:
            entity = await self._client.get_input_entity(chat_id)
            await self._client.edit_message(entity, message_id, text)
        except Exception as exc:
            reason = _EDIT_REJECTION_REASONS.get(type(exc).__name__)
            if reason is not None:
                raise MessageEditRejected(str(exc), reason=reason) from exc
            raise translate_flood_wait(exc) from exc
        return message_id


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


class TelethonPinBackend:
    """Adapter from the Telethon ``TelegramClient`` to :class:`PinBackend`.

    Resolves the peer then calls ``pin_message`` / ``unpin_message``. ``silent``
    suppresses the pin service notification and ``pm_oneside`` pins only on the
    acting side of a private chat. ``unpin_message`` with ``message_id=None``
    unpins every pinned message. ``FloodWaitError`` is translated so the worker
    queue can pause-and-retry rather than mark a generic failure.
    """

    def __init__(self, client: Any) -> None:
        self._client = client

    async def pin_message(
        self, *, chat_id: int, message_id: int, silent: bool, pm_oneside: bool
    ) -> None:
        try:
            entity = await self._client.get_input_entity(chat_id)
            await self._client.pin_message(
                entity,
                message_id,
                notify=not silent,
                pm_oneside=pm_oneside,
            )
        except Exception as exc:
            raise translate_flood_wait(exc) from exc

    async def unpin_message(
        self, *, chat_id: int, message_id: int | None
    ) -> None:
        try:
            entity = await self._client.get_input_entity(chat_id)
            await self._client.unpin_message(entity, message_id)
        except Exception as exc:
            raise translate_flood_wait(exc) from exc


class TelethonMediaDownloadBackend:
    """Adapter from the Telethon ``TelegramClient`` to :class:`MediaDownloadBackend`.

    ``probe_media`` fetches the message and reports its media metadata (name,
    size, MIME) via Telethon's ``message.file`` without transferring bytes;
    ``None`` is returned for a text-only or missing message. ``download_media``
    fetches the message then calls ``download_media(msg, file=target_path)`` and
    reports the actually-written path/size/MIME. ``FloodWaitError`` is translated
    so the worker queue can pause-and-retry rather than mark a generic failure.
    """

    def __init__(self, client: Any) -> None:
        self._client = client

    async def _get_message(self, chat_id: int, message_id: int) -> Any:
        entity = await self._client.get_input_entity(chat_id)
        return await self._client.get_messages(entity, ids=message_id)

    async def probe_media(
        self, *, chat_id: int, message_id: int
    ) -> MediaInfo | None:
        from telegram_assistant.messages.media_download import MediaInfo

        try:
            msg = await self._get_message(chat_id, message_id)
        except Exception as exc:
            raise translate_flood_wait(exc) from exc
        if msg is None or getattr(msg, "media", None) is None:
            return None
        file = getattr(msg, "file", None)
        return MediaInfo(
            filename=getattr(file, "name", None) if file is not None else None,
            size=getattr(file, "size", None) if file is not None else None,
            mime=getattr(file, "mime_type", None) if file is not None else None,
        )

    async def download_media(
        self, *, chat_id: int, message_id: int, target_path: str
    ) -> DownloadedMedia:
        from telegram_assistant.messages.media_download import DownloadedMedia

        try:
            msg = await self._get_message(chat_id, message_id)
            if msg is None or getattr(msg, "media", None) is None:
                raise ValueError(
                    f"message {message_id} in chat {chat_id} has no downloadable media"
                )
            saved = await self._client.download_media(msg, file=target_path)
        except Exception as exc:
            raise translate_flood_wait(exc) from exc
        if saved is None:
            raise ValueError(
                f"download of message {message_id} in chat {chat_id} produced no file"
            )
        import os

        file = getattr(msg, "file", None)
        return DownloadedMedia(
            path=str(saved),
            size=os.path.getsize(saved),
            mime=getattr(file, "mime_type", None) if file is not None else None,
        )


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
    "TelethonDeleteBackend",
    "TelethonEditBackend",
    "TelethonReactionBackend",
    "TelethonPinBackend",
    "TelethonMediaDownloadBackend",
    "TelethonForwardBackend",
]
