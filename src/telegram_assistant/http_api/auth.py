"""Bearer-token auth dependency for the HTTP API."""

from __future__ import annotations

import secrets

from fastapi import Depends, Header, HTTPException, Request, status


def _expected_token(request: Request) -> str:
    config = getattr(request.app.state, "config", None)
    if config is None or not getattr(config, "http", None):
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Server is not configured",
        )
    return config.http.bearer_token


async def require_bearer_token(
    request: Request,
    authorization: str | None = Header(default=None),
) -> None:
    expected = _expected_token(request)
    if not authorization:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing Authorization header",
            headers={"WWW-Authenticate": "Bearer"},
        )
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authorization header must be 'Bearer <token>'",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if not secrets.compare_digest(token, expected):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid bearer token",
        )


BearerAuth = Depends(require_bearer_token)
