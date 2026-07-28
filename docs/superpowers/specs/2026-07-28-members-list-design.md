# `members list` — read-only participants command

**Date:** 2026-07-28
**Status:** approved design, not yet implemented
**Source task:** `docs/TODO.md` → «`members list` — read-only команда для списка участников чата»

## Problem

There is no read-only way to learn a chat's membership. The concrete driver: check
whether `@pressfinity_news_bot` is present in the 75 groups of the `Агентства`
folder. `members bulk-add --dry-run` cannot answer this — it plans an add without
checking membership (`action: would_add` even for a user who is already in the
chat), and the only place Telegram answers `already_member` is a real `bulk-add`
run, i.e. a write plus `FLOOD_WAIT` risk.

## Scope

One per-chat command with two modes:

1. **list** — paginated participants of one chat, with filters.
2. **check** (`--user <ref>`) — a single `channels.GetParticipant` RPC answering
   "is this user in this chat, and in what role".

Sweeping a whole folder is **out of scope**: the caller loops over the chats
(e.g. from `folders inspect`) and issues one `--user` call per chat. That keeps
this a plain read op — one RPC per chat, no bulk progress, no queue, no pacing
policy of its own.

## 1. Domain — new module `members/listing.py`

`members/service.py` is already ~1155 lines of bulk-add/bulk-remove queue logic;
the read op goes into its own module, mirroring how `messages/` splits `search.py`,
`reactions.py`, `pinning.py` out of `service.py`. Re-exported from
`members/__init__.py`.

```python
@dataclass(frozen=True)
class Participant:
    user_id: int
    username: str | None
    first_name: str | None
    last_name: str | None
    is_bot: bool
    role: str          # creator | admin | member | restricted | left | banned
    def to_dict(self) -> dict[str, Any]

@dataclass(frozen=True)
class MemberListResult:
    participants: tuple[Participant, ...]
    participants_count: int | None   # total the chat reports; None when unknown
    truncated: bool                  # cut off by `limit` or by Telegram's ceiling
    requested_user: str | None = None

    @property
    def is_member(self) -> bool | None:   # None when `--user` was not supplied
        ...

    def to_dict(self) -> dict[str, Any]   # the one payload builder all surfaces use

class MemberListBackend(Protocol):
    async def list_participants(
        self, *, chat_id: int, limit: int, query: str | None, filter: str
    ) -> MemberListResult: ...
    async def get_participant(
        self, *, chat_id: int, user: str
    ) -> Participant | None: ...

async def list_members(
    *, backend: MemberListBackend, chat_id: int, limit: int = 200,
    query: str | None = None, filter: str = "all", user: str | None = None,
    authorizer: Authorizer | None = None,
) -> MemberListResult
```

Rules:

- Validation in the domain, before any RPC: `limit > 0`; `filter ∈ {all, admins, bots}`;
  `user` is mutually exclusive with `query` (they answer different questions).
  Violations raise `ValueError`.
- READ gate: when an `authorizer` is supplied, `require(chat_id, AccessLevel.READ)`
  runs **before** the backend call, like `get_recent_messages`.
- `user` mode calls `get_participant` and returns a result carrying 0 or 1
  participants, `participants_count=None`, `truncated=False`, `requested_user=user`.
  `filter` is ignored in this mode (the answer is about one named user); passing
  `--filter` alongside `--user` is accepted and has no effect.
- `is_member` is `True` only when a participant was found **and** its role is
  neither `left` nor `banned` — `channels.GetParticipant` answers for a user who
  left or was banned too, and reporting those as members would silently mislead
  the membership check this command exists for. The role stays in the payload, so
  the caller can tell "never joined" (`participants: []`) from "left".
- No operation row, no idempotency key, no `--dry-run` — it is a pure read.
- `to_dict()` emits `user` / `is_member` keys **only** when `requested_user` is set,
  so the plain list payload keeps its shape.

## 2. Adapter — `TelethonMemberListBackend` in `members/telethon_backend.py`

Lives next to `TelethonMemberBackend` and reuses its `_classify_rpc_error`
(so `FloodWaitError` keeps mapping to `worker.queue.FloodWaitError`).

- The peer is resolved **once** via `get_input_entity`. `InputPeerChannel` →
  `channels.GetParticipants`; `InputPeerChat` (legacy basic group) →
  `messages.GetFullChat`; anything else (a user, self) raises `ValueError`
  ("chat has no participants") → 400 / exit 2 rather than an opaque RPC error.
- Filter mapping: `all` → `ChannelParticipantsSearch(query or "")`,
  `admins` → `ChannelParticipantsAdmins()`, `bots` → `ChannelParticipantsBots()`.
  **Search, not `ChannelParticipantsRecent`** — `Recent` is capped near 200 and does
  not page; Telethon's own `iter_participants` uses `Search("")` for full
  enumeration for exactly this reason.
- `admins` / `bots` have no server-side query, so `--query` is applied **locally**
  when combined with them.
- Paging: pages of 200 until `limit` is reached. Stop conditions: an empty page, a
  non-advancing offset, or `count` reached. `participants_count = result.count`;
  `truncated = collected < participants_count` (this is also how Telegram's ~10k
  full-enumeration ceiling surfaces).
- Basic groups: `messages.GetFullChat` returns the whole roster at once (≤200), so
  `filter` and `query` are applied locally and `truncated` is always `False`.
- Role mapping — channels: `ChannelParticipantCreator` → `creator`,
  `ChannelParticipantAdmin` → `admin`, `ChannelParticipantBanned` → `restricted`
  (or `left` when `.left` is set), `ChannelParticipantLeft` → `left`, otherwise
  `member`. Basic: `ChatParticipantCreator` → `creator`,
  `ChatParticipantAdmin` → `admin`, otherwise `member`.
- `get_participant`: channels → `channels.GetParticipant`;
  `UserNotParticipantError` → `None` (a normal negative answer, not an error).
  Basic groups → `GetFullChat` and scan the roster.

## 3. Surfaces

- **CLI** `members list` — `--chat-id` / `--chat-name` / `--entity` (exactly one),
  `--folder-name` / `--folder-id` for the `--chat-name` lookup, `--limit` (200),
  `--query`, `--filter`, `--user`, `--config`. Same skeleton as `messages recent`:
  a `_build_member_list_backends()` helper, `_cli_authorizer`,
  `_raise_for_access_or_entity_error`, JSON on stdout.
- **HTTP** `GET /telegram/members/list?chat_id=|entity=&limit=&query=&filter=&user=`
  — symmetric with `GET /telegram/messages/recent` rather than a REST-shaped
  `/groups/{chat_id}/members`, because the latter cannot take an `entity` ref. A new
  `member_list_backend_factory` on `app.state` returns `None` until the Telethon
  client is connected, and the route answers **503** in that case.
- **MCP** `telegram_members_list` with the same parameters, READ-gated through the
  same domain op.
- Error taxonomy, unchanged from the rest of the project: `AccessDenied` → 403 /
  exit 3; entity not found → 404 / exit 2; `ValueError` → 400 / exit 2;
  `FloodWaitError` follows the existing read-op mapping.

## 4. Tests and documentation

`tests/test_members_list.py`:

- domain: `limit`/`filter`/`user+query` validation, READ gate fires before any
  backend call, `--user` present/absent paths, `truncated` reporting;
- adapter against a fake Telethon client: multi-page enumeration, stop on
  non-advancing offset, basic-group `GetFullChat` fallback, `UserNotParticipantError`
  → `None`, role mapping for every participant class;
- CLI: flag validation and JSON payload;
- HTTP: 200, 400 (bad filter / both refs / neither), 403 (READ denied), 503 (no backend).

Plus `telegram_members_list` added to `EXPECTED_TOOL_NAMES` in
`tests/test_mcp_mount.py`; README (Commands + MCP tool catalog);
`skills/telegram-assistant/SKILL.md` and its re-sync to
`~/.claude/skills/telegram-assistant/SKILL.md` (the `tests/test_skill_inventory.py`
guard fails otherwise); a short CLAUDE.md note placing `members/listing.py`.

## Out of scope

- Folder-wide sweep / bulk membership audit.
- `kicked` / `restricted` / `contacts` filters (need admin rights, rare cases).
- Any caching of the roster.
