# `members list` Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a read-only `members list` operation — list a chat's participants, or answer "is this one user a member" in a single RPC — across the domain layer, CLI, HTTP and MCP.

**Architecture:** A new domain module `members/listing.py` holds the dataclasses, the `MemberListBackend` protocol and the `list_members()` op (READ-gated, no operation row, no `--dry-run`); `TelethonMemberListBackend` in the existing `members/telethon_backend.py` translates it into `channels.GetParticipants` / `channels.GetParticipant`, falling back to `messages.GetFullChat` for legacy basic groups. The three surfaces mirror `messages recent` exactly: CLI command, `GET /telegram/members/list`, MCP tool `telegram_members_list`.

**Tech Stack:** Python 3.12, Telethon ≥ 1.44, FastAPI, Typer, FastMCP, pytest (asyncio auto mode), ruff.

**Spec:** `docs/superpowers/specs/2026-07-28-members-list-design.md`

## Global Constraints

- Virtualenv is `.venv`; run everything through it (`source .venv/bin/activate` — the pre-commit hook needs it or `git commit` fails with "`pre-commit` not found").
- Lint: `ruff check src tests` (line-length 100, py312, `select = ["E","F","W","I","B","UP"]`, ignores E501). `filter` as a parameter name is fine — flake8-builtins is not enabled.
- Tests: `pytest`, asyncio mode auto — async tests need `@pytest.mark.asyncio`.
- No real Telegram traffic in `tests/` — inject fakes.
- Telethon is imported **lazily inside methods** in backend adapters, never at module load.
- Error taxonomy, unchanged: `AccessDenied` → HTTP 403 / CLI exit 3; entity not found → 404 / exit 2; `ValueError` → 400 / exit 2. The CLI reaches this via `_raise_for_access_or_entity_error(exc)`.
- The domain layer stays Telethon-free: `members/listing.py` imports nothing from telethon.
- Every surface change must land with its docs (`README.md`, `skills/telegram-assistant/SKILL.md` + re-sync) — `tests/test_skill_inventory.py` fails otherwise.

---

### Task 1: Domain module `members/listing.py`

**Files:**
- Create: `src/telegram_assistant/members/listing.py`
- Modify: `src/telegram_assistant/members/__init__.py` (re-export the new names)
- Test: `tests/test_members_list.py`

**Interfaces:**
- Consumes: `telegram_assistant.access.service.{AccessLevel, Authorizer}`.
- Produces: `Participant`, `MemberListResult`, `MemberListBackend`, `list_members`, `DEFAULT_MEMBER_LIST_LIMIT = 200`, `VALID_MEMBER_FILTERS = frozenset({"all","admins","bots"})`, `NON_MEMBER_ROLES = frozenset({"left","banned"})` — all re-exported from `telegram_assistant.members`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_members_list.py`:

```python
"""Tests for the members-list READ op.

``list_members`` is the read counterpart to the bulk add/remove ops: it is
gated behind READ-level authorization (denied before any backend call), takes
either a listing request or a single-``user`` membership check, and never
opens an operation row.
"""

from __future__ import annotations

import pytest

from telegram_assistant.access import AccessDenied, AccessLevel, Authorizer
from telegram_assistant.config.models import AccessConfig, AccessRule
from telegram_assistant.entities import ResolvedEntity
from telegram_assistant.members import (
    MemberListResult,
    Participant,
    list_members,
)


class FakeResolver:
    def __init__(self, mapping: dict[object, int]) -> None:
        self._mapping = mapping

    async def resolve(self, ref: object) -> ResolvedEntity:
        return ResolvedEntity(chat_id=self._mapping[ref], title=str(ref), kind="channel")


class FakeListBackend:
    """In-memory MemberListBackend recording every call."""

    def __init__(
        self,
        participants: list[Participant] | None = None,
        *,
        found: Participant | None = None,
        total: int | None = None,
    ) -> None:
        self._participants = participants or []
        self._found = found
        self._total = total
        self.list_calls: list[dict[str, object]] = []
        self.get_calls: list[dict[str, object]] = []

    async def list_participants(
        self, *, chat_id: int, limit: int, query: str | None, filter: str
    ) -> MemberListResult:
        self.list_calls.append(
            {"chat_id": chat_id, "limit": limit, "query": query, "filter": filter}
        )
        selected = self._participants[:limit]
        total = self._total if self._total is not None else len(self._participants)
        return MemberListResult(
            participants=tuple(selected),
            participants_count=total,
            truncated=len(selected) < total,
        )

    async def get_participant(self, *, chat_id: int, user: str) -> Participant | None:
        self.get_calls.append({"chat_id": chat_id, "user": user})
        return self._found


def _participant(uid: int, *, role: str = "member", is_bot: bool = False) -> Participant:
    return Participant(
        user_id=uid,
        username=f"user{uid}",
        first_name=f"First{uid}",
        last_name=None,
        is_bot=is_bot,
        role=role,
    )


@pytest.mark.asyncio
async def test_list_members_defaults_to_limit_200_and_filter_all() -> None:
    backend = FakeListBackend([_participant(i) for i in range(1, 4)])
    result = await list_members(backend=backend, chat_id=42)
    assert backend.list_calls == [
        {"chat_id": 42, "limit": 200, "query": None, "filter": "all"}
    ]
    assert len(result.participants) == 3
    assert result.participants_count == 3
    assert result.truncated is False
    assert result.is_member is None


@pytest.mark.asyncio
async def test_list_members_reports_truncation() -> None:
    backend = FakeListBackend([_participant(i) for i in range(1, 6)], total=99)
    result = await list_members(backend=backend, chat_id=42, limit=2)
    assert len(result.participants) == 2
    assert result.participants_count == 99
    assert result.truncated is True


@pytest.mark.asyncio
async def test_list_members_passes_query_and_filter() -> None:
    backend = FakeListBackend([_participant(1, is_bot=True)])
    await list_members(backend=backend, chat_id=42, query="press", filter="bots")
    assert backend.list_calls[-1]["query"] == "press"
    assert backend.list_calls[-1]["filter"] == "bots"


@pytest.mark.asyncio
async def test_list_members_rejects_nonpositive_limit() -> None:
    backend = FakeListBackend()
    with pytest.raises(ValueError):
        await list_members(backend=backend, chat_id=42, limit=0)
    assert backend.list_calls == []


@pytest.mark.asyncio
async def test_list_members_rejects_unknown_filter() -> None:
    backend = FakeListBackend()
    with pytest.raises(ValueError):
        await list_members(backend=backend, chat_id=42, filter="kicked")
    assert backend.list_calls == []


@pytest.mark.asyncio
async def test_list_members_rejects_user_with_query() -> None:
    backend = FakeListBackend()
    with pytest.raises(ValueError):
        await list_members(backend=backend, chat_id=42, user="@bot", query="bot")
    assert backend.get_calls == []
    assert backend.list_calls == []


@pytest.mark.asyncio
async def test_user_mode_reports_membership() -> None:
    backend = FakeListBackend(found=_participant(7, role="member", is_bot=True))
    result = await list_members(backend=backend, chat_id=42, user="@pressfinity_news_bot")
    assert backend.get_calls == [{"chat_id": 42, "user": "@pressfinity_news_bot"}]
    assert backend.list_calls == []
    assert result.is_member is True
    assert result.requested_user == "@pressfinity_news_bot"
    assert result.participants[0].user_id == 7
    assert result.to_dict()["is_member"] is True
    assert result.to_dict()["user"] == "@pressfinity_news_bot"


@pytest.mark.asyncio
async def test_user_mode_reports_absence() -> None:
    backend = FakeListBackend(found=None)
    result = await list_members(backend=backend, chat_id=42, user="@nope")
    assert result.is_member is False
    assert result.participants == ()
    assert result.participants_count is None


@pytest.mark.asyncio
async def test_user_mode_left_or_banned_is_not_a_member() -> None:
    # `channels.GetParticipant` answers for users who left or were banned too;
    # counting them as members would defeat the membership check.
    for role in ("left", "banned"):
        backend = FakeListBackend(found=_participant(7, role=role))
        result = await list_members(backend=backend, chat_id=42, user="@gone")
        assert result.is_member is False, role
        assert result.participants[0].role == role


@pytest.mark.asyncio
async def test_plain_list_payload_omits_user_keys() -> None:
    backend = FakeListBackend([_participant(1)])
    result = await list_members(backend=backend, chat_id=42)
    payload = result.to_dict()
    assert "user" not in payload
    assert "is_member" not in payload
    assert payload["count"] == 1
    assert payload["participants"][0]["username"] == "user1"


@pytest.mark.asyncio
async def test_read_rule_allows_listing() -> None:
    backend = FakeListBackend([_participant(1)])
    config = AccessConfig(rules=[AccessRule(chat="@team", permission="read")])
    authorizer = Authorizer(config, resolver=FakeResolver({"@team": 42}))
    result = await list_members(backend=backend, chat_id=42, authorizer=authorizer)
    assert len(result.participants) == 1


@pytest.mark.asyncio
async def test_write_only_rule_denies_listing() -> None:
    backend = FakeListBackend([_participant(1)])
    config = AccessConfig(rules=[AccessRule(chat="@team", permission="write")])
    authorizer = Authorizer(config, resolver=FakeResolver({"@team": 42}))
    with pytest.raises(AccessDenied) as excinfo:
        await list_members(backend=backend, chat_id=42, authorizer=authorizer)
    assert excinfo.value.required_level is AccessLevel.READ
    assert backend.list_calls == []


@pytest.mark.asyncio
async def test_user_mode_is_read_gated_before_any_rpc() -> None:
    backend = FakeListBackend(found=_participant(7))
    config = AccessConfig(rules=[AccessRule(chat="@team", permission="read")])
    authorizer = Authorizer(config, resolver=FakeResolver({"@team": 42}))
    with pytest.raises(AccessDenied):
        await list_members(backend=backend, chat_id=999, user="@bot", authorizer=authorizer)
    assert backend.get_calls == []
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/test_members_list.py -q`
Expected: collection error / FAIL — `ImportError: cannot import name 'list_members' from 'telegram_assistant.members'`.

- [ ] **Step 3: Write `src/telegram_assistant/members/listing.py`**

```python
"""Read-only participants listing — the READ op of the members domain.

Kept out of :mod:`telegram_assistant.members.service` (which is ~1150 lines of
bulk add/remove queue logic) the same way ``messages/`` splits ``search.py``
out of its own ``service.py``: this op opens no operation row, has no
idempotency key and no ``--dry-run``. It answers two questions with one shape —
"who is in this chat" (paginated, filtered) and, with ``user``, "is this one
user in this chat" (a single RPC, which is what makes a sweep over dozens of
chats affordable).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from telegram_assistant.access.service import AccessLevel, Authorizer

#: Default page/limit for a listing. Telegram serves participants 200 at a time.
DEFAULT_MEMBER_LIST_LIMIT = 200

#: Filters a caller may ask for. ``all`` uses Telegram's search filter (which is
#: what supports full enumeration); ``admins``/``bots`` use the dedicated ones.
VALID_MEMBER_FILTERS: frozenset[str] = frozenset({"all", "admins", "bots"})

#: Roles Telegram still answers for, but which are *not* current membership.
NON_MEMBER_ROLES: frozenset[str] = frozenset({"left", "banned"})


@dataclass(frozen=True)
class Participant:
    """One chat participant.

    ``role`` is one of ``creator``, ``admin``, ``member``, ``restricted``,
    ``left`` or ``banned``. ``username`` and the name fields are ``None`` when
    Telegram did not supply the user object.
    """

    user_id: int
    username: str | None
    first_name: str | None
    last_name: str | None
    is_bot: bool
    role: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "user_id": self.user_id,
            "username": self.username,
            "first_name": self.first_name,
            "last_name": self.last_name,
            "is_bot": self.is_bot,
            "role": self.role,
        }


@dataclass(frozen=True)
class MemberListResult:
    """Outcome of :func:`list_members`.

    ``participants_count`` is what the chat reports as its total (``None`` when
    unknown, e.g. in ``user`` mode). ``truncated`` says the walk stopped before
    exhausting the chat — because ``limit`` was reached or because Telegram
    stopped serving pages (its ~10k full-enumeration ceiling).
    """

    participants: tuple[Participant, ...]
    participants_count: int | None = None
    truncated: bool = False
    requested_user: str | None = None

    @property
    def is_member(self) -> bool | None:
        """``None`` unless a single ``user`` was asked about.

        A user who left or was banned is *not* a member: Telegram answers
        ``GetParticipant`` for them too, and reporting them as present would
        defeat the membership check this op exists for.
        """
        if self.requested_user is None:
            return None
        return any(p.role not in NON_MEMBER_ROLES for p in self.participants)

    def to_dict(self) -> dict[str, Any]:
        """The payload body shared by the CLI, HTTP and MCP surfaces.

        The ``user``/``is_member`` keys appear only in ``user`` mode, so a plain
        listing keeps its shape.
        """
        payload: dict[str, Any] = {
            "count": len(self.participants),
            "participants": [p.to_dict() for p in self.participants],
            "participants_count": self.participants_count,
            "truncated": self.truncated,
        }
        if self.requested_user is not None:
            payload["user"] = self.requested_user
            payload["is_member"] = self.is_member
        return payload


class MemberListBackend(Protocol):
    """Telethon-facing surface needed to read a chat's participants.

    Production wires this to :class:`TelethonMemberListBackend`; tests inject a
    fake.
    """

    async def list_participants(
        self, *, chat_id: int, limit: int, query: str | None, filter: str
    ) -> MemberListResult:
        ...

    async def get_participant(
        self, *, chat_id: int, user: str
    ) -> Participant | None:
        ...


async def list_members(
    *,
    backend: MemberListBackend,
    chat_id: int,
    limit: int = DEFAULT_MEMBER_LIST_LIMIT,
    query: str | None = None,
    filter: str = "all",
    user: str | None = None,
    authorizer: Authorizer | None = None,
) -> MemberListResult:
    """List participants of ``chat_id``, or check one ``user``'s membership.

    A READ op: when an ``authorizer`` is supplied it must grant READ on the
    chat, checked before any Telegram call. ``user`` short-circuits the walk
    into a single ``GetParticipant`` and is mutually exclusive with ``query``
    (they answer different questions); ``filter`` is ignored in that mode.
    """
    if limit <= 0:
        raise ValueError("list_members requires a positive limit")
    if filter not in VALID_MEMBER_FILTERS:
        raise ValueError(
            f"unknown filter {filter!r}; expected one of "
            f"{', '.join(sorted(VALID_MEMBER_FILTERS))}"
        )
    if user is not None and query is not None:
        raise ValueError("user and query are mutually exclusive")
    if user is not None and not user.strip():
        raise ValueError("user reference must not be empty")

    if authorizer is not None:
        await authorizer.require(chat_id, AccessLevel.READ)

    if user is not None:
        participant = await backend.get_participant(chat_id=chat_id, user=user)
        return MemberListResult(
            participants=() if participant is None else (participant,),
            participants_count=None,
            truncated=False,
            requested_user=user,
        )

    return await backend.list_participants(
        chat_id=chat_id, limit=limit, query=query, filter=filter
    )


__all__ = [
    "DEFAULT_MEMBER_LIST_LIMIT",
    "NON_MEMBER_ROLES",
    "VALID_MEMBER_FILTERS",
    "MemberListBackend",
    "MemberListResult",
    "Participant",
    "list_members",
]
```

- [ ] **Step 4: Re-export from `src/telegram_assistant/members/__init__.py`**

Add a second import block after the existing `from telegram_assistant.members.service import (...)` block, and add the names to `__all__` (keep it alphabetically sorted, as it is today):

```python
from telegram_assistant.members.listing import (
    DEFAULT_MEMBER_LIST_LIMIT,
    NON_MEMBER_ROLES,
    VALID_MEMBER_FILTERS,
    MemberListBackend,
    MemberListResult,
    Participant,
    list_members,
)
```

`__all__` gains: `"DEFAULT_MEMBER_LIST_LIMIT"`, `"MemberListBackend"`, `"MemberListResult"`, `"NON_MEMBER_ROLES"`, `"Participant"`, `"VALID_MEMBER_FILTERS"`, `"list_members"`.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_members_list.py -q && .venv/bin/ruff check src tests`
Expected: all tests PASS, ruff clean.

- [ ] **Step 6: Commit**

```bash
source .venv/bin/activate
git add src/telegram_assistant/members/listing.py src/telegram_assistant/members/__init__.py tests/test_members_list.py
git commit -m "feat(members): add read-only list_members domain op"
```

---

### Task 2: Telethon adapter `TelethonMemberListBackend`

**Files:**
- Modify: `src/telegram_assistant/members/telethon_backend.py` (append the new class + module-private helpers; reuse the existing `_classify_rpc_error`)
- Test: `tests/test_members_list_backend.py`

**Interfaces:**
- Consumes: `Participant`, `MemberListResult` from Task 1; the existing `_classify_rpc_error` and `coerce_user_ref` already imported in that module.
- Produces: `TelethonMemberListBackend(client)` implementing `list_participants(*, chat_id, limit, query, filter)` and `get_participant(*, chat_id, user)`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_members_list_backend.py`. The fake client mimics the Telethon surface the adapter touches — `get_input_entity` and `__call__(request)` — and fake Telethon types are plain objects whose **class names** match the real ones (the adapter dispatches on `type(x).__name__` so it never imports telethon at module load):

```python
"""Tests for the Telethon members-list adapter.

The adapter is exercised against a fake client: participant classes are stand-ins
whose class *names* match Telethon's, because that is what the role mapping and
the peer dispatch key on.
"""

from __future__ import annotations

import pytest

from telegram_assistant.members.telethon_backend import TelethonMemberListBackend


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


class User:
    def __init__(self, uid: int, username=None, bot=False, first="F", last=None) -> None:
        self.id = uid
        self.username = username
        self.bot = bot
        self.first_name = first
        self.last_name = last


class ChannelParticipant:
    def __init__(self, user_id: int) -> None:
        self.user_id = user_id


class ChannelParticipantAdmin(ChannelParticipant):
    pass


class ChannelParticipantCreator(ChannelParticipant):
    pass


class ChannelParticipantLeft:
    def __init__(self, user_id: int) -> None:
        self.peer = InputPeerUser(user_id)


class ChannelParticipantBanned:
    def __init__(self, user_id: int, *, left: bool) -> None:
        self.peer = InputPeerUser(user_id)
        self.left = left


class ChatParticipant:
    def __init__(self, user_id: int) -> None:
        self.user_id = user_id


class ChatParticipantAdmin(ChatParticipant):
    pass


class ChatParticipantCreator(ChatParticipant):
    pass


class Page:
    """Stand-in for ``channels.ChannelParticipants``."""

    def __init__(self, participants, users, count) -> None:
        self.participants = participants
        self.users = users
        self.count = count


class FullChatResult:
    def __init__(self, participants, users) -> None:
        self.full_chat = type("FullChat", (), {"participants": type(
            "Container", (), {"participants": participants})()})()
        self.users = users


class UserNotParticipantError(Exception):
    """Name-matched by the adapter's error classifier."""


class FakeClient:
    """Records requests and replays canned results, keyed by request class name."""

    def __init__(self, *, peer, pages=None, full_chat=None, participant=None,
                 participant_error=None, user_peer=None) -> None:
        self._peer = peer
        self._pages = list(pages or [])
        self._full_chat = full_chat
        self._participant = participant
        self._participant_error = participant_error
        self._user_peer = user_peer
        self.requests: list[object] = []

    async def get_input_entity(self, ref):
        if isinstance(ref, int) or self._user_peer is None:
            return self._peer
        return self._user_peer

    async def __call__(self, request):
        self.requests.append(request)
        name = type(request).__name__
        if name == "GetParticipantsRequest":
            if not self._pages:
                return Page([], [], 0)
            return self._pages.pop(0)
        if name == "GetFullChatRequest":
            return self._full_chat
        if name == "GetParticipantRequest":
            if self._participant_error is not None:
                raise self._participant_error
            return self._participant
        raise AssertionError(f"unexpected request {name}")


# --- listing ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_channel_listing_pages_until_limit() -> None:
    users = [User(i, username=f"u{i}") for i in range(1, 6)]
    pages = [
        Page([ChannelParticipant(1), ChannelParticipant(2)], users[:2], 5),
        Page([ChannelParticipant(3), ChannelParticipant(4)], users[2:4], 5),
    ]
    client = FakeClient(peer=InputPeerChannel(7), pages=pages)
    backend = TelethonMemberListBackend(client)

    result = await backend.list_participants(
        chat_id=-1007, limit=4, query=None, filter="all"
    )

    assert [p.user_id for p in result.participants] == [1, 2, 3, 4]
    assert result.participants_count == 5
    assert result.truncated is True
    assert len(client.requests) == 2
    assert client.requests[1].offset == 2


@pytest.mark.asyncio
async def test_channel_listing_stops_on_empty_page_and_is_not_truncated() -> None:
    users = [User(1, username="u1")]
    pages = [Page([ChannelParticipant(1)], users, 1)]
    client = FakeClient(peer=InputPeerChannel(7), pages=pages)
    backend = TelethonMemberListBackend(client)

    result = await backend.list_participants(
        chat_id=-1007, limit=200, query=None, filter="all"
    )

    assert [p.user_id for p in result.participants] == [1]
    assert result.truncated is False


@pytest.mark.asyncio
async def test_channel_roles_are_mapped() -> None:
    users = [User(i, username=f"u{i}") for i in range(1, 6)]
    raw = [
        ChannelParticipantCreator(1),
        ChannelParticipantAdmin(2),
        ChannelParticipant(3),
        ChannelParticipantBanned(4, left=True),
        ChannelParticipantBanned(5, left=False),
    ]
    client = FakeClient(peer=InputPeerChannel(7), pages=[Page(raw, users, 5)])
    backend = TelethonMemberListBackend(client)

    result = await backend.list_participants(
        chat_id=-1007, limit=200, query=None, filter="all"
    )

    assert [p.role for p in result.participants] == [
        "creator",
        "admin",
        "member",
        "banned",
        "restricted",
    ]


@pytest.mark.asyncio
async def test_query_is_applied_locally_for_admins_filter() -> None:
    # ChannelParticipantsAdmins has no server-side search, so the adapter
    # filters the returned rows itself.
    users = [User(1, username="alice"), User(2, username="bob")]
    client = FakeClient(
        peer=InputPeerChannel(7),
        pages=[Page([ChannelParticipantAdmin(1), ChannelParticipantAdmin(2)], users, 2)],
    )
    backend = TelethonMemberListBackend(client)

    result = await backend.list_participants(
        chat_id=-1007, limit=200, query="ali", filter="admins"
    )

    assert [p.username for p in result.participants] == ["alice"]
    assert type(client.requests[0].filter).__name__ == "ChannelParticipantsAdmins"


@pytest.mark.asyncio
async def test_all_filter_uses_server_side_search() -> None:
    client = FakeClient(peer=InputPeerChannel(7), pages=[Page([], [], 0)])
    backend = TelethonMemberListBackend(client)

    await backend.list_participants(chat_id=-1007, limit=10, query="press", filter="all")

    sent = client.requests[0].filter
    assert type(sent).__name__ == "ChannelParticipantsSearch"
    assert sent.q == "press"


@pytest.mark.asyncio
async def test_basic_group_falls_back_to_get_full_chat() -> None:
    users = [User(1, username="alice"), User(2, username="botty", bot=True)]
    full = FullChatResult([ChatParticipantCreator(1), ChatParticipant(2)], users)
    client = FakeClient(peer=InputPeerChat(55), full_chat=full)
    backend = TelethonMemberListBackend(client)

    result = await backend.list_participants(
        chat_id=-55, limit=200, query=None, filter="all"
    )

    assert type(client.requests[0]).__name__ == "GetFullChatRequest"
    assert [(p.user_id, p.role) for p in result.participants] == [(1, "creator"), (2, "member")]
    assert result.participants_count == 2
    assert result.truncated is False


@pytest.mark.asyncio
async def test_basic_group_applies_bots_filter_locally() -> None:
    users = [User(1, username="alice"), User(2, username="botty", bot=True)]
    full = FullChatResult([ChatParticipant(1), ChatParticipant(2)], users)
    client = FakeClient(peer=InputPeerChat(55), full_chat=full)
    backend = TelethonMemberListBackend(client)

    result = await backend.list_participants(
        chat_id=-55, limit=200, query=None, filter="bots"
    )

    assert [p.user_id for p in result.participants] == [2]


@pytest.mark.asyncio
async def test_user_peer_is_rejected() -> None:
    client = FakeClient(peer=InputPeerUser(9))
    backend = TelethonMemberListBackend(client)

    with pytest.raises(ValueError):
        await backend.list_participants(chat_id=9, limit=10, query=None, filter="all")


# --- single-user check ------------------------------------------------------


@pytest.mark.asyncio
async def test_get_participant_returns_the_member() -> None:
    found = type("ChannelParticipantResult", (), {})()
    found.participant = ChannelParticipantAdmin(7)
    found.users = [User(7, username="pressfinity_news_bot", bot=True)]
    client = FakeClient(
        peer=InputPeerChannel(3), participant=found, user_peer=InputPeerUser(7)
    )
    backend = TelethonMemberListBackend(client)

    participant = await backend.get_participant(chat_id=-1003, user="@pressfinity_news_bot")

    assert participant is not None
    assert participant.user_id == 7
    assert participant.role == "admin"
    assert participant.is_bot is True


@pytest.mark.asyncio
async def test_get_participant_returns_none_when_absent() -> None:
    client = FakeClient(
        peer=InputPeerChannel(3),
        participant_error=UserNotParticipantError("USER_NOT_PARTICIPANT"),
        user_peer=InputPeerUser(7),
    )
    backend = TelethonMemberListBackend(client)

    assert await backend.get_participant(chat_id=-1003, user="@ghost") is None


@pytest.mark.asyncio
async def test_get_participant_scans_basic_group_roster() -> None:
    users = [User(1, username="alice"), User(7, username="botty", bot=True)]
    full = FullChatResult([ChatParticipant(1), ChatParticipantAdmin(7)], users)
    client = FakeClient(peer=InputPeerChat(55), full_chat=full, user_peer=InputPeerUser(7))
    backend = TelethonMemberListBackend(client)

    participant = await backend.get_participant(chat_id=-55, user="@botty")

    assert participant is not None
    assert participant.role == "admin"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/test_members_list_backend.py -q`
Expected: FAIL — `ImportError: cannot import name 'TelethonMemberListBackend'`.

- [ ] **Step 3: Implement the adapter**

Append to `src/telegram_assistant/members/telethon_backend.py`. First extend the module imports at the top (`from telegram_assistant.members.listing import MemberListResult, Participant`), then add:

```python
#: Telegram serves participants 200 at a time; a wider page is silently clamped.
_PARTICIPANTS_PAGE_SIZE = 200

#: Legacy basic groups top out around 200 members — one GetFullChat is enough.
_BASIC_CHAT_ROSTER_CAP = 1000


def _participant_user_id(raw: Any) -> int | None:
    """Read a participant's user id.

    Telegram spells it ``user_id`` on the plain/admin/creator classes and
    ``peer`` (a ``PeerUser``) on the left/banned ones.
    """
    user_id = getattr(raw, "user_id", None)
    if user_id is not None:
        return int(user_id)
    peer = getattr(raw, "peer", None)
    peer_id = getattr(peer, "user_id", None)
    return int(peer_id) if peer_id is not None else None


def _channel_role(raw: Any) -> str:
    name = type(raw).__name__
    if name == "ChannelParticipantCreator":
        return "creator"
    if name == "ChannelParticipantAdmin":
        return "admin"
    if name == "ChannelParticipantBanned":
        # ``left`` means kicked out; otherwise the user is still in the chat
        # under restrictions.
        return "banned" if getattr(raw, "left", False) else "restricted"
    if name == "ChannelParticipantLeft":
        return "left"
    return "member"


def _chat_role(raw: Any) -> str:
    name = type(raw).__name__
    if name == "ChatParticipantCreator":
        return "creator"
    if name == "ChatParticipantAdmin":
        return "admin"
    return "member"


def _to_participant(raw: Any, users: dict[int, Any], role: str) -> Participant | None:
    user_id = _participant_user_id(raw)
    if user_id is None:
        return None
    user = users.get(user_id)
    return Participant(
        user_id=user_id,
        username=getattr(user, "username", None),
        first_name=getattr(user, "first_name", None),
        last_name=getattr(user, "last_name", None),
        is_bot=bool(getattr(user, "bot", False)),
        role=role,
    )


def _matches_query(participant: Participant, query: str | None) -> bool:
    if not query:
        return True
    needle = query.casefold()
    haystack = " ".join(
        part
        for part in (
            participant.username,
            participant.first_name,
            participant.last_name,
        )
        if part
    )
    return needle in haystack.casefold()


def _matches_filter(participant: Participant, filter: str) -> bool:
    if filter == "admins":
        return participant.role in {"creator", "admin"}
    if filter == "bots":
        return participant.is_bot
    return True


class TelethonMemberListBackend:
    """Adapter from the Telethon ``TelegramClient`` to ``MemberListBackend``.

    Supergroups/channels go through ``channels.GetParticipants`` /
    ``channels.GetParticipant``; legacy basic groups have neither, so they fall
    back to a single ``messages.GetFullChat`` whose roster is filtered locally.
    """

    def __init__(self, client: Any) -> None:
        self._client = client

    async def list_participants(
        self, *, chat_id: int, limit: int, query: str | None, filter: str
    ) -> MemberListResult:
        peer = await self._input_peer(chat_id)
        kind = type(peer).__name__
        if kind == "InputPeerChannel":
            return await self._channel_participants(
                peer, limit=limit, query=query, filter=filter
            )
        if kind == "InputPeerChat":
            return await self._chat_participants(
                peer, limit=limit, query=query, filter=filter
            )
        raise ValueError(
            f"chat {chat_id} has no participants (resolved to {kind})"
        )

    async def get_participant(
        self, *, chat_id: int, user: str
    ) -> Participant | None:
        peer = await self._input_peer(chat_id)
        kind = type(peer).__name__
        member = await self._input_peer_for_user(user)
        if kind == "InputPeerChannel":
            from telethon.tl import functions

            try:
                result = await self._client(
                    functions.channels.GetParticipantRequest(
                        channel=peer, participant=member
                    )
                )
            except Exception as exc:
                mapped = _classify_rpc_error(exc)
                if isinstance(mapped, MemberNotPresentError):
                    # A normal negative answer, not a failure.
                    return None
                raise mapped from exc
            users = {u.id: u for u in getattr(result, "users", ()) or ()}
            raw = getattr(result, "participant", None)
            if raw is None:
                return None
            return _to_participant(raw, users, _channel_role(raw))
        if kind == "InputPeerChat":
            target_id = getattr(member, "user_id", None)
            roster = await self._chat_participants(
                peer, limit=_BASIC_CHAT_ROSTER_CAP, query=None, filter="all"
            )
            for participant in roster.participants:
                if participant.user_id == target_id:
                    return participant
            return None
        raise ValueError(
            f"chat {chat_id} has no participants (resolved to {kind})"
        )

    # -- internals ---------------------------------------------------------

    async def _input_peer(self, chat_id: int) -> Any:
        try:
            return await self._client.get_input_entity(chat_id)
        except Exception as exc:
            raise _classify_rpc_error(exc) from exc

    async def _input_peer_for_user(self, user: str) -> Any:
        try:
            return await self._client.get_input_entity(coerce_user_ref(user))
        except Exception as exc:
            raise _classify_rpc_error(exc) from exc

    def _channel_filter(self, filter: str, query: str | None) -> Any:
        from telethon.tl import types

        if filter == "admins":
            return types.ChannelParticipantsAdmins()
        if filter == "bots":
            return types.ChannelParticipantsBots()
        # Search — not ``Recent``, which caps out near 200 and does not page.
        # Telethon's own iter_participants uses Search("") for full walks.
        return types.ChannelParticipantsSearch(query or "")

    async def _channel_participants(
        self, peer: Any, *, limit: int, query: str | None, filter: str
    ) -> MemberListResult:
        from telethon.tl import functions

        participants_filter = self._channel_filter(filter, query)
        # ``admins``/``bots`` have no server-side search, so the query is
        # re-applied locally for them.
        local_query = query if filter in {"admins", "bots"} else None

        collected: list[Participant] = []
        seen: set[int] = set()
        total: int | None = None
        offset = 0
        exhausted = False

        while len(collected) < limit:
            page_size = min(_PARTICIPANTS_PAGE_SIZE, limit - len(collected))
            try:
                page = await self._client(
                    functions.channels.GetParticipantsRequest(
                        channel=peer,
                        filter=participants_filter,
                        offset=offset,
                        limit=page_size,
                        hash=0,
                    )
                )
            except Exception as exc:
                raise _classify_rpc_error(exc) from exc

            raw_participants = list(getattr(page, "participants", ()) or ())
            if not raw_participants:
                exhausted = True
                break
            count = getattr(page, "count", None)
            if count is not None:
                total = int(count)
            users = {u.id: u for u in getattr(page, "users", ()) or ()}

            for raw in raw_participants:
                mapped = _to_participant(raw, users, _channel_role(raw))
                if mapped is None or mapped.user_id in seen:
                    continue
                if not _matches_query(mapped, local_query):
                    continue
                seen.add(mapped.user_id)
                collected.append(mapped)
                if len(collected) >= limit:
                    break

            offset += len(raw_participants)
            if total is not None and offset >= total:
                exhausted = True
                break

        return MemberListResult(
            participants=tuple(collected),
            participants_count=total,
            truncated=not exhausted,
        )

    async def _chat_participants(
        self, peer: Any, *, limit: int, query: str | None, filter: str
    ) -> MemberListResult:
        from telethon.tl import functions

        try:
            result = await self._client(
                functions.messages.GetFullChatRequest(chat_id=peer.chat_id)
            )
        except Exception as exc:
            raise _classify_rpc_error(exc) from exc

        full_chat = getattr(result, "full_chat", None)
        container = getattr(full_chat, "participants", None)
        raw_participants = list(getattr(container, "participants", ()) or ())
        users = {u.id: u for u in getattr(result, "users", ()) or ()}

        mapped: list[Participant] = []
        for raw in raw_participants:
            participant = _to_participant(raw, users, _chat_role(raw))
            if participant is not None:
                mapped.append(participant)

        selected = [
            p
            for p in mapped
            if _matches_filter(p, filter) and _matches_query(p, query)
        ]
        return MemberListResult(
            participants=tuple(selected[:limit]),
            participants_count=len(mapped),
            truncated=len(selected) > limit,
        )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_members_list_backend.py -q && .venv/bin/ruff check src tests`
Expected: PASS, ruff clean.

- [ ] **Step 5: Commit**

```bash
source .venv/bin/activate
git add src/telegram_assistant/members/telethon_backend.py tests/test_members_list_backend.py
git commit -m "feat(members): add Telethon participants-list adapter"
```

---

### Task 3: CLI `members list`

**Files:**
- Modify: `src/telegram_assistant/cli/main.py` (new `_build_member_list_backends` helper + `members list` command, next to the existing `members` app at line ~2473)
- Test: `tests/test_members_list_surfaces.py`

**Interfaces:**
- Consumes: `list_members`, `DEFAULT_MEMBER_LIST_LIMIT` (Task 1), `TelethonMemberListBackend` (Task 2), and the existing `_load_config_or_exit`, `_cli_authorizer`, `_resolve_folder_name`, `_raise_for_access_or_entity_error`, `TelethonSessionManager`.
- Produces: `cli_main._build_member_list_backends(config_path)` returning `(config, manager, _open)` where `_open()` yields `(list_backend, folder_backend, resolver)` — tests monkeypatch this name.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_members_list_surfaces.py` with the CLI half (the HTTP half is appended in Task 4):

```python
"""Surface tests for `members list` — CLI and HTTP wiring.

The domain op is covered by ``test_members_list.py`` and the adapter by
``test_members_list_backend.py``; this module checks flag/param validation,
payload shape and the status/exit-code taxonomy.
"""

from __future__ import annotations

import json
import tempfile
import textwrap
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from telegram_assistant.cli import main as cli_main
from telegram_assistant.entities import ResolvedEntity
from telegram_assistant.members import MemberListResult, Participant
from telegram_assistant.persistence import OperationStore

AUTH = {"Authorization": "Bearer secret_token"}


class FakeListBackend:
    def __init__(self, participants: list[Participant], *, found: Participant | None = None) -> None:
        self._participants = participants
        self._found = found
        self.list_calls: list[dict[str, Any]] = []
        self.get_calls: list[dict[str, Any]] = []

    async def list_participants(
        self, *, chat_id: int, limit: int, query: str | None, filter: str
    ) -> MemberListResult:
        self.list_calls.append(
            {"chat_id": chat_id, "limit": limit, "query": query, "filter": filter}
        )
        return MemberListResult(
            participants=tuple(self._participants[:limit]),
            participants_count=len(self._participants),
            truncated=len(self._participants) > limit,
        )

    async def get_participant(self, *, chat_id: int, user: str) -> Participant | None:
        self.get_calls.append({"chat_id": chat_id, "user": user})
        return self._found


class FakeResolver:
    def __init__(self, mapping: dict[str, int]) -> None:
        self._mapping = mapping

    async def resolve(self, ref: object) -> ResolvedEntity:
        return ResolvedEntity(
            chat_id=self._mapping[str(ref)], title=str(ref), kind="channel"
        )


class FakeFolderBackend:
    pass


def _participant(uid: int, *, role: str = "member", is_bot: bool = False) -> Participant:
    return Participant(
        user_id=uid,
        username=f"user{uid}",
        first_name=f"First{uid}",
        last_name=None,
        is_bot=is_bot,
        role=role,
    )


def _config_with_access(access_block: str | None) -> str:
    base = textwrap.dedent(
        """
        telegram:
          api_id: 123456
          api_hash: "telegram_api_hash"
          session_path: /data/telegram-assistant.session
          default_chat_folder:
            folder_id: 2
            folder_name: "Planfix clients"
        {access}
        http:
          host: "0.0.0.0"
          port: 8085
          bearer_token: "secret_token"
        logging:
          level: INFO
        """
    )
    indented = ""
    if access_block is not None:
        indented = textwrap.indent(access_block, "  ")
    return base.format(access=indented).strip()


_READ_ACCESS = "access:\n  rules:\n    - all: true\n      permission: read\n"
_WRITE_ACCESS = "access:\n  rules:\n    - all: true\n      permission: write\n"


def _make_store() -> OperationStore:
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".db")
    tmp.close()
    return OperationStore(Path(tmp.name))


def _write_config(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "config.yml"
    path.write_text(text, encoding="utf-8")
    return path


class _DummyManager:
    async def disconnect(self) -> None:
        return None


def _patch_cli_backends(
    monkeypatch: pytest.MonkeyPatch,
    backend: FakeListBackend,
    *,
    resolver: FakeResolver | None = None,
) -> None:
    def _factory(config_path):
        config = cli_main._load_config_or_exit(config_path)

        async def _open():
            return (backend, FakeFolderBackend(), resolver or FakeResolver({}))

        return config, _DummyManager(), _open

    monkeypatch.setattr(cli_main, "_build_member_list_backends", _factory)


def test_cli_members_list_happy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_file = _write_config(tmp_path, _config_with_access(_READ_ACCESS))
    backend = FakeListBackend([_participant(i) for i in range(1, 4)])
    _patch_cli_backends(monkeypatch, backend)

    result = CliRunner().invoke(
        cli_main.app,
        ["members", "list", "--chat-id", "-100123", "--limit", "2",
         "--config", str(config_file)],
    )

    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload["telegram_chat_id"] == -100123
    assert payload["count"] == 2
    assert payload["participants_count"] == 3
    assert payload["truncated"] is True
    assert payload["filter"] == "all"
    assert backend.list_calls == [
        {"chat_id": -100123, "limit": 2, "query": None, "filter": "all"}
    ]


def test_cli_members_list_user_check(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_file = _write_config(tmp_path, _config_with_access(_READ_ACCESS))
    backend = FakeListBackend([], found=_participant(7, is_bot=True))
    _patch_cli_backends(monkeypatch, backend)

    result = CliRunner().invoke(
        cli_main.app,
        ["members", "list", "--chat-id", "-100123",
         "--user", "@pressfinity_news_bot", "--config", str(config_file)],
    )

    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload["is_member"] is True
    assert payload["user"] == "@pressfinity_news_bot"
    assert backend.get_calls == [
        {"chat_id": -100123, "user": "@pressfinity_news_bot"}
    ]


def test_cli_members_list_resolves_entity(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_file = _write_config(tmp_path, _config_with_access(_READ_ACCESS))
    backend = FakeListBackend([_participant(1)])
    _patch_cli_backends(monkeypatch, backend, resolver=FakeResolver({"@team": -100999}))

    result = CliRunner().invoke(
        cli_main.app,
        ["members", "list", "--entity", "@team", "--config", str(config_file)],
    )

    assert result.exit_code == 0, result.stdout
    assert backend.list_calls[-1]["chat_id"] == -100999


def test_cli_members_list_requires_exactly_one_ref(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_file = _write_config(tmp_path, _config_with_access(_READ_ACCESS))
    _patch_cli_backends(monkeypatch, FakeListBackend([]))

    result = CliRunner().invoke(
        cli_main.app, ["members", "list", "--config", str(config_file)]
    )

    assert result.exit_code == 2


def test_cli_members_list_rejects_user_with_query(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_file = _write_config(tmp_path, _config_with_access(_READ_ACCESS))
    backend = FakeListBackend([])
    _patch_cli_backends(monkeypatch, backend)

    result = CliRunner().invoke(
        cli_main.app,
        ["members", "list", "--chat-id", "-100123", "--user", "@bot",
         "--query", "bot", "--config", str(config_file)],
    )

    assert result.exit_code == 2
    assert backend.get_calls == []


def test_cli_members_list_access_denied_exits_3(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_file = _write_config(tmp_path, _config_with_access(_WRITE_ACCESS))
    backend = FakeListBackend([_participant(1)])
    _patch_cli_backends(monkeypatch, backend)

    result = CliRunner().invoke(
        cli_main.app,
        ["members", "list", "--chat-id", "-100123", "--config", str(config_file)],
    )

    assert result.exit_code == 3
    assert backend.list_calls == []
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/test_members_list_surfaces.py -q`
Expected: FAIL — `AttributeError: module 'telegram_assistant.cli.main' has no attribute '_build_member_list_backends'`.

- [ ] **Step 3: Add the backend builder and the command**

In `src/telegram_assistant/cli/main.py`, after the existing `members` commands (`members_bulk_remove` ends around line ~2900; put this right after it, keeping the `members` commands together):

```python
def _build_member_list_backends(config_path: Path | None):
    """Open the Telethon-backed participants-list + folder backends + resolver.

    Mirrors :func:`_build_message_read_backends`: a read op needs the read
    backend, the folder backend (for `--chat-name` and folder access rules) and
    a shared entity resolver so ``--entity`` works. Tests monkeypatch this to
    inject fakes.
    """
    config = _load_config_or_exit(config_path)
    manager = TelethonSessionManager(config.telegram)

    async def _open():
        from telegram_assistant.entities import TelethonEntityResolver
        from telegram_assistant.folders import TelethonFolderBackend
        from telegram_assistant.members.telethon_backend import (
            TelethonMemberListBackend,
        )

        client = await manager.get_client()
        if not await client.is_user_authorized():
            raise RuntimeError(
                "Telethon session is not authorized; run "
                "`telegram-assistant auth` first."
            )
        return (
            TelethonMemberListBackend(client),
            TelethonFolderBackend(client),
            TelethonEntityResolver(client),
        )

    return config, manager, _open


@members_app.command("list")
def members_list(
    chat_id: int | None = typer.Option(
        None, "--chat-id", help="Numeric Telegram chat id to read."
    ),
    chat_name: str | None = typer.Option(
        None, "--chat-name", help="Chat title (resolved within --folder-name)."
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
        None, "--folder-id", help="Optional folder id cross-check."
    ),
    limit: int = typer.Option(
        DEFAULT_MEMBER_LIST_LIMIT,
        "--limit",
        help="Maximum number of participants to return (default 200).",
    ),
    query: str | None = typer.Option(
        None,
        "--query",
        help="Substring match on username/first/last name (server-side for the "
        "default filter).",
    ),
    filter: str = typer.Option(
        "all", "--filter", help="Which participants to list: all|admins|bots."
    ),
    user: str | None = typer.Option(
        None,
        "--user",
        help="Check one user's membership with a single request instead of "
        "listing (mutually exclusive with --query).",
    ),
    config_path: Path | None = typer.Option(  # noqa: B008
        None,
        "--config",
        "-c",
        help="Path to config.yml (defaults: ./data/config.yml, then ~/.config/telegram-assistant/config.yml).",
        exists=False,
    ),
) -> None:
    """List a chat's participants, or check one user's membership (READ-gated)."""
    from telegram_assistant.folders import FolderError, resolve_chat_in_folder
    from telegram_assistant.members import list_members

    refs = sum([chat_id is not None, chat_name is not None, entity is not None])
    if refs != 1:
        typer.echo(
            "exactly one of --chat-id, --chat-name, or --entity must be supplied",
            err=True,
        )
        raise typer.Exit(code=2)

    config, manager, open_backends = _build_member_list_backends(config_path)

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
            list_backend, folder_backend, resolver = await open_backends()
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
            result = await list_members(
                backend=list_backend,
                chat_id=resolved_chat_id,
                limit=limit,
                query=query,
                filter=filter,
                user=user,
                authorizer=authorizer,
            )
            return {
                "telegram_chat_id": resolved_chat_id,
                "limit": limit,
                "query": query,
                "filter": filter,
                **result.to_dict(),
            }
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
        # Bad caller input (limit, filter, user+query) — exit 2 like the rest.
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc
    except Exception as exc:
        _raise_for_access_or_entity_error(exc)
        typer.echo(f"members list failed: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    typer.echo(json.dumps(payload, sort_keys=True, default=str))
```

`DEFAULT_MEMBER_LIST_LIMIT` must be importable at module import time for the Typer default — add it to the existing top-of-file imports:

```python
from telegram_assistant.members import DEFAULT_MEMBER_LIST_LIMIT
```

(if `cli/main.py` has no top-level `telegram_assistant.members` import yet, add this one; the heavier `list_members` import stays lazy inside the command, matching `messages recent`).

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_members_list_surfaces.py -q && .venv/bin/ruff check src tests`
Expected: PASS, ruff clean. `tests/test_skill_inventory.py` will now fail — that is expected and fixed in Task 6.

- [ ] **Step 5: Commit**

```bash
source .venv/bin/activate
git add src/telegram_assistant/cli/main.py tests/test_members_list_surfaces.py
git commit -m "feat(cli): add members list command"
```

---

### Task 4: HTTP `GET /telegram/members/list`

**Files:**
- Modify: `src/telegram_assistant/http_api/app.py` (factory type alias ~line 71, `_default_member_list_backend_factory` after `_default_member_backend_factory` ~line 171, `create_app` kwarg ~line 552, state assignment ~line 802)
- Modify: `src/telegram_assistant/http_api/members.py` (`_member_list_backend_or_503` helper + the route)
- Test: `tests/test_members_list_surfaces.py` (append)

**Interfaces:**
- Consumes: `list_members`, `MemberListBackend`, `DEFAULT_MEMBER_LIST_LIMIT` (Task 1); `TelethonMemberListBackend` (Task 2); existing `build_authorizer`, `translate_access_error`, `resolve_entity_chat_id`, `_folder_backend_optional`.
- Produces: `app.state.member_list_backend_factory`; `create_app(..., member_list_backend_factory=...)`; `http_api.members._member_list_backend_or_503(request)` (reused by the MCP tool in Task 5).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_members_list_surfaces.py`:

```python
# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

from fastapi.testclient import TestClient  # noqa: E402

from telegram_assistant.config import load_config_from_text  # noqa: E402
from telegram_assistant.http_api import create_app  # noqa: E402


def _http_client(
    *,
    access_block: str | None = None,
    backend: FakeListBackend | None = None,
    resolver: FakeResolver | None = None,
    has_factory: bool = True,
) -> TestClient:
    config = load_config_from_text(_config_with_access(access_block))
    app = create_app(
        config,
        session_manager=None,
        member_list_backend_factory=(
            (lambda _r: backend) if has_factory else (lambda _r: None)
        ),
        folder_backend_factory=lambda _r: None,
        resolver_factory=(
            (lambda _r: resolver) if resolver is not None else (lambda _r: None)
        ),
        operation_store=_make_store(),
    )
    return TestClient(app)


def test_http_members_list_returns_rows() -> None:
    backend = FakeListBackend([_participant(i) for i in range(1, 4)])
    client = _http_client(access_block=_READ_ACCESS, backend=backend)

    resp = client.get(
        "/telegram/members/list",
        params={"chat_id": -100123, "limit": 2},
        headers=AUTH,
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["telegram_chat_id"] == -100123
    assert body["count"] == 2
    assert body["participants_count"] == 3
    assert body["truncated"] is True
    assert body["participants"][0]["user_id"] == 1


def test_http_members_list_user_check() -> None:
    backend = FakeListBackend([], found=_participant(7, is_bot=True))
    client = _http_client(access_block=_READ_ACCESS, backend=backend)

    resp = client.get(
        "/telegram/members/list",
        params={"chat_id": -100123, "user": "@pressfinity_news_bot"},
        headers=AUTH,
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["is_member"] is True


def test_http_members_list_resolves_entity() -> None:
    backend = FakeListBackend([_participant(1)])
    client = _http_client(
        access_block=_READ_ACCESS,
        backend=backend,
        resolver=FakeResolver({"@team": -100999}),
    )

    resp = client.get(
        "/telegram/members/list", params={"entity": "@team"}, headers=AUTH
    )

    assert resp.status_code == 200, resp.text
    assert backend.list_calls[-1]["chat_id"] == -100999


def test_http_members_list_requires_exactly_one_ref() -> None:
    client = _http_client(access_block=_READ_ACCESS, backend=FakeListBackend([]))
    resp = client.get("/telegram/members/list", headers=AUTH)
    assert resp.status_code == 400


def test_http_members_list_rejects_unknown_filter() -> None:
    backend = FakeListBackend([])
    client = _http_client(access_block=_READ_ACCESS, backend=backend)

    resp = client.get(
        "/telegram/members/list",
        params={"chat_id": -100123, "filter": "kicked"},
        headers=AUTH,
    )

    assert resp.status_code == 400
    assert backend.list_calls == []


def test_http_members_list_denied_without_read() -> None:
    backend = FakeListBackend([_participant(1)])
    client = _http_client(access_block=_WRITE_ACCESS, backend=backend)

    resp = client.get(
        "/telegram/members/list", params={"chat_id": -100123}, headers=AUTH
    )

    assert resp.status_code == 403
    assert backend.list_calls == []


def test_http_members_list_503_without_backend() -> None:
    client = _http_client(access_block=_READ_ACCESS, has_factory=False)
    resp = client.get(
        "/telegram/members/list", params={"chat_id": -100123}, headers=AUTH
    )
    assert resp.status_code == 503
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/test_members_list_surfaces.py -q -k http`
Expected: FAIL — `TypeError: create_app() got an unexpected keyword argument 'member_list_backend_factory'`.

- [ ] **Step 3: Wire the factory in `http_api/app.py`**

Next to `MemberBackendFactory` (line ~71):

```python
MemberListBackendFactory = Callable[[Request], MemberListBackend | None]
```

with `MemberListBackend` added to the existing `from telegram_assistant.members import (...)` import block at the top of the file.

After `_default_member_backend_factory` (line ~171):

```python
def _default_member_list_backend_factory(
    session_manager: TelethonSessionManager | None,
) -> MemberListBackendFactory:
    """Build a Telethon-backed participants-list factory for the read op.

    Mirrors :func:`_default_message_read_backend_factory`: returns ``None``
    until a Telethon client is available so the endpoint can return 503.
    """

    def _factory(_request: Request) -> MemberListBackend | None:
        if session_manager is None:
            return None
        client = getattr(session_manager, "_client", None)
        if client is None:
            return None
        from telegram_assistant.members.telethon_backend import (
            TelethonMemberListBackend,
        )

        return TelethonMemberListBackend(client)

    return _factory
```

In `create_app`, add the keyword next to `member_backend_factory` (line ~552):

```python
    member_list_backend_factory: MemberListBackendFactory | None = None,
```

and the state assignment next to the `member_backend_factory` one (line ~802):

```python
    app.state.member_list_backend_factory = (
        member_list_backend_factory
        if member_list_backend_factory is not None
        else _default_member_list_backend_factory(session_manager)
    )
```

- [ ] **Step 4: Add the helper and the route in `http_api/members.py`**

Extend the module's `from telegram_assistant.members import (...)` block with `DEFAULT_MEMBER_LIST_LIMIT`, `MemberListBackend`, `list_members`, and add `resolve_entity_chat_id` to the `http_api.access` import. Then add the helper next to `_member_backend_or_503`:

```python
def _member_list_backend_or_503(request: Request) -> MemberListBackend:
    factory = getattr(request.app.state, "member_list_backend_factory", None)
    if factory is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Telegram member-list backend is not configured (session may be unauthorized)",
        )
    backend = factory(request)
    if backend is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Telegram member-list backend is not available",
        )
    return backend
```

and the route inside `build_router()`, before the existing `POST /groups/{chat_id}/members/bulk-add`:

```python
    @router.get("/members/list")
    async def members_list(
        request: Request,
        chat_id: int | None = None,
        entity: str | None = None,
        limit: int = DEFAULT_MEMBER_LIST_LIMIT,
        query: str | None = None,
        filter: str = "all",
        user: str | None = None,
    ) -> dict[str, Any]:
        """List a chat's participants, or check one user's membership (READ-gated).

        Accepts either a numeric ``chat_id`` or a flexible ``entity`` reference,
        mirroring ``GET /telegram/messages/recent``. ``user`` answers membership
        for one user with a single request and is mutually exclusive with
        ``query``; ``filter`` is one of ``all``, ``admins``, ``bots``.
        """
        if (chat_id is None) == (entity is None):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="provide exactly one of chat_id or entity",
            )

        backend = _member_list_backend_or_503(request)
        if entity is not None:
            resolved_chat_id = await resolve_entity_chat_id(request, entity)
        else:
            resolved_chat_id = chat_id  # type: ignore[assignment]

        authorizer = build_authorizer(
            request, folder_backend=_folder_backend_optional(request)
        )
        try:
            result = await list_members(
                backend=backend,
                chat_id=resolved_chat_id,
                limit=limit,
                query=query,
                filter=filter,
                user=user,
                authorizer=authorizer,
            )
        except AccessDenied as exc:
            raise translate_access_error(exc) from exc
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
            ) from exc

        return {
            "telegram_chat_id": resolved_chat_id,
            "limit": limit,
            "query": query,
            "filter": filter,
            **result.to_dict(),
        }
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_members_list_surfaces.py -q && .venv/bin/ruff check src tests`
Expected: PASS, ruff clean.

- [ ] **Step 6: Commit**

```bash
source .venv/bin/activate
git add src/telegram_assistant/http_api/app.py src/telegram_assistant/http_api/members.py tests/test_members_list_surfaces.py
git commit -m "feat(http): add GET /telegram/members/list"
```

---

### Task 5: MCP tool `telegram_members_list`

**Files:**
- Modify: `src/telegram_assistant/http_api/mcp/tools.py` (import `_member_list_backend_or_503` + `list_members`; add the tool next to `telegram_members_add`, line ~2012)
- Modify: `tests/test_mcp_mount.py` (add `"telegram_members_list"` to `EXPECTED_TOOL_NAMES`, line ~222)

**Interfaces:**
- Consumes: `_member_list_backend_or_503` (Task 4), `_member_folder_backend_optional` (already aliased in `tools.py`), `list_members` (Task 1), the existing `_request`, `_raise_from_exception`, `build_authorizer`, `resolve_entity_chat_id`, `READ_TELEGRAM` annotations.
- Produces: MCP tool `telegram_members_list`.

- [ ] **Step 1: Add the expected tool name (the failing test)**

In `tests/test_mcp_mount.py`, add `"telegram_members_list",` to `EXPECTED_TOOL_NAMES` right after `"telegram_members_add",`.

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/bin/pytest tests/test_mcp_mount.py -q`
Expected: FAIL — the mounted tool set is missing `telegram_members_list`.

- [ ] **Step 3: Register the tool**

Extend the existing `from telegram_assistant.http_api.members import (...)` block with `_member_list_backend_or_503`, and the `from telegram_assistant.members import (...)` block with `list_members` and `DEFAULT_MEMBER_LIST_LIMIT`. Then, immediately before the `telegram_members_add` tool:

```python
    @server.tool(
        name="telegram_members_list",
        annotations=READ_TELEGRAM,
        structured_output=True,
    )
    async def telegram_members_list(
        chat_id: int | None = None,
        entity: str | None = None,
        limit: int = DEFAULT_MEMBER_LIST_LIMIT,
        query: str | None = None,
        filter: str = "all",
        user: str | None = None,
    ) -> dict[str, Any]:
        """List a chat's participants, or check whether one user is a member.

        ``user`` answers membership for a single user with one request (and is
        mutually exclusive with ``query``) — use it to check many chats cheaply.
        ``filter`` is one of ``all``, ``admins``, ``bots``.
        """
        request = _request(provider)
        try:
            if (chat_id is None) == (entity is None):
                raise ValueError("provide exactly one of chat_id or entity")
            backend = _member_list_backend_or_503(request)  # type: ignore[arg-type]
            resolved_chat_id = (
                await resolve_entity_chat_id(request, entity)  # type: ignore[arg-type]
                if entity is not None
                else chat_id
            )
            authorizer = build_authorizer(
                request,
                folder_backend=_member_folder_backend_optional(request),  # type: ignore[arg-type]
            )
            result = await list_members(
                backend=backend,
                chat_id=resolved_chat_id,  # type: ignore[arg-type]
                limit=limit,
                query=query,
                filter=filter,
                user=user,
                authorizer=authorizer,
            )
            return {
                "telegram_chat_id": resolved_chat_id,
                "limit": limit,
                "query": query,
                "filter": filter,
                **result.to_dict(),
            }
        except Exception as exc:
            _raise_from_exception(exc)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_mcp_mount.py tests/test_mcp_tools.py -q && .venv/bin/ruff check src tests`
Expected: PASS, ruff clean.

- [ ] **Step 5: Commit**

```bash
source .venv/bin/activate
git add src/telegram_assistant/http_api/mcp/tools.py tests/test_mcp_mount.py
git commit -m "feat(mcp): add telegram_members_list tool"
```

---

### Task 6: Documentation and full-suite green

**Files:**
- Modify: `skills/telegram-assistant/SKILL.md` (catalog row + a usage note)
- Modify: `README.md` (Commands section + MCP tool catalog + HTTP endpoint list)
- Modify: `CLAUDE.md` (one line placing `members/listing.py`)
- Modify: `docs/TODO.md` (check off the `members list` item)
- Copy: `~/.claude/skills/telegram-assistant/SKILL.md` (re-sync)

**Interfaces:**
- Consumes: everything from Tasks 1–5.
- Produces: `tests/test_skill_inventory.py` green.

- [ ] **Step 1: Run the guard to see it fail**

Run: `.venv/bin/pytest tests/test_skill_inventory.py -q`
Expected: FAIL — `members list` is a CLI command with no SKILL.md catalog row.

- [ ] **Step 2: Add the SKILL.md catalog row**

In the resource/action table (near line 220, next to the `members bulk-add` / `bulk-remove` rows), add:

```markdown
| `members` | `list` | List a chat's participants, or check one user's membership with `--user` (read-only, no writes). | `telegram-assistant members list --entity @chat --user @some_bot` |
```

Also add a short note in the read-only/verification section: `members list --user <ref>` is the cheap way to check membership across many chats — one request per chat, no writes, unlike `members bulk-add --dry-run`, which plans an add without checking membership.

- [ ] **Step 3: Update README.md**

- Commands section: document `members list` with all flags (`--chat-id/--chat-name/--entity`, `--folder-name/--folder-id`, `--limit`, `--query`, `--filter`, `--user`), noting it is READ-gated and never writes.
- HTTP endpoints: `GET /telegram/members/list` with its query parameters and the 400/403/503 answers.
- MCP tool catalog: `telegram_members_list`.
- Note the legacy-basic-group fallback and that `participants_count`/`truncated` report Telegram's ceiling.

- [ ] **Step 4: Add the CLAUDE.md pointer**

In the Architecture section where the per-area module split is described, note that `members/` splits the read op out: `listing.py` holds `list_members` (READ-gated, no operation row) with `TelethonMemberListBackend` in `telethon_backend.py`; supergroups use `channels.GetParticipants`/`GetParticipant` and legacy basic groups fall back to `messages.GetFullChat`; `--user` answers membership in one RPC and treats `left`/`banned` as not-a-member.

- [ ] **Step 5: Re-sync the skill**

```bash
cp skills/telegram-assistant/SKILL.md ~/.claude/skills/telegram-assistant/SKILL.md
```

- [ ] **Step 6: Check off the TODO item**

In `docs/TODO.md`, mark the `members list` item `- [x]` (leave the sub-bullets; the `do` finalize flow removes completed items later).

- [ ] **Step 7: Run the whole suite**

Run: `.venv/bin/pytest -q && .venv/bin/ruff check src tests`
Expected: full suite PASS, ruff clean.

- [ ] **Step 8: Commit**

```bash
source .venv/bin/activate
git add README.md CLAUDE.md docs/TODO.md skills/telegram-assistant/SKILL.md
git commit -m "docs: document members list across README, SKILL and CLAUDE"
```

---

## Manual verification (live, read-only)

Read-only checks against the live account may be run without asking (per CLAUDE.md); nothing here writes.

```bash
# a chat you can read — full listing
telegram-assistant members list --entity me --limit 5

# the actual driver: is the bot in this chat?
telegram-assistant members list --entity "<some chat>" --user @pressfinity_news_bot

# sweep the folder (the loop lives outside the command by design)
telegram-assistant folders inspect --folder-name "Агентства" \
  | jq -r '.chats[].chat_id' \
  | while read -r cid; do
      telegram-assistant members list --chat-id "$cid" --user @pressfinity_news_bot \
        | jq -c '{chat: .telegram_chat_id, is_member: .is_member}'
    done
```

Expect `is_member: true/false` per chat, exit 0; a chat with no read grant exits 3.
