"""MCP HTTP integration helpers."""

from telegram_assistant.http_api.mcp.oauth import (
    GoogleIdentity,
    GoogleOidcProvider,
    HttpGoogleOidcProvider,
    OAuthAuthorizationServer,
    TokenClaims,
    TokenValidationError,
    build_oauth_router,
)
from telegram_assistant.http_api.mcp.server import build_fastmcp_server

__all__ = [
    "GoogleIdentity",
    "GoogleOidcProvider",
    "HttpGoogleOidcProvider",
    "OAuthAuthorizationServer",
    "TokenClaims",
    "TokenValidationError",
    "build_fastmcp_server",
    "build_oauth_router",
]
