"""Group-creation domain shared by HTTP, CLI, and worker.

The :func:`create_group` function is the single source of truth for the
"create a Telegram supergroup" workflow. It orchestrates the supergroup
creation, member/admin/reserve population, invite-link creation, and folder
placement. Integration-specific side effects (e.g. a ``/task <id>`` service
message) are delegated to the optional plugin layer via
:meth:`PluginRegistry.after_group_create`, so the core stays integration-free.

Idempotency is anchored at the persistence layer: the operation key is derived
from ``external_ref`` if present, otherwise the exact ``title``. A replay of a
completed call returns the saved result without touching Telegram.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from telegram_assistant.config.models import TelegramConfig, TopicsLayout
from telegram_assistant.folders.service import (
    FolderBackend,
    FolderError,
    FolderPeerFailureError,
    resolve_folder,
)
from telegram_assistant.persistence import idempotency
from telegram_assistant.persistence.models import (
    OperationRecord,
    OperationStatus,
)
from telegram_assistant.persistence.store import OperationStore
from telegram_assistant.plugins import PluginRegistry
from telegram_assistant.worker.queue import FloodWaitError

logger = logging.getLogger(__name__)


class GroupError(RuntimeError):
    """Base class for group-creation failures surfaced to callers."""


class GroupCreateFailed(GroupError):
    """A previous attempt with this idempotency key already failed."""


class GroupCreatePending(GroupError):
    """A concurrent attempt with this idempotency key is still in flight."""


class GroupCreateNeedsReview(GroupError):
    """A previous attempt resulted in ``needs_review`` and must not auto-retry."""


class GroupLayoutSetFailed(GroupError):
    """A previous layout-set attempt with this idempotency key already failed."""


class GroupLayoutSetPending(GroupError):
    """A concurrent layout-set attempt with this idempotency key is in flight."""


class GroupLayoutSetNeedsReview(GroupError):
    """A previous layout-set attempt resulted in needs_review."""


def _layout_to_tabs(layout: str) -> bool:
    """Map the public ``"list" | "tabs"`` string to the Telethon ``tabs`` flag."""
    if layout == "tabs":
        return True
    if layout == "list":
        return False
    raise ValueError(f"unknown topics layout {layout!r}; expected 'list' or 'tabs'")


def _tabs_to_layout(tabs: bool) -> TopicsLayout:
    """Inverse of :func:`_layout_to_tabs`."""
    return "tabs" if tabs else "list"


@dataclass(frozen=True)
class GroupCreateRequest:
    """Input shape shared by HTTP, CLI, and tests."""

    title: str
    external_ref: int | str | None = None
    about: str | None = None
    admins: Sequence[str] = ()
    members: Sequence[str] = ()
    reserve_admins: Sequence[str] | None = None
    reserve_members: Sequence[str] | None = None
    skip_reserve: bool = False
    enable_topics: bool | None = None
    topics_layout: TopicsLayout | None = None
    create_invite_link: bool | None = None
    folder_name: str | None = None
    folder_id: int | None = None
    skip_folder: bool = False

    def to_payload(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "external_ref": self.external_ref,
            "about": self.about,
            "admins": list(self.admins),
            "members": list(self.members),
            "reserve_admins": (
                list(self.reserve_admins) if self.reserve_admins is not None else None
            ),
            "reserve_members": (
                list(self.reserve_members) if self.reserve_members is not None else None
            ),
            "skip_reserve": self.skip_reserve,
            "enable_topics": self.enable_topics,
            "topics_layout": self.topics_layout,
            "create_invite_link": self.create_invite_link,
            "folder_name": self.folder_name,
            "folder_id": self.folder_id,
            "skip_folder": self.skip_folder,
        }


@dataclass(frozen=True)
class GroupCreateResult:
    """Result returned by both the live execution and a replay."""

    telegram_chat_id: int
    title: str
    external_ref: int | str | None
    invite_link: str | None
    folder_id: int | None
    folder_name: str | None
    topics_enabled: bool
    admins_added: list[str] = field(default_factory=list)
    members_added: list[str] = field(default_factory=list)
    admins_promoted: list[str] = field(default_factory=list)
    skipped: list[dict[str, Any]] = field(default_factory=list)
    task_message_sent: bool = False
    replayed: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "telegram_chat_id": self.telegram_chat_id,
            "title": self.title,
            "external_ref": self.external_ref,
            "invite_link": self.invite_link,
            "folder_id": self.folder_id,
            "folder_name": self.folder_name,
            "topics_enabled": self.topics_enabled,
            "admins_added": list(self.admins_added),
            "members_added": list(self.members_added),
            "admins_promoted": list(self.admins_promoted),
            "skipped": list(self.skipped),
            "task_message_sent": self.task_message_sent,
            "replayed": self.replayed,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> GroupCreateResult:
        return cls(
            telegram_chat_id=int(payload["telegram_chat_id"]),
            title=str(payload["title"]),
            # Legacy records persisted ``planfix_task_id``; accept both on read.
            external_ref=payload.get("external_ref", payload.get("planfix_task_id")),
            invite_link=payload.get("invite_link"),
            folder_id=payload.get("folder_id"),
            folder_name=payload.get("folder_name"),
            topics_enabled=bool(payload.get("topics_enabled", False)),
            admins_added=list(payload.get("admins_added") or []),
            members_added=list(payload.get("members_added") or []),
            admins_promoted=list(payload.get("admins_promoted") or []),
            skipped=list(payload.get("skipped") or []),
            task_message_sent=bool(payload.get("task_message_sent", False)),
            replayed=True,
        )


class GroupBackend(Protocol):
    """Telethon-facing operations needed to build a client supergroup.

    Tests inject a fake; production wires this to a Telethon-specific adapter.
    Each method is a thin verb so the domain function stays pure orchestration.
    """

    async def create_supergroup(
        self,
        *,
        title: str,
        about: str | None,
        enable_topics: bool,
    ) -> int:
        ...

    async def add_member(self, *, chat_id: int, user: str) -> None:
        ...

    async def promote_admin(self, *, chat_id: int, user: str) -> None:
        ...

    async def create_invite_link(self, *, chat_id: int) -> str:
        ...

    async def send_message(self, *, chat_id: int, text: str) -> int:
        ...

    async def set_topics_layout(self, *, chat_id: int, tabs: bool) -> None:
        ...

    async def get_topics_layout(self, *, chat_id: int) -> bool:
        ...

    async def chat_exists(self, *, chat_id: int) -> bool:
        ...

    async def set_default_permissions(
        self,
        *,
        chat_id: int,
        allow_create_topics: bool,
        allow_pin_messages: bool,
    ) -> None:
        ...

    async def get_recent_messages(
        self, *, chat_id: int, limit: int
    ) -> list[dict[str, Any]]:
        """Return up to ``limit`` recent messages, newest first.

        Each entry is ``{"id", "sender_username", "reply_to_msg_id", "text"}``;
        ``sender_username`` and ``reply_to_msg_id`` may be ``None``.
        """
        ...

    async def delete_messages(
        self, *, chat_id: int, message_ids: Sequence[int]
    ) -> None:
        ...


def _resolved_reserves(
    explicit: Sequence[str] | None,
    *,
    fallback: Sequence[str],
    skip: bool,
) -> list[str]:
    """Combine explicit overrides with configured defaults.

    ``explicit=None`` means "use the configured defaults" so a caller that does
    not pass anything still gets reserves; an empty list ``[]`` means "do not
    add any from this category". ``skip=True`` zeroes both.
    """
    if skip:
        return []
    if explicit is None:
        return list(fallback)
    return list(explicit)


def _drop_blank(items: Sequence[str]) -> list[str]:
    """Drop ``None`` and blank/whitespace-only user references.

    A stray empty string in ``members``/``admins``/reserves (common when a
    Planfix scenario interpolates a missing field) must not crash the create —
    it is silently skipped rather than passed down to the backend.
    """
    return [item for item in items if item is not None and str(item).strip()]


def _dedupe(items: Sequence[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        out.append(item)
    return out


async def _execute_create(
    *,
    backend: GroupBackend,
    folder_backend: FolderBackend | None,
    request: GroupCreateRequest,
    config: TelegramConfig,
    plugins: PluginRegistry,
) -> GroupCreateResult:
    enable_topics = (
        request.enable_topics
        if request.enable_topics is not None
        else config.defaults.enable_topics
    )
    create_link = (
        request.create_invite_link
        if request.create_invite_link is not None
        else config.defaults.create_invite_link
    )

    # A plugin-provided postfix is a presentation concern only: it lands on the
    # Telegram title but never enters the idempotency key (keyed on raw
    # request.title), so replays of the same external_ref still match.
    effective_title = f"{request.title}{plugins.title_postfix()}"

    chat_id = await backend.create_supergroup(
        title=effective_title,
        about=request.about,
        enable_topics=enable_topics,
    )

    skipped: list[dict[str, Any]] = []

    # Open up default member permissions right after creation so ordinary
    # members can create topics and pin messages. Best-effort: a FLOOD_WAIT
    # still surfaces as needs_review, but any other failure must not abort the
    # create (the chat already exists).
    perms = config.defaults.default_member_permissions
    try:
        await backend.set_default_permissions(
            chat_id=chat_id,
            allow_create_topics=perms.create_topics,
            allow_pin_messages=perms.pin_messages,
        )
    except FloodWaitError:
        raise
    except Exception as exc:
        logger.warning(
            "post-create set_default_permissions failed for chat %s: %s",
            chat_id,
            exc,
        )
        skipped.append(
            {"step": "set_default_permissions", "reason": str(exc)}
        )

    if enable_topics:
        layout = request.topics_layout or config.defaults.topics_layout
        try:
            await backend.set_topics_layout(
                chat_id=chat_id, tabs=_layout_to_tabs(layout)
            )
        except FloodWaitError:
            # FLOOD_WAIT on a soft-preference call still means Telegram is
            # throttling this account — let the caller promote it to
            # needs_review like every other in-flight throttle.
            raise
        except Exception as exc:
            # The chat is already created and the layout default is a soft
            # preference — don't fail the create. The operator can retry via
            # `groups set-layout` later, which carries its own idempotency row.
            logger.warning(
                "post-create set_topics_layout failed for chat %s (layout=%s): %s",
                chat_id,
                layout,
                exc,
            )

    reserve_admins = _resolved_reserves(
        request.reserve_admins,
        fallback=config.reserve_admins,
        skip=request.skip_reserve,
    )
    reserve_members = _resolved_reserves(
        request.reserve_members,
        fallback=config.reserve_members,
        skip=request.skip_reserve,
    )

    # Build the ordered population plan, deduping users that appear in multiple
    # buckets so we never invite the same handle twice.
    all_members = _dedupe(
        _drop_blank(
            [*request.members, *reserve_members, *request.admins, *reserve_admins]
        )
    )
    members_added: list[str] = []
    for user in all_members:
        try:
            await backend.add_member(chat_id=chat_id, user=user)
        except FloodWaitError:
            # FLOOD_WAIT during population must surface as needs_review (via
            # the caller's broad handler), not get silently dropped into
            # `skipped`. Recording it as "skipped" hides a transient throttle
            # behind a successful-looking response.
            raise
        except Exception as exc:
            skipped.append(
                {"step": "add_member", "user": user, "reason": str(exc)}
            )
            continue
        members_added.append(user)

    admins_promoted: list[str] = []
    for admin in _dedupe(_drop_blank([*request.admins, *reserve_admins])):
        if admin not in members_added:
            # We never managed to add this user, so promoting them would
            # certainly fail. Record the skip and move on.
            skipped.append(
                {"step": "promote_admin", "user": admin, "reason": "not_in_chat"}
            )
            continue
        try:
            await backend.promote_admin(chat_id=chat_id, user=admin)
        except FloodWaitError:
            raise
        except Exception as exc:
            skipped.append(
                {"step": "promote_admin", "user": admin, "reason": str(exc)}
            )
            continue
        admins_promoted.append(admin)

    invite_link: str | None = None
    if create_link:
        try:
            invite_link = await backend.create_invite_link(chat_id=chat_id)
        except FloodWaitError:
            raise
        except Exception as exc:
            skipped.append(
                {"step": "create_invite_link", "reason": str(exc)}
            )

    folder_id: int | None = None
    folder_name: str | None = None
    if not request.skip_folder:
        target_folder_name = (
            request.folder_name or config.default_chat_folder.folder_name
        )
        target_folder_id = (
            request.folder_id
            if request.folder_id is not None
            else config.default_chat_folder.folder_id
        )
        # The supergroup was already created above, so any folder-related
        # failure (missing folder, mid-mutation error, missing backend)
        # leaves a half-applied change on Telegram's side — surface it as
        # needs_review so the operator can finish placing the chat by hand.
        if folder_backend is None:
            raise FolderPeerFailureError(
                f"folder backend is unavailable; group {chat_id} was created but "
                f"could not be placed into folder {target_folder_name!r}"
            )
        try:
            snapshot = await resolve_folder(
                folder_backend,
                folder_name=target_folder_name,
                folder_id=target_folder_id,
            )
            await folder_backend.add_chat_to_folder(snapshot.folder_id, chat_id)
        except FolderPeerFailureError:
            raise
        except FolderError as exc:
            raise FolderPeerFailureError(
                f"group {chat_id} was created but folder placement failed: {exc}"
            ) from exc
        except Exception as exc:
            raise FolderPeerFailureError(
                f"failed to add chat {chat_id} to folder {target_folder_name!r}: "
                f"{exc}"
            ) from exc
        folder_id = snapshot.folder_id
        folder_name = snapshot.folder_name

    # Delegate post-create side effects (service messages, welcome cleanup) to
    # the active plugins. With no plugin enabled this is a no-op and the core
    # stays integration-free. Best-effort: failures land in `skipped`.
    task_message_sent = await plugins.after_group_create(
        backend=backend,
        chat_id=chat_id,
        external_ref=request.external_ref,
        members_added=members_added,
        skipped=skipped,
    )

    return GroupCreateResult(
        telegram_chat_id=chat_id,
        title=effective_title,
        external_ref=request.external_ref,
        invite_link=invite_link,
        folder_id=folder_id,
        folder_name=folder_name,
        topics_enabled=enable_topics,
        admins_added=list(request.admins),
        members_added=members_added,
        admins_promoted=admins_promoted,
        skipped=skipped,
        task_message_sent=task_message_sent,
    )


async def create_group(
    *,
    backend: GroupBackend,
    folder_backend: FolderBackend | None,
    store: OperationStore,
    config: TelegramConfig,
    request: GroupCreateRequest,
    plugins: PluginRegistry | None = None,
) -> tuple[GroupCreateResult, OperationRecord]:
    """Create a group, or replay the saved result for the same idempotency key.

    The state machine lives in :class:`OperationStore`; this function only
    drives the transitions:

    * `completed` → return the saved result with ``replayed=True``
    * `failed` → raise :class:`GroupCreateFailed`
    * `needs_review` → raise :class:`GroupCreateNeedsReview`
    * `pending` (from a parallel call) → raise :class:`GroupCreatePending`
    * new row → run the live workflow, transition to `completed`/`failed`/
      `needs_review` depending on the outcome
    """
    if plugins is None:
        plugins = PluginRegistry()
    if not request.title.strip() and request.external_ref is None:
        raise ValueError("group create requires external_ref or non-empty title")

    key = idempotency.group_create_key(
        external_ref=request.external_ref, title=request.title
    )
    begin = store.begin_operation(
        operation_type=idempotency.GROUP_CREATE,
        idempotency_key=key,
        request_payload=request.to_payload(),
    )

    if not begin.created:
        op = begin.operation
        if op.status is OperationStatus.COMPLETED:
            payload = op.result_payload or {}
            saved_chat_id = payload.get("telegram_chat_id")
            # Before replaying, confirm the saved chat still exists on Telegram.
            # If it was manually deleted out-of-band, the saved result points at
            # a dead chat — drop the stale operation and fall through to a fresh
            # create instead of handing back a chat id that no longer resolves.
            try:
                still_exists = (
                    saved_chat_id is not None
                    and await backend.chat_exists(chat_id=int(saved_chat_id))
                )
            except FloodWaitError as exc:
                # A throttle during the existence check is ambiguous — we don't
                # know whether the chat is gone. Surface needs_review rather than
                # silently re-creating a chat that may still be alive.
                raise GroupCreateNeedsReview(
                    f"FLOOD_WAIT while verifying chat {saved_chat_id} exists: {exc}"
                ) from exc
            if still_exists:
                return GroupCreateResult.from_dict(payload), op
            logger.warning(
                "saved chat %s for operation %s no longer exists; dropping the "
                "stale operation and re-creating the group",
                saved_chat_id,
                op.id,
            )
            store.delete_operation(op.id)
            begin = store.begin_operation(
                operation_type=idempotency.GROUP_CREATE,
                idempotency_key=key,
                request_payload=request.to_payload(),
            )
        elif op.status is OperationStatus.FAILED:
            raise GroupCreateFailed(op.error or "previous attempt failed")
        elif op.status is OperationStatus.NEEDS_REVIEW:
            raise GroupCreateNeedsReview(
                op.error or "previous attempt needs review"
            )
        else:
            # pending: another request is already in flight (or a previous run
            # crashed before transitioning). We don't auto-retry from this
            # surface; callers can `operations retry`.
            raise GroupCreatePending(
                f"operation {op.id} is still pending; retry via 'operations retry'"
            )

    operation_id = begin.operation.id
    try:
        result = await _execute_create(
            backend=backend,
            folder_backend=folder_backend,
            request=request,
            config=config,
            plugins=plugins,
        )
    except FolderPeerFailureError as exc:
        op = store.mark_needs_review(operation_id, str(exc))
        raise GroupCreateNeedsReview(str(exc)) from exc
    except FloodWaitError as exc:
        # The supergroup is already live on Telegram by the time member
        # population hits FLOOD_WAIT. Marking the operation `failed` would
        # leave a half-populated chat with no clear path forward; promote it
        # to `needs_review` so the operator can finish placement by hand.
        op = store.mark_needs_review(operation_id, f"FLOOD_WAIT during group population: {exc}")
        raise GroupCreateNeedsReview(str(exc)) from exc
    except GroupError:
        raise
    except Exception as exc:
        store.fail_operation(operation_id, str(exc))
        raise

    op = store.complete_operation(operation_id, result.to_dict())
    return result, op


# ---------------------------------------------------------------------------
# Topics layout (list / tabs)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LayoutSetRequest:
    """Input shape for :func:`set_topics_layout`."""

    telegram_chat_id: int
    layout: TopicsLayout

    def to_payload(self) -> dict[str, Any]:
        return {
            "telegram_chat_id": self.telegram_chat_id,
            "layout": self.layout,
        }


@dataclass(frozen=True)
class LayoutSetResult:
    """Result returned by :func:`set_topics_layout` and its replay path."""

    telegram_chat_id: int
    layout: TopicsLayout
    replayed: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "telegram_chat_id": self.telegram_chat_id,
            "layout": self.layout,
            "replayed": self.replayed,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> LayoutSetResult:
        layout = payload.get("layout", "list")
        if layout not in ("list", "tabs"):
            raise ValueError(f"unknown layout {layout!r} in saved payload")
        return cls(
            telegram_chat_id=int(payload["telegram_chat_id"]),
            layout=layout,
            replayed=True,
        )


async def set_topics_layout(
    *,
    backend: GroupBackend,
    store: OperationStore,
    request: LayoutSetRequest,
) -> tuple[LayoutSetResult, OperationRecord]:
    """Set the topics layout for an existing forum chat, idempotently.

    Idempotency key: ``group_layout_set:{chat_id}:{layout}``. Re-setting the
    same layout twice replays the completed operation without touching
    Telegram. Switching layouts (`list` <-> `tabs`) creates a new operation
    row keyed under the new layout value, so each direction has its own
    history.
    """
    if request.layout not in ("list", "tabs"):
        raise ValueError(
            f"set_topics_layout layout must be 'list' or 'tabs', got {request.layout!r}"
        )

    key = idempotency.group_layout_set_key(
        telegram_chat_id=request.telegram_chat_id,
        layout=request.layout,
    )
    begin = store.begin_operation(
        operation_type=idempotency.GROUP_LAYOUT_SET,
        idempotency_key=key,
        request_payload=request.to_payload(),
    )

    if not begin.created:
        op = begin.operation
        if op.status is OperationStatus.COMPLETED:
            payload = op.result_payload or {}
            return LayoutSetResult.from_dict(payload), op
        if op.status is OperationStatus.FAILED:
            raise GroupLayoutSetFailed(op.error or "previous attempt failed")
        if op.status is OperationStatus.NEEDS_REVIEW:
            raise GroupLayoutSetNeedsReview(
                op.error or "previous attempt needs review"
            )
        raise GroupLayoutSetPending(
            f"operation {op.id} is still pending; retry via 'operations retry'"
        )

    operation_id = begin.operation.id
    try:
        await backend.set_topics_layout(
            chat_id=request.telegram_chat_id,
            tabs=_layout_to_tabs(request.layout),
        )
    except FloodWaitError as exc:
        store.mark_needs_review(
            operation_id, f"FLOOD_WAIT during set_topics_layout: {exc}"
        )
        raise GroupLayoutSetNeedsReview(str(exc)) from exc
    except GroupError:
        raise
    except Exception as exc:
        store.fail_operation(operation_id, str(exc))
        raise

    result = LayoutSetResult(
        telegram_chat_id=request.telegram_chat_id,
        layout=request.layout,
    )
    op = store.complete_operation(operation_id, result.to_dict())
    return result, op


async def get_topics_layout(
    *,
    backend: GroupBackend,
    telegram_chat_id: int,
) -> TopicsLayout:
    """Return the current topics layout for ``telegram_chat_id``.

    Pure read — does not touch :class:`OperationStore`.
    """
    tabs = await backend.get_topics_layout(chat_id=telegram_chat_id)
    return _tabs_to_layout(bool(tabs))
