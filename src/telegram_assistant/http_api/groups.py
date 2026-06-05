"""HTTP routes for supergroup creation."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request, status
from pydantic import BaseModel, Field

from telegram_assistant.folders import FolderBackend, FolderError
from telegram_assistant.groups import (
    GroupBackend,
    GroupCreateFailed,
    GroupCreateNeedsReview,
    GroupCreatePending,
    GroupCreateRequest,
    GroupLayoutSetFailed,
    GroupLayoutSetNeedsReview,
    GroupLayoutSetPending,
    LayoutSetRequest,
    create_group,
    get_topics_layout,
    set_topics_layout,
)
from telegram_assistant.http_api.auth import BearerAuth
from telegram_assistant.persistence.store import OperationStore
from telegram_assistant.worker.queue import FloodWaitError


class LayoutSetBody(BaseModel):
    chat_id: int
    layout: Literal["list", "tabs"]


class GroupCreateBody(BaseModel):
    title: str = Field(..., min_length=1)
    # Generic idempotency anchor. ``planfix_task_id`` is a backward-compat alias.
    external_ref: int | str | None = None
    planfix_task_id: int | str | None = None
    about: str | None = None
    admins: list[str] = Field(default_factory=list)
    members: list[str] = Field(default_factory=list)
    reserve_admins: list[str] | None = None
    reserve_members: list[str] | None = None
    skip_reserve: bool = False
    enable_topics: bool | None = None
    topics_layout: Literal["list", "tabs"] | None = None
    create_invite_link: bool | None = None
    folder_name: str | None = None
    folder_id: int | None = None
    skip_folder: bool = False

    @property
    def effective_external_ref(self) -> int | str | None:
        return self.external_ref if self.external_ref is not None else self.planfix_task_id


def _backends_or_503(
    request: Request,
) -> tuple[GroupBackend, FolderBackend | None]:
    factory = getattr(request.app.state, "group_backend_factory", None)
    if factory is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Telegram group backend is not configured (session may be unauthorized)",
        )
    backend = factory(request)
    if backend is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Telegram group backend is not available",
        )
    folder_factory = getattr(request.app.state, "folder_backend_factory", None)
    folder_backend = folder_factory(request) if folder_factory is not None else None
    return backend, folder_backend


def _store_or_503(request: Request) -> OperationStore:
    store = getattr(request.app.state, "operation_store", None)
    if store is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Operation store is not configured",
        )
    return store


def build_router() -> APIRouter:
    router = APIRouter(dependencies=[BearerAuth])

    @router.post("/groups")
    async def create(
        body: GroupCreateBody, request: Request
    ) -> dict[str, Any]:
        config = request.app.state.config
        backend, folder_backend = _backends_or_503(request)
        store = _store_or_503(request)

        # Refuse up front when folder placement is requested but the folder
        # backend isn't ready. Without this guard the supergroup is created on
        # Telegram first, then `create_group` raises FolderPeerFailureError,
        # leaving an orphan group behind on every unauthorized request.
        if not body.skip_folder and folder_backend is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=(
                    "Telegram folder backend is not available; "
                    "set skip_folder=true to create the group without folder placement"
                ),
            )

        domain_request = GroupCreateRequest(
            title=body.title,
            external_ref=body.effective_external_ref,
            about=body.about,
            admins=body.admins,
            members=body.members,
            reserve_admins=body.reserve_admins,
            reserve_members=body.reserve_members,
            skip_reserve=body.skip_reserve,
            enable_topics=body.enable_topics,
            topics_layout=body.topics_layout,
            create_invite_link=body.create_invite_link,
            folder_name=body.folder_name,
            folder_id=body.folder_id,
            skip_folder=body.skip_folder,
        )

        try:
            result, op = await create_group(
                backend=backend,
                folder_backend=folder_backend,
                store=store,
                config=config.telegram,
                request=domain_request,
                plugins=request.app.state.plugin_registry,
            )
        except GroupCreatePending as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"error": "pending", "message": str(exc)},
            ) from exc
        except GroupCreateNeedsReview as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail={"error": "needs_review", "message": str(exc)},
            ) from exc
        except GroupCreateFailed as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"error": "previous_attempt_failed", "message": str(exc)},
            ) from exc
        except FolderError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"error": "folder_error", "message": str(exc)},
            ) from exc
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=str(exc),
            ) from exc

        payload = result.to_dict()
        payload["operation_id"] = op.id
        payload["operation_status"] = op.status.value
        return payload

    @router.post("/groups/layout")
    async def set_layout(
        body: LayoutSetBody, request: Request
    ) -> dict[str, Any]:
        backend, _ = _backends_or_503(request)
        store = _store_or_503(request)

        domain_request = LayoutSetRequest(
            telegram_chat_id=body.chat_id,
            layout=body.layout,
        )

        try:
            result, op = await set_topics_layout(
                backend=backend, store=store, request=domain_request
            )
        except GroupLayoutSetPending as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"error": "pending", "message": str(exc)},
            ) from exc
        except GroupLayoutSetNeedsReview as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail={"error": "needs_review", "message": str(exc)},
            ) from exc
        except GroupLayoutSetFailed as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"error": "previous_attempt_failed", "message": str(exc)},
            ) from exc
        except FloodWaitError as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail={"error": "needs_review", "message": str(exc)},
            ) from exc
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=str(exc),
            ) from exc

        payload = result.to_dict()
        payload["operation_id"] = op.id
        payload["operation_status"] = op.status.value
        return payload

    @router.get("/groups/layout")
    async def get_layout(
        request: Request,
        chat_id: int = Query(...),
    ) -> dict[str, Any]:
        backend, _ = _backends_or_503(request)
        try:
            layout = await get_topics_layout(
                backend=backend, telegram_chat_id=chat_id
            )
        except FloodWaitError as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail={"error": "needs_review", "message": str(exc)},
            ) from exc
        return {"chat_id": chat_id, "layout": layout}

    return router
