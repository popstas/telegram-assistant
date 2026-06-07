"""Local OAuth Authorization Server used by the optional MCP HTTP surface."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response

from telegram_assistant.config import McpConfig

MCP_ADMIN_SCOPE = "telegram:admin"


@dataclass(frozen=True)
class GoogleIdentity:
    """Google OIDC identity accepted by the local Authorization Server."""

    subject: str
    email: str
    email_verified: bool = True


class GoogleOidcProvider(Protocol):
    """Small seam for replacing live Google OIDC with a deterministic fake."""

    def authorization_url(self, *, state: str, redirect_uri: str) -> str:
        """Build a user-agent redirect URL for the Google login gate."""

    async def authenticate(self, *, code: str, redirect_uri: str) -> GoogleIdentity:
        """Exchange a Google authorization code and validate the returned id_token."""


class HttpGoogleOidcProvider:
    """Google OIDC provider backed by Google's token and tokeninfo endpoints."""

    _authorization_endpoint = "https://accounts.google.com/o/oauth2/v2/auth"
    _token_endpoint = "https://oauth2.googleapis.com/token"
    _tokeninfo_endpoint = "https://oauth2.googleapis.com/tokeninfo"

    def __init__(self, config: McpConfig) -> None:
        if config.google_client_id is None or config.google_client_secret is None:
            raise ValueError("Google client credentials are required")
        self._client_id = config.google_client_id
        self._client_secret = config.google_client_secret

    def authorization_url(self, *, state: str, redirect_uri: str) -> str:
        return (
            self._authorization_endpoint
            + "?"
            + urlencode(
                {
                    "client_id": self._client_id,
                    "redirect_uri": redirect_uri,
                    "response_type": "code",
                    "scope": "openid email profile",
                    "state": state,
                }
            )
        )

    async def authenticate(self, *, code: str, redirect_uri: str) -> GoogleIdentity:
        import httpx

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                token_response = await client.post(
                    self._token_endpoint,
                    data={
                        "grant_type": "authorization_code",
                        "code": code,
                        "client_id": self._client_id,
                        "client_secret": self._client_secret,
                        "redirect_uri": redirect_uri,
                    },
                )
                token_response.raise_for_status()
                id_token = token_response.json().get("id_token")
                if not id_token:
                    raise OAuthHttpError(
                        502,
                        "google_oidc_error",
                        "Google token response did not contain an id_token",
                    )

                tokeninfo_response = await client.get(
                    self._tokeninfo_endpoint,
                    params={"id_token": id_token},
                )
                tokeninfo_response.raise_for_status()
                claims = tokeninfo_response.json()
        except OAuthHttpError:
            raise
        except (httpx.HTTPError, ValueError) as exc:
            raise OAuthHttpError(
                502,
                "google_oidc_error",
                "Google OIDC validation failed",
            ) from exc

        if claims.get("aud") != self._client_id:
            raise OAuthHttpError(
                403,
                "invalid_google_token",
                "Google id_token audience does not match the configured client",
            )
        email = claims.get("email")
        if not isinstance(email, str) or not email:
            raise OAuthHttpError(
                403,
                "invalid_google_token",
                "Google id_token did not contain an email",
            )
        email_verified = claims.get("email_verified")
        if email_verified not in (True, "true", "True", "1", 1):
            raise OAuthHttpError(
                403,
                "invalid_google_token",
                "Google email is not verified",
            )
        subject = claims.get("sub")
        return GoogleIdentity(
            subject=str(subject or email),
            email=email,
            email_verified=True,
        )


@dataclass(frozen=True)
class RegisteredClient:
    client_id: str
    redirect_uris: tuple[str, ...]
    scopes: tuple[str, ...]
    client_name: str | None
    issued_at: int


@dataclass(frozen=True)
class PendingGoogleLogin:
    client_id: str
    redirect_uri: str
    requested_scopes: tuple[str, ...]
    resource: str
    client_state: str | None
    code_challenge: str
    code_challenge_method: str
    created_at: int


@dataclass(frozen=True)
class AuthorizationCode:
    code: str
    client_id: str
    redirect_uri: str
    requested_scopes: tuple[str, ...]
    resource: str
    subject: str
    email: str
    code_challenge: str
    code_challenge_method: str
    expires_at: int


@dataclass(frozen=True)
class TokenClaims:
    issuer: str
    subject: str
    audience: str
    scopes: tuple[str, ...]
    client_id: str
    email: str
    issued_at: int
    expires_at: int
    token_type: str


class OAuthHttpError(Exception):
    """OAuth-shaped HTTP error returned by AS endpoints."""

    def __init__(self, status_code: int, error: str, description: str) -> None:
        super().__init__(description)
        self.status_code = status_code
        self.error = error
        self.description = description

    def response(self) -> JSONResponse:
        return JSONResponse(
            {
                "error": self.error,
                "error_description": self.description,
            },
            status_code=self.status_code,
        )


class TokenValidationError(Exception):
    """Raised when an MCP bearer token cannot be accepted."""

    def __init__(self, status_code: int, error: str, description: str) -> None:
        super().__init__(description)
        self.status_code = status_code
        self.error = error
        self.description = description


def _base64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _base64url_decode(data: str) -> bytes:
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + padding)


def _json_b64(data: Mapping[str, Any]) -> str:
    return _base64url_encode(
        json.dumps(data, separators=(",", ":"), sort_keys=True).encode("utf-8")
    )


def _join_url(base: str, path: str) -> str:
    parts = urlsplit(base)
    base_path = parts.path.rstrip("/")
    joined_path = f"{base_path}/{path.lstrip('/')}" if base_path else f"/{path.lstrip('/')}"
    return urlunsplit((parts.scheme, parts.netloc, joined_path, "", ""))


def _metadata_url_for_resource(resource: str) -> str:
    parts = urlsplit(resource)
    resource_path = parts.path.rstrip("/")
    metadata_path = "/.well-known/oauth-protected-resource"
    if resource_path:
        metadata_path += resource_path
    return urlunsplit((parts.scheme, parts.netloc, metadata_path, "", ""))


def _normalize_audience(value: str) -> str:
    parts = urlsplit(value)
    scheme = parts.scheme.lower()
    netloc = parts.netloc.lower()
    path = parts.path.rstrip("/")
    return urlunsplit((scheme, netloc, path, "", ""))


def _normalize_redirect_uri(value: str) -> str:
    parts = urlsplit(value)
    return urlunsplit(
        (
            parts.scheme.lower(),
            parts.netloc.lower(),
            parts.path,
            parts.query,
            "",
        )
    )


def _append_query(url: str, params: Mapping[str, str]) -> str:
    separator = "&" if "?" in url else "?"
    return url + separator + urlencode(params)


def _scope_tuple(value: str | None, default: tuple[str, ...]) -> tuple[str, ...]:
    if value is None or value.strip() == "":
        return default
    return tuple(dict.fromkeys(part for part in value.split() if part))


def _public_dict_value(data: Mapping[str, Any], key: str) -> str | None:
    value = data.get(key)
    if value is None:
        return None
    if isinstance(value, list):
        if not value:
            return None
        value = value[0]
    return str(value)


class OAuthAuthorizationServer:
    """In-process OAuth AS for MCP clients, gated by Google OIDC login."""

    def __init__(
        self,
        config: McpConfig,
        *,
        google_provider: GoogleOidcProvider | None = None,
        now: Any | None = None,
    ) -> None:
        if not config.enabled:
            raise ValueError("MCP OAuth server requires mcp.enabled=true")
        if config.issuer_url is None or config.server_url is None or config.signing_secret is None:
            raise ValueError("MCP OAuth server config is incomplete")

        self.config = config
        self.issuer_url = config.issuer_url.rstrip("/")
        self.resource = config.server_url.rstrip("/")
        self._normalized_resource = _normalize_audience(self.resource)
        self._signing_secret = config.signing_secret
        self._required_scopes = tuple(dict.fromkeys(config.required_scopes))
        self._allowed_emails = {email.lower() for email in config.allowed_emails}
        self._allowed_domains = {
            domain.lower().lstrip("@") for domain in config.allowed_domains
        }
        self._admin_emails = {email.lower() for email in config.admin_emails}
        self._admin_domains = {
            domain.lower().lstrip("@") for domain in config.admin_domains
        }
        supported_scopes = list(self._required_scopes)
        if (
            self._admin_emails
            or self._admin_domains
            or MCP_ADMIN_SCOPE in self._required_scopes
        ):
            supported_scopes.append(MCP_ADMIN_SCOPE)
        self._supported_scopes = tuple(dict.fromkeys(supported_scopes))
        self._allowed_redirect_uris = {
            _normalize_redirect_uri(uri) for uri in config.allowed_redirect_uris
        }
        self._allowed_redirect_hosts = {
            host.lower().strip("[]") for host in config.allowed_redirect_hosts
        }
        self._google_provider = google_provider or HttpGoogleOidcProvider(config)
        self._now = now or time.time
        self._clients: dict[str, RegisteredClient] = {}
        self._pending_google_logins: dict[str, PendingGoogleLogin] = {}
        self._authorization_codes: dict[str, AuthorizationCode] = {}
        self._max_registered_clients = 1024
        self._max_pending_google_logins = 1024
        self._max_authorization_codes = 2048
        self._registered_client_ttl_seconds = 24 * 60 * 60
        self._pending_google_login_ttl_seconds = 300

    @property
    def authorization_endpoint(self) -> str:
        return _join_url(self.issuer_url, "/authorize")

    @property
    def token_endpoint(self) -> str:
        return _join_url(self.issuer_url, "/token")

    @property
    def registration_endpoint(self) -> str:
        return _join_url(self.issuer_url, "/register")

    @property
    def protected_resource_metadata_url(self) -> str:
        return _metadata_url_for_resource(self.resource)

    def authorization_server_metadata(self) -> dict[str, Any]:
        return {
            "issuer": self.issuer_url,
            "authorization_endpoint": self.authorization_endpoint,
            "token_endpoint": self.token_endpoint,
            "registration_endpoint": self.registration_endpoint,
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": ["none"],
            "scopes_supported": list(self._supported_scopes),
            "resource_indicators_supported": True,
        }

    def protected_resource_metadata(self) -> dict[str, Any]:
        return {
            "resource": self.resource,
            "authorization_servers": [self.issuer_url],
            "scopes_supported": list(self._supported_scopes),
            "bearer_methods_supported": ["header"],
        }

    def register_client(self, data: Mapping[str, Any]) -> dict[str, Any]:
        self._cleanup_ephemeral_state()
        if len(self._clients) >= self._max_registered_clients:
            self._evict_oldest_registered_client()

        redirect_uris_raw = data.get("redirect_uris")
        if not isinstance(redirect_uris_raw, list) or not redirect_uris_raw:
            raise OAuthHttpError(
                400,
                "invalid_client_metadata",
                "redirect_uris must be a non-empty list",
            )
        if not all(isinstance(uri, str) and uri for uri in redirect_uris_raw):
            raise OAuthHttpError(
                400,
                "invalid_client_metadata",
                "redirect_uris entries must be non-empty strings",
            )
        redirect_uris = tuple(redirect_uris_raw)
        for redirect_uri in redirect_uris:
            self._validate_redirect_uri(redirect_uri)

        requested_scopes = self._validate_scopes(
            _scope_tuple(_public_dict_value(data, "scope"), self._required_scopes)
        )
        client_id = "mcp_" + secrets.token_urlsafe(24)
        client = RegisteredClient(
            client_id=client_id,
            redirect_uris=redirect_uris,
            scopes=requested_scopes,
            client_name=_public_dict_value(data, "client_name"),
            issued_at=self._timestamp(),
        )
        self._clients[client_id] = client
        return {
            "client_id": client.client_id,
            "client_id_issued_at": client.issued_at,
            "redirect_uris": list(client.redirect_uris),
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
            "scope": " ".join(client.scopes),
            **({"client_name": client.client_name} if client.client_name else {}),
        }

    def start_authorization(self, params: Mapping[str, str]) -> str:
        self._cleanup_ephemeral_state()
        if params.get("response_type") != "code":
            raise OAuthHttpError(400, "unsupported_response_type", "response_type must be code")

        client_id = params.get("client_id")
        if client_id is None or client_id not in self._clients:
            raise OAuthHttpError(400, "invalid_client", "Unknown client_id")
        client = self._clients[client_id]

        redirect_uri = params.get("redirect_uri")
        if redirect_uri not in client.redirect_uris:
            raise OAuthHttpError(400, "invalid_request", "redirect_uri is not registered")

        resource = params.get("resource")
        if resource is None:
            raise OAuthHttpError(400, "invalid_target", "resource is required")
        self._validate_resource(resource)

        requested_scopes = self._validate_scopes(
            _scope_tuple(params.get("scope"), self._required_scopes)
        )
        if not set(requested_scopes).issubset(set(client.scopes)):
            raise OAuthHttpError(
                400,
                "invalid_scope",
                "requested scopes exceed registered client scopes",
            )

        code_challenge = params.get("code_challenge")
        if not code_challenge:
            raise OAuthHttpError(400, "invalid_request", "code_challenge is required")
        code_challenge_method = params.get("code_challenge_method")
        if code_challenge_method != "S256":
            raise OAuthHttpError(
                400,
                "invalid_request",
                "code_challenge_method must be S256",
            )
        if len(self._pending_google_logins) >= self._max_pending_google_logins:
            raise OAuthHttpError(
                429,
                "temporarily_unavailable",
                "too many pending OAuth authorizations",
            )

        google_state = secrets.token_urlsafe(32)
        self._pending_google_logins[google_state] = PendingGoogleLogin(
            client_id=client.client_id,
            redirect_uri=redirect_uri,
            requested_scopes=requested_scopes,
            resource=resource,
            client_state=params.get("state"),
            code_challenge=code_challenge,
            code_challenge_method=code_challenge_method,
            created_at=self._timestamp(),
        )
        return self._google_provider.authorization_url(
            state=google_state,
            redirect_uri=self.authorization_endpoint,
        )

    def is_google_callback(self, params: Mapping[str, str]) -> bool:
        return "code" in params and params.get("state") in self._pending_google_logins

    async def complete_google_login(self, *, state: str, code: str) -> str:
        self._cleanup_ephemeral_state()
        pending = self._pending_google_logins.pop(state, None)
        if pending is None:
            raise OAuthHttpError(400, "invalid_request", "Unknown Google login state")

        try:
            identity = await self._google_provider.authenticate(
                code=code,
                redirect_uri=self.authorization_endpoint,
            )
            self._validate_google_identity(identity)
            self._validate_scope_identity(identity, pending.requested_scopes)
            if len(self._authorization_codes) >= self._max_authorization_codes:
                raise OAuthHttpError(
                    429,
                    "temporarily_unavailable",
                    "too many pending OAuth authorization codes",
                )
        except OAuthHttpError as exc:
            return self._authorization_error_redirect(pending, exc)

        authorization_code = secrets.token_urlsafe(32)
        self._authorization_codes[authorization_code] = AuthorizationCode(
            code=authorization_code,
            client_id=pending.client_id,
            redirect_uri=pending.redirect_uri,
            requested_scopes=pending.requested_scopes,
            resource=pending.resource,
            subject=identity.subject,
            email=identity.email,
            code_challenge=pending.code_challenge,
            code_challenge_method=pending.code_challenge_method,
            expires_at=self._timestamp() + 300,
        )
        redirect_params = {"code": authorization_code}
        if pending.client_state is not None:
            redirect_params["state"] = pending.client_state
        return _append_query(pending.redirect_uri, redirect_params)

    def exchange_token(self, data: Mapping[str, str]) -> dict[str, Any]:
        grant_type = data.get("grant_type")
        if grant_type == "authorization_code":
            return self._exchange_authorization_code(data)
        if grant_type == "refresh_token":
            return self._exchange_refresh_token(data)
        raise OAuthHttpError(400, "unsupported_grant_type", "grant_type is not supported")

    def validate_access_token(
        self,
        token: str,
        *,
        audience: str | None = None,
        required_scopes: tuple[str, ...] | list[str] | None = None,
        now: int | None = None,
    ) -> TokenClaims:
        claims = self._decode_signed_token(token)
        if claims.get("typ") != "access":
            raise TokenValidationError(401, "invalid_token", "token is not an access token")

        expected_audience = audience or self.resource
        if _normalize_audience(str(claims.get("aud", ""))) != _normalize_audience(
            expected_audience
        ):
            raise TokenValidationError(401, "invalid_token", "token audience is invalid")
        if claims.get("iss") != self.issuer_url:
            raise TokenValidationError(401, "invalid_token", "token issuer is invalid")

        now_ts = self._timestamp() if now is None else now
        exp = self._int_claim(claims, "exp")
        if exp <= now_ts:
            raise TokenValidationError(401, "invalid_token", "token has expired")

        scopes = tuple(str(claims.get("scope", "")).split())
        needed = tuple(required_scopes) if required_scopes is not None else self._required_scopes
        if not set(needed).issubset(set(scopes)):
            raise TokenValidationError(403, "insufficient_scope", "token scopes are insufficient")

        return TokenClaims(
            issuer=str(claims["iss"]),
            subject=str(claims["sub"]),
            audience=str(claims["aud"]),
            scopes=scopes,
            client_id=str(claims["client_id"]),
            email=str(claims["email"]),
            issued_at=self._int_claim(claims, "iat"),
            expires_at=exp,
            token_type=str(claims["typ"]),
        )

    def _exchange_authorization_code(self, data: Mapping[str, str]) -> dict[str, Any]:
        self._cleanup_ephemeral_state()
        code = data.get("code")
        if not code:
            raise OAuthHttpError(400, "invalid_request", "code is required")
        authorization_code = self._authorization_codes.pop(code, None)
        if authorization_code is None:
            raise OAuthHttpError(400, "invalid_grant", "authorization code is invalid")
        if authorization_code.expires_at <= self._timestamp():
            raise OAuthHttpError(400, "invalid_grant", "authorization code has expired")

        if data.get("client_id") != authorization_code.client_id:
            raise OAuthHttpError(400, "invalid_client", "client_id does not match code")
        if data.get("redirect_uri") != authorization_code.redirect_uri:
            raise OAuthHttpError(400, "invalid_grant", "redirect_uri does not match code")
        resource = data.get("resource")
        if resource is None:
            raise OAuthHttpError(400, "invalid_target", "resource is required")
        if _normalize_audience(resource) != _normalize_audience(authorization_code.resource):
            raise OAuthHttpError(400, "invalid_target", "resource does not match code")

        self._validate_pkce(
            verifier=data.get("code_verifier"),
            challenge=authorization_code.code_challenge,
        )
        return self._token_response(
            client_id=authorization_code.client_id,
            subject=authorization_code.subject,
            email=authorization_code.email,
            scopes=authorization_code.requested_scopes,
            resource=authorization_code.resource,
        )

    def _exchange_refresh_token(self, data: Mapping[str, str]) -> dict[str, Any]:
        refresh_token = data.get("refresh_token")
        if not refresh_token:
            raise OAuthHttpError(400, "invalid_request", "refresh_token is required")
        claims = self._decode_signed_token(refresh_token, for_oauth_error=True)
        if claims.get("typ") != "refresh":
            raise OAuthHttpError(400, "invalid_grant", "refresh token is invalid")
        if claims.get("iss") != self.issuer_url:
            raise OAuthHttpError(400, "invalid_grant", "refresh token issuer is invalid")
        exp = self._int_claim(claims, "exp", for_oauth_error=True)
        if exp <= self._timestamp():
            raise OAuthHttpError(400, "invalid_grant", "refresh token has expired")

        resource = data.get("resource") or str(claims.get("aud", ""))
        if _normalize_audience(resource) != _normalize_audience(str(claims.get("aud", ""))):
            raise OAuthHttpError(400, "invalid_target", "resource does not match refresh token")
        self._validate_resource(resource)

        scopes = tuple(str(claims.get("scope", "")).split())
        identity = GoogleIdentity(
            subject=str(claims["sub"]),
            email=str(claims["email"]),
        )
        self._validate_google_identity(identity)
        self._validate_scope_identity(identity, scopes)
        return self._token_response(
            client_id=str(claims["client_id"]),
            subject=str(claims["sub"]),
            email=str(claims["email"]),
            scopes=scopes,
            resource=resource,
        )

    def _authorization_error_redirect(
        self,
        pending: PendingGoogleLogin,
        exc: OAuthHttpError,
    ) -> str:
        params = {
            "error": exc.error,
            "error_description": exc.description,
        }
        if pending.client_state is not None:
            params["state"] = pending.client_state
        return _append_query(pending.redirect_uri, params)

    def _cleanup_ephemeral_state(self) -> None:
        now_ts = self._timestamp()
        client_cutoff = now_ts - self._registered_client_ttl_seconds
        self._clients = {
            client_id: client
            for client_id, client in self._clients.items()
            if client.issued_at > client_cutoff
        }
        pending_cutoff = now_ts - self._pending_google_login_ttl_seconds
        self._pending_google_logins = {
            state: pending
            for state, pending in self._pending_google_logins.items()
            if pending.created_at > pending_cutoff
        }
        self._authorization_codes = {
            code: authorization_code
            for code, authorization_code in self._authorization_codes.items()
            if authorization_code.expires_at > now_ts
        }

    def _evict_oldest_registered_client(self) -> None:
        if not self._clients:
            return
        oldest_client_id = min(
            self._clients,
            key=lambda client_id: self._clients[client_id].issued_at,
        )
        del self._clients[oldest_client_id]

    def _token_response(
        self,
        *,
        client_id: str,
        subject: str,
        email: str,
        scopes: tuple[str, ...],
        resource: str,
    ) -> dict[str, Any]:
        now_ts = self._timestamp()
        access_exp = now_ts + self.config.access_token_ttl_seconds
        refresh_exp = now_ts + self.config.refresh_token_ttl_seconds
        access_token = self._encode_signed_token(
            {
                "typ": "access",
                "iss": self.issuer_url,
                "sub": subject,
                "aud": resource,
                "iat": now_ts,
                "exp": access_exp,
                "client_id": client_id,
                "email": email,
                "scope": " ".join(scopes),
            }
        )
        refresh_token = self._encode_signed_token(
            {
                "typ": "refresh",
                "iss": self.issuer_url,
                "sub": subject,
                "aud": resource,
                "iat": now_ts,
                "exp": refresh_exp,
                "client_id": client_id,
                "email": email,
                "scope": " ".join(scopes),
            }
        )
        return {
            "access_token": access_token,
            "token_type": "Bearer",
            "expires_in": self.config.access_token_ttl_seconds,
            "refresh_token": refresh_token,
            "scope": " ".join(scopes),
        }

    def _validate_scopes(self, scopes: tuple[str, ...]) -> tuple[str, ...]:
        unsupported = sorted(set(scopes) - set(self._supported_scopes))
        if unsupported:
            raise OAuthHttpError(
                400,
                "invalid_scope",
                "unsupported scopes: " + ", ".join(unsupported),
            )
        return scopes

    def _validate_resource(self, resource: str) -> None:
        if _normalize_audience(resource) != self._normalized_resource:
            raise OAuthHttpError(400, "invalid_target", "resource is not this MCP server")

    def _validate_redirect_uri(self, redirect_uri: str) -> None:
        parts = urlsplit(redirect_uri)
        if parts.fragment:
            raise OAuthHttpError(
                400,
                "invalid_client_metadata",
                "redirect_uris must not contain fragments",
            )
        if parts.scheme.lower() not in {"http", "https"} or not parts.hostname:
            raise OAuthHttpError(
                400,
                "invalid_client_metadata",
                "redirect_uris must be absolute HTTP(S) URLs",
            )
        if _normalize_redirect_uri(redirect_uri) in self._allowed_redirect_uris:
            return
        if parts.hostname.lower() in self._allowed_redirect_hosts:
            return
        raise OAuthHttpError(
            400,
            "invalid_client_metadata",
            "redirect_uris must use a configured trusted URI or host",
        )

    def _validate_pkce(self, *, verifier: str | None, challenge: str) -> None:
        if not verifier:
            raise OAuthHttpError(400, "invalid_request", "code_verifier is required")
        try:
            verifier_bytes = verifier.encode("ascii")
        except UnicodeEncodeError as exc:
            raise OAuthHttpError(
                400,
                "invalid_request",
                "code_verifier must contain only ASCII characters",
            ) from exc
        computed = _base64url_encode(hashlib.sha256(verifier_bytes).digest())
        if not secrets.compare_digest(computed, challenge):
            raise OAuthHttpError(400, "invalid_grant", "PKCE verification failed")

    def _validate_google_identity(self, identity: GoogleIdentity) -> None:
        email = identity.email.lower()
        domain = email.rsplit("@", 1)[-1] if "@" in email else ""
        if not identity.email_verified:
            raise OAuthHttpError(403, "access_denied", "Google email is not verified")
        if email in self._allowed_emails or domain in self._allowed_domains:
            return
        raise OAuthHttpError(403, "access_denied", "Google account is not allowed")

    def _validate_scope_identity(
        self,
        identity: GoogleIdentity,
        scopes: tuple[str, ...],
    ) -> None:
        if MCP_ADMIN_SCOPE not in scopes:
            return
        email = identity.email.lower()
        domain = email.rsplit("@", 1)[-1] if "@" in email else ""
        if email in self._admin_emails or domain in self._admin_domains:
            return
        raise OAuthHttpError(
            403,
            "access_denied",
            "Google account is not allowed to request telegram:admin",
        )

    def _encode_signed_token(self, claims: Mapping[str, Any]) -> str:
        header = _json_b64({"alg": "HS256", "typ": "JWT"})
        payload = _json_b64(claims)
        signing_input = f"{header}.{payload}"
        signature = hmac.new(
            self._signing_secret.encode("utf-8"),
            signing_input.encode("ascii"),
            hashlib.sha256,
        ).digest()
        return f"{signing_input}.{_base64url_encode(signature)}"

    def _decode_signed_token(
        self,
        token: str,
        *,
        for_oauth_error: bool = False,
    ) -> dict[str, Any]:
        try:
            header_b64, payload_b64, signature_b64 = token.split(".", 2)
            header = json.loads(_base64url_decode(header_b64))
            if header.get("alg") != "HS256":
                raise ValueError("unsupported alg")
            signing_input = f"{header_b64}.{payload_b64}"
            expected_signature = hmac.new(
                self._signing_secret.encode("utf-8"),
                signing_input.encode("ascii"),
                hashlib.sha256,
            ).digest()
            actual_signature = _base64url_decode(signature_b64)
            if not hmac.compare_digest(expected_signature, actual_signature):
                raise ValueError("signature mismatch")
            payload = json.loads(_base64url_decode(payload_b64))
            if not isinstance(payload, dict):
                raise ValueError("payload is not an object")
            return payload
        except Exception as exc:
            if for_oauth_error:
                raise OAuthHttpError(400, "invalid_grant", "token is invalid") from exc
            raise TokenValidationError(401, "invalid_token", "token is invalid") from exc

    def _int_claim(
        self,
        claims: Mapping[str, Any],
        key: str,
        *,
        for_oauth_error: bool = False,
    ) -> int:
        try:
            return int(claims[key])
        except Exception as exc:
            if for_oauth_error:
                raise OAuthHttpError(400, "invalid_grant", f"token missing {key}") from exc
            raise TokenValidationError(401, "invalid_token", f"token missing {key}") from exc

    def _timestamp(self) -> int:
        return int(self._now())


async def _request_data(request: Request) -> dict[str, Any]:
    content_type = request.headers.get("content-type", "")
    if content_type.startswith("application/json"):
        payload = await request.json()
        if not isinstance(payload, dict):
            raise OAuthHttpError(400, "invalid_request", "request body must be an object")
        return payload

    body = (await request.body()).decode("utf-8")
    parsed = parse_qs(body, keep_blank_values=True)
    return {key: values[-1] for key, values in parsed.items()}


def build_oauth_router(server: OAuthAuthorizationServer) -> APIRouter:
    """Build FastAPI routes for the local OAuth Authorization Server."""

    router = APIRouter()

    @router.get("/.well-known/oauth-authorization-server")
    async def authorization_server_metadata() -> dict[str, Any]:
        return server.authorization_server_metadata()

    @router.get("/.well-known/oauth-protected-resource")
    async def protected_resource_metadata() -> dict[str, Any]:
        return server.protected_resource_metadata()

    @router.get("/.well-known/oauth-protected-resource/{_path:path}")
    async def protected_resource_metadata_for_path(_path: str) -> dict[str, Any]:
        return server.protected_resource_metadata()

    @router.post("/register")
    async def register(request: Request) -> Response:
        try:
            data = await _request_data(request)
            return JSONResponse(server.register_client(data), status_code=201)
        except OAuthHttpError as exc:
            return exc.response()

    @router.get("/authorize")
    async def authorize(request: Request) -> Response:
        params = {key: value for key, value in request.query_params.items()}
        try:
            if server.is_google_callback(params):
                redirect_url = await server.complete_google_login(
                    state=params["state"],
                    code=params["code"],
                )
                return RedirectResponse(redirect_url, status_code=302)
            redirect_url = server.start_authorization(params)
            return RedirectResponse(redirect_url, status_code=302)
        except OAuthHttpError as exc:
            return exc.response()

    @router.post("/token")
    async def token(request: Request) -> Response:
        try:
            data = await _request_data(request)
            str_data = {key: str(value) for key, value in data.items()}
            return JSONResponse(server.exchange_token(str_data))
        except OAuthHttpError as exc:
            return exc.response()

    return router
