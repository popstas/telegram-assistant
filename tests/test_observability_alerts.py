"""Tests for alert trigger behavior and sinks."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from telegram_assistant.config.models import AlertsConfig
from telegram_assistant.observability import (
    AlertEmitter,
    AlertEvent,
    AlertType,
    ErrorRateTracker,
    FloodWaitTracker,
    LogAlertSink,
    StuckBulkTracker,
    WebhookAlertSink,
    build_alert_emitter,
)


class RecordingSink:
    def __init__(self) -> None:
        self.events: list[AlertEvent] = []

    async def emit(self, event: AlertEvent) -> None:
        self.events.append(event)


# ---- Trigger: telethon session unauthorized ---------------------------------


async def test_telethon_session_unauthorized_event_emitted_to_log_sink(caplog: pytest.LogCaptureFixture) -> None:
    sink = RecordingSink()
    emitter = AlertEmitter([sink])
    event = AlertEvent(
        type=AlertType.TELETHON_SESSION_UNAUTHORIZED,
        message="session is unauthorized",
        severity="error",
        context={"account_label": "planfix-assistant-main"},
    )
    await emitter.emit(event)
    assert sink.events == [event]
    payload = event.to_dict()
    assert payload["type"] == "telethon_session_unauthorized"
    assert payload["context"]["account_label"] == "planfix-assistant-main"


# ---- Trigger: repeated FLOOD_WAIT -------------------------------------------


def test_flood_wait_tracker_fires_after_threshold() -> None:
    tracker = FloodWaitTracker(threshold=3)
    assert tracker.record_flood_wait("chat-1", seconds=5) is None
    assert tracker.record_flood_wait("chat-1", seconds=8) is None
    event = tracker.record_flood_wait("chat-1", seconds=12)
    assert event is not None
    assert event.type is AlertType.FLOOD_WAIT_REPEATED
    assert event.context["scope"] == "chat-1"
    assert event.context["consecutive"] == 3
    assert event.context["last_seconds"] == 12


def test_flood_wait_tracker_resets_on_success() -> None:
    tracker = FloodWaitTracker(threshold=2)
    tracker.record_flood_wait("chat-1")
    tracker.record_success("chat-1")
    # First flood-wait after success must not trigger yet.
    assert tracker.record_flood_wait("chat-1") is None


def test_flood_wait_tracker_scopes_are_independent() -> None:
    tracker = FloodWaitTracker(threshold=2)
    assert tracker.record_flood_wait("chat-A") is None
    assert tracker.record_flood_wait("chat-B") is None
    # B has only one flood_wait so far, A has only one — neither should trip.
    assert tracker.record_flood_wait("chat-A") is not None


# ---- Trigger: stuck bulk operation ------------------------------------------


def test_stuck_bulk_tracker_fires_after_deadline() -> None:
    tracker = StuckBulkTracker(stuck_after_seconds=10.0)
    tracker.record_progress("op-1", now=0.0)
    assert tracker.check_stuck("op-1", now=5.0) is None
    event = tracker.check_stuck("op-1", now=15.0)
    assert event is not None
    assert event.type is AlertType.BULK_OPERATION_STUCK
    assert event.context["operation_id"] == "op-1"
    assert event.context["elapsed_seconds"] == pytest.approx(15.0)


def test_stuck_bulk_tracker_returns_none_for_unknown_op() -> None:
    tracker = StuckBulkTracker(stuck_after_seconds=1.0)
    assert tracker.check_stuck("missing", now=100.0) is None


# ---- Trigger: operation moved to needs_review -------------------------------


async def test_operation_needs_review_event_renders_redacted_context() -> None:
    sink = RecordingSink()
    emitter = AlertEmitter([sink])
    await emitter.emit(
        AlertEvent(
            type=AlertType.OPERATION_NEEDS_REVIEW,
            message="operation requires manual review",
            severity="error",
            context={
                "operation_id": "op-99",
                "bearer_token": "should not leak",
                "invite_link": "https://t.me/+secret",
            },
        )
    )
    assert sink.events[0].type is AlertType.OPERATION_NEEDS_REVIEW
    payload = sink.events[0].to_dict()
    assert payload["context"]["bearer_token"] == "***REDACTED***"
    assert payload["context"]["invite_link"] == "***REDACTED***"


# ---- Trigger: default chat folder unavailable -------------------------------


async def test_default_folder_unavailable_event_carries_folder_name() -> None:
    sink = RecordingSink()
    emitter = AlertEmitter([sink])
    await emitter.emit(
        AlertEvent(
            type=AlertType.DEFAULT_FOLDER_UNAVAILABLE,
            message="default folder missing",
            severity="error",
            context={"folder_name": "Planfix clients"},
        )
    )
    assert sink.events[0].context["folder_name"] == "Planfix clients"


# ---- Trigger: error rate above threshold ------------------------------------


def test_error_rate_tracker_does_not_fire_below_window() -> None:
    tracker = ErrorRateTracker(window_size=5, threshold=0.5)
    # 4 errors but window not full
    for _ in range(4):
        assert tracker.record(error=True) is None


def test_error_rate_tracker_fires_when_threshold_crossed() -> None:
    tracker = ErrorRateTracker(window_size=4, threshold=0.5)
    assert tracker.record(error=False) is None
    assert tracker.record(error=False) is None
    assert tracker.record(error=True) is None
    event = tracker.record(error=True)
    assert event is not None
    assert event.type is AlertType.ERROR_RATE_ABOVE_THRESHOLD
    assert event.context["errors"] == 2
    assert event.context["window_size"] == 4


def test_error_rate_tracker_recovers_when_errors_drop() -> None:
    tracker = ErrorRateTracker(window_size=4, threshold=0.75)
    for _ in range(4):
        tracker.record(error=True)
    # Sliding the window — 3 successes incoming, ratio falls below.
    tracker.record(error=False)
    tracker.record(error=False)
    tracker.record(error=False)
    # Now only 1 error in the window — well under 0.75
    assert tracker.record(error=False) is None


# ---- Sinks ------------------------------------------------------------------


async def test_log_alert_sink_forwards_to_logger() -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    class FakeLogger:
        def warning(self, event: str, **fields: Any) -> None:
            calls.append((event, fields))

        error = warning

    sink = LogAlertSink(logger=FakeLogger())
    await sink.emit(
        AlertEvent(
            type=AlertType.TELETHON_SESSION_UNAUTHORIZED,
            message="hi",
            severity="error",
            context={"account_label": "main"},
        )
    )
    assert calls
    event, fields = calls[0]
    assert event == "alert"
    assert fields["alert_type"] == "telethon_session_unauthorized"
    assert fields["account_label"] == "main"


async def test_webhook_alert_sink_posts_payload() -> None:
    posted: list[dict[str, Any]] = []

    class FakeClient:
        async def post(self, url: str, json: dict[str, Any]) -> None:
            posted.append({"url": url, "json": json})

    sink = WebhookAlertSink("https://example.test/alert", client=FakeClient())
    await sink.emit(
        AlertEvent(
            type=AlertType.FLOOD_WAIT_REPEATED,
            message="flood",
            severity="warning",
            context={"scope": "chat-1"},
        )
    )
    assert len(posted) == 1
    assert posted[0]["url"] == "https://example.test/alert"
    assert posted[0]["json"]["type"] == "flood_wait_repeated"
    assert posted[0]["json"]["context"]["scope"] == "chat-1"


async def test_webhook_alert_sink_swallows_errors() -> None:
    class FakeClient:
        async def post(self, url: str, json: dict[str, Any]) -> None:
            raise RuntimeError("network down")

    logged: list[tuple[str, dict[str, Any]]] = []

    class FakeLogger:
        def error(self, event: str, **fields: Any) -> None:
            logged.append((event, fields))

    sink = WebhookAlertSink(
        "https://example.test/alert", client=FakeClient(), logger=FakeLogger()
    )
    # Must not raise — alert pipeline cannot bring down the caller.
    await sink.emit(
        AlertEvent(type=AlertType.OPERATION_NEEDS_REVIEW, message="x")
    )
    assert logged
    assert logged[0][0] == "alert_webhook_failed"


# ---- Builder ----------------------------------------------------------------


def test_build_alert_emitter_log_only() -> None:
    emitter = build_alert_emitter(AlertsConfig())
    assert len(emitter.sinks) == 1
    assert isinstance(emitter.sinks[0], LogAlertSink)


def test_build_alert_emitter_with_webhook() -> None:
    emitter = build_alert_emitter(
        AlertsConfig(webhook_url="https://example.test/alert")
    )
    assert len(emitter.sinks) == 2
    assert isinstance(emitter.sinks[0], LogAlertSink)
    assert isinstance(emitter.sinks[1], WebhookAlertSink)


# ---- Emitter fan-out --------------------------------------------------------


async def test_emitter_fans_out_to_all_sinks() -> None:
    a, b = RecordingSink(), RecordingSink()
    emitter = AlertEmitter([a, b])
    event = AlertEvent(type=AlertType.BULK_OPERATION_STUCK, message="x")
    await emitter.emit(event)
    assert a.events == [event]
    assert b.events == [event]


def test_emitter_emit_sync_runs_when_no_loop() -> None:
    sink = RecordingSink()
    emitter = AlertEmitter([sink])
    emitter.emit_sync(
        AlertEvent(type=AlertType.DEFAULT_FOLDER_UNAVAILABLE, message="x")
    )
    assert sink.events
    assert sink.events[0].type is AlertType.DEFAULT_FOLDER_UNAVAILABLE


def test_emitter_emit_sync_schedules_inside_running_loop() -> None:
    sink = RecordingSink()
    emitter = AlertEmitter([sink])

    async def _run() -> None:
        emitter.emit_sync(
            AlertEvent(type=AlertType.OPERATION_NEEDS_REVIEW, message="x")
        )
        # Yield once so the scheduled task gets a turn.
        await asyncio.sleep(0)

    asyncio.run(_run())
    assert sink.events
