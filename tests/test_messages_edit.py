"""Tests for Task 4 — the edit-message domain operation and adapter.

Exercises :func:`edit_message` with in-memory fakes (no Telethon traffic):
validation, the ``WRITE`` access gate, ``dry_run`` short-circuit, the
session-limit guard backed by :class:`SentMessageRegistry`, and the
:class:`TelethonEditBackend` adapter translating Telegram edit restrictions and
``FloodWaitError``.
"""

from __future__ import annotations

from typing import Any

import pytest

from telegram_assistant.access import AccessDenied, AccessLevel, Authorizer
from telegram_assistant.config.models import AccessConfig, AccessRule
from telegram_assistant.entities import ResolvedEntity
from telegram_assistant.messages import (
    MessageEditForbidden,
    MessageEditRejected,
    MessageEditRequest,
    SentMessageRegistry,
    edit_message,
)
from telegram_assistant.messages.telethon_backend import TelethonEditBackend
from telegram_assistant.worker.queue import FloodWaitError


class FakeResolver:
    """Maps a chat ref to a :class:`ResolvedEntity` via a lookup table."""

    def __init__(self, mapping: dict[object, int]) -> None:
        self._mapping = mapping

    async def resolve(self, ref: object) -> ResolvedEntity:
        chat_id = self._mapping[ref]
        return ResolvedEntity(chat_id=chat_id, title=str(ref), kind="channel")


class FakeEditBackend:
    """Records edit calls and echoes the edited message id."""

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def edit_message(
        self, *, chat_id: int, message_id: int, text: str
    ) -> int:
        self.calls.append(
            {"chat_id": chat_id, "message_id": message_id, "text": text}
        )
        return message_id


def _authorizer(rules: list[AccessRule], mapping: dict[object, int]) -> Authorizer:
    return Authorizer(
        AccessConfig(rules=rules), resolver=FakeResolver(mapping), folder_backend=None
    )


# ---------------------------------------------------------------------------
# Basic edit + validation
# ---------------------------------------------------------------------------


async def test_edit_success() -> None:
    backend = FakeEditBackend()
    req = MessageEditRequest(telegram_chat_id=-100, message_id=11, text="new")
    result = await edit_message(backend, request=req)
    assert result.text == "new"
    assert result.telegram_message_id == 11
    assert result.dry_run is False
    assert result.to_dict()["text"] == "new"
    assert backend.calls == [
        {"chat_id": -100, "message_id": 11, "text": "new"}
    ]


async def test_edit_rejects_non_positive_id() -> None:
    backend = FakeEditBackend()
    with pytest.raises(ValueError):
        await edit_message(
            backend,
            request=MessageEditRequest(telegram_chat_id=1, message_id=0, text="x"),
        )
    assert backend.calls == []


async def test_edit_rejects_empty_text() -> None:
    backend = FakeEditBackend()
    with pytest.raises(ValueError):
        await edit_message(
            backend,
            request=MessageEditRequest(telegram_chat_id=1, message_id=5, text="   "),
        )
    assert backend.calls == []


# ---------------------------------------------------------------------------
# Access gate (WRITE)
# ---------------------------------------------------------------------------


async def test_edit_denied_without_write_permission() -> None:
    backend = FakeEditBackend()
    authz = _authorizer([AccessRule(chat="@c", permission="read")], {"@c": 555})
    req = MessageEditRequest(telegram_chat_id=555, message_id=9, text="x")
    with pytest.raises(AccessDenied) as exc:
        await edit_message(backend, request=req, authorizer=authz)
    assert exc.value.required_level is AccessLevel.WRITE
    assert backend.calls == []


async def test_edit_allowed_with_write_permission() -> None:
    backend = FakeEditBackend()
    authz = _authorizer([AccessRule(chat="@c", permission="write")], {"@c": 555})
    req = MessageEditRequest(telegram_chat_id=555, message_id=9, text="x")
    result = await edit_message(backend, request=req, authorizer=authz)
    assert result.telegram_message_id == 9
    assert backend.calls[0]["chat_id"] == 555


# ---------------------------------------------------------------------------
# Dry run
# ---------------------------------------------------------------------------


async def test_dry_run_does_not_call_backend() -> None:
    backend = FakeEditBackend()
    authz = _authorizer([AccessRule(chat="@c", permission="write")], {"@c": 42})
    req = MessageEditRequest(
        telegram_chat_id=42, message_id=1, text="new", dry_run=True
    )
    result = await edit_message(backend, request=req, authorizer=authz)
    assert result.dry_run is True
    assert result.text == "new"
    assert backend.calls == []


# ---------------------------------------------------------------------------
# Session limit
# ---------------------------------------------------------------------------


async def test_session_limit_blocks_unknown_id() -> None:
    backend = FakeEditBackend()
    reg = SentMessageRegistry()
    req = MessageEditRequest(telegram_chat_id=-100, message_id=6, text="x")
    with pytest.raises(MessageEditForbidden) as exc:
        await edit_message(
            backend,
            request=req,
            sent_registry=reg,
            only_session_messages=True,
        )
    assert exc.value.message_id == 6
    assert backend.calls == []


async def test_session_limit_allows_recorded_id() -> None:
    backend = FakeEditBackend()
    reg = SentMessageRegistry()
    reg.record(-100, 6)
    req = MessageEditRequest(telegram_chat_id=-100, message_id=6, text="x")
    result = await edit_message(
        backend, request=req, sent_registry=reg, only_session_messages=True
    )
    assert result.telegram_message_id == 6
    assert backend.calls[0]["message_id"] == 6


async def test_session_limit_without_registry_blocks() -> None:
    backend = FakeEditBackend()
    req = MessageEditRequest(telegram_chat_id=-100, message_id=5, text="x")
    with pytest.raises(MessageEditForbidden):
        await edit_message(backend, request=req, only_session_messages=True)
    assert backend.calls == []


async def test_session_limit_off_allows_unknown_id() -> None:
    backend = FakeEditBackend()
    req = MessageEditRequest(telegram_chat_id=-100, message_id=999, text="x")
    result = await edit_message(backend, request=req)
    assert result.telegram_message_id == 999


async def test_session_limit_matches_canonical_chat_id() -> None:
    backend = FakeEditBackend()
    reg = SentMessageRegistry()
    reg.record(1234567890, 7)
    req = MessageEditRequest(
        telegram_chat_id=-1001234567890, message_id=7, text="x"
    )
    result = await edit_message(
        backend, request=req, sent_registry=reg, only_session_messages=True
    )
    assert result.telegram_message_id == 7


# ---------------------------------------------------------------------------
# Adapter: TelethonEditBackend
# ---------------------------------------------------------------------------


class _TelethonFloodWaitError(Exception):
    def __init__(self, seconds: int) -> None:
        super().__init__(f"flood wait {seconds}")
        self.seconds = seconds


_TelethonFloodWaitError.__name__ = "FloodWaitError"


class _EditClient:
    """Telethon client double recording ``edit_message`` calls."""

    def __init__(self, raise_exc: Exception | None = None) -> None:
        self.raise_exc = raise_exc
        self.calls: list[dict[str, Any]] = []

    async def get_input_entity(self, chat_id: int) -> int:
        return chat_id

    async def edit_message(self, entity: Any, message_id: int, text: str) -> Any:
        self.calls.append(
            {"entity": entity, "message_id": message_id, "text": text}
        )
        if self.raise_exc is not None:
            raise self.raise_exc
        return None


async def test_adapter_edits_and_returns_id() -> None:
    client = _EditClient()
    backend = TelethonEditBackend(client)
    edited = await backend.edit_message(chat_id=-100, message_id=8, text="hi")
    assert edited == 8
    assert client.calls == [{"entity": -100, "message_id": 8, "text": "hi"}]


async def test_adapter_translates_flood_wait() -> None:
    client = _EditClient(raise_exc=_TelethonFloodWaitError(30))
    backend = TelethonEditBackend(client)
    with pytest.raises(FloodWaitError):
        await backend.edit_message(chat_id=-100, message_id=8, text="hi")


@pytest.mark.parametrize(
    "exc_name,reason",
    [
        ("MessageAuthorRequiredError", "not_own_message"),
        ("MessageEditTimeExpiredError", "edit_window_expired"),
        ("MessageNotModifiedError", "not_modified"),
    ],
)
async def test_adapter_translates_edit_restrictions(
    exc_name: str, reason: str
) -> None:
    exc = type(exc_name, (Exception,), {})("nope")
    client = _EditClient(raise_exc=exc)
    backend = TelethonEditBackend(client)
    with pytest.raises(MessageEditRejected) as caught:
        await backend.edit_message(chat_id=-100, message_id=8, text="hi")
    assert caught.value.reason == reason
