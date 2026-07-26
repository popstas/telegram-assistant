"""Tests for the persistent folder-membership cache (Task 2)."""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import pytest

from telegram_assistant.config.models import AccessConfig
from telegram_assistant.persistence import FolderMembershipCache
from telegram_assistant.persistence.folder_cache import PAYLOAD_VERSION
from telegram_assistant.persistence.schema import SCHEMA_VERSION, bootstrap

# ---------------------------------------------------------------------------
# Schema bump
# ---------------------------------------------------------------------------


def test_schema_bootstrap_creates_cache_table(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    bootstrap(db)
    with sqlite3.connect(str(db)) as conn:
        rows = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        version = conn.execute(
            "SELECT value FROM schema_meta WHERE key = 'schema_version'"
        ).fetchone()[0]
    assert "folder_membership_cache" in rows
    assert int(version) == SCHEMA_VERSION
    assert SCHEMA_VERSION >= 2


def test_bootstrap_idempotent_on_existing_db(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    # First bootstrap without the cache table (simulate an existing v1 DB) by
    # bootstrapping fully, dropping the new table, then re-bootstrapping.
    bootstrap(db)
    with sqlite3.connect(str(db)) as conn:
        conn.execute("DROP TABLE folder_membership_cache")
        conn.commit()
    # Re-running bootstrap must recreate it without error (idempotent).
    bootstrap(db)
    with sqlite3.connect(str(db)) as conn:
        rows = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    assert "folder_membership_cache" in rows


# ---------------------------------------------------------------------------
# Store round-trip
# ---------------------------------------------------------------------------


def test_load_empty_returns_none(tmp_path: Path) -> None:
    cache = FolderMembershipCache(tmp_path / "state.db")
    assert cache.load() is None


def test_save_load_round_trip(tmp_path: Path) -> None:
    cache = FolderMembershipCache(tmp_path / "state.db")
    mapping = {1: ("Clients", {123, 456}), 2: ("Team", {789})}
    cache.save(mapping, fetched_at=1000.5)

    loaded = cache.load()
    assert loaded is not None
    got_map, fetched_at = loaded
    assert got_map == mapping
    assert fetched_at == pytest.approx(1000.5)
    # Verify set types are preserved for the authorizer.
    assert isinstance(got_map[1][1], set)


def test_same_named_folders_round_trip_separately(tmp_path: Path) -> None:
    # Regression (PR #17): two folders sharing a title must stay distinct rows
    # in the payload — a name-keyed cache silently dropped one of them.
    cache = FolderMembershipCache(tmp_path / "state.db")
    mapping = {1: ("Clients", {10}), 2: ("Clients", {20})}
    cache.save(mapping, fetched_at=1.0)

    assert cache.load() == (mapping, 1.0)


def test_old_payload_shape_is_a_miss(tmp_path: Path) -> None:
    # A row written by the previous (name-keyed, unversioned) format must load
    # as a cache miss so the caller refetches instead of crashing.
    db = tmp_path / "state.db"
    cache = FolderMembershipCache(db)
    cache.save({1: ("Clients", {10})}, fetched_at=1.0)
    with sqlite3.connect(str(db)) as conn:
        conn.execute(
            "UPDATE folder_membership_cache SET payload = ? WHERE id = 1",
            (json.dumps({"Clients": [10]}),),
        )
        conn.commit()

    assert FolderMembershipCache(db).load() is None


def test_future_payload_version_is_a_miss(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    cache = FolderMembershipCache(db)
    cache.save({1: ("Clients", {10})}, fetched_at=1.0)
    with sqlite3.connect(str(db)) as conn:
        conn.execute(
            "UPDATE folder_membership_cache SET payload = ? WHERE id = 1",
            (json.dumps({"version": PAYLOAD_VERSION + 1, "folders": []}),),
        )
        conn.commit()

    assert FolderMembershipCache(db).load() is None


def test_corrupt_payload_is_a_miss(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    cache = FolderMembershipCache(db)
    cache.save({1: ("Clients", {10})}, fetched_at=1.0)
    with sqlite3.connect(str(db)) as conn:
        conn.execute(
            "UPDATE folder_membership_cache SET payload = 'not json' WHERE id = 1"
        )
        conn.commit()

    assert FolderMembershipCache(db).load() is None


@pytest.mark.parametrize(
    "payload",
    [
        # Right version, wrong shape: truncated write / hand-edited DB / a
        # future format that happens to reuse the version.
        {"version": PAYLOAD_VERSION, "folders": [{"name": "Clients"}]},
        {"version": PAYLOAD_VERSION, "folders": [{"id": "x", "name": "Clients"}]},
        {"version": PAYLOAD_VERSION, "folders": [{"id": 1, "name": "C", "chat_ids": 5}]},
        {"version": PAYLOAD_VERSION, "folders": "nope"},
    ],
)
def test_structurally_broken_payload_is_a_miss(tmp_path: Path, payload: object) -> None:
    """A malformed row must not raise out of ``load`` — that would 500 the gate."""
    db = tmp_path / "state.db"
    cache = FolderMembershipCache(db)
    cache.save({1: ("Clients", {10})}, fetched_at=1.0)
    with sqlite3.connect(str(db)) as conn:
        conn.execute(
            "UPDATE folder_membership_cache SET payload = ? WHERE id = 1",
            (json.dumps(payload),),
        )
        conn.commit()

    assert FolderMembershipCache(db).load() is None


def test_save_overwrites_single_row(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    cache = FolderMembershipCache(db)
    cache.save({1: ("A", {1})}, fetched_at=1.0)
    cache.save({2: ("B", {2, 3})}, fetched_at=2.0)

    loaded = cache.load()
    assert loaded == ({2: ("B", {2, 3})}, 2.0)
    with sqlite3.connect(str(db)) as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM folder_membership_cache"
        ).fetchone()[0]
    assert count == 1


def test_clear_makes_the_next_load_miss(tmp_path: Path) -> None:
    cache = FolderMembershipCache(tmp_path / "state.db")
    cache.save({1: ("A", {1})}, fetched_at=1.0)
    cache.clear()
    assert cache.load() is None
    # Clear on an already-cleared cache is a no-op.
    cache.clear()
    assert cache.load() is None


def test_conditional_save_is_skipped_when_clear_won_the_race(tmp_path: Path) -> None:
    """A fetch overtaken by an invalidation must not restore the stale map.

    Process B starts its fetch at ``started``; process A mutates folder
    membership and clears the shared row; B then tries to persist the map it
    read *before* the mutation. Without the fence the just-changed chat would be
    judged against the pre-mutation map for a whole TTL, in every process.
    """
    cache = FolderMembershipCache(tmp_path / "state.db")
    started = time.time()
    cache.clear()  # stamped "now", i.e. after `started`

    assert cache.save({1: ("A", {1})}, started, not_after=started) is False
    assert cache.load() is None


def test_conditional_save_writes_when_nothing_newer_landed(tmp_path: Path) -> None:
    cache = FolderMembershipCache(tmp_path / "state.db")
    cache.clear()
    started = time.time() + 1.0  # the fetch started after the invalidation

    assert cache.save({1: ("A", {1})}, started, not_after=started) is True
    assert cache.load() == ({1: ("A", {1})}, started)


def test_save_empty_map(tmp_path: Path) -> None:
    cache = FolderMembershipCache(tmp_path / "state.db")
    cache.save({}, fetched_at=5.0)
    assert cache.load() == ({}, 5.0)


def test_two_instances_share_persisted_map(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    FolderMembershipCache(db).save({7: ("Clients", {42})}, fetched_at=10.0)
    # A fresh process/instance reads the persisted map — the main CLI win.
    reloaded = FolderMembershipCache(db).load()
    assert reloaded == ({7: ("Clients", {42})}, 10.0)


# ---------------------------------------------------------------------------
# Config knob
# ---------------------------------------------------------------------------


def test_folder_cache_ttl_default() -> None:
    cfg = AccessConfig()
    assert cfg.folder_cache_ttl == 300


def test_folder_cache_ttl_explicit() -> None:
    cfg = AccessConfig(folder_cache_ttl=0)
    assert cfg.folder_cache_ttl == 0


def test_folder_cache_ttl_rejects_negative() -> None:
    with pytest.raises(ValueError):
        AccessConfig(folder_cache_ttl=-1)
