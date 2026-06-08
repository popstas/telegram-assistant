"""Unit tests for :class:`TelethonMessageBackend`.

Covers the write side of the message adapter: text-only sends route through
``send_message``, attachment sends through ``send_file`` (single id vs album
list), scheduling and topic reply ids are forwarded, an empty caption becomes
``None``, and Telethon ``FloodWaitError`` is translated for the worker queue.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from telegram_assistant.messages.telethon_backend import (
    TelethonDeleteBackend,
    TelethonMessageBackend,
)
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
