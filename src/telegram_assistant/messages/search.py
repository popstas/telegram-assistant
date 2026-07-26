"""Message text-search domain shared by HTTP, CLI, and the worker.

A single entry point, :func:`search_messages`, runs a server-side text search
inside one resolved chat and returns :class:`RecentMessage` rows newest-first.
Searching is a READ operation: when an :class:`Authorizer` is supplied it must
grant ``READ`` on the target chat or :class:`AccessDenied` is raised before any
Telegram call.

Kept in its own module (like :mod:`reactions`/:mod:`forwarding`) so
:mod:`service` stays focused on the send flow. The domain depends on the narrow
:class:`SearchBackend` protocol; the production Telethon adapter lives in
:mod:`telethon_backend` and tests inject fakes. The optional ``minutes``
time-window filter is applied client-side in the service for parity with
:func:`get_recent_messages`; the fixed ``from_date``/``to_date`` range is
instead pushed down to the backend (so older matches are not lost behind the
first ``limit`` rows) and re-checked here for inclusive UTC semantics.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Protocol

from telegram_assistant.access.service import AccessLevel, Authorizer
from telegram_assistant.messages.service import RecentMessage


class SearchBackend(Protocol):
    """Telethon-facing surface needed to text-search a chat.

    ``query`` is the (non-empty) search string handed to Telegram's server-side
    search. ``from_user`` optionally narrows to one sender, ``topic_id`` scopes
    the search to one forum topic, and ``limit`` bounds the number of rows
    returned. ``from_date``/``to_date`` (always both or neither, timezone-aware
    UTC — the domain validates and normalises them) bound the message date
    server-side. Results are newest-first.
    """

    async def search_messages(
        self,
        *,
        chat_id: int,
        query: str,
        from_user: str | int | None = None,
        limit: int = 20,
        topic_id: int | None = None,
        from_date: datetime | None = None,
        to_date: datetime | None = None,
    ) -> list[RecentMessage]:
        ...


def normalize_search_range(
    *,
    from_date: datetime | None,
    to_date: datetime | None,
    minutes: int | None = None,
) -> tuple[datetime, datetime] | None:
    """Validate a fixed search range and return it normalised to UTC.

    Returns ``None`` when neither bound is given. Raises :class:`ValueError`
    when the pair is unusable, so every surface (CLI/HTTP/MCP) rejects the same
    inputs with the same message before any Telegram call:

    * only one of ``from_date``/``to_date`` supplied;
    * either bound is naive (no timezone) — the intended instant is ambiguous;
    * ``from_date`` is later than ``to_date``;
    * the range is combined with the relative ``minutes`` window.
    """
    if from_date is None and to_date is None:
        return None
    if from_date is None or to_date is None:
        raise ValueError(
            "search_messages requires both from_date and to_date when using a date range"
        )
    if from_date.tzinfo is None or from_date.tzinfo.utcoffset(from_date) is None:
        raise ValueError("search_messages requires a timezone-aware from_date")
    if to_date.tzinfo is None or to_date.tzinfo.utcoffset(to_date) is None:
        raise ValueError("search_messages requires a timezone-aware to_date")
    if minutes is not None:
        raise ValueError(
            "search_messages accepts either minutes or a from_date/to_date range, not both"
        )
    start = from_date.astimezone(UTC)
    end = to_date.astimezone(UTC)
    if start > end:
        raise ValueError("search_messages requires from_date <= to_date")
    return start, end


def _parse_message_date(message: RecentMessage) -> datetime | None:
    """Return the message timestamp as an aware UTC datetime, or ``None``."""
    if message.date is None:
        return None
    try:
        stamp = datetime.fromisoformat(message.date)
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=UTC)
    return stamp.astimezone(UTC)


async def search_messages(
    *,
    backend: SearchBackend,
    chat_id: int,
    query: str,
    from_user: str | int | None = None,
    limit: int = 20,
    topic_id: int | None = None,
    minutes: int | None = None,
    from_date: datetime | None = None,
    to_date: datetime | None = None,
    authorizer: Authorizer | None = None,
    now: datetime | None = None,
) -> list[RecentMessage]:
    """Search ``chat_id`` for messages matching ``query``, newest first.

    This is a READ op: when an ``authorizer`` is supplied it must grant READ on
    the target chat or :class:`AccessDenied` is raised before any Telegram call.

    Validation:

    * ``query`` must be non-empty (after stripping);
    * ``limit`` must be positive;
    * ``minutes`` must be positive when given;
    * ``from_date``/``to_date`` must satisfy :func:`normalize_search_range`.

    ``minutes`` optionally narrows the result to messages newer than
    ``now - minutes`` (default ``now`` is the current UTC time), applied
    client-side after the backend returns (mirroring :func:`get_recent_messages`):
    the backend returns the newest ``limit`` matches and the window then drops
    any that fall outside it, so the result may be shorter than ``limit``.
    Messages whose ``date`` the backend could not supply are excluded when a
    window is active (their age is unknown).

    ``from_date``/``to_date`` are the fixed-range alternative (mutually
    exclusive with ``minutes``). Unlike ``minutes`` the bounds are pushed down
    to the backend, which pages until ``limit`` in-range rows are collected, so
    matches older than the newest ``limit`` hits are not lost. The bounds are
    inclusive (``from_date <= date <= to_date``) and normalised to UTC; the
    check is re-applied here after mapping because Telegram's own date filter
    is second-granular, and rows without a parseable date are excluded.

    Use :func:`normalize_search_range` to obtain the same normalised bounds a
    surface should echo back to the caller.
    """
    if not query or not query.strip():
        raise ValueError("search_messages requires a non-empty query")
    if limit <= 0:
        raise ValueError("search_messages requires a positive limit")
    if minutes is not None and minutes <= 0:
        raise ValueError("search_messages requires a positive minutes window")
    date_range = normalize_search_range(
        from_date=from_date, to_date=to_date, minutes=minutes
    )

    if authorizer is not None:
        await authorizer.require(chat_id, AccessLevel.READ)

    messages = await backend.search_messages(
        chat_id=chat_id,
        query=query,
        from_user=from_user,
        limit=limit,
        topic_id=topic_id,
        from_date=date_range[0] if date_range is not None else None,
        to_date=date_range[1] if date_range is not None else None,
    )
    if date_range is not None:
        start, end = date_range
        in_range: list[RecentMessage] = []
        for message in messages:
            stamp = _parse_message_date(message)
            if stamp is None:
                continue
            if start <= stamp <= end:
                in_range.append(message)
        return in_range[:limit]
    if minutes is None:
        return messages

    reference = now if now is not None else datetime.now(UTC)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=UTC)
    cutoff = reference - timedelta(minutes=minutes)

    filtered: list[RecentMessage] = []
    for message in messages:
        stamp = _parse_message_date(message)
        if stamp is None:
            continue
        if stamp >= cutoff:
            filtered.append(message)
    return filtered


__all__ = [
    "SearchBackend",
    "normalize_search_range",
    "search_messages",
]
