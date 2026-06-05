"""HTTP routes for sending messages and service commands."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field, model_validator

from telegram_assistant.folders import (
    AmbiguousChatNameError,
    ChatNotFoundError,
    FolderBackend,
    FolderError,
    FolderIdMismatchError,
    FolderNotFoundError,
    resolve_chat_in_folder,
)
from telegram_assistant.http_api.auth import BearerAuth
from telegram_assistant.messages import (
    MassSendRequest,
    MessageBackend,
    MessageSendFailed,
    MessageSendNeedsReview,
    MessageSendPending,
    SendMessageRequest,
    mass_send_message,
    send_message,
)
from telegram_assistant.persistence.store import OperationStore
from telegram_assistant.topics import (
    AmbiguousTopicNameError,
    TopicBackend,
    TopicNotFoundError,
    resolve_topic_id_by_name,
)


class MessageSendBody(BaseModel):
    text: str = Field(..., min_length=1)
    telegram_chat_id: int | None = None
    chat_name: str | None = None
    folder_name: str | None = None
    folder_id: int | None = None
    telegram_topic_id: int | None = None
    topic_name: str | None = None
    operation_id: str | None = None

    @model_validator(mode="after")
    def _shape(self) -> MessageSendBody:
        has_chat_id = self.telegram_chat_id is not None
        has_chat_name = self.chat_name is not None
        has_folder = self.folder_name is not None
        has_topic_name = self.topic_name is not None
        has_topic_id = self.telegram_topic_id is not None

        # Three valid call shapes:
        # 1. Targeted with explicit chat id           — has_chat_id, no chat_name
        # 2. Targeted by chat name                    — has_chat_name + has_folder
        # 3. Mass mode (folder + topic_name)          — has_folder + has_topic_name + no chat_id/chat_name
        if not has_chat_id and not has_chat_name:
            # Must be mass mode.
            if not (has_folder and has_topic_name):
                raise ValueError(
                    "must provide telegram_chat_id, chat_name+folder_name, "
                    "or folder_name+topic_name (mass mode)"
                )
            if has_topic_id:
                raise ValueError(
                    "telegram_topic_id is not valid in mass mode; "
                    "use topic_name to resolve per chat"
                )
            return self

        if has_chat_id and has_chat_name:
            raise ValueError(
                "provide exactly one of telegram_chat_id or chat_name"
            )
        if has_chat_name and not has_folder:
            raise ValueError("chat_name requires folder_name")
        return self


def _message_backend_or_503(request: Request) -> MessageBackend:
    factory = getattr(request.app.state, "message_backend_factory", None)
    if factory is None:
        # Fall back to the topic backend factory — the Telethon adapter for
        # topics already implements ``send_message`` with the same signature.
        factory = getattr(request.app.state, "topic_backend_factory", None)
    if factory is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Telegram message backend is not configured (session may be unauthorized)",
        )
    backend = factory(request)
    if backend is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Telegram message backend is not available",
        )
    return backend


def _topic_backend_optional(request: Request) -> TopicBackend | None:
    factory = getattr(request.app.state, "topic_backend_factory", None)
    if factory is None:
        return None
    return factory(request)


def _folder_backend_or_503(request: Request) -> FolderBackend:
    factory = getattr(request.app.state, "folder_backend_factory", None)
    if factory is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Telegram folder backend is not configured",
        )
    backend = factory(request)
    if backend is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Telegram folder backend is not available",
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


def build_router() -> APIRouter:
    router = APIRouter(dependencies=[BearerAuth])

    @router.post("/messages")
    async def send(body: MessageSendBody, request: Request) -> dict[str, Any]:
        backend = _message_backend_or_503(request)
        store = _store_or_503(request)

        is_mass = (
            body.telegram_chat_id is None
            and body.chat_name is None
            and body.folder_name is not None
            and body.topic_name is not None
        )

        if is_mass:
            topic_backend = _topic_backend_optional(request)
            if topic_backend is None:
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="Telegram topic backend is not available for mass send",
                )
            folder_backend = _folder_backend_or_503(request)
            try:
                result = await mass_send_message(
                    message_backend=backend,
                    topic_backend=topic_backend,
                    folder_backend=folder_backend,
                    store=store,
                    request=MassSendRequest(
                        folder_name=body.folder_name or "",
                        topic_name=body.topic_name or "",
                        text=body.text,
                        folder_id=body.folder_id,
                        operation_id=body.operation_id,
                    ),
                )
            except FolderError as exc:
                raise _translate_folder_error(exc) from exc
            except ValueError as exc:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=str(exc),
                ) from exc

            payload = result.to_dict()
            payload["mode"] = "mass"
            return payload

        # Targeted send: resolve chat_id (and topic_id if topic_name given).
        if body.telegram_chat_id is not None:
            telegram_chat_id = body.telegram_chat_id
            chat_name_for_log: str | None = None
        else:
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
            telegram_chat_id = chat.chat_id
            chat_name_for_log = chat.title

        telegram_topic_id = body.telegram_topic_id
        topic_name_for_log: str | None = None
        if telegram_topic_id is None and body.topic_name is not None:
            topic_backend = _topic_backend_optional(request)
            if topic_backend is None:
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="Telegram topic backend is not available for topic_name resolution",
                )
            try:
                telegram_topic_id = await resolve_topic_id_by_name(
                    backend=topic_backend,
                    telegram_chat_id=telegram_chat_id,
                    topic_name=body.topic_name,
                )
            except AmbiguousTopicNameError as exc:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail={
                        "error": "ambiguous_topic_name",
                        "topic_name": exc.topic_name,
                        "telegram_chat_id": exc.telegram_chat_id,
                        "matches": exc.matches,
                    },
                ) from exc
            except TopicNotFoundError as exc:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
                ) from exc
            topic_name_for_log = body.topic_name

        domain_request = SendMessageRequest(
            telegram_chat_id=telegram_chat_id,
            text=body.text,
            telegram_topic_id=telegram_topic_id,
            operation_id=body.operation_id,
            chat_name=chat_name_for_log,
            topic_name=topic_name_for_log,
        )

        try:
            result, op = await send_message(
                backend=backend, store=store, request=domain_request
            )
        except MessageSendPending as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"error": "pending", "message": str(exc)},
            ) from exc
        except MessageSendNeedsReview as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail={"error": "needs_review", "message": str(exc)},
            ) from exc
        except MessageSendFailed as exc:
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
        payload["mode"] = "targeted"
        return payload

    return router
