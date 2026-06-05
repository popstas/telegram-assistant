"""Tests for Task 8 — single topic creation (domain, HTTP, CLI)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from telegram_assistant.cli import main as cli_main
from telegram_assistant.config import load_config_from_text
from telegram_assistant.config.models import PlanfixPluginConfig
from telegram_assistant.folders import (
    FolderChat,
    FolderSnapshot,
)
from telegram_assistant.http_api import create_app
from telegram_assistant.persistence import (
    OperationStatus,
    OperationStore,
)
from telegram_assistant.plugins import PluginRegistry
from telegram_assistant.plugins.planfix import PlanfixPlugin
from telegram_assistant.topics import (
    TopicCreateFailed,
    TopicCreateNeedsReview,
    TopicCreateRequest,
    create_topic,
)
from telegram_assistant.worker.queue import FloodWaitError


def _planfix_plugins() -> PluginRegistry:
    """Registry with the Planfix plugin enabled, for /task-behavior tests."""
    return PluginRegistry([PlanfixPlugin(PlanfixPluginConfig(enabled=True))])

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeTopicBackend:
    """In-memory :class:`TopicBackend` recording every call."""

    def __init__(
        self,
        *,
        topic_id: int = 555,
        message_id: int = 12345,
        fail_create: bool = False,
        fail_send: bool = False,
    ) -> None:
        self._topic_id = topic_id
        self._message_id = message_id
        self._fail_create = fail_create
        self._fail_send = fail_send
        self.created: list[tuple[int, str]] = []
        self.messages: list[tuple[int, str, int | None]] = []

    async def create_topic(self, *, chat_id: int, name: str) -> int:
        if self._fail_create:
            raise RuntimeError("server overloaded")
        self.created.append((chat_id, name))
        return self._topic_id

    async def send_message(
        self, *, chat_id: int, text: str, topic_id: int | None = None
    ) -> int:
        if self._fail_send:
            raise RuntimeError("rate limited")
        self.messages.append((chat_id, text, topic_id))
        return self._message_id


class FakeFolderBackend:
    """Minimal folder backend used for `--chat-name` resolution tests."""

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
# Domain tests
# ---------------------------------------------------------------------------


@pytest.fixture()
def store(tmp_path: Path) -> OperationStore:
    return OperationStore(tmp_path / "state.db")


async def test_create_topic_task_id_branch_sends_task_command(
    store: OperationStore,
) -> None:
    backend = FakeTopicBackend()
    request = TopicCreateRequest(
        telegram_chat_id=-100,
        topic_name="Client request",
        external_ref=42,
    )

    result, op = await create_topic(
        backend=backend, store=store, request=request, plugins=_planfix_plugins()
    )

    assert op.status is OperationStatus.COMPLETED
    assert result.telegram_topic_id == 555
    assert result.first_message_kind == "task"
    assert result.first_message_text == "/task 42"
    assert backend.messages == [(-100, "/task 42", 555)]
    assert result.replayed is False


async def test_create_topic_message_branch_sends_explicit_message(
    store: OperationStore,
) -> None:
    backend = FakeTopicBackend()
    request = TopicCreateRequest(
        telegram_chat_id=-100,
        topic_name="Greeting",
        message="Welcome!",
    )

    result, _ = await create_topic(backend=backend, store=store, request=request)

    assert result.first_message_kind == "message"
    assert result.first_message_text == "Welcome!"
    assert backend.messages == [(-100, "Welcome!", 555)]


async def test_create_topic_default_branch_duplicates_topic_name(
    store: OperationStore,
) -> None:
    backend = FakeTopicBackend()
    request = TopicCreateRequest(
        telegram_chat_id=-100,
        topic_name="Just a topic",
    )

    result, _ = await create_topic(backend=backend, store=store, request=request)

    assert result.first_message_kind == "topic_name"
    assert result.first_message_text == "Just a topic"
    assert backend.messages == [(-100, "Just a topic", 555)]


async def test_create_topic_idempotent_by_planfix_task_id(
    store: OperationStore,
) -> None:
    backend1 = FakeTopicBackend(topic_id=100)
    request = TopicCreateRequest(
        telegram_chat_id=-100,
        topic_name="Initial",
        external_ref=7,
    )
    first, op1 = await create_topic(
        backend=backend1, store=store, request=request
    )
    assert first.replayed is False

    # Re-call: different chat name in payload would matter, but key is task id.
    backend2 = FakeTopicBackend(topic_id=999)
    second, op2 = await create_topic(
        backend=backend2,
        store=store,
        request=TopicCreateRequest(
            telegram_chat_id=-100,
            topic_name="Different name",
            external_ref=7,
        ),
    )
    assert second.replayed is True
    assert second.telegram_topic_id == 100
    assert second.topic_name == "Initial"
    assert op1.id == op2.id
    assert backend2.created == []


async def test_create_topic_idempotent_by_chat_and_name(
    store: OperationStore,
) -> None:
    backend1 = FakeTopicBackend(topic_id=11)
    request = TopicCreateRequest(
        telegram_chat_id=-100,
        topic_name="Repeatable",
    )
    first, _ = await create_topic(
        backend=backend1, store=store, request=request
    )
    assert first.telegram_topic_id == 11

    backend2 = FakeTopicBackend(topic_id=22)
    second, _ = await create_topic(
        backend=backend2, store=store, request=request
    )
    assert second.replayed is True
    assert second.telegram_topic_id == 11
    assert backend2.created == []


async def test_create_topic_failure_records_failed(
    store: OperationStore,
) -> None:
    backend = FakeTopicBackend(fail_create=True)
    request = TopicCreateRequest(
        telegram_chat_id=-100,
        topic_name="Doomed",
        external_ref=9,
    )

    with pytest.raises(RuntimeError):
        await create_topic(backend=backend, store=store, request=request)

    with pytest.raises(TopicCreateFailed):
        await create_topic(
            backend=FakeTopicBackend(), store=store, request=request
        )


async def test_create_topic_first_message_failure_is_non_fatal(
    store: OperationStore,
) -> None:
    backend = FakeTopicBackend(fail_send=True)
    request = TopicCreateRequest(
        telegram_chat_id=-100,
        topic_name="Created but silent",
        external_ref=33,
    )
    result, op = await create_topic(
        backend=backend, store=store, request=request, plugins=_planfix_plugins()
    )
    assert op.status is OperationStatus.COMPLETED
    assert result.telegram_topic_id == 555
    assert result.first_message_kind == "task"
    assert result.first_message_text == "/task 33"
    assert result.telegram_message_id is None


async def test_create_topic_flood_wait_marks_needs_review_not_failed(
    store: OperationStore,
) -> None:
    """FLOOD_WAIT on the single-shot create_topic must NOT burn the
    idempotency key on a terminal ``failed`` state. The op is transient and
    must be retryable once the FLOOD_WAIT window expires; marking it
    ``needs_review`` keeps the row recoverable via ``operations retry``.
    """

    class FloodingBackend:
        async def create_topic(self, *, chat_id: int, name: str) -> int:
            raise FloodWaitError(7.0)

        async def send_message(
            self, *, chat_id: int, text: str, topic_id: int | None = None
        ) -> int:
            raise AssertionError("send_message should not run after FLOOD_WAIT")

    backend = FloodingBackend()
    request = TopicCreateRequest(
        telegram_chat_id=-100,
        topic_name="FloodWaiting",
        external_ref=77,
    )

    with pytest.raises(TopicCreateNeedsReview):
        await create_topic(backend=backend, store=store, request=request)

    # Replay must NOT raise TopicCreateFailed — that would mean the op was
    # locked into a terminal failure on a transient signal.
    with pytest.raises(TopicCreateNeedsReview):
        await create_topic(backend=backend, store=store, request=request)


async def test_create_topic_rejects_empty_name(store: OperationStore) -> None:
    backend = FakeTopicBackend()
    request = TopicCreateRequest(telegram_chat_id=-100, topic_name="   ")
    with pytest.raises(ValueError):
        await create_topic(backend=backend, store=store, request=request)


# ---------------------------------------------------------------------------
# HTTP tests
# ---------------------------------------------------------------------------


def _http_client(
    minimal_config_yaml: str,
    backend: FakeTopicBackend,
    *,
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
        topic_backend_factory=lambda _request: backend,
        folder_backend_factory=(
            (lambda _request: folder_backend)
            if folder_backend is not None
            else None
        ),
        operation_store=store,
    )
    return TestClient(app)


def test_http_create_topic_happy_path(minimal_config_yaml: str) -> None:
    backend = FakeTopicBackend()
    client = _http_client(minimal_config_yaml, backend)
    resp = client.post(
        "/telegram/topics",
        json={
            "telegram_chat_id": -100,
            "topic_name": "Hello",
            "planfix_task_id": 42,
        },
        headers={"Authorization": "Bearer secret_token"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["telegram_chat_id"] == -100
    assert body["telegram_topic_id"] == 555
    assert body["first_message_kind"] == "task"
    assert body["operation_status"] == "completed"


def test_http_create_topic_with_chat_name_resolves_via_folder(
    minimal_config_yaml: str,
) -> None:
    folder_backend = FakeFolderBackend(
        [
            FolderSnapshot(
                folder_id=2,
                folder_name="Planfix clients",
                chats=[FolderChat(chat_id=-100, title="Acme")],
            )
        ]
    )
    backend = FakeTopicBackend()
    client = _http_client(
        minimal_config_yaml, backend, folder_backend=folder_backend
    )
    resp = client.post(
        "/telegram/topics",
        json={
            "chat_name": "Acme",
            "folder_name": "Planfix clients",
            "topic_name": "Issue 1",
        },
        headers={"Authorization": "Bearer secret_token"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["telegram_chat_id"] == -100
    assert body["first_message_kind"] == "topic_name"


def test_http_create_topic_requires_auth(minimal_config_yaml: str) -> None:
    backend = FakeTopicBackend()
    client = _http_client(minimal_config_yaml, backend)
    resp = client.post(
        "/telegram/topics",
        json={"telegram_chat_id": -100, "topic_name": "x"},
    )
    assert resp.status_code == 401


def test_http_create_topic_chat_name_without_folder_is_400(
    minimal_config_yaml: str,
) -> None:
    backend = FakeTopicBackend()
    client = _http_client(minimal_config_yaml, backend)
    resp = client.post(
        "/telegram/topics",
        json={"chat_name": "Acme", "topic_name": "x"},
        headers={"Authorization": "Bearer secret_token"},
    )
    # pydantic rejects via 422 since the model validator fires
    assert resp.status_code == 422


def test_http_create_topic_replay_returns_same_topic(
    minimal_config_yaml: str,
) -> None:
    import tempfile

    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".db")
    tmp.close()
    store = OperationStore(Path(tmp.name))

    backend1 = FakeTopicBackend(topic_id=77)
    client1 = _http_client(minimal_config_yaml, backend1, store=store)
    r1 = client1.post(
        "/telegram/topics",
        json={
            "telegram_chat_id": -100,
            "topic_name": "A",
            "planfix_task_id": 1,
        },
        headers={"Authorization": "Bearer secret_token"},
    )
    assert r1.status_code == 200

    backend2 = FakeTopicBackend(topic_id=999)
    client2 = _http_client(minimal_config_yaml, backend2, store=store)
    r2 = client2.post(
        "/telegram/topics",
        json={
            "telegram_chat_id": -100,
            "topic_name": "A",
            "planfix_task_id": 1,
        },
        headers={"Authorization": "Bearer secret_token"},
    )
    assert r2.status_code == 200, r2.text
    body = r2.json()
    assert body["telegram_topic_id"] == 77
    assert body["replayed"] is True
    assert backend2.created == []


# ---------------------------------------------------------------------------
# CLI tests
# ---------------------------------------------------------------------------


def _write_config(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "config.yml"
    p.write_text(body)
    return p


def _patch_cli_topic_backends(
    monkeypatch: pytest.MonkeyPatch,
    backend: FakeTopicBackend,
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

    monkeypatch.setattr(cli_main, "_build_topic_backends", _factory)


def test_cli_topics_create_with_chat_id(
    minimal_config_yaml: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_file = _write_config(tmp_path, minimal_config_yaml)
    backend = FakeTopicBackend()
    folder_backend = FakeFolderBackend([])
    store = OperationStore(tmp_path / "state.db")
    _patch_cli_topic_backends(monkeypatch, backend, folder_backend, store)

    runner = CliRunner()
    result = runner.invoke(
        cli_main.app,
        [
            "topics",
            "create",
            "--topic-name",
            "Demo",
            "--chat-id",
            "-100",
            "--planfix-task-id",
            "55",
            "--config",
            str(config_file),
        ],
    )
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload["telegram_chat_id"] == -100
    assert payload["telegram_topic_id"] == 555
    assert payload["first_message_kind"] == "task"


def test_cli_topics_create_with_chat_name_resolves_via_folder(
    minimal_config_yaml: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_file = _write_config(tmp_path, minimal_config_yaml)
    backend = FakeTopicBackend(topic_id=88)
    folder_backend = FakeFolderBackend(
        [
            FolderSnapshot(
                folder_id=2,
                folder_name="Planfix clients",
                chats=[FolderChat(chat_id=-500, title="Acme")],
            )
        ]
    )
    store = OperationStore(tmp_path / "state.db")
    _patch_cli_topic_backends(monkeypatch, backend, folder_backend, store)

    runner = CliRunner()
    result = runner.invoke(
        cli_main.app,
        [
            "topics",
            "create",
            "--topic-name",
            "From-name",
            "--chat-name",
            "Acme",
            "--config",
            str(config_file),
        ],
    )
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload["telegram_chat_id"] == -500
    assert payload["telegram_topic_id"] == 88


def test_cli_topics_create_requires_chat_id_or_name(
    minimal_config_yaml: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_file = _write_config(tmp_path, minimal_config_yaml)
    backend = FakeTopicBackend()
    folder_backend = FakeFolderBackend([])
    store = OperationStore(tmp_path / "state.db")
    _patch_cli_topic_backends(monkeypatch, backend, folder_backend, store)

    runner = CliRunner()
    result = runner.invoke(
        cli_main.app,
        [
            "topics",
            "create",
            "--topic-name",
            "Demo",
            "--config",
            str(config_file),
        ],
    )
    assert result.exit_code == 2
