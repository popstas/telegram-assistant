"""HTTP tests for ``/telegram/groups/layout`` (Task 6).

Covers the success path for both methods plus failure modes: 503 on unbuilt
backend, 422 on invalid layout enum, 401/403 when bearer token is missing or
wrong, and FLOOD_WAIT translated to 502.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

from telegram_assistant.config import load_config_from_text
from telegram_assistant.http_api import create_app
from telegram_assistant.persistence import (
    OperationStatus,
    OperationStore,
    idempotency,
)
from telegram_assistant.worker.queue import FloodWaitError


class FakeLayoutBackend:
    """Minimal GroupBackend exposing only the layout methods used here."""

    def __init__(
        self,
        *,
        forum_tabs: bool = False,
        set_error: Exception | None = None,
        get_error: Exception | None = None,
    ) -> None:
        self.forum_tabs = forum_tabs
        self._set_error = set_error
        self._get_error = get_error
        self.set_calls: list[tuple[int, bool]] = []
        self.get_calls: list[int] = []

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
        if self._set_error is not None:
            raise self._set_error
        self.set_calls.append((chat_id, tabs))
        self.forum_tabs = tabs

    async def get_topics_layout(self, *, chat_id: int) -> bool:
        self.get_calls.append(chat_id)
        if self._get_error is not None:
            raise self._get_error
        return self.forum_tabs

    async def set_default_permissions(
        self,
        *,
        chat_id: int,
        allow_create_topics: bool,
        allow_pin_messages: bool,
    ) -> None:
        raise NotImplementedError


def _make_store() -> OperationStore:
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".db")
    tmp.close()
    return OperationStore(Path(tmp.name))


def _http_client(
    minimal_config_yaml: str,
    backend: FakeLayoutBackend | None,
    *,
    store: OperationStore | None = None,
) -> TestClient:
    config = load_config_from_text(minimal_config_yaml)
    if store is None:
        store = _make_store()
    if backend is None:
        factory = lambda _request: None  # noqa: E731
    else:
        factory = lambda _request: backend  # noqa: E731
    app = create_app(
        config,
        session_manager=None,
        group_backend_factory=factory,
        folder_backend_factory=lambda _request: None,
        operation_store=store,
    )
    return TestClient(app)


# ---------------------------------------------------------------------------
# POST /telegram/groups/layout — success
# ---------------------------------------------------------------------------


def test_http_set_layout_happy_path_tabs(minimal_config_yaml: str) -> None:
    backend = FakeLayoutBackend(forum_tabs=False)
    client = _http_client(minimal_config_yaml, backend)
    resp = client.post(
        "/telegram/groups/layout",
        json={"chat_id": -100, "layout": "tabs"},
        headers={"Authorization": "Bearer secret_token"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["telegram_chat_id"] == -100
    assert body["layout"] == "tabs"
    assert body["replayed"] is False
    assert body["operation_status"] == "completed"
    assert "operation_id" in body
    assert backend.set_calls == [(-100, True)]


def test_http_set_layout_happy_path_list(minimal_config_yaml: str) -> None:
    backend = FakeLayoutBackend(forum_tabs=True)
    client = _http_client(minimal_config_yaml, backend)
    resp = client.post(
        "/telegram/groups/layout",
        json={"chat_id": -200, "layout": "list"},
        headers={"Authorization": "Bearer secret_token"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["layout"] == "list"
    assert backend.set_calls == [(-200, False)]


def test_http_set_layout_replays_on_repeat(minimal_config_yaml: str) -> None:
    store = _make_store()
    backend1 = FakeLayoutBackend()
    client1 = _http_client(minimal_config_yaml, backend1, store=store)
    r1 = client1.post(
        "/telegram/groups/layout",
        json={"chat_id": -7, "layout": "tabs"},
        headers={"Authorization": "Bearer secret_token"},
    )
    assert r1.status_code == 200, r1.text
    assert r1.json()["replayed"] is False
    assert backend1.set_calls == [(-7, True)]

    backend2 = FakeLayoutBackend()
    client2 = _http_client(minimal_config_yaml, backend2, store=store)
    r2 = client2.post(
        "/telegram/groups/layout",
        json={"chat_id": -7, "layout": "tabs"},
        headers={"Authorization": "Bearer secret_token"},
    )
    assert r2.status_code == 200, r2.text
    body = r2.json()
    assert body["layout"] == "tabs"
    assert body["replayed"] is True
    assert backend2.set_calls == []


# ---------------------------------------------------------------------------
# GET /telegram/groups/layout — success
# ---------------------------------------------------------------------------


def test_http_get_layout_returns_tabs(minimal_config_yaml: str) -> None:
    backend = FakeLayoutBackend(forum_tabs=True)
    client = _http_client(minimal_config_yaml, backend)
    resp = client.get(
        "/telegram/groups/layout",
        params={"chat_id": -100},
        headers={"Authorization": "Bearer secret_token"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body == {"chat_id": -100, "layout": "tabs"}
    assert backend.get_calls == [-100]


def test_http_get_layout_returns_list(minimal_config_yaml: str) -> None:
    backend = FakeLayoutBackend(forum_tabs=False)
    client = _http_client(minimal_config_yaml, backend)
    resp = client.get(
        "/telegram/groups/layout",
        params={"chat_id": -200},
        headers={"Authorization": "Bearer secret_token"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"chat_id": -200, "layout": "list"}


def test_http_get_layout_returns_502_on_flood_wait(minimal_config_yaml: str) -> None:
    from telegram_assistant.worker.queue import FloodWaitError

    backend = FakeLayoutBackend(get_error=FloodWaitError(seconds=12))
    client = _http_client(minimal_config_yaml, backend)
    resp = client.get(
        "/telegram/groups/layout",
        params={"chat_id": -300},
        headers={"Authorization": "Bearer secret_token"},
    )
    assert resp.status_code == 502, resp.text
    detail = resp.json()["detail"]
    assert detail["error"] == "needs_review"


# ---------------------------------------------------------------------------
# Failure modes
# ---------------------------------------------------------------------------


def test_http_set_layout_503_when_backend_unavailable(
    minimal_config_yaml: str,
) -> None:
    client = _http_client(minimal_config_yaml, None)
    resp = client.post(
        "/telegram/groups/layout",
        json={"chat_id": -100, "layout": "tabs"},
        headers={"Authorization": "Bearer secret_token"},
    )
    assert resp.status_code == 503, resp.text


def test_http_get_layout_503_when_backend_unavailable(
    minimal_config_yaml: str,
) -> None:
    client = _http_client(minimal_config_yaml, None)
    resp = client.get(
        "/telegram/groups/layout",
        params={"chat_id": -100},
        headers={"Authorization": "Bearer secret_token"},
    )
    assert resp.status_code == 503, resp.text


def test_http_set_layout_422_on_invalid_enum(minimal_config_yaml: str) -> None:
    backend = FakeLayoutBackend()
    client = _http_client(minimal_config_yaml, backend)
    resp = client.post(
        "/telegram/groups/layout",
        json={"chat_id": -100, "layout": "grid"},
        headers={"Authorization": "Bearer secret_token"},
    )
    assert resp.status_code == 422, resp.text
    assert backend.set_calls == []


def test_http_set_layout_422_on_missing_chat_id(minimal_config_yaml: str) -> None:
    backend = FakeLayoutBackend()
    client = _http_client(minimal_config_yaml, backend)
    resp = client.post(
        "/telegram/groups/layout",
        json={"layout": "tabs"},
        headers={"Authorization": "Bearer secret_token"},
    )
    assert resp.status_code == 422, resp.text


def test_http_get_layout_422_on_missing_chat_id(minimal_config_yaml: str) -> None:
    backend = FakeLayoutBackend()
    client = _http_client(minimal_config_yaml, backend)
    resp = client.get(
        "/telegram/groups/layout",
        headers={"Authorization": "Bearer secret_token"},
    )
    assert resp.status_code == 422, resp.text


def test_http_set_layout_requires_auth(minimal_config_yaml: str) -> None:
    backend = FakeLayoutBackend()
    client = _http_client(minimal_config_yaml, backend)
    resp = client.post(
        "/telegram/groups/layout",
        json={"chat_id": -100, "layout": "tabs"},
    )
    assert resp.status_code == 401


def test_http_get_layout_requires_auth(minimal_config_yaml: str) -> None:
    backend = FakeLayoutBackend()
    client = _http_client(minimal_config_yaml, backend)
    resp = client.get(
        "/telegram/groups/layout",
        params={"chat_id": -100},
    )
    assert resp.status_code == 401


def test_http_set_layout_rejects_wrong_token(minimal_config_yaml: str) -> None:
    backend = FakeLayoutBackend()
    client = _http_client(minimal_config_yaml, backend)
    resp = client.post(
        "/telegram/groups/layout",
        json={"chat_id": -100, "layout": "tabs"},
        headers={"Authorization": "Bearer wrong_token"},
    )
    assert resp.status_code == 403


def test_http_get_layout_rejects_wrong_token(minimal_config_yaml: str) -> None:
    backend = FakeLayoutBackend()
    client = _http_client(minimal_config_yaml, backend)
    resp = client.get(
        "/telegram/groups/layout",
        params={"chat_id": -100},
        headers={"Authorization": "Bearer wrong_token"},
    )
    assert resp.status_code == 403


def test_http_set_layout_flood_wait_returns_502(
    minimal_config_yaml: str,
) -> None:
    store = _make_store()
    backend = FakeLayoutBackend(set_error=FloodWaitError(seconds=5))
    client = _http_client(minimal_config_yaml, backend, store=store)
    resp = client.post(
        "/telegram/groups/layout",
        json={"chat_id": -100, "layout": "tabs"},
        headers={"Authorization": "Bearer secret_token"},
    )
    assert resp.status_code == 502, resp.text
    detail = resp.json()["detail"]
    assert detail["error"] == "needs_review"


def test_http_set_layout_previous_failed_returns_409(
    minimal_config_yaml: str,
) -> None:
    """Pre-seed a failed operation in the store and verify the endpoint
    surfaces ``previous_attempt_failed`` (409) on a retry, without touching the
    backend."""
    store = _make_store()
    key = idempotency.group_layout_set_key(
        telegram_chat_id=-99, layout="tabs"
    )
    begin = store.begin_operation(
        operation_type=idempotency.GROUP_LAYOUT_SET,
        idempotency_key=key,
        request_payload={"telegram_chat_id": -99, "layout": "tabs"},
    )
    store.fail_operation(begin.operation.id, "nope")

    backend = FakeLayoutBackend()
    client = _http_client(minimal_config_yaml, backend, store=store)
    resp = client.post(
        "/telegram/groups/layout",
        json={"chat_id": -99, "layout": "tabs"},
        headers={"Authorization": "Bearer secret_token"},
    )
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["error"] == "previous_attempt_failed"
    assert backend.set_calls == []


def test_http_set_layout_completed_op_status(minimal_config_yaml: str) -> None:
    backend = FakeLayoutBackend()
    store = _make_store()
    client = _http_client(minimal_config_yaml, backend, store=store)
    resp = client.post(
        "/telegram/groups/layout",
        json={"chat_id": -100, "layout": "tabs"},
        headers={"Authorization": "Bearer secret_token"},
    )
    assert resp.status_code == 200
    op_id = resp.json()["operation_id"]
    record = store.get_operation(op_id)
    assert record is not None
    assert record.status is OperationStatus.COMPLETED
