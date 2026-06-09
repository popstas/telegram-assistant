"""Planfix integration plugin (optional, off by default).

Encapsulates every Planfix-specific behavior that used to live in the core
domain: the ``/task <id>`` service message sent into new groups and topics,
the ``@planfix_bot`` welcome/reply cleanup, and treating ``@planfix_bot`` as a
protected account for the member-removal ``--force`` guard. Activated by
``plugins.planfix.enabled`` in config.

For topics the surviving first message is the topic name (sent by the core);
this plugin then posts ``/task <id>`` as a second message and, when
``cleanup_messages`` is on, deletes that command together with the bot's
welcome/reply — scoped to the topic — so only the topic-name message remains.

The core imports nothing from this module — it is loaded lazily by
:func:`telegram_assistant.plugins.base.build_registry` only when enabled.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import Any

from telegram_assistant.config.models import PlanfixPluginConfig
from telegram_assistant.observability.logging import get_logger
from telegram_assistant.worker.queue import FloodWaitError

logger = get_logger(__name__)

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
        logger.debug(
            "planfix after_group_create",
            chat_id=chat_id,
            external_ref=external_ref,
            members_added=list(members_added),
            bot_handle=self._bot_handle(),
            task_text=text,
            cleanup_messages=self._config.cleanup_messages,
        )
        if text is None:
            return False
        try:
            task_message_id = await backend.send_message(chat_id=chat_id, text=text)
        except Exception as exc:  # best-effort: chat already exists
            skipped.append({"step": "task_message", "reason": str(exc)})
            logger.debug(
                "planfix task message send failed",
                chat_id=chat_id,
                reason=str(exc),
            )
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

    async def after_topic_create(
        self,
        *,
        backend: Any,
        chat_id: int,
        topic_id: int,
        external_ref: int | str | None,
        skipped: list[dict[str, Any]],
    ) -> bool:
        """Post ``/task <ref>`` into a new topic and clean it up.

        The core has already sent the topic name as the surviving first
        message. Here we send ``/task <ref>`` as a second message and, when
        ``cleanup_messages`` is on, delete that command together with the bot's
        welcome/reply — all scoped to ``topic_id``. Best-effort: every failure
        lands in ``skipped`` and never raises (the topic already exists).
        """
        text = self.topic_first_message(external_ref=external_ref)
        logger.debug(
            "planfix after_topic_create",
            chat_id=chat_id,
            topic_id=topic_id,
            external_ref=external_ref,
            task_text=text,
            cleanup_messages=self._config.cleanup_messages,
        )
        if text is None:
            return False
        try:
            task_message_id = await backend.send_message(
                chat_id=chat_id, text=text, topic_id=topic_id
            )
        except Exception as exc:  # best-effort: topic already exists
            skipped.append({"step": "task_message", "reason": str(exc)})
            logger.debug(
                "planfix topic task message send failed",
                chat_id=chat_id,
                topic_id=topic_id,
                reason=str(exc),
            )
            return False

        if self._config.cleanup_messages and task_message_id is not None:
            await self._cleanup_messages(
                backend=backend,
                chat_id=chat_id,
                task_message_id=task_message_id,
                wait_seconds=self._config.task_reply_wait_seconds,
                skipped=skipped,
                topic_id=topic_id,
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
        topic_id: int | None = None,
    ) -> None:
        """Delete the bot's welcome message, our `/task` command, and the
        bot's reply to it.

        Polls ``get_recent_messages`` up to ``wait_seconds`` for the bot reply
        (a bot message replying to ``task_message_id``). Whether or not the
        reply arrives, the welcome message and the command are still deleted;
        a missing reply is recorded in ``skipped`` rather than failing the
        create. Every backend error here (including a throttle) is best-effort.

        When ``topic_id`` is set the message scan is scoped to that forum topic
        (the group backend has no ``topic_id`` parameter, so it is only passed
        through when present).
        """
        bot_handle = self._bot_handle()

        def _is_bot(msg: dict[str, Any]) -> bool:
            sender = str(msg.get("sender_username") or "").lstrip("@").lower()
            return sender == bot_handle

        get_kwargs: dict[str, Any] = {"chat_id": chat_id, "limit": 20}
        if topic_id is not None:
            get_kwargs["topic_id"] = topic_id

        messages: list[dict[str, Any]] = []
        reply_id: int | None = None
        elapsed = 0.0
        while True:
            try:
                messages = list(await backend.get_recent_messages(**get_kwargs))
            except FloodWaitError as exc:
                skipped.append({"step": "cleanup_get_messages", "reason": str(exc)})
                logger.debug(
                    "planfix cleanup get_messages flood_wait",
                    chat_id=chat_id,
                    topic_id=topic_id,
                    reason=str(exc),
                )
                return
            except Exception as exc:
                skipped.append({"step": "cleanup_get_messages", "reason": str(exc)})
                logger.debug(
                    "planfix cleanup get_messages failed",
                    chat_id=chat_id,
                    topic_id=topic_id,
                    reason=str(exc),
                )
                return
            logger.debug(
                "planfix cleanup fetched messages",
                chat_id=chat_id,
                topic_id=topic_id,
                task_message_id=task_message_id,
                count=len(messages),
                messages=[
                    {
                        "id": m.get("id"),
                        "sender": m.get("sender_username"),
                        "reply_to": m.get("reply_to_msg_id"),
                    }
                    for m in messages
                ],
            )
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

        logger.debug(
            "planfix cleanup deleting",
            chat_id=chat_id,
            topic_id=topic_id,
            task_message_id=task_message_id,
            reply_id=reply_id,
            delete_ids=unique,
        )
        try:
            await backend.delete_messages(chat_id=chat_id, message_ids=unique)
        except Exception as exc:
            skipped.append({"step": "cleanup_delete", "reason": str(exc)})
            logger.debug(
                "planfix cleanup delete failed",
                chat_id=chat_id,
                topic_id=topic_id,
                delete_ids=unique,
                reason=str(exc),
            )
            return
        logger.debug(
            "planfix cleanup deleted",
            chat_id=chat_id,
            topic_id=topic_id,
            delete_ids=unique,
        )
