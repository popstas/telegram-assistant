"""Tests for the FastMCP streamable-HTTP mount."""

from __future__ import annotations

import base64
import hashlib
from urllib.parse import parse_qs, urlencode, urlsplit

from fastapi.testclient import TestClient

from telegram_assistant.config import load_config_from_text
from telegram_assistant.http_api import create_app
from telegram_assistant.http_api.mcp import GoogleIdentity
from telegram_assistant.telegram_client.session import SessionState


class FakeGoogleOidcProvider:
    def __init__(self) -> None:
        self.identity = GoogleIdentity(
            subject="google-sub",
            email="owner@example.test",
        )

    def authorization_url(self, *, state: str, redirect_uri: str) -> str:
        return redirect_uri + "?" + urlencode({"code": "fake-google-code", "state": state})

    async def authenticate(self, *, code: str, redirect_uri: str) -> GoogleIdentity:
        return self.identity


class FakeSessionManager:
    _client = None

    async def state(self) -> SessionState:
        return SessionState(
            authorized=False,
            account_label="telegram-assistant-main",
            session_path="/data/telegram-assistant.session",
        )


def _enabled_mcp_yaml(
    minimal_config_yaml: str,
    *,
    required_scopes: tuple[str, ...] = ("mcp", "telegram:read"),
    access_token_ttl_seconds: int = 600,
) -> str:
    scopes_yaml = "\n".join(f'    - "{scope}"' for scope in required_scopes)
    return (
        minimal_config_yaml
        + f"""

mcp:
  enabled: true
  server_url: "http://testserver/mcp"
  issuer_url: "http://testserver"
  google_client_id: "google-client-id"
  google_client_secret: "google-client-secret"
  allowed_emails:
    - "owner@example.test"
  required_scopes:
{scopes_yaml}
  access_token_ttl_seconds: {access_token_ttl_seconds}
  refresh_token_ttl_seconds: 1200
  signing_secret: "local-token-signing-secret"
"""
    )


def _client(minimal_config_yaml: str) -> TestClient:
    config = load_config_from_text(_enabled_mcp_yaml(minimal_config_yaml))
    app = create_app(
        config,
        session_manager=FakeSessionManager(),  # type: ignore[arg-type]
        mcp_google_provider=FakeGoogleOidcProvider(),
    )
    return TestClient(app)


def _code_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _register_client(client: TestClient, *, scope: str = "mcp telegram:read") -> str:
    response = client.post(
        "/register",
        json={
            "client_name": "MCP test client",
            "redirect_uris": ["http://client.example/callback"],
            "scope": scope,
        },
    )
    assert response.status_code == 201
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
    assert response.status_code == 302

    callback = client.get(response.headers["location"], follow_redirects=False)
    assert callback.status_code == 302
    query = parse_qs(urlsplit(callback.headers["location"]).query)
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
    assert response.status_code == 200
    return response.json()["access_token"]


def _mcp_headers(token: str | None = None) -> dict[str, str]:
    headers = {"Accept": "application/json, text/event-stream"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _initialize_payload(request_id: int = 1) -> dict[str, object]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "mcp-test", "version": "1.0"},
        },
    }


def test_mcp_mount_is_absent_when_disabled(minimal_config_yaml: str) -> None:
    config = load_config_from_text(minimal_config_yaml)
    client = TestClient(create_app(config, session_manager=FakeSessionManager()))  # type: ignore[arg-type]

    response = client.post(
        "/mcp",
        json=_initialize_payload(),
        headers=_mcp_headers(),
    )

    assert response.status_code == 404


def test_mcp_initialize_and_tools_list_are_reachable_with_token(
    minimal_config_yaml: str,
) -> None:
    with _client(minimal_config_yaml) as client:
        token = _mint_token(client)

        initialize = client.post(
            "/mcp",
            json=_initialize_payload(),
            headers=_mcp_headers(token),
        )
        assert initialize.status_code == 200
        assert initialize.json()["result"]["serverInfo"]["name"] == "telegram-assistant"

        initialized = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
            headers=_mcp_headers(token),
        )
        assert initialized.status_code == 202

        tools = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            headers=_mcp_headers(token),
        )
        assert tools.status_code == 200
        assert tools.json()["result"]["tools"] == []


def test_mcp_mount_rejects_missing_invalid_expired_and_wrong_audience_tokens(
    minimal_config_yaml: str,
) -> None:
    with _client(minimal_config_yaml) as client:
        valid_token = _mint_token(client)
        server = client.app.state.mcp_oauth_server
        claims = server.validate_access_token(valid_token)

        expired_token = server._encode_signed_token(  # noqa: SLF001
            {
                "typ": "access",
                "iss": claims.issuer,
                "sub": claims.subject,
                "aud": claims.audience,
                "iat": claims.issued_at,
                "exp": claims.issued_at - 1,
                "client_id": claims.client_id,
                "email": claims.email,
                "scope": " ".join(claims.scopes),
            }
        )
        wrong_audience_token = server._encode_signed_token(  # noqa: SLF001
            {
                "typ": "access",
                "iss": claims.issuer,
                "sub": claims.subject,
                "aud": "http://testserver/other",
                "iat": claims.issued_at,
                "exp": claims.expires_at,
                "client_id": claims.client_id,
                "email": claims.email,
                "scope": " ".join(claims.scopes),
            }
        )

        cases = [
            (None, 401),
            ("not-a-token", 401),
            (expired_token, 401),
            (wrong_audience_token, 401),
        ]
        for token, expected_status in cases:
            response = client.post(
                "/mcp",
                json=_initialize_payload(),
                headers=_mcp_headers(token),
            )
            assert response.status_code == expected_status
            assert response.json()["error"] == "invalid_token"


def test_mcp_mount_rejects_insufficient_scope_token(
    minimal_config_yaml: str,
) -> None:
    with _client(minimal_config_yaml) as client:
        token = _mint_token(client, scope="mcp")

        response = client.post(
            "/mcp",
            json=_initialize_payload(),
            headers=_mcp_headers(token),
        )

        assert response.status_code == 403
        assert response.json()["error"] == "insufficient_scope"


def test_mcp_enabled_keeps_health_open_and_telegram_bearer_auth_unchanged(
    minimal_config_yaml: str,
) -> None:
    with _client(minimal_config_yaml) as client:
        health = client.get("/health")
        assert health.status_code == 200
        assert health.json()["status"] == "ok"

        missing = client.get("/telegram/whoami")
        assert missing.status_code == 401

        wrong = client.get(
            "/telegram/whoami",
            headers={"Authorization": "Bearer wrong"},
        )
        assert wrong.status_code == 403

        accepted = client.get(
            "/telegram/whoami",
            headers={"Authorization": "Bearer secret_token"},
        )
        assert accepted.status_code == 200
        assert accepted.json() == {"status": "authenticated"}
