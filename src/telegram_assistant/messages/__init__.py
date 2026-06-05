"""Message-send domain shared by HTTP, CLI, and the worker."""

from telegram_assistant.messages.service import (
    MassSendItemResult,
    MassSendRequest,
    MassSendResult,
    MessageBackend,
    MessageReadBackend,
    MessageSendFailed,
    MessageSendNeedsReview,
    MessageSendPending,
    RecentMessage,
    SendMessageRequest,
    SendMessageResult,
    get_recent_messages,
    is_service_command,
    mass_send_message,
    redact_message_text,
    send_message,
)

__all__ = [
    "MassSendItemResult",
    "MassSendRequest",
    "MassSendResult",
    "MessageBackend",
    "MessageReadBackend",
    "MessageSendFailed",
    "MessageSendNeedsReview",
    "MessageSendPending",
    "RecentMessage",
    "SendMessageRequest",
    "SendMessageResult",
    "get_recent_messages",
    "is_service_command",
    "mass_send_message",
    "redact_message_text",
    "send_message",
]
