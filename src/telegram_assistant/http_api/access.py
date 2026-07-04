"""Shared HTTP wiring for the entity resolver and the access authorizer.

Every protected router needs the same three things once access control and the
shared entity resolver are in play:

* build an :class:`~telegram_assistant.entities.EntityResolver` from the
  Telethon session (via ``app.state.resolver_factory``);
* build an :class:`~telegram_assistant.access.Authorizer` from
  ``config.telegram.access`` plus that resolver and a folder backend (so it can
  resolve ``chat`` rules and discover folder memberships) — a no-op sentinel
  when ``access`` is ``None``;
* translate the resulting domain errors into HTTP responses
  (``AccessDenied`` → 403, entity not-found → 404, ambiguous entity → 409).

Keeping this in one module means the routers stay thin and the 403/404/409
contract is defined in exactly one place.
"""

from __future__ import annotations

from fastapi import HTTPException, Request, status

from telegram_assistant.access import AccessDenied, Authorizer
from telegram_assistant.entities import (
    AmbiguousEntityError,
    EntityNotFoundError,
    EntityResolver,
)
from telegram_assistant.folders import FolderBackend


def resolver_optional(request: Request) -> EntityResolver | None:
    """Return the per-request entity resolver, or ``None`` when unavailable.

    Mirrors the backend-factory contract: the factory returns ``None`` until a
    Telethon client is connected, so callers that strictly need a resolver can
    surface 503.
    """
    factory = getattr(request.app.state, "resolver_factory", None)
    if factory is None:
        return None
    return factory(request)


def build_authorizer(
    request: Request, *, folder_backend: FolderBackend | None = None
) -> Authorizer:
    """Construct the per-request :class:`Authorizer` from app config.

    When ``config.telegram.access`` is ``None`` this yields the allow-all no-op
    sentinel; the resolver/folder backend are only consulted lazily when the
    policy actually references ``chat`` / ``folder`` targets.
    """
    config = getattr(request.app.state, "config", None)
    access = None
    if config is not None:
        access = getattr(config.telegram, "access", None)
    resolver = resolver_optional(request)
    return Authorizer(access, resolver=resolver, folder_backend=folder_backend)


def translate_entity_error(exc: Exception) -> HTTPException | None:
    """Map an entity-resolution error to an HTTP response (or ``None``)."""
    if isinstance(exc, AmbiguousEntityError):
        return HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": "ambiguous_entity",
                "entity": exc.ref,
                "matches": exc.matches,
            },
        )
    if isinstance(exc, EntityNotFoundError):
        return HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        )
    return None


def translate_access_error(exc: Exception) -> HTTPException | None:
    """Map ``AccessDenied`` (and entity errors) to an HTTP response (or ``None``)."""
    if isinstance(exc, AccessDenied):
        return HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error": "access_denied",
                "chat_ref": str(exc.chat_ref),
                "required_level": exc.required_level.name.lower(),
                "granted_level": (
                    exc.granted_level.name.lower()
                    if exc.granted_level is not None
                    else None
                ),
                "matched_rule": exc.matched_rule,
            },
        )
    return translate_entity_error(exc)


def delete_only_session_messages_default(request: Request) -> bool:
    """The policy-level ``delete_only_session_messages`` default.

    Reads ``config.telegram.access.delete_only_session_messages``. When the
    access policy is omitted (allow-all) the safe default ``True`` still applies
    so out of the box delete only touches messages this process sent. Per-rule
    overrides are resolved separately via
    :meth:`Authorizer.delete_only_session_messages`, which takes this value as
    its ``default``.
    """
    config = getattr(request.app.state, "config", None)
    access = None
    if config is not None:
        access = getattr(config.telegram, "access", None)
    if access is None:
        return True
    return access.delete_only_session_messages


def sent_message_registry(request: Request):
    """Return the process-global sent-message registry, or ``None``."""
    return getattr(request.app.state, "sent_message_registry", None)


async def resolve_entity_chat_id(request: Request, entity: str | int) -> int:
    """Resolve an entity reference to a numeric ``chat_id`` for HTTP callers.

    Raises 503 when no resolver is wired (session unauthorized), 404/409 for
    not-found / ambiguous references.
    """
    resolver = resolver_optional(request)
    if resolver is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Telegram entity resolver is not available (session may be unauthorized)",
        )
    try:
        resolved = await resolver.resolve(entity)
    except (AmbiguousEntityError, EntityNotFoundError) as exc:
        translated = translate_entity_error(exc)
        assert translated is not None  # noqa: S101 - both branches handled above
        raise translated from exc
    return resolved.chat_id


__all__ = [
    "build_authorizer",
    "delete_only_session_messages_default",
    "resolve_entity_chat_id",
    "resolver_optional",
    "sent_message_registry",
    "translate_access_error",
    "translate_entity_error",
]
