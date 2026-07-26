"""Telethon-backed :class:`FolderBackend` implementation.

Kept separate from :mod:`service` so domain functions and tests stay free of
Telethon imports. Telethon's representation of dialog filters varies between
versions — newer Telethon returns a ``messages.DialogFilters`` wrapper with a
``.filters`` attribute, older releases return a plain list — so the adapter
normalises both shapes into :class:`FolderSnapshot`.
"""

from __future__ import annotations

from typing import Any

from telegram_assistant.folders.service import (
    FolderChat,
    FolderChats,
    FolderNotFoundError,
    FolderSnapshot,
)
from telegram_assistant.telegram_client.errors import translate_flood_wait


def _normalise_title(value: Any) -> str:
    """Return a string title for a folder filter regardless of Telethon shape."""
    if value is None:
        return ""
    text = getattr(value, "text", value)
    return str(text)


def _peer_chat_id(peer: Any) -> int | None:
    """Return the bare chat id carried by an ``InputPeer*`` object.

    ``InputPeerChannel.channel_id`` / ``InputPeerChat.chat_id`` /
    ``InputPeerUser.user_id`` already hold the numeric id in bare (positive)
    form, so membership checks can read it directly without a ``get_entity``
    round-trip. Returns ``None`` for peer shapes that carry none of these.
    """
    for attr in ("channel_id", "chat_id", "user_id"):
        value = getattr(peer, attr, None)
        if value is not None:
            return int(value)
    return None


def _is_self_peer(peer: Any) -> bool:
    """Return ``True`` for ``InputPeerSelf`` — the only id-less peer shape.

    Telegram serialises "Saved Messages" inside a folder as ``InputPeerSelf``,
    which carries no ``user_id``, so :func:`_peer_chat_id` cannot read one.
    """
    return type(peer).__name__ == "InputPeerSelf"


def _entity_title(entity: Any) -> str:
    title = getattr(entity, "title", None)
    if title:
        return str(title)
    parts = [
        getattr(entity, "first_name", None) or "",
        getattr(entity, "last_name", None) or "",
    ]
    joined = " ".join(p for p in parts if p).strip()
    return joined or str(getattr(entity, "username", "") or "")


class TelethonFolderBackend:
    """Adapter from the Telethon ``TelegramClient`` to :class:`FolderBackend`."""

    def __init__(self, client: Any) -> None:
        self._client = client

    async def _fetch_filters(self) -> list[Any]:
        from telethon.tl.functions.messages import GetDialogFiltersRequest

        try:
            result = await self._client(GetDialogFiltersRequest())
        except Exception as exc:
            raise translate_flood_wait(exc) from exc
        filters = getattr(result, "filters", result)
        return list(filters)

    async def _own_user_id(self) -> int | None:
        """Return the signed-in account's bare user id (``None`` if unavailable)."""
        try:
            me = await self._client.get_me()
        except Exception as exc:
            raise translate_flood_wait(exc) from exc
        user_id = getattr(me, "id", None)
        return None if user_id is None else int(user_id)

    async def list_folders(self) -> list[FolderSnapshot]:
        snapshots: list[FolderSnapshot] = []
        for f in await self._fetch_filters():
            folder_id = getattr(f, "id", None)
            raw_title = getattr(f, "title", None)
            if folder_id is None or raw_title is None:
                # DialogFilterDefault has neither id nor title — it's the
                # built-in "All chats" filter that the user can't move chats
                # into. Skip silently.
                continue
            include = list(getattr(f, "include_peers", []) or [])
            pinned = list(getattr(f, "pinned_peers", []) or [])
            chats: list[FolderChat] = []
            for peer in pinned + include:
                try:
                    entity = await self._client.get_entity(peer)
                except Exception:
                    continue
                chat_id = getattr(entity, "id", None)
                if chat_id is None:
                    continue
                chats.append(
                    FolderChat(chat_id=int(chat_id), title=_entity_title(entity))
                )
            snapshots.append(
                FolderSnapshot(
                    folder_id=int(folder_id),
                    folder_name=_normalise_title(raw_title),
                    chats=chats,
                )
            )
        return snapshots

    async def list_folder_chat_ids(self) -> list[FolderChats]:
        """Return one :class:`FolderChats` entry per folder, keyed by folder id.

        Fetches the dialog filters once (:meth:`_fetch_filters`, already wrapped
        in ``translate_flood_wait``) and reads bare peer ids straight from the
        ``InputPeer*`` objects — **no** ``get_entity`` calls. This is the fast
        path the authorizer uses for folder-rule membership checks, which only
        need ids, never titles. ``list_folders`` (which does resolve titles) is
        left untouched for ``folders inspect``.

        Entries carry the folder's stable ``id`` alongside its title: Telegram
        permits two folders with the same title, and a title-keyed mapping would
        silently drop all but the last of them — wrongly denying access to every
        chat in the shadowed folder.

        ``InputPeerSelf`` (Saved Messages) is the one peer shape that carries no
        numeric id; it is resolved once via ``get_me`` — dropping it would leave
        Saved Messages out of its folder and deny every rule that grants the
        folder, for the whole cache TTL and every process sharing it.
        """
        memberships: list[FolderChats] = []
        self_id: int | None = None
        for f in await self._fetch_filters():
            folder_id = getattr(f, "id", None)
            raw_title = getattr(f, "title", None)
            if folder_id is None or raw_title is None:
                # DialogFilterDefault ("All chats") — no id/title, skip.
                continue
            include = list(getattr(f, "include_peers", []) or [])
            pinned = list(getattr(f, "pinned_peers", []) or [])
            ids: set[int] = set()
            for peer in pinned + include:
                chat_id = _peer_chat_id(peer)
                if chat_id is None and _is_self_peer(peer):
                    if self_id is None:
                        self_id = await self._own_user_id()
                    chat_id = self_id
                if chat_id is not None:
                    ids.add(chat_id)
            memberships.append(
                FolderChats(
                    folder_id=int(folder_id),
                    folder_name=_normalise_title(raw_title),
                    chat_ids=ids,
                )
            )
        return memberships

    async def resolve_chat(self, chat_ref: str | int) -> FolderChat:
        try:
            entity = await self._client.get_entity(chat_ref)
        except Exception as exc:
            raise translate_flood_wait(exc) from exc
        chat_id = getattr(entity, "id", None)
        if chat_id is None:
            raise ValueError(f"resolved entity for {chat_ref!r} has no id")
        return FolderChat(chat_id=int(chat_id), title=_entity_title(entity))

    async def add_chat_to_folder(self, folder_id: int, chat_id: int) -> None:
        from telethon.tl.functions.messages import (
            UpdateDialogFilterRequest,
        )

        filters = await self._fetch_filters()
        target = next(
            (f for f in filters if getattr(f, "id", None) == folder_id),
            None,
        )
        if target is None:
            raise FolderNotFoundError(
                f"folder id {folder_id} no longer exists in Telegram folder list"
            )
        try:
            input_peer = await self._client.get_input_entity(chat_id)
        except Exception as exc:
            raise translate_flood_wait(exc) from exc
        include_peers = list(getattr(target, "include_peers", []) or [])
        if input_peer not in include_peers:
            include_peers.append(input_peer)
            target.include_peers = include_peers
        try:
            await self._client(
                UpdateDialogFilterRequest(id=folder_id, filter=target)
            )
        except Exception as exc:
            raise translate_flood_wait(exc) from exc

    async def remove_chat_from_folder(self, folder_id: int, chat_id: int) -> None:
        from telethon.tl.functions.messages import (
            UpdateDialogFilterRequest,
        )

        filters = await self._fetch_filters()
        target = next(
            (f for f in filters if getattr(f, "id", None) == folder_id),
            None,
        )
        if target is None:
            raise FolderNotFoundError(
                f"folder id {folder_id} no longer exists in Telegram folder list"
            )
        try:
            input_peer = await self._client.get_input_entity(chat_id)
        except Exception as exc:
            raise translate_flood_wait(exc) from exc
        # A chat can be referenced from both include_peers and pinned_peers; drop
        # it from each so the chat fully leaves the folder.
        include_peers = [
            p
            for p in (getattr(target, "include_peers", []) or [])
            if p != input_peer
        ]
        pinned_peers = [
            p
            for p in (getattr(target, "pinned_peers", []) or [])
            if p != input_peer
        ]
        target.include_peers = include_peers
        target.pinned_peers = pinned_peers
        try:
            await self._client(
                UpdateDialogFilterRequest(id=folder_id, filter=target)
            )
        except Exception as exc:
            raise translate_flood_wait(exc) from exc
