# First-day fixes for group creation

## Overview

Seven fixes surfaced on the first production day of the Telegram/Planfix assistant, all centered on the group-creation workflow (`groups/service.py`, `groups/telethon_backend.py`) plus the membership and persistence layers. Each fix is small and independent; they share the same domain/adapter split and idempotency conventions already used across the project.

The seven fixes:

1. Configurable chat-title postfix (so Planfix scenarios don't have to append it).
2. Per-request `topics_layout` on group creation (create group, then apply layout).
3. Verify a chat actually still exists before replaying a completed `group_create` — re-create if it was manually deleted.
4. Tolerate empty strings in `members` (skip blanks instead of crashing the operation).
5. Delete @planfix_bot's welcome message after it's added to the group.
6. Delete the `/task <id>` service command and @planfix_bot's reply to it (poll briefly for the reply, then delete welcome + command + reply together).
7. Set default group permissions so all members can create topics and pin messages.

## Context

- Files involved:
  - `src/telegram_assistant/config/models.py` — `TelegramDefaults` (postfix, cleanup, default-permissions knobs)
  - `src/telegram_assistant/groups/service.py` — `GroupCreateRequest`, `_execute_create`, `create_group` replay path
  - `src/telegram_assistant/groups/telethon_backend.py` — `GroupBackend` adapter (new verbs)
  - `src/telegram_assistant/http_api/groups.py` — `GroupCreateBody`
  - `src/telegram_assistant/cli/main.py` — `groups create` options + dry-run output
  - `src/telegram_assistant/members/service.py` — empty-string filtering in bulk add
  - `src/telegram_assistant/persistence/store.py` — `delete_operation` for stale-replay re-create
  - `skills/telegram-assistant/SKILL.md`, `README.md`, `CLAUDE.md` — docs
- Related patterns:
  - Domain `Backend` protocol + `telethon_backend.py` adapter; tests inject fakes.
  - Idempotency keys in `persistence/idempotency.py`; `group_create` keys on `planfix_task_id` else raw `title` — the postfix must NOT enter the key so Planfix replays still match.
  - Best-effort sub-steps append to `skipped[]` and never fail the create (e.g. existing `set_topics_layout`, `task_message`).
- Dependencies: none new; Telethon `EditChatDefaultBannedRights`, `DeleteMessagesRequest`/`delete_messages`, and history reads are already-available Telethon calls.

## Development Approach

- **Testing approach**: Regular (code first, then tests), matching the existing fake-backend unit-test style under `tests/`.
- Complete each task fully (code + tests green) before the next.
- **CRITICAL: every task includes new/updated tests and the suite must pass before moving on.**
- **CRITICAL: all tests must pass before starting the next task.**
- Keep new config knobs minimal with safe defaults; no new abstractions beyond new backend verbs.

## Implementation Steps

### Task 1: Configurable chat-title postfix (fix 1)

**Files:**
- Modify: `src/telegram_assistant/config/models.py`
- Modify: `src/telegram_assistant/groups/service.py`
- Modify: `src/telegram_assistant/cli/main.py` (dry-run effective title)

- [x] Add `group_title_postfix: str = ""` to `TelegramDefaults`.
- [x] In `_execute_create`, compute the effective Telegram title as `request.title + config.defaults.group_title_postfix` and pass it to `create_supergroup`; keep the idempotency key on the raw `request.title` (unchanged).
- [x] Set `GroupCreateResult.title` to the effective title actually used on Telegram.
- [x] Reflect the effective title in the CLI dry-run `planned_actions`/`resolved` output.
- [x] Write tests: postfix applied to created title; idempotency key/replay still keyed on raw title; empty postfix is a no-op.
- [x] Run `pytest` — must pass before Task 2.

### Task 2: Per-request topics_layout on group creation (fix 2)

**Files:**
- Modify: `src/telegram_assistant/groups/service.py` (`GroupCreateRequest`, `to_payload`, `_execute_create`)
- Modify: `src/telegram_assistant/http_api/groups.py` (`GroupCreateBody`)
- Modify: `src/telegram_assistant/cli/main.py` (`--topics-layout` option)

- [x] Add `topics_layout: TopicsLayout | None = None` to `GroupCreateRequest` (+ `to_payload`).
- [x] In `_execute_create`, resolve layout as `request.topics_layout or config.defaults.topics_layout` and pass it to the existing post-create `set_topics_layout` call (group is created first, layout applied after — already the order).
- [x] Add `topics_layout` to `GroupCreateBody` and thread it into `GroupCreateRequest`.
- [x] Add `--topics-layout [list|tabs]` to `groups create` and into the request; show it in dry-run output.
- [x] Write tests: per-request layout overrides config default; absent value falls back to config; layout still skipped when `enable_topics` is false.
- [x] Run `pytest` — must pass before Task 3.

### Task 3: Default group permissions — create_topics, pin_messages (fix 7)

**Files:**
- Modify: `src/telegram_assistant/config/models.py`
- Modify: `src/telegram_assistant/groups/service.py` (protocol + `_execute_create`)
- Modify: `src/telegram_assistant/groups/telethon_backend.py`

- [x] Add config knob (e.g. `default_member_permissions: {create_topics: bool = True, pin_messages: bool = True}`) under `TelegramConfig`/`TelegramDefaults`.
- [x] Add `set_default_permissions(*, chat_id, allow_create_topics, allow_pin_messages)` to the `GroupBackend` protocol; implement in `TelethonGroupBackend` via `EditChatDefaultBannedRights` (allowed flags map to `manage_topics=False`/`pin_messages=False` in the default banned rights, leaving other defaults intact).
- [x] Call it in `_execute_create` right after `create_supergroup`; best-effort (FLOOD_WAIT re-raises like other calls, other errors append to `skipped[]`).
- [x] Write tests with a fake backend: permissions verb invoked with expected flags; failure recorded in `skipped` without failing the create; FLOOD_WAIT propagates to `needs_review`.
- [x] Run `pytest` — must pass before Task 4.

### Task 4: Tolerate empty strings in members (fix 4)

**Files:**
- Modify: `src/telegram_assistant/groups/service.py` (population step)
- Modify: `src/telegram_assistant/members/service.py` (bulk add)

- [x] Add a small helper to drop blank/whitespace-only user references; apply it to `members`/`admins`/reserves before the dedupe/population loop in `_execute_create`.
- [x] In `bulk_add_members`, filter blank items before `normalize_user_ref` so a stray empty string no longer raises and aborts the whole batch (blanks are dropped from the run).
- [x] Write tests: group create with an empty-string member still succeeds and skips the blank; bulk add with a blank entry processes the rest normally.
- [x] Run `pytest` — must pass before Task 5.

### Task 5: Verify real chat existence before replaying group_create (fix 3)

**Files:**
- Modify: `src/telegram_assistant/groups/telethon_backend.py` (+ protocol in `service.py`)
- Modify: `src/telegram_assistant/persistence/store.py`
- Modify: `src/telegram_assistant/groups/service.py` (replay path in `create_group`)

- [x] Add `chat_exists(*, chat_id) -> bool` to the `GroupBackend` protocol and implement in `TelethonGroupBackend` (resolve the entity / `GetFullChannelRequest`; return `False` on the "channel/chat not found" error, re-raise FLOOD_WAIT).
- [x] Add `delete_operation(operation_id)` to `OperationStore` that removes the operation row, its `idempotency_index` entry, and any `operation_items` in one transaction.
- [x] In `create_group`, when the existing op is `COMPLETED`, verify the saved `telegram_chat_id` still exists; if it does not, delete the stale operation and fall through to a fresh `begin_operation` + live create instead of replaying.
- [x] Write tests: replay returns saved result when chat exists; when `chat_exists` is False the stale op is dropped and a new group is created with a new chat id; FLOOD_WAIT during the existence check surfaces as `needs_review` (no silent re-create).
- [x] Run `pytest` — must pass before Task 6.

### Task 6: Clean up service messages — welcome + /task + bot reply (fixes 5 & 6)

**Files:**
- Modify: `src/telegram_assistant/config/models.py`
- Modify: `src/telegram_assistant/groups/telethon_backend.py` (+ protocol in `service.py`)
- Modify: `src/telegram_assistant/groups/service.py` (`_execute_create`)

- [x] Add config knobs under `TelegramDefaults`: `cleanup_service_messages: bool = True` and `task_reply_wait_seconds: int = <small default>`.
- [x] Add backend verbs `get_recent_messages(*, chat_id, limit)` (returns id + sender/bot + reply-to/text summary) and `delete_messages(*, chat_id, message_ids)` to the protocol and `TelethonGroupBackend`.
- [x] In `_execute_create`, after population and the `/task` send: poll `get_recent_messages` up to `task_reply_wait_seconds` for @planfix_bot's reply, then delete the @planfix_bot welcome message, our `/task` message (id already returned by `send_message`), and the bot reply.
- [x] Make cleanup best-effort: failures append to `skipped[]`, never fail the create; gated by `cleanup_service_messages` and only when the `/task` message was sent / bot is in the group.
- [x] Write tests with a fake backend: welcome + command + reply ids deleted when the reply arrives within the wait; reply that never arrives still deletes welcome + command and records the missing reply in `skipped`; cleanup disabled by config is a no-op.
- [x] Run `pytest` — must pass before Task 7.

### Task 7: Verify acceptance criteria

- [x] Run full suite: `pytest` (519 passed)
- [x] Run linter: `ruff check src tests` (all checks passed)
- [x] Verify coverage stays at 80%+ for the changed modules (groups/service 89%, members/service 94%, persistence/store 90%, config/models 98%; telethon adapters intentionally fake-backed per CLAUDE.md).

### Task 8: Update documentation

- [x] Update `skills/telegram-assistant/SKILL.md` for the new `groups create` options/HTTP fields and re-sync to `~/.claude/skills/telegram-assistant/SKILL.md` (satisfies `tests/test_skill_inventory.py`).
- [x] Update the Commands section in `README.md`.
- [x] Update `CLAUDE.md` Config section if new config knobs change documented behavior.
