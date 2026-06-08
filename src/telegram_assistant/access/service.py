"""Config-driven read/write access gate enforced in the domain layer.

There is a single identity here — one technical Telethon account plus one HTTP
bearer token — so access control scopes *which chats/folders this instance may
touch* (read vs write), not per-caller identity. The policy lives in
:class:`telegram_assistant.config.models.AccessConfig`:

* When ``telegram.access`` is ``None`` the authorizer is a no-op sentinel —
  every ``require`` returns immediately (allow-all, backward compatible).
* When an ``access`` block is present it is deny-by-default. The effective
  *capability set* for a chat is the **union** of capabilities across every
  matching rule: a wildcard ``all`` rule, any folder the chat belongs to, and an
  explicit ``chat`` rule. Capabilities are **independent** — ``read``, ``write``
  and ``delete`` each grant *only* themselves; ``write`` does **not** imply
  ``read``. A chat that should be both readable and writable must be granted
  both permissions explicitly.

So one config can simultaneously express a read-all baseline (wildcard ``all``
+ ``permission: read``) layered with targeted ``write`` rules on selected
chats/folders; a chat covered by both ends up with ``{read, write}``.

Following the service/backend split, the authorizer depends only on protocols
(an :class:`EntityResolver` to resolve ``chat`` rule refs and a
``FolderBackend`` to discover folder memberships); tests inject fakes.
"""

from __future__ import annotations

import enum
from typing import TYPE_CHECKING

from telegram_assistant.config.models import AccessConfig
from telegram_assistant.observability.logging import get_logger

if TYPE_CHECKING:  # pragma: no cover - typing only, avoids import cycles
    from collections.abc import Iterable

    from telegram_assistant.entities.service import EntityResolver
    from telegram_assistant.folders.service import FolderBackend

_log = get_logger(__name__)


class AccessLevel(enum.IntEnum):
    """Independent access capabilities.

    These are **not** ordered semantically — each names a distinct capability
    that grants only itself (``write`` does *not* imply ``read``). The integer
    values exist only to give the members a stable, deterministic sort/display
    order (e.g. when summarising a granted capability set).
    """

    READ = 1
    WRITE = 2
    DELETE = 3


_PERMISSION_TO_LEVEL = {
    "read": AccessLevel.READ,
    "write": AccessLevel.WRITE,
    "delete": AccessLevel.DELETE,
}


def _caps_repr(caps: set[AccessLevel]) -> str | None:
    """A single representative capability name for a granted set (for logging).

    Returns the highest-valued granted capability's lowercase name, or ``None``
    when nothing is granted. Mirrors :attr:`AccessDenied.granted_level`.
    """
    return max(caps).name.lower() if caps else None


def _canonical_chat_id(chat_id: int) -> int:
    """Reduce a chat id to the bare form the rule index is keyed on.

    ``chat`` rule refs are resolved through ``EntityRef.numeric_id``, which
    strips the ``-100`` channel marker, so the index is keyed by bare ids. A
    request that carries the *marked* form (``-1001234567890``) must be reduced
    to the same bare id (``1234567890``) before lookup or it would never match
    an otherwise-granting ``chat`` rule — denying a permitted chat. Mirrors
    ``EntityRef.numeric_id`` so both sides normalise identically.
    """
    text = str(chat_id)
    if text.startswith("-100"):
        bare = text[4:]
        if bare.isdigit():
            return int(bare)
    return abs(chat_id)


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
        granted_caps: Iterable[AccessLevel] | None = None,
        matched_rule: str | None = None,
    ) -> None:
        caps = frozenset(granted_caps or ())
        granted_text = (
            ",".join(c.name.lower() for c in sorted(caps)) if caps else "none"
        )
        super().__init__(
            f"access denied for {chat_ref!r}: requires "
            f"{required_level.name.lower()}, granted {granted_text}"
        )
        self.chat_ref = chat_ref
        self.required_level = required_level
        self.granted_caps: frozenset[AccessLevel] = caps
        self.matched_rule = matched_rule

    @property
    def granted_level(self) -> AccessLevel | None:
        """A single representative of the granted caps for display/logging.

        The capability set is independent (no ordering semantics), but observers
        and the HTTP error body expect one value; return the highest-valued
        granted capability, or ``None`` when nothing was granted.
        """
        return max(self.granted_caps) if self.granted_caps else None


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
        self._default_caps: set[AccessLevel] = set()
        self._chat_caps: dict[int, set[AccessLevel]] = {}
        self._folder_caps: dict[str, set[AccessLevel]] = {}
        self._memberships: dict[int, set[str]] | None = None

    @property
    def enabled(self) -> bool:
        """Whether a policy is active (``False`` ⇒ allow-all no-op)."""
        return self._config is not None

    async def _ensure_index(self) -> None:
        if self._built or self._config is None:
            return
        default_caps: set[AccessLevel] = set()
        chat_caps: dict[int, set[AccessLevel]] = {}
        folder_caps: dict[str, set[AccessLevel]] = {}
        for rule in self._config.rules:
            levels = {
                _PERMISSION_TO_LEVEL[perm] for perm in rule.effective_permissions
            }
            if rule.all:
                default_caps |= levels
            elif rule.folder is not None:
                folder_caps.setdefault(rule.folder, set()).update(levels)
            else:
                refs = rule.chat_refs
                if refs and self._resolver is None:
                    raise RuntimeError(
                        "authorizer requires an entity resolver to resolve "
                        "chat-targeted access rules"
                    )
                for ref in refs:
                    resolved = await self._resolver.resolve(ref)
                    chat_caps.setdefault(resolved.chat_id, set()).update(levels)
        self._default_caps = default_caps
        self._chat_caps = chat_caps
        self._folder_caps = folder_caps
        self._built = True

    async def _folder_memberships(self, chat_id: int) -> set[str]:
        # No folder rules → folder membership is irrelevant; skip the scan.
        if not self._folder_caps:
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

    def _effective_chat_caps(
        self, chat_id: int, memberships: Iterable[str]
    ) -> tuple[set[AccessLevel], str | None]:
        """Return ``(granted_caps, matched_rule_description)`` for a chat.

        Capabilities **union** across every matching rule. ``matched`` names the
        most specific rule kind that contributed (``chat`` > ``folder`` >
        ``all``), for observability.
        """
        caps: set[AccessLevel] = set()
        matched: str | None = None
        if self._default_caps:
            caps |= self._default_caps
            matched = "all"
        for folder in memberships:
            folder_caps = self._folder_caps.get(folder)
            if folder_caps:
                caps |= folder_caps
                matched = f"folder:{folder}"
        chat_caps = self._chat_caps.get(chat_id)
        if chat_caps:
            caps |= chat_caps
            matched = "chat"
        return caps, matched

    async def _chat_access(
        self,
        chat_id: int,
        folder_memberships: Iterable[str] | None,
    ) -> tuple[int, set[AccessLevel], str | None]:
        await self._ensure_index()
        lookup_id = _canonical_chat_id(chat_id)
        if folder_memberships is None:
            memberships: Iterable[str] = await self._folder_memberships(lookup_id)
        else:
            memberships = set(folder_memberships)
        caps, matched = self._effective_chat_caps(lookup_id, memberships)
        return lookup_id, caps, matched

    async def allows(
        self,
        chat_id: int,
        level: AccessLevel,
        *,
        folder_memberships: Iterable[str] | None = None,
    ) -> bool:
        """Return whether ``chat_id`` has ``level`` without logging or raising."""
        if self._config is None:
            return True
        _lookup_id, caps, _matched = await self._chat_access(
            chat_id, folder_memberships
        )
        return level in caps

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
        _lookup_id, caps, matched = await self._chat_access(
            chat_id, folder_memberships
        )
        if level not in caps:
            _log.warning(
                "access_denied",
                chat_ref=chat_id,
                telegram_chat_id=chat_id,
                required_level=level.name.lower(),
                granted_level=_caps_repr(caps),
                matched_rule=matched,
            )
            raise AccessDenied(
                chat_ref=chat_id,
                required_level=level,
                granted_caps=caps,
                matched_rule=matched,
            )
        _log.debug(
            "access_granted",
            chat_ref=chat_id,
            telegram_chat_id=chat_id,
            required_level=level.name.lower(),
            granted_level=_caps_repr(caps),
            matched_rule=matched,
        )

    async def require_folder(self, folder_name: str, level: AccessLevel) -> None:
        """Raise :class:`AccessDenied` unless ``folder_name`` is granted ``level``.

        Used for destination-folder gating (e.g. group create). The effective
        capability set is the union of the wildcard default and any folder rule.
        With no active policy this is a no-op.
        """
        if self._config is None:
            return
        await self._ensure_index()
        caps: set[AccessLevel] = set()
        matched: str | None = None
        if self._default_caps:
            caps |= self._default_caps
            matched = "all"
        folder_caps = self._folder_caps.get(folder_name)
        if folder_caps:
            caps |= folder_caps
            matched = f"folder:{folder_name}"
        if level not in caps:
            _log.warning(
                "access_denied",
                chat_ref=f"folder:{folder_name}",
                required_level=level.name.lower(),
                granted_level=_caps_repr(caps),
                matched_rule=matched,
            )
            raise AccessDenied(
                chat_ref=f"folder:{folder_name}",
                required_level=level,
                granted_caps=caps,
                matched_rule=matched,
            )
        _log.debug(
            "access_granted",
            chat_ref=f"folder:{folder_name}",
            required_level=level.name.lower(),
            granted_level=_caps_repr(caps),
            matched_rule=matched,
        )


__all__ = [
    "AccessDenied",
    "AccessLevel",
    "Authorizer",
]
