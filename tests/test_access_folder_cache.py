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
from telegram_assistant.persistence import FolderMembershipCache


class _CountingFastBackend:
    """Fast membership surface that records how often it is called."""

    def __init__(self, folder_map: dict[str, set[int]]) -> None:
        self._folder_map = folder_map
        self.calls = 0

    async def list_folder_chat_ids(self) -> dict[str, set[int]]:
        self.calls += 1
        return {k: set(v) for k, v in self._folder_map.items()}


class _RaisingBackend:
    """Fast surface that always fails to fetch."""

    def __init__(self) -> None:
        self.calls = 0

    async def list_folder_chat_ids(self) -> dict[str, set[int]]:
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
    cache.save({"Clients": {10, 11}}, fetched_at=time.time())
    backend = _CountingFastBackend({"Clients": {10, 11}})
    auth = Authorizer(_cfg(ttl=300), folder_backend=backend, cache=cache)

    await auth.require(10, AccessLevel.WRITE)
    await auth.require(11, AccessLevel.WRITE)
    with pytest.raises(AccessDenied):
        await auth.require(99, AccessLevel.WRITE)

    assert backend.calls == 0  # served entirely from cache


@pytest.mark.asyncio
async def test_fresh_cache_normalises_marked_ids(tmp_path: Path) -> None:
    cache = FolderMembershipCache(tmp_path / "state.db")
    cache.save({"Clients": {1234567890}}, fetched_at=time.time())
    backend = _CountingFastBackend({"Clients": {1234567890}})
    auth = Authorizer(_cfg(), folder_backend=backend, cache=cache)

    # Request carries the -100 marked form; still matches the bare cached id.
    await auth.require(-1001234567890, AccessLevel.WRITE)
    assert backend.calls == 0


@pytest.mark.asyncio
async def test_expired_cache_refetches_and_rewrites(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    cache = FolderMembershipCache(db)
    # Stale row: old chat membership fetched long ago.
    cache.save({"Clients": {10}}, fetched_at=time.time() - 10_000)
    backend = _CountingFastBackend({"Clients": {20, 21}})
    auth = Authorizer(_cfg(ttl=300), folder_backend=backend, cache=cache)

    # Fresh fetch replaces the stale membership.
    await auth.require(20, AccessLevel.WRITE)
    assert backend.calls == 1

    # The refetched map was written back with a fresh timestamp.
    reloaded = FolderMembershipCache(db).load()
    assert reloaded is not None
    mapping, fetched_at = reloaded
    assert mapping == {"Clients": {20, 21}}
    assert time.time() - fetched_at < 60


@pytest.mark.asyncio
async def test_empty_cache_fetches_and_populates(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    cache = FolderMembershipCache(db)
    backend = _CountingFastBackend({"Clients": {10}})
    auth = Authorizer(_cfg(), folder_backend=backend, cache=cache)

    await auth.require(10, AccessLevel.WRITE)
    assert backend.calls == 1
    assert FolderMembershipCache(db).load()[0] == {"Clients": {10}}


@pytest.mark.asyncio
async def test_backend_error_serves_stale_map(tmp_path: Path) -> None:
    cache = FolderMembershipCache(tmp_path / "state.db")
    cache.save({"Clients": {10}}, fetched_at=time.time() - 10_000)
    backend = _RaisingBackend()
    auth = Authorizer(_cfg(ttl=300), folder_backend=backend, cache=cache)

    # Fetch fails, but a stale cached map is served rather than propagating.
    await auth.require(10, AccessLevel.WRITE)
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
    cache.save({"Clients": {10}}, fetched_at=time.time())
    backend = _CountingFastBackend({"Clients": {20}})
    auth = Authorizer(_cfg(ttl=0), folder_backend=backend, cache=cache)

    await auth.require(20, AccessLevel.WRITE)
    with pytest.raises(AccessDenied):
        await auth.require(10, AccessLevel.WRITE)
    assert backend.calls == 1
    # ttl=0 must not write the cache either (stale row untouched).
    assert FolderMembershipCache(db).load()[0] == {"Clients": {10}}


@pytest.mark.asyncio
async def test_no_cache_matches_pre_cache_behaviour(tmp_path: Path) -> None:
    backend = _CountingFastBackend({"Clients": {10}})
    auth = Authorizer(_cfg(), folder_backend=backend, cache=None)

    await auth.require(10, AccessLevel.WRITE)
    assert backend.calls == 1


@pytest.mark.asyncio
async def test_clear_invalidates_cache(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    cache = FolderMembershipCache(db)
    cache.save({"Clients": {10}}, fetched_at=time.time())

    # Simulate a hot-reload dropping the row; the next authorizer refetches.
    cache.clear()
    assert FolderMembershipCache(db).load() is None

    backend = _CountingFastBackend({"Clients": {20}})
    auth = Authorizer(_cfg(), folder_backend=backend, cache=cache)
    await auth.require(20, AccessLevel.WRITE)
    assert backend.calls == 1


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
    cache.save({"Clients": {1}}, fetched_at=1.0)
    assert cache.load()[0] == {"Clients": {1}}


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
