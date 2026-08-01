"""Unit tests for :class:`TelethonMessageBackend`.

Covers the write side of the message adapter: text-only sends route through
``send_message``, attachment sends through ``send_file`` (single id vs album
list), scheduling and topic reply ids are forwarded, an empty caption becomes
``None``, and Telethon ``FloodWaitError`` is translated for the worker queue.
:class:`TelethonSearchBackend` is covered too: one ``messages.Search`` RPC
carrying every filter, plus ``offset_id`` pagination.
"""

from __future__ import annotations

import io
import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from telethon.tl.types import MessageEmpty, PeerUser

from telegram_assistant.messages import telethon_backend
from telegram_assistant.messages.service import (
    MessageSendFailed,
    MessageSendUnconfirmed,
    RichMessageUnsupported,
)
from telegram_assistant.messages.telethon_backend import (
    _SEARCH_PAGE_SIZE,
    TelethonDeleteBackend,
    TelethonMessageBackend,
    TelethonSearchBackend,
)
from telegram_assistant.observability.logging import configure_logging
from telegram_assistant.worker.queue import FloodWaitError


class _Sent:
    def __init__(self, msg_id: int) -> None:
        self.id = msg_id


class _RecordingClient:
    """Telethon client double recording send_message / send_file calls."""

    def __init__(self) -> None:
        self.message_calls: list[dict[str, Any]] = []
        self.file_calls: list[dict[str, Any]] = []

    async def send_message(self, chat_id: int, text: str, **kwargs: Any) -> Any:
        self.message_calls.append(
            {"chat_id": chat_id, "text": text, "kwargs": kwargs}
        )
        return _Sent(555)

    async def send_file(self, chat_id: int, files: Any, **kwargs: Any) -> Any:
        self.file_calls.append(
            {"chat_id": chat_id, "files": files, "kwargs": kwargs}
        )
        files = list(files)
        if len(files) > 1:
            return [_Sent(600 + i) for i in range(len(files))]
        return _Sent(600)


class _TelethonFloodWaitError(Exception):
    """Stand-in matching the upstream class name ``FloodWaitError``."""

    def __init__(self, seconds: int) -> None:
        super().__init__(f"flood wait {seconds}")
        self.seconds = seconds


# Rename so type(exc).__name__ == "FloodWaitError" for translate_flood_wait.
_TelethonFloodWaitError.__name__ = "FloodWaitError"


@pytest.mark.asyncio
async def test_text_only_routes_through_send_message() -> None:
    client = _RecordingClient()
    backend = TelethonMessageBackend(client)

    result = await backend.send_message(chat_id=-100123, text="hello")

    assert result == 555
    assert client.file_calls == []
    assert len(client.message_calls) == 1
    call = client.message_calls[0]
    assert call["chat_id"] == -100123
    assert call["text"] == "hello"
    # No topic / schedule kwargs for a plain send.
    assert call["kwargs"] == {}


@pytest.mark.asyncio
async def test_text_send_forwards_topic_and_schedule() -> None:
    client = _RecordingClient()
    backend = TelethonMessageBackend(client)
    when = datetime(2030, 1, 1, tzinfo=UTC)

    await backend.send_message(
        chat_id=42, text="hi", topic_id=7, schedule_at=when
    )

    call = client.message_calls[0]
    assert call["kwargs"]["reply_to"] == 7
    assert call["kwargs"]["schedule"] == when


@pytest.mark.asyncio
async def test_text_send_reply_to_message_id_sets_reply_to() -> None:
    client = _RecordingClient()
    backend = TelethonMessageBackend(client)

    await backend.send_message(chat_id=42, text="re", reply_to_message_id=99)

    assert client.message_calls[0]["kwargs"]["reply_to"] == 99


@pytest.mark.asyncio
async def test_reply_to_message_id_wins_over_topic_id() -> None:
    """A forum reply targets the message; replying inside a topic keeps it
    threaded, so an explicit reply id takes precedence over the topic root."""
    client = _RecordingClient()
    backend = TelethonMessageBackend(client)

    await backend.send_message(
        chat_id=42, text="re", topic_id=7, reply_to_message_id=99
    )

    assert client.message_calls[0]["kwargs"]["reply_to"] == 99


@pytest.mark.asyncio
async def test_file_send_reply_to_message_id_sets_reply_to() -> None:
    client = _RecordingClient()
    backend = TelethonMessageBackend(client)

    await backend.send_message(
        chat_id=10, text="cap", files=("/tmp/a.png",), reply_to_message_id=12
    )

    assert client.file_calls[0]["kwargs"]["reply_to"] == 12


@pytest.mark.asyncio
async def test_single_file_returns_single_id_with_caption() -> None:
    client = _RecordingClient()
    backend = TelethonMessageBackend(client)

    result = await backend.send_message(
        chat_id=10, text="caption", files=("/tmp/a.png",)
    )

    assert result == 600
    assert client.message_calls == []
    call = client.file_calls[0]
    assert call["files"] == ["/tmp/a.png"]
    assert call["kwargs"]["caption"] == "caption"


@pytest.mark.asyncio
async def test_album_returns_list_of_ids() -> None:
    client = _RecordingClient()
    backend = TelethonMessageBackend(client)

    result = await backend.send_message(
        chat_id=10, text="", files=("/tmp/a.png", "/tmp/b.png")
    )

    assert result == [600, 601]
    call = client.file_calls[0]
    assert call["files"] == ["/tmp/a.png", "/tmp/b.png"]
    # Empty caption must collapse to None so Telethon sends no extra text.
    assert call["kwargs"]["caption"] is None


@pytest.mark.asyncio
async def test_file_send_forwards_topic_and_schedule() -> None:
    client = _RecordingClient()
    backend = TelethonMessageBackend(client)
    when = datetime(2030, 6, 1, tzinfo=UTC)

    await backend.send_message(
        chat_id=10,
        text="cap",
        topic_id=3,
        files=("/tmp/a.png",),
        schedule_at=when,
    )

    call = client.file_calls[0]
    assert call["kwargs"]["reply_to"] == 3
    assert call["kwargs"]["schedule"] == when


@pytest.mark.asyncio
async def test_flood_wait_is_translated_on_text_send() -> None:
    class _Flooding(_RecordingClient):
        async def send_message(self, chat_id: int, text: str, **kwargs: Any) -> Any:
            raise _TelethonFloodWaitError(30)

    backend = TelethonMessageBackend(_Flooding())
    with pytest.raises(FloodWaitError):
        await backend.send_message(chat_id=1, text="boom")


@pytest.mark.asyncio
async def test_flood_wait_is_translated_on_file_send() -> None:
    class _Flooding(_RecordingClient):
        async def send_file(self, chat_id: int, files: Any, **kwargs: Any) -> Any:
            raise _TelethonFloodWaitError(45)

    backend = TelethonMessageBackend(_Flooding())
    with pytest.raises(FloodWaitError):
        await backend.send_message(chat_id=1, text="x", files=("/tmp/a.png",))


# ---------------------------------------------------------------------------
# Rich message send (raw SendMessageRequest + InputRichMessageMarkdown)
# ---------------------------------------------------------------------------


class _RawClient(_RecordingClient):
    """Records raw ``client(request)`` invocations and the resolved peer.

    The rich path cannot go through ``client.send_message`` — Telethon's
    high-level helper has no ``rich_message`` parameter — so it resolves an
    input peer and invokes the raw request.
    """

    def __init__(self, *, updates: Any = None) -> None:
        super().__init__()
        self.raw_calls: list[Any] = []
        self.peers: list[int] = []
        self._updates = updates

    async def get_input_entity(self, chat_id: int) -> Any:
        self.peers.append(chat_id)
        return f"peer:{chat_id}"

    async def __call__(self, request: Any) -> Any:
        self.raw_calls.append(request)
        if self._updates is None:
            # Telegram echoes the request's own ``random_id`` back; the default
            # envelope must too, or it looks like another request's update.
            return _rich_updates(4242, random_id=request.random_id)
        return self._updates


def _rich_updates(message_id: int, *, random_id: int) -> Any:
    """An ``Updates`` envelope shaped like the Task 1 spike observed."""
    from telethon.tl.types import UpdateMessageID, Updates

    return Updates(
        updates=[UpdateMessageID(id=message_id, random_id=random_id)],
        users=[],
        chats=[],
        date=datetime(2026, 1, 1, tzinfo=UTC),
        seq=0,
    )


RICH_MD = "# Heading\n\nBody paragraph.\n"


@pytest.mark.asyncio
async def test_rich_send_issues_raw_request_with_markdown() -> None:
    from telethon.tl.functions.messages import SendMessageRequest
    from telethon.tl.types import InputRichMessageMarkdown

    client = _RawClient()
    backend = TelethonMessageBackend(client)

    result = await backend.send_message(
        chat_id=-100123, text="", rich_markdown=RICH_MD
    )

    assert result == 4242
    # The high-level helpers are untouched — rich sends are raw-only.
    assert client.message_calls == []
    assert client.file_calls == []
    assert client.peers == [-100123]
    assert len(client.raw_calls) == 1
    request = client.raw_calls[0]
    assert isinstance(request, SendMessageRequest)
    assert request.peer == "peer:-100123"
    assert request.message == ""
    assert isinstance(request.rich_message, InputRichMessageMarkdown)
    assert request.rich_message.markdown == RICH_MD
    # v1 never sets the optional flags or inline files.
    assert request.rich_message.files is None
    assert not request.rich_message.rtl
    assert not request.rich_message.noautolink
    # Telethon fills random_id itself.
    assert request.random_id is not None
    assert request.reply_to is None
    assert request.schedule_date is None


@pytest.mark.asyncio
async def test_rich_send_forwards_schedule_date() -> None:
    client = _RawClient()
    backend = TelethonMessageBackend(client)
    when = datetime(2030, 1, 1, tzinfo=UTC)

    await backend.send_message(
        chat_id=42, text="", rich_markdown=RICH_MD, schedule_at=when
    )

    assert client.raw_calls[0].schedule_date == when


@pytest.mark.asyncio
async def test_rich_send_topic_only_replies_to_topic_root() -> None:
    from telethon.tl.types import InputReplyToMessage

    client = _RawClient()
    backend = TelethonMessageBackend(client)

    await backend.send_message(
        chat_id=42, text="", rich_markdown=RICH_MD, topic_id=7
    )

    reply_to = client.raw_calls[0].reply_to
    assert isinstance(reply_to, InputReplyToMessage)
    assert reply_to.reply_to_msg_id == 7
    assert reply_to.top_msg_id is None


@pytest.mark.asyncio
async def test_rich_send_reply_inside_topic_keeps_thread() -> None:
    """Mirrors the plain path: an explicit reply id wins, but the topic root
    still rides along as ``top_msg_id`` so the reply stays in the topic."""
    client = _RawClient()
    backend = TelethonMessageBackend(client)

    await backend.send_message(
        chat_id=42,
        text="",
        rich_markdown=RICH_MD,
        topic_id=7,
        reply_to_message_id=99,
    )

    reply_to = client.raw_calls[0].reply_to
    assert reply_to.reply_to_msg_id == 99
    assert reply_to.top_msg_id == 7


@pytest.mark.asyncio
async def test_rich_send_extracts_id_from_update_new_message() -> None:
    """No ``UpdateMessageID`` in the envelope — fall back to the new-message
    update's own id."""
    from telethon.tl.types import UpdateNewChannelMessage, Updates

    class _Msg:
        id = 777

    updates = Updates(
        updates=[UpdateNewChannelMessage(message=_Msg(), pts=1, pts_count=1)],
        users=[],
        chats=[],
        date=datetime(2026, 1, 1, tzinfo=UTC),
        seq=0,
    )
    backend = TelethonMessageBackend(_RawClient(updates=updates))

    assert await backend.send_message(
        chat_id=1, text="", rich_markdown=RICH_MD
    ) == 777


@pytest.mark.asyncio
async def test_rich_send_extracts_id_from_update_short_sent_message() -> None:
    """``messages.sendMessage`` answers 1:1 peers with ``UpdateShortSentMessage``
    — no update list, the new id on the envelope itself. Telethon's own sender
    special-cases it; missing it would report a delivered article as failed."""
    from telethon.tl.types import UpdateShortSentMessage

    sent = UpdateShortSentMessage(
        id=555, pts=2, pts_count=1, date=datetime(2026, 1, 1, tzinfo=UTC)
    )
    backend = TelethonMessageBackend(_RawClient(updates=sent))

    assert await backend.send_message(
        chat_id=1, text="", rich_markdown=RICH_MD
    ) == 555


@pytest.mark.asyncio
async def test_rich_send_unwraps_update_short_envelope() -> None:
    """``UpdateShort`` carries a single update under ``.update``."""
    from telethon.tl.types import UpdateMessageID, UpdateShort

    class _EchoingClient(_RawClient):
        async def __call__(self, request: Any) -> Any:
            self.raw_calls.append(request)
            return UpdateShort(
                update=UpdateMessageID(id=606, random_id=request.random_id),
                date=datetime(2026, 1, 1, tzinfo=UTC),
            )

    backend = TelethonMessageBackend(_EchoingClient())

    assert await backend.send_message(
        chat_id=1, text="", rich_markdown=RICH_MD
    ) == 606


@pytest.mark.asyncio
async def test_rich_send_extracts_id_from_update_new_scheduled_message() -> None:
    """A scheduled send answers with ``UpdateNewScheduledMessage``; Telethon's own
    extractor handles it, so the fallback must too — otherwise a successfully
    scheduled article would be reported as a failed send."""
    from telethon.tl.types import UpdateNewScheduledMessage, Updates

    class _Msg:
        id = 888

    updates = Updates(
        updates=[UpdateNewScheduledMessage(message=_Msg())],
        users=[],
        chats=[],
        date=datetime(2026, 1, 1, tzinfo=UTC),
        seq=0,
    )
    backend = TelethonMessageBackend(_RawClient(updates=updates))

    assert await backend.send_message(
        chat_id=1,
        text="",
        rich_markdown=RICH_MD,
        schedule_at=datetime(2030, 1, 1, tzinfo=UTC),
    ) == 888


@pytest.mark.asyncio
async def test_rich_send_prefers_update_matching_own_random_id() -> None:
    """An ``Updates`` container can carry updates belonging to another request.
    The id must be picked by *this* request's ``random_id``, not by position."""
    from telethon.tl.types import UpdateMessageID, Updates

    class _EchoingClient(_RawClient):
        async def __call__(self, request: Any) -> Any:
            self.raw_calls.append(request)
            return Updates(
                updates=[
                    # A stranger's update arrives first.
                    UpdateMessageID(id=111, random_id=12345),
                    UpdateMessageID(id=222, random_id=request.random_id),
                ],
                users=[],
                chats=[],
                date=datetime(2026, 1, 1, tzinfo=UTC),
                seq=0,
            )

    backend = TelethonMessageBackend(_EchoingClient())

    assert await backend.send_message(
        chat_id=1, text="", rich_markdown=RICH_MD
    ) == 222


@pytest.mark.asyncio
async def test_rich_send_accepts_an_unkeyed_update_message_id() -> None:
    """An ``UpdateMessageID`` carrying no ``random_id`` claims no other request,
    so it is still ours to read: refusing it would report a delivered article as
    an unconfirmed send and quarantine the operation for nothing."""
    from telethon.tl.types import Updates

    class _UnkeyedUpdateMessageID:
        def __init__(self, message_id: int) -> None:
            self.id = message_id
            self.random_id = None

    _UnkeyedUpdateMessageID.__name__ = "UpdateMessageID"

    class _EchoingClient(_RawClient):
        async def __call__(self, request: Any) -> Any:
            self.raw_calls.append(request)
            return Updates(
                updates=[_UnkeyedUpdateMessageID(909)],
                users=[],
                chats=[],
                date=datetime(2026, 1, 1, tzinfo=UTC),
                seq=0,
            )

    backend = TelethonMessageBackend(_EchoingClient())

    assert await backend.send_message(chat_id=1, text="", rich_markdown=RICH_MD) == 909


@pytest.mark.asyncio
async def test_rich_send_ignores_foreign_keyed_update_message_id() -> None:
    """A keyed ``UpdateMessageID`` that isn't ours must never be used as a
    fallback: its id would be returned as the article's *and* recorded in the
    ``SentMessageRegistry``, granting this session edit/delete over a message it
    never sent. Our own ``UpdateNewChannelMessage`` wins instead."""
    from telethon.tl.types import UpdateMessageID, UpdateNewChannelMessage, Updates

    class _Msg:
        id = 777

    class _EchoingClient(_RawClient):
        async def __call__(self, request: Any) -> Any:
            self.raw_calls.append(request)
            return Updates(
                updates=[
                    # Another request's update — same container, different key.
                    UpdateMessageID(id=111, random_id=request.random_id + 1),
                    UpdateNewChannelMessage(
                        message=_Msg(), pts=1, pts_count=1
                    ),
                ],
                users=[],
                chats=[],
                date=datetime(2026, 1, 1, tzinfo=UTC),
                seq=0,
            )

    backend = TelethonMessageBackend(_EchoingClient())

    assert await backend.send_message(
        chat_id=1, text="", rich_markdown=RICH_MD
    ) == 777


@pytest.mark.asyncio
async def test_rich_send_ignores_update_new_message_paired_with_foreign_id() -> None:
    """Telegram pairs an ``UpdateMessageID`` with the ``UpdateNew*Message`` for the
    *same* message, so the ``UpdateNew*Message`` scan must not hand back the id the
    ``random_id`` check just refused — otherwise the foreign-key guard is a no-op
    in exactly the shape it exists for."""
    from telethon.tl.types import UpdateMessageID, UpdateNewChannelMessage, Updates

    class _Msg:
        id = 111

    class _EchoingClient(_RawClient):
        async def __call__(self, request: Any) -> Any:
            self.raw_calls.append(request)
            return Updates(
                updates=[
                    # Another request's pair: both entries name message 111.
                    UpdateMessageID(id=111, random_id=request.random_id + 1),
                    UpdateNewChannelMessage(message=_Msg(), pts=1, pts_count=1),
                ],
                users=[],
                chats=[],
                date=datetime(2026, 1, 1, tzinfo=UTC),
                seq=0,
            )

    backend = TelethonMessageBackend(_EchoingClient())

    with pytest.raises(MessageSendUnconfirmed):
        await backend.send_message(chat_id=1, text="", rich_markdown=RICH_MD)


@pytest.mark.asyncio
async def test_rich_send_ignores_incoming_short_message_envelope_id() -> None:
    """``UpdateShortMessage`` is an *incoming*-message envelope that also carries
    an ``id`` — someone else's. Only ``UpdateShortSentMessage`` may be read off the
    envelope itself (Telethon's own extractor draws the same line)."""
    from telethon.tl.types import UpdateShortMessage

    envelope = UpdateShortMessage(
        id=999,
        user_id=42,
        message="not ours",
        pts=2,
        pts_count=1,
        date=datetime(2026, 1, 1, tzinfo=UTC),
    )
    backend = TelethonMessageBackend(_RawClient(updates=envelope))

    with pytest.raises(MessageSendUnconfirmed):
        await backend.send_message(chat_id=1, text="", rich_markdown=RICH_MD)


@pytest.mark.asyncio
async def test_rich_send_foreign_update_message_id_alone_is_unconfirmed() -> None:
    """With only a foreign ``UpdateMessageID`` in the envelope there is no id we
    can claim, so the send is unconfirmed (needs_review) rather than silently
    reporting a stranger's message id."""
    from telethon.tl.types import UpdateMessageID, Updates

    class _EchoingClient(_RawClient):
        async def __call__(self, request: Any) -> Any:
            self.raw_calls.append(request)
            return Updates(
                updates=[UpdateMessageID(id=111, random_id=request.random_id + 1)],
                users=[],
                chats=[],
                date=datetime(2026, 1, 1, tzinfo=UTC),
                seq=0,
            )

    backend = TelethonMessageBackend(_EchoingClient())

    with pytest.raises(MessageSendUnconfirmed):
        await backend.send_message(chat_id=1, text="", rich_markdown=RICH_MD)


@pytest.mark.asyncio
async def test_rich_send_without_message_id_raises_unconfirmed() -> None:
    """The request succeeded, so delivery is *uncertain*, not failed: this must
    not be ``MessageSendFailed`` (which the surfaces render as 409
    previous_attempt_failed) — the service turns it into ``needs_review``."""
    from telethon.tl.types import Updates

    updates = Updates(
        updates=[],
        users=[],
        chats=[],
        date=datetime(2026, 1, 1, tzinfo=UTC),
        seq=0,
    )
    backend = TelethonMessageBackend(_RawClient(updates=updates))

    with pytest.raises(MessageSendUnconfirmed, match="message id"):
        await backend.send_message(chat_id=1, text="", rich_markdown=RICH_MD)
    assert not issubclass(MessageSendUnconfirmed, MessageSendFailed)


@pytest.mark.asyncio
async def test_rich_send_flood_wait_is_translated() -> None:
    class _Flooding(_RawClient):
        async def __call__(self, request: Any) -> Any:
            raise _TelethonFloodWaitError(30)

    backend = TelethonMessageBackend(_Flooding())
    with pytest.raises(FloodWaitError):
        await backend.send_message(chat_id=1, text="", rich_markdown=RICH_MD)


@pytest.mark.asyncio
async def test_rich_send_on_old_telethon_reports_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Telethon < 1.44 (layer < 227) has no ``InputRichMessageMarkdown``; the
    caller must learn that, not see an ImportError or AttributeError.

    It must also not be ``MessageSendFailed``: that is the idempotency-replay
    class, and the surfaces render it as ``previous_attempt_failed`` (409 / exit
    2), pointing the operator at a prior attempt instead of the Telethon pin.
    """
    monkeypatch.setattr(
        telethon_backend, "_import_rich_markdown_type", lambda: None
    )
    client = _RawClient()
    backend = TelethonMessageBackend(client)

    with pytest.raises(RichMessageUnsupported, match=r"telethon>=1\.44"):
        await backend.send_message(chat_id=1, text="", rich_markdown=RICH_MD)

    assert client.raw_calls == []
    assert not issubclass(RichMessageUnsupported, MessageSendFailed)


@pytest.mark.asyncio
async def test_rich_send_rejects_attachments() -> None:
    """Belt-and-braces: the service already forbids the combination, but the
    backend must not silently drop either half."""
    backend = TelethonMessageBackend(_RawClient())

    with pytest.raises(ValueError, match="rich_markdown"):
        await backend.send_message(
            chat_id=1, text="", rich_markdown=RICH_MD, files=("/tmp/a.png",)
        )


# ---------------------------------------------------------------------------
# Rich message media (upload + InputRichMessageMarkdown.files)
# ---------------------------------------------------------------------------


def _uploaded_media(request: Any) -> Any:
    """Answer ``messages.uploadMedia`` the way Telegram does: a MessageMedia*."""
    from telethon.tl.types import (
        Document,
        InputMediaUploadedPhoto,
        MessageMediaDocument,
        MessageMediaPhoto,
        Photo,
    )

    date = datetime(2026, 1, 1, tzinfo=UTC)
    if isinstance(request.media, InputMediaUploadedPhoto):
        return MessageMediaPhoto(
            photo=Photo(
                id=101,
                access_hash=202,
                file_reference=b"ref-photo",
                date=date,
                sizes=[],
                dc_id=2,
            )
        )
    return MessageMediaDocument(
        document=Document(
            id=303,
            access_hash=404,
            file_reference=b"ref-doc",
            date=date,
            mime_type=request.media.mime_type,
            size=1,
            dc_id=2,
            attributes=list(request.media.attributes),
        )
    )


class _UploadingClient(_RawClient):
    """``_RawClient`` that also answers ``upload_file`` / ``messages.uploadMedia``."""

    def __init__(
        self,
        *,
        upload_error: Exception | None = None,
        send_error: Exception | None = None,
    ) -> None:
        super().__init__()
        self.uploads: list[str] = []
        self.upload_media: list[Any] = []
        self._upload_error = upload_error
        self._send_error = send_error

    async def upload_file(self, path: Any, **kwargs: Any) -> Any:
        if self._upload_error is not None:
            raise self._upload_error
        label = kwargs.get("file_name") or str(path)
        self.uploads.append(label)
        return f"handle:{label}"

    async def __call__(self, request: Any) -> Any:
        from telethon.tl.functions.messages import UploadMediaRequest

        if isinstance(request, UploadMediaRequest):
            self.upload_media.append(request)
            return _uploaded_media(request)
        if self._send_error is not None:
            raise self._send_error
        return await super().__call__(request)


def _rich_file(tmp_path: Any, name: str, file_id: str, kind: str) -> Any:
    from telegram_assistant.messages.rich_markdown import RichFile

    path = tmp_path / name
    path.write_bytes(b"\x00binary")
    return RichFile(id=file_id, path=str(path), caption="", kind=kind)


MEDIA_MD = (
    "# Article\n"
    "\n"
    "![shot](tg://photo?id=shot)\n"
    "\n"
    "![clip](tg://video?id=clip)\n"
)


@pytest.mark.asyncio
async def test_rich_send_uploads_files_in_markdown_order(
    tmp_path: Any, monkeypatch: Any
) -> None:
    """Each file is uploaded once, in the markdown's order, and comes back as an
    ``InputRichFile`` keyed by the id the body already names."""
    from telethon.tl.functions.messages import UploadMediaRequest
    from telethon.tl.types import (
        InputMediaUploadedDocument,
        InputMediaUploadedPhoto,
        InputRichFileDocument,
        InputRichFilePhoto,
    )

    from telegram_assistant.messages import media_probe

    _fake_probe(monkeypatch, None)
    monkeypatch.setattr(media_probe, "extract_thumbnail", lambda path, *, duration: None)
    photo = _rich_file(tmp_path, "shot.png", "shot", "photo")
    video = _rich_file(tmp_path, "clip.mp4", "clip", "video")
    client = _UploadingClient()
    backend = TelethonMessageBackend(client)

    result = await backend.send_message(
        chat_id=-100123,
        text="",
        rich_markdown=MEDIA_MD,
        rich_files=(photo, video),
    )

    assert result == 4242
    assert client.uploads == [photo.path, video.path]
    assert [type(call) for call in client.upload_media] == [
        UploadMediaRequest,
        UploadMediaRequest,
    ]
    # uploadMedia binds the upload to the destination peer, resolved once.
    assert client.peers == [-100123]
    assert [call.peer for call in client.upload_media] == [
        "peer:-100123",
        "peer:-100123",
    ]
    assert isinstance(client.upload_media[0].media, InputMediaUploadedPhoto)
    assert isinstance(client.upload_media[1].media, InputMediaUploadedDocument)
    assert client.upload_media[1].media.mime_type == "video/mp4"

    rich_message = client.raw_calls[-1].rich_message
    assert rich_message.markdown == MEDIA_MD
    files = rich_message.files
    assert [type(f) for f in files] == [InputRichFilePhoto, InputRichFileDocument]
    # The ids are the ones the markdown's tg:// references name — a mismatch
    # would send an article whose media blocks point at nothing.
    assert [f.id for f in files] == ["shot", "clip"]
    assert files[0].photo.id == 101
    assert files[1].document.id == 303


@pytest.mark.asyncio
async def test_rich_send_video_carries_video_attribute(
    tmp_path: Any, monkeypatch: Any
) -> None:
    """``tg://video`` only resolves when the upload really is a video document."""
    from telethon.tl.types import DocumentAttributeVideo

    from telegram_assistant.messages import media_probe

    _fake_probe(monkeypatch, None)
    monkeypatch.setattr(media_probe, "extract_thumbnail", lambda path, *, duration: None)
    video = _rich_file(tmp_path, "clip.mp4", "clip", "video")
    client = _UploadingClient()
    backend = TelethonMessageBackend(client)

    await backend.send_message(
        chat_id=1, text="", rich_markdown=MEDIA_MD, rich_files=(video,)
    )

    attributes = client.upload_media[0].media.attributes
    assert any(isinstance(attr, DocumentAttributeVideo) for attr in attributes)


@pytest.mark.asyncio
async def test_rich_send_audio_gets_audio_attribute(
    tmp_path: Any, monkeypatch: Any
) -> None:
    """Telethon only infers ``DocumentAttributeAudio`` with a metadata library
    installed; without it an ``.mp3`` would be a plain document and
    ``tg://audio`` would not resolve."""
    from telethon.tl.types import DocumentAttributeAudio

    _fake_probe(monkeypatch, None)
    audio = _rich_file(tmp_path, "voice.mp3", "voice", "audio")
    client = _UploadingClient()
    backend = TelethonMessageBackend(client)

    await backend.send_message(
        chat_id=1,
        text="",
        rich_markdown="![](tg://audio?id=voice)\n",
        rich_files=(audio,),
    )

    media = client.upload_media[0].media
    assert media.mime_type == "audio/mpeg"
    assert any(isinstance(attr, DocumentAttributeAudio) for attr in media.attributes)


@pytest.mark.asyncio
async def test_rich_send_without_media_uploads_nothing() -> None:
    """The media-less article keeps the pre-media shape: no upload RPC, and
    ``files`` left unset so a backend/server predating media sees no change."""
    client = _UploadingClient()
    backend = TelethonMessageBackend(client)

    await backend.send_message(chat_id=1, text="", rich_markdown=RICH_MD)

    assert client.uploads == []
    assert client.upload_media == []
    assert client.raw_calls[0].rich_message.files is None


@pytest.mark.asyncio
async def test_rich_files_without_markdown_rejected(tmp_path: Any) -> None:
    """The ids only mean something to the markdown naming them."""
    client = _UploadingClient()
    backend = TelethonMessageBackend(client)

    with pytest.raises(ValueError, match="rich_files requires rich_markdown"):
        await backend.send_message(
            chat_id=1,
            text="hi",
            rich_files=(_rich_file(tmp_path, "shot.png", "shot", "photo"),),
        )
    assert client.uploads == []


@pytest.mark.asyncio
async def test_rich_media_on_old_telethon_reports_version(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``InputRichFilePhoto``/``InputRichFileDocument`` arrived with layer 227.
    Like the markdown probe, a missing pair is a deployment problem naming the
    version — never ``MessageSendFailed`` — and nothing is uploaded first."""
    monkeypatch.setattr(telethon_backend, "_import_rich_file_types", lambda: None)
    client = _UploadingClient()
    backend = TelethonMessageBackend(client)

    with pytest.raises(RichMessageUnsupported, match=r"telethon>=1\.44"):
        await backend.send_message(
            chat_id=1,
            text="",
            rich_markdown=MEDIA_MD,
            rich_files=(_rich_file(tmp_path, "shot.png", "shot", "photo"),),
        )

    assert client.uploads == []
    assert client.raw_calls == []
    assert not issubclass(RichMessageUnsupported, MessageSendFailed)


class _ChatSendMediaForbiddenError(Exception):
    """Stand-in matching the upstream class name."""


_ChatSendMediaForbiddenError.__name__ = "ChatSendMediaForbiddenError"


@pytest.mark.asyncio
async def test_media_rights_rejection_on_upload_names_chat(tmp_path: Any) -> None:
    """A chat that forbids media rejects the whole article — there is no
    media-less half to fall back to, so the caller is told which chat refused."""
    from telegram_assistant.messages.service import RichMediaForbidden

    client = _UploadingClient(upload_error=_ChatSendMediaForbiddenError("no media"))
    backend = TelethonMessageBackend(client)

    with pytest.raises(RichMediaForbidden, match="-100777") as excinfo:
        await backend.send_message(
            chat_id=-100777,
            text="",
            rich_markdown=MEDIA_MD,
            rich_files=(_rich_file(tmp_path, "shot.png", "shot", "photo"),),
        )

    assert "ChatSendMediaForbiddenError" in str(excinfo.value)
    # A ValueError, so every surface's existing 400 / exit-2 path carries it.
    assert isinstance(excinfo.value, ValueError)
    assert client.raw_calls == []


@pytest.mark.asyncio
async def test_media_rights_rejection_on_send_names_chat() -> None:
    """The article's own media (a remote URL, say) can be refused by the send
    itself, with nothing uploaded — same mapping."""
    from telegram_assistant.messages.service import RichMediaForbidden

    client = _UploadingClient(send_error=_ChatSendMediaForbiddenError("no media"))
    backend = TelethonMessageBackend(client)

    with pytest.raises(RichMediaForbidden, match="42"):
        await backend.send_message(chat_id=42, text="", rich_markdown=RICH_MD)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "rpc_code", ["CHAT_SEND_DOCS_FORBIDDEN", "CHAT_SEND_AUDIOS_FORBIDDEN"]
)
async def test_media_rights_rejection_telethon_leaves_unnamed(rpc_code: str) -> None:
    """Telethon 1.44 generates no class for these two codes — they arrive as a
    bare ``ForbiddenError`` — and they cover exactly the kinds local uploads
    added (video/``.gif`` are documents, ``tg://audio`` is an audio document).
    Built through Telethon's own factory, not a name-shaped stand-in, so the
    mapping is pinned against what the wire actually produces."""
    from telethon.errors import rpc_message_to_error

    from telegram_assistant.messages.service import RichMediaForbidden

    class _RawRpcError:
        error_code = 403
        error_message = rpc_code

    error = rpc_message_to_error(_RawRpcError(), request=None)
    assert type(error).__name__ == "ForbiddenError"

    client = _UploadingClient(send_error=error)
    backend = TelethonMessageBackend(client)

    with pytest.raises(RichMediaForbidden, match="42") as excinfo:
        await backend.send_message(chat_id=42, text="", rich_markdown=RICH_MD)

    assert rpc_code in str(excinfo.value)


@pytest.mark.asyncio
async def test_plain_send_rejection_is_not_a_media_rights_problem() -> None:
    """``CHAT_SEND_PLAIN_FORBIDDEN`` matches the ``CHAT_SEND_*_FORBIDDEN``
    shape but is about text, not media — Telethon names it, and its ``message``
    is a bare ``FORBIDDEN``, so the RPC-string fallback must not claim it."""
    from telethon.errors import rpc_message_to_error

    from telegram_assistant.messages.service import RichMediaForbidden

    class _RawRpcError:
        error_code = 403
        error_message = "CHAT_SEND_PLAIN_FORBIDDEN"

    error = rpc_message_to_error(_RawRpcError(), request=None)
    client = _UploadingClient(send_error=error)
    backend = TelethonMessageBackend(client)

    with pytest.raises(Exception) as excinfo:
        await backend.send_message(chat_id=42, text="", rich_markdown=RICH_MD)

    assert not isinstance(excinfo.value, RichMediaForbidden)


class _MediaCaptionTooLongError(Exception):
    """Stand-in matching the upstream class name."""


_MediaCaptionTooLongError.__name__ = "MediaCaptionTooLongError"


@pytest.mark.asyncio
async def test_too_long_caption_is_not_reported_as_a_rights_problem() -> None:
    """An over-long caption is bad input, not a permission problem: telling the
    operator the chat forbids media would send them to check admin rights
    instead of shortening the caption. Still a ``ValueError``, so it keeps the
    400 / exit-2 path."""
    from telegram_assistant.messages.service import RichMediaForbidden

    client = _UploadingClient(send_error=_MediaCaptionTooLongError("too long"))
    backend = TelethonMessageBackend(client)

    with pytest.raises(ValueError) as excinfo:
        await backend.send_message(chat_id=42, text="", rich_markdown=RICH_MD)

    assert not isinstance(excinfo.value, RichMediaForbidden)
    assert "caption" in str(excinfo.value)
    assert "does not allow the media" not in str(excinfo.value)


@pytest.mark.asyncio
async def test_a_gif_is_uploaded_as_a_converted_mp4(
    tmp_path: Any, monkeypatch: Any
) -> None:
    """An ``image/gif`` upload is transcoded server-side only below an
    undocumented size threshold (a 21.2 MB gif kept ``image/gif`` and failed the
    send with ``RICH_MESSAGE_VIDEO_INVALID``). Telegram stores "GIFs" as silent
    mp4 documents marked ``DocumentAttributeAnimated``, so conversion runs
    unconditionally before the file is uploaded."""
    from telethon.tl.types import DocumentAttributeAnimated, DocumentAttributeFilename

    from telegram_assistant.messages import media_probe
    from telegram_assistant.messages.media_probe import MediaProbe

    converted = tmp_path / "converted.mp4"
    converted.write_bytes(b"\x00mp4")
    monkeypatch.setattr(media_probe, "convert_gif_to_mp4", lambda path: converted)
    _fake_probe(
        monkeypatch,
        MediaProbe(duration=3.5, width=480, height=270, has_video=True, has_audio=False),
    )
    monkeypatch.setattr(media_probe, "extract_thumbnail", lambda path, *, duration: None)
    gif = _rich_file(tmp_path, "loop.gif", "loop", "video")
    client = _UploadingClient()
    backend = TelethonMessageBackend(client)

    await backend.send_message(
        chat_id=1,
        text="",
        rich_markdown="![](tg://video?id=loop)\n",
        rich_files=(gif,),
    )

    media = client.upload_media[0].media
    assert media.mime_type == "video/mp4"
    assert any(isinstance(a, DocumentAttributeAnimated) for a in media.attributes)
    names = [a.file_name for a in media.attributes if isinstance(a, DocumentAttributeFilename)]
    # The temp file's name must not leak: the article shows the author's name.
    assert names == ["loop.mp4"]
    assert client.uploads == [str(converted)]


@pytest.mark.asyncio
async def test_a_converted_gifs_probe_failure_is_logged_with_the_original_name(
    tmp_path: Any, monkeypatch: Any, _restore_logging: Any
) -> None:
    """For a ``.gif`` the probe and thumbnail run against the temp mp4, but a
    warning naming that temp path is useless once the ``finally`` block unlinks
    it — the log must name the author's ``loop.gif`` instead."""
    from telegram_assistant.messages import media_probe

    converted = tmp_path / "converted.mp4"
    converted.write_bytes(b"\x00mp4")
    monkeypatch.setattr(media_probe, "convert_gif_to_mp4", lambda path: converted)
    _fake_probe(monkeypatch, None)
    monkeypatch.setattr(media_probe, "extract_thumbnail", lambda path, *, duration: None)
    gif = _rich_file(tmp_path, "loop.gif", "loop", "video")
    client = _UploadingClient()
    backend = TelethonMessageBackend(client)

    buf = io.StringIO()
    configure_logging(level="DEBUG", stream=buf, force=True)

    await backend.send_message(
        chat_id=1,
        text="",
        rich_markdown="![](tg://video?id=loop)\n",
        rich_files=(gif,),
    )

    lines = [json.loads(line) for line in buf.getvalue().strip().splitlines() if line.strip()]
    warnings = [r for r in lines if r.get("level") == "warning"]
    assert warnings, lines
    assert all("loop.gif" in r.get("path", "") for r in warnings), lines
    assert not any("converted.mp4" in r.get("path", "") for r in warnings), lines


@pytest.mark.asyncio
async def test_the_converted_gif_temp_file_is_removed(
    tmp_path: Any, monkeypatch: Any
) -> None:
    from telegram_assistant.messages import media_probe
    from telegram_assistant.messages.media_probe import MediaProbe

    converted = tmp_path / "converted.mp4"
    converted.write_bytes(b"\x00mp4")
    monkeypatch.setattr(media_probe, "convert_gif_to_mp4", lambda path: converted)
    _fake_probe(
        monkeypatch,
        MediaProbe(duration=3.5, width=480, height=270, has_video=True, has_audio=False),
    )
    monkeypatch.setattr(media_probe, "extract_thumbnail", lambda path, *, duration: None)
    gif = _rich_file(tmp_path, "loop.gif", "loop", "video")
    client = _UploadingClient()
    backend = TelethonMessageBackend(client)

    await backend.send_message(
        chat_id=1,
        text="",
        rich_markdown="![](tg://video?id=loop)\n",
        rich_files=(gif,),
    )

    assert not converted.exists()


@pytest.mark.asyncio
async def test_the_converted_gif_temp_file_is_removed_after_a_failed_send(
    tmp_path: Any, monkeypatch: Any
) -> None:
    """A failed upload must not leave the temp mp4 behind."""
    from telegram_assistant.messages import media_probe
    from telegram_assistant.messages.media_probe import MediaProbe

    converted = tmp_path / "converted.mp4"
    converted.write_bytes(b"\x00mp4")
    monkeypatch.setattr(media_probe, "convert_gif_to_mp4", lambda path: converted)
    _fake_probe(
        monkeypatch,
        MediaProbe(duration=3.5, width=480, height=270, has_video=True, has_audio=False),
    )
    monkeypatch.setattr(media_probe, "extract_thumbnail", lambda path, *, duration: None)
    gif = _rich_file(tmp_path, "loop.gif", "loop", "video")
    client = _UploadingClient(upload_error=RuntimeError("boom"))
    backend = TelethonMessageBackend(client)

    with pytest.raises(Exception):  # noqa: B017 - any translated error is fine here
        await backend.send_message(
            chat_id=1,
            text="",
            rich_markdown="![](tg://video?id=loop)\n",
            rich_files=(gif,),
        )

    assert not converted.exists()


@pytest.mark.asyncio
async def test_a_failed_gif_conversion_is_a_value_error(
    tmp_path: Any, monkeypatch: Any
) -> None:
    """``MediaConversionError`` reaches the surfaces as a ``ValueError`` so the
    operator reads the ffmpeg reason on exit 2, not an empty 500."""
    from telegram_assistant.messages import media_probe

    def boom(path: Any) -> Any:
        raise media_probe.MediaConversionError("ffmpeg: moov atom not found")

    monkeypatch.setattr(media_probe, "convert_gif_to_mp4", boom)
    gif = _rich_file(tmp_path, "loop.gif", "loop", "video")
    client = _UploadingClient()
    backend = TelethonMessageBackend(client)

    with pytest.raises(ValueError, match="moov atom"):
        await backend.send_message(
            chat_id=1,
            text="",
            rich_markdown="![](tg://video?id=loop)\n",
            rich_files=(gif,),
        )

    assert client.upload_media == []


@pytest.mark.asyncio
async def test_flood_wait_during_upload_is_translated(tmp_path: Any) -> None:
    """FLOOD_WAIT stays a queue-visible pause even when it hits the upload."""
    client = _UploadingClient(upload_error=_TelethonFloodWaitError(20))
    backend = TelethonMessageBackend(client)

    with pytest.raises(FloodWaitError):
        await backend.send_message(
            chat_id=1,
            text="",
            rich_markdown=MEDIA_MD,
            rich_files=(_rich_file(tmp_path, "shot.png", "shot", "photo"),),
        )


# ---------------------------------------------------------------------------
# TelethonDeleteBackend
# ---------------------------------------------------------------------------


class _DeletingClient:
    """Telethon client double recording get_input_entity / delete_messages."""

    def __init__(self) -> None:
        self.delete_calls: list[dict[str, Any]] = []

    async def get_input_entity(self, chat_id: int) -> Any:
        return f"peer:{chat_id}"

    async def delete_messages(
        self, entity: Any, message_ids: Any, *, revoke: bool = True
    ) -> Any:
        self.delete_calls.append(
            {"entity": entity, "message_ids": list(message_ids), "revoke": revoke}
        )
        return []


@pytest.mark.asyncio
async def test_delete_backend_revoke_default_true() -> None:
    client = _DeletingClient()
    backend = TelethonDeleteBackend(client)
    count = await backend.delete_messages(chat_id=-100, message_ids=(11, 12))
    assert count == 2
    call = client.delete_calls[0]
    assert call["entity"] == "peer:-100"
    assert call["message_ids"] == [11, 12]
    assert call["revoke"] is True


@pytest.mark.asyncio
async def test_delete_backend_no_revoke() -> None:
    client = _DeletingClient()
    backend = TelethonDeleteBackend(client)
    await backend.delete_messages(chat_id=5, message_ids=(7,), revoke=False)
    assert client.delete_calls[0]["revoke"] is False


@pytest.mark.asyncio
async def test_delete_backend_flood_wait_is_translated() -> None:
    class _Flooding(_DeletingClient):
        async def delete_messages(
            self, entity: Any, message_ids: Any, *, revoke: bool = True
        ) -> Any:
            raise _TelethonFloodWaitError(15)

    backend = TelethonDeleteBackend(_Flooding())
    with pytest.raises(FloodWaitError):
        await backend.delete_messages(chat_id=1, message_ids=(1,))


# ---------------------------------------------------------------------------
# TelethonSearchBackend
# ---------------------------------------------------------------------------


class _SearchMsg:
    """Raw ``messages.Search`` hit — no resolved ``sender``, nested reply header."""

    def __init__(
        self,
        msg_id: int,
        *,
        date: datetime | None = None,
        message: str = "",
        from_id: Any = None,
        reply_to: Any = None,
        media: Any = None,
        sender_id: int | None = None,
    ) -> None:
        self.id = msg_id
        self.date = date
        self.message = message
        self.from_id = from_id
        self.reply_to = reply_to
        self.media = media
        # Telethon's ``Message.__init__`` derives this from ``from_id``/
        # ``peer_id`` before any entity resolution, so a raw search hit always
        # carries it — including for channel posts and incoming private
        # messages, which have no ``from_id`` at all.
        self.sender_id = (
            sender_id
            if sender_id is not None
            else getattr(from_id, "user_id", None)
            if from_id is not None
            else None
        )


class _PeerUser:
    def __init__(self, user_id: int) -> None:
        self.user_id = user_id


class _ReplyHeader:
    def __init__(self, reply_to_msg_id: int) -> None:
        self.reply_to_msg_id = reply_to_msg_id


class _User:
    def __init__(self, user_id: int, username: str | None) -> None:
        self.id = user_id
        self.username = username


class _SearchPage:
    def __init__(
        self,
        messages: list[Any],
        users: list[Any] | None = None,
        chats: list[Any] | None = None,
    ) -> None:
        self.messages = messages
        self.users = list(users or [])
        self.chats = list(chats or [])


class _SearchingClient:
    """Telethon client double serving canned ``messages.Search`` pages.

    The last page is served repeatedly so a broken termination condition shows
    up as the request cap below rather than an infinite loop.
    """

    def __init__(self, pages: list[_SearchPage]) -> None:
        self._pages = list(pages) or [_SearchPage([])]
        self.requests: list[Any] = []
        self.entity_calls: list[Any] = []

    async def get_input_entity(self, ref: Any) -> Any:
        self.entity_calls.append(ref)
        return f"peer:{ref}"

    async def __call__(self, request: Any) -> Any:
        self.requests.append(request)
        assert len(self.requests) <= 10, "search paging did not terminate"
        if len(self._pages) > 1:
            return self._pages.pop(0)
        return self._pages[0]


_BASE = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
_FROM = _BASE - timedelta(hours=1)
_TO = _BASE + timedelta(hours=1)


@pytest.mark.asyncio
async def test_search_builds_one_request_with_all_filters() -> None:
    """The flagged combination (query + topic + sender + range) is one RPC."""
    client = _SearchingClient(
        [_SearchPage([_SearchMsg(1010, date=_BASE, message="hit")])]
    )
    backend = TelethonSearchBackend(client)

    rows = await backend.search_messages(
        chat_id=-100777,
        query="report",
        from_user="@bob",
        limit=5,
        topic_id=42,
        from_date=_FROM,
        to_date=_TO,
    )

    assert [row.id for row in rows] == [1010]
    request = client.requests[0]
    assert request.peer == "peer:-100777"
    assert request.q == "report"
    assert request.from_id == "peer:@bob"
    assert request.top_msg_id == 42
    # The range filter is re-applied locally, so the wire page is full width
    # rather than `limit` — see `test_search_page_width_survives_a_small_limit`.
    assert request.limit == _SEARCH_PAGE_SIZE
    assert request.offset_id == 0
    # Bounds are widened by a second; the exact inclusive check runs on rows.
    assert request.min_date == _FROM - timedelta(seconds=1)
    assert request.max_date == _TO + timedelta(seconds=1)
    # A short page is not an end-of-history signal, so paging asks once more
    # with an advanced offset — carrying the very same filter set.
    assert [r.offset_id for r in client.requests] == [0, 1010]
    assert all(
        (r.q, r.from_id, r.top_msg_id, r.min_date, r.max_date)
        == (
            request.q,
            request.from_id,
            request.top_msg_id,
            request.min_date,
            request.max_date,
        )
        for r in client.requests
    )


@pytest.mark.asyncio
async def test_search_does_not_stop_on_a_short_page() -> None:
    """Channels may drop undisplayable rows from a full slice (Telethon's own
    caveat), so ``len(page) < page_size`` must not hide older matches."""
    client = _SearchingClient(
        [
            # Two rows for a full-width page: a naive short-page break would
            # stop here and never see id 940.
            _SearchPage([_SearchMsg(960, date=_BASE, message="a"), _SearchMsg(950, date=_BASE, message="b")]),
            _SearchPage([_SearchMsg(940, date=_BASE, message="c")]),
            _SearchPage([]),
        ]
    )
    backend = TelethonSearchBackend(client)

    rows = await backend.search_messages(
        chat_id=1, query="x", limit=5, from_date=_FROM, to_date=_TO
    )

    assert [row.id for row in rows] == [960, 950, 940]


@pytest.mark.asyncio
async def test_search_stops_when_newest_id_cannot_have_older_messages() -> None:
    """Ids start at 1, so a newest id <= page size ends history without an
    extra RPC."""
    client = _SearchingClient([_SearchPage([_SearchMsg(2, date=_BASE, message="a")])])
    backend = TelethonSearchBackend(client)

    rows = await backend.search_messages(
        chat_id=1, query="x", limit=5, from_date=_FROM, to_date=_TO
    )

    assert [row.id for row in rows] == [2]
    assert len(client.requests) == 1


@pytest.mark.asyncio
async def test_search_skips_message_empty_rows() -> None:
    """``MessageEmpty`` carries no text/date; it must not spend a limit slot."""
    client = _SearchingClient(
        [
            _SearchPage(
                [
                    _SearchMsg(9, date=_BASE, message="kept"),
                    MessageEmpty(id=8, peer_id=PeerUser(1)),
                    _SearchMsg(7, date=_BASE, message="also kept"),
                ]
            )
        ]
    )
    backend = TelethonSearchBackend(client)

    rows = await backend.search_messages(chat_id=1, query="x", limit=20)

    assert [row.id for row in rows] == [9, 7]


@pytest.mark.asyncio
async def test_search_without_range_sends_no_date_bounds() -> None:
    client = _SearchingClient(
        [_SearchPage([_SearchMsg(3, date=_BASE, message="a")])]
    )
    backend = TelethonSearchBackend(client)

    rows = await backend.search_messages(chat_id=5, query="a", limit=2)

    assert [row.id for row in rows] == [3]
    request = client.requests[0]
    assert request.min_date is None
    assert request.max_date is None
    assert request.from_id is None
    assert request.top_msg_id is None
    # No sender lookup when from_user is absent.
    assert client.entity_calls == [5]


@pytest.mark.asyncio
async def test_search_query_with_topic_only() -> None:
    client = _SearchingClient(
        [_SearchPage([_SearchMsg(8, date=_BASE, message="in topic")])]
    )
    backend = TelethonSearchBackend(client)

    rows = await backend.search_messages(
        chat_id=-100, query="topic", limit=3, topic_id=99
    )

    assert [row.text for row in rows] == ["in topic"]
    request = client.requests[0]
    assert request.q == "topic"
    assert request.top_msg_id == 99


@pytest.mark.asyncio
async def test_search_range_bounds_are_inclusive() -> None:
    client = _SearchingClient(
        [
            _SearchPage(
                [
                    _SearchMsg(4, date=_TO + timedelta(seconds=1), message="after"),
                    _SearchMsg(3, date=_TO, message="upper edge"),
                    _SearchMsg(2, date=_FROM, message="lower edge"),
                    _SearchMsg(1, date=_FROM - timedelta(seconds=1), message="before"),
                ]
            )
        ]
    )
    backend = TelethonSearchBackend(client)

    rows = await backend.search_messages(
        chat_id=1, query="edge", limit=10, from_date=_FROM, to_date=_TO
    )

    assert [row.id for row in rows] == [3, 2]


@pytest.mark.asyncio
async def test_search_pages_until_limit_collected_newest_first() -> None:
    out_of_range = _BASE + timedelta(hours=5)
    client = _SearchingClient(
        [
            _SearchPage(
                [
                    _SearchMsg(930, date=_BASE, message="one"),
                    _SearchMsg(929, date=out_of_range, message="skip"),
                    _SearchMsg(928, date=_BASE, message="two"),
                ]
            ),
            _SearchPage(
                [
                    # Overlapping id 928 must be deduped, not counted twice.
                    _SearchMsg(928, date=_BASE, message="two"),
                    _SearchMsg(927, date=_BASE, message="three"),
                    _SearchMsg(926, date=_BASE, message="four"),
                ]
            ),
        ]
    )
    backend = TelethonSearchBackend(client)

    rows = await backend.search_messages(
        chat_id=1, query="x", limit=3, from_date=_FROM, to_date=_TO
    )

    assert [row.id for row in rows] == [930, 928, 927]
    assert len(client.requests) == 2
    # The second page asks for messages older than the last one processed.
    assert client.requests[0].offset_id == 0
    assert client.requests[1].offset_id == 928


@pytest.mark.asyncio
async def test_search_stops_when_offset_does_not_advance() -> None:
    """A server replaying the same page must not loop forever."""
    stale = _BASE + timedelta(hours=5)
    client = _SearchingClient(
        [
            _SearchPage(
                [
                    _SearchMsg(910, date=stale, message="out"),
                    _SearchMsg(909, date=stale, message="out"),
                ]
            )
        ]
    )
    backend = TelethonSearchBackend(client)

    rows = await backend.search_messages(
        chat_id=1, query="x", limit=2, from_date=_FROM, to_date=_TO
    )

    assert rows == []
    assert len(client.requests) == 2


@pytest.mark.asyncio
async def test_search_stops_on_empty_page() -> None:
    client = _SearchingClient([_SearchPage([])])
    backend = TelethonSearchBackend(client)

    rows = await backend.search_messages(chat_id=1, query="nothing", limit=5)

    assert rows == []
    assert len(client.requests) == 1


@pytest.mark.asyncio
async def test_search_maps_sender_reply_and_media_fallback() -> None:
    client = _SearchingClient(
        [
            _SearchPage(
                [
                    _SearchMsg(
                        12,
                        date=_BASE,
                        message="",
                        from_id=_PeerUser(77),
                        reply_to=_ReplyHeader(11),
                        media=type("MessageMediaPhoto", (), {})(),
                    )
                ],
                users=[_User(77, "bob")],
            )
        ]
    )
    backend = TelethonSearchBackend(client)

    rows = await backend.search_messages(chat_id=1, query="pic", limit=5)

    row = rows[0]
    assert row.sender == "bob"
    assert row.reply_to == 11
    assert row.text == "[photo]"
    assert row.date == _BASE.isoformat()


@pytest.mark.asyncio
async def test_search_maps_channel_sender_from_result_chats() -> None:
    """A broadcast post has no ``from_id``; its sender lives in ``chats``.

    ``sender_id`` carries the ``-100`` marker there, so the envelope index must
    be keyed the same way or every channel hit reports ``sender: null`` while
    ``messages recent`` reports the channel for the very same message.
    """
    from telethon.tl.types import Channel

    channel = Channel(
        id=555,
        title="News",
        photo=None,
        date=_BASE,
        username="newschan",
    )
    client = _SearchingClient(
        [
            _SearchPage(
                [_SearchMsg(9, date=_BASE, message="release", sender_id=-1000000000555)],
                chats=[channel],
            )
        ]
    )
    backend = TelethonSearchBackend(client)

    rows = await backend.search_messages(chat_id=-1000000000555, query="release", limit=5)

    assert [row.sender for row in rows] == ["newschan"]


@pytest.mark.asyncio
async def test_search_maps_incoming_private_sender_without_from_id() -> None:
    """Layer 119+ drops ``from_id`` on private messages; ``sender_id`` remains."""
    client = _SearchingClient(
        [
            _SearchPage(
                [_SearchMsg(4, date=_BASE, message="hi", sender_id=77)],
                users=[_User(77, "bob")],
            )
        ]
    )
    backend = TelethonSearchBackend(client)

    rows = await backend.search_messages(chat_id=77, query="hi", limit=5)

    assert [row.sender for row in rows] == ["bob"]


@pytest.mark.asyncio
async def test_search_flood_wait_is_translated() -> None:
    class _Flooding(_SearchingClient):
        async def __call__(self, request: Any) -> Any:
            raise _TelethonFloodWaitError(20)

    backend = TelethonSearchBackend(_Flooding([_SearchPage([])]))
    with pytest.raises(FloodWaitError):
        await backend.search_messages(chat_id=1, query="x", limit=1)


# ---------------------------------------------------------------------------
# Private-chat sender filtering
#
# Telegram ignores `from_id` when the peer is a user, so the sender filter has
# to be re-applied locally or `--from-user` silently returns both sides.
# ---------------------------------------------------------------------------


class _PrivateSearchingClient(_SearchingClient):
    """Search double whose peers are real ``InputPeerUser`` objects."""

    def __init__(self, pages: list[_SearchPage], *, self_id: int = 1) -> None:
        super().__init__(pages)
        self._self_id = self_id

    async def get_input_entity(self, ref: Any) -> Any:
        from telethon.tl.types import InputPeerUser

        self.entity_calls.append(ref)
        user_id = self._self_id if ref == "me" else abs(int(ref)) if isinstance(ref, int) else 7
        return InputPeerUser(user_id=user_id, access_hash=0)

    async def get_me(self) -> Any:
        return _User(self._self_id, "self")


class _PrivateMsg(_SearchMsg):
    """Raw hit with the ``sender_id``/``out`` fields Telethon fills in."""

    def __init__(self, msg_id: int, *, sender_id: int | None, out: bool = False) -> None:
        super().__init__(msg_id, date=_BASE, message=f"m{msg_id}")
        self.sender_id = sender_id
        self.out = out


@pytest.mark.asyncio
async def test_private_chat_sender_filter_is_applied_locally() -> None:
    """`from_id` is dropped for a user peer and the filter runs on the rows."""
    client = _PrivateSearchingClient(
        [
            _SearchPage(
                [
                    _PrivateMsg(30, sender_id=7),
                    _PrivateMsg(29, sender_id=None, out=True),
                    _PrivateMsg(28, sender_id=7),
                ]
            )
        ]
    )
    backend = TelethonSearchBackend(client)

    rows = await backend.search_messages(chat_id=7, query="m", from_user=7, limit=10)

    # Telegram would have ignored a server-side from_id, so we must not send one.
    assert client.requests[0].from_id is None
    # Only the partner's messages survive — the outgoing one is ours.
    assert [row.id for row in rows] == [30, 28]


@pytest.mark.asyncio
async def test_private_chat_sender_filter_matches_own_outgoing_messages() -> None:
    """Outgoing private messages carry no ``from_id``; the sender is still us."""
    client = _PrivateSearchingClient(
        [
            _SearchPage(
                [
                    _PrivateMsg(30, sender_id=7),
                    _PrivateMsg(29, sender_id=None, out=True),
                ]
            )
        ],
        self_id=1,
    )
    backend = TelethonSearchBackend(client)

    rows = await backend.search_messages(chat_id=7, query="m", from_user=1, limit=10)

    assert [row.id for row in rows] == [29]


@pytest.mark.asyncio
async def test_group_search_still_pushes_from_id_server_side() -> None:
    """Non-user peers keep the server-side filter — no local second-guessing."""
    client = _SearchingClient([_SearchPage([_SearchMsg(10, date=_BASE, message="hit")])])
    backend = TelethonSearchBackend(client)

    rows = await backend.search_messages(
        chat_id=-100777, query="hit", from_user="@bob", limit=5
    )

    assert client.requests[0].from_id == "peer:@bob"
    assert [row.id for row in rows] == [10]


@pytest.mark.asyncio
async def test_private_chat_third_party_sender_answers_without_paging() -> None:
    """A 1:1 chat has two senders, so a third party matches nothing — no RPC.

    The local filter would discard every row anyway, and paging on would walk
    the chat's whole match set to return the same empty list.
    """
    client = _PrivateSearchingClient(
        [_SearchPage([_PrivateMsg(30, sender_id=7)])], self_id=1
    )
    backend = TelethonSearchBackend(client)

    rows = await backend.search_messages(chat_id=7, query="m", from_user=99, limit=10)

    assert rows == []
    assert client.requests == []


@pytest.mark.asyncio
async def test_search_page_width_survives_a_small_limit() -> None:
    """A small `limit` must not shrink how far the RPC cap can scan.

    Locally filtered rows do not count toward `limit`, so a wire page tied to
    `limit` would cut the `_SEARCH_MAX_PAGES` budget to `limit * pages`
    messages: `--limit 1` would answer `[]` on a chat where `--limit 20` finds
    the very same message.
    """
    client = _PrivateSearchingClient(
        [
            _SearchPage(
                # The wanted row sits behind a run of our own messages, which
                # only the *local* sender filter can drop.
                [_PrivateMsg(930 - i, sender_id=None, out=True) for i in range(20)]
                + [_PrivateMsg(900, sender_id=7)]
            )
        ],
        self_id=1,
    )
    backend = TelethonSearchBackend(client)

    rows = await backend.search_messages(chat_id=7, query="m", from_user=7, limit=1)

    assert client.requests[0].limit == _SEARCH_PAGE_SIZE
    assert [row.id for row in rows] == [900]


@pytest.mark.asyncio
async def test_search_page_width_follows_limit_without_local_filters() -> None:
    """With nothing filtered locally the wire page stays as small as `limit`."""
    client = _SearchingClient([_SearchPage([_SearchMsg(910, date=_BASE, message="a")])])
    backend = TelethonSearchBackend(client)

    await backend.search_messages(chat_id=-100777, query="a", limit=3)

    assert client.requests[0].limit == 3


@pytest.mark.asyncio
async def test_search_paging_is_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    """Locally filtered rows must not buy unbounded pages of RPCs."""
    from telegram_assistant.messages import telethon_backend as backend_module

    monkeypatch.setattr(backend_module, "_SEARCH_MAX_PAGES", 3)
    monkeypatch.setattr(backend_module, "_SEARCH_PAGE_SIZE", 2)
    # Every page is full and its offset keeps advancing, but the date bounds
    # drop every row — the only thing that can stop the loop is the cap.
    stale = _BASE - timedelta(days=30)
    pages = [
        _SearchPage([_SearchMsg(100 - i * 2, date=stale, message="m"),
                     _SearchMsg(99 - i * 2, date=stale, message="m")])
        for i in range(10)
    ]
    client = _SearchingClient(pages)
    backend = TelethonSearchBackend(client)

    rows = await backend.search_messages(
        chat_id=-100777, query="m", limit=10, from_date=_FROM, to_date=_TO
    )

    assert rows == []
    assert len(client.requests) == 3


# ---------------------------------------------------------------------------
# Media attributes from the ffprobe-backed prober
# ---------------------------------------------------------------------------


def _fake_probe(monkeypatch: Any, probe: Any) -> None:
    """Make every ``probe_media`` call in the backend answer with *probe*."""
    from telegram_assistant.messages import media_probe

    monkeypatch.setattr(media_probe, "probe_media", lambda path: probe)


@pytest.mark.asyncio
async def test_video_carries_the_probed_duration_and_dimensions(
    tmp_path: Any, monkeypatch: Any
) -> None:
    """Telethon's stub is ``duration=0, w=1, h=1`` for every mp4 and the server
    only repairs small files — a 12 MB+ video keeps the stub and renders as an
    empty rectangle."""
    from telethon.tl.types import DocumentAttributeVideo

    from telegram_assistant.messages.media_probe import MediaProbe

    _fake_probe(
        monkeypatch,
        MediaProbe(duration=73.681, width=854, height=480, has_video=True, has_audio=True),
    )
    video = _rich_file(tmp_path, "clip.mp4", "clip", "video")
    client = _UploadingClient()
    backend = TelethonMessageBackend(client)

    await backend.send_message(
        chat_id=1, text="", rich_markdown=MEDIA_MD, rich_files=(video,)
    )

    attributes = client.upload_media[0].media.attributes
    video_attrs = [a for a in attributes if isinstance(a, DocumentAttributeVideo)]
    # Exactly one: two DocumentAttributeVideo in one document is a malformed
    # request, so the probed one replaces Telethon's stub rather than joining it.
    assert len(video_attrs) == 1
    assert video_attrs[0].duration == 74
    assert video_attrs[0].w == 854
    assert video_attrs[0].h == 480
    assert video_attrs[0].supports_streaming is True


@pytest.mark.asyncio
async def test_audio_carries_the_probed_duration(tmp_path: Any, monkeypatch: Any) -> None:
    """The hard-coded ``duration=0`` made ``tg://audio`` resolve but showed a
    zero-length track in the clients."""
    from telethon.tl.types import DocumentAttributeAudio

    from telegram_assistant.messages.media_probe import MediaProbe

    _fake_probe(
        monkeypatch,
        MediaProbe(duration=184.5, width=None, height=None, has_video=False, has_audio=True),
    )
    audio = _rich_file(tmp_path, "voice.mp3", "voice", "audio")
    client = _UploadingClient()
    backend = TelethonMessageBackend(client)

    await backend.send_message(
        chat_id=1,
        text="",
        rich_markdown="![](tg://audio?id=voice)\n",
        rich_files=(audio,),
    )

    attributes = client.upload_media[0].media.attributes
    audio_attrs = [a for a in attributes if isinstance(a, DocumentAttributeAudio)]
    assert len(audio_attrs) == 1
    assert audio_attrs[0].duration == 184


@pytest.mark.asyncio
async def test_a_failed_probe_keeps_the_send_working(
    tmp_path: Any, monkeypatch: Any
) -> None:
    """No ffprobe on the box is not a send failure: the pre-probe stub goes out,
    exactly as it did before this feature."""
    from telethon.tl.types import DocumentAttributeAudio, DocumentAttributeVideo

    _fake_probe(monkeypatch, None)
    video = _rich_file(tmp_path, "clip.mp4", "clip", "video")
    audio = _rich_file(tmp_path, "voice.mp3", "voice", "audio")
    client = _UploadingClient()
    backend = TelethonMessageBackend(client)

    await backend.send_message(
        chat_id=1,
        text="",
        rich_markdown="![](tg://video?id=clip)\n\n![](tg://audio?id=voice)\n",
        rich_files=(video, audio),
    )

    assert any(
        isinstance(a, DocumentAttributeVideo)
        for a in client.upload_media[0].media.attributes
    )
    # Still present, so tg://audio keeps resolving without a prober.
    assert any(
        isinstance(a, DocumentAttributeAudio)
        for a in client.upload_media[1].media.attributes
    )


@pytest.fixture
def _restore_logging():
    """Snapshot/restore the root logger so ``configure_logging(force=True)``
    below doesn't leave the root handler writing to a dead ``StringIO`` buffer
    (which would corrupt log capture in later tests).

    ``get_logger`` is structlog-backed (``PrintLoggerFactory``), which writes
    straight to its configured stream rather than through the stdlib
    ``logging`` module — so ``caplog`` never sees it. This mirrors the
    stream-capture pattern ``tests/test_access.py`` and
    ``tests/test_observability_logging.py`` already use for the same reason.
    """
    root = logging.getLogger()
    saved_handlers = root.handlers[:]
    saved_level = root.level
    try:
        yield
    finally:
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)


@pytest.mark.asyncio
async def test_a_failed_probe_is_logged_with_the_file(
    tmp_path: Any, monkeypatch: Any, _restore_logging: Any
) -> None:
    """Uploads used to write nothing to the log at all, so a broken article was
    undiagnosable after the fact."""
    _fake_probe(monkeypatch, None)
    video = _rich_file(tmp_path, "clip.mp4", "clip", "video")
    client = _UploadingClient()
    backend = TelethonMessageBackend(client)

    buf = io.StringIO()
    configure_logging(level="DEBUG", stream=buf, force=True)

    await backend.send_message(
        chat_id=1, text="", rich_markdown=MEDIA_MD, rich_files=(video,)
    )

    lines = [json.loads(line) for line in buf.getvalue().strip().splitlines() if line.strip()]
    warnings = [r for r in lines if r.get("level") == "warning"]
    assert any("clip.mp4" in r.get("path", "") for r in warnings), lines


@pytest.mark.asyncio
async def test_a_photo_is_never_probed(tmp_path: Any, monkeypatch: Any) -> None:
    """A photo is uploaded as ``InputMediaUploadedPhoto`` and has no attributes
    at all — probing it would spend a subprocess per image for nothing."""
    from telegram_assistant.messages import media_probe

    calls: list[str] = []
    monkeypatch.setattr(
        media_probe, "probe_media", lambda path: calls.append(str(path)) or None
    )
    photo = _rich_file(tmp_path, "shot.png", "shot", "photo")
    client = _UploadingClient()
    backend = TelethonMessageBackend(client)

    await backend.send_message(
        chat_id=1, text="", rich_markdown="![](tg://photo?id=shot)\n", rich_files=(photo,)
    )

    assert calls == []


@pytest.mark.asyncio
async def test_video_carries_a_generated_thumbnail(
    tmp_path: Any, monkeypatch: Any
) -> None:
    """A large video comes back from the server with ``thumbs=None``; without a
    preview the clients draw an empty rectangle even with correct dimensions."""
    from telegram_assistant.messages import media_probe
    from telegram_assistant.messages.media_probe import MediaProbe

    _fake_probe(
        monkeypatch,
        MediaProbe(duration=73.681, width=854, height=480, has_video=True, has_audio=True),
    )
    seen: dict[str, Any] = {}

    def fake_thumb(path: Any, *, duration: float) -> bytes:
        seen["duration"] = duration
        return b"\xff\xd8jpeg"

    monkeypatch.setattr(media_probe, "extract_thumbnail", fake_thumb)
    video = _rich_file(tmp_path, "clip.mp4", "clip", "video")
    client = _UploadingClient()
    backend = TelethonMessageBackend(client)

    await backend.send_message(
        chat_id=1, text="", rich_markdown=MEDIA_MD, rich_files=(video,)
    )

    assert client.upload_media[0].media.thumb == "handle:thumb.jpg"
    # The probe is reused rather than run a second time for the seek offset.
    assert seen["duration"] == pytest.approx(73.681)


@pytest.mark.asyncio
async def test_a_missing_thumbnail_does_not_fail_the_send(
    tmp_path: Any, monkeypatch: Any
) -> None:
    from telegram_assistant.messages import media_probe
    from telegram_assistant.messages.media_probe import MediaProbe

    _fake_probe(
        monkeypatch,
        MediaProbe(duration=5.0, width=100, height=100, has_video=True, has_audio=False),
    )
    monkeypatch.setattr(media_probe, "extract_thumbnail", lambda path, *, duration: None)
    video = _rich_file(tmp_path, "clip.mp4", "clip", "video")
    client = _UploadingClient()
    backend = TelethonMessageBackend(client)

    await backend.send_message(
        chat_id=1, text="", rich_markdown=MEDIA_MD, rich_files=(video,)
    )

    assert client.upload_media[0].media.thumb is None


@pytest.mark.asyncio
async def test_audio_gets_no_thumbnail(tmp_path: Any, monkeypatch: Any) -> None:
    """Only videos need a preview frame; running ffmpeg over every mp3 would
    spend a subprocess for nothing."""
    from telegram_assistant.messages import media_probe
    from telegram_assistant.messages.media_probe import MediaProbe

    calls: list[Any] = []
    _fake_probe(
        monkeypatch,
        MediaProbe(duration=184.5, width=None, height=None, has_video=False, has_audio=True),
    )
    monkeypatch.setattr(
        media_probe,
        "extract_thumbnail",
        lambda path, *, duration: calls.append(path) or None,
    )
    audio = _rich_file(tmp_path, "voice.mp3", "voice", "audio")
    client = _UploadingClient()
    backend = TelethonMessageBackend(client)

    await backend.send_message(
        chat_id=1,
        text="",
        rich_markdown="![](tg://audio?id=voice)\n",
        rich_files=(audio,),
    )

    assert calls == []
    assert client.upload_media[0].media.thumb is None


@pytest.mark.asyncio
async def test_each_rich_media_upload_is_logged(
    tmp_path: Any, monkeypatch: Any, _restore_logging: Any
) -> None:
    """Uploading 34 files used to write nothing to the log at all, even at
    DEBUG, so a broken article could not be diagnosed after the fact.

    ``get_logger`` is structlog-backed (``PrintLoggerFactory``), which writes
    straight to its configured stream rather than through the stdlib
    ``logging`` module — so ``caplog`` never sees it, hence the stream-capture
    pattern from ``test_a_failed_probe_is_logged_with_the_file`` above.
    """
    from telegram_assistant.messages import media_probe
    from telegram_assistant.messages.media_probe import MediaProbe

    _fake_probe(
        monkeypatch,
        MediaProbe(duration=73.681, width=854, height=480, has_video=True, has_audio=True),
    )
    monkeypatch.setattr(media_probe, "extract_thumbnail", lambda path, *, duration: None)
    photo = _rich_file(tmp_path, "shot.png", "shot", "photo")
    video = _rich_file(tmp_path, "clip.mp4", "clip", "video")
    client = _UploadingClient()
    backend = TelethonMessageBackend(client)

    buf = io.StringIO()
    configure_logging(level="INFO", stream=buf, force=True)

    await backend.send_message(
        chat_id=1, text="", rich_markdown=MEDIA_MD, rich_files=(photo, video)
    )

    lines = [json.loads(line) for line in buf.getvalue().strip().splitlines() if line.strip()]
    uploaded = [r for r in lines if r.get("event") == "rich media uploaded"]
    assert any("shot.png" in r.get("path", "") for r in uploaded), lines
    assert any("clip.mp4" in r.get("path", "") for r in uploaded), lines
