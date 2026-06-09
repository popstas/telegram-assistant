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
from telegram_assistant.http_api.mcp.server import (
    build_fastmcp_server,
    configure_mcp_tools,
)

__all__ = [
    "GoogleIdentity",
    "GoogleOidcProvider",
    "HttpGoogleOidcProvider",
    "OAuthAuthorizationServer",
    "TokenClaims",
    "TokenValidationError",
    "build_fastmcp_server",
    "configure_mcp_tools",
    "build_oauth_router",
]
