"""Server-side pacing + FLOOD_WAIT retry for burst-sensitive message ops.

Pin/unpin is the motivating case: Telegram answers a rapid series of `pin`
calls with `FLOOD_WAIT` (in practice after ~3 quick pins), and until now the
error was merely translated and handed back to the caller — every surface had
to cope on its own. This module puts the policy in the domain layer instead, so
CLI, HTTP and MCP all inherit it:

  1. **Pace** — before each real backend call, wait out the shared gate
     (:class:`RateGate`, backed by SQLite) so a burst spreads over
     ``min_interval_seconds``. The gate is cross-process: a CLI one-shot and the
     running server pace against each other, since they drive the same account.
  2. **Retry** — on ``FloodWaitError`` sleep ``seconds + safety margin`` and try
     again, mirroring :class:`telegram_assistant.worker.queue.WorkerQueue`
     semantics (margin 5s, bounded attempts). The wait is also written back into
     the gate so *other* processes back off too.
  3. **Report** — when the retry budget is exhausted (or a single wait exceeds
     the cap) raise :class:`PacedFloodWaitError`, which carries
     ``retry_after_seconds`` so surfaces can tell the caller when to come back.

:class:`PacedFloodWaitError` subclasses ``FloodWaitError``, so existing
flood-wait error mapping (HTTP 502 / MCP ``needs_review``) keeps working
unchanged; surfaces only add the retry-after detail.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Protocol, TypeVar

from telegram_assistant.entities.service import EntityRef
from telegram_assistant.observability.logging import get_logger
from telegram_assistant.worker.queue import FloodWaitError

_log = get_logger(__name__)

T = TypeVar("T")

SleepFn = Callable[[float], Awaitable[None]]
ClockFn = Callable[[], float]

# Defaults mirror the worker queue so pacing and queued retries behave alike.
DEFAULT_FLOOD_WAIT_MARGIN_SECONDS = 5.0
DEFAULT_MAX_FLOOD_WAIT_RETRIES = 3
# A single FLOOD_WAIT longer than this is not worth holding a request open for;
# report it to the caller instead of sleeping for minutes.
DEFAULT_MAX_FLOOD_WAIT_SECONDS = 60.0


class RateGate(Protocol):
    """Shared pacing state — see :class:`~persistence.rate_gate.RateGateStore`."""

    def reserve(
        self,
        key: str,
        min_interval_seconds: float,
        now: float,
        *,
        max_wait: float | None = None,
    ) -> float:
        ...

    def block_until(self, key: str, next_allowed_at: float) -> None:
        ...


class PacedFloodWaitError(FloodWaitError):
    """FLOOD_WAIT that pacing could not absorb within its retry budget.

    ``retry_after_seconds`` is how long the caller should wait before trying
    again, and ``retry_at`` the corresponding epoch (the value written into the
    shared gate, so it is the same instant every surface reports).
    """

    def __init__(
        self,
        seconds: float,
        *,
        retry_after_seconds: float,
        retry_at: float,
        attempts: int,
    ) -> None:
        super().__init__(seconds)
        self.retry_after_seconds = max(float(retry_after_seconds), 0.0)
        self.retry_at = float(retry_at)
        self.attempts = int(attempts)
        if self.attempts:
            reason = (
                f"FLOOD_WAIT {self.seconds:.0f}s not absorbed after "
                f"{self.attempts} attempt(s)"
            )
        else:
            # Raised before the op ran: the shared gate is still blocked from an
            # earlier FLOOD_WAIT, longer than we are willing to wait inline.
            reason = "pacing gate is blocked by an earlier FLOOD_WAIT"
        self.args = (f"{reason}; retry after {self.retry_after_seconds:.0f}s",)


class Pacer:
    """Runs a coroutine under a shared minimum interval + FLOOD_WAIT retries.

    ``gate`` may be ``None`` (no cross-process pacing state available — the
    retry behaviour still applies) and ``min_interval_seconds`` ``0`` disables
    pacing entirely. ``sleep``/``clock`` are injectable so tests use a fake
    clock instead of waiting for real seconds.
    """

    def __init__(
        self,
        gate: RateGate | None = None,
        *,
        min_interval_seconds: float = 0.0,
        flood_wait_safety_margin_seconds: float = DEFAULT_FLOOD_WAIT_MARGIN_SECONDS,
        max_flood_wait_retries: int = DEFAULT_MAX_FLOOD_WAIT_RETRIES,
        max_flood_wait_seconds: float = DEFAULT_MAX_FLOOD_WAIT_SECONDS,
        sleep: SleepFn | None = None,
        clock: ClockFn | None = None,
    ) -> None:
        if max_flood_wait_retries < 1:
            raise ValueError("max_flood_wait_retries must be >= 1")
        self._gate = gate
        self._min_interval = max(float(min_interval_seconds), 0.0)
        self._margin = float(flood_wait_safety_margin_seconds)
        self._max_retries = int(max_flood_wait_retries)
        self._max_wait = float(max_flood_wait_seconds)
        self._sleep: SleepFn = sleep if sleep is not None else asyncio.sleep
        self._clock: ClockFn = clock if clock is not None else time.time

    async def run(self, key: str, op: Callable[[], Awaitable[T]]) -> T:
        """Pace, run ``op``, and retry it through bounded FLOOD_WAIT pauses."""
        attempts = 0
        while True:
            # Re-taken on *every* attempt, not just the first. The retry sleep
            # below backs this caller off, and `block_until` tells the others to
            # back off — but the gate then opens for everyone at the same
            # instant: while we sleep, another process can reserve that very
            # slot and walk in at `retry_at`, so both calls would hit Telegram
            # together at the retry boundary — the burst the gate exists to
            # prevent, on the one chat Telegram just flood-waited. Reserving
            # again makes the retry queue behind whoever booked in the meantime.
            await self._wait_for_slot(key, attempts=attempts)
            try:
                return await op()
            except FloodWaitError as exc:
                attempts += 1
                pause = max(float(getattr(exc, "seconds", 0.0)), 0.0) + self._margin
                retry_at = self._clock() + pause
                # Push the shared gate out so other processes/surfaces also back
                # off for this chat instead of walking into the same wall.
                # Best-effort: a gate fault here must not replace the
                # FLOOD_WAIT we are about to retry (or report) with a storage
                # error — this caller still sleeps, the others just miss the
                # hint. Mirrors how the folder-membership cache degrades.
                # The inner binding must NOT be named `exc`: Python deletes an
                # `except ... as` name when its block ends, which would unbind
                # the FLOOD_WAIT we are still handling below.
                if self._gate is not None:
                    try:
                        self._gate.block_until(key, retry_at)
                    except Exception as gate_exc:
                        _log.warning(
                            "rate_gate_block_until_failed", key=key, error=str(gate_exc)
                        )
                if attempts >= self._max_retries or pause > self._max_wait:
                    raise PacedFloodWaitError(
                        getattr(exc, "seconds", 0.0),
                        retry_after_seconds=pause,
                        retry_at=retry_at,
                        attempts=attempts,
                    ) from exc
                # A silent multi-minute (or multi-hour, across retries) sleep is
                # indistinguishable from a hang to whoever is watching the
                # process — name the chat/key, the pause and which attempt this
                # is before going quiet for it.
                _log.warning(
                    "flood_wait_pause", key=key, seconds=pause, attempt=attempts
                )
                await self._sleep(pause)

    async def _wait_for_slot(self, key: str, *, attempts: int = 0) -> None:
        if self._gate is None or self._min_interval <= 0:
            return
        # The cap bounds waits *written into the gate by a FLOOD_WAIT*; it must
        # never reject the operator's own configured interval. With
        # `pin_min_interval_seconds` above `max_flood_wait_seconds` a plain
        # min-interval wait would otherwise exceed the cap, so every paced call
        # would fail with a flood-wait error Telegram never sent.
        cap = max(self._max_wait, self._min_interval)
        # `max_wait` keeps the reservation conditional: a call we are about to
        # reject must not book (and thereby advance) the slot, or a client
        # polling a flood-waited chat would push its own retry time further out
        # with every rejected attempt.
        # Best-effort, like the pacer's construction sites: an unopenable DB
        # already degrades to "no cross-process spacing" there, and a *runtime*
        # fault (write lock held past the busy timeout by the very CLI one-shot
        # this gate exists to pace against, read-only DB dir) must degrade the
        # same way. Letting `sqlite3.OperationalError` escape would surface as
        # an unhandled HTTP 500 — it is neither `AccessDenied`, `FloodWaitError`
        # nor `ValueError`, the only errors the pin/unpin routes translate.
        try:
            wait = self._gate.reserve(
                key, self._min_interval, self._clock(), max_wait=cap
            )
        except Exception as gate_exc:
            _log.warning("rate_gate_reserve_failed", key=key, error=str(gate_exc))
            return
        if wait <= 0:
            return
        if wait > cap:
            # The gate is far in the future — typically a FLOOD_WAIT another
            # call (or another process) wrote into it. Holding this request open
            # for that long defeats the same cap `run()` applies to its own
            # sleeps, so report the wait instead of sleeping it off.
            # `seconds` carries the real wait too: it is the attribute
            # `worker.queue.WorkerQueue` sleeps on (`max(fw.seconds, 0) +
            # margin`), so leaving it at 0 would make a queued paced op burn its
            # whole retry budget in a few seconds and land in `needs_review`.
            raise PacedFloodWaitError(
                wait,
                retry_after_seconds=wait,
                retry_at=self._clock() + wait,
                # `attempts` is what the op has already spent: 0 before the
                # first call, and the real count when a retry finds the gate
                # pushed further out by someone else while it slept.
                attempts=attempts,
            )
        await self._sleep(wait)


def pin_pacing_key(chat_id: int) -> str:
    """Gate key for pin/unpin — Telegram's pin limits bite per chat.

    The id is reduced to its bare form (``EntityRef.numeric_id``, i.e. the
    ``-100`` marker stripped) because the same chat reaches the domain in either
    shape: an explicit ``telegram_chat_id: -1001234567890`` keeps the marker,
    while an ``entity``/``chat_name`` lookup yields the bare ``1234567890``.
    Keying on the raw value would open *two* independent gate rows for one chat,
    so cross-surface pacing — the whole point of the shared SQLite gate — would
    silently not apply, and a ``block_until()`` written after a FLOOD_WAIT under
    one shape would leave the other wide open.
    """
    return f"pin:{EntityRef(raw=int(chat_id)).numeric_id}"


def ttl_pacing_key(chat_id: int) -> str:
    """Gate key for auto-delete (TTL) writes — a different Telegram limit than pins.

    Its own gate row rather than sharing ``pin:``: Telegram meters
    ``messages.SetHistoryTTL`` separately, and observed waits on it escalate far
    past anything pins produce (261s, 703s and 866s within one hour on one
    account, 2026-08-05). Sharing a row would let a slow TTL sweep throttle
    unrelated pins and vice versa.

    The id is reduced to its bare form for the same reason ``pin_pacing_key``
    does it: an explicit ``--chat-id -1001234567890`` keeps the marker while an
    ``--entity`` lookup yields the bare id, and keying on the raw value would
    open two independent rows for one chat.
    """
    return f"ttl:{EntityRef(raw=int(chat_id)).numeric_id}"


def retry_after_details(exc: BaseException) -> dict[str, float] | None:
    """Extract the retry-after payload from a paced flood-wait error.

    Returns ``None`` for anything without retry-after information, so surfaces
    can call it unconditionally on their flood-wait path.
    """
    retry_after = getattr(exc, "retry_after_seconds", None)
    if retry_after is None:
        return None
    details: dict[str, float] = {"retry_after_seconds": float(retry_after)}
    retry_at = getattr(exc, "retry_at", None)
    if retry_at is not None:
        details["retry_at"] = float(retry_at)
    return details


__all__ = [
    "DEFAULT_FLOOD_WAIT_MARGIN_SECONDS",
    "DEFAULT_MAX_FLOOD_WAIT_RETRIES",
    "DEFAULT_MAX_FLOOD_WAIT_SECONDS",
    "Pacer",
    "PacedFloodWaitError",
    "RateGate",
    "pin_pacing_key",
    "retry_after_details",
    "ttl_pacing_key",
]
