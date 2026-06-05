"""Tests for chat-folder resolution and folder-membership operations.

Covers Task 6 of the MVP plan: folder lookup, ``folder_id`` cross-check, chat
name disambiguation, the inspect/add-chat HTTP routes, and the matching CLI
commands. All tests use an in-memory :class:`FolderBackend` so they never
touch Telethon — the Telethon adapter is exercised only through type checks.
"""

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
    ) -> None:
        self._folders = folders
        self._known_chats = known_chats or {}
        self._add_should_fail = add_should_fail
        self._add_should_raise = add_should_raise
        self.added: list[tuple[int, int]] = []

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

    def _factory(config_path: Path | None) -> Any:
        async def _open() -> FakeFolderBackend:
            return backend

        return None, _FakeManager(), _open

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
