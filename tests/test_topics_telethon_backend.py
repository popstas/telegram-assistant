"""Unit tests for the Telethon adapter that creates / lists / closes topics.

Telethon shipped the forum-topic requests under two different module paths
across versions and renamed the chat-entity kwarg from ``channel`` to
``peer``. The adapter routes around both via ``_import_forum_request`` and
``_peer_kwarg`` — these tests cover the version-detection paths so the live
e2e script doesn't have to be the first place those shims fail.
"""

from __future__ import annotations

import inspect
from typing import Any

import pytest

from telegram_assistant.topics import telethon_backend as tb
from telegram_assistant.topics.telethon_backend import (
    TelethonTopicBackend,
    _extract_topic_id,
    _peer_kwarg,
)


class _Peer:
    def __init__(self, chat_id: int) -> None:
        self.chat_id = chat_id


class _RecordingClient:
    """Telethon client double that captures the request objects sent through it."""

    def __init__(self, response: Any = None) -> None:
        self._response = response
        self.calls: list[Any] = []
        self._peer_lookups: list[int] = []

    async def get_input_entity(self, chat_id: int) -> _Peer:
        self._peer_lookups.append(chat_id)
        return _Peer(chat_id)

    async def __call__(self, request: Any) -> Any:
        self.calls.append(request)
        return self._response

    async def send_message(
        self, chat_id: int, text: str, **kwargs: Any
    ) -> Any:
        self.calls.append(("send_message", chat_id, text, kwargs))

        class _Sent:
            id = 4242

        return _Sent()


class _PeerRequest:
    """Telethon-1.43+ style request that takes ``peer`` + ``random_id``."""

    def __init__(self, *, peer: Any, title: str, random_id: int) -> None:
        self.peer = peer
        self.title = title
        self.random_id = random_id


class _ChannelRequest:
    """Legacy Telethon style: ``channel`` kwarg, no ``random_id``."""

    def __init__(self, *, channel: Any, title: str) -> None:
        self.channel = channel
        self.title = title


def test_peer_kwarg_picks_peer_when_request_has_peer() -> None:
    assert _peer_kwarg(_PeerRequest, "peer-obj") == {"peer": "peer-obj"}


def test_peer_kwarg_picks_channel_when_request_has_channel() -> None:
    assert _peer_kwarg(_ChannelRequest, "chan-obj") == {"channel": "chan-obj"}


def test_peer_kwarg_raises_when_neither_supported() -> None:
    class _Bogus:
        def __init__(self, *, foo: str) -> None: ...

    with pytest.raises(RuntimeError, match="neither `peer` nor `channel`"):
        _peer_kwarg(_Bogus, "x")


def test_extract_topic_id_finds_id_in_updates() -> None:
    class _Upd:
        def __init__(self, id_value: int) -> None:
            self.id = id_value

    class _Wrapper:
        def __init__(self) -> None:
            self.updates = [_Upd(0), _Upd(99)]

    assert _extract_topic_id(_Wrapper()) == 99


def test_extract_topic_id_falls_back_to_message_field() -> None:
    class _Upd:
        def __init__(self, message_value: int) -> None:
            self.message = message_value

    class _Wrapper:
        def __init__(self) -> None:
            self.updates = [_Upd(7)]

    assert _extract_topic_id(_Wrapper()) == 7


def test_extract_topic_id_raises_when_updates_empty() -> None:
    class _Wrapper:
        updates: list[Any] = []

    with pytest.raises(RuntimeError, match="did not return a topic id"):
        _extract_topic_id(_Wrapper())


@pytest.mark.asyncio
async def test_create_topic_uses_peer_and_random_id_when_supported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Newer Telethon: signature is ``(peer, title, random_id)``."""
    response = type("R", (), {"updates": [type("U", (), {"id": 11})()]})()
    client = _RecordingClient(response=response)
    monkeypatch.setattr(
        tb,
        "_import_forum_request",
        lambda name: _PeerRequest if name == "CreateForumTopicRequest" else None,
    )

    backend = TelethonTopicBackend(client)
    topic_id = await backend.create_topic(chat_id=42, name="hello")

    assert topic_id == 11
    assert client._peer_lookups == [42]
    assert len(client.calls) == 1
    sent = client.calls[0]
    assert isinstance(sent, _PeerRequest)
    assert sent.title == "hello"
    assert sent.peer.chat_id == 42
    # random_id must be a positive 63-bit int
    params = inspect.signature(_PeerRequest).parameters
    assert "random_id" in params
    assert isinstance(sent.random_id, int) and sent.random_id > 0


@pytest.mark.asyncio
async def test_create_topic_falls_back_to_channel_kwarg(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Legacy Telethon: signature is ``(channel, title)``."""
    response = type("R", (), {"updates": [type("U", (), {"id": 5})()]})()
    client = _RecordingClient(response=response)
    monkeypatch.setattr(
        tb,
        "_import_forum_request",
        lambda name: _ChannelRequest if name == "CreateForumTopicRequest" else None,
    )

    backend = TelethonTopicBackend(client)
    topic_id = await backend.create_topic(chat_id=7, name="hi")

    assert topic_id == 5
    assert isinstance(client.calls[0], _ChannelRequest)
    assert client.calls[0].channel.chat_id == 7
    assert client.calls[0].title == "hi"


class _FloodingClient:
    """Telethon client double that always raises a fake ``FloodWaitError``."""

    def __init__(self) -> None:
        # Define a class named exactly "FloodWaitError" so the translator's
        # name-based detection matches without us having to import telethon.
        class FloodWaitError(Exception):
            def __init__(self, seconds: int) -> None:
                super().__init__(f"FLOOD_WAIT_{seconds}")
                self.seconds = seconds

        self._fw_cls = FloodWaitError

    async def get_input_entity(self, chat_id: int) -> _Peer:
        return _Peer(chat_id)

    async def __call__(self, request: Any) -> Any:
        raise self._fw_cls(seconds=7)

    async def send_message(self, *args: Any, **kwargs: Any) -> Any:
        raise self._fw_cls(seconds=11)


@pytest.mark.asyncio
async def test_create_topic_translates_flood_wait(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Telethon's FloodWaitError must surface as the queue's FloodWaitError.

    The queue only pauses-and-retries on its own ``FloodWaitError`` signal; a
    bare upstream exception would mark the operation failed instead, defeating
    rate-limit handling.
    """
    from telegram_assistant.worker.queue import FloodWaitError

    monkeypatch.setattr(
        tb,
        "_import_forum_request",
        lambda name: _PeerRequest if name == "CreateForumTopicRequest" else None,
    )

    backend = TelethonTopicBackend(_FloodingClient())
    with pytest.raises(FloodWaitError) as excinfo:
        await backend.create_topic(chat_id=42, name="hello")
    assert excinfo.value.seconds == 7.0


@pytest.mark.asyncio
async def test_send_message_translates_flood_wait() -> None:
    from telegram_assistant.worker.queue import FloodWaitError

    backend = TelethonTopicBackend(_FloodingClient())
    with pytest.raises(FloodWaitError) as excinfo:
        await backend.send_message(chat_id=1, text="hi")
    assert excinfo.value.seconds == 11.0


class _ResolveFloodingClient:
    """Client where ``get_input_entity`` raises Telethon's FloodWaitError.

    Telethon's peer resolver can itself RPC for unknown usernames/peers and
    surface FLOOD_WAIT; the backend must translate that case too — not just
    the trailing request call.
    """

    def __init__(self) -> None:
        class FloodWaitError(Exception):
            def __init__(self, seconds: int) -> None:
                super().__init__(f"FLOOD_WAIT_{seconds}")
                self.seconds = seconds

        self._fw_cls = FloodWaitError

    async def get_input_entity(self, chat_id: int) -> Any:
        raise self._fw_cls(seconds=13)

    async def __call__(self, request: Any) -> Any:
        raise AssertionError("request should not run after get_input_entity FLOOD_WAIT")


@pytest.mark.asyncio
async def test_create_topic_translates_flood_wait_on_resolver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A FLOOD_WAIT raised by get_input_entity must surface as the project
    FloodWaitError, not the raw Telethon class. Otherwise the queue treats
    it as a generic terminal failure.
    """
    from telegram_assistant.worker.queue import FloodWaitError

    monkeypatch.setattr(
        tb,
        "_import_forum_request",
        lambda name: _PeerRequest if name == "CreateForumTopicRequest" else None,
    )

    backend = TelethonTopicBackend(_ResolveFloodingClient())
    with pytest.raises(FloodWaitError) as excinfo:
        await backend.create_topic(chat_id=42, name="hello")
    assert excinfo.value.seconds == 13.0


class _PagingForumTopicsRequest:
    def __init__(
        self,
        *,
        peer: Any,
        offset_date: Any,
        offset_id: int,
        offset_topic: int,
        limit: int,
    ) -> None:
        self.peer = peer
        self.offset_date = offset_date
        self.offset_id = offset_id
        self.offset_topic = offset_topic
        self.limit = limit


class _PagingTopicsClient:
    """Returns a multi-page topic list; advances pagination by ``offset_topic``."""

    def __init__(self, total: int, page_size: int = 100) -> None:
        self._page_size = page_size
        # Pre-build topic stand-ins with monotonically increasing ids.
        self._topics = [
            type(
                "T",
                (),
                {
                    "id": i,
                    "title": f"t{i}",
                    "closed": False,
                    "top_message": i * 10,
                    "date": None,
                },
            )()
            for i in range(1, total + 1)
        ]
        self.calls = 0

    async def get_input_entity(self, chat_id: int) -> _Peer:
        return _Peer(chat_id)

    async def __call__(self, request: Any) -> Any:
        self.calls += 1
        start = request.offset_topic
        page = [t for t in self._topics if t.id > start][: request.limit]
        return type("Result", (), {"topics": page})()


class _IterMessagesClient:
    """Client double recording ``iter_messages`` kwargs and yielding messages."""

    def __init__(self, messages: list[Any]) -> None:
        self._messages = messages
        self.iter_calls: list[dict[str, Any]] = []
        self.deleted: list[tuple[Any, list[int]]] = []

    async def get_input_entity(self, chat_id: int) -> _Peer:
        return _Peer(chat_id)

    def iter_messages(self, channel: Any, **kwargs: Any) -> Any:
        self.iter_calls.append({"channel": channel, **kwargs})

        async def _gen() -> Any:
            for m in self._messages:
                yield m

        return _gen()

    async def delete_messages(self, channel: Any, ids: list[int]) -> None:
        self.deleted.append((channel, list(ids)))


def _msg(msg_id: int, *, username: str | None, reply_to: int | None, text: str) -> Any:
    sender = type("S", (), {"username": username})() if username is not None else None
    return type(
        "M",
        (),
        {"id": msg_id, "sender": sender, "reply_to_msg_id": reply_to, "message": text},
    )()


@pytest.mark.asyncio
async def test_get_recent_messages_scopes_to_topic_and_maps_fields() -> None:
    messages = [
        _msg(201, username="planfix_bot", reply_to=200, text="ok"),
        _msg(200, username=None, reply_to=555, text="/task 9"),
        _msg(198, username="planfix_bot", reply_to=None, text="welcome"),
    ]
    client = _IterMessagesClient(messages)
    backend = TelethonTopicBackend(client)

    out = await backend.get_recent_messages(chat_id=-100, limit=20, topic_id=555)

    # The scan is scoped to the topic thread via ``reply_to``.
    assert client.iter_calls[0]["limit"] == 20
    assert client.iter_calls[0]["reply_to"] == 555
    assert out[0] == {
        "id": 201,
        "sender_username": "planfix_bot",
        "reply_to_msg_id": 200,
        "text": "ok",
    }
    # A message with no sender maps to ``sender_username = None``.
    assert out[1]["sender_username"] is None


@pytest.mark.asyncio
async def test_get_recent_messages_unscoped_without_topic_id() -> None:
    client = _IterMessagesClient([])
    backend = TelethonTopicBackend(client)

    await backend.get_recent_messages(chat_id=-100, limit=5)

    assert "reply_to" not in client.iter_calls[0]
    assert client.iter_calls[0]["limit"] == 5


@pytest.mark.asyncio
async def test_delete_messages_forwards_ids_and_skips_empty() -> None:
    client = _IterMessagesClient([])
    backend = TelethonTopicBackend(client)

    await backend.delete_messages(chat_id=-100, message_ids=[198, 200, 201])
    assert client.deleted == [(_PeerEq(-100), [198, 200, 201])]

    await backend.delete_messages(chat_id=-100, message_ids=[])
    # No second delete call for an empty id list.
    assert len(client.deleted) == 1


class _PeerEq:
    """Compares equal to a ``_Peer`` with the same chat_id (delete target)."""

    def __init__(self, chat_id: int) -> None:
        self.chat_id = chat_id

    def __eq__(self, other: Any) -> bool:
        return getattr(other, "chat_id", None) == self.chat_id


@pytest.mark.asyncio
async def test_list_topics_paginates_beyond_first_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        tb,
        "_import_forum_request",
        lambda name: (
            _PagingForumTopicsRequest if name == "GetForumTopicsRequest" else None
        ),
    )
    client = _PagingTopicsClient(total=237)
    backend = TelethonTopicBackend(client)

    topics = await backend.list_topics(chat_id=1)

    assert len(topics) == 237
    assert topics[0].topic_id == 1
    assert topics[-1].topic_id == 237
    # 237 over 100/page => 3 calls (100, 100, 37 stops).
    assert client.calls == 3
