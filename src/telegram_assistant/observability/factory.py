"""Build alert plumbing from :class:`AlertsConfig`."""

from __future__ import annotations

from telegram_assistant.config.models import AlertsConfig
from telegram_assistant.observability.alerts import (
    AlertEmitter,
    AlertSink,
    LogAlertSink,
    WebhookAlertSink,
)


def build_alert_emitter(config: AlertsConfig) -> AlertEmitter:
    """Always include :class:`LogAlertSink`; add a webhook sink when configured."""
    sinks: list[AlertSink] = [LogAlertSink()]
    if config.webhook_url:
        sinks.append(WebhookAlertSink(config.webhook_url))
    return AlertEmitter(sinks)
