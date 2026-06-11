"""Telethon-backed :class:`GroupBackend` implementation.

Kept separate from :mod:`service` so the domain layer stays free of Telethon
imports. Each method translates a domain verb (``create_supergroup``,
``add_member``, ``promote_admin``, ``create_invite_link``, ``send_message``)
into the corresponding Telethon call.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from telegram_assistant.telegram_client.errors import translate_flood_wait


class TelethonGroupBackend:
    """Adapter from the Telethon ``TelegramClient`` to :class:`GroupBackend`."""

    def __init__(self, client: Any) -> None:
        self._client = client

    async def create_supergroup(
        self,
        *,
        title: str,
        about: str | None,
        enable_topics: bool,
    ) -> int:
        from telethon.tl.functions.channels import (
            CreateChannelRequest,
            ToggleForumRequest,
        )

        try:
            result = await self._client(
                CreateChannelRequest(
                    title=title,
                    about=about or "",
                    megagroup=True,
                )
            )
        except Exception as exc:
            raise translate_flood_wait(exc) from exc
        # CreateChannelRequest returns Updates; the new channel sits in `.chats`.
        chats = getattr(result, "chats", None) or []
        if not chats:
            raise RuntimeError(
                "Telegram did not return a chat for the created supergroup"
            )
        channel = chats[0]
        chat_id = int(getattr(channel, "id", 0))
        if enable_topics:
            # Telethon 1.43 added the `tabs` argument to ToggleForumRequest.
            # Older Telethon versions reject the kwarg; try the new signature
            # first and fall back for callers stuck on the legacy build.
            try:
                await self._client(
                    ToggleForumRequest(channel=channel, enabled=True, tabs=True)
                )
            except TypeError:
                try:
                    await self._client(
                        ToggleForumRequest(channel=channel, enabled=True)
                    )
                except Exception as exc:
                    raise translate_flood_wait(exc) from exc
            except Exception as exc:
                raise translate_flood_wait(exc) from exc
        return chat_id

    async def add_member(self, *, chat_id: int, user: str) -> None:
        from telethon.tl.functions.channels import InviteToChannelRequest

        try:
            channel = await self._client.get_input_entity(chat_id)
            member = await self._client.get_input_entity(user)
            await self._client(
                InviteToChannelRequest(channel=channel, users=[member])
            )
        except Exception as exc:
            raise translate_flood_wait(exc) from exc

    async def promote_admin(self, *, chat_id: int, user: str) -> None:
        from telethon.tl.functions.channels import EditAdminRequest
        from telethon.tl.types import ChatAdminRights

        try:
            channel = await self._client.get_input_entity(chat_id)
            member = await self._client.get_input_entity(user)
        except Exception as exc:
            raise translate_flood_wait(exc) from exc
        rights = ChatAdminRights(
            change_info=True,
            post_messages=False,
            edit_messages=False,
            delete_messages=True,
            ban_users=True,
            invite_users=True,
            pin_messages=True,
            add_admins=False,
            anonymous=False,
            manage_call=True,
            other=True,
            manage_topics=True,
        )
        try:
            await self._client(
                EditAdminRequest(
                    channel=channel,
                    user_id=member,
                    admin_rights=rights,
                    rank="admin",
                )
            )
        except Exception as exc:
            raise translate_flood_wait(exc) from exc

    async def create_invite_link(self, *, chat_id: int) -> str:
        from telethon.tl.functions.messages import ExportChatInviteRequest

        try:
            channel = await self._client.get_input_entity(chat_id)
            result = await self._client(ExportChatInviteRequest(peer=channel))
        except Exception as exc:
            raise translate_flood_wait(exc) from exc
        link = getattr(result, "link", None)
        if not link:
            raise RuntimeError(
                f"Telegram did not return an invite link for chat {chat_id}"
            )
        return str(link)

    async def send_message(self, *, chat_id: int, text: str) -> int:
        try:
            sent = await self._client.send_message(chat_id, text)
        except Exception as exc:
            raise translate_flood_wait(exc) from exc
        message_id = getattr(sent, "id", 0)
        return int(message_id)

    async def set_topics_layout(self, *, chat_id: int, tabs: bool) -> None:
        from telethon.tl.functions.channels import ToggleForumRequest

        try:
            channel = await self._client.get_input_entity(chat_id)
        except Exception as exc:
            raise translate_flood_wait(exc) from exc
        try:
            await self._client(
                ToggleForumRequest(channel=channel, enabled=True, tabs=tabs)
            )
        except TypeError:
            if tabs:
                try:
                    await self._client(
                        ToggleForumRequest(channel=channel, enabled=True)
                    )
                except Exception as exc:
                    raise translate_flood_wait(exc) from exc
                return
            raise RuntimeError(
                "Telethon build does not support the `tabs` argument to "
                "ToggleForumRequest; cannot switch a forum to list layout"
            ) from None
        except Exception as exc:
            raise translate_flood_wait(exc) from exc

    async def set_title(self, *, chat_id: int, title: str) -> None:
        from telethon.tl.functions.channels import EditTitleRequest

        try:
            channel = await self._client.get_input_entity(chat_id)
            await self._client(EditTitleRequest(channel=channel, title=title))
        except Exception as exc:
            raise translate_flood_wait(exc) from exc

    async def set_default_permissions(
        self,
        *,
        chat_id: int,
        allow_create_topics: bool,
        allow_pin_messages: bool,
    ) -> None:
        from telethon.tl.functions.messages import (
            EditChatDefaultBannedRightsRequest,
        )
        from telethon.tl.types import ChatBannedRights

        try:
            channel = await self._client.get_input_entity(chat_id)
        except Exception as exc:
            raise translate_flood_wait(exc) from exc
        # An allowed action maps to a *cleared* banned-rights flag. We only
        # touch the two flags we manage and leave every other default right at
        # its standard (unbanned) value.
        rights = ChatBannedRights(
            until_date=None,
            manage_topics=not allow_create_topics,
            pin_messages=not allow_pin_messages,
        )
        try:
            await self._client(
                EditChatDefaultBannedRightsRequest(peer=channel, banned_rights=rights)
            )
        except Exception as exc:
            raise translate_flood_wait(exc) from exc

    async def get_recent_messages(
        self, *, chat_id: int, limit: int
    ) -> list[dict[str, Any]]:
        try:
            channel = await self._client.get_input_entity(chat_id)
            out: list[dict[str, Any]] = []
            async for msg in self._client.iter_messages(channel, limit=limit):
                sender = getattr(msg, "sender", None)
                username = (
                    getattr(sender, "username", None) if sender is not None else None
                )
                reply_to = getattr(msg, "reply_to_msg_id", None)
                out.append(
                    {
                        "id": int(getattr(msg, "id", 0)),
                        "sender_username": username,
                        "reply_to_msg_id": int(reply_to) if reply_to else None,
                        "text": getattr(msg, "message", "") or "",
                    }
                )
        except Exception as exc:
            raise translate_flood_wait(exc) from exc
        return out

    async def delete_messages(
        self, *, chat_id: int, message_ids: Sequence[int]
    ) -> None:
        ids = [int(m) for m in message_ids]
        if not ids:
            return
        try:
            channel = await self._client.get_input_entity(chat_id)
            await self._client.delete_messages(channel, ids)
        except Exception as exc:
            raise translate_flood_wait(exc) from exc

    async def chat_exists(self, *, chat_id: int) -> bool:
        """Return whether ``chat_id`` still resolves to a live channel.

        Used to guard the group_create replay path: a chat deleted out-of-band
        must be re-created, not replayed. A FLOOD_WAIT is re-raised (translated)
        so the caller can promote it to ``needs_review``; a "not found"-style
        error resolves to ``False``. Any other error propagates unchanged.
        """
        from telethon.errors import (
            ChannelInvalidError,
            ChannelPrivateError,
            PeerIdInvalidError,
        )
        from telethon.tl.functions.channels import GetFullChannelRequest

        try:
            channel = await self._client.get_input_entity(chat_id)
            await self._client(GetFullChannelRequest(channel=channel))
        except Exception as exc:
            translated = translate_flood_wait(exc)
            if translated is not exc:
                # A FLOOD_WAIT was translated to our queue signal — re-raise it.
                raise translated from exc
            # The chat is gone when Telegram reports the channel/peer is
            # invalid, private, or unknown, or when entity resolution can't
            # find it (``get_input_entity`` raises ``ValueError``). Match the
            # concrete exception types rather than sniffing message strings,
            # which drift across Telethon versions and locales. Anything else
            # is unexpected and propagates unchanged.
            if isinstance(
                exc,
                (
                    ChannelInvalidError,
                    ChannelPrivateError,
                    PeerIdInvalidError,
                    ValueError,
                ),
            ):
                return False
            raise
        return True

    async def get_topics_layout(self, *, chat_id: int) -> bool:
        from telethon.tl.functions.channels import GetFullChannelRequest

        try:
            channel = await self._client.get_input_entity(chat_id)
            full = await self._client(GetFullChannelRequest(channel=channel))
        except Exception as exc:
            raise translate_flood_wait(exc) from exc
        # `forum_tabs` is a flag on the Channel constructor (flags2.19), not
        # on ChannelFull. Telegram returns the matching Channel in
        # `messages.ChatFull.chats`; resolve it by id from `full_chat.id` and
        # fall back to the first chat if Telegram only returned one.
        chats = list(getattr(full, "chats", None) or [])
        full_chat = getattr(full, "full_chat", None)
        target_id = int(getattr(full_chat, "id", 0)) if full_chat is not None else 0
        target = next(
            (c for c in chats if int(getattr(c, "id", 0)) == target_id),
            None,
        )
        if target is None and chats:
            target = chats[0]
        return bool(getattr(target, "forum_tabs", False))
