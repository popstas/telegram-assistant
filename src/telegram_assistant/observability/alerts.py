"""Alert hooks for operational incidents the service must surface.

Six trigger types are required by the plan:

* ``telethon_session_unauthorized`` — emitted when the worker discovers a
  request can't proceed because the Telethon session is logged out.
* ``flood_wait_repeated`` — emitted when consecutive ``FLOOD_WAIT`` pauses
  cross :class:`FloodWaitTracker.threshold` for the same chat.
* ``bulk_operation_stuck`` — emitted when a bulk operation hasn't made
  progress within a configured timeout.
* ``operation_needs_review`` — emitted whenever an operation transitions to
  ``needs_review`` so an operator can pick it up.
* ``default_folder_unavailable`` — emitted when the configured chat folder
  is missing, which would silently break group placement.
* ``error_rate_above_threshold`` — emitted when the sliding-window error
  rate crosses :class:`ErrorRateTracker.threshold`.

Sinks are pluggable: :class:`LogAlertSink` is always wired up so alerts go
to the JSON log even without an out-of-band channel, and
:class:`WebhookAlertSink` POSTs to an HTTPS URL when one is configured.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol

from telegram_assistant.observability.logging import (
    get_logger,
    redact_secrets,
)


class AlertType(StrEnum):
    TELETHON_SESSION_UNAUTHORIZED = "telethon_session_unauthorized"
    FLOOD_WAIT_REPEATED = "flood_wait_repeated"
    BULK_OPERATION_STUCK = "bulk_operation_stuck"
    OPERATION_NEEDS_REVIEW = "operation_needs_review"
    DEFAULT_FOLDER_UNAVAILABLE = "default_folder_unavailable"
    ERROR_RATE_ABOVE_THRESHOLD = "error_rate_above_threshold"


@dataclass(frozen=True)
class AlertEvent:
    """Payload delivered to every configured sink."""

    type: AlertType
    message: str
    severity: str = "warning"
    context: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type.value,
            "message": self.message,
            "severity": self.severity,
            "context": redact_secrets(dict(self.context)),
        }


class AlertSink(Protocol):
    """A destination capable of accepting :class:`AlertEvent` instances."""

    async def emit(self, event: AlertEvent) -> None:
        ...


class LogAlertSink:
    """Default sink: forward the alert as a structured ``alert`` log entry.

    Always wired up so alerts are at minimum visible in the JSON log even
    when no external channel is configured.
    """

    def __init__(self, logger: Any | None = None) -> None:
        self._logger = logger if logger is not None else get_logger("alerts")

    async def emit(self, event: AlertEvent) -> None:
        method = getattr(self._logger, event.severity, None)
        if method is None:
            method = self._logger.warning
        method(
            "alert",
            alert_type=event.type.value,
            message=event.message,
            **event.context,
        )


class WebhookAlertSink:
    """Optional sink: POST the alert payload as JSON to ``url``.

    Failures to deliver are logged and swallowed — an alert pipeline should
    not itself bring the service down. The HTTP client is injected so tests
    don't need a real network.
    """

    def __init__(
        self,
        url: str,
        *,
        client: Any | None = None,
        logger: Any | None = None,
    ) -> None:
        self._url = url
        self._client = client
        self._logger = logger if logger is not None else get_logger("alerts")

    async def emit(self, event: AlertEvent) -> None:
        payload = event.to_dict()
        try:
            if self._client is None:
                # Lazy import so httpx is only required when a webhook is
                # actually configured.
                import httpx

                async with httpx.AsyncClient(timeout=10.0) as client:
                    await client.post(self._url, json=payload)
            else:
                await self._client.post(self._url, json=payload)
        except Exception as exc:
            self._logger.error(
                "alert_webhook_failed",
                alert_type=event.type.value,
                error=str(exc),
            )


class AlertEmitter:
    """Fan an alert event out to every configured sink."""

    def __init__(self, sinks: Iterable[AlertSink]) -> None:
        self._sinks = list(sinks)
        # Tasks scheduled by ``emit_sync`` are anchored here so the event-loop
        # GC can't reclaim them before they finish; ``asyncio.create_task``'s
        # only strong reference is from the caller, and our caller is the
        # function returning to a sync frame.
        self._pending: set[asyncio.Task[None]] = set()

    @property
    def sinks(self) -> list[AlertSink]:
        return list(self._sinks)

    async def emit(self, event: AlertEvent) -> None:
        for sink in self._sinks:
            await sink.emit(event)

    def emit_sync(self, event: AlertEvent) -> None:
        """Schedule the emit from a sync context.

        Used by code paths that aren't natively async (the CLI, certain
        startup checks). Falls back to running the coroutine to completion
        when no event loop is running.
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(self.emit(event))
        else:
            task = loop.create_task(self.emit(event))
            self._pending.add(task)
            task.add_done_callback(self._pending.discard)


class FloodWaitTracker:
    """Count consecutive FLOOD_WAIT events per scope and trigger an alert.

    The queue calls :meth:`record_flood_wait` whenever Telegram returns
    ``FLOOD_WAIT`` for a given scope (typically a chat id or ``"global"``)
    and :meth:`record_success` on a successful operation. Crossing the
    ``threshold`` of consecutive flood-waits returns an :class:`AlertEvent`
    that the caller emits — kept synchronous so the caller controls the
    scheduling of the async sink call.
    """

    def __init__(self, *, threshold: int = 3) -> None:
        if threshold < 1:
            raise ValueError("threshold must be >= 1")
        self._threshold = threshold
        self._counts: dict[str, int] = {}

    @property
    def threshold(self) -> int:
        return self._threshold

    def record_success(self, scope: str = "global") -> None:
        self._counts.pop(scope, None)

    def record_flood_wait(
        self,
        scope: str = "global",
        *,
        seconds: float | None = None,
    ) -> AlertEvent | None:
        count = self._counts.get(scope, 0) + 1
        self._counts[scope] = count
        if count >= self._threshold:
            return AlertEvent(
                type=AlertType.FLOOD_WAIT_REPEATED,
                message=(
                    f"FLOOD_WAIT seen {count} times in a row for scope={scope}"
                ),
                severity="warning",
                context={
                    "scope": scope,
                    "consecutive": count,
                    "last_seconds": seconds,
                },
            )
        return None


class ErrorRateTracker:
    """Sliding-window error counter.

    The caller pushes outcomes via :meth:`record` and gets back a non-``None``
    alert when the ratio of errors in the trailing ``window_size`` operations
    crosses ``threshold``. The window is intentionally small so unit tests
    can assert exact transitions.
    """

    def __init__(self, *, window_size: int = 20, threshold: float = 0.5) -> None:
        if window_size < 1:
            raise ValueError("window_size must be >= 1")
        if not 0.0 < threshold <= 1.0:
            raise ValueError("threshold must be in (0, 1]")
        self._window: deque[bool] = deque(maxlen=window_size)
        self._threshold = threshold

    @property
    def threshold(self) -> float:
        return self._threshold

    def record(self, *, error: bool) -> AlertEvent | None:
        self._window.append(bool(error))
        if len(self._window) < self._window.maxlen:  # type: ignore[operator]
            return None
        errors = sum(1 for x in self._window if x)
        ratio = errors / len(self._window)
        if ratio >= self._threshold:
            return AlertEvent(
                type=AlertType.ERROR_RATE_ABOVE_THRESHOLD,
                message=(
                    f"error_rate={ratio:.2f} crossed threshold={self._threshold:.2f}"
                ),
                severity="error",
                context={
                    "errors": errors,
                    "window_size": len(self._window),
                    "threshold": self._threshold,
                },
            )
        return None


class StuckBulkTracker:
    """Detect a bulk operation that hasn't made progress within a deadline."""

    def __init__(self, *, stuck_after_seconds: float = 600.0) -> None:
        if stuck_after_seconds <= 0:
            raise ValueError("stuck_after_seconds must be positive")
        self._stuck_after = float(stuck_after_seconds)
        self._last_progress: dict[str, float] = {}

    def record_progress(self, operation_id: str, *, now: float | None = None) -> None:
        self._last_progress[operation_id] = now if now is not None else time.monotonic()

    def check_stuck(
        self,
        operation_id: str,
        *,
        now: float | None = None,
    ) -> AlertEvent | None:
        last = self._last_progress.get(operation_id)
        if last is None:
            return None
        clock = now if now is not None else time.monotonic()
        elapsed = clock - last
        if elapsed >= self._stuck_after:
            return AlertEvent(
                type=AlertType.BULK_OPERATION_STUCK,
                message=(
                    f"bulk operation {operation_id} has not progressed for "
                    f"{elapsed:.0f}s (threshold={self._stuck_after:.0f}s)"
                ),
                severity="error",
                context={
                    "operation_id": operation_id,
                    "elapsed_seconds": elapsed,
                    "stuck_after_seconds": self._stuck_after,
                },
            )
        return None
