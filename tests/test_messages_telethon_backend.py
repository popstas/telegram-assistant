"""Unit tests for the Telethon message-send adapter."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from telegram_assistant.config import load_config_from_text
from telegram_assistant.http_api import create_app
from telegram_assistant.messages import SendMessageBackendResult
from telegram_assistant.messages.telethon_backend import TelethonMessageBackend
from telegram_assistant.worker.queue import FloodWaitError


class _Sent:
    def __init__(self, message_id: int) -> None:
        self.id = message_id


class _RecordingClient:
    def __init__(self, response: Any) -> None:
        self._response = response
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    async def send_message(self, *args: Any, **kwargs: Any) -> Any:
        self.calls.append(("send_message", args, kwargs))
        return self._response

    async def send_file(self, *args: Any, **kwargs: Any) -> Any:
        self.calls.append(("send_file", args, kwargs))
        return self._response


async def test_telethon_message_backend_sends_text_with_topic_and_schedule() -> None:
    schedule_at = datetime(2026, 6, 7, 12, 0, tzinfo=UTC)
    client = _RecordingClient(_Sent(101))
    backend = TelethonMessageBackend(client)

    result = await backend.send_message(
        chat_id=-100,
        text="hello",
        topic_id=42,
        schedule_at=schedule_at,
    )

    assert result == SendMessageBackendResult(
        telegram_message_id=101,
        telegram_message_ids=(101,),
    )
    assert client.calls == [
        (
            "send_message",
            (-100, "hello"),
            {"reply_to": 42, "schedule": schedule_at},
        )
    ]


async def test_telethon_message_backend_sends_files_with_caption_and_schedule() -> None:
    schedule_at = datetime(2026, 6, 7, 12, 0, tzinfo=UTC)
    client = _RecordingClient(_Sent(202))
    backend = TelethonMessageBackend(client)

    result = await backend.send_message(
        chat_id=-100,
        text="caption",
        topic_id=7,
        files=("/tmp/a.jpg", "https://example.test/b.jpg"),
        schedule_at=schedule_at,
    )

    assert result == SendMessageBackendResult(
        telegram_message_id=202,
        telegram_message_ids=(202,),
    )
    assert client.calls == [
        (
            "send_file",
            (-100, ("/tmp/a.jpg", "https://example.test/b.jpg")),
            {"caption": "caption", "reply_to": 7, "schedule": schedule_at},
        )
    ]


async def test_telethon_message_backend_sends_media_only_without_caption() -> None:
    client = _RecordingClient(_Sent(303))
    backend = TelethonMessageBackend(client)

    await backend.send_message(chat_id=-100, text="", files=("/tmp/a.pdf",))

    assert client.calls == [
        (
            "send_file",
            (-100, ("/tmp/a.pdf",)),
            {"caption": None},
        )
    ]


async def test_telethon_message_backend_normalizes_album_ids() -> None:
    client = _RecordingClient([_Sent(401), _Sent(402), _Sent(403)])
    backend = TelethonMessageBackend(client)

    result = await backend.send_message(
        chat_id=-100,
        text="album",
        files=("/tmp/1.jpg", "/tmp/2.jpg", "/tmp/3.jpg"),
    )

    assert result.telegram_message_id == 401
    assert result.telegram_message_ids == (401, 402, 403)


async def test_telethon_message_backend_translates_flood_wait() -> None:
    class _FloodingClient:
        async def send_message(self, *args: Any, **kwargs: Any) -> Any:
            class FloodWaitError(Exception):
                def __init__(self, seconds: int) -> None:
                    super().__init__(f"FLOOD_WAIT_{seconds}")
                    self.seconds = seconds

            raise FloodWaitError(seconds=9)

    backend = TelethonMessageBackend(_FloodingClient())

    with pytest.raises(FloodWaitError) as excinfo:
        await backend.send_message(chat_id=-100, text="hello")

    assert excinfo.value.seconds == 9.0


def test_app_default_message_backend_factory_uses_message_adapter(
    minimal_config_yaml: str,
) -> None:
    class _SessionManager:
        _client = object()

    config = load_config_from_text(minimal_config_yaml)
    app = create_app(config, session_manager=_SessionManager())

    backend = app.state.message_backend_factory(None)

    assert isinstance(backend, TelethonMessageBackend)
