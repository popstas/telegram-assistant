"""HTTP routes for supergroup creation."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request, status
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
from telegram_assistant.groups import (
    ContactSpec,
    GroupBackend,
    GroupCreateFailed,
    GroupCreateNeedsReview,
    GroupCreatePending,
    GroupCreateRequest,
    GroupLayoutSetFailed,
    GroupLayoutSetNeedsReview,
    GroupLayoutSetPending,
    GroupRenameFailed,
    GroupRenameNeedsReview,
    GroupRenamePending,
    GroupRenameRequest,
    LayoutSetRequest,
    create_group,
    get_topics_layout,
    rename_group,
    set_topics_layout,
)
from telegram_assistant.http_api.access import (
    build_authorizer,
    resolve_entity_chat_id,
    translate_access_error,
)
from telegram_assistant.http_api.auth import BearerAuth
from telegram_assistant.persistence.store import OperationStore
from telegram_assistant.worker.queue import FloodWaitError


def _first_or_none(value: str | list[str] | None) -> str | None:
    """Collapse a string-or-list request value to its first non-blank element."""
    if isinstance(value, list):
        value = next((item for item in value if str(item).strip()), None)
    if value is None:
        return None
    text = str(value).strip()
    return text or None


class LayoutSetBody(BaseModel):
    chat_id: int
    layout: Literal["list", "tabs"]


class GroupRenameBody(BaseModel):
    new_title: str = Field(..., min_length=1)
    chat_id: int | None = None
    chat_name: str | None = None
    entity: str | int | None = None
    folder_name: str | None = None
    folder_id: int | None = None
    reason: str | None = None

    @model_validator(mode="after")
    def _exactly_one_chat_ref(self) -> GroupRenameBody:
        refs = sum(
            [
                self.chat_id is not None,
                self.chat_name is not None,
                self.entity is not None,
            ]
        )
        if refs != 1:
            raise ValueError(
                "provide exactly one of chat_id, chat_name, or entity"
            )
        if self.chat_name is not None and self.folder_name is None:
            raise ValueError("chat_name requires folder_name")
        return self


class ContactBody(BaseModel):
    phone: str = Field(..., min_length=1)
    name: str = Field(..., min_length=1)


class GroupCreateBody(BaseModel):
    title: str = Field(..., min_length=1)
    # Generic idempotency anchor. ``planfix_task_id`` is a backward-compat alias.
    external_ref: int | str | None = None
    planfix_task_id: int | str | None = None
    about: str | None = None
    admins: list[str] = Field(default_factory=list)
    members: list[str] = Field(default_factory=list)
    managers: list[str] = Field(default_factory=list)
    contacts: list[ContactBody] = Field(default_factory=list)
    reserve_admins: list[str] | None = None
    reserve_members: list[str] | None = None
    skip_reserve: bool = False
    enable_topics: bool | None = None
    topics_layout: Literal["list", "tabs"] | None = None
    create_invite_link: bool | None = None
    folder_name: str | None = None
    folder_id: int | None = None
    skip_folder: bool = False
    # ``lang`` selects the language of the localized ``answer`` response string.
    # ``telegram_id``, when filled, adds a phone-style client by numeric id.
    # Both may arrive as a bare value or a list of strings (first element wins),
    # mirroring the google-drive-access ``_apply_lang`` shape.
    lang: str | list[str] | None = None
    telegram_id: int | str | list[str] | None = None

    @property
    def effective_external_ref(self) -> int | str | None:
        return self.external_ref if self.external_ref is not None else self.planfix_task_id

    @property
    def effective_lang(self) -> str | None:
        return _first_or_none(self.lang)

    @property
    def effective_telegram_id(self) -> str | None:
        value = self.telegram_id
        if isinstance(value, list):
            value = next(
                (item for item in value if str(item).strip()), None
            )
        if value is None:
            return None
        text = str(value).strip()
        return text or None


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


def _folder_backend_optional(request: Request) -> FolderBackend | None:
    factory = getattr(request.app.state, "folder_backend_factory", None)
    if factory is None:
        return None
    return factory(request)


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


async def _resolve_rename_chat_id(body: GroupRenameBody, request: Request) -> int:
    """Resolve the rename body's chat reference to a numeric chat id.

    Mirrors the topic close/rename resolution order: ``entity`` via the shared
    resolver, then ``chat_id`` verbatim, then ``chat_name`` within a folder.
    """
    if body.entity is not None:
        return await resolve_entity_chat_id(request, body.entity)
    if body.chat_id is not None:
        return body.chat_id
    folder_backend = _folder_backend_optional(request)
    if folder_backend is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Telegram folder backend is not available for chat_name resolution",
        )
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
            managers=body.managers,
            contacts=[
                ContactSpec(phone=c.phone, name=c.name) for c in body.contacts
            ],
            reserve_admins=body.reserve_admins,
            reserve_members=body.reserve_members,
            skip_reserve=body.skip_reserve,
            enable_topics=body.enable_topics,
            topics_layout=body.topics_layout,
            create_invite_link=body.create_invite_link,
            folder_name=body.folder_name,
            folder_id=body.folder_id,
            skip_folder=body.skip_folder,
            lang=body.effective_lang,
            telegram_id=body.effective_telegram_id,
        )

        authorizer = build_authorizer(request, folder_backend=folder_backend)
        try:
            result, op = await create_group(
                backend=backend,
                folder_backend=folder_backend,
                store=store,
                config=config.telegram,
                request=domain_request,
                plugins=request.app.state.plugin_registry,
                authorizer=authorizer,
            )
        except AccessDenied as exc:
            raise translate_access_error(exc) from exc
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

    @router.post("/groups/rename")
    async def rename(body: GroupRenameBody, request: Request) -> dict[str, Any]:
        # Reuse the existing group backend factory: rename is a thin WRITE op on
        # the supergroup, so no dedicated rename backend is warranted.
        backend, folder_backend = _backends_or_503(request)
        store = _store_or_503(request)
        chat_id = await _resolve_rename_chat_id(body, request)

        domain_request = GroupRenameRequest(
            telegram_chat_id=chat_id,
            new_title=body.new_title,
            reason=body.reason,
        )

        authorizer = build_authorizer(request, folder_backend=folder_backend)
        try:
            result, op = await rename_group(
                backend=backend,
                store=store,
                request=domain_request,
                authorizer=authorizer,
            )
        except AccessDenied as exc:
            raise translate_access_error(exc) from exc
        except GroupRenamePending as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"error": "pending", "message": str(exc)},
            ) from exc
        except GroupRenameNeedsReview as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail={"error": "needs_review", "message": str(exc)},
            ) from exc
        except GroupRenameFailed as exc:
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

    return router
