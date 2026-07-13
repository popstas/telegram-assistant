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
from telegram_assistant.messages import RecentMessage, search_messages


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
    ) -> list[RecentMessage]:
        self.calls.append(
            {
                "chat_id": chat_id,
                "query": query,
                "from_user": from_user,
                "limit": limit,
                "topic_id": topic_id,
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
async def test_search_minutes_none_returns_all() -> None:
    backend = FakeSearchBackend(_messages(3))
    out = await search_messages(backend=backend, chat_id=42, query="needle", minutes=None)
    assert len(out) == 3


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
    def __init__(self, msgs):
        self._msgs = msgs
        self.iter_kwargs: dict[str, Any] = {}

    async def get_input_entity(self, chat_id):
        return chat_id

    def iter_messages(self, entity, **kwargs):
        self.iter_kwargs = {"entity": entity, **kwargs}
        msgs = self._msgs[: kwargs.get("limit", 20)]

        async def _gen():
            for m in msgs:
                yield m

        return _gen()


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
    assert client.iter_kwargs == {
        "entity": 42,
        "search": "needle",
        "from_user": "@bob",
        "reply_to": 7,
        "limit": 5,
    }
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
