"""FastMCP server wiring for the optional HTTP MCP surface."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

from mcp.server.auth.provider import AccessToken
from mcp.server.auth.settings import AuthSettings
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from telegram_assistant.config import AppConfig
from telegram_assistant.http_api.mcp.oauth import (
    OAuthAuthorizationServer,
    TokenValidationError,
)
from telegram_assistant.http_api.mcp.tools import (
    AppStateProvider,
    register_telegram_tools,
)


def _tool_is_disabled(name: str, disabled_tools: list[str]) -> bool:
    """Return True when ``name`` matches a disabled entry (exact or prefix).

    An entry ending in ``*`` matches by prefix (``telegram_groups_*`` disables
    every ``telegram_groups_…`` tool); otherwise it must equal the tool name.
    """

    for entry in disabled_tools:
        if entry.endswith("*"):
            if name.startswith(entry[:-1]):
                return True
        elif name == entry:
            return True
    return False


def configure_mcp_tools(
    server: FastMCP[Any],
    app_state_provider: AppStateProvider,
    disabled_tools: list[str],
) -> None:
    """Register the full telegram tool catalog, then prune disabled tools.

    Re-registering is a no-op for tools already present and re-adds any
    previously removed tool whose prefix/name is no longer disabled, so this is
    safe to call again on config hot-reload to re-apply ``mcp.disabled_tools``.
    """

    manager = server._tool_manager  # noqa: SLF001
    previous_warn = manager.warn_on_duplicate_tools
    manager.warn_on_duplicate_tools = False
    try:
        register_telegram_tools(server, app_state_provider)
    finally:
        manager.warn_on_duplicate_tools = previous_warn
    for tool in list(manager.list_tools()):
        if _tool_is_disabled(tool.name, disabled_tools):
            server.remove_tool(tool.name)


_LOCAL_BIND_HOSTS = {"127.0.0.1", "localhost", "::1"}
_LOCAL_ALLOWED_HOSTS = ["127.0.0.1:*", "localhost:*", "[::1]:*"]
_LOCAL_ALLOWED_ORIGINS = [
    "http://127.0.0.1:*",
    "http://localhost:*",
    "http://[::1]:*",
]


def _mcp_transport_security(config: AppConfig) -> TransportSecuritySettings | None:
    if config.http.host not in _LOCAL_BIND_HOSTS:
        return None

    allowed_hosts = [*_LOCAL_ALLOWED_HOSTS]
    if config.mcp is not None and config.mcp.server_url is not None:
        parsed = urlsplit(config.mcp.server_url)
        if parsed.netloc and parsed.netloc not in allowed_hosts:
            allowed_hosts.append(parsed.netloc)
        if parsed.hostname is not None:
            host = parsed.hostname
            wildcard_host = f"[{host}]:*" if ":" in host else f"{host}:*"
            if wildcard_host not in allowed_hosts:
                allowed_hosts.append(wildcard_host)

    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=allowed_hosts,
        allowed_origins=[*_LOCAL_ALLOWED_ORIGINS],
    )

class OAuthTokenVerifier:
    """Adapt the local OAuth AS token validator to the MCP SDK interface."""

    def __init__(self, server: OAuthAuthorizationServer) -> None:
        self._server = server

    async def verify_token(self, token: str) -> AccessToken | None:
        try:
            claims = self._server.validate_access_token(
                token,
                required_scopes=[],
            )
        except TokenValidationError:
            return None

        return AccessToken(
            token=token,
            client_id=claims.client_id,
            scopes=list(claims.scopes),
            expires_at=claims.expires_at,
            resource=claims.audience,
            subject=claims.subject,
            claims={
                "iss": claims.issuer,
                "aud": claims.audience,
                "email": claims.email,
                "iat": claims.issued_at,
                "exp": claims.expires_at,
                "typ": claims.token_type,
            },
        )


def build_fastmcp_server(
    config: AppConfig,
    *,
    oauth_server: OAuthAuthorizationServer,
    app_state_provider: AppStateProvider | None = None,
) -> FastMCP[Any]:
    """Build the FastMCP server mounted by the FastAPI app."""

    if config.mcp is None or not config.mcp.enabled:
        raise ValueError("FastMCP server requires enabled mcp config")
    if config.mcp.issuer_url is None or config.mcp.server_url is None:
        raise ValueError("FastMCP server config is incomplete")

    server: FastMCP[Any] = FastMCP(
        "telegram-assistant",
        host=config.http.host,
        port=config.http.port,
        streamable_http_path="/mcp",
        stateless_http=True,
        json_response=True,
        auth=AuthSettings(
            issuer_url=config.mcp.issuer_url,
            required_scopes=config.mcp.required_scopes,
            resource_server_url=config.mcp.server_url,
        ),
        token_verifier=OAuthTokenVerifier(oauth_server),
        transport_security=_mcp_transport_security(config),
    )
    if app_state_provider is not None:
        configure_mcp_tools(server, app_state_provider, config.mcp.disabled_tools)
    return server
