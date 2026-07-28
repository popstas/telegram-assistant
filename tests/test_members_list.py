"""Tests for the members-list READ op.

``list_members`` is the read counterpart to the bulk add/remove ops: it is
gated behind READ-level authorization (denied before any backend call), takes
either a listing request or a single-``user`` membership check, and never
opens an operation row.
"""

from __future__ import annotations

import pytest

from telegram_assistant.access import AccessDenied, AccessLevel, Authorizer
from telegram_assistant.config.models import AccessConfig, AccessRule
from telegram_assistant.entities import ResolvedEntity
from telegram_assistant.members import (
    MemberListResult,
    Participant,
    list_members,
)


class FakeResolver:
    def __init__(self, mapping: dict[object, int]) -> None:
        self._mapping = mapping

    async def resolve(self, ref: object) -> ResolvedEntity:
        return ResolvedEntity(chat_id=self._mapping[ref], title=str(ref), kind="channel")


class FakeListBackend:
    """In-memory MemberListBackend recording every call."""

    def __init__(
        self,
        participants: list[Participant] | None = None,
        *,
        found: Participant | None = None,
        total: int | None = None,
    ) -> None:
        self._participants = participants or []
        self._found = found
        self._total = total
        self.list_calls: list[dict[str, object]] = []
        self.get_calls: list[dict[str, object]] = []

    async def list_participants(
        self, *, chat_id: int, limit: int, query: str | None, filter: str
    ) -> MemberListResult:
        self.list_calls.append(
            {"chat_id": chat_id, "limit": limit, "query": query, "filter": filter}
        )
        selected = self._participants[:limit]
        total = self._total if self._total is not None else len(self._participants)
        return MemberListResult(
            participants=tuple(selected),
            participants_count=total,
            truncated=len(selected) < total,
        )

    async def get_participant(self, *, chat_id: int, user: str) -> Participant | None:
        self.get_calls.append({"chat_id": chat_id, "user": user})
        return self._found


def _participant(uid: int, *, role: str = "member", is_bot: bool = False) -> Participant:
    return Participant(
        user_id=uid,
        username=f"user{uid}",
        first_name=f"First{uid}",
        last_name=None,
        is_bot=is_bot,
        role=role,
    )


@pytest.mark.asyncio
async def test_list_members_defaults_to_limit_200_and_filter_all() -> None:
    backend = FakeListBackend([_participant(i) for i in range(1, 4)])
    result = await list_members(backend=backend, chat_id=42)
    assert backend.list_calls == [
        {"chat_id": 42, "limit": 200, "query": None, "filter": "all"}
    ]
    assert len(result.participants) == 3
    assert result.participants_count == 3
    assert result.truncated is False
    assert result.is_member is None


@pytest.mark.asyncio
async def test_list_members_reports_truncation() -> None:
    backend = FakeListBackend([_participant(i) for i in range(1, 6)], total=99)
    result = await list_members(backend=backend, chat_id=42, limit=2)
    assert len(result.participants) == 2
    assert result.participants_count == 99
    assert result.truncated is True


@pytest.mark.asyncio
async def test_list_members_passes_query_and_filter() -> None:
    backend = FakeListBackend([_participant(1, is_bot=True)])
    await list_members(backend=backend, chat_id=42, query="press", filter="bots")
    assert backend.list_calls[-1]["query"] == "press"
    assert backend.list_calls[-1]["filter"] == "bots"


@pytest.mark.asyncio
async def test_list_members_rejects_nonpositive_limit() -> None:
    backend = FakeListBackend()
    with pytest.raises(ValueError):
        await list_members(backend=backend, chat_id=42, limit=0)
    assert backend.list_calls == []


@pytest.mark.asyncio
async def test_list_members_rejects_unknown_filter() -> None:
    backend = FakeListBackend()
    with pytest.raises(ValueError):
        await list_members(backend=backend, chat_id=42, filter="kicked")
    assert backend.list_calls == []


@pytest.mark.asyncio
async def test_list_members_rejects_user_with_query() -> None:
    backend = FakeListBackend()
    with pytest.raises(ValueError):
        await list_members(backend=backend, chat_id=42, user="@bot", query="bot")
    assert backend.get_calls == []
    assert backend.list_calls == []


@pytest.mark.asyncio
async def test_user_mode_reports_membership() -> None:
    backend = FakeListBackend(found=_participant(7, role="member", is_bot=True))
    result = await list_members(backend=backend, chat_id=42, user="@pressfinity_news_bot")
    assert backend.get_calls == [{"chat_id": 42, "user": "@pressfinity_news_bot"}]
    assert backend.list_calls == []
    assert result.is_member is True
    assert result.requested_user == "@pressfinity_news_bot"
    assert result.participants[0].user_id == 7
    assert result.to_dict()["is_member"] is True
    assert result.to_dict()["user"] == "@pressfinity_news_bot"


@pytest.mark.asyncio
async def test_user_mode_reports_absence() -> None:
    backend = FakeListBackend(found=None)
    result = await list_members(backend=backend, chat_id=42, user="@nope")
    assert result.is_member is False
    assert result.participants == ()
    assert result.participants_count is None


@pytest.mark.asyncio
async def test_user_mode_left_or_banned_is_not_a_member() -> None:
    # `channels.GetParticipant` answers for users who left or were banned too;
    # counting them as members would defeat the membership check.
    for role in ("left", "banned"):
        backend = FakeListBackend(found=_participant(7, role=role))
        result = await list_members(backend=backend, chat_id=42, user="@gone")
        assert result.is_member is False, role
        assert result.participants[0].role == role


@pytest.mark.asyncio
async def test_user_mode_ignores_filter() -> None:
    # The filter is about a listing; a named user is answered either way.
    backend = FakeListBackend(found=_participant(7, is_bot=True))
    result = await list_members(
        backend=backend, chat_id=42, user="@bot", filter="admins"
    )
    assert result.is_member is True
    assert backend.list_calls == []


@pytest.mark.asyncio
async def test_plain_list_payload_omits_user_keys() -> None:
    backend = FakeListBackend([_participant(1)])
    result = await list_members(backend=backend, chat_id=42)
    payload = result.to_dict()
    assert "user" not in payload
    assert "is_member" not in payload
    assert payload["count"] == 1
    assert payload["participants"][0]["username"] == "user1"


@pytest.mark.asyncio
async def test_read_rule_allows_listing() -> None:
    backend = FakeListBackend([_participant(1)])
    config = AccessConfig(rules=[AccessRule(chat="@team", permission="read")])
    authorizer = Authorizer(config, resolver=FakeResolver({"@team": 42}))
    result = await list_members(backend=backend, chat_id=42, authorizer=authorizer)
    assert len(result.participants) == 1


@pytest.mark.asyncio
async def test_write_only_rule_denies_listing() -> None:
    backend = FakeListBackend([_participant(1)])
    config = AccessConfig(rules=[AccessRule(chat="@team", permission="write")])
    authorizer = Authorizer(config, resolver=FakeResolver({"@team": 42}))
    with pytest.raises(AccessDenied) as excinfo:
        await list_members(backend=backend, chat_id=42, authorizer=authorizer)
    assert excinfo.value.required_level is AccessLevel.READ
    assert backend.list_calls == []


@pytest.mark.asyncio
async def test_user_mode_is_read_gated_before_any_rpc() -> None:
    backend = FakeListBackend(found=_participant(7))
    config = AccessConfig(rules=[AccessRule(chat="@team", permission="read")])
    authorizer = Authorizer(config, resolver=FakeResolver({"@team": 42}))
    with pytest.raises(AccessDenied):
        await list_members(backend=backend, chat_id=999, user="@bot", authorizer=authorizer)
    assert backend.get_calls == []
