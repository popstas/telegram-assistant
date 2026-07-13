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
:func:`get_recent_messages`.
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
    returned. Results are newest-first.
    """

    async def search_messages(
        self,
        *,
        chat_id: int,
        query: str,
        from_user: str | int | None = None,
        limit: int = 20,
        topic_id: int | None = None,
    ) -> list[RecentMessage]:
        ...


async def search_messages(
    *,
    backend: SearchBackend,
    chat_id: int,
    query: str,
    from_user: str | int | None = None,
    limit: int = 20,
    topic_id: int | None = None,
    minutes: int | None = None,
    authorizer: Authorizer | None = None,
    now: datetime | None = None,
) -> list[RecentMessage]:
    """Search ``chat_id`` for messages matching ``query``, newest first.

    This is a READ op: when an ``authorizer`` is supplied it must grant READ on
    the target chat or :class:`AccessDenied` is raised before any Telegram call.

    Validation:

    * ``query`` must be non-empty (after stripping);
    * ``limit`` must be positive;
    * ``minutes`` must be positive when given.

    ``minutes`` optionally narrows the result to messages newer than
    ``now - minutes`` (default ``now`` is the current UTC time), applied
    client-side after the backend returns (mirroring :func:`get_recent_messages`):
    the backend returns the newest ``limit`` matches and the window then drops
    any that fall outside it, so the result may be shorter than ``limit``.
    Messages whose ``date`` the backend could not supply are excluded when a
    window is active (their age is unknown).
    """
    if not query or not query.strip():
        raise ValueError("search_messages requires a non-empty query")
    if limit <= 0:
        raise ValueError("search_messages requires a positive limit")
    if minutes is not None and minutes <= 0:
        raise ValueError("search_messages requires a positive minutes window")

    if authorizer is not None:
        await authorizer.require(chat_id, AccessLevel.READ)

    messages = await backend.search_messages(
        chat_id=chat_id,
        query=query,
        from_user=from_user,
        limit=limit,
        topic_id=topic_id,
    )
    if minutes is None:
        return messages

    reference = now if now is not None else datetime.now(UTC)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=UTC)
    cutoff = reference - timedelta(minutes=minutes)

    filtered: list[RecentMessage] = []
    for message in messages:
        if message.date is None:
            continue
        try:
            stamp = datetime.fromisoformat(message.date)
        except ValueError:
            continue
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=UTC)
        if stamp >= cutoff:
            filtered.append(message)
    return filtered


__all__ = [
    "SearchBackend",
    "search_messages",
]
