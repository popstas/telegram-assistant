"""Tests for the persistent folder-membership cache (Task 2)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from telegram_assistant.config.models import AccessConfig
from telegram_assistant.persistence import FolderMembershipCache
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
    mapping = {"Clients": {123, 456}, "Team": {789}}
    cache.save(mapping, fetched_at=1000.5)

    loaded = cache.load()
    assert loaded is not None
    got_map, fetched_at = loaded
    assert got_map == mapping
    assert fetched_at == pytest.approx(1000.5)
    # Verify set types are preserved for the authorizer.
    assert isinstance(got_map["Clients"], set)


def test_save_overwrites_single_row(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    cache = FolderMembershipCache(db)
    cache.save({"A": {1}}, fetched_at=1.0)
    cache.save({"B": {2, 3}}, fetched_at=2.0)

    loaded = cache.load()
    assert loaded == ({"B": {2, 3}}, 2.0)
    with sqlite3.connect(str(db)) as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM folder_membership_cache"
        ).fetchone()[0]
    assert count == 1


def test_clear_removes_row(tmp_path: Path) -> None:
    cache = FolderMembershipCache(tmp_path / "state.db")
    cache.save({"A": {1}}, fetched_at=1.0)
    cache.clear()
    assert cache.load() is None
    # Clear on an already-empty cache is a no-op.
    cache.clear()
    assert cache.load() is None


def test_save_empty_map(tmp_path: Path) -> None:
    cache = FolderMembershipCache(tmp_path / "state.db")
    cache.save({}, fetched_at=5.0)
    assert cache.load() == ({}, 5.0)


def test_two_instances_share_persisted_map(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    FolderMembershipCache(db).save({"Clients": {42}}, fetched_at=10.0)
    # A fresh process/instance reads the persisted map — the main CLI win.
    reloaded = FolderMembershipCache(db).load()
    assert reloaded == ({"Clients": {42}}, 10.0)


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
