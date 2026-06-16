"""Tests for topic open/reopen (domain, HTTP, CLI)."""

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
    TopicOpenRequest,
    TopicSummary,
    open_topic,
)


class FakeTopicBackend:
    """In-memory TopicBackend recording open/list calls."""

    def __init__(
        self,
        *,
        topics: list[TopicSummary] | None = None,
        fail_open: bool = False,
    ) -> None:
        self._topics = list(topics or [])
        self._fail_open = fail_open
        self.opened: list[tuple[int, int]] = []

    async def create_topic(self, *, chat_id: int, name: str) -> int:
        raise NotImplementedError

    async def send_message(
        self, *, chat_id: int, text: str, topic_id: int | None = None
    ) -> int:
        raise NotImplementedError

    async def close_topic(self, *, chat_id: int, topic_id: int) -> None:
        raise NotImplementedError

    async def open_topic(self, *, chat_id: int, topic_id: int) -> None:
        if self._fail_open:
            raise RuntimeError("server overloaded")
        self.opened.append((chat_id, topic_id))
        for idx, t in enumerate(self._topics):
            if t.topic_id == topic_id:
                self._topics[idx] = TopicSummary(
                    topic_id=t.topic_id, title=t.title, closed=False
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


async def test_open_topic_happy_path(store: OperationStore) -> None:
    backend = FakeTopicBackend(
        topics=[TopicSummary(topic_id=42, title="Issue 1", closed=True)]
    )
    request = TopicOpenRequest(
        telegram_chat_id=-100, telegram_topic_id=42, reason="reopened"
    )
    result, op = await open_topic(backend=backend, store=store, request=request)
    assert op.status is OperationStatus.COMPLETED
    assert result.telegram_chat_id == -100
    assert result.telegram_topic_id == 42
    assert result.status == "open"
    assert result.reason == "reopened"
    assert result.replayed is False
    assert backend.opened == [(-100, 42)]


async def test_open_topic_reruns_on_reopen(
    store: OperationStore,
) -> None:
    backend = FakeTopicBackend(
        topics=[TopicSummary(topic_id=42, title="Issue 1", closed=True)]
    )
    request = TopicOpenRequest(telegram_chat_id=-100, telegram_topic_id=42)
    first, op1 = await open_topic(backend=backend, store=store, request=request)
    assert first.replayed is False
    assert backend.opened == [(-100, 42)]

    # Close/open are repeatable state-setters: a second call re-executes
    # against the backend (the prior record is superseded), so toggling works.
    backend2 = FakeTopicBackend(
        topics=[TopicSummary(topic_id=42, title="Issue 1", closed=False)]
    )
    second, op2 = await open_topic(
        backend=backend2, store=store, request=request
    )
    assert second.replayed is False
    assert second.status == "open"
    assert op1.id != op2.id
    assert backend2.opened == [(-100, 42)]


async def test_open_topic_executes_every_call(store: OperationStore) -> None:
    """No idempotency: each call runs the backend and records a fresh op."""
    backend = FakeTopicBackend(
        topics=[TopicSummary(topic_id=42, title="Issue 1", closed=True)]
    )
    request = TopicOpenRequest(telegram_chat_id=-100, telegram_topic_id=42)
    op_ids = []
    for _ in range(3):
        result, op = await open_topic(
            backend=backend, store=store, request=request
        )
        assert result.replayed is False
        assert op.status is OperationStatus.COMPLETED
        op_ids.append(op.id)
    assert backend.opened == [(-100, 42), (-100, 42), (-100, 42)]
    assert len(set(op_ids)) == 3  # a distinct audit op per call


async def test_open_topic_failure_does_not_latch(store: OperationStore) -> None:
    backend = FakeTopicBackend(fail_open=True)
    request = TopicOpenRequest(telegram_chat_id=-100, telegram_topic_id=99)
    with pytest.raises(RuntimeError):
        await open_topic(backend=backend, store=store, request=request)
    # A failed attempt must not permanently latch the key (the bug that made
    # /open return 409 forever): the next call supersedes the stale failed
    # record and re-executes successfully.
    healthy = FakeTopicBackend(
        topics=[TopicSummary(topic_id=99, title="Issue", closed=True)]
    )
    result, _ = await open_topic(backend=healthy, store=store, request=request)
    assert result.status == "open"
    assert healthy.opened == [(-100, 99)]


async def test_open_topic_rejects_non_positive_topic_id(
    store: OperationStore,
) -> None:
    backend = FakeTopicBackend()
    with pytest.raises(ValueError):
        await open_topic(
            backend=backend,
            store=store,
            request=TopicOpenRequest(
                telegram_chat_id=-100, telegram_topic_id=0
            ),
        )


async def test_open_and_close_keys_are_independent(
    store: OperationStore,
) -> None:
    """Opening then (conceptually) the same chat+topic for open uses its own key.

    A close operation for the same chat+topic must not collide with an open
    operation, so opening succeeds on its own fresh idempotency key.
    """
    from telegram_assistant.topics import TopicCloseRequest, close_topic

    class _Backend(FakeTopicBackend):
        async def close_topic(self, *, chat_id: int, topic_id: int) -> None:
            return None

    backend = _Backend(
        topics=[TopicSummary(topic_id=5, title="X", closed=True)]
    )
    _, close_op = await close_topic(
        backend=backend,
        store=store,
        request=TopicCloseRequest(telegram_chat_id=-100, telegram_topic_id=5),
    )
    _, open_op = await open_topic(
        backend=backend,
        store=store,
        request=TopicOpenRequest(telegram_chat_id=-100, telegram_topic_id=5),
    )
    assert close_op.id != open_op.id
    assert backend.opened == [(-100, 5)]


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


def test_http_open_topic_happy_path(minimal_config_yaml: str) -> None:
    backend = FakeTopicBackend(
        topics=[TopicSummary(topic_id=42, title="Issue 1", closed=True)]
    )
    client = _http_client(minimal_config_yaml, backend)
    resp = client.post(
        "/telegram/topics/42/open",
        json={"telegram_chat_id": -100, "reason": "reopen"},
        headers={"Authorization": "Bearer secret_token"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["telegram_chat_id"] == -100
    assert body["telegram_topic_id"] == 42
    assert body["status"] == "open"
    assert body["reason"] == "reopen"
    assert body["operation_status"] == "completed"
    assert backend.opened == [(-100, 42)]


def test_http_open_topic_reruns_on_reopen(
    minimal_config_yaml: str,
) -> None:
    import tempfile

    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".db")
    tmp.close()
    store = OperationStore(Path(tmp.name))

    backend = FakeTopicBackend(
        topics=[TopicSummary(topic_id=7, title="Issue", closed=True)]
    )
    client = _http_client(minimal_config_yaml, backend, store=store)
    r1 = client.post(
        "/telegram/topics/7/open",
        json={"telegram_chat_id": -100},
        headers={"Authorization": "Bearer secret_token"},
    )
    assert r1.status_code == 200

    # A second open re-executes (state-setter), not a silent replay.
    backend2 = FakeTopicBackend(
        topics=[TopicSummary(topic_id=7, title="Issue", closed=False)]
    )
    client2 = _http_client(minimal_config_yaml, backend2, store=store)
    r2 = client2.post(
        "/telegram/topics/7/open",
        json={"telegram_chat_id": -100},
        headers={"Authorization": "Bearer secret_token"},
    )
    assert r2.status_code == 200, r2.text
    body = r2.json()
    assert body["replayed"] is False
    assert body["status"] == "open"
    assert backend2.opened == [(-100, 7)]


def test_http_open_topic_requires_auth(minimal_config_yaml: str) -> None:
    backend = FakeTopicBackend()
    client = _http_client(minimal_config_yaml, backend)
    resp = client.post(
        "/telegram/topics/1/open",
        json={"telegram_chat_id": -100},
    )
    assert resp.status_code == 401


def test_http_open_topic_resolves_chat_by_name(
    minimal_config_yaml: str,
) -> None:
    backend = FakeTopicBackend(
        topics=[TopicSummary(topic_id=11, title="Issue", closed=True)]
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
        "/telegram/topics/11/open",
        json={"chat_name": "Acme", "folder_name": "Planfix clients"},
        headers={"Authorization": "Bearer secret_token"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["telegram_chat_id"] == -100
    assert backend.opened == [(-100, 11)]


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


def test_cli_topics_open_with_topic_id(
    minimal_config_yaml: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_file = _write_config(tmp_path, minimal_config_yaml)
    backend = FakeTopicBackend(
        topics=[TopicSummary(topic_id=42, title="Issue 1", closed=True)]
    )
    folder_backend = FakeFolderBackend([])
    store = OperationStore(tmp_path / "state.db")
    _patch_cli_topic_backends(monkeypatch, backend, folder_backend, store)

    runner = CliRunner()
    result = runner.invoke(
        cli_main.app,
        [
            "topics",
            "open",
            "--topic-id",
            "42",
            "--chat-id",
            "-100",
            "--reason",
            "reopened",
            "--config",
            str(config_file),
        ],
    )
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload["telegram_topic_id"] == 42
    assert payload["status"] == "open"
    assert payload["reason"] == "reopened"
    assert backend.opened == [(-100, 42)]


def test_cli_topics_open_dry_run_reports_already_open(
    minimal_config_yaml: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_file = _write_config(tmp_path, minimal_config_yaml)
    backend = FakeTopicBackend(
        topics=[TopicSummary(topic_id=42, title="Issue 1", closed=False)]
    )
    folder_backend = FakeFolderBackend([])
    store = OperationStore(tmp_path / "state.db")
    _patch_cli_topic_backends(monkeypatch, backend, folder_backend, store)

    runner = CliRunner()
    result = runner.invoke(
        cli_main.app,
        [
            "topics",
            "open",
            "--topic-id",
            "42",
            "--chat-id",
            "-100",
            "--dry-run",
            "--config",
            str(config_file),
        ],
    )
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload["command"] == "topics.open"
    assert payload["resolved"]["already_open"] is True
    assert backend.opened == []


def test_cli_topics_open_with_topic_name_resolves(
    minimal_config_yaml: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_file = _write_config(tmp_path, minimal_config_yaml)
    backend = FakeTopicBackend(
        topics=[
            TopicSummary(topic_id=1, title="Alpha", closed=True),
            TopicSummary(topic_id=2, title="Beta", closed=True),
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
            "open",
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
    assert backend.opened == [(-100, 2)]


def test_cli_topics_open_ambiguous_topic_name_fails(
    minimal_config_yaml: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_file = _write_config(tmp_path, minimal_config_yaml)
    backend = FakeTopicBackend(
        topics=[
            TopicSummary(topic_id=1, title="Same", closed=True),
            TopicSummary(topic_id=2, title="Same", closed=True),
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
            "open",
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
    assert backend.opened == []


def test_cli_topics_open_requires_topic_id_or_name(
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
            "open",
            "--chat-id",
            "-100",
            "--config",
            str(config_file),
        ],
    )
    assert result.exit_code == 2


def test_cli_topics_open_requires_chat_ref(
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
            "open",
            "--topic-id",
            "42",
            "--config",
            str(config_file),
        ],
    )
    assert result.exit_code == 2
