"""Tests for structured logging shape and secret redaction."""

from __future__ import annotations

import io
import json
import logging

import pytest

from telegram_assistant.observability import (
    bind_request_context,
    clear_request_context,
    configure_logging,
    get_logger,
    redact_invite_links,
    redact_secrets,
)


@pytest.fixture(autouse=True)
def _reset_context() -> None:
    clear_request_context()
    yield
    clear_request_context()


def _capture_log(call) -> dict:
    buf = io.StringIO()
    configure_logging(level="DEBUG", stream=buf, force=True)
    call(get_logger("test"))
    raw = buf.getvalue().strip().splitlines()
    assert raw, "expected at least one log line"
    # Each line is a JSON object on its own.
    return json.loads(raw[-1])


def test_log_line_is_json_with_context_fields() -> None:
    bind_request_context(
        operation_id="op-1",
        request_id="req-1",
        planfix_task_id="42",
        chat_name="Client chat",
        topic_name="Topic 1",
        telegram_chat_id=-1001,
        telegram_topic_id=7,
        operation_type="topics.create",
        result="completed",
        duration_ms=12,
    )
    record = _capture_log(
        lambda log: log.info("topic_created", bulk_item="item-1")
    )
    assert record["event"] == "topic_created"
    assert record["operation_id"] == "op-1"
    assert record["request_id"] == "req-1"
    assert record["planfix_task_id"] == "42"
    assert record["chat_name"] == "Client chat"
    assert record["topic_name"] == "Topic 1"
    assert record["telegram_chat_id"] == -1001
    assert record["telegram_topic_id"] == 7
    assert record["operation_type"] == "topics.create"
    assert record["bulk_item"] == "item-1"
    assert record["result"] == "completed"
    assert record["duration_ms"] == 12
    assert "timestamp" in record
    assert "level" in record


def test_telethon_logger_capped_at_warning_by_default() -> None:
    configure_logging(level="DEBUG", stream=io.StringIO(), force=True)
    assert logging.getLogger("telethon").level == logging.WARNING


def test_telethon_logger_honors_more_restrictive_app_level() -> None:
    configure_logging(level="ERROR", stream=io.StringIO(), force=True)
    # ERROR is quieter than the WARNING cap, so Telethon follows it.
    assert logging.getLogger("telethon").level == logging.ERROR


def test_telethon_level_override_reenables_debug() -> None:
    configure_logging(
        level="INFO", telethon_level="DEBUG", stream=io.StringIO(), force=True
    )
    assert logging.getLogger("telethon").level == logging.DEBUG


def test_bearer_token_is_redacted() -> None:
    record = _capture_log(
        lambda log: log.info(
            "auth", bearer_token="secret_token", authorization="Bearer s3cret"
        )
    )
    assert record["bearer_token"] == "***REDACTED***"
    assert record["authorization"] == "***REDACTED***"


def test_api_hash_is_redacted() -> None:
    record = _capture_log(
        lambda log: log.info("client_init", api_hash="abcdef1234567890")
    )
    assert record["api_hash"] == "***REDACTED***"


def test_invite_link_in_message_is_redacted() -> None:
    record = _capture_log(
        lambda log: log.info(
            "group_created",
            message="join here: https://t.me/+abcDEF12345",
            invite_link="https://t.me/joinchat/XYZ",
        )
    )
    assert "***REDACTED***" in record["message"]
    assert "abcDEF12345" not in record["message"]
    assert record["invite_link"] == "***REDACTED***"


def test_redact_secrets_handles_nested_mapping() -> None:
    out = redact_secrets(
        {
            "outer": {
                "bearer_token": "x",
                "items": [
                    {"invite_link": "https://t.me/+abc"},
                    {"public": "https://t.me/+xyz inside text"},
                ],
            },
        }
    )
    assert out["outer"]["bearer_token"] == "***REDACTED***"
    assert out["outer"]["items"][0]["invite_link"] == "***REDACTED***"
    assert "***REDACTED***" in out["outer"]["items"][1]["public"]


def test_redact_invite_links_leaves_public_handle_alone() -> None:
    assert redact_invite_links("ping @some_handle") == "ping @some_handle"
    assert redact_invite_links("see https://t.me/some_handle") == (
        "see https://t.me/some_handle"
    )
    assert "***REDACTED***" in redact_invite_links("https://t.me/+abc")
    assert "***REDACTED***" in redact_invite_links(
        "https://t.me/joinchat/abc_def-1"
    )


def test_bind_request_context_skips_unknown_and_none_fields() -> None:
    bind_request_context(operation_id="op-2", unknown_field="ignored", chat_name=None)
    record = _capture_log(lambda log: log.info("noop"))
    assert record["operation_id"] == "op-2"
    assert "unknown_field" not in record
    assert "chat_name" not in record
