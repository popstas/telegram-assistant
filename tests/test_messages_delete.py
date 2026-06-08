"""Tests for Task 6 — the delete-message domain operation.

Exercises :func:`delete_messages` with in-memory fakes (no Telethon traffic):
revoke default + ``--no-revoke``, the ``DELETE`` access gate, ``dry_run``
short-circuit, and the session-limit guard backed by
:class:`SentMessageRegistry`.
"""

from __future__ import annotations

import pytest

from telegram_assistant.access import AccessDenied, AccessLevel, Authorizer
from telegram_assistant.config.models import AccessConfig, AccessRule
from telegram_assistant.entities import ResolvedEntity
from telegram_assistant.messages import (
    DeleteMessagesRequest,
    MessageDeleteForbidden,
    SentMessageRegistry,
    delete_messages,
)


class FakeResolver:
    """Maps a chat ref to a :class:`ResolvedEntity` via a lookup table."""

    def __init__(self, mapping: dict[object, int]) -> None:
        self._mapping = mapping

    async def resolve(self, ref: object) -> ResolvedEntity:
        chat_id = self._mapping[ref]
        return ResolvedEntity(chat_id=chat_id, title=str(ref), kind="channel")


class FakeDeleteBackend:
    """Records delete calls and returns the requested-id count."""

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def delete_messages(
        self, *, chat_id: int, message_ids: tuple[int, ...], revoke: bool = True
    ) -> int:
        message_ids = tuple(message_ids)
        self.calls.append(
            {"chat_id": chat_id, "message_ids": message_ids, "revoke": revoke}
        )
        return len(message_ids)


# ---------------------------------------------------------------------------
# Basic delete + revoke handling
# ---------------------------------------------------------------------------


async def test_delete_revoke_default_true() -> None:
    backend = FakeDeleteBackend()
    req = DeleteMessagesRequest(telegram_chat_id=-100, message_ids=(11, 12))
    result = await delete_messages(backend, request=req)
    assert result.revoke is True
    assert result.deleted == 2
    assert result.dry_run is False
    assert result.message_ids == [11, 12]
    assert backend.calls == [
        {"chat_id": -100, "message_ids": (11, 12), "revoke": True}
    ]


async def test_delete_no_revoke() -> None:
    backend = FakeDeleteBackend()
    req = DeleteMessagesRequest(telegram_chat_id=-100, message_ids=(7,), revoke=False)
    result = await delete_messages(backend, request=req)
    assert result.revoke is False
    assert backend.calls[0]["revoke"] is False


async def test_delete_rejects_empty_ids() -> None:
    backend = FakeDeleteBackend()
    with pytest.raises(ValueError):
        await delete_messages(
            backend, request=DeleteMessagesRequest(telegram_chat_id=1, message_ids=())
        )
    assert backend.calls == []


async def test_delete_rejects_non_positive_ids() -> None:
    backend = FakeDeleteBackend()
    with pytest.raises(ValueError):
        await delete_messages(
            backend,
            request=DeleteMessagesRequest(telegram_chat_id=1, message_ids=(1, 0)),
        )
    assert backend.calls == []


# ---------------------------------------------------------------------------
# Access gate (DELETE)
# ---------------------------------------------------------------------------


def _authorizer(rules: list[AccessRule], mapping: dict[object, int]) -> Authorizer:
    return Authorizer(
        AccessConfig(rules=rules), resolver=FakeResolver(mapping), folder_backend=None
    )


async def test_delete_denied_without_delete_permission() -> None:
    # A chat granted only write must not be deletable.
    backend = FakeDeleteBackend()
    authz = _authorizer([AccessRule(chat="@c", permission="write")], {"@c": 555})
    req = DeleteMessagesRequest(telegram_chat_id=555, message_ids=(9,))
    with pytest.raises(AccessDenied) as exc:
        await delete_messages(backend, request=req, authorizer=authz)
    assert exc.value.required_level is AccessLevel.DELETE
    assert backend.calls == []


async def test_delete_allowed_with_delete_permission() -> None:
    backend = FakeDeleteBackend()
    authz = _authorizer([AccessRule(chat="@c", permission="delete")], {"@c": 555})
    req = DeleteMessagesRequest(telegram_chat_id=555, message_ids=(9,))
    result = await delete_messages(backend, request=req, authorizer=authz)
    assert result.deleted == 1
    assert backend.calls[0]["chat_id"] == 555


# ---------------------------------------------------------------------------
# Dry run
# ---------------------------------------------------------------------------


async def test_dry_run_does_not_call_backend() -> None:
    backend = FakeDeleteBackend()
    authz = _authorizer([AccessRule(chat="@c", permission="delete")], {"@c": 42})
    req = DeleteMessagesRequest(
        telegram_chat_id=42, message_ids=(1, 2), dry_run=True
    )
    result = await delete_messages(backend, request=req, authorizer=authz)
    assert result.dry_run is True
    assert result.deleted == 0
    assert result.message_ids == [1, 2]
    # Authorized but never deleted.
    assert backend.calls == []


# ---------------------------------------------------------------------------
# Session limit
# ---------------------------------------------------------------------------


async def test_session_limit_blocks_unknown_ids() -> None:
    backend = FakeDeleteBackend()
    reg = SentMessageRegistry()
    reg.record(-100, 5)
    req = DeleteMessagesRequest(telegram_chat_id=-100, message_ids=(5, 6))
    with pytest.raises(MessageDeleteForbidden) as exc:
        await delete_messages(
            backend,
            request=req,
            sent_registry=reg,
            only_session_messages=True,
        )
    # Only the unrecorded id is reported; nothing is deleted.
    assert exc.value.message_ids == [6]
    assert backend.calls == []


async def test_session_limit_allows_recorded_ids() -> None:
    backend = FakeDeleteBackend()
    reg = SentMessageRegistry()
    reg.record(-100, 5)
    reg.record(-100, 6)
    req = DeleteMessagesRequest(telegram_chat_id=-100, message_ids=(5, 6))
    result = await delete_messages(
        backend, request=req, sent_registry=reg, only_session_messages=True
    )
    assert result.deleted == 2
    assert backend.calls[0]["message_ids"] == (5, 6)


async def test_session_limit_without_registry_blocks_all() -> None:
    # Flag on but no registry → every id is treated as unrecorded.
    backend = FakeDeleteBackend()
    req = DeleteMessagesRequest(telegram_chat_id=-100, message_ids=(5,))
    with pytest.raises(MessageDeleteForbidden):
        await delete_messages(backend, request=req, only_session_messages=True)
    assert backend.calls == []


async def test_session_limit_off_allows_unknown_ids() -> None:
    # Default (only_session_messages=False) deletes regardless of the registry.
    backend = FakeDeleteBackend()
    req = DeleteMessagesRequest(telegram_chat_id=-100, message_ids=(999,))
    result = await delete_messages(backend, request=req)
    assert result.deleted == 1


async def test_session_limit_matches_canonical_chat_id() -> None:
    # Recorded under the bare form, deleted via the -100 marked form.
    backend = FakeDeleteBackend()
    reg = SentMessageRegistry()
    reg.record(1234567890, 7)
    req = DeleteMessagesRequest(
        telegram_chat_id=-1001234567890, message_ids=(7,)
    )
    result = await delete_messages(
        backend, request=req, sent_registry=reg, only_session_messages=True
    )
    assert result.deleted == 1
