"""Tests for the access-control config + authorizer (Task 2).

The :class:`Authorizer` is exercised with in-memory fakes for the entity
resolver and the folder backend, so no Telethon traffic is needed. Config
validation of :class:`AccessRule` (exactly one target) is covered too.
"""

from __future__ import annotations

import io
import json

import pytest

from telegram_assistant.access import AccessDenied, AccessLevel, Authorizer
from telegram_assistant.config.models import AccessConfig, AccessRule
from telegram_assistant.entities import ResolvedEntity
from telegram_assistant.folders import FolderChat, FolderSnapshot
from telegram_assistant.observability import configure_logging

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeResolver:
    """Maps a chat ref to a :class:`ResolvedEntity` via a lookup table."""

    def __init__(self, mapping: dict[object, int]) -> None:
        self._mapping = mapping

    async def resolve(self, ref: object) -> ResolvedEntity:
        chat_id = self._mapping[ref]
        return ResolvedEntity(chat_id=chat_id, title=str(ref), kind="channel")


class FakeFolderBackend:
    def __init__(self, folders: list[FolderSnapshot]) -> None:
        self._folders = folders

    async def list_folders(self) -> list[FolderSnapshot]:
        return list(self._folders)

    async def resolve_chat(self, chat_ref: str | int) -> FolderChat:  # pragma: no cover
        raise NotImplementedError

    async def add_chat_to_folder(  # pragma: no cover
        self, folder_id: int, chat_id: int
    ) -> None:
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Config validation
# ---------------------------------------------------------------------------


def test_access_rule_defaults_to_write() -> None:
    rule = AccessRule(chat="@x")
    assert rule.permission == "write"


def test_access_rule_requires_exactly_one_target() -> None:
    with pytest.raises(ValueError):
        AccessRule(permission="read")  # no target
    with pytest.raises(ValueError):
        AccessRule(chat="@x", folder="Clients")  # two targets
    with pytest.raises(ValueError):
        AccessRule(folder="Clients", all=True)  # two targets


def test_access_rule_all_false_is_not_a_target() -> None:
    # `all: false` does not count as a target, so a rule with only all=false
    # and no chat/folder is invalid.
    with pytest.raises(ValueError):
        AccessRule(all=False)
    # but all=false + a real target is fine
    rule = AccessRule(chat=123, all=False)
    assert rule.chat == 123


def test_access_rule_chats_list_is_a_target() -> None:
    # `chats` (list form) counts as the chat target kind.
    rule = AccessRule(chats=[1, 2], permission="write")
    assert rule.chat_refs == [1, 2]
    # chat (singular) + chats (list) union into the same kind.
    rule2 = AccessRule(chat=1, chats=[2, 3])
    assert rule2.chat_refs == [1, 2, 3]


def test_access_rule_folder_id_is_its_own_target_kind() -> None:
    rule = AccessRule(folder_id=7, permission="read")
    assert rule.folder_id == 7
    assert rule.folder is None
    # ...and it conflicts with every other target kind.
    with pytest.raises(ValueError):
        AccessRule(folder="Clients", folder_id=7)
    with pytest.raises(ValueError):
        AccessRule(chat=1, folder_id=7)
    with pytest.raises(ValueError):
        AccessRule(folder_id=7, all=True)


def test_access_rule_chats_conflicts_with_other_kinds() -> None:
    with pytest.raises(ValueError):
        AccessRule(chats=[1], folder="Clients")  # two target kinds
    with pytest.raises(ValueError):
        AccessRule(chats=[1], all=True)  # two target kinds
    with pytest.raises(ValueError):
        AccessRule(permissions=["read"])  # no target


def test_access_rule_permissions_list_overrides_singular() -> None:
    rule = AccessRule(chat=1, permissions=["read", "delete"])
    assert rule.effective_permissions == ["read", "delete"]
    # empty permissions falls back to the singular permission (default write).
    rule2 = AccessRule(chat=1)
    assert rule2.effective_permissions == ["write"]
    rule3 = AccessRule(chat=1, permissions=[])
    assert rule3.effective_permissions == ["write"]


# ---------------------------------------------------------------------------
# Allow-all (no policy)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_none_config_is_allow_all_noop() -> None:
    auth = Authorizer(None)
    assert auth.enabled is False
    # Never raises regardless of level / chat.
    await auth.require(999, AccessLevel.WRITE)
    await auth.require_folder("anything", AccessLevel.WRITE)


# ---------------------------------------------------------------------------
# Deny-by-default
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_policy_denies_everything() -> None:
    auth = Authorizer(AccessConfig(rules=[]))
    assert auth.enabled is True
    with pytest.raises(AccessDenied):
        await auth.require(1, AccessLevel.READ)


# ---------------------------------------------------------------------------
# Chat rules
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_write_chat_rule_grants_only_write() -> None:
    # Independent capabilities: write grants ONLY write — read and delete are
    # denied (write no longer implies read).
    resolver = FakeResolver({"@client": 555})
    auth = Authorizer(
        AccessConfig(rules=[AccessRule(chat="@client", permission="write")]),
        resolver=resolver,
    )
    await auth.require(555, AccessLevel.WRITE)
    with pytest.raises(AccessDenied):
        await auth.require(555, AccessLevel.READ)  # write does NOT imply read
    with pytest.raises(AccessDenied):
        await auth.require(555, AccessLevel.DELETE)
    with pytest.raises(AccessDenied):
        await auth.require(556, AccessLevel.READ)  # different chat


@pytest.mark.asyncio
async def test_read_chat_rule_grants_only_read() -> None:
    resolver = FakeResolver({777: 777})
    auth = Authorizer(
        AccessConfig(rules=[AccessRule(chat=777, permission="read")]),
        resolver=resolver,
    )
    await auth.require(777, AccessLevel.READ)
    with pytest.raises(AccessDenied):
        await auth.require(777, AccessLevel.WRITE)
    with pytest.raises(AccessDenied):
        await auth.require(777, AccessLevel.DELETE)


@pytest.mark.asyncio
async def test_delete_chat_rule_grants_only_delete() -> None:
    resolver = FakeResolver({888: 888})
    auth = Authorizer(
        AccessConfig(rules=[AccessRule(chat=888, permission="delete")]),
        resolver=resolver,
    )
    await auth.require(888, AccessLevel.DELETE)
    with pytest.raises(AccessDenied):
        await auth.require(888, AccessLevel.READ)
    with pytest.raises(AccessDenied):
        await auth.require(888, AccessLevel.WRITE)


@pytest.mark.asyncio
async def test_multiple_permission_rules_accumulate_exact_caps() -> None:
    # Two rules on the same chat (read + delete) grant exactly {read, delete};
    # write is NOT implied.
    resolver = FakeResolver({5: 5})
    config = AccessConfig(
        rules=[
            AccessRule(chat=5, permission="read"),
            AccessRule(chat=5, permission="delete"),
        ]
    )
    auth = Authorizer(config, resolver=resolver)
    await auth.require(5, AccessLevel.READ)
    await auth.require(5, AccessLevel.DELETE)
    assert await auth.allows(5, AccessLevel.READ) is True
    assert await auth.allows(5, AccessLevel.DELETE) is True
    assert await auth.allows(5, AccessLevel.WRITE) is False
    with pytest.raises(AccessDenied):
        await auth.require(5, AccessLevel.WRITE)


@pytest.mark.asyncio
async def test_marked_request_chat_id_matches_bare_rule() -> None:
    """A marked ``-100…`` request id must match a rule resolved to the bare id.

    The rule side normalises via ``EntityRef.numeric_id`` (``-1001234567890`` →
    ``1234567890``); ``require`` must apply the same normalisation so the same
    marked id supplied at call time is not denied.
    """
    resolver = FakeResolver({"-1001234567890": 1234567890})
    auth = Authorizer(
        AccessConfig(
            rules=[AccessRule(chat="-1001234567890", permission="write")]
        ),
        resolver=resolver,
    )
    # Marked form (as a user would type with --chat-id) resolves to the rule.
    await auth.require(-1001234567890, AccessLevel.WRITE)
    # Bare form matches the same rule too (granted cap is write, not read).
    await auth.require(1234567890, AccessLevel.WRITE)
    # An unrelated chat is still denied.
    with pytest.raises(AccessDenied):
        await auth.require(-1009999999999, AccessLevel.WRITE)


# ---------------------------------------------------------------------------
# Folder rules
# ---------------------------------------------------------------------------


def _clients_folder() -> FolderSnapshot:
    return FolderSnapshot(
        folder_id=1,
        folder_name="Clients",
        chats=[FolderChat(chat_id=10, title="A"), FolderChat(chat_id=11, title="B")],
    )


@pytest.mark.asyncio
async def test_folder_rule_grants_member_chats() -> None:
    auth = Authorizer(
        AccessConfig(rules=[AccessRule(folder="Clients", permission="write")]),
        folder_backend=FakeFolderBackend([_clients_folder()]),
    )
    await auth.require(10, AccessLevel.WRITE)
    await auth.require(11, AccessLevel.WRITE)
    with pytest.raises(AccessDenied):
        await auth.require(99, AccessLevel.READ)  # not in folder


@pytest.mark.asyncio
async def test_require_folder_for_destination_gating() -> None:
    auth = Authorizer(
        AccessConfig(rules=[AccessRule(folder="Clients", permission="write")]),
        folder_backend=FakeFolderBackend([_clients_folder()]),
    )
    await auth.require_folder("Clients", AccessLevel.WRITE)
    with pytest.raises(AccessDenied):
        await auth.require_folder("Other", AccessLevel.WRITE)


@pytest.mark.asyncio
async def test_explicit_folder_memberships_override_backend_scan() -> None:
    # The caller can supply the memberships it already knows, avoiding a scan.
    auth = Authorizer(
        AccessConfig(rules=[AccessRule(folder="Clients", permission="write")]),
        folder_backend=FakeFolderBackend([]),  # backend would report nothing
    )
    await auth.require(10, AccessLevel.WRITE, folder_memberships=["Clients"])


# ---------------------------------------------------------------------------
# Wildcard `all`
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_wildcard_all_read_grants_read_everywhere() -> None:
    auth = Authorizer(AccessConfig(rules=[AccessRule(all=True, permission="read")]))
    await auth.require(1, AccessLevel.READ)
    await auth.require(2, AccessLevel.READ)
    with pytest.raises(AccessDenied):
        await auth.require(1, AccessLevel.WRITE)  # read-all does not grant write


# ---------------------------------------------------------------------------
# Union of capabilities across all / folder / chat rules
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_union_caps_across_all_folder_and_chat_rules() -> None:
    resolver = FakeResolver({"@vip": 42})
    config = AccessConfig(
        rules=[
            AccessRule(all=True, permission="read"),
            AccessRule(folder="Clients", permission="write"),
            AccessRule(chat="@vip", permission="write"),
        ]
    )
    auth = Authorizer(
        config,
        resolver=resolver,
        folder_backend=FakeFolderBackend([_clients_folder()]),
    )
    # Read everywhere (baseline).
    await auth.require(12345, AccessLevel.READ)
    # Folder member 10 unions the read baseline with the folder write rule.
    await auth.require(10, AccessLevel.WRITE)
    await auth.require(10, AccessLevel.READ)
    # The explicit chat unions read baseline + chat write rule.
    await auth.require(42, AccessLevel.WRITE)
    await auth.require(42, AccessLevel.READ)
    # No rule grants delete anywhere.
    with pytest.raises(AccessDenied):
        await auth.require(10, AccessLevel.DELETE)
    # But a random chat only gets read, not write.
    with pytest.raises(AccessDenied):
        await auth.require(99999, AccessLevel.WRITE)


@pytest.mark.asyncio
async def test_duplicate_targets_union_capabilities() -> None:
    # Two rules on the same chat accumulate caps as a set-union (no ordering).
    resolver = FakeResolver({5: 5})
    config = AccessConfig(
        rules=[
            AccessRule(chat=5, permission="read"),
            AccessRule(chat=5, permission="write"),
        ]
    )
    auth = Authorizer(config, resolver=resolver)
    await auth.require(5, AccessLevel.WRITE)
    await auth.require(5, AccessLevel.READ)
    with pytest.raises(AccessDenied):
        await auth.require(5, AccessLevel.DELETE)


@pytest.mark.asyncio
async def test_multi_chat_multi_permission_rule_grants_both() -> None:
    # A single rule with chats: [a, b] + permissions: [write, delete] grants
    # both caps to both chats — and nothing else.
    resolver = FakeResolver({"@a": 100, "@b": 200})
    config = AccessConfig(
        rules=[
            AccessRule(chats=["@a", "@b"], permissions=["write", "delete"]),
        ]
    )
    auth = Authorizer(config, resolver=resolver)
    for chat_id in (100, 200):
        await auth.require(chat_id, AccessLevel.WRITE)
        await auth.require(chat_id, AccessLevel.DELETE)
        with pytest.raises(AccessDenied):
            await auth.require(chat_id, AccessLevel.READ)  # not granted


@pytest.mark.asyncio
async def test_singular_chat_with_chats_union_in_one_rule() -> None:
    # chat (singular) + chats (list) within the same rule union together.
    resolver = FakeResolver({1: 1, 2: 2, 3: 3})
    config = AccessConfig(
        rules=[AccessRule(chat=1, chats=[2, 3], permission="read")],
    )
    auth = Authorizer(config, resolver=resolver)
    for chat_id in (1, 2, 3):
        await auth.require(chat_id, AccessLevel.READ)
        with pytest.raises(AccessDenied):
            await auth.require(chat_id, AccessLevel.WRITE)


@pytest.mark.asyncio
async def test_legacy_singular_rules_still_apply() -> None:
    # Backward compatibility: the old singular chat + permission form keeps
    # working unchanged.
    resolver = FakeResolver({"@legacy": 9})
    auth = Authorizer(
        AccessConfig(rules=[AccessRule(chat="@legacy", permission="write")]),
        resolver=resolver,
    )
    await auth.require(9, AccessLevel.WRITE)
    with pytest.raises(AccessDenied):
        await auth.require(9, AccessLevel.READ)


@pytest.mark.asyncio
async def test_folder_rule_with_permissions_list() -> None:
    auth = Authorizer(
        AccessConfig(
            rules=[AccessRule(folder="Clients", permissions=["read", "write"])]
        ),
        folder_backend=FakeFolderBackend([_clients_folder()]),
    )
    await auth.require(10, AccessLevel.READ)
    await auth.require(10, AccessLevel.WRITE)
    with pytest.raises(AccessDenied):
        await auth.require(10, AccessLevel.DELETE)


@pytest.mark.asyncio
async def test_denied_carries_required_and_matched_metadata() -> None:
    auth = Authorizer(AccessConfig(rules=[AccessRule(all=True, permission="read")]))
    with pytest.raises(AccessDenied) as excinfo:
        await auth.require(7, AccessLevel.WRITE)
    exc = excinfo.value
    assert exc.required_level is AccessLevel.WRITE
    assert exc.granted_level is AccessLevel.READ
    assert exc.granted_caps == frozenset({AccessLevel.READ})
    assert exc.matched_rule == "all"


# ---------------------------------------------------------------------------
# Observability: access decisions are logged (Task 5)
# ---------------------------------------------------------------------------


@pytest.fixture
def _restore_logging():
    """Snapshot/restore the root logger so ``configure_logging(force=True)`` in
    the logging tests below doesn't leave the root handler writing to a dead
    StringIO buffer (which would corrupt log capture in later tests)."""
    import logging

    root = logging.getLogger()
    saved_handlers = root.handlers[:]
    saved_level = root.level
    try:
        yield
    finally:
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)


def _capture_access_log(buf: io.StringIO) -> list[dict]:
    records = []
    for line in buf.getvalue().strip().splitlines():
        line = line.strip()
        if not line:
            continue
        records.append(json.loads(line))
    return records


@pytest.mark.asyncio
async def test_denied_chat_emits_structured_log_line(_restore_logging) -> None:
    buf = io.StringIO()
    configure_logging(level="DEBUG", stream=buf, force=True)
    auth = Authorizer(AccessConfig(rules=[AccessRule(all=True, permission="read")]))
    with pytest.raises(AccessDenied):
        await auth.require(7, AccessLevel.WRITE)
    denied = [
        r for r in _capture_access_log(buf) if r.get("event") == "access_denied"
    ]
    assert denied, "expected an access_denied log line"
    record = denied[-1]
    assert record["chat_ref"] == 7
    assert record["telegram_chat_id"] == 7
    assert record["required_level"] == "write"
    assert record["granted_level"] == "read"
    assert record["matched_rule"] == "all"


@pytest.mark.asyncio
async def test_denied_folder_emits_structured_log_line(_restore_logging) -> None:
    buf = io.StringIO()
    configure_logging(level="DEBUG", stream=buf, force=True)
    auth = Authorizer(
        AccessConfig(rules=[AccessRule(folder="Clients", permission="write")]),
        folder_backend=FakeFolderBackend([_clients_folder()]),
    )
    with pytest.raises(AccessDenied):
        await auth.require_folder("Other", AccessLevel.WRITE)
    denied = [
        r for r in _capture_access_log(buf) if r.get("event") == "access_denied"
    ]
    assert denied, "expected an access_denied log line for the folder"
    record = denied[-1]
    assert record["chat_ref"] == "folder:Other"
    assert record["required_level"] == "write"
    assert record["granted_level"] is None
    assert record["matched_rule"] is None


# ---------------------------------------------------------------------------
# Per-rule delete_only_session_messages override
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_only_override_absent_inherits_default() -> None:
    # No rule sets the override -> the policy-level default flows through
    # unchanged for both values.
    resolver = FakeResolver({"@client": 555})
    auth = Authorizer(
        AccessConfig(rules=[AccessRule(chat="@client", permission="delete")]),
        resolver=resolver,
    )
    assert await auth.delete_only_session_messages(555, default=True) is True
    assert await auth.delete_only_session_messages(555, default=False) is False


@pytest.mark.asyncio
async def test_delete_only_chat_rule_overrides_default() -> None:
    # A chat rule opting out (false) relaxes the safe default only for that
    # chat; an uncovered chat still inherits the default.
    resolver = FakeResolver({"me": 241225329})
    auth = Authorizer(
        AccessConfig(
            rules=[
                AccessRule(
                    chat="me",
                    permissions=["write", "delete"],
                    delete_only_session_messages=False,
                )
            ]
        ),
        resolver=resolver,
    )
    assert await auth.delete_only_session_messages(241225329, default=True) is False
    # A different chat is untouched by the override.
    assert await auth.delete_only_session_messages(999, default=True) is True


@pytest.mark.asyncio
async def test_delete_only_chat_override_beats_all_rule() -> None:
    # Specificity: a chat rule (false) wins over an `all` rule (true), which in
    # turn wins over the policy default.
    resolver = FakeResolver({7: 7})
    auth = Authorizer(
        AccessConfig(
            rules=[
                AccessRule(all=True, permission="delete", delete_only_session_messages=True),
                AccessRule(chat=7, permission="delete", delete_only_session_messages=False),
            ]
        ),
        resolver=resolver,
    )
    # chat 7 -> chat override false; chat 8 -> all override true (not default false)
    assert await auth.delete_only_session_messages(7, default=False) is False
    assert await auth.delete_only_session_messages(8, default=False) is True


@pytest.mark.asyncio
async def test_delete_only_folder_override_applies_to_members() -> None:
    auth = Authorizer(
        AccessConfig(
            rules=[
                AccessRule(
                    folder="Clients",
                    permission="delete",
                    delete_only_session_messages=False,
                )
            ]
        ),
        folder_backend=FakeFolderBackend([_clients_folder()]),
    )
    # chats 10/11 are in the Clients folder -> folder override false
    assert await auth.delete_only_session_messages(10, default=True) is False
    # a non-member inherits the default
    assert await auth.delete_only_session_messages(99, default=True) is True


@pytest.mark.asyncio
async def test_delete_only_conflicting_same_level_is_restrictive() -> None:
    # Two chat rules for the same chat set conflicting overrides; the
    # restrictive True wins.
    resolver = FakeResolver({5: 5})
    auth = Authorizer(
        AccessConfig(
            rules=[
                AccessRule(chat=5, permission="delete", delete_only_session_messages=False),
                AccessRule(chat=5, permission="read", delete_only_session_messages=True),
            ]
        ),
        resolver=resolver,
    )
    assert await auth.delete_only_session_messages(5, default=False) is True


@pytest.mark.asyncio
async def test_delete_only_none_config_returns_default() -> None:
    auth = Authorizer(None)
    assert await auth.delete_only_session_messages(1, default=True) is True
    assert await auth.delete_only_session_messages(1, default=False) is False


# ---------------------------------------------------------------------------
# Per-rule edit_only_session_messages override (mirror of delete resolution)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_edit_only_override_absent_inherits_default() -> None:
    resolver = FakeResolver({"@client": 555})
    auth = Authorizer(
        AccessConfig(rules=[AccessRule(chat="@client", permission="write")]),
        resolver=resolver,
    )
    assert await auth.edit_only_session_messages(555, default=True) is True
    assert await auth.edit_only_session_messages(555, default=False) is False


@pytest.mark.asyncio
async def test_edit_only_chat_rule_overrides_default() -> None:
    resolver = FakeResolver({"me": 241225329})
    auth = Authorizer(
        AccessConfig(
            rules=[
                AccessRule(
                    chat="me",
                    permission="write",
                    edit_only_session_messages=False,
                )
            ]
        ),
        resolver=resolver,
    )
    assert await auth.edit_only_session_messages(241225329, default=True) is False
    assert await auth.edit_only_session_messages(999, default=True) is True


@pytest.mark.asyncio
async def test_edit_only_chat_override_beats_all_rule() -> None:
    resolver = FakeResolver({7: 7})
    auth = Authorizer(
        AccessConfig(
            rules=[
                AccessRule(all=True, permission="write", edit_only_session_messages=True),
                AccessRule(chat=7, permission="write", edit_only_session_messages=False),
            ]
        ),
        resolver=resolver,
    )
    assert await auth.edit_only_session_messages(7, default=False) is False
    assert await auth.edit_only_session_messages(8, default=False) is True


@pytest.mark.asyncio
async def test_edit_only_folder_override_applies_to_members() -> None:
    auth = Authorizer(
        AccessConfig(
            rules=[
                AccessRule(
                    folder="Clients",
                    permission="write",
                    edit_only_session_messages=False,
                )
            ]
        ),
        folder_backend=FakeFolderBackend([_clients_folder()]),
    )
    assert await auth.edit_only_session_messages(10, default=True) is False
    assert await auth.edit_only_session_messages(99, default=True) is True


@pytest.mark.asyncio
async def test_edit_only_conflicting_same_level_is_restrictive() -> None:
    resolver = FakeResolver({5: 5})
    auth = Authorizer(
        AccessConfig(
            rules=[
                AccessRule(chat=5, permission="write", edit_only_session_messages=False),
                AccessRule(chat=5, permission="read", edit_only_session_messages=True),
            ]
        ),
        resolver=resolver,
    )
    assert await auth.edit_only_session_messages(5, default=False) is True


@pytest.mark.asyncio
async def test_edit_only_independent_of_delete_only() -> None:
    # A rule setting only the edit override leaves delete on the policy default,
    # and vice versa — the two flags resolve independently.
    resolver = FakeResolver({3: 3})
    auth = Authorizer(
        AccessConfig(
            rules=[
                AccessRule(
                    chat=3,
                    permission="write",
                    edit_only_session_messages=False,
                )
            ]
        ),
        resolver=resolver,
    )
    assert await auth.edit_only_session_messages(3, default=True) is False
    assert await auth.delete_only_session_messages(3, default=True) is True


@pytest.mark.asyncio
async def test_edit_only_none_config_returns_default() -> None:
    auth = Authorizer(None)
    assert await auth.edit_only_session_messages(1, default=True) is True
    assert await auth.edit_only_session_messages(1, default=False) is False


# ---------------------------------------------------------------------------
# Same-named folders: name rules union, `folder_id` rules select one (Task 3)
# ---------------------------------------------------------------------------


def _twin_clients_folders() -> list[FolderSnapshot]:
    """Two distinct folders sharing the title ``Clients``."""
    return [
        FolderSnapshot(
            folder_id=1,
            folder_name="Clients",
            chats=[FolderChat(chat_id=10, title="A")],
        ),
        FolderSnapshot(
            folder_id=2,
            folder_name="Clients",
            chats=[FolderChat(chat_id=20, title="B")],
        ),
    ]


@pytest.mark.asyncio
async def test_name_rule_unions_all_same_named_folders() -> None:
    # Regression: a title-keyed map kept only the last folder, silently denying
    # the chats of the shadowed one.
    auth = Authorizer(
        AccessConfig(rules=[AccessRule(folder="Clients", permission="write")]),
        folder_backend=FakeFolderBackend(_twin_clients_folders()),
    )
    await auth.require(10, AccessLevel.WRITE)
    await auth.require(20, AccessLevel.WRITE)
    with pytest.raises(AccessDenied):
        await auth.require(99, AccessLevel.WRITE)


@pytest.mark.asyncio
async def test_folder_id_rule_targets_exactly_one_folder() -> None:
    auth = Authorizer(
        AccessConfig(rules=[AccessRule(folder_id=2, permission="write")]),
        folder_backend=FakeFolderBackend(_twin_clients_folders()),
    )
    await auth.require(20, AccessLevel.WRITE)
    # The twin folder shares the title but not the id -> denied.
    with pytest.raises(AccessDenied):
        await auth.require(10, AccessLevel.WRITE)


@pytest.mark.asyncio
async def test_folder_id_rule_reports_matched_rule() -> None:
    auth = Authorizer(
        AccessConfig(rules=[AccessRule(folder_id=2, permissions=["read", "write"])]),
        folder_backend=FakeFolderBackend(_twin_clients_folders()),
    )
    caps, matched = await auth.describe(20)
    assert caps == frozenset({AccessLevel.READ, AccessLevel.WRITE})
    assert matched == "folder_id:2"
    _caps, matched_name = await auth.describe(10)
    assert matched_name is None


@pytest.mark.asyncio
async def test_folder_id_and_name_rules_union() -> None:
    auth = Authorizer(
        AccessConfig(
            rules=[
                AccessRule(folder="Clients", permission="read"),
                AccessRule(folder_id=1, permission="write"),
            ]
        ),
        folder_backend=FakeFolderBackend(_twin_clients_folders()),
    )
    # Folder 1 gets read (name rule) + write (id rule); folder 2 only read.
    await auth.require(10, AccessLevel.READ)
    await auth.require(10, AccessLevel.WRITE)
    await auth.require(20, AccessLevel.READ)
    with pytest.raises(AccessDenied):
        await auth.require(20, AccessLevel.WRITE)


@pytest.mark.asyncio
async def test_require_folder_granted_by_wildcard_rule() -> None:
    """`all:` is a separate code path in `require_folder` from chat gating.

    The documented "wildcard baseline" shape is what gates `groups create` into
    a folder, so losing the wildcard here would deny every folder placement.
    """
    auth = Authorizer(AccessConfig(rules=[AccessRule(all=True, permission="write")]))
    await auth.require_folder("Clients", AccessLevel.WRITE)
    await auth.require_folder("Clients", AccessLevel.WRITE, folder_id=2)


@pytest.mark.asyncio
async def test_require_folder_wildcard_read_does_not_grant_write() -> None:
    """Capabilities stay independent on the folder path too."""
    auth = Authorizer(AccessConfig(rules=[AccessRule(all=True, permission="read")]))
    await auth.require_folder("Clients", AccessLevel.READ)
    with pytest.raises(AccessDenied):
        await auth.require_folder("Clients", AccessLevel.WRITE)


@pytest.mark.asyncio
async def test_require_folder_accepts_folder_id_rule() -> None:
    auth = Authorizer(
        AccessConfig(rules=[AccessRule(folder_id=2, permission="write")]),
        folder_backend=FakeFolderBackend(_twin_clients_folders()),
    )
    await auth.require_folder("Clients", AccessLevel.WRITE, folder_id=2)
    # The same title with the other id (or with no id at all) is not granted.
    with pytest.raises(AccessDenied):
        await auth.require_folder("Clients", AccessLevel.WRITE, folder_id=1)
    with pytest.raises(AccessDenied):
        await auth.require_folder("Clients", AccessLevel.WRITE)


@pytest.mark.asyncio
async def test_folder_id_rule_delete_only_override() -> None:
    auth = Authorizer(
        AccessConfig(
            rules=[
                AccessRule(
                    folder_id=2,
                    permissions=["write", "delete"],
                    delete_only_session_messages=False,
                )
            ]
        ),
        folder_backend=FakeFolderBackend(_twin_clients_folders()),
    )
    # Member of folder 2 -> override applies; the same-titled twin does not.
    assert await auth.delete_only_session_messages(20, default=True) is False
    assert await auth.delete_only_session_messages(10, default=True) is True


@pytest.mark.asyncio
async def test_folder_id_rule_edit_only_override() -> None:
    auth = Authorizer(
        AccessConfig(
            rules=[
                AccessRule(
                    folder_id=1,
                    permission="write",
                    edit_only_session_messages=False,
                )
            ]
        ),
        folder_backend=FakeFolderBackend(_twin_clients_folders()),
    )
    assert await auth.edit_only_session_messages(10, default=True) is False
    assert await auth.edit_only_session_messages(20, default=True) is True


@pytest.mark.asyncio
async def test_folder_level_overrides_restrictive_across_name_and_id() -> None:
    # A name rule (false) and an id rule (true) both match chat 10; both sit at
    # the folder level, so the restrictive True wins.
    auth = Authorizer(
        AccessConfig(
            rules=[
                AccessRule(
                    folder="Clients",
                    permissions=["write", "delete"],
                    delete_only_session_messages=False,
                ),
                AccessRule(
                    folder_id=1,
                    permission="delete",
                    delete_only_session_messages=True,
                ),
            ]
        ),
        folder_backend=FakeFolderBackend(_twin_clients_folders()),
    )
    assert await auth.delete_only_session_messages(10, default=False) is True
    # Chat 20 only matches the name rule -> its false override stands.
    assert await auth.delete_only_session_messages(20, default=True) is False


@pytest.mark.asyncio
async def test_folder_id_rule_denies_caller_supplied_name_memberships() -> None:
    # Callers may hand over bare folder names (no id); those can only satisfy
    # name rules, never an id rule.
    auth = Authorizer(
        AccessConfig(rules=[AccessRule(folder_id=1, permission="write")]),
        folder_backend=FakeFolderBackend(_twin_clients_folders()),
    )
    with pytest.raises(AccessDenied):
        await auth.require(10, AccessLevel.WRITE, folder_memberships=["Clients"])
    # An explicit (id, name) membership pair does match.
    await auth.require(10, AccessLevel.WRITE, folder_memberships=[(1, "Clients")])
