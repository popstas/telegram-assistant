"""Config-driven read/write access control for the technical account."""

from telegram_assistant.access.service import (
    AccessDenied,
    AccessLevel,
    Authorizer,
)

__all__ = [
    "AccessDenied",
    "AccessLevel",
    "Authorizer",
]
