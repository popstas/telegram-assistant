# Messages operations (edit/pin/download/search) + folder-rule access performance

## Overview

Two workstreams from `docs/TODO.md`:

1. **Access perf:** with any `folder:` rule in `telegram.access`, every gated operation takes
   ~45–58s because `Authorizer._folder_memberships()` calls
   `folder_backend.list_folders()`, which does `await client.get_entity(peer)` for every chat
   in every folder (`src/telegram_assistant/folders/telethon_backend.py:71-73`) — hundreds of
   sequential round-trips just to learn titles that membership checks never use.
   Fix = **variant A** (build the chat→folders map straight from `InputPeer` ids, no
   `get_entity`; one `GetDialogFiltersRequest`) + **variant C** (persistent SQLite cache of the
   membership map with TTL + stale fallback), decided with the user.
2. **New message operations**, each following the established
   service / telethon_backend / CLI / HTTP / MCP / backend-factory pattern with access gates
   and `--dry-run`:
   - `messages edit` — edit text/caption of a sent message, gated by a new
     `edit_only_session_messages` policy flag (default `true`, per-rule overridable — mirror of
     `delete_only_session_messages`).
   - `messages pin` / `messages unpin` — pin/unpin a message (WRITE gate, `--silent`,
     `--pm-oneside`, unpin one id or all).
   - `messages download` — download media/document/voice **from** an existing message to a
     local file (READ gate). Distinct from `messages/downloads.py`, which fetches URL→temp
     **for sending**.
   - `messages search` — text search inside a chat (READ gate), returns `RecentMessage`-shaped
     rows newest-first.

## Context (from discovery)

- **Authorizer** `src/telegram_assistant/access/service.py`: `_ensure_index()` builds
  per-instance rule index; `_folder_memberships(chat_id)` (`:223`) short-circuits when no
  folder rules, else builds full `dict[int, set[str]]` from `list_folders()` and caches it
  per-instance (one request). Per-rule `delete_only_session_messages` plumbing to mirror:
  `config/models.py:83` (rule field), `:142` (policy default),
  `_merge_delete_only`/`_default_delete_only`/`_chat_delete_only`/`_folder_delete_only`,
  resolution chat > folder > all > policy default, restrictive-`true` wins,
  `Authorizer.delete_only_session_messages()` (`:313`).
- **Folder backend** `folders/telethon_backend.py`: `_fetch_filters()` wraps
  `GetDialogFiltersRequest`; `InputPeerChannel.channel_id` / `InputPeerChat.chat_id` /
  `InputPeerUser.user_id` already carry the ids — `get_entity` is needed only for titles.
- **Persistence** `persistence/schema.py`: 4 tables, `SCHEMA_VERSION = 1`, idempotent
  `bootstrap()`; `connect()` sets WAL + busy_timeout. A cache table fits here.
- **Hot reload** `config/reload.py` `reload_config_into_state(..., on_swap)`; `on_swap` in
  `http_api/app.py:529-559` swaps config under `app.state.config_lock`. Authorizer is rebuilt
  per request, but a persistent cache needs explicit invalidation in `on_swap`.
- **Domain-op templates**: `messages/reactions.py`, `messages/forwarding.py` (frozen
  dataclass Request/Result + narrow backend `Protocol` + single service fn that calls
  `authorizer.require(...)` before the backend; no OperationStore for small ops).
  Delete gate template: `messages/service.py:768` `delete_messages(...)` +
  `MessageDeleteForbidden` + `SentMessageRegistry` (`messages/sent_registry.py`, tracks
  `(chat_id, message_id)`, app-level instance at `http_api/app.py:597`, fresh per CLI run).
  READ-gated template: `get_recent_messages` (`service.py:625`), `RecentMessage` (`:256`).
- **Adapters** `messages/telethon_backend.py`: one class per op, every call wrapped in
  `translate_flood_wait`.
- **CLI** `cli/main.py`: `messages_app` group; per-command `_build_<op>_backends(config_path)`
  helpers; target selector "exactly one of `--chat-id`/`--chat-name`/`--entity`" → exit 2;
  `--dry-run` resolves + authorizes then exits; `_cli_authorizer` (`:161`).
- **HTTP** `http_api/messages.py`: Pydantic bodies, `_<op>_backend_or_503` helpers reading
  `app.state.<op>_backend_factory`; error map `AccessDenied`→403, forbidden gates→403,
  `FloodWaitError`→502, `ValueError`→400. Factories: default fns in `http_api/app.py:209-326`,
  `create_app` params `:383-394`, state assignment `:600-654`.
- **MCP** `http_api/mcp/tools.py`: `@server.tool(name="telegram_messages_*", annotations=...,
  structured_output=True)` delegating to `_resolve_*` helpers; update
  `EXPECTED_TOOL_NAMES` in `tests/test_mcp_mount.py:215-238`.
- **Docs guards**: `tests/test_skill_inventory.py` requires a catalog-table row in
  `skills/telegram-assistant/SKILL.md` for every CLI command (add rows in the same task as the
  CLI command, or tests fail); README Commands list + MCP tool catalog table; SKILL.md must be
  re-synced to `~/.claude/skills/telegram-assistant/SKILL.md`.
- **Test templates**: `tests/test_messages_reactions.py`, `test_messages_forward.py`,
  `test_messages_delete.py`, `test_messages_delete_surfaces.py`, `test_messages_recent.py`,
  `test_sent_registry.py`, `test_messages_telethon_backend.py`, `test_access.py`,
  `test_access_enforcement.py`, `test_cli_access.py`, `test_http_access.py`,
  `test_mcp_mount.py`, `test_mcp_tools.py`, `test_dry_run_members_messages.py`.

## Development Approach

- **Testing approach**: Regular (code first, then tests in the same task) — user's choice.
- Complete each task fully before moving to the next.
- Make small, focused changes.
- **CRITICAL: every task MUST include new/updated tests** for code changes in that task
  - tests are not optional — they are a required part of the checklist
  - unit tests for new and modified functions/methods, success and error scenarios
- **CRITICAL: all tests must pass before starting next task** — no exceptions
- **CRITICAL: update this plan file when scope changes during implementation**
- Run `pytest` after each change; `ruff check src tests` before finishing a task.
- Maintain backward compatibility: `telegram.access` absent ⇒ allow-all no-op; missing new
  config fields ⇒ current behavior.

## Testing Strategy

- **Unit tests**: required for every task, with fakes (no real Telegram traffic), following
  the template files listed in Context.
- **E2E**: no UI e2e in this project; live `scripts/e2e_*.sh` need a real session and are
  out of scope for the loop (Post-Completion).

## Progress Tracking

- Mark completed items with `[x]` immediately when done
- Add newly discovered tasks with ➕ prefix
- Document issues/blockers with ⚠️ prefix
- Update plan if implementation deviates from original scope

## What Goes Where

- **Implementation Steps** (`[ ]` checkboxes): code, tests, docs achievable in this repo.
- **Post-Completion** (no checkboxes): live e2e against real Telegram, deployment.
- Checkboxes only in Task sections.

## Implementation Steps

### Task 1: Variant A — build folder membership without get_entity

- [x] add `list_folder_chat_ids()` to the folder backend surface used by the authorizer: in
      `folders/telethon_backend.py`, a method that fetches dialog filters once
      (`_fetch_filters`) and returns `dict[str, set[int]]` (folder title → bare chat ids) by
      reading ids directly from `InputPeerChannel.channel_id` / `InputPeerChat.chat_id` /
      `InputPeerUser.user_id` — **no `get_entity` calls**; wrap in `translate_flood_wait`
- [x] normalise ids to the same bare form the rule index uses (`_canonical_chat_id`)
- [x] switch `Authorizer._folder_memberships()` (`access/service.py`) to call
      `list_folder_chat_ids()` when the injected folder backend provides it, keeping the
      existing `list_folders()` path as fallback for fake/simple backends (protocol via
      `getattr`/narrow Protocol update); `list_folders()` itself stays unchanged for CLI
      `folders inspect`
- [x] write tests: membership map built from a fake backend exposing `list_folder_chat_ids`
      (marked/bare id normalisation, folder-rule require() passes/denies), fallback path when
      only `list_folders` exists, FloodWait propagation
- [x] run tests — must pass before task 2

### Task 2: Membership cache table + store (variant C storage)

- [x] add `folder_membership_cache` storage to `persistence/schema.py`: single-row table
      (`id INTEGER PRIMARY KEY CHECK (id = 1)`, `payload TEXT` JSON of
      `{folder_name: [chat_id, ...]}`, `fetched_at REAL`); bump `SCHEMA_VERSION`, keep
      `bootstrap()` idempotent for existing DBs
- [x] add a small store class (e.g. `FolderMembershipCache` in `persistence/`) with
      `load() -> (map, fetched_at) | None`, `save(map, fetched_at)`, `clear()`, reusing
      `connect()` (WAL, busy_timeout); thread-safe like `OperationStore`
- [x] add config knob `telegram.access.folder_cache_ttl` (seconds, int, default `300`,
      `0` disables persistent caching) to `AccessConfig` in `config/models.py`
- [x] write tests: save/load round-trip, clear, schema bootstrap on existing v1 DB,
      ttl field validation/default
- [x] run tests — must pass before task 3

### Task 3: Wire cache into authorizer (read-through, stale fallback, invalidation)

- [x] inject optional cache into `Authorizer` (new ctor arg, default `None` — no behavior
      change when absent); `_folder_memberships()` becomes: cache fresh (age < ttl) → use it;
      expired/missing → fetch via Task-1 path and `save()`; fetch raises → fall back to stale
      cached map, log a warning (propagate only when no cache exists at all)
- [x] HTTP wiring: build the cache from the app's existing SQLite path and pass it through
      `build_authorizer` (`http_api/access.py`); clear the cache in the hot-reload `on_swap`
      (`http_api/app.py`) so access-rule edits apply cleanly
- [x] CLI wiring: `_cli_authorizer` (`cli/main.py`) constructs the cache from the config DB
      path so a fresh CLI process reuses the persisted map (this is the main win: CLI = one
      process per call)
- [x] MCP path inherits HTTP wiring (verify no extra changes needed)
- [x] write tests: fresh-cache hit skips backend entirely, expired cache refetches and
      rewrites, backend error serves stale map, `folder_cache_ttl: 0` bypasses cache,
      hot-reload/`clear()` invalidation
- [x] run tests — must pass before task 4

### Task 4: `messages edit` domain op + `edit_only_session_messages` gate

- [x] create `messages/editing.py` modeled on `reactions.py`: `EditBackend` Protocol
      (`edit_message(chat_id, message_id, text) -> EditResult` data), frozen
      `MessageEditRequest`/`MessageEditResult` (+`to_dict()`), service fn
      `edit_message(backend, *, request, authorizer, sent_registry, only_session_messages)`
      — validate → `authorizer.require(chat_id, WRITE)` → session-gate via
      `sent_registry.contains(chat_id, message_id)` raising `MessageEditForbidden` → dry_run
      short-circuit → backend
- [x] config: `AccessConfig.edit_only_session_messages: bool = True` and per-rule
      `AccessRule.edit_only_session_messages: bool | None` in `config/models.py`
- [x] authorizer: `edit_only_session_messages(chat_id, *, default, folder_memberships)`
      mirroring the delete resolution exactly (chat > folder > all > policy default,
      restrictive-`true` wins on same level; extend `_ensure_index` with edit-only maps)
- [x] adapter `TelethonEditBackend` in `messages/telethon_backend.py` using
      `client.edit_message`; wrap in `translate_flood_wait`; surface Telegram edit
      restrictions (not-own-message, ~48h edit window, unmodified text) as clear domain errors
- [x] write tests: service success/dry-run, WRITE denial, session-gate forbidden (registry
      miss) and pass (registry hit), `edit_only_session_messages: false` policy and per-rule
      override precedence + restrictive-wins (mirror `test_messages_delete.py` /
      `test_access.py` cases), adapter error translation
- [x] run tests — must pass before task 5

### Task 5: `messages edit` surfaces (CLI + HTTP + MCP)

- [x] CLI `messages edit` in `cli/main.py`: `--message-id`, `--text`, target selector
      (exactly one of `--chat-id`/`--chat-name`/`--entity`), `--dry-run`;
      `_build_edit_backends(config_path)`; `AccessDenied`/forbidden → exit 3
- [x] HTTP `POST /telegram/messages/edit` in `http_api/messages.py`: `EditBody`,
      `_edit_backend_or_503`, error mapping (403/404/409/400/502) like delete
- [x] factory: `_default_edit_backend_factory` + `create_app` param +
      `app.state.edit_backend_factory` in `http_api/app.py`
- [x] MCP tool `telegram_messages_edit` in `http_api/mcp/tools.py` (WRITE_IDEMPOTENT,
      structured_output) + add to `EXPECTED_TOOL_NAMES` in `tests/test_mcp_mount.py`
- [x] add `messages | edit` row to the SKILL.md catalog table (keeps
      `test_skill_inventory.py` green)
- [x] write tests: CLI (happy, dry-run, exit codes), HTTP (200/403/503/400), MCP tool
      registration + call — follow `test_messages_delete_surfaces.py`
- [x] run tests — must pass before task 6

### Task 6: `messages pin` / `messages unpin` domain op + adapter

- [x] create `messages/pinning.py`: `PinBackend` Protocol (`pin_message(chat_id, message_id,
      *, silent, pm_oneside)`, `unpin_message(chat_id, message_id | None)` — `None` = unpin
      all), frozen Request/Result dataclasses, service fns `pin_message`/`unpin_message` with
      `authorizer.require(chat_id, WRITE)` and dry-run support
- [x] adapter `TelethonPinBackend` in `messages/telethon_backend.py` using
      `client.pin_message` / `client.unpin_message` (silent/pm_oneside passthrough);
      `translate_flood_wait`
- [x] write tests: pin/unpin success + dry-run, WRITE denial, unpin-all vs unpin-one,
      adapter arg passthrough + error translation
- [x] run tests — must pass before task 7

### Task 7: pin/unpin surfaces (CLI + HTTP + MCP)

- [x] CLI `messages pin` (`--message-id`, `--silent`, `--pm-oneside`, `--dry-run`) and
      `messages unpin` (`--message-id` optional, `--all` for unpin-all, `--dry-run`) with the
      standard target selector; `_build_pin_backends(config_path)`
- [x] HTTP `POST /telegram/messages/pin` and `/telegram/messages/unpin` with bodies +
      `_pin_backend_or_503`; factory + `create_app` param + `app.state.pin_backend_factory`
- [x] MCP tools `telegram_messages_pin` / `telegram_messages_unpin` + `EXPECTED_TOOL_NAMES`
- [x] SKILL.md catalog rows for `messages pin` and `messages unpin`
- [x] write tests: CLI/HTTP/MCP surface tests incl. dry-run and 503 path
- [x] run tests — must pass before task 8

### Task 8: `messages download` domain op + adapter (READ gate)

- [x] create `messages/media_download.py` (name avoids clashing with existing
      `downloads.py` URL-fetcher): `MediaDownloadBackend` Protocol
      (`download_media(chat_id, message_id, target_path) -> saved path/size/mime`), frozen
      Request/Result, service fn with `authorizer.require(chat_id, READ)`, target-path
      resolution (`out` file vs `dir` + original filename), clear error when the message has
      no downloadable media, optional max-size guard (reuse limits pattern from
      `messages/attachments.py`), dry-run support
- [x] adapter `TelethonMediaDownloadBackend` using `client.get_messages` +
      `client.download_media`; `translate_flood_wait`
- [x] write tests: success (file written path returned), no-media error, READ denial,
      dry-run, size-limit rejection, adapter translation
- [x] run tests — must pass before task 9

### Task 9: download surfaces (CLI + HTTP + MCP)

- [x] CLI `messages download`: `--message-id`, `--out` (file) / `--dir` (directory,
      mutually exclusive), target selector, `--dry-run`
- [x] HTTP `POST /telegram/messages/download`: body with `message_id` + optional
      `out_dir`; response returns the **server-side saved path** + size/mime (no
      base64/streaming in this iteration — documented in Technical Details); factory +
      `_download_backend_or_503` + `app.state` wiring
- [x] MCP tool `telegram_messages_download` (read-only annotation is wrong — it writes a
      local file; use non-destructive write hints) + `EXPECTED_TOOL_NAMES`
- [x] SKILL.md catalog row for `messages download`
- [x] write tests: CLI/HTTP/MCP surfaces, 503 path, invalid flag combos (exit 2 / 400)
- [x] run tests — must pass before task 10

### Task 10: `messages search` domain op + adapter (READ gate)

- [x] create `messages/search.py`: `SearchBackend` Protocol
      (`search_messages(chat_id, query, *, from_user, limit, topic_id) -> list[RecentMessage]`),
      service fn `search_messages(...)` with `authorizer.require(chat_id, READ)`, required
      non-empty `query`, optional `minutes` time-window filtering done in the service like
      `get_recent_messages`, newest-first ordering, reuse the `RecentMessage` row shape
- [x] adapter `TelethonSearchBackend` using `client.iter_messages(chat, search=query,
      from_user=..., limit=..., reply_to=topic_id)`; `translate_flood_wait`
- [x] write tests: query matching via fake backend, empty-query rejection, minutes window,
      limit, READ denial, adapter arg passthrough + translation
- [x] run tests — must pass before task 11

### Task 11: search surfaces (CLI + HTTP + MCP)

- [x] CLI `messages search`: `--query` (required), `--from`, `--limit`, `--minutes`,
      `--topic-id`, target selector (no `--dry-run`: read-only like `recent`)
- [x] HTTP `GET /telegram/messages/search` (query params mirroring `recent`) +
      `_search_backend_or_503` + factory + `app.state` wiring
- [x] MCP tool `telegram_messages_search` (read-only annotations like recent) +
      `EXPECTED_TOOL_NAMES`
- [x] SKILL.md catalog row for `messages search`
- [x] write tests: CLI/HTTP/MCP surfaces incl. 503 and validation errors
- [x] run tests — must pass before task 12

### Task 12: Verify acceptance criteria

- [x] verify all Overview requirements implemented (perf path never calls `get_entity` for
      membership; all four ops gated correctly; per-rule edit override resolution matches
      delete's) — `_fetch_folder_map` prefers `list_folder_chat_ids()` (bare peer ids, no
      `get_entity`); edit=WRITE, pin/unpin=WRITE, download=READ, search=READ; edit and delete
      share `_resolve_session_only`
- [x] verify edge cases: access config absent (allow-all + gates default from policy),
      `folder_cache_ttl: 0`, marked vs bare chat ids across all new ops — covered by
      `test_folder_membership_ids.py`, `test_folder_membership_cache.py`,
      `test_access_folder_cache.py`, `test_access.py`
- [x] run full test suite `pytest` — 1382 passed
- [x] run `ruff check src tests` — all issues fixed (No issues found)
- [x] verify test coverage of new modules is in line with the project standard — dedicated
      test files per new module (edit/pin/download/search domain + surfaces, membership
      ids/cache) covering success + error scenarios; project ships no coverage tooling

### Task 13: [Final] Update documentation

- [ ] SKILL.md: per-command deep-dive sections for `edit`/`pin`/`unpin`/`download`/`search`
      (catalog rows were added in surface tasks); re-sync to
      `~/.claude/skills/telegram-assistant/SKILL.md`
- [ ] README.md: Commands bullets + MCP tool catalog rows for the 5 new tools; note
      `edit_only_session_messages` and `folder_cache_ttl` in the access-config docs
- [ ] CLAUDE.md: extend the access section with `edit_only_session_messages` and the
      membership cache/TTL behavior
- [ ] update `docs/TODO.md`: check off the five implemented items

## Technical Details

- **Membership map shape**: `dict[str, set[int]]` (folder title → bare chat ids) at the
  backend; authorizer inverts to `dict[int, set[str]]` as today. Bare ids via
  `_canonical_chat_id` so marked/bare requests match.
- **Cache payload**: single JSON row `{folder_name: [chat_id, ...]}` + `fetched_at`
  (caller-supplied `time.time()`); one Telegram account per deployment ⇒ one row suffices.
  TTL check = `now - fetched_at < folder_cache_ttl`. Stale fallback only on fetch error;
  a stale-served decision is bounded by TTL + outage window (accepted trade-off).
- **Edit gate**: reuses `SentMessageRegistry` (in-memory, per process) — same semantics and
  limitation as delete: after a server restart the registry is empty, so session-gated edits
  of older messages are denied until `edit_only_session_messages: false` (globally or
  per rule).
- **Telegram edit limits**: only own messages, ~48h window, text must change — surface
  Telethon errors (`MessageEditTimeExpired`, `MessageAuthorRequired`, `MessageNotModified`)
  as 400/403-style domain errors, not 500s.
- **Download response** (HTTP/MCP): server-side file path + metadata; no base64/streaming in
  this iteration (YAGNI — CLI is the primary consumer; revisit if a remote MCP client needs
  bytes).
- **Search**: Telethon `iter_messages(search=...)` uses Telegram server-side search;
  `minutes` filtering client-side in the service for parity with `recent`.
- **Error taxonomy** unchanged: `AccessDenied` → CLI exit 3 / HTTP 403; entity-not-found →
  exit 2 / 404; ambiguous → 409; `FloodWaitError` → 502 on HTTP, pause+retry in worker.

## Post-Completion

*No checkboxes — external/manual items.*

**Manual verification:**
- Live timing check with a real session: `telegram-assistant messages send` with `folder:`
  rules active should complete in ~1–2s (first call) and faster with warm cache.
- Live smoke of `messages edit/pin/unpin/download/search` against the `Client chat test`
  chat via `scripts/e2e_*.sh`-style calls (requires authorized session; extend
  `scripts/e2e_cli_test.sh` if desired).

**External updates:**
- Redeploy the Docker service so the schema bump creates the cache table.
- If other machines sync `~/.claude/skills/telegram-assistant/SKILL.md`, re-sync there too.
