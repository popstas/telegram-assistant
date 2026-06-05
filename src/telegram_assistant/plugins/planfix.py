"""Planfix integration plugin (optional, off by default).

Encapsulates every Planfix-specific behavior that used to live in the core
domain: the ``/task <id>`` service message sent into new groups and topics,
the ``@planfix_bot`` welcome/reply cleanup, and treating ``@planfix_bot`` as a
protected account for the member-removal ``--force`` guard. Activated by
``plugins.planfix.enabled`` in config.

The core imports nothing from this module — it is loaded lazily by
:func:`telegram_assistant.plugins.base.build_registry` only when enabled.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import Any

from telegram_assistant.config.models import PlanfixPluginConfig
from telegram_assistant.worker.queue import FloodWaitError

# How often to poll for the bot's reply during welcome/reply cleanup.
_POLL_INTERVAL = 1.0


class PlanfixPlugin:
    """Bundled plugin restoring the original Planfix-specific behaviors."""

    name = "planfix"

    def __init__(self, config: PlanfixPluginConfig) -> None:
        self._config = config

    def _bot_handle(self) -> str:
        return self._config.bot_username.lstrip("@").lower()

    def _has_ref(self, external_ref: int | str | None) -> bool:
        return external_ref is not None and bool(str(external_ref).strip())

    # -- Hook points ------------------------------------------------------

    def title_postfix(self) -> str:
        return self._config.group_title_postfix

    def protected_accounts(self) -> set[str]:
        return {self._config.bot_username}

    def topic_first_message(self, *, external_ref: int | str | None) -> str | None:
        if self._has_ref(external_ref):
            return f"/task {external_ref}"
        return None

    def group_first_message(
        self, *, external_ref: int | str | None, members_added: Sequence[str]
    ) -> str | None:
        if not self._has_ref(external_ref):
            return None
        bot = self._bot_handle()
        bot_in_group = any(u.lstrip("@").lower() == bot for u in members_added)
        if not bot_in_group:
            return None
        return f"/task {external_ref}"

    async def after_group_create(
        self,
        *,
        backend: Any,
        chat_id: int,
        external_ref: int | str | None,
        members_added: Sequence[str],
        skipped: list[dict[str, Any]],
    ) -> bool:
        text = self.group_first_message(
            external_ref=external_ref, members_added=members_added
        )
        if text is None:
            return False
        try:
            task_message_id = await backend.send_message(chat_id=chat_id, text=text)
        except Exception as exc:  # best-effort: chat already exists
            skipped.append({"step": "task_message", "reason": str(exc)})
            return False

        # Best-effort cleanup of the chat's service noise: the bot's welcome
        # message, our `/task` command, and the bot's reply to it. Only runs
        # when enabled. Any failure is recorded in `skipped` and never fails
        # the create — the chat already exists and this is purely cosmetic.
        if self._config.cleanup_messages and task_message_id is not None:
            await self._cleanup_messages(
                backend=backend,
                chat_id=chat_id,
                task_message_id=task_message_id,
                wait_seconds=self._config.task_reply_wait_seconds,
                skipped=skipped,
            )
        return True

    # -- Cleanup ----------------------------------------------------------

    async def _cleanup_messages(
        self,
        *,
        backend: Any,
        chat_id: int,
        task_message_id: int,
        wait_seconds: int,
        skipped: list[dict[str, Any]],
    ) -> None:
        """Delete the bot's welcome message, our `/task` command, and the
        bot's reply to it.

        Polls ``get_recent_messages`` up to ``wait_seconds`` for the bot reply
        (a bot message replying to ``task_message_id``). Whether or not the
        reply arrives, the welcome message and the command are still deleted;
        a missing reply is recorded in ``skipped`` rather than failing the
        create. Every backend error here (including a throttle) is best-effort.
        """
        bot_handle = self._bot_handle()

        def _is_bot(msg: dict[str, Any]) -> bool:
            sender = str(msg.get("sender_username") or "").lstrip("@").lower()
            return sender == bot_handle

        messages: list[dict[str, Any]] = []
        reply_id: int | None = None
        elapsed = 0.0
        while True:
            try:
                messages = list(
                    await backend.get_recent_messages(chat_id=chat_id, limit=20)
                )
            except FloodWaitError as exc:
                skipped.append({"step": "cleanup_get_messages", "reason": str(exc)})
                return
            except Exception as exc:
                skipped.append({"step": "cleanup_get_messages", "reason": str(exc)})
                return
            reply = next(
                (
                    m
                    for m in messages
                    if _is_bot(m) and m.get("reply_to_msg_id") == task_message_id
                ),
                None,
            )
            if reply is not None:
                reply_id = int(reply["id"])
                break
            if elapsed >= wait_seconds:
                break
            await asyncio.sleep(_POLL_INTERVAL)
            elapsed += _POLL_INTERVAL

        if reply_id is None:
            skipped.append(
                {
                    "step": "cleanup_bot_reply",
                    "reason": "bot reply did not arrive within wait window",
                }
            )

        # The welcome message is any bot message that is not the reply we just
        # matched. Collect those, then the command, then the reply — deduped.
        to_delete: list[int] = [
            int(m["id"]) for m in messages if _is_bot(m) and int(m["id"]) != reply_id
        ]
        to_delete.append(task_message_id)
        if reply_id is not None:
            to_delete.append(reply_id)

        seen: set[int] = set()
        unique: list[int] = []
        for mid in to_delete:
            if mid in seen:
                continue
            seen.add(mid)
            unique.append(mid)

        try:
            await backend.delete_messages(chat_id=chat_id, message_ids=unique)
        except Exception as exc:
            skipped.append({"step": "cleanup_delete", "reason": str(exc)})
