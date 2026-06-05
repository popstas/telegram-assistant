"""Per-service access enforcement tests (Task 2).

Each domain entry point accepts an optional ``authorizer``. These tests inject
a real :class:`Authorizer` built from an :class:`AccessConfig` and assert the
WRITE/READ matrix: permitted calls succeed, denied calls raise
:class:`AccessDenied` (and never reach the operation store), and mass-send marks
unpermitted chats ``skipped`` with reason ``access_denied``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from telegram_assistant.access import AccessDenied, Authorizer
from telegram_assistant.config import load_config_from_text
from telegram_assistant.config.models import AccessConfig, AccessRule
from telegram_assistant.entities import ResolvedEntity
from telegram_assistant.folders import FolderChat, FolderSnapshot, add_chat_to_folder
from telegram_assistant.groups import GroupCreateRequest, create_group
from telegram_assistant.members import (
    BulkMemberAddRequest,
    BulkMemberItem,
    BulkMemberRemoveItem,
    BulkMemberRemoveRequest,
    bulk_add_members,
    bulk_remove_members,
)
from telegram_assistant.messages import (
    MassSendRequest,
    SendMessageRequest,
    mass_send_message,
    send_message,
)
from telegram_assistant.persistence import OperationStore
from telegram_assistant.plugins import build_registry
from telegram_assistant.topics import (
    TopicCloseRequest,
    TopicCreateRequest,
    TopicSummary,
    close_topic,
    create_topic,
)
from telegram_assistant.worker import WorkerQueue

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeResolver:
    def __init__(self, mapping: dict[object, int]) -> None:
        self._mapping = mapping

    async def resolve(self, ref: object) -> ResolvedEntity:
        return ResolvedEntity(chat_id=self._mapping[ref], title=str(ref), kind="channel")


class FakeMessageBackend:
    def __init__(self, *, topics_per_chat: dict[int, list[TopicSummary]] | None = None) -> None:
        self._topics_per_chat = topics_per_chat or {}
        self.sent: list[dict[str, Any]] = []
        self._next_id = 1000

    async def send_message(
        self, *, chat_id: int, text: str, topic_id: int | None = None
    ) -> int:
        self._next_id += 1
        self.sent.append({"chat_id": chat_id, "text": text, "topic_id": topic_id})
        return self._next_id

    async def create_topic(self, *, chat_id: int, name: str) -> int:
        self._next_id += 1
        return self._next_id

    async def close_topic(self, *, chat_id: int, topic_id: int) -> None:
        return None

    async def list_topics(self, *, chat_id: int) -> list[TopicSummary]:
        return list(self._topics_per_chat.get(chat_id, []))


class FakeFolderBackend:
    def __init__(self, folders: list[FolderSnapshot]) -> None:
        self._folders = folders
        self.added: list[tuple[int, int]] = []

    async def list_folders(self) -> list[FolderSnapshot]:
        return [
            FolderSnapshot(
                folder_id=f.folder_id,
                folder_name=f.folder_name,
                chats=list(f.chats),
            )
            for f in self._folders
        ]

    async def resolve_chat(self, chat_ref: str | int) -> FolderChat:
        if isinstance(chat_ref, int):
            return FolderChat(chat_id=chat_ref, title=f"Chat {chat_ref}")
        raise LookupError(f"unknown chat ref {chat_ref!r}")

    async def add_chat_to_folder(self, folder_id: int, chat_id: int) -> None:
        self.added.append((folder_id, chat_id))


class FakeMemberBackend:
    def __init__(self) -> None:
        self.added: list[tuple[int, str]] = []

    async def add_member(self, *, chat_id: int, user: str) -> None:
        self.added.append((chat_id, user))

    async def promote_admin(self, *, chat_id: int, user: str) -> None:
        return None

    async def ban_member(self, *, chat_id: int, user: str) -> None:
        self.added.append((chat_id, f"-{user}"))

    async def unban_member(self, *, chat_id: int, user: str) -> None:
        return None


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def store(tmp_path: Path) -> OperationStore:
    return OperationStore(tmp_path / "state.db")


def _make_queue(store: OperationStore) -> WorkerQueue:
    async def fake_sleep(seconds: float) -> None:
        return None

    return WorkerQueue(
        store,
        max_parallel=1,
        flood_wait_safety_margin_seconds=1.0,
        sleep=fake_sleep,
    )


def _chat_write_authorizer(chat_id: int) -> Authorizer:
    return Authorizer(
        AccessConfig(rules=[AccessRule(chat=chat_id, permission="write")]),
        resolver=FakeResolver({chat_id: chat_id}),
    )


def _operation_count(store: OperationStore) -> int:
    """Count persisted operation rows (deny must happen before any begin)."""
    import sqlite3

    conn = sqlite3.connect(store._database_path)
    try:
        return conn.execute("SELECT COUNT(*) FROM operations").fetchone()[0]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# send_message
# ---------------------------------------------------------------------------


async def test_send_message_allowed_when_write_granted(store: OperationStore) -> None:
    backend = FakeMessageBackend()
    auth = _chat_write_authorizer(100)
    result, _ = await send_message(
        backend=backend,
        store=store,
        request=SendMessageRequest(telegram_chat_id=100, text="hi"),
        authorizer=auth,
    )
    assert result.telegram_message_id is not None
    assert backend.sent and backend.sent[0]["chat_id"] == 100


async def test_send_message_denied_without_grant(store: OperationStore) -> None:
    backend = FakeMessageBackend()
    auth = _chat_write_authorizer(100)
    with pytest.raises(AccessDenied):
        await send_message(
            backend=backend,
            store=store,
            request=SendMessageRequest(telegram_chat_id=200, text="hi"),
            authorizer=auth,
        )
    # No backend call and no operation row created on deny.
    assert backend.sent == []
    assert _operation_count(store) == 0


async def test_read_only_grant_denies_send(store: OperationStore) -> None:
    backend = FakeMessageBackend()
    auth = Authorizer(
        AccessConfig(rules=[AccessRule(all=True, permission="read")]),
    )
    with pytest.raises(AccessDenied):
        await send_message(
            backend=backend,
            store=store,
            request=SendMessageRequest(telegram_chat_id=100, text="hi"),
            authorizer=auth,
        )


# ---------------------------------------------------------------------------
# mass_send_message
# ---------------------------------------------------------------------------


async def test_mass_send_marks_unpermitted_chats_access_denied(
    store: OperationStore,
) -> None:
    topic = TopicSummary(topic_id=7, title="General")
    folder = FolderSnapshot(
        folder_id=1,
        folder_name="Clients",
        chats=[FolderChat(chat_id=10, title="A"), FolderChat(chat_id=11, title="B")],
    )
    backend = FakeMessageBackend(topics_per_chat={10: [topic], 11: [topic]})
    folder_backend = FakeFolderBackend([folder])
    # Only chat 10 may be written.
    auth = _chat_write_authorizer(10)

    result = await mass_send_message(
        message_backend=backend,
        topic_backend=backend,
        folder_backend=folder_backend,
        store=store,
        request=MassSendRequest(
            folder_name="Clients", topic_name="General", text="hello"
        ),
        authorizer=auth,
    )

    assert result.sent == 1
    assert result.skipped == 1
    sent_to = {s["chat_id"] for s in backend.sent}
    assert sent_to == {10}
    denied = [it for it in result.items if it.reason == "access_denied"]
    assert len(denied) == 1
    assert denied[0].telegram_chat_id == 11
    assert denied[0].status == "skipped"


# ---------------------------------------------------------------------------
# topics
# ---------------------------------------------------------------------------


async def test_create_topic_denied(store: OperationStore) -> None:
    backend = FakeMessageBackend()
    auth = _chat_write_authorizer(100)
    with pytest.raises(AccessDenied):
        await create_topic(
            backend=backend,
            store=store,
            request=TopicCreateRequest(telegram_chat_id=200, topic_name="T"),
            authorizer=auth,
        )
    assert _operation_count(store) == 0


async def test_close_topic_denied(store: OperationStore) -> None:
    backend = FakeMessageBackend()
    auth = _chat_write_authorizer(100)
    with pytest.raises(AccessDenied):
        await close_topic(
            backend=backend,
            store=store,
            request=TopicCloseRequest(telegram_chat_id=200, telegram_topic_id=5),
            authorizer=auth,
        )


# ---------------------------------------------------------------------------
# members
# ---------------------------------------------------------------------------


async def test_bulk_add_members_denied_does_no_work(store: OperationStore) -> None:
    backend = FakeMemberBackend()
    queue = _make_queue(store)
    auth = _chat_write_authorizer(100)
    with pytest.raises(AccessDenied):
        await bulk_add_members(
            backend=backend,
            store=store,
            queue=queue,
            request=BulkMemberAddRequest(
                telegram_chat_id=200, items=[BulkMemberItem(user="@x")]
            ),
            authorizer=auth,
        )
    assert backend.added == []
    assert _operation_count(store) == 0


async def test_bulk_add_members_allowed(store: OperationStore) -> None:
    backend = FakeMemberBackend()
    queue = _make_queue(store)
    auth = _chat_write_authorizer(100)
    result, _ = await bulk_add_members(
        backend=backend,
        store=store,
        queue=queue,
        request=BulkMemberAddRequest(
            telegram_chat_id=100, items=[BulkMemberItem(user="@x")]
        ),
        authorizer=auth,
    )
    assert result.added == 1


async def test_bulk_remove_members_denied(store: OperationStore) -> None:
    backend = FakeMemberBackend()
    queue = _make_queue(store)
    auth = _chat_write_authorizer(100)
    with pytest.raises(AccessDenied):
        await bulk_remove_members(
            backend=backend,
            store=store,
            queue=queue,
            request=BulkMemberRemoveRequest(
                telegram_chat_id=200, items=[BulkMemberRemoveItem(user="@x")]
            ),
            authorizer=auth,
        )


# ---------------------------------------------------------------------------
# folders
# ---------------------------------------------------------------------------


async def test_add_chat_to_folder_denied_on_resolved_chat() -> None:
    folder = FolderSnapshot(folder_id=1, folder_name="Clients", chats=[])
    backend = FakeFolderBackend([folder])
    auth = _chat_write_authorizer(10)
    with pytest.raises(AccessDenied):
        await add_chat_to_folder(
            backend,
            folder_name="Clients",
            chat_ref=999,
            authorizer=auth,
        )
    assert backend.added == []


async def test_add_chat_to_folder_allowed() -> None:
    folder = FolderSnapshot(folder_id=1, folder_name="Clients", chats=[])
    backend = FakeFolderBackend([folder])
    auth = _chat_write_authorizer(10)
    res = await add_chat_to_folder(
        backend,
        folder_name="Clients",
        chat_ref=10,
        authorizer=auth,
    )
    assert res["chat_id"] == 10
    assert backend.added == [(1, 10)]


# ---------------------------------------------------------------------------
# group create (destination-folder gate)
# ---------------------------------------------------------------------------


async def test_create_group_denied_when_folder_not_writable(
    minimal_config_yaml: str, store: OperationStore
) -> None:
    config = load_config_from_text(minimal_config_yaml)
    from tests.test_groups import FakeGroupBackend  # reuse the rich fake

    backend = FakeGroupBackend()
    folder_backend = FakeFolderBackend(
        [FolderSnapshot(folder_id=2, folder_name="Planfix clients", chats=[])]
    )
    # Grant write only to a different folder than the destination.
    auth = Authorizer(
        AccessConfig(rules=[AccessRule(folder="Other", permission="write")]),
        folder_backend=folder_backend,
    )
    with pytest.raises(AccessDenied):
        await create_group(
            backend=backend,
            folder_backend=folder_backend,
            store=store,
            config=config.telegram,
            plugins=build_registry(config),
            request=GroupCreateRequest(title="Acme", external_ref=1),
            authorizer=auth,
        )
    assert backend.created == []
    assert _operation_count(store) == 0


async def test_create_group_allowed_when_destination_folder_writable(
    minimal_config_yaml: str, store: OperationStore
) -> None:
    config = load_config_from_text(minimal_config_yaml)
    from tests.test_groups import FakeGroupBackend

    backend = FakeGroupBackend()
    folder_backend = FakeFolderBackend(
        [FolderSnapshot(folder_id=2, folder_name="Planfix clients", chats=[])]
    )
    auth = Authorizer(
        AccessConfig(rules=[AccessRule(folder="Planfix clients", permission="write")]),
        folder_backend=folder_backend,
    )
    result, _ = await create_group(
        backend=backend,
        folder_backend=folder_backend,
        store=store,
        config=config.telegram,
        plugins=build_registry(config),
        request=GroupCreateRequest(title="Acme", external_ref=1),
        authorizer=auth,
    )
    assert result.telegram_chat_id == backend._chat_id
