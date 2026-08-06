"""Tests for the Telethon set-ttl adapter.

Fakes are stand-ins whose class *names* match Telethon's, because that is what
the peer dispatch keys on — the same convention as
``tests/test_members_list_backend.py``.
"""

from __future__ import annotations

import pytest

from telegram_assistant.chats.telethon_backend import TelethonChatTtlBackend
from telegram_assistant.worker.queue import FloodWaitError


class InputPeerChannel:
    def __init__(self, channel_id: int) -> None:
        self.channel_id = channel_id


class InputPeerChat:
    def __init__(self, chat_id: int) -> None:
        self.chat_id = chat_id


class InputPeerUser:
    def __init__(self, user_id: int) -> None:
        self.user_id = user_id


class _FullChat:
    def __init__(self, ttl_period) -> None:
        self.ttl_period = ttl_period


class FullChannelResult:
    def __init__(self, ttl_period) -> None:
        self.full_chat = _FullChat(ttl_period)


class FullUserResult:
    def __init__(self, ttl_period) -> None:
        self.full_user = _FullChat(ttl_period)


class TypeNotFoundError(Exception):
    """Name-matched by the adapter; Telethon raises this when a response
    carries a constructor newer than the installed layer."""


class FakeClient:
    def __init__(self, *, peer, full=None, full_error=None, set_error=None) -> None:
        self._peer = peer
        self._full = full
        self._full_error = full_error
        self._set_error = set_error
        self.requests: list[object] = []

    async def get_input_entity(self, ref):
        return self._peer

    async def __call__(self, request):
        self.requests.append(request)
        name = type(request).__name__
        if name in {"GetFullChannelRequest", "GetFullChatRequest", "GetFullUserRequest"}:
            if self._full_error is not None:
                raise self._full_error
            return self._full
        if name == "SetHistoryTTLRequest":
            if self._set_error is not None:
                raise self._set_error
            return object()
        raise AssertionError(f"unexpected request {name}")

    @property
    def request_names(self) -> list[str]:
        return [type(r).__name__ for r in self.requests]


def _flood_error() -> Exception:
    """Name-spoofed like Telethon's own, so ``translate_flood_wait`` matches it
    by class name alone — the same convention as
    ``tests/test_messages_pin.py::_flood_error``."""

    class _Flood(Exception):
        def __init__(self) -> None:
            self.seconds = 7

    _Flood.__name__ = "FloodWaitError"
    return _Flood()


# --- get_ttl ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_ttl_reads_a_supergroup() -> None:
    client = FakeClient(peer=InputPeerChannel(7), full=FullChannelResult(86400))
    backend = TelethonChatTtlBackend(client)

    assert await backend.get_ttl(chat_id=-1007) == 86400
    assert client.request_names == ["GetFullChannelRequest"]


@pytest.mark.asyncio
async def test_get_ttl_reads_a_basic_group() -> None:
    client = FakeClient(peer=InputPeerChat(55), full=FullChannelResult(2678400))
    backend = TelethonChatTtlBackend(client)

    assert await backend.get_ttl(chat_id=-55) == 2678400
    assert client.request_names == ["GetFullChatRequest"]


@pytest.mark.asyncio
async def test_get_ttl_reads_a_user() -> None:
    client = FakeClient(peer=InputPeerUser(9), full=FullUserResult(604800))
    backend = TelethonChatTtlBackend(client)

    assert await backend.get_ttl(chat_id=9) == 604800
    assert client.request_names == ["GetFullUserRequest"]


@pytest.mark.asyncio
async def test_get_ttl_returns_none_when_off() -> None:
    client = FakeClient(peer=InputPeerChannel(7), full=FullChannelResult(None))
    backend = TelethonChatTtlBackend(client)

    assert await backend.get_ttl(chat_id=-1007) is None


@pytest.mark.asyncio
async def test_unsupported_peer_raises_value_error() -> None:
    class InputPeerEmpty:
        pass

    client = FakeClient(peer=InputPeerEmpty())
    backend = TelethonChatTtlBackend(client)

    with pytest.raises(ValueError) as exc:
        await backend.get_ttl(chat_id=1)
    assert "1" in str(exc.value)


@pytest.mark.asyncio
async def test_get_ttl_forbidden_channel_raises_value_error_naming_chat() -> None:
    from telethon.errors import ChannelPrivateError

    exc = ChannelPrivateError(request=object())
    client = FakeClient(peer=InputPeerChannel(13), full_error=exc)
    backend = TelethonChatTtlBackend(client)

    with pytest.raises(ValueError, match="13") as excinfo:
        await backend.get_ttl(chat_id=13)
    assert excinfo.value.__cause__ is exc


@pytest.mark.asyncio
async def test_get_ttl_translates_flood_wait() -> None:
    """The single most-cited fact behind this feature is that this method's
    flood waits escalate into the hundreds of seconds — if the raw Telethon
    error ever leaked through untranslated, the pacer's ``except
    FloodWaitError`` in ``messages/pacing.py`` would never fire and a
    ``chats set-ttl`` call would exit 1 on the first wait instead of sitting
    through it. See ``tests/test_messages_pin.py::test_telethon_pin_translates_flood_wait``
    for the identical pattern."""
    client = FakeClient(peer=InputPeerChannel(7), full_error=_flood_error())
    backend = TelethonChatTtlBackend(client)

    with pytest.raises(FloodWaitError):
        await backend.get_ttl(chat_id=-1007)


@pytest.mark.asyncio
async def test_get_ttl_forbidden_basic_group_raises_value_error_naming_chat() -> None:
    from telethon.errors import ChatForbiddenError

    exc = ChatForbiddenError(request=object())
    client = FakeClient(peer=InputPeerChat(14), full_error=exc)
    backend = TelethonChatTtlBackend(client)

    with pytest.raises(ValueError, match="14") as excinfo:
        await backend.get_ttl(chat_id=14)
    assert excinfo.value.__cause__ is exc


# --- set_ttl ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_set_ttl_sends_the_request_with_the_period() -> None:
    client = FakeClient(peer=InputPeerChannel(7), full=FullChannelResult(None))
    backend = TelethonChatTtlBackend(client)

    await backend.set_ttl(chat_id=-1007, period=86400)

    assert client.request_names == ["SetHistoryTTLRequest"]
    assert client.requests[0].period == 86400


@pytest.mark.asyncio
async def test_set_ttl_zero_disables() -> None:
    client = FakeClient(peer=InputPeerChannel(7), full=FullChannelResult(None))
    backend = TelethonChatTtlBackend(client)

    await backend.set_ttl(chat_id=-1007, period=0)

    assert client.requests[0].period == 0


@pytest.mark.asyncio
async def test_set_ttl_translates_flood_wait() -> None:
    """Mirror of ``test_get_ttl_translates_flood_wait`` for the write side —
    the pacer wraps ``set_chat_ttl``'s backend call, so an untranslated
    ``FloodWaitError`` here would silently drop the feature's headline
    sit-through-the-wait behaviour."""
    client = FakeClient(
        peer=InputPeerChannel(7),
        full=FullChannelResult(None),
        set_error=_flood_error(),
    )
    backend = TelethonChatTtlBackend(client)

    with pytest.raises(FloodWaitError):
        await backend.set_ttl(chat_id=-1007, period=86400)


@pytest.mark.asyncio
async def test_unparseable_response_is_swallowed() -> None:
    """Proven live 2026-08-05: the write applied, only the response failed to
    parse. Raising here would report a successful change as a failure — the
    domain's read-back is what decides."""
    client = FakeClient(
        peer=InputPeerChannel(7),
        full=FullChannelResult(None),
        set_error=TypeNotFoundError("Could not find a matching Constructor ID"),
    )
    backend = TelethonChatTtlBackend(client)

    await backend.set_ttl(chat_id=-1007, period=0)  # must not raise


@pytest.mark.asyncio
async def test_other_errors_propagate() -> None:
    client = FakeClient(
        peer=InputPeerChannel(7),
        full=FullChannelResult(None),
        set_error=RuntimeError("CHAT_ADMIN_REQUIRED"),
    )
    backend = TelethonChatTtlBackend(client)

    with pytest.raises(RuntimeError):
        await backend.set_ttl(chat_id=-1007, period=0)
