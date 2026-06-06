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
    ForwardMessagesRequest,
    TelethonMessageBackend,
    forward_messages,
)


class FakeForwardBackend:
    def __init__(self, returned_ids: tuple[int, ...] = (501, 502)) -> None:
        self.returned_ids = returned_ids
        self.forwarded: list[dict[str, Any]] = []

    async def forward_messages(
        self,
        *,
        source_chat_id: int,
        target_chat_id: int,
        message_ids: tuple[int, ...],
    ) -> tuple[int, ...]:
        self.forwarded.append(
            {
                "source_chat_id": source_chat_id,
                "target_chat_id": target_chat_id,
                "message_ids": message_ids,
            }
        )
        return self.returned_ids


class FakeFolderBackend:
    def __init__(self) -> None:
        self.folders = [
            FolderSnapshot(
                folder_id=2,
                folder_name="Planfix clients",
                chats=[
                    FolderChat(chat_id=42, title="Source"),
                    FolderChat(chat_id=43, title="Target"),
                ],
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
    def __init__(self, denied_chat_id: int, granted_level: AccessLevel | None) -> None:
        self.denied_chat_id = denied_chat_id
        self.granted_level = granted_level
        self.calls: list[tuple[int, AccessLevel]] = []

    async def require(self, chat_id: int, level: AccessLevel) -> None:
        self.calls.append((chat_id, level))
        if chat_id == self.denied_chat_id:
            raise AccessDenied(
                chat_ref=chat_id,
                required_level=level,
                granted_level=self.granted_level,
                matched_rule="all",
            )


async def test_forward_messages_returns_forwarded_ids() -> None:
    backend = FakeForwardBackend(returned_ids=(701, 702))

    result = await forward_messages(
        backend=backend,
        request=ForwardMessagesRequest(
            source_chat_id=42,
            target_chat_id=43,
            message_ids=(101, 102),
        ),
    )

    assert result.to_dict() == {
        "source_chat_id": 42,
        "target_chat_id": 43,
        "message_ids": [101, 102],
        "forwarded_message_ids": [701, 702],
    }
    assert backend.forwarded == [
        {
            "source_chat_id": 42,
            "target_chat_id": 43,
            "message_ids": (101, 102),
        }
    ]


@pytest.mark.parametrize("message_ids", [(), (0,), (-1,), (1, 0)])
async def test_forward_messages_rejects_invalid_message_ids(
    message_ids: tuple[int, ...],
) -> None:
    backend = FakeForwardBackend()

    with pytest.raises(ValueError, match="positive"):
        await forward_messages(
            backend=backend,
            request=ForwardMessagesRequest(
                source_chat_id=42,
                target_chat_id=43,
                message_ids=message_ids,
            ),
        )

    assert backend.forwarded == []


async def test_forward_messages_requires_read_on_source_before_backend_call() -> None:
    backend = FakeForwardBackend()
    authorizer = DenyAuthorizer(denied_chat_id=42, granted_level=None)

    with pytest.raises(AccessDenied):
        await forward_messages(
            backend=backend,
            request=ForwardMessagesRequest(
                source_chat_id=42,
                target_chat_id=43,
                message_ids=(101,),
            ),
            authorizer=authorizer,  # type: ignore[arg-type]
        )

    assert authorizer.calls == [(42, AccessLevel.READ)]
    assert backend.forwarded == []


async def test_forward_messages_requires_write_on_target_before_backend_call() -> None:
    backend = FakeForwardBackend()
    authorizer = DenyAuthorizer(denied_chat_id=43, granted_level=AccessLevel.READ)

    with pytest.raises(AccessDenied):
        await forward_messages(
            backend=backend,
            request=ForwardMessagesRequest(
                source_chat_id=42,
                target_chat_id=43,
                message_ids=(101,),
            ),
            authorizer=authorizer,  # type: ignore[arg-type]
        )

    assert authorizer.calls == [(42, AccessLevel.READ), (43, AccessLevel.WRITE)]
    assert backend.forwarded == []


def _write_config(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "config.yml"
    path.write_text(body)
    return path


def test_cli_messages_forward_dry_run(
    minimal_config_yaml: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = _write_config(tmp_path, minimal_config_yaml)
    backend = FakeForwardBackend()

    class FakeManager:
        async def get_client(self) -> object:
            raise AssertionError("chat-id dry-run should not need a live client")

        async def disconnect(self) -> None:
            return None

    def factory(config_path_arg: Path | None) -> Any:
        config = load_config_from_text(minimal_config_yaml)

        async def open_backends() -> tuple[FakeForwardBackend, object, FakeFolderBackend]:
            return backend, object(), FakeFolderBackend()

        return config, FakeManager(), object(), open_backends

    monkeypatch.setattr(cli_main, "_build_message_backends", factory)

    result = CliRunner().invoke(
        cli_main.app,
        [
            "messages",
            "forward",
            "--from-chat-id",
            "42",
            "--to-chat-id",
            "43",
            "--message-id",
            "101",
            "--message-id",
            "102",
            "--dry-run",
            "--config",
            str(config_path),
        ],
    )

    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["command"] == "messages.forward"
    assert payload["status"] == "dry_run"
    assert payload["resolved"] == {
        "source_chat_id": 42,
        "target_chat_id": 43,
        "message_ids": [101, 102],
    }
    assert backend.forwarded == []


def _http_client(
    minimal_config_yaml: str, *, backend: FakeForwardBackend
) -> TestClient:
    app = create_app(
        load_config_from_text(minimal_config_yaml),
        message_backend_factory=lambda _request: backend,
        folder_backend_factory=lambda _request: FakeFolderBackend(),
    )
    return TestClient(app)


def test_http_messages_forward_success(minimal_config_yaml: str) -> None:
    backend = FakeForwardBackend(returned_ids=(801, 802))
    client = _http_client(minimal_config_yaml, backend=backend)

    response = client.post(
        "/telegram/messages/forward",
        headers={"Authorization": "Bearer secret_token"},
        json={
            "from_chat_id": 42,
            "to_chat_id": 43,
            "message_ids": [101, 102],
        },
    )

    assert response.status_code == 200, response.text
    assert response.json() == {
        "source_chat_id": 42,
        "target_chat_id": 43,
        "message_ids": [101, 102],
        "forwarded_message_ids": [801, 802],
    }
    assert backend.forwarded == [
        {
            "source_chat_id": 42,
            "target_chat_id": 43,
            "message_ids": (101, 102),
        }
    ]


def test_http_messages_forward_forbidden_on_target(
    minimal_config_yaml: str,
) -> None:
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
    backend = FakeForwardBackend()
    app = create_app(
        config,
        message_backend_factory=lambda _request: backend,
        folder_backend_factory=lambda _request: FakeFolderBackend(),
    )
    client = TestClient(app)

    response = client.post(
        "/telegram/messages/forward",
        headers={"Authorization": "Bearer secret_token"},
        json={
            "from_chat_id": 42,
            "to_chat_id": 43,
            "message_ids": [101],
        },
    )

    assert response.status_code == 403
    assert response.json()["detail"]["error"] == "access_denied"
    assert response.json()["detail"]["required_level"] == "write"
    assert backend.forwarded == []


async def test_telethon_message_backend_forwards_messages() -> None:
    class FakeMessage:
        def __init__(self, message_id: int) -> None:
            self.id = message_id

    class FakeClient:
        def __init__(self) -> None:
            self.calls: list[tuple[Any, Any, dict[str, Any]]] = []

        async def forward_messages(
            self, target: int, messages: tuple[int, ...], **kwargs: Any
        ) -> list[FakeMessage]:
            self.calls.append((target, messages, kwargs))
            return [FakeMessage(901), FakeMessage(902)]

    client = FakeClient()
    backend = TelethonMessageBackend(client)

    result = await backend.forward_messages(
        source_chat_id=42,
        target_chat_id=43,
        message_ids=(101, 102),
    )

    assert result == (901, 902)
    assert client.calls == [(43, (101, 102), {"from_peer": 42})]
