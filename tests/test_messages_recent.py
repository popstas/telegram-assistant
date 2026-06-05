"""Tests for Task 3 — get-recent-messages first-class READ op.

The domain op :func:`get_recent_messages` returns :class:`RecentMessage` rows
(default limit 5) and is gated behind READ-level authorization: a config that
grants no access raises :class:`AccessDenied` before any backend call.
"""

from __future__ import annotations

import pytest

from telegram_assistant.access import AccessDenied, AccessLevel, Authorizer
from telegram_assistant.config.models import AccessConfig, AccessRule
from telegram_assistant.entities import ResolvedEntity
from telegram_assistant.messages import (
    RecentMessage,
    get_recent_messages,
)


class FakeResolver:
    def __init__(self, mapping: dict[object, int]) -> None:
        self._mapping = mapping

    async def resolve(self, ref: object) -> ResolvedEntity:
        return ResolvedEntity(chat_id=self._mapping[ref], title=str(ref), kind="channel")


class FakeReadBackend:
    """In-memory MessageReadBackend recording the requested limit."""

    def __init__(self, messages: list[RecentMessage]) -> None:
        self._messages = messages
        self.calls: list[dict[str, int]] = []

    async def get_recent_messages(
        self, *, chat_id: int, limit: int = 5
    ) -> list[RecentMessage]:
        self.calls.append({"chat_id": chat_id, "limit": limit})
        return self._messages[:limit]


def _messages(n: int) -> list[RecentMessage]:
    return [
        RecentMessage(
            id=i,
            sender=f"user{i}",
            date=f"2026-06-06T00:00:0{i}+00:00",
            reply_to=None,
            text=f"msg {i}",
        )
        for i in range(1, n + 1)
    ]


@pytest.mark.asyncio
async def test_get_recent_default_limit_is_five() -> None:
    backend = FakeReadBackend(_messages(10))
    out = await get_recent_messages(backend=backend, chat_id=42)
    assert backend.calls == [{"chat_id": 42, "limit": 5}]
    assert len(out) == 5
    assert all(isinstance(m, RecentMessage) for m in out)
    assert out[0].id == 1


@pytest.mark.asyncio
async def test_get_recent_limit_override() -> None:
    backend = FakeReadBackend(_messages(10))
    out = await get_recent_messages(backend=backend, chat_id=42, limit=3)
    assert backend.calls == [{"chat_id": 42, "limit": 3}]
    assert len(out) == 3


@pytest.mark.asyncio
async def test_get_recent_rejects_nonpositive_limit() -> None:
    backend = FakeReadBackend(_messages(3))
    with pytest.raises(ValueError):
        await get_recent_messages(backend=backend, chat_id=42, limit=0)
    assert backend.calls == []


@pytest.mark.asyncio
async def test_get_recent_allowed_by_read_rule() -> None:
    backend = FakeReadBackend(_messages(2))
    config = AccessConfig(rules=[AccessRule(chat="@team", permission="read")])
    authorizer = Authorizer(config, resolver=FakeResolver({"@team": 42}))
    out = await get_recent_messages(
        backend=backend, chat_id=42, limit=2, authorizer=authorizer
    )
    assert len(out) == 2
    assert backend.calls == [{"chat_id": 42, "limit": 2}]


@pytest.mark.asyncio
async def test_get_recent_allowed_by_write_rule_write_implies_read() -> None:
    backend = FakeReadBackend(_messages(1))
    config = AccessConfig(rules=[AccessRule(chat="@team", permission="write")])
    authorizer = Authorizer(config, resolver=FakeResolver({"@team": 42}))
    out = await get_recent_messages(
        backend=backend, chat_id=42, authorizer=authorizer
    )
    assert len(out) == 1


@pytest.mark.asyncio
async def test_get_recent_denied_when_chat_not_permitted() -> None:
    backend = FakeReadBackend(_messages(3))
    config = AccessConfig(rules=[AccessRule(chat="@team", permission="read")])
    authorizer = Authorizer(config, resolver=FakeResolver({"@team": 42}))
    with pytest.raises(AccessDenied) as excinfo:
        await get_recent_messages(
            backend=backend, chat_id=999, authorizer=authorizer
        )
    assert excinfo.value.required_level is AccessLevel.READ
    # Denied before any backend call.
    assert backend.calls == []


@pytest.mark.asyncio
async def test_get_recent_no_authorizer_is_allow_all() -> None:
    backend = FakeReadBackend(_messages(3))
    out = await get_recent_messages(backend=backend, chat_id=12345, authorizer=None)
    assert len(out) == 3


class _FakeMsg:
    def __init__(self, *, id, message=None, sender_username=None, reply_to=None, date=None, media=None):
        self.id = id
        self.message = message
        self.sender = type("S", (), {"username": sender_username})()
        self.reply_to_msg_id = reply_to
        self.date = date
        self.media = media


class _FakeClient:
    def __init__(self, msgs):
        self._msgs = msgs

    async def get_input_entity(self, chat_id):
        return chat_id

    def iter_messages(self, channel, limit):
        msgs = self._msgs[:limit]

        async def _gen():
            for m in msgs:
                yield m

        return _gen()


@pytest.mark.asyncio
async def test_telethon_backend_maps_text_media_and_date() -> None:
    import datetime as dt

    from telegram_assistant.messages.telethon_backend import (
        TelethonMessageReadBackend,
    )

    when = dt.datetime(2026, 6, 6, 12, 0, 0, tzinfo=dt.UTC)
    media = type("MessageMediaPhoto", (), {})()
    client = _FakeClient(
        [
            _FakeMsg(id=2, message="", sender_username="bob", date=when, media=media),
            _FakeMsg(id=1, message="hi", sender_username="alice", reply_to=1, date=when),
        ]
    )
    backend = TelethonMessageReadBackend(client)
    out = await backend.get_recent_messages(chat_id=42, limit=5)
    assert out[0].id == 2
    assert out[0].text == "[photo]"  # media-only summary
    assert out[0].sender == "bob"
    assert out[0].date == "2026-06-06T12:00:00+00:00"
    assert out[1].text == "hi"
    assert out[1].reply_to == 1


@pytest.mark.asyncio
async def test_recent_message_to_dict() -> None:
    msg = RecentMessage(
        id=7,
        sender="alice",
        date="2026-06-06T00:00:00+00:00",
        reply_to=3,
        text="hello",
    )
    assert msg.to_dict() == {
        "id": 7,
        "sender": "alice",
        "date": "2026-06-06T00:00:00+00:00",
        "reply_to": 3,
        "text": "hello",
    }
