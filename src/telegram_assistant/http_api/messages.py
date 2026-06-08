"""HTTP routes for sending messages and service commands."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, model_validator

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
    delete_only_session_messages,
    resolve_entity_chat_id,
    sent_message_registry,
    translate_access_error,
)
from telegram_assistant.http_api.auth import BearerAuth
from telegram_assistant.messages import (
    AttachmentError,
    DeleteBackend,
    DeleteMessagesRequest,
    ForwardBackend,
    ForwardMessagesRequest,
    MassSendRequest,
    MessageBackend,
    MessageDeleteForbidden,
    MessageReadBackend,
    MessageSendFailed,
    MessageSendNeedsReview,
    MessageSendPending,
    ReactionBackend,
    ScheduleError,
    SendMessageRequest,
    SendReactionRequest,
    delete_messages,
    forward_messages,
    get_recent_messages,
    make_url_downloader,
    mass_send_message,
    resolve_schedule_at,
    send_message,
    set_message_reaction,
    validate_file_urls,
)
from telegram_assistant.persistence.store import OperationStore
from telegram_assistant.topics import (
    AmbiguousTopicNameError,
    TopicBackend,
    TopicNotFoundError,
    resolve_topic_id_by_name,
)
from telegram_assistant.worker.queue import FloodWaitError


def _translate_flood_wait(exc: FloodWaitError) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_502_BAD_GATEWAY,
        detail={"error": "needs_review", "message": str(exc)},
    )


class MessageSendBody(BaseModel):
    # ``text`` doubles as the caption and may be empty when attachments are
    # present (media-only send), so it is optional with an empty-string default.
    text: str = ""
    telegram_chat_id: int | None = None
    chat_name: str | None = None
    # Flexible entity reference (numeric id with/without -100, @username, t.me /
    # invite link, phone, or exact title) resolved via the shared resolver.
    entity: str | int | None = None
    folder_name: str | None = None
    folder_id: int | None = None
    telegram_topic_id: int | None = None
    topic_name: str | None = None
    operation_id: str | None = None
    # Attachments: HTTP accepts remote ``file_urls``. ``files`` is kept in the
    # model only to return a clear 400 for old callers; server-local paths are
    # not uploaded over HTTP.
    files: list[str] | None = None
    file_urls: list[str] | None = None
    schedule_at: datetime | None = None
    delay_seconds: int | None = None
    # Thread the send as a reply to an existing message id (targeted only).
    reply_to_message_id: int | None = None

    @model_validator(mode="after")
    def _shape(self) -> MessageSendBody:
        has_chat_id = self.telegram_chat_id is not None
        has_chat_name = self.chat_name is not None
        has_entity = self.entity is not None
        has_folder = self.folder_name is not None
        has_topic_name = self.topic_name is not None
        has_topic_id = self.telegram_topic_id is not None

        has_attachments = bool(self.files or self.file_urls)
        if not (self.text and self.text.strip()) and not has_attachments:
            raise ValueError(
                "must provide non-empty text or at least one files/file_urls "
                "attachment"
            )
        if self.schedule_at is not None and self.delay_seconds is not None:
            raise ValueError("provide only one of schedule_at or delay_seconds")
        if self.reply_to_message_id is not None and self.reply_to_message_id <= 0:
            raise ValueError("reply_to_message_id must be a positive integer")

        # ``entity`` is a direct chat reference, equivalent to telegram_chat_id
        # for shape purposes (no folder lookup needed).
        targeted_refs = sum([has_chat_id, has_chat_name, has_entity])

        # Valid call shapes:
        # 1. Targeted with explicit chat id   — telegram_chat_id, no chat_name/entity
        # 2. Targeted by entity reference     — entity, no chat_id/chat_name
        # 3. Targeted by chat name            — chat_name + folder_name
        # 4. Mass mode (folder + topic_name)  — folder + topic_name + no targeted ref
        if targeted_refs == 0:
            # Must be mass mode.
            if not (has_folder and has_topic_name):
                raise ValueError(
                    "must provide telegram_chat_id, entity, chat_name+folder_name, "
                    "or folder_name+topic_name (mass mode)"
                )
            if has_topic_id:
                raise ValueError(
                    "telegram_topic_id is not valid in mass mode; "
                    "use topic_name to resolve per chat"
                )
            if (
                has_attachments
                or self.schedule_at is not None
                or self.delay_seconds is not None
                or self.reply_to_message_id is not None
            ):
                raise ValueError(
                    "files/file_urls/schedule_at/delay_seconds/reply_to_message_id "
                    "are only supported for targeted sends, not mass mode"
                )
            return self

        if targeted_refs > 1:
            raise ValueError(
                "provide exactly one of telegram_chat_id, entity, or chat_name"
            )
        if has_chat_name and not has_folder:
            raise ValueError("chat_name requires folder_name")
        return self


class ReactionBody(BaseModel):
    """Set or clear an emoji reaction on one message in a target chat.

    The target is one of ``telegram_chat_id`` / ``entity`` / ``chat_name`` +
    ``folder_name`` (same shape as a targeted send). Provide exactly one of a
    non-empty ``emoji`` or ``clear=true``.
    """

    message_id: int
    emoji: str | None = None
    clear: bool = False
    telegram_chat_id: int | None = None
    entity: str | int | None = None
    chat_name: str | None = None
    folder_name: str | None = None
    folder_id: int | None = None

    @model_validator(mode="after")
    def _shape(self) -> ReactionBody:
        if self.message_id <= 0:
            raise ValueError("message_id must be a positive integer")
        has_emoji = bool(self.emoji and self.emoji.strip())
        if has_emoji and self.clear:
            raise ValueError("provide either emoji or clear, not both")
        if not has_emoji and not self.clear:
            raise ValueError("provide either an emoji to set or clear=true")
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


class ForwardBody(BaseModel):
    """Forward one or more messages from a source chat into a target chat.

    The source is one of ``from_chat_id`` / ``from_entity``; the target is one
    of ``to_chat_id`` / ``to_entity``. ``message_ids`` must hold at least one
    positive id. Forwarding READ-gates the source and WRITE-gates the target.
    """

    message_ids: list[int]
    from_chat_id: int | None = None
    from_entity: str | int | None = None
    to_chat_id: int | None = None
    to_entity: str | int | None = None

    @model_validator(mode="after")
    def _shape(self) -> ForwardBody:
        if not self.message_ids:
            raise ValueError("message_ids must contain at least one id")
        if any(mid <= 0 for mid in self.message_ids):
            raise ValueError("every message_id must be a positive integer")
        source_refs = sum(
            [self.from_chat_id is not None, self.from_entity is not None]
        )
        if source_refs != 1:
            raise ValueError(
                "provide exactly one of from_chat_id or from_entity"
            )
        target_refs = sum(
            [self.to_chat_id is not None, self.to_entity is not None]
        )
        if target_refs != 1:
            raise ValueError("provide exactly one of to_chat_id or to_entity")
        return self


class DeleteBody(BaseModel):
    """Delete one or more messages from a target chat (DELETE-gated).

    The target is one of ``telegram_chat_id`` / ``entity`` / ``chat_name`` +
    ``folder_name`` (same shape as a targeted send). ``message_ids`` must hold at
    least one positive id. ``revoke`` defaults to ``True`` (delete for everyone).
    ``dry_run`` resolves + authorizes (and runs the session-limit check) without
    deleting; ``force`` is carried for surface consistency with the project's
    ``--force`` convention.
    """

    message_ids: list[int]
    revoke: bool = True
    dry_run: bool = False
    force: bool = False
    telegram_chat_id: int | None = None
    entity: str | int | None = None
    chat_name: str | None = None
    folder_name: str | None = None
    folder_id: int | None = None

    @model_validator(mode="after")
    def _shape(self) -> DeleteBody:
        if not self.message_ids:
            raise ValueError("message_ids must contain at least one id")
        if any(mid <= 0 for mid in self.message_ids):
            raise ValueError("every message_id must be a positive integer")
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


def _message_backend_or_503(request: Request) -> MessageBackend:
    factory = getattr(request.app.state, "message_backend_factory", None)
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


def _reaction_backend_or_503(request: Request) -> ReactionBackend:
    factory = getattr(request.app.state, "reaction_backend_factory", None)
    if factory is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Telegram reaction backend is not configured (session may be unauthorized)",
        )
    backend = factory(request)
    if backend is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Telegram reaction backend is not available",
        )
    return backend


def _forward_backend_or_503(request: Request) -> ForwardBackend:
    factory = getattr(request.app.state, "forward_backend_factory", None)
    if factory is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Telegram forward backend is not configured (session may be unauthorized)",
        )
    backend = factory(request)
    if backend is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Telegram forward backend is not available",
        )
    return backend


def _delete_backend_or_503(request: Request) -> DeleteBackend:
    factory = getattr(request.app.state, "delete_backend_factory", None)
    if factory is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Telegram delete backend is not configured (session may be unauthorized)",
        )
    backend = factory(request)
    if backend is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Telegram delete backend is not available",
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


def _folder_backend_optional(request: Request) -> FolderBackend | None:
    factory = getattr(request.app.state, "folder_backend_factory", None)
    if factory is None:
        return None
    return factory(request)


def _read_backend_or_503(request: Request) -> MessageReadBackend:
    factory = getattr(request.app.state, "message_read_backend_factory", None)
    if factory is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Telegram message-read backend is not configured (session may be unauthorized)",
        )
    backend = factory(request)
    if backend is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Telegram message-read backend is not available",
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
            and body.entity is None
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
            authorizer = build_authorizer(request, folder_backend=folder_backend)
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
                    authorizer=authorizer,
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
        if body.entity is not None:
            telegram_chat_id = await resolve_entity_chat_id(request, body.entity)
            chat_name_for_log: str | None = None
        elif body.telegram_chat_id is not None:
            telegram_chat_id = body.telegram_chat_id
            chat_name_for_log = None
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

        try:
            resolved_schedule_at = resolve_schedule_at(
                schedule_at=body.schedule_at,
                delay_seconds=body.delay_seconds,
            )
        except ScheduleError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
            ) from exc

        authorizer = build_authorizer(
            request, folder_backend=_folder_backend_optional(request)
        )
        files = tuple(body.files or ())
        if files:
            try:
                await authorizer.require(telegram_chat_id, AccessLevel.WRITE)
            except AccessDenied as exc:
                raise translate_access_error(exc) from exc
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    "HTTP server-local files are not supported; use file_urls "
                    "with http(s) URLs"
                ),
            )

        file_urls = tuple(body.file_urls or ())
        try:
            validate_file_urls(file_urls)
        except AttachmentError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
            ) from exc

        domain_request = SendMessageRequest(
            telegram_chat_id=telegram_chat_id,
            text=body.text,
            telegram_topic_id=telegram_topic_id,
            operation_id=body.operation_id,
            chat_name=chat_name_for_log,
            topic_name=topic_name_for_log,
            files=files,
            file_urls=file_urls,
            schedule_at=resolved_schedule_at,
            reply_to_message_id=body.reply_to_message_id,
        )

        try:
            result, op = await send_message(
                backend=backend,
                store=store,
                request=domain_request,
                authorizer=authorizer,
                sent_registry=sent_message_registry(request),
                downloader=(
                    make_url_downloader(
                        fetcher=getattr(
                            request.app.state, "attachment_fetcher", None
                        )
                    )
                    if file_urls
                    else None
                ),
            )
        except AccessDenied as exc:
            raise translate_access_error(exc) from exc
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

    @router.post("/messages/reactions")
    async def react(body: ReactionBody, request: Request) -> dict[str, Any]:
        """Set or clear an emoji reaction on a message (WRITE-gated)."""
        backend = _reaction_backend_or_503(request)

        if body.entity is not None:
            telegram_chat_id = await resolve_entity_chat_id(request, body.entity)
            chat_name_for_log: str | None = None
        elif body.telegram_chat_id is not None:
            telegram_chat_id = body.telegram_chat_id
            chat_name_for_log = None
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

        authorizer = build_authorizer(
            request, folder_backend=_folder_backend_optional(request)
        )
        try:
            result = await set_message_reaction(
                backend,
                request=SendReactionRequest(
                    telegram_chat_id=telegram_chat_id,
                    message_id=body.message_id,
                    emoji=body.emoji,
                    clear=body.clear,
                    chat_name=chat_name_for_log,
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

    @router.post("/messages/forward")
    async def forward(body: ForwardBody, request: Request) -> dict[str, Any]:
        """Forward messages from a source chat to a target chat.

        READ-gated on the source, WRITE-gated on the target.
        """
        backend = _forward_backend_or_503(request)

        if body.from_entity is not None:
            from_chat_id = await resolve_entity_chat_id(request, body.from_entity)
        else:
            from_chat_id = body.from_chat_id  # type: ignore[assignment]
        if body.to_entity is not None:
            to_chat_id = await resolve_entity_chat_id(request, body.to_entity)
        else:
            to_chat_id = body.to_chat_id  # type: ignore[assignment]

        authorizer = build_authorizer(
            request, folder_backend=_folder_backend_optional(request)
        )
        try:
            result = await forward_messages(
                backend,
                request=ForwardMessagesRequest(
                    from_chat_id=from_chat_id,
                    to_chat_id=to_chat_id,
                    message_ids=tuple(body.message_ids),
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

    @router.post("/messages/delete")
    async def delete(body: DeleteBody, request: Request) -> dict[str, Any]:
        """Delete messages from a chat (DELETE-gated).

        Honors ``telegram.access.delete_only_session_messages`` (default true):
        when active, only messages this server process sent can be deleted.
        """
        backend = _delete_backend_or_503(request)

        if body.entity is not None:
            telegram_chat_id = await resolve_entity_chat_id(request, body.entity)
            chat_name_for_log: str | None = None
        elif body.telegram_chat_id is not None:
            telegram_chat_id = body.telegram_chat_id
            chat_name_for_log = None
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

        authorizer = build_authorizer(
            request, folder_backend=_folder_backend_optional(request)
        )
        try:
            result = await delete_messages(
                backend,
                request=DeleteMessagesRequest(
                    telegram_chat_id=telegram_chat_id,
                    message_ids=tuple(body.message_ids),
                    revoke=body.revoke,
                    dry_run=body.dry_run,
                    force=body.force,
                    chat_name=chat_name_for_log,
                ),
                authorizer=authorizer,
                sent_registry=sent_message_registry(request),
                only_session_messages=delete_only_session_messages(request),
            )
        except AccessDenied as exc:
            raise translate_access_error(exc) from exc
        except MessageDeleteForbidden as exc:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={
                    "error": "delete_forbidden",
                    "message": str(exc),
                    "message_ids": exc.message_ids,
                },
            ) from exc
        except FloodWaitError as exc:
            raise _translate_flood_wait(exc) from exc
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
            ) from exc

        return result.to_dict()

    @router.get("/messages/recent")
    async def recent(
        request: Request,
        chat_id: int | None = None,
        entity: str | None = None,
        limit: int = 5,
        minutes: int | None = None,
    ) -> dict[str, Any]:
        """Return up to ``limit`` recent messages (READ-gated).

        Accepts either a numeric ``chat_id`` or a flexible ``entity`` reference
        (resolved via the shared resolver). The op requires READ on the resolved
        chat; an unpermitted chat returns 403. ``minutes`` optionally restricts
        the result to messages newer than ``now - minutes`` (composed with
        ``limit``).
        """
        if (chat_id is None) == (entity is None):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="provide exactly one of chat_id or entity",
            )
        if limit <= 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="limit must be a positive integer",
            )
        if minutes is not None and minutes <= 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="minutes must be a positive integer",
            )

        backend = _read_backend_or_503(request)
        if entity is not None:
            resolved_chat_id = await resolve_entity_chat_id(request, entity)
        else:
            resolved_chat_id = chat_id  # type: ignore[assignment]

        authorizer = build_authorizer(
            request, folder_backend=_folder_backend_optional(request)
        )
        try:
            messages = await get_recent_messages(
                backend=backend,
                chat_id=resolved_chat_id,
                limit=limit,
                minutes=minutes,
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
            "minutes": minutes,
            "count": len(messages),
            "messages": [m.to_dict() for m in messages],
        }

    return router
