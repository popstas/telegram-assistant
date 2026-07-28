"""Telethon-backed :class:`MemberAddBackend` implementation.

Kept separate from :mod:`service` so the domain layer stays Telethon-free.
The adapter translates the two domain verbs (``add_member``,
``promote_admin``) into the corresponding Telethon RPCs and maps known
Telegram error strings into our :class:`MemberPrivacyError` /
:class:`MemberAlreadyPresentError` so the bulk loop can categorise the
outcome.

:class:`TelethonMemberListBackend` at the bottom of this module is the read
counterpart, backing :func:`telegram_assistant.members.listing.list_members`.
"""

from __future__ import annotations

from typing import Any

from telegram_assistant.members.listing import MemberListResult, Participant
from telegram_assistant.members.service import (
    MemberAlreadyPresentError,
    MemberNotPresentError,
    MemberPrivacyError,
    coerce_user_ref,
)
from telegram_assistant.worker.queue import FloodWaitError

# Telegram's RPC error class names that map to our domain errors. We match by
# class name so we don't have to import telethon at module load — the import
# happens lazily inside the methods.
_PRIVACY_RPC_ERRORS = {
    "UserPrivacyRestrictedError",
    "UserNotMutualContactError",
    "UserChannelsTooMuchError",
    "PeerFloodError",
}

_ALREADY_PRESENT_RPC_ERRORS = {
    "UserAlreadyParticipantError",
}

_NOT_PRESENT_RPC_ERRORS = {
    "UserNotParticipantError",
    "ParticipantIdInvalidError",
}


def _classify_rpc_error(exc: Exception) -> Exception:
    name = type(exc).__name__
    if name == "FloodWaitError":
        seconds = getattr(exc, "seconds", 0) or 0
        return FloodWaitError(float(seconds))
    if name in _PRIVACY_RPC_ERRORS:
        return MemberPrivacyError(str(exc) or name)
    if name in _ALREADY_PRESENT_RPC_ERRORS:
        return MemberAlreadyPresentError(str(exc) or name)
    if name in _NOT_PRESENT_RPC_ERRORS:
        return MemberNotPresentError(str(exc) or name)
    return exc


class TelethonMemberBackend:
    """Adapter from the Telethon ``TelegramClient`` to ``MemberAddBackend``."""

    def __init__(self, client: Any) -> None:
        self._client = client

    async def add_member(self, *, chat_id: int, user: str) -> None:
        from telethon.tl.functions.channels import InviteToChannelRequest

        try:
            channel = await self._client.get_input_entity(chat_id)
            member = await self._client.get_input_entity(coerce_user_ref(user))
            await self._client(
                InviteToChannelRequest(channel=channel, users=[member])
            )
        except Exception as exc:
            raise _classify_rpc_error(exc) from exc

    async def promote_admin(self, *, chat_id: int, user: str) -> None:
        from telethon.tl.functions.channels import EditAdminRequest
        from telethon.tl.types import ChatAdminRights

        try:
            channel = await self._client.get_input_entity(chat_id)
            member = await self._client.get_input_entity(coerce_user_ref(user))
        except Exception as exc:
            raise _classify_rpc_error(exc) from exc
        rights = ChatAdminRights(
            change_info=True,
            post_messages=False,
            edit_messages=False,
            delete_messages=True,
            ban_users=True,
            invite_users=True,
            pin_messages=True,
            add_admins=False,
            anonymous=False,
            manage_call=True,
            other=True,
            manage_topics=True,
        )
        try:
            await self._client(
                EditAdminRequest(
                    channel=channel,
                    user_id=member,
                    admin_rights=rights,
                    rank="admin",
                )
            )
        except Exception as exc:
            raise _classify_rpc_error(exc) from exc

    async def ban_member(self, *, chat_id: int, user: str) -> None:
        from telethon.tl.functions.channels import EditBannedRequest
        from telethon.tl.types import ChatBannedRights

        try:
            channel = await self._client.get_input_entity(chat_id)
            member = await self._client.get_input_entity(coerce_user_ref(user))
        except Exception as exc:
            raise _classify_rpc_error(exc) from exc
        banned = ChatBannedRights(
            until_date=None,
            view_messages=True,
            send_messages=True,
            send_media=True,
            send_stickers=True,
            send_gifs=True,
            send_games=True,
            send_inline=True,
            embed_links=True,
        )
        try:
            await self._client(
                EditBannedRequest(channel=channel, participant=member, banned_rights=banned)
            )
        except Exception as exc:
            raise _classify_rpc_error(exc) from exc

    async def unban_member(self, *, chat_id: int, user: str) -> None:
        from telethon.tl.functions.channels import EditBannedRequest
        from telethon.tl.types import ChatBannedRights

        try:
            channel = await self._client.get_input_entity(chat_id)
            member = await self._client.get_input_entity(coerce_user_ref(user))
        except Exception as exc:
            raise _classify_rpc_error(exc) from exc
        cleared = ChatBannedRights(until_date=None)
        try:
            await self._client(
                EditBannedRequest(channel=channel, participant=member, banned_rights=cleared)
            )
        except Exception as exc:
            raise _classify_rpc_error(exc) from exc


# ---------------------------------------------------------------------------
# Read side: participants listing
# ---------------------------------------------------------------------------

#: Telegram serves participants 200 at a time; a wider page is silently clamped.
_PARTICIPANTS_PAGE_SIZE = 200

#: Legacy basic groups top out around 200 members — one GetFullChat is enough.
_BASIC_CHAT_ROSTER_CAP = 1000


def _participant_user_id(raw: Any) -> int | None:
    """Read a participant's user id.

    Telegram spells it ``user_id`` on the plain/admin/creator classes and
    ``peer`` (a ``PeerUser``) on the left/banned ones.
    """
    user_id = getattr(raw, "user_id", None)
    if user_id is not None:
        return int(user_id)
    peer = getattr(raw, "peer", None)
    peer_id = getattr(peer, "user_id", None)
    return int(peer_id) if peer_id is not None else None


def _channel_role(raw: Any) -> str:
    name = type(raw).__name__
    if name == "ChannelParticipantCreator":
        return "creator"
    if name == "ChannelParticipantAdmin":
        return "admin"
    if name == "ChannelParticipantBanned":
        # ``left`` means kicked out; otherwise the user is still in the chat
        # under restrictions.
        return "banned" if getattr(raw, "left", False) else "restricted"
    if name == "ChannelParticipantLeft":
        return "left"
    return "member"


def _chat_role(raw: Any) -> str:
    name = type(raw).__name__
    if name == "ChatParticipantCreator":
        return "creator"
    if name == "ChatParticipantAdmin":
        return "admin"
    return "member"


def _to_participant(raw: Any, users: dict[int, Any], role: str) -> Participant | None:
    user_id = _participant_user_id(raw)
    if user_id is None:
        return None
    user = users.get(user_id)
    return Participant(
        user_id=user_id,
        username=getattr(user, "username", None),
        first_name=getattr(user, "first_name", None),
        last_name=getattr(user, "last_name", None),
        is_bot=bool(getattr(user, "bot", False)),
        role=role,
    )


def _matches_query(participant: Participant, query: str | None) -> bool:
    if not query:
        return True
    needle = query.casefold()
    haystack = " ".join(
        part
        for part in (
            participant.username,
            participant.first_name,
            participant.last_name,
        )
        if part
    )
    return needle in haystack.casefold()


def _matches_filter(participant: Participant, filter: str) -> bool:
    if filter == "admins":
        return participant.role in {"creator", "admin"}
    if filter == "bots":
        return participant.is_bot
    return True


class TelethonMemberListBackend:
    """Adapter from the Telethon ``TelegramClient`` to ``MemberListBackend``.

    Supergroups/channels go through ``channels.GetParticipants`` /
    ``channels.GetParticipant``; legacy basic groups have neither, so they fall
    back to a single ``messages.GetFullChat`` whose roster is filtered locally.
    """

    def __init__(self, client: Any) -> None:
        self._client = client

    async def list_participants(
        self, *, chat_id: int, limit: int, query: str | None, filter: str
    ) -> MemberListResult:
        peer = await self._input_peer(chat_id)
        kind = type(peer).__name__
        if kind == "InputPeerChannel":
            return await self._channel_participants(
                peer, limit=limit, query=query, filter=filter
            )
        if kind == "InputPeerChat":
            return await self._chat_participants(
                peer, limit=limit, query=query, filter=filter
            )
        raise ValueError(f"chat {chat_id} has no participants (resolved to {kind})")

    async def get_participant(self, *, chat_id: int, user: str) -> Participant | None:
        peer = await self._input_peer(chat_id)
        kind = type(peer).__name__
        member = await self._input_peer_for_user(user)
        if kind == "InputPeerChannel":
            from telethon.tl import functions

            try:
                result = await self._client(
                    functions.channels.GetParticipantRequest(
                        channel=peer, participant=member
                    )
                )
            except Exception as exc:
                mapped = _classify_rpc_error(exc)
                if isinstance(mapped, MemberNotPresentError):
                    # A normal negative answer, not a failure.
                    return None
                raise mapped from exc
            users = {u.id: u for u in getattr(result, "users", ()) or ()}
            raw = getattr(result, "participant", None)
            if raw is None:
                return None
            return _to_participant(raw, users, _channel_role(raw))
        if kind == "InputPeerChat":
            target_id = getattr(member, "user_id", None)
            roster = await self._chat_participants(
                peer, limit=_BASIC_CHAT_ROSTER_CAP, query=None, filter="all"
            )
            for participant in roster.participants:
                if participant.user_id == target_id:
                    return participant
            return None
        raise ValueError(f"chat {chat_id} has no participants (resolved to {kind})")

    # -- internals ---------------------------------------------------------

    async def _input_peer(self, chat_id: int) -> Any:
        try:
            return await self._client.get_input_entity(chat_id)
        except Exception as exc:
            raise _classify_rpc_error(exc) from exc

    async def _input_peer_for_user(self, user: str) -> Any:
        try:
            return await self._client.get_input_entity(coerce_user_ref(user))
        except Exception as exc:
            raise _classify_rpc_error(exc) from exc

    def _channel_filter(self, filter: str, query: str | None) -> Any:
        from telethon.tl import types

        if filter == "admins":
            return types.ChannelParticipantsAdmins()
        if filter == "bots":
            return types.ChannelParticipantsBots()
        # Search — not ``Recent``, which caps out near 200 and does not page.
        # Telethon's own iter_participants uses Search("") for full walks.
        return types.ChannelParticipantsSearch(query or "")

    async def _channel_participants(
        self, peer: Any, *, limit: int, query: str | None, filter: str
    ) -> MemberListResult:
        from telethon.tl import functions

        participants_filter = self._channel_filter(filter, query)
        # ``admins``/``bots`` have no server-side search, so the query is
        # re-applied locally for them.
        local_query = query if filter in {"admins", "bots"} else None

        collected: list[Participant] = []
        seen: set[int] = set()
        total: int | None = None
        offset = 0
        exhausted = False

        while len(collected) < limit:
            page_size = min(_PARTICIPANTS_PAGE_SIZE, limit - len(collected))
            try:
                page = await self._client(
                    functions.channels.GetParticipantsRequest(
                        channel=peer,
                        filter=participants_filter,
                        offset=offset,
                        limit=page_size,
                        hash=0,
                    )
                )
            except Exception as exc:
                raise _classify_rpc_error(exc) from exc

            raw_participants = list(getattr(page, "participants", ()) or ())
            if not raw_participants:
                exhausted = True
                break
            count = getattr(page, "count", None)
            if count is not None:
                total = int(count)
            users = {u.id: u for u in getattr(page, "users", ()) or ()}

            for raw in raw_participants:
                mapped = _to_participant(raw, users, _channel_role(raw))
                if mapped is None or mapped.user_id in seen:
                    continue
                if not _matches_query(mapped, local_query):
                    continue
                seen.add(mapped.user_id)
                collected.append(mapped)
                if len(collected) >= limit:
                    break

            offset += len(raw_participants)
            if total is not None and offset >= total:
                exhausted = True
                break

        return MemberListResult(
            participants=tuple(collected),
            participants_count=total,
            truncated=not exhausted,
        )

    async def _chat_participants(
        self, peer: Any, *, limit: int, query: str | None, filter: str
    ) -> MemberListResult:
        from telethon.tl import functions

        try:
            result = await self._client(
                functions.messages.GetFullChatRequest(chat_id=peer.chat_id)
            )
        except Exception as exc:
            raise _classify_rpc_error(exc) from exc

        full_chat = getattr(result, "full_chat", None)
        container = getattr(full_chat, "participants", None)
        raw_participants = list(getattr(container, "participants", ()) or ())
        users = {u.id: u for u in getattr(result, "users", ()) or ()}

        mapped: list[Participant] = []
        for raw in raw_participants:
            participant = _to_participant(raw, users, _chat_role(raw))
            if participant is not None:
                mapped.append(participant)

        selected = [
            p for p in mapped if _matches_filter(p, filter) and _matches_query(p, query)
        ]
        return MemberListResult(
            participants=tuple(selected[:limit]),
            participants_count=len(mapped),
            truncated=len(selected) > limit,
        )
