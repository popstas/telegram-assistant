"""Unit tests for the ``file_urls`` download-to-temp helper (Task 10)."""

from __future__ import annotations

import asyncio
import os
import tempfile
from collections.abc import AsyncIterator

import pytest

from telegram_assistant.messages import download_url_to_temp
from telegram_assistant.messages.downloads import DownloadError, make_url_downloader


def _bytes_fetcher(chunks: list[bytes]):
    async def _fetch(url: str, timeout: float) -> AsyncIterator[bytes]:
        for chunk in chunks:
            yield chunk

    return _fetch


def _capture_mkstemp(monkeypatch) -> list[str]:
    """Record every temp path mkstemp hands out, so tests can assert cleanup."""
    created: list[str] = []
    real_mkstemp = tempfile.mkstemp

    def _wrapped(*args, **kwargs):
        fd, path = real_mkstemp(*args, **kwargs)
        created.append(path)
        return fd, path

    monkeypatch.setattr(
        "telegram_assistant.messages.downloads.tempfile.mkstemp", _wrapped
    )
    return created


async def test_download_url_to_temp_writes_body(monkeypatch) -> None:
    created = _capture_mkstemp(monkeypatch)
    fetcher = _bytes_fetcher([b"hello ", b"world"])

    path = await download_url_to_temp(
        "https://example.com/a.png", fetcher=fetcher, max_bytes=1024
    )

    try:
        assert os.path.isfile(path)
        with open(path, "rb") as fh:
            assert fh.read() == b"hello world"
        # The returned path is exactly the one we created.
        assert created == [path]
    finally:
        os.unlink(path)


async def test_download_url_to_temp_rejects_oversize_and_cleans_up(monkeypatch) -> None:
    created = _capture_mkstemp(monkeypatch)
    fetcher = _bytes_fetcher([b"x" * 10, b"y" * 10])

    with pytest.raises(DownloadError) as excinfo:
        await download_url_to_temp(
            "https://example.com/big.bin", fetcher=fetcher, max_bytes=15
        )

    assert "limit" in str(excinfo.value)
    # The partial temp file must be removed on the oversize abort.
    assert created and not os.path.exists(created[0])


async def test_download_url_to_temp_times_out_and_cleans_up(monkeypatch) -> None:
    created = _capture_mkstemp(monkeypatch)

    async def _slow_fetch(url: str, timeout: float) -> AsyncIterator[bytes]:
        await asyncio.sleep(10)
        yield b"never"

    with pytest.raises(DownloadError) as excinfo:
        await download_url_to_temp(
            "https://example.com/slow.bin",
            fetcher=_slow_fetch,
            timeout_seconds=0.05,
        )

    assert "timed out" in str(excinfo.value)
    assert created and not os.path.exists(created[0])


async def test_download_url_to_temp_unreachable_cleans_up(monkeypatch) -> None:
    created = _capture_mkstemp(monkeypatch)

    async def _broken_fetch(url: str, timeout: float) -> AsyncIterator[bytes]:
        raise ConnectionError("connection refused")
        yield b""  # pragma: no cover - makes this an async generator

    with pytest.raises(DownloadError) as excinfo:
        await download_url_to_temp(
            "https://example.com/nope.bin", fetcher=_broken_fetch
        )

    assert "failed to download" in str(excinfo.value)
    assert created and not os.path.exists(created[0])


async def test_download_url_to_temp_rejects_empty_body(monkeypatch) -> None:
    created = _capture_mkstemp(monkeypatch)
    fetcher = _bytes_fetcher([])

    with pytest.raises(DownloadError) as excinfo:
        await download_url_to_temp("https://example.com/empty.bin", fetcher=fetcher)

    assert "empty" in str(excinfo.value)
    assert created and not os.path.exists(created[0])


@pytest.mark.parametrize("bad", [0, -1])
async def test_download_url_to_temp_validates_limits(bad: int) -> None:
    with pytest.raises(ValueError):
        await download_url_to_temp("https://example.com/a", max_bytes=bad)
    with pytest.raises(ValueError):
        await download_url_to_temp("https://example.com/a", timeout_seconds=bad)


async def test_make_url_downloader_uses_bound_limits() -> None:
    downloader = make_url_downloader(
        max_bytes=4, fetcher=_bytes_fetcher([b"toolong"])
    )
    with pytest.raises(DownloadError):
        await downloader("https://example.com/a.bin")
