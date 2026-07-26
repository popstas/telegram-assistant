# PR #17 review fixes: download collisions, folder_id access, pin pacing, search date range

## Overview

Address all confirmed findings from the PR #17 review (https://github.com/popstas/telegram-assistant/pull/17, comments by wyrtensi, 2026-07-24), in the reviewer's priority order:

1. **Download overwrite**: `messages download` into `out_dir` silently overwrites an existing file with the same name. Fix: never overwrite — atomically pick a free name (`report (1).pdf` style).
2. **Folder access by name collides**: access rules and the folder-membership cache are keyed by folder *name*. Telegram allows two folders with the same title; `list_folder_chat_ids()` does a plain assignment per title, so the last folder wins and chats in the earlier one get wrongly denied. Fix: key the internal model/cache by stable `folder_id`, keep name rules as a compat layer that **unions all** same-named folders, and add a new `folder_id` rule target for exact selection.
3. **Pin/unpin flood**: rapid single `pin` calls hit `FLOOD_WAIT` (observed: 404s after 3 quick pins) and today the error is just translated and surfaced — no pacing, no retry. Fix: shared server-side pacing + FLOOD_WAIT sleep/retry in the domain layer so CLI, HTTP and MCP all get it; on exhausted budget report the next-retry time.
4. **Search fixed date range**: only relative `minutes` exists, and it is applied client-side *after* the backend returned the first `limit` rows (older matches are lost). Also `query` + `topic_id` go through `iter_messages(search=…, reply_to=…)` — the reviewer flagged the combination as fragile. Fix: add `from_date`/`to_date` (ISO-8601 with timezone, inclusive, UTC-normalised), rewrite the Telethon adapter to a single `functions.messages.SearchRequest` carrying `q`, `from_id`, `top_msg_id` and both dates simultaneously, with proper `offset_id` pagination until `limit` matching rows are collected.

Non-blocking observation (symlink hardening of `download_root`) goes to Post-Completion — the reviewer explicitly marked it as not a practical risk for single-user deployments.

## Context (from discovery)

- Download: `src/telegram_assistant/messages/media_download.py` — `_resolve_target_path` (~line 160) joins `out_dir + basename(filename)` with no collision handling; `TelethonMediaDownloadBackend.download_media` (`messages/telethon_backend.py:355`) writes to the exact path. Surfaces: HTTP `POST /telegram/messages/download` (`http_api/messages.py:1224`, `_resolve_download_dir:598`), CLI `messages download` (`cli/main.py:5381`), MCP `_resolve_download` (`http_api/mcp/tools.py:985`).
- Folder access: `access/service.py` — `Authorizer` keys `_folder_caps`/`_folder_delete_only`/`_folder_edit_only` by name (~lines 170–181), `_fetch_folder_map:305`, `_invert_folder_map:332`. `folders/telethon_backend.py:106` `list_folder_chat_ids()` returns `dict[str, set[int]]`; line 130 assigns per-title (collision). `folder_id` **is** available from `DialogFilter.id` (`list_folders()` already uses it, line 76). Cache: `persistence/folder_cache.py` (`FolderMembershipCache`, table `folder_membership_cache`, JSON payload `MembershipMap = dict[str, set[int]]`). Config: `AccessRule` in `config/models.py:52` (has `folder: str | None`, no `folder_id`).
- Pinning: `messages/pinning.py` (`pin_message:125`, `unpin_message:166` — direct backend call, no retry); `TelethonPinBackend` (`messages/telethon_backend.py:296`) only wraps errors via `translate_flood_wait`. FLOOD_WAIT retry pattern to mirror: `worker/queue.py` `run_operation` (margin `_fw_margin=5.0`, budget `_max_fw_retries=6`, injectable `_sleep`). `FloodWaitError` lives in `worker/queue.py:31` (carries `.seconds`).
- Search: `messages/search.py` (`SearchBackend` protocol line 27, domain `search_messages:47`, `minutes` post-filter at 96–113); `TelethonSearchBackend.search_messages` (`messages/telethon_backend.py:397`) uses `iter_messages(search=, from_user=, reply_to=, limit=)`. Surfaces: HTTP `GET /telegram/messages/search` (`http_api/messages.py:1394`), CLI `messages search` (`cli/main.py:3971`), MCP `telegram_messages_search` (`http_api/mcp/tools.py:1100`).
- Tests: `tests/test_messages_download.py`, `test_messages_download_surfaces.py`, `test_folder_membership_ids.py`, `test_folder_membership_cache.py`, `test_access.py`, `test_access_folder_cache.py`, `test_messages_pin.py`, `test_messages_pin_surfaces.py`, `test_messages_search.py`, `test_messages_search_surfaces.py`, `test_messages_telethon_backend.py`, `test_worker_queue.py`.
- Docs contract (CLAUDE.md): any CLI/HTTP/MCP surface change requires updating `skills/telegram-assistant/SKILL.md` + re-sync to `~/.claude/skills/telegram-assistant/SKILL.md`, README Commands/usage + MCP catalog, and `EXPECTED_TOOL_NAMES` in `tests/test_mcp_mount.py` only if tool names change (they don't here — only arguments).

## Development Approach

- **Testing approach**: Regular (code first, then tests in the same task)
- Complete each task fully before moving to the next
- Make small, focused changes
- **CRITICAL: every task MUST include new/updated tests** for code changes in that task
  - tests are not optional - they are a required part of the checklist
  - cover both success and error scenarios
- **CRITICAL: all tests must pass before starting next task** - no exceptions
- **CRITICAL: update this plan file when scope changes during implementation**
- Run tests after each change (`pytest`, lint with `ruff check src tests`)
- Maintain backward compatibility: name-based `folder:` rules keep working (union semantics); `minutes` search keeps working; existing download callers keep working when no collision exists; config without new knobs behaves as before

## Testing Strategy

- **Unit tests**: required for every task (see Development Approach above)
- No UI e2e in this project; live e2e scripts (`scripts/e2e_*.sh`) require a real authorized session — record as skipped if unavailable, list manual checks in Post-Completion

## Progress Tracking

- Mark completed items with `[x]` immediately when done
- Add newly discovered tasks with ➕ prefix
- Document issues/blockers with ⚠️ prefix
- Update plan if implementation deviates from original scope

## What Goes Where

- **Implementation Steps** (`[ ]` checkboxes): code, tests, docs in this repo
- **Post-Completion** (no checkboxes): manual/live-account verification, optional hardening
- Checkboxes only in Task sections

## Implementation Steps

### Task 1: Download without overwrite — unique free filename

- [x] in `messages/media_download.py`, replace the current "resolve path then let backend write" flow with collision-safe creation: when the target from `out_dir + name` (or `out_path`) already exists, generate `stem (1).ext`, `stem (2).ext`, … ; claim the name **atomically** with `os.open(path, os.O_CREAT | os.O_EXCL)` in a loop to avoid races, then hand the claimed path to the backend (Telethon writes into the existing empty file)
- [x] keep `dry_run` reporting the path that *would* be used (first free name at check time), clearly documented as best-effort
- [x] ensure the size-guard cleanup path (`os.remove` after post-transfer size re-check) removes the claimed file, including the empty placeholder on backend failure (wrap transfer in try/except → unlink placeholder, re-raise)
- [x] result keeps returning the actual final `path` so HTTP/CLI/MCP responses show the `(N)` name; no surface signature changes needed
- [x] write tests in `tests/test_messages_download.py`: second download of same filename yields `name (1).ext` and preserves the first file; third yields `(2)`; `out_path` collisions also get a free name; placeholder removed on backend exception; dry-run path reporting
- [x] update/extend `tests/test_messages_download_surfaces.py` if response assertions mention paths
- [x] run tests - must pass before next task

➕ `_claim_path` also creates the target directory when missing (`os.makedirs(..., exist_ok=True)`) — claiming a name requires the parent to exist, and previously a non-existent `out_dir` would only fail deep inside Telethon.

### Task 2: Folder membership keyed by folder_id (backend + cache)

- [ ] introduce a folder-membership structure carrying identity: change `folders/telethon_backend.py` `list_folder_chat_ids()` (and the `FolderBackend` protocol users) to return per-folder entries `{folder_id: int, folder_name: str, chat_ids: set[int]}` (e.g. `dict[int, FolderChats]` or `list[FolderChats]` dataclass) instead of `dict[str, set[int]]` — no per-title assignment, so same-named folders no longer collide
- [ ] update `persistence/folder_cache.py` payload to persist `folder_id` + `folder_name` + ids; bump/version the JSON shape so an old-format cached row is treated as a cache miss (not a crash), and `clear()` still works
- [ ] update `access/service.py` `_fetch_folder_map` / `_invert_folder_map` to build `chat_id -> set[(folder_id, folder_name)]` memberships; keep the `list_folders()` snapshot fallback path consistent
- [ ] write tests: `tests/test_folder_membership_ids.py` — two folders with the same title keep **both** id-keyed entries (the collision regression test); `tests/test_folder_membership_cache.py` — new payload round-trip, old-format row treated as miss
- [ ] run tests - must pass before next task

### Task 3: `folder_id` access rules + name rules union all same-named folders

- [ ] add `folder_id: int | None = None` to `AccessRule` in `config/models.py` as a new target kind (counts toward the "exactly one target kind" validation together with `chat`/`chats`/`folder`/`all`)
- [ ] in `access/service.py` `_ensure_index`, index folder rules two ways: by name (compat) and by id; in `_effective_chat_caps`, a **name** rule matches a chat if *any* of its member folders has that name (union across same-named folders), an **id** rule matches only the exact folder; `matched` diagnostic strings become `folder:<name>` / `folder_id:<id>`
- [ ] keep specificity resolution for `delete_only_session_messages` / `edit_only_session_messages` as chat > folder (id or name — treat both as folder-level; restrictive `true` wins on conflict) > all > policy default
- [ ] verify the hot-reload `on_swap` cache clear (`http_api/app.py:844`) and CLI cache wiring (`cli/main.py` `_cli_folder_membership_cache`) still work with the new payload
- [ ] write tests in `tests/test_access.py` / `tests/test_access_enforcement.py`: name rule grants access to chats in *both* same-named folders; `folder_id` rule targets exactly one of them; `folder_id` rule with per-rule `delete_only_session_messages` override; config validation errors (rule with both `folder` and `folder_id`, rule with neither target)
- [ ] extend `tests/test_access_folder_cache.py` for TTL/stale-serve behavior over the id-keyed map
- [ ] run tests - must pass before next task

### Task 4: Pin/unpin pacing + FLOOD_WAIT retry shared across surfaces

- [ ] add a small pacer to the pin domain (`messages/pinning.py` or a sibling module): before each real (non-dry-run) `pin`/`unpin` backend call, enforce a minimum interval since the previous pin-op; persist the last-op timestamp in SQLite next to `OperationStore` (new small table, e.g. `rate_gate(key, next_allowed_at)`) so separate CLI processes and the server share pacing state; injectable clock/sleep for tests
- [ ] add config knob `telegram.pin_min_interval_seconds` (default conservative, e.g. 2.0; `0` disables pacing) in `config/models.py`, threaded from HTTP app state, CLI builder, and MCP the same way other knobs are
- [ ] on `FloodWaitError` from the backend, sleep `seconds + margin` and retry within a bounded budget (mirror `worker/queue.py` semantics: margin ~5s, max retries); when the budget is exhausted or the wait exceeds a sane cap, raise a structured error carrying `retry_after_seconds`
- [ ] surface `retry_after_seconds` in all three surfaces: HTTP keeps the existing `_translate_flood_wait` status but includes retry-after in the body/header; CLI prints the next-attempt time; MCP returns it in the tool error payload — no tool renames
- [ ] write tests in `tests/test_messages_pin.py`: pacing delays the second rapid pin (fake clock, no real sleep); FLOOD_WAIT → sleep+retry → success; budget exhausted → structured error with `retry_after_seconds`; `dry_run` skips pacing and backend; `pin_min_interval_seconds: 0` disables pacing
- [ ] extend `tests/test_messages_pin_surfaces.py`: HTTP/CLI/MCP propagate retry-after info
- [ ] run tests - must pass before next task

### Task 5: Search date range — domain contract and validation

- [ ] extend `messages/search.py`: `SearchBackend.search_messages` and domain `search_messages(...)` accept `from_date: datetime | None`, `to_date: datetime | None`
- [ ] domain validation (before any backend/Telegram call, shared by all surfaces): both bounds required together; each must be timezone-aware (reject naive); `from_date <= to_date`; mutually exclusive with `minutes` — clear `ValueError` messages for each case; normalise both to UTC via `astimezone(UTC)`
- [ ] inclusive semantics: `from_date <= message.date <= to_date`; after the backend returns, the domain re-applies the inclusive UTC check to `RecentMessage.date` (rows without a parseable date are excluded from range results)
- [ ] domain result exposes the normalised UTC bounds so surfaces can echo them (e.g. return object/tuple carrying `applied_from_date`/`applied_to_date`, or surfaces recompute from the validated values)
- [ ] write tests in `tests/test_messages_search.py`: messages exactly at both bounds included; just-outside excluded; single bound → error; naive datetime → error; `from > to` → error; `minutes` + range → error; `minutes`-only path unchanged
- [ ] run tests - must pass before next task

### Task 6: Telethon search backend — single `messages.SearchRequest` with pagination

- [ ] rewrite `TelethonSearchBackend.search_messages` (`messages/telethon_backend.py:397`) to call `functions.messages.SearchRequest` directly: `peer` + `q=query` + `filter=InputMessagesFilterEmpty()` + `min_date=from_date` / `max_date=to_date` + `from_id` (resolved `from_user` input entity) + `top_msg_id=topic_id` + `offset_id`/`limit` — all filters in **one** request, fixing the flagged `query`+`topic_id` combination
- [ ] paginate: request pages, keep only rows passing the final inclusive range check, dedupe message ids, advance `offset_id` to the last processed message, stop on `limit` collected / empty page / non-advancing offset; preserve newest-first order
- [ ] keep `translate_flood_wait` wrapping and the existing row mapping (`_media_summary` fallback, `reply_to_msg_id`, ISO date)
- [ ] write tests in `tests/test_messages_telethon_backend.py` with a fake client: the built `SearchRequest` contains `q`, `from_id`, `top_msg_id`, both dates and `limit` simultaneously; multi-page collection returns exactly `limit` in-range messages newest-first; dedupe and non-advancing-offset termination; no-range and `query`+`topic_id`-only calls still work
- [ ] run tests - must pass before next task

### Task 7: Date range on CLI, HTTP and MCP surfaces

- [ ] CLI `messages search` (`cli/main.py:3971`): add `--from-date` / `--to-date` (ISO-8601 strings parsed to aware datetimes; parse errors → clear message, exit 2 semantics consistent with existing validation)
- [ ] HTTP `GET /telegram/messages/search` (`http_api/messages.py:1394`): add `from_date` / `to_date` query params typed Pydantic `AwareDatetime`; domain `ValueError` → existing HTTP 400 path; response echoes normalised UTC `from_date`/`to_date` alongside the existing echoed params
- [ ] MCP `telegram_messages_search` (`http_api/mcp/tools.py:1100`): add `from_date: datetime | None` / `to_date: datetime | None` args delegating to the same domain validation (no duplicate business validation in MCP)
- [ ] CLI and MCP outputs also include the applied UTC bounds for reproducibility
- [ ] write tests in `tests/test_messages_search_surfaces.py`: all three surfaces accept the range and return echoed UTC bounds; all three reject naive dates / single bound / `minutes`+range with 400 / exit-code / tool-error respectively; combination `query + from_user + topic_id + range` passes through to the domain intact
- [ ] run tests - must pass before next task

### Task 8: Verify acceptance criteria

- [ ] re-read both PR #17 review comments and check every confirmed defect and recommendation is addressed: no-overwrite downloads; folder_id-keyed cache + union-by-name compat + `folder_id` rule; pin pacing/FLOOD_WAIT retry with next-attempt reporting across CLI/HTTP/MCP; `from_date`/`to_date` via single SearchRequest with the reviewer's target test list covered
- [ ] verify edge cases: same-title folders regression test, download `(N)` race claim, pacing with `0` interval, range bounds inclusivity at both edges
- [ ] run full test suite (`pytest`) — all pass
- [ ] run `ruff check src tests` — clean
- [ ] confirm backward compat: config without `folder_id`/`pin_min_interval_seconds` and search without range behave exactly as before

### Task 9: [Final] Update documentation

- [ ] update `skills/telegram-assistant/SKILL.md` sections for `messages download` (unique-name behavior), `messages pin`/`unpin` (pacing, retry-after), `messages search` (`--from-date`/`--to-date`), access rules (`folder_id` target, same-name union semantics); re-sync to `~/.claude/skills/telegram-assistant/SKILL.md`
- [ ] update `README.md`: CLI bullets (lines ~97–100), access config block (~130–148: `folder_id` rule example + same-name note), HTTP endpoints (~177–179), MCP catalog args (~221–226), `download_root` section (~318–323), new `pin_min_interval_seconds` knob
- [ ] update `CLAUDE.md` Config/architecture paragraphs describing `list_folder_chat_ids()` (now id-keyed), the folder cache payload, access rule targets, download no-overwrite, pin pacing
- [ ] verify `tests/test_skill_inventory.py` and `tests/test_mcp_mount.py` pass (no MCP tool renames expected)
- [ ] run full test suite one last time

## Technical Details

- **Unique filename**: split `name.ext` on the last dot (respect names without extension); candidate sequence `name.ext`, `name (1).ext`, `name (2).ext`…; claim via `os.open(candidate, os.O_CREAT | os.O_EXCL | os.O_WRONLY)` + close — the created empty file is the reservation; Telethon `download_media(file=path)` then writes into it. Cap attempts (e.g. 1000) with a clear error.
- **Folder membership payload v2** (SQLite `folder_membership_cache` JSON): `{"version": 2, "folders": [{"id": 5, "name": "Clients", "chat_ids": [...]}, ...]}`; loader returns miss on missing/old version.
- **Rule matching**: memberships resolve to `set[tuple[folder_id, folder_name]]` per chat; name-rule lookup matches on the name component (naturally unioning same-named folders), id-rule on the id component.
- **Pacer state**: `rate_gate` table `(key TEXT PRIMARY KEY, next_allowed_at REAL)`; key `"pin:<chat_id>"` (per-chat) — Telegram pin limits are per-chat in practice; a global fallback key is unnecessary complexity (YAGNI). On FLOOD_WAIT, write `now + seconds + margin` into the gate so *other* processes also back off.
- **SearchRequest paging**: Telegram returns newest-first; `max_date` in `SearchRequest` is exclusive-ish/second-granular in places — that is exactly why the domain re-applies the inclusive UTC check after mapping; the adapter may over-fetch (e.g. `max_date = to_date + 1s`) and let the domain filter trim, as long as tests pin the inclusive contract.
- **Error taxonomy** unchanged: validation → 400 / CLI exit 2-style messages; `AccessDenied` → 403/exit 3; FLOOD_WAIT budget exhausted → existing flood-wait HTTP mapping + `retry_after_seconds`.

## Post-Completion

*Items requiring manual intervention or external systems — informational only*

**Manual verification** (needs authorized live session + `Clients`/`Client chat test` fixtures):
- re-run reviewer's manual checklist: rapid 5× pin series now paces instead of dying on `FLOOD_WAIT 404`; second download of a same-named attachment produces `name (1).ext`; search «с 1 июля по 10 июля» via `--from-date/--to-date` returns bounded, newest-first results; access rule on one of two same-named folders behaves per `folder_id`
- `scripts/e2e_test.sh` / `e2e_cli_test.sh` / `e2e_http_extras_test.sh` against the real test account
- silent-pin push-notification absence still needs a second chat participant to confirm

**Optional hardening** (reviewer's non-blocking note):
- defense-in-depth symlink resolution inside `download_root` for multi-user deployments (`os.path.realpath` both sides before the prefix check in `_resolve_download_dir`)
