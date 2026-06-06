"""Smoke tests for FastAPI app factory, bearer-token auth, and CLI skeleton."""

from __future__ import annotations

from fastapi.testclient import TestClient
from typer.testing import CliRunner

from telegram_assistant.cli.main import app as cli_app
from telegram_assistant.config import load_config_from_text
from telegram_assistant.http_api import create_app
from telegram_assistant.messages.telethon_backend import (
    TelethonForwardBackend,
    TelethonMessageBackend,
    TelethonMessageReadBackend,
    TelethonReactionBackend,
)
from telegram_assistant.notifications import TelethonNotificationBackend


def _client(minimal_config_yaml: str) -> TestClient:
    config = load_config_from_text(minimal_config_yaml)
    return TestClient(create_app(config))


def test_health_endpoint_does_not_require_auth(minimal_config_yaml: str) -> None:
    client = _client(minimal_config_yaml)
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert "telegram_session" in body
    assert "database" in body
    assert "default_folder" in body


def test_protected_endpoint_requires_authorization_header(minimal_config_yaml: str) -> None:
    client = _client(minimal_config_yaml)
    response = client.get("/telegram/whoami")
    assert response.status_code == 401


def test_protected_endpoint_rejects_wrong_scheme(minimal_config_yaml: str) -> None:
    client = _client(minimal_config_yaml)
    response = client.get(
        "/telegram/whoami",
        headers={"Authorization": "Basic secret_token"},
    )
    assert response.status_code == 401


def test_protected_endpoint_rejects_wrong_token(minimal_config_yaml: str) -> None:
    client = _client(minimal_config_yaml)
    response = client.get(
        "/telegram/whoami",
        headers={"Authorization": "Bearer wrong"},
    )
    assert response.status_code == 403


def test_protected_endpoint_accepts_correct_token(minimal_config_yaml: str) -> None:
    client = _client(minimal_config_yaml)
    response = client.get(
        "/telegram/whoami",
        headers={"Authorization": "Bearer secret_token"},
    )
    assert response.status_code == 200
    assert response.json() == {"status": "authenticated"}


def test_cli_exposes_required_subcommand_groups() -> None:
    runner = CliRunner()
    result = runner.invoke(cli_app, ["--help"])
    assert result.exit_code == 0
    for group in (
        "auth",
        "health",
        "groups",
        "topics",
        "members",
        "messages",
        "notifications",
        "folders",
        "operations",
    ):
        assert group in result.stdout


def test_default_message_operation_factories_use_telethon_backends(
    minimal_config_yaml: str,
) -> None:
    class _FakeSessionManager:
        def __init__(self) -> None:
            self._client = object()

    config = load_config_from_text(minimal_config_yaml)
    app = create_app(config, session_manager=_FakeSessionManager())  # type: ignore[arg-type]

    assert isinstance(app.state.message_backend_factory(None), TelethonMessageBackend)
    assert isinstance(
        app.state.message_read_backend_factory(None), TelethonMessageReadBackend
    )
    assert isinstance(app.state.reaction_backend_factory(None), TelethonReactionBackend)
    assert isinstance(app.state.forward_backend_factory(None), TelethonForwardBackend)
    assert isinstance(
        app.state.notification_backend_factory(None), TelethonNotificationBackend
    )


def test_cli_version_command() -> None:
    from telegram_assistant import __version__

    runner = CliRunner()
    result = runner.invoke(cli_app, ["version"])
    assert result.exit_code == 0
    assert __version__ in result.stdout
