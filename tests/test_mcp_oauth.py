"""Tests for the local MCP OAuth Authorization Server."""

from __future__ import annotations

import base64
import hashlib
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest
from fastapi.testclient import TestClient

from telegram_assistant.config import load_config_from_text
from telegram_assistant.http_api import create_app
from telegram_assistant.http_api.mcp import (
    GoogleIdentity,
    HttpGoogleOidcProvider,
    TokenValidationError,
)
from telegram_assistant.http_api.mcp.oauth import OAuthHttpError


class FakeGoogleOidcProvider:
    def __init__(self, identity: GoogleIdentity) -> None:
        self.identity = identity
        self.authenticated_codes: list[tuple[str, str]] = []

    def authorization_url(self, *, state: str, redirect_uri: str) -> str:
        return redirect_uri + "?" + urlencode({"code": "fake-google-code", "state": state})

    async def authenticate(self, *, code: str, redirect_uri: str) -> GoogleIdentity:
        self.authenticated_codes.append((code, redirect_uri))
        return self.identity


class FakeHttpResponse:
    def __init__(self, payload: dict[str, object]) -> None:
        self._payload = payload

    def json(self) -> dict[str, object]:
        return self._payload

    def raise_for_status(self) -> None:
        return None


class FakeGoogleHttpClient:
    token_payload: dict[str, object] = {"id_token": "google-id-token"}
    tokeninfo_payload: dict[str, object] = {
        "aud": "google-client-id",
        "sub": "google-sub",
        "email": "owner@example.test",
        "email_verified": True,
    }
    calls: list[tuple[str, dict[str, object] | None]] = []

    def __init__(self, *, timeout: float) -> None:
        self.timeout = timeout

    async def __aenter__(self) -> FakeGoogleHttpClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def post(self, url: str, *, data: dict[str, object]) -> FakeHttpResponse:
        self.calls.append((url, data))
        return FakeHttpResponse(self.token_payload)

    async def get(self, url: str, *, params: dict[str, object]) -> FakeHttpResponse:
        self.calls.append((url, params))
        return FakeHttpResponse(self.tokeninfo_payload)


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
  allowed_redirect_hosts:
    - "client.example"
    - "second.example"
    - "replacement.example"
""",
    admin: str = "",
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
{admin.rstrip()}
  required_scopes:
{scopes.rstrip()}
  access_token_ttl_seconds: {access_ttl}
  refresh_token_ttl_seconds: 1200
  signing_secret: "local-token-signing-secret-with-32-chars"
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


def _mcp_config(minimal_config_yaml: str):
    config = load_config_from_text(_enabled_mcp_yaml(minimal_config_yaml))
    assert config.mcp is not None
    return config.mcp


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


def test_register_rejects_untrusted_remote_redirect_uri(
    minimal_config_yaml: str,
) -> None:
    provider = FakeGoogleOidcProvider(GoogleIdentity(subject="sub", email="owner@example.test"))
    client, _server = _client(minimal_config_yaml, provider)

    response = client.post(
        "/register",
        json={
            "client_name": "Untrusted client",
            "redirect_uris": ["https://attacker.example/callback"],
            "scope": "mcp telegram:read",
        },
    )

    assert response.status_code == 400
    assert response.json()["error"] == "invalid_client_metadata"
    assert "redirect_uris" in response.json()["error_description"]


def test_register_allows_loopback_redirect_uri_by_default(
    minimal_config_yaml: str,
) -> None:
    provider = FakeGoogleOidcProvider(GoogleIdentity(subject="sub", email="owner@example.test"))
    client, _server = _client(minimal_config_yaml, provider)

    response = client.post(
        "/register",
        json={
            "client_name": "Local MCP client",
            "redirect_uris": ["http://127.0.0.1:6274/oauth/callback"],
            "scope": "mcp telegram:read",
        },
    )

    assert response.status_code == 201


@pytest.mark.asyncio
async def test_http_google_provider_exchanges_code_and_validates_tokeninfo(
    minimal_config_yaml: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import httpx

    FakeGoogleHttpClient.calls = []
    FakeGoogleHttpClient.token_payload = {"id_token": "google-id-token"}
    FakeGoogleHttpClient.tokeninfo_payload = {
        "aud": "google-client-id",
        "sub": "google-sub",
        "email": "owner@example.test",
        "email_verified": True,
    }
    monkeypatch.setattr(httpx, "AsyncClient", FakeGoogleHttpClient)
    provider = HttpGoogleOidcProvider(_mcp_config(minimal_config_yaml))

    identity = await provider.authenticate(
        code="google-code",
        redirect_uri="http://testserver/authorize",
    )

    assert identity == GoogleIdentity(
        subject="google-sub",
        email="owner@example.test",
        email_verified=True,
    )
    assert FakeGoogleHttpClient.calls == [
        (
            "https://oauth2.googleapis.com/token",
            {
                "grant_type": "authorization_code",
                "code": "google-code",
                "client_id": "google-client-id",
                "client_secret": "google-client-secret",
                "redirect_uri": "http://testserver/authorize",
            },
        ),
        (
            "https://oauth2.googleapis.com/tokeninfo",
            {"id_token": "google-id-token"},
        ),
    ]


@pytest.mark.asyncio
async def test_http_google_provider_rejects_unverified_email(
    minimal_config_yaml: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import httpx

    FakeGoogleHttpClient.calls = []
    FakeGoogleHttpClient.token_payload = {"id_token": "google-id-token"}
    FakeGoogleHttpClient.tokeninfo_payload = {
        "aud": "google-client-id",
        "sub": "google-sub",
        "email": "owner@example.test",
        "email_verified": False,
    }
    monkeypatch.setattr(httpx, "AsyncClient", FakeGoogleHttpClient)
    provider = HttpGoogleOidcProvider(_mcp_config(minimal_config_yaml))

    with pytest.raises(OAuthHttpError) as exc:
        await provider.authenticate(
            code="google-code",
            redirect_uri="http://testserver/authorize",
        )

    assert exc.value.status_code == 403
    assert exc.value.error == "invalid_google_token"


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


def _authorize_without_scope_and_get_code(
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


def test_oauth_metadata_advertises_admin_scope_only_when_configured(
    minimal_config_yaml: str,
) -> None:
    provider = FakeGoogleOidcProvider(GoogleIdentity(subject="sub", email="owner@example.test"))
    config = load_config_from_text(
        _enabled_mcp_yaml(
            minimal_config_yaml,
            admin="""
  admin_emails:
    - "owner@example.test"
""",
        )
    )
    client = TestClient(create_app(config, mcp_google_provider=provider))

    auth_metadata = client.get("/.well-known/oauth-authorization-server")
    resource_metadata = client.get("/.well-known/oauth-protected-resource/mcp")

    assert auth_metadata.json()["scopes_supported"] == [
        "mcp",
        "telegram:read",
        "telegram:admin",
    ]
    assert resource_metadata.json()["scopes_supported"] == [
        "mcp",
        "telegram:read",
        "telegram:admin",
    ]


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


def test_omitted_oauth_scope_defaults_to_required_scopes_only(
    minimal_config_yaml: str,
) -> None:
    provider = FakeGoogleOidcProvider(GoogleIdentity(subject="google-sub", email="owner@example.test"))
    client, server = _client(minimal_config_yaml, provider)

    register = client.post(
        "/register",
        json={
            "client_name": "MCP Inspector",
            "redirect_uris": ["http://client.example/callback"],
        },
    )
    assert register.status_code == 201
    assert register.json()["scope"] == "mcp telegram:read"

    client_id = register.json()["client_id"]
    code = _authorize_without_scope_and_get_code(client, client_id)
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
    assert token_response.json()["scope"] == "mcp telegram:read"
    claims = server.validate_access_token(token_response.json()["access_token"])
    assert claims.scopes == ("mcp", "telegram:read")


def test_authorization_omitted_scope_does_not_inherit_registered_admin_scope(
    minimal_config_yaml: str,
) -> None:
    provider = FakeGoogleOidcProvider(GoogleIdentity(subject="google-sub", email="owner@example.test"))
    config = load_config_from_text(
        _enabled_mcp_yaml(
            minimal_config_yaml,
            admin="""
  admin_emails:
    - "owner@example.test"
""",
        )
    )
    app = create_app(config, mcp_google_provider=provider)
    client = TestClient(app)
    server = app.state.mcp_oauth_server
    register = client.post(
        "/register",
        json={
            "client_name": "Admin-capable client",
            "redirect_uris": ["http://client.example/callback"],
            "scope": "mcp telegram:read telegram:admin",
        },
    )
    assert register.status_code == 201
    client_id = register.json()["client_id"]

    code = _authorize_without_scope_and_get_code(client, client_id)
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
    assert token_response.json()["scope"] == "mcp telegram:read"
    claims = server.validate_access_token(token_response.json()["access_token"])
    assert "telegram:admin" not in claims.scopes


def test_admin_scope_is_not_supported_without_admin_allowlist(
    minimal_config_yaml: str,
) -> None:
    provider = FakeGoogleOidcProvider(GoogleIdentity(subject="google-sub", email="owner@example.test"))
    client, _server = _client(minimal_config_yaml, provider)

    response = client.post(
        "/register",
        json={
            "client_name": "Admin client",
            "redirect_uris": ["http://client.example/callback"],
            "scope": "mcp telegram:read telegram:admin",
        },
    )

    assert response.status_code == 400
    assert response.json()["error"] == "invalid_scope"


def test_non_admin_google_identity_cannot_authorize_admin_scope(
    minimal_config_yaml: str,
) -> None:
    provider = FakeGoogleOidcProvider(GoogleIdentity(subject="google-sub", email="owner@example.test"))
    config = load_config_from_text(
        _enabled_mcp_yaml(
            minimal_config_yaml,
            allowed="""
  allowed_emails:
    - "owner@example.test"
  allowed_redirect_hosts:
    - "client.example"
""",
            admin="""
  admin_emails:
    - "admin@example.test"
""",
        )
    )
    client = TestClient(create_app(config, mcp_google_provider=provider))

    register = client.post(
        "/register",
        json={
            "client_name": "Admin client",
            "redirect_uris": ["http://client.example/callback"],
            "scope": "mcp telegram:read telegram:admin",
        },
    )
    assert register.status_code == 201
    client_id = register.json()["client_id"]

    response = client.get(
        "/authorize",
        params={
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": "http://client.example/callback",
            "scope": "mcp telegram:read telegram:admin",
            "resource": "http://testserver/mcp",
            "state": "client-state",
            "code_challenge": _code_challenge("test-code-verifier"),
            "code_challenge_method": "S256",
        },
        follow_redirects=False,
    )
    callback = client.get(response.headers["location"], follow_redirects=False)

    assert callback.status_code == 302
    query = parse_qs(urlsplit(callback.headers["location"]).query)
    assert query["error"] == ["access_denied"]
    assert query["state"] == ["client-state"]


def test_admin_google_identity_can_authorize_admin_scope(
    minimal_config_yaml: str,
) -> None:
    provider = FakeGoogleOidcProvider(GoogleIdentity(subject="google-sub", email="admin@example.test"))
    config = load_config_from_text(
        _enabled_mcp_yaml(
            minimal_config_yaml,
            allowed="""
  allowed_emails:
    - "admin@example.test"
  allowed_redirect_hosts:
    - "client.example"
""",
            admin="""
  admin_emails:
    - "admin@example.test"
""",
        )
    )
    app = create_app(config, mcp_google_provider=provider)
    client = TestClient(app)
    server = app.state.mcp_oauth_server

    register = client.post(
        "/register",
        json={
            "client_name": "Admin client",
            "redirect_uris": ["http://client.example/callback"],
            "scope": "mcp telegram:read telegram:admin",
        },
    )
    assert register.status_code == 201
    client_id = register.json()["client_id"]
    code = _authorize_and_get_code(client, client_id, scope="mcp telegram:read telegram:admin")
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
    assert token_response.json()["scope"] == "mcp telegram:read telegram:admin"
    claims = server.validate_access_token(token_response.json()["access_token"])
    assert claims.scopes == ("mcp", "telegram:read", "telegram:admin")


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

    assert callback.status_code == 302
    redirected = urlsplit(callback.headers["location"])
    assert redirected.scheme == "http"
    assert redirected.netloc == "client.example"
    assert redirected.path == "/callback"
    query = parse_qs(redirected.query)
    assert query["error"] == ["access_denied"]
    assert query["state"] == ["client-state"]


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


def test_refresh_token_exchange_rechecks_current_allowlist(
    minimal_config_yaml: str,
) -> None:
    provider = FakeGoogleOidcProvider(GoogleIdentity(subject="google-sub", email="owner@example.test"))
    client, server = _client(minimal_config_yaml, provider)
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

    server._allowed_emails = set()  # noqa: SLF001
    server._allowed_domains = set()  # noqa: SLF001

    refresh = client.post(
        "/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": token_body["refresh_token"],
            "resource": "http://testserver/mcp",
        },
    )

    assert refresh.status_code == 403
    assert refresh.json()["error"] == "access_denied"


def test_dynamic_client_registration_evicts_oldest_client_at_cap(
    minimal_config_yaml: str,
) -> None:
    provider = FakeGoogleOidcProvider(GoogleIdentity(subject="sub", email="owner@example.test"))
    client, server = _client(minimal_config_yaml, provider)
    server._max_registered_clients = 1  # noqa: SLF001
    first_client_id = _register_client(client)

    response = client.post(
        "/register",
        json={
            "client_name": "Second client",
            "redirect_uris": ["http://second.example/callback"],
            "scope": "mcp",
        },
    )

    assert response.status_code == 201
    second_client_id = response.json()["client_id"]
    assert first_client_id not in server._clients  # noqa: SLF001
    assert second_client_id in server._clients  # noqa: SLF001

    evicted = client.get(
        "/authorize",
        params={
            "response_type": "code",
            "client_id": first_client_id,
            "redirect_uri": "http://client.example/callback",
            "scope": "mcp",
            "resource": "http://testserver/mcp",
            "code_challenge": _code_challenge("test-code-verifier"),
            "code_challenge_method": "S256",
        },
    )
    assert evicted.status_code == 400
    assert evicted.json()["error"] == "invalid_client"


def test_expired_dynamic_clients_do_not_exhaust_registration_cap(
    minimal_config_yaml: str,
) -> None:
    provider = FakeGoogleOidcProvider(GoogleIdentity(subject="sub", email="owner@example.test"))
    client, server = _client(minimal_config_yaml, provider)
    now = 1_000
    server._now = lambda: now  # noqa: SLF001
    server._max_registered_clients = 1  # noqa: SLF001
    server._registered_client_ttl_seconds = 10  # noqa: SLF001
    _register_client(client)

    now = 1_011
    response = client.post(
        "/register",
        json={
            "client_name": "Replacement client",
            "redirect_uris": ["http://replacement.example/callback"],
            "scope": "mcp",
        },
    )

    assert response.status_code == 201
