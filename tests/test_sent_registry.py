"""Tests for Task 5 — SentMessageRegistry + send-path recording."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from telegram_assistant.access.service import _canonical_chat_id
from telegram_assistant.messages import SendMessageRequest, SentMessageRegistry, send_message
from telegram_assistant.persistence import OperationStore


class FakeMessageBackend:
    """Minimal MessageBackend returning incrementing ids (album-aware)."""

    def __init__(self, *, next_id: int = 100) -> None:
        self._next_id = next_id
        self.sent: list[dict[str, Any]] = []

    async def send_message(
        self,
        *,
        chat_id: int,
        text: str,
        topic_id: int | None = None,
        files: tuple[str, ...] = (),
        schedule_at: Any = None,
    ) -> int | list[int]:
        files = tuple(files)
        if len(files) > 1:
            ids = [self._next_id + i for i in range(len(files))]
            self._next_id += len(files)
            self.sent.append({"chat_id": chat_id, "ids": ids})
            return ids
        msg_id = self._next_id
        self._next_id += 1
        self.sent.append({"chat_id": chat_id, "id": msg_id})
        return msg_id


@pytest.fixture()
def store(tmp_path: Path) -> OperationStore:
    return OperationStore(tmp_path / "state.db")


# ---------------------------------------------------------------------------
# Registry unit behaviour
# ---------------------------------------------------------------------------


def test_record_and_contains_round_trip() -> None:
    reg = SentMessageRegistry()
    assert reg.contains(1234567890, 55) is False
    reg.record(1234567890, 55)
    assert reg.contains(1234567890, 55) is True
    assert reg.contains(1234567890, 56) is False
    assert len(reg) == 1


def test_canonical_id_matches_authorizer() -> None:
    reg = SentMessageRegistry()
    # Recorded with the -100 marked form...
    reg.record(-1001234567890, 7)
    # ...must be found via the bare form, exactly like the access authorizer.
    assert _canonical_chat_id(-1001234567890) == _canonical_chat_id(1234567890)
    assert reg.contains(1234567890, 7) is True
    assert reg.contains(-1001234567890, 7) is True
    # And only one entry exists despite the two id forms.
    assert len(reg) == 1


def test_record_is_best_effort_and_never_raises() -> None:
    reg = SentMessageRegistry()
    # Non-coercible ids are silently ignored rather than blowing up a send.
    reg.record("not-an-int", 1)  # type: ignore[arg-type]
    reg.record(123, "nope")  # type: ignore[arg-type]
    assert len(reg) == 0
    assert reg.contains("not-an-int", 1) is False  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Send-path recording
# ---------------------------------------------------------------------------


async def test_send_message_records_sent_id(store: OperationStore) -> None:
    backend = FakeMessageBackend()
    reg = SentMessageRegistry()
    req = SendMessageRequest(
        telegram_chat_id=-1001234567890, text="hi", operation_id="op-1"
    )
    result, _ = await send_message(
        backend=backend, store=store, request=req, sent_registry=reg
    )
    assert result.telegram_message_id is not None
    assert reg.contains(-1001234567890, result.telegram_message_id) is True


async def test_send_message_records_all_album_ids(store: OperationStore) -> None:
    backend = FakeMessageBackend()
    reg = SentMessageRegistry()
    req = SendMessageRequest(
        telegram_chat_id=-100,
        text="album",
        files=("a.jpg", "b.jpg"),
        operation_id="op-album",
    )
    result, _ = await send_message(
        backend=backend, store=store, request=req, sent_registry=reg
    )
    assert result.telegram_message_ids is not None
    for message_id in result.telegram_message_ids:
        assert reg.contains(-100, message_id) is True
    assert len(reg) == len(result.telegram_message_ids)


async def test_send_message_without_registry_is_noop(store: OperationStore) -> None:
    backend = FakeMessageBackend()
    req = SendMessageRequest(telegram_chat_id=-100, text="hi", operation_id="op-x")
    # No registry passed — send still succeeds.
    result, _ = await send_message(backend=backend, store=store, request=req)
    assert result.telegram_message_id is not None


async def test_replay_does_not_record(store: OperationStore) -> None:
    backend = FakeMessageBackend()
    reg = SentMessageRegistry()
    req = SendMessageRequest(
        telegram_chat_id=-100, text="hi", operation_id="op-replay"
    )
    result, _ = await send_message(
        backend=backend, store=store, request=req, sent_registry=reg
    )
    first_id = result.telegram_message_id
    assert first_id is not None
    assert len(reg) == 1

    # Replay under the same operation_id with a *fresh* registry: a replay
    # belongs to the original sender, so nothing new is recorded.
    reg2 = SentMessageRegistry()
    replay, _ = await send_message(
        backend=backend, store=store, request=req, sent_registry=reg2
    )
    assert replay.replayed is True
    assert replay.telegram_message_id == first_id
    assert len(reg2) == 0
