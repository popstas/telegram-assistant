"""HTTP routes for chat-folder inspection and chat-into-folder placement."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field, model_validator

from telegram_assistant.access import AccessDenied
from telegram_assistant.folders import (
    AmbiguousChatNameError,
    ChatNotFoundError,
    FolderBackend,
    FolderIdMismatchError,
    FolderNotFoundError,
    FolderPeerFailureError,
    add_chat_to_folder,
    inspect_folder,
    resolve_chat_in_folder,
)
from telegram_assistant.http_api.access import (
    build_authorizer,
    resolve_entity_chat_id,
    translate_access_error,
)
from telegram_assistant.http_api.auth import BearerAuth


class AddChatRequest(BaseModel):
    chat_id: int | None = None
    chat_name: str | None = None
    entity: str | int | None = None
    folder_id: int | None = Field(
        default=None,
        description="Optional cross-check against the resolved folder id.",
    )

    @model_validator(mode="after")
    def _exactly_one_chat_ref(self) -> AddChatRequest:
        refs = sum(
            [self.chat_id is not None, self.chat_name is not None, self.entity is not None]
        )
        if refs != 1:
            raise ValueError(
                "provide exactly one of chat_id, chat_name, or entity"
            )
        return self


def _backend_or_503(request: Request) -> FolderBackend:
    factory = getattr(request.app.state, "folder_backend_factory", None)
    if factory is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Telegram folder backend is not configured (session may be unauthorized)",
        )
    backend = factory(request)
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
    if isinstance(exc, FolderPeerFailureError):
        # The worker layer would persist this as needs_review; HTTP callers
        # need to see that the change didn't take effect.
        return HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"error": "needs_review", "message": str(exc)},
        )
    return HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)
    )


def build_router() -> APIRouter:
    router = APIRouter(dependencies=[BearerAuth])

    @router.get("/folders/{folder_name}")
    async def inspect(
        folder_name: str,
        request: Request,
        folder_id: int | None = None,
    ) -> dict[str, Any]:
        backend = _backend_or_503(request)
        try:
            snapshot = await inspect_folder(
                backend, folder_name=folder_name, folder_id=folder_id
            )
        except Exception as exc:
            raise _translate_folder_error(exc) from exc
        return snapshot.to_dict()

    @router.post("/folders/{folder_name}/chats")
    async def add_chat(
        folder_name: str,
        body: AddChatRequest,
        request: Request,
    ) -> dict[str, Any]:
        backend = _backend_or_503(request)
        if body.entity is not None:
            chat_ref: str | int = await resolve_entity_chat_id(request, body.entity)
        elif body.chat_id is not None:
            chat_ref = body.chat_id
        else:
            chat_ref = None  # type: ignore[assignment]
        authorizer = build_authorizer(request, folder_backend=backend)
        try:
            if chat_ref is None:
                # chat_name -> chat_id via folder membership lookup
                resolved = await resolve_chat_in_folder(
                    backend,
                    folder_name=folder_name,
                    chat_name=body.chat_name or "",
                    folder_id=body.folder_id,
                )
                chat_ref = resolved.chat_id
            result = await add_chat_to_folder(
                backend,
                folder_name=folder_name,
                chat_ref=chat_ref,
                folder_id=body.folder_id,
                authorizer=authorizer,
            )
        except AccessDenied as exc:
            raise translate_access_error(exc) from exc
        except HTTPException:
            raise
        except Exception as exc:
            raise _translate_folder_error(exc) from exc
        return result

    return router
