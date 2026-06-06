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


def _peer_key(peer: Any) -> tuple[str, int] | None:
    for attr in ("channel_id", "chat_id", "user_id"):
        value = getattr(peer, attr, None)
        if value is not None:
            return attr, int(value)
    return None


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
        input_key = _peer_key(input_peer)

        def _without_target(peers: list[Any]) -> list[Any]:
            result: list[Any] = []
            for peer in peers:
                if peer == input_peer:
                    continue
                if input_key is not None and _peer_key(peer) == input_key:
                    continue
                result.append(peer)
            return result

        target.include_peers = _without_target(
            list(getattr(target, "include_peers", []) or [])
        )
        target.pinned_peers = _without_target(
            list(getattr(target, "pinned_peers", []) or [])
        )
        try:
            await self._client(
                UpdateDialogFilterRequest(id=folder_id, filter=target)
            )
        except Exception as exc:
            raise translate_flood_wait(exc) from exc
