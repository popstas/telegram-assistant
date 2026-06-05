"""Message-send domain shared by HTTP, CLI, and the worker."""

from telegram_assistant.messages.service import (
    MassSendItemResult,
    MassSendRequest,
    MassSendResult,
    MessageBackend,
    MessageSendFailed,
    MessageSendNeedsReview,
    MessageSendPending,
    SendMessageRequest,
    SendMessageResult,
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
    "MessageSendFailed",
    "MessageSendNeedsReview",
    "MessageSendPending",
    "SendMessageRequest",
    "SendMessageResult",
    "is_service_command",
    "mass_send_message",
    "redact_message_text",
    "send_message",
]
