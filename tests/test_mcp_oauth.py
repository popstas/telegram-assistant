"""Tests for the local MCP OAuth Authorization Server."""

from __future__ import annotations

import base64
import hashlib
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest
from fastapi.testclient import TestClient

from telegram_assistant.config import load_config_from_text
from telegram_assistant.http_api import create_app
from telegram_assistant.http_api.mcp import GoogleIdentity, TokenValidationError


class FakeGoogleOidcProvider:
    def __init__(self, identity: GoogleIdentity) -> None:
        self.identity = identity
        self.authenticated_codes: list[tuple[str, str]] = []

    def authorization_url(self, *, state: str, redirect_uri: str) -> str:
        return redirect_uri + "?" + urlencode({"code": "fake-google-code", "state": state})

    async def authenticate(self, *, code: str, redirect_uri: str) -> GoogleIdentity:
        self.authenticated_codes.append((code, redirect_uri))
        return self.identity


def _enabled_mcp_yaml(
    minimal_config_yaml: str,
    *,
    scopes: str = """
    - "mcp"
    - "telegram:read"
""",
    access_ttl: int = 600,
    allowed: str = """
  allowed_emails:
    - "owner@example.test"
""",
) -> str:
    return (
        minimal_config_yaml
        + f"""

mcp:
  enabled: true
  server_url: "http://testserver/mcp"
  issuer_url: "http://testserver"
  google_client_id: "google-client-id"
  google_client_secret: "google-client-secret"
{allowed.rstrip()}
  required_scopes:
{scopes.rstrip()}
  access_token_ttl_seconds: {access_ttl}
  refresh_token_ttl_seconds: 1200
  signing_secret: "local-token-signing-secret"
"""
    )


def _code_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _client(
    minimal_config_yaml: str,
    provider: FakeGoogleOidcProvider,
    *,
    access_ttl: int = 600,
) -> tuple[TestClient, object]:
    config = load_config_from_text(
        _enabled_mcp_yaml(minimal_config_yaml, access_ttl=access_ttl)
    )
    app = create_app(config, mcp_google_provider=provider)
    return TestClient(app), app.state.mcp_oauth_server


def _register_client(client: TestClient) -> str:
    response = client.post(
        "/register",
        json={
            "client_name": "MCP Inspector",
            "redirect_uris": ["http://client.example/callback"],
            "scope": "mcp telegram:read",
        },
    )
    assert response.status_code == 201
    body = response.json()
    assert body["token_endpoint_auth_method"] == "none"
    assert body["scope"] == "mcp telegram:read"
    return body["client_id"]


def _authorize_and_get_code(
    client: TestClient,
    client_id: str,
    *,
    verifier: str = "test-code-verifier",
) -> str:
    response = client.get(
        "/authorize",
        params={
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": "http://client.example/callback",
            "scope": "mcp telegram:read",
            "resource": "http://testserver/mcp",
            "state": "client-state",
            "code_challenge": _code_challenge(verifier),
            "code_challenge_method": "S256",
        },
        follow_redirects=False,
    )
    assert response.status_code == 302
    google_location = response.headers["location"]
    assert google_location.startswith("http://testserver/authorize?")

    callback = client.get(google_location, follow_redirects=False)
    assert callback.status_code == 302
    redirected = callback.headers["location"]
    parts = urlsplit(redirected)
    assert parts.scheme == "http"
    assert parts.netloc == "client.example"
    assert parts.path == "/callback"
    query = parse_qs(parts.query)
    assert query["state"] == ["client-state"]
    return query["code"][0]


def test_oauth_metadata_routes_are_enabled_only_with_mcp(
    minimal_config_yaml: str,
) -> None:
    disabled_config = load_config_from_text(minimal_config_yaml)
    disabled_client = TestClient(create_app(disabled_config))
    assert disabled_client.get("/.well-known/oauth-authorization-server").status_code == 404

    provider = FakeGoogleOidcProvider(GoogleIdentity(subject="sub", email="owner@example.test"))
    enabled_client, _server = _client(minimal_config_yaml, provider)

    auth_metadata = enabled_client.get("/.well-known/oauth-authorization-server")
    assert auth_metadata.status_code == 200
    assert auth_metadata.json() == {
        "issuer": "http://testserver",
        "authorization_endpoint": "http://testserver/authorize",
        "token_endpoint": "http://testserver/token",
        "registration_endpoint": "http://testserver/register",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none"],
        "scopes_supported": ["mcp", "telegram:read"],
        "resource_indicators_supported": True,
    }

    resource_metadata = enabled_client.get("/.well-known/oauth-protected-resource/mcp")
    assert resource_metadata.status_code == 200
    assert resource_metadata.json() == {
        "resource": "http://testserver/mcp",
        "authorization_servers": ["http://testserver"],
        "scopes_supported": ["mcp", "telegram:read"],
        "bearer_methods_supported": ["header"],
    }


def test_fake_google_oauth_flow_mints_audience_bound_token(
    minimal_config_yaml: str,
) -> None:
    provider = FakeGoogleOidcProvider(GoogleIdentity(subject="google-sub", email="owner@example.test"))
    client, server = _client(minimal_config_yaml, provider)
    client_id = _register_client(client)
    code = _authorize_and_get_code(client, client_id)

    token_response = client.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "client_id": client_id,
            "redirect_uri": "http://client.example/callback",
            "resource": "http://testserver/mcp",
            "code_verifier": "test-code-verifier",
        },
    )

    assert token_response.status_code == 200
    token_body = token_response.json()
    assert token_body["token_type"] == "Bearer"
    assert token_body["expires_in"] == 600
    assert token_body["scope"] == "mcp telegram:read"
    assert provider.authenticated_codes == [
        ("fake-google-code", "http://testserver/authorize")
    ]

    claims = server.validate_access_token(token_body["access_token"])
    assert claims.issuer == "http://testserver"
    assert claims.subject == "google-sub"
    assert claims.audience == "http://testserver/mcp"
    assert claims.scopes == ("mcp", "telegram:read")
    assert claims.client_id == client_id
    assert claims.email == "owner@example.test"


def test_google_allowlist_is_enforced(minimal_config_yaml: str) -> None:
    provider = FakeGoogleOidcProvider(
        GoogleIdentity(subject="google-sub", email="intruder@example.test")
    )
    client, _server = _client(minimal_config_yaml, provider)
    client_id = _register_client(client)

    response = client.get(
        "/authorize",
        params={
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": "http://client.example/callback",
            "scope": "mcp telegram:read",
            "resource": "http://testserver/mcp",
            "state": "client-state",
            "code_challenge": _code_challenge("test-code-verifier"),
            "code_challenge_method": "S256",
        },
        follow_redirects=False,
    )
    callback = client.get(response.headers["location"], follow_redirects=False)

    assert callback.status_code == 403
    assert callback.json()["error"] == "access_denied"


def test_access_token_validation_checks_audience_scope_and_ttl(
    minimal_config_yaml: str,
) -> None:
    provider = FakeGoogleOidcProvider(GoogleIdentity(subject="google-sub", email="owner@example.test"))
    client, server = _client(minimal_config_yaml, provider, access_ttl=10)
    client_id = _register_client(client)
    code = _authorize_and_get_code(client, client_id)
    token_body = client.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "client_id": client_id,
            "redirect_uri": "http://client.example/callback",
            "resource": "http://testserver/mcp",
            "code_verifier": "test-code-verifier",
        },
    ).json()

    claims = server.validate_access_token(token_body["access_token"])
    assert claims.expires_at - claims.issued_at == 10

    with pytest.raises(TokenValidationError) as wrong_audience:
        server.validate_access_token(
            token_body["access_token"],
            audience="http://other.example/mcp",
        )
    assert wrong_audience.value.status_code == 401

    with pytest.raises(TokenValidationError) as wrong_scope:
        server.validate_access_token(
            token_body["access_token"],
            required_scopes=["mcp", "telegram:write"],
        )
    assert wrong_scope.value.status_code == 403

    with pytest.raises(TokenValidationError) as expired:
        server.validate_access_token(
            token_body["access_token"],
            now=claims.expires_at,
        )
    assert expired.value.status_code == 401
