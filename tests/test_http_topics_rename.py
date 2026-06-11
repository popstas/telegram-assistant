"""HTTP tests for topic rename (Task 7).

Two surfaces:

* ``POST /telegram/topics/{topic_id}/rename`` — id in the path.
* ``POST /telegram/topics/rename`` — ``topic_name`` resolved within the chat.

Covers the success paths (by id and by name), idempotent replay, fresh op on a
new title, plus the failure taxonomy: 503 when the backend is unbuilt, 403 when
the WRITE gate denies, 404 on topic not-found, 409 on ambiguous topic name, and
FLOOD_WAIT translated to 502 (needs_review).
"""

from __future__ import annotations

import tempfile
import textwrap
from pathlib import Path

from fastapi.testclient import TestClient

from telegram_assistant.config import load_config_from_text
from telegram_assistant.folders import FolderChat, FolderSnapshot
from telegram_assistant.http_api import create_app
from telegram_assistant.persistence import OperationStore
from telegram_assistant.topics import TopicSummary
from telegram_assistant.worker.queue import FloodWaitError

AUTH = {"Authorization": "Bearer secret_token"}


def _config_with_access(access_block: str | None) -> str:
    base = textwrap.dedent(
        """
        telegram:
          api_id: 123456
          api_hash: "telegram_api_hash"
          session_path: /data/telegram-assistant.session
          default_chat_folder:
            folder_id: 2
            folder_name: "Planfix clients"
        {access}
        http:
          host: "0.0.0.0"
          port: 8085
          bearer_token: "secret_token"
        logging:
          level: INFO
        """
    )
    indented = ""
    if access_block is not None:
        indented = textwrap.indent(access_block, "  ")
    return base.format(access=indented).strip()


def _make_store() -> OperationStore:
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".db")
    tmp.close()
    return OperationStore(Path(tmp.name))


class FakeTopicBackend:
    """In-memory TopicBackend recording rename/list calls."""

    def __init__(
        self,
        *,
        topics: list[TopicSummary] | None = None,
        rename_error: Exception | None = None,
    ) -> None:
        self._topics = list(topics or [])
        self._rename_error = rename_error
        self.renamed: list[tuple[int, int, str]] = []

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
        self.renamed.append((chat_id, topic_id, title))
        for idx, t in enumerate(self._topics):
            if t.topic_id == topic_id:
                self._topics[idx] = TopicSummary(
                    topic_id=t.topic_id, title=title, closed=t.closed
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


def _client(
    access_block: str | None,
    *,
    backend: FakeTopicBackend | None = None,
    folder_backend: FakeFolderBackend | None = None,
    store: OperationStore | None = None,
) -> TestClient:
    config = load_config_from_text(_config_with_access(access_block))
    if store is None:
        store = _make_store()
    if backend is None:
        factory = lambda _r: None  # noqa: E731
    else:
        factory = lambda _r: backend  # noqa: E731
    app = create_app(
        config,
        session_manager=None,
        topic_backend_factory=factory,
        folder_backend_factory=(
            (lambda _r: folder_backend) if folder_backend is not None else None
        ),
        operation_store=store,
    )
    return TestClient(app)


# ---------------------------------------------------------------------------
# success — by id
# ---------------------------------------------------------------------------


def test_http_rename_by_id_happy_path() -> None:
    backend = FakeTopicBackend(topics=[TopicSummary(topic_id=42, title="Old")])
    client = _client(None, backend=backend)
    resp = client.post(
        "/telegram/topics/42/rename",
        json={"telegram_chat_id": -100, "new_title": "New title"},
        headers=AUTH,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["telegram_chat_id"] == -100
    assert body["telegram_topic_id"] == 42
    assert body["new_title"] == "New title"
    assert body["status"] == "renamed"
    assert body["replayed"] is False
    assert body["operation_status"] == "completed"
    assert "operation_id" in body
    assert backend.renamed == [(-100, 42, "New title")]


def test_http_rename_by_id_replays_on_repeat() -> None:
    store = _make_store()
    backend1 = FakeTopicBackend(topics=[TopicSummary(topic_id=7, title="Old")])
    client1 = _client(None, backend=backend1, store=store)
    r1 = client1.post(
        "/telegram/topics/7/rename",
        json={"telegram_chat_id": -5, "new_title": "Same"},
        headers=AUTH,
    )
    assert r1.status_code == 200, r1.text
    assert r1.json()["replayed"] is False
    assert backend1.renamed == [(-5, 7, "Same")]

    backend2 = FakeTopicBackend()
    client2 = _client(None, backend=backend2, store=store)
    r2 = client2.post(
        "/telegram/topics/7/rename",
        json={"telegram_chat_id": -5, "new_title": "Same"},
        headers=AUTH,
    )
    assert r2.status_code == 200, r2.text
    body = r2.json()
    assert body["new_title"] == "Same"
    assert body["replayed"] is True
    assert backend2.renamed == []


def test_http_rename_new_title_is_fresh_op() -> None:
    store = _make_store()
    backend = FakeTopicBackend(topics=[TopicSummary(topic_id=7, title="Old")])
    client = _client(None, backend=backend, store=store)
    r1 = client.post(
        "/telegram/topics/7/rename",
        json={"telegram_chat_id": -5, "new_title": "First"},
        headers=AUTH,
    )
    assert r1.status_code == 200, r1.text
    r2 = client.post(
        "/telegram/topics/7/rename",
        json={"telegram_chat_id": -5, "new_title": "Second"},
        headers=AUTH,
    )
    assert r2.status_code == 200, r2.text
    assert r2.json()["replayed"] is False
    assert backend.renamed == [(-5, 7, "First"), (-5, 7, "Second")]


# ---------------------------------------------------------------------------
# success — by name
# ---------------------------------------------------------------------------


def test_http_rename_by_name_happy_path() -> None:
    backend = FakeTopicBackend(topics=[TopicSummary(topic_id=11, title="Issue")])
    client = _client(None, backend=backend)
    resp = client.post(
        "/telegram/topics/rename",
        json={
            "telegram_chat_id": -100,
            "topic_name": "Issue",
            "new_title": "Resolved",
        },
        headers=AUTH,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["telegram_topic_id"] == 11
    assert body["new_title"] == "Resolved"
    assert backend.renamed == [(-100, 11, "Resolved")]


def test_http_rename_by_name_resolves_chat_by_name() -> None:
    backend = FakeTopicBackend(topics=[TopicSummary(topic_id=11, title="Issue")])
    folder_backend = FakeFolderBackend(
        [
            FolderSnapshot(
                folder_id=2,
                folder_name="Planfix clients",
                chats=[FolderChat(chat_id=-100, title="Acme")],
            )
        ]
    )
    client = _client(None, backend=backend, folder_backend=folder_backend)
    resp = client.post(
        "/telegram/topics/rename",
        json={
            "chat_name": "Acme",
            "folder_name": "Planfix clients",
            "topic_name": "Issue",
            "new_title": "Resolved",
        },
        headers=AUTH,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["telegram_chat_id"] == -100
    assert backend.renamed == [(-100, 11, "Resolved")]


def test_http_rename_by_name_not_found_404() -> None:
    backend = FakeTopicBackend(topics=[TopicSummary(topic_id=11, title="Issue")])
    client = _client(None, backend=backend)
    resp = client.post(
        "/telegram/topics/rename",
        json={
            "telegram_chat_id": -100,
            "topic_name": "Ghost",
            "new_title": "X",
        },
        headers=AUTH,
    )
    assert resp.status_code == 404, resp.text
    assert backend.renamed == []


def test_http_rename_by_name_ambiguous_409() -> None:
    backend = FakeTopicBackend(
        topics=[
            TopicSummary(topic_id=1, title="Dup"),
            TopicSummary(topic_id=2, title="Dup"),
        ]
    )
    client = _client(None, backend=backend)
    resp = client.post(
        "/telegram/topics/rename",
        json={
            "telegram_chat_id": -100,
            "topic_name": "Dup",
            "new_title": "X",
        },
        headers=AUTH,
    )
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["error"] == "ambiguous_topic_name"
    assert detail["topic_name"] == "Dup"
    assert detail["matches"] == [1, 2]
    assert backend.renamed == []


# ---------------------------------------------------------------------------
# failure modes
# ---------------------------------------------------------------------------


def test_http_rename_503_when_backend_unavailable() -> None:
    client = _client(None, backend=None)
    resp = client.post(
        "/telegram/topics/42/rename",
        json={"telegram_chat_id": -100, "new_title": "X"},
        headers=AUTH,
    )
    assert resp.status_code == 503, resp.text


def test_http_rename_by_name_503_when_backend_unavailable() -> None:
    client = _client(None, backend=None)
    resp = client.post(
        "/telegram/topics/rename",
        json={"telegram_chat_id": -100, "topic_name": "Issue", "new_title": "X"},
        headers=AUTH,
    )
    assert resp.status_code == 503, resp.text


def test_http_rename_denied_when_policy_present() -> None:
    backend = FakeTopicBackend(topics=[TopicSummary(topic_id=42, title="Old")])
    # Deny-by-default: a present-but-empty policy grants nothing.
    client = _client("access:\n  rules: []\n", backend=backend)
    resp = client.post(
        "/telegram/topics/42/rename",
        json={"telegram_chat_id": -100, "new_title": "X"},
        headers=AUTH,
    )
    assert resp.status_code == 403, resp.text
    detail = resp.json()["detail"]
    assert detail["error"] == "access_denied"
    assert detail["required_level"] == "write"
    assert backend.renamed == []


def test_http_rename_read_only_rule_denies_write() -> None:
    backend = FakeTopicBackend(topics=[TopicSummary(topic_id=42, title="Old")])
    access = "access:\n  rules:\n    - all: true\n      permission: read\n"
    client = _client(access, backend=backend)
    resp = client.post(
        "/telegram/topics/42/rename",
        json={"telegram_chat_id": -100, "new_title": "X"},
        headers=AUTH,
    )
    assert resp.status_code == 403, resp.text
    assert backend.renamed == []


def test_http_rename_allowed_by_wildcard_write() -> None:
    backend = FakeTopicBackend(topics=[TopicSummary(topic_id=42, title="Old")])
    access = "access:\n  rules:\n    - all: true\n      permission: write\n"
    client = _client(access, backend=backend)
    resp = client.post(
        "/telegram/topics/42/rename",
        json={"telegram_chat_id": -100, "new_title": "X"},
        headers=AUTH,
    )
    assert resp.status_code == 200, resp.text
    assert backend.renamed == [(-100, 42, "X")]


def test_http_rename_by_name_denied_before_resolution() -> None:
    backend = FakeTopicBackend(topics=[TopicSummary(topic_id=42, title="Issue")])
    client = _client("access:\n  rules: []\n", backend=backend)
    resp = client.post(
        "/telegram/topics/rename",
        json={"telegram_chat_id": -100, "topic_name": "Issue", "new_title": "X"},
        headers=AUTH,
    )
    assert resp.status_code == 403, resp.text
    assert backend.renamed == []


def test_http_rename_flood_wait_returns_502() -> None:
    backend = FakeTopicBackend(
        topics=[TopicSummary(topic_id=42, title="Old")],
        rename_error=FloodWaitError(seconds=5),
    )
    client = _client(None, backend=backend)
    resp = client.post(
        "/telegram/topics/42/rename",
        json={"telegram_chat_id": -100, "new_title": "X"},
        headers=AUTH,
    )
    assert resp.status_code == 502, resp.text
    assert resp.json()["detail"]["error"] == "needs_review"


def test_http_rename_rejects_non_positive_topic_id() -> None:
    backend = FakeTopicBackend()
    client = _client(None, backend=backend)
    resp = client.post(
        "/telegram/topics/0/rename",
        json={"telegram_chat_id": -100, "new_title": "X"},
        headers=AUTH,
    )
    assert resp.status_code == 400, resp.text
    assert backend.renamed == []


def test_http_rename_422_on_missing_title() -> None:
    backend = FakeTopicBackend()
    client = _client(None, backend=backend)
    resp = client.post(
        "/telegram/topics/42/rename",
        json={"telegram_chat_id": -100},
        headers=AUTH,
    )
    assert resp.status_code == 422, resp.text
    assert backend.renamed == []


def test_http_rename_422_on_no_chat_ref() -> None:
    backend = FakeTopicBackend()
    client = _client(None, backend=backend)
    resp = client.post(
        "/telegram/topics/42/rename",
        json={"new_title": "X"},
        headers=AUTH,
    )
    assert resp.status_code == 422, resp.text
    assert backend.renamed == []


def test_http_rename_requires_auth() -> None:
    backend = FakeTopicBackend()
    client = _client(None, backend=backend)
    resp = client.post(
        "/telegram/topics/42/rename",
        json={"telegram_chat_id": -100, "new_title": "X"},
    )
    assert resp.status_code == 401


def test_http_rename_rejects_wrong_token() -> None:
    backend = FakeTopicBackend()
    client = _client(None, backend=backend)
    resp = client.post(
        "/telegram/topics/42/rename",
        json={"telegram_chat_id": -100, "new_title": "X"},
        headers={"Authorization": "Bearer wrong_token"},
    )
    assert resp.status_code == 403
