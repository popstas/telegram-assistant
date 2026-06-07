"""Integration coverage for the MCP protocol surface and OAuth gate."""

from __future__ import annotations

import base64
import hashlib
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

from fastapi.testclient import TestClient

from telegram_assistant.config import load_config_from_text
from telegram_assistant.http_api import create_app
from telegram_assistant.http_api.mcp import GoogleIdentity
from telegram_assistant.messages import RecentMessage
from telegram_assistant.persistence import OperationStore
from telegram_assistant.telegram_client.session import SessionState
from tests.test_mcp_mount import _enabled_mcp_yaml, _initialize_payload, _mcp_headers


class FakeGoogleOidcProvider:
    def __init__(self) -> None:
        self.identity = GoogleIdentity(
            subject="google-sub",
            email="owner@example.test",
        )
        self.authorizations: list[tuple[str, str]] = []
        self.authentications: list[tuple[str, str]] = []

    def authorization_url(self, *, state: str, redirect_uri: str) -> str:
        self.authorizations.append((state, redirect_uri))
        return redirect_uri + "?" + urlencode({"code": "fake-google-code", "state": state})

    async def authenticate(self, *, code: str, redirect_uri: str) -> GoogleIdentity:
        self.authentications.append((code, redirect_uri))
        return self.identity


class FakeSessionManager:
    _client = None

    async def state(self) -> SessionState:
        return SessionState(
            authorized=False,
            account_label="telegram-assistant-main",
            session_path="/data/telegram-assistant.session",
        )


class FakeReadBackend:
    def __init__(self) -> None:
        self.calls: list[dict[str, int]] = []

    async def get_recent_messages(
        self, *, chat_id: int, limit: int = 5
    ) -> list[RecentMessage]:
        self.calls.append({"chat_id": chat_id, "limit": limit})
        return [
            RecentMessage(
                id=101,
                sender="alice",
                date=None,
                reply_to=None,
                text="integration message",
            )
        ][:limit]


def _code_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _app_client(
    minimal_config_yaml: str,
    tmp_path: Path,
) -> tuple[TestClient, FakeGoogleOidcProvider, FakeReadBackend]:
    provider = FakeGoogleOidcProvider()
    backend = FakeReadBackend()
    config = load_config_from_text(_enabled_mcp_yaml(minimal_config_yaml))
    app = create_app(
        config,
        session_manager=FakeSessionManager(),  # type: ignore[arg-type]
        mcp_google_provider=provider,
        message_read_backend_factory=lambda _request: backend,
        operation_store=OperationStore(tmp_path / "state.db"),
        resolver_factory=lambda _request: None,
    )
    return TestClient(app), provider, backend


def _register_client(client: TestClient, *, scope: str = "mcp telegram:read") -> str:
    response = client.post(
        "/register",
        json={
            "client_name": "MCP Inspector",
            "redirect_uris": ["http://client.example/callback"],
            "scope": scope,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["client_id"]


def _authorize_and_get_code(
    client: TestClient,
    client_id: str,
    *,
    scope: str = "mcp telegram:read",
    verifier: str = "test-code-verifier",
) -> str:
    response = client.get(
        "/authorize",
        params={
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": "http://client.example/callback",
            "scope": scope,
            "resource": "http://testserver/mcp",
            "state": "client-state",
            "code_challenge": _code_challenge(verifier),
            "code_challenge_method": "S256",
        },
        follow_redirects=False,
    )
    assert response.status_code == 302, response.text

    google_callback = client.get(response.headers["location"], follow_redirects=False)
    assert google_callback.status_code == 302, google_callback.text
    query = parse_qs(urlsplit(google_callback.headers["location"]).query)
    assert query["state"] == ["client-state"]
    return query["code"][0]


def _mint_token(
    client: TestClient,
    *,
    scope: str = "mcp telegram:read",
    verifier: str = "test-code-verifier",
) -> str:
    client_id = _register_client(client, scope=scope)
    code = _authorize_and_get_code(
        client,
        client_id,
        scope=scope,
        verifier=verifier,
    )
    response = client.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "client_id": client_id,
            "redirect_uri": "http://client.example/callback",
            "resource": "http://testserver/mcp",
            "code_verifier": verifier,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()["access_token"]


def _initialize(client: TestClient, token: str) -> None:
    headers = _mcp_headers(token)
    initialize = client.post("/mcp", json=_initialize_payload(), headers=headers)
    assert initialize.status_code == 200, initialize.text
    assert initialize.json()["result"]["serverInfo"]["name"] == "telegram-assistant"

    initialized = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        headers=headers,
    )
    assert initialized.status_code == 202, initialized.text


def _tools_call_payload(
    name: str = "telegram_messages_recent",
    arguments: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": 3,
        "method": "tools/call",
        "params": {
            "name": name,
            "arguments": arguments or {"chat_id": -100123, "limit": 1},
        },
    }


def test_mcp_protocol_round_trip_uses_fake_google_oauth_and_fake_backend(
    minimal_config_yaml: str,
    tmp_path: Path,
) -> None:
    client, provider, backend = _app_client(minimal_config_yaml, tmp_path)
    with client:
        token = _mint_token(client)
        _initialize(client, token)

        tools = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            headers=_mcp_headers(token),
        )
        assert tools.status_code == 200, tools.text
        tool_names = {tool["name"] for tool in tools.json()["result"]["tools"]}
        assert "telegram_messages_recent" in tool_names

        call = client.post(
            "/mcp",
            json=_tools_call_payload(),
            headers=_mcp_headers(token),
        )

    assert call.status_code == 200, call.text
    result = call.json()["result"]
    assert result["isError"] is False
    assert result["structuredContent"]["telegram_chat_id"] == -100123
    assert result["structuredContent"]["messages"][0]["text"] == "integration message"
    assert backend.calls == [{"chat_id": -100123, "limit": 1}]
    assert len(provider.authorizations) == 1
    assert provider.authentications == [
        ("fake-google-code", "http://testserver/authorize")
    ]


def test_mcp_tools_call_rejects_wrong_audience_and_insufficient_scope_tokens(
    minimal_config_yaml: str,
    tmp_path: Path,
) -> None:
    client, _provider, _backend = _app_client(minimal_config_yaml, tmp_path)
    with client:
        valid_token = _mint_token(client)
        claims = client.app.state.mcp_oauth_server.validate_access_token(valid_token)
        wrong_audience_token = client.app.state.mcp_oauth_server._encode_signed_token(  # noqa: SLF001
            {
                "typ": "access",
                "iss": claims.issuer,
                "sub": claims.subject,
                "aud": "http://testserver/not-mcp",
                "iat": claims.issued_at,
                "exp": claims.expires_at,
                "client_id": claims.client_id,
                "email": claims.email,
                "scope": " ".join(claims.scopes),
            }
        )
        insufficient_scope_token = _mint_token(client, scope="mcp")

        wrong_audience = client.post(
            "/mcp",
            json=_tools_call_payload(),
            headers=_mcp_headers(wrong_audience_token),
        )
        insufficient_scope = client.post(
            "/mcp",
            json=_tools_call_payload(),
            headers=_mcp_headers(insufficient_scope_token),
        )

    assert wrong_audience.status_code == 401
    assert wrong_audience.json()["error"] == "invalid_token"
    assert insufficient_scope.status_code == 403
    assert insufficient_scope.json()["error"] == "insufficient_scope"


def test_mcp_enabled_leaves_health_open_and_telegram_bearer_auth_unchanged(
    minimal_config_yaml: str,
    tmp_path: Path,
) -> None:
    client, _provider, _backend = _app_client(minimal_config_yaml, tmp_path)
    with client:
        health = client.get("/health")
        missing = client.get("/telegram/whoami")
        wrong = client.get(
            "/telegram/whoami",
            headers={"Authorization": "Bearer wrong"},
        )
        accepted = client.get(
            "/telegram/whoami",
            headers={"Authorization": "Bearer secret_token"},
        )

    assert health.status_code == 200
    assert health.json()["status"] == "ok"
    assert missing.status_code == 401
    assert wrong.status_code == 403
    assert accepted.status_code == 200
    assert accepted.json() == {"status": "authenticated"}
