"""Notification mute/unmute domain and adapters."""

from telegram_assistant.notifications.service import (
    MuteRequest,
    MuteResult,
    NotificationBackend,
    mute_chat,
    unmute_chat,
)
from telegram_assistant.notifications.telethon_backend import (
    FOREVER_MUTE_UNTIL,
    TelethonNotificationBackend,
)

__all__ = [
    "FOREVER_MUTE_UNTIL",
    "MuteRequest",
    "MuteResult",
    "NotificationBackend",
    "TelethonNotificationBackend",
    "mute_chat",
    "unmute_chat",
]
