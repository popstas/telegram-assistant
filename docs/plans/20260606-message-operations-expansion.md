# Telegram message operations expansion

## Overview

Implement the first six open `docs/TODO.md` items after the completed access/entity/read work:

1. Sending media: photos, videos, and documents/files.
2. Scheduled/delayed messages.
3. Mute/unmute a contact or chat.
4. Remove a contact/chat from a Telegram folder.
5. Set/remove emoji reactions on messages.
6. Forward messages from one entity to another.

The last two open TODO items are intentionally excluded from this plan:

- HTTP MCP server with Google OAuth.
- `INTEGRATION.md` / agent setup layer.

## Context

- Existing architecture is a shared domain layer with CLI and HTTP surfaces.
- Domain modules keep Telethon out of `service.py`; production adapters live in `telethon_backend.py`.
- HTTP backend factories on `app.state.*_backend_factory` return `None` as `503 Service Unavailable`.
- Chat-targeting surfaces already support `--entity` / `entity` and resolve before authorizing.
- Access control is enforced in the domain/service layer:
  - read operations require `AccessLevel.READ`;
  - mutating operations require `AccessLevel.WRITE`;
  - unconfigured access remains allow-all.
- Public CLI/HTTP changes must update both `README.md` and `skills/telegram-assistant/SKILL.md`; `tests/test_skill_inventory.py` guards drift.
- Mutating CLI commands should support `--dry-run`.

## Development Approach

- Use TDD for each task: domain tests first, then Telethon adapter tests with fakes, then CLI/HTTP tests.
- Keep tasks independently commit-sized.
- Prefer extending `src/telegram_assistant/messages/` for message-like operations. Add narrow modules only when one file would mix unrelated responsibilities.
- Reuse the existing `_cli_authorizer`, entity resolver, folder resolver, and HTTP access translators.
- Keep backward compatibility for existing plain-text `messages send`.

## Testing Strategy

- Unit tests use fakes only; no real Telegram traffic in pytest.
- Add or extend CLI tests with `CliRunner` and monkeypatched backend builders.
- Add or extend HTTP tests with `TestClient` and injected backend factories.
- Add Telethon adapter tests with fake clients/request classes where possible.
- Extend live e2e scripts only after the fake-backed surfaces are complete.
- Run after each task:

```bash
.venv/bin/pytest -q
ruff check src tests
```

## Technical Details

### Shared message operation model

- Extend `MessageBackend` or introduce narrow protocols in `messages/service.py`:
  - `send_text_message(...)` can stay as the existing `send_message(...)` contract if clearer.
  - `send_media(...)` for file attachments/albums.
  - `schedule_message(...)` can be handled by the same send call if the request shape has `schedule_at`.
  - `forward_messages(...)`.
  - `send_reaction(...)`.
- Keep result shapes serializable through `to_dict()` and replayable when idempotency applies.
- For idempotent mutating message operations, anchor on caller-supplied `operation_id`; reject missing `operation_id` only if the existing text-send behavior already requires it. Otherwise keep current replay semantics.

### Input decisions

- Media CLI:
  - `telegram-assistant messages send --entity ... --text "caption" --file path1 --file path2`
  - `--file` may repeat.
  - `--file-url` may repeat and is passed to Telethon as a URL string.
  - `--caption` is not needed at first; keep `--text` as the caption/text field so existing command shape remains compact.
- Media HTTP:
  - Add JSON fields to `POST /telegram/messages`: `files: list[str] | None`, `file_urls: list[str] | None`.
  - Treat local paths as server-side paths. Do not add multipart upload in this plan; it needs storage and request-size policy not present in the project.
  - Allow `files` and `file_urls` together as one attachment list.
  - Validate local files exist, are regular files, and are not empty before calling Telethon.
  - Validate URL attachments use `http` or `https`; do not prefetch remote URLs for size/type inspection.
- Scheduled messages:
  - CLI accepts `--schedule-at` as ISO-8601 datetime and `--delay` as duration (`10m`, `2h`, `1d`).
  - HTTP accepts `schedule_at` ISO-8601 and `delay_seconds`.
  - Exactly one scheduling mode is allowed.
  - Scheduling still creates/sends through the same service path and operation record; result is `scheduled=True`.
- Mute/unmute:
  - New resource group: `notifications mute` / `notifications unmute` or `chats mute` / `chats unmute`.
  - Prefer `notifications` because the Telethon operation is notification settings, not chat membership.
  - CLI accepts `--entity`, `--chat-id`, or `--chat-name` and optional `--duration`.
  - Allow to pass mute duration in hours (default is forever).
  - HTTP endpoints: `POST /telegram/notifications/mute` and `POST /telegram/notifications/unmute`.
- Folder remove:
  - Add inverse of existing `folders add-chat`.
  - CLI: `folders remove-chat`.
  - HTTP: `DELETE /telegram/folders/{folder_name}/chats`.
  - Idempotent: if the chat is not in the folder, return `already_absent=True` and do not call Telethon mutation.
- Reactions:
  - CLI: `messages react --entity ... --message-id 123 --emoji 👍`; removing uses `--clear`.
  - HTTP: `POST /telegram/messages/reactions` with `message_id`, `emoji | null`, `clear`.
  - WRITE-gated on the target chat.
- Forward:
  - CLI: `messages forward --from-entity ... --message-id 1 --message-id 2 --to-entity ...`.
  - HTTP: `POST /telegram/messages/forward`.
  - Require WRITE on target. Also require READ on source if `telegram.access` is configured; forwarding reads from source and mutates target.

## Implementation Steps

### Task 1: Media send request model and domain behavior

- [x] Add attachment request/result dataclasses to `src/telegram_assistant/messages/service.py`.
- [x] Extend `SendMessageRequest` with `files: tuple[str, ...]`, `file_urls: tuple[str, ...]`, and `schedule_at: datetime | None`, while preserving existing text-only behavior.
- [x] Update `MessageBackend` protocol to accept optional `files`, `schedule_at`, and keep `topic_id`.
- [x] Validate:
  - text or at least one attachment is required;
  - attachment paths/URLs are non-empty;
  - media send is WRITE-gated before operation creation;
  - persisted request payload redacts service-command text as today and stores only attachment references, not file contents.
- [x] Add domain tests in `tests/test_messages.py` for text-only compatibility, media-only, media+caption, albums, empty request rejection, access denied before backend call, and replay behavior.
- [x] Run `.venv/bin/pytest -q tests/test_messages.py`.

### Task 2: Telethon media and schedule adapter

- [x] Move the production message-send adapter out of `TelethonTopicBackend.send_message` fallback into `messages/telethon_backend.py` as `TelethonMessageBackend`.
- [x] Implement:
  - `client.send_message(chat_id, text, reply_to=topic_id, schedule=schedule_at)` for text-only;
  - `client.send_file(chat_id, files, caption=text or None, reply_to=topic_id, schedule=schedule_at)` for attachments.
- [x] Normalize return message ids:
  - single message returns one id;
  - album returns the first id plus `telegram_message_ids` in the result payload.
- [x] Translate Telethon `FloodWaitError` with `translate_flood_wait`.
- [x] Update HTTP app default `message_backend_factory` to use `TelethonMessageBackend` instead of falling back to topic backend.
- [x] Add adapter tests in `tests/test_messages.py` or a new `tests/test_messages_telethon_backend.py` using a fake client.
- [x] Run `.venv/bin/pytest -q tests/test_messages.py tests/test_app_skeleton.py`.

### Task 3: Wire media and schedule into CLI/HTTP

- [x] CLI `messages send`:
  - add repeated `--file`;
  - add repeated `--file-url`;
  - add `--schedule-at`;
  - add `--delay`;
  - include attachments and scheduling in `--dry-run` JSON.
- [x] HTTP `POST /telegram/messages`:
  - add `files`, `file_urls`, `schedule_at`, `delay_seconds`;
  - validate schedule shape with the Pydantic model;
  - return media ids and scheduling fields in the response.
- [x] Add parsing helper for relative delays in CLI and HTTP; test `10m`, `2h`, `1d`, invalid units, and past absolute dates.
- [x] Decide past schedule behavior in code: reject past `schedule_at` with exit code 2 / HTTP 400.
- [x] Add CLI tests for dry-run and real fake-backed media/scheduled sends.
- [x] Add HTTP tests for media and scheduled sends.
- [x] Run `.venv/bin/pytest -q tests/test_messages.py tests/test_dry_run_members_messages.py`.

### Task 4: Folder remove-chat domain, Telethon adapter, CLI, and HTTP

- [x] Extend `FolderBackend` with `remove_chat_from_folder(folder_id: int, chat_id: int) -> None`.
- [x] Add `remove_chat_from_folder(...)` to `folders/service.py`:
  - resolve folder;
  - resolve chat;
  - require WRITE on the resolved chat;
  - if absent, return `already_absent=True`;
  - if present, call backend and return serializable result.
- [x] Implement Telethon removal by editing the target dialog filter `include_peers` and `pinned_peers`, then calling `UpdateDialogFilterRequest`.
- [x] Add `folders remove-chat` CLI with `--dry-run`, mirroring `folders add-chat`.
- [x] Add `DELETE /telegram/folders/{folder_name}/chats` with the same body shape as add-chat.
- [x] Add tests in `tests/test_folders.py`, `tests/test_dry_run_folders_operations.py`, and HTTP coverage for absent/idempotent, present/remove, access denied, and backend failure.
- [x] Update README and skill catalog for `folders remove-chat`.
- [x] Run `.venv/bin/pytest -q tests/test_folders.py tests/test_dry_run_folders_operations.py tests/test_skill_inventory.py`.

### Task 5: Notifications mute/unmute

- [x] Create `src/telegram_assistant/notifications/service.py` with:
  - `NotificationBackend` protocol;
  - `MuteRequest`, `MuteResult`;
  - `mute_chat(...)` and `unmute_chat(...)`.
- [x] WRITE-gate both operations after entity resolution.
- [x] Implement `src/telegram_assistant/notifications/telethon_backend.py` using `UpdateNotifySettings`:
  - mute until a date when duration is provided;
  - mute indefinitely when duration is omitted;
  - unmute restores normal notification settings.
- [x] Register HTTP backend factory in `http_api/app.py`.
- [x] Add `src/telegram_assistant/http_api/notifications.py`.
- [x] Add CLI group `notifications` with `mute` and `unmute`, including `--dry-run`.
- [x] Add tests for domain validation, access denial, CLI dry-run, HTTP success/403/503, and fake Telethon request construction.
- [x] Update README and skill catalog.
- [x] Run `.venv/bin/pytest -q tests/test_notifications.py tests/test_skill_inventory.py`.

### Task 6: Reactions

- [x] Add reaction request/result types and backend protocol in `messages/service.py` or a small `messages/reactions.py` if `service.py` becomes too large.
- [x] Implement `set_message_reaction(...)`:
  - require `message_id > 0`;
  - require either `emoji` or `clear=True`, not both;
  - WRITE-gate the target chat;
  - call backend and return `{telegram_chat_id, telegram_message_id, emoji, cleared}`.
- [x] Implement Telethon reaction adapter using `SendReaction`.
- [x] Add CLI `messages react` with `--message-id`, `--emoji`, `--clear`, entity targeting, and `--dry-run`.
- [x] Add HTTP `POST /telegram/messages/reactions`.
- [x] Add tests for set, clear, invalid shape, access denied, CLI dry-run, HTTP success/400/403, and fake Telethon request construction.
- [x] Update README and skill catalog.
- [x] Run `.venv/bin/pytest -q tests/test_messages_reactions.py tests/test_skill_inventory.py`.

### Task 7: Forward messages

- [x] Add forward request/result types and backend protocol in `messages/service.py` or `messages/forwarding.py`.
- [x] Implement `forward_messages(...)`:
  - validate one or more positive `message_ids`;
  - resolve source and target before authorization in surfaces;
  - require READ on source and WRITE on target;
  - call backend and return forwarded ids.
- [x] Implement Telethon adapter with `client.forward_messages(target, message_ids, from_peer=source)`.
- [x] Add CLI `messages forward`:
  - source: `--from-chat-id` / `--from-entity`;
  - target: existing target flags or `--to-chat-id` / `--to-entity`;
  - repeated `--message-id`;
  - `--dry-run`.
- [x] Add HTTP `POST /telegram/messages/forward`.
- [x] Add tests for validation, source READ denial, target WRITE denial, CLI dry-run, HTTP success/403, and fake Telethon request construction.
- [x] Update README and skill catalog.
- [x] Run `.venv/bin/pytest -q tests/test_messages_forward.py tests/test_skill_inventory.py`.

### Task 8: Documentation, skill sync, e2e, and full verification

- [x] Update `README.md` Commands and usage notes for:
  - media/scheduled `messages send`;
  - `messages react`;
  - `messages forward`;
  - `notifications mute/unmute`;
  - `folders remove-chat`.
- [x] Update `skills/telegram-assistant/SKILL.md` resource/action catalog, confirmation buckets, dry-run supported command list, and per-command extraction rules.
- [x] Sync the skill file to `~/.claude/skills/telegram-assistant/SKILL.md` in the same change. (global path is hardlinked to the repo file — same inode, always in sync)
- [x] Extend live e2e scripts with conservative cases:
  - scheduled message with a near-future time only in the test chat;
  - folder remove/add round-trip for the test chat;
  - reaction set/clear on a message created during the script;
  - forward a test message into the test chat;
  - media send using a small generated temporary text/image file.
- [x] Run `.venv/bin/pytest -q`. (734 passed)
- [x] Run `ruff check src tests`. (All checks passed)
- [x] Run live e2e scripts only when an authorized test session is available: (skipped — requires an authorized Telethon session + live Telegram account, not available in this environment)

```bash
bash scripts/e2e_test.sh
bash scripts/e2e_cli_test.sh
bash scripts/e2e_http_extras_test.sh
```

## Acceptance Criteria

- Plain text `messages send` remains backward compatible.
- Media send works for local server paths and URLs through CLI and HTTP.
- Scheduled sends reject invalid/past schedules and pass future schedules to Telethon.
- Mute/unmute, folder remove, reactions, and forwarding are available in both CLI and HTTP.
- Every mutating operation is WRITE-gated; forward additionally READ-gates the source.
- Every new mutating CLI command has a meaningful `--dry-run`.
- README and `skills/telegram-assistant/SKILL.md` match the CLI inventory.
- Full pytest and ruff pass.

## Post-Completion

- Decide release bump after implementation. This is a backward-compatible feature set, so pre-1.0 SemVer suggests a minor release unless implementation introduces a deliberate breaking change.
- Do not start the excluded MCP/OAuth or integration-guide work from this plan.
