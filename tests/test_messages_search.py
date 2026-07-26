"""Tests for Task 10 — `messages search` READ op + Telethon adapter.

The domain op :func:`search_messages` runs a server-side text search inside one
resolved chat and returns :class:`RecentMessage` rows newest-first. It is gated
behind READ-level authorization: a config that grants no READ raises
:class:`AccessDenied` before any backend call. The optional ``minutes`` window
is applied client-side in the service for parity with ``get_recent_messages``.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest

from telegram_assistant.access import AccessDenied, AccessLevel, Authorizer
from telegram_assistant.config.models import AccessConfig, AccessRule
from telegram_assistant.entities import ResolvedEntity
from telegram_assistant.messages import (
    RecentMessage,
    normalize_search_range,
    search_messages,
)


class FakeResolver:
    def __init__(self, mapping: dict[object, int]) -> None:
        self._mapping = mapping

    async def resolve(self, ref: object) -> ResolvedEntity:
        return ResolvedEntity(chat_id=self._mapping[ref], title=str(ref), kind="channel")


class FakeSearchBackend:
    """In-memory SearchBackend recording the passed-through arguments."""

    def __init__(self, messages: list[RecentMessage]) -> None:
        self._messages = messages
        self.calls: list[dict[str, Any]] = []

    async def search_messages(
        self,
        *,
        chat_id: int,
        query: str,
        from_user: str | int | None = None,
        limit: int = 20,
        topic_id: int | None = None,
        from_date: dt.datetime | None = None,
        to_date: dt.datetime | None = None,
    ) -> list[RecentMessage]:
        self.calls.append(
            {
                "chat_id": chat_id,
                "query": query,
                "from_user": from_user,
                "limit": limit,
                "topic_id": topic_id,
                "from_date": from_date,
                "to_date": to_date,
            }
        )
        return self._messages[:limit]


def _messages(n: int) -> list[RecentMessage]:
    return [
        RecentMessage(
            id=i,
            sender=f"user{i}",
            date=f"2026-06-06T00:00:0{i}+00:00",
            reply_to=None,
            text=f"needle {i}",
        )
        for i in range(1, n + 1)
    ]


def _msg_at(id: int, when: dt.datetime) -> RecentMessage:
    return RecentMessage(
        id=id,
        sender=f"user{id}",
        date=when.isoformat(),
        reply_to=None,
        text=f"needle {id}",
    )


@pytest.mark.asyncio
async def test_search_returns_rows_and_passes_args() -> None:
    backend = FakeSearchBackend(_messages(3))
    out = await search_messages(
        backend=backend,
        chat_id=42,
        query="needle",
        from_user="@bob",
        limit=10,
        topic_id=7,
    )
    assert backend.calls == [
        {
            "chat_id": 42,
            "query": "needle",
            "from_user": "@bob",
            "limit": 10,
            "topic_id": 7,
            "from_date": None,
            "to_date": None,
        }
    ]
    assert [m.id for m in out] == [1, 2, 3]
    assert all(isinstance(m, RecentMessage) for m in out)


@pytest.mark.asyncio
async def test_search_default_limit_is_twenty() -> None:
    backend = FakeSearchBackend(_messages(5))
    await search_messages(backend=backend, chat_id=42, query="needle")
    assert backend.calls[0]["limit"] == 20


@pytest.mark.asyncio
async def test_search_rejects_empty_query() -> None:
    backend = FakeSearchBackend(_messages(3))
    with pytest.raises(ValueError):
        await search_messages(backend=backend, chat_id=42, query="   ")
    assert backend.calls == []


@pytest.mark.asyncio
async def test_search_rejects_nonpositive_limit() -> None:
    backend = FakeSearchBackend(_messages(3))
    with pytest.raises(ValueError):
        await search_messages(backend=backend, chat_id=42, query="x", limit=0)
    assert backend.calls == []


@pytest.mark.asyncio
async def test_search_rejects_nonpositive_minutes() -> None:
    backend = FakeSearchBackend(_messages(3))
    with pytest.raises(ValueError):
        await search_messages(backend=backend, chat_id=42, query="x", minutes=0)
    assert backend.calls == []


@pytest.mark.asyncio
async def test_search_minutes_excludes_old_messages() -> None:
    now = dt.datetime(2026, 6, 6, 12, 0, 0, tzinfo=dt.UTC)
    backend = FakeSearchBackend(
        [
            _msg_at(3, now - dt.timedelta(minutes=2)),  # inside window
            _msg_at(2, now - dt.timedelta(minutes=8)),  # inside
            _msg_at(1, now - dt.timedelta(minutes=30)),  # outside
        ]
    )
    out = await search_messages(
        backend=backend, chat_id=42, query="needle", minutes=10, now=now
    )
    assert [m.id for m in out] == [3, 2]


@pytest.mark.asyncio
async def test_search_minutes_excludes_messages_without_date() -> None:
    now = dt.datetime(2026, 6, 6, 12, 0, 0, tzinfo=dt.UTC)
    backend = FakeSearchBackend(
        [
            _msg_at(2, now - dt.timedelta(minutes=1)),
            RecentMessage(id=1, sender="x", date=None, reply_to=None, text="no date"),
        ]
    )
    out = await search_messages(
        backend=backend, chat_id=42, query="needle", minutes=10, now=now
    )
    assert [m.id for m in out] == [2]


@pytest.mark.asyncio
async def test_search_minutes_excludes_messages_with_unparseable_date() -> None:
    now = dt.datetime(2026, 6, 6, 12, 0, 0, tzinfo=dt.UTC)
    backend = FakeSearchBackend(
        [
            _msg_at(2, now - dt.timedelta(minutes=1)),
            RecentMessage(
                id=1, sender="x", date="not-a-timestamp", reply_to=None, text="bad date"
            ),
        ]
    )
    out = await search_messages(
        backend=backend, chat_id=42, query="needle", minutes=10, now=now
    )
    assert [m.id for m in out] == [2]


@pytest.mark.asyncio
async def test_search_minutes_treats_naive_now_as_utc() -> None:
    naive_now = dt.datetime(2026, 6, 6, 12, 0, 0)  # noqa: DTZ001 — naive on purpose
    aware_now = naive_now.replace(tzinfo=dt.UTC)
    backend = FakeSearchBackend(
        [
            _msg_at(3, aware_now - dt.timedelta(minutes=2)),  # inside window
            _msg_at(1, aware_now - dt.timedelta(minutes=30)),  # outside
        ]
    )
    out = await search_messages(
        backend=backend, chat_id=42, query="needle", minutes=10, now=naive_now
    )
    assert [m.id for m in out] == [3]


@pytest.mark.asyncio
async def test_search_minutes_none_returns_all() -> None:
    backend = FakeSearchBackend(_messages(3))
    out = await search_messages(backend=backend, chat_id=42, query="needle", minutes=None)
    assert len(out) == 3


# ---------------------------------------------------------------------------
# Fixed date range (from_date/to_date)
# ---------------------------------------------------------------------------


_FROM = dt.datetime(2026, 7, 1, 0, 0, 0, tzinfo=dt.UTC)
_TO = dt.datetime(2026, 7, 10, 23, 59, 59, tzinfo=dt.UTC)


@pytest.mark.asyncio
async def test_search_range_is_pushed_down_to_backend_normalised_to_utc() -> None:
    tz = dt.timezone(dt.timedelta(hours=3))
    backend = FakeSearchBackend([_msg_at(1, _FROM)])
    await search_messages(
        backend=backend,
        chat_id=42,
        query="needle",
        from_date=_FROM.astimezone(tz),
        to_date=_TO.astimezone(tz),
    )
    call = backend.calls[0]
    assert call["from_date"] == _FROM
    assert call["to_date"] == _TO
    assert call["from_date"].tzinfo is dt.UTC
    assert call["to_date"].tzinfo is dt.UTC


@pytest.mark.asyncio
async def test_search_range_includes_both_bounds_exactly() -> None:
    backend = FakeSearchBackend([_msg_at(2, _TO), _msg_at(1, _FROM)])
    out = await search_messages(
        backend=backend, chat_id=42, query="needle", from_date=_FROM, to_date=_TO
    )
    assert [m.id for m in out] == [2, 1]


@pytest.mark.asyncio
async def test_search_range_excludes_just_outside_bounds() -> None:
    backend = FakeSearchBackend(
        [
            _msg_at(4, _TO + dt.timedelta(seconds=1)),  # just after
            _msg_at(3, _TO),  # inclusive upper bound
            _msg_at(2, _FROM),  # inclusive lower bound
            _msg_at(1, _FROM - dt.timedelta(seconds=1)),  # just before
        ]
    )
    out = await search_messages(
        backend=backend, chat_id=42, query="needle", from_date=_FROM, to_date=_TO
    )
    assert [m.id for m in out] == [3, 2]


@pytest.mark.asyncio
async def test_search_range_compares_across_timezones() -> None:
    tz = dt.timezone(dt.timedelta(hours=-5))
    inside = (_FROM + dt.timedelta(days=1)).astimezone(tz)
    backend = FakeSearchBackend([_msg_at(1, inside)])
    out = await search_messages(
        backend=backend, chat_id=42, query="needle", from_date=_FROM, to_date=_TO
    )
    assert [m.id for m in out] == [1]


@pytest.mark.asyncio
async def test_search_range_excludes_rows_without_parseable_date() -> None:
    backend = FakeSearchBackend(
        [
            _msg_at(3, _FROM),
            RecentMessage(id=2, sender="x", date=None, reply_to=None, text="no date"),
            RecentMessage(id=1, sender="x", date="whenever", reply_to=None, text="bad"),
        ]
    )
    out = await search_messages(
        backend=backend, chat_id=42, query="needle", from_date=_FROM, to_date=_TO
    )
    assert [m.id for m in out] == [3]


@pytest.mark.asyncio
async def test_search_range_respects_limit() -> None:
    """``limit`` reaches the backend and bounds the in-range rows returned."""
    backend = FakeSearchBackend(
        [_msg_at(i, _FROM + dt.timedelta(hours=i)) for i in range(5, 0, -1)]
    )
    out = await search_messages(
        backend=backend, chat_id=42, query="needle", limit=2, from_date=_FROM, to_date=_TO
    )
    assert backend.calls[0]["limit"] == 2
    assert [m.id for m in out] == [5, 4]


@pytest.mark.asyncio
async def test_search_rejects_single_bound() -> None:
    backend = FakeSearchBackend(_messages(3))
    with pytest.raises(ValueError, match="both from_date and to_date"):
        await search_messages(backend=backend, chat_id=42, query="x", from_date=_FROM)
    with pytest.raises(ValueError, match="both from_date and to_date"):
        await search_messages(backend=backend, chat_id=42, query="x", to_date=_TO)
    assert backend.calls == []


@pytest.mark.asyncio
async def test_search_rejects_naive_bounds() -> None:
    backend = FakeSearchBackend(_messages(3))
    naive_from = dt.datetime(2026, 7, 1, 0, 0, 0)  # noqa: DTZ001 — naive on purpose
    naive_to = dt.datetime(2026, 7, 10, 0, 0, 0)  # noqa: DTZ001 — naive on purpose
    with pytest.raises(ValueError, match="timezone-aware from_date"):
        await search_messages(
            backend=backend, chat_id=42, query="x", from_date=naive_from, to_date=_TO
        )
    with pytest.raises(ValueError, match="timezone-aware to_date"):
        await search_messages(
            backend=backend, chat_id=42, query="x", from_date=_FROM, to_date=naive_to
        )
    assert backend.calls == []


@pytest.mark.asyncio
async def test_search_rejects_inverted_range() -> None:
    backend = FakeSearchBackend(_messages(3))
    with pytest.raises(ValueError, match="from_date <= to_date"):
        await search_messages(
            backend=backend, chat_id=42, query="x", from_date=_TO, to_date=_FROM
        )
    assert backend.calls == []


@pytest.mark.asyncio
async def test_search_rejects_minutes_combined_with_range() -> None:
    backend = FakeSearchBackend(_messages(3))
    with pytest.raises(ValueError, match="either minutes or a from_date/to_date range"):
        await search_messages(
            backend=backend,
            chat_id=42,
            query="x",
            minutes=10,
            from_date=_FROM,
            to_date=_TO,
        )
    assert backend.calls == []


@pytest.mark.asyncio
async def test_search_range_validated_before_access_check() -> None:
    """Validation is cheap and surface-shared: it runs before the authorizer."""
    backend = FakeSearchBackend(_messages(1))
    config = AccessConfig(rules=[AccessRule(chat="@team", permission="write")])
    authorizer = Authorizer(config, resolver=FakeResolver({"@team": 42}))
    with pytest.raises(ValueError):
        await search_messages(
            backend=backend,
            chat_id=42,
            query="needle",
            from_date=_FROM,
            authorizer=authorizer,
        )
    assert backend.calls == []


def test_normalize_search_range_returns_none_without_bounds() -> None:
    assert normalize_search_range(from_date=None, to_date=None) is None
    assert normalize_search_range(from_date=None, to_date=None, minutes=10) is None


def test_normalize_search_range_returns_utc_bounds() -> None:
    tz = dt.timezone(dt.timedelta(hours=5, minutes=30))
    bounds = normalize_search_range(
        from_date=_FROM.astimezone(tz), to_date=_TO.astimezone(tz)
    )
    assert bounds == (_FROM, _TO)


def test_normalize_search_range_allows_equal_bounds() -> None:
    assert normalize_search_range(from_date=_FROM, to_date=_FROM) == (_FROM, _FROM)


@pytest.mark.asyncio
async def test_search_allowed_by_read_rule() -> None:
    backend = FakeSearchBackend(_messages(2))
    config = AccessConfig(rules=[AccessRule(chat="@team", permission="read")])
    authorizer = Authorizer(config, resolver=FakeResolver({"@team": 42}))
    out = await search_messages(
        backend=backend, chat_id=42, query="needle", authorizer=authorizer
    )
    assert len(out) == 2
    assert backend.calls[0]["chat_id"] == 42


@pytest.mark.asyncio
async def test_search_denied_by_write_only_rule() -> None:
    backend = FakeSearchBackend(_messages(1))
    config = AccessConfig(rules=[AccessRule(chat="@team", permission="write")])
    authorizer = Authorizer(config, resolver=FakeResolver({"@team": 42}))
    with pytest.raises(AccessDenied) as excinfo:
        await search_messages(
            backend=backend, chat_id=42, query="needle", authorizer=authorizer
        )
    assert excinfo.value.required_level is AccessLevel.READ
    assert backend.calls == []


@pytest.mark.asyncio
async def test_search_no_authorizer_is_allow_all() -> None:
    backend = FakeSearchBackend(_messages(3))
    out = await search_messages(backend=backend, chat_id=12345, query="needle")
    assert len(out) == 3


# ---------------------------------------------------------------------------
# Telethon adapter
# ---------------------------------------------------------------------------


class _FakeMsg:
    def __init__(
        self, *, id, message=None, sender_username=None, reply_to=None, date=None, media=None
    ):
        self.id = id
        self.message = message
        self.sender = type("S", (), {"username": sender_username})()
        self.reply_to_msg_id = reply_to
        self.date = date
        self.media = media


class _FakeClient:
    """Client double answering the single ``messages.Search`` RPC."""

    def __init__(self, msgs):
        self._msgs = msgs
        self.requests: list[Any] = []

    async def get_input_entity(self, ref):
        return ref

    async def __call__(self, request):
        self.requests.append(request)
        if len(self.requests) > 1:
            return type("_Page", (), {"messages": [], "users": []})()
        return type(
            "_Page", (), {"messages": self._msgs[: request.limit], "users": []}
        )()


@pytest.mark.asyncio
async def test_telethon_search_passes_args_and_maps_rows() -> None:
    from telegram_assistant.messages.telethon_backend import TelethonSearchBackend

    when = dt.datetime(2026, 6, 6, 12, 0, 0, tzinfo=dt.UTC)
    media = type("MessageMediaPhoto", (), {})()
    client = _FakeClient(
        [
            _FakeMsg(id=2, message="", sender_username="bob", date=when, media=media),
            _FakeMsg(id=1, message="hi needle", sender_username="alice", reply_to=1, date=when),
        ]
    )
    backend = TelethonSearchBackend(client)
    out = await backend.search_messages(
        chat_id=42, query="needle", from_user="@bob", limit=5, topic_id=7
    )
    assert len(client.requests) == 1
    request = client.requests[0]
    assert request.peer == 42
    assert request.q == "needle"
    assert request.from_id == "@bob"
    assert request.top_msg_id == 7
    assert request.limit == 5
    assert request.min_date is None
    assert request.max_date is None
    assert out[0].id == 2
    assert out[0].text == "[photo]"  # media-only summary
    assert out[0].sender == "bob"
    assert out[0].date == "2026-06-06T12:00:00+00:00"
    assert out[1].text == "hi needle"
    assert out[1].reply_to == 1


@pytest.mark.asyncio
async def test_telethon_search_translates_flood_wait() -> None:
    from telegram_assistant.messages.telethon_backend import TelethonSearchBackend
    from telegram_assistant.worker.queue import FloodWaitError

    class _Flood(Exception):
        def __init__(self) -> None:
            self.seconds = 7

    _Flood.__name__ = "FloodWaitError"

    class _FloodClient:
        async def get_input_entity(self, chat_id):
            raise _Flood()

    backend = TelethonSearchBackend(_FloodClient())
    with pytest.raises(FloodWaitError):
        await backend.search_messages(chat_id=42, query="needle")
