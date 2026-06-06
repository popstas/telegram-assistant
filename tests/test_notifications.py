"""Tests for Task 5 — notifications mute/unmute (domain, Telethon, CLI, HTTP)."""

from __future__ import annotations

import json
import tempfile
import textwrap
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from telegram_assistant.cli import main as cli_main
from telegram_assistant.config import load_config_from_text
from telegram_assistant.folders import FolderChat, FolderSnapshot
from telegram_assistant.http_api import create_app
from telegram_assistant.notifications import (
    MuteRequest,
    mute_chat,
    unmute_chat,
)
from telegram_assistant.notifications.telethon_backend import (
    _MUTE_FOREVER_UNTIL,
    TelethonNotificationBackend,
)
from telegram_assistant.persistence import OperationStore
from telegram_assistant.worker.queue import FloodWaitError

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeNotificationBackend:
    def __init__(
        self,
        *,
        raise_on_mute: Exception | None = None,
        raise_on_unmute: Exception | None = None,
    ) -> None:
        self.muted: list[dict[str, Any]] = []
        self.unmuted: list[dict[str, Any]] = []
        self._raise_on_mute = raise_on_mute
        self._raise_on_unmute = raise_on_unmute

    async def mute_chat(self, *, chat_id: int, mute_until: datetime | None) -> None:
        if self._raise_on_mute is not None:
            raise self._raise_on_mute
        self.muted.append({"chat_id": chat_id, "mute_until": mute_until})

    async def unmute_chat(self, *, chat_id: int) -> None:
        if self._raise_on_unmute is not None:
            raise self._raise_on_unmute
        self.unmuted.append({"chat_id": chat_id})


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


async def test_mute_forever_passes_none_to_backend() -> None:
    backend = FakeNotificationBackend()
    result = await mute_chat(
        backend, request=MuteRequest(telegram_chat_id=-100, chat_name="Acme")
    )
    assert result.muted is True
    assert result.duration_hours is None
    assert result.mute_until is None
    assert result.chat_name == "Acme"
    assert backend.muted == [{"chat_id": -100, "mute_until": None}]


async def test_mute_with_duration_computes_mute_until() -> None:
    backend = FakeNotificationBackend()
    now = datetime(2030, 1, 1, 12, 0, tzinfo=UTC)
    result = await mute_chat(
        backend,
        request=MuteRequest(telegram_chat_id=-100, duration_hours=3),
        now=now,
    )
    assert result.muted is True
    assert result.duration_hours == 3
    expected = datetime(2030, 1, 1, 15, 0, tzinfo=UTC)
    assert result.mute_until == expected.isoformat()
    assert backend.muted[0]["mute_until"] == expected


async def test_mute_rejects_non_positive_duration() -> None:
    backend = FakeNotificationBackend()
    with pytest.raises(ValueError):
        await mute_chat(
            backend, request=MuteRequest(telegram_chat_id=-100, duration_hours=0)
        )
    assert backend.muted == []


async def test_mute_rejects_unrepresentable_duration() -> None:
    backend = FakeNotificationBackend()
    with pytest.raises(ValueError):
        await mute_chat(
            backend, request=MuteRequest(telegram_chat_id=-100, duration_hours=10**20)
        )
    assert backend.muted == []


async def test_mute_rejects_duration_beyond_telegram_limit() -> None:
    backend = FakeNotificationBackend()
    now = datetime(2037, 12, 31, 23, 0, tzinfo=UTC)
    with pytest.raises(ValueError):
        await mute_chat(
            backend,
            request=MuteRequest(telegram_chat_id=-100, duration_hours=2),
            now=now,
        )
    assert backend.muted == []


async def test_unmute_calls_backend() -> None:
    backend = FakeNotificationBackend()
    result = await unmute_chat(
        backend, request=MuteRequest(telegram_chat_id=-100, chat_name="Acme")
    )
    assert result.muted is False
    assert result.mute_until is None
    assert backend.unmuted == [{"chat_id": -100}]


async def test_mute_denied_before_backend_call() -> None:
    from telegram_assistant.access import AccessDenied, Authorizer
    from telegram_assistant.config.models import AccessConfig, AccessRule

    backend = FakeNotificationBackend()
    authorizer = Authorizer(
        AccessConfig(rules=[AccessRule(all=True, permission="read")])
    )
    with pytest.raises(AccessDenied):
        await mute_chat(
            backend,
            request=MuteRequest(telegram_chat_id=-100),
            authorizer=authorizer,
        )
    assert backend.muted == []


async def test_unmute_denied_before_backend_call() -> None:
    from telegram_assistant.access import AccessDenied, Authorizer
    from telegram_assistant.config.models import AccessConfig, AccessRule

    backend = FakeNotificationBackend()
    authorizer = Authorizer(
        AccessConfig(rules=[AccessRule(all=True, permission="read")])
    )
    with pytest.raises(AccessDenied):
        await unmute_chat(
            backend,
            request=MuteRequest(telegram_chat_id=-100),
            authorizer=authorizer,
        )
    assert backend.unmuted == []


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


async def test_telethon_mute_forever_builds_request() -> None:
    from telethon.tl.types import InputNotifyPeer

    client = FakeTelethonClient()
    backend = TelethonNotificationBackend(client)
    await backend.mute_chat(chat_id=-100, mute_until=None)
    assert len(client.requests) == 1
    req = client.requests[0]
    assert isinstance(req.peer, InputNotifyPeer)
    assert req.peer.peer == "peer:-100"
    assert req.settings.mute_until == _MUTE_FOREVER_UNTIL


async def test_telethon_mute_until_builds_request() -> None:
    client = FakeTelethonClient()
    backend = TelethonNotificationBackend(client)
    when = datetime(2030, 6, 1, tzinfo=UTC)
    await backend.mute_chat(chat_id=42, mute_until=when)
    req = client.requests[0]
    assert req.peer.peer == "peer:42"
    assert req.settings.mute_until == when


async def test_telethon_unmute_builds_request() -> None:
    client = FakeTelethonClient()
    backend = TelethonNotificationBackend(client)
    await backend.unmute_chat(chat_id=-100)
    req = client.requests[0]
    assert req.peer.peer == "peer:-100"
    assert req.settings.mute_until == 0


async def test_telethon_translates_flood_wait() -> None:
    # ``translate_flood_wait`` matches Telethon's error by class name, so name
    # the stand-in accordingly without importing Telethon's own exception.
    class _Flood(Exception):
        def __init__(self) -> None:
            self.seconds = 7

    _Flood.__name__ = "FloodWaitError"

    client = FakeTelethonClient(raise_on_call=_Flood())
    backend = TelethonNotificationBackend(client)
    with pytest.raises(FloodWaitError):
        await backend.mute_chat(chat_id=-100, mute_until=None)


# ---------------------------------------------------------------------------
# CLI tests
# ---------------------------------------------------------------------------


def _write_config(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "config.yml"
    p.write_text(body)
    return p


def _patch_cli_notification_backends(
    monkeypatch: pytest.MonkeyPatch,
    backend: FakeNotificationBackend,
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

    monkeypatch.setattr(cli_main, "_build_notification_backends", _factory)


def test_cli_mute_dry_run(
    minimal_config_yaml: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_file = _write_config(tmp_path, minimal_config_yaml)
    backend = FakeNotificationBackend()
    folder_backend = FakeFolderBackend([])
    _patch_cli_notification_backends(monkeypatch, backend, folder_backend)

    runner = CliRunner()
    result = runner.invoke(
        cli_main.app,
        [
            "notifications",
            "mute",
            "--chat-id",
            "-100",
            "--duration",
            "5",
            "--dry-run",
            "--config",
            str(config_file),
        ],
    )
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload["dry_run"] is True
    assert payload["command"] == "notifications.mute"
    assert payload["resolved"]["telegram_chat_id"] == -100
    assert payload["resolved"]["duration_hours"] == 5
    assert payload["resolved"]["forever"] is False
    # Dry-run must not touch the backend.
    assert backend.muted == []


def test_cli_unmute_dry_run(
    minimal_config_yaml: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_file = _write_config(tmp_path, minimal_config_yaml)
    backend = FakeNotificationBackend()
    folder_backend = FakeFolderBackend([])
    _patch_cli_notification_backends(monkeypatch, backend, folder_backend)

    runner = CliRunner()
    result = runner.invoke(
        cli_main.app,
        [
            "notifications",
            "unmute",
            "--chat-id",
            "-100",
            "--dry-run",
            "--config",
            str(config_file),
        ],
    )
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload["command"] == "notifications.unmute"
    assert payload["resolved"]["telegram_chat_id"] == -100
    assert backend.unmuted == []


def test_cli_mute_real(
    minimal_config_yaml: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_file = _write_config(tmp_path, minimal_config_yaml)
    backend = FakeNotificationBackend()
    folder_backend = FakeFolderBackend([])
    _patch_cli_notification_backends(monkeypatch, backend, folder_backend)

    runner = CliRunner()
    result = runner.invoke(
        cli_main.app,
        [
            "notifications",
            "mute",
            "--chat-id",
            "-100",
            "--config",
            str(config_file),
        ],
    )
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload["muted"] is True
    assert payload["telegram_chat_id"] == -100
    assert backend.muted == [{"chat_id": -100, "mute_until": None}]


def test_cli_unmute_real(
    minimal_config_yaml: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_file = _write_config(tmp_path, minimal_config_yaml)
    backend = FakeNotificationBackend()
    folder_backend = FakeFolderBackend([])
    _patch_cli_notification_backends(monkeypatch, backend, folder_backend)

    runner = CliRunner()
    result = runner.invoke(
        cli_main.app,
        [
            "notifications",
            "unmute",
            "--chat-id",
            "-100",
            "--config",
            str(config_file),
        ],
    )
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload["muted"] is False
    assert backend.unmuted == [{"chat_id": -100}]


def test_cli_mute_requires_exactly_one_ref(
    minimal_config_yaml: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_file = _write_config(tmp_path, minimal_config_yaml)
    backend = FakeNotificationBackend()
    folder_backend = FakeFolderBackend([])
    _patch_cli_notification_backends(monkeypatch, backend, folder_backend)

    runner = CliRunner()
    result = runner.invoke(
        cli_main.app,
        ["notifications", "mute", "--config", str(config_file)],
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
    notification_backend: FakeNotificationBackend | None = None,
    folder_backend: FakeFolderBackend | None = None,
    has_factory: bool = True,
) -> TestClient:
    config = load_config_from_text(_config_with_access(access_block))
    app = create_app(
        config,
        session_manager=None,
        notification_backend_factory=(
            (lambda _r: notification_backend) if has_factory else (lambda _r: None)
        ),
        folder_backend_factory=lambda _r: folder_backend,
        resolver_factory=lambda _r: None,
        operation_store=_make_store(),
    )
    return TestClient(app)


def test_http_mute_success() -> None:
    backend = FakeNotificationBackend()
    client = _http_client(notification_backend=backend)
    resp = client.post(
        "/telegram/notifications/mute",
        json={"telegram_chat_id": -100, "duration_hours": 2},
        headers=AUTH,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["muted"] is True
    assert body["telegram_chat_id"] == -100
    assert body["duration_hours"] == 2
    assert len(backend.muted) == 1


def test_http_mute_by_chat_name_success() -> None:
    backend = FakeNotificationBackend()
    folder_backend = FakeFolderBackend(
        [
            FolderSnapshot(
                folder_id=2,
                folder_name="Planfix clients",
                chats=[FolderChat(chat_id=-100, title="Acme")],
            )
        ]
    )
    client = _http_client(
        notification_backend=backend, folder_backend=folder_backend
    )

    resp = client.post(
        "/telegram/notifications/mute",
        json={
            "chat_name": "Acme",
            "folder_name": "Planfix clients",
            "duration_hours": 2,
        },
        headers=AUTH,
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["chat_name"] == "Acme"
    assert backend.muted[0]["chat_id"] == -100


def test_http_unmute_success() -> None:
    backend = FakeNotificationBackend()
    client = _http_client(notification_backend=backend)
    resp = client.post(
        "/telegram/notifications/unmute",
        json={"telegram_chat_id": -100},
        headers=AUTH,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["muted"] is False
    assert backend.unmuted == [{"chat_id": -100}]


def test_http_mute_requires_auth() -> None:
    backend = FakeNotificationBackend()
    client = _http_client(notification_backend=backend)
    resp = client.post(
        "/telegram/notifications/mute", json={"telegram_chat_id": -100}
    )
    assert resp.status_code == 401


def test_http_mute_503_when_backend_unavailable() -> None:
    client = _http_client(has_factory=False)
    resp = client.post(
        "/telegram/notifications/mute",
        json={"telegram_chat_id": -100},
        headers=AUTH,
    )
    assert resp.status_code == 503, resp.text


def test_http_mute_flood_wait_returns_needs_review() -> None:
    backend = FakeNotificationBackend(raise_on_mute=FloodWaitError(9))
    client = _http_client(notification_backend=backend)
    resp = client.post(
        "/telegram/notifications/mute",
        json={"telegram_chat_id": -100},
        headers=AUTH,
    )
    assert resp.status_code == 502, resp.text
    assert resp.json()["detail"]["error"] == "needs_review"


def test_http_unmute_flood_wait_returns_needs_review() -> None:
    backend = FakeNotificationBackend(raise_on_unmute=FloodWaitError(9))
    client = _http_client(notification_backend=backend)
    resp = client.post(
        "/telegram/notifications/unmute",
        json={"telegram_chat_id": -100},
        headers=AUTH,
    )
    assert resp.status_code == 502, resp.text
    assert resp.json()["detail"]["error"] == "needs_review"


def test_http_mute_403_when_denied() -> None:
    backend = FakeNotificationBackend()
    client = _http_client(
        access_block="access:\n  rules: []\n", notification_backend=backend
    )
    resp = client.post(
        "/telegram/notifications/mute",
        json={"telegram_chat_id": -100},
        headers=AUTH,
    )
    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"]["error"] == "access_denied"
    assert backend.muted == []


def test_http_unmute_403_when_denied() -> None:
    backend = FakeNotificationBackend()
    client = _http_client(
        access_block="access:\n  rules: []\n", notification_backend=backend
    )
    resp = client.post(
        "/telegram/notifications/unmute",
        json={"telegram_chat_id": -100},
        headers=AUTH,
    )
    assert resp.status_code == 403, resp.text
    assert backend.unmuted == []


def test_http_mute_bad_duration_422() -> None:
    backend = FakeNotificationBackend()
    client = _http_client(notification_backend=backend)
    resp = client.post(
        "/telegram/notifications/mute",
        json={"telegram_chat_id": -100, "duration_hours": 0},
        headers=AUTH,
    )
    assert resp.status_code == 422


def test_http_mute_rejects_unrepresentable_duration() -> None:
    backend = FakeNotificationBackend()
    client = _http_client(notification_backend=backend)
    resp = client.post(
        "/telegram/notifications/mute",
        json={"telegram_chat_id": -100, "duration_hours": 10**20},
        headers=AUTH,
    )
    assert resp.status_code in {400, 422}, resp.text
    assert backend.muted == []
