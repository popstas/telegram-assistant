"""Structured (JSON) logging configuration with secret redaction.

All long-form operations log through ``structlog.get_logger`` so each line
carries the standard operation context fields defined in the plan's
Technical Details section: ``operation_id``, ``request_id`` (HTTP) or
``invocation_id`` (CLI), ``planfix_task_id``, ``chat_name``, ``topic_name``,
``telegram_chat_id``, ``telegram_topic_id``, operation type, bulk item,
result, error, duration.

Secrets that show up in payloads — the HTTP bearer token, ``api_hash`` from
the Telegram config, and any invite link — are scrubbed before the record is
serialized. Redaction happens in a structlog processor so callers cannot
forget to call it.
"""

from __future__ import annotations

import logging
import re
import sys
from collections.abc import Iterable, Mapping
from typing import Any

import structlog

LOG_CONTEXT_FIELDS: tuple[str, ...] = (
    "operation_id",
    "request_id",
    "invocation_id",
    "planfix_task_id",
    "chat_name",
    "topic_name",
    "telegram_chat_id",
    "telegram_topic_id",
    "operation_type",
    "bulk_item",
    "result",
    "error",
    "duration_ms",
)

# Field names whose VALUES are always sensitive (full redaction).
_SECRET_FIELDS: frozenset[str] = frozenset(
    {
        "bearer_token",
        "api_hash",
        "authorization",
        "token",
        "password",
        "invite_link",
    }
)

# Field names whose values are treated as sensitive enough to redact in full
# even though the key itself doesn't look secret. Used for nested payloads.
_SENSITIVE_VALUE_PATTERN = re.compile(
    r"(https?://t(?:elegram)?\.me/(?:joinchat/|\+)[A-Za-z0-9_\-]+)"
)

_REDACTED = "***REDACTED***"


def redact_invite_links(text: str) -> str:
    """Replace any Telegram invite link in ``text`` with the redacted marker.

    Covers both legacy ``t.me/joinchat/<hash>`` and modern ``t.me/+<hash>``
    plus the ``telegram.me`` host. Public-username ``t.me/<name>`` links are
    not invite links and are left alone.
    """
    if not text:
        return text
    return _SENSITIVE_VALUE_PATTERN.sub(_REDACTED, text)


def _redact_value(key: str, value: Any) -> Any:
    if isinstance(value, Mapping):
        return {k: _redact_value(k, v) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact_value(key, v) for v in value]
    if isinstance(value, tuple):
        return tuple(_redact_value(key, v) for v in value)
    if isinstance(value, str):
        if key.lower() in _SECRET_FIELDS:
            return _REDACTED
        return redact_invite_links(value)
    return value


def redact_secrets(event_dict: dict[str, Any]) -> dict[str, Any]:
    """structlog processor: redact known-sensitive fields and invite links."""
    return {k: _redact_value(k, v) for k, v in event_dict.items()}


def _redact_processor(
    _logger: Any, _method_name: str, event_dict: dict[str, Any]
) -> dict[str, Any]:
    return redact_secrets(event_dict)


_CONFIGURED = False


# Third-party loggers whose chatter propagates to the root logger. Telethon
# logs every MTProto packet at DEBUG and "User is already connected!" at INFO,
# so it floods output whenever the app level is INFO or lower. We cap it
# independently of the app level (see ``telethon_level`` below).
_NOISY_LOGGERS: tuple[str, ...] = ("telethon",)


def configure_logging(
    level: str = "INFO",
    *,
    stream: Any = None,
    force: bool = False,
    telethon_level: str | None = None,
) -> None:
    """Configure structlog + stdlib logging to emit one JSON object per line.

    Safe to call multiple times — only the first call (or one with
    ``force=True``) installs handlers, so importing the module never
    duplicates output.

    ``telethon_level`` controls Telethon's own stdlib logger. When ``None``
    (the default) Telethon is capped so it is never more verbose than
    ``WARNING``, regardless of the app ``level`` — this keeps per-health-check
    Telethon spam out of the logs while the app runs at DEBUG/INFO. Pass an
    explicit level (e.g. ``"DEBUG"``) to opt back into Telethon's output.
    """
    global _CONFIGURED
    if _CONFIGURED and not force:
        return

    target = stream if stream is not None else sys.stderr
    handler = logging.StreamHandler(target)
    handler.setFormatter(logging.Formatter("%(message)s"))

    root = logging.getLogger()
    # Replace existing handlers so a second `configure_logging(force=True)`
    # call from tests doesn't double-print.
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level.upper())

    # Cap noisy third-party loggers. Default: never more verbose than WARNING,
    # but honor a more restrictive app level (e.g. ERROR keeps Telethon at
    # ERROR). An explicit ``telethon_level`` overrides the cap entirely.
    if telethon_level is not None:
        noisy_level: int | str = telethon_level.upper()
    else:
        app_numeric = logging.getLevelName(level.upper())
        noisy_level = (
            max(app_numeric, logging.WARNING)
            if isinstance(app_numeric, int)
            else logging.WARNING
        )
    for name in _NOISY_LOGGERS:
        logging.getLogger(name).setLevel(noisy_level)

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            _redact_processor,
            structlog.processors.JSONRenderer(sort_keys=True),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.getLevelName(level.upper())
            if isinstance(level, str)
            else level
        ),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(file=target),
        cache_logger_on_first_use=False,
    )
    _CONFIGURED = True


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """Return a structlog logger. Configures defaults if not yet configured."""
    if not _CONFIGURED:
        configure_logging()
    return structlog.get_logger(name) if name else structlog.get_logger()


def bind_request_context(
    *,
    fields: Iterable[str] = LOG_CONTEXT_FIELDS,
    **values: Any,
) -> None:
    """Bind selected context fields into the contextvars-scoped log context.

    Unknown fields are ignored to keep call sites tolerant; ``None`` values
    are skipped so the rendered log line is not polluted with nulls.
    """
    allowed = set(fields)
    payload = {k: v for k, v in values.items() if k in allowed and v is not None}
    if payload:
        structlog.contextvars.bind_contextvars(**payload)


def clear_request_context() -> None:
    """Drop the contextvars-scoped log context. Tests use this for isolation."""
    structlog.contextvars.clear_contextvars()
