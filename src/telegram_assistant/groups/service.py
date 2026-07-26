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

from telegram_assistant.access.service import AccessLevel, Authorizer
from telegram_assistant.config.models import TelegramConfig, TopicsLayout
from telegram_assistant.folders.service import (
    FolderBackend,
    FolderError,
    FolderPeerFailureError,
    resolve_folder,
)
from telegram_assistant.groups.answers import answer, normalize_lang
from telegram_assistant.members.service import looks_like_phone, normalize_phone
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


class GroupRenameFailed(GroupError):
    """A previous rename attempt with this idempotency key already failed."""


class GroupRenamePending(GroupError):
    """A concurrent rename attempt with this idempotency key is in flight."""


class GroupRenameNeedsReview(GroupError):
    """A previous rename attempt resulted in needs_review."""


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
class ContactSpec:
    """A user identified by phone + name, imported to contacts before adding.

    A bare Telegram user id only resolves once the account has seen the user.
    Supplying ``phone`` + ``name`` lets group create import the user into the
    account's Telegram contacts first (which caches them), so the subsequent
    chat-add resolves. ``phone`` is normalised via
    :func:`telegram_assistant.members.service.normalize_phone`.
    """

    phone: str
    name: str

    def to_payload(self) -> dict[str, Any]:
        return {"phone": self.phone, "name": self.name}


@dataclass(frozen=True)
class GroupCreateRequest:
    """Input shape shared by HTTP, CLI, and tests."""

    title: str
    external_ref: int | str | None = None
    about: str | None = None
    admins: Sequence[str] = ()
    members: Sequence[str] = ()
    managers: Sequence[str] = ()
    contacts: Sequence[ContactSpec] = ()
    reserve_admins: Sequence[str] | None = None
    reserve_members: Sequence[str] | None = None
    skip_reserve: bool = False
    enable_topics: bool | None = None
    topics_layout: TopicsLayout | None = None
    create_invite_link: bool | None = None
    folder_name: str | None = None
    folder_id: int | None = None
    skip_folder: bool = False
    lang: str | None = None
    telegram_id: int | str | None = None

    def to_payload(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "external_ref": self.external_ref,
            "about": self.about,
            "admins": list(self.admins),
            "members": list(self.members),
            "managers": list(self.managers),
            "contacts": [c.to_payload() for c in self.contacts],
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
            "lang": self.lang,
            "telegram_id": self.telegram_id,
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
    contacts_imported: list[dict[str, Any]] = field(default_factory=list)
    skipped: list[dict[str, Any]] = field(default_factory=list)
    task_message_sent: bool = False
    replayed: bool = False
    answer: str = ""

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
            "contacts_imported": list(self.contacts_imported),
            "skipped": list(self.skipped),
            "task_message_sent": self.task_message_sent,
            "replayed": self.replayed,
            "answer": self.answer,
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
            contacts_imported=list(payload.get("contacts_imported") or []),
            skipped=list(payload.get("skipped") or []),
            task_message_sent=bool(payload.get("task_message_sent", False)),
            replayed=True,
            answer=str(payload.get("answer") or ""),
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

    async def import_contact(
        self, *, phone: str, first_name: str, last_name: str = ""
    ) -> int | None:
        """Import a phone contact; return the resolved Telegram user id.

        Returns ``None`` when the phone has no associated Telegram account
        (nothing to add). Importing caches the user so a subsequent
        ``add_member`` by numeric id resolves.
        """
        ...

    async def promote_admin(self, *, chat_id: int, user: str) -> None:
        ...

    async def create_invite_link(self, *, chat_id: int) -> str:
        ...

    async def send_message(self, *, chat_id: int, text: str) -> int:
        ...

    async def set_topics_layout(self, *, chat_id: int, tabs: bool) -> None:
        ...

    async def set_title(self, *, chat_id: int, title: str) -> None:
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


def _resolve_client_telegram_id(value: int | str | None) -> str | None:
    """Return the client's Telegram id as a canonical string, or ``None``.

    The Planfix integration sends ``telegram_id`` as ``"0"`` (often wrapped as
    ``["0"]``) to mean "no telegram id". A valid Telegram user id is a positive
    integer, so treat ``0``, negative, blank, or non-numeric values as
    **absent** — this is what makes the phone-without-telegram_id warning fire
    instead of trying to add user ``0``.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        numeric = int(text)
    except ValueError:
        return None
    if numeric <= 0:
        return None
    return str(numeric)


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

    # Normalise contact phones up front: a malformed phone is bad input, so it
    # must abort before any supergroup is created (not leave a half-built chat).
    normalized_contacts: list[tuple[str, str]] = [
        (normalize_phone(c.phone), c.name) for c in request.contacts
    ]

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

    # Import phone+name contacts before population. A successful import caches
    # the user and yields a numeric id, which is folded into the member list so
    # the normal add loop adds them. A phone with no Telegram account (or an
    # import error) is recorded and skipped — the group still gets created.
    contacts_imported: list[dict[str, Any]] = []
    contact_user_ids: list[str] = []
    for phone, name in normalized_contacts:
        record: dict[str, Any] = {"phone": phone, "name": name, "user_id": None}
        try:
            user_id = await backend.import_contact(
                phone=phone, first_name=name, last_name=""
            )
        except FloodWaitError:
            raise
        except Exception as exc:
            record["status"] = "error"
            record["reason"] = str(exc)
            contacts_imported.append(record)
            skipped.append(
                {"step": "import_contact", "phone": phone, "reason": str(exc)}
            )
            continue
        if user_id is None:
            record["status"] = "no_account"
            contacts_imported.append(record)
            skipped.append(
                {
                    "step": "import_contact",
                    "phone": phone,
                    "reason": "no_telegram_account",
                }
            )
            continue
        record["status"] = "imported"
        record["user_id"] = user_id
        contacts_imported.append(record)
        contact_user_ids.append(str(user_id))

    # Resolve how the client member (``members[0]``) is connected. When it is a
    # phone-style reference the client cannot be added by phone alone:
    #   * with a ``telegram_id`` filled, substitute the numeric id so the normal
    #     add loop connects the client → "client added" answer;
    #   * without one, drop the client member (the group is still created) and
    #     record the skip → phone-without-telegram_id warning answer.
    # A non-phone client (e.g. ``@user``) is left untouched → plain "created".
    effective_members = list(request.members)
    answer_key = "group_created"
    substituted_client_id: str | None = None
    if effective_members and looks_like_phone(str(effective_members[0])):
        client_ref = effective_members[0]
        telegram_id = _resolve_client_telegram_id(request.telegram_id)
        if telegram_id:
            effective_members[0] = telegram_id
            substituted_client_id = telegram_id
            answer_key = "group_created_client_added"
        else:
            effective_members = effective_members[1:]
            skipped.append(
                {
                    "step": "client_invite",
                    "user": client_ref,
                    "reason": "phone_without_telegram_id",
                }
            )
            answer_key = "client_phone_no_telegram_id"

    # Decision log to diagnose the phone-without-telegram_id warning in the live
    # integration: shows whether the first member was seen as a phone, whether a
    # telegram_id was supplied, and which answer branch was chosen.
    first_member = str(request.members[0]) if request.members else None
    logger.info(
        "group create client-member decision: members=%d first=%r "
        "is_phone=%s telegram_id_present=%s answer_key=%s",
        len(request.members),
        first_member,
        bool(first_member and looks_like_phone(first_member)),
        _resolve_client_telegram_id(request.telegram_id) is not None,
        answer_key,
    )

    # Build the ordered population plan, deduping users that appear in multiple
    # buckets so we never invite the same handle twice. Imported contacts go
    # first so their resolved ids are added before the named handles.
    all_members = _dedupe(
        _drop_blank(
            [
                *contact_user_ids,
                *effective_members,
                *request.managers,
                *reserve_members,
                *request.admins,
                *reserve_admins,
            ]
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

    # We optimistically set "client added" when substituting the numeric id.
    # If that add did not land (privacy restriction, invalid id), don't claim
    # the client was added — the failed add is already recorded in `skipped`.
    if (
        answer_key == "group_created_client_added"
        and substituted_client_id not in members_added
    ):
        answer_key = "group_created"

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

    answer_text = answer(
        normalize_lang(request.lang), answer_key, title=effective_title
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
        contacts_imported=contacts_imported,
        skipped=skipped,
        task_message_sent=task_message_sent,
        answer=answer_text,
    )


async def create_group(
    *,
    backend: GroupBackend,
    folder_backend: FolderBackend | None,
    store: OperationStore,
    config: TelegramConfig,
    request: GroupCreateRequest,
    plugins: PluginRegistry | None = None,
    authorizer: Authorizer | None = None,
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

    # Validate contact phones up front: a malformed phone is bad input and must
    # fail before we create an operation row or touch Telegram.
    for contact in request.contacts:
        normalize_phone(contact.phone)

    # Group create is gated by WRITE on the destination folder (the place the
    # new chat will land). Checked up front so a denied create never reaches the
    # operation store or Telegram. ``skip_folder`` creates an unplaced chat with
    # no destination to gate on.
    if authorizer is not None and not request.skip_folder:
        target_folder_name = (
            request.folder_name or config.default_chat_folder.folder_name
        )
        # The destination folder id (when known) lets a ``folder_id`` rule gate
        # the create as precisely as a name rule. The configured default id only
        # applies when the request did not name a different folder — otherwise
        # it would describe a folder the chat is not headed for.
        target_folder_id = request.folder_id
        if target_folder_id is None and request.folder_name is None:
            target_folder_id = config.default_chat_folder.folder_id
        await authorizer.require_folder(
            target_folder_name, AccessLevel.WRITE, folder_id=target_folder_id
        )

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
                # Replaying a prior completed operation (idempotency key =
                # planfix_task_id or title). The answer/skipped come from the
                # stored result, NOT from a fresh phone/telegram_id evaluation —
                # a row created before the answer field has answer="".
                logger.info(
                    "group create replaying completed operation %s (key=%r); "
                    "returning stored answer=%r",
                    op.id,
                    op.idempotency_key,
                    payload.get("answer", ""),
                )
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

    client_unconnected = any(
        entry.get("step") == "client_invite"
        and entry.get("reason") == "phone_without_telegram_id"
        for entry in result.skipped
    )
    if client_unconnected:
        # Product decision: the phone-without-telegram_id warning is NOT cached.
        # The client could not be connected and the warning asks the operator to
        # fill telegram_id and resend the SAME task — so we drop the operation
        # row to keep the idempotency key free for that retry instead of
        # replaying the warning forever. (The group was still created; a retry
        # without telegram_id simply warns again — accepted trade-off.)
        logger.info(
            "group create not caching phone-without-telegram_id outcome for "
            "operation %s (key=%r); idempotency key left free for retry",
            operation_id,
            begin.operation.idempotency_key,
        )
        store.delete_operation(operation_id)
        return result, begin.operation

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


# ---------------------------------------------------------------------------
# Group rename
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GroupRenameRequest:
    """Input shape for :func:`rename_group`."""

    telegram_chat_id: int
    new_title: str
    reason: str | None = None

    def to_payload(self) -> dict[str, Any]:
        return {
            "telegram_chat_id": self.telegram_chat_id,
            "new_title": self.new_title,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class GroupRenameResult:
    """Result returned by both the live execution and a replay."""

    telegram_chat_id: int
    old_title: str | None
    new_title: str
    status: str = "renamed"
    replayed: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "telegram_chat_id": self.telegram_chat_id,
            "old_title": self.old_title,
            "new_title": self.new_title,
            "status": self.status,
            "replayed": self.replayed,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> GroupRenameResult:
        return cls(
            telegram_chat_id=int(payload["telegram_chat_id"]),
            old_title=payload.get("old_title"),
            new_title=str(payload["new_title"]),
            status=str(payload.get("status", "renamed")),
            replayed=True,
        )


async def rename_group(
    *,
    backend: GroupBackend,
    store: OperationStore,
    request: GroupRenameRequest,
    authorizer: Authorizer | None = None,
) -> tuple[GroupRenameResult, OperationRecord]:
    """Rename an existing supergroup, or replay the saved result for the same key.

    Idempotency key: ``group_rename:chat={id}:title={new_title}``. Re-running the
    same rename replays the completed operation without touching Telegram; a
    different target title is a fresh operation keyed under the new title.

    State machine transitions mirror :func:`set_topics_layout`:

    * `completed`    → return saved result with ``replayed=True``
    * `failed`       → raise :class:`GroupRenameFailed`
    * `needs_review` → raise :class:`GroupRenameNeedsReview`
    * `pending`      → raise :class:`GroupRenamePending`
    """
    if not request.new_title.strip():
        raise ValueError("rename_group requires a non-empty new_title")

    # Renaming a group is a WRITE op on the chat itself.
    if authorizer is not None:
        await authorizer.require(request.telegram_chat_id, AccessLevel.WRITE)

    key = idempotency.group_rename_key(
        telegram_chat_id=request.telegram_chat_id,
        new_title=request.new_title,
    )
    begin = store.begin_operation(
        operation_type=idempotency.GROUP_RENAME,
        idempotency_key=key,
        request_payload=request.to_payload(),
    )

    if not begin.created:
        op = begin.operation
        if op.status is OperationStatus.COMPLETED:
            payload = op.result_payload or {}
            return GroupRenameResult.from_dict(payload), op
        if op.status is OperationStatus.FAILED:
            raise GroupRenameFailed(op.error or "previous attempt failed")
        if op.status is OperationStatus.NEEDS_REVIEW:
            raise GroupRenameNeedsReview(op.error or "previous attempt needs review")
        raise GroupRenamePending(
            f"operation {op.id} is still pending; retry via 'operations retry'"
        )

    operation_id = begin.operation.id
    try:
        await backend.set_title(
            chat_id=request.telegram_chat_id,
            title=request.new_title.strip(),
        )
    except FloodWaitError as exc:
        # FLOOD_WAIT must not lock the idempotency key into a terminal failure —
        # promote to needs_review so an operator can `operations retry`.
        store.mark_needs_review(
            operation_id, f"FLOOD_WAIT during group rename: {exc}"
        )
        raise GroupRenameNeedsReview(str(exc)) from exc
    except GroupError:
        raise
    except Exception as exc:
        store.fail_operation(operation_id, str(exc))
        raise

    result = GroupRenameResult(
        telegram_chat_id=request.telegram_chat_id,
        old_title=None,
        new_title=request.new_title.strip(),
        status="renamed",
    )
    op = store.complete_operation(operation_id, result.to_dict())
    return result, op
