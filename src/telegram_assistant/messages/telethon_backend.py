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

from datetime import UTC, datetime, timedelta
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


def _import_rich_markdown_type() -> Any:
    """Return ``InputRichMessageMarkdown``, or ``None`` on an older Telethon.

    The rich-message constructor arrived in Telethon 1.44 (layer 227). Probing
    for it here — rather than importing at module level — keeps the whole
    adapter importable on an older build so only a rich send fails, and it
    fails with a version hint instead of an ImportError at startup.
    """
    from telethon.tl import types

    return getattr(types, "InputRichMessageMarkdown", None)


def _extract_rich_message_id(result: Any, *, random_id: int | None = None) -> int | None:
    """Find the sent message's id in the raw ``messages.sendMessage`` result.

    ``UpdateMessageID`` is the reliable source — the spike saw it for both
    private and channel peers — with the ``UpdateNew*Message`` variants as a
    fallback for envelopes that omit it.

    An ``Updates`` container may carry updates unrelated to *this* request, so
    an ``UpdateMessageID`` is picked by the request's own ``random_id``
    (Telethon's own sender keys strictly on it). A *keyed* update that names a
    different ``random_id`` is another request's and is never used as a
    fallback: its id would be reported as ours and recorded in the
    ``SentMessageRegistry``, handing this session edit/delete rights over a
    message it did not send. An unkeyed entry — or any entry at all when this
    request's ``random_id`` could not be read — is still better than reporting a
    delivered article as failed.

    Telegram pairs each ``UpdateMessageID`` with the ``UpdateNew*Message`` for
    the *same* message, so the id of a foreign-keyed ``UpdateMessageID`` is
    excluded from the ``UpdateNew*Message`` scan too — otherwise the fallback
    would hand back the very id the ``random_id`` check just refused. Any other
    ``UpdateNew*Message`` may still be ours (our own ``UpdateMessageID`` could
    be missing from the envelope), so it is not a blanket refusal.

    The envelope itself varies: the spike saw a full ``Updates``, but the same
    request also answers with ``UpdateShort`` (one update under ``.update``)
    and with ``UpdateShortSentMessage``, which carries the new id on itself and
    no update list at all. Telethon's own high-level sender special-cases both,
    and this path bypasses it — missing them would report a *delivered* article
    as a failed send and lock the idempotency key on a terminal state. The bare
    ``result.id`` read is gated on that one type name: ``UpdateShortMessage`` and
    ``UpdateShortChatMessage`` are *incoming*-message envelopes that also carry
    an ``id``, and it belongs to someone else's message (Telethon's own
    extractor returns nothing for them for the same reason).
    """
    updates = getattr(result, "updates", None)
    if isinstance(updates, list):
        candidates = updates
    else:
        single = getattr(result, "update", None)
        candidates = [single] if single is not None else [result]
    message_ids = [
        (getattr(update, "random_id", None), getattr(update, "id", None))
        for update in candidates
        if type(update).__name__ == "UpdateMessageID"
    ]
    foreign_ids: set[int] = set()
    if random_id is not None:
        for update_random_id, message_id in message_ids:
            if message_id is None:
                continue
            if update_random_id == random_id:
                return int(message_id)
            if update_random_id is not None:
                foreign_ids.add(int(message_id))
    for update_random_id, message_id in message_ids:
        if random_id is not None and update_random_id is not None:
            # Keyed, and not ours — belongs to another request in the same
            # container. Fall through to the UpdateNew*Message scan instead.
            continue
        if message_id is not None:
            return int(message_id)
    for update in candidates:
        # ``UpdateNewScheduledMessage`` is the scheduled-send counterpart;
        # Telethon's own extractor handles all three.
        if type(update).__name__ in (
            "UpdateNewMessage",
            "UpdateNewChannelMessage",
            "UpdateNewScheduledMessage",
        ):
            message_id = getattr(getattr(update, "message", None), "id", None)
            if message_id is not None and int(message_id) not in foreign_ids:
                return int(message_id)
    # ``UpdateShortSentMessage``: no updates, no nested message — just the id.
    if type(result).__name__ == "UpdateShortSentMessage":
        own_id = getattr(result, "id", None)
        if isinstance(own_id, int) and own_id > 0:
            return own_id
    return None


class TelethonMessageBackend:
    """Adapter from the Telethon ``TelegramClient`` to :class:`MessageBackend`.

    Handles text-only sends via ``send_message`` and attachment sends (single
    file or album) via ``send_file``. ``schedule_at`` defers delivery; ``text``
    doubles as the caption when attachments are present and may be empty for a
    media-only send. ``FloodWaitError`` is translated so the worker queue can
    pause-and-retry instead of marking the operation as a generic failure.

    A ``rich_markdown`` send (Telegram article) takes a separate path: the
    high-level ``client.send_message`` has no ``rich_message`` parameter, so it
    issues the raw ``messages.SendMessageRequest`` with an
    ``InputRichMessageMarkdown`` body. The server parses the markdown; nothing
    is validated or rewritten here beyond the domain-level checks.
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
        rich_markdown: str | None = None,
    ) -> int | list[int]:
        files = tuple(files)
        # ``reply_to`` carries either an explicit reply target or, in a forum,
        # the topic root. An explicit ``reply_to_message_id`` wins: replying to
        # a message inside a topic keeps the reply threaded in that topic.
        reply_to = (
            reply_to_message_id if reply_to_message_id is not None else topic_id
        )
        if rich_markdown is not None:
            if files:
                raise ValueError(
                    "rich_markdown cannot be combined with file attachments"
                )
            return await self._send_rich_message(
                chat_id=chat_id,
                rich_markdown=rich_markdown,
                topic_id=topic_id,
                schedule_at=schedule_at,
                reply_to_message_id=reply_to_message_id,
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

    async def _send_rich_message(
        self,
        *,
        chat_id: int,
        rich_markdown: str,
        topic_id: int | None,
        schedule_at: datetime | None,
        reply_to_message_id: int | None,
    ) -> int:
        from telegram_assistant.messages.service import (
            MessageSendUnconfirmed,
            RichMessageUnsupported,
        )

        rich_type = _import_rich_markdown_type()
        if rich_type is None:
            # Not MessageSendFailed: that class means "a previous attempt with
            # this idempotency key failed" and the surfaces render it as 409
            # previous_attempt_failed / exit 2, which would point the operator at
            # a prior attempt instead of at the Telethon version.
            raise RichMessageUnsupported(
                "rich message send requires telethon>=1.44 (layer 227); "
                "the installed Telethon has no InputRichMessageMarkdown"
            )

        from telethon.tl.functions.messages import SendMessageRequest
        from telethon.tl.types import InputReplyToMessage

        kwargs: dict[str, Any] = {}
        if reply_to_message_id is not None:
            # Replying inside a forum topic: the topic root rides along as
            # ``top_msg_id`` so the reply stays in the topic.
            kwargs["reply_to"] = InputReplyToMessage(
                reply_to_msg_id=reply_to_message_id, top_msg_id=topic_id
            )
        elif topic_id is not None:
            kwargs["reply_to"] = InputReplyToMessage(reply_to_msg_id=topic_id)
        if schedule_at is not None:
            kwargs["schedule_date"] = schedule_at

        try:
            peer = await self._client.get_input_entity(chat_id)
            # Built up front so ``random_id`` (auto-filled by the constructor) can
            # be matched against the ``UpdateMessageID`` in the response.
            send = SendMessageRequest(
                peer=peer,
                # A rich message carries its body in ``rich_message``;
                # ``message`` must be empty.
                message="",
                rich_message=rich_type(markdown=rich_markdown),
                **kwargs,
            )
            result = await self._client(send)
        except Exception as exc:
            raise translate_flood_wait(exc) from exc

        message_id = _extract_rich_message_id(
            result, random_id=getattr(send, "random_id", None)
        )
        if message_id is None:
            # The request itself succeeded, so the article may well be in the
            # chat — the service quarantines this as ``needs_review`` instead of
            # a terminal ``failed`` that would invite a duplicate re-send.
            raise MessageSendUnconfirmed(
                "rich message send returned no message id in "
                f"{type(result).__name__}"
            )
        return message_id


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


#: Telegram caps a single ``messages.Search`` page at 100 rows.
_SEARCH_PAGE_SIZE = 100

#: Hard cap on ``messages.Search`` RPCs per call. Rows dropped by the *local*
#: filters (the private-chat sender filter, the exact date bounds) do not count
#: toward ``limit``, so without a cap a query whose matches are mostly filtered
#: out would page through a chat's entire match set — hundreds of sequential
#: RPCs and a self-inflicted FLOOD_WAIT. Returning a short result is the better
#: failure mode.
_SEARCH_MAX_PAGES = 20


def _search_usernames(result: Any) -> dict[int, str | None]:
    """Map ``sender_id -> username`` from a raw search result's envelope.

    Both ``users`` **and** ``chats`` are indexed: a broadcast post, an anonymous
    admin, or a channel-signed supergroup message reports the *channel* as its
    sender, and those senders only appear in ``chats``. Telethon marks channel
    sender ids (``-100…``), so chat entries are keyed the same way — that is
    what :func:`_search_sender` looks them up by.
    """
    from telethon import utils

    out: dict[int, str | None] = {}
    for user in getattr(result, "users", None) or ():
        user_id = getattr(user, "id", None)
        if user_id is None:
            continue
        out[int(user_id)] = getattr(user, "username", None)
    for chat in getattr(result, "chats", None) or ():
        chat_id = getattr(chat, "id", None)
        if chat_id is None:
            continue
        try:
            marked = int(utils.get_peer_id(chat))
        except (TypeError, ValueError):
            # A test double or an unknown entity shape: fall back to the bare id
            # rather than dropping the sender entirely.
            marked = int(chat_id)
        out[marked] = getattr(chat, "username", None)
    return out


def _search_sender(msg: Any, usernames: dict[int, str | None]) -> str | None:
    """Return the sender username for a raw search hit, if known.

    A message from ``messages.Search`` never goes through Telethon's
    ``_finish_init``, so ``msg.sender`` is normally ``None`` and the usernames
    have to come from the result envelope. The lookup keys on ``sender_id``,
    which ``Message.__init__`` derives from ``from_id``/``peer_id`` — unlike a
    bare ``from_id.user_id`` it also covers channel posts, anonymous admins and
    **incoming private messages** (layer 119+ drops ``from_id`` there), all of
    which would otherwise report no sender at all while ``messages recent``
    reports one for the very same message.
    """
    sender = getattr(msg, "sender", None)
    username = getattr(sender, "username", None) if sender is not None else None
    if username is not None:
        return username
    sender_id = getattr(msg, "sender_id", None)
    if sender_id is None:
        from_id = getattr(msg, "from_id", None)
        sender_id = getattr(from_id, "user_id", None) if from_id is not None else None
    if sender_id is None:
        return None
    return usernames.get(int(sender_id))


def _is_user_peer(peer: Any) -> bool:
    """Return ``True`` when ``peer`` addresses a 1:1 chat (a user or ourselves)."""
    from telethon.tl.types import InputPeerSelf, InputPeerUser, InputPeerUserFromMessage

    return isinstance(peer, InputPeerSelf | InputPeerUser | InputPeerUserFromMessage)


def _search_sender_id(msg: Any, *, self_id: int, peer_user_id: int) -> int | None:
    """Return the numeric sender of a raw private-chat hit.

    ``messages.Search`` rows are real Telethon ``Message`` objects, so
    ``sender_id`` is already derived from ``from_id``/``peer_id`` without any
    entity resolution. It stays ``None`` for *outgoing* private messages
    (layer 119+ drops ``from_id`` there), where the sender can only be us; an
    incoming one can only be the chat partner.
    """
    sender_id = getattr(msg, "sender_id", None)
    if sender_id is not None:
        return int(sender_id)
    return self_id if getattr(msg, "out", False) else peer_user_id


def _search_reply_to(msg: Any) -> int | None:
    """Return the replied-to message id of a raw search hit, if any."""
    reply_to = getattr(msg, "reply_to_msg_id", None)
    if reply_to is None:
        header = getattr(msg, "reply_to", None)
        reply_to = (
            getattr(header, "reply_to_msg_id", None) if header is not None else None
        )
    return int(reply_to) if reply_to else None


class TelethonSearchBackend:
    """Adapter from the Telethon ``TelegramClient`` to :class:`SearchBackend`.

    Issues Telegram's server-side search as a single ``messages.Search`` RPC per
    page, carrying **all** filters at once — ``q``, ``from_id``, ``top_msg_id``
    and both date bounds — instead of composing ``iter_messages(search=…,
    reply_to=…)``, whose ``query`` + ``topic_id`` combination is unreliable.

    ``from_date``/``to_date`` (already validated and UTC-normalised by the
    domain) are pushed down as ``min_date``/``max_date``, widened by a second on
    each side because Telegram's own bounds are second-granular and exclusive in
    places; the inclusive check is then re-applied here (and again in the domain)
    so the contract stays ``from_date <= date <= to_date``.

    Pages are collected newest-first via ``offset_id`` until ``limit`` in-range
    rows are gathered, a page comes back empty, the offset stops advancing, the
    newest id in a page is not greater than the page size (ids start at 1, so
    nothing older can exist), or ``_SEARCH_MAX_PAGES`` RPCs have been spent
    (locally filtered rows do not count toward ``limit``, so the cap keeps a
    mostly-filtered query from walking a whole chat). A *short* page is
    deliberately **not** a stop condition: channels may omit undisplayable
    messages, so ``len(page) < page_size`` would silently truncate the result —
    Telethon's own iterator refuses the same shortcut. Message ids are deduped
    across pages, and ``MessageEmpty`` placeholders are skipped (they carry no
    text or date) while still advancing the offset. ``FloodWaitError`` is
    translated so the worker queue can pause-and-retry rather than mark a
    generic failure.
    """

    def __init__(self, client: Any) -> None:
        self._client = client

    async def search_messages(
        self,
        *,
        chat_id: int,
        query: str,
        from_user: str | int | None = None,
        limit: int = 20,
        topic_id: int | None = None,
        from_date: datetime | None = None,
        to_date: datetime | None = None,
    ) -> list[RecentMessage]:
        from telethon.tl.functions.messages import SearchRequest
        from telethon.tl.types import InputMessagesFilterEmpty, MessageEmpty

        # Widen the server-side window by a second on each side; the exact
        # inclusive bounds are enforced below on the mapped rows.
        min_date = from_date - timedelta(seconds=1) if from_date is not None else None
        max_date = to_date + timedelta(seconds=1) if to_date is not None else None

        try:
            peer = await self._client.get_input_entity(chat_id)
            from_peer = (
                await self._client.get_input_entity(from_user)
                if from_user is not None
                else None
            )
            # Telegram *ignores* `from_id` when the peer is a user, so a private
            # chat searched with a sender filter would come back with both
            # sides' messages. Drop the (useless) server-side filter there and
            # apply it locally instead, the way Telethon's own message iterator
            # does — otherwise the filter silently does nothing.
            local_from_id: int | None = None
            self_id = 0
            peer_user_id = 0
            if from_peer is not None and _is_user_peer(peer):
                from telethon.tl.types import InputPeerSelf

                self_id = int(getattr(await self._client.get_me(), "id", 0) or 0)
                peer_user_id = (
                    self_id
                    if isinstance(peer, InputPeerSelf)
                    else int(getattr(peer, "user_id", 0) or 0)
                )
                local_from_id = (
                    self_id
                    if isinstance(from_peer, InputPeerSelf)
                    else int(getattr(from_peer, "user_id", 0) or 0)
                )
                from_peer = None
                if local_from_id not in (self_id, peer_user_id):
                    # A 1:1 chat only ever has two senders, so a third party can
                    # match nothing. Answer without paging: the local filter
                    # would otherwise discard every row and we would walk the
                    # whole match set to return an empty list anyway.
                    return []
            # Rows dropped by the *local* filters below do not count toward
            # `limit`, so tying the wire page size to `limit` would shrink the
            # `_SEARCH_MAX_PAGES` budget to `limit * _SEARCH_MAX_PAGES`
            # messages: `--limit 1` on a 1:1 chat whose 20 newest matches are
            # ours would answer `[]` while `--limit 20` finds them, making the
            # result silently depend on `limit`. Page at full width whenever a
            # local filter can drop rows; `out[:limit]` stays the only
            # truncation, and the `page[0].id <= page_size` stop condition holds
            # for any page size.
            locally_filtered = (
                local_from_id is not None or from_date is not None or to_date is not None
            )
            page_size = (
                _SEARCH_PAGE_SIZE
                if locally_filtered
                else max(1, min(limit, _SEARCH_PAGE_SIZE))
            )
            out: list[RecentMessage] = []
            seen: set[int] = set()
            offset_id = 0
            pages = 0
            while len(out) < limit and pages < _SEARCH_MAX_PAGES:
                pages += 1
                result = await self._client(
                    SearchRequest(
                        peer=peer,
                        q=query,
                        filter=InputMessagesFilterEmpty(),
                        min_date=min_date,
                        max_date=max_date,
                        offset_id=offset_id,
                        add_offset=0,
                        limit=page_size,
                        max_id=0,
                        min_id=0,
                        hash=0,
                        from_id=from_peer,
                        top_msg_id=topic_id,
                    )
                )
                page = list(getattr(result, "messages", None) or ())
                if not page:
                    break
                usernames = _search_usernames(result)
                next_offset = offset_id
                for msg in page:
                    msg_id = int(getattr(msg, "id", 0))
                    if next_offset == 0 or 0 < msg_id < next_offset:
                        next_offset = msg_id
                    if msg_id in seen:
                        continue
                    seen.add(msg_id)
                    # A deleted/undisplayable slot: it has an id (so it already
                    # moved the offset above) but no text, date or media, and
                    # emitting it would spend a `limit` slot on an empty row.
                    if isinstance(msg, MessageEmpty):
                        continue
                    if local_from_id is not None and (
                        _search_sender_id(
                            msg, self_id=self_id, peer_user_id=peer_user_id
                        )
                        != local_from_id
                    ):
                        continue
                    date = getattr(msg, "date", None)
                    if from_date is not None or to_date is not None:
                        if date is None:
                            continue
                        stamp = date if date.tzinfo is not None else date.replace(tzinfo=UTC)
                        stamp = stamp.astimezone(UTC)
                        if from_date is not None and stamp < from_date:
                            continue
                        if to_date is not None and stamp > to_date:
                            continue
                    text = getattr(msg, "message", "") or ""
                    if not text:
                        media = getattr(msg, "media", None)
                        if media is not None:
                            text = _media_summary(media)
                    out.append(
                        RecentMessage(
                            id=msg_id,
                            sender=_search_sender(msg, usernames),
                            date=date.isoformat() if date is not None else None,
                            reply_to=_search_reply_to(msg),
                            text=text,
                        )
                    )
                    if len(out) >= limit:
                        break
                # An offset that did not move older would replay the same page.
                if next_offset == offset_id or next_offset <= 0:
                    break
                offset_id = next_offset
                # `len(page) < page_size` is *not* a safe end-of-history signal
                # — a channel may drop undisplayable messages from a full slice,
                # and stopping there would hide older in-range matches. The safe
                # equivalent (Telethon uses the same one): message ids start at
                # 1, so a page whose newest id is not above the page size has
                # nothing older behind it.
                if int(getattr(page[0], "id", 0) or 0) <= page_size:
                    break
        except Exception as exc:
            raise translate_flood_wait(exc) from exc
        return out[:limit]


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
    "TelethonSearchBackend",
    "TelethonForwardBackend",
]
