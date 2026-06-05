"""Telethon-backed :class:`ResolverBackend` implementation.

Kept separate from :mod:`service` so the domain layer and tests stay free of
Telethon imports. The adapter implements the three low-level lookups the
resolver orchestrates (numeric id, string handle/link/phone, exact title) and
translates Telethon's ``FloodWaitError`` into the project's queue signal so the
worker can pause-and-retry rather than treating a throttle as a hard failure.
"""

from __future__ import annotations

from typing import Any

from telegram_assistant.entities.service import (
    CachingEntityResolver,
    ResolvedEntity,
)
from telegram_assistant.telegram_client.errors import translate_flood_wait


def _entity_title(entity: Any) -> str:
    """Best-effort display title for a channel, group, or user entity."""
    title = getattr(entity, "title", None)
    if title:
        return str(title)
    parts = [
        getattr(entity, "first_name", None) or "",
        getattr(entity, "last_name", None) or "",
    ]
    joined = " ".join(p for p in parts if p).strip()
    return joined or str(getattr(entity, "username", "") or "")


def _entity_kind(entity: Any) -> str:
    """Classify an entity as ``"user"``, ``"channel"``, or ``"chat"``.

    Users have no ``title``; channels/supergroups carry the ``broadcast`` /
    ``megagroup`` flags that a basic-group ``Chat`` lacks.
    """
    if getattr(entity, "title", None) is None:
        return "user"
    if hasattr(entity, "broadcast") or hasattr(entity, "megagroup"):
        return "channel"
    return "chat"


def _to_resolved(entity: Any) -> ResolvedEntity | None:
    chat_id = getattr(entity, "id", None)
    if chat_id is None:
        return None
    return ResolvedEntity(
        chat_id=int(chat_id),
        title=_entity_title(entity),
        kind=_entity_kind(entity),
        username=getattr(entity, "username", None) or None,
    )


class TelethonResolverBackend:
    """Adapter from a Telethon ``TelegramClient`` to :class:`ResolverBackend`."""

    def __init__(self, client: Any) -> None:
        self._client = client

    async def resolve_by_id(self, numeric_id: int) -> ResolvedEntity | None:
        from telethon.tl.types import PeerChannel, PeerChat, PeerUser

        # Probe each peer type in turn; a wrong guess raises a benign lookup
        # error we skip, but a FLOOD_WAIT must surface immediately.
        for peer_cls in (PeerChannel, PeerChat, PeerUser):
            try:
                entity = await self._client.get_entity(peer_cls(numeric_id))
            except Exception as exc:
                translated = translate_flood_wait(exc)
                if translated is not exc:
                    raise translated from exc
                continue
            resolved = _to_resolved(entity)
            if resolved is not None:
                return resolved

        # No peer-type probe matched — scan the dialog list for the bare id.
        try:
            async for dialog in self._client.iter_dialogs():
                entity = getattr(dialog, "entity", None)
                if entity is None:
                    continue
                if int(getattr(entity, "id", 0)) == numeric_id:
                    return _to_resolved(entity)
        except Exception as exc:
            raise translate_flood_wait(exc) from exc
        return None

    async def resolve_by_handle(self, handle: str) -> ResolvedEntity | None:
        try:
            entity = await self._client.get_entity(handle)
        except Exception as exc:
            translated = translate_flood_wait(exc)
            if translated is not exc:
                raise translated from exc
            # Not found / invalid handle — let the resolver try a title scan.
            return None
        return _to_resolved(entity)

    async def find_by_title(self, title: str) -> list[ResolvedEntity]:
        matches: list[ResolvedEntity] = []
        try:
            async for dialog in self._client.iter_dialogs():
                entity = getattr(dialog, "entity", None)
                if entity is None:
                    continue
                if _entity_title(entity) == title:
                    resolved = _to_resolved(entity)
                    if resolved is not None:
                        matches.append(resolved)
        except Exception as exc:
            raise translate_flood_wait(exc) from exc
        return matches


class TelethonEntityResolver(CachingEntityResolver):
    """Production resolver: a caching resolver over a Telethon backend."""

    def __init__(self, client: Any) -> None:
        super().__init__(TelethonResolverBackend(client))


__all__ = [
    "TelethonEntityResolver",
    "TelethonResolverBackend",
]
