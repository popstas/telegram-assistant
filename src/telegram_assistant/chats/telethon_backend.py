"""Telethon adapter for the chat-inspect op.

Two RPCs at most: ``get_input_entity`` to learn the peer kind, then one
``GetFull*`` request. The shallow half of the answer (``forum_tabs``,
``restriction_reason``, the rights defaults) is read out of that response's own
``chats``/``users`` list rather than a second ``get_entity`` — ``forum_tabs``
is a flag on ``Channel``, not on ``ChannelFull``, which is why
``groups/telethon_backend.py::get_topics_layout`` already resolves it that way.
"""

from __future__ import annotations

from typing import Any

from telegram_assistant.chats.service import ChatInfo
from telegram_assistant.telegram_client.errors import translate_flood_wait

#: Never leaves the process: a peer credential, not metadata.
_REDACTED_RAW_KEYS = frozenset({"access_hash"})


def _rights(raw: Any) -> dict[str, Any] | None:
    """Flag dict for ChatAdminRights / ChatBannedRights, minus the type tag."""
    if raw is None:
        return None
    to_dict = getattr(raw, "to_dict", None)
    if to_dict is None:
        return None
    return {k: v for k, v in to_dict().items() if k != "_"}


def _usernames(raw: Any) -> tuple[str, ...]:
    """Active alternative usernames only — an inactive one is not reachable."""
    return tuple(
        str(u.username)
        for u in (raw or ())
        if getattr(u, "username", None) and getattr(u, "active", True)
    )


def _restriction_reasons(raw: Any) -> tuple[dict[str, Any], ...]:
    return tuple(
        {
            "platform": getattr(r, "platform", None),
            "reason": getattr(r, "reason", None),
            "text": getattr(r, "text", None),
        }
        for r in (raw or ())
    )


def _reactions(raw: Any) -> Any:
    """``"all"``, ``"none"``, or the list of allowed emoticons."""
    if raw is None:
        return None
    name = type(raw).__name__
    if name == "ChatReactionsAll":
        return "all"
    if name == "ChatReactionsNone":
        return "none"
    return [
        getattr(r, "emoticon", None) or getattr(r, "document_id", None)
        for r in (getattr(raw, "reactions", ()) or ())
    ]


def _birthday(raw: Any) -> dict[str, Any] | None:
    if raw is None:
        return None
    return {
        "day": getattr(raw, "day", None),
        "month": getattr(raw, "month", None),
        "year": getattr(raw, "year", None),
    }


def _peer_id(raw: Any) -> int | None:
    """Read the numeric id off any InputPeer/Peer shape."""
    for attr in ("channel_id", "chat_id", "user_id"):
        value = getattr(raw, attr, None)
        if value is not None:
            return int(value)
    return None


def _serialize(raw: Any) -> dict[str, Any] | None:
    if raw is None:
        return None
    to_dict = getattr(raw, "to_dict", None)
    if to_dict is not None:
        payload = to_dict()
    else:
        payload = {
            k: v for k, v in vars(raw).items() if not k.startswith("_")
        }
    return {k: v for k, v in payload.items() if k not in _REDACTED_RAW_KEYS}


def _shallow_for(result: Any, full: Any, bucket: str) -> Any:
    """The shallow object matching ``full.id`` in ``result.<bucket>``.

    Telegram returns the peer alongside its Full object; match by id and fall
    back to the only entry when it returned just one.
    """
    items = list(getattr(result, bucket, None) or [])
    target_id = int(getattr(full, "id", 0) or 0)
    match = next((i for i in items if int(getattr(i, "id", 0) or 0) == target_id), None)
    if match is None and items:
        return items[0]
    return match


class TelethonChatInspectBackend:
    """Adapter from the Telethon ``TelegramClient`` to ``ChatInspectBackend``."""

    def __init__(self, client: Any) -> None:
        self._client = client

    async def inspect_chat(self, *, chat_id: int, raw: bool) -> ChatInfo:
        try:
            peer = await self._client.get_input_entity(chat_id)
        except Exception as exc:
            raise translate_flood_wait(exc) from exc

        kind = type(peer).__name__
        if kind == "InputPeerChannel":
            return await self._inspect_channel(peer, raw=raw)
        if kind == "InputPeerChat":
            return await self._inspect_basic_group(peer, raw=raw)
        if kind in {"InputPeerUser", "InputPeerSelf"}:
            return await self._inspect_user(peer, raw=raw)
        raise ValueError(f"chat {chat_id} cannot be inspected (resolved to {kind})")

    # --- per-kind branches --------------------------------------------------

    async def _inspect_channel(self, peer: Any, *, raw: bool) -> ChatInfo:
        from telethon.tl import functions

        try:
            result = await self._client(
                functions.channels.GetFullChannelRequest(channel=peer)
            )
        except Exception as exc:
            raise translate_flood_wait(exc) from exc

        full = getattr(result, "full_chat", None)
        entity = _shallow_for(result, full, "chats")
        broadcast = bool(getattr(entity, "broadcast", False))

        return ChatInfo(
            chat_id=int(getattr(full, "id", 0) or getattr(peer, "channel_id", 0)),
            kind="channel" if broadcast else "supergroup",
            title=getattr(entity, "title", None),
            username=getattr(entity, "username", None),
            usernames=_usernames(getattr(entity, "usernames", None)),
            about=getattr(full, "about", None) or None,
            created_at=getattr(entity, "date", None),
            ttl_period=getattr(full, "ttl_period", None),
            pinned_message_id=getattr(full, "pinned_msg_id", None),
            archived=getattr(full, "folder_id", None) == 1,
            muted=_is_muted(getattr(full, "notify_settings", None)),
            muted_until=getattr(
                getattr(full, "notify_settings", None), "mute_until", None
            ),
            has_scheduled=bool(getattr(full, "has_scheduled", False)),
            restricted=bool(getattr(entity, "restricted", False)),
            restriction_reason=_restriction_reasons(
                getattr(entity, "restriction_reason", None)
            ),
            verified=bool(getattr(entity, "verified", False)),
            scam=bool(getattr(entity, "scam", False)),
            fake=bool(getattr(entity, "fake", False)),
            is_creator=bool(getattr(entity, "creator", False)),
            left=bool(getattr(entity, "left", False)),
            invite_link=getattr(getattr(full, "exported_invite", None), "link", None),
            my_admin_rights=_rights(getattr(entity, "admin_rights", None)),
            default_banned_rights=_rights(
                getattr(entity, "default_banned_rights", None)
            ),
            is_forum=bool(getattr(entity, "forum", False)),
            topics_layout=(
                ("tabs" if getattr(entity, "forum_tabs", False) else "list")
                if getattr(entity, "forum", False)
                else None
            ),
            broadcast=broadcast,
            megagroup=bool(getattr(entity, "megagroup", False)),
            gigagroup=bool(getattr(entity, "gigagroup", False)),
            participants_count=getattr(full, "participants_count", None),
            admins_count=getattr(full, "admins_count", None),
            kicked_count=getattr(full, "kicked_count", None),
            banned_count=getattr(full, "banned_count", None),
            online_count=getattr(full, "online_count", None),
            slowmode_seconds=getattr(full, "slowmode_seconds", None),
            slowmode_next_send_date=getattr(full, "slowmode_next_send_date", None),
            linked_chat_id=getattr(full, "linked_chat_id", None),
            migrated_from_chat_id=getattr(full, "migrated_from_chat_id", None),
            hidden_prehistory=bool(getattr(full, "hidden_prehistory", False)),
            participants_hidden=bool(getattr(full, "participants_hidden", False)),
            antispam=bool(getattr(full, "antispam", False)),
            can_view_participants=bool(getattr(full, "can_view_participants", False)),
            can_view_stats=bool(getattr(full, "can_view_stats", False)),
            can_delete_channel=bool(getattr(full, "can_delete_channel", False)),
            can_set_username=bool(getattr(full, "can_set_username", False)),
            join_to_send=bool(getattr(entity, "join_to_send", False)),
            join_request=bool(getattr(entity, "join_request", False)),
            requests_pending=getattr(full, "requests_pending", None),
            noforwards=bool(getattr(entity, "noforwards", False)),
            unread_count=getattr(full, "unread_count", None),
            available_reactions=_reactions(getattr(full, "available_reactions", None)),
            reactions_limit=getattr(full, "reactions_limit", None),
            call_active=bool(getattr(entity, "call_active", False)),
            raw=(
                {"entity": _serialize(entity), "full": _serialize(full)}
                if raw
                else None
            ),
        )

    async def _inspect_basic_group(self, peer: Any, *, raw: bool) -> ChatInfo:
        from telethon.tl import functions

        try:
            result = await self._client(
                functions.messages.GetFullChatRequest(chat_id=peer.chat_id)
            )
        except Exception as exc:
            raise translate_flood_wait(exc) from exc

        full = getattr(result, "full_chat", None)
        entity = _shallow_for(result, full, "chats")

        return ChatInfo(
            chat_id=int(getattr(full, "id", 0) or getattr(peer, "chat_id", 0)),
            kind="basic_group",
            title=getattr(entity, "title", None),
            about=getattr(full, "about", None) or None,
            created_at=getattr(entity, "date", None),
            ttl_period=getattr(full, "ttl_period", None),
            pinned_message_id=getattr(full, "pinned_msg_id", None),
            archived=getattr(full, "folder_id", None) == 1,
            muted=_is_muted(getattr(full, "notify_settings", None)),
            muted_until=getattr(
                getattr(full, "notify_settings", None), "mute_until", None
            ),
            has_scheduled=bool(getattr(full, "has_scheduled", False)),
            is_creator=bool(getattr(entity, "creator", False)),
            left=bool(getattr(entity, "left", False)),
            invite_link=getattr(getattr(full, "exported_invite", None), "link", None),
            my_admin_rights=_rights(getattr(entity, "admin_rights", None)),
            default_banned_rights=_rights(
                getattr(entity, "default_banned_rights", None)
            ),
            participants_count=getattr(entity, "participants_count", None),
            deactivated=bool(getattr(entity, "deactivated", False)),
            migrated_to_chat_id=_peer_id(getattr(entity, "migrated_to", None)),
            can_set_username=bool(getattr(full, "can_set_username", False)),
            requests_pending=getattr(full, "requests_pending", None),
            noforwards=bool(getattr(entity, "noforwards", False)),
            available_reactions=_reactions(getattr(full, "available_reactions", None)),
            reactions_limit=getattr(full, "reactions_limit", None),
            call_active=bool(getattr(entity, "call_active", False)),
            raw=(
                {"entity": _serialize(entity), "full": _serialize(full)}
                if raw
                else None
            ),
        )

    async def _inspect_user(self, peer: Any, *, raw: bool) -> ChatInfo:
        from telethon.tl import functions

        try:
            result = await self._client(functions.users.GetFullUserRequest(id=peer))
        except Exception as exc:
            raise translate_flood_wait(exc) from exc

        full = getattr(result, "full_user", None)
        entity = _shallow_for(result, full, "users")
        first = getattr(entity, "first_name", None)
        last = getattr(entity, "last_name", None)
        title = " ".join(part for part in (first, last) if part) or None

        return ChatInfo(
            chat_id=int(getattr(full, "id", 0) or getattr(peer, "user_id", 0)),
            kind="bot" if getattr(entity, "bot", False) else "user",
            title=title,
            username=getattr(entity, "username", None),
            usernames=_usernames(getattr(entity, "usernames", None)),
            about=getattr(full, "about", None) or None,
            ttl_period=getattr(full, "ttl_period", None),
            pinned_message_id=getattr(full, "pinned_msg_id", None),
            archived=getattr(full, "folder_id", None) == 1,
            muted=_is_muted(getattr(full, "notify_settings", None)),
            muted_until=getattr(
                getattr(full, "notify_settings", None), "mute_until", None
            ),
            has_scheduled=bool(getattr(full, "has_scheduled", False)),
            restricted=bool(getattr(entity, "restricted", False)),
            restriction_reason=_restriction_reasons(
                getattr(entity, "restriction_reason", None)
            ),
            verified=bool(getattr(entity, "verified", False)),
            scam=bool(getattr(entity, "scam", False)),
            fake=bool(getattr(entity, "fake", False)),
            first_name=first,
            last_name=last,
            phone=getattr(entity, "phone", None),
            is_bot=bool(getattr(entity, "bot", False)),
            is_deleted=bool(getattr(entity, "deleted", False)),
            is_premium=bool(getattr(entity, "premium", False)),
            is_contact=bool(getattr(entity, "contact", False)),
            is_mutual_contact=bool(getattr(entity, "mutual_contact", False)),
            blocked=bool(getattr(full, "blocked", False)),
            common_chats_count=getattr(full, "common_chats_count", None),
            birthday=_birthday(getattr(full, "birthday", None)),
            personal_channel_id=getattr(full, "personal_channel_id", None),
            last_seen_status=(
                type(getattr(entity, "status", None)).__name__
                if getattr(entity, "status", None) is not None
                else None
            ),
            raw=(
                {"entity": _serialize(entity), "full": _serialize(full)}
                if raw
                else None
            ),
        )


def _is_muted(settings: Any) -> bool:
    """A chat is muted when ``silent`` is set or ``mute_until`` is populated."""
    if settings is None:
        return False
    if getattr(settings, "silent", False):
        return True
    return getattr(settings, "mute_until", None) is not None


__all__ = ["TelethonChatInspectBackend"]
