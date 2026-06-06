"""Boundary validation helpers for message-send inputs."""

from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse

_DELAY_RE = re.compile(r"^(?P<amount>[1-9][0-9]*)(?P<unit>[mhd])$")


def parse_relative_delay(value: str) -> timedelta:
    """Parse a compact delay like ``10m``, ``2h``, or ``1d``."""
    match = _DELAY_RE.fullmatch(value.strip())
    if match is None:
        raise ValueError("delay must be a positive integer followed by m, h, or d")
    amount = int(match.group("amount"))
    unit = match.group("unit")
    if unit == "m":
        return timedelta(minutes=amount)
    if unit == "h":
        return timedelta(hours=amount)
    return timedelta(days=amount)


def parse_schedule_at(value: str) -> datetime:
    """Parse an ISO-8601 datetime string accepted by CLI and HTTP."""
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("schedule_at must be an ISO-8601 datetime") from exc


def resolve_schedule_at(
    *,
    schedule_at: str | datetime | None = None,
    delay: str | None = None,
    delay_seconds: int | None = None,
    now: datetime | None = None,
) -> datetime | None:
    """Resolve absolute or relative schedule inputs to a datetime.

    Exactly one scheduling mode may be supplied. Absolute dates must be in the
    future. Relative delays are computed from ``now`` or the current time.
    """
    modes = [
        schedule_at is not None,
        delay is not None,
        delay_seconds is not None,
    ]
    if sum(modes) > 1:
        raise ValueError("provide exactly one of schedule_at, delay, or delay_seconds")
    if not any(modes):
        return None

    if schedule_at is not None:
        resolved = (
            schedule_at
            if isinstance(schedule_at, datetime)
            else parse_schedule_at(schedule_at)
        )
    elif delay is not None:
        resolved = _now_for_relative(now) + parse_relative_delay(delay)
    else:
        assert delay_seconds is not None
        if delay_seconds <= 0:
            raise ValueError("delay_seconds must be positive")
        resolved = _now_for_relative(now) + timedelta(seconds=delay_seconds)

    reference_now = _now_for_datetime(resolved) if now is None else now
    if _normalize_for_compare(resolved) <= _normalize_for_compare(reference_now):
        raise ValueError("schedule_at must be in the future")
    return resolved


def normalize_attachment_inputs(
    *,
    files: Iterable[str] | None = None,
    file_urls: Iterable[str] | None = None,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Validate attachment references and return immutable tuples."""
    local_files = tuple(str(item) for item in (files or ()))
    urls = tuple(str(item) for item in (file_urls or ()))
    for item in local_files:
        if not item.strip():
            raise ValueError("file references must be non-empty")
        path = Path(item)
        if not path.is_file():
            raise ValueError(f"file does not exist or is not a regular file: {item}")
        if path.stat().st_size <= 0:
            raise ValueError(f"file must not be empty: {item}")
    for item in urls:
        if not item.strip():
            raise ValueError("file_url references must be non-empty")
        parsed = urlparse(item)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("file_url must use http or https")
    return local_files, urls


def enforce_media_root(
    files: Iterable[str],
    *,
    media_root: str | None,
) -> None:
    """Restrict server-local attachment paths to an allowlisted root.

    Enforced only at the HTTP boundary, where ``files`` are supplied by a
    bearer-token holder. Without this, any caller with WRITE access to a chat
    could attach arbitrary process-readable server files (e.g. ``config.yml``,
    the Telethon session) by their path and read them back from the chat.

    ``media_root is None`` disables server-local paths over HTTP entirely; the
    caller must use ``file_urls`` instead. When set, each path must resolve —
    symlinks included — to a location inside the root. The CLI does not call
    this; operator-supplied local paths there are trusted.
    """
    paths = tuple(files)
    if not paths:
        return
    if media_root is None:
        raise ValueError(
            "server-local file paths are not allowed over HTTP; configure "
            "http.media_root to an allowlisted directory, or use file_urls"
        )
    root = Path(media_root).resolve()
    for item in paths:
        resolved = Path(item).resolve()
        if resolved != root and root not in resolved.parents:
            raise ValueError(
                f"file path is outside the allowed media root: {item}"
            )


def _now_for_relative(now: datetime | None) -> datetime:
    return now or datetime.now()


def _now_for_datetime(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return datetime.now()
    return datetime.now(value.tzinfo)


def _normalize_for_compare(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value
    return value.astimezone().replace(tzinfo=None)
