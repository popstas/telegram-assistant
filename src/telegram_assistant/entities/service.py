"""Shared entity-resolution domain used by every interface.

A Telegram *entity reference* can arrive in many shapes — a bare numeric id
(with or without the ``-100`` channel marker), an ``@username``, a ``t.me`` or
``joinchat``/``+invite`` link, a phone number, or the exact chat title. This
module turns any of those into a single :class:`ResolvedEntity` so the rest of
the codebase keeps working with plain numeric ``chat_id`` values.

Following the project's service/backend split, the orchestration here is pure
logic that depends only on a :class:`ResolverBackend` protocol; the production
Telethon adapter lives in :mod:`telethon_backend`. Tests inject a fake backend
to exercise the resolution order, the per-request cache, and the ambiguity /
not-found error semantics without spinning up Telethon.

Resolution order (mirrors ``telegram-download-chat`` ``core/entities.py``):

1. numeric reference (``-100`` prefix stripped to the bare channel id) →
   ``PeerChannel`` / ``PeerChat`` / ``PeerUser`` probe → dialog scan by id;
2. otherwise delegate the raw string to ``client.get_entity()`` which handles
   ``@username`` / ``t.me`` / ``joinchat``/``+invite`` / phone;
3. if that finds nothing, treat the string as an exact chat title and scan the
   dialog list — a single match wins, several raise
   :class:`AmbiguousEntityError`, none raises :class:`EntityNotFoundError`.

``FloodWaitError`` raised by the backend is allowed to propagate (translated,
never swallowed) so the worker queue can pause-and-retry as usual.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

EntityRefInput = str | int


class EntityError(RuntimeError):
    """Base class for entity-resolution failures surfaced to callers."""


class EntityNotFoundError(EntityError):
    """No entity matched the supplied reference."""


class AmbiguousEntityError(EntityError):
    """More than one entity matched the supplied reference (e.g. title)."""

    def __init__(self, *, ref: str, matches: list[int]) -> None:
        super().__init__(
            f"entity reference {ref!r} matches {len(matches)} entities: {matches!r}"
        )
        self.ref = ref
        self.matches = list(matches)


@dataclass(frozen=True)
class EntityRef:
    """A normalised entity reference shared by HTTP, CLI, and tests."""

    raw: str | int

    @classmethod
    def parse(cls, value: EntityRefInput | EntityRef) -> EntityRef:
        """Coerce a raw ``str``/``int`` (or an existing ref) into an ``EntityRef``."""
        if isinstance(value, EntityRef):
            return value
        if isinstance(value, str):
            value = value.strip()
        return cls(raw=value)

    @property
    def is_numeric(self) -> bool:
        """Whether the reference is a bare numeric id (optionally ``-`` signed)."""
        if isinstance(self.raw, int):
            return True
        return bool(self.raw) and self.raw.lstrip("-").isdigit()

    @property
    def numeric_id(self) -> int:
        """The bare channel/chat/user id, with the ``-100`` marker stripped.

        Telegram's "marked" channel ids look like ``-1001234567890``; Telethon's
        ``Peer*`` constructors expect the bare ``1234567890``. Legacy basic-group
        ids arrive as plain negatives (``-123456``) and only need ``abs``.
        """
        raw_int = int(self.raw)
        text = str(raw_int)
        if text.startswith("-100"):
            return int(text[4:])
        return abs(raw_int)

    @property
    def text(self) -> str:
        """The string form passed to ``get_entity`` / used for the title scan."""
        return str(self.raw)

    @property
    def cache_key(self) -> str:
        """Stable key for the per-request resolution cache."""
        return str(self.raw)


@dataclass(frozen=True)
class ResolvedEntity:
    """A resolved Telegram entity in the codebase's canonical numeric form."""

    chat_id: int
    title: str
    kind: str
    username: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "chat_id": self.chat_id,
            "title": self.title,
            "kind": self.kind,
            "username": self.username,
        }


class ResolverBackend(Protocol):
    """Low-level lookups the resolver depends on.

    Production wires this to Telethon; tests inject a fake. Each method returns
    ``None`` (or an empty list) when nothing matched so the orchestration can
    fall through the resolution order. ``FloodWaitError`` must be raised, never
    swallowed.
    """

    async def resolve_by_id(self, numeric_id: int) -> ResolvedEntity | None:
        """Resolve a bare numeric id via peer-type probes then a dialog scan."""
        ...

    async def resolve_by_handle(self, handle: str) -> ResolvedEntity | None:
        """Resolve a string handle/link/phone via ``client.get_entity``."""
        ...

    async def find_by_title(self, title: str) -> list[ResolvedEntity]:
        """Return every dialog whose exact title equals ``title``."""
        ...


class EntityResolver(Protocol):
    """The single ``resolve`` interface every surface depends on."""

    async def resolve(self, ref: EntityRefInput | EntityRef) -> ResolvedEntity:
        ...


class CachingEntityResolver:
    """Concrete :class:`EntityResolver` over a :class:`ResolverBackend`.

    Holds a per-instance (per-request) cache so resolving the same reference
    twice within one request hits Telegram once. Built fresh per request, the
    cache never leaks across requests.
    """

    def __init__(self, backend: ResolverBackend) -> None:
        self._backend = backend
        self._cache: dict[str, ResolvedEntity] = {}

    async def resolve(self, ref: EntityRefInput | EntityRef) -> ResolvedEntity:
        entity_ref = EntityRef.parse(ref)
        key = entity_ref.cache_key
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        resolved = await self._resolve_uncached(entity_ref)
        self._cache[key] = resolved
        return resolved

    async def _resolve_uncached(self, ref: EntityRef) -> ResolvedEntity:
        if ref.is_numeric:
            resolved = await self._backend.resolve_by_id(ref.numeric_id)
            if resolved is not None:
                return resolved
            raise EntityNotFoundError(
                f"no entity found for numeric reference {ref.text!r}"
            )

        resolved = await self._backend.resolve_by_handle(ref.text)
        if resolved is not None:
            return resolved

        # The handle did not resolve — fall back to an exact-title dialog scan.
        matches = await self._backend.find_by_title(ref.text)
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise AmbiguousEntityError(
                ref=ref.text, matches=[m.chat_id for m in matches]
            )
        raise EntityNotFoundError(f"no entity found for reference {ref.text!r}")


__all__ = [
    "AmbiguousEntityError",
    "CachingEntityResolver",
    "EntityError",
    "EntityNotFoundError",
    "EntityRef",
    "EntityRefInput",
    "EntityResolver",
    "ResolvedEntity",
    "ResolverBackend",
]
