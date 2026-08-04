"""Tests for the Telethon chat-inspect adapter.

Exercised against a fake client: the stand-in classes' *names* are what the
peer dispatch keys on, mirroring tests/test_members_list_backend.py.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from telegram_assistant.chats.telethon_backend import TelethonChatInspectBackend

# --- fake telethon-shaped objects -----------------------------------------


class InputPeerChannel:
    def __init__(self, channel_id: int) -> None:
        self.channel_id = channel_id


class InputPeerChat:
    def __init__(self, chat_id: int) -> None:
        self.chat_id = chat_id


class InputPeerUser:
    def __init__(self, user_id: int) -> None:
        self.user_id = user_id


class Rights:
    """Stand-in for ChatAdminRights / ChatBannedRights."""

    def __init__(self, **flags) -> None:
        self._flags = flags

    def to_dict(self) -> dict:
        return {"_": "ChatAdminRights", **self._flags}


class NotifySettings:
    def __init__(self, mute_until=None, silent=False) -> None:
        self.mute_until = mute_until
        self.silent = silent


class InviteExported:
    def __init__(self, link: str) -> None:
        self.link = link


class RestrictionReason:
    def __init__(self, platform: str, reason: str, text: str) -> None:
        self.platform = platform
        self.reason = reason
        self.text = text


class Username:
    def __init__(self, username: str, active: bool = True) -> None:
        self.username = username
        self.active = active


class ReactionEmoji:
    def __init__(self, emoticon: str) -> None:
        self.emoticon = emoticon


class ChatReactionsSome:
    def __init__(self, reactions) -> None:
        self.reactions = reactions


class ChatReactionsAll:
    pass


class Photo:
    """Stand-in for Telethon's Photo/UserProfilePhoto/ChatPhoto.

    Carries its own ``access_hash`` — this is the exact nested shape that
    leaked live through ``--raw`` (``raw.full.chat_photo.access_hash``,
    ``raw.full.profile_photo.access_hash``): a top-level-only redaction filter
    never reaches a key nested one level down like this.
    """

    def __init__(self, photo_id: int, access_hash: int) -> None:
        self.id = photo_id
        self.access_hash = access_hash

    def to_dict(self) -> dict:
        return {"_": "Photo", "id": self.id, "access_hash": self.access_hash}


class Channel:
    def __init__(self, cid: int, **kw) -> None:
        self.id = cid
        self.title = kw.get("title", "Chat")
        self.username = kw.get("username")
        self.usernames = kw.get("usernames")
        self.date = kw.get("date")
        self.creator = kw.get("creator", False)
        self.left = kw.get("left", False)
        self.broadcast = kw.get("broadcast", False)
        self.megagroup = kw.get("megagroup", True)
        self.gigagroup = kw.get("gigagroup", False)
        self.forum = kw.get("forum", False)
        self.forum_tabs = kw.get("forum_tabs", False)
        self.verified = kw.get("verified", False)
        self.scam = kw.get("scam", False)
        self.fake = kw.get("fake", False)
        self.restricted = kw.get("restricted", False)
        self.restriction_reason = kw.get("restriction_reason")
        self.noforwards = kw.get("noforwards", False)
        self.join_to_send = kw.get("join_to_send", False)
        self.join_request = kw.get("join_request", False)
        self.call_active = kw.get("call_active", False)
        self.admin_rights = kw.get("admin_rights")
        self.default_banned_rights = kw.get("default_banned_rights")
        self.photo = kw.get("photo")
        self.access_hash = 999999


class ChannelFull:
    def __init__(self, cid: int, **kw) -> None:
        self.id = cid
        self.about = kw.get("about", "")
        self.ttl_period = kw.get("ttl_period")
        self.pinned_msg_id = kw.get("pinned_msg_id")
        self.folder_id = kw.get("folder_id")
        self.notify_settings = kw.get("notify_settings", NotifySettings())
        self.has_scheduled = kw.get("has_scheduled", False)
        self.participants_count = kw.get("participants_count")
        self.admins_count = kw.get("admins_count")
        self.kicked_count = kw.get("kicked_count")
        self.banned_count = kw.get("banned_count")
        self.online_count = kw.get("online_count")
        self.slowmode_seconds = kw.get("slowmode_seconds")
        self.slowmode_next_send_date = kw.get("slowmode_next_send_date")
        self.linked_chat_id = kw.get("linked_chat_id")
        self.migrated_from_chat_id = kw.get("migrated_from_chat_id")
        self.hidden_prehistory = kw.get("hidden_prehistory", False)
        self.participants_hidden = kw.get("participants_hidden", False)
        self.antispam = kw.get("antispam", False)
        self.can_view_participants = kw.get("can_view_participants", False)
        self.can_view_stats = kw.get("can_view_stats", False)
        self.can_delete_channel = kw.get("can_delete_channel", False)
        self.can_set_username = kw.get("can_set_username", False)
        self.requests_pending = kw.get("requests_pending")
        self.unread_count = kw.get("unread_count")
        self.available_reactions = kw.get("available_reactions")
        self.reactions_limit = kw.get("reactions_limit")
        self.exported_invite = kw.get("exported_invite")
        self.chat_photo = kw.get("chat_photo")

    def to_dict(self) -> dict:
        # Real ChannelFull.to_dict() calls self.chat_photo.to_dict() itself
        # (verified by reading the generated telethon source) -- mirror that
        # here so this fake exercises the same "already nested inside a plain
        # dict by the time _serialize sees it" shape that leaked live.
        return {
            "_": "ChannelFull",
            "id": self.id,
            "about": self.about,
            "chat_photo": self.chat_photo.to_dict() if self.chat_photo is not None else None,
        }


class Chat:
    def __init__(self, cid: int, **kw) -> None:
        self.id = cid
        self.title = kw.get("title", "Legacy")
        self.date = kw.get("date")
        self.creator = kw.get("creator", False)
        self.left = kw.get("left", False)
        self.deactivated = kw.get("deactivated", False)
        self.noforwards = kw.get("noforwards", False)
        self.call_active = kw.get("call_active", False)
        self.participants_count = kw.get("participants_count")
        self.migrated_to = kw.get("migrated_to")
        self.admin_rights = kw.get("admin_rights")
        self.default_banned_rights = kw.get("default_banned_rights")
        self.photo = kw.get("photo")


class ChatFull:
    def __init__(self, cid: int, **kw) -> None:
        self.id = cid
        self.about = kw.get("about", "")
        self.ttl_period = kw.get("ttl_period")
        self.pinned_msg_id = kw.get("pinned_msg_id")
        self.folder_id = kw.get("folder_id")
        self.notify_settings = kw.get("notify_settings", NotifySettings())
        self.has_scheduled = kw.get("has_scheduled", False)
        self.can_set_username = kw.get("can_set_username", False)
        self.requests_pending = kw.get("requests_pending")
        self.available_reactions = kw.get("available_reactions")
        self.reactions_limit = kw.get("reactions_limit")
        self.exported_invite = kw.get("exported_invite")
        self.chat_photo = kw.get("chat_photo")

    def to_dict(self) -> dict:
        return {
            "_": "ChatFull",
            "id": self.id,
            "chat_photo": self.chat_photo.to_dict() if self.chat_photo is not None else None,
        }


class InputPeerChannelMigrated:
    def __init__(self, channel_id: int) -> None:
        self.channel_id = channel_id


class UserStatusRecently:
    pass


class User:
    def __init__(self, uid: int, **kw) -> None:
        self.id = uid
        self.first_name = kw.get("first_name", "First")
        self.last_name = kw.get("last_name")
        self.username = kw.get("username")
        self.usernames = kw.get("usernames")
        self.phone = kw.get("phone")
        self.bot = kw.get("bot", False)
        self.deleted = kw.get("deleted", False)
        self.premium = kw.get("premium", False)
        self.contact = kw.get("contact", False)
        self.mutual_contact = kw.get("mutual_contact", False)
        self.verified = kw.get("verified", False)
        self.scam = kw.get("scam", False)
        self.fake = kw.get("fake", False)
        self.restricted = kw.get("restricted", False)
        self.restriction_reason = kw.get("restriction_reason")
        self.status = kw.get("status")
        self.photo = kw.get("photo")
        self.access_hash = 777777


class Birthday:
    def __init__(self, day: int, month: int, year=None) -> None:
        self.day = day
        self.month = month
        self.year = year


class UserFull:
    def __init__(self, uid: int, **kw) -> None:
        self.id = uid
        self.about = kw.get("about")
        self.ttl_period = kw.get("ttl_period")
        self.pinned_msg_id = kw.get("pinned_msg_id")
        self.folder_id = kw.get("folder_id")
        self.notify_settings = kw.get("notify_settings", NotifySettings())
        self.has_scheduled = kw.get("has_scheduled", False)
        self.blocked = kw.get("blocked", False)
        self.common_chats_count = kw.get("common_chats_count")
        self.birthday = kw.get("birthday")
        self.personal_channel_id = kw.get("personal_channel_id")
        self.profile_photo = kw.get("profile_photo")

    def to_dict(self) -> dict:
        return {
            "_": "UserFull",
            "id": self.id,
            "profile_photo": (
                self.profile_photo.to_dict() if self.profile_photo is not None else None
            ),
        }


class FullChannelResult:
    def __init__(self, full_chat, chats) -> None:
        self.full_chat = full_chat
        self.chats = chats
        self.users = []


class FullUserResult:
    def __init__(self, full_user, users) -> None:
        self.full_user = full_user
        self.users = users
        self.chats = []


class FakeClient:
    def __init__(self, *, peer, result) -> None:
        self._peer = peer
        self._result = result
        self.requests: list[object] = []

    async def get_input_entity(self, ref):
        return self._peer

    async def __call__(self, request):
        self.requests.append(request)
        return self._result


# --- supergroup -------------------------------------------------------------


@pytest.mark.asyncio
async def test_supergroup_mapping() -> None:
    created = datetime(2024, 3, 1, tzinfo=UTC)
    channel = Channel(
        5,
        title="Team",
        username="teamchat",
        usernames=[Username("alt"), Username("dead", active=False)],
        date=created,
        forum=True,
        forum_tabs=True,
        creator=True,
        noforwards=True,
        admin_rights=Rights(delete_messages=True),
        default_banned_rights=Rights(send_media=True),
    )
    full = ChannelFull(
        5,
        about="About us",
        ttl_period=86400,
        pinned_msg_id=41,
        folder_id=1,
        participants_count=12,
        admins_count=2,
        kicked_count=0,
        banned_count=1,
        online_count=3,
        slowmode_seconds=30,
        available_reactions=ChatReactionsSome([ReactionEmoji("👍")]),
        exported_invite=InviteExported("https://t.me/+abc"),
        notify_settings=NotifySettings(mute_until=created, silent=True),
    )
    client = FakeClient(
        peer=InputPeerChannel(5), result=FullChannelResult(full, [channel])
    )
    backend = TelethonChatInspectBackend(client)

    info = await backend.inspect_chat(chat_id=-1000000000005, raw=False)

    assert info.chat_id == 5
    assert info.kind == "supergroup"
    assert info.title == "Team"
    assert info.username == "teamchat"
    assert info.usernames == ("alt",)
    assert info.about == "About us"
    assert info.ttl_period == 86400
    assert info.pinned_message_id == 41
    assert info.archived is True
    assert info.muted is True
    assert info.muted_until == created
    assert info.created_at == created
    assert info.is_forum is True
    assert info.topics_layout == "tabs"
    assert info.participants_count == 12
    assert info.admins_count == 2
    assert info.banned_count == 1
    assert info.online_count == 3
    assert info.slowmode_seconds == 30
    assert info.noforwards is True
    assert info.is_creator is True
    assert info.invite_link == "https://t.me/+abc"
    assert info.available_reactions == ["👍"]
    assert info.my_admin_rights == {"delete_messages": True}
    assert info.default_banned_rights == {"send_media": True}
    assert info.raw is None


@pytest.mark.asyncio
async def test_broadcast_channel_kind_and_layout_default() -> None:
    channel = Channel(6, broadcast=True, megagroup=False)
    client = FakeClient(
        peer=InputPeerChannel(6), result=FullChannelResult(ChannelFull(6), [channel])
    )
    backend = TelethonChatInspectBackend(client)

    info = await backend.inspect_chat(chat_id=6, raw=False)

    assert info.kind == "channel"
    assert info.broadcast is True
    assert info.is_forum is False
    assert info.topics_layout is None


@pytest.mark.asyncio
async def test_reactions_all_maps_to_all() -> None:
    channel = Channel(6)
    full = ChannelFull(6, available_reactions=ChatReactionsAll())
    client = FakeClient(peer=InputPeerChannel(6), result=FullChannelResult(full, [channel]))
    backend = TelethonChatInspectBackend(client)

    info = await backend.inspect_chat(chat_id=6, raw=False)

    assert info.available_reactions == "all"


@pytest.mark.asyncio
async def test_restriction_reason_is_mapped() -> None:
    channel = Channel(
        8,
        restricted=True,
        restriction_reason=[RestrictionReason("all", "terms", "violated ToS")],
    )
    client = FakeClient(
        peer=InputPeerChannel(8), result=FullChannelResult(ChannelFull(8), [channel])
    )
    backend = TelethonChatInspectBackend(client)

    info = await backend.inspect_chat(chat_id=8, raw=False)

    assert info.restricted is True
    assert info.restriction_reason == (
        {"platform": "all", "reason": "terms", "text": "violated ToS"},
    )


# --- forbidden peers ---------------------------------------------------------
#
# The peer resolves (get_input_entity succeeds, telethon normalizes a
# ChannelForbidden/ChatForbidden into an ordinary InputPeerChannel/InputPeerChat
# before returning it — see telethon.utils.get_input_peer), but the Full fetch
# itself is refused because we were kicked/banned or never had access. Per the
# plan's error table this must surface as ValueError naming the chat, so the
# CLI maps it to exit 2 rather than the unmapped exit 1 a bare RPCError would
# get.


class RaisingClient:
    """A fake client whose ``__call__`` raises instead of returning a result."""

    def __init__(self, *, peer, exc: BaseException) -> None:
        self._peer = peer
        self._exc = exc

    async def get_input_entity(self, ref):
        return self._peer

    async def __call__(self, request):
        raise self._exc


@pytest.mark.asyncio
async def test_forbidden_channel_raises_value_error_naming_chat() -> None:
    from telethon.errors import ChannelPrivateError

    exc = ChannelPrivateError(request=object())
    client = RaisingClient(peer=InputPeerChannel(13), exc=exc)
    backend = TelethonChatInspectBackend(client)

    with pytest.raises(ValueError, match="13") as excinfo:
        await backend.inspect_chat(chat_id=13, raw=False)

    assert excinfo.value.__cause__ is exc


@pytest.mark.asyncio
async def test_forbidden_basic_group_raises_value_error_naming_chat() -> None:
    from telethon.errors import ChatForbiddenError

    exc = ChatForbiddenError(request=object())
    client = RaisingClient(peer=InputPeerChat(14), exc=exc)
    backend = TelethonChatInspectBackend(client)

    with pytest.raises(ValueError, match="14") as excinfo:
        await backend.inspect_chat(chat_id=14, raw=False)

    assert excinfo.value.__cause__ is exc


# --- legacy basic group -----------------------------------------------------


@pytest.mark.asyncio
async def test_basic_group_mapping() -> None:
    chat = Chat(
        9,
        title="Old",
        participants_count=4,
        deactivated=True,
        migrated_to=InputPeerChannelMigrated(500),
    )
    full = ChatFull(9, about="legacy", ttl_period=60)
    client = FakeClient(peer=InputPeerChat(9), result=FullChannelResult(full, [chat]))
    backend = TelethonChatInspectBackend(client)

    info = await backend.inspect_chat(chat_id=9, raw=False)

    assert info.kind == "basic_group"
    assert info.title == "Old"
    assert info.participants_count == 4
    assert info.deactivated is True
    assert info.migrated_to_chat_id == 500
    assert info.ttl_period == 60
    assert info.is_forum is False
    assert info.admins_count is None


# --- users and bots ---------------------------------------------------------


@pytest.mark.asyncio
async def test_user_mapping() -> None:
    user = User(
        11,
        first_name="Ann",
        last_name="Lee",
        username="annlee",
        phone="79990000000",
        premium=True,
        contact=True,
        status=UserStatusRecently(),
    )
    full = UserFull(
        11,
        about="bio",
        ttl_period=604800,
        blocked=True,
        common_chats_count=3,
        birthday=Birthday(4, 7, 1990),
    )
    client = FakeClient(peer=InputPeerUser(11), result=FullUserResult(full, [user]))
    backend = TelethonChatInspectBackend(client)

    info = await backend.inspect_chat(chat_id=11, raw=False)

    assert info.kind == "user"
    assert info.title == "Ann Lee"
    assert info.first_name == "Ann"
    assert info.last_name == "Lee"
    assert info.phone == "79990000000"
    assert info.is_premium is True
    assert info.is_contact is True
    assert info.blocked is True
    assert info.common_chats_count == 3
    assert info.birthday == {"day": 4, "month": 7, "year": 1990}
    assert info.ttl_period == 604800
    assert info.last_seen_status == "UserStatusRecently"


@pytest.mark.asyncio
async def test_bot_kind() -> None:
    user = User(12, first_name="Helper", bot=True)
    client = FakeClient(peer=InputPeerUser(12), result=FullUserResult(UserFull(12), [user]))
    backend = TelethonChatInspectBackend(client)

    info = await backend.inspect_chat(chat_id=12, raw=False)

    assert info.kind == "bot"
    assert info.is_bot is True


# --- raw --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_raw_carries_both_halves_without_access_hash() -> None:
    channel = Channel(5, title="Team")
    client = FakeClient(
        peer=InputPeerChannel(5), result=FullChannelResult(ChannelFull(5), [channel])
    )
    backend = TelethonChatInspectBackend(client)

    info = await backend.inspect_chat(chat_id=5, raw=True)

    assert set(info.raw) == {"entity", "full"}
    assert info.raw["full"]["_"] == "ChannelFull"
    assert info.raw["entity"]["title"] == "Team"
    assert "access_hash" not in info.raw["entity"]


@pytest.mark.asyncio
async def test_user_raw_strips_access_hash() -> None:
    user = User(11, first_name="Ann")
    client = FakeClient(peer=InputPeerUser(11), result=FullUserResult(UserFull(11), [user]))
    backend = TelethonChatInspectBackend(client)

    info = await backend.inspect_chat(chat_id=11, raw=True)

    assert "access_hash" not in info.raw["entity"]


# --- raw: nested access_hash (the live leak this backfills) -----------------
#
# The live finding: raw.full.chat_photo.access_hash /
# raw.full.profile_photo.access_hash survived a top-level-only redaction
# filter. Each test below covers one peer kind and exercises *both*
# serialization paths at once: the "full" side goes through Full.to_dict(),
# which (mirroring real Telethon) has already flattened chat_photo/
# profile_photo into a plain nested dict by the time _serialize sees it; the
# "entity" side has no to_dict() of its own, so it goes through the vars()
# fallback, whose `photo` attribute is still a *live* Photo object with its
# own to_dict() -- the second path the review called out as a possible
# unclosed hole. Asserting against the whole payload via json.dumps (rather
# than the top-level dict only) is the same check the live verification ran.


@pytest.mark.asyncio
async def test_raw_strips_access_hash_from_nested_channel_photo() -> None:
    channel = Channel(5, title="Team", photo=Photo(101, access_hash=999999001))
    full = ChannelFull(5, about="About us", chat_photo=Photo(102, access_hash=999999002))
    client = FakeClient(
        peer=InputPeerChannel(5), result=FullChannelResult(full, [channel])
    )
    backend = TelethonChatInspectBackend(client)

    info = await backend.inspect_chat(chat_id=5, raw=True)

    assert info.raw["full"]["chat_photo"]["id"] == 102
    assert "access_hash" not in info.raw["full"]["chat_photo"]
    # entity has no to_dict() of its own -- vars() fallback -- so `photo` is
    # still a live Photo object going in; _redact must recurse into it via
    # its own to_dict() rather than leaving it untouched.
    assert info.raw["entity"]["photo"]["id"] == 101
    assert "access_hash" not in info.raw["entity"]["photo"]
    assert "access_hash" not in json.dumps(info.raw, default=str)


@pytest.mark.asyncio
async def test_raw_strips_access_hash_from_nested_basic_group_photo() -> None:
    chat = Chat(9, title="Old", photo=Photo(201, access_hash=999999003))
    full = ChatFull(9, about="legacy", chat_photo=Photo(202, access_hash=999999004))
    client = FakeClient(peer=InputPeerChat(9), result=FullChannelResult(full, [chat]))
    backend = TelethonChatInspectBackend(client)

    info = await backend.inspect_chat(chat_id=9, raw=True)

    assert info.raw["full"]["chat_photo"]["id"] == 202
    assert "access_hash" not in info.raw["full"]["chat_photo"]
    assert info.raw["entity"]["photo"]["id"] == 201
    assert "access_hash" not in info.raw["entity"]["photo"]
    assert "access_hash" not in json.dumps(info.raw, default=str)


@pytest.mark.asyncio
async def test_raw_strips_access_hash_from_nested_user_photo() -> None:
    user = User(11, first_name="Ann", photo=Photo(301, access_hash=999999005))
    full = UserFull(11, about="bio", profile_photo=Photo(302, access_hash=999999006))
    client = FakeClient(peer=InputPeerUser(11), result=FullUserResult(full, [user]))
    backend = TelethonChatInspectBackend(client)

    info = await backend.inspect_chat(chat_id=11, raw=True)

    assert info.raw["full"]["profile_photo"]["id"] == 302
    assert "access_hash" not in info.raw["full"]["profile_photo"]
    assert info.raw["entity"]["photo"]["id"] == 301
    assert "access_hash" not in info.raw["entity"]["photo"]
    assert "access_hash" not in json.dumps(info.raw, default=str)


# --- _redact: shapes that break a naive recursive redaction -----------------
#
# Every other test in this file goes through the public inspect_chat()
# surface, matching house style (see e.g. tests/test_topics_telethon_backend.py
# -- private helpers are exercised only indirectly). These three are a
# deliberate exception: the round-1 review explicitly named three shapes a
# naive recursive redaction can get wrong -- "a value that is a list of
# objects ..., a bytes value, and a self-referential or deeply nested
# structure" -- and none of the three peer-kind payloads above happens to
# carry a list of access_hash-bearing objects or a cyclic reference, so
# there is no way to exercise those specific shapes through inspect_chat()
# without inventing an unrealistic Full/entity fixture. _redact() is not
# exported (absent from __all__); it is imported directly here only for this.


def test_redact_strips_access_hash_from_a_list_of_nested_objects() -> None:
    from telegram_assistant.chats.telethon_backend import _redact

    photos = [Photo(1, access_hash=111), Photo(2, access_hash=222)]

    result = _redact({"photos": photos})

    assert result == {"photos": [{"_": "Photo", "id": 1}, {"_": "Photo", "id": 2}]}


def test_redact_leaves_bytes_untouched() -> None:
    from telegram_assistant.chats.telethon_backend import _redact

    result = _redact({"file_reference": b"\x00\x01raw-bytes"})

    assert result == {"file_reference": b"\x00\x01raw-bytes"}


def test_redact_guards_against_a_reference_cycle() -> None:
    from telegram_assistant.chats.telethon_backend import _redact

    class Cyclic:
        def to_dict(self):
            return {"access_hash": 1, "self": self}

    cyclic = Cyclic()

    # Must terminate rather than raise RecursionError / hang, and the cycle
    # is broken (not silently re-entered) rather than truncating an
    # otherwise-acyclic payload.
    result = _redact(cyclic)

    assert "access_hash" not in result
    assert result["self"] is None
