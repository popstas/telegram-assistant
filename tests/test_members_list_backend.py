"""Tests for the Telethon members-list adapter.

The adapter is exercised against a fake client: participant classes are stand-ins
whose class *names* match Telethon's, because that is what the role mapping and
the peer dispatch key on.
"""

from __future__ import annotations

import pytest

from telegram_assistant.members.telethon_backend import TelethonMemberListBackend

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


class User:
    def __init__(self, uid: int, username=None, bot=False, first="F", last=None) -> None:
        self.id = uid
        self.username = username
        self.bot = bot
        self.first_name = first
        self.last_name = last


class ChannelParticipant:
    def __init__(self, user_id: int) -> None:
        self.user_id = user_id


class ChannelParticipantAdmin(ChannelParticipant):
    pass


class ChannelParticipantCreator(ChannelParticipant):
    pass


class ChannelParticipantLeft:
    def __init__(self, user_id: int) -> None:
        self.peer = InputPeerUser(user_id)


class ChannelParticipantBanned:
    def __init__(self, user_id: int, *, left: bool) -> None:
        self.peer = InputPeerUser(user_id)
        self.left = left


class ChatParticipant:
    def __init__(self, user_id: int) -> None:
        self.user_id = user_id


class ChatParticipantAdmin(ChatParticipant):
    pass


class ChatParticipantCreator(ChatParticipant):
    pass


class Page:
    """Stand-in for ``channels.ChannelParticipants``."""

    def __init__(self, participants, users, count) -> None:
        self.participants = participants
        self.users = users
        self.count = count


class _Container:
    def __init__(self, participants) -> None:
        self.participants = participants


class _FullChat:
    def __init__(self, participants) -> None:
        self.participants = _Container(participants)


class FullChatResult:
    def __init__(self, participants, users) -> None:
        self.full_chat = _FullChat(participants)
        self.users = users


class UserNotParticipantError(Exception):
    """Name-matched by the adapter's error classifier."""


class FakeClient:
    """Records requests and replays canned results, keyed by request class name."""

    def __init__(
        self,
        *,
        peer,
        pages=None,
        full_chat=None,
        participant=None,
        participant_error=None,
        user_peer=None,
    ) -> None:
        self._peer = peer
        self._pages = list(pages or [])
        self._full_chat = full_chat
        self._participant = participant
        self._participant_error = participant_error
        self._user_peer = user_peer
        self.requests: list[object] = []

    async def get_input_entity(self, ref):
        if isinstance(ref, int) or self._user_peer is None:
            return self._peer
        return self._user_peer

    async def __call__(self, request):
        self.requests.append(request)
        name = type(request).__name__
        if name == "GetParticipantsRequest":
            if not self._pages:
                return Page([], [], 0)
            return self._pages.pop(0)
        if name == "GetFullChatRequest":
            return self._full_chat
        if name == "GetParticipantRequest":
            if self._participant_error is not None:
                raise self._participant_error
            return self._participant
        raise AssertionError(f"unexpected request {name}")


# --- listing ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_channel_listing_pages_until_limit() -> None:
    users = [User(i, username=f"u{i}") for i in range(1, 6)]
    pages = [
        Page([ChannelParticipant(1), ChannelParticipant(2)], users[:2], 5),
        Page([ChannelParticipant(3), ChannelParticipant(4)], users[2:4], 5),
    ]
    client = FakeClient(peer=InputPeerChannel(7), pages=pages)
    backend = TelethonMemberListBackend(client)

    result = await backend.list_participants(
        chat_id=-1007, limit=4, query=None, filter="all"
    )

    assert [p.user_id for p in result.participants] == [1, 2, 3, 4]
    assert result.participants_count == 5
    assert result.truncated is True
    assert len(client.requests) == 2
    assert client.requests[1].offset == 2


@pytest.mark.asyncio
async def test_channel_listing_stops_on_empty_page_and_is_not_truncated() -> None:
    users = [User(1, username="u1")]
    pages = [Page([ChannelParticipant(1)], users, 1)]
    client = FakeClient(peer=InputPeerChannel(7), pages=pages)
    backend = TelethonMemberListBackend(client)

    result = await backend.list_participants(
        chat_id=-1007, limit=200, query=None, filter="all"
    )

    assert [p.user_id for p in result.participants] == [1]
    assert result.truncated is False


@pytest.mark.asyncio
async def test_channel_roles_are_mapped() -> None:
    users = [User(i, username=f"u{i}") for i in range(1, 7)]
    raw = [
        ChannelParticipantCreator(1),
        ChannelParticipantAdmin(2),
        ChannelParticipant(3),
        ChannelParticipantBanned(4, left=True),
        ChannelParticipantBanned(5, left=False),
        ChannelParticipantLeft(6),
    ]
    client = FakeClient(peer=InputPeerChannel(7), pages=[Page(raw, users, 6)])
    backend = TelethonMemberListBackend(client)

    result = await backend.list_participants(
        chat_id=-1007, limit=200, query=None, filter="all"
    )

    assert [p.role for p in result.participants] == [
        "creator",
        "admin",
        "member",
        "banned",
        "restricted",
        "left",
    ]


@pytest.mark.asyncio
async def test_query_is_applied_locally_for_admins_filter() -> None:
    # ChannelParticipantsAdmins has no server-side search, so the adapter
    # filters the returned rows itself.
    users = [User(1, username="alice"), User(2, username="bob")]
    client = FakeClient(
        peer=InputPeerChannel(7),
        pages=[Page([ChannelParticipantAdmin(1), ChannelParticipantAdmin(2)], users, 2)],
    )
    backend = TelethonMemberListBackend(client)

    result = await backend.list_participants(
        chat_id=-1007, limit=200, query="ali", filter="admins"
    )

    assert [p.username for p in result.participants] == ["alice"]
    assert type(client.requests[0].filter).__name__ == "ChannelParticipantsAdmins"


@pytest.mark.asyncio
async def test_all_filter_uses_server_side_search() -> None:
    client = FakeClient(peer=InputPeerChannel(7), pages=[Page([], [], 0)])
    backend = TelethonMemberListBackend(client)

    await backend.list_participants(chat_id=-1007, limit=10, query="press", filter="all")

    sent = client.requests[0].filter
    assert type(sent).__name__ == "ChannelParticipantsSearch"
    assert sent.q == "press"


@pytest.mark.asyncio
async def test_bots_filter_uses_dedicated_telegram_filter() -> None:
    client = FakeClient(peer=InputPeerChannel(7), pages=[Page([], [], 0)])
    backend = TelethonMemberListBackend(client)

    await backend.list_participants(chat_id=-1007, limit=10, query=None, filter="bots")

    assert type(client.requests[0].filter).__name__ == "ChannelParticipantsBots"


@pytest.mark.asyncio
async def test_basic_group_falls_back_to_get_full_chat() -> None:
    users = [User(1, username="alice"), User(2, username="botty", bot=True)]
    full = FullChatResult([ChatParticipantCreator(1), ChatParticipant(2)], users)
    client = FakeClient(peer=InputPeerChat(55), full_chat=full)
    backend = TelethonMemberListBackend(client)

    result = await backend.list_participants(
        chat_id=-55, limit=200, query=None, filter="all"
    )

    assert type(client.requests[0]).__name__ == "GetFullChatRequest"
    assert [(p.user_id, p.role) for p in result.participants] == [
        (1, "creator"),
        (2, "member"),
    ]
    assert result.participants_count == 2
    assert result.truncated is False


@pytest.mark.asyncio
async def test_basic_group_applies_bots_filter_locally() -> None:
    users = [User(1, username="alice"), User(2, username="botty", bot=True)]
    full = FullChatResult([ChatParticipant(1), ChatParticipant(2)], users)
    client = FakeClient(peer=InputPeerChat(55), full_chat=full)
    backend = TelethonMemberListBackend(client)

    result = await backend.list_participants(
        chat_id=-55, limit=200, query=None, filter="bots"
    )

    assert [p.user_id for p in result.participants] == [2]


@pytest.mark.asyncio
async def test_user_peer_is_rejected() -> None:
    client = FakeClient(peer=InputPeerUser(9))
    backend = TelethonMemberListBackend(client)

    with pytest.raises(ValueError):
        await backend.list_participants(chat_id=9, limit=10, query=None, filter="all")


# --- single-user check ------------------------------------------------------


class _ParticipantResult:
    def __init__(self, participant, users) -> None:
        self.participant = participant
        self.users = users


@pytest.mark.asyncio
async def test_get_participant_returns_the_member() -> None:
    found = _ParticipantResult(
        ChannelParticipantAdmin(7),
        [User(7, username="pressfinity_news_bot", bot=True)],
    )
    client = FakeClient(
        peer=InputPeerChannel(3), participant=found, user_peer=InputPeerUser(7)
    )
    backend = TelethonMemberListBackend(client)

    participant = await backend.get_participant(chat_id=-1003, user="@pressfinity_news_bot")

    assert participant is not None
    assert participant.user_id == 7
    assert participant.role == "admin"
    assert participant.is_bot is True


@pytest.mark.asyncio
async def test_get_participant_returns_none_when_absent() -> None:
    client = FakeClient(
        peer=InputPeerChannel(3),
        participant_error=UserNotParticipantError("USER_NOT_PARTICIPANT"),
        user_peer=InputPeerUser(7),
    )
    backend = TelethonMemberListBackend(client)

    assert await backend.get_participant(chat_id=-1003, user="@ghost") is None


@pytest.mark.asyncio
async def test_get_participant_scans_basic_group_roster() -> None:
    users = [User(1, username="alice"), User(7, username="botty", bot=True)]
    full = FullChatResult([ChatParticipant(1), ChatParticipantAdmin(7)], users)
    client = FakeClient(peer=InputPeerChat(55), full_chat=full, user_peer=InputPeerUser(7))
    backend = TelethonMemberListBackend(client)

    participant = await backend.get_participant(chat_id=-55, user="@botty")

    assert participant is not None
    assert participant.role == "admin"


@pytest.mark.asyncio
async def test_get_participant_absent_from_basic_group() -> None:
    users = [User(1, username="alice")]
    full = FullChatResult([ChatParticipant(1)], users)
    client = FakeClient(peer=InputPeerChat(55), full_chat=full, user_peer=InputPeerUser(9))
    backend = TelethonMemberListBackend(client)

    assert await backend.get_participant(chat_id=-55, user="@ghost") is None
