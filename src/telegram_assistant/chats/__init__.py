"""Chat-wide operations: metadata inspection (read) and auto-delete TTL (write)."""

from telegram_assistant.chats.service import (
    CHAT_KINDS,
    ChatInfo,
    ChatInspectBackend,
    inspect_chat,
)
from telegram_assistant.chats.ttl import (
    MAX_TTL_SECONDS,
    ChatTtlBackend,
    SetTtlRequest,
    SetTtlResult,
    parse_ttl,
    set_chat_ttl,
)

__all__ = [
    "CHAT_KINDS",
    "MAX_TTL_SECONDS",
    "ChatInfo",
    "ChatInspectBackend",
    "ChatTtlBackend",
    "SetTtlRequest",
    "SetTtlResult",
    "inspect_chat",
    "parse_ttl",
    "set_chat_ttl",
]
