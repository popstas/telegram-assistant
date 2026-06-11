"""HTTP tests for ``POST /telegram/groups/rename`` (Task 6).

Covers the success path (by chat_id and by entity), idempotent replay, plus the
failure taxonomy: 503 when the backend is unbuilt, 403 when the WRITE gate
denies, 404 on entity not-found, 409 on ambiguous entity, and FLOOD_WAIT
translated to 502 (needs_review).
"""

from __future__ import annotations

import tempfile
import textwrap
from pathlib import Path

from fastapi.testclient import TestClient

from telegram_assistant.config import load_config_from_text
from telegram_assistant.entities import (
    AmbiguousEntityError,
    EntityNotFoundError,
    ResolvedEntity,
)
from telegram_assistant.http_api import create_app
from telegram_assistant.persistence import OperationStore, idempotency
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


class FakeRenameBackend:
    """Minimal GroupBackend exposing only ``set_title``."""

    def __init__(self, *, set_error: Exception | None = None) -> None:
        self._set_error = set_error
        self.set_calls: list[tuple[int, str]] = []

    async def set_title(self, *, chat_id: int, title: str) -> None:
        if self._set_error is not None:
            raise self._set_error
        self.set_calls.append((chat_id, title))


class FakeResolver:
    def __init__(self, *, mapping=None, error: Exception | None = None) -> None:
        self._mapping = mapping or {}
        self._error = error

    async def resolve(self, ref) -> ResolvedEntity:
        if self._error is not None:
            raise self._error
        chat_id = self._mapping[str(ref)]
        return ResolvedEntity(chat_id=chat_id, title=str(ref), kind="channel")


def _client(
    access_block: str | None,
    *,
    backend: FakeRenameBackend | None = None,
    resolver=None,
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
        group_backend_factory=factory,
        folder_backend_factory=lambda _r: None,
        resolver_factory=(lambda _r: resolver)
        if resolver is not None
        else (lambda _r: None),
        operation_store=store,
    )
    return TestClient(app)


# ---------------------------------------------------------------------------
# success
# ---------------------------------------------------------------------------


def test_http_rename_happy_path_by_chat_id() -> None:
    backend = FakeRenameBackend()
    client = _client(None, backend=backend)
    resp = client.post(
        "/telegram/groups/rename",
        json={"chat_id": -100, "new_title": "New title"},
        headers=AUTH,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["telegram_chat_id"] == -100
    assert body["new_title"] == "New title"
    assert body["status"] == "renamed"
    assert body["replayed"] is False
    assert body["operation_status"] == "completed"
    assert "operation_id" in body
    assert backend.set_calls == [(-100, "New title")]


def test_http_rename_via_entity() -> None:
    backend = FakeRenameBackend()
    resolver = FakeResolver(mapping={"@client": -100777})
    client = _client(None, backend=backend, resolver=resolver)
    resp = client.post(
        "/telegram/groups/rename",
        json={"entity": "@client", "new_title": "Renamed"},
        headers=AUTH,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["telegram_chat_id"] == -100777
    assert backend.set_calls == [(-100777, "Renamed")]


def test_http_rename_replays_on_repeat() -> None:
    store = _make_store()
    backend1 = FakeRenameBackend()
    client1 = _client(None, backend=backend1, store=store)
    r1 = client1.post(
        "/telegram/groups/rename",
        json={"chat_id": -7, "new_title": "Same"},
        headers=AUTH,
    )
    assert r1.status_code == 200, r1.text
    assert r1.json()["replayed"] is False
    assert backend1.set_calls == [(-7, "Same")]

    backend2 = FakeRenameBackend()
    client2 = _client(None, backend=backend2, store=store)
    r2 = client2.post(
        "/telegram/groups/rename",
        json={"chat_id": -7, "new_title": "Same"},
        headers=AUTH,
    )
    assert r2.status_code == 200, r2.text
    body = r2.json()
    assert body["new_title"] == "Same"
    assert body["replayed"] is True
    assert backend2.set_calls == []


def test_http_rename_new_title_is_fresh_op() -> None:
    store = _make_store()
    backend = FakeRenameBackend()
    client = _client(None, backend=backend, store=store)
    r1 = client.post(
        "/telegram/groups/rename",
        json={"chat_id": -7, "new_title": "First"},
        headers=AUTH,
    )
    assert r1.status_code == 200, r1.text
    r2 = client.post(
        "/telegram/groups/rename",
        json={"chat_id": -7, "new_title": "Second"},
        headers=AUTH,
    )
    assert r2.status_code == 200, r2.text
    assert r2.json()["replayed"] is False
    assert backend.set_calls == [(-7, "First"), (-7, "Second")]


# ---------------------------------------------------------------------------
# failure modes
# ---------------------------------------------------------------------------


def test_http_rename_503_when_backend_unavailable() -> None:
    client = _client(None, backend=None)
    resp = client.post(
        "/telegram/groups/rename",
        json={"chat_id": -100, "new_title": "X"},
        headers=AUTH,
    )
    assert resp.status_code == 503, resp.text


def test_http_rename_denied_when_policy_present() -> None:
    backend = FakeRenameBackend()
    # Deny-by-default: a present-but-empty policy grants nothing.
    client = _client("access:\n  rules: []\n", backend=backend)
    resp = client.post(
        "/telegram/groups/rename",
        json={"chat_id": -100, "new_title": "X"},
        headers=AUTH,
    )
    assert resp.status_code == 403, resp.text
    detail = resp.json()["detail"]
    assert detail["error"] == "access_denied"
    assert detail["required_level"] == "write"
    assert backend.set_calls == []


def test_http_rename_read_only_rule_denies_write() -> None:
    backend = FakeRenameBackend()
    access = "access:\n  rules:\n    - all: true\n      permission: read\n"
    client = _client(access, backend=backend)
    resp = client.post(
        "/telegram/groups/rename",
        json={"chat_id": -100, "new_title": "X"},
        headers=AUTH,
    )
    assert resp.status_code == 403, resp.text
    assert backend.set_calls == []


def test_http_rename_allowed_by_wildcard_write() -> None:
    backend = FakeRenameBackend()
    access = "access:\n  rules:\n    - all: true\n      permission: write\n"
    client = _client(access, backend=backend)
    resp = client.post(
        "/telegram/groups/rename",
        json={"chat_id": -100, "new_title": "X"},
        headers=AUTH,
    )
    assert resp.status_code == 200, resp.text
    assert backend.set_calls == [(-100, "X")]


def test_http_rename_entity_not_found_404() -> None:
    backend = FakeRenameBackend()
    resolver = FakeResolver(error=EntityNotFoundError("nope"))
    client = _client(None, backend=backend, resolver=resolver)
    resp = client.post(
        "/telegram/groups/rename",
        json={"entity": "@ghost", "new_title": "X"},
        headers=AUTH,
    )
    assert resp.status_code == 404, resp.text
    assert backend.set_calls == []


def test_http_rename_entity_ambiguous_409() -> None:
    backend = FakeRenameBackend()
    resolver = FakeResolver(error=AmbiguousEntityError(ref="Team", matches=[1, 2]))
    client = _client(None, backend=backend, resolver=resolver)
    resp = client.post(
        "/telegram/groups/rename",
        json={"entity": "Team", "new_title": "X"},
        headers=AUTH,
    )
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["error"] == "ambiguous_entity"
    assert detail["matches"] == [1, 2]


def test_http_rename_previous_failed_returns_409() -> None:
    """A pre-seeded failed op under the target-title key surfaces
    ``previous_attempt_failed`` (409) without touching the backend."""
    store = _make_store()
    key = idempotency.group_rename_key(telegram_chat_id=-99, new_title="X")
    begin = store.begin_operation(
        operation_type=idempotency.GROUP_RENAME,
        idempotency_key=key,
        request_payload={"telegram_chat_id": -99, "new_title": "X"},
    )
    store.fail_operation(begin.operation.id, "nope")

    backend = FakeRenameBackend()
    client = _client(None, backend=backend, store=store)
    resp = client.post(
        "/telegram/groups/rename",
        json={"chat_id": -99, "new_title": "X"},
        headers=AUTH,
    )
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["error"] == "previous_attempt_failed"
    assert backend.set_calls == []


def test_http_rename_pending_returns_409() -> None:
    """A still-pending op under the target-title key surfaces ``pending``
    (409) without touching the backend."""
    store = _make_store()
    key = idempotency.group_rename_key(telegram_chat_id=-99, new_title="X")
    store.begin_operation(
        operation_type=idempotency.GROUP_RENAME,
        idempotency_key=key,
        request_payload={"telegram_chat_id": -99, "new_title": "X"},
    )

    backend = FakeRenameBackend()
    client = _client(None, backend=backend, store=store)
    resp = client.post(
        "/telegram/groups/rename",
        json={"chat_id": -99, "new_title": "X"},
        headers=AUTH,
    )
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["error"] == "pending"
    assert backend.set_calls == []


def test_http_rename_flood_wait_returns_502() -> None:
    backend = FakeRenameBackend(set_error=FloodWaitError(seconds=5))
    client = _client(None, backend=backend)
    resp = client.post(
        "/telegram/groups/rename",
        json={"chat_id": -100, "new_title": "X"},
        headers=AUTH,
    )
    assert resp.status_code == 502, resp.text
    assert resp.json()["detail"]["error"] == "needs_review"


def test_http_rename_422_on_missing_title() -> None:
    backend = FakeRenameBackend()
    client = _client(None, backend=backend)
    resp = client.post(
        "/telegram/groups/rename",
        json={"chat_id": -100},
        headers=AUTH,
    )
    assert resp.status_code == 422, resp.text
    assert backend.set_calls == []


def test_http_rename_422_on_no_chat_ref() -> None:
    backend = FakeRenameBackend()
    client = _client(None, backend=backend)
    resp = client.post(
        "/telegram/groups/rename",
        json={"new_title": "X"},
        headers=AUTH,
    )
    assert resp.status_code == 422, resp.text
    assert backend.set_calls == []


def test_http_rename_requires_auth() -> None:
    backend = FakeRenameBackend()
    client = _client(None, backend=backend)
    resp = client.post(
        "/telegram/groups/rename",
        json={"chat_id": -100, "new_title": "X"},
    )
    assert resp.status_code == 401


def test_http_rename_rejects_wrong_token() -> None:
    backend = FakeRenameBackend()
    client = _client(None, backend=backend)
    resp = client.post(
        "/telegram/groups/rename",
        json={"chat_id": -100, "new_title": "X"},
        headers={"Authorization": "Bearer wrong_token"},
    )
    assert resp.status_code == 403
