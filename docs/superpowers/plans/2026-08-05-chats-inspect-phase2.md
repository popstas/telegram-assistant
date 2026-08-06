# `chats inspect` Phase 2 — HTTP + MCP surfaces

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Expose the already-shipped `chats inspect` domain op on the HTTP API (`GET /telegram/chats/inspect`) and on the MCP server (`telegram_chats_inspect`). Phase 1 (the `chats/` domain package and the CLI command) is complete and merged on this branch; **no new domain logic** is written here.

**Architecture:** A new router module `src/telegram_assistant/http_api/chats.py` holds one route plus the shared helper `inspect_chat_for_request()` that the MCP tool reuses verbatim — the same pattern by which `mcp/tools.py` reuses `http_api/members.py`'s `_member_list_backend_or_503` and `http_api/topics.py`'s `_resolve_chat_id_generic`. A `chat_inspect_backend_factory` on `app.state` (built once in `create_app()`, like every other backend factory) returns `None` until the Telethon client is connected, which the route turns into `503`.

**Tech Stack:** Python 3.12, FastAPI, FastMCP, Telethon >= 1.44, pytest + pytest-asyncio (asyncio mode auto), ruff.

**Spec:** `docs/superpowers/specs/2026-08-05-chats-inspect-design.md` (section "Phase 2 — remaining surfaces")

**Research:** `.superpowers/research/phase2-surfaces.md`

## Global Constraints

- Use `.venv` — run everything as `.venv/bin/pytest`, `.venv/bin/ruff`.
- `ruff check src tests` must pass: line-length 100, target py312, `E501` ignored.
- The full suite is currently **2318 passing** and must stay green at every commit.
- No new domain logic. `src/telegram_assistant/chats/service.py` and `chats/telethon_backend.py` are **not** modified by this plan; if a task seems to need a change there, stop and report it.
- `chats/service.py` must not import `telethon`.
- `access_hash` must never appear in any payload. The remote surfaces never pass `raw=True`, so they cannot emit one.
- READ-gated on every surface, enforced in the domain layer — the surfaces do not re-implement the gate, they build the `Authorizer` and let `inspect_chat()` check it.
- `chat_id` in the payload is the bare id (no `-100` marker).
- HTTP status mapping: `AccessDenied` → 403, entity-not-found → 404, ambiguous entity → 409, domain `ValueError` → 400, backend unavailable → 503, `FloodWaitError` → 502 with `Retry-After`.

---

## Decisions this plan implements (from the spec, not re-openable)

1. **Chat reference.** Both remote surfaces take the same set the CLI does: `chat_id`, `entity`, or `chat_name` with `folder_name` / `folder_id`. **The model followed is `messages pin`** (`PinBody._shape` in `src/telegram_assistant/http_api/messages.py:446-463` plus the `pin` route's resolution ladder at `messages.py:1282-1300`): a 3-way exclusive-or over `telegram_chat_id`/`entity`/`chat_name`, with `chat_name requires folder_name`. `messages edit` (`EditBody._shape`, `messages.py:405-424`) is byte-for-byte the same shape, so either would do; `pin` is named because it is also the surface whose `FloodWaitError` mapping is being reused (decision 3), which keeps this plan pointing at one precedent rather than two.
   **Folder defaulting:** *none* on the remote surfaces. `telegram.default_chat_folder.folder_name` is a CLI convenience applied in `cli/main.py::chats_inspect` via `_resolve_folder_name`; no HTTP body in this codebase defaults it, and `PinBody` requires `folder_name` explicitly whenever `chat_name` is given. This plan does the same.
   **Parameter naming:** the remote surfaces call the numeric parameter `chat_id`, not `telegram_chat_id`. The payload's own key is `chat_id`, the CLI flag is `--chat-id`, and the two sibling bare-READ endpoints (`GET /telegram/members/list`, `GET /telegram/messages/recent`) already spell it `chat_id`. `telegram_chat_id` is the spelling used by the POST bodies that carry a *message* id next to it, which this op does not.
2. **`raw` is CLI-only.** Both surfaces accept a `raw` parameter and **reject** it — HTTP 400, MCP a tool error — with a message naming the reason. The rejection runs before the backend is even resolved, and after it the surfaces call `inspect_chat(..., raw=False)` unconditionally, so no remote caller's flag can reach the serializer. Tests on both surfaces prove the rejection *and* that the fake backend recorded zero calls.
3. **`FloodWaitError` is mapped.** HTTP answers 502 with `Retry-After` and `retry_after_seconds` in the body; MCP reports `needs_review` with the same field. This reuses the exact mapping `messages pin`/`unpin` established — `_translate_flood_wait` (`http_api/messages.py:93-109`) on the HTTP side and the `FolderPeerFailureError | FloodWaitError` branch of `_raise_from_exception` (`http_api/mcp/tools.py:428-436`) on the MCP side, both reading `retry_after_details()` (`messages/pacing.py:240-253`). `members list` maps none of this; that omission is deliberately **not** copied.
   One wrinkle the plan handles explicitly: `retry_after_details()` reads `exc.retry_after_seconds`, which only a **paced** flood-wait (`PacedFloodWaitError`, `messages/pacing.py:69-98`) carries. `chats inspect` is a one-shot read with no pacer, so `chats/telethon_backend.py` surfaces a bare `worker.queue.FloodWaitError` whose window lives on `.seconds`. Task 1 adds a five-line `_annotate_retry_after()` in the *surface* module that copies `.seconds` onto `retry_after_seconds`/`retry_at` before re-raising, so the existing mapping produces the promised payload instead of a poorer second one. No domain file changes.
4. **Payload unchanged.** Both surfaces return exactly `ChatInfo.to_dict()` — no `telegram_chat_id` wrapper key, no `limit`/`filter` echo (`members list` has those because it has such parameters; this op does not).

## Verified facts a fresh implementer would otherwise have to rediscover

- **`_on_swap` needs no entry.** Verified by reading `src/telegram_assistant/http_api/app.py:735-764`: the config hot-reload closure rebuilds `plugin_registry`, clears `folder_membership_cache`, calls `_ensure_state_stores`, and re-applies `mcp.disabled_tools`. It touches **no** `*_backend_factory`; every factory is built once in `create_app()` closed over the stable `session_manager`. Adding `chat_inspect_backend_factory` to `_on_swap` would be dead code. **Do not add it.**
- **`mcp.disabled_tools` needs no code change.** `configure_mcp_tools` (`http_api/mcp/server.py`) applies the filter after `register_telegram_tools` has registered everything, at mount time and on every hot-reload. A tool registered inside `register_telegram_tools` is automatically prunable by `telegram_chats_inspect` or `telegram_chats_*`.
- **`tests/test_skill_inventory.py` is CLI-only.** It compares the Typer command tree against the SKILL.md catalog; `chats inspect` is already in both from phase 1, so it is green now and stays green. It does not enumerate HTTP routes or MCP tools.
- **`tests/test_mcp_mount.py::EXPECTED_TOOL_NAMES` is asserted for exact equality** against the live `tools/list` response. Adding a tool without adding its name there fails `test_mcp_initialize_and_tools_list_are_reachable_with_token`.
- **No test enumerates HTTP routes generically** — there is no route-inventory guard to update.

---

### Task 1: HTTP route `GET /telegram/chats/inspect` + `chat_inspect_backend_factory`

**Files:**
- Create: `src/telegram_assistant/http_api/chats.py`
- Modify: `src/telegram_assistant/http_api/app.py`
- Create: `tests/test_chats_inspect_surfaces.py`

**Interfaces:**

*Consumes (all already exist, unchanged):*
- `telegram_assistant.chats.inspect_chat(*, backend: ChatInspectBackend, chat_id: int, raw: bool = False, authorizer: Authorizer | None = None) -> ChatInfo`
- `telegram_assistant.chats.ChatInspectBackend` — Protocol, `async def inspect_chat(self, *, chat_id: int, raw: bool) -> ChatInfo`
- `telegram_assistant.chats.ChatInfo.to_dict() -> dict[str, Any]` — omits the `raw` key entirely when `raw is None`
- `telegram_assistant.http_api.access.build_authorizer(request, *, folder_backend=None) -> Authorizer`
- `telegram_assistant.http_api.access.translate_access_error(exc) -> HTTPException | None`
- `telegram_assistant.http_api.topics._resolve_chat_id_generic(*, telegram_chat_id: int | None, chat_name: str | None, entity: str | int | None = None, folder_name: str | None, folder_id: int | None, request: Request) -> int` — resolves entity → resolver (503/404/409), numeric passthrough, or `resolve_chat_in_folder` (503/404/409 via `_translate_folder_error`)
- `telegram_assistant.http_api.messages._translate_flood_wait(exc: FloodWaitError) -> HTTPException`
- `telegram_assistant.http_api.auth.BearerAuth`
- `telegram_assistant.worker.queue.FloodWaitError` — `RuntimeError` subclass with `.seconds: float`
- `telegram_assistant.chats.telethon_backend.TelethonChatInspectBackend(client)`

*Produces (consumed by Task 2):*
- `telegram_assistant.http_api.chats.RAW_REJECTED_MESSAGE: str`
- `telegram_assistant.http_api.chats.validate_chat_inspect_args(*, chat_id: int | None, chat_name: str | None, entity: str | int | None, folder_name: str | None, raw: bool) -> None` — raises `ValueError`
- `telegram_assistant.http_api.chats._chat_inspect_backend_or_503(request) -> ChatInspectBackend`
- `telegram_assistant.http_api.chats.inspect_chat_for_request(request, *, chat_id=None, chat_name=None, entity=None, folder_name=None, folder_id=None, raw=False) -> dict[str, Any]` — **this is the single entry point Task 2's MCP tool calls**
- `telegram_assistant.http_api.chats.build_router() -> APIRouter`
- `telegram_assistant.http_api.app.ChatInspectBackendFactory = Callable[[Request], ChatInspectBackend | None]`
- `create_app(..., chat_inspect_backend_factory: ChatInspectBackendFactory | None = None, ...)` and `app.state.chat_inspect_backend_factory`
- In `tests/test_chats_inspect_surfaces.py`: `FakeInspectBackend`, `FakeResolver`, `FakeFolderBackend`, `_chat_info()`, `_folder_backend()`, `_config_with_access()`, `_READ_ACCESS`, `_WRITE_ACCESS`, `_make_store()`, `_http_client()`, `AUTH` — Task 2 adds MCP tests to the *same* file and reuses `FakeInspectBackend`, `FakeResolver`, `FakeFolderBackend`, `_chat_info`, `_folder_backend`.

---

- [ ] **Step 1: Write the failing test file**

Create `tests/test_chats_inspect_surfaces.py`:

```python
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
```

- [ ] **Step 2: Run the test and watch it fail for the right reason**

```bash
.venv/bin/pytest tests/test_chats_inspect_surfaces.py -q
```

Expected: every test errors with `TypeError: create_app() got an unexpected keyword argument 'chat_inspect_backend_factory'`.

- [ ] **Step 3: Create `src/telegram_assistant/http_api/chats.py`**

```python
"""HTTP routes for read-only chat metadata (the ``chats`` domain).

One route, ``GET /telegram/chats/inspect``, exposing
:func:`telegram_assistant.chats.inspect_chat`. The reference handling, the READ
gate wiring and the domain call live in :func:`inspect_chat_for_request`, which
the MCP tool ``telegram_chats_inspect`` calls verbatim — the two remote surfaces
must not be able to drift apart on which references they accept or on whether
``raw`` reaches the domain op.
"""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status

from telegram_assistant.access import AccessDenied
from telegram_assistant.chats import ChatInspectBackend, inspect_chat
from telegram_assistant.folders import FolderBackend
from telegram_assistant.http_api.access import build_authorizer, translate_access_error
from telegram_assistant.http_api.auth import BearerAuth
from telegram_assistant.http_api.messages import _translate_flood_wait
from telegram_assistant.http_api.topics import _resolve_chat_id_generic
from telegram_assistant.worker.queue import FloodWaitError

#: Why a remote caller may not ask for the serialized Telethon objects. The
#: curated payload is designed to be enough; ``raw`` carries considerably more
#: (a legacy group's whole member roster via ``ChatFull.participants``, a user's
#: business location and stories), and this project already keeps local-only
#: capabilities off the remote surfaces — ``scan_media`` resolves server-side
#: paths for the CLI alone, ``messages download --out`` is unconfined only there.
RAW_REJECTED_MESSAGE = (
    "raw is CLI-only: the serialized entity/Full objects are never returned "
    "over HTTP or MCP; run `telegram-assistant chats inspect --raw` locally"
)


def _chat_inspect_backend_or_503(request: Request) -> ChatInspectBackend:
    """Resolve the chat-inspect backend, or raise 503.

    Two stages like every sibling helper: no factory at all means nobody wired
    one (only a test opts out); a factory returning ``None`` is the production
    case where the Telethon client is not connected yet.
    """
    factory = getattr(request.app.state, "chat_inspect_backend_factory", None)
    if factory is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Telegram chat-inspect backend is not configured (session may be unauthorized)",
        )
    backend = factory(request)
    if backend is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Telegram chat-inspect backend is not available",
        )
    return backend


def _folder_backend_optional(request: Request) -> FolderBackend | None:
    factory = getattr(request.app.state, "folder_backend_factory", None)
    if factory is None:
        return None
    return factory(request)


def validate_chat_inspect_args(
    *,
    chat_id: int | None,
    chat_name: str | None,
    entity: str | int | None,
    folder_name: str | None,
    raw: bool,
) -> None:
    """Reject a malformed remote chats-inspect request (raises ``ValueError``).

    ``raw`` is checked **first**, and rejected rather than ignored: a silently
    dropped ``raw=true`` is indistinguishable from an empty raw payload, so the
    caller would never learn the flag went nowhere. Checking it before the
    reference rules also means the message names the real problem even when the
    request is malformed in two ways at once.

    The reference rules mirror ``PinBody._shape``: exactly one of ``chat_id`` /
    ``chat_name`` / ``entity``, and ``chat_name`` needs ``folder_name`` (there
    is no config-derived folder default on the remote surfaces — that is a CLI
    convenience).
    """
    if raw:
        raise ValueError(RAW_REJECTED_MESSAGE)
    refs = sum([chat_id is not None, chat_name is not None, entity is not None])
    if refs != 1:
        raise ValueError("provide exactly one of chat_id, chat_name, or entity")
    if chat_name is not None and folder_name is None:
        raise ValueError("chat_name requires folder_name")


def _annotate_retry_after(exc: FloodWaitError) -> FloodWaitError:
    """Give an *unpaced* flood-wait the retry fields the surfaces report.

    ``messages pin``/``unpin`` run behind a pacer, so what reaches their
    surfaces is a ``PacedFloodWaitError`` already carrying
    ``retry_after_seconds``/``retry_at`` — the two fields
    :func:`retry_after_details` reads and that both surfaces echo (HTTP also as
    the standard ``Retry-After`` header). ``chats inspect`` is a one-shot read
    with no pacer, so its adapter surfaces a bare ``FloodWaitError`` whose wait
    window lives on ``.seconds`` only. Copying it across lets the *same*
    mapping produce the same payload here rather than a second, poorer one —
    the caller of a read op needs to know when to come back just as much.
    """
    if getattr(exc, "retry_after_seconds", None) is None:
        seconds = float(getattr(exc, "seconds", 0.0) or 0.0)
        exc.retry_after_seconds = seconds  # type: ignore[attr-defined]
        exc.retry_at = time.time() + seconds  # type: ignore[attr-defined]
    return exc


async def inspect_chat_for_request(
    request: Request,
    *,
    chat_id: int | None = None,
    chat_name: str | None = None,
    entity: str | int | None = None,
    folder_name: str | None = None,
    folder_id: int | None = None,
    raw: bool = False,
) -> dict[str, Any]:
    """Resolve the chat reference, gate READ, and return the inspect payload.

    Shared by the HTTP route and the MCP tool. It raises rather than mapping,
    so each surface applies its own taxonomy: ``ValueError`` for malformed
    input, ``HTTPException`` for the 503/404/409 resolution failures,
    ``AccessDenied`` for a denied chat, and ``FloodWaitError`` (already carrying
    retry-after) for a throttle.

    ``raw`` is only ever *rejected* here; the domain call passes ``raw=False``
    unconditionally, so no remote caller's flag can reach the serializer.
    """
    validate_chat_inspect_args(
        chat_id=chat_id,
        chat_name=chat_name,
        entity=entity,
        folder_name=folder_name,
        raw=raw,
    )
    backend = _chat_inspect_backend_or_503(request)
    resolved_chat_id = await _resolve_chat_id_generic(
        telegram_chat_id=chat_id,
        chat_name=chat_name,
        entity=entity,
        folder_name=folder_name,
        folder_id=folder_id,
        request=request,
    )
    authorizer = build_authorizer(
        request, folder_backend=_folder_backend_optional(request)
    )
    try:
        info = await inspect_chat(
            backend=backend,
            chat_id=resolved_chat_id,
            raw=False,
            authorizer=authorizer,
        )
    except FloodWaitError as exc:
        _annotate_retry_after(exc)
        raise
    return info.to_dict()


def build_router() -> APIRouter:
    router = APIRouter(dependencies=[BearerAuth])

    @router.get("/chats/inspect")
    async def chats_inspect(
        request: Request,
        chat_id: int | None = None,
        chat_name: str | None = None,
        entity: str | None = None,
        folder_name: str | None = None,
        folder_id: int | None = None,
        raw: bool = False,
    ) -> dict[str, Any]:
        """Read one chat's metadata: TTL, description, counts, rights (READ-gated).

        Target the chat with exactly one of ``chat_id``, ``entity``, or
        ``chat_name`` (which requires ``folder_name``, optionally cross-checked
        by ``folder_id``) — the same set the CLI takes. ``raw`` is accepted only
        so it can be rejected with 400: the serialized Telethon objects are
        CLI-only. The body is the domain payload verbatim.
        """
        try:
            return await inspect_chat_for_request(
                request,
                chat_id=chat_id,
                chat_name=chat_name,
                entity=entity,
                folder_name=folder_name,
                folder_id=folder_id,
                raw=raw,
            )
        except AccessDenied as exc:
            raise translate_access_error(exc) from exc
        except FloodWaitError as exc:
            raise _translate_flood_wait(exc) from exc
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
            ) from exc

    return router
```

- [ ] **Step 4: Wire the factory and the router into `src/telegram_assistant/http_api/app.py`**

Four edits plus two imports, all mirroring `member_list_backend_factory` line for line.

**4a — domain import.** Between `from telegram_assistant import __version__` and `from telegram_assistant.config import (`, insert:

```python
from telegram_assistant.chats import ChatInspectBackend
```

**4b — router import.** Between `from telegram_assistant.http_api.auth import BearerAuth` and `from telegram_assistant.http_api.folders import build_router as build_folders_router`, insert:

```python
from telegram_assistant.http_api.chats import build_router as build_chats_router
```

**4c — type alias.** In the factory-alias block, directly *above* `FolderBackendFactory = Callable[[Request], FolderBackend | None]`, insert:

```python
ChatInspectBackendFactory = Callable[[Request], ChatInspectBackend | None]
```

**4d — default factory builder.** Directly after `_default_member_list_backend_factory`'s closing `    return _factory` (the one whose body imports `TelethonMemberListBackend`) and its blank lines, before `def _default_group_backend_factory(`, insert:

```python
def _default_chat_inspect_backend_factory(
    session_manager: TelethonSessionManager | None,
) -> ChatInspectBackendFactory:
    """Build a Telethon-backed chat-inspect factory for the read op.

    Mirrors :func:`_default_member_list_backend_factory`: returns ``None`` until
    a Telethon client is available so the endpoint can return 503.
    """

    def _factory(_request: Request) -> ChatInspectBackend | None:
        if session_manager is None:
            return None
        client = getattr(session_manager, "_client", None)
        if client is None:
            return None
        from telegram_assistant.chats.telethon_backend import (
            TelethonChatInspectBackend,
        )

        return TelethonChatInspectBackend(client)

    return _factory


```

**4e — `create_app()` parameter.** Directly after the line `    member_list_backend_factory: MemberListBackendFactory | None = None,` in the `create_app(` signature, insert:

```python
    chat_inspect_backend_factory: ChatInspectBackendFactory | None = None,
```

**4f — `app.state` assignment.** Directly after the `app.state.member_list_backend_factory = (...)` block (the one ending `else _default_member_list_backend_factory(session_manager)\n    )`), insert:

```python
    app.state.chat_inspect_backend_factory = (
        chat_inspect_backend_factory
        if chat_inspect_backend_factory is not None
        else _default_chat_inspect_backend_factory(session_manager)
    )
```

**4g — router mount.** Directly after `    app.include_router(build_members_router(), prefix="/telegram")`, insert:

```python
    app.include_router(build_chats_router(), prefix="/telegram")
```

Do **not** add anything to `_on_swap` — every backend factory is built once in `create_app()` and the hot-reload closure deliberately does not touch them (verified at `app.py:735-764`).

- [ ] **Step 5: Run the new test file and see it green**

```bash
.venv/bin/pytest tests/test_chats_inspect_surfaces.py -q
```

Expected: 14 passed.

- [ ] **Step 6: Run the full suite**

```bash
.venv/bin/pytest -q
```

Expected: 2332 passed (2318 + the 14 new), 0 failed. `tests/test_docker_image.py` may skip when Docker is unavailable.

- [ ] **Step 7: Lint**

```bash
.venv/bin/ruff check src tests
```

Expected: `All checks passed!`

- [ ] **Step 8: Commit**

```bash
git add src/telegram_assistant/http_api/chats.py src/telegram_assistant/http_api/app.py tests/test_chats_inspect_surfaces.py
git commit -m "feat(http): serve chats inspect at GET /telegram/chats/inspect"
```

---

### Task 2: MCP tool `telegram_chats_inspect`

**Files:**
- Modify: `src/telegram_assistant/http_api/mcp/tools.py`
- Modify: `tests/test_mcp_mount.py` (`EXPECTED_TOOL_NAMES` only)
- Modify: `tests/test_chats_inspect_surfaces.py` (append the MCP section)

**Interfaces:**

*Consumes (from Task 1, unchanged):*
- `telegram_assistant.http_api.chats.inspect_chat_for_request(request, *, chat_id=None, chat_name=None, entity=None, folder_name=None, folder_id=None, raw=False) -> dict[str, Any]`
- `create_app(..., chat_inspect_backend_factory=...)` and `app.state.chat_inspect_backend_factory`
- From `tests/test_chats_inspect_surfaces.py`: `FakeInspectBackend`, `FakeResolver`, `FakeFolderBackend`, `_chat_info`, `_folder_backend`, `CHAT_ID`

*Consumes (already exists, unchanged):*
- `tools.py::_request(provider) -> _McpRequest` — the `app.state` shim whose `.app.state` makes the HTTP helpers work unmodified
- `tools.py::_raise_from_exception(exc) -> NoReturn` — maps `HTTPException` by status, `AccessDenied` → 403 `access_denied`, entity errors → 404/409, `FloodWaitError` → 502 `needs_review` with `retry_after_details(exc)` as `detail`, `ValueError` → 400 `invalid_request`
- `tools.py::READ_TELEGRAM` — the read-op `ToolAnnotations`
- `tests/test_mcp_mount.py`: `FakeGoogleOidcProvider`, `FakeSessionManager`, `_enabled_mcp_yaml`, `_initialize_payload`, `_mcp_headers`, `_mint_token`

*Produces:*
- MCP tool `telegram_chats_inspect(chat_id=None, chat_name=None, entity=None, folder_name=None, folder_id=None, raw=False) -> dict[str, Any]`
- `"telegram_chats_inspect"` in `EXPECTED_TOOL_NAMES`
- In the test file: `_with_access()`, `_mcp_client()`, `_initialize()`, `_call_tool()`

---

- [ ] **Step 1: Add the failing mount assertion**

In `tests/test_mcp_mount.py`, add `"telegram_chats_inspect",` as the **first** entry of `EXPECTED_TOOL_NAMES`, above `"telegram_folders_add_chat",` (the set is written alphabetically and `c` < `f`):

```python
EXPECTED_TOOL_NAMES = {
    "telegram_chats_inspect",
    "telegram_folders_add_chat",
    "telegram_folders_inspect",
```

- [ ] **Step 2: Run the mount test and watch it fail**

```bash
.venv/bin/pytest tests/test_mcp_mount.py -q -k tools_list
```

Expected: FAIL — the live `tools/list` response is missing `telegram_chats_inspect`, so the exact-equality assertion in `test_mcp_initialize_and_tools_list_are_reachable_with_token` fails.

- [ ] **Step 3: Append the MCP functional tests to `tests/test_chats_inspect_surfaces.py`**

There is no existing MCP functional test for `telegram_members_list` to copy, so these are modelled on `tests/test_mcp_tools.py::test_mcp_recent_messages_reads_via_backend` (the nearest fully-worked MCP READ-op test) with its `_client`/`_initialize`/`_call_tool` scaffolding rebuilt locally — `test_mcp_tools.py::_client` does not accept a chat-inspect factory, and importing that module would drag in its whole fake-backend zoo for one tool.

First extend the import block at the top of the file. Replace:

```python
import tempfile
import textwrap
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
```

with:

```python
import json
import tempfile
import textwrap
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
```

and add, after `from telegram_assistant.worker.queue import FloodWaitError`:

```python
from tests.test_mcp_mount import (
    FakeGoogleOidcProvider,
    FakeSessionManager,
    _enabled_mcp_yaml,
    _initialize_payload,
    _list_tools,
    _mcp_headers,
    _mint_token,
)
```

Then append to the end of the file:

```python
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
    """The JSON error body an MCP tool failure carries."""
    return json.loads(result["content"][0]["text"])


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
```

- [ ] **Step 4: Run the MCP tests and watch them fail**

```bash
.venv/bin/pytest tests/test_chats_inspect_surfaces.py -q -k mcp
```

Expected: FAIL — the tool does not exist, so `tools/call` answers with an "Unknown tool: telegram_chats_inspect" error (`isError` is `True` with a `tool_error` payload rather than the asserted code), and `test_mcp_chats_inspect_reads_via_backend` fails on `result["isError"] is False`.

- [ ] **Step 5: Register the tool in `src/telegram_assistant/http_api/mcp/tools.py`**

**5a — import.** Directly after the `from telegram_assistant.http_api.access import (...)` block (the one ending with `translate_entity_error,\n)`), and before `from telegram_assistant.http_api.folders import AddChatRequest`, insert:

```python
from telegram_assistant.http_api.chats import inspect_chat_for_request
```

**5b — the tool.** Inside `register_telegram_tools`, directly after the `telegram_members_list` tool's final `            _raise_from_exception(exc)` line and before the `    @server.tool(\n        name="telegram_members_add",` decorator, insert:

```python
    @server.tool(
        name="telegram_chats_inspect",
        annotations=READ_TELEGRAM,
        structured_output=True,
    )
    async def telegram_chats_inspect(
        chat_id: int | None = None,
        chat_name: str | None = None,
        entity: str | int | None = None,
        folder_name: str | None = None,
        folder_id: int | None = None,
        raw: bool = False,
    ) -> dict[str, Any]:
        """Read one chat's metadata: TTL, description, counts, rights (READ-gated).

        Answers "what is this chat" for every peer kind with one flat payload —
        auto-delete ``ttl_period``, ``about``, member counts, slow mode,
        restrictions, our own rights — so a caller can read a field without
        branching on whether the target is a supergroup, a channel or a private
        chat. Target it with exactly one of ``chat_id``, ``entity``, or
        ``chat_name`` (which requires ``folder_name``, optionally cross-checked
        by ``folder_id``). It never writes: there is no way to *change* any of
        these settings through this tool.

        ``raw`` is accepted only so it can be rejected — the serialized Telethon
        objects are CLI-only, and silently dropping the flag would look like an
        empty raw payload.
        """
        request = _request(provider)
        try:
            return await inspect_chat_for_request(
                request,  # type: ignore[arg-type]
                chat_id=chat_id,
                chat_name=chat_name,
                entity=entity,
                folder_name=folder_name,
                folder_id=folder_id,
                raw=raw,
            )
        except Exception as exc:
            _raise_from_exception(exc)

```

- [ ] **Step 6: Run both affected test files and see them green**

```bash
.venv/bin/pytest tests/test_chats_inspect_surfaces.py tests/test_mcp_mount.py -q
```

Expected: all pass (14 HTTP + 10 MCP tests in the surfaces file, plus the whole mount file).

- [ ] **Step 7: Run the full suite**

```bash
.venv/bin/pytest -q
```

Expected: 2342 passed (2318 + 14 from Task 1 + 10 here), 0 failed.

- [ ] **Step 8: Lint**

```bash
.venv/bin/ruff check src tests
```

Expected: `All checks passed!`

- [ ] **Step 9: Commit**

```bash
git add src/telegram_assistant/http_api/mcp/tools.py tests/test_mcp_mount.py tests/test_chats_inspect_surfaces.py
git commit -m "feat(mcp): add telegram_chats_inspect tool"
```

---

### Task 3: Documentation — SKILL.md, README, CLAUDE.md, skill sync

**Files:**
- Modify: `skills/telegram-assistant/SKILL.md`
- Modify: `README.md` (the HTTP endpoint list **and** the MCP tool catalog)
- Modify: `CLAUDE.md` (the backend-factory enumeration)
- Copy: `~/.claude/skills/telegram-assistant/SKILL.md`

**Interfaces:**
- Consumes: the HTTP route from Task 1 (`GET /telegram/chats/inspect`, params `chat_id` / `chat_name` + `folder_name`/`folder_id` / `entity`, plus the rejected `raw`) and the MCP tool from Task 2 (`telegram_chats_inspect`, same arguments).
- Produces: no code. Documentation only, plus a still-green `tests/test_skill_inventory.py`.

**Why `CLAUDE.md` needs a line:** its Architecture paragraph enumerates the backend factories on `app.state.*_backend_factory` and states "When changing how backends are constructed, preserve this contract". A new factory that is not listed there breaks that enumeration. The `## Common commands` CLI line already names `chats inspect` (added in phase 1) and needs no change.

---

- [ ] **Step 1: Add the remote-surfaces bullet to the SKILL's per-pair section**

In `skills/telegram-assistant/SKILL.md`, inside `#### \`chats\` / \`inspect\``, insert directly **after** the `- \`--raw\`: adds a \`raw\` key ...` bullet (the one ending "its shape moves with the Telegram layer.") and **before** the `- Note it does **not** write anything:` bullet:

```markdown
- Other surfaces: the same op is served by HTTP `GET /telegram/chats/inspect`
  and by the MCP tool `telegram_chats_inspect`, taking the same chat references
  (`chat_id` / `chat_name` + `folder_name`/`folder_id` / `entity`) and returning
  the same payload. `raw` is **CLI-only** there — both surfaces *reject*
  `raw=true` (HTTP `400`, MCP a tool error) rather than ignoring it, so a
  serialized dump can only be produced locally. This skill still uses the CLI;
  mention the remote surfaces only if the human is asking about them.
```

- [ ] **Step 2: Note the mapped flood-wait in the SKILL's error bullet**

In the same section, replace the `- Typical errors:` bullet with:

```markdown
- Typical errors: `exactly one of --chat-id, --chat-name, or --entity must be
  supplied` (exit 2), `chat <id> cannot be inspected (resolved to ...)` (exit 2
  — the reference resolved to something with no metadata to read),
  `chat <id> is private or inaccessible` (exit 2), `chat <id> is forbidden`
  (exit 2 — we were removed from it), `access denied ...` (exit 3), entity
  not-found / ambiguous (exit 2). A `FLOOD_WAIT` exits 1 on the CLI (one-shot
  read, nothing retries it); on HTTP/MCP the same throttle comes back as
  `502` / `needs_review` carrying `retry_after_seconds` — wait that long and
  try again rather than retrying immediately.
```

- [ ] **Step 3: Verify the CLI-catalog guard is still green**

```bash
.venv/bin/pytest tests/test_skill_inventory.py -q
```

Expected: PASS. (The guard compares the Typer command tree against the SKILL catalog; `chats inspect` has been in both since phase 1 and neither changed here.)

- [ ] **Step 4: Add the HTTP endpoint bullet to `README.md`**

In the `## HTTP API` bullet list, insert directly after the `- \`GET /telegram/members/list\` ...` bullet:

```markdown
- `GET /telegram/chats/inspect` returns one chat's metadata (READ-gated). Query params: exactly one of `chat_id`, `entity`, or `chat_name` (which requires `folder_name`, optionally cross-checked by `folder_id`) — the same references the CLI takes. The body is one flat JSON object with the same keys for every chat kind (`null` where a field does not apply): `chat_id` (bare id, no `-100`), `kind`, `title`, `about`, `ttl_period` (auto-delete window in seconds, `null` when off), `pinned_message_id`, `archived`, `muted`/`muted_until`/`silent`, `restricted` + `restriction_reason`, `invite_link`, `my_admin_rights`, `default_banned_rights`, plus the groups/channels block (`is_forum`, `topics_layout`, `participants_count`, `admins_count`, `slowmode_seconds`, `linked_chat_id`, …) and the users/bots block (`phone`, `is_premium`, `blocked`, `common_chats_count`, `birthday`, …). `raw` is **CLI-only**: passing `raw=true` is a `400` naming the reason rather than a silently dropped flag, so the serialized Telethon objects are never returned remotely and `access_hash` can never leak. Missing/duplicate references and an uninspectable peer are `400`, a denied chat is `403`, an unresolvable reference is `404`, an ambiguous one `409`, a `FLOOD_WAIT` is `502` with `retry_after_seconds` and the `Retry-After` header, and no connected session is `503`. It reads only — there is no endpoint to change any of these settings.
```

- [ ] **Step 5: Add the MCP tool-catalog row to `README.md`**

In the `MCP tool catalog:` table, insert directly after the `| \`telegram_members_list\` | ... |` row (keeping the read ops together; `telegram_folders_inspect`, the other bare-READ inspect op, is two rows below):

```markdown
| `telegram_chats_inspect` | exactly one of `chat_id`/`entity`/`chat_name` (+ `folder_name`, optional `folder_id`); READ-gated read op returning one flat metadata payload per chat kind — `ttl_period`, `about`, `pinned_message_id`, `archived`, `muted`/`muted_until`/`silent`, `restricted`, `invite_link`, `my_admin_rights`, `default_banned_rights`, plus the groups/channels and users/bots blocks. `raw` is CLI-only and is **rejected** (tool error) rather than ignored; a `FLOOD_WAIT` comes back as `needs_review` with `retry_after_seconds` |
```

- [ ] **Step 6: List the new factory in `CLAUDE.md`**

In the Architecture paragraph beginning "This split is what lets tests inject fakes without spinning up Telethon.", extend the parenthesised factory list so it reads:

```
(including `message_backend_factory`, `message_read_backend_factory`, `reaction_backend_factory`, `forward_backend_factory`, `edit_backend_factory`, `pin_backend_factory`, `download_backend_factory`, `search_backend_factory`, `chat_inspect_backend_factory`, `notification_backend_factory`, and `resolver_factory`)
```

i.e. insert `` `chat_inspect_backend_factory`, `` between `` `search_backend_factory`, `` and `` `notification_backend_factory`, ``.

- [ ] **Step 7: Sync the skill to the user skills directory**

```bash
cp skills/telegram-assistant/SKILL.md ~/.claude/skills/telegram-assistant/SKILL.md
diff skills/telegram-assistant/SKILL.md ~/.claude/skills/telegram-assistant/SKILL.md && echo "skill in sync"
```

Expected: `skill in sync` (no diff output).

- [ ] **Step 8: Run the full suite**

```bash
.venv/bin/pytest -q
```

Expected: 2342 passed, 0 failed.

- [ ] **Step 9: Lint**

```bash
.venv/bin/ruff check src tests
```

Expected: `All checks passed!`

- [ ] **Step 10: Commit**

```bash
git add skills/telegram-assistant/SKILL.md README.md CLAUDE.md
git commit -m "docs: document the chats inspect HTTP route and MCP tool"
```

---

## Self-Review

### Spec coverage

Every phase-2 requirement in `docs/superpowers/specs/2026-08-05-chats-inspect-design.md` maps to a task:

| Spec requirement | Task | Where |
| --- | --- | --- |
| HTTP `GET /telegram/chats/inspect` | 1 | Step 3 (`build_router`) + Step 4g (mount) |
| `chat_inspect_backend_factory` on `app.state`, `None` → 503 | 1 | Steps 3 (`_chat_inspect_backend_or_503`), 4c/4d/4e/4f; tested by `test_http_chats_inspect_503_without_backend` |
| MCP tool `telegram_chats_inspect` | 2 | Step 5b |
| `EXPECTED_TOOL_NAMES` in `tests/test_mcp_mount.py` | 2 | Step 1 |
| `tests/test_chats_inspect_surfaces.py` | 1 (HTTP half) + 2 (MCP half) | Task 1 Step 1, Task 2 Step 3 |
| README MCP tool catalog | 3 | Step 5 |
| Decision 1 — same chat references as the CLI, `messages pin` as the model | 1 | `validate_chat_inspect_args` + `_resolve_chat_id_generic`; tested by the entity / chat-name / XOR / `chat_name requires folder_name` tests on both surfaces |
| Decision 2 — `raw` accepted and rejected; domain op not called | 1 + 2 | `validate_chat_inspect_args` checks `raw` first; `test_http_chats_inspect_rejects_raw` and `test_mcp_chats_inspect_rejects_raw` both assert `backend.calls == []` |
| Decision 2b — surfaces call the domain op with `raw=False` | 1 | `inspect_chat_for_request` hard-codes `raw=False`; every happy-path test asserts the recorded call is `{"chat_id": …, "raw": False}` |
| Decision 3 — `FloodWaitError` → HTTP 502 + `Retry-After` + `retry_after_seconds`, MCP `needs_review` with the same field, reusing the pin/unpin mapping | 1 + 2 | `_translate_flood_wait` reused, `_annotate_retry_after` feeds it; `test_http_chats_inspect_maps_flood_wait_to_retry_after` and `test_mcp_chats_inspect_maps_flood_wait_to_needs_review` |
| Decision 4 — payload is `ChatInfo.to_dict()`, nothing else | 1 | `inspect_chat_for_request` returns `info.to_dict()`; `test_http_chats_inspect_returns_the_domain_payload` asserts `"telegram_chat_id" not in body` |
| SKILL.md + README + skill re-sync (`CLAUDE.md` checked) | 3 | Steps 1, 2, 4, 5, 6, 7 |
| `_on_swap` question answered from the real file | — | "Verified facts" section: `app.py:735-764` touches no factory ⇒ no entry, stated as a "do not add" instruction in Task 1 Step 4 |

No gaps found.

### Placeholder scan

Searched this plan for `TBD`, `add appropriate`, `handle edge cases`, `write tests for the above`, `similar to Task N`, `etc.` in place of code, and `...` standing in for an unwritten body. No hits: every file is given in full (`http_api/chats.py` complete, both test halves complete, every `app.py` / `tools.py` / doc edit quoted with an explicit anchor). The only ellipses in the plan are inside prose descriptions of README payload field lists, where they are part of the documentation text being written, not a placeholder for the implementer.

### Type consistency

- The factory is `chat_inspect_backend_factory` (snake) / `ChatInspectBackendFactory` (alias) in Task 1's `app.py` edits, in Task 1's `_http_client`, and in Task 2's `_mcp_client` — one spelling everywhere.
- `inspect_chat_for_request(request, *, chat_id, chat_name, entity, folder_name, folder_id, raw)` is defined once in Task 1 and called with exactly those keywords from the HTTP route (Task 1) and the MCP tool (Task 2).
- `entity` is `str | int | None` in `inspect_chat_for_request`, `validate_chat_inspect_args` and the MCP tool (matching `_resolve_chat_id_generic`), and narrowed to `str | None` only on the FastAPI query signature, where a query value is always a string. That widening is safe in one direction and is the same split `messages pin` uses (`PinBody.entity: str | int | None`, resolved from a JSON body).
- `FakeInspectBackend.inspect_chat(self, *, chat_id: int, raw: bool) -> ChatInfo` matches the `ChatInspectBackend` protocol in `chats/service.py:145` exactly (both keyword-only, both required, no defaults on the protocol).
- `FakeInspectBackend`, `FakeResolver`, `FakeFolderBackend`, `_chat_info`, `_folder_backend` and `CHAT_ID` are defined in Task 1's test file and reused unchanged by Task 2's appended section — Task 2 adds only `_with_access`, `_mcp_client`, `_initialize`, `_call_tool`, `_error_payload` and the two `_MCP_*_ACCESS` constants.
- `RAW_REJECTED_MESSAGE` starts with `raw is CLI-only:`, which is the substring both raw-rejection tests assert on.
