"""Unit tests for the group-rename backend + service additions (Task 2).

Covers:

  * Telethon adapter ``set_title`` (records ``EditTitleRequest`` + translates
    FLOOD_WAIT on both the resolver and the request path).
  * Service-layer ``rename_group``: happy path, replay (same key, no second
    backend call), new title → fresh op, WRITE-denied → AccessDenied,
    FLOOD_WAIT → needs_review, generic failure → failed.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from telegram_assistant.access import AccessDenied, AccessLevel, Authorizer
from telegram_assistant.config.models import AccessConfig, AccessRule
from telegram_assistant.entities import ResolvedEntity
from telegram_assistant.groups.service import (
    GroupRenameFailed,
    GroupRenameNeedsReview,
    GroupRenameRequest,
    GroupRenameResult,
    rename_group,
)
from telegram_assistant.groups.telethon_backend import TelethonGroupBackend
from telegram_assistant.persistence import (
    OperationStatus,
    OperationStore,
)
from telegram_assistant.worker.queue import FloodWaitError

# ---------------------------------------------------------------------------
# Telethon adapter
# ---------------------------------------------------------------------------


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
async def test_set_title_sends_edit_title_request() -> None:
    client = _RecordingClient()
    backend = TelethonGroupBackend(client)

    await backend.set_title(chat_id=-100123, title="New Title")

    assert client.peer_lookups == [-100123]
    assert len(client.calls) == 1
    req = client.calls[0]
    assert req.channel.chat_id == -100123
    assert req.title == "New Title"


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

    async def __call__(self, request: Any) -> Any:  # pragma: no cover
        raise AssertionError("request should not run after resolver FLOOD_WAIT")


@pytest.mark.asyncio
async def test_set_title_translates_flood_wait() -> None:
    backend = TelethonGroupBackend(_FloodingClient())
    with pytest.raises(FloodWaitError) as excinfo:
        await backend.set_title(chat_id=42, title="x")
    assert excinfo.value.seconds == 9.0


@pytest.mark.asyncio
async def test_set_title_translates_flood_wait_on_resolver() -> None:
    backend = TelethonGroupBackend(_ResolveFloodingClient())
    with pytest.raises(FloodWaitError) as excinfo:
        await backend.set_title(chat_id=42, title="x")
    assert excinfo.value.seconds == 4.0


# ---------------------------------------------------------------------------
# Service layer
# ---------------------------------------------------------------------------


class _FakeGroupBackend:
    """Minimal :class:`GroupBackend` for service-level rename tests.

    Only ``set_title`` does real work; every other protocol method raises if
    accidentally called.
    """

    def __init__(self, *, set_error: Exception | None = None) -> None:
        self._set_error = set_error
        self.set_title_calls: list[tuple[int, str]] = []

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
        raise NotImplementedError

    async def set_title(self, *, chat_id: int, title: str) -> None:
        if self._set_error is not None:
            raise self._set_error
        self.set_title_calls.append((chat_id, title))

    async def get_topics_layout(self, *, chat_id: int) -> bool:
        raise NotImplementedError

    async def set_default_permissions(
        self,
        *,
        chat_id: int,
        allow_create_topics: bool,
        allow_pin_messages: bool,
    ) -> None:
        raise NotImplementedError


class _FakeResolver:
    """Maps a chat ref to a :class:`ResolvedEntity` via a lookup table."""

    def __init__(self, mapping: dict[object, int]) -> None:
        self._mapping = mapping

    async def resolve(self, ref: object) -> ResolvedEntity:
        chat_id = self._mapping[ref]
        return ResolvedEntity(chat_id=chat_id, title=str(ref), kind="channel")


def _authorizer(rules: list[AccessRule], mapping: dict[object, int]) -> Authorizer:
    return Authorizer(
        AccessConfig(rules=rules),
        resolver=_FakeResolver(mapping),
        folder_backend=None,
    )


@pytest.fixture()
def store(tmp_path: Path) -> OperationStore:
    return OperationStore(tmp_path / "state.db")


@pytest.mark.asyncio
async def test_rename_group_happy_path(store: OperationStore) -> None:
    backend = _FakeGroupBackend()
    result, op = await rename_group(
        backend=backend,
        store=store,
        request=GroupRenameRequest(telegram_chat_id=-100, new_title="Renamed"),
    )

    assert op.status is OperationStatus.COMPLETED
    assert result.telegram_chat_id == -100
    assert result.new_title == "Renamed"
    assert result.status == "renamed"
    assert result.replayed is False
    assert backend.set_title_calls == [(-100, "Renamed")]


@pytest.mark.asyncio
async def test_rename_group_strips_title_whitespace(store: OperationStore) -> None:
    backend = _FakeGroupBackend()
    result, _ = await rename_group(
        backend=backend,
        store=store,
        request=GroupRenameRequest(telegram_chat_id=-100, new_title="  Spaced  "),
    )
    assert result.new_title == "Spaced"
    assert backend.set_title_calls == [(-100, "Spaced")]


@pytest.mark.asyncio
async def test_rename_group_rejects_blank_title(store: OperationStore) -> None:
    backend = _FakeGroupBackend()
    with pytest.raises(ValueError):
        await rename_group(
            backend=backend,
            store=store,
            request=GroupRenameRequest(telegram_chat_id=-100, new_title="   "),
        )
    assert backend.set_title_calls == []


@pytest.mark.asyncio
async def test_rename_group_replays_on_completed(store: OperationStore) -> None:
    backend = _FakeGroupBackend()
    request = GroupRenameRequest(telegram_chat_id=-100, new_title="Renamed")

    first, op1 = await rename_group(backend=backend, store=store, request=request)
    assert first.replayed is False
    assert backend.set_title_calls == [(-100, "Renamed")]

    backend2 = _FakeGroupBackend()
    second, op2 = await rename_group(backend=backend2, store=store, request=request)
    assert second.replayed is True
    assert second.new_title == "Renamed"
    assert op1.id == op2.id
    assert backend2.set_title_calls == []


@pytest.mark.asyncio
async def test_rename_group_new_title_is_fresh_op(store: OperationStore) -> None:
    """A different target title is a brand-new operation keyed under that title."""
    backend = _FakeGroupBackend()
    first, op1 = await rename_group(
        backend=backend,
        store=store,
        request=GroupRenameRequest(telegram_chat_id=-100, new_title="One"),
    )
    second, op2 = await rename_group(
        backend=backend,
        store=store,
        request=GroupRenameRequest(telegram_chat_id=-100, new_title="Two"),
    )
    assert op1.id != op2.id
    assert first.new_title == "One"
    assert second.new_title == "Two"
    assert backend.set_title_calls == [(-100, "One"), (-100, "Two")]


@pytest.mark.asyncio
async def test_rename_group_write_denied(store: OperationStore) -> None:
    backend = _FakeGroupBackend()
    authz = _authorizer([AccessRule(chat="@c", permission="read")], {"@c": 555})
    with pytest.raises(AccessDenied) as exc:
        await rename_group(
            backend=backend,
            store=store,
            request=GroupRenameRequest(telegram_chat_id=555, new_title="Nope"),
            authorizer=authz,
        )
    assert exc.value.required_level is AccessLevel.WRITE
    assert backend.set_title_calls == []


@pytest.mark.asyncio
async def test_rename_group_write_allowed(store: OperationStore) -> None:
    backend = _FakeGroupBackend()
    authz = _authorizer([AccessRule(chat="@c", permission="write")], {"@c": 555})
    result, op = await rename_group(
        backend=backend,
        store=store,
        request=GroupRenameRequest(telegram_chat_id=555, new_title="Yes"),
        authorizer=authz,
    )
    assert op.status is OperationStatus.COMPLETED
    assert result.new_title == "Yes"
    assert backend.set_title_calls == [(555, "Yes")]


@pytest.mark.asyncio
async def test_rename_group_flood_wait_marks_needs_review(store: OperationStore) -> None:
    backend = _FakeGroupBackend(set_error=FloodWaitError(seconds=12.0))
    request = GroupRenameRequest(telegram_chat_id=-100, new_title="Renamed")

    with pytest.raises(GroupRenameNeedsReview):
        await rename_group(backend=backend, store=store, request=request)

    # Replay raises NeedsReview, not FloodWaitError, and never hits the backend.
    backend2 = _FakeGroupBackend()
    with pytest.raises(GroupRenameNeedsReview):
        await rename_group(backend=backend2, store=store, request=request)
    assert backend2.set_title_calls == []


@pytest.mark.asyncio
async def test_rename_group_generic_error_marks_failed(store: OperationStore) -> None:
    backend = _FakeGroupBackend(set_error=RuntimeError("not an admin"))
    request = GroupRenameRequest(telegram_chat_id=-100, new_title="Renamed")

    with pytest.raises(RuntimeError, match="not an admin"):
        await rename_group(backend=backend, store=store, request=request)

    backend2 = _FakeGroupBackend()
    with pytest.raises(GroupRenameFailed, match="not an admin"):
        await rename_group(backend=backend2, store=store, request=request)
    assert backend2.set_title_calls == []


def test_group_rename_result_round_trip() -> None:
    result = GroupRenameResult(
        telegram_chat_id=-100,
        old_title="Old",
        new_title="New",
    )
    restored = GroupRenameResult.from_dict(result.to_dict())
    assert restored.telegram_chat_id == -100
    assert restored.old_title == "Old"
    assert restored.new_title == "New"
    assert restored.status == "renamed"
    assert restored.replayed is True
