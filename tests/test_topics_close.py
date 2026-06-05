"""Tests for Task 10 — topic close (domain, HTTP, CLI)."""

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
from telegram_assistant.persistence import (
    OperationStatus,
    OperationStore,
)
from telegram_assistant.topics import (
    AmbiguousTopicNameError,
    TopicCloseFailed,
    TopicCloseRequest,
    TopicNotFoundError,
    TopicSummary,
    close_topic,
    resolve_topic_id_by_name,
)


class FakeTopicBackend:
    """In-memory TopicBackend recording close/list calls."""

    def __init__(
        self,
        *,
        topics: list[TopicSummary] | None = None,
        fail_close: bool = False,
    ) -> None:
        self._topics = list(topics or [])
        self._fail_close = fail_close
        self.closed: list[tuple[int, int]] = []

    async def create_topic(self, *, chat_id: int, name: str) -> int:
        raise NotImplementedError

    async def send_message(
        self, *, chat_id: int, text: str, topic_id: int | None = None
    ) -> int:
        raise NotImplementedError

    async def close_topic(self, *, chat_id: int, topic_id: int) -> None:
        if self._fail_close:
            raise RuntimeError("server overloaded")
        self.closed.append((chat_id, topic_id))
        for idx, t in enumerate(self._topics):
            if t.topic_id == topic_id:
                self._topics[idx] = TopicSummary(
                    topic_id=t.topic_id, title=t.title, closed=True
                )
                break

    async def list_topics(self, *, chat_id: int) -> list[TopicSummary]:
        return list(self._topics)


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
# Domain tests
# ---------------------------------------------------------------------------


@pytest.fixture()
def store(tmp_path: Path) -> OperationStore:
    return OperationStore(tmp_path / "state.db")


async def test_close_topic_happy_path(store: OperationStore) -> None:
    backend = FakeTopicBackend(
        topics=[TopicSummary(topic_id=42, title="Issue 1")]
    )
    request = TopicCloseRequest(
        telegram_chat_id=-100, telegram_topic_id=42, reason="resolved"
    )
    result, op = await close_topic(backend=backend, store=store, request=request)
    assert op.status is OperationStatus.COMPLETED
    assert result.telegram_chat_id == -100
    assert result.telegram_topic_id == 42
    assert result.status == "closed"
    assert result.reason == "resolved"
    assert result.replayed is False
    assert backend.closed == [(-100, 42)]


async def test_close_topic_is_idempotent_on_reclose(
    store: OperationStore,
) -> None:
    backend = FakeTopicBackend(
        topics=[TopicSummary(topic_id=42, title="Issue 1")]
    )
    request = TopicCloseRequest(telegram_chat_id=-100, telegram_topic_id=42)
    first, op1 = await close_topic(backend=backend, store=store, request=request)
    assert first.replayed is False
    assert backend.closed == [(-100, 42)]

    # Second call with the same chat+topic ids must replay without invoking
    # the backend a second time.
    backend2 = FakeTopicBackend()
    second, op2 = await close_topic(
        backend=backend2, store=store, request=request
    )
    assert second.replayed is True
    assert second.status == "closed"
    assert op1.id == op2.id
    assert backend2.closed == []


async def test_close_topic_failure_marks_failed(store: OperationStore) -> None:
    backend = FakeTopicBackend(fail_close=True)
    request = TopicCloseRequest(telegram_chat_id=-100, telegram_topic_id=99)
    with pytest.raises(RuntimeError):
        await close_topic(backend=backend, store=store, request=request)
    with pytest.raises(TopicCloseFailed):
        await close_topic(
            backend=FakeTopicBackend(), store=store, request=request
        )


async def test_close_topic_rejects_non_positive_topic_id(
    store: OperationStore,
) -> None:
    backend = FakeTopicBackend()
    with pytest.raises(ValueError):
        await close_topic(
            backend=backend,
            store=store,
            request=TopicCloseRequest(
                telegram_chat_id=-100, telegram_topic_id=0
            ),
        )


async def test_resolve_topic_id_by_name_happy_path() -> None:
    backend = FakeTopicBackend(
        topics=[
            TopicSummary(topic_id=1, title="Alpha"),
            TopicSummary(topic_id=2, title="Beta"),
        ]
    )
    found = await resolve_topic_id_by_name(
        backend=backend, telegram_chat_id=-100, topic_name="Beta"
    )
    assert found == 2


async def test_resolve_topic_id_by_name_missing_raises() -> None:
    backend = FakeTopicBackend(topics=[TopicSummary(topic_id=1, title="Alpha")])
    with pytest.raises(TopicNotFoundError):
        await resolve_topic_id_by_name(
            backend=backend, telegram_chat_id=-100, topic_name="Missing"
        )


async def test_resolve_topic_id_by_name_ambiguous_raises() -> None:
    backend = FakeTopicBackend(
        topics=[
            TopicSummary(topic_id=1, title="Same"),
            TopicSummary(topic_id=2, title="Same"),
        ]
    )
    with pytest.raises(AmbiguousTopicNameError) as exc_info:
        await resolve_topic_id_by_name(
            backend=backend, telegram_chat_id=-100, topic_name="Same"
        )
    assert sorted(exc_info.value.matches) == [1, 2]


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


def test_http_close_topic_happy_path(minimal_config_yaml: str) -> None:
    backend = FakeTopicBackend(
        topics=[TopicSummary(topic_id=42, title="Issue 1")]
    )
    client = _http_client(minimal_config_yaml, backend)
    resp = client.post(
        "/telegram/topics/42/close",
        json={"telegram_chat_id": -100, "reason": "done"},
        headers={"Authorization": "Bearer secret_token"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["telegram_chat_id"] == -100
    assert body["telegram_topic_id"] == 42
    assert body["status"] == "closed"
    assert body["reason"] == "done"
    assert body["operation_status"] == "completed"
    assert backend.closed == [(-100, 42)]


def test_http_close_topic_replays_on_reclose(
    minimal_config_yaml: str,
) -> None:
    import tempfile

    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".db")
    tmp.close()
    store = OperationStore(Path(tmp.name))

    backend = FakeTopicBackend(
        topics=[TopicSummary(topic_id=7, title="Issue")]
    )
    client = _http_client(minimal_config_yaml, backend, store=store)
    r1 = client.post(
        "/telegram/topics/7/close",
        json={"telegram_chat_id": -100},
        headers={"Authorization": "Bearer secret_token"},
    )
    assert r1.status_code == 200

    backend2 = FakeTopicBackend()
    client2 = _http_client(minimal_config_yaml, backend2, store=store)
    r2 = client2.post(
        "/telegram/topics/7/close",
        json={"telegram_chat_id": -100},
        headers={"Authorization": "Bearer secret_token"},
    )
    assert r2.status_code == 200, r2.text
    body = r2.json()
    assert body["replayed"] is True
    assert body["status"] == "closed"
    assert backend2.closed == []


def test_http_close_topic_requires_auth(minimal_config_yaml: str) -> None:
    backend = FakeTopicBackend()
    client = _http_client(minimal_config_yaml, backend)
    resp = client.post(
        "/telegram/topics/1/close",
        json={"telegram_chat_id": -100},
    )
    assert resp.status_code == 401


def test_http_close_topic_resolves_chat_by_name(
    minimal_config_yaml: str,
) -> None:
    backend = FakeTopicBackend(
        topics=[TopicSummary(topic_id=11, title="Issue")]
    )
    folder_backend = FakeFolderBackend(
        [
            FolderSnapshot(
                folder_id=2,
                folder_name="Planfix clients",
                chats=[FolderChat(chat_id=-100, title="Acme")],
            )
        ]
    )
    client = _http_client(
        minimal_config_yaml, backend, folder_backend=folder_backend
    )
    resp = client.post(
        "/telegram/topics/11/close",
        json={"chat_name": "Acme", "folder_name": "Planfix clients"},
        headers={"Authorization": "Bearer secret_token"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["telegram_chat_id"] == -100
    assert backend.closed == [(-100, 11)]


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


def test_cli_topics_close_with_topic_id(
    minimal_config_yaml: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_file = _write_config(tmp_path, minimal_config_yaml)
    backend = FakeTopicBackend(
        topics=[TopicSummary(topic_id=42, title="Issue 1")]
    )
    folder_backend = FakeFolderBackend([])
    store = OperationStore(tmp_path / "state.db")
    _patch_cli_topic_backends(monkeypatch, backend, folder_backend, store)

    runner = CliRunner()
    result = runner.invoke(
        cli_main.app,
        [
            "topics",
            "close",
            "--topic-id",
            "42",
            "--chat-id",
            "-100",
            "--reason",
            "resolved",
            "--config",
            str(config_file),
        ],
    )
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload["telegram_topic_id"] == 42
    assert payload["status"] == "closed"
    assert payload["reason"] == "resolved"
    assert backend.closed == [(-100, 42)]


def test_cli_topics_close_with_topic_name_resolves(
    minimal_config_yaml: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_file = _write_config(tmp_path, minimal_config_yaml)
    backend = FakeTopicBackend(
        topics=[
            TopicSummary(topic_id=1, title="Alpha"),
            TopicSummary(topic_id=2, title="Beta"),
        ]
    )
    folder_backend = FakeFolderBackend([])
    store = OperationStore(tmp_path / "state.db")
    _patch_cli_topic_backends(monkeypatch, backend, folder_backend, store)

    runner = CliRunner()
    result = runner.invoke(
        cli_main.app,
        [
            "topics",
            "close",
            "--topic-name",
            "Beta",
            "--chat-id",
            "-100",
            "--config",
            str(config_file),
        ],
    )
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload["telegram_topic_id"] == 2
    assert backend.closed == [(-100, 2)]


def test_cli_topics_close_ambiguous_topic_name_fails(
    minimal_config_yaml: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_file = _write_config(tmp_path, minimal_config_yaml)
    backend = FakeTopicBackend(
        topics=[
            TopicSummary(topic_id=1, title="Same"),
            TopicSummary(topic_id=2, title="Same"),
        ]
    )
    folder_backend = FakeFolderBackend([])
    store = OperationStore(tmp_path / "state.db")
    _patch_cli_topic_backends(monkeypatch, backend, folder_backend, store)

    runner = CliRunner()
    result = runner.invoke(
        cli_main.app,
        [
            "topics",
            "close",
            "--topic-name",
            "Same",
            "--chat-id",
            "-100",
            "--config",
            str(config_file),
        ],
    )
    assert result.exit_code == 2
    assert "matches 2 topics" in (result.stderr or result.stdout)
    assert backend.closed == []


def test_cli_topics_close_requires_topic_id_or_name(
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
            "close",
            "--chat-id",
            "-100",
            "--config",
            str(config_file),
        ],
    )
    assert result.exit_code == 2


def test_cli_topics_close_requires_chat_ref(
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
            "close",
            "--topic-id",
            "42",
            "--config",
            str(config_file),
        ],
    )
    assert result.exit_code == 2
