# Read/Write/Permission features: entity resolver + access control + first-class read op

**Status:** planned · **Date:** 2026-06-06

Covers three interlocking TODO items, planned together because the access gate's
stated flow is *resolve → then authorize*, and the read-level permission only
becomes testable once there is a real read operation to protect:

1. **Entity resolver** — accept a flexible entity ref everywhere instead of numeric `chat_id`.
2. **Access control** — config-driven read/write allowlist enforced in the domain layer.
3. **Get-recent-messages** — promote the internal helper to a first-class read op.

## Settled decisions

- **Single identity.** One technical Telethon account + one HTTP bearer token. Permissions
  scope *which chats/folders this instance may touch* (read vs write), not per-caller identity.
  Per-identity auth is the later MCP/OAuth item — out of scope here.
- **Default = allow-all when unconfigured.** If `telegram.access` is absent → behaves exactly
  as today (every chat permitted). If the block is present → **deny by default**; only rules grant.
  Security is opt-in; backward compatible.
- **`write` implies `read`.** Read ops need ≥read; mutating ops need write.
- **Create/folder ops gated by destination folder.** A group-create is allowed if its target
  folder is write-permitted. Mass-send / folder ops are gated per-resolved-chat.
- **Deny is loud.** New `AccessDenied` → HTTP **403**, CLI non-zero exit, structured log line.
  Add an "operation not permitted by access policy" category to the `docs/init-plan.md` taxonomy.

## Phase 1 — Entity resolver (foundation)

New shared resolver so every domain accepts one flexible ref. Mirrors
`telegram-download-chat`'s `core/entities.py:172-252` resolution order.

- **New module `entities/`** (`service.py` + `telethon_backend.py`), matching the
  service/backend split used elsewhere:
  - `EntityRef` value object; `ResolvedEntity` (chat_id, title, kind, username?).
  - `EntityResolver` protocol: `resolve(ref) -> ResolvedEntity`.
  - `TelethonEntityResolver` implementing the order: numeric (with/without `-100`) →
    `PeerChannel`/`PeerChat`/`PeerUser` → dialog scan by id → delegate raw string to
    `client.get_entity()` for `@username` / `t.me` / `joinchat/+invite` / phone →
    title: scan dialogs, **error on ambiguity** (`AmbiguousEntityError`) and not-found
    (`EntityNotFoundError`).
  - **Per-request cache** of resolved refs (passed object, cleared per CLI invocation / HTTP request).
- Errors fit the taxonomy; `FloodWaitError` is translated, not swallowed.
- Numeric `chat_id` keeps working unchanged (backward compatible).
- **Wiring deferred to Phase 4** — Phase 1 ships the resolver + tests standalone so it can be
  reviewed in isolation; surfaces adopt it in Phase 4.

## Phase 2 — Access control (the gate)

### Config (`config/models.py`)

```python
class AccessRule(BaseModel):           # extra="forbid"; exactly one of chat/folder
    chat: str | None = None            # entity ref: id / @username / title / link
    folder: str | None = None          # folder name
    permission: Literal["read", "write"] = "write"

class AccessConfig(BaseModel):         # extra="forbid"
    rules: list[AccessRule] = []

# TelegramConfig:  access: AccessConfig | None = None   # None => allow-all
```

`config/loader.py` template gets a commented `access:` example (stays commented so the default
generated config is allow-all). Validator: each rule sets exactly one of `chat`/`folder`.

### Authorizer (`access/service.py`, new module)

- `AccessLevel` enum (`READ < WRITE`); `AccessDenied(RuntimeError)` carrying chat ref + required level.
- `Authorizer` built once per request from `AccessConfig` + an `EntityResolver` + a folder backend:
  - Lazily builds an **index**: `chat_levels: dict[int, AccessLevel]` (rule chat refs resolved
    via the resolver) and `folder_levels: dict[str, AccessLevel]`.
  - `await require(chat_id, level, *, folder_memberships)` → effective level =
    `max(chat_levels.get(chat_id), *(folder_levels[f] for f in memberships))`; raise `AccessDenied`
    if `< level`. Folder membership of a target chat is looked up via the folder backend (cached).
  - `await require_folder(folder_name, level)` for create-by-folder.
  - **Allow-all path:** when config `access is None`, `Authorizer` is the no-op sentinel — `require`
    returns immediately. Keeps every call site uniform.

### Enforcement (domain services — *not* just CLI)

Each service fn gains `authorizer: Authorizer | None = None` (consistent with how backends/store
are threaded today). At the top of the operation:

| Operation | Required | Target |
| --- | --- | --- |
| `messages.send_message`, member add/remove, topic create/close, folder add/remove | WRITE | resolved chat |
| `groups.create_group` | WRITE | **destination folder** |
| `messages.mass_send_message` (folder mode) | WRITE | per-resolved-chat → unpermitted chats become `skipped` reason=`access_denied` |
| `messages.get_recent_messages` (Phase 3) | READ | resolved chat |

`None`/no-op authorizer → behavior identical to today.

## Phase 3 — First-class get-recent read op

- Promote `get_recent_messages` (`groups/telethon_backend.py:200`) to a domain op in `messages/`:
  `MessageReadBackend` protocol + `get_recent_messages(chat_id, limit=5) -> list[RecentMessage]`
  (id, sender, date, reply_to, text/media summary). Default **limit 5**.
- Requires READ via the authorizer. This is the canonical consumer that makes read-vs-write real.

## Phase 4 — Surfaces, docs, taxonomy, tests

- **CLI (`cli/main.py`):** accept `--entity` (resolved via Phase 1) alongside existing
  `--chat-id`/`--chat-name` on messages/groups/topics/members/folders; new `messages recent
  [--entity] [--limit 5]`. Build `Authorizer` + resolver from loaded config; map `AccessDenied`
  to a clear non-zero exit, `Ambiguous/NotFound` entity errors to clear messages.
- **HTTP (`http_api/*`):** entity field in request bodies; `GET /telegram/messages/recent`;
  build authorizer/resolver from `app.state` (factory pattern, `None` → 503 as today);
  add `AccessDenied → 403`, entity errors → 404/409, to the error translators.
- **Error taxonomy:** add the access-denied category to `docs/init-plan.md` (§Ошибки).
- **Observability:** log access decisions (denied: chat ref, required level, matched rule/none).
- **Docs sync (guarded by `tests/test_skill_inventory.py`):** update
  `skills/telegram-assistant/SKILL.md`, re-sync to `~/.claude/skills/...`, and `README.md`
  Commands section for `messages recent`, `--entity`, and the `access:` config.
- **Tests (fakes, no real Telegram):**
  - resolver: numeric variants, `@username`, link, title ambiguity→error, not-found.
  - authorizer: chat rule, folder rule, read vs write, write-implies-read, allow-all when `None`,
    deny-by-default when block present, create-by-folder.
  - per-service: denied when unauthorized / allowed when permitted; mass-send skip=`access_denied`.
  - HTTP 403 + CLI exit code.
- **e2e — extend existing `scripts/e2e_*.sh`** (real account, idempotent/re-runnable): configure
  an allowlist permitting only folder `Clients` / chat `Client chat test`, then assert (a) a
  permitted send succeeds, (b) a non-listed chat returns access-denied (HTTP 403 / CLI non-zero
  exit), (c) `messages recent` returns ≤5, and (d) the resolver works via both `@username` and
  exact title against `Client chat test`.

## Backward-compatibility & risks

- No `access:` block → zero behavior change. Numeric `chat_id` paths untouched.
- Folder-membership lookups add a folder-backend round-trip per gated op when access is enabled —
  cache per request; allow-all path skips it entirely.
- Resolver title-scan can be slow on large dialog lists — only triggered for title refs; ids/usernames
  stay fast.

## Out of scope (separate TODO items)

Media send, scheduled messages, mute/unmute, reactions, forward, folder-remove, the MCP/OAuth
server, PyPI publishing.
