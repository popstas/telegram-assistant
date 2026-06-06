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

__all__ = [
    "GoogleIdentity",
    "GoogleOidcProvider",
    "HttpGoogleOidcProvider",
    "OAuthAuthorizationServer",
    "TokenClaims",
    "TokenValidationError",
    "build_oauth_router",
]
