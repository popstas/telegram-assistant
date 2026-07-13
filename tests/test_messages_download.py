"""Tests for Task 8 — message media-download domain op + Telethon adapter."""

from __future__ import annotations

import os
from typing import Any

import pytest

from telegram_assistant.access import AccessDenied, Authorizer
from telegram_assistant.config.models import AccessConfig, AccessRule
from telegram_assistant.messages import (
    DownloadedMedia,
    MediaDownloadRequest,
    MediaInfo,
    MediaTooLargeError,
    NoDownloadableMediaError,
    download_media,
)
from telegram_assistant.messages.telethon_backend import (
    TelethonMediaDownloadBackend,
)
from telegram_assistant.worker.queue import FloodWaitError

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


_DEFAULT_INFO = MediaInfo(filename="photo.jpg", size=100, mime="image/jpeg")
_UNSET = object()


class FakeMediaDownloadBackend:
    def __init__(
        self,
        *,
        info: Any = _UNSET,
        downloaded: DownloadedMedia | None = None,
        raise_on_probe: Exception | None = None,
    ) -> None:
        self._info = _DEFAULT_INFO if info is _UNSET else info
        self._downloaded = downloaded
        self._raise_on_probe = raise_on_probe
        self.probe_calls: list[dict[str, Any]] = []
        self.download_calls: list[dict[str, Any]] = []

    async def probe_media(
        self, *, chat_id: int, message_id: int
    ) -> MediaInfo | None:
        if self._raise_on_probe is not None:
            raise self._raise_on_probe
        self.probe_calls.append({"chat_id": chat_id, "message_id": message_id})
        return self._info

    async def download_media(
        self, *, chat_id: int, message_id: int, target_path: str
    ) -> DownloadedMedia:
        self.download_calls.append(
            {
                "chat_id": chat_id,
                "message_id": message_id,
                "target_path": target_path,
            }
        )
        if self._downloaded is not None:
            return self._downloaded
        return DownloadedMedia(path=target_path, size=100, mime="image/jpeg")


# ---------------------------------------------------------------------------
# download_media domain tests
# ---------------------------------------------------------------------------


async def test_download_to_out_path_returns_written_file() -> None:
    backend = FakeMediaDownloadBackend()
    result = await download_media(
        backend,
        request=MediaDownloadRequest(
            telegram_chat_id=-100,
            message_id=42,
            out_path="/tmp/dest.jpg",
            chat_name="Acme",
        ),
    )
    assert result.dry_run is False
    assert result.path == "/tmp/dest.jpg"
    assert result.size == 100
    assert result.mime == "image/jpeg"
    assert result.chat_name == "Acme"
    assert backend.download_calls == [
        {"chat_id": -100, "message_id": 42, "target_path": "/tmp/dest.jpg"}
    ]


async def test_download_to_out_dir_joins_original_filename() -> None:
    backend = FakeMediaDownloadBackend(
        info=MediaInfo(filename="report.pdf", size=50, mime="application/pdf"),
        downloaded=DownloadedMedia(
            path="/downloads/report.pdf", size=50, mime="application/pdf"
        ),
    )
    result = await download_media(
        backend,
        request=MediaDownloadRequest(
            telegram_chat_id=-100, message_id=42, out_dir="/downloads"
        ),
    )
    assert backend.download_calls[0]["target_path"] == os.path.join(
        "/downloads", "report.pdf"
    )
    assert result.path == "/downloads/report.pdf"


async def test_download_to_out_dir_fallback_name_when_no_filename() -> None:
    backend = FakeMediaDownloadBackend(
        info=MediaInfo(filename=None, size=50, mime="audio/ogg")
    )
    await download_media(
        backend,
        request=MediaDownloadRequest(
            telegram_chat_id=-100, message_id=7, out_dir="/downloads"
        ),
    )
    assert backend.download_calls[0]["target_path"] == os.path.join(
        "/downloads", "message-7.bin"
    )


async def test_download_out_dir_strips_path_components() -> None:
    backend = FakeMediaDownloadBackend(
        info=MediaInfo(filename="../../escape.sh", size=10, mime=None)
    )
    await download_media(
        backend,
        request=MediaDownloadRequest(
            telegram_chat_id=-100, message_id=7, out_dir="/downloads"
        ),
    )
    assert backend.download_calls[0]["target_path"] == os.path.join(
        "/downloads", "escape.sh"
    )


async def test_download_dry_run_probes_but_skips_transfer() -> None:
    backend = FakeMediaDownloadBackend()
    result = await download_media(
        backend,
        request=MediaDownloadRequest(
            telegram_chat_id=-100,
            message_id=42,
            out_dir="/downloads",
            dry_run=True,
        ),
    )
    assert result.dry_run is True
    assert result.path == os.path.join("/downloads", "photo.jpg")
    assert result.size == 100
    assert result.mime == "image/jpeg"
    assert backend.probe_calls  # probed
    assert backend.download_calls == []  # not transferred


async def test_download_no_media_raises() -> None:
    backend = FakeMediaDownloadBackend(info=None)
    with pytest.raises(NoDownloadableMediaError):
        await download_media(
            backend,
            request=MediaDownloadRequest(
                telegram_chat_id=-100, message_id=42, out_path="/tmp/x"
            ),
        )
    assert backend.download_calls == []


async def test_download_size_limit_rejected_before_transfer() -> None:
    backend = FakeMediaDownloadBackend(
        info=MediaInfo(filename="big.bin", size=5000, mime=None)
    )
    with pytest.raises(MediaTooLargeError) as exc:
        await download_media(
            backend,
            request=MediaDownloadRequest(
                telegram_chat_id=-100,
                message_id=42,
                out_path="/tmp/x",
                max_bytes=1000,
            ),
        )
    assert exc.value.size == 5000
    assert exc.value.max_bytes == 1000
    assert backend.download_calls == []


async def test_download_size_limit_allows_unknown_size() -> None:
    backend = FakeMediaDownloadBackend(
        info=MediaInfo(filename="unknown.bin", size=None, mime=None)
    )
    result = await download_media(
        backend,
        request=MediaDownloadRequest(
            telegram_chat_id=-100,
            message_id=42,
            out_path="/tmp/x",
            max_bytes=1000,
        ),
    )
    assert result.dry_run is False
    assert backend.download_calls  # transfer proceeded


async def test_download_rejects_non_positive_message_id() -> None:
    backend = FakeMediaDownloadBackend()
    with pytest.raises(ValueError):
        await download_media(
            backend,
            request=MediaDownloadRequest(
                telegram_chat_id=-100, message_id=0, out_path="/tmp/x"
            ),
        )
    assert backend.probe_calls == []


async def test_download_requires_exactly_one_target() -> None:
    backend = FakeMediaDownloadBackend()
    # neither
    with pytest.raises(ValueError):
        await download_media(
            backend,
            request=MediaDownloadRequest(telegram_chat_id=-100, message_id=1),
        )
    # both
    with pytest.raises(ValueError):
        await download_media(
            backend,
            request=MediaDownloadRequest(
                telegram_chat_id=-100,
                message_id=1,
                out_path="/tmp/x",
                out_dir="/downloads",
            ),
        )
    assert backend.probe_calls == []


async def test_download_rejects_non_positive_max_bytes() -> None:
    backend = FakeMediaDownloadBackend()
    with pytest.raises(ValueError):
        await download_media(
            backend,
            request=MediaDownloadRequest(
                telegram_chat_id=-100,
                message_id=1,
                out_path="/tmp/x",
                max_bytes=0,
            ),
        )
    assert backend.probe_calls == []


async def test_download_denied_before_backend_call() -> None:
    backend = FakeMediaDownloadBackend()
    authorizer = Authorizer(
        AccessConfig(rules=[AccessRule(all=True, permission="write")])
    )
    with pytest.raises(AccessDenied):
        await download_media(
            backend,
            request=MediaDownloadRequest(
                telegram_chat_id=-100, message_id=1, out_path="/tmp/x"
            ),
            authorizer=authorizer,
        )
    assert backend.probe_calls == []


async def test_download_allowed_with_read_rule() -> None:
    backend = FakeMediaDownloadBackend()
    authorizer = Authorizer(
        AccessConfig(rules=[AccessRule(all=True, permission="read")])
    )
    result = await download_media(
        backend,
        request=MediaDownloadRequest(
            telegram_chat_id=-100, message_id=5, out_path="/tmp/x"
        ),
        authorizer=authorizer,
    )
    assert result.dry_run is False
    assert backend.download_calls


# ---------------------------------------------------------------------------
# Telethon adapter tests
# ---------------------------------------------------------------------------


class _FakeFile:
    def __init__(self, name: str | None, size: int | None, mime: str | None) -> None:
        self.name = name
        self.size = size
        self.mime_type = mime


class _FakeMessage:
    def __init__(
        self, *, media: Any, file: _FakeFile | None = None
    ) -> None:
        self.media = media
        self.file = file


class FakeTelethonClient:
    def __init__(
        self,
        *,
        message: Any = None,
        raise_on_call: Exception | None = None,
        write_bytes: bytes = b"data",
    ) -> None:
        self._message = message
        self._raise_on_call = raise_on_call
        self._write_bytes = write_bytes
        self.download_calls: list[Any] = []

    async def get_input_entity(self, chat_id: int) -> str:
        return f"peer:{chat_id}"

    async def get_messages(self, entity: Any, *, ids: int) -> Any:
        if self._raise_on_call is not None:
            raise self._raise_on_call
        return self._message

    async def download_media(self, msg: Any, *, file: str) -> str:
        self.download_calls.append({"msg": msg, "file": file})
        with open(file, "wb") as fh:
            fh.write(self._write_bytes)
        return file


async def test_telethon_probe_returns_metadata() -> None:
    msg = _FakeMessage(
        media=object(),
        file=_FakeFile("pic.jpg", 321, "image/jpeg"),
    )
    backend = TelethonMediaDownloadBackend(FakeTelethonClient(message=msg))
    info = await backend.probe_media(chat_id=-100, message_id=1)
    assert info is not None
    assert info.filename == "pic.jpg"
    assert info.size == 321
    assert info.mime == "image/jpeg"


async def test_telethon_probe_none_for_text_message() -> None:
    msg = _FakeMessage(media=None)
    backend = TelethonMediaDownloadBackend(FakeTelethonClient(message=msg))
    assert await backend.probe_media(chat_id=-100, message_id=1) is None


async def test_telethon_probe_none_for_missing_message() -> None:
    backend = TelethonMediaDownloadBackend(FakeTelethonClient(message=None))
    assert await backend.probe_media(chat_id=-100, message_id=1) is None


async def test_telethon_download_writes_file(tmp_path: Any) -> None:
    msg = _FakeMessage(
        media=object(), file=_FakeFile("pic.jpg", 4, "image/jpeg")
    )
    dest = str(tmp_path / "out.jpg")
    client = FakeTelethonClient(message=msg, write_bytes=b"abcd")
    backend = TelethonMediaDownloadBackend(client)
    result = await backend.download_media(
        chat_id=-100, message_id=1, target_path=dest
    )
    assert result.path == dest
    assert result.size == 4
    assert result.mime == "image/jpeg"
    assert os.path.isfile(dest)


async def test_telethon_download_no_media_raises(tmp_path: Any) -> None:
    msg = _FakeMessage(media=None)
    backend = TelethonMediaDownloadBackend(FakeTelethonClient(message=msg))
    with pytest.raises(ValueError):
        await backend.download_media(
            chat_id=-100, message_id=1, target_path=str(tmp_path / "x")
        )


def _flood_error() -> Exception:
    class _Flood(Exception):
        def __init__(self) -> None:
            self.seconds = 7

    _Flood.__name__ = "FloodWaitError"
    return _Flood()


async def test_telethon_probe_translates_flood_wait() -> None:
    backend = TelethonMediaDownloadBackend(
        FakeTelethonClient(raise_on_call=_flood_error())
    )
    with pytest.raises(FloodWaitError):
        await backend.probe_media(chat_id=-100, message_id=1)


async def test_telethon_download_translates_flood_wait(tmp_path: Any) -> None:
    backend = TelethonMediaDownloadBackend(
        FakeTelethonClient(raise_on_call=_flood_error())
    )
    with pytest.raises(FloodWaitError):
        await backend.download_media(
            chat_id=-100, message_id=1, target_path=str(tmp_path / "x")
        )
