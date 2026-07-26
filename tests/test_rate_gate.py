"""Tests for the SQLite rate gate backing pin/unpin pacing."""

from __future__ import annotations

from pathlib import Path

from telegram_assistant.persistence import RateGateStore


def _store(tmp_path: Path) -> RateGateStore:
    return RateGateStore(tmp_path / "state.db")


def test_first_reserve_does_not_wait(tmp_path: Path) -> None:
    store = _store(tmp_path)
    assert store.reserve("pin:-100", 2.0, now=1000.0) == 0.0


def test_second_reserve_waits_the_interval(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.reserve("pin:-100", 2.0, now=1000.0)
    assert store.reserve("pin:-100", 2.0, now=1000.0) == 2.0


def test_reserve_is_per_key(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.reserve("pin:-100", 2.0, now=1000.0)
    assert store.reserve("pin:-200", 2.0, now=1000.0) == 0.0


def test_gate_reopens_after_the_interval_elapses(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.reserve("pin:-100", 2.0, now=1000.0)
    assert store.reserve("pin:-100", 2.0, now=1002.5) == 0.0


def test_reservations_stack_for_a_burst(tmp_path: Path) -> None:
    """Three back-to-back reservations get three distinct slots."""
    store = _store(tmp_path)
    waits = [store.reserve("pin:-100", 2.0, now=1000.0) for _ in range(3)]
    assert waits == [0.0, 2.0, 4.0]


def test_gate_is_shared_across_store_instances(tmp_path: Path) -> None:
    """A second process (fresh store on the same DB) sees the same gate."""
    first = _store(tmp_path)
    first.reserve("pin:-100", 2.0, now=1000.0)
    second = RateGateStore(tmp_path / "state.db")
    assert second.reserve("pin:-100", 2.0, now=1000.0) == 2.0


def test_block_until_pushes_the_gate_out(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.block_until("pin:-100", 1030.0)
    assert store.peek("pin:-100") == 1030.0
    assert store.reserve("pin:-100", 2.0, now=1000.0) == 30.0


def test_block_until_never_moves_the_gate_backwards(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.block_until("pin:-100", 1030.0)
    store.block_until("pin:-100", 1005.0)
    assert store.peek("pin:-100") == 1030.0


def test_peek_missing_key_is_none(tmp_path: Path) -> None:
    assert _store(tmp_path).peek("pin:-100") is None


def test_clear_drops_every_gate(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.reserve("pin:-100", 2.0, now=1000.0)
    store.clear()
    assert store.peek("pin:-100") is None
    assert store.reserve("pin:-100", 2.0, now=1000.0) == 0.0


def test_zero_interval_never_waits(tmp_path: Path) -> None:
    store = _store(tmp_path)
    waits = [store.reserve("pin:-100", 0.0, now=1000.0) for _ in range(3)]
    assert waits == [0.0, 0.0, 0.0]
