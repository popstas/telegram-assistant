"""Download remote ``file_urls`` to temp files before sending.

Instead of handing ``http``/``https`` URLs to Telethon and letting it fetch
them itself, the send path downloads each URL to a local temp file first and
passes the local path. This gives one place to enforce **size and time limits**
and to surface unreachable/oversize/timeout failures as a clear
:class:`DownloadError`, rather than an opaque Telethon error mid-send.

The temp file belongs to the caller: :func:`~telegram_assistant.messages.service.send_message`
removes it in a ``finally`` once the send completes or fails. ``fetcher`` is
injectable so tests can drive download/size/timeout behaviour without real
network traffic.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import suppress
from urllib.parse import urlparse

# Bounds for a single ``file_urls`` download. Generous enough for typical media
# while still bounding memory/time on a stalled or hostile URL.
DEFAULT_MAX_BYTES = 50 * 1024 * 1024  # 50 MB
DEFAULT_TIMEOUT_SECONDS = 30.0
_CHUNK_SIZE = 64 * 1024

# A fetcher yields the response body of ``url`` in chunks, honouring ``timeout``.
Fetcher = Callable[[str, float], AsyncIterator[bytes]]
# A downloader turns one URL into a local temp-file path (caller cleans it up).
Downloader = Callable[[str], Awaitable[str]]


class DownloadError(ValueError):
    """A ``file_urls`` download failed: unreachable, oversize, timeout, or empty."""


async def _httpx_fetcher(url: str, timeout: float) -> AsyncIterator[bytes]:
    """Default fetcher — stream ``url`` over httpx, following redirects."""
    import httpx

    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        async with client.stream("GET", url) as response:
            response.raise_for_status()
            async for chunk in response.aiter_bytes(_CHUNK_SIZE):
                yield chunk


def _suffix_for(url: str) -> str:
    """Best-effort file extension from the URL path, for a tidy temp name."""
    name = os.path.basename(urlparse(url).path)
    _, ext = os.path.splitext(name)
    return ext if 0 < len(ext) <= 16 else ""


def _remove(path: str) -> None:
    with suppress(OSError):
        os.unlink(path)


async def download_url_to_temp(
    url: str,
    *,
    max_bytes: int = DEFAULT_MAX_BYTES,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    fetcher: Fetcher | None = None,
) -> str:
    """Stream ``url`` to a temp file and return its path; the caller deletes it.

    Enforces ``max_bytes`` (raises :class:`DownloadError` as soon as the body
    exceeds it, without buffering the whole response) and ``timeout_seconds``
    (a whole-download deadline). Any fetch/IO failure, an oversize body, a
    timeout, or an empty download is surfaced as :class:`DownloadError`. On any
    failure the partial temp file is removed before raising.
    """
    if max_bytes <= 0:
        raise ValueError("max_bytes must be positive")
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    active_fetcher = fetcher or _httpx_fetcher

    fd, path = tempfile.mkstemp(prefix="tg-attach-", suffix=_suffix_for(url))
    os.close(fd)
    written = 0

    async def _stream() -> None:
        nonlocal written
        with open(path, "wb") as fh:
            async for chunk in active_fetcher(url, timeout_seconds):
                written += len(chunk)
                if written > max_bytes:
                    raise DownloadError(
                        f"download exceeded the {max_bytes}-byte limit: {url}"
                    )
                fh.write(chunk)

    try:
        await asyncio.wait_for(_stream(), timeout=timeout_seconds)
    except DownloadError:
        _remove(path)
        raise
    except TimeoutError as exc:
        _remove(path)
        raise DownloadError(
            f"download timed out after {timeout_seconds}s: {url}"
        ) from exc
    except Exception as exc:
        _remove(path)
        raise DownloadError(f"failed to download {url}: {exc}") from exc

    if written <= 0:
        _remove(path)
        raise DownloadError(f"downloaded file is empty: {url}")
    return path


def make_url_downloader(
    *,
    max_bytes: int = DEFAULT_MAX_BYTES,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    fetcher: Fetcher | None = None,
) -> Downloader:
    """Build a :data:`Downloader` bound to the given limits and fetcher.

    Surfaces pass the result to ``send_message`` so each ``file_urls`` entry is
    downloaded to a temp file before reaching Telethon.
    """

    async def _download(url: str) -> str:
        return await download_url_to_temp(
            url,
            max_bytes=max_bytes,
            timeout_seconds=timeout_seconds,
            fetcher=fetcher,
        )

    return _download


__all__ = [
    "DEFAULT_MAX_BYTES",
    "DEFAULT_TIMEOUT_SECONDS",
    "Downloader",
    "DownloadError",
    "Fetcher",
    "download_url_to_temp",
    "make_url_downloader",
]
