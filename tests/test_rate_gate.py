"""Tests for the SQLite rate gate backing pin/unpin pacing."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
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


def test_zero_interval_never_waits(tmp_path: Path) -> None:
    store = _store(tmp_path)
    waits = [store.reserve("pin:-100", 0.0, now=1000.0) for _ in range(3)]
    assert waits == [0.0, 0.0, 0.0]


def test_reserve_over_max_wait_does_not_book_the_slot(tmp_path: Path) -> None:
    """A slot the caller will refuse must not be consumed.

    Otherwise a client polling a flood-waited chat pushes its own retry time
    further out with every rejected attempt.
    """
    store = _store(tmp_path)
    store.block_until("pin:-100", 1600.0)

    waits = [
        store.reserve("pin:-100", 2.0, now=1000.0 + i, max_wait=60.0) for i in range(5)
    ]

    # Each poll reports a *shrinking* wait, and the gate never moves.
    assert waits == [600.0, 599.0, 598.0, 597.0, 596.0]
    assert store.peek("pin:-100") == 1600.0


def test_reserve_within_max_wait_still_books_the_slot(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.block_until("pin:-100", 1010.0)

    assert store.reserve("pin:-100", 2.0, now=1000.0, max_wait=60.0) == 10.0
    assert store.peek("pin:-100") == 1012.0


def test_concurrent_reservers_get_distinct_slots(tmp_path: Path) -> None:
    """The gate's whole point: N racing callers must not share one slot.

    Separate store instances stand in for separate processes (the CLI and the
    server). The reservation advances the slot inside a single write
    transaction, so eight simultaneous calls at the same ``now`` have to be
    handed the eight distinct multiples of the interval — dropping the
    transaction (or reading before it) would hand several of them ``0.0`` and
    let the burst through.
    """
    db = tmp_path / "state.db"
    stores = [RateGateStore(db) for _ in range(8)]
    barrier = threading.Barrier(len(stores))

    def _reserve(store: RateGateStore) -> float:
        barrier.wait()
        return store.reserve("pin:-100", 2.0, now=1000.0)

    with ThreadPoolExecutor(max_workers=len(stores)) as pool:
        waits = sorted(pool.map(_reserve, stores))

    assert waits == [2.0 * i for i in range(len(stores))]
