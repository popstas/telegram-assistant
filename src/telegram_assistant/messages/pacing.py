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

from telegram_assistant.worker.queue import FloodWaitError

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

    def reserve(self, key: str, min_interval_seconds: float, now: float) -> float:
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
        self.args = (
            f"FLOOD_WAIT {self.seconds:.0f}s not absorbed after {self.attempts} "
            f"attempt(s); retry after {self.retry_after_seconds:.0f}s",
        )


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

    @property
    def min_interval_seconds(self) -> float:
        return self._min_interval

    async def run(self, key: str, op: Callable[[], Awaitable[T]]) -> T:
        """Pace, run ``op``, and retry it through bounded FLOOD_WAIT pauses."""
        await self._wait_for_slot(key)

        attempts = 0
        while True:
            try:
                return await op()
            except FloodWaitError as exc:
                attempts += 1
                pause = max(float(getattr(exc, "seconds", 0.0)), 0.0) + self._margin
                retry_at = self._clock() + pause
                # Push the shared gate out so other processes/surfaces also back
                # off for this chat instead of walking into the same wall.
                if self._gate is not None:
                    self._gate.block_until(key, retry_at)
                if attempts >= self._max_retries or pause > self._max_wait:
                    raise PacedFloodWaitError(
                        getattr(exc, "seconds", 0.0),
                        retry_after_seconds=pause,
                        retry_at=retry_at,
                        attempts=attempts,
                    ) from exc
                await self._sleep(pause)

    async def _wait_for_slot(self, key: str) -> None:
        if self._gate is None or self._min_interval <= 0:
            return
        wait = self._gate.reserve(key, self._min_interval, self._clock())
        if wait > 0:
            await self._sleep(wait)


def pin_pacing_key(chat_id: int) -> str:
    """Gate key for pin/unpin — Telegram's pin limits bite per chat."""
    return f"pin:{chat_id}"


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
]
