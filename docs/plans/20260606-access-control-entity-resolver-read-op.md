# Read/Write/Permission features: entity resolver, access control, and a first-class read op

## Overview

Add three interlocking capabilities to `telegram-assistant`, planned together because the access
gate's flow is *resolve → then authorize*, and the read-level permission only becomes testable once
there is a real read operation to protect:

1. A shared **entity resolver** so every interface accepts a flexible entity reference
   (numeric id with/without `-100`, `@username`, `t.me`/invite link, phone, exact chat title)
   instead of only a numeric `chat_id`.
2. A config-driven **read/write access gate** enforced in the domain/service layer so the assistant
   can only act on permitted chats. Absent config means allow-all (backward compatible); once an
   `access` block is present it is deny-by-default, and `write` implies `read`.
3. Promoting the internal **get-recent-messages** helper to a first-class read operation across all
   entities, which is the canonical consumer that makes read-vs-write enforcement real.

## Context

- Adopted from `docs/draft-access-control-entity-resolver.md` (covers three interlocking TODO items).
- Single identity: one technical Telethon account + one HTTP bearer token. Permissions scope *which
  chats/folders this instance may touch* (read vs write), not per-caller identity. Per-identity auth
  is the later MCP/OAuth item — out of scope here.
- Impacted components: `config/models.py`, `config/loader.py`, new `entities/` and `access/`
  modules, `messages/`, `groups/`, `topics/`, `members/`, `folders/` services, `http_api/*`,
  `cli/main.py`, observability, `docs/init-plan.md` error taxonomy, `skills/telegram-assistant/SKILL.md`
  (+ `~/.claude` sync), `README.md`.
- Architecture constraint: each domain area keeps the `service.py` (pure logic + `Backend` protocol)
  / `telethon_backend.py` (production adapter) split so tests inject fakes. HTTP backends are built
  via `app.state.*_backend_factory`, returning `None` → `503` when the Telethon client is not
  connected; preserve that contract.
- Settled decisions: default = allow-all when unconfigured; create/folder ops gated by destination
  folder; deny is loud (new `AccessDenied` → HTTP 403 / CLI non-zero exit / structured log line) and
  gets a new category in the `docs/init-plan.md` error taxonomy.
- Numeric `chat_id` paths must keep working unchanged (backward compatible).

## Development Approach

- Testing approach: regular (write tests alongside each task).
- Complete each task fully before moving to the next; the resolver (Task 1) is the foundation the
  authorizer and surfaces build on.
- Update this plan when scope changes during implementation.

## Testing Strategy

- Unit tests required for every code-changing Task, using fakes — no real Telegram traffic.
- Run the project test suite after each Task before proceeding.
- e2e (real account) is added in the surfaces task by extending the existing `scripts/e2e_*.sh`.

## Progress Tracking

- Mark completed items with `[x]` immediately when done.
- Update this plan if implementation deviates from the original scope.

## Technical Details

### Entity resolver

- New `entities/` module (`service.py` + `telethon_backend.py`) mirroring the service/backend split.
- `EntityRef` input value; `ResolvedEntity` output (chat_id, title, kind, optional username).
- `EntityResolver` protocol: `resolve(ref) -> ResolvedEntity`. `TelethonEntityResolver` resolution
  order (per `telegram-download-chat` `core/entities.py:172-252`): numeric (with/without `-100`) →
  `PeerChannel`/`PeerChat`/`PeerUser` → dialog scan by id → delegate raw string to
  `client.get_entity()` for `@username` / `t.me` / `joinchat`/`+invite` / phone → title: scan
  dialogs.
- Error on ambiguity (`AmbiguousEntityError`) and not-found (`EntityNotFoundError`), fitting the
  error taxonomy. `FloodWaitError` is translated, not swallowed.
- Per-request cache of resolved refs.

### Access control

- Config (`config/models.py`): `AccessRule` (`extra="forbid"`, exactly one target of
  `chat` / `folder` / `all: true` (wildcard, every chat); `permission: read|write` default `write`);
  `AccessConfig` (`rules: list[AccessRule]`); `TelegramConfig.access: AccessConfig | None = None`
  (None ⇒ allow-all). `config/loader.py` template gains a commented `access:` example so the
  generated default stays allow-all.
- **Rules combine as a union, highest level wins.** The effective level for a chat is the max
  `AccessLevel` across every matching rule (wildcard `all`, any folder it belongs to, an explicit
  `chat` rule), and `write` implies `read`. So one config can simultaneously express: (1) a wildcard
  `all` + `permission: read` rule → read every chat; (2) per-`chat` / per-`folder` `permission: write`
  rules → write to selected usernames/folders; (3) a `folder` `permission: write` rule → manage
  members and topics for that folder (member add/remove and topic create/close are WRITE-level ops).
  These layer on top of the read-all baseline without conflict.
- `access/service.py`: `AccessLevel` enum (`READ < WRITE`); `AccessDenied(RuntimeError)` carrying
  chat ref + required level; `Authorizer` built per request from `AccessConfig` + an `EntityResolver`
  + a folder backend. It lazily builds an index — `default_level: AccessLevel | None` (from wildcard
  `all` rules), `chat_levels: dict[int, AccessLevel]` (rule chat refs resolved via the resolver), and
  `folder_levels: dict[str, AccessLevel]` — and exposes `require(chat_id, level, *, folder_memberships)`
  (granting the max of the wildcard default, the chat's folder levels, and any explicit chat level)
  and `require_folder(folder_name, level)`. When config `access is None`, the authorizer is a no-op
  sentinel (`require` returns immediately).
- Enforcement matrix (in the domain services, not just CLI): send / member add+remove / topic
  create+close / folder add+remove → WRITE on resolved chat; group create → WRITE on destination
  folder; mass-send folder mode → WRITE per-resolved-chat (unpermitted chats become `skipped` with
  reason `access_denied`); get-recent → READ on resolved chat.

### Get-recent read op

- Promote `get_recent_messages` (`groups/telethon_backend.py:200`) to a domain op in `messages/`:
  `MessageReadBackend` protocol + `get_recent_messages(chat_id, limit=5) -> list[RecentMessage]`
  (id, sender, date, reply_to, text/media summary). Default limit 5; requires READ via the authorizer.

## Implementation Steps

### Task 1: Build the shared entity resolver

- [ ] Add `entities/` module with `EntityRef`, `ResolvedEntity`, the `EntityResolver` protocol, and
  `AmbiguousEntityError` / `EntityNotFoundError`
- [ ] Implement `TelethonEntityResolver` with the full resolution order (numeric variants → peer
  types → dialog scan by id → `client.get_entity()` for usernames/links/phones → title dialog scan)
- [ ] Add a per-request resolution cache and translate `FloodWaitError` instead of swallowing it
- [ ] Keep numeric `chat_id` resolution working unchanged (backward compatible)
- [ ] write tests for the resolver (numeric variants, `@username`, link, title ambiguity → error,
  not-found)
- [ ] run project tests - must pass before next task

### Task 2: Add the access-control config and authorizer, enforced in the domain layer

- [ ] Add `AccessRule` / `AccessConfig` to `config/models.py` and `TelegramConfig.access`
  (None ⇒ allow-all); validate exactly one target of `chat` / `folder` / `all` per rule
- [ ] Add a commented `access:` example to the `config/loader.py` template (default stays allow-all);
  show a combined config: a wildcard `all: read` rule plus per-`chat`/`folder` `write` rules
- [ ] Add `access/service.py` with `AccessLevel`, `AccessDenied`, and `Authorizer` (wildcard default
  + chat + folder index with union/highest-level-wins resolution, `require` / `require_folder`, no-op
  sentinel when `access is None`)
- [ ] Thread an optional `authorizer` into the domain services and enforce the matrix: WRITE for
  send / members / topics / folders, WRITE on destination folder for group create, READ where
  applicable; mass-send marks unpermitted chats `skipped` reason `access_denied`
- [ ] write tests for the authorizer (chat rule, folder rule, wildcard `all` rule, read vs write,
  write-implies-read, union of read-all baseline + targeted write rules, allow-all when None,
  deny-by-default when present, create-by-folder) and per-service deny/allow
- [ ] run project tests - must pass before next task

### Task 3: Promote get-recent-messages to a first-class read op

- [ ] Move `get_recent_messages` into `messages/` as a domain op with a `MessageReadBackend`
  protocol and a `RecentMessage` shape (id, sender, date, reply_to, text/media summary), default
  limit 5
- [ ] Gate the read op behind READ-level authorization
- [ ] write tests for the read op (limit default/override, READ-denied path)
- [ ] run project tests - must pass before next task

### Task 4: Wire the resolver, authorizer, and read op into CLI and HTTP

- [ ] CLI (`cli/main.py`): accept `--entity` (resolved via the resolver) alongside existing
  `--chat-id`/`--chat-name` across messages/groups/topics/members/folders; add
  `messages recent [--entity] [--limit 5]`; build the `Authorizer` + resolver from loaded config
- [ ] Map `AccessDenied` to a clear CLI non-zero exit and entity ambiguity/not-found to clear messages
- [ ] HTTP (`http_api/*`): add an entity field to request bodies, add `GET /telegram/messages/recent`,
  build authorizer/resolver from `app.state` via the factory pattern (None → 503), and add
  `AccessDenied → 403` plus entity errors → 404/409 to the error translators
- [ ] Extend the existing `scripts/e2e_*.sh` (real account, idempotent): allowlist permitting only
  folder `Clients` / chat `Client chat test`, then assert a permitted send succeeds, a non-listed
  chat returns access-denied (403 / non-zero exit), `messages recent` returns ≤5, and the resolver
  works via `@username` and exact title against `Client chat test`
- [ ] write tests for CLI exit codes and HTTP 403 / entity-error responses
- [ ] run project tests - must pass before next task

### Task 5: Update error taxonomy, observability, and the documentation guards

- [ ] Add the access-denied category to the `docs/init-plan.md` error taxonomy (§Ошибки)
- [ ] Log access decisions in observability (denied: chat ref, required level, matched rule or none)
- [ ] Update `skills/telegram-assistant/SKILL.md` and re-sync it to `~/.claude/skills/...`, and
  update the `README.md` Commands section for `messages recent`, `--entity`, and the `access:` config
- [ ] write/adjust tests including the `tests/test_skill_inventory.py` guard
- [ ] run project tests - must pass before next task

### Task 6: Verify acceptance criteria

- [ ] verify all requirements from Overview are implemented (resolver accepts every ref form; access
  gate enforces read/write in the domain layer with allow-all when unconfigured and deny-by-default
  when present; get-recent is a first-class READ op)
- [ ] run full project test suite
- [ ] run project linter (`ruff check src tests`) - all issues must be fixed

## Post-Completion

*Items requiring manual intervention - no checkboxes, informational only*

- Run the live e2e scripts (`scripts/e2e_*.sh`) against an authorized Telethon session with the
  configured `default_chat_folder.folder_name` set to `Clients` (containing chat `Client chat test`)
  — these mutate the real test account and are not run by `pytest`.
- Delete the source draft `docs/draft-access-control-entity-resolver.md` once this plan is adopted.
- Install the `ralphex` CLI to execute this plan.
