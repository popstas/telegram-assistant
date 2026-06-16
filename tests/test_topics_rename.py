"""Unit tests for the topic-rename backend + service additions (Task 3).

Covers:

  * Telethon adapter ``rename_topic`` (records ``EditForumTopicRequest`` with
    the new title + translates FLOOD_WAIT on both the resolver and the request
    path).
  * Service-layer ``rename_topic``: happy path, replay (same key, no second
    backend call), new title → fresh op, WRITE-denied → AccessDenied,
    FLOOD_WAIT → needs_review, generic failure → failed, positive topic-id
    guard, blank-title guard.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from telegram_assistant.access import AccessDenied, AccessLevel, Authorizer
from telegram_assistant.config.models import AccessConfig, AccessRule
from telegram_assistant.entities import ResolvedEntity
from telegram_assistant.persistence import (
    OperationStatus,
    OperationStore,
)
from telegram_assistant.topics.service import (
    TopicRenameFailed,
    TopicRenameNeedsReview,
    TopicRenameRequest,
    TopicRenameResult,
    rename_topic,
)
from telegram_assistant.topics.telethon_backend import TelethonTopicBackend
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
async def test_rename_topic_sends_edit_forum_topic_request() -> None:
    client = _RecordingClient()
    backend = TelethonTopicBackend(client)

    await backend.rename_topic(chat_id=-100123, topic_id=42, title="New Title")

    assert client.peer_lookups == [-100123]
    assert len(client.calls) == 1
    req = client.calls[0]
    # EditForumTopicRequest takes ``channel`` (or ``peer``) — both are the peer.
    peer = getattr(req, "channel", None) or getattr(req, "peer", None)
    assert peer.chat_id == -100123
    assert req.topic_id == 42
    assert req.title == "New Title"
    # Renaming must not flip the ``closed`` flag.
    assert getattr(req, "closed", None) is None


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
async def test_rename_topic_translates_flood_wait() -> None:
    backend = TelethonTopicBackend(_FloodingClient())
    with pytest.raises(FloodWaitError) as excinfo:
        await backend.rename_topic(chat_id=42, topic_id=1, title="x")
    assert excinfo.value.seconds == 9.0


@pytest.mark.asyncio
async def test_rename_topic_translates_flood_wait_on_resolver() -> None:
    backend = TelethonTopicBackend(_ResolveFloodingClient())
    with pytest.raises(FloodWaitError) as excinfo:
        await backend.rename_topic(chat_id=42, topic_id=1, title="x")
    assert excinfo.value.seconds == 4.0


# ---------------------------------------------------------------------------
# Service layer
# ---------------------------------------------------------------------------


class _FakeTopicBackend:
    """Minimal :class:`TopicBackend` for service-level rename tests.

    Only ``rename_topic`` does real work; every other protocol method raises if
    accidentally called.
    """

    def __init__(self, *, rename_error: Exception | None = None) -> None:
        self._rename_error = rename_error
        self.rename_calls: list[tuple[int, int, str]] = []

    async def create_topic(self, *, chat_id: int, name: str) -> int:
        raise NotImplementedError

    async def send_message(
        self, *, chat_id: int, text: str, topic_id: int | None = None
    ) -> int:
        raise NotImplementedError

    async def close_topic(self, *, chat_id: int, topic_id: int) -> None:
        raise NotImplementedError

    async def rename_topic(self, *, chat_id: int, topic_id: int, title: str) -> None:
        if self._rename_error is not None:
            raise self._rename_error
        self.rename_calls.append((chat_id, topic_id, title))

    async def get_recent_messages(
        self, *, chat_id: int, limit: int, topic_id: int | None = None
    ) -> list[dict[str, Any]]:
        raise NotImplementedError

    async def delete_messages(
        self, *, chat_id: int, message_ids: Any
    ) -> None:
        raise NotImplementedError

    async def list_topics(self, *, chat_id: int) -> list[Any]:
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
async def test_rename_topic_happy_path(store: OperationStore) -> None:
    backend = _FakeTopicBackend()
    result, op = await rename_topic(
        backend=backend,
        store=store,
        request=TopicRenameRequest(
            telegram_chat_id=-100, telegram_topic_id=42, new_title="Renamed"
        ),
    )

    assert op.status is OperationStatus.COMPLETED
    assert result.telegram_chat_id == -100
    assert result.telegram_topic_id == 42
    assert result.new_title == "Renamed"
    assert result.status == "renamed"
    assert result.replayed is False
    assert backend.rename_calls == [(-100, 42, "Renamed")]


@pytest.mark.asyncio
async def test_rename_topic_strips_title_whitespace(store: OperationStore) -> None:
    backend = _FakeTopicBackend()
    result, _ = await rename_topic(
        backend=backend,
        store=store,
        request=TopicRenameRequest(
            telegram_chat_id=-100, telegram_topic_id=42, new_title="  Spaced  "
        ),
    )
    assert result.new_title == "Spaced"
    assert backend.rename_calls == [(-100, 42, "Spaced")]


@pytest.mark.asyncio
async def test_rename_topic_rejects_blank_title(store: OperationStore) -> None:
    backend = _FakeTopicBackend()
    with pytest.raises(ValueError):
        await rename_topic(
            backend=backend,
            store=store,
            request=TopicRenameRequest(
                telegram_chat_id=-100, telegram_topic_id=42, new_title="   "
            ),
        )
    assert backend.rename_calls == []


@pytest.mark.asyncio
async def test_rename_topic_rejects_nonpositive_topic_id(store: OperationStore) -> None:
    backend = _FakeTopicBackend()
    with pytest.raises(ValueError):
        await rename_topic(
            backend=backend,
            store=store,
            request=TopicRenameRequest(
                telegram_chat_id=-100, telegram_topic_id=0, new_title="X"
            ),
        )
    assert backend.rename_calls == []


@pytest.mark.asyncio
async def test_rename_topic_replays_on_completed(store: OperationStore) -> None:
    backend = _FakeTopicBackend()
    request = TopicRenameRequest(
        telegram_chat_id=-100, telegram_topic_id=42, new_title="Renamed"
    )

    first, op1 = await rename_topic(backend=backend, store=store, request=request)
    assert first.replayed is False
    assert backend.rename_calls == [(-100, 42, "Renamed")]

    backend2 = _FakeTopicBackend()
    second, op2 = await rename_topic(backend=backend2, store=store, request=request)
    assert second.replayed is True
    assert second.new_title == "Renamed"
    assert op1.id == op2.id
    assert backend2.rename_calls == []


@pytest.mark.asyncio
async def test_rename_topic_new_title_is_fresh_op(store: OperationStore) -> None:
    """A different target title is a brand-new operation keyed under that title."""
    backend = _FakeTopicBackend()
    first, op1 = await rename_topic(
        backend=backend,
        store=store,
        request=TopicRenameRequest(
            telegram_chat_id=-100, telegram_topic_id=42, new_title="One"
        ),
    )
    second, op2 = await rename_topic(
        backend=backend,
        store=store,
        request=TopicRenameRequest(
            telegram_chat_id=-100, telegram_topic_id=42, new_title="Two"
        ),
    )
    assert op1.id != op2.id
    assert first.new_title == "One"
    assert second.new_title == "Two"
    assert backend.rename_calls == [(-100, 42, "One"), (-100, 42, "Two")]


@pytest.mark.asyncio
async def test_rename_topic_distinct_topics_are_distinct_ops(
    store: OperationStore,
) -> None:
    """Same title on different topics keys distinct operations."""
    backend = _FakeTopicBackend()
    _, op1 = await rename_topic(
        backend=backend,
        store=store,
        request=TopicRenameRequest(
            telegram_chat_id=-100, telegram_topic_id=1, new_title="Same"
        ),
    )
    _, op2 = await rename_topic(
        backend=backend,
        store=store,
        request=TopicRenameRequest(
            telegram_chat_id=-100, telegram_topic_id=2, new_title="Same"
        ),
    )
    assert op1.id != op2.id
    assert backend.rename_calls == [(-100, 1, "Same"), (-100, 2, "Same")]


@pytest.mark.asyncio
async def test_rename_topic_write_denied(store: OperationStore) -> None:
    backend = _FakeTopicBackend()
    authz = _authorizer([AccessRule(chat="@c", permission="read")], {"@c": 555})
    with pytest.raises(AccessDenied) as exc:
        await rename_topic(
            backend=backend,
            store=store,
            request=TopicRenameRequest(
                telegram_chat_id=555, telegram_topic_id=42, new_title="Nope"
            ),
            authorizer=authz,
        )
    assert exc.value.required_level is AccessLevel.WRITE
    assert backend.rename_calls == []


@pytest.mark.asyncio
async def test_rename_topic_write_allowed(store: OperationStore) -> None:
    backend = _FakeTopicBackend()
    authz = _authorizer([AccessRule(chat="@c", permission="write")], {"@c": 555})
    result, op = await rename_topic(
        backend=backend,
        store=store,
        request=TopicRenameRequest(
            telegram_chat_id=555, telegram_topic_id=42, new_title="Yes"
        ),
        authorizer=authz,
    )
    assert op.status is OperationStatus.COMPLETED
    assert result.new_title == "Yes"
    assert backend.rename_calls == [(555, 42, "Yes")]


@pytest.mark.asyncio
async def test_rename_topic_flood_wait_marks_needs_review(store: OperationStore) -> None:
    backend = _FakeTopicBackend(rename_error=FloodWaitError(seconds=12.0))
    request = TopicRenameRequest(
        telegram_chat_id=-100, telegram_topic_id=42, new_title="Renamed"
    )

    with pytest.raises(TopicRenameNeedsReview):
        await rename_topic(backend=backend, store=store, request=request)

    # Replay raises NeedsReview, not FloodWaitError, and never hits the backend.
    backend2 = _FakeTopicBackend()
    with pytest.raises(TopicRenameNeedsReview):
        await rename_topic(backend=backend2, store=store, request=request)
    assert backend2.rename_calls == []


@pytest.mark.asyncio
async def test_rename_topic_generic_error_marks_failed(store: OperationStore) -> None:
    backend = _FakeTopicBackend(rename_error=RuntimeError("not an admin"))
    request = TopicRenameRequest(
        telegram_chat_id=-100, telegram_topic_id=42, new_title="Renamed"
    )

    with pytest.raises(RuntimeError, match="not an admin"):
        await rename_topic(backend=backend, store=store, request=request)

    backend2 = _FakeTopicBackend()
    with pytest.raises(TopicRenameFailed, match="not an admin"):
        await rename_topic(backend=backend2, store=store, request=request)
    assert backend2.rename_calls == []


def test_topic_rename_result_round_trip() -> None:
    result = TopicRenameResult(
        telegram_chat_id=-100,
        telegram_topic_id=42,
        old_title="Old",
        new_title="New",
    )
    restored = TopicRenameResult.from_dict(result.to_dict())
    assert restored.telegram_chat_id == -100
    assert restored.telegram_topic_id == 42
    assert restored.old_title == "Old"
    assert restored.new_title == "New"
    assert restored.status == "renamed"
    assert restored.replayed is True
