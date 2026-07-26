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
import time
from typing import TYPE_CHECKING

from telegram_assistant.config.models import AccessConfig
from telegram_assistant.observability.logging import get_logger

if TYPE_CHECKING:  # pragma: no cover - typing only, avoids import cycles
    from collections.abc import Iterable

    from telegram_assistant.entities.service import EntityResolver
    from telegram_assistant.folders.service import FolderBackend
    from telegram_assistant.persistence.folder_cache import (
        FolderMembershipCache,
        MembershipMap,
    )

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


# A chat's folder membership: the stable folder id (``None`` when a caller
# supplied a bare folder name) plus the folder title. Both components exist
# because folder titles are not unique in Telegram.
Membership = tuple[int | None, str]


def _normalise_memberships(items: Iterable[Membership | str]) -> set[Membership]:
    """Accept caller-supplied memberships as bare names or ``(id, name)`` pairs.

    Callers that already know the folder (e.g. ``groups create`` passing the
    destination folder) hand over a plain name; the internal map carries ids.
    """
    result: set[Membership] = set()
    for item in items:
        if isinstance(item, str):
            result.add((None, item))
        else:
            folder_id, folder_name = item
            result.add((folder_id, folder_name))
    return result


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

    An optional persistent :class:`FolderMembershipCache` makes the
    folder-membership lookup read-through: when the injected policy sets
    ``folder_cache_ttl > 0`` a still-fresh cached map is reused without any
    Telegram round-trip (the big win for the CLI, which is one process per
    call), an expired/missing entry is refetched and rewritten, and a fetch
    error falls back to the stale cached map when one exists.
    """

    def __init__(
        self,
        config: AccessConfig | None,
        *,
        resolver: EntityResolver | None = None,
        folder_backend: FolderBackend | None = None,
        cache: FolderMembershipCache | None = None,
    ) -> None:
        self._config = config
        self._resolver = resolver
        self._folder_backend = folder_backend
        self._cache = cache
        self._built = False
        self._default_caps: set[AccessLevel] = set()
        self._chat_caps: dict[int, set[AccessLevel]] = {}
        # Folder rules are indexed twice: by title (compat — matches *every*
        # same-named folder) and by stable id (exact single folder).
        self._folder_caps: dict[str, set[AccessLevel]] = {}
        self._folder_id_caps: dict[int, set[AccessLevel]] = {}
        # Per-level ``delete_only_session_messages`` overrides (only rules that
        # set the flag land here). Within a level conflicting values collapse
        # restrictively (``True`` wins) at build time.
        self._default_delete_only: bool | None = None
        self._chat_delete_only: dict[int, bool] = {}
        self._folder_delete_only: dict[str, bool] = {}
        self._folder_id_delete_only: dict[int, bool] = {}
        # Per-level ``edit_only_session_messages`` overrides, resolved with the
        # exact same specificity/restrictive-wins rules as the delete maps above.
        self._default_edit_only: bool | None = None
        self._chat_edit_only: dict[int, bool] = {}
        self._folder_edit_only: dict[str, bool] = {}
        self._folder_id_edit_only: dict[int, bool] = {}
        self._memberships: dict[int, set[Membership]] | None = None

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
        folder_id_caps: dict[int, set[AccessLevel]] = {}
        default_delete_only: bool | None = None
        chat_delete_only: dict[int, bool] = {}
        folder_delete_only: dict[str, bool] = {}
        folder_id_delete_only: dict[int, bool] = {}
        default_edit_only: bool | None = None
        chat_edit_only: dict[int, bool] = {}
        folder_edit_only: dict[str, bool] = {}
        folder_id_edit_only: dict[int, bool] = {}

        def _merge_only(existing: bool | None, new: bool) -> bool:
            # Restrictive (True) wins on conflict within a level.
            return new if existing is None else (existing or new)

        for rule in self._config.rules:
            levels = {
                _PERMISSION_TO_LEVEL[perm] for perm in rule.effective_permissions
            }
            delete_override = rule.delete_only_session_messages
            edit_override = rule.edit_only_session_messages
            if rule.all:
                default_caps |= levels
                if delete_override is not None:
                    default_delete_only = _merge_only(
                        default_delete_only, delete_override
                    )
                if edit_override is not None:
                    default_edit_only = _merge_only(default_edit_only, edit_override)
            elif rule.folder is not None:
                folder_caps.setdefault(rule.folder, set()).update(levels)
                if delete_override is not None:
                    folder_delete_only[rule.folder] = _merge_only(
                        folder_delete_only.get(rule.folder), delete_override
                    )
                if edit_override is not None:
                    folder_edit_only[rule.folder] = _merge_only(
                        folder_edit_only.get(rule.folder), edit_override
                    )
            elif rule.folder_id is not None:
                folder_id_caps.setdefault(rule.folder_id, set()).update(levels)
                if delete_override is not None:
                    folder_id_delete_only[rule.folder_id] = _merge_only(
                        folder_id_delete_only.get(rule.folder_id), delete_override
                    )
                if edit_override is not None:
                    folder_id_edit_only[rule.folder_id] = _merge_only(
                        folder_id_edit_only.get(rule.folder_id), edit_override
                    )
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
                    if delete_override is not None:
                        chat_delete_only[resolved.chat_id] = _merge_only(
                            chat_delete_only.get(resolved.chat_id), delete_override
                        )
                    if edit_override is not None:
                        chat_edit_only[resolved.chat_id] = _merge_only(
                            chat_edit_only.get(resolved.chat_id), edit_override
                        )
        self._default_caps = default_caps
        self._chat_caps = chat_caps
        self._folder_caps = folder_caps
        self._folder_id_caps = folder_id_caps
        self._default_delete_only = default_delete_only
        self._chat_delete_only = chat_delete_only
        self._folder_delete_only = folder_delete_only
        self._folder_id_delete_only = folder_id_delete_only
        self._default_edit_only = default_edit_only
        self._chat_edit_only = chat_edit_only
        self._folder_edit_only = folder_edit_only
        self._folder_id_edit_only = folder_id_edit_only
        self._built = True

    async def _folder_memberships(self, chat_id: int) -> set[Membership]:
        # No folder rules (by name or id) → folder membership is irrelevant;
        # skip the scan.
        if not self._folder_caps and not self._folder_id_caps:
            return set()
        if self._memberships is None:
            self._memberships = await self._resolve_memberships()
        return self._memberships.get(chat_id, set())

    def invalidate_folder_memberships(self) -> None:
        """Forget the folder-membership map, in memory and in the shared cache.

        Called by the domain ops that *change* folder membership (folder
        add-chat/remove-chat, placing a freshly created group). Without this the
        persistent single-row cache would keep serving the pre-mutation map for
        up to ``folder_cache_ttl`` seconds — to this process *and* every other
        one sharing the DB — so a chat just placed into a ``folder:``-granted
        folder would be denied, and a chat just removed would keep its grant.
        A cache fault must never break the mutation that already succeeded, so
        the clear is best-effort.
        """
        self._memberships = None
        if self._cache is None:
            return
        try:
            self._cache.clear()
        except Exception as exc:  # pragma: no cover - defensive
            _log.warning("folder_membership_cache_clear_failed", error=str(exc))

    async def _resolve_memberships(self) -> dict[int, set[Membership]]:
        """Resolve the ``chat_id -> {(folder_id, folder_name)}`` map, cached.

        When a persistent cache is injected and the policy sets
        ``folder_cache_ttl > 0``:

        * a cached row younger than the TTL is used verbatim — no backend
          round-trip (the CLI win: one process per call reuses the last fetch);
        * an expired/missing entry is refetched via the folder backend and
          written back stamped with the epoch the fetch *started* at — which is
          also the fence that keeps a fetch overtaken by an
          :meth:`invalidate_folder_memberships` from restoring the stale map;
        * a fetch failure serves the stale cached map (logging a warning); the
          error only propagates when nothing is cached to fall back to.

        With no cache (or ``folder_cache_ttl == 0``) it always fetches, matching
        the pre-cache behaviour.
        """
        ttl = self._config.folder_cache_ttl if self._config is not None else 0
        cache = self._cache if ttl > 0 else None
        cached: tuple[MembershipMap, float] | None = None
        if cache is not None:
            try:
                cached = cache.load()
            except Exception as exc:
                # A cache fault must degrade to a live fetch, never deny.
                _log.warning("folder_membership_cache_load_failed", error=str(exc))
        if cached is not None and (time.time() - cached[1]) < ttl:
            return self._invert_folder_map(cached[0])
        # Stamped *before* the fetch: it is both the age the TTL should count
        # from and the fence the conditional save below uses, so a mutation that
        # invalidates the cache mid-fetch is not overwritten by the map we
        # started reading before it.
        started = time.time()
        try:
            folder_map = await self._fetch_folder_map()
        except Exception as exc:
            if cached is not None:
                _log.warning(
                    "folder_membership_fetch_failed_serving_stale",
                    error=str(exc),
                    cached_age_seconds=time.time() - cached[1],
                )
                return self._invert_folder_map(cached[0])
            raise
        # Never persist a map we did not actually fetch: with no folder backend
        # `_fetch_folder_map` returns an empty map, and writing that into the
        # shared single-row cache would deny every folder rule — for this
        # process *and* every other one — until the TTL expires.
        if cache is not None and self._folder_backend is not None:
            try:
                cache.save(folder_map, started, not_after=started)
            except Exception as exc:
                # Same invariant as the load path: a cache fault must never turn
                # a decision we already have into a 500 — the map is live and
                # usable, only the persistence for the *next* process failed.
                _log.warning("folder_membership_cache_save_failed", error=str(exc))
        return self._invert_folder_map(folder_map)

    async def _fetch_folder_map(self) -> MembershipMap:
        """Fetch the ``folder_id -> (folder_name, {bare chat id})`` map.

        Prefers the fast ``list_folder_chat_ids()`` path (bare peer ids, no
        ``get_entity`` round-trips) when the injected backend exposes it, and
        falls back to scanning ``list_folders()`` for simple/fake backends that
        only implement the title-resolving surface. Both surfaces report the
        folder's stable id, so two same-named folders stay distinct entries.
        Ids are canonicalised to the same bare form the rule index, request
        lookups, and the cache use.
        """
        folder_map: MembershipMap = {}
        backend = self._folder_backend
        if backend is None:
            return folder_map

        def _add(folder_id: int, folder_name: str, chat_ids: Iterable[int]) -> None:
            _name, ids = folder_map.setdefault(folder_id, (folder_name, set()))
            ids.update(_canonical_chat_id(cid) for cid in chat_ids)

        list_ids = getattr(backend, "list_folder_chat_ids", None)
        if callable(list_ids):
            for entry in await list_ids():
                _add(entry.folder_id, entry.folder_name, entry.chat_ids)
        else:
            for snapshot in await backend.list_folders():
                _add(
                    snapshot.folder_id,
                    snapshot.folder_name,
                    (chat.chat_id for chat in snapshot.chats),
                )
        return folder_map

    @staticmethod
    def _invert_folder_map(folder_map: MembershipMap) -> dict[int, set[Membership]]:
        """Invert the folder map into ``chat id -> {(folder_id, folder_name)}``."""
        memberships: dict[int, set[Membership]] = {}
        for folder_id, (folder_name, chat_ids) in folder_map.items():
            for cid in chat_ids:
                memberships.setdefault(_canonical_chat_id(cid), set()).add(
                    (folder_id, folder_name)
                )
        return memberships

    def _effective_chat_caps(
        self, chat_id: int, memberships: Iterable[Membership]
    ) -> tuple[set[AccessLevel], str | None]:
        """Return ``(granted_caps, matched_rule_description)`` for a chat.

        Capabilities **union** across every matching rule. A **name** folder rule
        matches when *any* of the chat's folders carries that title (so two
        same-named folders both grant), an **id** rule only on the exact folder.
        ``matched`` names the most specific rule kind that contributed (``chat``
        > ``folder:<name>``/``folder_id:<id>`` > ``all``), for observability.
        """
        caps: set[AccessLevel] = set()
        matched: str | None = None
        if self._default_caps:
            caps |= self._default_caps
            matched = "all"
        for folder_id, folder_name in memberships:
            folder_caps = self._folder_caps.get(folder_name)
            if folder_caps:
                caps |= folder_caps
                matched = f"folder:{folder_name}"
            id_caps = (
                self._folder_id_caps.get(folder_id) if folder_id is not None else None
            )
            if id_caps:
                caps |= id_caps
                matched = f"folder_id:{folder_id}"
        chat_caps = self._chat_caps.get(chat_id)
        if chat_caps:
            caps |= chat_caps
            matched = "chat"
        return caps, matched

    async def _chat_access(
        self,
        chat_id: int,
        folder_memberships: Iterable[Membership | str] | None,
    ) -> tuple[int, set[AccessLevel], str | None]:
        await self._ensure_index()
        lookup_id = _canonical_chat_id(chat_id)
        if folder_memberships is None:
            memberships: Iterable[Membership] = await self._folder_memberships(
                lookup_id
            )
        else:
            memberships = _normalise_memberships(folder_memberships)
        caps, matched = self._effective_chat_caps(lookup_id, memberships)
        return lookup_id, caps, matched

    async def allows(
        self,
        chat_id: int,
        level: AccessLevel,
        *,
        folder_memberships: Iterable[Membership | str] | None = None,
    ) -> bool:
        """Return whether ``chat_id`` has ``level`` without logging or raising."""
        if self._config is None:
            return True
        _lookup_id, caps, _matched = await self._chat_access(
            chat_id, folder_memberships
        )
        return level in caps

    async def describe(
        self,
        chat_id: int,
        *,
        folder_memberships: Iterable[Membership | str] | None = None,
    ) -> tuple[frozenset[AccessLevel], str | None]:
        """Return ``(granted_caps, matched_rule)`` for ``chat_id`` without raising.

        Used by access-inspection tooling (e.g. the ``access check`` CLI) that
        wants to report *what* a chat is granted rather than gate a single
        operation. For the allow-all sentinel (no active policy) every
        capability is granted and the matched rule is reported as
        ``"allow_all"``.
        """
        if self._config is None:
            return frozenset(AccessLevel), "allow_all"
        _lookup_id, caps, matched = await self._chat_access(
            chat_id, folder_memberships
        )
        return frozenset(caps), matched

    async def _resolve_session_only(
        self,
        chat_id: int,
        *,
        which: str,
        default: bool,
        folder_memberships: Iterable[Membership | str] | None,
    ) -> bool:
        """Resolve a ``*_only_session_messages`` flag by specificity.

        ``which`` selects the override maps (``"delete"`` or ``"edit"``). Starts
        from the policy-level ``default`` and applies the most specific matching
        rule override (chat rule > folder rule > all rule). Name- and
        id-targeted folder rules sit at the *same* (folder) level, so a
        restrictive ``True`` from either wins. Within one level a restrictive
        ``True`` already won at index-build time. The override maps are read
        *after* :meth:`_ensure_index` populates them.
        """
        await self._ensure_index()
        if which == "delete":
            default_override = self._default_delete_only
            folder_overrides_map = self._folder_delete_only
            folder_id_overrides_map = self._folder_id_delete_only
            chat_overrides_map = self._chat_delete_only
        else:
            default_override = self._default_edit_only
            folder_overrides_map = self._folder_edit_only
            folder_id_overrides_map = self._folder_id_edit_only
            chat_overrides_map = self._chat_edit_only
        lookup_id = _canonical_chat_id(chat_id)
        if folder_memberships is None:
            memberships: Iterable[Membership] = await self._folder_memberships(
                lookup_id
            )
        else:
            memberships = _normalise_memberships(folder_memberships)
        effective = default
        if default_override is not None:
            effective = default_override
        folder_overrides = [
            folder_overrides_map[folder_name]
            for _folder_id, folder_name in memberships
            if folder_name in folder_overrides_map
        ]
        folder_overrides.extend(
            folder_id_overrides_map[folder_id]
            for folder_id, _folder_name in memberships
            if folder_id is not None and folder_id in folder_id_overrides_map
        )
        if folder_overrides:
            # Multiple folders (by name or id) may match; restrictive (True)
            # wins across the whole folder level.
            effective = any(folder_overrides)
        if lookup_id in chat_overrides_map:
            effective = chat_overrides_map[lookup_id]
        return effective

    async def delete_only_session_messages(
        self,
        chat_id: int,
        *,
        default: bool,
        folder_memberships: Iterable[Membership | str] | None = None,
    ) -> bool:
        """Resolve the effective ``delete_only_session_messages`` for a chat.

        Starts from the policy-level ``default`` and applies the most specific
        matching rule override (chat rule > folder rule > all rule). Within one
        level a restrictive ``True`` already won at index-build time. With no
        active policy the ``default`` is returned unchanged.
        """
        if self._config is None:
            return default
        return await self._resolve_session_only(
            chat_id,
            which="delete",
            default=default,
            folder_memberships=folder_memberships,
        )

    async def edit_only_session_messages(
        self,
        chat_id: int,
        *,
        default: bool,
        folder_memberships: Iterable[Membership | str] | None = None,
    ) -> bool:
        """Resolve the effective ``edit_only_session_messages`` for a chat.

        Mirror of :meth:`delete_only_session_messages`: chat rule > folder rule
        > all rule > policy ``default``, restrictive ``True`` wins on same-level
        conflict. With no active policy the ``default`` is returned unchanged.
        """
        if self._config is None:
            return default
        return await self._resolve_session_only(
            chat_id,
            which="edit",
            default=default,
            folder_memberships=folder_memberships,
        )

    async def require(
        self,
        chat_id: int,
        level: AccessLevel,
        *,
        folder_memberships: Iterable[Membership | str] | None = None,
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

    async def require_folder(
        self,
        folder_name: str,
        level: AccessLevel,
        *,
        folder_id: int | None = None,
    ) -> None:
        """Raise :class:`AccessDenied` unless the folder is granted ``level``.

        Used for destination-folder gating (e.g. group create). The effective
        capability set is the union of the wildcard default, any name rule
        carrying ``folder_name`` and — when the caller knows the folder's stable
        id — any ``folder_id`` rule for it. With no active policy this is a
        no-op.
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
        id_caps = self._folder_id_caps.get(folder_id) if folder_id is not None else None
        if id_caps:
            caps |= id_caps
            matched = f"folder_id:{folder_id}"
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
