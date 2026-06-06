from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
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
from telegram_assistant.notifications import (
    FOREVER_MUTE_UNTIL,
    MuteRequest,
    TelethonNotificationBackend,
    mute_chat,
    unmute_chat,
)


class FakeNotificationBackend:
    def __init__(self) -> None:
        self.muted: list[dict[str, Any]] = []
        self.unmuted: list[int] = []

    async def mute_chat(
        self, *, chat_id: int, mute_until: datetime | None = None
    ) -> None:
        self.muted.append({"chat_id": chat_id, "mute_until": mute_until})

    async def unmute_chat(self, *, chat_id: int) -> None:
        self.unmuted.append(chat_id)


class DenyAuthorizer:
    async def require(self, chat_id: int, level: AccessLevel) -> None:
        raise AccessDenied(
            chat_ref=chat_id,
            required_level=level,
            granted_level=AccessLevel.READ,
            matched_rule="all",
        )


class FakeFolderBackend:
    async def list_folders(self) -> list[FolderSnapshot]:
        return [
            FolderSnapshot(
                folder_id=2,
                folder_name="Planfix clients",
                chats=[FolderChat(chat_id=42, title="Client")],
            )
        ]

    async def resolve_chat(self, chat_ref: str | int) -> FolderChat:
        return FolderChat(chat_id=int(chat_ref), title=str(chat_ref))

    async def add_chat_to_folder(self, folder_id: int, chat_id: int) -> None:
        raise NotImplementedError

    async def remove_chat_from_folder(self, folder_id: int, chat_id: int) -> None:
        raise NotImplementedError


@pytest.mark.asyncio
async def test_mute_chat_duration_and_result() -> None:
    backend = FakeNotificationBackend()
    now = datetime(2026, 6, 6, 12, 0, tzinfo=UTC)

    result = await mute_chat(
        backend=backend,
        request=MuteRequest(telegram_chat_id=42, duration=timedelta(hours=2)),
        now=now,
    )

    assert result.to_dict() == {
        "telegram_chat_id": 42,
        "muted": True,
        "mute_until": "2026-06-06T14:00:00+00:00",
        "muted_forever": False,
    }
    assert backend.muted == [
        {"chat_id": 42, "mute_until": datetime(2026, 6, 6, 14, 0, tzinfo=UTC)}
    ]


@pytest.mark.asyncio
async def test_unmute_chat_result() -> None:
    backend = FakeNotificationBackend()

    result = await unmute_chat(
        backend=backend,
        request=MuteRequest(telegram_chat_id=42),
    )

    assert result.to_dict() == {
        "telegram_chat_id": 42,
        "muted": False,
        "mute_until": None,
        "muted_forever": False,
    }
    assert backend.unmuted == [42]


def test_mute_request_rejects_invalid_duration() -> None:
    with pytest.raises(ValueError, match="duration"):
        MuteRequest(telegram_chat_id=42, duration=timedelta(seconds=0))


@pytest.mark.asyncio
async def test_mute_access_denied_before_backend_call() -> None:
    backend = FakeNotificationBackend()

    with pytest.raises(AccessDenied):
        await mute_chat(
            backend=backend,
            request=MuteRequest(telegram_chat_id=42),
            authorizer=DenyAuthorizer(),  # type: ignore[arg-type]
        )

    assert backend.muted == []


def _write_config(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "config.yml"
    path.write_text(body)
    return path


def _patch_notification_backends(
    monkeypatch: pytest.MonkeyPatch,
    *,
    backend: FakeNotificationBackend,
    config_body: str,
) -> None:
    class FakeManager:
        async def get_client(self) -> object:
            raise AssertionError("chat-id dry-run should not need a live client")

        async def disconnect(self) -> None:
            return None

    def factory(config_path: Path | None) -> Any:
        config = load_config_from_text(config_body)

        async def open_backends() -> tuple[FakeNotificationBackend, FakeFolderBackend]:
            return backend, FakeFolderBackend()

        return config, FakeManager(), open_backends

    monkeypatch.setattr(cli_main, "_build_notification_backends", factory)


def test_cli_notifications_mute_dry_run(
    minimal_config_yaml: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = _write_config(tmp_path, minimal_config_yaml)
    backend = FakeNotificationBackend()
    _patch_notification_backends(
        monkeypatch, backend=backend, config_body=minimal_config_yaml
    )

    result = CliRunner().invoke(
        cli_main.app,
        [
            "notifications",
            "mute",
            "--chat-id",
            "42",
            "--duration",
            "3",
            "--dry-run",
            "--config",
            str(config_path),
        ],
    )

    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["command"] == "notifications.mute"
    assert payload["status"] == "dry_run"
    assert payload["resolved"]["telegram_chat_id"] == 42
    assert payload["resolved"]["duration_hours"] == 3
    assert backend.muted == []


def test_cli_notifications_unmute_dry_run(
    minimal_config_yaml: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = _write_config(tmp_path, minimal_config_yaml)
    backend = FakeNotificationBackend()
    _patch_notification_backends(
        monkeypatch, backend=backend, config_body=minimal_config_yaml
    )

    result = CliRunner().invoke(
        cli_main.app,
        [
            "notifications",
            "unmute",
            "--chat-id",
            "42",
            "--dry-run",
            "--config",
            str(config_path),
        ],
    )

    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["command"] == "notifications.unmute"
    assert payload["resolved"]["telegram_chat_id"] == 42
    assert backend.unmuted == []


def test_http_notifications_mute_success(minimal_config_yaml: str) -> None:
    backend = FakeNotificationBackend()
    app = create_app(
        load_config_from_text(minimal_config_yaml),
        notification_backend_factory=lambda _request: backend,
        folder_backend_factory=lambda _request: FakeFolderBackend(),
    )
    client = TestClient(app)

    response = client.post(
        "/telegram/notifications/mute",
        headers={"Authorization": "Bearer secret_token"},
        json={"chat_id": 42, "duration_hours": 1},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["telegram_chat_id"] == 42
    assert body["muted"] is True
    assert body["muted_forever"] is False
    assert backend.muted[0]["chat_id"] == 42
    assert backend.muted[0]["mute_until"] is not None


def test_http_notifications_unmute_success(minimal_config_yaml: str) -> None:
    backend = FakeNotificationBackend()
    app = create_app(
        load_config_from_text(minimal_config_yaml),
        notification_backend_factory=lambda _request: backend,
        folder_backend_factory=lambda _request: FakeFolderBackend(),
    )
    client = TestClient(app)

    response = client.post(
        "/telegram/notifications/unmute",
        headers={"Authorization": "Bearer secret_token"},
        json={"chat_id": 42},
    )

    assert response.status_code == 200
    assert response.json()["muted"] is False
    assert backend.unmuted == [42]


def test_http_notifications_forbidden(minimal_config_yaml: str) -> None:
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
    app = create_app(
        config,
        notification_backend_factory=lambda _request: FakeNotificationBackend(),
        folder_backend_factory=lambda _request: FakeFolderBackend(),
    )
    client = TestClient(app)

    response = client.post(
        "/telegram/notifications/mute",
        headers={"Authorization": "Bearer secret_token"},
        json={"chat_id": 42},
    )

    assert response.status_code == 403
    assert response.json()["detail"]["error"] == "access_denied"


def test_http_notifications_backend_unavailable(minimal_config_yaml: str) -> None:
    app = create_app(
        load_config_from_text(minimal_config_yaml),
        notification_backend_factory=lambda _request: None,
    )
    client = TestClient(app)

    response = client.post(
        "/telegram/notifications/mute",
        headers={"Authorization": "Bearer secret_token"},
        json={"chat_id": 42},
    )

    assert response.status_code == 503


@pytest.mark.asyncio
async def test_telethon_notification_backend_constructs_mute_and_unmute_requests() -> None:
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
    backend = TelethonNotificationBackend(client)
    until = datetime(2026, 6, 6, 14, 0, tzinfo=UTC)

    await backend.mute_chat(chat_id=42, mute_until=until)
    await backend.mute_chat(chat_id=42)
    await backend.unmute_chat(chat_id=42)

    assert client.requests[0].peer.peer is client.peer
    assert client.requests[0].settings.mute_until == until
    assert client.requests[1].settings.mute_until == FOREVER_MUTE_UNTIL
    # Unmute must send an explicit epoch-0 clear; ``None`` would omit the flag
    # and leave any existing mute in place.
    assert client.requests[2].settings.mute_until == 0
