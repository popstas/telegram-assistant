# Topics Layout (list / tabs): Config, CLI, HTTP, Skill, Docs

## Overview

Telegram forum chats expose two display modes for their topics: a vertical **list** (default) or horizontal **tabs** across the top. Today `telegram-assistant` always leaves new forums on Telegram's default ("list") and has no way to inspect or change the layout. This plan adds:

1. A `telegram.defaults.topics_layout: "list" | "tabs"` config option that is applied automatically when `groups create` makes a new forum.
2. CLI commands `groups set-layout` and `groups get-layout` to read and change the layout of any existing chat.
3. HTTP API parity (`POST` / `GET /telegram/groups/layout`).
4. Updates to `skills/telegram-assistant/SKILL.md` (and synced user-level copy), `README.md` (full CLI inventory), and `CLAUDE.md` (rule: always update the skill when CLI/HTTP changes).

Reference chats the user wants to verify against:

- `-1003915612716` — currently **list**
- `-1003911170598` — currently **tabs**

## Context

- Adopted from `~/.claude/plans/add-config-option-and-distributed-hamming.md` (ralphex-plan output).
- Telethon semantics (verified against Telegram core docs):
  - `ToggleForumRequest(channel, enabled=True, tabs=True)` → tabs view
  - `ToggleForumRequest(channel, enabled=True, tabs=False)` → list view
  - Read via `GetFullChannelRequest(channel)`: `forum_tabs` is a flag on the `Channel` constructor (flags2.19), not on `ChannelFull`. Locate the matching `Channel` in `messages.ChatFull.chats` by `full_chat.id`.
  - `ToggleViewForumAsMessagesRequest` is **not** the right call — it controls a per-account preference, not the admin layout.
- Critical files:
  - `src/telegram_assistant/config/models.py` — add `TopicsLayout` literal + field on `TelegramDefaults`.
  - `src/telegram_assistant/groups/service.py` — extend `GroupBackend` protocol, add `set_topics_layout` and `get_topics_layout` service functions, hook into `create_group` to apply default layout after creation.
  - `src/telegram_assistant/groups/telethon_backend.py` — implement layout set/get against Telethon.
  - `src/telegram_assistant/persistence/keys.py` (or wherever `topic_close_key` lives) — add `group_layout_set_key(chat_id, layout)`.
  - `src/telegram_assistant/cli/main.py` — add `groups set-layout` and `groups get-layout` subcommands; wire config default into `groups create`.
  - `src/telegram_assistant/http_api/groups.py` — add `POST /telegram/groups/layout` and `GET /telegram/groups/layout`.
  - `skills/telegram-assistant/SKILL.md` (mirrored to `~/.claude/skills/telegram-assistant/SKILL.md`).
  - `README.md` — add a Commands reference section.
  - `CLAUDE.md` — add an "Updating CLI/HTTP" rule.
- Reused patterns:
  - `topics/service.py::close_topic` (lines 753–819) — template for single-chat mutation with `OperationStore` and `FloodWaitError` → `needs_review`.
  - `topics/service.py::TopicBackend` protocol — shape for backend method additions.
  - `http_api/topics.py::build_router` — request-body + router-factory shape.
  - `cli/main.py` topics `close` command (lines 1087–1250) — template for the new CLI subcommands.
- Approach: treat layout as a single-chat admin mutation reusing the existing service/backend/CLI/HTTP fourfold pattern. Set-layout goes through `OperationStore` so FLOOD_WAIT is handled like other mutations; get-layout is a plain read. Config default applied unconditionally after `groups create` when `enable_topics=True` (idempotent). Boolean ↔ string mapping centralized in `_layout_to_tabs` / `_tabs_to_layout` helpers.

## Development Approach

- Testing approach: regular (code first, then tests) using fake backends.
- Complete each task fully before moving to the next.
- Make small, focused changes; maintain backward compatibility (default `topics_layout="list"` matches today's behavior).
- Update this plan file when scope changes during implementation.
- Mark completed items with `[x]` immediately when done; add `➕` prefix for newly discovered tasks, `⚠️` for blockers.

## Testing Strategy

- **Unit tests**: required for every task; one `tests/test_groups_layout.py` is the new home for set/get layout coverage; extend `tests/test_groups.py` for the create-flow integration.
- **CLI tests** via Typer `CliRunner` + fake backend factory (see existing `tests/test_cli_*.py`).
- **HTTP tests** mirror `tests/test_http_topics.py`: success, 503 on unbuilt backend, 422 on invalid enum, bearer-token auth required.
- **Skill inventory guard**: a new `tests/test_skill_inventory.py` parses `SKILL.md`'s catalog and asserts it lists every CLI command exported by `cli/main.py` (cheap structural guard against future drift).
- Run `pytest` and `ruff check src tests` after each task; both must be clean before moving on.
- Live e2e (`scripts/e2e_cli_test.sh`) extension is optional — flagged at Task 10 for user review.

## Progress Tracking

- Mark completed items with `[x]` immediately when done.
- Add `➕` prefix for newly discovered tasks.
- Add `⚠️` prefix for blockers; document the cause.
- Update plan if implementation deviates from original scope.

## Technical Details

**Telethon calls**:

- Set: `await client(ToggleForumRequest(channel=await client.get_input_entity(chat_id), enabled=True, tabs=bool))`
- Get: `full = await client(GetFullChannelRequest(channel=await client.get_input_entity(chat_id)))`; locate the `Channel` in `full.chats` matching `full.full_chat.id`, then `return bool(getattr(channel, "forum_tabs", False))`. (`forum_tabs` is a flag on `Channel`, not on `ChannelFull`.)

**Idempotency key**: `group_layout_set:{chat_id}:{layout}`. Setting the same layout twice replays the completed operation; switching layouts creates a new operation row.

**Failure handling**: same taxonomy as `close_topic` — `FloodWaitError` → `needs_review` (retryable via `operations retry`); other Telethon errors → `failed` with the error message captured.

**Config default behavior in `groups create`**: applied unconditionally when `enable_topics=True`, since `ToggleForumRequest` is idempotent. A failure during this post-create step does not fail the create call itself — it is logged and an independent layout operation row records the failure for later retry.

**Layout string ↔ bool mapping**: centralized in `_layout_to_tabs(layout: str) -> bool` and `_tabs_to_layout(tabs: bool) -> str` to keep all higher layers talking in `"list" | "tabs"`.

## Implementation Steps

### Task 1: Add `topics_layout` to config schema

- [x] add `TopicsLayout = Literal["list", "tabs"]` in `src/telegram_assistant/config/models.py`
- [x] add `topics_layout: TopicsLayout = "list"` to `TelegramDefaults`
- [x] update any committed example config (search for sample `config.yml` under `docs/` or repo root) to mention the new field
- [x] write a unit test (in `tests/test_config_loader.py` or the existing config test file) asserting the default is `"list"` and that `"tabs"` parses correctly
- [x] write a unit test asserting an invalid value (e.g. `"grid"`) raises a Pydantic validation error
- [x] run `pytest` and `ruff check src tests` — must pass before Task 2

### Task 2: Extend backend protocol and Telethon adapter

- [x] in `src/telegram_assistant/groups/service.py`, extend the `GroupBackend` Protocol with `async def set_topics_layout(self, chat_id: int, tabs: bool) -> None` and `async def get_topics_layout(self, chat_id: int) -> bool`
- [x] in `src/telegram_assistant/groups/telethon_backend.py`, implement `set_topics_layout` using `from telethon.tl.functions.channels import ToggleForumRequest`, calling `ToggleForumRequest(channel=input_channel, enabled=True, tabs=tabs)`; wrap `FloodWaitError` via the existing `translate_flood_wait()` helper
- [x] in the same file, implement `get_topics_layout` using `from telethon.tl.functions.channels import GetFullChannelRequest`: read `forum_tabs` from the matching `Channel` in `full.chats` (resolved by `full.full_chat.id`), since `forum_tabs` is a flag on `Channel`, not on `ChannelFull`
- [x] create `tests/test_groups_layout.py` with a fake backend that records calls, asserting the service-layer mapping (string ↔ bool) round-trips correctly (deferred to Task 3, which lands the service helpers; this task covers the adapter-level round-trip)
- [x] write error-case tests in `tests/test_groups_layout.py` for the Telethon adapter (FLOOD_WAIT translation, missing attribute defaults to `False`)
- [x] run `pytest tests/test_groups_layout.py` and full `pytest` — must pass before Task 3

### Task 3: Add `set_topics_layout` and `get_topics_layout` service functions

- [x] add `LayoutSetRequest` / `LayoutSetResult` dataclasses (with `.to_payload()` / `.from_dict()`) in `groups/service.py`, matching the project's request/result conventions
- [x] add `set_topics_layout(request, *, backend, store, config) -> (LayoutSetResult, OperationRecord)` mirroring `close_topic`: idempotency key `group_layout_set:{chat_id}:{layout}`, FLOOD_WAIT → `needs_review`, other errors → `fail_operation`
- [x] add `get_topics_layout(chat_id, *, backend) -> "list" | "tabs"` (no OperationStore; pure read)
- [x] add helpers `_layout_to_tabs(layout)` and `_tabs_to_layout(tabs)`
- [x] register `group_layout_set_key(chat_id, layout)` alongside the other idempotency keys (in `persistence/idempotency.py`)
- [x] write success-path tests in `tests/test_groups_layout.py`: replay-on-completed, fresh write path, get returns correct string for each bool, helpers round-trip
- [x] write error-path tests: FLOOD_WAIT → needs_review status, generic backend exception → failed status with message captured
- [x] run `pytest` — must pass before Task 4

### Task 4: Add CLI commands `groups set-layout` and `groups get-layout`

- [x] in `cli/main.py`, add `@groups_app.command("set-layout")` accepting `--chat-id`, `--layout list|tabs` (default sourced from `config.telegram.defaults.topics_layout`), `--dry-run`, `--config`
- [x] add `@groups_app.command("get-layout")` accepting `--chat-id`, `--config`; prints `list` or `tabs` to stdout
- [x] dry-run must resolve the chat and print the intended change without calling the backend (match the dry-run shape used by `topics close`)
- [x] write CLI tests in `tests/test_cli_groups_layout.py` using `CliRunner` + fake backend factory: success path for both commands
- [x] write CLI tests for dry-run path (no backend call), invalid `--layout` value, missing `--chat-id`
- [x] run `pytest tests/test_cli_groups_layout.py` and full `pytest` — must pass before Task 5

### Task 5: Apply config default during `groups create`

- [x] in `groups/service.py::create_group`, after a successful supergroup create with `enable_topics=True`, call `backend.set_topics_layout(chat_id, _layout_to_tabs(config.telegram.defaults.topics_layout))`
- [x] handle errors from the post-create layout call as a separate operation status (do not fail the create itself — log a warning; the group already exists and idempotency-keyed retries handle layout separately)
- [x] extend `tests/test_groups.py` (or `test_groups_create.py` if present): `topics_layout=tabs` config → backend.set_topics_layout called with `tabs=True`
- [x] extend the same test file: `topics_layout=list` config → called with `tabs=False`
- [x] extend the same test file: `enable_topics=False` → `set_topics_layout` not called at all
- [x] extend the same test file: post-create layout failure → create still returns success; warning logged
- [x] run `pytest` — must pass before Task 6

### Task 6: Add HTTP endpoints

- [x] in `http_api/groups.py`, add `POST /telegram/groups/layout` with body `{ chat_id: int, layout: "list" | "tabs" }` returning the operation record + new layout; respond 503 when `group_backend_factory()` returns `None` (preserve existing factory contract)
- [x] add `GET /telegram/groups/layout?chat_id=…` returning `{ chat_id, layout }`; same 503 contract
- [x] write HTTP tests in `tests/test_http_groups_layout.py` (mirror existing `tests/test_http_*` style): success path for both methods
- [x] write HTTP tests for failure modes: 503 on unbuilt backend, 422 on invalid layout enum, 401/403 when bearer token missing
- [x] run `pytest tests/test_http_groups_layout.py` and full `pytest` — must pass before Task 7

### Task 7: Update SKILL.md and add inventory guard

- [x] add `groups set-layout` and `groups get-layout` rows to the resources/actions catalog in `skills/telegram-assistant/SKILL.md`
- [x] add a per-pair extraction-rules block: required flags (`--chat-id`, `--layout`), config fallback for `--layout`, confirmation policy (single-object state change → confirm by default), typical errors (chat not a forum, missing admin rights, FLOOD_WAIT → needs_review)
- [x] add a short Russian scenario covering "переключи топики чата X на tabs" and "какой layout у чата X"
- [x] sync the file to `~/.claude/skills/telegram-assistant/SKILL.md` (document the sync step as part of the rule added in Task 9)
- [x] add `tests/test_skill_inventory.py` that parses the SKILL.md catalog and asserts it lists every CLI command exported by `cli/main.py` (cheap structural guard against future drift)
- [x] write the inventory test to cover both directions: every CLI command appears in SKILL.md, and every SKILL.md command exists in the CLI (catches both omissions and stale entries)
- [x] run `pytest tests/test_skill_inventory.py` and full `pytest` — must pass before Task 8

### Task 8: Add CLI Commands section to README

- [x] add a `## Commands` section to `README.md` after `## Quick start`, listing all CLI commands grouped by resource (auth, health, version, groups, topics, members, messages, folders, operations) with one-line descriptions sourced from each Typer command's help text
- [x] include the two new layout commands in the `groups` group
- [x] add a short "Updating this list" note pointing readers at `cli/main.py` and the skill inventory test
- [x] no dedicated test, but the `test_skill_inventory.py` guard from Task 7 indirectly catches missing entries when SKILL.md and README drift together
- [x] run `pytest` — must pass before Task 9

### Task 9: Update CLAUDE.md with the "always update the skill" rule

- [x] add a new section (between `## Config` and `## Common commands`) titled `## Updating CLI/HTTP` stating: "When you add or change a CLI command or HTTP endpoint, update `skills/telegram-assistant/SKILL.md` and re-sync it to `~/.claude/skills/telegram-assistant/SKILL.md` in the same change. Update the Commands section in `README.md` too. The `tests/test_skill_inventory.py` guard will fail otherwise."
- [x] sanity-check the rule wording reads cleanly in the existing CLAUDE.md flow
- [x] no automated test; verify by reading the rendered file
- [x] run `pytest` and `ruff check src tests` — must pass before Task 10

### Task 10: Verify acceptance criteria

- [x] verify all requirements from Overview are implemented (config field, CLI set/get, HTTP set/get, groups-create integration, skill update, README commands list, CLAUDE.md rule)
- [x] verify edge cases: non-forum chat → error surfaced; `enable_topics=False` create → no layout call; dry-run prints intended change without backend call
- [x] run the full project test suite (`pytest`) — 488 passed
- [x] run the project linter (`ruff check src tests`) — all issues must be fixed
- [x] confirm test coverage of new modules meets the project standard (e.g. `pytest --cov` if configured) — coverage not configured in pyproject.toml; N/A

## Post-Completion

*Items requiring manual intervention - no checkboxes, informational only*

- Manual verification against the user's reference chats (requires authorized session at `data/sessions/expertizemeAssistant/session.session`):

  ```
  telegram-assistant groups get-layout --chat-id -1003915612716   # expect: list
  telegram-assistant groups get-layout --chat-id -1003911170598   # expect: tabs
  ```

- Round-trip check on a disposable test forum (not the reference chats unless user OKs):

  ```
  telegram-assistant groups set-layout --chat-id <test-chat> --layout tabs
  telegram-assistant groups get-layout --chat-id <test-chat>      # expect: tabs
  telegram-assistant groups set-layout --chat-id <test-chat> --layout list
  telegram-assistant groups get-layout --chat-id <test-chat>      # expect: list
  ```

- HTTP smoke (once uvicorn is running):

  ```
  curl -H "Authorization: Bearer $TOKEN" "http://localhost:8085/telegram/groups/layout?chat_id=-1003911170598"
  curl -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
    -d '{"chat_id": <test-chat>, "layout": "tabs"}' http://localhost:8085/telegram/groups/layout
  ```

- Visually confirm in the Telegram client that `-1003915612716` shows topics as a list and `-1003911170598` shows them as tabs after a round-trip (sanity-check that the boolean mapping matches the UI).

- Trigger "переключи топики чата … на tabs" in Telegram and confirm the agent picks up the updated SKILL.md.

- Optional: add a layout step to `scripts/e2e_cli_test.sh` for live e2e parity (flagged for review at Task 10).

- Release notes: include in `CHANGELOG.md` on next release (`git-cliff` will pick up the conventional commits automatically).
