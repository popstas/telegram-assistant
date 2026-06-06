"""FastMCP server wiring for the optional HTTP MCP surface."""

from __future__ import annotations

from typing import Any

from mcp.server.auth.provider import AccessToken
from mcp.server.auth.settings import AuthSettings
from mcp.server.fastmcp import FastMCP

from telegram_assistant.config import AppConfig
from telegram_assistant.http_api.mcp.oauth import (
    OAuthAuthorizationServer,
    TokenValidationError,
)
from telegram_assistant.http_api.mcp.tools import (
    AppStateProvider,
    register_telegram_tools,
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
    )
    if app_state_provider is not None:
        register_telegram_tools(server, app_state_provider)
    return server
