"""Structured logging, secret redaction, and alert-hook plumbing."""

from telegram_assistant.observability.alerts import (
    AlertEmitter,
    AlertEvent,
    AlertSink,
    AlertType,
    ErrorRateTracker,
    FloodWaitTracker,
    LogAlertSink,
    StuckBulkTracker,
    WebhookAlertSink,
)
from telegram_assistant.observability.factory import build_alert_emitter
from telegram_assistant.observability.logging import (
    LOG_CONTEXT_FIELDS,
    bind_request_context,
    clear_request_context,
    configure_logging,
    get_logger,
    redact_invite_links,
    redact_secrets,
)

__all__ = [
    "AlertEmitter",
    "AlertEvent",
    "AlertSink",
    "AlertType",
    "ErrorRateTracker",
    "FloodWaitTracker",
    "LogAlertSink",
    "StuckBulkTracker",
    "WebhookAlertSink",
    "LOG_CONTEXT_FIELDS",
    "bind_request_context",
    "build_alert_emitter",
    "clear_request_context",
    "configure_logging",
    "get_logger",
    "redact_invite_links",
    "redact_secrets",
]
