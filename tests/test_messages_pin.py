"""Tests for Task 6 — message pin/unpin domain op + Telethon adapter."""

from __future__ import annotations

import sqlite3
from typing import Any

import pytest

from telegram_assistant.access import AccessDenied, Authorizer
from telegram_assistant.config.models import AccessConfig, AccessRule
from telegram_assistant.messages import (
    PacedFloodWaitError,
    Pacer,
    PinMessageRequest,
    UnpinMessageRequest,
    pin_message,
    unpin_message,
)
from telegram_assistant.messages.pacing import pin_pacing_key
from telegram_assistant.messages.telethon_backend import TelethonPinBackend
from telegram_assistant.persistence import RateGateStore
from telegram_assistant.worker.queue import FloodWaitError

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakePinBackend:
    def __init__(self, *, raise_on_call: Exception | None = None) -> None:
        self.pins: list[dict[str, Any]] = []
        self.unpins: list[dict[str, Any]] = []
        self._raise_on_call = raise_on_call

    async def pin_message(
        self, *, chat_id: int, message_id: int, silent: bool, pm_oneside: bool
    ) -> None:
        if self._raise_on_call is not None:
            raise self._raise_on_call
        self.pins.append(
            {
                "chat_id": chat_id,
                "message_id": message_id,
                "silent": silent,
                "pm_oneside": pm_oneside,
            }
        )

    async def unpin_message(
        self, *, chat_id: int, message_id: int | None
    ) -> None:
        if self._raise_on_call is not None:
            raise self._raise_on_call
        self.unpins.append({"chat_id": chat_id, "message_id": message_id})


# ---------------------------------------------------------------------------
# pin_message domain tests
# ---------------------------------------------------------------------------


async def test_pin_passes_flags_to_backend() -> None:
    backend = FakePinBackend()
    result = await pin_message(
        backend,
        request=PinMessageRequest(
            telegram_chat_id=-100,
            message_id=42,
            silent=True,
            pm_oneside=True,
            chat_name="Acme",
        ),
    )
    assert result.dry_run is False
    assert result.silent is True
    assert result.pm_oneside is True
    assert result.telegram_message_id == 42
    assert result.chat_name == "Acme"
    assert backend.pins == [
        {"chat_id": -100, "message_id": 42, "silent": True, "pm_oneside": True}
    ]


async def test_pin_dry_run_skips_backend() -> None:
    backend = FakePinBackend()
    result = await pin_message(
        backend,
        request=PinMessageRequest(
            telegram_chat_id=-100, message_id=42, dry_run=True
        ),
    )
    assert result.dry_run is True
    assert result.telegram_message_id == 42
    assert backend.pins == []


async def test_pin_rejects_non_positive_message_id() -> None:
    backend = FakePinBackend()
    with pytest.raises(ValueError):
        await pin_message(
            backend,
            request=PinMessageRequest(telegram_chat_id=-100, message_id=0),
        )
    assert backend.pins == []


async def test_pin_denied_before_backend_call() -> None:
    backend = FakePinBackend()
    authorizer = Authorizer(
        AccessConfig(rules=[AccessRule(all=True, permission="read")])
    )
    with pytest.raises(AccessDenied):
        await pin_message(
            backend,
            request=PinMessageRequest(telegram_chat_id=-100, message_id=1),
            authorizer=authorizer,
        )
    assert backend.pins == []


async def test_pin_dry_run_still_authorizes() -> None:
    backend = FakePinBackend()
    authorizer = Authorizer(
        AccessConfig(rules=[AccessRule(all=True, permission="read")])
    )
    with pytest.raises(AccessDenied):
        await pin_message(
            backend,
            request=PinMessageRequest(
                telegram_chat_id=-100, message_id=1, dry_run=True
            ),
            authorizer=authorizer,
        )
    assert backend.pins == []


async def test_pin_allowed_with_write_rule() -> None:
    backend = FakePinBackend()
    authorizer = Authorizer(
        AccessConfig(rules=[AccessRule(all=True, permission="write")])
    )
    result = await pin_message(
        backend,
        request=PinMessageRequest(telegram_chat_id=-100, message_id=5),
        authorizer=authorizer,
    )
    assert result.dry_run is False
    assert backend.pins == [
        {"chat_id": -100, "message_id": 5, "silent": False, "pm_oneside": False}
    ]


# ---------------------------------------------------------------------------
# unpin_message domain tests
# ---------------------------------------------------------------------------


async def test_unpin_one_passes_id_to_backend() -> None:
    backend = FakePinBackend()
    result = await unpin_message(
        backend,
        request=UnpinMessageRequest(telegram_chat_id=-100, message_id=42),
    )
    assert result.unpinned_all is False
    assert result.telegram_message_id == 42
    assert result.dry_run is False
    assert backend.unpins == [{"chat_id": -100, "message_id": 42}]


async def test_unpin_all_passes_none_to_backend() -> None:
    backend = FakePinBackend()
    result = await unpin_message(
        backend,
        request=UnpinMessageRequest(telegram_chat_id=-100, message_id=None),
    )
    assert result.unpinned_all is True
    assert result.telegram_message_id is None
    assert backend.unpins == [{"chat_id": -100, "message_id": None}]


async def test_unpin_dry_run_skips_backend() -> None:
    backend = FakePinBackend()
    result = await unpin_message(
        backend,
        request=UnpinMessageRequest(
            telegram_chat_id=-100, message_id=42, dry_run=True
        ),
    )
    assert result.dry_run is True
    assert result.unpinned_all is False
    assert backend.unpins == []


async def test_unpin_rejects_non_positive_message_id() -> None:
    backend = FakePinBackend()
    with pytest.raises(ValueError):
        await unpin_message(
            backend,
            request=UnpinMessageRequest(telegram_chat_id=-100, message_id=0),
        )
    assert backend.unpins == []


async def test_unpin_denied_before_backend_call() -> None:
    backend = FakePinBackend()
    authorizer = Authorizer(
        AccessConfig(rules=[AccessRule(all=True, permission="read")])
    )
    with pytest.raises(AccessDenied):
        await unpin_message(
            backend,
            request=UnpinMessageRequest(telegram_chat_id=-100, message_id=None),
            authorizer=authorizer,
        )
    assert backend.unpins == []


# ---------------------------------------------------------------------------
# Telethon adapter tests
# ---------------------------------------------------------------------------


class FakeTelethonClient:
    def __init__(self, *, raise_on_call: Exception | None = None) -> None:
        self.pin_calls: list[Any] = []
        self.unpin_calls: list[Any] = []
        self._raise_on_call = raise_on_call

    async def get_input_entity(self, chat_id: int) -> str:
        return f"peer:{chat_id}"

    async def pin_message(
        self, entity: Any, message_id: int, *, notify: bool, pm_oneside: bool
    ) -> None:
        if self._raise_on_call is not None:
            raise self._raise_on_call
        self.pin_calls.append(
            {
                "entity": entity,
                "message_id": message_id,
                "notify": notify,
                "pm_oneside": pm_oneside,
            }
        )

    async def unpin_message(self, entity: Any, message_id: int | None) -> None:
        if self._raise_on_call is not None:
            raise self._raise_on_call
        self.unpin_calls.append({"entity": entity, "message_id": message_id})


async def test_telethon_pin_passes_args() -> None:
    client = FakeTelethonClient()
    backend = TelethonPinBackend(client)
    await backend.pin_message(
        chat_id=-100, message_id=7, silent=True, pm_oneside=True
    )
    assert client.pin_calls == [
        {
            "entity": "peer:-100",
            "message_id": 7,
            "notify": False,
            "pm_oneside": True,
        }
    ]


async def test_telethon_pin_notify_when_not_silent() -> None:
    client = FakeTelethonClient()
    backend = TelethonPinBackend(client)
    await backend.pin_message(
        chat_id=-100, message_id=7, silent=False, pm_oneside=False
    )
    assert client.pin_calls[0]["notify"] is True


async def test_telethon_unpin_one_passes_id() -> None:
    client = FakeTelethonClient()
    backend = TelethonPinBackend(client)
    await backend.unpin_message(chat_id=-100, message_id=7)
    assert client.unpin_calls == [{"entity": "peer:-100", "message_id": 7}]


async def test_telethon_unpin_all_passes_none() -> None:
    client = FakeTelethonClient()
    backend = TelethonPinBackend(client)
    await backend.unpin_message(chat_id=-100, message_id=None)
    assert client.unpin_calls == [{"entity": "peer:-100", "message_id": None}]


def _flood_error() -> Exception:
    class _Flood(Exception):
        def __init__(self) -> None:
            self.seconds = 7

    _Flood.__name__ = "FloodWaitError"
    return _Flood()


async def test_telethon_pin_translates_flood_wait() -> None:
    client = FakeTelethonClient(raise_on_call=_flood_error())
    backend = TelethonPinBackend(client)
    with pytest.raises(FloodWaitError):
        await backend.pin_message(
            chat_id=-100, message_id=7, silent=False, pm_oneside=False
        )


async def test_telethon_unpin_translates_flood_wait() -> None:
    client = FakeTelethonClient(raise_on_call=_flood_error())
    backend = TelethonPinBackend(client)
    with pytest.raises(FloodWaitError):
        await backend.unpin_message(chat_id=-100, message_id=7)


# ---------------------------------------------------------------------------
# Pacing + FLOOD_WAIT retry
# ---------------------------------------------------------------------------


class FakeClock:
    """Monotonic fake clock — `sleep` advances it instead of waiting."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start
        self.sleeps: list[float] = []

    def time(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class FloodThenOkBackend:
    """Raises FLOOD_WAIT for the first ``failures`` calls, then succeeds."""

    def __init__(self, failures: int, *, seconds: float = 7.0) -> None:
        self._remaining = failures
        self._seconds = seconds
        self.pins: list[dict[str, Any]] = []
        self.unpins: list[dict[str, Any]] = []
        self.attempts = 0

    async def pin_message(
        self, *, chat_id: int, message_id: int, silent: bool, pm_oneside: bool
    ) -> None:
        self.attempts += 1
        if self._remaining > 0:
            self._remaining -= 1
            raise FloodWaitError(self._seconds)
        self.pins.append({"chat_id": chat_id, "message_id": message_id})

    async def unpin_message(self, *, chat_id: int, message_id: int | None) -> None:
        self.attempts += 1
        if self._remaining > 0:
            self._remaining -= 1
            raise FloodWaitError(self._seconds)
        self.unpins.append({"chat_id": chat_id, "message_id": message_id})


def _pacer(tmp_path, clock: FakeClock, **kwargs: Any) -> Pacer:
    gate = RateGateStore(tmp_path / "state.db")
    kwargs.setdefault("min_interval_seconds", 2.0)
    return Pacer(gate, sleep=clock.sleep, clock=clock.time, **kwargs)


async def test_pacing_delays_the_second_rapid_pin(tmp_path) -> None:
    backend = FakePinBackend()
    clock = FakeClock()
    pacer = _pacer(tmp_path, clock)

    for message_id in (1, 2, 3):
        await pin_message(
            backend,
            request=PinMessageRequest(telegram_chat_id=-100, message_id=message_id),
            pacer=pacer,
        )

    # First pin goes straight through; each following one waits its interval.
    assert clock.sleeps == [2.0, 2.0]
    assert [p["message_id"] for p in backend.pins] == [1, 2, 3]


async def test_pacing_is_per_chat(tmp_path) -> None:
    backend = FakePinBackend()
    clock = FakeClock()
    pacer = _pacer(tmp_path, clock)

    await pin_message(
        backend,
        request=PinMessageRequest(telegram_chat_id=-100, message_id=1),
        pacer=pacer,
    )
    await pin_message(
        backend,
        request=PinMessageRequest(telegram_chat_id=-200, message_id=1),
        pacer=pacer,
    )
    assert clock.sleeps == []


async def test_pin_and_unpin_share_the_chat_gate(tmp_path) -> None:
    backend = FakePinBackend()
    clock = FakeClock()
    pacer = _pacer(tmp_path, clock)

    await pin_message(
        backend,
        request=PinMessageRequest(telegram_chat_id=-100, message_id=1),
        pacer=pacer,
    )
    await unpin_message(
        backend,
        request=UnpinMessageRequest(telegram_chat_id=-100, message_id=1),
        pacer=pacer,
    )
    assert clock.sleeps == [2.0]


async def test_zero_interval_disables_pacing(tmp_path) -> None:
    backend = FakePinBackend()
    clock = FakeClock()
    pacer = _pacer(tmp_path, clock, min_interval_seconds=0.0)

    for message_id in (1, 2, 3):
        await pin_message(
            backend,
            request=PinMessageRequest(telegram_chat_id=-100, message_id=message_id),
            pacer=pacer,
        )
    assert clock.sleeps == []
    assert len(backend.pins) == 3


async def test_dry_run_skips_pacing_and_backend(tmp_path) -> None:
    backend = FakePinBackend()
    clock = FakeClock()
    pacer = _pacer(tmp_path, clock)

    for message_id in (1, 2, 3):
        await pin_message(
            backend,
            request=PinMessageRequest(
                telegram_chat_id=-100, message_id=message_id, dry_run=True
            ),
            pacer=pacer,
        )
    assert clock.sleeps == []
    assert backend.pins == []


async def test_flood_wait_is_slept_off_and_retried(tmp_path) -> None:
    backend = FloodThenOkBackend(failures=1, seconds=7.0)
    clock = FakeClock()
    pacer = _pacer(tmp_path, clock)

    result = await pin_message(
        backend,
        request=PinMessageRequest(telegram_chat_id=-100, message_id=5),
        pacer=pacer,
    )
    assert result.dry_run is False
    assert backend.attempts == 2
    # FLOOD_WAIT 7s + the 5s safety margin, mirroring the worker queue.
    assert clock.sleeps == [12.0]
    assert backend.pins == [{"chat_id": -100, "message_id": 5}]


async def test_flood_wait_retry_blocks_the_shared_gate(tmp_path) -> None:
    """A FLOOD_WAIT backs off every process, not just the one that tripped it."""
    backend = FloodThenOkBackend(failures=1, seconds=7.0)
    clock = FakeClock()
    gate = RateGateStore(tmp_path / "state.db")
    pacer = Pacer(
        gate, min_interval_seconds=2.0, sleep=clock.sleep, clock=clock.time
    )

    await pin_message(
        backend,
        request=PinMessageRequest(telegram_chat_id=-100, message_id=5),
        pacer=pacer,
    )
    # A fresh store (another process) sees the gate pushed past the pause.
    other = RateGateStore(tmp_path / "state.db")
    assert other.peek(pin_pacing_key(-100)) >= 1012.0


async def test_flood_wait_retry_reserves_its_own_slot(tmp_path) -> None:
    """The retry must re-book the shared slot instead of walking through it.

    ``block_until`` only tells the *other* processes to back off; the gate then
    opens for everyone at the same instant. While this caller sleeps off the
    FLOOD_WAIT another process can reserve exactly that slot, so a retry that
    called the backend straight from the sleep would hit Telegram together with
    it — on the chat Telegram just flood-waited.
    """
    clock = FakeClock()
    gate = RateGateStore(tmp_path / "state.db")
    other = RateGateStore(tmp_path / "state.db")
    backend = FloodThenOkBackend(failures=1, seconds=7.0)
    stolen: list[float] = []

    async def sleep_then_steal(seconds: float) -> None:
        await clock.sleep(seconds)
        if not stolen:
            # Another process books the slot the FLOOD_WAIT opened up.
            stolen.append(other.reserve(pin_pacing_key(-100), 2.0, clock.time()))

    pacer = Pacer(
        gate, min_interval_seconds=2.0, sleep=sleep_then_steal, clock=clock.time
    )

    await pin_message(
        backend,
        request=PinMessageRequest(telegram_chat_id=-100, message_id=5),
        pacer=pacer,
    )

    assert stolen == [0.0]  # the other process went first, at the boundary
    # 12s FLOOD_WAIT pause, then the retry waits its own interval behind it.
    assert clock.sleeps == [12.0, 2.0]
    assert backend.pins == [{"chat_id": -100, "message_id": 5}]


async def test_flood_wait_budget_exhausted_reports_retry_after(tmp_path) -> None:
    backend = FloodThenOkBackend(failures=99, seconds=7.0)
    clock = FakeClock()
    pacer = _pacer(tmp_path, clock, max_flood_wait_retries=3)

    with pytest.raises(PacedFloodWaitError) as excinfo:
        await pin_message(
            backend,
            request=PinMessageRequest(telegram_chat_id=-100, message_id=5),
            pacer=pacer,
        )
    exc = excinfo.value
    assert backend.attempts == 3
    assert exc.attempts == 3
    assert exc.retry_after_seconds == 12.0
    assert exc.retry_at == clock.now + 12.0
    assert "retry after 12s" in str(exc)
    # Still a FloodWaitError, so existing surface mappings keep working.
    assert isinstance(exc, FloodWaitError)


async def test_flood_wait_longer_than_cap_is_not_slept_off(tmp_path) -> None:
    backend = FloodThenOkBackend(failures=1, seconds=600.0)
    clock = FakeClock()
    pacer = _pacer(tmp_path, clock, max_flood_wait_seconds=60.0)

    with pytest.raises(PacedFloodWaitError) as excinfo:
        await pin_message(
            backend,
            request=PinMessageRequest(telegram_chat_id=-100, message_id=5),
            pacer=pacer,
        )
    assert excinfo.value.retry_after_seconds == 605.0
    assert clock.sleeps == []
    assert backend.attempts == 1


async def test_gate_blocked_beyond_cap_reports_instead_of_waiting(tmp_path) -> None:
    """A gate parked behind a huge FLOOD_WAIT must not hold the next call open.

    The over-cap FLOOD_WAIT is written into the shared gate, so the *following*
    pin (any surface, any process) would otherwise sleep the whole 10 minutes
    inside the request — exactly what ``max_flood_wait_seconds`` exists to stop.
    """
    clock = FakeClock()
    gate = RateGateStore(tmp_path / "state.db")
    gate.block_until(pin_pacing_key(-100), clock.now + 600.0)
    backend = FakePinBackend()
    pacer = Pacer(
        gate,
        min_interval_seconds=2.0,
        max_flood_wait_seconds=60.0,
        sleep=clock.sleep,
        clock=clock.time,
    )

    with pytest.raises(PacedFloodWaitError) as excinfo:
        await pin_message(
            backend,
            request=PinMessageRequest(telegram_chat_id=-100, message_id=5),
            pacer=pacer,
        )
    exc = excinfo.value
    assert exc.attempts == 0
    assert exc.retry_after_seconds == pytest.approx(600.0)
    assert exc.retry_at == pytest.approx(clock.now + 600.0)
    # `seconds` is what `worker.queue.WorkerQueue` sleeps on; leaving it at 0
    # would make a queued paced op exhaust its retries in seconds.
    assert exc.seconds == pytest.approx(600.0)
    assert "retry after 600s" in str(exc)
    assert clock.sleeps == []
    assert backend.pins == []


async def test_rejected_gate_wait_does_not_push_the_gate_further_out(tmp_path) -> None:
    """Polling a blocked gate must not extend the block.

    ``reserve`` advances the gate by the pacing interval, so a rejection that
    still booked its slot would make every retry report a *larger*
    ``retry_after`` than the last — the caller would never be let through.
    """
    clock = FakeClock()
    gate = RateGateStore(tmp_path / "state.db")
    blocked_until = clock.now + 600.0
    gate.block_until(pin_pacing_key(-100), blocked_until)
    backend = FakePinBackend()
    pacer = Pacer(
        gate,
        min_interval_seconds=2.0,
        max_flood_wait_seconds=60.0,
        sleep=clock.sleep,
        clock=clock.time,
    )

    waits = []
    for _ in range(5):
        with pytest.raises(PacedFloodWaitError) as excinfo:
            await pin_message(
                backend,
                request=PinMessageRequest(telegram_chat_id=-100, message_id=5),
                pacer=pacer,
            )
        waits.append(excinfo.value.retry_after_seconds)

    assert waits == sorted(waits, reverse=True)
    assert waits[-1] == pytest.approx(600.0)
    assert gate.peek(pin_pacing_key(-100)) == pytest.approx(blocked_until)
    assert backend.pins == []


async def test_interval_above_the_flood_cap_still_paces(tmp_path) -> None:
    """``pin_min_interval_seconds`` is only bounded by ``ge=0``.

    The flood-wait cap must not reject the operator's *own* interval: with an
    interval above the cap every paced call would otherwise fail with a
    flood-wait error Telegram never sent, instead of simply waiting.
    """
    backend = FakePinBackend()
    clock = FakeClock()
    pacer = _pacer(
        tmp_path, clock, min_interval_seconds=90.0, max_flood_wait_seconds=60.0
    )

    for message_id in (1, 2):
        await pin_message(
            backend,
            request=PinMessageRequest(telegram_chat_id=-100, message_id=message_id),
            pacer=pacer,
        )

    assert clock.sleeps == [90.0]
    assert [p["message_id"] for p in backend.pins] == [1, 2]
    # A FLOOD_WAIT beyond the cap is still reported rather than slept off.
    flooding = FloodThenOkBackend(failures=1, seconds=600.0)
    with pytest.raises(PacedFloodWaitError):
        await pin_message(
            flooding,
            request=PinMessageRequest(telegram_chat_id=-200, message_id=9),
            pacer=pacer,
        )


async def test_gate_wait_within_cap_is_still_slept_off(tmp_path) -> None:
    """The cap only rejects long waits — ordinary pacing still waits its turn."""
    clock = FakeClock()
    gate = RateGateStore(tmp_path / "state.db")
    gate.block_until(pin_pacing_key(-100), clock.now + 10.0)
    backend = FakePinBackend()
    pacer = Pacer(
        gate,
        min_interval_seconds=2.0,
        max_flood_wait_seconds=60.0,
        sleep=clock.sleep,
        clock=clock.time,
    )

    await pin_message(
        backend,
        request=PinMessageRequest(telegram_chat_id=-100, message_id=5),
        pacer=pacer,
    )
    assert clock.sleeps == [pytest.approx(10.0)]
    assert [p["message_id"] for p in backend.pins] == [5]


async def test_unpin_flood_wait_budget_exhausted(tmp_path) -> None:
    backend = FloodThenOkBackend(failures=99, seconds=3.0)
    clock = FakeClock()
    pacer = _pacer(tmp_path, clock, max_flood_wait_retries=2)

    with pytest.raises(PacedFloodWaitError) as excinfo:
        await unpin_message(
            backend,
            request=UnpinMessageRequest(telegram_chat_id=-100, message_id=None),
            pacer=pacer,
        )
    assert excinfo.value.retry_after_seconds == 8.0
    assert backend.attempts == 2


async def test_without_pacer_backend_is_called_directly(tmp_path) -> None:
    """No pacer ⇒ pre-pacing behaviour: no waits, FLOOD_WAIT propagates as-is."""
    backend = FloodThenOkBackend(failures=1)
    with pytest.raises(FloodWaitError) as excinfo:
        await pin_message(
            backend,
            request=PinMessageRequest(telegram_chat_id=-100, message_id=5),
        )
    assert not isinstance(excinfo.value, PacedFloodWaitError)
    assert backend.attempts == 1


async def test_pacer_without_gate_still_retries_flood_wait() -> None:
    """An unopenable DB degrades to retries-only, not to a hard failure."""
    backend = FloodThenOkBackend(failures=1, seconds=1.0)
    clock = FakeClock()
    pacer = Pacer(
        None, min_interval_seconds=2.0, sleep=clock.sleep, clock=clock.time
    )
    await pin_message(
        backend,
        request=PinMessageRequest(telegram_chat_id=-100, message_id=5),
        pacer=pacer,
    )
    assert clock.sleeps == [6.0]
    assert backend.attempts == 2


async def test_paced_pin_still_denied_before_any_wait(tmp_path) -> None:
    backend = FakePinBackend()
    clock = FakeClock()
    pacer = _pacer(tmp_path, clock)
    authorizer = Authorizer(
        AccessConfig(rules=[AccessRule(all=True, permission="read")])
    )
    with pytest.raises(AccessDenied):
        await pin_message(
            backend,
            request=PinMessageRequest(telegram_chat_id=-100, message_id=1),
            authorizer=authorizer,
            pacer=pacer,
        )
    assert clock.sleeps == []
    assert backend.pins == []


async def test_marked_and_bare_chat_ids_share_one_gate(tmp_path) -> None:
    """`-1001234567890` and `1234567890` are one chat, so one pacing slot.

    Explicit-id callers pass the marked form while `entity`/`chat_name`
    resolution yields the bare one; keying the gate on the raw value would open
    two independent rows and silently stop pacing the chat.
    """
    backend = FakePinBackend()
    clock = FakeClock()
    pacer = _pacer(tmp_path, clock)

    await pin_message(
        backend,
        request=PinMessageRequest(telegram_chat_id=-1001234567890, message_id=1),
        pacer=pacer,
    )
    await pin_message(
        backend,
        request=PinMessageRequest(telegram_chat_id=1234567890, message_id=2),
        pacer=pacer,
    )
    assert clock.sleeps == [2.0]


async def test_flood_wait_under_a_marked_id_blocks_the_bare_id(tmp_path) -> None:
    """A gate blocked after FLOOD_WAIT must not be bypassable by id shape."""
    flooding = FloodThenOkBackend(failures=1, seconds=600.0)
    clock = FakeClock()
    pacer = _pacer(tmp_path, clock)
    with pytest.raises(PacedFloodWaitError):
        await pin_message(
            flooding,
            request=PinMessageRequest(telegram_chat_id=-1001234567890, message_id=1),
            pacer=pacer,
        )

    backend = FakePinBackend()
    with pytest.raises(PacedFloodWaitError):
        await pin_message(
            backend,
            request=PinMessageRequest(telegram_chat_id=1234567890, message_id=2),
            pacer=pacer,
        )
    assert backend.pins == []


class BrokenGate:
    """Rate gate whose storage faults at runtime (locked/read-only DB).

    Construction already degrades to ``gate=None`` at the pacer's build sites;
    this covers the *runtime* fault, which happens on a gate that opened fine.
    """

    def __init__(self, *, fail_reserve: bool = True, fail_block: bool = True) -> None:
        self._fail_reserve = fail_reserve
        self._fail_block = fail_block
        self.blocked: list[tuple[str, float]] = []

    def reserve(
        self,
        key: str,
        min_interval_seconds: float,
        now: float,
        *,
        max_wait: float | None = None,
    ) -> float:
        if self._fail_reserve:
            raise sqlite3.OperationalError("database is locked")
        return 0.0

    def block_until(self, key: str, next_allowed_at: float) -> None:
        if self._fail_block:
            raise sqlite3.OperationalError("database is locked")
        self.blocked.append((key, next_allowed_at))


async def test_gate_reserve_fault_degrades_to_unpaced() -> None:
    """A storage fault must not turn a pin into an unhandled 500.

    ``sqlite3.OperationalError`` is neither ``AccessDenied``, ``FloodWaitError``
    nor ``ValueError``, so the HTTP routes would let it escape untranslated.
    The pacer's documented contract is best-effort spacing, so it degrades to
    "no cross-process pacing" exactly like an unopenable DB does at build time.
    """
    backend = FakePinBackend()
    clock = FakeClock()
    pacer = Pacer(
        BrokenGate(), min_interval_seconds=2.0, sleep=clock.sleep, clock=clock.time
    )

    result = await pin_message(
        backend,
        request=PinMessageRequest(telegram_chat_id=-100, message_id=5),
        pacer=pacer,
    )
    assert result.dry_run is False
    assert [pin["message_id"] for pin in backend.pins] == [5]
    assert clock.sleeps == []


async def test_gate_block_until_fault_still_retries_the_flood_wait() -> None:
    """A gate fault while backing others off must not kill our own retry.

    ``block_until`` runs inside the ``except FloodWaitError`` handler, so an
    unguarded fault there would replace a recoverable FLOOD_WAIT — one sleep
    away from succeeding — with a storage error.
    """
    backend = FloodThenOkBackend(failures=1, seconds=7.0)
    clock = FakeClock()
    pacer = Pacer(
        BrokenGate(fail_reserve=False),
        min_interval_seconds=2.0,
        sleep=clock.sleep,
        clock=clock.time,
    )

    result = await pin_message(
        backend,
        request=PinMessageRequest(telegram_chat_id=-100, message_id=5),
        pacer=pacer,
    )
    assert result.dry_run is False
    assert backend.attempts == 2
    assert clock.sleeps == [12.0]


async def test_gate_fault_keeps_the_paced_flood_wait_error() -> None:
    """Exhausting the retry budget still reports FLOOD_WAIT, not the DB fault."""
    backend = FloodThenOkBackend(failures=5, seconds=7.0)
    clock = FakeClock()
    pacer = Pacer(
        BrokenGate(fail_reserve=False),
        min_interval_seconds=2.0,
        sleep=clock.sleep,
        clock=clock.time,
    )

    with pytest.raises(PacedFloodWaitError) as excinfo:
        await unpin_message(
            backend,
            request=UnpinMessageRequest(telegram_chat_id=-100, message_id=5),
            pacer=pacer,
        )
    assert excinfo.value.retry_after_seconds == 12.0
