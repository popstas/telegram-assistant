"""HTTP routes for muting/unmuting a chat or contact's notifications."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, model_validator

from telegram_assistant.access import AccessDenied
from telegram_assistant.folders import (
    AmbiguousChatNameError,
    ChatNotFoundError,
    FolderBackend,
    FolderError,
    FolderIdMismatchError,
    FolderNotFoundError,
    resolve_chat_in_folder,
)
from telegram_assistant.http_api.access import (
    build_authorizer,
    resolve_entity_chat_id,
    translate_access_error,
)
from telegram_assistant.http_api.auth import BearerAuth
from telegram_assistant.notifications import (
    MuteRequest,
    NotificationBackend,
    mute_chat,
    unmute_chat,
)
from telegram_assistant.worker.queue import FloodWaitError


def _translate_flood_wait(exc: FloodWaitError) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_502_BAD_GATEWAY,
        detail={"error": "needs_review", "message": str(exc)},
    )


class _TargetBody(BaseModel):
    """Shared target shape: exactly one of chat_id/entity/chat_name+folder_name."""

    telegram_chat_id: int | None = None
    entity: str | int | None = None
    chat_name: str | None = None
    folder_name: str | None = None
    folder_id: int | None = None

    @model_validator(mode="after")
    def _target_shape(self) -> _TargetBody:
        refs = sum(
            [
                self.telegram_chat_id is not None,
                self.entity is not None,
                self.chat_name is not None,
            ]
        )
        if refs != 1:
            raise ValueError(
                "provide exactly one of telegram_chat_id, entity, or chat_name"
            )
        if self.chat_name is not None and self.folder_name is None:
            raise ValueError("chat_name requires folder_name")
        return self


class MuteBody(_TargetBody):
    # ``duration_hours`` mutes until ``now + duration``; omitted ⇒ mute forever.
    duration_hours: int | None = None

    @model_validator(mode="after")
    def _duration_positive(self) -> MuteBody:
        if self.duration_hours is not None and self.duration_hours <= 0:
            raise ValueError("duration_hours must be a positive integer")
        return self


class UnmuteBody(_TargetBody):
    pass


def _backend_or_503(request: Request) -> NotificationBackend:
    factory = getattr(request.app.state, "notification_backend_factory", None)
    if factory is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Telegram notification backend is not configured (session may be unauthorized)",
        )
    backend = factory(request)
    if backend is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Telegram notification backend is not available",
        )
    return backend


def _folder_backend_optional(request: Request) -> FolderBackend | None:
    factory = getattr(request.app.state, "folder_backend_factory", None)
    if factory is None:
        return None
    return factory(request)


def _folder_backend_or_503(request: Request) -> FolderBackend:
    backend = _folder_backend_optional(request)
    if backend is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Telegram folder backend is not available for chat_name resolution",
        )
    return backend


def _translate_folder_error(exc: Exception) -> HTTPException:
    if isinstance(exc, FolderNotFoundError):
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    if isinstance(exc, FolderIdMismatchError):
        return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    if isinstance(exc, AmbiguousChatNameError):
        return HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": "ambiguous_chat_name",
                "chat_name": exc.chat_name,
                "folder_name": exc.folder_name,
                "matches": exc.matches,
            },
        )
    if isinstance(exc, ChatNotFoundError):
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    return HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)
    )


async def _resolve_target(
    request: Request, body: _TargetBody
) -> tuple[int, str | None]:
    """Resolve the body's target into ``(telegram_chat_id, chat_name_for_log)``."""
    if body.entity is not None:
        return await resolve_entity_chat_id(request, body.entity), None
    if body.telegram_chat_id is not None:
        return body.telegram_chat_id, None
    folder_backend = _folder_backend_or_503(request)
    try:
        chat = await resolve_chat_in_folder(
            folder_backend,
            folder_name=body.folder_name or "",
            chat_name=body.chat_name or "",
            folder_id=body.folder_id,
        )
    except FolderError as exc:
        raise _translate_folder_error(exc) from exc
    return chat.chat_id, chat.title


def build_router() -> APIRouter:
    router = APIRouter(dependencies=[BearerAuth])

    @router.post("/notifications/mute")
    async def mute(body: MuteBody, request: Request) -> dict[str, Any]:
        backend = _backend_or_503(request)
        telegram_chat_id, chat_name = await _resolve_target(request, body)
        authorizer = build_authorizer(
            request, folder_backend=_folder_backend_optional(request)
        )
        try:
            result = await mute_chat(
                backend,
                request=MuteRequest(
                    telegram_chat_id=telegram_chat_id,
                    duration_hours=body.duration_hours,
                    chat_name=chat_name,
                ),
                authorizer=authorizer,
            )
        except AccessDenied as exc:
            raise translate_access_error(exc) from exc
        except FloodWaitError as exc:
            raise _translate_flood_wait(exc) from exc
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
            ) from exc
        return result.to_dict()

    @router.post("/notifications/unmute")
    async def unmute(body: UnmuteBody, request: Request) -> dict[str, Any]:
        backend = _backend_or_503(request)
        telegram_chat_id, chat_name = await _resolve_target(request, body)
        authorizer = build_authorizer(
            request, folder_backend=_folder_backend_optional(request)
        )
        try:
            result = await unmute_chat(
                backend,
                request=MuteRequest(
                    telegram_chat_id=telegram_chat_id,
                    chat_name=chat_name,
                ),
                authorizer=authorizer,
            )
        except AccessDenied as exc:
            raise translate_access_error(exc) from exc
        except FloodWaitError as exc:
            raise _translate_flood_wait(exc) from exc
        return result.to_dict()

    return router
