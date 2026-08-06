"""Read-only chat metadata — the inspect op of the chats domain.

A READ op in the shape of :mod:`telegram_assistant.members.listing`: no
operation row, no idempotency key, no ``--dry-run``. It answers "what is this
chat" for every peer kind with one flat payload, so a caller can read
``ttl_period`` without knowing whether the target is a supergroup or a private
chat.

The payload is a *curated* set rather than a dump of Telethon's ``*Full``
objects: those carry 60+ fields that move with the Telegram layer, and pinning
tests to them would turn a Telethon upgrade into a test failure. ``raw`` is the
escape hatch for everything left out.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Protocol

from telegram_assistant.access.service import AccessLevel, Authorizer

#: Peer kinds ``ChatInfo.kind`` may report.
CHAT_KINDS: frozenset[str] = frozenset(
    {"user", "bot", "basic_group", "supergroup", "channel"}
)


@dataclass(frozen=True)
class ChatInfo:
    """One chat's metadata, flat and kind-agnostic.

    Every field exists for every kind; the ones Telegram does not answer for a
    given peer are ``None`` (or ``False`` for flags). That is deliberate — a
    caller running ``jq .ttl_period`` must not have to branch on ``kind``.
    """

    # --- identity (all kinds) ---
    chat_id: int
    kind: str
    title: str | None = None
    username: str | None = None
    usernames: tuple[str, ...] = ()
    about: str | None = None
    created_at: datetime | None = None

    # --- the reason this op exists, plus the settings next to it ---
    ttl_period: int | None = None
    pinned_message_id: int | None = None
    archived: bool = False
    #: Notifications suppressed *right now* — i.e. ``muted_until`` is in the
    #: future. An expired mute, and the ``mute_until = 0`` Telegram writes for
    #: an unmute, both report ``False`` with ``muted_until`` ``None``.
    muted: bool = False
    muted_until: datetime | None = None
    #: The separate ``silent`` notify flag: the notification's *sound* is off,
    #: which is not the same thing as the chat being muted — hence its own
    #: field rather than being folded into ``muted``.
    silent: bool = False
    has_scheduled: bool = False

    # --- trust / restrictions (all kinds) ---
    restricted: bool = False
    restriction_reason: tuple[dict[str, Any], ...] = ()
    verified: bool = False
    scam: bool = False
    fake: bool = False

    # --- our standing (all kinds) ---
    is_creator: bool = False
    left: bool = False
    invite_link: str | None = None
    my_admin_rights: dict[str, Any] | None = None
    default_banned_rights: dict[str, Any] | None = None

    # --- groups and channels ---
    is_forum: bool = False
    topics_layout: str | None = None
    broadcast: bool = False
    megagroup: bool = False
    gigagroup: bool = False
    participants_count: int | None = None
    admins_count: int | None = None
    kicked_count: int | None = None
    banned_count: int | None = None
    online_count: int | None = None
    slowmode_seconds: int | None = None
    slowmode_next_send_date: datetime | None = None
    linked_chat_id: int | None = None
    migrated_from_chat_id: int | None = None
    migrated_to_chat_id: int | None = None
    deactivated: bool = False
    hidden_prehistory: bool = False
    participants_hidden: bool = False
    antispam: bool = False
    can_view_participants: bool = False
    can_view_stats: bool = False
    can_delete_channel: bool = False
    can_set_username: bool = False
    join_to_send: bool = False
    join_request: bool = False
    requests_pending: int | None = None
    noforwards: bool = False
    unread_count: int | None = None
    available_reactions: Any = None
    reactions_limit: int | None = None
    call_active: bool = False

    # --- users and bots ---
    first_name: str | None = None
    last_name: str | None = None
    phone: str | None = None
    is_bot: bool = False
    is_deleted: bool = False
    is_premium: bool = False
    is_contact: bool = False
    is_mutual_contact: bool = False
    blocked: bool = False
    common_chats_count: int | None = None
    birthday: dict[str, Any] | None = None
    personal_channel_id: int | None = None
    last_seen_status: str | None = None

    # --- escape hatch ---
    raw: dict[str, Any] | None = field(default=None)

    def to_dict(self) -> dict[str, Any]:
        """The payload body. ``raw`` appears only when it was requested."""
        payload = asdict(self)
        payload["usernames"] = list(self.usernames)
        payload["restriction_reason"] = list(self.restriction_reason)
        if self.raw is None:
            payload.pop("raw")
        return payload


class ChatInspectBackend(Protocol):
    """Telethon-facing surface needed to read one chat's metadata.

    Production wires this to
    :class:`telegram_assistant.chats.telethon_backend.TelethonChatInspectBackend`;
    tests inject a fake.
    """

    async def inspect_chat(self, *, chat_id: int, raw: bool) -> ChatInfo:
        ...


async def inspect_chat(
    *,
    backend: ChatInspectBackend,
    chat_id: int,
    raw: bool = False,
    authorizer: Authorizer | None = None,
) -> ChatInfo:
    """Read ``chat_id``'s metadata.

    A READ op: when an ``authorizer`` is supplied it must grant READ on the
    chat, checked before any Telegram call. The payload carries the
    description, the member counts and the invite link, so a denied caller must
    cost no round trip and learn nothing about the chat.
    """
    if authorizer is not None:
        await authorizer.require(chat_id, AccessLevel.READ)

    return await backend.inspect_chat(chat_id=chat_id, raw=raw)


__all__ = [
    "CHAT_KINDS",
    "ChatInfo",
    "ChatInspectBackend",
    "inspect_chat",
]
