"""Unit tests for :class:`TelethonMessageBackend`.

Covers the write side of the message adapter: text-only sends route through
``send_message``, attachment sends through ``send_file`` (single id vs album
list), scheduling and topic reply ids are forwarded, an empty caption becomes
``None``, and Telethon ``FloodWaitError`` is translated for the worker queue.
:class:`TelethonSearchBackend` is covered too: one ``messages.Search`` RPC
carrying every filter, plus ``offset_id`` pagination.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from telethon.tl.types import MessageEmpty, PeerUser

from telegram_assistant.messages.telethon_backend import (
    _SEARCH_PAGE_SIZE,
    TelethonDeleteBackend,
    TelethonMessageBackend,
    TelethonSearchBackend,
)
from telegram_assistant.worker.queue import FloodWaitError


class _Sent:
    def __init__(self, msg_id: int) -> None:
        self.id = msg_id


class _RecordingClient:
    """Telethon client double recording send_message / send_file calls."""

    def __init__(self) -> None:
        self.message_calls: list[dict[str, Any]] = []
        self.file_calls: list[dict[str, Any]] = []

    async def send_message(self, chat_id: int, text: str, **kwargs: Any) -> Any:
        self.message_calls.append(
            {"chat_id": chat_id, "text": text, "kwargs": kwargs}
        )
        return _Sent(555)

    async def send_file(self, chat_id: int, files: Any, **kwargs: Any) -> Any:
        self.file_calls.append(
            {"chat_id": chat_id, "files": files, "kwargs": kwargs}
        )
        files = list(files)
        if len(files) > 1:
            return [_Sent(600 + i) for i in range(len(files))]
        return _Sent(600)


class _TelethonFloodWaitError(Exception):
    """Stand-in matching the upstream class name ``FloodWaitError``."""

    def __init__(self, seconds: int) -> None:
        super().__init__(f"flood wait {seconds}")
        self.seconds = seconds


# Rename so type(exc).__name__ == "FloodWaitError" for translate_flood_wait.
_TelethonFloodWaitError.__name__ = "FloodWaitError"


@pytest.mark.asyncio
async def test_text_only_routes_through_send_message() -> None:
    client = _RecordingClient()
    backend = TelethonMessageBackend(client)

    result = await backend.send_message(chat_id=-100123, text="hello")

    assert result == 555
    assert client.file_calls == []
    assert len(client.message_calls) == 1
    call = client.message_calls[0]
    assert call["chat_id"] == -100123
    assert call["text"] == "hello"
    # No topic / schedule kwargs for a plain send.
    assert call["kwargs"] == {}


@pytest.mark.asyncio
async def test_text_send_forwards_topic_and_schedule() -> None:
    client = _RecordingClient()
    backend = TelethonMessageBackend(client)
    when = datetime(2030, 1, 1, tzinfo=UTC)

    await backend.send_message(
        chat_id=42, text="hi", topic_id=7, schedule_at=when
    )

    call = client.message_calls[0]
    assert call["kwargs"]["reply_to"] == 7
    assert call["kwargs"]["schedule"] == when


@pytest.mark.asyncio
async def test_text_send_reply_to_message_id_sets_reply_to() -> None:
    client = _RecordingClient()
    backend = TelethonMessageBackend(client)

    await backend.send_message(chat_id=42, text="re", reply_to_message_id=99)

    assert client.message_calls[0]["kwargs"]["reply_to"] == 99


@pytest.mark.asyncio
async def test_reply_to_message_id_wins_over_topic_id() -> None:
    """A forum reply targets the message; replying inside a topic keeps it
    threaded, so an explicit reply id takes precedence over the topic root."""
    client = _RecordingClient()
    backend = TelethonMessageBackend(client)

    await backend.send_message(
        chat_id=42, text="re", topic_id=7, reply_to_message_id=99
    )

    assert client.message_calls[0]["kwargs"]["reply_to"] == 99


@pytest.mark.asyncio
async def test_file_send_reply_to_message_id_sets_reply_to() -> None:
    client = _RecordingClient()
    backend = TelethonMessageBackend(client)

    await backend.send_message(
        chat_id=10, text="cap", files=("/tmp/a.png",), reply_to_message_id=12
    )

    assert client.file_calls[0]["kwargs"]["reply_to"] == 12


@pytest.mark.asyncio
async def test_single_file_returns_single_id_with_caption() -> None:
    client = _RecordingClient()
    backend = TelethonMessageBackend(client)

    result = await backend.send_message(
        chat_id=10, text="caption", files=("/tmp/a.png",)
    )

    assert result == 600
    assert client.message_calls == []
    call = client.file_calls[0]
    assert call["files"] == ["/tmp/a.png"]
    assert call["kwargs"]["caption"] == "caption"


@pytest.mark.asyncio
async def test_album_returns_list_of_ids() -> None:
    client = _RecordingClient()
    backend = TelethonMessageBackend(client)

    result = await backend.send_message(
        chat_id=10, text="", files=("/tmp/a.png", "/tmp/b.png")
    )

    assert result == [600, 601]
    call = client.file_calls[0]
    assert call["files"] == ["/tmp/a.png", "/tmp/b.png"]
    # Empty caption must collapse to None so Telethon sends no extra text.
    assert call["kwargs"]["caption"] is None


@pytest.mark.asyncio
async def test_file_send_forwards_topic_and_schedule() -> None:
    client = _RecordingClient()
    backend = TelethonMessageBackend(client)
    when = datetime(2030, 6, 1, tzinfo=UTC)

    await backend.send_message(
        chat_id=10,
        text="cap",
        topic_id=3,
        files=("/tmp/a.png",),
        schedule_at=when,
    )

    call = client.file_calls[0]
    assert call["kwargs"]["reply_to"] == 3
    assert call["kwargs"]["schedule"] == when


@pytest.mark.asyncio
async def test_flood_wait_is_translated_on_text_send() -> None:
    class _Flooding(_RecordingClient):
        async def send_message(self, chat_id: int, text: str, **kwargs: Any) -> Any:
            raise _TelethonFloodWaitError(30)

    backend = TelethonMessageBackend(_Flooding())
    with pytest.raises(FloodWaitError):
        await backend.send_message(chat_id=1, text="boom")


@pytest.mark.asyncio
async def test_flood_wait_is_translated_on_file_send() -> None:
    class _Flooding(_RecordingClient):
        async def send_file(self, chat_id: int, files: Any, **kwargs: Any) -> Any:
            raise _TelethonFloodWaitError(45)

    backend = TelethonMessageBackend(_Flooding())
    with pytest.raises(FloodWaitError):
        await backend.send_message(chat_id=1, text="x", files=("/tmp/a.png",))


# ---------------------------------------------------------------------------
# TelethonDeleteBackend
# ---------------------------------------------------------------------------


class _DeletingClient:
    """Telethon client double recording get_input_entity / delete_messages."""

    def __init__(self) -> None:
        self.delete_calls: list[dict[str, Any]] = []

    async def get_input_entity(self, chat_id: int) -> Any:
        return f"peer:{chat_id}"

    async def delete_messages(
        self, entity: Any, message_ids: Any, *, revoke: bool = True
    ) -> Any:
        self.delete_calls.append(
            {"entity": entity, "message_ids": list(message_ids), "revoke": revoke}
        )
        return []


@pytest.mark.asyncio
async def test_delete_backend_revoke_default_true() -> None:
    client = _DeletingClient()
    backend = TelethonDeleteBackend(client)
    count = await backend.delete_messages(chat_id=-100, message_ids=(11, 12))
    assert count == 2
    call = client.delete_calls[0]
    assert call["entity"] == "peer:-100"
    assert call["message_ids"] == [11, 12]
    assert call["revoke"] is True


@pytest.mark.asyncio
async def test_delete_backend_no_revoke() -> None:
    client = _DeletingClient()
    backend = TelethonDeleteBackend(client)
    await backend.delete_messages(chat_id=5, message_ids=(7,), revoke=False)
    assert client.delete_calls[0]["revoke"] is False


@pytest.mark.asyncio
async def test_delete_backend_flood_wait_is_translated() -> None:
    class _Flooding(_DeletingClient):
        async def delete_messages(
            self, entity: Any, message_ids: Any, *, revoke: bool = True
        ) -> Any:
            raise _TelethonFloodWaitError(15)

    backend = TelethonDeleteBackend(_Flooding())
    with pytest.raises(FloodWaitError):
        await backend.delete_messages(chat_id=1, message_ids=(1,))


# ---------------------------------------------------------------------------
# TelethonSearchBackend
# ---------------------------------------------------------------------------


class _SearchMsg:
    """Raw ``messages.Search`` hit — no resolved ``sender``, nested reply header."""

    def __init__(
        self,
        msg_id: int,
        *,
        date: datetime | None = None,
        message: str = "",
        from_id: Any = None,
        reply_to: Any = None,
        media: Any = None,
        sender_id: int | None = None,
    ) -> None:
        self.id = msg_id
        self.date = date
        self.message = message
        self.from_id = from_id
        self.reply_to = reply_to
        self.media = media
        # Telethon's ``Message.__init__`` derives this from ``from_id``/
        # ``peer_id`` before any entity resolution, so a raw search hit always
        # carries it — including for channel posts and incoming private
        # messages, which have no ``from_id`` at all.
        self.sender_id = (
            sender_id
            if sender_id is not None
            else getattr(from_id, "user_id", None)
            if from_id is not None
            else None
        )


class _PeerUser:
    def __init__(self, user_id: int) -> None:
        self.user_id = user_id


class _ReplyHeader:
    def __init__(self, reply_to_msg_id: int) -> None:
        self.reply_to_msg_id = reply_to_msg_id


class _User:
    def __init__(self, user_id: int, username: str | None) -> None:
        self.id = user_id
        self.username = username


class _SearchPage:
    def __init__(
        self,
        messages: list[Any],
        users: list[Any] | None = None,
        chats: list[Any] | None = None,
    ) -> None:
        self.messages = messages
        self.users = list(users or [])
        self.chats = list(chats or [])


class _SearchingClient:
    """Telethon client double serving canned ``messages.Search`` pages.

    The last page is served repeatedly so a broken termination condition shows
    up as the request cap below rather than an infinite loop.
    """

    def __init__(self, pages: list[_SearchPage]) -> None:
        self._pages = list(pages) or [_SearchPage([])]
        self.requests: list[Any] = []
        self.entity_calls: list[Any] = []

    async def get_input_entity(self, ref: Any) -> Any:
        self.entity_calls.append(ref)
        return f"peer:{ref}"

    async def __call__(self, request: Any) -> Any:
        self.requests.append(request)
        assert len(self.requests) <= 10, "search paging did not terminate"
        if len(self._pages) > 1:
            return self._pages.pop(0)
        return self._pages[0]


_BASE = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
_FROM = _BASE - timedelta(hours=1)
_TO = _BASE + timedelta(hours=1)


@pytest.mark.asyncio
async def test_search_builds_one_request_with_all_filters() -> None:
    """The flagged combination (query + topic + sender + range) is one RPC."""
    client = _SearchingClient(
        [_SearchPage([_SearchMsg(1010, date=_BASE, message="hit")])]
    )
    backend = TelethonSearchBackend(client)

    rows = await backend.search_messages(
        chat_id=-100777,
        query="report",
        from_user="@bob",
        limit=5,
        topic_id=42,
        from_date=_FROM,
        to_date=_TO,
    )

    assert [row.id for row in rows] == [1010]
    request = client.requests[0]
    assert request.peer == "peer:-100777"
    assert request.q == "report"
    assert request.from_id == "peer:@bob"
    assert request.top_msg_id == 42
    # The range filter is re-applied locally, so the wire page is full width
    # rather than `limit` — see `test_search_page_width_survives_a_small_limit`.
    assert request.limit == _SEARCH_PAGE_SIZE
    assert request.offset_id == 0
    # Bounds are widened by a second; the exact inclusive check runs on rows.
    assert request.min_date == _FROM - timedelta(seconds=1)
    assert request.max_date == _TO + timedelta(seconds=1)
    # A short page is not an end-of-history signal, so paging asks once more
    # with an advanced offset — carrying the very same filter set.
    assert [r.offset_id for r in client.requests] == [0, 1010]
    assert all(
        (r.q, r.from_id, r.top_msg_id, r.min_date, r.max_date)
        == (
            request.q,
            request.from_id,
            request.top_msg_id,
            request.min_date,
            request.max_date,
        )
        for r in client.requests
    )


@pytest.mark.asyncio
async def test_search_does_not_stop_on_a_short_page() -> None:
    """Channels may drop undisplayable rows from a full slice (Telethon's own
    caveat), so ``len(page) < page_size`` must not hide older matches."""
    client = _SearchingClient(
        [
            # Two rows for a full-width page: a naive short-page break would
            # stop here and never see id 940.
            _SearchPage([_SearchMsg(960, date=_BASE, message="a"), _SearchMsg(950, date=_BASE, message="b")]),
            _SearchPage([_SearchMsg(940, date=_BASE, message="c")]),
            _SearchPage([]),
        ]
    )
    backend = TelethonSearchBackend(client)

    rows = await backend.search_messages(
        chat_id=1, query="x", limit=5, from_date=_FROM, to_date=_TO
    )

    assert [row.id for row in rows] == [960, 950, 940]


@pytest.mark.asyncio
async def test_search_stops_when_newest_id_cannot_have_older_messages() -> None:
    """Ids start at 1, so a newest id <= page size ends history without an
    extra RPC."""
    client = _SearchingClient([_SearchPage([_SearchMsg(2, date=_BASE, message="a")])])
    backend = TelethonSearchBackend(client)

    rows = await backend.search_messages(
        chat_id=1, query="x", limit=5, from_date=_FROM, to_date=_TO
    )

    assert [row.id for row in rows] == [2]
    assert len(client.requests) == 1


@pytest.mark.asyncio
async def test_search_skips_message_empty_rows() -> None:
    """``MessageEmpty`` carries no text/date; it must not spend a limit slot."""
    client = _SearchingClient(
        [
            _SearchPage(
                [
                    _SearchMsg(9, date=_BASE, message="kept"),
                    MessageEmpty(id=8, peer_id=PeerUser(1)),
                    _SearchMsg(7, date=_BASE, message="also kept"),
                ]
            )
        ]
    )
    backend = TelethonSearchBackend(client)

    rows = await backend.search_messages(chat_id=1, query="x", limit=20)

    assert [row.id for row in rows] == [9, 7]


@pytest.mark.asyncio
async def test_search_without_range_sends_no_date_bounds() -> None:
    client = _SearchingClient(
        [_SearchPage([_SearchMsg(3, date=_BASE, message="a")])]
    )
    backend = TelethonSearchBackend(client)

    rows = await backend.search_messages(chat_id=5, query="a", limit=2)

    assert [row.id for row in rows] == [3]
    request = client.requests[0]
    assert request.min_date is None
    assert request.max_date is None
    assert request.from_id is None
    assert request.top_msg_id is None
    # No sender lookup when from_user is absent.
    assert client.entity_calls == [5]


@pytest.mark.asyncio
async def test_search_query_with_topic_only() -> None:
    client = _SearchingClient(
        [_SearchPage([_SearchMsg(8, date=_BASE, message="in topic")])]
    )
    backend = TelethonSearchBackend(client)

    rows = await backend.search_messages(
        chat_id=-100, query="topic", limit=3, topic_id=99
    )

    assert [row.text for row in rows] == ["in topic"]
    request = client.requests[0]
    assert request.q == "topic"
    assert request.top_msg_id == 99


@pytest.mark.asyncio
async def test_search_range_bounds_are_inclusive() -> None:
    client = _SearchingClient(
        [
            _SearchPage(
                [
                    _SearchMsg(4, date=_TO + timedelta(seconds=1), message="after"),
                    _SearchMsg(3, date=_TO, message="upper edge"),
                    _SearchMsg(2, date=_FROM, message="lower edge"),
                    _SearchMsg(1, date=_FROM - timedelta(seconds=1), message="before"),
                ]
            )
        ]
    )
    backend = TelethonSearchBackend(client)

    rows = await backend.search_messages(
        chat_id=1, query="edge", limit=10, from_date=_FROM, to_date=_TO
    )

    assert [row.id for row in rows] == [3, 2]


@pytest.mark.asyncio
async def test_search_pages_until_limit_collected_newest_first() -> None:
    out_of_range = _BASE + timedelta(hours=5)
    client = _SearchingClient(
        [
            _SearchPage(
                [
                    _SearchMsg(930, date=_BASE, message="one"),
                    _SearchMsg(929, date=out_of_range, message="skip"),
                    _SearchMsg(928, date=_BASE, message="two"),
                ]
            ),
            _SearchPage(
                [
                    # Overlapping id 928 must be deduped, not counted twice.
                    _SearchMsg(928, date=_BASE, message="two"),
                    _SearchMsg(927, date=_BASE, message="three"),
                    _SearchMsg(926, date=_BASE, message="four"),
                ]
            ),
        ]
    )
    backend = TelethonSearchBackend(client)

    rows = await backend.search_messages(
        chat_id=1, query="x", limit=3, from_date=_FROM, to_date=_TO
    )

    assert [row.id for row in rows] == [930, 928, 927]
    assert len(client.requests) == 2
    # The second page asks for messages older than the last one processed.
    assert client.requests[0].offset_id == 0
    assert client.requests[1].offset_id == 928


@pytest.mark.asyncio
async def test_search_stops_when_offset_does_not_advance() -> None:
    """A server replaying the same page must not loop forever."""
    stale = _BASE + timedelta(hours=5)
    client = _SearchingClient(
        [
            _SearchPage(
                [
                    _SearchMsg(910, date=stale, message="out"),
                    _SearchMsg(909, date=stale, message="out"),
                ]
            )
        ]
    )
    backend = TelethonSearchBackend(client)

    rows = await backend.search_messages(
        chat_id=1, query="x", limit=2, from_date=_FROM, to_date=_TO
    )

    assert rows == []
    assert len(client.requests) == 2


@pytest.mark.asyncio
async def test_search_stops_on_empty_page() -> None:
    client = _SearchingClient([_SearchPage([])])
    backend = TelethonSearchBackend(client)

    rows = await backend.search_messages(chat_id=1, query="nothing", limit=5)

    assert rows == []
    assert len(client.requests) == 1


@pytest.mark.asyncio
async def test_search_maps_sender_reply_and_media_fallback() -> None:
    client = _SearchingClient(
        [
            _SearchPage(
                [
                    _SearchMsg(
                        12,
                        date=_BASE,
                        message="",
                        from_id=_PeerUser(77),
                        reply_to=_ReplyHeader(11),
                        media=type("MessageMediaPhoto", (), {})(),
                    )
                ],
                users=[_User(77, "bob")],
            )
        ]
    )
    backend = TelethonSearchBackend(client)

    rows = await backend.search_messages(chat_id=1, query="pic", limit=5)

    row = rows[0]
    assert row.sender == "bob"
    assert row.reply_to == 11
    assert row.text == "[photo]"
    assert row.date == _BASE.isoformat()


@pytest.mark.asyncio
async def test_search_maps_channel_sender_from_result_chats() -> None:
    """A broadcast post has no ``from_id``; its sender lives in ``chats``.

    ``sender_id`` carries the ``-100`` marker there, so the envelope index must
    be keyed the same way or every channel hit reports ``sender: null`` while
    ``messages recent`` reports the channel for the very same message.
    """
    from telethon.tl.types import Channel

    channel = Channel(
        id=555,
        title="News",
        photo=None,
        date=_BASE,
        username="newschan",
    )
    client = _SearchingClient(
        [
            _SearchPage(
                [_SearchMsg(9, date=_BASE, message="release", sender_id=-1000000000555)],
                chats=[channel],
            )
        ]
    )
    backend = TelethonSearchBackend(client)

    rows = await backend.search_messages(chat_id=-1000000000555, query="release", limit=5)

    assert [row.sender for row in rows] == ["newschan"]


@pytest.mark.asyncio
async def test_search_maps_incoming_private_sender_without_from_id() -> None:
    """Layer 119+ drops ``from_id`` on private messages; ``sender_id`` remains."""
    client = _SearchingClient(
        [
            _SearchPage(
                [_SearchMsg(4, date=_BASE, message="hi", sender_id=77)],
                users=[_User(77, "bob")],
            )
        ]
    )
    backend = TelethonSearchBackend(client)

    rows = await backend.search_messages(chat_id=77, query="hi", limit=5)

    assert [row.sender for row in rows] == ["bob"]


@pytest.mark.asyncio
async def test_search_flood_wait_is_translated() -> None:
    class _Flooding(_SearchingClient):
        async def __call__(self, request: Any) -> Any:
            raise _TelethonFloodWaitError(20)

    backend = TelethonSearchBackend(_Flooding([_SearchPage([])]))
    with pytest.raises(FloodWaitError):
        await backend.search_messages(chat_id=1, query="x", limit=1)


# ---------------------------------------------------------------------------
# Private-chat sender filtering
#
# Telegram ignores `from_id` when the peer is a user, so the sender filter has
# to be re-applied locally or `--from-user` silently returns both sides.
# ---------------------------------------------------------------------------


class _PrivateSearchingClient(_SearchingClient):
    """Search double whose peers are real ``InputPeerUser`` objects."""

    def __init__(self, pages: list[_SearchPage], *, self_id: int = 1) -> None:
        super().__init__(pages)
        self._self_id = self_id

    async def get_input_entity(self, ref: Any) -> Any:
        from telethon.tl.types import InputPeerUser

        self.entity_calls.append(ref)
        user_id = self._self_id if ref == "me" else abs(int(ref)) if isinstance(ref, int) else 7
        return InputPeerUser(user_id=user_id, access_hash=0)

    async def get_me(self) -> Any:
        return _User(self._self_id, "self")


class _PrivateMsg(_SearchMsg):
    """Raw hit with the ``sender_id``/``out`` fields Telethon fills in."""

    def __init__(self, msg_id: int, *, sender_id: int | None, out: bool = False) -> None:
        super().__init__(msg_id, date=_BASE, message=f"m{msg_id}")
        self.sender_id = sender_id
        self.out = out


@pytest.mark.asyncio
async def test_private_chat_sender_filter_is_applied_locally() -> None:
    """`from_id` is dropped for a user peer and the filter runs on the rows."""
    client = _PrivateSearchingClient(
        [
            _SearchPage(
                [
                    _PrivateMsg(30, sender_id=7),
                    _PrivateMsg(29, sender_id=None, out=True),
                    _PrivateMsg(28, sender_id=7),
                ]
            )
        ]
    )
    backend = TelethonSearchBackend(client)

    rows = await backend.search_messages(chat_id=7, query="m", from_user=7, limit=10)

    # Telegram would have ignored a server-side from_id, so we must not send one.
    assert client.requests[0].from_id is None
    # Only the partner's messages survive — the outgoing one is ours.
    assert [row.id for row in rows] == [30, 28]


@pytest.mark.asyncio
async def test_private_chat_sender_filter_matches_own_outgoing_messages() -> None:
    """Outgoing private messages carry no ``from_id``; the sender is still us."""
    client = _PrivateSearchingClient(
        [
            _SearchPage(
                [
                    _PrivateMsg(30, sender_id=7),
                    _PrivateMsg(29, sender_id=None, out=True),
                ]
            )
        ],
        self_id=1,
    )
    backend = TelethonSearchBackend(client)

    rows = await backend.search_messages(chat_id=7, query="m", from_user=1, limit=10)

    assert [row.id for row in rows] == [29]


@pytest.mark.asyncio
async def test_group_search_still_pushes_from_id_server_side() -> None:
    """Non-user peers keep the server-side filter — no local second-guessing."""
    client = _SearchingClient([_SearchPage([_SearchMsg(10, date=_BASE, message="hit")])])
    backend = TelethonSearchBackend(client)

    rows = await backend.search_messages(
        chat_id=-100777, query="hit", from_user="@bob", limit=5
    )

    assert client.requests[0].from_id == "peer:@bob"
    assert [row.id for row in rows] == [10]


@pytest.mark.asyncio
async def test_private_chat_third_party_sender_answers_without_paging() -> None:
    """A 1:1 chat has two senders, so a third party matches nothing — no RPC.

    The local filter would discard every row anyway, and paging on would walk
    the chat's whole match set to return the same empty list.
    """
    client = _PrivateSearchingClient(
        [_SearchPage([_PrivateMsg(30, sender_id=7)])], self_id=1
    )
    backend = TelethonSearchBackend(client)

    rows = await backend.search_messages(chat_id=7, query="m", from_user=99, limit=10)

    assert rows == []
    assert client.requests == []


@pytest.mark.asyncio
async def test_search_page_width_survives_a_small_limit() -> None:
    """A small `limit` must not shrink how far the RPC cap can scan.

    Locally filtered rows do not count toward `limit`, so a wire page tied to
    `limit` would cut the `_SEARCH_MAX_PAGES` budget to `limit * pages`
    messages: `--limit 1` would answer `[]` on a chat where `--limit 20` finds
    the very same message.
    """
    client = _PrivateSearchingClient(
        [
            _SearchPage(
                # The wanted row sits behind a run of our own messages, which
                # only the *local* sender filter can drop.
                [_PrivateMsg(930 - i, sender_id=None, out=True) for i in range(20)]
                + [_PrivateMsg(900, sender_id=7)]
            )
        ],
        self_id=1,
    )
    backend = TelethonSearchBackend(client)

    rows = await backend.search_messages(chat_id=7, query="m", from_user=7, limit=1)

    assert client.requests[0].limit == _SEARCH_PAGE_SIZE
    assert [row.id for row in rows] == [900]


@pytest.mark.asyncio
async def test_search_page_width_follows_limit_without_local_filters() -> None:
    """With nothing filtered locally the wire page stays as small as `limit`."""
    client = _SearchingClient([_SearchPage([_SearchMsg(910, date=_BASE, message="a")])])
    backend = TelethonSearchBackend(client)

    await backend.search_messages(chat_id=-100777, query="a", limit=3)

    assert client.requests[0].limit == 3


@pytest.mark.asyncio
async def test_search_paging_is_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    """Locally filtered rows must not buy unbounded pages of RPCs."""
    from telegram_assistant.messages import telethon_backend as backend_module

    monkeypatch.setattr(backend_module, "_SEARCH_MAX_PAGES", 3)
    monkeypatch.setattr(backend_module, "_SEARCH_PAGE_SIZE", 2)
    # Every page is full and its offset keeps advancing, but the date bounds
    # drop every row — the only thing that can stop the loop is the cap.
    stale = _BASE - timedelta(days=30)
    pages = [
        _SearchPage([_SearchMsg(100 - i * 2, date=stale, message="m"),
                     _SearchMsg(99 - i * 2, date=stale, message="m")])
        for i in range(10)
    ]
    client = _SearchingClient(pages)
    backend = TelethonSearchBackend(client)

    rows = await backend.search_messages(
        chat_id=-100777, query="m", limit=10, from_date=_FROM, to_date=_TO
    )

    assert rows == []
    assert len(client.requests) == 3
