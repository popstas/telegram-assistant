"""Shared Telethon client wrapper.

Exposes a single async Telethon client that is shared by the HTTP API, the
CLI, and the background worker. Loading is lazy: callers obtain the client
through :func:`get_client`, which constructs (and connects) it on first use
and reuses it on subsequent calls within the same event loop.
"""

from __future__ import annotations

from telegram_assistant.telegram_client.session import (
    SessionState,
    TelethonSessionManager,
    get_client,
    interactive_login,
    reset_client,
    session_state,
)

__all__ = [
    "SessionState",
    "TelethonSessionManager",
    "get_client",
    "interactive_login",
    "reset_client",
    "session_state",
]
