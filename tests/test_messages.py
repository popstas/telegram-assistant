"""Tests for Task 13 — send message / service command (domain, HTTP, CLI)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from telegram_assistant.cli import main as cli_main
from telegram_assistant.config import load_config_from_text
from telegram_assistant.folders import (
    FolderChat,
    FolderSnapshot,
)
from telegram_assistant.http_api import create_app
from telegram_assistant.messages import (
    MassSendRequest,
    MessageSendFailed,
    MessageSendNeedsReview,
    SendMessageRequest,
    is_service_command,
    mass_send_message,
    redact_message_text,
    send_message,
)
from telegram_assistant.persistence import (
    OperationStatus,
    OperationStore,
)
from telegram_assistant.topics import TopicSummary
from telegram_assistant.worker.queue import FloodWaitError


class FakeMessageBackend:
    """In-memory MessageBackend recording send_message calls.

    The signature matches the TopicBackend.send_message contract so the same
    fake doubles as a topic backend when needed.
    """

    def __init__(
        self,
        *,
        topics_per_chat: dict[int, list[TopicSummary]] | None = None,
        fail_send: bool = False,
        next_id: int = 100,
    ) -> None:
        self._topics_per_chat = topics_per_chat or {}
        self._fail_send = fail_send
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
        if self._fail_send:
            raise RuntimeError("telegram error")
        files = tuple(files)
        if len(files) > 1:
            # Album — one id per attachment.
            ids = [self._next_id + i for i in range(len(files))]
            self._next_id += len(files)
            self.sent.append(
                {
                    "chat_id": chat_id,
                    "text": text,
                    "topic_id": topic_id,
                    "files": files,
                    "schedule_at": schedule_at,
                    "id": ids[0],
                    "ids": ids,
                }
            )
            return ids
        msg_id = self._next_id
        self._next_id += 1
        self.sent.append(
            {
                "chat_id": chat_id,
                "text": text,
                "topic_id": topic_id,
                "files": files,
                "schedule_at": schedule_at,
                "id": msg_id,
            }
        )
        return msg_id

    async def create_topic(self, *, chat_id: int, name: str) -> int:
        raise NotImplementedError

    async def close_topic(self, *, chat_id: int, topic_id: int) -> None:
        raise NotImplementedError

    async def list_topics(self, *, chat_id: int) -> list[TopicSummary]:
        return list(self._topics_per_chat.get(chat_id, []))


class FakeFolderBackend:
    def __init__(self, folders: list[FolderSnapshot]) -> None:
        self._folders = folders

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
        raise NotImplementedError

    async def add_chat_to_folder(self, folder_id: int, chat_id: int) -> None:
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture()
def store(tmp_path: Path) -> OperationStore:
    return OperationStore(tmp_path / "state.db")


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_is_service_command_detects_slash_prefix() -> None:
    assert is_service_command("/task 123")
    assert is_service_command("  /task 123")
    assert not is_service_command("hello")
    assert not is_service_command("")


def test_redact_message_text_redacts_args_only_for_commands() -> None:
    assert redact_message_text("/task 12345") == "/task [redacted]"
    assert redact_message_text("/start") == "/start"
    assert redact_message_text("hello world") == "hello world"


# ---------------------------------------------------------------------------
# Domain tests — targeted send
# ---------------------------------------------------------------------------


async def test_send_message_happy_path(store: OperationStore) -> None:
    backend = FakeMessageBackend()
    req = SendMessageRequest(
        telegram_chat_id=-100, text="hello", operation_id="op-1"
    )
    result, op = await send_message(backend=backend, store=store, request=req)
    assert op.status is OperationStatus.COMPLETED
    assert result.telegram_chat_id == -100
    assert result.telegram_message_id is not None
    assert result.is_service_command is False
    assert result.replayed is False
    assert len(backend.sent) == 1


async def test_send_message_with_topic_id(store: OperationStore) -> None:
    backend = FakeMessageBackend()
    req = SendMessageRequest(
        telegram_chat_id=-100,
        text="topic msg",
        telegram_topic_id=42,
        operation_id="op-topic",
    )
    result, _ = await send_message(backend=backend, store=store, request=req)
    assert result.telegram_topic_id == 42
    assert backend.sent[0]["topic_id"] == 42


async def test_send_message_replays_same_operation_id(
    store: OperationStore,
) -> None:
    backend = FakeMessageBackend()
    req = SendMessageRequest(
        telegram_chat_id=-100, text="hello", operation_id="op-dup"
    )
    first, op1 = await send_message(backend=backend, store=store, request=req)
    assert first.replayed is False

    backend2 = FakeMessageBackend()
    second, op2 = await send_message(backend=backend2, store=store, request=req)
    assert second.replayed is True
    assert op1.id == op2.id
    assert backend2.sent == []
    assert second.telegram_message_id == first.telegram_message_id


async def test_send_message_failure_marks_failed(store: OperationStore) -> None:
    backend = FakeMessageBackend(fail_send=True)
    req = SendMessageRequest(
        telegram_chat_id=-100, text="hi", operation_id="op-fail"
    )
    with pytest.raises(RuntimeError):
        await send_message(backend=backend, store=store, request=req)
    # Second attempt with the same key surfaces the saved failure rather than
    # silently re-sending.
    with pytest.raises(MessageSendFailed):
        await send_message(
            backend=FakeMessageBackend(), store=store, request=req
        )


async def test_send_message_flood_wait_marks_needs_review_not_failed(
    store: OperationStore,
) -> None:
    """FLOOD_WAIT on a single-shot send must be retryable. Marking the row
    ``failed`` would burn the idempotency key on a transient signal — every
    subsequent replay would surface ``MessageSendFailed`` forever.
    """

    class FloodingBackend:
        async def send_message(
            self, *, chat_id: int, text: str, topic_id: int | None = None
        ) -> int:
            raise FloodWaitError(5.0)

    req = SendMessageRequest(
        telegram_chat_id=-100, text="hi", operation_id="op-fw"
    )
    with pytest.raises(MessageSendNeedsReview):
        await send_message(backend=FloodingBackend(), store=store, request=req)

    # Replay must NOT raise MessageSendFailed — the row is needs_review, not
    # terminal-failed.
    with pytest.raises(MessageSendNeedsReview):
        await send_message(backend=FloodingBackend(), store=store, request=req)


async def test_send_message_service_command_marks_flag(
    store: OperationStore,
) -> None:
    backend = FakeMessageBackend()
    req = SendMessageRequest(
        telegram_chat_id=-100, text="/task 12345", operation_id="svc-1"
    )
    result, op = await send_message(backend=backend, store=store, request=req)
    assert result.is_service_command is True
    # Real text reaches Telegram (Planfix bot needs the id).
    assert backend.sent[0]["text"] == "/task 12345"
    # But the persisted request payload is redacted so logs/replays don't
    # leak the id outside the operation_id-bound audit trail.
    assert "[redacted]" in op.request_payload["text"]


async def test_send_message_rejects_empty_text(store: OperationStore) -> None:
    backend = FakeMessageBackend()
    with pytest.raises(ValueError):
        await send_message(
            backend=backend,
            store=store,
            request=SendMessageRequest(
                telegram_chat_id=-100, text="", operation_id="op"
            ),
        )


# ---------------------------------------------------------------------------
# Domain tests — media / scheduled send
# ---------------------------------------------------------------------------


async def test_send_message_text_only_still_omits_media_kwargs(
    store: OperationStore,
) -> None:
    """Backward compatibility: a text-only send must not pass files/schedule to
    the backend so backends predating attachments keep working."""
    backend = FakeMessageBackend()
    req = SendMessageRequest(
        telegram_chat_id=-100, text="plain", operation_id="text-only"
    )
    result, _ = await send_message(backend=backend, store=store, request=req)
    assert result.telegram_message_ids is None
    assert result.scheduled is False
    # No attachments recorded for a plain send.
    assert backend.sent[0]["files"] == ()
    assert backend.sent[0]["schedule_at"] is None


async def test_send_message_media_only(store: OperationStore) -> None:
    backend = FakeMessageBackend()
    req = SendMessageRequest(
        telegram_chat_id=-100,
        text="",
        files=("/data/a.png",),
        operation_id="media-only",
    )
    result, op = await send_message(backend=backend, store=store, request=req)
    assert op.status is OperationStatus.COMPLETED
    assert result.telegram_message_id is not None
    assert result.telegram_message_ids is None
    assert backend.sent[0]["files"] == ("/data/a.png",)
    # Only references are persisted, never contents.
    assert op.request_payload["files"] == ["/data/a.png"]


async def test_send_message_media_with_caption(store: OperationStore) -> None:
    backend = FakeMessageBackend()
    req = SendMessageRequest(
        telegram_chat_id=-100,
        text="look at this",
        files=("/data/a.png",),
        operation_id="media-caption",
    )
    result, _ = await send_message(backend=backend, store=store, request=req)
    assert result.telegram_message_id is not None
    assert backend.sent[0]["text"] == "look at this"
    assert backend.sent[0]["files"] == ("/data/a.png",)


async def test_send_message_album_returns_all_ids(store: OperationStore) -> None:
    backend = FakeMessageBackend()
    req = SendMessageRequest(
        telegram_chat_id=-100,
        text="album caption",
        files=("/data/a.png", "/data/b.png"),
        file_urls=("https://example.com/c.png",),
        operation_id="album-1",
    )
    result, _ = await send_message(backend=backend, store=store, request=req)
    assert result.telegram_message_ids is not None
    assert len(result.telegram_message_ids) == 3
    assert result.telegram_message_id == result.telegram_message_ids[0]
    # files + file_urls combined into one ordered attachment list.
    assert backend.sent[0]["files"] == (
        "/data/a.png",
        "/data/b.png",
        "https://example.com/c.png",
    )


async def test_send_message_rejects_empty_when_no_text_and_no_files(
    store: OperationStore,
) -> None:
    backend = FakeMessageBackend()
    with pytest.raises(ValueError):
        await send_message(
            backend=backend,
            store=store,
            request=SendMessageRequest(
                telegram_chat_id=-100, text="", operation_id="empty"
            ),
        )
    assert backend.sent == []


async def test_send_message_rejects_blank_attachment_ref(
    store: OperationStore,
) -> None:
    backend = FakeMessageBackend()
    with pytest.raises(ValueError):
        await send_message(
            backend=backend,
            store=store,
            request=SendMessageRequest(
                telegram_chat_id=-100,
                text="hi",
                files=("  ",),
                operation_id="blank",
            ),
        )
    assert backend.sent == []


async def test_send_message_schedule_at_sets_scheduled_flag(
    store: OperationStore,
) -> None:
    from datetime import UTC, datetime

    backend = FakeMessageBackend()
    when = datetime(2030, 1, 1, 12, 0, tzinfo=UTC)
    req = SendMessageRequest(
        telegram_chat_id=-100,
        text="later",
        schedule_at=when,
        operation_id="sched-1",
    )
    result, op = await send_message(backend=backend, store=store, request=req)
    assert result.scheduled is True
    assert backend.sent[0]["schedule_at"] == when
    assert op.request_payload["schedule_at"] == when.isoformat()


async def test_send_message_media_denied_before_backend_call(
    store: OperationStore,
) -> None:
    from telegram_assistant.access import AccessDenied, Authorizer
    from telegram_assistant.config.models import AccessConfig, AccessRule

    backend = FakeMessageBackend()
    authorizer = Authorizer(AccessConfig(rules=[AccessRule(all=True, permission="read")]))
    req = SendMessageRequest(
        telegram_chat_id=-100,
        text="",
        files=("/data/a.png",),
        operation_id="denied-media",
    )
    with pytest.raises(AccessDenied):
        await send_message(
            backend=backend, store=store, request=req, authorizer=authorizer
        )
    # Denied before any backend traffic.
    assert backend.sent == []


async def test_send_message_media_replays_same_operation_id(
    store: OperationStore,
) -> None:
    backend = FakeMessageBackend()
    req = SendMessageRequest(
        telegram_chat_id=-100,
        text="caption",
        files=("/data/a.png", "/data/b.png"),
        operation_id="media-replay",
    )
    first, _ = await send_message(backend=backend, store=store, request=req)
    assert first.replayed is False
    assert first.telegram_message_ids is not None

    backend2 = FakeMessageBackend()
    second, _ = await send_message(backend=backend2, store=store, request=req)
    assert second.replayed is True
    assert backend2.sent == []
    assert second.telegram_message_ids == first.telegram_message_ids
    assert second.telegram_message_id == first.telegram_message_id


# ---------------------------------------------------------------------------
# Domain tests — mass send
# ---------------------------------------------------------------------------


async def test_mass_send_targets_only_chats_with_topic(
    store: OperationStore,
) -> None:
    folder = FolderSnapshot(
        folder_id=2,
        folder_name="Planfix clients",
        chats=[
            FolderChat(chat_id=-100, title="Acme"),
            FolderChat(chat_id=-200, title="Beta"),
            FolderChat(chat_id=-300, title="Gamma"),
        ],
    )
    topics_per_chat = {
        -100: [TopicSummary(topic_id=11, title="Daily")],
        -200: [TopicSummary(topic_id=22, title="Other")],
        -300: [TopicSummary(topic_id=33, title="Daily")],
    }
    msg_backend = FakeMessageBackend(topics_per_chat=topics_per_chat)
    folder_backend = FakeFolderBackend([folder])

    req = MassSendRequest(
        folder_name="Planfix clients",
        topic_name="Daily",
        text="standup time",
        operation_id="mass-1",
    )
    result = await mass_send_message(
        message_backend=msg_backend,
        topic_backend=msg_backend,
        folder_backend=folder_backend,
        store=store,
        request=req,
    )
    assert result.sent == 2
    assert result.skipped == 1
    assert result.failed == 0

    by_chat = {it.telegram_chat_id: it for it in result.items}
    assert by_chat[-100].status == "sent"
    assert by_chat[-100].telegram_topic_id == 11
    assert by_chat[-100].telegram_message_id is not None
    assert by_chat[-300].status == "sent"
    assert by_chat[-200].status == "skipped"
    assert by_chat[-200].reason == "topic_not_found"

    # Sent twice — once per matching chat.
    assert len(msg_backend.sent) == 2


async def test_mass_send_flood_wait_on_list_topics_reports_failed(
    store: OperationStore,
) -> None:
    """A FLOOD_WAIT raised by ``list_topics`` must NOT be silently turned into
    ``topic_not_found``. The chat should be reported as ``failed`` with a
    distinct reason so the operator knows to retry, rather than concluding the
    topic was permanently missing.
    """

    class FloodingTopicBackend:
        async def list_topics(self, *, chat_id: int) -> list[TopicSummary]:
            raise FloodWaitError(7.0)

    folder = FolderSnapshot(
        folder_id=1,
        folder_name="F",
        chats=[FolderChat(chat_id=-100, title="Acme")],
    )
    folder_backend = FakeFolderBackend([folder])
    msg_backend = FakeMessageBackend()

    req = MassSendRequest(
        folder_name="F", topic_name="Daily", text="hi", operation_id="mfw"
    )
    result = await mass_send_message(
        message_backend=msg_backend,
        topic_backend=FloodingTopicBackend(),
        folder_backend=folder_backend,
        store=store,
        request=req,
    )
    assert result.failed == 1
    assert result.skipped == 0
    assert result.sent == 0
    item = result.items[0]
    assert item.status == "failed"
    assert item.reason == "list_topics_flood_wait"
    assert item.error is not None and "FLOOD_WAIT" in item.error


async def test_mass_send_skips_ambiguous_topic_name(
    store: OperationStore,
) -> None:
    folder = FolderSnapshot(
        folder_id=2,
        folder_name="F",
        chats=[FolderChat(chat_id=-100, title="Acme")],
    )
    topics_per_chat = {
        -100: [
            TopicSummary(topic_id=1, title="Dup"),
            TopicSummary(topic_id=2, title="Dup"),
        ]
    }
    msg_backend = FakeMessageBackend(topics_per_chat=topics_per_chat)
    folder_backend = FakeFolderBackend([folder])

    req = MassSendRequest(
        folder_name="F", topic_name="Dup", text="hi", operation_id="m"
    )
    result = await mass_send_message(
        message_backend=msg_backend,
        topic_backend=msg_backend,
        folder_backend=folder_backend,
        store=store,
        request=req,
    )
    assert result.sent == 0
    assert result.skipped == 1
    assert msg_backend.sent == []


async def test_mass_send_replays_per_chat_with_operation_id(
    store: OperationStore,
) -> None:
    folder = FolderSnapshot(
        folder_id=2,
        folder_name="F",
        chats=[
            FolderChat(chat_id=-100, title="A"),
            FolderChat(chat_id=-200, title="B"),
        ],
    )
    topics_per_chat = {
        -100: [TopicSummary(topic_id=11, title="T")],
        -200: [TopicSummary(topic_id=22, title="T")],
    }
    msg_backend = FakeMessageBackend(topics_per_chat=topics_per_chat)
    folder_backend = FakeFolderBackend([folder])

    req = MassSendRequest(
        folder_name="F", topic_name="T", text="ping", operation_id="run-1"
    )
    r1 = await mass_send_message(
        message_backend=msg_backend,
        topic_backend=msg_backend,
        folder_backend=folder_backend,
        store=store,
        request=req,
    )
    assert r1.sent == 2

    msg_backend2 = FakeMessageBackend(topics_per_chat=topics_per_chat)
    r2 = await mass_send_message(
        message_backend=msg_backend2,
        topic_backend=msg_backend2,
        folder_backend=folder_backend,
        store=store,
        request=req,
    )
    assert r2.sent == 0
    assert r2.existed == 2
    assert msg_backend2.sent == []


async def test_mass_send_service_command_flag(
    store: OperationStore,
) -> None:
    folder = FolderSnapshot(
        folder_id=2,
        folder_name="F",
        chats=[FolderChat(chat_id=-100, title="A")],
    )
    topics_per_chat = {-100: [TopicSummary(topic_id=11, title="T")]}
    msg_backend = FakeMessageBackend(topics_per_chat=topics_per_chat)
    folder_backend = FakeFolderBackend([folder])
    req = MassSendRequest(
        folder_name="F",
        topic_name="T",
        text="/task 9000",
        operation_id="svc-mass",
    )
    result = await mass_send_message(
        message_backend=msg_backend,
        topic_backend=msg_backend,
        folder_backend=folder_backend,
        store=store,
        request=req,
    )
    assert result.is_service_command is True
    assert result.sent == 1


# ---------------------------------------------------------------------------
# HTTP tests
# ---------------------------------------------------------------------------


def _http_client(
    minimal_config_yaml: str,
    *,
    message_backend: FakeMessageBackend,
    topic_backend: FakeMessageBackend | None = None,
    folder_backend: FakeFolderBackend | None = None,
    store: OperationStore | None = None,
) -> TestClient:
    config = load_config_from_text(minimal_config_yaml)
    if store is None:
        import tempfile

        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".db")
        tmp.close()
        store = OperationStore(Path(tmp.name))
    app = create_app(
        config,
        session_manager=None,
        message_backend_factory=lambda _request: message_backend,
        topic_backend_factory=(
            (lambda _request: topic_backend)
            if topic_backend is not None
            else None
        ),
        folder_backend_factory=(
            (lambda _request: folder_backend)
            if folder_backend is not None
            else None
        ),
        operation_store=store,
    )
    return TestClient(app)


def test_http_send_targeted_by_chat_id(minimal_config_yaml: str) -> None:
    backend = FakeMessageBackend()
    client = _http_client(minimal_config_yaml, message_backend=backend)
    resp = client.post(
        "/telegram/messages",
        json={
            "telegram_chat_id": -100,
            "text": "hi",
            "operation_id": "h-1",
        },
        headers={"Authorization": "Bearer secret_token"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["telegram_chat_id"] == -100
    assert body["mode"] == "targeted"
    assert body["operation_status"] == "completed"


def test_http_send_requires_auth(minimal_config_yaml: str) -> None:
    backend = FakeMessageBackend()
    client = _http_client(minimal_config_yaml, message_backend=backend)
    resp = client.post(
        "/telegram/messages",
        json={"telegram_chat_id": -100, "text": "hi"},
    )
    assert resp.status_code == 401


def test_http_send_mass_mode_across_folder(minimal_config_yaml: str) -> None:
    folder = FolderSnapshot(
        folder_id=2,
        folder_name="Planfix clients",
        chats=[
            FolderChat(chat_id=-100, title="Acme"),
            FolderChat(chat_id=-200, title="Beta"),
        ],
    )
    topics_per_chat = {
        -100: [TopicSummary(topic_id=11, title="Daily")],
        -200: [TopicSummary(topic_id=22, title="Other")],
    }
    backend = FakeMessageBackend(topics_per_chat=topics_per_chat)
    folder_backend = FakeFolderBackend([folder])
    client = _http_client(
        minimal_config_yaml,
        message_backend=backend,
        topic_backend=backend,
        folder_backend=folder_backend,
    )
    resp = client.post(
        "/telegram/messages",
        json={
            "folder_name": "Planfix clients",
            "topic_name": "Daily",
            "text": "ping",
            "operation_id": "mass-http",
        },
        headers={"Authorization": "Bearer secret_token"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["mode"] == "mass"
    assert body["sent"] == 1
    assert body["skipped"] == 1
    statuses = {item["telegram_chat_id"]: item["status"] for item in body["items"]}
    assert statuses[-100] == "sent"
    assert statuses[-200] == "skipped"


def test_http_send_service_command(minimal_config_yaml: str) -> None:
    backend = FakeMessageBackend()
    client = _http_client(minimal_config_yaml, message_backend=backend)
    resp = client.post(
        "/telegram/messages",
        json={
            "telegram_chat_id": -100,
            "text": "/task 12345",
            "operation_id": "svc-http",
        },
        headers={"Authorization": "Bearer secret_token"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["is_service_command"] is True
    # Real Telegram call carried the full command (Planfix bot parses the id).
    assert backend.sent[0]["text"] == "/task 12345"


def test_http_send_rejects_missing_target(minimal_config_yaml: str) -> None:
    backend = FakeMessageBackend()
    client = _http_client(minimal_config_yaml, message_backend=backend)
    resp = client.post(
        "/telegram/messages",
        json={"text": "hi"},
        headers={"Authorization": "Bearer secret_token"},
    )
    # Pydantic validation surfaces the model_validator error as 422.
    assert resp.status_code == 422


def test_http_send_resolves_chat_by_name(minimal_config_yaml: str) -> None:
    folder = FolderSnapshot(
        folder_id=2,
        folder_name="Planfix clients",
        chats=[FolderChat(chat_id=-100, title="Acme")],
    )
    backend = FakeMessageBackend()
    folder_backend = FakeFolderBackend([folder])
    client = _http_client(
        minimal_config_yaml,
        message_backend=backend,
        folder_backend=folder_backend,
    )
    resp = client.post(
        "/telegram/messages",
        json={
            "chat_name": "Acme",
            "folder_name": "Planfix clients",
            "text": "hello",
            "operation_id": "name-1",
        },
        headers={"Authorization": "Bearer secret_token"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["telegram_chat_id"] == -100
    assert backend.sent[0]["chat_id"] == -100


# ---------------------------------------------------------------------------
# CLI tests
# ---------------------------------------------------------------------------


def _write_config(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "config.yml"
    p.write_text(body)
    return p


def _patch_cli_message_backends(
    monkeypatch: pytest.MonkeyPatch,
    backend: FakeMessageBackend,
    folder_backend: FakeFolderBackend,
    store: OperationStore,
) -> None:
    class _FakeManager:
        async def disconnect(self) -> None:
            return None

    def _factory(config_path: Path | None) -> Any:
        from telegram_assistant.config import load_config

        config = load_config(config_path)

        async def _open() -> Any:
            return backend, folder_backend

        return config, _FakeManager(), store, _open

    monkeypatch.setattr(cli_main, "_build_message_backends", _factory)


def test_cli_messages_send_targeted_by_chat_id(
    minimal_config_yaml: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_file = _write_config(tmp_path, minimal_config_yaml)
    backend = FakeMessageBackend()
    folder_backend = FakeFolderBackend([])
    store = OperationStore(tmp_path / "state.db")
    _patch_cli_message_backends(monkeypatch, backend, folder_backend, store)

    runner = CliRunner()
    result = runner.invoke(
        cli_main.app,
        [
            "messages",
            "send",
            "--text",
            "hi",
            "--chat-id",
            "-100",
            "--operation-id",
            "cli-1",
            "--config",
            str(config_file),
        ],
    )
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload["telegram_chat_id"] == -100
    assert payload["mode"] == "targeted"
    assert backend.sent[0]["chat_id"] == -100


def test_cli_messages_send_mass_mode(
    minimal_config_yaml: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_file = _write_config(tmp_path, minimal_config_yaml)
    folder = FolderSnapshot(
        folder_id=2,
        folder_name="Planfix clients",
        chats=[
            FolderChat(chat_id=-100, title="A"),
            FolderChat(chat_id=-200, title="B"),
        ],
    )
    topics_per_chat = {
        -100: [TopicSummary(topic_id=11, title="Daily")],
        -200: [TopicSummary(topic_id=22, title="Other")],
    }
    backend = FakeMessageBackend(topics_per_chat=topics_per_chat)
    folder_backend = FakeFolderBackend([folder])
    store = OperationStore(tmp_path / "state.db")
    _patch_cli_message_backends(monkeypatch, backend, folder_backend, store)

    runner = CliRunner()
    result = runner.invoke(
        cli_main.app,
        [
            "messages",
            "send",
            "--text",
            "/task 99",
            "--folder-name",
            "Planfix clients",
            "--topic-name",
            "Daily",
            "--operation-id",
            "cli-mass",
            "--config",
            str(config_file),
        ],
    )
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload["mode"] == "mass"
    assert payload["sent"] == 1
    assert payload["skipped"] == 1
    assert payload["is_service_command"] is True


def test_cli_messages_send_targeted_with_topic_name_resolves(
    minimal_config_yaml: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_file = _write_config(tmp_path, minimal_config_yaml)
    topics_per_chat = {
        -100: [
            TopicSummary(topic_id=11, title="Alpha"),
            TopicSummary(topic_id=22, title="Beta"),
        ]
    }
    backend = FakeMessageBackend(topics_per_chat=topics_per_chat)
    folder_backend = FakeFolderBackend([])
    store = OperationStore(tmp_path / "state.db")
    _patch_cli_message_backends(monkeypatch, backend, folder_backend, store)

    runner = CliRunner()
    result = runner.invoke(
        cli_main.app,
        [
            "messages",
            "send",
            "--text",
            "hi topic",
            "--chat-id",
            "-100",
            "--topic-name",
            "Beta",
            "--operation-id",
            "topic-name",
            "--config",
            str(config_file),
        ],
    )
    assert result.exit_code == 0, result.stdout
    assert backend.sent[0]["topic_id"] == 22


def test_cli_messages_send_requires_chat_or_topic_for_mass(
    minimal_config_yaml: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_file = _write_config(tmp_path, minimal_config_yaml)
    backend = FakeMessageBackend()
    folder_backend = FakeFolderBackend([])
    store = OperationStore(tmp_path / "state.db")
    _patch_cli_message_backends(monkeypatch, backend, folder_backend, store)

    runner = CliRunner()
    result = runner.invoke(
        cli_main.app,
        [
            "messages",
            "send",
            "--text",
            "hi",
            "--config",
            str(config_file),
        ],
    )
    # No chat-id and no topic-name → can't decide between targeted and mass.
    assert result.exit_code == 2
