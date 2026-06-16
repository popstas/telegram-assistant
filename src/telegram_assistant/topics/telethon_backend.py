"""Telethon-backed :class:`TopicBackend` implementation.

Kept separate from :mod:`service` so the domain layer stays free of Telethon
imports.
"""

from __future__ import annotations

import inspect
import secrets
from collections.abc import Sequence
from typing import Any

from telegram_assistant.telegram_client.errors import translate_flood_wait
from telegram_assistant.topics.service import TopicSummary


def _import_forum_request(name: str) -> Any:
    """Locate a forum-topic request type across Telethon versions.

    Telethon shipped these requests under ``tl.functions.channels`` in older
    builds and moved them to ``tl.functions.messages`` later. We try both so
    the adapter works against whichever Telethon the host has installed.
    """
    try:
        module = __import__(
            "telethon.tl.functions.channels", fromlist=[name]
        )
        return getattr(module, name)
    except (ImportError, AttributeError):
        module = __import__(
            "telethon.tl.functions.messages", fromlist=[name]
        )
        return getattr(module, name)


def _peer_kwarg(request_cls: Any, peer: Any) -> dict[str, Any]:
    """Return the entity kwarg for ``request_cls`` (``peer`` vs ``channel``)."""
    params = inspect.signature(request_cls).parameters
    if "peer" in params:
        return {"peer": peer}
    if "channel" in params:
        return {"channel": peer}
    raise RuntimeError(
        f"{request_cls.__name__} accepts neither `peer` nor `channel`"
    )


def _extract_topic_id(updates: Any) -> int:
    """Find the new topic id inside the Telethon ``Updates`` result.

    ``CreateForumTopicRequest`` returns an ``Updates`` envelope whose
    ``updates`` list contains ``UpdateMessageID`` carrying the new topic's
    root message id — which Telegram also uses as the topic id.
    """
    for update in getattr(updates, "updates", None) or []:
        topic_id = getattr(update, "id", None)
        if topic_id is None:
            topic_id = getattr(update, "message", None)
        if isinstance(topic_id, int) and topic_id > 0:
            return topic_id
    raise RuntimeError("Telegram did not return a topic id for the new topic")


class TelethonTopicBackend:
    """Adapter from the Telethon ``TelegramClient`` to :class:`TopicBackend`."""

    def __init__(self, client: Any) -> None:
        self._client = client

    async def create_topic(self, *, chat_id: int, name: str) -> int:
        # Wrap the whole body — `get_input_entity` can RPC for unknown peers
        # and surface Telethon's own `FloodWaitError`, which the worker queue
        # only recognises after `translate_flood_wait`.
        try:
            request_cls = _import_forum_request("CreateForumTopicRequest")
            peer = await self._client.get_input_entity(chat_id)
            kwargs: dict[str, Any] = {**_peer_kwarg(request_cls, peer), "title": name}
            params = inspect.signature(request_cls).parameters
            if "random_id" in params:
                # Telethon's newer signature requires an explicit random_id so that
                # replays don't accidentally create duplicates server-side.
                kwargs["random_id"] = secrets.randbits(63)
            result = await self._client(request_cls(**kwargs))
        except Exception as exc:
            raise translate_flood_wait(exc) from exc
        return _extract_topic_id(result)

    async def send_message(
        self, *, chat_id: int, text: str, topic_id: int | None = None
    ) -> int:
        kwargs: dict[str, Any] = {}
        if topic_id is not None:
            kwargs["reply_to"] = topic_id
        try:
            sent = await self._client.send_message(chat_id, text, **kwargs)
        except Exception as exc:
            raise translate_flood_wait(exc) from exc
        return int(getattr(sent, "id", 0))

    async def get_recent_messages(
        self, *, chat_id: int, limit: int, topic_id: int | None = None
    ) -> list[dict[str, Any]]:
        # When ``topic_id`` is set, scope the scan to that forum topic thread so
        # cleanup never touches messages from sibling topics. Telethon's
        # ``reply_to`` filter on ``iter_messages`` selects a single topic thread.
        try:
            channel = await self._client.get_input_entity(chat_id)
            kwargs: dict[str, Any] = {"limit": limit}
            if topic_id is not None:
                kwargs["reply_to"] = topic_id
            out: list[dict[str, Any]] = []
            async for msg in self._client.iter_messages(channel, **kwargs):
                sender = getattr(msg, "sender", None)
                username = (
                    getattr(sender, "username", None) if sender is not None else None
                )
                reply_to = getattr(msg, "reply_to_msg_id", None)
                out.append(
                    {
                        "id": int(getattr(msg, "id", 0)),
                        "sender_username": username,
                        "reply_to_msg_id": int(reply_to) if reply_to else None,
                        "text": getattr(msg, "message", "") or "",
                    }
                )
        except Exception as exc:
            raise translate_flood_wait(exc) from exc
        return out

    async def delete_messages(
        self, *, chat_id: int, message_ids: Sequence[int]
    ) -> None:
        ids = [int(m) for m in message_ids]
        if not ids:
            return
        try:
            channel = await self._client.get_input_entity(chat_id)
            await self._client.delete_messages(channel, ids)
        except Exception as exc:
            raise translate_flood_wait(exc) from exc

    async def close_topic(self, *, chat_id: int, topic_id: int) -> None:
        try:
            request_cls = _import_forum_request("EditForumTopicRequest")
            peer = await self._client.get_input_entity(chat_id)
            kwargs: dict[str, Any] = {
                **_peer_kwarg(request_cls, peer),
                "topic_id": topic_id,
                "closed": True,
            }
            await self._client(request_cls(**kwargs))
        except Exception as exc:
            raise translate_flood_wait(exc) from exc

    async def open_topic(self, *, chat_id: int, topic_id: int) -> None:
        try:
            request_cls = _import_forum_request("EditForumTopicRequest")
            peer = await self._client.get_input_entity(chat_id)
            kwargs: dict[str, Any] = {
                **_peer_kwarg(request_cls, peer),
                "topic_id": topic_id,
                "closed": False,
            }
            await self._client(request_cls(**kwargs))
        except Exception as exc:
            raise translate_flood_wait(exc) from exc

    async def rename_topic(self, *, chat_id: int, topic_id: int, title: str) -> None:
        try:
            request_cls = _import_forum_request("EditForumTopicRequest")
            peer = await self._client.get_input_entity(chat_id)
            kwargs: dict[str, Any] = {
                **_peer_kwarg(request_cls, peer),
                "topic_id": topic_id,
                "title": title,
            }
            await self._client(request_cls(**kwargs))
        except Exception as exc:
            raise translate_flood_wait(exc) from exc

    async def list_topics(self, *, chat_id: int) -> list[TopicSummary]:
        try:
            request_cls = _import_forum_request("GetForumTopicsRequest")
            peer = await self._client.get_input_entity(chat_id)
        except Exception as exc:
            raise translate_flood_wait(exc) from exc
        page_size = 100
        offset_date: Any = None
        offset_id = 0
        offset_topic = 0
        out: list[TopicSummary] = []
        # Paginate until Telegram returns fewer than ``page_size`` items.
        # Without this, long-lived Planfix groups with >100 topics would
        # silently truncate the list and resolve-by-name lookups would
        # raise spurious TopicNotFoundError.
        while True:
            kwargs: dict[str, Any] = {
                **_peer_kwarg(request_cls, peer),
                "offset_date": offset_date,
                "offset_id": offset_id,
                "offset_topic": offset_topic,
                "limit": page_size,
            }
            try:
                result = await self._client(request_cls(**kwargs))
            except Exception as exc:
                raise translate_flood_wait(exc) from exc
            page = list(getattr(result, "topics", None) or [])
            for topic in page:
                topic_id = getattr(topic, "id", None)
                title = getattr(topic, "title", None)
                closed = bool(getattr(topic, "closed", False))
                if isinstance(topic_id, int) and isinstance(title, str):
                    out.append(TopicSummary(topic_id=topic_id, title=title, closed=closed))
            if len(page) < page_size:
                break
            last = page[-1]
            last_id = getattr(last, "id", None)
            if not isinstance(last_id, int) or last_id == offset_topic:
                # Defensive guard: if the server doesn't advance the cursor,
                # stop rather than spinning forever.
                break
            offset_topic = last_id
            top_message = getattr(last, "top_message", None)
            offset_id = int(top_message) if isinstance(top_message, int) else 0
            offset_date = getattr(last, "date", None)
        return out
