"""HTTP routes for group membership operations."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field

from telegram_assistant.access import AccessDenied
from telegram_assistant.folders import FolderBackend
from telegram_assistant.http_api.access import (
    build_authorizer,
    resolve_entity_chat_id,
    translate_access_error,
)
from telegram_assistant.http_api.auth import BearerAuth
from telegram_assistant.members import (
    DEFAULT_MEMBER_LIST_LIMIT,
    BulkMemberAddFailed,
    BulkMemberAddNeedsReview,
    BulkMemberAddPending,
    BulkMemberAddRequest,
    BulkMemberItem,
    BulkMemberRemoveFailed,
    BulkMemberRemoveItem,
    BulkMemberRemoveNeedsReview,
    BulkMemberRemovePending,
    BulkMemberRemoveRequest,
    MemberAddBackend,
    MemberListBackend,
    MemberRemoveBackend,
    bulk_add_members,
    bulk_remove_members,
    list_members,
    normalize_user_ref,
    protected_user_set,
)
from telegram_assistant.persistence.store import OperationStore
from telegram_assistant.worker.queue import WorkerQueue


class BulkMemberItemBody(BaseModel):
    user: str = Field(..., min_length=1)
    role: str = Field(default="member")


class BulkMemberAddBody(BaseModel):
    items: list[BulkMemberItemBody] = Field(..., min_length=1)
    continue_on_error: bool = True
    operation_id: str | None = None


class BulkMemberRemoveItemBody(BaseModel):
    user: str = Field(..., min_length=1)


class BulkMemberRemoveBody(BaseModel):
    items: list[BulkMemberRemoveItemBody] = Field(..., min_length=1)
    mode: str = "ban_unban"
    continue_on_error: bool = True
    operation_id: str | None = None
    force: bool = False


def _member_backend_or_503(request: Request) -> MemberAddBackend:
    factory = getattr(request.app.state, "member_backend_factory", None)
    if factory is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Telegram member backend is not configured (session may be unauthorized)",
        )
    backend = factory(request)
    if backend is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Telegram member backend is not available",
        )
    return backend


def _member_list_backend_or_503(request: Request) -> MemberListBackend:
    factory = getattr(request.app.state, "member_list_backend_factory", None)
    if factory is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Telegram member-list backend is not configured (session may be unauthorized)",
        )
    backend = factory(request)
    if backend is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Telegram member-list backend is not available",
        )
    return backend


def _member_remove_backend_or_503(request: Request) -> MemberRemoveBackend:
    """Resolve the remove backend, falling back to the add factory when the
    same Telethon adapter implements both protocols (the production case).
    """
    factory = getattr(request.app.state, "member_remove_backend_factory", None)
    if factory is None:
        factory = getattr(request.app.state, "member_backend_factory", None)
    if factory is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Telegram member backend is not configured (session may be unauthorized)",
        )
    backend = factory(request)
    if backend is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Telegram member backend is not available",
        )
    return backend


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


def _worker_queue_for_request(request: Request) -> WorkerQueue:
    store = _store_or_503(request)
    config = getattr(request.app.state, "config", None)
    if config is None:
        return WorkerQueue(store)
    queue_cfg = getattr(config, "queue", None)
    if queue_cfg is None:
        return WorkerQueue(store)
    return WorkerQueue(
        store,
        max_parallel=getattr(queue_cfg, "max_parallel_telegram_ops", 1),
        flood_wait_safety_margin_seconds=getattr(
            queue_cfg, "flood_wait_safety_margin_seconds", 5.0
        ),
        default_retry_delay_seconds=getattr(
            queue_cfg, "default_retry_delay_seconds", 30.0
        ),
    )


def build_router() -> APIRouter:
    router = APIRouter(dependencies=[BearerAuth])

    @router.get("/members/list")
    async def members_list(
        request: Request,
        chat_id: int | None = None,
        entity: str | None = None,
        limit: int = DEFAULT_MEMBER_LIST_LIMIT,
        query: str | None = None,
        filter: str = "all",
        user: str | None = None,
    ) -> dict[str, Any]:
        """List a chat's participants, or check one user's membership (READ-gated).

        Accepts either a numeric ``chat_id`` or a flexible ``entity`` reference,
        mirroring ``GET /telegram/messages/recent``. ``user`` answers membership
        for one user with a single request and is mutually exclusive with
        ``query``; ``filter`` is one of ``all``, ``admins``, ``bots``.
        """
        if (chat_id is None) == (entity is None):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="provide exactly one of chat_id or entity",
            )

        backend = _member_list_backend_or_503(request)
        if entity is not None:
            resolved_chat_id = await resolve_entity_chat_id(request, entity)
        else:
            resolved_chat_id = chat_id  # type: ignore[assignment]

        authorizer = build_authorizer(
            request, folder_backend=_folder_backend_optional(request)
        )
        try:
            result = await list_members(
                backend=backend,
                chat_id=resolved_chat_id,
                limit=limit,
                query=query,
                filter=filter,
                user=user,
                authorizer=authorizer,
            )
        except AccessDenied as exc:
            raise translate_access_error(exc) from exc
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
            ) from exc

        return {
            "telegram_chat_id": resolved_chat_id,
            "limit": limit,
            "query": query,
            "filter": filter,
            **result.to_dict(),
        }

    @router.post("/groups/{chat_id}/members/bulk-add")
    async def bulk_add(
        chat_id: int, body: BulkMemberAddBody, request: Request
    ) -> dict[str, Any]:
        backend = _member_backend_or_503(request)
        store = _store_or_503(request)
        queue = _worker_queue_for_request(request)

        try:
            domain_items = tuple(
                BulkMemberItem(user=it.user, role=it.role) for it in body.items
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=str(exc),
            ) from exc

        domain_request = BulkMemberAddRequest(
            telegram_chat_id=chat_id,
            items=domain_items,
            continue_on_error=body.continue_on_error,
            operation_id=body.operation_id,
        )

        authorizer = build_authorizer(
            request, folder_backend=_folder_backend_optional(request)
        )
        try:
            result, op = await bulk_add_members(
                backend=backend,
                store=store,
                queue=queue,
                request=domain_request,
                authorizer=authorizer,
            )
        except AccessDenied as exc:
            raise translate_access_error(exc) from exc
        except BulkMemberAddPending as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"error": "pending", "message": str(exc)},
            ) from exc
        except BulkMemberAddNeedsReview as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail={"error": "needs_review", "message": str(exc)},
            ) from exc
        except BulkMemberAddFailed as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"error": "previous_attempt_failed", "message": str(exc)},
            ) from exc
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=str(exc),
            ) from exc

        payload = result.to_dict()
        payload["operation_status"] = op.status.value
        return payload

    @router.post("/groups/{chat_id}/members/bulk-remove")
    async def bulk_remove(
        chat_id: int, body: BulkMemberRemoveBody, request: Request
    ) -> dict[str, Any]:
        backend = _member_remove_backend_or_503(request)
        store = _store_or_503(request)
        queue = _worker_queue_for_request(request)

        # Mirror the CLI's protected-account guard so an authenticated caller
        # can't accidentally (or deliberately) kick the reserve admins or any
        # plugin-protected service account (e.g. the Planfix bot). The caller
        # must pass `force: true` to remove protected accounts.
        if not body.force:
            config = getattr(request.app.state, "config", None)
            if config is not None and getattr(config, "telegram", None) is not None:
                protected = protected_user_set(
                    config=config.telegram,
                    plugins=request.app.state.plugin_registry,
                )
                blocked: list[str] = []
                for it in body.items:
                    try:
                        if normalize_user_ref(it.user).value in protected:
                            blocked.append(it.user)
                    except ValueError:
                        continue
                if blocked:
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail={
                            "error": "protected_accounts",
                            "message": (
                                "refusing to remove protected accounts without "
                                "force=true"
                            ),
                            "users": blocked,
                        },
                    )

        domain_items = tuple(
            BulkMemberRemoveItem(user=it.user) for it in body.items
        )

        try:
            domain_request = BulkMemberRemoveRequest(
                telegram_chat_id=chat_id,
                items=domain_items,
                mode=body.mode,
                continue_on_error=body.continue_on_error,
                operation_id=body.operation_id,
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=str(exc),
            ) from exc

        authorizer = build_authorizer(
            request, folder_backend=_folder_backend_optional(request)
        )
        try:
            result, op = await bulk_remove_members(
                backend=backend,
                store=store,
                queue=queue,
                request=domain_request,
                authorizer=authorizer,
            )
        except AccessDenied as exc:
            raise translate_access_error(exc) from exc
        except BulkMemberRemovePending as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"error": "pending", "message": str(exc)},
            ) from exc
        except BulkMemberRemoveNeedsReview as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail={"error": "needs_review", "message": str(exc)},
            ) from exc
        except BulkMemberRemoveFailed as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"error": "previous_attempt_failed", "message": str(exc)},
            ) from exc
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=str(exc),
            ) from exc

        payload = result.to_dict()
        payload["operation_status"] = op.status.value
        return payload

    return router
