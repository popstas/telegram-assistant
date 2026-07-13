"""Message media-download domain shared by HTTP, CLI, and the worker.

A single entry point, :func:`download_media`, downloads the media/document/voice
attached to one already-existing message to a local file on the server. This is
a READ operation: when an :class:`Authorizer` is supplied it must grant ``READ``
on the source chat or :class:`AccessDenied` is raised before any Telegram call.

Distinct from :mod:`downloads`, which fetches a URL to a temp file **for
sending**; this op pulls media **out of** an existing message.

Following the project's service/backend split, the domain depends on the narrow
:class:`MediaDownloadBackend` protocol; the production Telethon adapter lives in
:mod:`telethon_backend` and tests inject fakes. The backend is split into a
cheap :meth:`probe_media` (metadata only) and :meth:`download_media` (the actual
transfer) so target-path resolution, the no-media error, and the optional
size guard all live in the pure service and run before (and during) a dry-run.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Protocol

from telegram_assistant.access.service import AccessLevel, Authorizer


class NoDownloadableMediaError(ValueError):
    """The target message carries no downloadable media.

    Raised when a text-only message (or a missing message id) is asked to be
    downloaded. A :class:`ValueError` so surfaces map it to a 400-style error.
    """

    def __init__(self, message_id: int, *, chat_id: int) -> None:
        self.message_id = message_id
        self.chat_id = chat_id
        super().__init__(
            f"message {message_id} in chat {chat_id} has no downloadable media"
        )


class MediaTooLargeError(ValueError):
    """The media exceeds the requested ``max_bytes`` guard.

    Raised before the transfer starts when the probed size is known and over
    the caller-supplied limit. A :class:`ValueError` so surfaces map it to a
    400-style error.
    """

    def __init__(self, *, size: int, max_bytes: int) -> None:
        self.size = size
        self.max_bytes = max_bytes
        super().__init__(
            f"media size {size} bytes exceeds the {max_bytes}-byte limit"
        )


@dataclass(frozen=True)
class MediaInfo:
    """Metadata for the media attached to a message.

    ``filename`` is the original file name Telegram carries (``None`` when the
    media has none — e.g. a photo or a voice note); ``size`` is the byte size
    (``None`` when unknown); ``mime`` is the MIME type (``None`` when unknown).
    """

    filename: str | None = None
    size: int | None = None
    mime: str | None = None


@dataclass(frozen=True)
class DownloadedMedia:
    """What the backend actually wrote to disk.

    ``path`` is the saved file path, ``size`` its byte size, ``mime`` its MIME
    type when known.
    """

    path: str
    size: int
    mime: str | None = None


class MediaDownloadBackend(Protocol):
    """Telethon-facing surface needed to download a message's media.

    ``probe_media`` returns metadata (or ``None`` when the message has no
    downloadable media) without transferring anything; the service uses it to
    resolve the target path and enforce the size guard. ``download_media``
    performs the transfer to ``target_path`` and reports what was written.
    Implementations translate ``FloodWaitError`` into the project's queue signal.
    """

    async def probe_media(
        self, *, chat_id: int, message_id: int
    ) -> MediaInfo | None:
        ...

    async def download_media(
        self, *, chat_id: int, message_id: int, target_path: str
    ) -> DownloadedMedia:
        ...


@dataclass(frozen=True)
class MediaDownloadRequest:
    """Input to :func:`download_media`.

    ``telegram_chat_id`` is the resolved numeric source chat id and
    ``message_id`` the target message. Exactly one of ``out_path`` (an explicit
    destination file) or ``out_dir`` (a directory the original filename is
    joined into) must be given. ``max_bytes`` optionally caps the download
    size; ``dry_run`` resolves + authorizes + probes (validating the media and
    resolving the path) but does not transfer. ``chat_name`` is carried through
    for logging only.
    """

    telegram_chat_id: int
    message_id: int
    out_path: str | None = None
    out_dir: str | None = None
    max_bytes: int | None = None
    dry_run: bool = False
    chat_name: str | None = None


@dataclass(frozen=True)
class MediaDownloadResult:
    """Result of a download operation.

    ``path`` is the resolved (dry-run) or actually-written target path;
    ``size`` and ``mime`` come from the probe on a dry-run and from the written
    file otherwise. ``dry_run`` is ``True`` when nothing was actually written.
    """

    telegram_chat_id: int
    telegram_message_id: int
    path: str
    size: int | None
    mime: str | None
    dry_run: bool
    chat_name: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "telegram_chat_id": self.telegram_chat_id,
            "telegram_message_id": self.telegram_message_id,
            "path": self.path,
            "size": self.size,
            "mime": self.mime,
            "dry_run": self.dry_run,
            "chat_name": self.chat_name,
        }


def _resolve_target_path(
    *, out_path: str | None, out_dir: str | None, info: MediaInfo, message_id: int
) -> str:
    """Resolve the destination file path from the request + probed metadata.

    ``out_path`` is used verbatim. For ``out_dir`` the original filename is
    joined in (basename only, so a crafted name can't escape the directory);
    when the media has no name a stable ``message-<id>.bin`` fallback is used.
    """
    if out_path is not None:
        return out_path
    assert out_dir is not None  # guaranteed by the caller's validation
    name = os.path.basename((info.filename or "").strip())
    if not name:
        name = f"message-{message_id}.bin"
    return os.path.join(out_dir, name)


async def download_media(
    backend: MediaDownloadBackend,
    *,
    request: MediaDownloadRequest,
    authorizer: Authorizer | None = None,
) -> MediaDownloadResult:
    """Download the media attached to ``request.message_id`` to a local file.

    Validation:

    * ``message_id`` must be a positive integer;
    * exactly one of ``out_path`` / ``out_dir`` must be given;
    * ``max_bytes``, when given, must be positive.

    Reading a message's media is a READ op. When an ``authorizer`` is supplied
    it must grant ``READ`` on the source chat or :class:`AccessDenied` is raised
    before the backend is touched.

    The message is probed for metadata first: a message with no downloadable
    media raises :class:`NoDownloadableMediaError`, and a known size over
    ``max_bytes`` raises :class:`MediaTooLargeError` — both before any transfer.
    ``dry_run`` runs the access check, the probe, the size guard, and the
    path resolution, then returns without writing anything.
    """
    if request.message_id <= 0:
        raise ValueError("message_id must be a positive integer")
    if (request.out_path is None) == (request.out_dir is None):
        raise ValueError("exactly one of out_path or out_dir must be given")
    if request.max_bytes is not None and request.max_bytes <= 0:
        raise ValueError("max_bytes must be a positive integer")

    if authorizer is not None:
        await authorizer.require(request.telegram_chat_id, AccessLevel.READ)

    info = await backend.probe_media(
        chat_id=request.telegram_chat_id, message_id=request.message_id
    )
    if info is None:
        raise NoDownloadableMediaError(
            request.message_id, chat_id=request.telegram_chat_id
        )

    if (
        request.max_bytes is not None
        and info.size is not None
        and info.size > request.max_bytes
    ):
        raise MediaTooLargeError(size=info.size, max_bytes=request.max_bytes)

    target_path = _resolve_target_path(
        out_path=request.out_path,
        out_dir=request.out_dir,
        info=info,
        message_id=request.message_id,
    )

    if request.dry_run:
        return MediaDownloadResult(
            telegram_chat_id=request.telegram_chat_id,
            telegram_message_id=request.message_id,
            path=target_path,
            size=info.size,
            mime=info.mime,
            dry_run=True,
            chat_name=request.chat_name,
        )

    downloaded = await backend.download_media(
        chat_id=request.telegram_chat_id,
        message_id=request.message_id,
        target_path=target_path,
    )
    return MediaDownloadResult(
        telegram_chat_id=request.telegram_chat_id,
        telegram_message_id=request.message_id,
        path=downloaded.path,
        size=downloaded.size,
        mime=downloaded.mime if downloaded.mime is not None else info.mime,
        dry_run=False,
        chat_name=request.chat_name,
    )


__all__ = [
    "DownloadedMedia",
    "MediaDownloadBackend",
    "MediaDownloadRequest",
    "MediaDownloadResult",
    "MediaInfo",
    "MediaTooLargeError",
    "NoDownloadableMediaError",
    "download_media",
]
