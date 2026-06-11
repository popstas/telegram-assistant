# Rename groups and topics across all surfaces

## Overview
- Add a "rename" operation for Telegram supergroups and forum topics, exposed on every runtime surface (domain service, CLI, HTTP, MCP, skill).
- Solves: operators can create groups/topics but cannot change a title afterward without going outside this tool.
- Integrates by mirroring the existing `groups create` / `groups layout` / `topics close` shapes: one domain service per area depending on a `Backend` protocol, a Telethon adapter, OperationStore-backed idempotency, the `telegram.access` WRITE gate, and per-surface adapters wired through `app.state.*_backend_factory`.

## Context (from discovery)
- Files/components involved:
  - Domain: `src/telegram_assistant/groups/service.py`, `groups/telethon_backend.py`, `topics/service.py`, `topics/telethon_backend.py`
  - Idempotency: `src/telegram_assistant/persistence/idempotency.py`
  - CLI: `src/telegram_assistant/cli/main.py` (`groups_app`, `topics_app`)
  - HTTP: `src/telegram_assistant/http_api/groups.py`, `http_api/topics.py`, factory wiring in `http_api/app.py`
  - MCP: `src/telegram_assistant/http_api/mcp/tools.py`
  - Skill/docs: `skills/telegram-assistant/SKILL.md` (+ sync to `~/.claude/skills/telegram-assistant/SKILL.md`), `README.md`
  - Tests: `tests/test_groups*.py`, `tests/test_topics*.py`, `tests/test_mcp_mount.py` (`EXPECTED_TOOL_NAMES`), `tests/test_skill_inventory.py`
  - E2E: `scripts/e2e_*.sh`
- Related patterns found:
  - `close_topic` (`topics/service.py`): OperationStore `begin_operation` → backend call → `complete_operation`/replay; FLOOD_WAIT → `mark_needs_review`; WRITE gate via `authorizer.require(chat_id, AccessLevel.WRITE)`.
  - `GroupLayoutSet*` error trio (Failed/Pending/NeedsReview) is the template for rename error classes.
  - `topics close` CLI/HTTP addressing: `--topic-id` OR `--topic-name` (resolved via `list_topics`, raising `TopicNotFoundError`/`AmbiguousTopicNameError`), plus chat addressing `--chat-id`/`--chat-name`/`--entity`/`--folder-name`/`--folder-id`.
  - Backend factories return `None` when Telethon isn't connected → router responds 503.
  - Telethon create uses `CreateForumTopicRequest` via `_import_forum_request`; rename will use `EditTitleRequest` (group) and `EditForumTopicRequest` (topic), wrapped in `translate_flood_wait`.
- Dependencies identified: `OperationStore`, `idempotency` keys, `access.Authorizer`/`AccessLevel`, entity `resolver_factory`, exit-code map (AccessDenied=3, not-found=2, ambiguous=HTTP 409).

## Development Approach
- **Testing approach**: Regular (code first, then tests) with fake backends — no real Telegram traffic in pytest.
- Complete each task fully before moving to the next.
- Make small, focused changes; follow existing file patterns rather than introducing new ones.
- **CRITICAL: every task MUST include new/updated tests** for code changes in that task (success + error scenarios), listed as separate checklist items.
- **CRITICAL: all tests must pass before starting the next task.**
- **CRITICAL: keep this plan in sync** if scope changes during implementation.
- Maintain backward compatibility: `telegram.access` absent ⇒ allow-all; new endpoints/tools are additive.

## Testing Strategy
- **Unit/integration tests**: required every task, using the fake backends and `minimal_config_yaml` fixture (`tests/conftest.py`). Cover: rename happy path, replay (same idempotency key), fresh op (new title = new key), WRITE-denied → AccessDenied, topic name not-found/ambiguous, dry-run does not mutate, FLOOD_WAIT → needs_review.
- **E2E tests**: extend `scripts/e2e_*.sh` with idempotent rename round-trips (rename group/topic → assert new title via inspect/list → rename back). Require an authorized test session; record as skipped when unavailable.
- No UI-based e2e (project has none).

## Progress Tracking
- Mark completed items with `[x]` immediately when done.
- Add newly discovered tasks with ➕ prefix; blockers with ⚠️ prefix.
- Update plan if implementation deviates from original scope.

## What Goes Where
- **Implementation Steps** (`[ ]`): code, tests, docs in this repo.
- **Post-Completion** (no checkboxes): live e2e against a real account, manual MCP inspector smoke.

## Implementation Steps

### Task 1: Add rename idempotency keys
- [x] add `GROUP_RENAME = "group_rename"` and `TOPIC_RENAME = "topic_rename"` constants in `persistence/idempotency.py`
- [x] add `group_rename_key(*, telegram_chat_id, new_title)` → `group_rename:chat={id}:title={new_title.strip()}`
- [x] add `topic_rename_key(*, telegram_chat_id, telegram_topic_id, new_title)` → `topic_rename:chat={id}:topic={tid}:title={new_title.strip()}`
- [x] write tests in `tests/test_idempotency.py` (or existing idempotency test module) for both key functions: stable for same inputs, distinct for different titles
- [x] run tests - must pass before next task

### Task 2: Group rename domain service + Telethon backend
- [x] add `GroupRenameRequest{telegram_chat_id:int, new_title:str, reason:str|None}` with `to_payload()` and `GroupRenameResult{telegram_chat_id, old_title:str|None, new_title, status:"renamed", replayed:bool}` with `to_dict`/`from_dict` in `groups/service.py`
- [x] add error classes `GroupRenameFailed/GroupRenamePending/GroupRenameNeedsReview` (mirror `GroupLayoutSet*`)
- [x] add `async def set_title(*, chat_id:int, title:str) -> None` to the `GroupBackend` Protocol
- [x] add `async def rename_group(*, backend, store, request, authorizer=None) -> tuple[GroupRenameResult, OperationRecord]`: WRITE gate → `begin_operation(GROUP_RENAME, group_rename_key(...))` → replay branch (COMPLETED/FAILED/PENDING/NEEDS_REVIEW) → `backend.set_title(...)` → FLOOD_WAIT → `mark_needs_review` + raise NeedsReview → `complete_operation`
- [x] implement `set_title` in `groups/telethon_backend.py` via `channels.EditTitleRequest(channel=peer, title=...)`, wrapped with `translate_flood_wait`
- [x] write tests in `tests/test_groups_rename.py` with a fake backend: happy path, replay (same key, no second backend call), new title → fresh op, WRITE-denied → AccessDenied, FLOOD_WAIT → NeedsReview
- [x] run tests - must pass before next task

### Task 3: Topic rename domain service + Telethon backend
- [x] add `TopicRenameRequest{telegram_chat_id:int, telegram_topic_id:int, new_title:str, reason:str|None}` + `TopicRenameResult` + `TopicRename{Failed,Pending,NeedsReview}` in `topics/service.py`
- [x] add `async def rename_topic(*, chat_id:int, topic_id:int, title:str) -> None` to the `TopicBackend` Protocol
- [x] add `async def rename_topic(*, backend, store, request, authorizer=None)` service fn: WRITE gate → `begin_operation(TOPIC_RENAME, topic_rename_key(...))` → replay → `backend.rename_topic(...)` → FLOOD_WAIT → needs_review → complete (positive `telegram_topic_id` required, like close)
- [x] implement backend `rename_topic` in `topics/telethon_backend.py` via `channels.EditForumTopicRequest(channel=peer, topic_id=..., title=...)`, wrapped with `translate_flood_wait`
- [x] write tests in `tests/test_topics_rename.py` with a fake backend: happy path, replay, new title → fresh op, WRITE-denied, FLOOD_WAIT → NeedsReview
- [x] run tests - must pass before next task

### Task 4: `groups rename` CLI command
- [x] add `@groups_app.command("rename")` in `cli/main.py`: chat addressing (`--chat-id`/`--chat-name`/`--entity`/`--folder-name`/`--folder-id`), `--new-title` (required), `--reason`, `--dry-run`, `--config-path`
- [x] resolve chat via the shared resolver, build `GroupRenameRequest`, call `rename_group`; `--dry-run` validates + prints plan without mutating
- [x] map errors to exit codes per existing helper (AccessDenied=3, entity-not-found=2); print result JSON like sibling commands
- [x] write CLI tests in `tests/` (mirror existing `groups`/`topics close` CLI tests): success, dry-run (no backend call), access-denied exit 3, not-found exit 2
- [x] run tests - must pass before next task

### Task 5: `topics rename` CLI command
- [x] add `@topics_app.command("rename")` in `cli/main.py`: `--topic-id` OR `--topic-name` (+ chat addressing), `--new-title` (required), `--reason`, `--dry-run`, `--config-path`
- [x] resolve chat + topic (name→id via `list_topics`, surfacing not-found/ambiguous), build `TopicRenameRequest`, call `rename_topic`; `--dry-run` no-mutate
- [x] map errors to exit codes (AccessDenied=3, not-found=2, ambiguous topic handled)
- [x] write CLI tests: success by id, success by name, ambiguous name, not-found, dry-run, access-denied
- [x] run tests - must pass before next task

### Task 6: Group rename HTTP endpoint
- [ ] add `POST /telegram/groups/rename` in `http_api/groups.py`: body `{entity|chat_id|chat_name|folder_*, new_title, reason?}`; resolve chat, enforce WRITE, call `rename_group`
- [ ] return 503 when `group_backend_factory`/resolver returns `None`; 403 AccessDenied, 404 not-found, 409 ambiguous, per the access taxonomy
- [ ] reuse the existing group backend factory (no new factory unless a dedicated rename backend is warranted; document choice inline)
- [ ] write tests in `tests/test_http_*` style: success, 503 when backend unavailable, 403 denied, 404 not-found
- [ ] run tests - must pass before next task

### Task 7: Topic rename HTTP endpoint
- [ ] add `POST /telegram/topics/{topic_id}/rename` in `http_api/topics.py` (id path) and a name-resolving body path matching `close`'s shape; enforce WRITE, call `rename_topic`
- [ ] return 503/403/404/409 consistently; reuse topic backend + resolver factories
- [ ] write HTTP tests: success by id, success by name, ambiguous → 409, not-found → 404, 503 when unavailable, 403 denied
- [ ] run tests - must pass before next task

### Task 8: MCP `telegram_groups_rename` + `telegram_topics_rename`
- [ ] register `telegram_groups_rename` and `telegram_topics_rename` tools in `http_api/mcp/tools.py`, delegating to the same domain services with WRITE enforcement (mirror `telegram_topics_close`)
- [ ] keep MCP arg surface minimal (entity/chat ref + new_title + optional topic id/name + reason), consistent with the trimmed `telegram_messages_send`
- [ ] add both names to `EXPECTED_TOOL_NAMES` in `tests/test_mcp_mount.py`
- [ ] verify the tools honor `mcp.disabled_tools` prefix pruning (covered by existing mount filter)
- [ ] write/extend MCP tests: tools present in mount, callable happy path with fake backend, WRITE-denied path
- [ ] run tests - must pass before next task

### Task 9: Skill + README sync
- [ ] add `groups rename` and `topics rename` to `skills/telegram-assistant/SKILL.md` (commands + usage examples) and the MCP tool catalog
- [ ] re-sync the skill to `~/.claude/skills/telegram-assistant/SKILL.md` (same content)
- [ ] update `README.md`: Commands/usage sections + MCP tool catalog with the two new tools
- [ ] ensure `tests/test_skill_inventory.py` passes (CLI catalog ↔ skill parity)
- [ ] run tests - must pass before next task

### Task 10: Extend live e2e scripts
- [ ] add a group rename round-trip to the appropriate `scripts/e2e_*.sh` (rename `Client chat test` → assert new title via folders/inspect or groups read → rename back to original)
- [ ] add a topic rename round-trip (create/find topic → rename → assert via topics list → rename back), keeping scripts idempotent/re-runnable
- [ ] guard so scripts skip cleanly (recorded as skipped) when no authorized session is present
- [ ] run `ruff check src tests` - fix all issues
- [ ] run full `pytest` - must pass before next task

### Task 11: Verify acceptance criteria
- [ ] verify rename works on all four code surfaces (CLI, HTTP, MCP) + skill/docs updated
- [ ] verify idempotency: identical rename replays; different title is a fresh op; FLOOD_WAIT → needs_review + `operations retry` works
- [ ] verify access gate: WRITE required; allow-all when `telegram.access` omitted
- [ ] verify `--dry-run` mutates nothing on CLI
- [ ] run full `pytest` + `ruff check src tests` - all green
- [ ] verify test coverage of new modules meets project standard

### Task 12: [Final] Documentation + changelog
- [ ] confirm README + SKILL.md reflect final command/tool names and args
- [ ] note any new behavior in docs if a user-facing doc references the command set
- [ ] regenerate changelog if required by the commit flow (`git-cliff` runs via pre-commit)

*Note: ralphex automatically moves completed plans to `docs/plans/completed/`.*

## Technical Details
- **Telethon calls**: group → `telethon.tl.functions.channels.EditTitleRequest(channel=input_entity, title=new_title)`; topic → `telethon.tl.functions.channels.EditForumTopicRequest(channel=input_entity, topic_id=topic_id, title=new_title)`. Both obtain `input_entity` via `client.get_input_entity(chat_id)` and wrap the body in `translate_flood_wait` so the worker recognizes FLOOD_WAIT.
- **Idempotency keys** (target-title keyed): `group_rename:chat={id}:title={t}`, `topic_rename:chat={id}:topic={tid}:title={t}`. Re-running the same rename replays the stored result without touching Telegram; a different title is a new operation.
- **Result payloads**: include `old_title` when cheaply known (e.g. fetched pre-rename), else `None`; `status="renamed"`, `replayed` flag set on replay branch.
- **Access**: `authorizer.require(chat_id, AccessLevel.WRITE)` before mutation; `require()` normalizes the `-100` marker. Absent `telegram.access` ⇒ allow-all no-op.
- **Error taxonomy**: AccessDenied → CLI exit 3 / HTTP 403; EntityNotFound/TopicNotFound → exit 2 / HTTP 404; AmbiguousEntity/AmbiguousTopicName → HTTP 409; backend factory `None` → HTTP 503.

## Post-Completion
*Items requiring manual intervention or external systems — no checkboxes, informational only*

**Manual verification**:
- Run `scripts/e2e_*.sh` against the authorized test session/account and confirm rename round-trips (group + topic) pass; otherwise record as skipped.
- MCP inspector smoke (`npx @modelcontextprotocol/inspector` against `/mcp`) to confirm `telegram_groups_rename` / `telegram_topics_rename` appear and execute.
