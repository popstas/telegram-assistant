"""Surface tests for `chats inspect` — HTTP and MCP wiring.

The domain op is covered by ``test_chats_inspect.py``, the Telethon adapter by
``test_chats_inspect_backend.py`` and the CLI by ``test_cli_chats_inspect.py``.
This module covers the two *remote* surfaces: parameter validation, the payload
shape, the CLI-only ``raw`` rejection and the status / error taxonomy.
"""

from __future__ import annotations

import json
import tempfile
import textwrap
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from telegram_assistant.chats import ChatInfo
from telegram_assistant.config import load_config_from_text
from telegram_assistant.entities import (
    AmbiguousEntityError,
    EntityNotFoundError,
    ResolvedEntity,
)
from telegram_assistant.folders import FolderChat, FolderSnapshot
from telegram_assistant.http_api import create_app
from telegram_assistant.persistence import OperationStore
from telegram_assistant.worker.queue import FloodWaitError
from tests.test_mcp_mount import (
    FakeGoogleOidcProvider,
    FakeSessionManager,
    _enabled_mcp_yaml,
    _initialize_payload,
    _list_tools,
    _mcp_headers,
    _mint_token,
)

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


def test_http_chats_inspect_reports_an_ambiguous_entity_as_409() -> None:
    """An ``entity`` matching several dialogs (e.g. a duplicate title) is 409.

    A dedicated resolver stand-in rather than ``FakeResolver`` (whose mapping
    only ever answers "found" or "not found") — mirrors the resolver
    ``test_http_groups_rename.py::test_http_rename_entity_ambiguous_409`` uses
    for the same shared ``resolve_entity_chat_id`` → ``AmbiguousEntityError``
    → 409 path.
    """
    backend = FakeInspectBackend()

    class _AmbiguousResolver:
        async def resolve(self, ref: object) -> ResolvedEntity:
            raise AmbiguousEntityError(ref=str(ref), matches=[111, 222])

    client = _http_client(
        access_block=_READ_ACCESS, backend=backend, resolver=_AmbiguousResolver()
    )

    resp = client.get(
        "/telegram/chats/inspect", params={"entity": "Client chat"}, headers=AUTH
    )

    assert resp.status_code == 409, resp.text
    assert backend.calls == []


def test_http_chats_inspect_reports_an_ambiguous_chat_name_as_409() -> None:
    """Two chats sharing a title inside the same folder is a *different* 409
    source from an ambiguous ``entity`` — a different exception
    (``AmbiguousChatNameError``, raised inside ``resolve_chat_in_folder``,
    ``folders/service.py``), caught by a different handler
    (``_translate_folder_error``, not ``translate_entity_error``), and a
    different response-body shape (``error: "ambiguous_chat_name"`` plus
    ``chat_name``/``folder_name``/``matches`` — not ``ambiguous_entity`` plus
    ``entity``/``matches``). A test that only checked the status code would
    not catch the two being collapsed into one.
    """
    backend = FakeInspectBackend()
    dup_folder = FakeFolderBackend(
        [
            FolderSnapshot(
                folder_id=2,
                folder_name="Planfix clients",
                chats=[
                    FolderChat(chat_id=CHAT_ID, title="Client chat"),
                    FolderChat(chat_id=CHAT_ID + 1, title="Client chat"),
                ],
            )
        ]
    )
    client = _http_client(
        access_block=_READ_ACCESS, backend=backend, folder_backend=dup_folder
    )

    resp = client.get(
        "/telegram/chats/inspect",
        params={"chat_name": "Client chat", "folder_name": "Planfix clients"},
        headers=AUTH,
    )

    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["error"] == "ambiguous_chat_name"
    assert detail["chat_name"] == "Client chat"
    assert detail["folder_name"] == "Planfix clients"
    assert sorted(detail["matches"]) == sorted([CHAT_ID, CHAT_ID + 1])
    assert backend.calls == []


def test_http_chats_inspect_reports_a_folder_id_mismatch_as_409() -> None:
    """A third, still-distinguishable 409 shape: the folder matched by name
    exists, but its numeric id disagrees with the caller's ``folder_id``
    (``FolderIdMismatchError``, plain-string body) — neither the
    ``ambiguous_entity`` nor the ``ambiguous_chat_name`` dict shape.
    """
    backend = FakeInspectBackend()
    client = _http_client(
        access_block=_READ_ACCESS,
        backend=backend,
        folder_backend=_folder_backend(),  # folder_id=2
    )

    resp = client.get(
        "/telegram/chats/inspect",
        params={
            "chat_name": "Client chat",
            "folder_name": "Planfix clients",
            "folder_id": 999,
        },
        headers=AUTH,
    )

    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert isinstance(detail, str)
    assert backend.calls == []


def test_http_chats_inspect_reports_a_missing_folder_as_404() -> None:
    """The folder itself not existing (``FolderNotFoundError``) is a distinct
    404 source from the chat not being found *inside* an existing folder
    (``ChatNotFoundError``, covered by
    ``test_http_chats_inspect_reports_a_missing_chat_name_as_404``).
    """
    backend = FakeInspectBackend()
    client = _http_client(
        access_block=_READ_ACCESS,
        backend=backend,
        folder_backend=_folder_backend(),  # only has "Planfix clients"
    )

    resp = client.get(
        "/telegram/chats/inspect",
        params={"chat_name": "Client chat", "folder_name": "Nonexistent folder"},
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


def test_http_chats_inspect_503_without_resolver_for_entity() -> None:
    """A distinct 503 source from the chat-inspect backend's own: the
    inspect backend is wired (so ``_chat_inspect_backend_or_503`` passes),
    but no entity resolver is — so resolution itself 503s before the
    inspect backend is ever called.
    """
    backend = FakeInspectBackend()
    client = _http_client(access_block=_READ_ACCESS, backend=backend)

    resp = client.get(
        "/telegram/chats/inspect", params={"entity": "@clientchat"}, headers=AUTH
    )

    assert resp.status_code == 503, resp.text
    assert backend.calls == []


def test_http_chats_inspect_503_without_folder_backend_for_chat_name() -> None:
    """Same shape as the resolver case, for the ``chat_name`` branch: the
    inspect backend is wired, but no folder backend is, so ``chat_name``
    resolution 503s before the inspect backend is ever called.
    """
    backend = FakeInspectBackend()
    client = _http_client(access_block=_READ_ACCESS, backend=backend)

    resp = client.get(
        "/telegram/chats/inspect",
        params={"chat_name": "Client chat", "folder_name": "Planfix clients"},
        headers=AUTH,
    )

    assert resp.status_code == 503, resp.text
    assert backend.calls == []


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


# --- MCP -------------------------------------------------------------------


def _with_access(minimal_config_yaml: str, access_block: str) -> str:
    """Splice an `access:` block into the shared minimal config fixture."""
    return minimal_config_yaml.replace(
        "  defaults:\n",
        f"  access:\n{access_block}  defaults:\n",
        1,
    )


_MCP_READ_ACCESS = "    rules:\n      - all: true\n        permission: read\n"
_MCP_WRITE_ACCESS = "    rules:\n      - all: true\n        permission: write\n"


def _mcp_client(
    config_yaml: str,
    tmp_path: Path,
    *,
    backend: FakeInspectBackend | None = None,
    resolver: FakeResolver | None = None,
    folder_backend: FakeFolderBackend | None = None,
    disabled_tools: tuple[str, ...] = (),
) -> TestClient:
    config = load_config_from_text(
        _enabled_mcp_yaml(config_yaml, disabled_tools=disabled_tools)
    )
    app = create_app(
        config,
        session_manager=FakeSessionManager(),  # type: ignore[arg-type]
        mcp_google_provider=FakeGoogleOidcProvider(),
        chat_inspect_backend_factory=(
            (lambda _r: backend) if backend is not None else (lambda _r: None)
        ),
        folder_backend_factory=(
            (lambda _r: folder_backend)
            if folder_backend is not None
            else (lambda _r: None)
        ),
        resolver_factory=lambda _r: resolver,
        operation_store=OperationStore(tmp_path / "state.db"),
    )
    return TestClient(app)


def _initialize(client: TestClient, token: str) -> None:
    headers = _mcp_headers(token)
    initialize = client.post("/mcp", json=_initialize_payload(), headers=headers)
    assert initialize.status_code == 200, initialize.text
    initialized = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        headers=headers,
    )
    assert initialized.status_code == 202, initialized.text


def _call_tool(
    client: TestClient, token: str, name: str, arguments: dict[str, Any]
) -> dict[str, Any]:
    response = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 7,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
        headers=_mcp_headers(token),
    )
    assert response.status_code == 200, response.text
    return response.json()["result"]


def _error_payload(result: dict[str, Any]) -> dict[str, Any]:
    """The JSON error body an MCP tool failure carries.

    The mcp SDK's ``Tool.run`` (``mcp/server/fastmcp/tools/base.py``) wraps
    *every* exception a tool raises — including our own ``McpToolError``,
    whose message is already the JSON payload — in
    ``f"Error executing tool {name}: {e}"`` before it reaches ``content[0]``.
    That prefix is universal (it predates this tool and applies to every MCP
    tool's error path), not something this tool's registration controls, so
    it is stripped here rather than in production code.
    """
    text = result["content"][0]["text"]
    prefix, _, remainder = text.partition(": ")
    if prefix.startswith("Error executing tool "):
        text = remainder
    return json.loads(text)


def test_mcp_chats_inspect_reads_via_backend(
    minimal_config_yaml: str, tmp_path: Path
) -> None:
    backend = FakeInspectBackend()
    config_yaml = _with_access(minimal_config_yaml, _MCP_READ_ACCESS)
    with _mcp_client(config_yaml, tmp_path, backend=backend) as client:
        token = _mint_token(client)
        _initialize(client, token)

        result = _call_tool(
            client, token, "telegram_chats_inspect", {"chat_id": CHAT_ID}
        )

    assert result["isError"] is False, result
    payload = result["structuredContent"]
    assert payload["chat_id"] == CHAT_ID
    assert payload["kind"] == "supergroup"
    assert payload["ttl_period"] == 86400
    assert payload["topics_layout"] == "tabs"
    assert "raw" not in payload
    assert backend.calls == [{"chat_id": CHAT_ID, "raw": False}]


def test_mcp_chats_inspect_resolves_entity(
    minimal_config_yaml: str, tmp_path: Path
) -> None:
    backend = FakeInspectBackend()
    config_yaml = _with_access(minimal_config_yaml, _MCP_READ_ACCESS)
    with _mcp_client(
        config_yaml,
        tmp_path,
        backend=backend,
        resolver=FakeResolver({"@clientchat": CHAT_ID}),
    ) as client:
        token = _mint_token(client)
        _initialize(client, token)

        result = _call_tool(
            client, token, "telegram_chats_inspect", {"entity": "@clientchat"}
        )

    assert result["isError"] is False, result
    assert result["structuredContent"]["chat_id"] == CHAT_ID
    assert backend.calls == [{"chat_id": CHAT_ID, "raw": False}]


def test_mcp_chats_inspect_resolves_chat_name_in_folder(
    minimal_config_yaml: str, tmp_path: Path
) -> None:
    backend = FakeInspectBackend()
    config_yaml = _with_access(minimal_config_yaml, _MCP_READ_ACCESS)
    with _mcp_client(
        config_yaml, tmp_path, backend=backend, folder_backend=_folder_backend()
    ) as client:
        token = _mint_token(client)
        _initialize(client, token)

        result = _call_tool(
            client,
            token,
            "telegram_chats_inspect",
            {"chat_name": "Client chat", "folder_name": "Planfix clients"},
        )

    assert result["isError"] is False, result
    assert result["structuredContent"]["chat_id"] == CHAT_ID
    assert backend.calls == [{"chat_id": CHAT_ID, "raw": False}]


def test_mcp_chats_inspect_rejects_raw(
    minimal_config_yaml: str, tmp_path: Path
) -> None:
    """`raw` is CLI-only — a tool error, never a silently dropped flag."""
    backend = FakeInspectBackend()
    config_yaml = _with_access(minimal_config_yaml, _MCP_READ_ACCESS)
    with _mcp_client(config_yaml, tmp_path, backend=backend) as client:
        token = _mint_token(client)
        _initialize(client, token)

        result = _call_tool(
            client,
            token,
            "telegram_chats_inspect",
            {"chat_id": CHAT_ID, "raw": True},
        )

    assert result["isError"] is True, result
    error = _error_payload(result)
    assert error["error"] == "invalid_request"
    assert error["status"] == 400
    assert "raw is CLI-only" in error["message"]
    # Nothing reached the domain op, so no raw payload could ever be built.
    assert backend.calls == []


def test_mcp_chats_inspect_requires_exactly_one_ref(
    minimal_config_yaml: str, tmp_path: Path
) -> None:
    backend = FakeInspectBackend()
    config_yaml = _with_access(minimal_config_yaml, _MCP_READ_ACCESS)
    with _mcp_client(config_yaml, tmp_path, backend=backend) as client:
        token = _mint_token(client)
        _initialize(client, token)

        result = _call_tool(client, token, "telegram_chats_inspect", {})

    assert result["isError"] is True, result
    error = _error_payload(result)
    assert error["error"] == "invalid_request"
    assert "exactly one" in error["message"]
    assert backend.calls == []


def test_mcp_chats_inspect_denied_without_read(
    minimal_config_yaml: str, tmp_path: Path
) -> None:
    backend = FakeInspectBackend()
    config_yaml = _with_access(minimal_config_yaml, _MCP_WRITE_ACCESS)
    with _mcp_client(config_yaml, tmp_path, backend=backend) as client:
        token = _mint_token(client)
        _initialize(client, token)

        result = _call_tool(
            client, token, "telegram_chats_inspect", {"chat_id": CHAT_ID}
        )

    assert result["isError"] is True, result
    assert _error_payload(result)["error"] == "access_denied"
    assert backend.calls == []


def test_mcp_chats_inspect_backend_unavailable(
    minimal_config_yaml: str, tmp_path: Path
) -> None:
    config_yaml = _with_access(minimal_config_yaml, _MCP_READ_ACCESS)
    with _mcp_client(config_yaml, tmp_path) as client:
        token = _mint_token(client)
        _initialize(client, token)

        result = _call_tool(
            client, token, "telegram_chats_inspect", {"chat_id": CHAT_ID}
        )

    assert result["isError"] is True, result
    error = _error_payload(result)
    assert error["error"] == "backend_unavailable"
    assert error["status"] == 503


def test_mcp_chats_inspect_maps_flood_wait_to_needs_review(
    minimal_config_yaml: str, tmp_path: Path
) -> None:
    backend = FakeInspectBackend(error=FloodWaitError(30.0))
    config_yaml = _with_access(minimal_config_yaml, _MCP_READ_ACCESS)
    with _mcp_client(config_yaml, tmp_path, backend=backend) as client:
        token = _mint_token(client)
        _initialize(client, token)

        result = _call_tool(
            client, token, "telegram_chats_inspect", {"chat_id": CHAT_ID}
        )

    assert result["isError"] is True, result
    error = _error_payload(result)
    assert error["error"] == "needs_review"
    assert error["status"] == 502
    assert error["detail"]["retry_after_seconds"] == 30.0


@pytest.mark.parametrize(
    "disabled",
    ["telegram_chats_inspect", "telegram_chats_*"],
)
def test_mcp_chats_inspect_can_be_disabled(
    minimal_config_yaml: str, tmp_path: Path, disabled: str
) -> None:
    """`mcp.disabled_tools` prunes it by exact name and by prefix wildcard."""
    config_yaml = _with_access(minimal_config_yaml, _MCP_READ_ACCESS)
    with _mcp_client(
        config_yaml,
        tmp_path,
        backend=FakeInspectBackend(),
        disabled_tools=(disabled,),
    ) as client:
        token = _mint_token(client)
        listed = _list_tools(client, token)

    assert "telegram_chats_inspect" not in set(listed)
    # The filter is targeted, not a blanket prune.
    assert "telegram_members_list" in set(listed)
