"""Surface-level attachment validation for media sends.

The pure domain (:mod:`service`) deliberately only checks that attachment
references are non-empty — it has no filesystem or request context. The CLI and
HTTP surfaces share these helpers to enforce the richer rules from the plan:

* local ``files`` must exist, be regular files, and be non-empty;
* ``file_urls`` must use ``http`` or ``https`` (remote URLs are never prefetched
  here for size/type inspection).
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from urllib.parse import urlparse


class AttachmentError(ValueError):
    """A local file or URL attachment failed surface-level validation."""


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
        scheme = urlparse(str(url)).scheme.lower()
        if scheme not in ("http", "https"):
            raise AttachmentError(f"file URL must use http or https: {url}")


__all__ = ["AttachmentError", "validate_file_urls", "validate_local_files"]
