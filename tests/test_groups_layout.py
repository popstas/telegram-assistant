"""Unit tests for the topics-layout backend + service additions.

Covers:

  * Telethon adapter ``set_topics_layout`` / ``get_topics_layout`` (Task 2)
  * Service-layer ``set_topics_layout`` / ``get_topics_layout`` (Task 3),
    including FLOOD_WAIT → needs_review, generic failure → failed, fresh
    write, replay-on-completed, and the ``_layout_to_tabs`` /
    ``_tabs_to_layout`` helper round-trip.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from telegram_assistant.groups.service import (
    GroupLayoutSetFailed,
    GroupLayoutSetNeedsReview,
    LayoutSetRequest,
    _layout_to_tabs,
    _tabs_to_layout,
    get_topics_layout,
    set_topics_layout,
)
from telegram_assistant.groups.telethon_backend import (
    TelethonGroupBackend,
)
from telegram_assistant.persistence import (
    OperationStatus,
    OperationStore,
)
from telegram_assistant.worker.queue import FloodWaitError


class _Peer:
    def __init__(self, chat_id: int) -> None:
        self.chat_id = chat_id


class _RecordingClient:
    """Records ``(request_class, kwargs)`` for every ``__call__`` invocation."""

    def __init__(self, response: Any = None) -> None:
        self._response = response
        self.calls: list[Any] = []
        self.peer_lookups: list[int] = []

    async def get_input_entity(self, chat_id: int) -> _Peer:
        self.peer_lookups.append(chat_id)
        return _Peer(chat_id)

    async def __call__(self, request: Any) -> Any:
        self.calls.append(request)
        return self._response


@pytest.mark.asyncio
async def test_set_topics_layout_passes_tabs_true() -> None:
    client = _RecordingClient()
    backend = TelethonGroupBackend(client)

    await backend.set_topics_layout(chat_id=42, tabs=True)

    assert client.peer_lookups == [42]
    assert len(client.calls) == 1
    req = client.calls[0]
    assert req.channel.chat_id == 42
    assert req.enabled is True
    assert req.tabs is True


@pytest.mark.asyncio
async def test_set_topics_layout_passes_tabs_false() -> None:
    client = _RecordingClient()
    backend = TelethonGroupBackend(client)

    await backend.set_topics_layout(chat_id=7, tabs=False)

    assert client.calls[0].tabs is False


class _LegacyToggleClient:
    """Mimics a Telethon build whose ``ToggleForumRequest`` rejects ``tabs=``."""

    def __init__(self) -> None:
        self.calls: list[Any] = []
        self._first = True

    async def get_input_entity(self, chat_id: int) -> _Peer:
        return _Peer(chat_id)

    async def __call__(self, request: Any) -> Any:
        self.calls.append(request)
        # First call uses tabs=...; Telethon's ToggleForumRequest constructor
        # would raise TypeError on the legacy build. Simulate that by rejecting
        # any request that carries a ``tabs`` attribute set on it.
        if self._first and hasattr(request, "tabs"):
            self._first = False
            raise TypeError("unexpected keyword argument 'tabs'")
        return None


@pytest.mark.asyncio
async def test_set_topics_layout_falls_back_when_legacy_telethon(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Older Telethon rejects ``tabs=...``; tabs=True should still succeed via
    the legacy two-argument signature.
    """
    # Patch ToggleForumRequest at the import site so we control its signature.
    import telethon.tl.functions.channels as channels_mod

    class _LegacyToggle:
        def __init__(self, *, channel: Any, enabled: bool, tabs: bool | None = None) -> None:
            if tabs is not None:
                # Mirror the legacy signature by refusing the kwarg.
                raise TypeError("unexpected keyword argument 'tabs'")
            self.channel = channel
            self.enabled = enabled

    monkeypatch.setattr(channels_mod, "ToggleForumRequest", _LegacyToggle)

    client = _RecordingClient()
    backend = TelethonGroupBackend(client)
    await backend.set_topics_layout(chat_id=11, tabs=True)

    assert len(client.calls) == 1
    sent = client.calls[0]
    assert isinstance(sent, _LegacyToggle)
    assert sent.enabled is True
    assert sent.channel.chat_id == 11


@pytest.mark.asyncio
async def test_set_topics_layout_legacy_rejects_list_layout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the build is too old to accept ``tabs=`` we cannot honour a request
    to switch *to* list — Telegram defaults new forums to list, so there's no
    legacy fallback for the demote direction. Surface a clear error rather than
    silently leaving the chat in tabs view.
    """
    import telethon.tl.functions.channels as channels_mod

    class _LegacyToggle:
        def __init__(self, *, channel: Any, enabled: bool, tabs: bool | None = None) -> None:
            if tabs is not None:
                raise TypeError("unexpected keyword argument 'tabs'")
            self.channel = channel
            self.enabled = enabled

    monkeypatch.setattr(channels_mod, "ToggleForumRequest", _LegacyToggle)

    backend = TelethonGroupBackend(_RecordingClient())
    with pytest.raises(RuntimeError, match="does not support the `tabs` argument"):
        await backend.set_topics_layout(chat_id=11, tabs=False)


class _FloodingClient:
    """Always raises a fake Telethon ``FloodWaitError`` on ``__call__``."""

    def __init__(self) -> None:
        class FloodWaitError(Exception):
            def __init__(self, seconds: int) -> None:
                super().__init__(f"FLOOD_WAIT_{seconds}")
                self.seconds = seconds

        self._fw_cls = FloodWaitError

    async def get_input_entity(self, chat_id: int) -> _Peer:
        return _Peer(chat_id)

    async def __call__(self, request: Any) -> Any:
        raise self._fw_cls(seconds=9)


@pytest.mark.asyncio
async def test_set_topics_layout_translates_flood_wait() -> None:
    backend = TelethonGroupBackend(_FloodingClient())
    with pytest.raises(FloodWaitError) as excinfo:
        await backend.set_topics_layout(chat_id=42, tabs=True)
    assert excinfo.value.seconds == 9.0


class _ResolveFloodingClient:
    """Raises FLOOD_WAIT from ``get_input_entity`` before any request runs."""

    def __init__(self) -> None:
        class FloodWaitError(Exception):
            def __init__(self, seconds: int) -> None:
                super().__init__(f"FLOOD_WAIT_{seconds}")
                self.seconds = seconds

        self._fw_cls = FloodWaitError

    async def get_input_entity(self, chat_id: int) -> Any:
        raise self._fw_cls(seconds=4)

    async def __call__(self, request: Any) -> Any:
        raise AssertionError("request should not run after resolver FLOOD_WAIT")


@pytest.mark.asyncio
async def test_set_topics_layout_translates_flood_wait_on_resolver() -> None:
    backend = TelethonGroupBackend(_ResolveFloodingClient())
    with pytest.raises(FloodWaitError) as excinfo:
        await backend.set_topics_layout(chat_id=42, tabs=True)
    assert excinfo.value.seconds == 4.0


class _FullChannelClient:
    """Returns a ``GetFullChannelRequest`` response shaped like real Telegram:
    ``forum_tabs`` lives on the ``Channel`` constructor (returned in
    ``messages.ChatFull.chats``), not on ``ChannelFull``.
    """

    def __init__(self, *, forum_tabs: Any = "missing", chat_id: int = 1) -> None:
        self._forum_tabs = forum_tabs
        self._chat_id = chat_id
        self.calls: list[Any] = []

    async def get_input_entity(self, chat_id: int) -> _Peer:
        return _Peer(chat_id)

    async def __call__(self, request: Any) -> Any:
        self.calls.append(request)

        full_chat = type("FC", (), {"id": self._chat_id})()
        if self._forum_tabs == "missing":
            channel = type("CH", (), {"id": self._chat_id})()
        else:
            channel = type(
                "CH", (), {"id": self._chat_id, "forum_tabs": self._forum_tabs}
            )()
        return type("R", (), {"full_chat": full_chat, "chats": [channel]})()


@pytest.mark.asyncio
async def test_get_topics_layout_returns_true_when_tabs() -> None:
    backend = TelethonGroupBackend(_FullChannelClient(forum_tabs=True))
    assert await backend.get_topics_layout(chat_id=1) is True


@pytest.mark.asyncio
async def test_get_topics_layout_returns_false_when_list() -> None:
    backend = TelethonGroupBackend(_FullChannelClient(forum_tabs=False))
    assert await backend.get_topics_layout(chat_id=1) is False


@pytest.mark.asyncio
async def test_get_topics_layout_defaults_to_false_when_attribute_missing() -> None:
    backend = TelethonGroupBackend(_FullChannelClient(forum_tabs="missing"))
    assert await backend.get_topics_layout(chat_id=1) is False


@pytest.mark.asyncio
async def test_get_topics_layout_translates_flood_wait() -> None:
    backend = TelethonGroupBackend(_FloodingClient())
    with pytest.raises(FloodWaitError) as excinfo:
        await backend.get_topics_layout(chat_id=42)
    assert excinfo.value.seconds == 9.0


@pytest.mark.asyncio
async def test_get_topics_layout_translates_flood_wait_on_resolver() -> None:
    backend = TelethonGroupBackend(_ResolveFloodingClient())
    with pytest.raises(FloodWaitError) as excinfo:
        await backend.get_topics_layout(chat_id=42)
    assert excinfo.value.seconds == 4.0


class _ChatExistsClient:
    """Resolves the entity, then raises ``raise_on_call`` from the request
    (or returns successfully when ``raise_on_call`` is ``None``)."""

    def __init__(self, *, raise_on_call: BaseException | None = None) -> None:
        self._raise_on_call = raise_on_call

    async def get_input_entity(self, chat_id: int) -> _Peer:
        return _Peer(chat_id)

    async def __call__(self, request: Any) -> Any:
        if self._raise_on_call is not None:
            raise self._raise_on_call
        return type("R", (), {})()


@pytest.mark.asyncio
async def test_chat_exists_true_when_request_succeeds() -> None:
    backend = TelethonGroupBackend(_ChatExistsClient())
    assert await backend.chat_exists(chat_id=1) is True


@pytest.mark.asyncio
@pytest.mark.parametrize("exc_name", ["ChannelInvalidError", "ChannelPrivateError"])
async def test_chat_exists_false_on_known_gone_errors(exc_name: str) -> None:
    from telethon import errors as telethon_errors

    exc_cls = getattr(telethon_errors, exc_name)
    # These RPC errors require an underlying request object; build a minimal one.
    request = type("Req", (), {"CONSTRUCTOR_ID": 0, "SUBCLASS_OF_ID": 0})()
    backend = TelethonGroupBackend(_ChatExistsClient(raise_on_call=exc_cls(request)))
    assert await backend.chat_exists(chat_id=1) is False


@pytest.mark.asyncio
async def test_chat_exists_false_when_entity_unresolvable() -> None:
    # ``get_input_entity`` raises ValueError when it cannot resolve the peer.
    class _Unresolvable:
        async def get_input_entity(self, chat_id: int) -> Any:
            raise ValueError("Cannot find any entity corresponding to ...")

        async def __call__(self, request: Any) -> Any:  # pragma: no cover
            raise AssertionError("request should not run after resolve failure")

    backend = TelethonGroupBackend(_Unresolvable())
    assert await backend.chat_exists(chat_id=1) is False


@pytest.mark.asyncio
async def test_chat_exists_reraises_unexpected_error() -> None:
    backend = TelethonGroupBackend(
        _ChatExistsClient(raise_on_call=RuntimeError("server overloaded"))
    )
    with pytest.raises(RuntimeError, match="server overloaded"):
        await backend.chat_exists(chat_id=1)


@pytest.mark.asyncio
async def test_chat_exists_translates_flood_wait() -> None:
    backend = TelethonGroupBackend(_FloodingClient())
    with pytest.raises(FloodWaitError) as excinfo:
        await backend.chat_exists(chat_id=42)
    assert excinfo.value.seconds == 9.0


# ---------------------------------------------------------------------------
# Service-layer tests (Task 3)
# ---------------------------------------------------------------------------


def test_layout_to_tabs_round_trip() -> None:
    assert _layout_to_tabs("tabs") is True
    assert _layout_to_tabs("list") is False
    assert _tabs_to_layout(True) == "tabs"
    assert _tabs_to_layout(False) == "list"
    assert _tabs_to_layout(_layout_to_tabs("tabs")) == "tabs"
    assert _tabs_to_layout(_layout_to_tabs("list")) == "list"


def test_layout_to_tabs_rejects_unknown_value() -> None:
    with pytest.raises(ValueError):
        _layout_to_tabs("grid")


class _FakeGroupBackend:
    """Minimal :class:`GroupBackend` for service-level tests.

    Only the layout methods need to do real work — every other method on the
    protocol exists so the fake satisfies the Protocol shape but raises if
    accidentally called from one of these tests.
    """

    def __init__(
        self,
        *,
        forum_tabs: bool = False,
        set_error: Exception | None = None,
    ) -> None:
        self.forum_tabs = forum_tabs
        self._set_error = set_error
        self.set_calls: list[tuple[int, bool]] = []
        self.get_calls: list[int] = []

    async def create_supergroup(
        self, *, title: str, about: str | None, enable_topics: bool
    ) -> int:
        raise NotImplementedError

    async def add_member(self, *, chat_id: int, user: str) -> None:
        raise NotImplementedError

    async def promote_admin(self, *, chat_id: int, user: str) -> None:
        raise NotImplementedError

    async def create_invite_link(self, *, chat_id: int) -> str:
        raise NotImplementedError

    async def send_message(self, *, chat_id: int, text: str) -> int:
        raise NotImplementedError

    async def set_topics_layout(self, *, chat_id: int, tabs: bool) -> None:
        if self._set_error is not None:
            raise self._set_error
        self.set_calls.append((chat_id, tabs))
        self.forum_tabs = tabs

    async def get_topics_layout(self, *, chat_id: int) -> bool:
        self.get_calls.append(chat_id)
        return self.forum_tabs

    async def set_default_permissions(
        self,
        *,
        chat_id: int,
        allow_create_topics: bool,
        allow_pin_messages: bool,
    ) -> None:
        raise NotImplementedError


@pytest.fixture()
def store(tmp_path: Path) -> OperationStore:
    return OperationStore(tmp_path / "state.db")


@pytest.mark.asyncio
async def test_set_topics_layout_fresh_write_to_tabs(store: OperationStore) -> None:
    backend = _FakeGroupBackend(forum_tabs=False)
    result, op = await set_topics_layout(
        backend=backend,
        store=store,
        request=LayoutSetRequest(telegram_chat_id=-100, layout="tabs"),
    )

    assert op.status is OperationStatus.COMPLETED
    assert result.telegram_chat_id == -100
    assert result.layout == "tabs"
    assert result.replayed is False
    assert backend.set_calls == [(-100, True)]


@pytest.mark.asyncio
async def test_set_topics_layout_fresh_write_to_list(store: OperationStore) -> None:
    backend = _FakeGroupBackend(forum_tabs=True)
    result, op = await set_topics_layout(
        backend=backend,
        store=store,
        request=LayoutSetRequest(telegram_chat_id=-100, layout="list"),
    )

    assert op.status is OperationStatus.COMPLETED
    assert result.layout == "list"
    assert backend.set_calls == [(-100, False)]


@pytest.mark.asyncio
async def test_set_topics_layout_replays_on_completed(
    store: OperationStore,
) -> None:
    backend = _FakeGroupBackend()
    request = LayoutSetRequest(telegram_chat_id=-100, layout="tabs")

    first, op1 = await set_topics_layout(backend=backend, store=store, request=request)
    assert first.replayed is False
    assert backend.set_calls == [(-100, True)]

    backend2 = _FakeGroupBackend()
    second, op2 = await set_topics_layout(
        backend=backend2, store=store, request=request
    )
    assert second.replayed is True
    assert second.layout == "tabs"
    assert op1.id == op2.id
    assert backend2.set_calls == []


@pytest.mark.asyncio
async def test_set_topics_layout_distinct_keys_per_layout(
    store: OperationStore,
) -> None:
    """Switching from tabs to list creates a new operation row keyed by the
    new layout — the previous tabs operation stays completed.
    """
    backend = _FakeGroupBackend()
    first, op1 = await set_topics_layout(
        backend=backend,
        store=store,
        request=LayoutSetRequest(telegram_chat_id=-100, layout="tabs"),
    )
    second, op2 = await set_topics_layout(
        backend=backend,
        store=store,
        request=LayoutSetRequest(telegram_chat_id=-100, layout="list"),
    )
    assert op1.id != op2.id
    assert first.layout == "tabs"
    assert second.layout == "list"
    assert backend.set_calls == [(-100, True), (-100, False)]


@pytest.mark.asyncio
async def test_set_topics_layout_flood_wait_marks_needs_review(
    store: OperationStore,
) -> None:
    backend = _FakeGroupBackend(set_error=FloodWaitError(seconds=12.0))
    request = LayoutSetRequest(telegram_chat_id=-100, layout="tabs")

    with pytest.raises(GroupLayoutSetNeedsReview):
        await set_topics_layout(backend=backend, store=store, request=request)

    # Replay raises NeedsReview, not FloodWaitError, and never hits the
    # backend again.
    backend2 = _FakeGroupBackend()
    with pytest.raises(GroupLayoutSetNeedsReview):
        await set_topics_layout(backend=backend2, store=store, request=request)
    assert backend2.set_calls == []


@pytest.mark.asyncio
async def test_set_topics_layout_generic_error_marks_failed(
    store: OperationStore,
) -> None:
    backend = _FakeGroupBackend(set_error=RuntimeError("chat is not a forum"))
    request = LayoutSetRequest(telegram_chat_id=-100, layout="tabs")

    with pytest.raises(RuntimeError, match="chat is not a forum"):
        await set_topics_layout(backend=backend, store=store, request=request)

    # The idempotency row now holds the captured error message.
    backend2 = _FakeGroupBackend()
    with pytest.raises(GroupLayoutSetFailed, match="chat is not a forum"):
        await set_topics_layout(backend=backend2, store=store, request=request)
    assert backend2.set_calls == []


@pytest.mark.asyncio
async def test_set_topics_layout_rejects_unknown_layout(
    store: OperationStore,
) -> None:
    with pytest.raises(ValueError):
        await set_topics_layout(
            backend=_FakeGroupBackend(),
            store=store,
            request=LayoutSetRequest(
                telegram_chat_id=-100,
                layout="grid",  # type: ignore[arg-type]
            ),
        )


@pytest.mark.asyncio
async def test_get_topics_layout_service_returns_tabs() -> None:
    backend = _FakeGroupBackend(forum_tabs=True)
    layout = await get_topics_layout(backend=backend, telegram_chat_id=-100)
    assert layout == "tabs"
    assert backend.get_calls == [-100]


@pytest.mark.asyncio
async def test_get_topics_layout_service_returns_list() -> None:
    backend = _FakeGroupBackend(forum_tabs=False)
    layout = await get_topics_layout(backend=backend, telegram_chat_id=-100)
    assert layout == "list"
    assert backend.get_calls == [-100]
