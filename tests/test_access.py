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
async def test_chat_rule_grants_write_and_implies_read() -> None:
    resolver = FakeResolver({"@client": 555})
    auth = Authorizer(
        AccessConfig(rules=[AccessRule(chat="@client", permission="write")]),
        resolver=resolver,
    )
    await auth.require(555, AccessLevel.WRITE)
    await auth.require(555, AccessLevel.READ)  # write implies read
    with pytest.raises(AccessDenied):
        await auth.require(556, AccessLevel.READ)  # different chat


@pytest.mark.asyncio
async def test_read_chat_rule_denies_write() -> None:
    resolver = FakeResolver({777: 777})
    auth = Authorizer(
        AccessConfig(rules=[AccessRule(chat=777, permission="read")]),
        resolver=resolver,
    )
    await auth.require(777, AccessLevel.READ)
    with pytest.raises(AccessDenied):
        await auth.require(777, AccessLevel.WRITE)


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
    # Bare form matches the same rule too.
    await auth.require(1234567890, AccessLevel.READ)
    # An unrelated chat is still denied.
    with pytest.raises(AccessDenied):
        await auth.require(-1009999999999, AccessLevel.READ)


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
# Union / highest-level-wins
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_union_read_all_baseline_plus_targeted_write() -> None:
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
    # Write to folder members (folder rule wins over read baseline).
    await auth.require(10, AccessLevel.WRITE)
    # Write to the explicit chat.
    await auth.require(42, AccessLevel.WRITE)
    # But a random chat only gets read, not write.
    with pytest.raises(AccessDenied):
        await auth.require(99999, AccessLevel.WRITE)


@pytest.mark.asyncio
async def test_highest_level_wins_across_duplicate_targets() -> None:
    resolver = FakeResolver({5: 5})
    config = AccessConfig(
        rules=[
            AccessRule(chat=5, permission="read"),
            AccessRule(chat=5, permission="write"),
        ]
    )
    auth = Authorizer(config, resolver=resolver)
    await auth.require(5, AccessLevel.WRITE)  # write rule wins


@pytest.mark.asyncio
async def test_denied_carries_required_and_matched_metadata() -> None:
    auth = Authorizer(AccessConfig(rules=[AccessRule(all=True, permission="read")]))
    with pytest.raises(AccessDenied) as excinfo:
        await auth.require(7, AccessLevel.WRITE)
    exc = excinfo.value
    assert exc.required_level is AccessLevel.WRITE
    assert exc.granted_level is AccessLevel.READ
    assert exc.matched_rule == "all"


# ---------------------------------------------------------------------------
# Observability: access decisions are logged (Task 5)
# ---------------------------------------------------------------------------


def _capture_access_log(buf: io.StringIO) -> list[dict]:
    records = []
    for line in buf.getvalue().strip().splitlines():
        line = line.strip()
        if not line:
            continue
        records.append(json.loads(line))
    return records


@pytest.mark.asyncio
async def test_denied_chat_emits_structured_log_line() -> None:
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
async def test_denied_folder_emits_structured_log_line() -> None:
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
