"""Config-driven read/write access gate enforced in the domain layer.

There is a single identity here — one technical Telethon account plus one HTTP
bearer token — so access control scopes *which chats/folders this instance may
touch* (read vs write), not per-caller identity. The policy lives in
:class:`telegram_assistant.config.models.AccessConfig`:

* When ``telegram.access`` is ``None`` the authorizer is a no-op sentinel —
  every ``require`` returns immediately (allow-all, backward compatible).
* When an ``access`` block is present it is deny-by-default. The effective
  level for a chat is the **union, highest level wins** across every matching
  rule: a wildcard ``all`` rule, any folder the chat belongs to, and an
  explicit ``chat`` rule. ``write`` implies ``read`` (``WRITE > READ``).

So one config can simultaneously express a read-all baseline (wildcard ``all``
+ ``permission: read``) layered with targeted ``write`` rules on selected
chats/folders, without conflict.

Following the service/backend split, the authorizer depends only on protocols
(an :class:`EntityResolver` to resolve ``chat`` rule refs and a
``FolderBackend`` to discover folder memberships); tests inject fakes.
"""

from __future__ import annotations

import enum
from typing import TYPE_CHECKING

from telegram_assistant.config.models import AccessConfig

if TYPE_CHECKING:  # pragma: no cover - typing only, avoids import cycles
    from collections.abc import Iterable

    from telegram_assistant.entities.service import EntityResolver
    from telegram_assistant.folders.service import FolderBackend


class AccessLevel(enum.IntEnum):
    """Ordered access levels. ``WRITE`` implies ``READ`` (``WRITE > READ``)."""

    READ = 1
    WRITE = 2


_PERMISSION_TO_LEVEL = {
    "read": AccessLevel.READ,
    "write": AccessLevel.WRITE,
}


class AccessDenied(RuntimeError):
    """The configured policy does not grant the required level for a chat/folder.

    Deny is loud: this surfaces as HTTP 403 / a non-zero CLI exit / a structured
    log line. ``matched_rule`` is a short human description of what (if anything)
    granted access, used by observability.
    """

    def __init__(
        self,
        *,
        chat_ref: object,
        required_level: AccessLevel,
        granted_level: AccessLevel | None = None,
        matched_rule: str | None = None,
    ) -> None:
        granted_text = granted_level.name.lower() if granted_level else "none"
        super().__init__(
            f"access denied for {chat_ref!r}: requires "
            f"{required_level.name.lower()}, granted {granted_text}"
        )
        self.chat_ref = chat_ref
        self.required_level = required_level
        self.granted_level = granted_level
        self.matched_rule = matched_rule


class Authorizer:
    """Per-request access gate built from an :class:`AccessConfig`.

    Construct with ``config=None`` for the allow-all no-op sentinel. Otherwise
    pass the :class:`AccessConfig` plus the dependencies needed to resolve the
    rules: an :class:`EntityResolver` (to turn ``chat`` rule refs into numeric
    ids) and a ``FolderBackend`` (to discover which folders a chat belongs to).
    The rule index and the folder-membership map are built lazily and cached for
    the lifetime of the authorizer (i.e. one request).
    """

    def __init__(
        self,
        config: AccessConfig | None,
        *,
        resolver: EntityResolver | None = None,
        folder_backend: FolderBackend | None = None,
    ) -> None:
        self._config = config
        self._resolver = resolver
        self._folder_backend = folder_backend
        self._built = False
        self._default_level: AccessLevel | None = None
        self._chat_levels: dict[int, AccessLevel] = {}
        self._folder_levels: dict[str, AccessLevel] = {}
        self._memberships: dict[int, set[str]] | None = None

    @property
    def enabled(self) -> bool:
        """Whether a policy is active (``False`` ⇒ allow-all no-op)."""
        return self._config is not None

    async def _ensure_index(self) -> None:
        if self._built or self._config is None:
            return
        default: AccessLevel | None = None
        chat_levels: dict[int, AccessLevel] = {}
        folder_levels: dict[str, AccessLevel] = {}
        for rule in self._config.rules:
            level = _PERMISSION_TO_LEVEL[rule.permission]
            if rule.all:
                default = level if default is None else max(default, level)
            elif rule.folder is not None:
                cur = folder_levels.get(rule.folder)
                folder_levels[rule.folder] = level if cur is None else max(cur, level)
            elif rule.chat is not None:
                if self._resolver is None:
                    raise RuntimeError(
                        "authorizer requires an entity resolver to resolve "
                        "chat-targeted access rules"
                    )
                resolved = await self._resolver.resolve(rule.chat)
                cur = chat_levels.get(resolved.chat_id)
                chat_levels[resolved.chat_id] = (
                    level if cur is None else max(cur, level)
                )
        self._default_level = default
        self._chat_levels = chat_levels
        self._folder_levels = folder_levels
        self._built = True

    async def _folder_memberships(self, chat_id: int) -> set[str]:
        # No folder rules → folder membership is irrelevant; skip the scan.
        if not self._folder_levels:
            return set()
        if self._memberships is None:
            memberships: dict[int, set[str]] = {}
            if self._folder_backend is not None:
                for snapshot in await self._folder_backend.list_folders():
                    for chat in snapshot.chats:
                        memberships.setdefault(chat.chat_id, set()).add(
                            snapshot.folder_name
                        )
            self._memberships = memberships
        return self._memberships.get(chat_id, set())

    def _effective_chat_level(
        self, chat_id: int, memberships: Iterable[str]
    ) -> tuple[AccessLevel | None, str | None]:
        """Return ``(granted_level, matched_rule_description)`` for a chat."""
        best: AccessLevel | None = None
        matched: str | None = None
        if self._default_level is not None:
            best = self._default_level
            matched = "all"
        for folder in memberships:
            lv = self._folder_levels.get(folder)
            if lv is not None and (best is None or lv > best):
                best = lv
                matched = f"folder:{folder}"
        chat_level = self._chat_levels.get(chat_id)
        if chat_level is not None and (best is None or chat_level > best):
            best = chat_level
            matched = "chat"
        return best, matched

    async def require(
        self,
        chat_id: int,
        level: AccessLevel,
        *,
        folder_memberships: Iterable[str] | None = None,
    ) -> None:
        """Raise :class:`AccessDenied` unless ``chat_id`` is granted ``level``.

        ``folder_memberships`` may be supplied by the caller (the folder names
        the chat belongs to); when omitted it is discovered lazily via the
        folder backend. With no active policy this is a no-op.
        """
        if self._config is None:
            return
        await self._ensure_index()
        if folder_memberships is None:
            memberships: Iterable[str] = await self._folder_memberships(chat_id)
        else:
            memberships = set(folder_memberships)
        granted, matched = self._effective_chat_level(chat_id, memberships)
        if granted is None or granted < level:
            raise AccessDenied(
                chat_ref=chat_id,
                required_level=level,
                granted_level=granted,
                matched_rule=matched,
            )

    async def require_folder(self, folder_name: str, level: AccessLevel) -> None:
        """Raise :class:`AccessDenied` unless ``folder_name`` is granted ``level``.

        Used for destination-folder gating (e.g. group create). The effective
        level is the max of the wildcard default and any folder rule. With no
        active policy this is a no-op.
        """
        if self._config is None:
            return
        await self._ensure_index()
        best: AccessLevel | None = None
        matched: str | None = None
        if self._default_level is not None:
            best = self._default_level
            matched = "all"
        folder_level = self._folder_levels.get(folder_name)
        if folder_level is not None and (best is None or folder_level > best):
            best = folder_level
            matched = f"folder:{folder_name}"
        if best is None or best < level:
            raise AccessDenied(
                chat_ref=f"folder:{folder_name}",
                required_level=level,
                granted_level=best,
                matched_rule=matched,
            )


__all__ = [
    "AccessDenied",
    "AccessLevel",
    "Authorizer",
]
