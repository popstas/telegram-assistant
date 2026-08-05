"""HTTP routes for read-only chat metadata (the ``chats`` domain).

One route, ``GET /telegram/chats/inspect``, exposing
:func:`telegram_assistant.chats.inspect_chat`. The reference handling, the READ
gate wiring and the domain call live in :func:`inspect_chat_for_request`, which
the MCP tool ``telegram_chats_inspect`` calls verbatim — the two remote surfaces
must not be able to drift apart on which references they accept or on whether
``raw`` reaches the domain op.
"""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status

from telegram_assistant.access import AccessDenied
from telegram_assistant.chats import ChatInspectBackend, inspect_chat
from telegram_assistant.folders import FolderBackend
from telegram_assistant.http_api.access import build_authorizer, translate_access_error
from telegram_assistant.http_api.auth import BearerAuth
from telegram_assistant.http_api.messages import _translate_flood_wait
from telegram_assistant.http_api.topics import _resolve_chat_id_generic
from telegram_assistant.worker.queue import FloodWaitError

#: Why a remote caller may not ask for the serialized Telethon objects. The
#: curated payload is designed to be enough; ``raw`` carries considerably more
#: (a legacy group's whole member roster via ``ChatFull.participants``, a user's
#: business location and stories), and this project already keeps local-only
#: capabilities off the remote surfaces — ``scan_media`` resolves server-side
#: paths for the CLI alone, ``messages download --out`` is unconfined only there.
RAW_REJECTED_MESSAGE = (
    "raw is CLI-only: the serialized entity/Full objects are never returned "
    "over HTTP or MCP; run `telegram-assistant chats inspect --raw` locally"
)


def _chat_inspect_backend_or_503(request: Request) -> ChatInspectBackend:
    """Resolve the chat-inspect backend, or raise 503.

    Two stages like every sibling helper: no factory at all means nobody wired
    one (only a test opts out); a factory returning ``None`` is the production
    case where the Telethon client is not connected yet.
    """
    factory = getattr(request.app.state, "chat_inspect_backend_factory", None)
    if factory is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Telegram chat-inspect backend is not configured (session may be unauthorized)",
        )
    backend = factory(request)
    if backend is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Telegram chat-inspect backend is not available",
        )
    return backend


def _folder_backend_optional(request: Request) -> FolderBackend | None:
    factory = getattr(request.app.state, "folder_backend_factory", None)
    if factory is None:
        return None
    return factory(request)


def validate_chat_inspect_args(
    *,
    chat_id: int | None,
    chat_name: str | None,
    entity: str | int | None,
    folder_name: str | None,
    raw: bool,
) -> None:
    """Reject a malformed remote chats-inspect request (raises ``ValueError``).

    ``raw`` is checked **first**, and rejected rather than ignored: a silently
    dropped ``raw=true`` is indistinguishable from an empty raw payload, so the
    caller would never learn the flag went nowhere. Checking it before the
    reference rules also means the message names the real problem even when the
    request is malformed in two ways at once.

    The reference rules mirror ``PinBody._shape``: exactly one of ``chat_id`` /
    ``chat_name`` / ``entity``, and ``chat_name`` needs ``folder_name`` (there
    is no config-derived folder default on the remote surfaces — that is a CLI
    convenience).
    """
    if raw:
        raise ValueError(RAW_REJECTED_MESSAGE)
    refs = sum([chat_id is not None, chat_name is not None, entity is not None])
    if refs != 1:
        raise ValueError("provide exactly one of chat_id, chat_name, or entity")
    if chat_name is not None and folder_name is None:
        raise ValueError("chat_name requires folder_name")


def _annotate_retry_after(exc: FloodWaitError) -> FloodWaitError:
    """Give an *unpaced* flood-wait the retry fields the surfaces report.

    ``messages pin``/``unpin`` run behind a pacer, so what reaches their
    surfaces is a ``PacedFloodWaitError`` already carrying
    ``retry_after_seconds``/``retry_at`` — the two fields
    :func:`retry_after_details` reads and that both surfaces echo (HTTP also as
    the standard ``Retry-After`` header). ``chats inspect`` is a one-shot read
    with no pacer, so its adapter surfaces a bare ``FloodWaitError`` whose wait
    window lives on ``.seconds`` only. Copying it across lets the *same*
    mapping produce the same payload here rather than a second, poorer one —
    the caller of a read op needs to know when to come back just as much.
    """
    if getattr(exc, "retry_after_seconds", None) is None:
        seconds = float(getattr(exc, "seconds", 0.0) or 0.0)
        exc.retry_after_seconds = seconds  # type: ignore[attr-defined]
        exc.retry_at = time.time() + seconds  # type: ignore[attr-defined]
    return exc


async def inspect_chat_for_request(
    request: Request,
    *,
    chat_id: int | None = None,
    chat_name: str | None = None,
    entity: str | int | None = None,
    folder_name: str | None = None,
    folder_id: int | None = None,
    raw: bool = False,
) -> dict[str, Any]:
    """Resolve the chat reference, gate READ, and return the inspect payload.

    Shared by the HTTP route and the MCP tool. It raises rather than mapping,
    so each surface applies its own taxonomy: ``ValueError`` for malformed
    input, ``HTTPException`` for the 503/404/409 resolution failures,
    ``AccessDenied`` for a denied chat, and ``FloodWaitError`` (already carrying
    retry-after) for a throttle.

    ``raw`` is only ever *rejected* here; the domain call passes ``raw=False``
    unconditionally, so no remote caller's flag can reach the serializer.
    """
    validate_chat_inspect_args(
        chat_id=chat_id,
        chat_name=chat_name,
        entity=entity,
        folder_name=folder_name,
        raw=raw,
    )
    backend = _chat_inspect_backend_or_503(request)
    try:
        # The annotation covers *reference resolution* too, not just the domain
        # call: the ``entity`` probe (``get_entity`` plus the exact-title
        # ``iter_dialogs`` scan) and the ``chat_name`` branch's
        # ``list_folders()`` are classic FLOOD_WAIT sources, and their adapters
        # raise the same bare ``FloodWaitError`` carrying only ``.seconds``.
        # Annotating only the ``inspect_chat`` call left half the surface
        # answering 502 with no ``Retry-After``/``retry_after_seconds`` at all.
        resolved_chat_id = await _resolve_chat_id_generic(
            telegram_chat_id=chat_id,
            chat_name=chat_name,
            entity=entity,
            folder_name=folder_name,
            folder_id=folder_id,
            request=request,
        )
        authorizer = build_authorizer(
            request, folder_backend=_folder_backend_optional(request)
        )
        info = await inspect_chat(
            backend=backend,
            chat_id=resolved_chat_id,
            raw=False,
            authorizer=authorizer,
        )
    except FloodWaitError as exc:
        _annotate_retry_after(exc)
        raise
    return info.to_dict()


def build_router() -> APIRouter:
    router = APIRouter(dependencies=[BearerAuth])

    @router.get("/chats/inspect")
    async def chats_inspect(
        request: Request,
        chat_id: int | None = None,
        chat_name: str | None = None,
        entity: str | None = None,
        folder_name: str | None = None,
        folder_id: int | None = None,
        raw: bool = False,
    ) -> dict[str, Any]:
        """Read one chat's metadata: TTL, description, counts, rights (READ-gated).

        Target the chat with exactly one of ``chat_id``, ``entity``, or
        ``chat_name`` (which requires ``folder_name``, optionally cross-checked
        by ``folder_id``) — the same set the CLI takes. ``raw`` is accepted only
        so it can be rejected with 400: the serialized Telethon objects are
        CLI-only. The body is the domain payload verbatim.
        """
        try:
            return await inspect_chat_for_request(
                request,
                chat_id=chat_id,
                chat_name=chat_name,
                entity=entity,
                folder_name=folder_name,
                folder_id=folder_id,
                raw=raw,
            )
        except AccessDenied as exc:
            raise translate_access_error(exc) from exc
        except FloodWaitError as exc:
            raise _translate_flood_wait(exc) from exc
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
            ) from exc

    return router
