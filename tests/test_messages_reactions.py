from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from telegram_assistant.access import AccessDenied, AccessLevel
from telegram_assistant.cli import main as cli_main
from telegram_assistant.config import load_config_from_text
from telegram_assistant.folders import FolderChat, FolderSnapshot
from telegram_assistant.http_api import create_app
from telegram_assistant.messages import (
    MessageReactionRequest,
    TelethonMessageBackend,
    set_message_reaction,
)


class FakeReactionBackend:
    def __init__(self) -> None:
        self.reactions: list[dict[str, Any]] = []

    async def set_message_reaction(
        self, *, chat_id: int, message_id: int, emoji: str | None
    ) -> None:
        self.reactions.append(
            {"chat_id": chat_id, "message_id": message_id, "emoji": emoji}
        )

    async def list_topics(self, *, chat_id: int) -> list[Any]:
        return []


class FakeFolderBackend:
    def __init__(self) -> None:
        self.folders = [
            FolderSnapshot(
                folder_id=2,
                folder_name="Planfix clients",
                chats=[FolderChat(chat_id=42, title="Client")],
            )
        ]

    async def list_folders(self) -> list[FolderSnapshot]:
        return list(self.folders)

    async def resolve_chat(self, chat_ref: str | int) -> FolderChat:
        return FolderChat(chat_id=int(chat_ref), title=str(chat_ref))

    async def add_chat_to_folder(self, folder_id: int, chat_id: int) -> None:
        raise NotImplementedError

    async def remove_chat_from_folder(self, folder_id: int, chat_id: int) -> None:
        raise NotImplementedError


class DenyAuthorizer:
    async def require(self, chat_id: int, level: AccessLevel) -> None:
        raise AccessDenied(
            chat_ref=chat_id,
            required_level=level,
            granted_level=AccessLevel.READ,
            matched_rule="all",
        )


async def test_set_message_reaction_sets_emoji() -> None:
    backend = FakeReactionBackend()

    result = await set_message_reaction(
        backend=backend,
        request=MessageReactionRequest(
            telegram_chat_id=42,
            telegram_message_id=123,
            emoji="👍",
        ),
    )

    assert result.to_dict() == {
        "telegram_chat_id": 42,
        "telegram_message_id": 123,
        "emoji": "👍",
        "cleared": False,
    }
    assert backend.reactions == [{"chat_id": 42, "message_id": 123, "emoji": "👍"}]


async def test_set_message_reaction_clears() -> None:
    backend = FakeReactionBackend()

    result = await set_message_reaction(
        backend=backend,
        request=MessageReactionRequest(
            telegram_chat_id=42,
            telegram_message_id=123,
            clear=True,
        ),
    )

    assert result.to_dict()["cleared"] is True
    assert backend.reactions == [{"chat_id": 42, "message_id": 123, "emoji": None}]


@pytest.mark.parametrize(
    "reaction_request,match",
    [
        (
            MessageReactionRequest(
                telegram_chat_id=42, telegram_message_id=0, emoji="👍"
            ),
            "positive",
        ),
        (
            MessageReactionRequest(
                telegram_chat_id=42,
                telegram_message_id=1,
                emoji="👍",
                clear=True,
            ),
            "either emoji",
        ),
        (
            MessageReactionRequest(telegram_chat_id=42, telegram_message_id=1),
            "emoji or clear",
        ),
    ],
)
async def test_set_message_reaction_rejects_invalid_shape(
    reaction_request: MessageReactionRequest, match: str
) -> None:
    backend = FakeReactionBackend()

    with pytest.raises(ValueError, match=match):
        await set_message_reaction(backend=backend, request=reaction_request)

    assert backend.reactions == []


async def test_set_message_reaction_access_denied_before_backend_call() -> None:
    backend = FakeReactionBackend()

    with pytest.raises(AccessDenied):
        await set_message_reaction(
            backend=backend,
            request=MessageReactionRequest(
                telegram_chat_id=42,
                telegram_message_id=123,
                emoji="👍",
            ),
            authorizer=DenyAuthorizer(),  # type: ignore[arg-type]
        )

    assert backend.reactions == []


def _http_client(
    minimal_config_yaml: str, *, backend: FakeReactionBackend
) -> TestClient:
    app = create_app(
        load_config_from_text(minimal_config_yaml),
        message_backend_factory=lambda _request: backend,
        folder_backend_factory=lambda _request: FakeFolderBackend(),
    )
    return TestClient(app)


def test_http_messages_reactions_success(minimal_config_yaml: str) -> None:
    backend = FakeReactionBackend()
    client = _http_client(minimal_config_yaml, backend=backend)

    response = client.post(
        "/telegram/messages/reactions",
        headers={"Authorization": "Bearer secret_token"},
        json={"telegram_chat_id": 42, "message_id": 123, "emoji": "👍"},
    )

    assert response.status_code == 200, response.text
    assert response.json() == {
        "telegram_chat_id": 42,
        "telegram_message_id": 123,
        "emoji": "👍",
        "cleared": False,
    }
    assert backend.reactions == [{"chat_id": 42, "message_id": 123, "emoji": "👍"}]


def test_http_messages_reactions_rejects_invalid_shape(
    minimal_config_yaml: str,
) -> None:
    backend = FakeReactionBackend()
    client = _http_client(minimal_config_yaml, backend=backend)

    response = client.post(
        "/telegram/messages/reactions",
        headers={"Authorization": "Bearer secret_token"},
        json={
            "telegram_chat_id": 42,
            "message_id": 123,
            "emoji": "👍",
            "clear": True,
        },
    )

    assert response.status_code == 400, response.text
    assert backend.reactions == []


def test_http_messages_reactions_forbidden(minimal_config_yaml: str) -> None:
    config = load_config_from_text(
        minimal_config_yaml.replace(
            "  reserve_admins:",
            "  access:\n"
            "    rules:\n"
            "      - all: true\n"
            "        permission: read\n"
            "  reserve_admins:",
        )
    )
    backend = FakeReactionBackend()
    app = create_app(
        config,
        message_backend_factory=lambda _request: backend,
        folder_backend_factory=lambda _request: FakeFolderBackend(),
    )
    client = TestClient(app)

    response = client.post(
        "/telegram/messages/reactions",
        headers={"Authorization": "Bearer secret_token"},
        json={"telegram_chat_id": 42, "message_id": 123, "emoji": "👍"},
    )

    assert response.status_code == 403
    assert response.json()["detail"]["error"] == "access_denied"
    assert backend.reactions == []


def _write_config(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "config.yml"
    path.write_text(body)
    return path


def test_cli_messages_react_dry_run(
    minimal_config_yaml: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = _write_config(tmp_path, minimal_config_yaml)
    backend = FakeReactionBackend()

    class FakeManager:
        async def get_client(self) -> object:
            raise AssertionError("chat-id dry-run should not need a live client")

        async def disconnect(self) -> None:
            return None

    def factory(config_path_arg: Path | None) -> Any:
        config = load_config_from_text(minimal_config_yaml)

        async def open_backends() -> tuple[FakeReactionBackend, FakeFolderBackend]:
            return backend, FakeFolderBackend()

        return config, FakeManager(), object(), open_backends

    monkeypatch.setattr(cli_main, "_build_message_backends", factory)

    result = CliRunner().invoke(
        cli_main.app,
        [
            "messages",
            "react",
            "--chat-id",
            "42",
            "--message-id",
            "123",
            "--emoji",
            "👍",
            "--dry-run",
            "--config",
            str(config_path),
        ],
    )

    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["command"] == "messages.react"
    assert payload["status"] == "dry_run"
    assert payload["resolved"] == {
        "telegram_chat_id": 42,
        "telegram_message_id": 123,
        "emoji": "👍",
        "clear": False,
    }
    assert backend.reactions == []


async def test_telethon_message_backend_constructs_reaction_requests() -> None:
    class FakePeer:
        pass

    class FakeClient:
        def __init__(self) -> None:
            self.peer = FakePeer()
            self.requests: list[Any] = []

        async def get_input_entity(self, chat_id: int) -> FakePeer:
            assert chat_id == 42
            return self.peer

        async def __call__(self, request: Any) -> None:
            self.requests.append(request)

    client = FakeClient()
    backend = TelethonMessageBackend(client)

    await backend.set_message_reaction(chat_id=42, message_id=123, emoji="👍")
    await backend.set_message_reaction(chat_id=42, message_id=124, emoji=None)

    assert client.requests[0].peer is client.peer
    assert client.requests[0].msg_id == 123
    assert client.requests[0].reaction[0].emoticon == "👍"
    assert client.requests[1].peer is client.peer
    assert client.requests[1].msg_id == 124
    assert client.requests[1].reaction == []
