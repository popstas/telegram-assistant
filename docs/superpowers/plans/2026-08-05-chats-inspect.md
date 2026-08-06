# `chats inspect` Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a read-only `telegram-assistant chats inspect` CLI command that reports what Telegram knows about one chat — auto-delete TTL, description, member counts, restrictions, our own rights — for supergroups, channels, legacy groups, users and bots.

**Architecture:** A new `chats/` domain package shaped like `members/listing.py`: a pure `service.py` (dataclass + protocol + `inspect_chat()` with the READ gate) and a `telethon_backend.py` adapter that issues `get_input_entity` plus one `GetFull*` request and maps the pair into one flat payload. The CLI wires them exactly like `members list`.

**Tech Stack:** Python 3.12, Telethon >= 1.44, Typer (CLI), pytest + pytest-asyncio (asyncio mode auto), ruff.

**Spec:** `docs/superpowers/specs/2026-08-05-chats-inspect-design.md`

## Global Constraints

- Use `.venv` — run everything as `.venv/bin/pytest`, `.venv/bin/ruff`, `.venv/bin/telegram-assistant`.
- `ruff check src tests` must pass: line-length 100, target py312, `E501` ignored.
- `chats/service.py` must not import `telethon` at all. `chats/telethon_backend.py` imports Telethon **inside** functions only (the pattern in `members/telethon_backend.py`), so the package imports cleanly without a session.
- The op opens **no** operation row, has **no** idempotency key and **no** `--dry-run`. It is READ-gated.
- `chat_id` in the payload is the **bare** id (no `-100` marker), matching `EntityRef.numeric_id`.
- `access_hash` must never appear in any payload, including `--raw`.
- Phase 1 is CLI-only. Do **not** add HTTP routes, MCP tools, or backend factories in this plan.
- Exit codes: caller-input and resolution failures → 2, `AccessDenied` → 3, anything else → 1.

---

### Task 1: `chats/` domain — `ChatInfo`, backend protocol, `inspect_chat()`

**Files:**
- Create: `src/telegram_assistant/chats/__init__.py`
- Create: `src/telegram_assistant/chats/service.py`
- Test: `tests/test_chats_inspect.py`

**Interfaces:**
- Consumes: `telegram_assistant.access.service.AccessLevel`, `Authorizer` (already exist; `await authorizer.require(chat_id, AccessLevel.READ)` raises `AccessDenied`).
- Produces:
  - `ChatInfo` — frozen dataclass, all fields keyword-constructible, `to_dict() -> dict[str, Any]`.
  - `ChatInspectBackend` — Protocol with `async def inspect_chat(self, *, chat_id: int, raw: bool) -> ChatInfo`.
  - `async def inspect_chat(*, backend: ChatInspectBackend, chat_id: int, raw: bool = False, authorizer: Authorizer | None = None) -> ChatInfo`.
  - `CHAT_KINDS: frozenset[str]` = `{"user", "bot", "basic_group", "supergroup", "channel"}`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_chats_inspect.py`:

```python
"""Tests for the read-only chat-inspect domain op."""

from __future__ import annotations

import pytest

from telegram_assistant.access import AccessDenied, Authorizer
from telegram_assistant.chats import ChatInfo, inspect_chat
from telegram_assistant.config.models import AccessConfig, AccessRule


class FakeBackend:
    """Records calls and returns a canned ChatInfo."""

    def __init__(self, info: ChatInfo | None = None) -> None:
        self.info = info or ChatInfo(chat_id=42, kind="supergroup", title="T")
        self.calls: list[dict[str, object]] = []

    async def inspect_chat(self, *, chat_id: int, raw: bool) -> ChatInfo:
        self.calls.append({"chat_id": chat_id, "raw": raw})
        return self.info


@pytest.mark.asyncio
async def test_inspect_chat_returns_backend_result() -> None:
    backend = FakeBackend()

    result = await inspect_chat(backend=backend, chat_id=42)

    assert result is backend.info
    assert backend.calls == [{"chat_id": 42, "raw": False}]


@pytest.mark.asyncio
async def test_inspect_chat_passes_raw_through() -> None:
    backend = FakeBackend()

    await inspect_chat(backend=backend, chat_id=42, raw=True)

    assert backend.calls == [{"chat_id": 42, "raw": True}]


@pytest.mark.asyncio
async def test_read_gate_denies_before_any_rpc() -> None:
    backend = FakeBackend()
    authorizer = Authorizer(AccessConfig(rules=[]))

    with pytest.raises(AccessDenied):
        await inspect_chat(backend=backend, chat_id=42, authorizer=authorizer)

    assert backend.calls == []


@pytest.mark.asyncio
async def test_read_gate_allows_granted_chat() -> None:
    backend = FakeBackend()
    authorizer = Authorizer(
        AccessConfig(rules=[AccessRule(all=True, permissions=["read"])])
    )

    result = await inspect_chat(backend=backend, chat_id=42, authorizer=authorizer)

    assert result.chat_id == 42
    assert backend.calls == [{"chat_id": 42, "raw": False}]


@pytest.mark.asyncio
async def test_write_only_grant_does_not_satisfy_read() -> None:
    backend = FakeBackend()
    authorizer = Authorizer(
        AccessConfig(rules=[AccessRule(all=True, permissions=["write"])])
    )

    with pytest.raises(AccessDenied):
        await inspect_chat(backend=backend, chat_id=42, authorizer=authorizer)

    assert backend.calls == []


def test_to_dict_omits_raw_when_absent() -> None:
    info = ChatInfo(chat_id=7, kind="user", title="Someone")

    payload = info.to_dict()

    assert payload["chat_id"] == 7
    assert payload["kind"] == "user"
    assert "raw" not in payload
    # Fields that do not apply to a user are present and null, so the shape
    # never depends on what was inspected.
    assert payload["admins_count"] is None
    assert payload["ttl_period"] is None


def test_to_dict_includes_raw_when_present() -> None:
    info = ChatInfo(
        chat_id=7, kind="supergroup", title="T", raw={"entity": {}, "full": {}}
    )

    payload = info.to_dict()

    assert payload["raw"] == {"entity": {}, "full": {}}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_chats_inspect.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'telegram_assistant.chats'`

- [ ] **Step 3: Write `src/telegram_assistant/chats/service.py`**

```python
"""Read-only chat metadata — the inspect op of the chats domain.

A READ op in the shape of :mod:`telegram_assistant.members.listing`: no
operation row, no idempotency key, no ``--dry-run``. It answers "what is this
chat" for every peer kind with one flat payload, so a caller can read
``ttl_period`` without knowing whether the target is a supergroup or a private
chat.

The payload is a *curated* set rather than a dump of Telethon's ``*Full``
objects: those carry 60+ fields that move with the Telegram layer, and pinning
tests to them would turn a Telethon upgrade into a test failure. ``raw`` is the
escape hatch for everything left out.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Protocol

from telegram_assistant.access.service import AccessLevel, Authorizer

#: Peer kinds ``ChatInfo.kind`` may report.
CHAT_KINDS: frozenset[str] = frozenset(
    {"user", "bot", "basic_group", "supergroup", "channel"}
)


@dataclass(frozen=True)
class ChatInfo:
    """One chat's metadata, flat and kind-agnostic.

    Every field exists for every kind; the ones Telegram does not answer for a
    given peer are ``None`` (or ``False`` for flags). That is deliberate — a
    caller running ``jq .ttl_period`` must not have to branch on ``kind``.
    """

    # --- identity (all kinds) ---
    chat_id: int
    kind: str
    title: str | None = None
    username: str | None = None
    usernames: tuple[str, ...] = ()
    about: str | None = None
    created_at: datetime | None = None

    # --- the reason this op exists, plus the settings next to it ---
    ttl_period: int | None = None
    pinned_message_id: int | None = None
    archived: bool = False
    muted: bool = False
    muted_until: datetime | None = None
    has_scheduled: bool = False

    # --- trust / restrictions (all kinds) ---
    restricted: bool = False
    restriction_reason: tuple[dict[str, Any], ...] = ()
    verified: bool = False
    scam: bool = False
    fake: bool = False

    # --- our standing (all kinds) ---
    is_creator: bool = False
    left: bool = False
    invite_link: str | None = None
    my_admin_rights: dict[str, Any] | None = None
    default_banned_rights: dict[str, Any] | None = None

    # --- groups and channels ---
    is_forum: bool = False
    topics_layout: str | None = None
    broadcast: bool = False
    megagroup: bool = False
    gigagroup: bool = False
    participants_count: int | None = None
    admins_count: int | None = None
    kicked_count: int | None = None
    banned_count: int | None = None
    online_count: int | None = None
    slowmode_seconds: int | None = None
    slowmode_next_send_date: datetime | None = None
    linked_chat_id: int | None = None
    migrated_from_chat_id: int | None = None
    migrated_to_chat_id: int | None = None
    deactivated: bool = False
    hidden_prehistory: bool = False
    participants_hidden: bool = False
    antispam: bool = False
    can_view_participants: bool = False
    can_view_stats: bool = False
    can_delete_channel: bool = False
    can_set_username: bool = False
    join_to_send: bool = False
    join_request: bool = False
    requests_pending: int | None = None
    noforwards: bool = False
    unread_count: int | None = None
    available_reactions: Any = None
    reactions_limit: int | None = None
    call_active: bool = False

    # --- users and bots ---
    first_name: str | None = None
    last_name: str | None = None
    phone: str | None = None
    is_bot: bool = False
    is_deleted: bool = False
    is_premium: bool = False
    is_contact: bool = False
    is_mutual_contact: bool = False
    blocked: bool = False
    common_chats_count: int | None = None
    birthday: dict[str, Any] | None = None
    personal_channel_id: int | None = None
    last_seen_status: str | None = None

    # --- escape hatch ---
    raw: dict[str, Any] | None = field(default=None)

    def to_dict(self) -> dict[str, Any]:
        """The payload body. ``raw`` appears only when it was requested."""
        payload = asdict(self)
        payload["usernames"] = list(self.usernames)
        payload["restriction_reason"] = list(self.restriction_reason)
        if self.raw is None:
            payload.pop("raw")
        return payload


class ChatInspectBackend(Protocol):
    """Telethon-facing surface needed to read one chat's metadata.

    Production wires this to
    :class:`telegram_assistant.chats.telethon_backend.TelethonChatInspectBackend`;
    tests inject a fake.
    """

    async def inspect_chat(self, *, chat_id: int, raw: bool) -> ChatInfo:
        ...


async def inspect_chat(
    *,
    backend: ChatInspectBackend,
    chat_id: int,
    raw: bool = False,
    authorizer: Authorizer | None = None,
) -> ChatInfo:
    """Read ``chat_id``'s metadata.

    A READ op: when an ``authorizer`` is supplied it must grant READ on the
    chat, checked before any Telegram call. The payload carries the
    description, the member counts and the invite link, so a denied caller must
    cost no round trip and learn nothing about the chat.
    """
    if authorizer is not None:
        await authorizer.require(chat_id, AccessLevel.READ)

    return await backend.inspect_chat(chat_id=chat_id, raw=raw)


__all__ = [
    "CHAT_KINDS",
    "ChatInfo",
    "ChatInspectBackend",
    "inspect_chat",
]
```

- [ ] **Step 4: Write `src/telegram_assistant/chats/__init__.py`**

```python
"""Chat-wide read operations (metadata inspection)."""

from telegram_assistant.chats.service import (
    CHAT_KINDS,
    ChatInfo,
    ChatInspectBackend,
    inspect_chat,
)

__all__ = [
    "CHAT_KINDS",
    "ChatInfo",
    "ChatInspectBackend",
    "inspect_chat",
]
```

- [ ] **Step 5: Run the tests**

Run: `.venv/bin/pytest tests/test_chats_inspect.py -v`
Expected: PASS (7 tests)

`AccessConfig` / `AccessRule` live in `telegram_assistant.config.models` (verified), while `Authorizer` / `AccessLevel` / `AccessDenied` are re-exported from `telegram_assistant.access` — the same import split `tests/test_members_list.py` uses.

- [ ] **Step 6: Lint**

Run: `.venv/bin/ruff check src/telegram_assistant/chats tests/test_chats_inspect.py`
Expected: `All checks passed!`

- [ ] **Step 7: Commit**

```bash
git add src/telegram_assistant/chats tests/test_chats_inspect.py
git commit -m "feat(chats): add read-only chat-inspect domain op"
```

---

### Task 2: Telethon adapter — map three peer kinds into `ChatInfo`

**Files:**
- Create: `src/telegram_assistant/chats/telethon_backend.py`
- Modify: `src/telegram_assistant/chats/__init__.py` (no new export — the adapter is imported by path, like `members.telethon_backend`)
- Test: `tests/test_chats_inspect_backend.py`

**Interfaces:**
- Consumes: `ChatInfo` from Task 1.
- Produces: `TelethonChatInspectBackend(client)` implementing `ChatInspectBackend`.

**Wire facts this task depends on** (do not re-derive):
- `channels.GetFullChannel` and `messages.GetFullChat` both answer with a `messages.ChatFull` carrying `.full_chat`, `.chats` and `.users`. `users.GetFullUser` answers with `.full_user`, `.chats` and `.users`.
- `forum_tabs` is a flag on the **`Channel`** constructor (flags2.19), not on `ChannelFull` — `groups/telethon_backend.py::get_topics_layout` already reads it out of the response's own `.chats`, matched by `full_chat.id`. Do the same; do not issue a second `get_entity`.
- `get_input_entity(chat_id)` returns `InputPeerChannel` / `InputPeerChat` / `InputPeerUser`, which is enough to dispatch. The adapter never calls `get_entity`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_chats_inspect_backend.py`:

```python
"""Tests for the Telethon chat-inspect adapter.

Exercised against a fake client: the stand-in classes' *names* are what the
peer dispatch keys on, mirroring tests/test_members_list_backend.py.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from telegram_assistant.chats.telethon_backend import TelethonChatInspectBackend

# --- fake telethon-shaped objects -----------------------------------------


class InputPeerChannel:
    def __init__(self, channel_id: int) -> None:
        self.channel_id = channel_id


class InputPeerChat:
    def __init__(self, chat_id: int) -> None:
        self.chat_id = chat_id


class InputPeerUser:
    def __init__(self, user_id: int) -> None:
        self.user_id = user_id


class Rights:
    """Stand-in for ChatAdminRights / ChatBannedRights."""

    def __init__(self, **flags) -> None:
        self._flags = flags

    def to_dict(self) -> dict:
        return {"_": "ChatAdminRights", **self._flags}


class NotifySettings:
    def __init__(self, mute_until=None, silent=False) -> None:
        self.mute_until = mute_until
        self.silent = silent


class InviteExported:
    def __init__(self, link: str) -> None:
        self.link = link


class RestrictionReason:
    def __init__(self, platform: str, reason: str, text: str) -> None:
        self.platform = platform
        self.reason = reason
        self.text = text


class Username:
    def __init__(self, username: str, active: bool = True) -> None:
        self.username = username
        self.active = active


class ReactionEmoji:
    def __init__(self, emoticon: str) -> None:
        self.emoticon = emoticon


class ChatReactionsSome:
    def __init__(self, reactions) -> None:
        self.reactions = reactions


class ChatReactionsAll:
    pass


class Channel:
    def __init__(self, cid: int, **kw) -> None:
        self.id = cid
        self.title = kw.get("title", "Chat")
        self.username = kw.get("username")
        self.usernames = kw.get("usernames")
        self.date = kw.get("date")
        self.creator = kw.get("creator", False)
        self.left = kw.get("left", False)
        self.broadcast = kw.get("broadcast", False)
        self.megagroup = kw.get("megagroup", True)
        self.gigagroup = kw.get("gigagroup", False)
        self.forum = kw.get("forum", False)
        self.forum_tabs = kw.get("forum_tabs", False)
        self.verified = kw.get("verified", False)
        self.scam = kw.get("scam", False)
        self.fake = kw.get("fake", False)
        self.restricted = kw.get("restricted", False)
        self.restriction_reason = kw.get("restriction_reason")
        self.noforwards = kw.get("noforwards", False)
        self.join_to_send = kw.get("join_to_send", False)
        self.join_request = kw.get("join_request", False)
        self.call_active = kw.get("call_active", False)
        self.admin_rights = kw.get("admin_rights")
        self.default_banned_rights = kw.get("default_banned_rights")
        self.access_hash = 999999


class ChannelFull:
    def __init__(self, cid: int, **kw) -> None:
        self.id = cid
        self.about = kw.get("about", "")
        self.ttl_period = kw.get("ttl_period")
        self.pinned_msg_id = kw.get("pinned_msg_id")
        self.folder_id = kw.get("folder_id")
        self.notify_settings = kw.get("notify_settings", NotifySettings())
        self.has_scheduled = kw.get("has_scheduled", False)
        self.participants_count = kw.get("participants_count")
        self.admins_count = kw.get("admins_count")
        self.kicked_count = kw.get("kicked_count")
        self.banned_count = kw.get("banned_count")
        self.online_count = kw.get("online_count")
        self.slowmode_seconds = kw.get("slowmode_seconds")
        self.slowmode_next_send_date = kw.get("slowmode_next_send_date")
        self.linked_chat_id = kw.get("linked_chat_id")
        self.migrated_from_chat_id = kw.get("migrated_from_chat_id")
        self.hidden_prehistory = kw.get("hidden_prehistory", False)
        self.participants_hidden = kw.get("participants_hidden", False)
        self.antispam = kw.get("antispam", False)
        self.can_view_participants = kw.get("can_view_participants", False)
        self.can_view_stats = kw.get("can_view_stats", False)
        self.can_delete_channel = kw.get("can_delete_channel", False)
        self.can_set_username = kw.get("can_set_username", False)
        self.requests_pending = kw.get("requests_pending")
        self.unread_count = kw.get("unread_count")
        self.available_reactions = kw.get("available_reactions")
        self.reactions_limit = kw.get("reactions_limit")
        self.exported_invite = kw.get("exported_invite")

    def to_dict(self) -> dict:
        return {"_": "ChannelFull", "id": self.id, "about": self.about}


class Chat:
    def __init__(self, cid: int, **kw) -> None:
        self.id = cid
        self.title = kw.get("title", "Legacy")
        self.date = kw.get("date")
        self.creator = kw.get("creator", False)
        self.left = kw.get("left", False)
        self.deactivated = kw.get("deactivated", False)
        self.noforwards = kw.get("noforwards", False)
        self.call_active = kw.get("call_active", False)
        self.participants_count = kw.get("participants_count")
        self.migrated_to = kw.get("migrated_to")
        self.admin_rights = kw.get("admin_rights")
        self.default_banned_rights = kw.get("default_banned_rights")


class ChatFull:
    def __init__(self, cid: int, **kw) -> None:
        self.id = cid
        self.about = kw.get("about", "")
        self.ttl_period = kw.get("ttl_period")
        self.pinned_msg_id = kw.get("pinned_msg_id")
        self.folder_id = kw.get("folder_id")
        self.notify_settings = kw.get("notify_settings", NotifySettings())
        self.has_scheduled = kw.get("has_scheduled", False)
        self.can_set_username = kw.get("can_set_username", False)
        self.requests_pending = kw.get("requests_pending")
        self.available_reactions = kw.get("available_reactions")
        self.reactions_limit = kw.get("reactions_limit")
        self.exported_invite = kw.get("exported_invite")

    def to_dict(self) -> dict:
        return {"_": "ChatFull", "id": self.id}


class InputPeerChannelMigrated:
    def __init__(self, channel_id: int) -> None:
        self.channel_id = channel_id


class UserStatusRecently:
    pass


class User:
    def __init__(self, uid: int, **kw) -> None:
        self.id = uid
        self.first_name = kw.get("first_name", "First")
        self.last_name = kw.get("last_name")
        self.username = kw.get("username")
        self.usernames = kw.get("usernames")
        self.phone = kw.get("phone")
        self.bot = kw.get("bot", False)
        self.deleted = kw.get("deleted", False)
        self.premium = kw.get("premium", False)
        self.contact = kw.get("contact", False)
        self.mutual_contact = kw.get("mutual_contact", False)
        self.verified = kw.get("verified", False)
        self.scam = kw.get("scam", False)
        self.fake = kw.get("fake", False)
        self.restricted = kw.get("restricted", False)
        self.restriction_reason = kw.get("restriction_reason")
        self.status = kw.get("status")
        self.access_hash = 777777


class Birthday:
    def __init__(self, day: int, month: int, year=None) -> None:
        self.day = day
        self.month = month
        self.year = year


class UserFull:
    def __init__(self, uid: int, **kw) -> None:
        self.id = uid
        self.about = kw.get("about")
        self.ttl_period = kw.get("ttl_period")
        self.pinned_msg_id = kw.get("pinned_msg_id")
        self.folder_id = kw.get("folder_id")
        self.notify_settings = kw.get("notify_settings", NotifySettings())
        self.has_scheduled = kw.get("has_scheduled", False)
        self.blocked = kw.get("blocked", False)
        self.common_chats_count = kw.get("common_chats_count")
        self.birthday = kw.get("birthday")
        self.personal_channel_id = kw.get("personal_channel_id")

    def to_dict(self) -> dict:
        return {"_": "UserFull", "id": self.id}


class FullChannelResult:
    def __init__(self, full_chat, chats) -> None:
        self.full_chat = full_chat
        self.chats = chats
        self.users = []


class FullUserResult:
    def __init__(self, full_user, users) -> None:
        self.full_user = full_user
        self.users = users
        self.chats = []


class FakeClient:
    def __init__(self, *, peer, result) -> None:
        self._peer = peer
        self._result = result
        self.requests: list[object] = []

    async def get_input_entity(self, ref):
        return self._peer

    async def __call__(self, request):
        self.requests.append(request)
        return self._result


# --- supergroup -------------------------------------------------------------


@pytest.mark.asyncio
async def test_supergroup_mapping() -> None:
    created = datetime(2024, 3, 1, tzinfo=timezone.utc)
    channel = Channel(
        5,
        title="Team",
        username="teamchat",
        usernames=[Username("alt"), Username("dead", active=False)],
        date=created,
        forum=True,
        forum_tabs=True,
        creator=True,
        noforwards=True,
        admin_rights=Rights(delete_messages=True),
        default_banned_rights=Rights(send_media=True),
    )
    full = ChannelFull(
        5,
        about="About us",
        ttl_period=86400,
        pinned_msg_id=41,
        folder_id=1,
        participants_count=12,
        admins_count=2,
        kicked_count=0,
        banned_count=1,
        online_count=3,
        slowmode_seconds=30,
        available_reactions=ChatReactionsSome([ReactionEmoji("👍")]),
        exported_invite=InviteExported("https://t.me/+abc"),
        notify_settings=NotifySettings(mute_until=created, silent=True),
    )
    client = FakeClient(
        peer=InputPeerChannel(5), result=FullChannelResult(full, [channel])
    )
    backend = TelethonChatInspectBackend(client)

    info = await backend.inspect_chat(chat_id=-1000000000005, raw=False)

    assert info.chat_id == 5
    assert info.kind == "supergroup"
    assert info.title == "Team"
    assert info.username == "teamchat"
    assert info.usernames == ("alt",)
    assert info.about == "About us"
    assert info.ttl_period == 86400
    assert info.pinned_message_id == 41
    assert info.archived is True
    assert info.muted is True
    assert info.muted_until == created
    assert info.created_at == created
    assert info.is_forum is True
    assert info.topics_layout == "tabs"
    assert info.participants_count == 12
    assert info.admins_count == 2
    assert info.banned_count == 1
    assert info.online_count == 3
    assert info.slowmode_seconds == 30
    assert info.noforwards is True
    assert info.is_creator is True
    assert info.invite_link == "https://t.me/+abc"
    assert info.available_reactions == ["👍"]
    assert info.my_admin_rights == {"delete_messages": True}
    assert info.default_banned_rights == {"send_media": True}
    assert info.raw is None


@pytest.mark.asyncio
async def test_broadcast_channel_kind_and_layout_default() -> None:
    channel = Channel(6, broadcast=True, megagroup=False)
    client = FakeClient(
        peer=InputPeerChannel(6), result=FullChannelResult(ChannelFull(6), [channel])
    )
    backend = TelethonChatInspectBackend(client)

    info = await backend.inspect_chat(chat_id=6, raw=False)

    assert info.kind == "channel"
    assert info.broadcast is True
    assert info.is_forum is False
    assert info.topics_layout is None


@pytest.mark.asyncio
async def test_reactions_all_maps_to_all() -> None:
    channel = Channel(6)
    full = ChannelFull(6, available_reactions=ChatReactionsAll())
    client = FakeClient(peer=InputPeerChannel(6), result=FullChannelResult(full, [channel]))
    backend = TelethonChatInspectBackend(client)

    info = await backend.inspect_chat(chat_id=6, raw=False)

    assert info.available_reactions == "all"


@pytest.mark.asyncio
async def test_restriction_reason_is_mapped() -> None:
    channel = Channel(
        8,
        restricted=True,
        restriction_reason=[RestrictionReason("all", "terms", "violated ToS")],
    )
    client = FakeClient(
        peer=InputPeerChannel(8), result=FullChannelResult(ChannelFull(8), [channel])
    )
    backend = TelethonChatInspectBackend(client)

    info = await backend.inspect_chat(chat_id=8, raw=False)

    assert info.restricted is True
    assert info.restriction_reason == (
        {"platform": "all", "reason": "terms", "text": "violated ToS"},
    )


# --- legacy basic group -----------------------------------------------------


@pytest.mark.asyncio
async def test_basic_group_mapping() -> None:
    chat = Chat(
        9,
        title="Old",
        participants_count=4,
        deactivated=True,
        migrated_to=InputPeerChannelMigrated(500),
    )
    full = ChatFull(9, about="legacy", ttl_period=60)
    client = FakeClient(peer=InputPeerChat(9), result=FullChannelResult(full, [chat]))
    backend = TelethonChatInspectBackend(client)

    info = await backend.inspect_chat(chat_id=9, raw=False)

    assert info.kind == "basic_group"
    assert info.title == "Old"
    assert info.participants_count == 4
    assert info.deactivated is True
    assert info.migrated_to_chat_id == 500
    assert info.ttl_period == 60
    assert info.is_forum is False
    assert info.admins_count is None


# --- users and bots ---------------------------------------------------------


@pytest.mark.asyncio
async def test_user_mapping() -> None:
    user = User(
        11,
        first_name="Ann",
        last_name="Lee",
        username="annlee",
        phone="79990000000",
        premium=True,
        contact=True,
        status=UserStatusRecently(),
    )
    full = UserFull(
        11,
        about="bio",
        ttl_period=604800,
        blocked=True,
        common_chats_count=3,
        birthday=Birthday(4, 7, 1990),
    )
    client = FakeClient(peer=InputPeerUser(11), result=FullUserResult(full, [user]))
    backend = TelethonChatInspectBackend(client)

    info = await backend.inspect_chat(chat_id=11, raw=False)

    assert info.kind == "user"
    assert info.title == "Ann Lee"
    assert info.first_name == "Ann"
    assert info.last_name == "Lee"
    assert info.phone == "79990000000"
    assert info.is_premium is True
    assert info.is_contact is True
    assert info.blocked is True
    assert info.common_chats_count == 3
    assert info.birthday == {"day": 4, "month": 7, "year": 1990}
    assert info.ttl_period == 604800
    assert info.last_seen_status == "UserStatusRecently"


@pytest.mark.asyncio
async def test_bot_kind() -> None:
    user = User(12, first_name="Helper", bot=True)
    client = FakeClient(peer=InputPeerUser(12), result=FullUserResult(UserFull(12), [user]))
    backend = TelethonChatInspectBackend(client)

    info = await backend.inspect_chat(chat_id=12, raw=False)

    assert info.kind == "bot"
    assert info.is_bot is True


# --- raw --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_raw_carries_both_halves_without_access_hash() -> None:
    channel = Channel(5, title="Team")
    client = FakeClient(
        peer=InputPeerChannel(5), result=FullChannelResult(ChannelFull(5), [channel])
    )
    backend = TelethonChatInspectBackend(client)

    info = await backend.inspect_chat(chat_id=5, raw=True)

    assert set(info.raw) == {"entity", "full"}
    assert info.raw["full"]["_"] == "ChannelFull"
    assert info.raw["entity"]["title"] == "Team"
    assert "access_hash" not in info.raw["entity"]


@pytest.mark.asyncio
async def test_user_raw_strips_access_hash() -> None:
    user = User(11, first_name="Ann")
    client = FakeClient(peer=InputPeerUser(11), result=FullUserResult(UserFull(11), [user]))
    backend = TelethonChatInspectBackend(client)

    info = await backend.inspect_chat(chat_id=11, raw=True)

    assert "access_hash" not in info.raw["entity"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_chats_inspect_backend.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'telegram_assistant.chats.telethon_backend'`

- [ ] **Step 3: Write `src/telegram_assistant/chats/telethon_backend.py`**

```python
"""Telethon adapter for the chat-inspect op.

Two RPCs at most: ``get_input_entity`` to learn the peer kind, then one
``GetFull*`` request. The shallow half of the answer (``forum_tabs``,
``restriction_reason``, the rights defaults) is read out of that response's own
``chats``/``users`` list rather than a second ``get_entity`` — ``forum_tabs``
is a flag on ``Channel``, not on ``ChannelFull``, which is why
``groups/telethon_backend.py::get_topics_layout`` already resolves it that way.
"""

from __future__ import annotations

from typing import Any

from telegram_assistant.chats.service import ChatInfo
from telegram_assistant.telegram_client.errors import translate_flood_wait

#: Never leaves the process: a peer credential, not metadata.
_REDACTED_RAW_KEYS = frozenset({"access_hash"})


def _bare_id(chat_id: int) -> int:
    """Strip the ``-100`` supergroup marker, matching ``EntityRef.numeric_id``."""
    text = str(chat_id)
    if text.startswith("-100"):
        return int(text[4:])
    return abs(int(chat_id))


def _rights(raw: Any) -> dict[str, Any] | None:
    """Flag dict for ChatAdminRights / ChatBannedRights, minus the type tag."""
    if raw is None:
        return None
    to_dict = getattr(raw, "to_dict", None)
    if to_dict is None:
        return None
    return {k: v for k, v in to_dict().items() if k != "_"}


def _usernames(raw: Any) -> tuple[str, ...]:
    """Active alternative usernames only — an inactive one is not reachable."""
    return tuple(
        str(u.username)
        for u in (raw or ())
        if getattr(u, "username", None) and getattr(u, "active", True)
    )


def _restriction_reasons(raw: Any) -> tuple[dict[str, Any], ...]:
    return tuple(
        {
            "platform": getattr(r, "platform", None),
            "reason": getattr(r, "reason", None),
            "text": getattr(r, "text", None),
        }
        for r in (raw or ())
    )


def _reactions(raw: Any) -> Any:
    """``"all"``, ``"none"``, or the list of allowed emoticons."""
    if raw is None:
        return None
    name = type(raw).__name__
    if name == "ChatReactionsAll":
        return "all"
    if name == "ChatReactionsNone":
        return "none"
    return [
        getattr(r, "emoticon", None) or getattr(r, "document_id", None)
        for r in (getattr(raw, "reactions", ()) or ())
    ]


def _birthday(raw: Any) -> dict[str, Any] | None:
    if raw is None:
        return None
    return {
        "day": getattr(raw, "day", None),
        "month": getattr(raw, "month", None),
        "year": getattr(raw, "year", None),
    }


def _peer_id(raw: Any) -> int | None:
    """Read the numeric id off any InputPeer/Peer shape."""
    for attr in ("channel_id", "chat_id", "user_id"):
        value = getattr(raw, attr, None)
        if value is not None:
            return int(value)
    return None


def _serialize(raw: Any) -> dict[str, Any] | None:
    if raw is None:
        return None
    to_dict = getattr(raw, "to_dict", None)
    if to_dict is not None:
        payload = to_dict()
    else:
        payload = {
            k: v for k, v in vars(raw).items() if not k.startswith("_")
        }
    return {k: v for k, v in payload.items() if k not in _REDACTED_RAW_KEYS}


def _shallow_for(result: Any, full: Any, bucket: str) -> Any:
    """The shallow object matching ``full.id`` in ``result.<bucket>``.

    Telegram returns the peer alongside its Full object; match by id and fall
    back to the only entry when it returned just one.
    """
    items = list(getattr(result, bucket, None) or [])
    target_id = int(getattr(full, "id", 0) or 0)
    match = next((i for i in items if int(getattr(i, "id", 0) or 0) == target_id), None)
    if match is None and items:
        return items[0]
    return match


class TelethonChatInspectBackend:
    """Adapter from the Telethon ``TelegramClient`` to ``ChatInspectBackend``."""

    def __init__(self, client: Any) -> None:
        self._client = client

    async def inspect_chat(self, *, chat_id: int, raw: bool) -> ChatInfo:
        try:
            peer = await self._client.get_input_entity(chat_id)
        except Exception as exc:
            raise translate_flood_wait(exc) from exc

        kind = type(peer).__name__
        if kind == "InputPeerChannel":
            return await self._inspect_channel(peer, raw=raw)
        if kind == "InputPeerChat":
            return await self._inspect_basic_group(peer, raw=raw)
        if kind in {"InputPeerUser", "InputPeerSelf"}:
            return await self._inspect_user(peer, raw=raw)
        raise ValueError(f"chat {chat_id} cannot be inspected (resolved to {kind})")

    # --- per-kind branches --------------------------------------------------

    async def _inspect_channel(self, peer: Any, *, raw: bool) -> ChatInfo:
        from telethon.tl import functions

        try:
            result = await self._client(
                functions.channels.GetFullChannelRequest(channel=peer)
            )
        except Exception as exc:
            raise translate_flood_wait(exc) from exc

        full = getattr(result, "full_chat", None)
        entity = _shallow_for(result, full, "chats")
        broadcast = bool(getattr(entity, "broadcast", False))

        return ChatInfo(
            chat_id=int(getattr(full, "id", 0) or getattr(peer, "channel_id", 0)),
            kind="channel" if broadcast else "supergroup",
            title=getattr(entity, "title", None),
            username=getattr(entity, "username", None),
            usernames=_usernames(getattr(entity, "usernames", None)),
            about=getattr(full, "about", None) or None,
            created_at=getattr(entity, "date", None),
            ttl_period=getattr(full, "ttl_period", None),
            pinned_message_id=getattr(full, "pinned_msg_id", None),
            archived=getattr(full, "folder_id", None) == 1,
            muted=_is_muted(getattr(full, "notify_settings", None)),
            muted_until=getattr(
                getattr(full, "notify_settings", None), "mute_until", None
            ),
            has_scheduled=bool(getattr(full, "has_scheduled", False)),
            restricted=bool(getattr(entity, "restricted", False)),
            restriction_reason=_restriction_reasons(
                getattr(entity, "restriction_reason", None)
            ),
            verified=bool(getattr(entity, "verified", False)),
            scam=bool(getattr(entity, "scam", False)),
            fake=bool(getattr(entity, "fake", False)),
            is_creator=bool(getattr(entity, "creator", False)),
            left=bool(getattr(entity, "left", False)),
            invite_link=getattr(getattr(full, "exported_invite", None), "link", None),
            my_admin_rights=_rights(getattr(entity, "admin_rights", None)),
            default_banned_rights=_rights(
                getattr(entity, "default_banned_rights", None)
            ),
            is_forum=bool(getattr(entity, "forum", False)),
            topics_layout=(
                ("tabs" if getattr(entity, "forum_tabs", False) else "list")
                if getattr(entity, "forum", False)
                else None
            ),
            broadcast=broadcast,
            megagroup=bool(getattr(entity, "megagroup", False)),
            gigagroup=bool(getattr(entity, "gigagroup", False)),
            participants_count=getattr(full, "participants_count", None),
            admins_count=getattr(full, "admins_count", None),
            kicked_count=getattr(full, "kicked_count", None),
            banned_count=getattr(full, "banned_count", None),
            online_count=getattr(full, "online_count", None),
            slowmode_seconds=getattr(full, "slowmode_seconds", None),
            slowmode_next_send_date=getattr(full, "slowmode_next_send_date", None),
            linked_chat_id=getattr(full, "linked_chat_id", None),
            migrated_from_chat_id=getattr(full, "migrated_from_chat_id", None),
            hidden_prehistory=bool(getattr(full, "hidden_prehistory", False)),
            participants_hidden=bool(getattr(full, "participants_hidden", False)),
            antispam=bool(getattr(full, "antispam", False)),
            can_view_participants=bool(getattr(full, "can_view_participants", False)),
            can_view_stats=bool(getattr(full, "can_view_stats", False)),
            can_delete_channel=bool(getattr(full, "can_delete_channel", False)),
            can_set_username=bool(getattr(full, "can_set_username", False)),
            join_to_send=bool(getattr(entity, "join_to_send", False)),
            join_request=bool(getattr(entity, "join_request", False)),
            requests_pending=getattr(full, "requests_pending", None),
            noforwards=bool(getattr(entity, "noforwards", False)),
            unread_count=getattr(full, "unread_count", None),
            available_reactions=_reactions(getattr(full, "available_reactions", None)),
            reactions_limit=getattr(full, "reactions_limit", None),
            call_active=bool(getattr(entity, "call_active", False)),
            raw=(
                {"entity": _serialize(entity), "full": _serialize(full)}
                if raw
                else None
            ),
        )

    async def _inspect_basic_group(self, peer: Any, *, raw: bool) -> ChatInfo:
        from telethon.tl import functions

        try:
            result = await self._client(
                functions.messages.GetFullChatRequest(chat_id=peer.chat_id)
            )
        except Exception as exc:
            raise translate_flood_wait(exc) from exc

        full = getattr(result, "full_chat", None)
        entity = _shallow_for(result, full, "chats")

        return ChatInfo(
            chat_id=int(getattr(full, "id", 0) or getattr(peer, "chat_id", 0)),
            kind="basic_group",
            title=getattr(entity, "title", None),
            about=getattr(full, "about", None) or None,
            created_at=getattr(entity, "date", None),
            ttl_period=getattr(full, "ttl_period", None),
            pinned_message_id=getattr(full, "pinned_msg_id", None),
            archived=getattr(full, "folder_id", None) == 1,
            muted=_is_muted(getattr(full, "notify_settings", None)),
            muted_until=getattr(
                getattr(full, "notify_settings", None), "mute_until", None
            ),
            has_scheduled=bool(getattr(full, "has_scheduled", False)),
            is_creator=bool(getattr(entity, "creator", False)),
            left=bool(getattr(entity, "left", False)),
            invite_link=getattr(getattr(full, "exported_invite", None), "link", None),
            my_admin_rights=_rights(getattr(entity, "admin_rights", None)),
            default_banned_rights=_rights(
                getattr(entity, "default_banned_rights", None)
            ),
            participants_count=getattr(entity, "participants_count", None),
            deactivated=bool(getattr(entity, "deactivated", False)),
            migrated_to_chat_id=_peer_id(getattr(entity, "migrated_to", None)),
            can_set_username=bool(getattr(full, "can_set_username", False)),
            requests_pending=getattr(full, "requests_pending", None),
            noforwards=bool(getattr(entity, "noforwards", False)),
            available_reactions=_reactions(getattr(full, "available_reactions", None)),
            reactions_limit=getattr(full, "reactions_limit", None),
            call_active=bool(getattr(entity, "call_active", False)),
            raw=(
                {"entity": _serialize(entity), "full": _serialize(full)}
                if raw
                else None
            ),
        )

    async def _inspect_user(self, peer: Any, *, raw: bool) -> ChatInfo:
        from telethon.tl import functions

        try:
            result = await self._client(functions.users.GetFullUserRequest(id=peer))
        except Exception as exc:
            raise translate_flood_wait(exc) from exc

        full = getattr(result, "full_user", None)
        entity = _shallow_for(result, full, "users")
        first = getattr(entity, "first_name", None)
        last = getattr(entity, "last_name", None)
        title = " ".join(part for part in (first, last) if part) or None

        return ChatInfo(
            chat_id=int(getattr(full, "id", 0) or getattr(peer, "user_id", 0)),
            kind="bot" if getattr(entity, "bot", False) else "user",
            title=title,
            username=getattr(entity, "username", None),
            usernames=_usernames(getattr(entity, "usernames", None)),
            about=getattr(full, "about", None) or None,
            ttl_period=getattr(full, "ttl_period", None),
            pinned_message_id=getattr(full, "pinned_msg_id", None),
            archived=getattr(full, "folder_id", None) == 1,
            muted=_is_muted(getattr(full, "notify_settings", None)),
            muted_until=getattr(
                getattr(full, "notify_settings", None), "mute_until", None
            ),
            has_scheduled=bool(getattr(full, "has_scheduled", False)),
            restricted=bool(getattr(entity, "restricted", False)),
            restriction_reason=_restriction_reasons(
                getattr(entity, "restriction_reason", None)
            ),
            verified=bool(getattr(entity, "verified", False)),
            scam=bool(getattr(entity, "scam", False)),
            fake=bool(getattr(entity, "fake", False)),
            first_name=first,
            last_name=last,
            phone=getattr(entity, "phone", None),
            is_bot=bool(getattr(entity, "bot", False)),
            is_deleted=bool(getattr(entity, "deleted", False)),
            is_premium=bool(getattr(entity, "premium", False)),
            is_contact=bool(getattr(entity, "contact", False)),
            is_mutual_contact=bool(getattr(entity, "mutual_contact", False)),
            blocked=bool(getattr(full, "blocked", False)),
            common_chats_count=getattr(full, "common_chats_count", None),
            birthday=_birthday(getattr(full, "birthday", None)),
            personal_channel_id=getattr(full, "personal_channel_id", None),
            last_seen_status=(
                type(getattr(entity, "status", None)).__name__
                if getattr(entity, "status", None) is not None
                else None
            ),
            raw=(
                {"entity": _serialize(entity), "full": _serialize(full)}
                if raw
                else None
            ),
        )


def _is_muted(settings: Any) -> bool:
    """A chat is muted when ``silent`` is set or ``mute_until`` is populated."""
    if settings is None:
        return False
    if getattr(settings, "silent", False):
        return True
    return getattr(settings, "mute_until", None) is not None


__all__ = ["TelethonChatInspectBackend"]
```

- [ ] **Step 4: Run the tests**

Run: `.venv/bin/pytest tests/test_chats_inspect_backend.py -v`
Expected: PASS (9 tests)

If `translate_flood_wait` is not importable from `telegram_assistant.telegram_client.errors`, run `grep -rn "def translate_flood_wait" src/` and import it from where it actually lives — `groups/telethon_backend.py` uses the same helper.

- [ ] **Step 5: Lint**

Run: `.venv/bin/ruff check src/telegram_assistant/chats tests/test_chats_inspect_backend.py`
Expected: `All checks passed!`

- [ ] **Step 6: Commit**

```bash
git add src/telegram_assistant/chats/telethon_backend.py tests/test_chats_inspect_backend.py
git commit -m "feat(chats): map channel/basic-group/user metadata in the Telethon adapter"
```

---

### Task 3: CLI command `chats inspect`

**Files:**
- Modify: `src/telegram_assistant/cli/main.py` (insert the new section immediately before the `# --- messages ---` divider that precedes `messages_app = typer.Typer(...)`)
- Test: `tests/test_cli_chats_inspect.py`

**Interfaces:**
- Consumes: `inspect_chat` and `TelethonChatInspectBackend` from Tasks 1-2; existing CLI helpers `_load_config_or_exit`, `_resolve_folder_name`, `_cli_authorizer`, `_raise_for_access_or_entity_error`, `TelethonSessionManager`.
- Produces: `_build_chat_inspect_backends(config_path)` returning `(config, manager, _open)` where `_open()` yields `(chat_backend, folder_backend, resolver)`. Tests monkeypatch this symbol.

- [ ] **Step 1: Write the failing test**

Create `tests/test_cli_chats_inspect.py`:

```python
"""CLI tests for `chats inspect`."""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from telegram_assistant.access import AccessDenied
from telegram_assistant.chats import ChatInfo
from telegram_assistant.cli import main as cli_main
from telegram_assistant.entities import EntityNotFoundError

runner = CliRunner()


class FakeChatBackend:
    def __init__(self, info: ChatInfo | None = None, error: Exception | None = None):
        self.info = info or ChatInfo(chat_id=5, kind="supergroup", title="Team")
        self.error = error
        self.calls: list[dict[str, object]] = []

    async def inspect_chat(self, *, chat_id: int, raw: bool) -> ChatInfo:
        self.calls.append({"chat_id": chat_id, "raw": raw})
        if self.error is not None:
            raise self.error
        return self.info


class FakeResolved:
    def __init__(self, chat_id: int) -> None:
        self.chat_id = chat_id


class FakeResolver:
    def __init__(self, chat_id: int = 5, error: Exception | None = None) -> None:
        self.chat_id = chat_id
        self.error = error

    async def resolve(self, ref: str):
        if self.error is not None:
            raise self.error
        return FakeResolved(self.chat_id)


class FakeManager:
    async def disconnect(self) -> None:
        return None


@pytest.fixture
def wire(monkeypatch, minimal_config_yaml, tmp_path):
    """Patch the backend builder; return a helper that installs fakes."""

    config_path = tmp_path / "config.yml"
    config_path.write_text(minimal_config_yaml, encoding="utf-8")

    def _install(backend, resolver=None, authorizer=None):
        config = cli_main._load_config_or_exit(config_path)

        def _build(_path):
            async def _open():
                return backend, object(), resolver or FakeResolver()

            return config, FakeManager(), _open

        monkeypatch.setattr(cli_main, "_build_chat_inspect_backends", _build)
        if authorizer is not None:
            monkeypatch.setattr(cli_main, "_cli_authorizer", lambda *a, **k: authorizer)
        return config_path

    return _install


def test_requires_exactly_one_reference(wire):
    config_path = wire(FakeChatBackend())

    result = runner.invoke(
        cli_main.app, ["chats", "inspect", "--config", str(config_path)]
    )

    assert result.exit_code == 2
    assert "exactly one of --chat-id, --chat-name, or --entity" in result.output


def test_rejects_two_references(wire):
    config_path = wire(FakeChatBackend())

    result = runner.invoke(
        cli_main.app,
        [
            "chats", "inspect",
            "--chat-id", "5",
            "--entity", "@team",
            "--config", str(config_path),
        ],
    )

    assert result.exit_code == 2


def test_prints_payload_json(wire):
    backend = FakeChatBackend(
        ChatInfo(chat_id=5, kind="supergroup", title="Team", ttl_period=86400)
    )
    config_path = wire(backend)

    result = runner.invoke(
        cli_main.app,
        ["chats", "inspect", "--chat-id", "5", "--config", str(config_path)],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["chat_id"] == 5
    assert payload["kind"] == "supergroup"
    assert payload["ttl_period"] == 86400
    assert "raw" not in payload
    assert backend.calls == [{"chat_id": 5, "raw": False}]


def test_entity_reference_is_resolved(wire):
    backend = FakeChatBackend()
    config_path = wire(backend, resolver=FakeResolver(chat_id=77))

    result = runner.invoke(
        cli_main.app,
        ["chats", "inspect", "--entity", "@team", "--config", str(config_path)],
    )

    assert result.exit_code == 0, result.output
    assert backend.calls == [{"chat_id": 77, "raw": False}]


def test_raw_flag_is_passed_through(wire):
    backend = FakeChatBackend(
        ChatInfo(chat_id=5, kind="supergroup", raw={"entity": {}, "full": {}})
    )
    config_path = wire(backend)

    result = runner.invoke(
        cli_main.app,
        ["chats", "inspect", "--chat-id", "5", "--raw", "--config", str(config_path)],
    )

    assert result.exit_code == 0, result.output
    assert backend.calls == [{"chat_id": 5, "raw": True}]
    assert json.loads(result.output)["raw"] == {"entity": {}, "full": {}}


def test_access_denied_exits_3(wire):
    backend = FakeChatBackend(error=AccessDenied("chat 5 is not readable"))
    config_path = wire(backend)

    result = runner.invoke(
        cli_main.app,
        ["chats", "inspect", "--chat-id", "5", "--config", str(config_path)],
    )

    assert result.exit_code == 3
    assert "access denied" in result.output


def test_unresolvable_entity_exits_2(wire):
    config_path = wire(
        FakeChatBackend(), resolver=FakeResolver(error=EntityNotFoundError("no such chat"))
    )

    result = runner.invoke(
        cli_main.app,
        ["chats", "inspect", "--entity", "@ghost", "--config", str(config_path)],
    )

    assert result.exit_code == 2
    assert "no such chat" in result.output


def test_domain_value_error_exits_2(wire):
    backend = FakeChatBackend(error=ValueError("chat 5 cannot be inspected"))
    config_path = wire(backend)

    result = runner.invoke(
        cli_main.app,
        ["chats", "inspect", "--chat-id", "5", "--config", str(config_path)],
    )

    assert result.exit_code == 2
    assert "cannot be inspected" in result.output


def test_unexpected_error_exits_1(wire):
    backend = FakeChatBackend(error=RuntimeError("boom"))
    config_path = wire(backend)

    result = runner.invoke(
        cli_main.app,
        ["chats", "inspect", "--chat-id", "5", "--config", str(config_path)],
    )

    assert result.exit_code == 1
    assert "chats inspect failed: boom" in result.output
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_cli_chats_inspect.py -v`
Expected: FAIL — `AttributeError: module 'telegram_assistant.cli.main' has no attribute '_build_chat_inspect_backends'`

- [ ] **Step 3: Add the CLI section**

Insert into `src/telegram_assistant/cli/main.py`, immediately **before** the line `# --- messages ---------------------------------------------------------------`:

```python
# --- chats ------------------------------------------------------------------

chats_app = typer.Typer(
    help="Read chat metadata.", no_args_is_help=True
)
app.add_typer(chats_app, name="chats")


def _build_chat_inspect_backends(config_path: Path | None):
    """Open the Telethon-backed chat-inspect + folder backends + resolver.

    Mirrors :func:`_build_member_list_backends`: the read backend, the folder
    backend (for ``--chat-name`` and folder access rules) and a shared entity
    resolver so ``--entity`` works. Tests monkeypatch this to inject fakes.
    """
    config = _load_config_or_exit(config_path)
    manager = TelethonSessionManager(config.telegram)

    async def _open():
        from telegram_assistant.chats.telethon_backend import (
            TelethonChatInspectBackend,
        )
        from telegram_assistant.entities import TelethonEntityResolver
        from telegram_assistant.folders import TelethonFolderBackend

        client = await manager.get_client()
        if not await client.is_user_authorized():
            raise RuntimeError(
                "Telethon session is not authorized; run "
                "`telegram-assistant auth` first."
            )
        return (
            TelethonChatInspectBackend(client),
            TelethonFolderBackend(client),
            TelethonEntityResolver(client),
        )

    return config, manager, _open


@chats_app.command("inspect")
def chats_inspect(
    chat_id: int | None = typer.Option(
        None,
        "--chat-id",
        help="Numeric Telegram chat id to read.",
    ),
    chat_name: str | None = typer.Option(
        None,
        "--chat-name",
        help="Chat title (resolved within --folder-name).",
    ),
    entity: str | None = typer.Option(
        None,
        "--entity",
        help="Flexible entity reference (numeric id, @username, t.me/invite link, "
        "phone, or exact title) resolved via the shared resolver.",
    ),
    folder_name: str | None = typer.Option(
        None,
        "--folder-name",
        help="Folder used for --chat-name lookup "
        "(defaults to telegram.default_chat_folder.folder_name).",
    ),
    folder_id: int | None = typer.Option(
        None,
        "--folder-id",
        help="Optional folder id cross-check.",
    ),
    raw: bool = typer.Option(
        False,
        "--raw",
        help="Also include the serialized entity and Full objects under 'raw'.",
    ),
    config_path: Path | None = typer.Option(  # noqa: B008
        None,
        "--config",
        "-c",
        help="Path to config.yml (defaults: ./data/config.yml, then ~/.config/telegram-assistant/config.yml).",
        exists=False,
    ),
) -> None:
    """Read one chat's metadata: TTL, description, counts, rights (READ-gated)."""
    from telegram_assistant.chats import inspect_chat
    from telegram_assistant.folders import FolderError, resolve_chat_in_folder

    refs = sum([chat_id is not None, chat_name is not None, entity is not None])
    if refs != 1:
        typer.echo(
            "exactly one of --chat-id, --chat-name, or --entity must be supplied",
            err=True,
        )
        raise typer.Exit(code=2)

    config, manager, open_backends = _build_chat_inspect_backends(config_path)

    if chat_name is not None:
        resolved_folder_name, default_fid, _ = _resolve_folder_name(
            folder_name, config_path
        )
        effective_folder_id = folder_id if folder_id is not None else default_fid
    else:
        resolved_folder_name = folder_name
        effective_folder_id = folder_id

    async def _run() -> dict[str, object]:
        try:
            chat_backend, folder_backend, resolver = await open_backends()
            if entity is not None:
                resolved_chat_id = (await resolver.resolve(entity)).chat_id
            elif chat_id is not None:
                resolved_chat_id = chat_id
            else:
                resolved = await resolve_chat_in_folder(
                    folder_backend,
                    folder_name=resolved_folder_name or "",
                    chat_name=chat_name or "",
                    folder_id=effective_folder_id,
                )
                resolved_chat_id = resolved.chat_id

            authorizer = _cli_authorizer(
                config, resolver=resolver, folder_backend=folder_backend
            )
            info = await inspect_chat(
                backend=chat_backend,
                chat_id=resolved_chat_id,
                raw=raw,
                authorizer=authorizer,
            )
            return info.to_dict()
        finally:
            try:
                await manager.disconnect()
            except Exception:
                pass

    try:
        payload = asyncio.run(_run())
    except FolderError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc
    except ValueError as exc:
        # Bad caller input / an uninspectable peer — exit 2 like the rest of the
        # domain rejections. AccessDenied/EntityError are RuntimeErrors, so they
        # fall through to the mapping below.
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc
    except Exception as exc:
        _raise_for_access_or_entity_error(exc)
        typer.echo(f"chats inspect failed: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    typer.echo(json.dumps(payload, sort_keys=True, default=str))
```

- [ ] **Step 4: Run the tests**

Run: `.venv/bin/pytest tests/test_cli_chats_inspect.py -v`
Expected: PASS (9 tests)

If `CliRunner` merges stderr into `result.output` differently than the assertions expect, mirror whatever `tests/test_cli_groups_layout.py` does — do not weaken the exit-code assertions.

- [ ] **Step 5: Run the full suite to catch collateral damage**

Run: `.venv/bin/pytest -q`
Expected: PASS except `tests/test_skill_inventory.py`, which now fails with `chats inspect` missing from the SKILL.md catalog. That failure is expected and is fixed in Task 4.

- [ ] **Step 6: Lint**

Run: `.venv/bin/ruff check src tests`
Expected: `All checks passed!`

- [ ] **Step 7: Commit**

```bash
git add src/telegram_assistant/cli/main.py tests/test_cli_chats_inspect.py
git commit -m "feat(cli): add chats inspect"
```

---

### Task 4: Documentation — SKILL.md, README, skill sync

**Files:**
- Modify: `skills/telegram-assistant/SKILL.md`
- Modify: `README.md` (the CLI command bullet list, next to the `members list` entry around line 101)
- Modify: `CLAUDE.md` (the "Run the CLI" line, which enumerates commands)
- Copy: `~/.claude/skills/telegram-assistant/SKILL.md`

**Interfaces:**
- Consumes: the CLI surface from Task 3. No code changes.
- Produces: a green `tests/test_skill_inventory.py`.

- [ ] **Step 1: Confirm the guard is red for the right reason**

Run: `.venv/bin/pytest tests/test_skill_inventory.py -v`
Expected: FAIL naming `chats inspect` as a CLI command missing from the SKILL.md catalog.

- [ ] **Step 2: Add the catalog row to `skills/telegram-assistant/SKILL.md`**

In the `## Resources & actions` table, insert directly after the `members` / `list` row:

```markdown
| `chats` | `inspect` | Read-only: report one chat's metadata — auto-delete TTL, description, member counts, slow mode, restrictions, our own rights (READ-gated, no `--dry-run`). `--raw` adds the serialized entity/Full objects. | `telegram-assistant chats inspect ...` |
```

- [ ] **Step 3: Add the per-pair section**

Insert a new `#### \`chats\` / \`inspect\`` block directly after the `#### \`members\` / \`list\`` section:

```markdown
#### `chats` / `inspect`

- Extract: chat reference (`--chat-id` / `--chat-name` / `--entity`), optional
  `--raw`.
- Required flags: exactly one chat reference.
- From config: `--folder-name` default when resolving `--chat-name`.
- Temp file: no.
- Automation: read-only — run immediately when the human asks «какой статус
  автоудаления у чата X», «что за чат X», «сколько участников в X», «почему в X
  не отправляется». No `--dry-run` (there is none), no confirmation.
- Payload: one flat JSON object with the same keys for every chat kind —
  fields that do not apply are `null`. Always present: `chat_id` (bare id, no
  `-100`), `kind` (`user`/`bot`/`basic_group`/`supergroup`/`channel`), `title`,
  `username`, `about`, `ttl_period` (auto-delete, seconds; `null` = off),
  `pinned_message_id`, `archived`, `muted`/`muted_until`, `restricted` +
  `restriction_reason`, `is_creator`, `left`, `invite_link`,
  `my_admin_rights`, `default_banned_rights`. Groups and channels add
  `is_forum`, `topics_layout`, `participants_count`, `admins_count`,
  `kicked_count`, `banned_count`, `online_count`, `slowmode_seconds`,
  `linked_chat_id`, `hidden_prehistory`, `antispam`, `join_to_send`,
  `noforwards`, `available_reactions` and friends. Private chats add
  `first_name`/`last_name`, `phone`, `is_bot`, `is_premium`, `is_contact`,
  `blocked`, `common_chats_count`, `birthday`, `last_seen_status`.
- `--raw`: adds a `raw` key holding `{"entity": …, "full": …}` — the two
  serialized Telegram objects behind the curated fields, minus `access_hash`.
  Use it only when the human asks for a field the curated set does not name;
  it is large and its shape moves with the Telegram layer.
- Note it does **not** write anything: there is no way to *change* the TTL,
  the description or the archive state through this CLI. If the human asks to
  set auto-delete, say so plainly rather than reaching for another command.
- Confirmation: not required (read-only). Still READ-gated by the
  `telegram.access` policy — a chat with no `read` grant exits 3 with
  `access denied`; surface that and stop.
- Typical errors: `exactly one of --chat-id, --chat-name, or --entity must be
  supplied` (exit 2), `chat <id> cannot be inspected (resolved to ...)` (exit 2
  — the reference resolved to something with no metadata to read),
  `access denied ...` (exit 3), entity not-found / ambiguous (exit 2).
```

- [ ] **Step 4: Add the scenario**

Insert a `### \`chats inspect\`` scenario directly after the `### \`members list\`` scenario:

```markdown
### `chats inspect`

Request: «Какой статус автоудаления у чата 2305069221?»

1. Resource/action: `chats` / `inspect`. Read-only — run it immediately, no
   `--dry-run`, no confirmation.
2. Run:

   ```bash
   telegram-assistant chats inspect --entity 2305069221
   ```

3. Read `ttl_period` from the payload: `null` means auto-delete is off,
   otherwise it is the window in seconds (86400 = 1 day, 604800 = 1 week).
   Report the other fields only if the human asked for them — the payload is
   wide by design.
4. If the human then asks to *change* it, stop: this command only reads.
```

- [ ] **Step 5: Verify the guard is green**

Run: `.venv/bin/pytest tests/test_skill_inventory.py -v`
Expected: PASS

- [ ] **Step 6: Update `README.md`**

Add this bullet directly after the `members list` bullet (around line 101):

```markdown
- `chats inspect` — read-only: report one chat's metadata (READ-gated, no writes, no `--dry-run`). Target with `--chat-id`/`--chat-name`/`--entity`. Returns one flat JSON object with the same keys for every chat kind (`null` where a field does not apply): `ttl_period` (auto-delete window in seconds, `null` when off), `about`, `pinned_message_id`, `archived`, `muted`/`muted_until`, `restricted` + `restriction_reason`, `invite_link`, `my_admin_rights`, `default_banned_rights`, plus `is_forum`/`topics_layout`/`participants_count`/`admins_count`/`slowmode_seconds`/`linked_chat_id` for groups and channels and `phone`/`is_premium`/`blocked`/`common_chats_count`/`birthday` for private chats. Supergroups, channels, legacy basic groups, users and bots are all supported (one `GetFull*` request each). `--raw` adds the serialized entity and Full objects under `raw` for fields the curated set does not name; `access_hash` is never included. It reads only — there is no command to change any of these settings.
```

- [ ] **Step 7: Update the CLI command list in `CLAUDE.md`**

In the "Run the CLI" bullet under `## Common commands`, add `chats inspect` to the parenthesised list of examples, after `members list`.

- [ ] **Step 8: Sync the skill to the user skills directory**

```bash
cp skills/telegram-assistant/SKILL.md ~/.claude/skills/telegram-assistant/SKILL.md
```

- [ ] **Step 9: Run the full suite**

Run: `.venv/bin/pytest -q`
Expected: PASS (no failures; `tests/test_docker_image.py` may skip when Docker is unavailable)

- [ ] **Step 10: Commit**

```bash
git add skills/telegram-assistant/SKILL.md README.md CLAUDE.md
git commit -m "docs: document chats inspect in the skill, README and CLAUDE.md"
```

---

### Task 5: Live read-only verification

**Files:** none — this task produces a report, not a diff.

**Interfaces:**
- Consumes: the finished command from Tasks 1-4.

This is a **read-only** check against the live account, which the project's e2e rule permits without asking first. Do **not** run any mutating e2e script here.

- [ ] **Step 1: Inspect the chat that motivated the feature**

Run: `.venv/bin/telegram-assistant chats inspect --entity 2305069221`
Expected: a JSON object; note `kind`, `title`, and `ttl_period`.

- [ ] **Step 2: Inspect a forum supergroup**

Run: `.venv/bin/telegram-assistant chats inspect --entity "e2e test group"`
Expected: `kind` is `supergroup`, `is_forum` and `topics_layout` populated.

Cross-check the layout against the existing command — the two must agree:

Run: `.venv/bin/telegram-assistant groups get-layout --chat-id <the -100… id of that chat>`
Expected: the same word `chats inspect` reported in `topics_layout`.

- [ ] **Step 3: Inspect a private chat (Saved Messages)**

Run: `.venv/bin/telegram-assistant chats inspect --entity me`
Expected: `kind` is `user`, `title` is your own name, no crash on the missing group-only fields.

- [ ] **Step 4: Inspect a broadcast channel**

Pick any channel id from `.venv/bin/telegram-assistant folders inspect` and run:

Run: `.venv/bin/telegram-assistant chats inspect --chat-id <id>`
Expected: `kind` is `channel`, `broadcast` is `true`.

- [ ] **Step 5: Check `--raw` on one of them**

Run: `.venv/bin/telegram-assistant chats inspect --entity me --raw`
Expected: a `raw` key with `entity` and `full` sub-objects, and no `access_hash` anywhere:

Run: `.venv/bin/telegram-assistant chats inspect --entity me --raw | grep -c access_hash`
Expected: `0`

- [ ] **Step 6: Report the results to the human**

Post the four `kind`/`ttl_period` pairs and flag any field that came back `null` where it should not have. Phase 2 (HTTP + MCP) starts only after the human has looked at this output.

---

## Self-Review

**Spec coverage:**

| Spec section | Task |
|---|---|
| `chats/` package, `service.py`, `ChatInfo`, protocol, `inspect_chat()` | 1 |
| READ gate before any RPC | 1 (tested), 3 (wired) |
| Telethon adapter, three peer kinds, shallow half from the Full response | 2 |
| Flat payload, curated field list, per-kind extras | 1 (shape), 2 (population) |
| `--raw` with both halves, `access_hash` stripped | 2 |
| CLI flags, reference exclusivity, folder defaults, JSON output | 3 |
| Error ladder (2 / 3 / 1) | 3 |
| Three test files mirroring `test_members_list*` | 1, 2, 3 |
| SKILL.md + README + skill sync + inventory guard | 4 |
| Live read-only verification across peer kinds | 5 |
| Phase 2 (HTTP/MCP) | out of scope — a separate plan |

**Deviation from the spec, deliberate:** the spec's architecture paragraph says the adapter "resolves the peer once via `get_entity`". The plan uses `get_input_entity` instead, because the `GetFull*` response already returns the shallow `Channel`/`Chat`/`User` in its own `chats`/`users` list — the same trick `get_topics_layout` uses — so `get_entity` would be a redundant round trip. The observable payload is unchanged.

**Type consistency:** `inspect_chat(*, backend, chat_id, raw, authorizer)` in Task 1 matches every call site in Tasks 2-3; `ChatInspectBackend.inspect_chat(*, chat_id, raw)` matches `TelethonChatInspectBackend.inspect_chat` and both fakes; `_build_chat_inspect_backends` returns the `(config, manager, _open)` triple the CLI and the CLI test both assume.
