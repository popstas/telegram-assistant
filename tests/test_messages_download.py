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
        raise_on_download: Exception | None = None,
        write_size: int | None = None,
    ) -> None:
        self._info = _DEFAULT_INFO if info is _UNSET else info
        self._downloaded = downloaded
        self._raise_on_probe = raise_on_probe
        self._raise_on_download = raise_on_download
        self._write_size = write_size
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
        if self._raise_on_download is not None:
            raise self._raise_on_download
        if self._write_size is not None:
            with open(target_path, "wb") as fh:
                fh.write(b"x" * self._write_size)
            return DownloadedMedia(
                path=target_path, size=self._write_size, mime="image/jpeg"
            )
        if self._downloaded is not None:
            return self._downloaded
        return DownloadedMedia(path=target_path, size=100, mime="image/jpeg")


# ---------------------------------------------------------------------------
# download_media domain tests
# ---------------------------------------------------------------------------


async def test_download_to_out_path_returns_written_file(tmp_path: Any) -> None:
    backend = FakeMediaDownloadBackend()
    dest = str(tmp_path / "dest.jpg")
    result = await download_media(
        backend,
        request=MediaDownloadRequest(
            telegram_chat_id=-100,
            message_id=42,
            out_path=dest,
            chat_name="Acme",
        ),
    )
    assert result.dry_run is False
    assert result.path == dest
    assert result.size == 100
    assert result.mime == "image/jpeg"
    assert result.chat_name == "Acme"
    assert backend.download_calls == [
        {"chat_id": -100, "message_id": 42, "target_path": dest}
    ]


async def test_download_to_out_dir_joins_original_filename(tmp_path: Any) -> None:
    backend = FakeMediaDownloadBackend(
        info=MediaInfo(filename="report.pdf", size=50, mime="application/pdf"),
    )
    result = await download_media(
        backend,
        request=MediaDownloadRequest(
            telegram_chat_id=-100, message_id=42, out_dir=str(tmp_path)
        ),
    )
    assert backend.download_calls[0]["target_path"] == os.path.join(
        str(tmp_path), "report.pdf"
    )
    assert result.path == os.path.join(str(tmp_path), "report.pdf")


async def test_download_to_out_dir_fallback_name_when_no_filename(
    tmp_path: Any,
) -> None:
    backend = FakeMediaDownloadBackend(
        info=MediaInfo(filename=None, size=50, mime="audio/ogg")
    )
    await download_media(
        backend,
        request=MediaDownloadRequest(
            telegram_chat_id=-100, message_id=7, out_dir=str(tmp_path)
        ),
    )
    assert backend.download_calls[0]["target_path"] == os.path.join(
        str(tmp_path), "message-7.bin"
    )


async def test_download_out_dir_strips_path_components(tmp_path: Any) -> None:
    backend = FakeMediaDownloadBackend(
        info=MediaInfo(filename="../../escape.sh", size=10, mime=None)
    )
    await download_media(
        backend,
        request=MediaDownloadRequest(
            telegram_chat_id=-100, message_id=7, out_dir=str(tmp_path)
        ),
    )
    assert backend.download_calls[0]["target_path"] == os.path.join(
        str(tmp_path), "escape.sh"
    )


async def test_download_creates_missing_out_dir(tmp_path: Any) -> None:
    backend = FakeMediaDownloadBackend()
    nested = tmp_path / "a" / "b"
    await download_media(
        backend,
        request=MediaDownloadRequest(
            telegram_chat_id=-100, message_id=7, out_dir=str(nested)
        ),
    )
    assert (nested / "photo.jpg").is_file()


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


async def test_download_size_limit_allows_unknown_size(tmp_path: Any) -> None:
    backend = FakeMediaDownloadBackend(
        info=MediaInfo(filename="unknown.bin", size=None, mime=None)
    )
    result = await download_media(
        backend,
        request=MediaDownloadRequest(
            telegram_chat_id=-100,
            message_id=42,
            out_path=str(tmp_path / "x"),
            max_bytes=1000,
        ),
    )
    assert result.dry_run is False
    assert backend.download_calls  # transfer proceeded (written size under cap)


async def test_download_size_limit_enforced_after_transfer_when_size_unknown(
    tmp_path: Any,
) -> None:
    # Probe reports no size, so the pre-transfer guard can't fire; the written
    # file turns out to be over the cap and must be rejected + removed.
    dest = tmp_path / "big.bin"
    backend = FakeMediaDownloadBackend(
        info=MediaInfo(filename="big.bin", size=None, mime=None),
        write_size=5000,
    )
    with pytest.raises(MediaTooLargeError) as exc:
        await download_media(
            backend,
            request=MediaDownloadRequest(
                telegram_chat_id=-100,
                message_id=42,
                out_path=str(dest),
                max_bytes=1000,
            ),
        )
    assert exc.value.size == 5000
    assert exc.value.max_bytes == 1000
    assert backend.download_calls  # transfer happened before the cap check
    assert not dest.exists()  # oversized file was removed


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


async def test_download_allowed_with_read_rule(tmp_path: Any) -> None:
    backend = FakeMediaDownloadBackend()
    authorizer = Authorizer(
        AccessConfig(rules=[AccessRule(all=True, permission="read")])
    )
    result = await download_media(
        backend,
        request=MediaDownloadRequest(
            telegram_chat_id=-100, message_id=5, out_path=str(tmp_path / "x")
        ),
        authorizer=authorizer,
    )
    assert result.dry_run is False
    assert backend.download_calls


# ---------------------------------------------------------------------------
# No-overwrite / unique filename
# ---------------------------------------------------------------------------


async def _download_photo(backend: Any, tmp_path: Any) -> str:
    result = await download_media(
        backend,
        request=MediaDownloadRequest(
            telegram_chat_id=-100, message_id=42, out_dir=str(tmp_path)
        ),
    )
    return result.path


async def test_download_repeat_gets_numbered_name_and_keeps_first(
    tmp_path: Any,
) -> None:
    first_backend = FakeMediaDownloadBackend(write_size=3)
    first = await _download_photo(first_backend, tmp_path)
    assert first == os.path.join(str(tmp_path), "photo.jpg")

    second_backend = FakeMediaDownloadBackend(write_size=7)
    second = await _download_photo(second_backend, tmp_path)
    assert second == os.path.join(str(tmp_path), "photo (1).jpg")

    third_backend = FakeMediaDownloadBackend(write_size=9)
    third = await _download_photo(third_backend, tmp_path)
    assert third == os.path.join(str(tmp_path), "photo (2).jpg")

    # the earlier downloads are untouched
    assert os.path.getsize(first) == 3
    assert os.path.getsize(second) == 7


async def test_download_out_path_collision_gets_free_name(tmp_path: Any) -> None:
    dest = tmp_path / "dest.jpg"
    dest.write_bytes(b"original")
    backend = FakeMediaDownloadBackend(write_size=4)
    result = await download_media(
        backend,
        request=MediaDownloadRequest(
            telegram_chat_id=-100, message_id=42, out_path=str(dest)
        ),
    )
    assert result.path == os.path.join(str(tmp_path), "dest (1).jpg")
    assert backend.download_calls[0]["target_path"] == result.path
    assert dest.read_bytes() == b"original"


async def test_download_collision_on_extensionless_name(tmp_path: Any) -> None:
    (tmp_path / "noext").write_bytes(b"a")
    backend = FakeMediaDownloadBackend(
        info=MediaInfo(filename="noext", size=1, mime=None), write_size=1
    )
    result = await download_media(
        backend,
        request=MediaDownloadRequest(
            telegram_chat_id=-100, message_id=42, out_dir=str(tmp_path)
        ),
    )
    assert result.path == os.path.join(str(tmp_path), "noext (1)")


async def test_download_removes_placeholder_on_backend_error(
    tmp_path: Any,
) -> None:
    backend = FakeMediaDownloadBackend(
        raise_on_download=FloodWaitError(5)
    )
    with pytest.raises(FloodWaitError):
        await download_media(
            backend,
            request=MediaDownloadRequest(
                telegram_chat_id=-100, message_id=42, out_dir=str(tmp_path)
            ),
        )
    assert backend.download_calls  # the claimed path was handed to the backend
    assert list(tmp_path.iterdir()) == []  # no empty placeholder left behind


async def test_download_placeholder_removed_does_not_block_next_name(
    tmp_path: Any,
) -> None:
    # A failed download must not burn the name: the retry gets the same path.
    failing = FakeMediaDownloadBackend(raise_on_download=RuntimeError("boom"))
    with pytest.raises(RuntimeError):
        await _download_photo(failing, tmp_path)
    retry = FakeMediaDownloadBackend(write_size=2)
    assert await _download_photo(retry, tmp_path) == os.path.join(
        str(tmp_path), "photo.jpg"
    )


async def test_download_dry_run_reports_first_free_name(tmp_path: Any) -> None:
    (tmp_path / "photo.jpg").write_bytes(b"a")
    (tmp_path / "photo (1).jpg").write_bytes(b"b")
    backend = FakeMediaDownloadBackend()
    result = await download_media(
        backend,
        request=MediaDownloadRequest(
            telegram_chat_id=-100,
            message_id=42,
            out_dir=str(tmp_path),
            dry_run=True,
        ),
    )
    assert result.path == os.path.join(str(tmp_path), "photo (2).jpg")
    # dry-run reserves nothing
    assert not (tmp_path / "photo (2).jpg").exists()
    assert backend.download_calls == []


async def test_download_oversized_removes_claimed_file(tmp_path: Any) -> None:
    (tmp_path / "photo.jpg").write_bytes(b"keep me")
    backend = FakeMediaDownloadBackend(
        info=MediaInfo(filename="photo.jpg", size=None, mime=None),
        write_size=5000,
    )
    with pytest.raises(MediaTooLargeError):
        await download_media(
            backend,
            request=MediaDownloadRequest(
                telegram_chat_id=-100,
                message_id=42,
                out_dir=str(tmp_path),
                max_bytes=10,
            ),
        )
    assert backend.download_calls[0]["target_path"] == os.path.join(
        str(tmp_path), "photo (1).jpg"
    )
    assert not (tmp_path / "photo (1).jpg").exists()
    assert (tmp_path / "photo.jpg").read_bytes() == b"keep me"


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
