"""Notification-settings (mute/unmute) domain shared by HTTP, CLI, and the worker."""

from telegram_assistant.notifications.service import (
    MuteRequest,
    MuteResult,
    NotificationBackend,
    mute_chat,
    unmute_chat,
)
from telegram_assistant.notifications.telethon_backend import (
    TelethonNotificationBackend,
)

__all__ = [
    "MuteRequest",
    "MuteResult",
    "NotificationBackend",
    "TelethonNotificationBackend",
    "mute_chat",
    "unmute_chat",
]
