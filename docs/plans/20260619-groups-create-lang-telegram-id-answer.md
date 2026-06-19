# Group create: `lang`, `telegram_id`, and localized `answer`

## Overview

Add three capabilities to the **HTTP** group-create flow (`POST /groups`), modeled on
`~/projects/expertizeme/google-drive-access` (every response carries a localized `answer`
string, and a `lang` field switches ru/en):

1. **`lang` request field** — accepts `"ru"` / `"en"` (default `ru`) and selects the language of
   a new localized `answer` summary string returned in the response. May arrive as a bare string
   **or a list of strings** (take the first element), mirroring google-drive-access `_apply_lang`.
2. **`telegram_id` request field** — when the client member (`members[0]`) is a phone-style
   reference and `telegram_id` is filled, the client is added to the group **by numeric id**
   instead of the phone link. May also arrive as a string or list of strings (take first
   non-empty).
3. **Phone-without-`telegram_id` guard** — if `members[0]` looks like a phone-style t.me link
   (`https://t.me/79222222222`, `https://t.me/+79222222222`) **and** `telegram_id` is empty, the
   group is still created but that client member is **skipped**, and the `answer` warns:
   «Клиента невозможно подключить по номеру телефона без telegram id. Впишите telegram id в
   контакт, после этого отправьте клиенту инвайт».

Problem it solves: the Planfix→HTTP integration that creates client groups needs a human-readable,
language-aware reply (the `answer`) and must not silently fail to connect a client when only a
phone number is known.

### Decisions locked with the user

- **Scope: HTTP only** (`POST /groups`). CLI and MCP surfaces are **not** changed in this work.
- **`answer` is always present** on the group-create response, with a small **ru/en translation
  table**; `lang` switches the language.
- **Phone-without-`telegram_id`**: create the group, **skip** the phone client member (record it in
  `skipped`), and return the warning `answer`.
- **`telegram_id` when filled**: add the client by that numeric id (substitute for the phone-style
  `members[0]`); `answer` → «Группа создана, клиент добавлен».
- **`lang` and `telegram_id` may arrive as a list of strings** — normalize by taking the first
  element.

## Context (from discovery)

Files/components involved:
- `src/telegram_assistant/http_api/groups.py` — `GroupCreateBody` (HTTP request model, line ~87)
  and the `POST /groups` handler that maps body → `GroupCreateRequest` (line ~223) and returns
  `result.to_dict()` (line ~283).
- `src/telegram_assistant/groups/service.py`:
  - `GroupCreateRequest` dataclass (line ~116) + `to_payload()` (persisted for idempotency/replay).
  - `GroupCreateResult` dataclass (line ~162) + `to_dict()` / `from_dict()`.
  - `_execute_create()` (line ~335) — builds `all_members` from `request.members` (line ~470) and
    runs the add-member loop (line ~483), recording failures in `skipped`. Returns
    `GroupCreateResult` (line ~582).
  - `create_group()` (line ~599) — idempotency state machine; replays persisted result on a
    completed key (so `answer` must be persisted via `to_dict`/`from_dict`).
- `src/telegram_assistant/members/service.py` — `_TME_PHONE_RE` (line ~161) and `normalize_phone`
  (line ~164) already parse `t.me/<phone>` and `t.me/+<phone>` links. Phone detection reuses this.

Related patterns found:
- google-drive-access `http/handler.py`: `_apply_lang(payload)` reads `payload["lang"]`, handles
  `list`, lowercases, accepts `ru`/`en`; responses are `{"answer": translate(key, **ctx)}`.
- The project has **no** existing i18n / `answer` / `telegram_id` — all introduced here.

Dependencies identified:
- `members.service.normalize_phone` / `_TME_PHONE_RE` for phone detection.
- `GroupCreateResult` is persisted (idempotency replay), so any new field needs
  `to_dict`/`from_dict` round-trip coverage.

## Development Approach

- **Testing approach**: TDD — write the failing unit test for each unit before implementing it.
- Complete each task fully before moving to the next; keep changes small and focused.
- **CRITICAL: every task MUST include new/updated tests** (success + error/edge cases) and **all
  tests must pass before starting the next task**.
- Run `pytest` after each change; run `ruff check src tests` before completion.
- Maintain backward compatibility: requests without `lang`/`telegram_id` behave exactly as today
  except the response now also includes an `answer` string (additive field).

## Testing Strategy

- **Unit tests**: required for every task — the i18n helpers, the request/result model changes
  (including `to_dict`/`from_dict` round-trip), and the `_execute_create` phone/`telegram_id`
  branching via the existing `FakeGroupBackend` in `tests/test_groups.py`.
- **HTTP tests**: extend the FastAPI test client coverage (pattern from `tests/test_http_*` and
  the existing group HTTP tests) to assert the body accepts `lang`/`telegram_id` (string and list
  forms) and the response carries the expected `answer`.
- No live Telegram traffic — all tests use fakes. (The `scripts/e2e_*.sh` live suite is not part
  of this HTTP-only change and is recorded as out of scope / skipped here.)

## Progress Tracking

- Mark completed items with `[x]` immediately when done.
- Add newly discovered tasks with ➕ prefix; document blockers with ⚠️ prefix.
- Keep this plan in sync with actual work.

## What Goes Where

- **Implementation Steps** (`[ ]`): code, tests, README update — all automatable in this repo.
- **Post-Completion** (no checkboxes): manual Planfix-integration verification, and the explicitly
  out-of-scope CLI/MCP parity.

## Implementation Steps

### Task 1: Add ru/en answer-message helpers (`lang` + `translate`)
- [x] create a small module `src/telegram_assistant/groups/answers.py` (or
      `src/telegram_assistant/i18n.py` if preferred) with:
  - `normalize_lang(value: str | list[str] | None) -> str` — accept a string or list-of-strings,
    take the first element if a list, lowercase, return `"ru"`/`"en"`, default `"ru"` for missing
    or unrecognized input.
  - a `MESSAGES` ru/en table with keys: `group_created` («Группа создана» / "Group created"),
    `group_created_client_added` («Группа создана, клиент добавлен» / "Group created, client
    added"), `client_phone_no_telegram_id` (exact RU string from Overview + an English
    translation).
  - `answer(lang: str, key: str, **ctx) -> str` returning the localized string (falls back to the
    key's ru text if the lang is missing).
- [x] write tests for `normalize_lang` (string `"RU"`/`"en"`, list `["en"]`, empty list, `None`,
      garbage → default `ru`).
- [x] write tests for `answer()` (each key in ru and en; unknown lang falls back to ru).
- [x] run `pytest` for the new test file — must pass before Task 2.

### Task 2: Add `lang` / `telegram_id` to request models and `answer` to the result
- [x] `GroupCreateRequest` (groups/service.py): add `lang: str | None = None` and
      `telegram_id: int | str | None = None`; include both in `to_payload()`.
- [x] `GroupCreateResult` (groups/service.py): add `answer: str = ""`; include it in `to_dict()`
      and read it in `from_dict()` (default `""` for legacy records).
- [x] `GroupCreateBody` (http_api/groups.py): add
      `lang: str | list[str] | None = None` and
      `telegram_id: int | str | list[str] | None = None`; add a small normalization (validator or
      helper) that collapses a list to its first element and treats blank/empty `telegram_id` as
      `None`. Map both into `GroupCreateRequest` in the `POST /groups` handler.
- [x] write tests: `GroupCreateResult.to_dict()`/`from_dict()` round-trip preserves `answer`;
      `GroupCreateRequest.to_payload()` includes `lang`/`telegram_id`.
- [x] write tests: `GroupCreateBody` parses `lang`/`telegram_id` from both a string and a
      `["..."]` list, and normalizes empty `telegram_id` (e.g. `""`, `[]`) to `None`.
- [x] run `pytest` — must pass before Task 3.

### Task 3: Phone-client + `telegram_id` branching and `answer` construction in `_execute_create`
- [x] add a public helper in `members/service.py`, e.g.
      `looks_like_phone(value: str) -> bool` (reuse `_TME_PHONE_RE` and/or `normalize_phone`,
      returning `True` for `t.me/79...`, `t.me/+79...`, and bare phone forms; `False` otherwise).
      Add unit tests for it.
- [x] in `_execute_create` (groups/service.py), before building `all_members`, compute the
      effective member list and the answer key from `members[0]`:
  - if `members` is non-empty and `looks_like_phone(members[0])`:
    - if `telegram_id` is empty/None → **drop** `members[0]` from the population list, append
      `{"step": "client_invite", "user": members[0], "reason": "phone_without_telegram_id"}` to
      `skipped`, set `answer_key = "client_phone_no_telegram_id"`.
    - else → **replace** `members[0]` with `str(telegram_id)` so the add-member loop adds the
      client by numeric id; set `answer_key = "group_created_client_added"`.
  - else → `answer_key = "group_created"`.
- [x] build the localized answer via the Task 1 helper using `normalize_lang(request.lang)` and set
      it on the returned `GroupCreateResult(answer=...)`.
- [x] write tests (via `FakeGroupBackend`):
  - phone `members[0]` + empty `telegram_id` → group created, client NOT in `members_added`,
    `skipped` has the `phone_without_telegram_id` entry, `answer` == the RU warning; and with
    `lang="en"` the English warning.
  - phone `members[0]` + `telegram_id` set → client added by that numeric id (`members_added`
    contains the id, not the phone link), `answer` == «Группа создана, клиент добавлен».
  - non-phone `members[0]` (e.g. `@user`) → unchanged behavior, `answer` == «Группа создана».
  - default `lang` (absent) → Russian answer.
- [x] run `pytest` — must pass before Task 4.

### Task 4: HTTP endpoint wiring and response coverage
- [x] confirm the `POST /groups` handler passes `lang`/`telegram_id` into `GroupCreateRequest` and
      that `result.to_dict()` (already returned) now surfaces `answer` alongside `operation_id` /
      `operation_status`.
- [x] write HTTP tests (FastAPI test client, following existing group HTTP test setup): a request
      with `lang`/`telegram_id` as strings and as `["..."]` lists succeeds and the JSON response
      includes the expected `answer`; a phone-`members[0]`-without-`telegram_id` request returns
      200 with the warning `answer` and a created group.
- [x] write a replay test: a second create with the same idempotency key returns the **persisted**
      `answer` (proves `to_dict`/`from_dict` round-trip through the operation store).
- [x] run `pytest` — must pass before Task 5.

### Task 5: Verify acceptance criteria
- [x] verify all three Overview behaviors are implemented and covered by tests.
- [x] verify edge cases: empty `members`, `telegram_id` as empty list/blank string, unknown
      `lang`, mixed-case `lang`.
- [x] run the full unit suite: `pytest`.
- [x] run `ruff check src tests` — fix all issues.

### Task 6: [Final] Update documentation
- [ ] update `README.md` HTTP section for `POST /groups`: document the new `lang` and
      `telegram_id` request fields (string-or-list), the new `answer` response field, and the
      phone-without-`telegram_id` behavior.
- [ ] note in README that `lang`/`telegram_id`/`answer` are currently **HTTP-only** (no CLI/MCP).

*Note: ralphex automatically moves completed plans to `docs/plans/completed/`.*

## Technical Details

- **`lang` normalization**: `list → first element → lowercase → {"ru","en"}`, default `"ru"`.
- **`telegram_id` normalization**: `list → first non-empty element`; blank/`""`/empty list → `None`.
  When used, it is passed to `backend.add_member` as a string id (the existing add-member loop and
  entity coercion already accept numeric-id strings — see `member ref coercion`).
- **Phone detection**: `members/service.looks_like_phone` reusing `_TME_PHONE_RE`; matches
  `https://t.me/79222222222`, `https://t.me/+79222222222`, `t.me/...`, and bare phone strings.
- **`answer` persistence**: stored in `GroupCreateResult.to_dict()` → operation `result_payload`,
  restored by `from_dict()`, so replays return the same `answer`.
- **Skip record shape**: `{"step": "client_invite", "user": <phone-link>, "reason":
  "phone_without_telegram_id"}` (consistent with existing `skipped` entries).
- **Backward compatibility**: absent `lang`/`telegram_id` → identical behavior plus an additive
  `answer` field. Legacy persisted results (no `answer`) read back as `answer=""`.

## Post-Completion

*Items requiring manual intervention or external systems — informational only.*

**Manual verification:**
- Exercise the real Planfix→HTTP integration: create a client group with `lang=ru`/`lang=en`, with
  and without `telegram_id`, and confirm the `answer` text shown in Planfix is correct and that a
  phone-only client is reported as not connectable.

**Out of scope (explicitly deferred per user decision):**
- CLI and MCP parity for `lang`/`telegram_id`/`answer` (project convention normally mirrors HTTP
  across surfaces; intentionally skipped for this HTTP-only change). Revisit if the fields should
  be reachable from `telegram-assistant groups create` or the MCP `telegram_groups_*` tools.
- Using `telegram_id` to add a client when `members[0]` is **not** phone-style (current logic only
  substitutes/guards on a phone-style `members[0]`).
