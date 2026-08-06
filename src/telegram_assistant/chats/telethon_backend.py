"""Telethon adapter for the chat-inspect op.

Two RPCs at most: ``get_input_entity`` to learn the peer kind, then one
``GetFull*`` request. The shallow half of the answer (``forum_tabs``,
``restriction_reason``, the rights defaults) is read out of that response's own
``chats``/``users`` list rather than a second ``get_entity`` — ``forum_tabs``
is a flag on ``Channel``, not on ``ChannelFull``, which is why
``groups/telethon_backend.py::get_topics_layout`` already resolves it that way.
"""

from __future__ import annotations

from datetime import UTC, datetime
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


def _redact(value: Any, *, _seen: frozenset[int] = frozenset()) -> Any:
    """Recursively strip ``_REDACTED_RAW_KEYS`` from a serialized value.

    Telethon's own ``to_dict()`` already recurses into nested TLObjects (its
    generated ``ChannelFull.to_dict()``, for instance, calls
    ``self.chat_photo.to_dict()`` itself), so an ``access_hash`` on a nested
    ``Photo`` reaches this function buried inside an already-plain ``dict`` —
    a *top-level-only* key filter never sees it, which is exactly how it
    leaked into ``--raw`` live (``raw.full.chat_photo.access_hash``,
    ``raw.full.profile_photo.access_hash``). This walks every shape
    ``to_dict()``/``vars()`` output can carry at any depth:

    * a ``dict`` — drop redacted keys, recurse into what is left;
    * a ``list``/``tuple`` — recurse into each item (Telegram returns these
      for e.g. ``usernames``, ``restriction_reason``, ``bot_info``);
    * an object that still carries its own ``to_dict()`` — reachable from the
      ``vars()`` fallback path in :func:`_serialize`, whose *nested*
      attributes may still be live objects even when the top-level one has no
      ``to_dict()`` of its own (a test fake is the concrete case; a mixed
      fake/real object graph is also possible).

    Anything else — ``str``, ``int``, ``bool``, ``bytes``, ``datetime``,
    ``None`` — is a leaf and is returned unchanged: none of those can carry a
    nested ``access_hash``, and neither ``bytes`` nor ``str`` are treated as a
    sequence of sub-values to recurse into.

    ``_seen`` (object ids already entered through the ``to_dict()`` branch)
    guards a reference cycle — an object whose own ``to_dict()`` yields itself
    or an ancestor — without imposing a depth cap: a legitimately deep,
    acyclic payload is walked in full rather than silently truncated.
    """
    if isinstance(value, dict):
        return {
            key: _redact(item, _seen=_seen)
            for key, item in value.items()
            if key not in _REDACTED_RAW_KEYS
        }
    if isinstance(value, (list, tuple)):
        return [_redact(item, _seen=_seen) for item in value]
    to_dict = getattr(value, "to_dict", None)
    if to_dict is not None:
        obj_id = id(value)
        if obj_id in _seen:
            return None
        return _redact(to_dict(), _seen=_seen | {obj_id})
    return value


def _serialize(raw: Any) -> dict[str, Any] | None:
    """Serialize *raw* for the ``raw`` payload with ``access_hash`` stripped
    at every depth, not just the top level — see :func:`_redact`."""
    if raw is None:
        return None
    to_dict = getattr(raw, "to_dict", None)
    if to_dict is not None:
        payload = to_dict()
    else:
        payload = {
            k: v for k, v in vars(raw).items() if not k.startswith("_")
        }
    return _redact(payload)


def _shallow_for(result: Any, full: Any, bucket: str) -> Any:
    """The shallow object matching ``full.id`` in ``result.<bucket>``.

    Telegram returns the peer alongside its Full object; match by id, and fall
    back to the only entry **when the bucket holds exactly one** — there the
    fallback cannot pick the wrong chat. With two or more entries there is no
    safe guess: ``GetFullChannel`` on a channel with a linked discussion group
    returns both in ``result.chats``, so taking ``items[0]`` on an id mismatch
    would map the *linked group's* title, flags and banned-rights defaults onto
    the chat that was asked about. ``None`` (every shallow field reported as
    ``None``/``False``) is the honest answer to "the peer Telegram sent back
    does not match the Full object it sent with it".
    """
    items = list(getattr(result, bucket, None) or [])
    target_id = int(getattr(full, "id", 0) or 0)
    match = next((i for i in items if int(getattr(i, "id", 0) or 0) == target_id), None)
    if match is None and len(items) == 1:
        return items[0]
    return match


def _raw_payload(entity: Any, full: Any, raw: bool) -> dict[str, Any] | None:
    """The ``raw`` field's value: both serialized halves, or ``None`` when unrequested.

    Shared by all three ``_inspect_*`` branches so the ``access_hash`` redaction
    in :func:`_serialize` is applied from exactly one call site rather than
    three copies that could drift.
    """
    if not raw:
        return None
    return {"entity": _serialize(entity), "full": _serialize(full)}


def _mute_fields(full: Any) -> tuple[bool, Any, bool]:
    """``(muted, muted_until, silent)`` read off ``full.notify_settings``, once.

    Shared by all three branches for the same reason as :func:`_raw_payload`.

    ``muted`` answers "are this chat's notifications suppressed **right now**",
    which is true only while ``mute_until`` lies in the future. Three shapes
    make the naive "``mute_until`` is populated" test wrong:

    * **unmuted.** Telegram spells an unmute as ``mute_until = 0``, and that is
      what this project itself writes (``notifications/telethon_backend.py``
      sends ``InputPeerNotifySettings(mute_until=0)``). Telethon 1.44's
      ``BinaryReader.tgread_date`` no longer special-cases 0 — it returns
      ``_EPOCH + timedelta(seconds=0)``, i.e. a perfectly non-``None``
      ``1970-01-01T00:00:00+00:00`` — so ``notifications unmute`` followed by
      ``chats inspect`` reported ``muted: true`` with an epoch ``muted_until``.
    * **an expired temporary mute.** The timestamp stays on the settings after
      it passes; only its position relative to now says anything.
    * **``silent``.** Per the TL schema that flag mutes the notification's
      *sound*, not the chat, so it gets its own field rather than being folded
      into ``muted``.

    ``muted_until`` is reported only when it is that future timestamp, so the
    payload never carries an epoch or a stale date as if it meant something.

    ``mute_until`` off the wire is always a timezone-aware UTC ``datetime``
    (``tgread_date`` builds it from a tz-aware epoch) or ``None`` — hence the
    tz-aware "now", and hence no branch for a bare ``int``: that shape is not
    reachable from a deserialized response.
    """
    settings = getattr(full, "notify_settings", None)
    if settings is None:
        return False, None, False

    silent = bool(getattr(settings, "silent", False))
    mute_until = getattr(settings, "mute_until", None)
    if mute_until is None or mute_until <= datetime.now(UTC):
        return False, None, silent
    return True, mute_until, silent


class TelethonChatInspectBackend:
    """Adapter from the Telethon ``TelegramClient`` to ``ChatInspectBackend``."""

    def __init__(self, client: Any) -> None:
        self._client = client

    async def inspect_chat(self, *, chat_id: int, raw: bool) -> ChatInfo:
        try:
            peer = await self._client.get_input_entity(chat_id)
        except Exception as exc:
            # Same shape as the two GetFull* branches below: re-raise the
            # original untouched when there is nothing to translate.
            # ``translate_flood_wait`` returns the *same object* then, so an
            # unconditional ``raise translated from exc`` would set
            # ``exc.__cause__ is exc``.
            translated = translate_flood_wait(exc)
            if translated is not exc:
                raise translated from exc
            raise

        kind = type(peer).__name__
        if kind == "InputPeerChannel":
            return await self._inspect_channel(peer, chat_id=chat_id, raw=raw)
        if kind == "InputPeerChat":
            return await self._inspect_basic_group(peer, chat_id=chat_id, raw=raw)
        if kind in {"InputPeerUser", "InputPeerSelf"}:
            return await self._inspect_user(peer, raw=raw)
        raise ValueError(f"chat {chat_id} cannot be inspected (resolved to {kind})")

    # --- per-kind branches --------------------------------------------------

    async def _inspect_channel(self, peer: Any, *, chat_id: int, raw: bool) -> ChatInfo:
        from telethon.errors import ChannelPrivateError
        from telethon.tl import functions

        try:
            result = await self._client(
                functions.channels.GetFullChannelRequest(channel=peer)
            )
        except Exception as exc:
            translated = translate_flood_wait(exc)
            if translated is not exc:
                raise translated from exc
            # The peer resolved (get_input_entity succeeded) but Telegram
            # refuses the Full fetch — we were kicked/banned or it is private
            # and we never joined. That is a caller-input-shaped failure, not
            # an internal error, so it maps to ValueError -> CLI exit 2 rather
            # than the unmapped exit 1 a bare RPCError would get.
            if isinstance(exc, ChannelPrivateError):
                raise ValueError(f"chat {chat_id} is private or inaccessible") from exc
            raise

        full = getattr(result, "full_chat", None)
        entity = _shallow_for(result, full, "chats")
        broadcast = bool(getattr(entity, "broadcast", False))
        muted, muted_until, silent = _mute_fields(full)

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
            muted=muted,
            muted_until=muted_until,
            silent=silent,
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
            raw=_raw_payload(entity, full, raw),
        )

    async def _inspect_basic_group(self, peer: Any, *, chat_id: int, raw: bool) -> ChatInfo:
        from telethon.errors import ChatForbiddenError
        from telethon.tl import functions

        try:
            result = await self._client(
                functions.messages.GetFullChatRequest(chat_id=peer.chat_id)
            )
        except Exception as exc:
            translated = translate_flood_wait(exc)
            if translated is not exc:
                raise translated from exc
            # Same shape as the channel branch above: the peer resolved but
            # Telegram refuses the Full fetch for this legacy basic group
            # (we were removed from it) -> ValueError -> CLI exit 2.
            if isinstance(exc, ChatForbiddenError):
                raise ValueError(f"chat {chat_id} is forbidden") from exc
            raise

        full = getattr(result, "full_chat", None)
        entity = _shallow_for(result, full, "chats")
        muted, muted_until, silent = _mute_fields(full)

        return ChatInfo(
            chat_id=int(getattr(full, "id", 0) or getattr(peer, "chat_id", 0)),
            kind="basic_group",
            title=getattr(entity, "title", None),
            about=getattr(full, "about", None) or None,
            created_at=getattr(entity, "date", None),
            ttl_period=getattr(full, "ttl_period", None),
            pinned_message_id=getattr(full, "pinned_msg_id", None),
            archived=getattr(full, "folder_id", None) == 1,
            muted=muted,
            muted_until=muted_until,
            silent=silent,
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
            raw=_raw_payload(entity, full, raw),
        )

    async def _inspect_user(self, peer: Any, *, raw: bool) -> ChatInfo:
        from telethon.tl import functions

        try:
            result = await self._client(functions.users.GetFullUserRequest(id=peer))
        except Exception as exc:
            # As above: re-raise the original untouched rather than
            # ``raise exc from exc``. There is no forbidden-peer branch here —
            # a user's Full fetch has no ChannelPrivateError analogue.
            translated = translate_flood_wait(exc)
            if translated is not exc:
                raise translated from exc
            raise

        full = getattr(result, "full_user", None)
        entity = _shallow_for(result, full, "users")
        first = getattr(entity, "first_name", None)
        last = getattr(entity, "last_name", None)
        title = " ".join(part for part in (first, last) if part) or None
        muted, muted_until, silent = _mute_fields(full)

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
            muted=muted,
            muted_until=muted_until,
            silent=silent,
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
            raw=_raw_payload(entity, full, raw),
        )


class TelethonChatTtlBackend:
    """Adapter from the Telethon ``TelegramClient`` to ``ChatTtlBackend``.

    Two RPCs at most per call, and the peer dispatch is the same shape as
    :class:`TelethonChatInspectBackend` — the ``ttl_period`` field lives on
    ``full_chat`` for channels and basic groups, on ``full_user`` for users.
    """

    def __init__(self, client: Any) -> None:
        self._client = client

    async def _peer(self, chat_id: int) -> tuple[Any, str]:
        try:
            peer = await self._client.get_input_entity(chat_id)
        except Exception as exc:
            translated = translate_flood_wait(exc)
            if translated is not exc:
                raise translated from exc
            raise
        kind = type(peer).__name__
        if kind not in {
            "InputPeerChannel",
            "InputPeerChat",
            "InputPeerUser",
            "InputPeerSelf",
        }:
            raise ValueError(
                f"chat {chat_id} has no auto-delete setting (resolved to {kind})"
            )
        return peer, kind

    async def get_ttl(self, *, chat_id: int) -> int | None:
        from telethon.errors import ChannelPrivateError, ChatForbiddenError
        from telethon.tl import functions

        peer, kind = await self._peer(chat_id)
        if kind == "InputPeerChannel":
            request = functions.channels.GetFullChannelRequest(channel=peer)
            attr = "full_chat"
        elif kind == "InputPeerChat":
            request = functions.messages.GetFullChatRequest(chat_id=peer.chat_id)
            attr = "full_chat"
        else:
            request = functions.users.GetFullUserRequest(id=peer)
            attr = "full_user"

        try:
            result = await self._client(request)
        except Exception as exc:
            translated = translate_flood_wait(exc)
            if translated is not exc:
                raise translated from exc
            # Mirrors the inspect adapter's two forbidden-Full-fetch branches
            # (``_inspect_channel``/``_inspect_basic_group``): the peer
            # resolved but Telegram refuses the Full fetch, which is
            # caller-input-shaped (exit 2), not an internal error.
            if kind == "InputPeerChannel" and isinstance(exc, ChannelPrivateError):
                raise ValueError(f"chat {chat_id} is private or inaccessible") from exc
            if kind == "InputPeerChat" and isinstance(exc, ChatForbiddenError):
                raise ValueError(f"chat {chat_id} is forbidden") from exc
            raise

        return getattr(getattr(result, attr, None), "ttl_period", None)

    async def set_ttl(self, *, chat_id: int, period: int) -> None:
        from telethon.tl import functions

        peer, _kind = await self._peer(chat_id)
        try:
            await self._client(
                functions.messages.SetHistoryTTLRequest(peer=peer, period=period)
            )
        except Exception as exc:
            translated = translate_flood_wait(exc)
            if translated is not exc:
                raise translated from exc
            # Proven live 2026-08-05 (Migragate): Telegram answered with a
            # constructor newer than the installed layer, Telethon could not
            # read it — and the write had applied. Treating that as a failure
            # would report a successful change as an error; the domain's
            # read-back is what decides. Matched by class *name* so no import
            # of a Telethon-version-specific symbol is needed.
            if type(exc).__name__ == "TypeNotFoundError":
                return
            raise


__all__ = ["TelethonChatInspectBackend", "TelethonChatTtlBackend"]
