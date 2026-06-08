"""Surface-level attachment validation for media sends.

The pure domain (:mod:`service`) deliberately only checks that attachment
references are non-empty — it has no filesystem or request context. The CLI and
HTTP surfaces share these helpers to enforce the richer rules from the plan:

* local ``files`` must exist, be regular files, and be non-empty;
* ``file_urls`` must use ``http`` or ``https`` (remote URLs are never prefetched
  here for size/type inspection);
* base64 ``attachments`` (``{filename, mime, content_b64}``) are decoded to a
  temp file under a size + allowed-type limit, sent, then cleaned up.
"""

from __future__ import annotations

import base64
import binascii
import os
import shutil
import tempfile
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from urllib.parse import urlparse

# Base64 attachments are intended for small inline files (a screenshot, a short
# document) handed straight through MCP/HTTP JSON; large media should use
# ``file_urls``. The decoded payload is bounded by this default (configurable
# per call) so a hostile or accidental megapayload can't exhaust memory.
DEFAULT_MAX_BASE64_BYTES = 1 * 1024 * 1024  # 1 MB

# Allowed top-level MIME types for base64 attachments. Covers every legitimate
# Telegram media family (images, audio, video, documents, plain text) while
# rejecting obviously malformed or unexpected types.
ALLOWED_BASE64_MIME_TOP_TYPES: tuple[str, ...] = (
    "image",
    "video",
    "audio",
    "application",
    "text",
)


class AttachmentError(ValueError):
    """A local file, URL, or base64 attachment failed surface-level validation."""


@dataclass(frozen=True)
class Base64Attachment:
    """One base64-encoded inline attachment.

    ``filename`` is required (it names the file Telegram shows and supplies the
    extension used for media-type detection). ``content_b64`` is the standard
    base64 encoding of the file bytes. ``mime`` is optional — when present it is
    validated against :data:`ALLOWED_BASE64_MIME_TOP_TYPES`; when omitted
    Telethon infers the type from the filename.
    """

    filename: str
    content_b64: str
    mime: str | None = None


def validate_local_files(paths: Iterable[str]) -> None:
    """Reject missing, non-regular, or empty local file attachments."""
    for path in paths:
        if not path or not str(path).strip():
            raise AttachmentError("file paths must be non-empty references")
        if not os.path.isfile(path):
            raise AttachmentError(f"file not found or not a regular file: {path}")
        if os.path.getsize(path) <= 0:
            raise AttachmentError(f"file is empty: {path}")


def validate_file_urls(urls: Iterable[str]) -> None:
    """Reject blank URLs or URLs that don't use ``http``/``https``."""
    for url in urls:
        if not url or not str(url).strip():
            raise AttachmentError("file URLs must be non-empty references")
        parsed = urlparse(str(url))
        scheme = parsed.scheme.lower()
        if scheme not in ("http", "https") or not parsed.netloc:
            raise AttachmentError(f"file URL must use http or https: {url}")


def _validate_base64_mime(mime: str | None, allowed_top_types: tuple[str, ...]) -> None:
    """Reject a present-but-malformed or disallowed MIME type.

    ``mime`` is optional; a blank/absent value is accepted (Telethon infers the
    type from the filename). When given it must be a well-formed ``type/subtype``
    whose top-level type is in ``allowed_top_types``.
    """
    if mime is None or not mime.strip():
        return
    parts = mime.strip().split("/")
    if len(parts) != 2 or not parts[0].strip() or not parts[1].strip():
        raise AttachmentError(f"invalid MIME type: {mime!r}")
    if parts[0].strip().lower() not in allowed_top_types:
        raise AttachmentError(
            f"MIME type not allowed: {mime} "
            f"(allowed top-level types: {', '.join(allowed_top_types)})"
        )


def decode_base64_attachment(
    *,
    filename: str,
    content_b64: str,
    mime: str | None = None,
    max_bytes: int = DEFAULT_MAX_BASE64_BYTES,
    allowed_top_types: tuple[str, ...] = ALLOWED_BASE64_MIME_TOP_TYPES,
) -> bytes:
    """Validate and decode one base64 attachment, returning the raw bytes.

    No filesystem IO happens here so callers can validate inline input (and
    surface a clean ``400``) before committing to an operation. Raises
    :class:`AttachmentError` on a missing filename, a disallowed/malformed MIME
    type, malformed base64, an empty payload, or a payload over ``max_bytes``.
    The size limit is enforced both cheaply on the encoded length (to avoid
    decoding a megapayload) and exactly on the decoded length.
    """
    if max_bytes <= 0:
        raise ValueError("max_bytes must be positive")
    if not filename or not filename.strip():
        raise AttachmentError("base64 attachment requires a non-empty filename")
    if not os.path.basename(filename.strip()):
        raise AttachmentError(f"base64 attachment filename has no basename: {filename!r}")
    _validate_base64_mime(mime, allowed_top_types)

    raw = (content_b64 or "").strip()
    if not raw:
        raise AttachmentError(
            f"base64 attachment {filename!r} has empty content_b64"
        )
    # Cheap pre-check: base64 inflates bytes by ~4/3, so an encoded string longer
    # than this can only decode to something over the limit. Reject before
    # spending memory decoding it.
    if len(raw) > (max_bytes // 3 + 1) * 4 + 4:
        raise AttachmentError(
            f"base64 attachment {filename!r} exceeds the {max_bytes}-byte limit"
        )
    try:
        data = base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise AttachmentError(
            f"base64 attachment {filename!r} is not valid base64"
        ) from exc
    if not data:
        raise AttachmentError(
            f"base64 attachment {filename!r} decoded to an empty file"
        )
    if len(data) > max_bytes:
        raise AttachmentError(
            f"base64 attachment {filename!r} exceeds the {max_bytes}-byte limit"
        )
    return data


def materialize_base64_attachments(
    attachments: Sequence[Base64Attachment],
    *,
    max_bytes: int = DEFAULT_MAX_BASE64_BYTES,
    allowed_top_types: tuple[str, ...] = ALLOWED_BASE64_MIME_TOP_TYPES,
) -> tuple[str | None, list[str]]:
    """Decode base64 ``attachments`` into temp files, preserving filenames.

    Returns ``(tmpdir, paths)`` where ``tmpdir`` is a fresh temp directory the
    caller must remove (e.g. :func:`shutil.rmtree`) after the send, and ``paths``
    are the written files in attachment order. ``tmpdir`` is ``None`` (and
    ``paths`` empty) when ``attachments`` is empty. The original ``filename`` is
    preserved inside the temp dir so Telegram shows the right name; any directory
    components in ``filename`` are stripped (basename only) so a crafted path
    can't escape the temp dir. On any failure the temp dir is removed before the
    :class:`AttachmentError` propagates.
    """
    if not attachments:
        return None, []

    tmpdir = tempfile.mkdtemp(prefix="tg-b64-")
    paths: list[str] = []
    try:
        for index, att in enumerate(attachments):
            data = decode_base64_attachment(
                filename=att.filename,
                content_b64=att.content_b64,
                mime=att.mime,
                max_bytes=max_bytes,
                allowed_top_types=allowed_top_types,
            )
            # Index-prefix the basename so two attachments that share a filename
            # don't clobber each other while still preserving the real name and
            # extension for Telegram.
            safe = os.path.basename(att.filename.strip())
            dest = os.path.join(tmpdir, f"{index}-{safe}")
            with open(dest, "wb") as fh:
                fh.write(data)
            paths.append(dest)
    except Exception:
        shutil.rmtree(tmpdir, ignore_errors=True)
        raise
    return tmpdir, paths


__all__ = [
    "ALLOWED_BASE64_MIME_TOP_TYPES",
    "DEFAULT_MAX_BASE64_BYTES",
    "AttachmentError",
    "Base64Attachment",
    "decode_base64_attachment",
    "materialize_base64_attachments",
    "validate_file_urls",
    "validate_local_files",
]
