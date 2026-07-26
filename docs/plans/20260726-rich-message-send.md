# Rich Message Send (Telegram Articles via InputRichMessageMarkdown)

## Overview

Add the ability to send Telegram "rich messages" (the July 2026 Rich Text Editor articles: headings, tables, quotes, up to 32k chars) through `messages send` on all three surfaces (CLI/HTTP/MCP). On the MTProto level this is the `rich_message: InputRichMessage` parameter of `messages.sendMessage`; we use the `InputRichMessageMarkdown` constructor — the server parses markdown itself, no PageBlock tree building required.

- Problem: the project can only send plain-text messages; articles/long posts are impossible.
- Solution: a `rich_markdown` input (CLI: `--rich-markdown <file.md>`; HTTP/MCP: `rich_markdown` string field) on the existing send operation, reusing all existing plumbing: targeting/entity resolution, dry-run, idempotency, WRITE access gate, SentMessageRegistry.
- v1 scope (YAGNI): markdown only. No inline media inside the article (`InputRichFile`), no HTML variant, no rich edit/read — separate future plans.

Key external facts (verified 2026-07-26):
- Telethon 1.44.0 (layer 227) has `InputRichMessageMarkdown`, `InputRichMessageHTML`, `RichMessage`, and `rich_message` kwargs on `SendMessageRequest`/`EditMessageRequest`. Installed 1.43.2 (layer 224) does NOT. Pin must become `telethon>=1.44,<2.0`.
- Telethon's high-level `client.send_message()` does not expose `rich_message` — the backend must use raw `functions.messages.SendMessageRequest`.
- The client-side editor is Premium-only; whether the server requires Premium for programmatic **user-account** sends is UNKNOWN — that's what the spike (Task 1) answers. Data point: Bot API 10.1 bots send rich messages without Premium (see reference project below), so odds are good.

## Reference implementation: telegram-functions-bot (grammY / Bot API)

`/home/popstas/projects/js/telegram-functions-bot` already ships rich message sending via **Bot API 10.1** (`sendRichMessage`), which mirrors the same server feature. Directly reusable knowledge:

- **Payload**: `sendRichMessage(chat_id, rich_message={markdown: raw_text}, ...)` — exactly one of `html`/`markdown` plus optional `is_rtl` / `skip_entity_detection` (Bot API names for MTProto's `rtl` / `noautolink` flags on `InputRichMessageMarkdown`). Raw LLM markdown is passed through with **zero preprocessing** (`src/telegram/send.ts:126-146`).
- **Markdown dialect** (documented at `node_modules/@grammyjs/types/rich.d.ts:19-140`): `# … ######` headings, tables with alignment, task lists, `>` quotes, fenced code with language, `---`, `~~strike~~`, `==marked==`, `||spoiler||`, footnotes `[^id]`, math `$x^2$`/`$$…$$`, `<details>`, `<tg-collage>`, `<tg-slideshow>`, `tg://user?id=` mentions — and **media as plain markdown images** `![](https://…jpg "caption")` (HTTP(S) URLs only, each becomes a separate block). So inline article media may not need `InputRichFile` at all when the media has a public URL.
- **Limits** (server-side, per Bot API docs): 32 768 UTF-8 chars, 500 blocks, 16 nesting levels, 50 media, 20 table columns. No 4096-char splitting on the rich path.
- **Fallback pattern**: rich-first, on any send error log + fall back to legacy parse_mode send (`send.ts:147-163`). For telegram-assistant v1 we deliberately do NOT auto-fall back — an ops tool should fail loudly (explicit error), the caller can re-send as plain text.
- **Known quirks** (`docs/TODO.md:20-21`): LLM-generated markdown often omits `#` on headings / produces single-line tables → silently renders as plain text; `sendRichMessageDraft` returns 400 `TEXTDRAFT_PEER_INVALID` in group chats (drafts are private-chat-only — not relevant to us, we don't send drafts).

## Context (from discovery)

- Send flow: `cli/main.py:3222` (`messages send`) → `http_api/messages.py:719` (`POST /messages`, `MessageSendBody:106` with `_shape:132` validator) → `messages/service.py:446` (`send_message`, `SendMessageRequest:296`, `MessageBackend` protocol `:230`) → `messages/telethon_backend.py:139` (`TelethonMessageBackend.send_message`, high-level only).
- Backend `extra` kwargs are passed **only when set** (`service.py:559-565`) — deliberate contract, pinned by `tests/test_messages.py::test_send_message_text_only_still_omits_media_kwargs:311`. `rich_markdown` must follow the same pattern.
- MCP: `http_api/mcp/tools.py:1154` `telegram_messages_send` builds the shared `MessageSendBody` and calls `_resolve_message_send:636`; tool kwargs are explicit and must be extended by hand. No new tool ⇒ `EXPECTED_TOOL_NAMES` unchanged.
- Access: `authorizer.require(chat_id, WRITE)` at `service.py:505` — applies to rich sends automatically. Registry recording at `service.py:612-616` — automatic too.
- Version-tolerant import precedent: `topics/telethon_backend.py:24-36` — the model for importing `InputRichMessageMarkdown` so Telethon <1.44 yields a clear error, not ImportError at import time.
- Tests: `tests/test_messages.py` (`FakeMessageBackend:38`), `tests/test_messages_telethon_backend.py` (fake Telethon client), surface tests pattern `tests/test_messages_edit_surfaces.py:112`. Dry-run contract: `tests/test_dry_run_contract.py`, `tests/test_dry_run_members_messages.py`.
- Docs guard: `tests/test_skill_inventory.py` — `messages send` already has a catalog row; option changes still require updating `skills/telegram-assistant/SKILL.md` + re-sync to `~/.claude/skills/telegram-assistant/SKILL.md`, README.

## Development Approach

- **Testing approach**: TDD (tests first) — write failing tests against fakes, then implement.
- Complete each task fully before moving to the next.
- Make small, focused changes.
- **CRITICAL: every task MUST include new/updated tests** for code changes in that task
  - tests are not optional — they are a required part of the checklist
  - cover both success and error scenarios
- **CRITICAL: all tests must pass before starting next task** — no exceptions
- **CRITICAL: update this plan file when scope changes during implementation**
- Run tests after each change (`pytest`, `ruff check src tests`).
- Maintain backward compatibility: text-only sends must keep passing no extra kwargs to backends; fake/legacy backends without `rich_markdown` support must keep working.

## Testing Strategy

- **Unit tests**: required for every task (fakes, no real Telegram traffic).
- **E2E**: no UI e2e in this project; live verification happens in the Task 1 spike (real test account) and stays available as a script. Unit/surface tests are the merge gate.

## Progress Tracking

- Mark completed items with `[x]` immediately when done
- Add newly discovered tasks with ➕ prefix
- Document issues/blockers with ⚠️ prefix
- Update plan if implementation deviates from original scope

## What Goes Where

- **Implementation Steps** (`[ ]` checkboxes): code, tests, docs — automatable in this repo.
- **Post-Completion** (no checkboxes): manual client-side rendering checks, Premium account decisions, external monitoring.

## Implementation Steps

### Task 1: Spike — live rich-message send from the test account

Goal: answer two blocking questions before writing feature code: (a) does the server accept `InputRichMessageMarkdown` from this (non-)Premium technical account, (b) what does the success/error surface look like (result Updates shape, error codes like `PREMIUM_ACCOUNT_REQUIRED`, markdown dialect quirks).

- [ ] bump `pyproject.toml` pin to `telethon>=1.44,<2.0`; reinstall into `.venv` (`pip install -e ".[dev]"`)
- [ ] run existing unit test suite on Telethon 1.44 — must pass (regression check for the upgrade itself)
- [ ] write `scripts/spike_rich_message.py`: connect via existing session (`data/sessions/expertizemeAssistant/session.session`), send raw `functions.messages.SendMessageRequest(peer=<Client chat test>, message="", random_id auto, rich_message=types.InputRichMessageMarkdown(markdown=...))` with a sample covering the grammY-documented dialect: heading, list, table with alignment, quote, fenced code, and one media-by-URL block `![](https://…jpg "caption")`; print resulting message id or the exact RPC error
- [ ] run the spike against the live test account (same preconditions as `scripts/e2e_*.sh`; if no authorized session is available, record as skipped and note it here with ⚠️)
- [ ] record findings in this plan under Technical Details (Premium requirement yes/no for a user account — bots don't need it, error taxonomy, how to extract message id from the Updates result, whether media-by-URL works via MTProto markdown); ⚠️ if the server rejects for non-Premium — STOP and surface to the user before continuing to Task 2

### Task 2: Domain layer — `rich_markdown` in service + Telethon backend

- [ ] TDD: add failing tests in `tests/test_messages.py`: send with `rich_markdown` passes it to backend as an extra kwarg; text-only send still omits it (extend `test_send_message_text_only_still_omits_media_kwargs`); validation errors — `rich_markdown` combined with `text`/`files`/`file_urls`/`base64_files` or mass mode → `ValueError`; empty or > 32 768 chars `rich_markdown` → `ValueError`
- [ ] TDD: add failing tests in `tests/test_messages_telethon_backend.py`: fake client asserts raw `SendMessageRequest` is issued with `InputRichMessageMarkdown(markdown=...)`, correct peer, `schedule_date`, and `reply_to`/topic mapping; Telethon-without-rich (import shim returns None) → clear `MessageSendFailed`-style error mentioning Telethon >= 1.44
- [ ] extend `SendMessageRequest` (`messages/service.py:296`): `rich_markdown: str | None = None`; include in `to_payload` under the same redaction rules as `text`; validation in `send_message:446` (mutually exclusive with text/attachments, forbidden in mass mode; allowed with `topic_id`/`reply_to_message_id`/`schedule_at`); pass to backend via the existing only-when-set `extra` kwargs block (`:559-565`)
- [ ] extend `MessageBackend` protocol signature (`service.py:243`) with `rich_markdown: str | None = None`
- [ ] implement in `TelethonMessageBackend.send_message` (`messages/telethon_backend.py:139`): version-tolerant import of `InputRichMessageMarkdown` (pattern of `topics/telethon_backend.py:24-36`); when `rich_markdown` set → raw `functions.messages.SendMessageRequest` with `rich_message=`, `reply_to=InputReplyToMessage(...)` for reply/topic, `schedule_date=` for scheduled; extract message id from the returned Updates (per spike findings)
- [ ] run tests — must pass before next task

### Task 3: HTTP surface — `rich_markdown` field on `POST /telegram/messages`

- [ ] TDD: add failing surface tests (new `tests/test_messages_rich_surfaces.py` or extend existing send surface tests): happy path with fake backend; 400 on `rich_markdown` + `text`; 400 on `rich_markdown` + attachments; 400 in mass mode; 403 without WRITE; dry-run payload includes `rich_markdown: true`/length marker per dry-run contract
- [ ] add `rich_markdown: str | None` to `MessageSendBody` (`http_api/messages.py:106`) and encode exclusivity in the `_shape` validator (`:132`)
- [ ] thread the field through the `send` route (`:720`) into `SendMessageRequest`
- [ ] run tests — must pass before next task

### Task 4: CLI — `--rich-markdown <file.md>` on `messages send`

- [ ] TDD: add failing CLI tests: `--rich-markdown` file read (UTF-8) and passed to domain; error when combined with `--text`/`--file`/`--file-url`/`--mass`; missing/unreadable file → clear error; `--dry-run` payload reflects rich send (update `tests/test_dry_run_members_messages.py` / `tests/test_dry_run_contract.py` as needed)
- [ ] add `--rich-markdown` option to `messages_send` (`cli/main.py:3223`): read file content, validate non-empty, pass into `SendMessageRequest`; extend the dry-run `planned_actions`/`resolved` payload (`:3620-3655`)
- [ ] run tests — must pass before next task

### Task 5: MCP — `rich_markdown` kwarg on `telegram_messages_send`

- [ ] TDD: add failing MCP test (pattern of existing MCP surface tests): tool call with `rich_markdown` reaches the fake backend; exclusivity errors map to the shared body validation
- [ ] add explicit `rich_markdown` kwarg to `telegram_messages_send` (`http_api/mcp/tools.py:1154`) and pass into `MessageSendBody`
- [ ] confirm `EXPECTED_TOOL_NAMES` in `tests/test_mcp_mount.py` needs no change (no new tool)
- [ ] run tests — must pass before next task

### Task 6: Verify acceptance criteria

- [ ] verify all Overview requirements implemented: rich send works on CLI+HTTP+MCP; markdown-only; exclusivity enforced; WRITE gate + registry + idempotency apply; graceful error on Telethon <1.44
- [ ] verify edge cases: empty markdown, mass mode, scheduled rich send, topic-targeted rich send, replay of the same operation id
- [ ] run full test suite (`pytest`)
- [ ] run `ruff check src tests` — all issues fixed
- [ ] re-run `scripts/spike_rich_message.py` as a live sanity check if an authorized session is available (record skipped otherwise)

### Task 7: Update documentation

- [ ] update `skills/telegram-assistant/SKILL.md` (messages send options/scenarios, short note on the rich markdown dialect: headings/tables/quotes/code/media-by-URL, 32k limit) and re-sync to `~/.claude/skills/telegram-assistant/SKILL.md`
- [ ] update `README.md` Commands/usage + HTTP API sections (and MCP tool description if the catalog lists parameters)
- [ ] update `CLAUDE.md` messages/ area description (rich send, telethon >= 1.44)
- [ ] run `pytest tests/test_skill_inventory.py tests/test_skill_structure.py` — must pass

## Technical Details

- **New input**: `rich_markdown` — markdown source string (CLI reads it from a file path). Server-side parsing via `InputRichMessageMarkdown#004b572c { flags, rtl?, noautolink?, markdown:string, files? }`. v1 never sets `files`, `rtl`, or `noautolink` (Bot API analogs: `is_rtl`, `skip_entity_detection`; add later only if needed). Inline media in v1: only via public HTTPS URLs inside the markdown itself (`![](https://…)`) — confirmed dialect feature on the Bot API side, verify via MTProto in the spike; no local-file upload (`InputRichFile`) in v1.
- **Markdown dialect + limits**: Telegram's own rich flavor (see Reference implementation section). Server limits: 32 768 chars / 500 blocks / 16 nesting levels / 50 media / 20 table columns. Surface-level validation in v1: non-empty + ≤ 32 768 chars; everything else is left to the server (its errors are more authoritative than any local lint). No 4096-char splitting on the rich path.
- **No silent fallback**: unlike telegram-functions-bot (which falls back to a legacy parse_mode send on rich errors), a failed rich send here maps to the normal send error taxonomy (`MessageSendFailed`/needs_review) — explicit failure, caller decides.
- **Raw request shape** (Telethon 1.44): `functions.messages.SendMessageRequest(peer=<entity>, message="", rich_message=types.InputRichMessageMarkdown(markdown=md), schedule_date=..., reply_to=types.InputReplyToMessage(reply_to_msg_id=..., top_msg_id=...))`; `random_id` is auto-generated by Telethon. Message-id extraction from the Updates result — confirm exact approach in the spike (candidates: scan for `UpdateMessageID`/`UpdateNewMessage`/`UpdateNewChannelMessage`, or Telethon's `client._get_response_message`).
- **Validation matrix**: `rich_markdown` XOR (`text` | attachments); forbidden with `mass`; allowed with `topic_id`, `reply_to_message_id`, `schedule_at`, `entity`/`chat_id`/`chat_name` targeting. Length: 1..32 768 chars.
- **Backward compat**: backend kwarg passed only when set, so `FakeMessageBackend` and any legacy backends without the kwarg keep working for non-rich sends.
- **Idempotency**: unchanged — same `begin_operation` path; `rich_markdown` participates in the persisted payload like `text` (same service-command redaction rule).
- **Spike findings** (fill in during Task 1):
  - Premium required for programmatic user-account send: _TBD_ (bots via Bot API: not required)
  - Error taxonomy observed: _TBD_
  - Message-id extraction: _TBD_
  - Markdown dialect notes (tables/headings render as expected via MTProto): _TBD_
  - Media-by-URL (`![](https://…)`) works via MTProto markdown: _TBD_

## Post-Completion

**Manual verification:**
- Open the sent article in Telegram Desktop ≥ 7.0.1 and mobile: headings/tables/quotes render correctly; long (>4096 chars) article arrives as a single message.
- If the spike showed Premium is required: decide whether to subscribe the technical account or shelve the feature; the code path should stay behind the clear server error either way.

**External / monitoring:**
- Layers 225–228 are not yet documented on core.telegram.org — behavior (limits, markdown dialect, errors) may shift; re-verify after the next Telethon/layer bump.
- Future follow-ups (separate plans): local-file inline media via `InputRichFile` (may be unnecessary if media-by-URL suffices — see spike findings), `InputRichMessageHTML`, rich `messages edit`, full-content read via `messages.getRichMessage`, `rtl`/`noautolink` flags.
