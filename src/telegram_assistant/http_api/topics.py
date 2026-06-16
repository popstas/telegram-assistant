"""HTTP routes for forum topic creation."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field, model_validator

from telegram_assistant.access import AccessDenied, AccessLevel
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
from telegram_assistant.persistence.store import OperationStore
from telegram_assistant.topics import (
    AmbiguousTopicNameError,
    BulkTopicCreateFailed,
    BulkTopicCreateNeedsReview,
    BulkTopicCreatePending,
    BulkTopicCreateRequest,
    BulkTopicItem,
    TopicBackend,
    TopicCloseFailed,
    TopicCloseNeedsReview,
    TopicClosePending,
    TopicCloseRequest,
    TopicCreateFailed,
    TopicCreateNeedsReview,
    TopicCreatePending,
    TopicCreateRequest,
    TopicNotFoundError,
    TopicRenameFailed,
    TopicRenameNeedsReview,
    TopicRenamePending,
    TopicRenameRequest,
    bulk_create_topics,
    close_topic,
    create_topic,
    rename_topic,
    resolve_topic_id_by_name,
)
from telegram_assistant.worker.queue import FloodWaitError, WorkerQueue


def _validate_chat_ref(
    *,
    telegram_chat_id: int | None,
    chat_name: str | None,
    entity: str | int | None,
    folder_name: str | None,
) -> None:
    """Shared chat-reference shape check: exactly one of id / name / entity."""
    refs = sum(
        [telegram_chat_id is not None, chat_name is not None, entity is not None]
    )
    if refs != 1:
        raise ValueError(
            "provide exactly one of telegram_chat_id, chat_name, or entity"
        )
    if chat_name is not None and folder_name is None:
        raise ValueError("chat_name requires folder_name")


class TopicCreateBody(BaseModel):
    topic_name: str = Field(..., min_length=1)
    telegram_chat_id: int | None = None
    chat_name: str | None = None
    entity: str | int | None = None
    folder_name: str | None = None
    folder_id: int | None = None
    # Generic idempotency anchor. ``planfix_task_id`` is a backward-compat alias.
    external_ref: int | str | None = None
    planfix_task_id: int | str | None = None
    message: str | None = None

    @property
    def effective_external_ref(self) -> int | str | None:
        return self.external_ref if self.external_ref is not None else self.planfix_task_id

    @model_validator(mode="after")
    def _exactly_one_chat_ref(self) -> TopicCreateBody:
        _validate_chat_ref(
            telegram_chat_id=self.telegram_chat_id,
            chat_name=self.chat_name,
            entity=self.entity,
            folder_name=self.folder_name,
        )
        return self


class TopicCloseBody(BaseModel):
    telegram_chat_id: int | None = None
    chat_name: str | None = None
    entity: str | int | None = None
    folder_name: str | None = None
    folder_id: int | None = None
    reason: str | None = None

    @model_validator(mode="after")
    def _exactly_one_chat_ref(self) -> TopicCloseBody:
        _validate_chat_ref(
            telegram_chat_id=self.telegram_chat_id,
            chat_name=self.chat_name,
            entity=self.entity,
            folder_name=self.folder_name,
        )
        return self


class TopicRenameBody(BaseModel):
    """Body for the id-path rename: ``POST /topics/{topic_id}/rename``."""

    new_title: str = Field(..., min_length=1)
    telegram_chat_id: int | None = None
    chat_name: str | None = None
    entity: str | int | None = None
    folder_name: str | None = None
    folder_id: int | None = None
    reason: str | None = None

    @model_validator(mode="after")
    def _exactly_one_chat_ref(self) -> TopicRenameBody:
        _validate_chat_ref(
            telegram_chat_id=self.telegram_chat_id,
            chat_name=self.chat_name,
            entity=self.entity,
            folder_name=self.folder_name,
        )
        return self


class TopicRenameByNameBody(TopicRenameBody):
    """Body for the name-resolving rename: ``POST /topics/rename``.

    Adds ``topic_name`` (resolved within the chat via ``list_topics``) on top of
    the same chat-reference + ``new_title`` shape as the id path.
    """

    topic_name: str = Field(..., min_length=1)


class BulkTopicItemBody(BaseModel):
    topic_name: str = Field(..., min_length=1)
    # Generic idempotency anchor. ``planfix_task_id`` is a backward-compat alias.
    external_ref: int | str | None = None
    planfix_task_id: int | str | None = None
    message: str | None = None

    @property
    def effective_external_ref(self) -> int | str | None:
        return self.external_ref if self.external_ref is not None else self.planfix_task_id


class BulkTopicCreateBody(BaseModel):
    items: list[BulkTopicItemBody] = Field(..., min_length=1)
    telegram_chat_id: int | None = None
    chat_name: str | None = None
    entity: str | int | None = None
    folder_name: str | None = None
    folder_id: int | None = None
    continue_on_error: bool = True
    operation_id: str | None = None

    @model_validator(mode="after")
    def _exactly_one_chat_ref(self) -> BulkTopicCreateBody:
        _validate_chat_ref(
            telegram_chat_id=self.telegram_chat_id,
            chat_name=self.chat_name,
            entity=self.entity,
            folder_name=self.folder_name,
        )
        return self


def _topic_backend_or_503(request: Request) -> TopicBackend:
    factory = getattr(request.app.state, "topic_backend_factory", None)
    if factory is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Telegram topic backend is not configured (session may be unauthorized)",
        )
    backend = factory(request)
    if backend is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Telegram topic backend is not available",
        )
    return backend


def _folder_backend_optional(request: Request) -> FolderBackend | None:
    factory = getattr(request.app.state, "folder_backend_factory", None)
    if factory is None:
        return None
    return factory(request)


def _store_or_503(request: Request) -> OperationStore:
    store = getattr(request.app.state, "operation_store", None)
    if store is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Operation store is not configured",
        )
    return store


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


def _translate_topic_resolution_error(exc: Exception) -> HTTPException:
    """Map topic-name resolution errors to HTTP responses.

    ``AmbiguousTopicNameError`` → 409, ``TopicNotFoundError`` → 404 (mirrors the
    entity not-found / ambiguous taxonomy).
    """
    if isinstance(exc, AmbiguousTopicNameError):
        return HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": "ambiguous_topic_name",
                "topic_name": exc.topic_name,
                "telegram_chat_id": exc.telegram_chat_id,
                "matches": exc.matches,
            },
        )
    if isinstance(exc, TopicNotFoundError):
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    return HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)
    )


async def _resolve_chat_id_generic(
    *,
    telegram_chat_id: int | None,
    chat_name: str | None,
    entity: str | int | None = None,
    folder_name: str | None,
    folder_id: int | None,
    request: Request,
) -> int:
    if entity is not None:
        return await resolve_entity_chat_id(request, entity)
    if telegram_chat_id is not None:
        return telegram_chat_id
    folder_backend = _folder_backend_optional(request)
    if folder_backend is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Telegram folder backend is not available for chat_name resolution",
        )
    try:
        chat = await resolve_chat_in_folder(
            folder_backend,
            folder_name=folder_name or "",
            chat_name=chat_name or "",
            folder_id=folder_id,
        )
    except FolderError as exc:
        raise _translate_folder_error(exc) from exc
    return chat.chat_id


async def _enforce_write(request: Request, chat_id: int) -> None:
    """Require WRITE on ``chat_id`` for the active policy, raising 403 on deny."""
    authorizer = build_authorizer(
        request, folder_backend=_folder_backend_optional(request)
    )
    try:
        await authorizer.require(chat_id, AccessLevel.WRITE)
    except AccessDenied as exc:
        raise translate_access_error(exc) from exc


async def _resolve_chat_id(
    body: TopicCreateBody, request: Request
) -> int:
    return await _resolve_chat_id_generic(
        telegram_chat_id=body.telegram_chat_id,
        chat_name=body.chat_name,
        entity=body.entity,
        folder_name=body.folder_name,
        folder_id=body.folder_id,
        request=request,
    )


def _worker_queue_for_request(request: Request) -> WorkerQueue:
    """Return a queue bound to the app's operation store.

    The HTTP layer creates a per-request queue rather than sharing a
    long-lived instance because the queue is essentially a thin wrapper
    around the store + a semaphore — the operations themselves are async and
    the semaphore default of 1 keeps bulk Telegram calls serialized.
    """
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

    @router.post("/topics")
    async def create(body: TopicCreateBody, request: Request) -> dict[str, Any]:
        backend = _topic_backend_or_503(request)
        store = _store_or_503(request)
        chat_id = await _resolve_chat_id(body, request)
        await _enforce_write(request, chat_id)

        domain_request = TopicCreateRequest(
            telegram_chat_id=chat_id,
            topic_name=body.topic_name,
            external_ref=body.effective_external_ref,
            message=body.message,
        )

        try:
            result, op = await create_topic(
                backend=backend,
                store=store,
                request=domain_request,
                plugins=request.app.state.plugin_registry,
            )
        except TopicCreatePending as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"error": "pending", "message": str(exc)},
            ) from exc
        except TopicCreateNeedsReview as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail={"error": "needs_review", "message": str(exc)},
            ) from exc
        except TopicCreateFailed as exc:
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
        payload["operation_id"] = op.id
        payload["operation_status"] = op.status.value
        return payload

    @router.post("/topics/bulk-create")
    async def bulk_create(
        body: BulkTopicCreateBody, request: Request
    ) -> dict[str, Any]:
        backend = _topic_backend_or_503(request)
        store = _store_or_503(request)
        chat_id = await _resolve_chat_id_generic(
            telegram_chat_id=body.telegram_chat_id,
            chat_name=body.chat_name,
            entity=body.entity,
            folder_name=body.folder_name,
            folder_id=body.folder_id,
            request=request,
        )
        await _enforce_write(request, chat_id)
        queue = _worker_queue_for_request(request)

        domain_request = BulkTopicCreateRequest(
            telegram_chat_id=chat_id,
            items=tuple(
                BulkTopicItem(
                    topic_name=it.topic_name,
                    external_ref=it.effective_external_ref,
                    message=it.message,
                )
                for it in body.items
            ),
            continue_on_error=body.continue_on_error,
            operation_id=body.operation_id,
        )

        try:
            result, op = await bulk_create_topics(
                backend=backend,
                store=store,
                queue=queue,
                request=domain_request,
                plugins=request.app.state.plugin_registry,
            )
        except BulkTopicCreatePending as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"error": "pending", "message": str(exc)},
            ) from exc
        except BulkTopicCreateNeedsReview as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail={"error": "needs_review", "message": str(exc)},
            ) from exc
        except BulkTopicCreateFailed as exc:
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

    @router.post("/topics/{topic_id}/close")
    async def close(
        topic_id: int, body: TopicCloseBody, request: Request
    ) -> dict[str, Any]:
        if topic_id <= 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="topic_id must be a positive integer",
            )
        backend = _topic_backend_or_503(request)
        store = _store_or_503(request)
        chat_id = await _resolve_chat_id_generic(
            telegram_chat_id=body.telegram_chat_id,
            chat_name=body.chat_name,
            entity=body.entity,
            folder_name=body.folder_name,
            folder_id=body.folder_id,
            request=request,
        )
        await _enforce_write(request, chat_id)

        domain_request = TopicCloseRequest(
            telegram_chat_id=chat_id,
            telegram_topic_id=topic_id,
            reason=body.reason,
        )

        try:
            result, op = await close_topic(
                backend=backend, store=store, request=domain_request
            )
        except TopicClosePending as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"error": "pending", "message": str(exc)},
            ) from exc
        except TopicCloseNeedsReview as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail={"error": "needs_review", "message": str(exc)},
            ) from exc
        except TopicCloseFailed as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"error": "previous_attempt_failed", "message": str(exc)},
            ) from exc
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
            ) from exc

        payload = result.to_dict()
        payload["operation_id"] = op.id
        payload["operation_status"] = op.status.value
        return payload

    async def _execute_rename(
        *,
        request: Request,
        backend: TopicBackend,
        store: OperationStore,
        chat_id: int,
        topic_id: int,
        new_title: str,
        reason: str | None,
    ) -> dict[str, Any]:
        domain_request = TopicRenameRequest(
            telegram_chat_id=chat_id,
            telegram_topic_id=topic_id,
            new_title=new_title,
            reason=reason,
        )
        try:
            result, op = await rename_topic(
                backend=backend, store=store, request=domain_request
            )
        except TopicRenamePending as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"error": "pending", "message": str(exc)},
            ) from exc
        except TopicRenameNeedsReview as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail={"error": "needs_review", "message": str(exc)},
            ) from exc
        except TopicRenameFailed as exc:
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
                status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
            ) from exc

        payload = result.to_dict()
        payload["operation_id"] = op.id
        payload["operation_status"] = op.status.value
        return payload

    @router.post("/topics/{topic_id}/rename")
    async def rename_by_id(
        topic_id: int, body: TopicRenameBody, request: Request
    ) -> dict[str, Any]:
        # Reuse the existing topic backend factory: rename is a thin WRITE op on
        # the topic, so no dedicated rename backend is warranted.
        if topic_id <= 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="topic_id must be a positive integer",
            )
        backend = _topic_backend_or_503(request)
        store = _store_or_503(request)
        chat_id = await _resolve_chat_id_generic(
            telegram_chat_id=body.telegram_chat_id,
            chat_name=body.chat_name,
            entity=body.entity,
            folder_name=body.folder_name,
            folder_id=body.folder_id,
            request=request,
        )
        await _enforce_write(request, chat_id)
        return await _execute_rename(
            request=request,
            backend=backend,
            store=store,
            chat_id=chat_id,
            topic_id=topic_id,
            new_title=body.new_title,
            reason=body.reason,
        )

    @router.post("/topics/rename")
    async def rename_by_name(
        body: TopicRenameByNameBody, request: Request
    ) -> dict[str, Any]:
        backend = _topic_backend_or_503(request)
        store = _store_or_503(request)
        chat_id = await _resolve_chat_id_generic(
            telegram_chat_id=body.telegram_chat_id,
            chat_name=body.chat_name,
            entity=body.entity,
            folder_name=body.folder_name,
            folder_id=body.folder_id,
            request=request,
        )
        await _enforce_write(request, chat_id)

        try:
            topic_id = await resolve_topic_id_by_name(
                backend=backend,
                telegram_chat_id=chat_id,
                topic_name=body.topic_name,
            )
        except (AmbiguousTopicNameError, TopicNotFoundError) as exc:
            raise _translate_topic_resolution_error(exc) from exc
        except FloodWaitError as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail={"error": "needs_review", "message": str(exc)},
            ) from exc
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
            ) from exc

        return await _execute_rename(
            request=request,
            backend=backend,
            store=store,
            chat_id=chat_id,
            topic_id=topic_id,
            new_title=body.new_title,
            reason=body.reason,
        )

    return router
