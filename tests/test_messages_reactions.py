"""Tests for Task 6 — message reactions (domain, Telethon, CLI, HTTP)."""

from __future__ import annotations

import json
import tempfile
import textwrap
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from telegram_assistant.cli import main as cli_main
from telegram_assistant.config import load_config_from_text
from telegram_assistant.folders import FolderChat, FolderSnapshot
from telegram_assistant.http_api import create_app
from telegram_assistant.messages import (
    SendReactionRequest,
    set_message_reaction,
)
from telegram_assistant.messages.telethon_backend import TelethonReactionBackend
from telegram_assistant.persistence import OperationStore
from telegram_assistant.worker.queue import FloodWaitError

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeReactionBackend:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def set_reaction(
        self, *, chat_id: int, message_id: int, emoji: str | None
    ) -> None:
        self.calls.append(
            {"chat_id": chat_id, "message_id": message_id, "emoji": emoji}
        )


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

    async def remove_chat_from_folder(self, folder_id: int, chat_id: int) -> None:
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Domain tests
# ---------------------------------------------------------------------------


async def test_set_reaction_passes_emoji_to_backend() -> None:
    backend = FakeReactionBackend()
    result = await set_message_reaction(
        backend,
        request=SendReactionRequest(
            telegram_chat_id=-100, message_id=42, emoji="👍", chat_name="Acme"
        ),
    )
    assert result.emoji == "👍"
    assert result.cleared is False
    assert result.telegram_message_id == 42
    assert result.chat_name == "Acme"
    assert backend.calls == [
        {"chat_id": -100, "message_id": 42, "emoji": "👍"}
    ]


async def test_clear_reaction_passes_none_to_backend() -> None:
    backend = FakeReactionBackend()
    result = await set_message_reaction(
        backend,
        request=SendReactionRequest(
            telegram_chat_id=-100, message_id=42, clear=True
        ),
    )
    assert result.emoji is None
    assert result.cleared is True
    assert backend.calls == [
        {"chat_id": -100, "message_id": 42, "emoji": None}
    ]


async def test_reaction_rejects_non_positive_message_id() -> None:
    backend = FakeReactionBackend()
    with pytest.raises(ValueError):
        await set_message_reaction(
            backend,
            request=SendReactionRequest(
                telegram_chat_id=-100, message_id=0, emoji="👍"
            ),
        )
    assert backend.calls == []


async def test_reaction_rejects_emoji_and_clear_together() -> None:
    backend = FakeReactionBackend()
    with pytest.raises(ValueError):
        await set_message_reaction(
            backend,
            request=SendReactionRequest(
                telegram_chat_id=-100, message_id=1, emoji="👍", clear=True
            ),
        )
    assert backend.calls == []


async def test_reaction_rejects_neither_emoji_nor_clear() -> None:
    backend = FakeReactionBackend()
    with pytest.raises(ValueError):
        await set_message_reaction(
            backend,
            request=SendReactionRequest(telegram_chat_id=-100, message_id=1),
        )
    assert backend.calls == []


async def test_reaction_denied_before_backend_call() -> None:
    from telegram_assistant.access import AccessDenied, Authorizer
    from telegram_assistant.config.models import AccessConfig, AccessRule

    backend = FakeReactionBackend()
    authorizer = Authorizer(
        AccessConfig(rules=[AccessRule(all=True, permission="read")])
    )
    with pytest.raises(AccessDenied):
        await set_message_reaction(
            backend,
            request=SendReactionRequest(
                telegram_chat_id=-100, message_id=1, emoji="👍"
            ),
            authorizer=authorizer,
        )
    assert backend.calls == []


# ---------------------------------------------------------------------------
# Telethon adapter tests (fake client recording the constructed request)
# ---------------------------------------------------------------------------


class FakeTelethonClient:
    def __init__(self, *, raise_on_call: Exception | None = None) -> None:
        self.requests: list[Any] = []
        self._raise_on_call = raise_on_call

    async def get_input_entity(self, chat_id: int) -> str:
        return f"peer:{chat_id}"

    async def __call__(self, request: Any) -> Any:
        if self._raise_on_call is not None:
            raise self._raise_on_call
        self.requests.append(request)
        return "ok"


async def test_telethon_set_reaction_builds_request() -> None:
    client = FakeTelethonClient()
    backend = TelethonReactionBackend(client)
    await backend.set_reaction(chat_id=-100, message_id=7, emoji="🔥")
    assert len(client.requests) == 1
    req = client.requests[0]
    assert req.peer == "peer:-100"
    assert req.msg_id == 7
    assert [r.emoticon for r in req.reaction] == ["🔥"]


async def test_telethon_clear_reaction_builds_request() -> None:
    client = FakeTelethonClient()
    backend = TelethonReactionBackend(client)
    await backend.set_reaction(chat_id=-100, message_id=7, emoji=None)
    req = client.requests[0]
    assert req.reaction is None


async def test_telethon_reaction_translates_flood_wait() -> None:
    class _Flood(Exception):
        def __init__(self) -> None:
            self.seconds = 7

    _Flood.__name__ = "FloodWaitError"

    client = FakeTelethonClient(raise_on_call=_Flood())
    backend = TelethonReactionBackend(client)
    with pytest.raises(FloodWaitError):
        await backend.set_reaction(chat_id=-100, message_id=7, emoji="👍")


# ---------------------------------------------------------------------------
# CLI tests
# ---------------------------------------------------------------------------


def _write_config(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "config.yml"
    p.write_text(body)
    return p


def _patch_cli_reaction_backends(
    monkeypatch: pytest.MonkeyPatch,
    backend: FakeReactionBackend,
    folder_backend: FakeFolderBackend,
) -> None:
    class _FakeManager:
        async def disconnect(self) -> None:
            return None

        async def get_client(self) -> Any:  # pragma: no cover - unused without entity
            raise NotImplementedError

    def _factory(config_path: Path | None) -> Any:
        from telegram_assistant.config import load_config

        config = load_config(config_path)

        async def _open() -> Any:
            return backend, folder_backend

        return config, _FakeManager(), _open

    monkeypatch.setattr(cli_main, "_build_reaction_backends", _factory)


def test_cli_react_dry_run(
    minimal_config_yaml: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_file = _write_config(tmp_path, minimal_config_yaml)
    backend = FakeReactionBackend()
    folder_backend = FakeFolderBackend([])
    _patch_cli_reaction_backends(monkeypatch, backend, folder_backend)

    runner = CliRunner()
    result = runner.invoke(
        cli_main.app,
        [
            "messages",
            "react",
            "--chat-id",
            "-100",
            "--message-id",
            "42",
            "--emoji",
            "👍",
            "--dry-run",
            "--config",
            str(config_file),
        ],
    )
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload["dry_run"] is True
    assert payload["command"] == "messages.react"
    assert payload["resolved"]["telegram_chat_id"] == -100
    assert payload["resolved"]["telegram_message_id"] == 42
    assert payload["resolved"]["emoji"] == "👍"
    assert payload["resolved"]["cleared"] is False
    assert backend.calls == []


def test_cli_react_real(
    minimal_config_yaml: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_file = _write_config(tmp_path, minimal_config_yaml)
    backend = FakeReactionBackend()
    folder_backend = FakeFolderBackend([])
    _patch_cli_reaction_backends(monkeypatch, backend, folder_backend)

    runner = CliRunner()
    result = runner.invoke(
        cli_main.app,
        [
            "messages",
            "react",
            "--chat-id",
            "-100",
            "--message-id",
            "42",
            "--emoji",
            "🔥",
            "--config",
            str(config_file),
        ],
    )
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload["emoji"] == "🔥"
    assert payload["cleared"] is False
    assert backend.calls == [
        {"chat_id": -100, "message_id": 42, "emoji": "🔥"}
    ]


def test_cli_react_clear_real(
    minimal_config_yaml: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_file = _write_config(tmp_path, minimal_config_yaml)
    backend = FakeReactionBackend()
    folder_backend = FakeFolderBackend([])
    _patch_cli_reaction_backends(monkeypatch, backend, folder_backend)

    runner = CliRunner()
    result = runner.invoke(
        cli_main.app,
        [
            "messages",
            "react",
            "--chat-id",
            "-100",
            "--message-id",
            "42",
            "--clear",
            "--config",
            str(config_file),
        ],
    )
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload["cleared"] is True
    assert backend.calls == [
        {"chat_id": -100, "message_id": 42, "emoji": None}
    ]


def test_cli_react_rejects_emoji_and_clear(
    minimal_config_yaml: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_file = _write_config(tmp_path, minimal_config_yaml)
    backend = FakeReactionBackend()
    folder_backend = FakeFolderBackend([])
    _patch_cli_reaction_backends(monkeypatch, backend, folder_backend)

    runner = CliRunner()
    result = runner.invoke(
        cli_main.app,
        [
            "messages",
            "react",
            "--chat-id",
            "-100",
            "--message-id",
            "42",
            "--emoji",
            "👍",
            "--clear",
            "--config",
            str(config_file),
        ],
    )
    assert result.exit_code == 2


# ---------------------------------------------------------------------------
# HTTP tests
# ---------------------------------------------------------------------------

AUTH = {"Authorization": "Bearer secret_token"}


def _make_store() -> OperationStore:
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".db")
    tmp.close()
    return OperationStore(Path(tmp.name))


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


def _http_client(
    *,
    access_block: str | None = None,
    reaction_backend: FakeReactionBackend | None = None,
    has_factory: bool = True,
) -> TestClient:
    config = load_config_from_text(_config_with_access(access_block))
    app = create_app(
        config,
        session_manager=None,
        reaction_backend_factory=(
            (lambda _r: reaction_backend) if has_factory else (lambda _r: None)
        ),
        folder_backend_factory=lambda _r: None,
        resolver_factory=lambda _r: None,
        operation_store=_make_store(),
    )
    return TestClient(app)


def test_http_react_success() -> None:
    backend = FakeReactionBackend()
    client = _http_client(reaction_backend=backend)
    resp = client.post(
        "/telegram/messages/reactions",
        json={"telegram_chat_id": -100, "message_id": 42, "emoji": "👍"},
        headers=AUTH,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["emoji"] == "👍"
    assert body["cleared"] is False
    assert body["telegram_message_id"] == 42
    assert len(backend.calls) == 1


def test_http_react_clear_success() -> None:
    backend = FakeReactionBackend()
    client = _http_client(reaction_backend=backend)
    resp = client.post(
        "/telegram/messages/reactions",
        json={"telegram_chat_id": -100, "message_id": 42, "clear": True},
        headers=AUTH,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["cleared"] is True
    assert backend.calls == [
        {"chat_id": -100, "message_id": 42, "emoji": None}
    ]


def test_http_react_requires_auth() -> None:
    backend = FakeReactionBackend()
    client = _http_client(reaction_backend=backend)
    resp = client.post(
        "/telegram/messages/reactions",
        json={"telegram_chat_id": -100, "message_id": 42, "emoji": "👍"},
    )
    assert resp.status_code == 401


def test_http_react_503_when_backend_unavailable() -> None:
    client = _http_client(has_factory=False)
    resp = client.post(
        "/telegram/messages/reactions",
        json={"telegram_chat_id": -100, "message_id": 42, "emoji": "👍"},
        headers=AUTH,
    )
    assert resp.status_code == 503, resp.text


def test_http_react_400_emoji_and_clear() -> None:
    backend = FakeReactionBackend()
    client = _http_client(reaction_backend=backend)
    resp = client.post(
        "/telegram/messages/reactions",
        json={
            "telegram_chat_id": -100,
            "message_id": 42,
            "emoji": "👍",
            "clear": True,
        },
        headers=AUTH,
    )
    # Pydantic body validation rejects the conflicting shape.
    assert resp.status_code == 422, resp.text
    assert backend.calls == []


def test_http_react_403_when_denied() -> None:
    backend = FakeReactionBackend()
    client = _http_client(
        access_block="access:\n  rules: []\n", reaction_backend=backend
    )
    resp = client.post(
        "/telegram/messages/reactions",
        json={"telegram_chat_id": -100, "message_id": 42, "emoji": "👍"},
        headers=AUTH,
    )
    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"]["error"] == "access_denied"
    assert backend.calls == []
