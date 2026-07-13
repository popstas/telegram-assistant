"""Tests for Task 6 — message pin/unpin domain op + Telethon adapter."""

from __future__ import annotations

from typing import Any

import pytest

from telegram_assistant.access import AccessDenied, Authorizer
from telegram_assistant.config.models import AccessConfig, AccessRule
from telegram_assistant.messages import (
    PinMessageRequest,
    UnpinMessageRequest,
    pin_message,
    unpin_message,
)
from telegram_assistant.messages.telethon_backend import TelethonPinBackend
from telegram_assistant.worker.queue import FloodWaitError

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakePinBackend:
    def __init__(self, *, raise_on_call: Exception | None = None) -> None:
        self.pins: list[dict[str, Any]] = []
        self.unpins: list[dict[str, Any]] = []
        self._raise_on_call = raise_on_call

    async def pin_message(
        self, *, chat_id: int, message_id: int, silent: bool, pm_oneside: bool
    ) -> None:
        if self._raise_on_call is not None:
            raise self._raise_on_call
        self.pins.append(
            {
                "chat_id": chat_id,
                "message_id": message_id,
                "silent": silent,
                "pm_oneside": pm_oneside,
            }
        )

    async def unpin_message(
        self, *, chat_id: int, message_id: int | None
    ) -> None:
        if self._raise_on_call is not None:
            raise self._raise_on_call
        self.unpins.append({"chat_id": chat_id, "message_id": message_id})


# ---------------------------------------------------------------------------
# pin_message domain tests
# ---------------------------------------------------------------------------


async def test_pin_passes_flags_to_backend() -> None:
    backend = FakePinBackend()
    result = await pin_message(
        backend,
        request=PinMessageRequest(
            telegram_chat_id=-100,
            message_id=42,
            silent=True,
            pm_oneside=True,
            chat_name="Acme",
        ),
    )
    assert result.dry_run is False
    assert result.silent is True
    assert result.pm_oneside is True
    assert result.telegram_message_id == 42
    assert result.chat_name == "Acme"
    assert backend.pins == [
        {"chat_id": -100, "message_id": 42, "silent": True, "pm_oneside": True}
    ]


async def test_pin_dry_run_skips_backend() -> None:
    backend = FakePinBackend()
    result = await pin_message(
        backend,
        request=PinMessageRequest(
            telegram_chat_id=-100, message_id=42, dry_run=True
        ),
    )
    assert result.dry_run is True
    assert result.telegram_message_id == 42
    assert backend.pins == []


async def test_pin_rejects_non_positive_message_id() -> None:
    backend = FakePinBackend()
    with pytest.raises(ValueError):
        await pin_message(
            backend,
            request=PinMessageRequest(telegram_chat_id=-100, message_id=0),
        )
    assert backend.pins == []


async def test_pin_denied_before_backend_call() -> None:
    backend = FakePinBackend()
    authorizer = Authorizer(
        AccessConfig(rules=[AccessRule(all=True, permission="read")])
    )
    with pytest.raises(AccessDenied):
        await pin_message(
            backend,
            request=PinMessageRequest(telegram_chat_id=-100, message_id=1),
            authorizer=authorizer,
        )
    assert backend.pins == []


async def test_pin_dry_run_still_authorizes() -> None:
    backend = FakePinBackend()
    authorizer = Authorizer(
        AccessConfig(rules=[AccessRule(all=True, permission="read")])
    )
    with pytest.raises(AccessDenied):
        await pin_message(
            backend,
            request=PinMessageRequest(
                telegram_chat_id=-100, message_id=1, dry_run=True
            ),
            authorizer=authorizer,
        )
    assert backend.pins == []


async def test_pin_allowed_with_write_rule() -> None:
    backend = FakePinBackend()
    authorizer = Authorizer(
        AccessConfig(rules=[AccessRule(all=True, permission="write")])
    )
    result = await pin_message(
        backend,
        request=PinMessageRequest(telegram_chat_id=-100, message_id=5),
        authorizer=authorizer,
    )
    assert result.dry_run is False
    assert backend.pins == [
        {"chat_id": -100, "message_id": 5, "silent": False, "pm_oneside": False}
    ]


# ---------------------------------------------------------------------------
# unpin_message domain tests
# ---------------------------------------------------------------------------


async def test_unpin_one_passes_id_to_backend() -> None:
    backend = FakePinBackend()
    result = await unpin_message(
        backend,
        request=UnpinMessageRequest(telegram_chat_id=-100, message_id=42),
    )
    assert result.unpinned_all is False
    assert result.telegram_message_id == 42
    assert result.dry_run is False
    assert backend.unpins == [{"chat_id": -100, "message_id": 42}]


async def test_unpin_all_passes_none_to_backend() -> None:
    backend = FakePinBackend()
    result = await unpin_message(
        backend,
        request=UnpinMessageRequest(telegram_chat_id=-100, message_id=None),
    )
    assert result.unpinned_all is True
    assert result.telegram_message_id is None
    assert backend.unpins == [{"chat_id": -100, "message_id": None}]


async def test_unpin_dry_run_skips_backend() -> None:
    backend = FakePinBackend()
    result = await unpin_message(
        backend,
        request=UnpinMessageRequest(
            telegram_chat_id=-100, message_id=42, dry_run=True
        ),
    )
    assert result.dry_run is True
    assert result.unpinned_all is False
    assert backend.unpins == []


async def test_unpin_rejects_non_positive_message_id() -> None:
    backend = FakePinBackend()
    with pytest.raises(ValueError):
        await unpin_message(
            backend,
            request=UnpinMessageRequest(telegram_chat_id=-100, message_id=0),
        )
    assert backend.unpins == []


async def test_unpin_denied_before_backend_call() -> None:
    backend = FakePinBackend()
    authorizer = Authorizer(
        AccessConfig(rules=[AccessRule(all=True, permission="read")])
    )
    with pytest.raises(AccessDenied):
        await unpin_message(
            backend,
            request=UnpinMessageRequest(telegram_chat_id=-100, message_id=None),
            authorizer=authorizer,
        )
    assert backend.unpins == []


# ---------------------------------------------------------------------------
# Telethon adapter tests
# ---------------------------------------------------------------------------


class FakeTelethonClient:
    def __init__(self, *, raise_on_call: Exception | None = None) -> None:
        self.pin_calls: list[Any] = []
        self.unpin_calls: list[Any] = []
        self._raise_on_call = raise_on_call

    async def get_input_entity(self, chat_id: int) -> str:
        return f"peer:{chat_id}"

    async def pin_message(
        self, entity: Any, message_id: int, *, notify: bool, pm_oneside: bool
    ) -> None:
        if self._raise_on_call is not None:
            raise self._raise_on_call
        self.pin_calls.append(
            {
                "entity": entity,
                "message_id": message_id,
                "notify": notify,
                "pm_oneside": pm_oneside,
            }
        )

    async def unpin_message(self, entity: Any, message_id: int | None) -> None:
        if self._raise_on_call is not None:
            raise self._raise_on_call
        self.unpin_calls.append({"entity": entity, "message_id": message_id})


async def test_telethon_pin_passes_args() -> None:
    client = FakeTelethonClient()
    backend = TelethonPinBackend(client)
    await backend.pin_message(
        chat_id=-100, message_id=7, silent=True, pm_oneside=True
    )
    assert client.pin_calls == [
        {
            "entity": "peer:-100",
            "message_id": 7,
            "notify": False,
            "pm_oneside": True,
        }
    ]


async def test_telethon_pin_notify_when_not_silent() -> None:
    client = FakeTelethonClient()
    backend = TelethonPinBackend(client)
    await backend.pin_message(
        chat_id=-100, message_id=7, silent=False, pm_oneside=False
    )
    assert client.pin_calls[0]["notify"] is True


async def test_telethon_unpin_one_passes_id() -> None:
    client = FakeTelethonClient()
    backend = TelethonPinBackend(client)
    await backend.unpin_message(chat_id=-100, message_id=7)
    assert client.unpin_calls == [{"entity": "peer:-100", "message_id": 7}]


async def test_telethon_unpin_all_passes_none() -> None:
    client = FakeTelethonClient()
    backend = TelethonPinBackend(client)
    await backend.unpin_message(chat_id=-100, message_id=None)
    assert client.unpin_calls == [{"entity": "peer:-100", "message_id": None}]


def _flood_error() -> Exception:
    class _Flood(Exception):
        def __init__(self) -> None:
            self.seconds = 7

    _Flood.__name__ = "FloodWaitError"
    return _Flood()


async def test_telethon_pin_translates_flood_wait() -> None:
    client = FakeTelethonClient(raise_on_call=_flood_error())
    backend = TelethonPinBackend(client)
    with pytest.raises(FloodWaitError):
        await backend.pin_message(
            chat_id=-100, message_id=7, silent=False, pm_oneside=False
        )


async def test_telethon_unpin_translates_flood_wait() -> None:
    client = FakeTelethonClient(raise_on_call=_flood_error())
    backend = TelethonPinBackend(client)
    with pytest.raises(FloodWaitError):
        await backend.unpin_message(chat_id=-100, message_id=7)
