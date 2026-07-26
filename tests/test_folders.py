"""Tests for chat-folder resolution and folder-membership operations.

Covers Task 6 of the MVP plan: folder lookup, ``folder_id`` cross-check, chat
name disambiguation, the inspect/add-chat HTTP routes, and the matching CLI
commands. All tests use an in-memory :class:`FolderBackend` so they never
touch Telethon — the Telethon adapter is exercised only through type checks.
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from telegram_assistant.cli import main as cli_main
from telegram_assistant.config import load_config_from_text
from telegram_assistant.folders import (
    AmbiguousChatNameError,
    ChatNotFoundError,
    FolderBackend,
    FolderChat,
    FolderIdMismatchError,
    FolderNotFoundError,
    FolderPeerFailureError,
    FolderSnapshot,
    add_chat_to_folder,
    inspect_folder,
    remove_chat_from_folder,
    resolve_chat_in_folder,
    resolve_folder,
)
from telegram_assistant.http_api import create_app


class FakeFolderBackend:
    """In-memory :class:`FolderBackend` used across HTTP and CLI tests."""

    def __init__(
        self,
        folders: list[FolderSnapshot],
        *,
        known_chats: dict[str | int, FolderChat] | None = None,
        add_should_fail: bool = False,
        add_should_raise: type[Exception] = RuntimeError,
        remove_should_fail: bool = False,
        remove_should_raise: type[Exception] = RuntimeError,
    ) -> None:
        self._folders = folders
        self._known_chats = known_chats or {}
        self._add_should_fail = add_should_fail
        self._add_should_raise = add_should_raise
        self._remove_should_fail = remove_should_fail
        self._remove_should_raise = remove_should_raise
        self.added: list[tuple[int, int]] = []
        self.removed: list[tuple[int, int]] = []

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
        if chat_ref in self._known_chats:
            return self._known_chats[chat_ref]
        if isinstance(chat_ref, int):
            return FolderChat(chat_id=chat_ref, title=f"Chat {chat_ref}")
        raise LookupError(f"unknown chat ref {chat_ref!r}")

    async def add_chat_to_folder(self, folder_id: int, chat_id: int) -> None:
        if self._add_should_fail:
            raise self._add_should_raise("telegram refused")
        for f in self._folders:
            if f.folder_id == folder_id:
                f.chats.append(FolderChat(chat_id=chat_id, title=f"Chat {chat_id}"))
                break
        self.added.append((folder_id, chat_id))

    async def remove_chat_from_folder(self, folder_id: int, chat_id: int) -> None:
        if self._remove_should_fail:
            raise self._remove_should_raise("telegram refused")
        for f in self._folders:
            if f.folder_id == folder_id:
                f.chats = [c for c in f.chats if c.chat_id != chat_id]
                break
        self.removed.append((folder_id, chat_id))


def _sample_folder() -> FolderSnapshot:
    return FolderSnapshot(
        folder_id=2,
        folder_name="Planfix clients",
        chats=[
            FolderChat(chat_id=100, title="Acme"),
            FolderChat(chat_id=200, title="Globex"),
        ],
    )


# ---------------------------------------------------------------------------
# resolve_folder
# ---------------------------------------------------------------------------


async def test_resolve_folder_found_by_name() -> None:
    backend = FakeFolderBackend([_sample_folder()])
    snapshot = await resolve_folder(backend, folder_name="Planfix clients")
    assert snapshot.folder_id == 2
    assert snapshot.chats_count == 2


async def test_resolve_folder_missing_raises() -> None:
    backend = FakeFolderBackend([])
    with pytest.raises(FolderNotFoundError):
        await resolve_folder(backend, folder_name="Planfix clients")


async def test_resolve_folder_id_mismatch_raises() -> None:
    backend = FakeFolderBackend([_sample_folder()])
    with pytest.raises(FolderIdMismatchError):
        await resolve_folder(
            backend, folder_name="Planfix clients", folder_id=999
        )


async def test_resolve_folder_id_match_passes() -> None:
    backend = FakeFolderBackend([_sample_folder()])
    snapshot = await resolve_folder(
        backend, folder_name="Planfix clients", folder_id=2
    )
    assert snapshot.folder_id == 2


async def test_resolve_folder_with_duplicate_names_requires_id() -> None:
    backend = FakeFolderBackend(
        [
            FolderSnapshot(folder_id=2, folder_name="Dup", chats=[]),
            FolderSnapshot(folder_id=3, folder_name="Dup", chats=[]),
        ]
    )
    with pytest.raises(FolderNotFoundError):
        await resolve_folder(backend, folder_name="Dup")
    snapshot = await resolve_folder(backend, folder_name="Dup", folder_id=3)
    assert snapshot.folder_id == 3


async def test_resolve_folder_with_duplicate_names_unknown_id_mismatch() -> None:
    backend = FakeFolderBackend(
        [
            FolderSnapshot(folder_id=2, folder_name="Dup", chats=[]),
            FolderSnapshot(folder_id=3, folder_name="Dup", chats=[]),
        ]
    )
    with pytest.raises(FolderIdMismatchError):
        await resolve_folder(backend, folder_name="Dup", folder_id=99)


# ---------------------------------------------------------------------------
# resolve_chat_in_folder
# ---------------------------------------------------------------------------


async def test_resolve_chat_unique() -> None:
    backend = FakeFolderBackend([_sample_folder()])
    chat = await resolve_chat_in_folder(
        backend, folder_name="Planfix clients", chat_name="Acme"
    )
    assert chat.chat_id == 100


async def test_resolve_chat_missing() -> None:
    backend = FakeFolderBackend([_sample_folder()])
    with pytest.raises(ChatNotFoundError):
        await resolve_chat_in_folder(
            backend, folder_name="Planfix clients", chat_name="Nope"
        )


async def test_resolve_chat_ambiguous_lists_matches() -> None:
    folder = FolderSnapshot(
        folder_id=2,
        folder_name="Planfix clients",
        chats=[
            FolderChat(chat_id=100, title="Dup"),
            FolderChat(chat_id=200, title="Dup"),
        ],
    )
    backend = FakeFolderBackend([folder])
    with pytest.raises(AmbiguousChatNameError) as exc_info:
        await resolve_chat_in_folder(
            backend, folder_name="Planfix clients", chat_name="Dup"
        )
    assert exc_info.value.matches == [100, 200]


# ---------------------------------------------------------------------------
# inspect_folder
# ---------------------------------------------------------------------------


async def test_inspect_folder_returns_chats() -> None:
    backend = FakeFolderBackend([_sample_folder()])
    snapshot = await inspect_folder(backend, folder_name="Planfix clients")
    payload = snapshot.to_dict()
    assert payload == {
        "folder_id": 2,
        "folder_name": "Planfix clients",
        "chats_count": 2,
        "chats": [
            {"chat_id": 100, "title": "Acme"},
            {"chat_id": 200, "title": "Globex"},
        ],
    }


# ---------------------------------------------------------------------------
# add_chat_to_folder
# ---------------------------------------------------------------------------


async def test_add_chat_when_already_present_is_idempotent() -> None:
    backend = FakeFolderBackend([_sample_folder()])
    result = await add_chat_to_folder(
        backend, folder_name="Planfix clients", chat_ref=100
    )
    assert result["already_in_folder"] is True
    assert backend.added == []


async def test_add_chat_appends_and_returns_result() -> None:
    backend = FakeFolderBackend([_sample_folder()])
    result = await add_chat_to_folder(
        backend, folder_name="Planfix clients", chat_ref=300
    )
    assert result["already_in_folder"] is False
    assert result["chat_id"] == 300
    assert backend.added == [(2, 300)]


async def test_add_chat_per_peer_failure_raises_needs_review() -> None:
    backend = FakeFolderBackend(
        [_sample_folder()], add_should_fail=True
    )
    with pytest.raises(FolderPeerFailureError):
        await add_chat_to_folder(
            backend, folder_name="Planfix clients", chat_ref=300
        )


# ---------------------------------------------------------------------------
# remove_chat_from_folder
# ---------------------------------------------------------------------------


async def test_remove_chat_when_absent_is_idempotent() -> None:
    backend = FakeFolderBackend([_sample_folder()])
    result = await remove_chat_from_folder(
        backend, folder_name="Planfix clients", chat_ref=999
    )
    assert result["already_absent"] is True
    assert backend.removed == []


async def test_remove_chat_present_removes_and_returns_result() -> None:
    backend = FakeFolderBackend([_sample_folder()])
    result = await remove_chat_from_folder(
        backend, folder_name="Planfix clients", chat_ref=100
    )
    assert result["already_absent"] is False
    assert result["chat_id"] == 100
    assert backend.removed == [(2, 100)]


async def test_remove_chat_per_peer_failure_raises_needs_review() -> None:
    backend = FakeFolderBackend(
        [_sample_folder()], remove_should_fail=True
    )
    with pytest.raises(FolderPeerFailureError):
        await remove_chat_from_folder(
            backend, folder_name="Planfix clients", chat_ref=100
        )


async def test_remove_chat_denied_before_backend_call() -> None:
    from telegram_assistant.access import AccessDenied, Authorizer
    from telegram_assistant.config.models import AccessConfig, AccessRule

    backend = FakeFolderBackend([_sample_folder()])
    authorizer = Authorizer(
        AccessConfig(rules=[AccessRule(all=True, permission="read")])
    )

    with pytest.raises(AccessDenied):
        await remove_chat_from_folder(
            backend,
            folder_name="Planfix clients",
            chat_ref=100,
            authorizer=authorizer,
        )

    assert backend.removed == []


async def test_remove_chat_absent_retry_allowed_with_folder_write_rule() -> None:
    from telegram_assistant.access import Authorizer
    from telegram_assistant.config.models import AccessConfig, AccessRule

    backend = FakeFolderBackend([_sample_folder()])
    access = AccessConfig(
        rules=[AccessRule(folder="Planfix clients", permission="write")]
    )

    first = await remove_chat_from_folder(
        backend,
        folder_name="Planfix clients",
        chat_ref=100,
        authorizer=Authorizer(access, folder_backend=backend),
    )
    assert first["already_absent"] is False
    assert backend.removed == [(2, 100)]

    second = await remove_chat_from_folder(
        backend,
        folder_name="Planfix clients",
        chat_ref=100,
        authorizer=Authorizer(access, folder_backend=backend),
    )
    assert second["already_absent"] is True
    assert backend.removed == [(2, 100)]


# ---------------------------------------------------------------------------
# HTTP routes
# ---------------------------------------------------------------------------


@pytest.fixture()
def fake_backend() -> FakeFolderBackend:
    return FakeFolderBackend([_sample_folder()])


def _make_app(
    minimal_config_yaml: str, backend: FolderBackend
) -> TestClient:
    config = load_config_from_text(minimal_config_yaml)
    app = create_app(
        config,
        session_manager=None,
        folder_backend_factory=lambda _request: backend,
    )
    return TestClient(app)


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


def test_http_inspect_returns_folder_payload(
    minimal_config_yaml: str, fake_backend: FakeFolderBackend
) -> None:
    client = _make_app(minimal_config_yaml, fake_backend)
    resp = client.get(
        "/telegram/folders/Planfix clients",
        headers={"Authorization": "Bearer secret_token"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["folder_id"] == 2
    assert body["chats_count"] == 2


def test_http_inspect_missing_folder_returns_404(
    minimal_config_yaml: str,
) -> None:
    client = _make_app(minimal_config_yaml, FakeFolderBackend([]))
    resp = client.get(
        "/telegram/folders/Nope",
        headers={"Authorization": "Bearer secret_token"},
    )
    assert resp.status_code == 404


def test_http_inspect_folder_id_mismatch_returns_409(
    minimal_config_yaml: str, fake_backend: FakeFolderBackend
) -> None:
    client = _make_app(minimal_config_yaml, fake_backend)
    resp = client.get(
        "/telegram/folders/Planfix clients?folder_id=999",
        headers={"Authorization": "Bearer secret_token"},
    )
    assert resp.status_code == 409


def test_http_inspect_403_when_folder_not_readable(
    fake_backend: FakeFolderBackend,
) -> None:
    """Listing a folder discloses its chats, so it needs the same READ gate as MCP."""
    client = _make_app(
        _config_with_access("access:\n  rules: []\n"),
        fake_backend,
    )

    resp = client.get(
        "/telegram/folders/Planfix clients",
        headers={"Authorization": "Bearer secret_token"},
    )

    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"]["error"] == "access_denied"


def test_http_inspect_403_not_404_for_a_missing_folder_when_denied(
    fake_backend: FakeFolderBackend,
) -> None:
    """A miss must not outrank the denial, or 404-vs-403 enumerates folder names."""
    client = _make_app(
        _config_with_access("access:\n  rules: []\n"),
        fake_backend,
    )

    resp = client.get(
        "/telegram/folders/No Such Folder",
        headers={"Authorization": "Bearer secret_token"},
    )

    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"]["error"] == "access_denied"


def test_http_inspect_404_for_a_missing_folder_when_granted(
    fake_backend: FakeFolderBackend,
) -> None:
    """The denial fence must not turn a genuine miss into a 403 for a grantee."""
    client = _make_app(
        _config_with_access("access:\n  rules:\n    - all: true\n      permissions: [read]\n"),
        fake_backend,
    )

    resp = client.get(
        "/telegram/folders/No Such Folder",
        headers={"Authorization": "Bearer secret_token"},
    )

    assert resp.status_code == 404, resp.text


def test_http_inspect_allowed_by_folder_read_rule(
    fake_backend: FakeFolderBackend,
) -> None:
    client = _make_app(
        _config_with_access(
            "access:\n"
            "  rules:\n"
            '    - folder: "Planfix clients"\n'
            "      permissions: [read]\n"
        ),
        fake_backend,
    )

    resp = client.get(
        "/telegram/folders/Planfix clients",
        headers={"Authorization": "Bearer secret_token"},
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["folder_id"] == 2


def test_http_inspect_allowed_by_folder_id_read_rule(
    fake_backend: FakeFolderBackend,
) -> None:
    """A `folder_id:` rule must grant an inspect by name.

    The caller has no reason to repeat the id in the query string, so gating on
    the request value instead of the resolved folder's own id would make the id
    rule kind unusable on this surface.
    """
    client = _make_app(
        _config_with_access(
            "access:\n  rules:\n    - folder_id: 2\n      permissions: [read]\n"
        ),
        fake_backend,
    )

    resp = client.get(
        "/telegram/folders/Planfix clients",
        headers={"Authorization": "Bearer secret_token"},
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["folder_id"] == 2


def test_http_inspect_folder_id_query_cannot_probe_other_folders(
    minimal_config_yaml: str,
) -> None:
    """A granted `folder_id` in the query must not authorize another name.

    The id is unverified request input on the resolution-failure path: honouring
    a `folder_id:` rule for it would let READ on one folder unlock the 404/409
    distinction (and the mismatch message's real ids) for every other title.
    """
    backend = FakeFolderBackend(
        [
            _sample_folder(),
            FolderSnapshot(
                folder_id=7,
                folder_name="Secret",
                chats=[FolderChat(chat_id=900, title="Board")],
            ),
        ]
    )
    client = _make_app(
        _config_with_access(
            "access:\n  rules:\n    - folder_id: 2\n      permissions: [read]\n"
        ),
        backend,
    )
    headers = {"Authorization": "Bearer secret_token"}

    # Existing title the caller has no rule for: 403, not a 409 disclosing id 7.
    mismatch = client.get("/telegram/folders/Secret?folder_id=2", headers=headers)
    assert mismatch.status_code == 403, mismatch.text
    assert "7" not in mismatch.text

    # Absent title: 403 too, so present and absent titles stay indistinguishable.
    missing = client.get("/telegram/folders/Nope?folder_id=2", headers=headers)
    assert missing.status_code == 403, missing.text

    # The rule still grants the folder it actually names, id in the query or not.
    granted = client.get(
        "/telegram/folders/Planfix clients?folder_id=2", headers=headers
    )
    assert granted.status_code == 200, granted.text
    assert granted.json()["folder_id"] == 2


def test_http_inspect_403_when_folder_id_rule_names_another_folder(
    fake_backend: FakeFolderBackend,
) -> None:
    client = _make_app(
        _config_with_access(
            "access:\n  rules:\n    - folder_id: 99\n      permissions: [read]\n"
        ),
        fake_backend,
    )

    resp = client.get(
        "/telegram/folders/Planfix clients",
        headers={"Authorization": "Bearer secret_token"},
    )

    assert resp.status_code == 403, resp.text
    assert "Acme" not in resp.text


def test_http_inspect_403_when_only_write_granted(
    fake_backend: FakeFolderBackend,
) -> None:
    """Capabilities are independent: `write` alone must not permit the listing."""
    client = _make_app(
        _config_with_access(
            "access:\n"
            "  rules:\n"
            '    - folder: "Planfix clients"\n'
            "      permissions: [write]\n"
        ),
        fake_backend,
    )

    resp = client.get(
        "/telegram/folders/Planfix clients",
        headers={"Authorization": "Bearer secret_token"},
    )

    assert resp.status_code == 403, resp.text


def test_http_inspect_requires_auth(
    minimal_config_yaml: str, fake_backend: FakeFolderBackend
) -> None:
    client = _make_app(minimal_config_yaml, fake_backend)
    resp = client.get("/telegram/folders/Planfix clients")
    assert resp.status_code == 401


def test_http_inspect_503_without_backend(minimal_config_yaml: str) -> None:
    config = load_config_from_text(minimal_config_yaml)
    app = create_app(config, session_manager=None)
    client = TestClient(app)
    resp = client.get(
        "/telegram/folders/Planfix clients",
        headers={"Authorization": "Bearer secret_token"},
    )
    assert resp.status_code == 503


def test_http_add_chat_by_id(
    minimal_config_yaml: str, fake_backend: FakeFolderBackend
) -> None:
    client = _make_app(minimal_config_yaml, fake_backend)
    resp = client.post(
        "/telegram/folders/Planfix clients/chats",
        json={"chat_id": 300},
        headers={"Authorization": "Bearer secret_token"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["chat_id"] == 300
    assert body["already_in_folder"] is False
    assert fake_backend.added == [(2, 300)]


def test_http_add_chat_by_name(
    minimal_config_yaml: str, fake_backend: FakeFolderBackend
) -> None:
    client = _make_app(minimal_config_yaml, fake_backend)
    resp = client.post(
        "/telegram/folders/Planfix clients/chats",
        json={"chat_name": "Acme"},
        headers={"Authorization": "Bearer secret_token"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["chat_id"] == 100
    assert body["already_in_folder"] is True


def test_http_add_chat_ambiguous_name_returns_409(
    minimal_config_yaml: str,
) -> None:
    folder = FolderSnapshot(
        folder_id=2,
        folder_name="Planfix clients",
        chats=[
            FolderChat(chat_id=100, title="Dup"),
            FolderChat(chat_id=200, title="Dup"),
        ],
    )
    client = _make_app(minimal_config_yaml, FakeFolderBackend([folder]))
    resp = client.post(
        "/telegram/folders/Planfix clients/chats",
        json={"chat_name": "Dup"},
        headers={"Authorization": "Bearer secret_token"},
    )
    assert resp.status_code == 409
    body = resp.json()
    assert body["detail"]["error"] == "ambiguous_chat_name"
    assert body["detail"]["matches"] == [100, 200]


def test_http_add_chat_peer_failure_returns_502(
    minimal_config_yaml: str,
) -> None:
    backend = FakeFolderBackend(
        [_sample_folder()], add_should_fail=True
    )
    client = _make_app(minimal_config_yaml, backend)
    resp = client.post(
        "/telegram/folders/Planfix clients/chats",
        json={"chat_id": 300},
        headers={"Authorization": "Bearer secret_token"},
    )
    assert resp.status_code == 502
    body = resp.json()
    assert body["detail"]["error"] == "needs_review"


def test_http_add_chat_requires_chat_ref(
    minimal_config_yaml: str, fake_backend: FakeFolderBackend
) -> None:
    client = _make_app(minimal_config_yaml, fake_backend)
    resp = client.post(
        "/telegram/folders/Planfix clients/chats",
        json={},
        headers={"Authorization": "Bearer secret_token"},
    )
    assert resp.status_code == 422


def test_http_remove_chat_by_id(
    minimal_config_yaml: str, fake_backend: FakeFolderBackend
) -> None:
    client = _make_app(minimal_config_yaml, fake_backend)
    resp = client.request(
        "DELETE",
        "/telegram/folders/Planfix clients/chats",
        json={"chat_id": 100},
        headers={"Authorization": "Bearer secret_token"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["chat_id"] == 100
    assert body["already_absent"] is False
    assert fake_backend.removed == [(2, 100)]


def test_http_remove_chat_absent_is_idempotent(
    minimal_config_yaml: str, fake_backend: FakeFolderBackend
) -> None:
    client = _make_app(minimal_config_yaml, fake_backend)
    resp = client.request(
        "DELETE",
        "/telegram/folders/Planfix clients/chats",
        json={"chat_id": 999},
        headers={"Authorization": "Bearer secret_token"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["chat_id"] == 999
    assert body["already_absent"] is True
    assert fake_backend.removed == []


def test_http_remove_chat_403_when_denied() -> None:
    backend = FakeFolderBackend([_sample_folder()])
    client = _make_app(
        _config_with_access("access:\n  rules: []\n"),
        backend,
    )

    resp = client.request(
        "DELETE",
        "/telegram/folders/Planfix clients/chats",
        json={"chat_id": 100},
        headers={"Authorization": "Bearer secret_token"},
    )

    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"]["error"] == "access_denied"
    assert backend.removed == []


def test_http_remove_chat_peer_failure_returns_502(
    minimal_config_yaml: str,
) -> None:
    backend = FakeFolderBackend(
        [_sample_folder()], remove_should_fail=True
    )
    client = _make_app(minimal_config_yaml, backend)
    resp = client.request(
        "DELETE",
        "/telegram/folders/Planfix clients/chats",
        json={"chat_id": 100},
        headers={"Authorization": "Bearer secret_token"},
    )
    assert resp.status_code == 502
    body = resp.json()
    assert body["detail"]["error"] == "needs_review"


def test_http_remove_chat_503_without_backend(minimal_config_yaml: str) -> None:
    config = load_config_from_text(minimal_config_yaml)
    app = create_app(config, session_manager=None)
    client = TestClient(app)
    resp = client.request(
        "DELETE",
        "/telegram/folders/Planfix clients/chats",
        json={"chat_id": 100},
        headers={"Authorization": "Bearer secret_token"},
    )
    assert resp.status_code == 503


# ---------------------------------------------------------------------------
# CLI commands
# ---------------------------------------------------------------------------


def _write_config(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "config.yml"
    p.write_text(body)
    return p


def _patch_cli_backend(
    monkeypatch: pytest.MonkeyPatch,
    backend: FakeFolderBackend,
) -> None:
    """Replace `_build_folder_backend` with one that yields ``backend``."""

    class _FakeManager:
        async def disconnect(self) -> None:
            return None

        async def get_client(self) -> object:
            return object()

    def _factory(config_path: Path | None) -> Any:
        from telegram_assistant.config import load_config

        async def _open() -> FakeFolderBackend:
            return backend

        return load_config(config_path), _FakeManager(), _open

    monkeypatch.setattr(cli_main, "_build_folder_backend", _factory)


def test_cli_folders_inspect_outputs_payload(
    minimal_config_yaml: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_file = _write_config(tmp_path, minimal_config_yaml)
    backend = FakeFolderBackend([_sample_folder()])
    _patch_cli_backend(monkeypatch, backend)

    runner = CliRunner()
    result = runner.invoke(
        cli_main.app,
        [
            "folders",
            "inspect",
            "--folder-name",
            "Planfix clients",
            "--config",
            str(config_file),
        ],
    )
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload["folder_id"] == 2
    assert payload["chats_count"] == 2


def test_cli_folders_inspect_stays_ungated_under_a_deny_all_policy(
    minimal_config_yaml: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The gate is deliberately remote-only — the local CLI must stay open.

    HTTP and MCP inspect are READ-gated because their payload enumerates every
    chat in the folder; the CLI is local and trusted, like `messages download
    --out`. Without this test, adding a gate to the CLI would break nothing.
    """
    deny_all = minimal_config_yaml.replace(
        "  defaults:\n", "  access:\n    rules: []\n  defaults:\n", 1
    )
    config_file = _write_config(tmp_path, deny_all)
    backend = FakeFolderBackend([_sample_folder()])
    _patch_cli_backend(monkeypatch, backend)

    runner = CliRunner()
    result = runner.invoke(
        cli_main.app,
        [
            "folders",
            "inspect",
            "--folder-name",
            "Planfix clients",
            "--config",
            str(config_file),
        ],
    )
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload["chats_count"] == 2


def test_cli_folders_inspect_uses_default_folder_name(
    minimal_config_yaml: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_file = _write_config(tmp_path, minimal_config_yaml)
    backend = FakeFolderBackend([_sample_folder()])
    _patch_cli_backend(monkeypatch, backend)

    runner = CliRunner()
    result = runner.invoke(
        cli_main.app,
        ["folders", "inspect", "--config", str(config_file)],
    )
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    # default_chat_folder.folder_name from the fixture is "Planfix clients"
    assert payload["folder_name"] == "Planfix clients"


def test_cli_folders_inspect_missing_folder_exits_2(
    minimal_config_yaml: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_file = _write_config(tmp_path, minimal_config_yaml)
    backend = FakeFolderBackend([])
    _patch_cli_backend(monkeypatch, backend)

    runner = CliRunner()
    result = runner.invoke(
        cli_main.app,
        [
            "folders",
            "inspect",
            "--folder-name",
            "Nope",
            "--config",
            str(config_file),
        ],
    )
    assert result.exit_code == 2
    # On exit_code != 0 typer routes the message through the runner; checking
    # stdout keeps the test stable across CliRunner versions.
    assert "Nope" in (result.stdout + (result.stderr or ""))


def test_cli_folders_add_chat_by_id(
    minimal_config_yaml: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_file = _write_config(tmp_path, minimal_config_yaml)
    backend = FakeFolderBackend([_sample_folder()])
    _patch_cli_backend(monkeypatch, backend)

    runner = CliRunner()
    result = runner.invoke(
        cli_main.app,
        [
            "folders",
            "add-chat",
            "--folder-name",
            "Planfix clients",
            "--chat-id",
            "300",
            "--config",
            str(config_file),
        ],
    )
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload["chat_id"] == 300
    assert payload["already_in_folder"] is False
    assert backend.added == [(2, 300)]


def test_cli_folders_add_chat_requires_chat_ref(
    minimal_config_yaml: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_file = _write_config(tmp_path, minimal_config_yaml)
    backend = FakeFolderBackend([_sample_folder()])
    _patch_cli_backend(monkeypatch, backend)

    runner = CliRunner()
    result = runner.invoke(
        cli_main.app,
        [
            "folders",
            "add-chat",
            "--folder-name",
            "Planfix clients",
            "--config",
            str(config_file),
        ],
    )
    assert result.exit_code == 2


def test_cli_folders_add_chat_ambiguous_name_fails(
    minimal_config_yaml: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_file = _write_config(tmp_path, minimal_config_yaml)
    folder = FolderSnapshot(
        folder_id=2,
        folder_name="Planfix clients",
        chats=[
            FolderChat(chat_id=100, title="Dup"),
            FolderChat(chat_id=200, title="Dup"),
        ],
    )
    backend = FakeFolderBackend([folder])
    _patch_cli_backend(monkeypatch, backend)

    runner = CliRunner()
    result = runner.invoke(
        cli_main.app,
        [
            "folders",
            "add-chat",
            "--folder-name",
            "Planfix clients",
            "--chat-name",
            "Dup",
            "--config",
            str(config_file),
        ],
    )
    assert result.exit_code == 2


def test_cli_folders_remove_chat_by_id(
    minimal_config_yaml: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_file = _write_config(tmp_path, minimal_config_yaml)
    backend = FakeFolderBackend([_sample_folder()])
    _patch_cli_backend(monkeypatch, backend)

    runner = CliRunner()
    result = runner.invoke(
        cli_main.app,
        [
            "folders",
            "remove-chat",
            "--folder-name",
            "Planfix clients",
            "--chat-id",
            "100",
            "--config",
            str(config_file),
        ],
    )
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload["chat_id"] == 100
    assert payload["already_absent"] is False
    assert backend.removed == [(2, 100)]


def test_cli_folders_remove_chat_absent_is_idempotent(
    minimal_config_yaml: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_file = _write_config(tmp_path, minimal_config_yaml)
    backend = FakeFolderBackend([_sample_folder()])
    _patch_cli_backend(monkeypatch, backend)

    runner = CliRunner()
    result = runner.invoke(
        cli_main.app,
        [
            "folders",
            "remove-chat",
            "--folder-name",
            "Planfix clients",
            "--chat-id",
            "999",
            "--config",
            str(config_file),
        ],
    )
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload["already_absent"] is True
    assert backend.removed == []


def test_cli_folders_remove_chat_403_when_denied(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_file = _write_config(
        tmp_path, _config_with_access("access:\n  rules: []\n")
    )
    backend = FakeFolderBackend([_sample_folder()])
    _patch_cli_backend(monkeypatch, backend)

    runner = CliRunner()
    result = runner.invoke(
        cli_main.app,
        [
            "folders",
            "remove-chat",
            "--folder-name",
            "Planfix clients",
            "--chat-id",
            "100",
            "--config",
            str(config_file),
        ],
    )

    assert result.exit_code == 3
    assert backend.removed == []


def test_cli_folders_remove_chat_requires_chat_ref(
    minimal_config_yaml: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_file = _write_config(tmp_path, minimal_config_yaml)
    backend = FakeFolderBackend([_sample_folder()])
    _patch_cli_backend(monkeypatch, backend)

    runner = CliRunner()
    result = runner.invoke(
        cli_main.app,
        [
            "folders",
            "remove-chat",
            "--folder-name",
            "Planfix clients",
            "--config",
            str(config_file),
        ],
    )
    assert result.exit_code == 2
