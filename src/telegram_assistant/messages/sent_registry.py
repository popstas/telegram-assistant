"""Process-lifetime registry of messages this server process sent.

Backs session-limited delete (plan Tasks 6/7): when
``telegram.access.delete_only_session_messages`` is true the delete op refuses
to delete any ``(chat_id, message_id)`` not recorded here. The registry is
in-memory and process-global (one instance per server process, held on
``app.state`` and reachable from the MCP context) — it is cleared on restart,
which is the intended safety property: a fresh process can only delete what it
has newly sent.

Chat ids are canonicalised exactly like the access authorizer so a recorded
``-100``-marked id and the bare form refer to the same chat regardless of which
form the send/delete path supplies.
"""

from __future__ import annotations

import threading

from telegram_assistant.access.service import _canonical_chat_id


class SentMessageRegistry:
    """Thread/async-safe set of ``(chat_id, message_id)`` this process sent.

    ``record`` is best-effort — it never raises, so a registry hiccup can never
    fail an otherwise-successful send. ``contains`` answers the session-limit
    check for the delete op. Both canonicalise the chat id via
    :func:`telegram_assistant.access.service._canonical_chat_id` so marked and
    bare ids match the same entry.

    A plain :class:`threading.Lock` guards the set: the operations are tiny
    synchronous set mutations/lookups, safe to call from both async send paths
    and any background thread without blocking the event loop meaningfully.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sent: set[tuple[int, int]] = set()

    @staticmethod
    def _key(chat_id: int, message_id: int) -> tuple[int, int] | None:
        try:
            return _canonical_chat_id(int(chat_id)), int(message_id)
        except (TypeError, ValueError):
            return None

    def record(self, chat_id: int, message_id: int) -> None:
        """Record that this process sent ``message_id`` in ``chat_id``.

        Best-effort: invalid ids are silently ignored so this can be called in
        a send's success path without ever turning a delivered message into a
        failed operation.
        """
        key = self._key(chat_id, message_id)
        if key is None:
            return
        with self._lock:
            self._sent.add(key)

    def contains(self, chat_id: int, message_id: int) -> bool:
        """Return whether ``(chat_id, message_id)`` was recorded this process."""
        key = self._key(chat_id, message_id)
        if key is None:
            return False
        with self._lock:
            return key in self._sent

    def __len__(self) -> int:
        with self._lock:
            return len(self._sent)


__all__ = ["SentMessageRegistry"]
