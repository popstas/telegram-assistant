"""Tests for the shared entity resolver (Task 1 of the access-control plan).

The resolution order, per-request cache, and ambiguity / not-found semantics
are exercised through an in-memory :class:`ResolverBackend` so no Telethon
traffic is needed. A small fake Telethon client additionally covers the
production adapter's id / handle / title paths and ``FloodWaitError``
translation.
"""

from __future__ import annotations

import pytest

from telegram_assistant.entities import (
    AmbiguousEntityError,
    CachingEntityResolver,
    EntityNotFoundError,
    EntityRef,
    ResolvedEntity,
    TelethonResolverBackend,
)
from telegram_assistant.worker.queue import FloodWaitError

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeResolverBackend:
    """In-memory :class:`ResolverBackend` with call counters for cache tests."""

    def __init__(
        self,
        *,
        by_id: dict[int, ResolvedEntity] | None = None,
        by_handle: dict[str, ResolvedEntity] | None = None,
        by_title: dict[str, list[ResolvedEntity]] | None = None,
        flood: bool = False,
    ) -> None:
        self.by_id = by_id or {}
        self.by_handle = by_handle or {}
        self.by_title = by_title or {}
        self.flood = flood
        self.id_calls = 0
        self.handle_calls = 0
        self.title_calls = 0

    async def resolve_by_id(self, numeric_id: int) -> ResolvedEntity | None:
        self.id_calls += 1
        if self.flood:
            raise FloodWaitError(5)
        return self.by_id.get(numeric_id)

    async def resolve_by_handle(self, handle: str) -> ResolvedEntity | None:
        self.handle_calls += 1
        if self.flood:
            raise FloodWaitError(5)
        return self.by_handle.get(handle)

    async def find_by_title(self, title: str) -> list[ResolvedEntity]:
        self.title_calls += 1
        return list(self.by_title.get(title, []))


# ---------------------------------------------------------------------------
# EntityRef normalisation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected_numeric, is_numeric",
    [
        (1234, 1234, True),
        ("1234", 1234, True),
        ("-1001234567890", 1234567890, True),
        (-1001234567890, 1234567890, True),
        ("-123456", 123456, True),
        ("@alice", None, False),
        ("Client chat test", None, False),
        ("https://t.me/joinchat/AAAA", None, False),
    ],
)
def test_entity_ref_normalisation(raw, expected_numeric, is_numeric):
    ref = EntityRef.parse(raw)
    assert ref.is_numeric is is_numeric
    if is_numeric:
        assert ref.numeric_id == expected_numeric


def test_entity_ref_strips_whitespace():
    assert EntityRef.parse("  @alice  ").raw == "@alice"


# ---------------------------------------------------------------------------
# Resolution order
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("ref", [1234, "1234", "-1001234", "-1234"])
async def test_numeric_variants_resolve_via_id(ref):
    """All numeric forms normalise to the same bare id and resolve identically."""
    entity = ResolvedEntity(chat_id=1234, title="Group", kind="channel")
    backend = FakeResolverBackend(by_id={1234: entity})
    resolver = CachingEntityResolver(backend)

    resolved = await resolver.resolve(ref)

    assert resolved is entity
    assert backend.handle_calls == 0
    assert backend.title_calls == 0


@pytest.mark.asyncio
async def test_username_resolves_via_handle():
    entity = ResolvedEntity(
        chat_id=42, title="Alice", kind="user", username="alice"
    )
    backend = FakeResolverBackend(by_handle={"@alice": entity})
    resolver = CachingEntityResolver(backend)

    resolved = await resolver.resolve("@alice")

    assert resolved is entity
    assert backend.id_calls == 0
    assert backend.title_calls == 0


@pytest.mark.asyncio
async def test_link_resolves_via_handle():
    entity = ResolvedEntity(chat_id=7, title="Private", kind="channel")
    link = "https://t.me/joinchat/AAAAAEHbEkejzxUjAUCfYg"
    backend = FakeResolverBackend(by_handle={link: entity})
    resolver = CachingEntityResolver(backend)

    assert await resolver.resolve(link) is entity


@pytest.mark.asyncio
async def test_title_fallback_when_handle_misses():
    """A non-numeric ref the handle lookup can't resolve falls back to title."""
    entity = ResolvedEntity(chat_id=99, title="Client chat test", kind="channel")
    backend = FakeResolverBackend(
        by_title={"Client chat test": [entity]}
    )
    resolver = CachingEntityResolver(backend)

    resolved = await resolver.resolve("Client chat test")

    assert resolved is entity
    assert backend.handle_calls == 1
    assert backend.title_calls == 1


# ---------------------------------------------------------------------------
# Error semantics
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_title_ambiguity_raises():
    a = ResolvedEntity(chat_id=1, title="Dup", kind="channel")
    b = ResolvedEntity(chat_id=2, title="Dup", kind="channel")
    backend = FakeResolverBackend(by_title={"Dup": [a, b]})
    resolver = CachingEntityResolver(backend)

    with pytest.raises(AmbiguousEntityError) as excinfo:
        await resolver.resolve("Dup")
    assert excinfo.value.matches == [1, 2]


@pytest.mark.asyncio
async def test_numeric_not_found_raises():
    resolver = CachingEntityResolver(FakeResolverBackend())
    with pytest.raises(EntityNotFoundError):
        await resolver.resolve(555)


@pytest.mark.asyncio
async def test_string_not_found_raises():
    resolver = CachingEntityResolver(FakeResolverBackend())
    with pytest.raises(EntityNotFoundError):
        await resolver.resolve("@ghost")


@pytest.mark.parametrize("raw", ["-100", -100])
def test_entity_ref_malformed_marked_id_does_not_crash(raw):
    """A bare ``-100`` marker with no channel id must not raise on ``int('')``."""
    ref = EntityRef.parse(raw)
    assert ref.is_numeric is True
    # Falls back to ``abs`` rather than crashing; the value is still numeric.
    assert ref.numeric_id == 100


@pytest.mark.asyncio
async def test_malformed_marked_id_resolves_to_not_found():
    """``--entity -100`` surfaces a clean EntityNotFoundError, not a ValueError."""
    resolver = CachingEntityResolver(FakeResolverBackend())
    with pytest.raises(EntityNotFoundError):
        await resolver.resolve("-100")


# ---------------------------------------------------------------------------
# Cache + FloodWait
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_per_request_cache_hits_backend_once():
    entity = ResolvedEntity(chat_id=1234, title="Group", kind="channel")
    backend = FakeResolverBackend(by_id={1234: entity})
    resolver = CachingEntityResolver(backend)

    first = await resolver.resolve(1234)
    second = await resolver.resolve(1234)

    assert first is second
    assert backend.id_calls == 1


@pytest.mark.asyncio
async def test_flood_wait_propagates_not_swallowed():
    resolver = CachingEntityResolver(FakeResolverBackend(flood=True))
    with pytest.raises(FloodWaitError):
        await resolver.resolve(1234)


# ---------------------------------------------------------------------------
# Telethon adapter
# ---------------------------------------------------------------------------


class _FakeEntity:
    def __init__(
        self,
        *,
        id: int,
        title: str | None = None,
        first_name: str | None = None,
        last_name: str | None = None,
        username: str | None = None,
        megagroup: bool | None = None,
    ) -> None:
        self.id = id
        if title is not None:
            self.title = title
        if first_name is not None:
            self.first_name = first_name
        if last_name is not None:
            self.last_name = last_name
        if username is not None:
            self.username = username
        if megagroup is not None:
            self.megagroup = megagroup


class _FakeDialog:
    def __init__(self, entity: _FakeEntity) -> None:
        self.entity = entity


class _FakeTelethonClient:
    """Minimal Telethon stand-in: resolves strings, raises on peer probes."""

    def __init__(
        self,
        *,
        by_handle: dict[str, _FakeEntity] | None = None,
        dialogs: list[_FakeEntity] | None = None,
    ) -> None:
        self._by_handle = by_handle or {}
        self._dialogs = dialogs or []

    async def get_entity(self, ref):
        if isinstance(ref, str):
            entity = self._by_handle.get(ref)
            if entity is None:
                raise ValueError(f"no entity for {ref!r}")
            return entity
        # A Peer* object — pretend none of the peer-type probes match so the
        # adapter falls through to the dialog scan.
        raise ValueError("peer probe miss")

    async def iter_dialogs(self):
        for entity in self._dialogs:
            yield _FakeDialog(entity)


@pytest.mark.asyncio
async def test_telethon_backend_resolves_handle_kind_and_username():
    client = _FakeTelethonClient(
        by_handle={
            "@alice": _FakeEntity(id=10, first_name="Alice", username="alice")
        }
    )
    backend = TelethonResolverBackend(client)

    resolved = await backend.resolve_by_handle("@alice")

    assert resolved == ResolvedEntity(
        chat_id=10, title="Alice", kind="user", username="alice"
    )


@pytest.mark.asyncio
async def test_telethon_backend_id_falls_back_to_dialog_scan():
    group = _FakeEntity(id=555, title="Some group", megagroup=True)
    client = _FakeTelethonClient(dialogs=[_FakeEntity(id=1, title="Other"), group])
    backend = TelethonResolverBackend(client)

    resolved = await backend.resolve_by_id(555)

    assert resolved is not None
    assert resolved.chat_id == 555
    assert resolved.kind == "channel"


@pytest.mark.asyncio
async def test_telethon_backend_find_by_title_exact_match():
    a = _FakeEntity(id=1, title="Client chat test")
    b = _FakeEntity(id=2, title="Client chat test")
    c = _FakeEntity(id=3, title="Unrelated")
    backend = TelethonResolverBackend(_FakeTelethonClient(dialogs=[a, b, c]))

    matches = await backend.find_by_title("Client chat test")

    assert sorted(m.chat_id for m in matches) == [1, 2]
