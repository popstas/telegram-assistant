"""Telegram operation tools for the optional FastMCP surface."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime
from types import SimpleNamespace
from typing import Any, Literal, NoReturn

from fastapi import HTTPException, status
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import ValidationError

from telegram_assistant import __version__
from telegram_assistant.access import AccessDenied, AccessLevel
from telegram_assistant.entities import AmbiguousEntityError, EntityNotFoundError
from telegram_assistant.folders import (
    AmbiguousChatNameError,
    ChatNotFoundError,
    FolderError,
    FolderIdMismatchError,
    FolderNotFoundError,
    FolderPeerFailureError,
    add_chat_to_folder,
    inspect_folder,
    remove_chat_from_folder,
    resolve_chat_in_folder,
)
from telegram_assistant.groups import (
    ContactSpec,
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
from telegram_assistant.health import collect_health
from telegram_assistant.http_api.access import (
    build_authorizer,
    delete_only_session_messages_default,
    edit_only_session_messages_default,
    resolve_entity_chat_id,
    sent_message_registry,
    translate_access_error,
    translate_entity_error,
)
from telegram_assistant.http_api.folders import AddChatRequest
from telegram_assistant.http_api.groups import (
    ContactBody,
    GroupCreateBody,
)
from telegram_assistant.http_api.groups import (
    _backends_or_503 as _group_backends_or_503,
)
from telegram_assistant.http_api.groups import (
    _store_or_503 as _group_store_or_503,
)
from telegram_assistant.http_api.members import (
    BulkMemberAddBody,
    BulkMemberItemBody,
    BulkMemberRemoveBody,
    BulkMemberRemoveItemBody,
    _member_backend_or_503,
    _member_remove_backend_or_503,
)
from telegram_assistant.http_api.members import (
    _folder_backend_optional as _member_folder_backend_optional,
)
from telegram_assistant.http_api.members import (
    _store_or_503 as _member_store_or_503,
)
from telegram_assistant.http_api.members import (
    _worker_queue_for_request as _member_worker_queue_for_request,
)
from telegram_assistant.http_api.messages import (
    DeleteBody,
    DownloadBody,
    EditBody,
    ForwardBody,
    MessageSendBody,
    PinBody,
    ReactionBody,
    UnpinBody,
    _build_pin_pacer,
    _delete_backend_or_503,
    _download_backend_or_503,
    _edit_backend_or_503,
    _forward_backend_or_503,
    _message_backend_or_503,
    _pin_backend_or_503,
    _reaction_backend_or_503,
    _read_backend_or_503,
    _resolve_download_dir,
    _search_backend_or_503,
)
from telegram_assistant.http_api.messages import (
    _folder_backend_optional as _message_folder_backend_optional,
)
from telegram_assistant.http_api.messages import (
    _folder_backend_or_503 as _message_folder_backend_or_503,
)
from telegram_assistant.http_api.messages import (
    _store_or_503 as _message_store_or_503,
)
from telegram_assistant.http_api.messages import (
    _topic_backend_optional as _message_topic_backend_optional,
)
from telegram_assistant.http_api.messages import (
    _translate_folder_error as _message_translate_folder_error,
)
from telegram_assistant.http_api.notifications import (
    MuteBody,
    UnmuteBody,
)
from telegram_assistant.http_api.notifications import (
    _backend_or_503 as _notification_backend_or_503,
)
from telegram_assistant.http_api.notifications import (
    _folder_backend_optional as _notification_folder_backend_optional,
)
from telegram_assistant.http_api.notifications import (
    _resolve_target as _notification_resolve_target,
)
from telegram_assistant.http_api.topics import (
    BulkTopicItemBody,
    TopicCloseBody,
    TopicCreateBody,
    TopicOpenBody,
    _enforce_write,
    _resolve_chat_id,
    _resolve_chat_id_generic,
    _topic_backend_or_503,
)
from telegram_assistant.http_api.topics import (
    _store_or_503 as _topic_store_or_503,
)
from telegram_assistant.http_api.topics import (
    _worker_queue_for_request as _topic_worker_queue_for_request,
)
from telegram_assistant.members import (
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
    bulk_add_members,
    bulk_remove_members,
    normalize_user_ref,
    protected_user_set,
)
from telegram_assistant.messages import (
    AttachmentError,
    Base64Attachment,
    DeleteMessagesRequest,
    ForwardMessagesRequest,
    MassSendRequest,
    MediaDownloadRequest,
    MessageDeleteForbidden,
    MessageEditForbidden,
    MessageEditRejected,
    MessageEditRequest,
    MessageSendFailed,
    MessageSendNeedsReview,
    MessageSendPending,
    PinMessageRequest,
    ScheduleError,
    SendMessageRequest,
    SendReactionRequest,
    UnpinMessageRequest,
    delete_messages,
    download_media,
    edit_message,
    forward_messages,
    get_recent_messages,
    make_url_downloader,
    mass_send_message,
    normalize_search_range,
    pin_message,
    resolve_schedule_at,
    retry_after_details,
    search_messages,
    send_message,
    set_message_reaction,
    unpin_message,
    validate_file_urls,
)
from telegram_assistant.notifications import MuteRequest, mute_chat, unmute_chat
from telegram_assistant.persistence.models import OperationStatus
from telegram_assistant.persistence.store import OperationNotFoundError, OperationStore
from telegram_assistant.topics import (
    AmbiguousTopicNameError,
    BulkTopicCreateFailed,
    BulkTopicCreateNeedsReview,
    BulkTopicCreatePending,
    BulkTopicCreateRequest,
    BulkTopicItem,
    TopicCloseFailed,
    TopicCloseNeedsReview,
    TopicClosePending,
    TopicCloseRequest,
    TopicCreateFailed,
    TopicCreateNeedsReview,
    TopicCreatePending,
    TopicCreateRequest,
    TopicNotFoundError,
    TopicOpenFailed,
    TopicOpenNeedsReview,
    TopicOpenPending,
    TopicOpenRequest,
    TopicRenameFailed,
    TopicRenameNeedsReview,
    TopicRenamePending,
    TopicRenameRequest,
    bulk_create_topics,
    close_topic,
    create_topic,
    open_topic,
    rename_topic,
    resolve_topic_id_by_name,
)
from telegram_assistant.worker.queue import FloodWaitError

AppStateProvider = Callable[[], Any]
MCP_ADMIN_SCOPE = "telegram:admin"


class McpToolError(RuntimeError):
    """JSON-encoded MCP tool error payload."""


class _McpRequest:
    """Minimal request adapter for existing request-scoped HTTP helpers."""

    def __init__(self, state: Any) -> None:
        self.app = SimpleNamespace(state=state)


READ_LOCAL = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)
READ_TELEGRAM = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=True,
)
WRITE_IDEMPOTENT = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=True,
)
WRITE_NONDESTRUCTIVE = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=False,
    openWorldHint=True,
)
WRITE_DESTRUCTIVE = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=True,
    idempotentHint=True,
    openWorldHint=True,
)


def _request(provider: AppStateProvider) -> _McpRequest:
    return _McpRequest(provider())


def _status_error_code(status_code: int) -> str:
    return {
        status.HTTP_400_BAD_REQUEST: "invalid_request",
        status.HTTP_403_FORBIDDEN: "access_denied",
        status.HTTP_404_NOT_FOUND: "not_found",
        status.HTTP_409_CONFLICT: "conflict",
        status.HTTP_502_BAD_GATEWAY: "needs_review",
        status.HTTP_503_SERVICE_UNAVAILABLE: "backend_unavailable",
    }.get(status_code, "tool_error")


def _raise_tool_error(
    *,
    code: str,
    status_code: int,
    message: str,
    detail: Any | None = None,
) -> NoReturn:
    payload = {
        "error": code,
        "status": status_code,
        "message": message,
    }
    if detail is not None:
        payload["detail"] = detail
    raise McpToolError(json.dumps(payload, sort_keys=True, default=str))


def _require_mcp_scope(scope: str) -> None:
    token = get_access_token()
    if token is None or scope not in token.scopes:
        _raise_tool_error(
            code="insufficient_scope",
            status_code=status.HTTP_403_FORBIDDEN,
            message=f"Required scope: {scope}",
        )


def _resolve_legacy_chat_id(
    *,
    chat_id: int | None,
    telegram_chat_id: int | None,
) -> int | None:
    if chat_id is not None and telegram_chat_id is not None and chat_id != telegram_chat_id:
        raise ValueError("provide either chat_id or telegram_chat_id, not both")
    return telegram_chat_id if telegram_chat_id is not None else chat_id


def _raise_from_http(exc: HTTPException) -> NoReturn:
    detail = exc.detail
    code = _status_error_code(exc.status_code)
    message = str(detail)
    if isinstance(detail, dict):
        code = str(detail.get("error") or code)
        message = str(detail.get("message") or detail)
    _raise_tool_error(
        code=code,
        status_code=exc.status_code,
        message=message,
        detail=detail,
    )


def _raise_from_exception(exc: Exception) -> NoReturn:
    if isinstance(exc, McpToolError):
        raise exc
    if isinstance(exc, HTTPException):
        _raise_from_http(exc)
    if isinstance(exc, ValidationError):
        _raise_tool_error(
            code="invalid_request",
            status_code=status.HTTP_400_BAD_REQUEST,
            message=str(exc),
            detail=exc.errors(),
        )
    translated = translate_access_error(exc)
    if translated is not None:
        _raise_from_http(translated)
    translated = translate_entity_error(exc)
    if translated is not None:
        _raise_from_http(translated)
    if isinstance(exc, FolderNotFoundError | ChatNotFoundError | EntityNotFoundError):
        _raise_tool_error(
            code="not_found",
            status_code=status.HTTP_404_NOT_FOUND,
            message=str(exc),
        )
    if isinstance(
        exc,
        FolderIdMismatchError
        | AmbiguousChatNameError
        | AmbiguousEntityError
        | AmbiguousTopicNameError,
    ):
        detail: dict[str, Any] = {"message": str(exc)}
        code = "conflict"
        if isinstance(exc, AmbiguousChatNameError):
            code = "ambiguous_chat_name"
            detail.update(
                {
                    "chat_name": exc.chat_name,
                    "folder_name": exc.folder_name,
                    "matches": exc.matches,
                }
            )
        if isinstance(exc, AmbiguousEntityError):
            code = "ambiguous_entity"
            detail.update({"entity": exc.ref, "matches": exc.matches})
        if isinstance(exc, AmbiguousTopicNameError):
            code = "ambiguous_topic_name"
            detail.update(
                {
                    "topic_name": exc.topic_name,
                    "telegram_chat_id": exc.telegram_chat_id,
                    "matches": exc.matches,
                }
            )
        _raise_tool_error(
            code=code,
            status_code=status.HTTP_409_CONFLICT,
            message=str(exc),
            detail=detail,
        )
    if isinstance(exc, TopicNotFoundError):
        _raise_tool_error(
            code="not_found",
            status_code=status.HTTP_404_NOT_FOUND,
            message=str(exc),
        )
    if isinstance(exc, FolderPeerFailureError | FloodWaitError):
        # A paced pin/unpin that ran out of retries knows when to come back —
        # pass that through so the client can schedule instead of guessing.
        _raise_tool_error(
            code="needs_review",
            status_code=status.HTTP_502_BAD_GATEWAY,
            message=str(exc),
            detail=retry_after_details(exc),
        )
    if isinstance(exc, OperationNotFoundError):
        _raise_tool_error(
            code="not_found",
            status_code=status.HTTP_404_NOT_FOUND,
            message=f"operation {exc} not found",
        )
    if isinstance(
        exc,
        GroupCreatePending
        | GroupLayoutSetPending
        | GroupRenamePending
        | MessageSendPending
        | TopicCreatePending
        | BulkTopicCreatePending
        | TopicClosePending
        | TopicOpenPending
        | TopicRenamePending
        | BulkMemberAddPending
        | BulkMemberRemovePending,
    ):
        _raise_tool_error(
            code="pending",
            status_code=status.HTTP_409_CONFLICT,
            message=str(exc),
        )
    if isinstance(
        exc,
        GroupCreateFailed
        | GroupLayoutSetFailed
        | GroupRenameFailed
        | MessageSendFailed
        | TopicCreateFailed
        | BulkTopicCreateFailed
        | TopicCloseFailed
        | TopicOpenFailed
        | TopicRenameFailed
        | BulkMemberAddFailed
        | BulkMemberRemoveFailed,
    ):
        _raise_tool_error(
            code="previous_attempt_failed",
            status_code=status.HTTP_409_CONFLICT,
            message=str(exc),
        )
    if isinstance(
        exc,
        GroupCreateNeedsReview
        | GroupLayoutSetNeedsReview
        | GroupRenameNeedsReview
        | MessageSendNeedsReview
        | TopicCreateNeedsReview
        | BulkTopicCreateNeedsReview
        | TopicCloseNeedsReview
        | TopicOpenNeedsReview
        | TopicRenameNeedsReview
        | BulkMemberAddNeedsReview
        | BulkMemberRemoveNeedsReview,
    ):
        _raise_tool_error(
            code="needs_review",
            status_code=status.HTTP_502_BAD_GATEWAY,
            message=str(exc),
        )
    if isinstance(exc, FolderError):
        translated = _message_translate_folder_error(exc)
        _raise_from_http(translated)
    if isinstance(exc, AccessDenied):
        translated = translate_access_error(exc)
        assert translated is not None
        _raise_from_http(translated)
    if isinstance(exc, MessageDeleteForbidden):
        _raise_tool_error(
            code="delete_forbidden",
            status_code=status.HTTP_403_FORBIDDEN,
            message=str(exc),
            detail={"message_ids": exc.message_ids},
        )
    if isinstance(exc, MessageEditForbidden):
        _raise_tool_error(
            code="edit_forbidden",
            status_code=status.HTTP_403_FORBIDDEN,
            message=str(exc),
            detail={"message_id": exc.message_id},
        )
    if isinstance(exc, MessageEditRejected):
        _raise_tool_error(
            code="edit_rejected",
            status_code=status.HTTP_400_BAD_REQUEST,
            message=str(exc),
            detail={"reason": exc.reason},
        )
    if isinstance(exc, AttachmentError | ScheduleError | ValueError):
        _raise_tool_error(
            code="invalid_request",
            status_code=status.HTTP_400_BAD_REQUEST,
            message=str(exc),
        )
    _raise_tool_error(
        code="tool_error",
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        message=str(exc),
    )


def _store_or_unavailable(request: _McpRequest) -> OperationStore:
    store = getattr(request.app.state, "operation_store", None)
    if store is None:
        _raise_tool_error(
            code="backend_unavailable",
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            message="Operation store is not configured",
        )
    return store


def _operation_summary(store: OperationStore, operation_id: str) -> dict[str, Any]:
    op = store.get_operation(operation_id)
    items = store.list_items(op.id)
    by_status: dict[str, int] = {}
    for item in items:
        by_status[item.status.value] = by_status.get(item.status.value, 0) + 1
    payload = op.to_dict()
    payload["items"] = {
        "total": len(items),
        "by_status": by_status,
        "needs_review": [
            {
                "id": item.id,
                "idempotency_key": item.idempotency_key,
                "error": item.error,
            }
            for item in items
            if item.status is OperationStatus.NEEDS_REVIEW
        ],
    }
    return payload


def _retry_operation(store: OperationStore, operation_id: str, *, dry_run: bool) -> dict[str, Any]:
    op = store.get_operation(operation_id)
    if op.status is OperationStatus.COMPLETED:
        _raise_tool_error(
            code="invalid_request",
            status_code=status.HTTP_400_BAD_REQUEST,
            message=f"operation {operation_id} is completed; nothing to retry",
        )

    if dry_run:
        items = store.list_items(operation_id)
        eligible = [
            item
            for item in items
            if item.status in (OperationStatus.FAILED, OperationStatus.NEEDS_REVIEW)
        ]
        would_reset_operation = op.status is not OperationStatus.PENDING
        planned_actions: list[str] = []
        if would_reset_operation:
            planned_actions.append(
                f"reset operation {operation_id} from {op.status.value} to pending"
            )
        for item in eligible:
            planned_actions.append(
                f"reset item {item.id} (key={item.idempotency_key!r}, "
                f"status={item.status.value}) to pending"
            )
        if not planned_actions:
            planned_actions.append(f"no-op: nothing to reset for operation {operation_id}")
        return {
            "status": "dry_run",
            "dry_run": True,
            "command": "operations.retry",
            "would": (
                f"reset operation {operation_id} "
                f"(and {len(eligible)} item(s)) for retry"
            ),
            "resolved": {
                "operation_id": operation_id,
                "operation_status": op.status.value,
                "operation_type": op.type,
                "would_reset_operation": would_reset_operation,
                "items_to_reset": [
                    {
                        "id": item.id,
                        "idempotency_key": item.idempotency_key,
                        "status": item.status.value,
                        "error": item.error,
                    }
                    for item in eligible
                ],
            },
            "planned_actions": planned_actions,
            "warnings": (
                [
                    f"operation {operation_id} is already pending and has no "
                    "failed/needs_review items; retry would be a no-op"
                ]
                if op.status is OperationStatus.PENDING and not eligible
                else []
            ),
        }

    reset_items = store.reset_items_for_retry(operation_id)
    if op.status is not OperationStatus.PENDING:
        store.reset_operation_for_retry(operation_id)
    return {
        "operation_id": operation_id,
        "operation_reset": op.status is not OperationStatus.PENDING,
        "items_reset": [item.id for item in reset_items],
    }


async def _resolve_message_send(
    request: _McpRequest, body: MessageSendBody
) -> dict[str, Any]:
    backend = _message_backend_or_503(request)  # type: ignore[arg-type]
    store = _message_store_or_503(request)  # type: ignore[arg-type]

    is_mass = (
        body.telegram_chat_id is None
        and body.chat_name is None
        and body.entity is None
        and body.folder_name is not None
        and body.topic_name is not None
    )
    if is_mass:
        topic_backend = _message_topic_backend_optional(request)  # type: ignore[arg-type]
        if topic_backend is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Telegram topic backend is not available for mass send",
            )
        folder_backend = _message_folder_backend_or_503(request)  # type: ignore[arg-type]
        authorizer = build_authorizer(request, folder_backend=folder_backend)  # type: ignore[arg-type]
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
            sent_registry=sent_message_registry(request),  # type: ignore[arg-type]
        )
        payload = result.to_dict()
        payload["mode"] = "mass"
        return payload

    if body.entity is not None:
        telegram_chat_id = await resolve_entity_chat_id(request, body.entity)  # type: ignore[arg-type]
        chat_name_for_log: str | None = None
    elif body.telegram_chat_id is not None:
        telegram_chat_id = body.telegram_chat_id
        chat_name_for_log = None
    else:
        folder_backend = _message_folder_backend_or_503(request)  # type: ignore[arg-type]
        chat = await resolve_chat_in_folder(
            folder_backend,
            folder_name=body.folder_name or "",
            chat_name=body.chat_name or "",
            folder_id=body.folder_id,
        )
        telegram_chat_id = chat.chat_id
        chat_name_for_log = chat.title

    telegram_topic_id = body.telegram_topic_id
    topic_name_for_log: str | None = None
    if telegram_topic_id is None and body.topic_name is not None:
        topic_backend = _message_topic_backend_optional(request)  # type: ignore[arg-type]
        if topic_backend is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Telegram topic backend is not available for topic_name resolution",
            )
        telegram_topic_id = await resolve_topic_id_by_name(
            backend=topic_backend,
            telegram_chat_id=telegram_chat_id,
            topic_name=body.topic_name,
        )
        topic_name_for_log = body.topic_name

    resolved_schedule_at = resolve_schedule_at(
        schedule_at=body.schedule_at,
        delay_seconds=body.delay_seconds,
    )
    authorizer = build_authorizer(
        request, folder_backend=_message_folder_backend_optional(request)  # type: ignore[arg-type]
    )
    files = tuple(body.files or ())
    if files:
        await authorizer.require(telegram_chat_id, AccessLevel.WRITE)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "MCP server-local files are not supported; use file_urls "
                "with http(s) URLs"
            ),
        )
    file_urls = tuple(body.file_urls or ())
    validate_file_urls(file_urls)

    base64_files = tuple(
        Base64Attachment(
            filename=att.filename,
            content_b64=att.content_b64,
            mime=att.mime,
        )
        for att in (body.base64_files or ())
    )

    result, op = await send_message(
        backend=backend,
        store=store,
        request=SendMessageRequest(
            telegram_chat_id=telegram_chat_id,
            text=body.text,
            telegram_topic_id=telegram_topic_id,
            operation_id=body.operation_id,
            chat_name=chat_name_for_log,
            topic_name=topic_name_for_log,
            files=files,
            file_urls=file_urls,
            base64_files=base64_files,
            schedule_at=resolved_schedule_at,
            reply_to_message_id=body.reply_to_message_id,
            rich_markdown=body.rich_markdown,
        ),
        authorizer=authorizer,
        sent_registry=sent_message_registry(request),  # type: ignore[arg-type]
        downloader=make_url_downloader() if file_urls else None,
    )
    payload = result.to_dict()
    payload["operation_id"] = op.id
    payload["operation_status"] = op.status.value
    payload["mode"] = "targeted"
    return payload


async def _resolve_reaction(request: _McpRequest, body: ReactionBody) -> dict[str, Any]:
    backend = _reaction_backend_or_503(request)  # type: ignore[arg-type]
    if body.entity is not None:
        telegram_chat_id = await resolve_entity_chat_id(request, body.entity)  # type: ignore[arg-type]
        chat_name_for_log: str | None = None
    elif body.telegram_chat_id is not None:
        telegram_chat_id = body.telegram_chat_id
        chat_name_for_log = None
    else:
        folder_backend = _message_folder_backend_or_503(request)  # type: ignore[arg-type]
        chat = await resolve_chat_in_folder(
            folder_backend,
            folder_name=body.folder_name or "",
            chat_name=body.chat_name or "",
            folder_id=body.folder_id,
        )
        telegram_chat_id = chat.chat_id
        chat_name_for_log = chat.title
    authorizer = build_authorizer(
        request, folder_backend=_message_folder_backend_optional(request)  # type: ignore[arg-type]
    )
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
    return result.to_dict()


async def _resolve_forward(request: _McpRequest, body: ForwardBody) -> dict[str, Any]:
    backend = _forward_backend_or_503(request)  # type: ignore[arg-type]
    from_chat_id = (
        await resolve_entity_chat_id(request, body.from_entity)  # type: ignore[arg-type]
        if body.from_entity is not None
        else body.from_chat_id
    )
    to_chat_id = (
        await resolve_entity_chat_id(request, body.to_entity)  # type: ignore[arg-type]
        if body.to_entity is not None
        else body.to_chat_id
    )
    authorizer = build_authorizer(
        request, folder_backend=_message_folder_backend_optional(request)  # type: ignore[arg-type]
    )
    result = await forward_messages(
        backend,
        request=ForwardMessagesRequest(
            from_chat_id=from_chat_id,  # type: ignore[arg-type]
            to_chat_id=to_chat_id,  # type: ignore[arg-type]
            message_ids=tuple(body.message_ids),
        ),
        authorizer=authorizer,
    )
    return result.to_dict()


async def _resolve_delete(request: _McpRequest, body: DeleteBody) -> dict[str, Any]:
    backend = _delete_backend_or_503(request)  # type: ignore[arg-type]
    if body.entity is not None:
        telegram_chat_id = await resolve_entity_chat_id(request, body.entity)  # type: ignore[arg-type]
        chat_name_for_log: str | None = None
    elif body.telegram_chat_id is not None:
        telegram_chat_id = body.telegram_chat_id
        chat_name_for_log = None
    else:
        folder_backend = _message_folder_backend_or_503(request)  # type: ignore[arg-type]
        chat = await resolve_chat_in_folder(
            folder_backend,
            folder_name=body.folder_name or "",
            chat_name=body.chat_name or "",
            folder_id=body.folder_id,
        )
        telegram_chat_id = chat.chat_id
        chat_name_for_log = chat.title

    authorizer = build_authorizer(
        request, folder_backend=_message_folder_backend_optional(request)  # type: ignore[arg-type]
    )
    only_session = await authorizer.delete_only_session_messages(
        telegram_chat_id,
        default=delete_only_session_messages_default(request),  # type: ignore[arg-type]
    )
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
        sent_registry=sent_message_registry(request),  # type: ignore[arg-type]
        only_session_messages=only_session,
    )
    return result.to_dict()


async def _resolve_edit(request: _McpRequest, body: EditBody) -> dict[str, Any]:
    backend = _edit_backend_or_503(request)  # type: ignore[arg-type]
    if body.entity is not None:
        telegram_chat_id = await resolve_entity_chat_id(request, body.entity)  # type: ignore[arg-type]
        chat_name_for_log: str | None = None
    elif body.telegram_chat_id is not None:
        telegram_chat_id = body.telegram_chat_id
        chat_name_for_log = None
    else:
        folder_backend = _message_folder_backend_or_503(request)  # type: ignore[arg-type]
        chat = await resolve_chat_in_folder(
            folder_backend,
            folder_name=body.folder_name or "",
            chat_name=body.chat_name or "",
            folder_id=body.folder_id,
        )
        telegram_chat_id = chat.chat_id
        chat_name_for_log = chat.title

    authorizer = build_authorizer(
        request, folder_backend=_message_folder_backend_optional(request)  # type: ignore[arg-type]
    )
    only_session = await authorizer.edit_only_session_messages(
        telegram_chat_id,
        default=edit_only_session_messages_default(request),  # type: ignore[arg-type]
    )
    result = await edit_message(
        backend,
        request=MessageEditRequest(
            telegram_chat_id=telegram_chat_id,
            message_id=body.message_id,
            text=body.text,
            dry_run=body.dry_run,
            chat_name=chat_name_for_log,
        ),
        authorizer=authorizer,
        sent_registry=sent_message_registry(request),  # type: ignore[arg-type]
        only_session_messages=only_session,
    )
    return result.to_dict()


async def _resolve_pin(request: _McpRequest, body: PinBody) -> dict[str, Any]:
    backend = _pin_backend_or_503(request)  # type: ignore[arg-type]
    if body.entity is not None:
        telegram_chat_id = await resolve_entity_chat_id(request, body.entity)  # type: ignore[arg-type]
        chat_name_for_log: str | None = None
    elif body.telegram_chat_id is not None:
        telegram_chat_id = body.telegram_chat_id
        chat_name_for_log = None
    else:
        folder_backend = _message_folder_backend_or_503(request)  # type: ignore[arg-type]
        chat = await resolve_chat_in_folder(
            folder_backend,
            folder_name=body.folder_name or "",
            chat_name=body.chat_name or "",
            folder_id=body.folder_id,
        )
        telegram_chat_id = chat.chat_id
        chat_name_for_log = chat.title

    authorizer = build_authorizer(
        request, folder_backend=_message_folder_backend_optional(request)  # type: ignore[arg-type]
    )
    result = await pin_message(
        backend,
        request=PinMessageRequest(
            telegram_chat_id=telegram_chat_id,
            message_id=body.message_id,
            silent=body.silent,
            pm_oneside=body.pm_oneside,
            dry_run=body.dry_run,
            chat_name=chat_name_for_log,
        ),
        authorizer=authorizer,
        pacer=_build_pin_pacer(request),  # type: ignore[arg-type]
    )
    return result.to_dict()


async def _resolve_unpin(request: _McpRequest, body: UnpinBody) -> dict[str, Any]:
    backend = _pin_backend_or_503(request)  # type: ignore[arg-type]
    if body.entity is not None:
        telegram_chat_id = await resolve_entity_chat_id(request, body.entity)  # type: ignore[arg-type]
        chat_name_for_log: str | None = None
    elif body.telegram_chat_id is not None:
        telegram_chat_id = body.telegram_chat_id
        chat_name_for_log = None
    else:
        folder_backend = _message_folder_backend_or_503(request)  # type: ignore[arg-type]
        chat = await resolve_chat_in_folder(
            folder_backend,
            folder_name=body.folder_name or "",
            chat_name=body.chat_name or "",
            folder_id=body.folder_id,
        )
        telegram_chat_id = chat.chat_id
        chat_name_for_log = chat.title

    authorizer = build_authorizer(
        request, folder_backend=_message_folder_backend_optional(request)  # type: ignore[arg-type]
    )
    result = await unpin_message(
        backend,
        request=UnpinMessageRequest(
            telegram_chat_id=telegram_chat_id,
            message_id=None if body.unpin_all else body.message_id,
            dry_run=body.dry_run,
            chat_name=chat_name_for_log,
        ),
        authorizer=authorizer,
        pacer=_build_pin_pacer(request),  # type: ignore[arg-type]
    )
    return result.to_dict()


async def _resolve_download(
    request: _McpRequest, body: DownloadBody
) -> dict[str, Any]:
    backend = _download_backend_or_503(request)  # type: ignore[arg-type]
    if body.entity is not None:
        telegram_chat_id = await resolve_entity_chat_id(request, body.entity)  # type: ignore[arg-type]
        chat_name_for_log: str | None = None
    elif body.telegram_chat_id is not None:
        telegram_chat_id = body.telegram_chat_id
        chat_name_for_log = None
    else:
        folder_backend = _message_folder_backend_or_503(request)  # type: ignore[arg-type]
        chat = await resolve_chat_in_folder(
            folder_backend,
            folder_name=body.folder_name or "",
            chat_name=body.chat_name or "",
            folder_id=body.folder_id,
        )
        telegram_chat_id = chat.chat_id
        chat_name_for_log = chat.title

    authorizer = build_authorizer(
        request, folder_backend=_message_folder_backend_optional(request)  # type: ignore[arg-type]
    )
    out_dir = _resolve_download_dir(request, body.out_dir)  # type: ignore[arg-type]
    result = await download_media(
        backend,
        request=MediaDownloadRequest(
            telegram_chat_id=telegram_chat_id,
            message_id=body.message_id,
            out_dir=out_dir,
            max_bytes=body.max_bytes,
            dry_run=body.dry_run,
            chat_name=chat_name_for_log,
        ),
        authorizer=authorizer,
    )
    return result.to_dict()


def register_telegram_tools(server: FastMCP[Any], provider: AppStateProvider) -> None:
    """Register all telegram_-prefixed tools on ``server``."""

    @server.tool(
        name="telegram_health",
        annotations=READ_LOCAL,
        structured_output=True,
    )
    async def telegram_health() -> dict[str, Any]:
        """Return the assistant health report."""
        request = _request(provider)
        try:
            report = await collect_health(
                request.app.state.config,
                session_manager=request.app.state.session_manager,
                database_path=request.app.state.database_path,
            )
            payload: dict[str, Any] = report.to_dict()
            payload["version"] = __version__
            return payload
        except Exception as exc:
            _raise_from_exception(exc)

    @server.tool(
        name="telegram_messages_recent",
        annotations=READ_TELEGRAM,
        structured_output=True,
    )
    async def telegram_messages_recent(
        chat_id: int | None = None,
        entity: str | None = None,
        limit: int = 5,
        minutes: int | None = None,
    ) -> dict[str, Any]:
        """Return recent messages from a chat.

        ``minutes`` optionally restricts the result to messages newer than
        ``now - minutes`` (composed with ``limit``).
        """
        request = _request(provider)
        try:
            if (chat_id is None) == (entity is None):
                raise ValueError("provide exactly one of chat_id or entity")
            backend = _read_backend_or_503(request)  # type: ignore[arg-type]
            resolved_chat_id = (
                await resolve_entity_chat_id(request, entity)  # type: ignore[arg-type]
                if entity is not None
                else chat_id
            )
            authorizer = build_authorizer(
                request,
                folder_backend=_message_folder_backend_optional(request),  # type: ignore[arg-type]
            )
            messages = await get_recent_messages(
                backend=backend,
                chat_id=resolved_chat_id,  # type: ignore[arg-type]
                limit=limit,
                minutes=minutes,
                authorizer=authorizer,
            )
            return {
                "telegram_chat_id": resolved_chat_id,
                "limit": limit,
                "minutes": minutes,
                "count": len(messages),
                "messages": [message.to_dict() for message in messages],
            }
        except Exception as exc:
            _raise_from_exception(exc)

    @server.tool(
        name="telegram_messages_search",
        annotations=READ_TELEGRAM,
        structured_output=True,
    )
    async def telegram_messages_search(
        query: str,
        chat_id: int | None = None,
        entity: str | None = None,
        from_user: str | None = None,
        limit: int = 20,
        minutes: int | None = None,
        topic_id: int | None = None,
        from_date: datetime | None = None,
        to_date: datetime | None = None,
    ) -> dict[str, Any]:
        """Text-search a chat's messages, newest-first (READ).

        ``query`` is required. ``from_user`` optionally narrows to one sender,
        ``topic_id`` scopes the search to one forum topic, and ``minutes``
        restricts the result to messages newer than ``now - minutes`` (composed
        with ``limit``).

        ``from_date``/``to_date`` are the fixed-range alternative to ``minutes``:
        both required together, timezone-aware, inclusive, and validated by the
        shared domain rules. The result echoes them normalised to UTC.
        """
        request = _request(provider)
        try:
            if (chat_id is None) == (entity is None):
                raise ValueError("provide exactly one of chat_id or entity")
            # Validated before the backend lookup so a bad range never reaches
            # Telegram; the same ValueError text every surface reports.
            applied_range = normalize_search_range(
                from_date=from_date, to_date=to_date, minutes=minutes
            )
            backend = _search_backend_or_503(request)  # type: ignore[arg-type]
            resolved_chat_id = (
                await resolve_entity_chat_id(request, entity)  # type: ignore[arg-type]
                if entity is not None
                else chat_id
            )
            authorizer = build_authorizer(
                request,
                folder_backend=_message_folder_backend_optional(request),  # type: ignore[arg-type]
            )
            messages = await search_messages(
                backend=backend,
                chat_id=resolved_chat_id,  # type: ignore[arg-type]
                query=query,
                from_user=from_user,
                limit=limit,
                minutes=minutes,
                topic_id=topic_id,
                from_date=from_date,
                to_date=to_date,
                authorizer=authorizer,
            )
            return {
                "telegram_chat_id": resolved_chat_id,
                "query": query,
                "from_user": from_user,
                "limit": limit,
                "minutes": minutes,
                "topic_id": topic_id,
                "from_date": (
                    applied_range[0].isoformat()
                    if applied_range is not None
                    else None
                ),
                "to_date": (
                    applied_range[1].isoformat()
                    if applied_range is not None
                    else None
                ),
                "count": len(messages),
                "messages": [message.to_dict() for message in messages],
            }
        except Exception as exc:
            _raise_from_exception(exc)

    @server.tool(
        name="telegram_messages_send",
        annotations=WRITE_IDEMPOTENT,
        structured_output=True,
    )
    async def telegram_messages_send(
        text: str = "",
        telegram_chat_id: int | None = None,
        entity: str | int | None = None,
        telegram_topic_id: int | None = None,
        topic_name: str | None = None,
        operation_id: str | None = None,
        file_urls: list[str] | None = None,
        base64_files: list[dict[str, Any]] | None = None,
        schedule_at: datetime | None = None,
        delay_seconds: int | None = None,
        reply_to_message_id: int | None = None,
        rich_markdown: str | None = None,
    ) -> dict[str, Any]:
        """Send a message to a chat (targeted by ``entity`` or ``telegram_chat_id``).

        ``base64_files`` are inline attachments ``[{filename, mime, content_b64}]``
        decoded to a temp file and sent (max 1 MB each); ``file_urls`` carry
        http(s) URLs downloaded server-side before the send. Chat targeting goes
        through the entity resolver; folder/chat-name and server-local ``files``
        are not part of the MCP surface.

        ``rich_markdown`` sends a Telegram rich message (article) instead: the
        markdown source (headings, tables, quotes, fenced code, media by public
        https URL; up to 32 768 chars) is parsed by the server. It is mutually
        exclusive with ``text`` and the attachment fields, and composes with
        ``telegram_topic_id``/``reply_to_message_id``/``schedule_at``.
        """
        request = _request(provider)
        try:
            body = MessageSendBody(
                text=text,
                telegram_chat_id=telegram_chat_id,
                entity=entity,
                telegram_topic_id=telegram_topic_id,
                topic_name=topic_name,
                operation_id=operation_id,
                file_urls=file_urls,
                base64_files=base64_files,
                schedule_at=schedule_at,
                delay_seconds=delay_seconds,
                reply_to_message_id=reply_to_message_id,
                rich_markdown=rich_markdown,
            )
            return await _resolve_message_send(request, body)
        except Exception as exc:
            _raise_from_exception(exc)

    @server.tool(
        name="telegram_messages_forward",
        annotations=WRITE_NONDESTRUCTIVE,
        structured_output=True,
    )
    async def telegram_messages_forward(
        message_ids: list[int],
        from_chat_id: int | None = None,
        from_entity: str | int | None = None,
        to_chat_id: int | None = None,
        to_entity: str | int | None = None,
    ) -> dict[str, Any]:
        """Forward messages from one chat to another."""
        request = _request(provider)
        try:
            body = ForwardBody(
                message_ids=message_ids,
                from_chat_id=from_chat_id,
                from_entity=from_entity,
                to_chat_id=to_chat_id,
                to_entity=to_entity,
            )
            return await _resolve_forward(request, body)
        except Exception as exc:
            _raise_from_exception(exc)

    @server.tool(
        name="telegram_messages_react",
        annotations=WRITE_IDEMPOTENT,
        structured_output=True,
    )
    async def telegram_messages_react(
        message_id: int,
        emoji: str | None = None,
        clear: bool = False,
        telegram_chat_id: int | None = None,
        entity: str | int | None = None,
        chat_name: str | None = None,
        folder_name: str | None = None,
        folder_id: int | None = None,
    ) -> dict[str, Any]:
        """Set or clear an emoji reaction on a message."""
        request = _request(provider)
        try:
            body = ReactionBody(
                message_id=message_id,
                emoji=emoji,
                clear=clear,
                telegram_chat_id=telegram_chat_id,
                entity=entity,
                chat_name=chat_name,
                folder_name=folder_name,
                folder_id=folder_id,
            )
            return await _resolve_reaction(request, body)
        except Exception as exc:
            _raise_from_exception(exc)

    @server.tool(
        name="telegram_messages_delete",
        annotations=WRITE_DESTRUCTIVE,
        structured_output=True,
    )
    async def telegram_messages_delete(
        message_ids: list[int],
        telegram_chat_id: int | None = None,
        entity: str | int | None = None,
        chat_name: str | None = None,
        folder_name: str | None = None,
        folder_id: int | None = None,
        revoke: bool = True,
        dry_run: bool = False,
        force: bool = False,
    ) -> dict[str, Any]:
        """Delete messages from a chat (DELETE-gated).

        Honors ``telegram.access.delete_only_session_messages`` (default true):
        when active, only messages this server process sent can be deleted.
        """
        request = _request(provider)
        try:
            body = DeleteBody(
                message_ids=message_ids,
                telegram_chat_id=telegram_chat_id,
                entity=entity,
                chat_name=chat_name,
                folder_name=folder_name,
                folder_id=folder_id,
                revoke=revoke,
                dry_run=dry_run,
                force=force,
            )
            return await _resolve_delete(request, body)
        except Exception as exc:
            _raise_from_exception(exc)

    @server.tool(
        name="telegram_messages_edit",
        annotations=WRITE_IDEMPOTENT,
        structured_output=True,
    )
    async def telegram_messages_edit(
        message_id: int,
        text: str,
        telegram_chat_id: int | None = None,
        entity: str | int | None = None,
        chat_name: str | None = None,
        folder_name: str | None = None,
        folder_id: int | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Edit the text/caption of a sent message (WRITE-gated).

        Honors ``telegram.access.edit_only_session_messages`` (default true):
        when active, only messages this server process sent can be edited.
        """
        request = _request(provider)
        try:
            body = EditBody(
                message_id=message_id,
                text=text,
                telegram_chat_id=telegram_chat_id,
                entity=entity,
                chat_name=chat_name,
                folder_name=folder_name,
                folder_id=folder_id,
                dry_run=dry_run,
            )
            return await _resolve_edit(request, body)
        except Exception as exc:
            _raise_from_exception(exc)

    @server.tool(
        name="telegram_messages_pin",
        annotations=WRITE_IDEMPOTENT,
        structured_output=True,
    )
    async def telegram_messages_pin(
        message_id: int,
        silent: bool = False,
        pm_oneside: bool = False,
        telegram_chat_id: int | None = None,
        entity: str | int | None = None,
        chat_name: str | None = None,
        folder_name: str | None = None,
        folder_id: int | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Pin a message in a target chat (WRITE-gated).

        ``silent`` suppresses the pin service notification; ``pm_oneside`` pins
        only on the acting side of a private chat.
        """
        request = _request(provider)
        try:
            body = PinBody(
                message_id=message_id,
                silent=silent,
                pm_oneside=pm_oneside,
                telegram_chat_id=telegram_chat_id,
                entity=entity,
                chat_name=chat_name,
                folder_name=folder_name,
                folder_id=folder_id,
                dry_run=dry_run,
            )
            return await _resolve_pin(request, body)
        except Exception as exc:
            _raise_from_exception(exc)

    @server.tool(
        name="telegram_messages_unpin",
        annotations=WRITE_IDEMPOTENT,
        structured_output=True,
    )
    async def telegram_messages_unpin(
        message_id: int | None = None,
        unpin_all: bool = False,
        telegram_chat_id: int | None = None,
        entity: str | int | None = None,
        chat_name: str | None = None,
        folder_name: str | None = None,
        folder_id: int | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Unpin a message (or all pinned messages) in a chat (WRITE-gated).

        Provide either a positive ``message_id`` (unpin one) or
        ``unpin_all=true`` (unpin every pinned message).
        """
        request = _request(provider)
        try:
            body = UnpinBody(
                message_id=message_id,
                unpin_all=unpin_all,
                telegram_chat_id=telegram_chat_id,
                entity=entity,
                chat_name=chat_name,
                folder_name=folder_name,
                folder_id=folder_id,
                dry_run=dry_run,
            )
            return await _resolve_unpin(request, body)
        except Exception as exc:
            _raise_from_exception(exc)

    @server.tool(
        name="telegram_messages_download",
        annotations=WRITE_NONDESTRUCTIVE,
        structured_output=True,
    )
    async def telegram_messages_download(
        message_id: int,
        out_dir: str | None = None,
        max_bytes: int | None = None,
        telegram_chat_id: int | None = None,
        entity: str | int | None = None,
        chat_name: str | None = None,
        folder_name: str | None = None,
        folder_id: int | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Download the media of an existing message to a server-side file (READ).

        Reading the source message is READ-gated, but the tool writes a local
        file (a non-destructive side effect), so it is not annotated read-only.
        ``out_dir`` is an optional server-side directory confined to
        ``telegram.download_root`` (defaults to the system temp directory) — a
        value escaping the root is rejected, so a READ-only identity cannot pick
        an arbitrary write location; the response reports the saved path plus
        size/mime. No bytes are streamed back in this iteration.
        """
        request = _request(provider)
        try:
            body = DownloadBody(
                message_id=message_id,
                out_dir=out_dir,
                max_bytes=max_bytes,
                telegram_chat_id=telegram_chat_id,
                entity=entity,
                chat_name=chat_name,
                folder_name=folder_name,
                folder_id=folder_id,
                dry_run=dry_run,
            )
            return await _resolve_download(request, body)
        except Exception as exc:
            _raise_from_exception(exc)

    @server.tool(
        name="telegram_groups_create",
        annotations=WRITE_IDEMPOTENT,
        structured_output=True,
    )
    async def telegram_groups_create(
        title: str,
        external_ref: int | str | None = None,
        planfix_task_id: int | str | None = None,
        about: str | None = None,
        admins: list[str] | None = None,
        members: list[str] | None = None,
        managers: list[str] | None = None,
        contacts: list[dict[str, str]] | None = None,
        reserve_admins: list[str] | None = None,
        reserve_members: list[str] | None = None,
        skip_reserve: bool = False,
        enable_topics: bool | None = None,
        topics_layout: Literal["list", "tabs"] | None = None,
        create_invite_link: bool | None = None,
        folder_name: str | None = None,
        folder_id: int | None = None,
        skip_folder: bool = False,
    ) -> dict[str, Any]:
        """Create a Telegram supergroup.

        ``contacts`` is an optional list of ``{"phone", "name"}`` entries: each
        is imported into the account's Telegram contacts (the phone is
        normalised) and then added as a regular member — use it for users only
        reachable by phone number.
        """
        request = _request(provider)
        try:
            body = GroupCreateBody(
                title=title,
                external_ref=external_ref,
                planfix_task_id=planfix_task_id,
                about=about,
                admins=admins or [],
                members=members or [],
                managers=managers or [],
                contacts=[ContactBody(**c) for c in (contacts or [])],
                reserve_admins=reserve_admins,
                reserve_members=reserve_members,
                skip_reserve=skip_reserve,
                enable_topics=enable_topics,
                topics_layout=topics_layout,
                create_invite_link=create_invite_link,
                folder_name=folder_name,
                folder_id=folder_id,
                skip_folder=skip_folder,
            )
            config = request.app.state.config
            backend, folder_backend = _group_backends_or_503(request)  # type: ignore[arg-type]
            store = _group_store_or_503(request)  # type: ignore[arg-type]
            if not body.skip_folder and folder_backend is None:
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail=(
                        "Telegram folder backend is not available; "
                        "set skip_folder=true to create the group without folder placement"
                    ),
                )
            result, op = await create_group(
                backend=backend,
                folder_backend=folder_backend,
                store=store,
                config=config.telegram,
                request=GroupCreateRequest(
                    title=body.title,
                    external_ref=body.effective_external_ref,
                    about=body.about,
                    admins=body.admins,
                    members=body.members,
                    contacts=[
                        ContactSpec(phone=c.phone, name=c.name)
                        for c in body.contacts
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
                ),
                plugins=request.app.state.plugin_registry,
                authorizer=build_authorizer(request, folder_backend=folder_backend),  # type: ignore[arg-type]
            )
            payload = result.to_dict()
            payload["operation_id"] = op.id
            payload["operation_status"] = op.status.value
            return payload
        except Exception as exc:
            _raise_from_exception(exc)

    @server.tool(
        name="telegram_topics_layout",
        annotations=WRITE_IDEMPOTENT,
        structured_output=True,
    )
    async def telegram_topics_layout(
        chat_id: int,
        layout: Literal["list", "tabs"] | None = None,
    ) -> dict[str, Any]:
        """Read or set a group's forum topics layout."""
        request = _request(provider)
        try:
            backend, folder_backend = _group_backends_or_503(request)  # type: ignore[arg-type]
            authorizer = build_authorizer(request, folder_backend=folder_backend)  # type: ignore[arg-type]
            if layout is None:
                await authorizer.require(chat_id, AccessLevel.READ)
                current = await get_topics_layout(
                    backend=backend,
                    telegram_chat_id=chat_id,
                )
                return {"chat_id": chat_id, "layout": current}
            await authorizer.require(chat_id, AccessLevel.WRITE)
            store = _group_store_or_503(request)  # type: ignore[arg-type]
            result, op = await set_topics_layout(
                backend=backend,
                store=store,
                request=LayoutSetRequest(
                    telegram_chat_id=chat_id,
                    layout=layout,
                ),
            )
            payload = result.to_dict()
            payload["operation_id"] = op.id
            payload["operation_status"] = op.status.value
            return payload
        except Exception as exc:
            _raise_from_exception(exc)

    @server.tool(
        name="telegram_groups_rename",
        annotations=WRITE_IDEMPOTENT,
        structured_output=True,
    )
    async def telegram_groups_rename(
        new_title: str,
        telegram_chat_id: int | None = None,
        chat_name: str | None = None,
        entity: str | int | None = None,
        folder_name: str | None = None,
        folder_id: int | None = None,
        reason: str | None = None,
    ) -> dict[str, Any]:
        """Rename a Telegram supergroup (WRITE-gated, idempotent by target title)."""
        request = _request(provider)
        try:
            # Reuse the group backend factory: rename is a thin WRITE op on the
            # supergroup, so no dedicated rename backend is warranted.
            backend, folder_backend = _group_backends_or_503(request)  # type: ignore[arg-type]
            store = _group_store_or_503(request)  # type: ignore[arg-type]
            chat_id = await _resolve_chat_id_generic(
                telegram_chat_id=telegram_chat_id,
                chat_name=chat_name,
                entity=entity,
                folder_name=folder_name,
                folder_id=folder_id,
                request=request,  # type: ignore[arg-type]
            )
            authorizer = build_authorizer(request, folder_backend=folder_backend)  # type: ignore[arg-type]
            result, op = await rename_group(
                backend=backend,
                store=store,
                request=GroupRenameRequest(
                    telegram_chat_id=chat_id,
                    new_title=new_title,
                    reason=reason,
                ),
                authorizer=authorizer,
            )
            payload = result.to_dict()
            payload["operation_id"] = op.id
            payload["operation_status"] = op.status.value
            return payload
        except Exception as exc:
            _raise_from_exception(exc)

    @server.tool(
        name="telegram_topics_create",
        annotations=WRITE_IDEMPOTENT,
        structured_output=True,
    )
    async def telegram_topics_create(
        topic_name: str,
        telegram_chat_id: int | None = None,
        chat_name: str | None = None,
        entity: str | int | None = None,
        folder_name: str | None = None,
        folder_id: int | None = None,
        external_ref: int | str | None = None,
        planfix_task_id: int | str | None = None,
        message: str | None = None,
    ) -> dict[str, Any]:
        """Create a forum topic in a Telegram group."""
        request = _request(provider)
        try:
            body = TopicCreateBody(
                topic_name=topic_name,
                telegram_chat_id=telegram_chat_id,
                chat_name=chat_name,
                entity=entity,
                folder_name=folder_name,
                folder_id=folder_id,
                external_ref=external_ref,
                planfix_task_id=planfix_task_id,
                message=message,
            )
            backend = _topic_backend_or_503(request)  # type: ignore[arg-type]
            store = _topic_store_or_503(request)  # type: ignore[arg-type]
            chat_id = await _resolve_chat_id(body, request)  # type: ignore[arg-type]
            await _enforce_write(request, chat_id)  # type: ignore[arg-type]
            result, op = await create_topic(
                backend=backend,
                store=store,
                request=TopicCreateRequest(
                    telegram_chat_id=chat_id,
                    topic_name=body.topic_name,
                    external_ref=body.effective_external_ref,
                    message=body.message,
                ),
                plugins=request.app.state.plugin_registry,
            )
            payload = result.to_dict()
            payload["operation_id"] = op.id
            payload["operation_status"] = op.status.value
            return payload
        except Exception as exc:
            _raise_from_exception(exc)

    @server.tool(
        name="telegram_topics_bulk_create",
        annotations=WRITE_IDEMPOTENT,
        structured_output=True,
    )
    async def telegram_topics_bulk_create(
        items: list[dict[str, Any]],
        telegram_chat_id: int | None = None,
        chat_name: str | None = None,
        entity: str | int | None = None,
        folder_name: str | None = None,
        folder_id: int | None = None,
        continue_on_error: bool = True,
        operation_id: str | None = None,
    ) -> dict[str, Any]:
        """Create several forum topics in a Telegram group."""
        request = _request(provider)
        try:
            parsed_items = [BulkTopicItemBody(**item) for item in items]
            body_items = [
                BulkTopicItem(
                    topic_name=item.topic_name,
                    external_ref=item.effective_external_ref,
                    message=item.message,
                )
                for item in parsed_items
            ]
            backend = _topic_backend_or_503(request)  # type: ignore[arg-type]
            store = _topic_store_or_503(request)  # type: ignore[arg-type]
            chat_id = await _resolve_chat_id_generic(
                telegram_chat_id=telegram_chat_id,
                chat_name=chat_name,
                entity=entity,
                folder_name=folder_name,
                folder_id=folder_id,
                request=request,  # type: ignore[arg-type]
            )
            await _enforce_write(request, chat_id)  # type: ignore[arg-type]
            result, op = await bulk_create_topics(
                backend=backend,
                store=store,
                queue=_topic_worker_queue_for_request(request),  # type: ignore[arg-type]
                request=BulkTopicCreateRequest(
                    telegram_chat_id=chat_id,
                    items=tuple(body_items),
                    continue_on_error=continue_on_error,
                    operation_id=operation_id,
                ),
                plugins=request.app.state.plugin_registry,
            )
            payload = result.to_dict()
            payload["operation_status"] = op.status.value
            return payload
        except Exception as exc:
            _raise_from_exception(exc)

    @server.tool(
        name="telegram_topics_close",
        annotations=WRITE_DESTRUCTIVE,
        structured_output=True,
    )
    async def telegram_topics_close(
        topic_id: int | None = None,
        topic_name: str | None = None,
        telegram_chat_id: int | None = None,
        chat_name: str | None = None,
        entity: str | int | None = None,
        folder_name: str | None = None,
        folder_id: int | None = None,
        reason: str | None = None,
    ) -> dict[str, Any]:
        """Close a forum topic."""
        request = _request(provider)
        try:
            if (topic_id is None) == (topic_name is None):
                raise ValueError("provide exactly one of topic_id or topic_name")
            if topic_id is not None and topic_id <= 0:
                raise ValueError("topic_id must be a positive integer")
            body = TopicCloseBody(
                telegram_chat_id=telegram_chat_id,
                chat_name=chat_name,
                entity=entity,
                folder_name=folder_name,
                folder_id=folder_id,
                reason=reason,
            )
            backend = _topic_backend_or_503(request)  # type: ignore[arg-type]
            store = _topic_store_or_503(request)  # type: ignore[arg-type]
            chat_id = await _resolve_chat_id_generic(
                telegram_chat_id=body.telegram_chat_id,
                chat_name=body.chat_name,
                entity=body.entity,
                folder_name=body.folder_name,
                folder_id=body.folder_id,
                request=request,  # type: ignore[arg-type]
            )
            await _enforce_write(request, chat_id)  # type: ignore[arg-type]
            effective_topic_id = (
                topic_id
                if topic_id is not None
                else await resolve_topic_id_by_name(
                    backend=backend,
                    telegram_chat_id=chat_id,
                    topic_name=topic_name or "",
                )
            )
            result, op = await close_topic(
                backend=backend,
                store=store,
                request=TopicCloseRequest(
                    telegram_chat_id=chat_id,
                    telegram_topic_id=effective_topic_id,
                    reason=body.reason,
                ),
            )
            payload = result.to_dict()
            payload["operation_id"] = op.id
            payload["operation_status"] = op.status.value
            return payload
        except Exception as exc:
            _raise_from_exception(exc)

    @server.tool(
        name="telegram_topics_open",
        annotations=WRITE_IDEMPOTENT,
        structured_output=True,
    )
    async def telegram_topics_open(
        topic_id: int | None = None,
        topic_name: str | None = None,
        telegram_chat_id: int | None = None,
        chat_name: str | None = None,
        entity: str | int | None = None,
        folder_name: str | None = None,
        folder_id: int | None = None,
        reason: str | None = None,
    ) -> dict[str, Any]:
        """Reopen a closed forum topic."""
        request = _request(provider)
        try:
            if (topic_id is None) == (topic_name is None):
                raise ValueError("provide exactly one of topic_id or topic_name")
            if topic_id is not None and topic_id <= 0:
                raise ValueError("topic_id must be a positive integer")
            body = TopicOpenBody(
                telegram_chat_id=telegram_chat_id,
                chat_name=chat_name,
                entity=entity,
                folder_name=folder_name,
                folder_id=folder_id,
                reason=reason,
            )
            backend = _topic_backend_or_503(request)  # type: ignore[arg-type]
            store = _topic_store_or_503(request)  # type: ignore[arg-type]
            chat_id = await _resolve_chat_id_generic(
                telegram_chat_id=body.telegram_chat_id,
                chat_name=body.chat_name,
                entity=body.entity,
                folder_name=body.folder_name,
                folder_id=body.folder_id,
                request=request,  # type: ignore[arg-type]
            )
            await _enforce_write(request, chat_id)  # type: ignore[arg-type]
            effective_topic_id = (
                topic_id
                if topic_id is not None
                else await resolve_topic_id_by_name(
                    backend=backend,
                    telegram_chat_id=chat_id,
                    topic_name=topic_name or "",
                )
            )
            result, op = await open_topic(
                backend=backend,
                store=store,
                request=TopicOpenRequest(
                    telegram_chat_id=chat_id,
                    telegram_topic_id=effective_topic_id,
                    reason=body.reason,
                ),
            )
            payload = result.to_dict()
            payload["operation_id"] = op.id
            payload["operation_status"] = op.status.value
            return payload
        except Exception as exc:
            _raise_from_exception(exc)

    @server.tool(
        name="telegram_topics_rename",
        annotations=WRITE_IDEMPOTENT,
        structured_output=True,
    )
    async def telegram_topics_rename(
        new_title: str,
        topic_id: int | None = None,
        topic_name: str | None = None,
        telegram_chat_id: int | None = None,
        chat_name: str | None = None,
        entity: str | int | None = None,
        folder_name: str | None = None,
        folder_id: int | None = None,
        reason: str | None = None,
    ) -> dict[str, Any]:
        """Rename a forum topic (WRITE-gated, idempotent by target title)."""
        request = _request(provider)
        try:
            if (topic_id is None) == (topic_name is None):
                raise ValueError("provide exactly one of topic_id or topic_name")
            if topic_id is not None and topic_id <= 0:
                raise ValueError("topic_id must be a positive integer")
            backend = _topic_backend_or_503(request)  # type: ignore[arg-type]
            store = _topic_store_or_503(request)  # type: ignore[arg-type]
            chat_id = await _resolve_chat_id_generic(
                telegram_chat_id=telegram_chat_id,
                chat_name=chat_name,
                entity=entity,
                folder_name=folder_name,
                folder_id=folder_id,
                request=request,  # type: ignore[arg-type]
            )
            await _enforce_write(request, chat_id)  # type: ignore[arg-type]
            effective_topic_id = (
                topic_id
                if topic_id is not None
                else await resolve_topic_id_by_name(
                    backend=backend,
                    telegram_chat_id=chat_id,
                    topic_name=topic_name or "",
                )
            )
            result, op = await rename_topic(
                backend=backend,
                store=store,
                request=TopicRenameRequest(
                    telegram_chat_id=chat_id,
                    telegram_topic_id=effective_topic_id,
                    new_title=new_title,
                    reason=reason,
                ),
            )
            payload = result.to_dict()
            payload["operation_id"] = op.id
            payload["operation_status"] = op.status.value
            return payload
        except Exception as exc:
            _raise_from_exception(exc)

    @server.tool(
        name="telegram_members_add",
        annotations=WRITE_IDEMPOTENT,
        structured_output=True,
    )
    async def telegram_members_add(
        items: list[dict[str, str]],
        chat_id: int | None = None,
        telegram_chat_id: int | None = None,
        chat_name: str | None = None,
        entity: str | int | None = None,
        folder_name: str | None = None,
        folder_id: int | None = None,
        continue_on_error: bool = True,
        operation_id: str | None = None,
    ) -> dict[str, Any]:
        """Add members to a Telegram group."""
        request = _request(provider)
        try:
            body = BulkMemberAddBody(
                items=[BulkMemberItemBody(**item) for item in items],
                continue_on_error=continue_on_error,
                operation_id=operation_id,
            )
            backend = _member_backend_or_503(request)  # type: ignore[arg-type]
            store = _member_store_or_503(request)  # type: ignore[arg-type]
            resolved_chat_id = await _resolve_chat_id_generic(
                telegram_chat_id=_resolve_legacy_chat_id(
                    chat_id=chat_id, telegram_chat_id=telegram_chat_id
                ),
                chat_name=chat_name,
                entity=entity,
                folder_name=folder_name,
                folder_id=folder_id,
                request=request,  # type: ignore[arg-type]
            )
            result, op = await bulk_add_members(
                backend=backend,
                store=store,
                queue=_member_worker_queue_for_request(request),  # type: ignore[arg-type]
                request=BulkMemberAddRequest(
                    telegram_chat_id=resolved_chat_id,
                    items=tuple(
                        BulkMemberItem(user=item.user, role=item.role)
                        for item in body.items
                    ),
                    continue_on_error=body.continue_on_error,
                    operation_id=body.operation_id,
                ),
                authorizer=build_authorizer(
                    request,
                    folder_backend=_member_folder_backend_optional(request),  # type: ignore[arg-type]
                ),
            )
            payload = result.to_dict()
            payload["operation_status"] = op.status.value
            return payload
        except Exception as exc:
            _raise_from_exception(exc)

    @server.tool(
        name="telegram_members_remove",
        annotations=WRITE_DESTRUCTIVE,
        structured_output=True,
    )
    async def telegram_members_remove(
        items: list[dict[str, str]],
        chat_id: int | None = None,
        telegram_chat_id: int | None = None,
        chat_name: str | None = None,
        entity: str | int | None = None,
        folder_name: str | None = None,
        folder_id: int | None = None,
        mode: str = "ban_unban",
        continue_on_error: bool = True,
        operation_id: str | None = None,
        force: bool = False,
    ) -> dict[str, Any]:
        """Remove members from a Telegram group."""
        request = _request(provider)
        try:
            body = BulkMemberRemoveBody(
                items=[BulkMemberRemoveItemBody(**item) for item in items],
                mode=mode,
                continue_on_error=continue_on_error,
                operation_id=operation_id,
                force=force,
            )
            backend = _member_remove_backend_or_503(request)  # type: ignore[arg-type]
            store = _member_store_or_503(request)  # type: ignore[arg-type]
            resolved_chat_id = await _resolve_chat_id_generic(
                telegram_chat_id=_resolve_legacy_chat_id(
                    chat_id=chat_id, telegram_chat_id=telegram_chat_id
                ),
                chat_name=chat_name,
                entity=entity,
                folder_name=folder_name,
                folder_id=folder_id,
                request=request,  # type: ignore[arg-type]
            )
            if not body.force:
                config = getattr(request.app.state, "config", None)
                if config is not None and getattr(config, "telegram", None) is not None:
                    protected = protected_user_set(
                        config=config.telegram,
                        plugins=request.app.state.plugin_registry,
                    )
                    blocked: list[str] = []
                    for item in body.items:
                        try:
                            if normalize_user_ref(item.user).value in protected:
                                blocked.append(item.user)
                        except ValueError:
                            continue
                    if blocked:
                        raise HTTPException(
                            status_code=status.HTTP_409_CONFLICT,
                            detail={
                                "error": "protected_accounts",
                                "message": (
                                    "refusing to remove protected accounts "
                                    "without force=true"
                                ),
                                "users": blocked,
                            },
                        )
            result, op = await bulk_remove_members(
                backend=backend,
                store=store,
                queue=_member_worker_queue_for_request(request),  # type: ignore[arg-type]
                request=BulkMemberRemoveRequest(
                    telegram_chat_id=resolved_chat_id,
                    items=tuple(
                        BulkMemberRemoveItem(user=item.user) for item in body.items
                    ),
                    mode=body.mode,
                    continue_on_error=body.continue_on_error,
                    operation_id=body.operation_id,
                ),
                authorizer=build_authorizer(
                    request,
                    folder_backend=_member_folder_backend_optional(request),  # type: ignore[arg-type]
                ),
            )
            payload = result.to_dict()
            payload["operation_status"] = op.status.value
            return payload
        except Exception as exc:
            _raise_from_exception(exc)

    @server.tool(
        name="telegram_folders_inspect",
        annotations=READ_TELEGRAM,
        structured_output=True,
    )
    async def telegram_folders_inspect(
        folder_name: str,
        folder_id: int | None = None,
    ) -> dict[str, Any]:
        """Inspect a Telegram folder and its chats."""
        request = _request(provider)
        try:
            backend = _message_folder_backend_or_503(request)  # type: ignore[arg-type]
            authorizer = build_authorizer(request, folder_backend=backend)  # type: ignore[arg-type]
            # Mirror of the HTTP route: resolve first so the READ gate sees the
            # folder's own id and a `folder_id:` rule grants an inspect by name.
            try:
                snapshot = await inspect_folder(
                    backend,
                    folder_name=folder_name,
                    folder_id=folder_id,
                )
            except (FolderNotFoundError, FolderIdMismatchError):
                # Mirror of the HTTP route: a resolution failure must not
                # outrank the denial, or an ungranted caller can tell present
                # folders (403) from absent ones (404) and drive a
                # dialog-filter RPC per probe. The gate runs on the requested
                # *name* only: the caller-supplied `folder_id` is unverified
                # here, so applying a `folder_id:` rule to it would let READ on
                # one folder unlock probing every other title.
                await authorizer.require_folder(folder_name, AccessLevel.READ)
                raise
            await authorizer.require_folder(
                snapshot.folder_name,
                AccessLevel.READ,
                folder_id=snapshot.folder_id,
            )
            return snapshot.to_dict()
        except Exception as exc:
            _raise_from_exception(exc)

    @server.tool(
        name="telegram_folders_add_chat",
        annotations=WRITE_IDEMPOTENT,
        structured_output=True,
    )
    async def telegram_folders_add_chat(
        folder_name: str,
        chat_id: int | None = None,
        chat_name: str | None = None,
        entity: str | int | None = None,
        folder_id: int | None = None,
    ) -> dict[str, Any]:
        """Add a chat to a Telegram folder."""
        request = _request(provider)
        try:
            body = AddChatRequest(
                chat_id=chat_id,
                chat_name=chat_name,
                entity=entity,
                folder_id=folder_id,
            )
            backend = _message_folder_backend_or_503(request)  # type: ignore[arg-type]
            if body.entity is not None:
                chat_ref: str | int = await resolve_entity_chat_id(request, body.entity)  # type: ignore[arg-type]
            elif body.chat_id is not None:
                chat_ref = body.chat_id
            else:
                resolved = await resolve_chat_in_folder(
                    backend,
                    folder_name=folder_name,
                    chat_name=body.chat_name or "",
                    folder_id=body.folder_id,
                )
                chat_ref = resolved.chat_id
            return await add_chat_to_folder(
                backend,
                folder_name=folder_name,
                chat_ref=chat_ref,
                folder_id=body.folder_id,
                authorizer=build_authorizer(request, folder_backend=backend),  # type: ignore[arg-type]
            )
        except Exception as exc:
            _raise_from_exception(exc)

    @server.tool(
        name="telegram_folders_remove_chat",
        annotations=WRITE_DESTRUCTIVE,
        structured_output=True,
    )
    async def telegram_folders_remove_chat(
        folder_name: str,
        chat_id: int | None = None,
        chat_name: str | None = None,
        entity: str | int | None = None,
        folder_id: int | None = None,
    ) -> dict[str, Any]:
        """Remove a chat from a Telegram folder."""
        request = _request(provider)
        try:
            body = AddChatRequest(
                chat_id=chat_id,
                chat_name=chat_name,
                entity=entity,
                folder_id=folder_id,
            )
            backend = _message_folder_backend_or_503(request)  # type: ignore[arg-type]
            if body.entity is not None:
                chat_ref: str | int = await resolve_entity_chat_id(request, body.entity)  # type: ignore[arg-type]
            elif body.chat_id is not None:
                chat_ref = body.chat_id
            else:
                resolved = await resolve_chat_in_folder(
                    backend,
                    folder_name=folder_name,
                    chat_name=body.chat_name or "",
                    folder_id=body.folder_id,
                )
                chat_ref = resolved.chat_id
            return await remove_chat_from_folder(
                backend,
                folder_name=folder_name,
                chat_ref=chat_ref,
                folder_id=body.folder_id,
                authorizer=build_authorizer(request, folder_backend=backend),  # type: ignore[arg-type]
            )
        except Exception as exc:
            _raise_from_exception(exc)

    @server.tool(
        name="telegram_notifications_mute",
        annotations=WRITE_IDEMPOTENT,
        structured_output=True,
    )
    async def telegram_notifications_mute(
        telegram_chat_id: int | None = None,
        entity: str | int | None = None,
        chat_name: str | None = None,
        folder_name: str | None = None,
        folder_id: int | None = None,
        duration_hours: int | None = None,
    ) -> dict[str, Any]:
        """Mute a chat's notifications."""
        request = _request(provider)
        try:
            body = MuteBody(
                telegram_chat_id=telegram_chat_id,
                entity=entity,
                chat_name=chat_name,
                folder_name=folder_name,
                folder_id=folder_id,
                duration_hours=duration_hours,
            )
            backend = _notification_backend_or_503(request)  # type: ignore[arg-type]
            resolved_chat_id, chat_name_for_log = await _notification_resolve_target(
                request, body  # type: ignore[arg-type]
            )
            result = await mute_chat(
                backend,
                request=MuteRequest(
                    telegram_chat_id=resolved_chat_id,
                    duration_hours=body.duration_hours,
                    chat_name=chat_name_for_log,
                ),
                authorizer=build_authorizer(
                    request,
                    folder_backend=_notification_folder_backend_optional(request),  # type: ignore[arg-type]
                ),
            )
            return result.to_dict()
        except Exception as exc:
            _raise_from_exception(exc)

    @server.tool(
        name="telegram_notifications_unmute",
        annotations=WRITE_IDEMPOTENT,
        structured_output=True,
    )
    async def telegram_notifications_unmute(
        telegram_chat_id: int | None = None,
        entity: str | int | None = None,
        chat_name: str | None = None,
        folder_name: str | None = None,
        folder_id: int | None = None,
    ) -> dict[str, Any]:
        """Unmute a chat's notifications."""
        request = _request(provider)
        try:
            body = UnmuteBody(
                telegram_chat_id=telegram_chat_id,
                entity=entity,
                chat_name=chat_name,
                folder_name=folder_name,
                folder_id=folder_id,
            )
            backend = _notification_backend_or_503(request)  # type: ignore[arg-type]
            resolved_chat_id, chat_name_for_log = await _notification_resolve_target(
                request, body  # type: ignore[arg-type]
            )
            result = await unmute_chat(
                backend,
                request=MuteRequest(
                    telegram_chat_id=resolved_chat_id,
                    chat_name=chat_name_for_log,
                ),
                authorizer=build_authorizer(
                    request,
                    folder_backend=_notification_folder_backend_optional(request),  # type: ignore[arg-type]
                ),
            )
            return result.to_dict()
        except Exception as exc:
            _raise_from_exception(exc)

    @server.tool(
        name="telegram_operations_status",
        annotations=READ_LOCAL,
        structured_output=True,
    )
    async def telegram_operations_status(operation_id: str) -> dict[str, Any]:
        """Inspect a persisted operation."""
        request = _request(provider)
        try:
            _require_mcp_scope(MCP_ADMIN_SCOPE)
            return _operation_summary(_store_or_unavailable(request), operation_id)
        except Exception as exc:
            _raise_from_exception(exc)

    @server.tool(
        name="telegram_operations_retry",
        annotations=WRITE_IDEMPOTENT,
        structured_output=True,
    )
    async def telegram_operations_retry(
        operation_id: str,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Reset a failed or needs-review operation for retry."""
        request = _request(provider)
        try:
            _require_mcp_scope(MCP_ADMIN_SCOPE)
            return _retry_operation(
                _store_or_unavailable(request),
                operation_id,
                dry_run=dry_run,
            )
        except Exception as exc:
            _raise_from_exception(exc)
