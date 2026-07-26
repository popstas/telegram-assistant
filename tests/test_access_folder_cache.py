"""Tests for wiring the persistent folder-membership cache into the Authorizer.

Covers the read-through behaviour added in Task 3: a fresh cache hit skips the
backend entirely, an expired entry refetches and rewrites, a backend error
serves the stale map, ``folder_cache_ttl: 0`` bypasses the cache, and
``clear()`` (used by the hot-reload) invalidates it.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from telegram_assistant.access import AccessDenied, AccessLevel, Authorizer
from telegram_assistant.config.models import AccessConfig, AccessRule
from telegram_assistant.folders import FolderChats
from telegram_assistant.persistence import FolderMembershipCache


class _CountingFastBackend:
    """Fast membership surface that records how often it is called."""

    def __init__(self, folders: list[FolderChats]) -> None:
        self._folders = folders
        self.calls = 0

    async def list_folder_chat_ids(self) -> list[FolderChats]:
        self.calls += 1
        return [
            FolderChats(
                folder_id=f.folder_id,
                folder_name=f.folder_name,
                chat_ids=set(f.chat_ids),
            )
            for f in self._folders
        ]


class _RaisingBackend:
    """Fast surface that always fails to fetch."""

    def __init__(self) -> None:
        self.calls = 0

    async def list_folder_chat_ids(self) -> list[FolderChats]:
        self.calls += 1
        raise RuntimeError("boom")


def _cfg(ttl: int = 300) -> AccessConfig:
    return AccessConfig(
        rules=[AccessRule(folder="Clients", permission="write")],
        folder_cache_ttl=ttl,
    )


@pytest.mark.asyncio
async def test_fresh_cache_hit_skips_backend(tmp_path: Path) -> None:
    cache = FolderMembershipCache(tmp_path / "state.db")
    cache.save({1: ("Clients", {10, 11})}, fetched_at=time.time())
    backend = _CountingFastBackend(
        [FolderChats(folder_id=1, folder_name="Clients", chat_ids={10, 11})]
    )
    auth = Authorizer(_cfg(ttl=300), folder_backend=backend, cache=cache)

    await auth.require(10, AccessLevel.WRITE)
    await auth.require(11, AccessLevel.WRITE)
    with pytest.raises(AccessDenied):
        await auth.require(99, AccessLevel.WRITE)

    assert backend.calls == 0  # served entirely from cache


@pytest.mark.asyncio
async def test_fresh_cache_normalises_marked_ids(tmp_path: Path) -> None:
    cache = FolderMembershipCache(tmp_path / "state.db")
    cache.save({1: ("Clients", {1234567890})}, fetched_at=time.time())
    backend = _CountingFastBackend(
        [FolderChats(folder_id=1, folder_name="Clients", chat_ids={1234567890})]
    )
    auth = Authorizer(_cfg(), folder_backend=backend, cache=cache)

    # Request carries the -100 marked form; still matches the bare cached id.
    await auth.require(-1001234567890, AccessLevel.WRITE)
    assert backend.calls == 0


@pytest.mark.asyncio
async def test_expired_cache_refetches_and_rewrites(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    cache = FolderMembershipCache(db)
    # Stale row: old chat membership fetched long ago.
    cache.save({1: ("Clients", {10})}, fetched_at=time.time() - 10_000)
    backend = _CountingFastBackend(
        [FolderChats(folder_id=1, folder_name="Clients", chat_ids={20, 21})]
    )
    auth = Authorizer(_cfg(ttl=300), folder_backend=backend, cache=cache)

    # Fresh fetch replaces the stale membership.
    await auth.require(20, AccessLevel.WRITE)
    assert backend.calls == 1

    # The refetched map was written back with a fresh timestamp.
    reloaded = FolderMembershipCache(db).load()
    assert reloaded is not None
    mapping, fetched_at = reloaded
    assert mapping == {1: ("Clients", {20, 21})}
    assert time.time() - fetched_at < 60


@pytest.mark.asyncio
async def test_empty_cache_fetches_and_populates(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    cache = FolderMembershipCache(db)
    backend = _CountingFastBackend(
        [FolderChats(folder_id=1, folder_name="Clients", chat_ids={10})]
    )
    auth = Authorizer(_cfg(), folder_backend=backend, cache=cache)

    await auth.require(10, AccessLevel.WRITE)
    assert backend.calls == 1
    assert FolderMembershipCache(db).load()[0] == {1: ("Clients", {10})}


@pytest.mark.asyncio
async def test_cache_read_failure_degrades_to_a_live_fetch(tmp_path: Path) -> None:
    """A raising `load()` (locked DB, unreadable file) must not deny the op.

    Distinct from the well-covered "load returns None" misses: an exception
    escaping `_resolve_memberships` would turn a transient DB fault into a hard
    denial of every folder-gated op in every process sharing the DB.
    """

    class _UnreadableCache(FolderMembershipCache):
        def load(self):  # type: ignore[override]
            raise RuntimeError("database is locked")

    backend = _CountingFastBackend(
        [FolderChats(folder_id=1, folder_name="Clients", chat_ids={10})]
    )
    auth = Authorizer(
        _cfg(ttl=300),
        folder_backend=backend,
        cache=_UnreadableCache(tmp_path / "state.db"),
    )

    await auth.require(10, AccessLevel.WRITE)
    assert backend.calls == 1
    with pytest.raises(AccessDenied):
        await auth.require(99, AccessLevel.WRITE)


@pytest.mark.asyncio
async def test_backend_error_serves_stale_map(tmp_path: Path) -> None:
    cache = FolderMembershipCache(tmp_path / "state.db")
    cache.save({1: ("Clients", {10})}, fetched_at=time.time() - 10_000)
    backend = _RaisingBackend()
    auth = Authorizer(_cfg(ttl=300), folder_backend=backend, cache=cache)

    # Fetch fails, but a stale cached map is served rather than propagating.
    await auth.require(10, AccessLevel.WRITE)
    assert backend.calls == 1


@pytest.mark.asyncio
async def test_cache_write_failure_does_not_break_the_decision(tmp_path: Path) -> None:
    """A read-only/locked DB must not turn a granted op into a 500.

    The map was fetched successfully; only persisting it for the *next* process
    failed, so the authorization decision stands — same invariant the load path
    already holds.
    """

    class _UnwritableCache(FolderMembershipCache):
        def save(self, mapping, fetched_at, *, not_after=None):  # type: ignore[override]
            raise RuntimeError("disk full")

    backend = _CountingFastBackend(
        [FolderChats(folder_id=1, folder_name="Clients", chat_ids={10})]
    )
    auth = Authorizer(
        _cfg(ttl=300),
        folder_backend=backend,
        cache=_UnwritableCache(tmp_path / "state.db"),
    )

    await auth.require(10, AccessLevel.WRITE)
    with pytest.raises(AccessDenied):
        await auth.require(99, AccessLevel.WRITE)
    assert backend.calls == 1


@pytest.mark.asyncio
async def test_backend_error_propagates_without_cache(tmp_path: Path) -> None:
    cache = FolderMembershipCache(tmp_path / "state.db")  # empty
    backend = _RaisingBackend()
    auth = Authorizer(_cfg(ttl=300), folder_backend=backend, cache=cache)

    with pytest.raises(RuntimeError):
        await auth.require(10, AccessLevel.WRITE)


@pytest.mark.asyncio
async def test_ttl_zero_bypasses_cache(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    cache = FolderMembershipCache(db)
    # A fresh row exists, but ttl=0 must ignore it and always fetch.
    cache.save({1: ("Clients", {10})}, fetched_at=time.time())
    backend = _CountingFastBackend(
        [FolderChats(folder_id=1, folder_name="Clients", chat_ids={20})]
    )
    auth = Authorizer(_cfg(ttl=0), folder_backend=backend, cache=cache)

    await auth.require(20, AccessLevel.WRITE)
    with pytest.raises(AccessDenied):
        await auth.require(10, AccessLevel.WRITE)
    assert backend.calls == 1
    # ttl=0 must not write the cache either (stale row untouched).
    assert FolderMembershipCache(db).load()[0] == {1: ("Clients", {10})}


@pytest.mark.asyncio
async def test_no_cache_matches_pre_cache_behaviour(tmp_path: Path) -> None:
    backend = _CountingFastBackend(
        [FolderChats(folder_id=1, folder_name="Clients", chat_ids={10})]
    )
    auth = Authorizer(_cfg(), folder_backend=backend, cache=None)

    await auth.require(10, AccessLevel.WRITE)
    assert backend.calls == 1


@pytest.mark.asyncio
async def test_clear_invalidates_cache(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    cache = FolderMembershipCache(db)
    cache.save({1: ("Clients", {10})}, fetched_at=time.time())

    # Simulate a hot-reload dropping the row; the next authorizer refetches.
    cache.clear()
    assert FolderMembershipCache(db).load() is None

    backend = _CountingFastBackend(
        [FolderChats(folder_id=1, folder_name="Clients", chat_ids={20})]
    )
    auth = Authorizer(_cfg(), folder_backend=backend, cache=cache)
    await auth.require(20, AccessLevel.WRITE)
    assert backend.calls == 1


# ---------------------------------------------------------------------------
# Invalidation on membership-changing operations
# ---------------------------------------------------------------------------


class _MutableFolderBackend:
    """Folder backend that both reports and mutates membership."""

    def __init__(self, folders: list[FolderChats]) -> None:
        self._folders = {f.folder_id: f for f in folders}
        self.fetches = 0

    async def list_folder_chat_ids(self) -> list[FolderChats]:
        self.fetches += 1
        return [
            FolderChats(
                folder_id=f.folder_id,
                folder_name=f.folder_name,
                chat_ids=set(f.chat_ids),
            )
            for f in self._folders.values()
        ]

    async def list_folders(self):
        from telegram_assistant.folders import FolderChat, FolderSnapshot

        return [
            FolderSnapshot(
                folder_id=f.folder_id,
                folder_name=f.folder_name,
                chats=[FolderChat(chat_id=c, title=f"Chat {c}") for c in f.chat_ids],
            )
            for f in self._folders.values()
        ]

    async def resolve_chat(self, chat_ref):
        from telegram_assistant.folders import FolderChat

        return FolderChat(chat_id=int(chat_ref), title=f"Chat {chat_ref}")

    async def add_chat_to_folder(self, folder_id: int, chat_id: int) -> None:
        self._folders[folder_id].chat_ids.add(chat_id)

    async def remove_chat_from_folder(self, folder_id: int, chat_id: int) -> None:
        self._folders[folder_id].chat_ids.discard(chat_id)


@pytest.mark.asyncio
async def test_add_chat_to_folder_invalidates_cache(tmp_path: Path) -> None:
    """A chat moved into a granted folder must not stay denied for the TTL."""
    from telegram_assistant.folders import add_chat_to_folder

    db = tmp_path / "state.db"
    cache = FolderMembershipCache(db)
    # Fresh cached map from before the move: chat 30 sits in "Other" only.
    cache.save(
        {1: ("Clients", {10}), 2: ("Other", {30})}, fetched_at=time.time()
    )
    backend = _MutableFolderBackend(
        [
            FolderChats(folder_id=1, folder_name="Clients", chat_ids={10}),
            FolderChats(folder_id=2, folder_name="Other", chat_ids={30}),
        ]
    )
    # The mover's own WRITE comes from the chat's current folder ("Other").
    mover = Authorizer(
        AccessConfig(
            rules=[
                AccessRule(folder="Clients", permission="write"),
                AccessRule(folder="Other", permission="write"),
            ],
            folder_cache_ttl=300,
        ),
        folder_backend=backend,
        cache=cache,
    )

    await add_chat_to_folder(
        backend, folder_name="Clients", chat_ref=30, authorizer=mover
    )

    # The shared cache row is gone, so the *next* authorizer — even in another
    # process — sees the new membership instead of the pre-move map.
    assert FolderMembershipCache(db).load() is None
    fresh = Authorizer(_cfg(ttl=300), folder_backend=backend, cache=cache)
    await fresh.require(30, AccessLevel.WRITE)


@pytest.mark.asyncio
async def test_remove_chat_from_folder_invalidates_cache(tmp_path: Path) -> None:
    """Revoking a folder-derived grant must take effect immediately."""
    from telegram_assistant.folders import remove_chat_from_folder

    db = tmp_path / "state.db"
    cache = FolderMembershipCache(db)
    cache.save({1: ("Clients", {10, 30})}, fetched_at=time.time())
    backend = _MutableFolderBackend(
        [FolderChats(folder_id=1, folder_name="Clients", chat_ids={10, 30})]
    )
    auth = Authorizer(_cfg(ttl=300), folder_backend=backend, cache=cache)

    await auth.require(30, AccessLevel.WRITE)

    await remove_chat_from_folder(
        backend, folder_name="Clients", chat_ref=30, authorizer=auth
    )

    assert FolderMembershipCache(db).load() is None
    fresh = Authorizer(_cfg(ttl=300), folder_backend=backend, cache=cache)
    with pytest.raises(AccessDenied):
        await fresh.require(30, AccessLevel.WRITE)


@pytest.mark.asyncio
async def test_already_in_folder_leaves_cache_alone(tmp_path: Path) -> None:
    """The idempotent no-op path changes nothing, so the cache stays warm."""
    from telegram_assistant.folders import add_chat_to_folder

    db = tmp_path / "state.db"
    cache = FolderMembershipCache(db)
    saved = {1: ("Clients", {10})}
    cache.save(saved, fetched_at=time.time())
    backend = _MutableFolderBackend(
        [FolderChats(folder_id=1, folder_name="Clients", chat_ids={10})]
    )
    auth = Authorizer(_cfg(ttl=300), folder_backend=backend, cache=cache)

    result = await add_chat_to_folder(
        backend, folder_name="Clients", chat_ref=10, authorizer=auth
    )
    assert result["already_in_folder"] is True
    assert FolderMembershipCache(db).load()[0] == saved


@pytest.mark.asyncio
async def test_create_group_invalidates_membership_cache(
    minimal_config_yaml: str, tmp_path: Path
) -> None:
    """A group placed into a folder must be writable right after creation."""
    from telegram_assistant.config.loader import load_config_from_text
    from telegram_assistant.groups import GroupCreateRequest, create_group
    from telegram_assistant.persistence import OperationStore
    from telegram_assistant.plugins import build_registry

    from .test_groups import FakeGroupBackend

    db = tmp_path / "state.db"
    cache = FolderMembershipCache(db)
    # Cached map predating the create; the new chat id is absent from it.
    cache.save({2: ("Planfix clients", set())}, fetched_at=time.time())
    config = load_config_from_text(minimal_config_yaml)
    backend = FakeGroupBackend()
    folder_backend = _MutableFolderBackend(
        [FolderChats(folder_id=2, folder_name="Planfix clients", chat_ids=set())]
    )
    auth = Authorizer(
        AccessConfig(
            rules=[
                AccessRule(folder="Planfix clients", permissions=["read", "write"])
            ],
            folder_cache_ttl=300,
        ),
        folder_backend=folder_backend,
        cache=cache,
    )

    result, _ = await create_group(
        backend=backend,
        folder_backend=folder_backend,
        store=OperationStore(tmp_path / "ops.db"),
        config=config.telegram,
        plugins=build_registry(config),
        request=GroupCreateRequest(
            title="Acme", external_ref=1, skip_reserve=True
        ),
        authorizer=auth,
    )

    assert FolderMembershipCache(db).load() is None
    # The follow-up write into the brand-new chat is not denied by a stale map.
    await auth.require(result.telegram_chat_id, AccessLevel.WRITE)


# ---------------------------------------------------------------------------
# CLI + HTTP wiring
# ---------------------------------------------------------------------------


def test_cli_helper_none_without_policy(tmp_path: Path) -> None:
    from telegram_assistant.cli.main import _cli_folder_membership_cache

    class _Cfg:
        class telegram:  # noqa: N801 - stand-in for config.telegram
            access = None

    assert _cli_folder_membership_cache(_Cfg()) is None


def test_cli_helper_builds_cache_with_policy(tmp_path: Path) -> None:
    from telegram_assistant.cli.main import _cli_folder_membership_cache

    class _Cfg:
        class telegram:  # noqa: N801 - stand-in for config.telegram
            access = AccessConfig(
                rules=[AccessRule(folder="Clients", permission="write")]
            )
            session_path = str(tmp_path / "sess.session")

    cache = _cli_folder_membership_cache(_Cfg())
    assert isinstance(cache, FolderMembershipCache)
    # Round-trips through the config-derived DB path.
    cache.save({1: ("Clients", {1})}, fetched_at=1.0)
    assert cache.load()[0] == {1: ("Clients", {1})}


def test_app_state_cache_only_with_policy(tmp_path: Path) -> None:
    from telegram_assistant.config.loader import load_config_from_text
    from telegram_assistant.http_api import create_app

    session = tmp_path / "sess.session"

    def _config(access_lines: str) -> str:
        return (
            "telegram:\n"
            "  api_id: 1\n"
            "  api_hash: h\n"
            f"  session_path: {session}\n"
            "  default_chat_folder:\n"
            "    folder_id: 2\n"
            "    folder_name: F\n"
            f"{access_lines}"
            "http:\n"
            "  host: 0.0.0.0\n"
            "  port: 8085\n"
            "  bearer_token: t\n"
        )

    allow_all = load_config_from_text(_config(""))
    app = create_app(allow_all, session_manager=None, operation_store=None)
    assert app.state.folder_membership_cache is None

    policy_lines = (
        "  access:\n"
        "    rules:\n"
        "      - folder: F\n"
        "        permission: write\n"
    )
    with_policy = load_config_from_text(_config(policy_lines))
    app2 = create_app(with_policy, session_manager=None, operation_store=None)
    assert app2.state.folder_membership_cache is not None


# ---------------------------------------------------------------------------
# id-keyed map: TTL / stale-serve behaviour with same-named folders (Task 3)
# ---------------------------------------------------------------------------


def _twin_cfg(ttl: int = 300, *, folder_id: int | None = None) -> AccessConfig:
    rule = (
        AccessRule(folder_id=folder_id, permission="write")
        if folder_id is not None
        else AccessRule(folder="Clients", permission="write")
    )
    return AccessConfig(rules=[rule], folder_cache_ttl=ttl)


@pytest.mark.asyncio
async def test_cached_same_named_folders_stay_distinct(tmp_path: Path) -> None:
    cache = FolderMembershipCache(tmp_path / "state.db")
    cache.save(
        {1: ("Clients", {10}), 2: ("Clients", {20})}, fetched_at=time.time()
    )
    backend = _CountingFastBackend([])

    # A name rule unions both cached folders...
    name_auth = Authorizer(_twin_cfg(), folder_backend=backend, cache=cache)
    await name_auth.require(10, AccessLevel.WRITE)
    await name_auth.require(20, AccessLevel.WRITE)

    # ...while an id rule only grants the folder it names.
    id_auth = Authorizer(
        _twin_cfg(folder_id=2), folder_backend=backend, cache=cache
    )
    await id_auth.require(20, AccessLevel.WRITE)
    with pytest.raises(AccessDenied):
        await id_auth.require(10, AccessLevel.WRITE)

    assert backend.calls == 0  # both served from the cached id-keyed map


@pytest.mark.asyncio
async def test_expired_cache_refetch_keeps_both_twins(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    cache = FolderMembershipCache(db)
    cache.save({1: ("Clients", {10})}, fetched_at=time.time() - 10_000)
    backend = _CountingFastBackend(
        [
            FolderChats(folder_id=1, folder_name="Clients", chat_ids={11}),
            FolderChats(folder_id=2, folder_name="Clients", chat_ids={21}),
        ]
    )
    auth = Authorizer(_twin_cfg(folder_id=2), folder_backend=backend, cache=cache)

    await auth.require(21, AccessLevel.WRITE)
    with pytest.raises(AccessDenied):
        await auth.require(11, AccessLevel.WRITE)
    assert backend.calls == 1
    assert FolderMembershipCache(db).load()[0] == {
        1: ("Clients", {11}),
        2: ("Clients", {21}),
    }


@pytest.mark.asyncio
async def test_stale_serve_preserves_folder_ids(tmp_path: Path) -> None:
    cache = FolderMembershipCache(tmp_path / "state.db")
    cache.save(
        {1: ("Clients", {10}), 2: ("Clients", {20})},
        fetched_at=time.time() - 10_000,
    )
    backend = _RaisingBackend()
    auth = Authorizer(
        _twin_cfg(folder_id=1), folder_backend=backend, cache=cache
    )

    # The fetch fails; the stale id-keyed map still distinguishes the twins.
    await auth.require(10, AccessLevel.WRITE)
    with pytest.raises(AccessDenied):
        await auth.require(20, AccessLevel.WRITE)
    assert backend.calls == 1
