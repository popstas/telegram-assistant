"""Helpers for translating Telethon RPC errors to the queue's signal types.

Telethon raises its own ``FloodWaitError`` from ``telethon.errors``; the worker
queue only pauses-and-retries when it sees the project's own
``worker.queue.FloodWaitError``. Backends that talk to Telethon must therefore
translate the upstream exception so the queue can react instead of marking the
operation as a generic failure.
"""

from __future__ import annotations

from telegram_assistant.worker.queue import FloodWaitError


def translate_flood_wait(exc: BaseException) -> BaseException:
    """Return our ``FloodWaitError`` if *exc* is a Telethon ``FloodWaitError``.

    Match by class name rather than ``isinstance`` so we don't have to import
    Telethon at module load time. Telethon's exception carries a ``seconds``
    attribute holding the wait window.
    """
    if type(exc).__name__ == "FloodWaitError":
        seconds = getattr(exc, "seconds", 0) or 0
        return FloodWaitError(float(seconds))
    return exc


__all__ = ["translate_flood_wait"]
