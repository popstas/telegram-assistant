"""Tests for numeric member references reaching Telethon as ints.

Telethon's ``get_input_entity`` treats a digit *string* as a phone number, so a
bare user id like ``"1234556"`` must be passed as ``int`` to resolve by id.
:func:`coerce_user_ref` performs that conversion at the wire boundary; these
tests cover the helper directly and assert the group/member Telethon backends
forward a coerced value to ``get_input_entity``.
"""

from __future__ import annotations

from typing import Any

import pytest

from telegram_assistant.groups.telethon_backend import TelethonGroupBackend
from telegram_assistant.members.service import coerce_user_ref
from telegram_assistant.members.telethon_backend import TelethonMemberBackend


class _RecordingClient:
    """Telethon client double recording ``get_input_entity`` arguments."""

    def __init__(self) -> None:
        self.entity_args: list[Any] = []

    async def get_input_entity(self, ref: Any) -> Any:
        self.entity_args.append(ref)
        return ref

    async def __call__(self, request: Any) -> Any:  # RPC invocation
        return None


# ---------------------------------------------------------------------------
# coerce_user_ref unit cases
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("1234556", 1234556),
        (" 99 ", 99),
        ("-100123", -100123),
    ],
)
def test_coerce_user_ref_numeric_becomes_int(raw: str, expected: int) -> None:
    result = coerce_user_ref(raw)
    assert result == expected
    assert isinstance(result, int)


@pytest.mark.parametrize(
    "raw",
    [
        "@alice",
        "alice",
        "+15551234567",
        "https://t.me/x",
    ],
)
def test_coerce_user_ref_non_numeric_passes_through(raw: str) -> None:
    result = coerce_user_ref(raw)
    assert result == raw
    assert isinstance(result, str)


# ---------------------------------------------------------------------------
# groups/telethon_backend boundary
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_group_add_member_passes_numeric_user_as_int() -> None:
    client = _RecordingClient()
    backend = TelethonGroupBackend(client)

    await backend.add_member(chat_id=-100777, user="1234556")

    # chat_id first (int), then the coerced member ref (int, not "1234556").
    assert client.entity_args == [-100777, 1234556]
    assert isinstance(client.entity_args[1], int)


@pytest.mark.asyncio
async def test_group_add_member_keeps_username_as_str() -> None:
    client = _RecordingClient()
    backend = TelethonGroupBackend(client)

    await backend.add_member(chat_id=-100777, user="@alice")

    assert client.entity_args == [-100777, "@alice"]
    assert isinstance(client.entity_args[1], str)


@pytest.mark.asyncio
async def test_group_promote_admin_passes_numeric_user_as_int() -> None:
    client = _RecordingClient()
    backend = TelethonGroupBackend(client)

    await backend.promote_admin(chat_id=-100777, user="1234556")

    assert client.entity_args[1] == 1234556
    assert isinstance(client.entity_args[1], int)


# ---------------------------------------------------------------------------
# members/telethon_backend boundary
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_member_add_member_passes_numeric_user_as_int() -> None:
    client = _RecordingClient()
    backend = TelethonMemberBackend(client)

    await backend.add_member(chat_id=-100777, user="1234556")

    assert client.entity_args == [-100777, 1234556]
    assert isinstance(client.entity_args[1], int)


@pytest.mark.asyncio
async def test_member_add_member_keeps_username_as_str() -> None:
    client = _RecordingClient()
    backend = TelethonMemberBackend(client)

    await backend.add_member(chat_id=-100777, user="bob")

    assert client.entity_args == [-100777, "bob"]
    assert isinstance(client.entity_args[1], str)


@pytest.mark.asyncio
async def test_member_ban_member_passes_numeric_user_as_int() -> None:
    client = _RecordingClient()
    backend = TelethonMemberBackend(client)

    await backend.ban_member(chat_id=-100777, user="1234556")

    assert client.entity_args[1] == 1234556
    assert isinstance(client.entity_args[1], int)
