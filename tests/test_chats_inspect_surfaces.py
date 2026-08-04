"""Surface tests for `chats inspect` — HTTP and MCP wiring.

The domain op is covered by ``test_chats_inspect.py``, the Telethon adapter by
``test_chats_inspect_backend.py`` and the CLI by ``test_cli_chats_inspect.py``.
This module covers the two *remote* surfaces: parameter validation, the payload
shape, the CLI-only ``raw`` rejection and the status / error taxonomy.
"""

from __future__ import annotations

import tempfile
import textwrap
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from telegram_assistant.chats import ChatInfo
from telegram_assistant.config import load_config_from_text
from telegram_assistant.entities import EntityNotFoundError, ResolvedEntity
from telegram_assistant.folders import FolderChat, FolderSnapshot
from telegram_assistant.http_api import create_app
from telegram_assistant.persistence import OperationStore
from telegram_assistant.worker.queue import FloodWaitError

AUTH = {"Authorization": "Bearer secret_token"}

#: The chat the fakes answer for. Bare id, no ``-100`` marker — that is what
#: ``ChatInfo.chat_id`` carries and what the payload must report.
CHAT_ID = 2305069221


def _chat_info(chat_id: int = CHAT_ID) -> ChatInfo:
    """A canned supergroup payload touching one field per payload group."""
    return ChatInfo(
        chat_id=chat_id,
        kind="supergroup",
        title="Client chat",
        username="clientchat",
        usernames=("clientchat",),
        about="A client chat",
        created_at=datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC),
        ttl_period=86400,
        megagroup=True,
        is_forum=True,
        topics_layout="tabs",
        participants_count=12,
    )


class FakeInspectBackend:
    """Records every call; returns a canned ``ChatInfo`` or raises ``error``."""

    def __init__(
        self, info: ChatInfo | None = None, *, error: Exception | None = None
    ) -> None:
        self._info = _chat_info() if info is None else info
        self._error = error
        self.calls: list[dict[str, Any]] = []

    async def inspect_chat(self, *, chat_id: int, raw: bool) -> ChatInfo:
        self.calls.append({"chat_id": chat_id, "raw": raw})
        if self._error is not None:
            raise self._error
        return self._info


class FakeResolver:
    def __init__(self, mapping: dict[str, int]) -> None:
        self._mapping = mapping

    async def resolve(self, ref: object) -> ResolvedEntity:
        key = str(ref)
        if key not in self._mapping:
            raise EntityNotFoundError(f"entity {key!r} not found")
        return ResolvedEntity(chat_id=self._mapping[key], title=key, kind="channel")


class FakeFolderBackend:
    """Just enough of ``FolderBackend`` for ``resolve_chat_in_folder``."""

    def __init__(self, snapshots: list[FolderSnapshot] | None = None) -> None:
        self._snapshots = [] if snapshots is None else snapshots

    async def list_folders(self) -> list[FolderSnapshot]:
        return list(self._snapshots)


def _folder_backend() -> FakeFolderBackend:
    return FakeFolderBackend(
        [
            FolderSnapshot(
                folder_id=2,
                folder_name="Planfix clients",
                chats=[FolderChat(chat_id=CHAT_ID, title="Client chat")],
            )
        ]
    )


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


_READ_ACCESS = "access:\n  rules:\n    - all: true\n      permission: read\n"
_WRITE_ACCESS = "access:\n  rules:\n    - all: true\n      permission: write\n"


def _make_store() -> OperationStore:
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".db")
    tmp.close()
    return OperationStore(Path(tmp.name))


def _http_client(
    *,
    access_block: str | None = None,
    backend: FakeInspectBackend | None = None,
    resolver: FakeResolver | None = None,
    folder_backend: FakeFolderBackend | None = None,
    has_factory: bool = True,
) -> TestClient:
    config = load_config_from_text(_config_with_access(access_block))
    app = create_app(
        config,
        session_manager=None,
        chat_inspect_backend_factory=(
            (lambda _r: backend) if has_factory else (lambda _r: None)
        ),
        folder_backend_factory=(
            (lambda _r: folder_backend)
            if folder_backend is not None
            else (lambda _r: None)
        ),
        resolver_factory=(
            (lambda _r: resolver) if resolver is not None else (lambda _r: None)
        ),
        operation_store=_make_store(),
    )
    return TestClient(app)


# --- HTTP ------------------------------------------------------------------


def test_http_chats_inspect_returns_the_domain_payload() -> None:
    backend = FakeInspectBackend()
    client = _http_client(access_block=_READ_ACCESS, backend=backend)

    resp = client.get(
        "/telegram/chats/inspect", params={"chat_id": CHAT_ID}, headers=AUTH
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    # Exactly ChatInfo.to_dict() — no wrapper keys, no echoed parameters.
    assert body["chat_id"] == CHAT_ID
    assert body["kind"] == "supergroup"
    assert body["ttl_period"] == 86400
    assert body["topics_layout"] == "tabs"
    assert body["participants_count"] == 12
    assert body["created_at"].startswith("2026-01-02T03:04:05")
    assert body["usernames"] == ["clientchat"]
    # Fields that do not apply to a supergroup are present and null.
    assert body["phone"] is None
    # `raw` is dropped entirely, never sent as null.
    assert "raw" not in body
    assert "telegram_chat_id" not in body
    # The remote surface never asks the backend for the raw objects.
    assert backend.calls == [{"chat_id": CHAT_ID, "raw": False}]


def test_http_chats_inspect_resolves_entity() -> None:
    backend = FakeInspectBackend()
    client = _http_client(
        access_block=_READ_ACCESS,
        backend=backend,
        resolver=FakeResolver({"@clientchat": CHAT_ID}),
    )

    resp = client.get(
        "/telegram/chats/inspect", params={"entity": "@clientchat"}, headers=AUTH
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["chat_id"] == CHAT_ID
    assert backend.calls == [{"chat_id": CHAT_ID, "raw": False}]


def test_http_chats_inspect_resolves_chat_name_in_folder() -> None:
    backend = FakeInspectBackend()
    client = _http_client(
        access_block=_READ_ACCESS,
        backend=backend,
        folder_backend=_folder_backend(),
    )

    resp = client.get(
        "/telegram/chats/inspect",
        params={"chat_name": "Client chat", "folder_name": "Planfix clients"},
        headers=AUTH,
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["chat_id"] == CHAT_ID
    assert backend.calls == [{"chat_id": CHAT_ID, "raw": False}]


def test_http_chats_inspect_reports_a_missing_entity_as_404() -> None:
    backend = FakeInspectBackend()
    client = _http_client(
        access_block=_READ_ACCESS,
        backend=backend,
        resolver=FakeResolver({}),
    )

    resp = client.get(
        "/telegram/chats/inspect", params={"entity": "@nope"}, headers=AUTH
    )

    assert resp.status_code == 404, resp.text
    assert backend.calls == []


def test_http_chats_inspect_reports_a_missing_chat_name_as_404() -> None:
    backend = FakeInspectBackend()
    client = _http_client(
        access_block=_READ_ACCESS,
        backend=backend,
        folder_backend=_folder_backend(),
    )

    resp = client.get(
        "/telegram/chats/inspect",
        params={"chat_name": "Other chat", "folder_name": "Planfix clients"},
        headers=AUTH,
    )

    assert resp.status_code == 404, resp.text
    assert backend.calls == []


def test_http_chats_inspect_requires_exactly_one_ref() -> None:
    backend = FakeInspectBackend()
    client = _http_client(access_block=_READ_ACCESS, backend=backend)

    none_given = client.get("/telegram/chats/inspect", headers=AUTH)
    two_given = client.get(
        "/telegram/chats/inspect",
        params={"chat_id": CHAT_ID, "entity": "@clientchat"},
        headers=AUTH,
    )

    assert none_given.status_code == 400, none_given.text
    assert "exactly one" in none_given.json()["detail"]
    assert two_given.status_code == 400, two_given.text
    assert backend.calls == []


def test_http_chats_inspect_requires_folder_name_with_chat_name() -> None:
    backend = FakeInspectBackend()
    client = _http_client(
        access_block=_READ_ACCESS,
        backend=backend,
        folder_backend=_folder_backend(),
    )

    resp = client.get(
        "/telegram/chats/inspect", params={"chat_name": "Client chat"}, headers=AUTH
    )

    assert resp.status_code == 400, resp.text
    assert resp.json()["detail"] == "chat_name requires folder_name"
    assert backend.calls == []


def test_http_chats_inspect_rejects_raw() -> None:
    """`raw` is CLI-only — rejected, never silently ignored."""
    backend = FakeInspectBackend()
    client = _http_client(access_block=_READ_ACCESS, backend=backend)

    resp = client.get(
        "/telegram/chats/inspect",
        params={"chat_id": CHAT_ID, "raw": "true"},
        headers=AUTH,
    )

    assert resp.status_code == 400, resp.text
    detail = resp.json()["detail"]
    assert "raw is CLI-only" in detail
    # The rejection happens before anything reaches the domain op, so a remote
    # caller's flag can never reach the serializer.
    assert backend.calls == []


def test_http_chats_inspect_raw_false_is_accepted() -> None:
    """An explicit `raw=false` is a normal request, not a rejection."""
    backend = FakeInspectBackend()
    client = _http_client(access_block=_READ_ACCESS, backend=backend)

    resp = client.get(
        "/telegram/chats/inspect",
        params={"chat_id": CHAT_ID, "raw": "false"},
        headers=AUTH,
    )

    assert resp.status_code == 200, resp.text
    assert backend.calls == [{"chat_id": CHAT_ID, "raw": False}]


def test_http_chats_inspect_denied_without_read() -> None:
    backend = FakeInspectBackend()
    client = _http_client(access_block=_WRITE_ACCESS, backend=backend)

    resp = client.get(
        "/telegram/chats/inspect", params={"chat_id": CHAT_ID}, headers=AUTH
    )

    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"]["error"] == "access_denied"
    # The gate runs before any Telegram call.
    assert backend.calls == []


def test_http_chats_inspect_503_without_backend() -> None:
    client = _http_client(access_block=_READ_ACCESS, has_factory=False)

    resp = client.get(
        "/telegram/chats/inspect", params={"chat_id": CHAT_ID}, headers=AUTH
    )

    assert resp.status_code == 503, resp.text


def test_http_chats_inspect_rejects_an_uninspectable_peer_with_400() -> None:
    backend = FakeInspectBackend(
        error=ValueError(f"chat {CHAT_ID} is private or inaccessible")
    )
    client = _http_client(access_block=_READ_ACCESS, backend=backend)

    resp = client.get(
        "/telegram/chats/inspect", params={"chat_id": CHAT_ID}, headers=AUTH
    )

    assert resp.status_code == 400, resp.text
    assert resp.json()["detail"] == f"chat {CHAT_ID} is private or inaccessible"


def test_http_chats_inspect_maps_flood_wait_to_502_with_retry_after() -> None:
    backend = FakeInspectBackend(error=FloodWaitError(30.0))
    client = _http_client(access_block=_READ_ACCESS, backend=backend)

    resp = client.get(
        "/telegram/chats/inspect", params={"chat_id": CHAT_ID}, headers=AUTH
    )

    assert resp.status_code == 502, resp.text
    detail = resp.json()["detail"]
    assert detail["error"] == "needs_review"
    assert detail["retry_after_seconds"] == 30.0
    assert detail["retry_at"] > 0
    assert resp.headers["Retry-After"] == "30"


def test_http_chats_inspect_requires_a_bearer_token() -> None:
    client = _http_client(access_block=_READ_ACCESS, backend=FakeInspectBackend())

    resp = client.get("/telegram/chats/inspect", params={"chat_id": CHAT_ID})

    # A missing Authorization header is 401 on every /telegram/* route
    # (tests/test_app_skeleton.py::test_protected_endpoint_requires_authorization_header).
    assert resp.status_code == 401, resp.text
