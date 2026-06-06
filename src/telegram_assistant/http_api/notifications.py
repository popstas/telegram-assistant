"""HTTP routes for chat notification settings."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field, model_validator

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


class NotificationTargetBody(BaseModel):
    chat_id: int | None = None
    chat_name: str | None = None
    entity: str | int | None = None
    folder_name: str | None = None
    folder_id: int | None = Field(
        default=None,
        description="Optional cross-check against the resolved folder id.",
    )

    @model_validator(mode="after")
    def _target_shape(self) -> NotificationTargetBody:
        refs = sum(
            [self.chat_id is not None, self.chat_name is not None, self.entity is not None]
        )
        if refs != 1:
            raise ValueError("provide exactly one of chat_id, chat_name, or entity")
        if self.chat_name is not None and not self.folder_name:
            raise ValueError("chat_name requires folder_name")
        return self


class MuteBody(NotificationTargetBody):
    duration_hours: int | None = Field(default=None, gt=0)


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
            detail="Telegram folder backend is not available",
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


async def _resolve_chat_id(body: NotificationTargetBody, request: Request) -> int:
    if body.entity is not None:
        return await resolve_entity_chat_id(request, body.entity)
    if body.chat_id is not None:
        return body.chat_id
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
    return chat.chat_id


def build_router() -> APIRouter:
    router = APIRouter(dependencies=[BearerAuth])

    @router.post("/notifications/mute")
    async def mute(body: MuteBody, request: Request) -> dict[str, Any]:
        backend = _backend_or_503(request)
        chat_id = await _resolve_chat_id(body, request)
        duration = (
            timedelta(hours=body.duration_hours)
            if body.duration_hours is not None
            else None
        )
        authorizer = build_authorizer(
            request, folder_backend=_folder_backend_optional(request)
        )
        try:
            result = await mute_chat(
                backend=backend,
                request=MuteRequest(telegram_chat_id=chat_id, duration=duration),
                authorizer=authorizer,
            )
        except AccessDenied as exc:
            raise translate_access_error(exc) from exc
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
            ) from exc
        return result.to_dict()

    @router.post("/notifications/unmute")
    async def unmute(
        body: NotificationTargetBody, request: Request
    ) -> dict[str, Any]:
        backend = _backend_or_503(request)
        chat_id = await _resolve_chat_id(body, request)
        authorizer = build_authorizer(
            request, folder_backend=_folder_backend_optional(request)
        )
        try:
            result = await unmute_chat(
                backend=backend,
                request=MuteRequest(telegram_chat_id=chat_id),
                authorizer=authorizer,
            )
        except AccessDenied as exc:
            raise translate_access_error(exc) from exc
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
            ) from exc
        return result.to_dict()

    return router
