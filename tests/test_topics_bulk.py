"""Tests for Task 9 — bulk topic creation (domain, HTTP, CLI)."""

from __future__ import annotations

import asyncio
import json
import textwrap
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
    BulkTopicCreateNeedsReview,
    BulkTopicCreateRequest,
    BulkTopicItem,
    bulk_create_topics,
)
from telegram_assistant.worker import (
    BulkItemSpec,
    FloodWaitError,
    NeedsReviewError,
    WorkerQueue,
)


def _planfix_plugins() -> PluginRegistry:
    """Registry with the Planfix plugin enabled, for /task-behavior tests."""
    return PluginRegistry([PlanfixPlugin(PlanfixPluginConfig(enabled=True))])

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeTopicBackend:
    """Records every Telethon call so tests can assert on side-effects.

    ``fail_for`` lets a test poison specific topic names (the second-element
    behavior determines what's raised). Topic ids are handed out sequentially
    starting at ``topic_id_start`` so multiple topics in a single bulk are
    distinguishable in assertions.
    """

    def __init__(
        self,
        *,
        topic_id_start: int = 1000,
        message_id: int = 7777,
        fail_for: dict[str, BaseException] | None = None,
    ) -> None:
        self._next_topic_id = topic_id_start
        self._message_id = message_id
        self._fail_for = fail_for or {}
        self.created: list[tuple[int, str]] = []
        self.messages: list[tuple[int, str, int | None]] = []

    async def create_topic(self, *, chat_id: int, name: str) -> int:
        if name in self._fail_for:
            raise self._fail_for[name]
        tid = self._next_topic_id
        self._next_topic_id += 1
        self.created.append((chat_id, name))
        return tid

    async def send_message(
        self, *, chat_id: int, text: str, topic_id: int | None = None
    ) -> int:
        self.messages.append((chat_id, text, topic_id))
        return self._message_id


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
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def store(tmp_path: Path) -> OperationStore:
    return OperationStore(tmp_path / "state.db")


def _make_queue(store: OperationStore, *, sleeps: list[float] | None = None) -> WorkerQueue:
    record = sleeps if sleeps is not None else []

    async def fake_sleep(seconds: float) -> None:
        record.append(seconds)

    return WorkerQueue(
        store,
        max_parallel=1,
        flood_wait_safety_margin_seconds=2.0,
        sleep=fake_sleep,
    )


# ---------------------------------------------------------------------------
# Domain tests
# ---------------------------------------------------------------------------


async def test_bulk_create_happy_path_creates_all_topics(
    store: OperationStore,
) -> None:
    backend = FakeTopicBackend()
    queue = _make_queue(store)
    req = BulkTopicCreateRequest(
        telegram_chat_id=-100,
        items=(
            BulkTopicItem(topic_name="Alpha", external_ref=1),
            BulkTopicItem(topic_name="Beta", message="hello"),
            BulkTopicItem(topic_name="Gamma"),
        ),
        operation_id="op-happy",
    )
    result, op = await bulk_create_topics(
        backend=backend, store=store, queue=queue, request=req, plugins=_planfix_plugins()
    )

    assert op.status is OperationStatus.COMPLETED
    assert result.created == 3
    assert result.existed == 0
    assert result.failed == 0
    assert result.needs_review == 0
    assert result.skipped == 0
    assert [it.status for it in result.items] == ["created", "created", "created"]
    # First-message logic is reused per item:
    assert result.items[0].first_message_kind == "task"
    assert result.items[1].first_message_kind == "message"
    assert result.items[2].first_message_kind == "topic_name"
    # Topic ids assigned sequentially
    assert [it.telegram_topic_id for it in result.items] == [1000, 1001, 1002]


async def test_bulk_create_per_item_idempotency_planfix_task_id(
    store: OperationStore,
) -> None:
    """Re-running with the same operation_id replays prior items as 'existed'."""
    backend1 = FakeTopicBackend(topic_id_start=500)
    queue = _make_queue(store)
    req = BulkTopicCreateRequest(
        telegram_chat_id=-100,
        items=(
            BulkTopicItem(topic_name="Alpha", external_ref=10),
            BulkTopicItem(topic_name="Beta", external_ref=20),
        ),
        operation_id="op-replay",
    )
    first, _ = await bulk_create_topics(
        backend=backend1, store=store, queue=queue, request=req, plugins=_planfix_plugins()
    )
    assert first.created == 2

    # Second run with the same operation_id — Telegram should not be touched.
    backend2 = FakeTopicBackend(topic_id_start=9999)
    second, op2 = await bulk_create_topics(
        backend=backend2, store=store, queue=queue, request=req, plugins=_planfix_plugins()
    )
    assert backend2.created == []
    assert backend2.messages == []
    assert second.created == 0
    assert second.existed == 2
    assert all(it.status == "existed" for it in second.items)
    # Saved topic ids are preserved across replay.
    assert [it.telegram_topic_id for it in second.items] == [500, 501]
    assert op2.status is OperationStatus.COMPLETED


async def test_bulk_create_resume_partial_after_restart(
    store: OperationStore,
) -> None:
    """If an earlier run only completed some items, a retry finishes the rest."""
    backend1 = FakeTopicBackend(
        topic_id_start=100,
        fail_for={"Beta": RuntimeError("transient: server overloaded")},
    )
    queue = _make_queue(store)
    req = BulkTopicCreateRequest(
        telegram_chat_id=-100,
        items=(
            BulkTopicItem(topic_name="Alpha", external_ref=1),
            BulkTopicItem(topic_name="Beta", external_ref=2),
            BulkTopicItem(topic_name="Gamma", external_ref=3),
        ),
        operation_id="op-resume",
    )
    first, op1 = await bulk_create_topics(
        backend=backend1, store=store, queue=queue, request=req, plugins=_planfix_plugins()
    )
    assert first.created == 2
    assert first.failed == 1
    # Parent transitions to needs_review because one item failed.
    assert op1.status is OperationStatus.NEEDS_REVIEW

    # Operator resets the parent + failed items for retry.
    store.reset_operation_for_retry(op1.id)
    store.reset_items_for_retry(op1.id)

    # Second run with a healthy backend.
    backend2 = FakeTopicBackend(topic_id_start=9000)
    second, op2 = await bulk_create_topics(
        backend=backend2, store=store, queue=queue, request=req, plugins=_planfix_plugins()
    )
    # Only Beta should be re-executed — Alpha and Gamma stay as `existed`.
    assert backend2.created == [(-100, "Beta")]
    assert second.created == 1
    assert second.existed == 2
    assert second.failed == 0
    assert op2.status is OperationStatus.COMPLETED


async def test_bulk_create_continue_on_error_keeps_running(
    store: OperationStore,
) -> None:
    backend = FakeTopicBackend(
        fail_for={"Beta": RuntimeError("privacy")},
    )
    queue = _make_queue(store)
    req = BulkTopicCreateRequest(
        telegram_chat_id=-100,
        items=(
            BulkTopicItem(topic_name="Alpha", external_ref=1),
            BulkTopicItem(topic_name="Beta", external_ref=2),
            BulkTopicItem(topic_name="Gamma", external_ref=3),
        ),
        continue_on_error=True,
        operation_id="op-continue",
    )
    result, op = await bulk_create_topics(
        backend=backend, store=store, queue=queue, request=req, plugins=_planfix_plugins()
    )
    statuses = [it.status for it in result.items]
    assert statuses == ["created", "failed", "created"]
    assert result.failed == 1
    assert op.status is OperationStatus.NEEDS_REVIEW
    # Backend got both successful create calls — Beta failed.
    assert (-100, "Alpha") in backend.created
    assert (-100, "Gamma") in backend.created
    assert (-100, "Beta") not in backend.created


async def test_bulk_create_stop_on_error_short_circuits(
    store: OperationStore,
) -> None:
    backend = FakeTopicBackend(
        fail_for={"Beta": RuntimeError("privacy")},
    )
    queue = _make_queue(store)
    req = BulkTopicCreateRequest(
        telegram_chat_id=-100,
        items=(
            BulkTopicItem(topic_name="Alpha", external_ref=1),
            BulkTopicItem(topic_name="Beta", external_ref=2),
            BulkTopicItem(topic_name="Gamma", external_ref=3),
        ),
        continue_on_error=False,
        operation_id="op-stop",
    )
    result, op = await bulk_create_topics(
        backend=backend, store=store, queue=queue, request=req, plugins=_planfix_plugins()
    )
    statuses = [it.status for it in result.items]
    # Alpha succeeds, Beta fails, Gamma is skipped without touching Telegram.
    assert statuses == ["created", "failed", "skipped"]
    assert (-100, "Gamma") not in backend.created
    assert op.status is OperationStatus.NEEDS_REVIEW
    assert result.skipped == 1


async def test_bulk_create_flood_wait_pauses_and_retries(
    store: OperationStore,
) -> None:
    """FLOOD_WAIT on a single item triggers a queue-managed sleep + retry."""

    call_log: list[str] = []

    class FloodyBackend:
        def __init__(self) -> None:
            self._calls = 0

        async def create_topic(self, *, chat_id: int, name: str) -> int:
            self._calls += 1
            call_log.append(f"create:{name}")
            if name == "Beta" and self._calls == 2:
                raise FloodWaitError(3.0)
            return 1000 + self._calls

        async def send_message(
            self, *, chat_id: int, text: str, topic_id: int | None = None
        ) -> int:
            call_log.append(f"send:{text}")
            return 5

    sleeps: list[float] = []
    queue = _make_queue(store, sleeps=sleeps)
    req = BulkTopicCreateRequest(
        telegram_chat_id=-100,
        items=(
            BulkTopicItem(topic_name="Alpha", external_ref=1),
            BulkTopicItem(topic_name="Beta", external_ref=2),
        ),
        operation_id="op-flood",
    )
    result, op = await bulk_create_topics(
        backend=FloodyBackend(), store=store, queue=queue, request=req, plugins=_planfix_plugins()
    )
    # The flood-wait + margin (3 + 2) shows up once for the retried call.
    assert sleeps == [5.0]
    assert result.created == 2
    assert op.status is OperationStatus.COMPLETED


async def test_bulk_create_first_message_flood_wait_propagates(
    store: OperationStore,
) -> None:
    """A FLOOD_WAIT raised by send_message during first-message must propagate
    to the queue so the item can pause-and-retry, not get swallowed into a
    successful-looking topic without its first message."""

    class FloodyFirstMessageBackend:
        def __init__(self) -> None:
            self.create_calls = 0
            self.send_calls = 0

        async def create_topic(self, *, chat_id: int, name: str) -> int:
            self.create_calls += 1
            return 1000 + self.create_calls

        async def send_message(
            self, *, chat_id: int, text: str, topic_id: int | None = None
        ) -> int:
            self.send_calls += 1
            if self.send_calls == 1:
                raise FloodWaitError(4.0)
            return 7777

    sleeps: list[float] = []
    queue = _make_queue(store, sleeps=sleeps)
    backend = FloodyFirstMessageBackend()
    req = BulkTopicCreateRequest(
        telegram_chat_id=-100,
        items=(BulkTopicItem(topic_name="Alpha", external_ref=1),),
        operation_id="op-fw-first-msg",
    )
    result, op = await bulk_create_topics(
        backend=backend, store=store, queue=queue, request=req, plugins=_planfix_plugins()
    )
    # The flood_wait + margin (4 + 2) shows up exactly once: the queue paused
    # the whole item and retried it cleanly, including the create_topic step.
    assert sleeps == [6.0]
    # create_topic ran twice (the queue retries the whole handler from scratch),
    # send_message ran twice too (first raised, second succeeded).
    assert backend.create_calls == 2
    assert backend.send_calls == 2
    assert result.created == 1
    assert op.status is OperationStatus.COMPLETED


async def test_bulk_create_per_item_key_fallback_to_chat_and_name(
    store: OperationStore,
) -> None:
    """Items without planfix_task_id fall back to chat+name dedup within the bulk."""
    backend = FakeTopicBackend()
    queue = _make_queue(store)
    req = BulkTopicCreateRequest(
        telegram_chat_id=-100,
        items=(
            # Same topic_name listed twice — second is a duplicate per the
            # documented per-item key derivation, so the second registration
            # of the same key replays the first item.
            BulkTopicItem(topic_name="Same"),
            BulkTopicItem(topic_name="Other"),
        ),
        operation_id="op-fallback",
    )
    result, _ = await bulk_create_topics(
        backend=backend, store=store, queue=queue, request=req, plugins=_planfix_plugins()
    )
    assert [(c, n) for c, n in backend.created] == [(-100, "Same"), (-100, "Other")]
    statuses = [it.status for it in result.items]
    assert statuses == ["created", "created"]


async def test_bulk_create_rejects_empty_items(store: OperationStore) -> None:
    queue = _make_queue(store)
    with pytest.raises(ValueError):
        await bulk_create_topics(
            backend=FakeTopicBackend(),
            store=store,
            queue=queue,
            request=BulkTopicCreateRequest(telegram_chat_id=-100, items=()),
        )


async def test_bulk_create_rejects_blank_topic_name(store: OperationStore) -> None:
    queue = _make_queue(store)
    with pytest.raises(ValueError):
        await bulk_create_topics(
            backend=FakeTopicBackend(),
            store=store,
            queue=queue,
            request=BulkTopicCreateRequest(
                telegram_chat_id=-100,
                items=(BulkTopicItem(topic_name="  "),),
            ),
        )


async def test_bulk_create_replay_after_needs_review_raises(
    store: OperationStore,
) -> None:
    backend = FakeTopicBackend(fail_for={"Bad": RuntimeError("boom")})
    queue = _make_queue(store)
    req = BulkTopicCreateRequest(
        telegram_chat_id=-100,
        items=(BulkTopicItem(topic_name="Bad", external_ref=99),),
        operation_id="op-nr",
    )
    _, op = await bulk_create_topics(
        backend=backend, store=store, queue=queue, request=req, plugins=_planfix_plugins()
    )
    assert op.status is OperationStatus.NEEDS_REVIEW

    # Calling again without resetting must NOT auto-retry.
    with pytest.raises(BulkTopicCreateNeedsReview):
        await bulk_create_topics(
            backend=FakeTopicBackend(), store=store, queue=queue, request=req, plugins=_planfix_plugins()
        )


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
            (lambda _request: folder_backend) if folder_backend is not None else None
        ),
        operation_store=store,
    )
    return TestClient(app)


def test_http_bulk_create_happy_path(minimal_config_yaml: str) -> None:
    backend = FakeTopicBackend()
    client = _http_client(minimal_config_yaml, backend)
    resp = client.post(
        "/telegram/topics/bulk-create",
        json={
            "telegram_chat_id": -100,
            "operation_id": "op-http-happy",
            "items": [
                {"topic_name": "Alpha", "planfix_task_id": 1},
                {"topic_name": "Beta", "message": "hello"},
            ],
        },
        headers={"Authorization": "Bearer secret_token"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["created"] == 2
    assert body["existed"] == 0
    assert body["failed"] == 0
    assert len(body["items"]) == 2
    assert body["operation_status"] == "completed"


def test_http_bulk_create_requires_auth(minimal_config_yaml: str) -> None:
    backend = FakeTopicBackend()
    client = _http_client(minimal_config_yaml, backend)
    resp = client.post(
        "/telegram/topics/bulk-create",
        json={"telegram_chat_id": -100, "items": [{"topic_name": "x"}]},
    )
    assert resp.status_code == 401


def test_http_bulk_create_replay_returns_existed(minimal_config_yaml: str) -> None:
    import tempfile

    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".db")
    tmp.close()
    store = OperationStore(Path(tmp.name))

    body = {
        "telegram_chat_id": -100,
        "operation_id": "op-http-replay",
        "items": [{"topic_name": "Alpha", "planfix_task_id": 1}],
    }
    backend1 = FakeTopicBackend(topic_id_start=42)
    client1 = _http_client(minimal_config_yaml, backend1, store=store)
    r1 = client1.post(
        "/telegram/topics/bulk-create",
        json=body,
        headers={"Authorization": "Bearer secret_token"},
    )
    assert r1.status_code == 200, r1.text
    assert r1.json()["created"] == 1

    backend2 = FakeTopicBackend(topic_id_start=999)
    client2 = _http_client(minimal_config_yaml, backend2, store=store)
    r2 = client2.post(
        "/telegram/topics/bulk-create",
        json=body,
        headers={"Authorization": "Bearer secret_token"},
    )
    assert r2.status_code == 200, r2.text
    body2 = r2.json()
    assert body2["created"] == 0
    assert body2["existed"] == 1
    # Saved topic id is preserved.
    assert body2["items"][0]["telegram_topic_id"] == 42
    assert backend2.created == []


def test_http_bulk_create_chat_name_resolution(minimal_config_yaml: str) -> None:
    folder_backend = FakeFolderBackend(
        [
            FolderSnapshot(
                folder_id=2,
                folder_name="Planfix clients",
                chats=[FolderChat(chat_id=-555, title="Acme")],
            )
        ]
    )
    backend = FakeTopicBackend()
    client = _http_client(minimal_config_yaml, backend, folder_backend=folder_backend)
    resp = client.post(
        "/telegram/topics/bulk-create",
        json={
            "chat_name": "Acme",
            "folder_name": "Planfix clients",
            "items": [{"topic_name": "T1"}],
        },
        headers={"Authorization": "Bearer secret_token"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["telegram_chat_id"] == -555


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


def test_cli_topics_bulk_create_from_csv(
    minimal_config_yaml: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_file = _write_config(tmp_path, minimal_config_yaml)
    csv_path = tmp_path / "topics.csv"
    csv_path.write_text(
        textwrap.dedent(
            """\
            planfix_task_id,topic_name,message
            101,Alpha,
            ,Beta,hello there
            103,Gamma,
            """
        )
    )

    backend = FakeTopicBackend(topic_id_start=300)
    folder_backend = FakeFolderBackend([])
    store = OperationStore(tmp_path / "state.db")
    _patch_cli_topic_backends(monkeypatch, backend, folder_backend, store)

    runner = CliRunner()
    result = runner.invoke(
        cli_main.app,
        [
            "topics",
            "bulk-create",
            "--chat-id",
            "-100",
            "--file",
            str(csv_path),
            "--operation-id",
            "op-cli-csv",
            "--config",
            str(config_file),
        ],
    )
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload["created"] == 3
    assert payload["telegram_chat_id"] == -100
    statuses = [it["status"] for it in payload["items"]]
    assert statuses == ["created", "created", "created"]
    # CSV parsing preserves message-when-supplied
    kinds = [it["first_message_kind"] for it in payload["items"]]
    assert kinds == ["task", "message", "task"]


def test_cli_topics_bulk_create_from_json(
    minimal_config_yaml: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_file = _write_config(tmp_path, minimal_config_yaml)
    json_path = tmp_path / "topics.json"
    json_path.write_text(
        json.dumps(
            [
                {"topic_name": "A", "planfix_task_id": 1},
                {"topic_name": "B"},
            ]
        )
    )

    backend = FakeTopicBackend(topic_id_start=200)
    folder_backend = FakeFolderBackend([])
    store = OperationStore(tmp_path / "state.db")
    _patch_cli_topic_backends(monkeypatch, backend, folder_backend, store)

    runner = CliRunner()
    result = runner.invoke(
        cli_main.app,
        [
            "topics",
            "bulk-create",
            "--chat-id",
            "-100",
            "--file",
            str(json_path),
            "--operation-id",
            "op-cli-json",
            "--config",
            str(config_file),
        ],
    )
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload["created"] == 2


def test_cli_topics_bulk_create_requires_chat_id_or_name(
    minimal_config_yaml: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_file = _write_config(tmp_path, minimal_config_yaml)
    csv_path = tmp_path / "topics.csv"
    csv_path.write_text("topic_name\nAlpha\n")

    backend = FakeTopicBackend()
    folder_backend = FakeFolderBackend([])
    store = OperationStore(tmp_path / "state.db")
    _patch_cli_topic_backends(monkeypatch, backend, folder_backend, store)

    runner = CliRunner()
    result = runner.invoke(
        cli_main.app,
        [
            "topics",
            "bulk-create",
            "--file",
            str(csv_path),
            "--config",
            str(config_file),
        ],
    )
    assert result.exit_code == 2


def test_cli_topics_bulk_create_rejects_missing_file(
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
            "bulk-create",
            "--chat-id",
            "-100",
            "--file",
            str(tmp_path / "missing.csv"),
            "--config",
            str(config_file),
        ],
    )
    assert result.exit_code == 2


# These imports must not be unused — the linter cares.
_ = (NeedsReviewError, BulkItemSpec, asyncio)
