"""Tests for the read-only chat-inspect domain op."""

from __future__ import annotations

import pytest

from telegram_assistant.access import AccessDenied, Authorizer
from telegram_assistant.chats import ChatInfo, inspect_chat
from telegram_assistant.config.models import AccessConfig, AccessRule


class FakeBackend:
    """Records calls and returns a canned ChatInfo."""

    def __init__(self, info: ChatInfo | None = None) -> None:
        self.info = info or ChatInfo(chat_id=42, kind="supergroup", title="T")
        self.calls: list[dict[str, object]] = []

    async def inspect_chat(self, *, chat_id: int, raw: bool) -> ChatInfo:
        self.calls.append({"chat_id": chat_id, "raw": raw})
        return self.info


@pytest.mark.asyncio
async def test_inspect_chat_returns_backend_result() -> None:
    backend = FakeBackend()

    result = await inspect_chat(backend=backend, chat_id=42)

    assert result is backend.info
    assert backend.calls == [{"chat_id": 42, "raw": False}]


@pytest.mark.asyncio
async def test_inspect_chat_passes_raw_through() -> None:
    backend = FakeBackend()

    await inspect_chat(backend=backend, chat_id=42, raw=True)

    assert backend.calls == [{"chat_id": 42, "raw": True}]


@pytest.mark.asyncio
async def test_read_gate_denies_before_any_rpc() -> None:
    backend = FakeBackend()
    authorizer = Authorizer(AccessConfig(rules=[]))

    with pytest.raises(AccessDenied):
        await inspect_chat(backend=backend, chat_id=42, authorizer=authorizer)

    assert backend.calls == []


@pytest.mark.asyncio
async def test_read_gate_allows_granted_chat() -> None:
    backend = FakeBackend()
    authorizer = Authorizer(
        AccessConfig(rules=[AccessRule(all=True, permissions=["read"])])
    )

    result = await inspect_chat(backend=backend, chat_id=42, authorizer=authorizer)

    assert result.chat_id == 42
    assert backend.calls == [{"chat_id": 42, "raw": False}]


@pytest.mark.asyncio
async def test_write_only_grant_does_not_satisfy_read() -> None:
    backend = FakeBackend()
    authorizer = Authorizer(
        AccessConfig(rules=[AccessRule(all=True, permissions=["write"])])
    )

    with pytest.raises(AccessDenied):
        await inspect_chat(backend=backend, chat_id=42, authorizer=authorizer)

    assert backend.calls == []


def test_to_dict_omits_raw_when_absent() -> None:
    info = ChatInfo(chat_id=7, kind="user", title="Someone")

    payload = info.to_dict()

    assert payload["chat_id"] == 7
    assert payload["kind"] == "user"
    assert "raw" not in payload
    # Fields that do not apply to a user are present and null, so the shape
    # never depends on what was inspected.
    assert payload["admins_count"] is None
    assert payload["ttl_period"] is None


def test_to_dict_includes_raw_when_present() -> None:
    info = ChatInfo(
        chat_id=7, kind="supergroup", title="T", raw={"entity": {}, "full": {}}
    )

    payload = info.to_dict()

    assert payload["raw"] == {"entity": {}, "full": {}}
