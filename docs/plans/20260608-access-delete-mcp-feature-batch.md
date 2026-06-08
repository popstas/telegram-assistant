# Access, Delete & MCP Feature Batch

## Overview

Implements the 14 open `docs/TODO.md` tasks as one coordinated multi-phase change across
the shared domain layer (CLI + HTTP + MCP). Themes:

- **Config hot-reload** — apply `data/config.yml` changes (notably `telegram.access`) without
  restarting the server.
- **Access model** — backward-compatible multi-chat / multi-permission rules and a new `delete`
  permission, plus access-inspection CLI commands.
- **Delete message** — a new delete operation across all surfaces, gated on `delete`, with an
  optional "only messages this server process sent" restriction backed by an in-memory registry.
- **Message ergonomics** — `reply_to` on send; `messages recent` filtering by minutes.
- **MCP surface** — slimmer `telegram_messages_send` args, base64 file attachments, reliable
  `file_urls` upload (download-to-temp), and server-side tool disabling by name/prefix.
- **Polish** — skill change (skip health check when healthy, prompt for message) and quieter
  `/health` logging (drop Telethon logs).

Benefits: operators can change access policy live; the assistant can reply/delete/scope its own
messages; MCP clients get a smaller, more reliable, configurable tool surface.

## Context (from discovery)

- **Domain areas** follow `service.py` (pure logic + `Backend` protocol) + `telethon_backend.py`
  (adapter): `groups/`, `topics/`, `members/`, `messages/`, `folders/`, `notifications/`.
  `messages/` is split: `attachments.py`, `service.py`, `reactions.py`, `forwarding.py`,
  `telethon_backend.py`.
- **Access**: `src/telegram_assistant/access/service.py` — `Authorizer` builds a rule index from
  `AccessConfig`. Currently a **linear** `AccessLevel` IntEnum (`READ=1 < WRITE=2`) with
  "highest level wins" via `max()`. `require(chat_id, level)` / `require_folder()` / `allows()`.
  `_canonical_chat_id()` normalises the `-100` marker.
- **Config**: `src/telegram_assistant/config/{loader.py,models.py}` — `load_config()` +
  Pydantic models. `AccessConfig`/`AccessRule` (one target `chat`/`folder`/`all` + one
  `permission`), `McpConfig`, `telegram.access`, `telegram.defaults`.
- **HTTP**: `src/telegram_assistant/http_api/app.py` builds the app via `create_app()` and holds
  backend factories + the resolver/authorizer on `app.state`. Routers: `messages.py`,
  `access.py`, `folders.py`, etc. Factories return `None` → router returns **503**.
- **CLI**: `src/telegram_assistant/cli/main.py` (Typer). Exit codes: AccessDenied **3**,
  not-found **2**.
- **MCP**: `src/telegram_assistant/http_api/mcp/{server.py,tools.py,oauth.py}`. Mounted at `/mcp`
  only when `mcp.enabled`.
- **Message backends**: `TelethonMessageBackend` is the default send backend (text/media/
  scheduled). `messages/attachments.py` validates attachments and currently rejects server-local
  `files`, accepting `file_urls`.
- **Tests**: pytest with fakes (`tests/test_*.py`), `tests/conftest.py` `minimal_config_yaml`
  fixture. `tests/test_mcp_mount.py` has `EXPECTED_TOOL_NAMES`. `tests/test_skill_inventory.py`
  guards CLI↔SKILL drift.
- **Live e2e**: `scripts/e2e_test.sh`, `scripts/e2e_cli_test.sh`, `scripts/e2e_http_extras_test.sh`
  (idempotent, require an authorized session + `Clients`/`Client chat test`).
- **Docs to keep in sync**: `skills/telegram-assistant/SKILL.md` (+ resync to
  `~/.claude/skills/telegram-assistant/SKILL.md`), `README.md`.
- `watchdog` is **not** currently a dependency (`pyproject.toml`).

### Confirmed design decisions

- Hot-reload via **`watchdog`** observer on `data/config.yml` with a **2s debounce**.
- Access schema extension is **backward-compatible** (singular forms keep working; add list forms).
- Permissions are **fully independent**: `read`, `write`, and `delete` each grant *only*
  themselves — **no implications** (`write` does **not** imply `read`). A chat that should be
  both readable and writable must be granted `permissions: [read, write]`. Internally the
  authorizer moves from a linear max-`AccessLevel` to an **independent capability-set** model so a
  chat can hold any subset of `{read, write, delete}`.
  - ⚠️ **Backward-compat break (intentional, per review):** the current codebase has `write`
    imply `read` (`WRITE > READ`). Removing that implication means existing configs that grant
    `write` to a chat and rely on implicit read (e.g. `messages recent`) will start being denied
    READ until updated to list `read` explicitly. Task 16 docs must call this out as a migration note.
- Session-limited delete tracks `(chat_id, message_id)` for the **server process lifetime**
  (in-memory, cleared on restart).
- Verification **extends the live `scripts/e2e_*.sh`** scripts in addition to unit/integration
  fakes.

## Development Approach

- **Testing approach**: Regular (code first, then tests) — matches the repo. Tests remain a
  required, non-optional deliverable of **every** task (listed as separate checkboxes).
- Complete each task fully before the next. Small, focused changes. Run tests after each change.
- **Every task includes new/updated unit tests** (success + error/edge cases) and they **must
  pass before starting the next task**.
- Maintain **backward compatibility** *except where intentionally changed*: `telegram.access`
  omitted ⇒ allow-all; existing single-target rules still parse and apply. **Intentional breaks
  (per review):** `write` no longer implies `read` (permissions are independent), and
  `telegram_messages_send` drops `chat_name`/`folder_name`/`folder_id`/`files`. Both are called
  out as migration notes in Task 16.
- Use `.venv` for all Python commands (`source .venv/bin/activate`). Lint with
  `ruff check src tests`.
- **Update this plan file when scope changes during implementation.**

## Testing Strategy

- **Unit/integration tests**: required for every task, using fakes (no real Telegram).
- **Live e2e** (`scripts/e2e_*.sh`): extend with steps for delete (revoke + session-limit),
  `reply_to`, `messages recent --minutes`, base64 send, and `file_urls` download. These are
  idempotent and run against an authorized test session; when no session is available, record
  them as **skipped** (do not delete the steps).
- **Inventory guards**: `tests/test_skill_inventory.py` and `EXPECTED_TOOL_NAMES` in
  `tests/test_mcp_mount.py` must stay green as commands/tools change.

## Progress Tracking

- Mark completed items `[x]` immediately when done.
- Add newly discovered tasks with ➕ prefix; blockers with ⚠️ prefix.
- Keep the plan in sync with actual work.

## What Goes Where

- **Implementation Steps** (`[ ]`): code, unit tests, e2e-script edits, doc updates — all
  achievable in this repo by the agent.
- **Post-Completion** (no checkboxes): running live e2e against a real account, deployment, and
  Docker-restart verification.

## Implementation Steps

### Task 1: Config hot-reload via watchdog
- [x] add `watchdog` to `[project.dependencies]` in `pyproject.toml`
- [x] add a `config/reload.py` (or extend `config/loader.py`) with a `ConfigWatcher` that
      observes the resolved `data/config.yml` path, debounces events for **2s**, and calls a
      reload callback
- [x] on reload: re-run `load_config()`, and on success atomically swap the config object held
      on `app.state` under a lock; on parse/validation error, log and **keep the previous config**
- [x] rebuild config-derived state on swap: the `Authorizer`/`AccessConfig` source and the MCP
      disabled-tools set (Task 12) read the current `app.state` config
- [x] start the watcher in the `create_app()` lifespan (start on startup, stop on shutdown);
      no-op cleanly when the config file path does not exist
- [x] write tests: debounce coalesces rapid edits; valid edit swaps config; invalid edit keeps
      old config and logs; watcher start/stop lifecycle
- [x] run tests + `ruff` — must pass before next task

### Task 2: Authorizer independent-capability model + `delete` permission
- [x] in `access/service.py` add `DELETE` to the `AccessLevel`/capability set and replace the
      linear `dict[int, AccessLevel]` max-merge with an **independent** capability set
      `dict[int, set[AccessLevel]]` — **no implications**: `read`⇒`{read}`, `write`⇒`{write}`,
      `delete`⇒`{delete}`. Drop the `WRITE > READ` ordering semantics entirely.
- [x] update `_PERMISSION_TO_LEVEL`, `_effective_chat_level`, `require`, `require_folder`,
      `allows` to test **capability membership** (`level in caps`) instead of `granted >= level`;
      union across matching rules is a set-union of capabilities
- [x] keep call sites stable: `require(chat_id, AccessLevel.WRITE)` etc. still compile; add the
      `AccessLevel.DELETE` path
- [x] write tests: `write` grants **only** write (read denied, delete denied); `delete` grants
      **only** delete; `read` grants only read; a rule with multiple permissions grants exactly
      that set; union across `all`/folder/chat rules accumulates caps; `-100` normalisation matches
- [x] run tests + `ruff` — must pass before next task

### Task 3: Backward-compatible multi-chat / multi-permission access rules
- [ ] in `config/models.py` extend `AccessRule`: add optional `chats: list[EntityRef]` and
      `permissions: list[Literal["read","write","delete"]]`; keep singular `chat`/`folder`/`all`
      + `permission`; add `delete` to the permission literal
- [ ] add a validator: a rule must name exactly one *target kind* (`chat`/`chats` vs `folder`
      vs `all`); permissions come from `permissions` if set else `[permission]`; reject empty
      permission sets
- [ ] update `Authorizer._ensure_index` to expand a rule over its chats × permissions (resolve
      each chat ref via the resolver) into the capability-set index
- [ ] write tests: a single rule with `chats: [a,b]` + `permissions: [write, delete]` grants both
      caps to both chats; legacy singular rules still parse and apply; validation errors for
      multi-target / empty permissions
- [ ] run tests + `ruff` — must pass before next task

### Task 4: Access management CLI commands (CLI + skill only, NOT MCP)
- [ ] add an `access` command group to `cli/main.py`:
      - `access list` — print the effective rules / capability index from the loaded config
      - `access check --entity <ref> --permission read|write|delete` — resolve the chat and report
        grant verdict + matched rule
      - `access add` — append a rule to `data/config.yml` (e.g.
        `access add --entity <ref>|--folder <name>|--all --permission read,write,delete`),
        re-serialising the access block; the watchdog hot-reload (Task 1) then applies it live.
        Support `--dry-run` (print the resulting rule, don't write).
- [ ] `access check` exits **0** when granted, **3** (AccessDenied convention) when denied, **2**
      when the entity cannot be resolved; `access add` validates the rule (reuse the Task 3
      validator) before writing
- [ ] expose the same access flows in the **skill** (Task 14/16); **do NOT add any access tool to
      MCP** — access management stays CLI + skill only
- [ ] write tests: `list` output for a sample config; `check` granted/denied/not-found exit codes;
      `access add` writes a valid rule (and `--dry-run` does not), round-trips through `load_config`
- [ ] run tests + `ruff` — must pass before next task

### Task 5: SentMessageRegistry (in-memory, process lifetime)
- [ ] add `messages/sent_registry.py` with a thread/async-safe `SentMessageRegistry` storing a
      set of `(chat_id, message_id)` with `record()` and `contains()`; one instance per server
      process, held on `app.state` and injected into MCP context
- [ ] record sent message ids from **all** send paths (text/media/scheduled) after a successful
      send in `messages/service.py` (or where send results return the message id)
- [ ] write tests: record + contains round-trip; canonical `-100` id handling matches the
      authorizer; recording is best-effort and never fails a send
- [ ] run tests + `ruff` — must pass before next task

### Task 6: Delete-message domain operation
- [ ] add a delete op in `messages/service.py` (+ a `DeleteBackend` protocol) and implement it in
      `messages/telethon_backend.py` using Telethon `delete_messages(..., revoke=...)`; default
      **`revoke=True`** (delete for everyone)
- [ ] gate on `AccessLevel.DELETE` via the authorizer on the resolved chat; support `dry_run`
      (resolve + authorize, do not delete) and `force` (required to delete in technical/protected
      chats, consistent with existing `--force` rules)
- [ ] enforce the session-limit option (Task 7 config flag): when enabled, reject ids not in the
      `SentMessageRegistry` with a clear error before calling Telethon
- [ ] write tests: revoke default + `--no-revoke`; access denied without `delete`; dry-run does
      not call backend; session-limit blocks unknown ids and allows recorded ids
- [ ] run tests + `ruff` — must pass before next task

### Task 7: Session-limit config flag + delete surfaces (CLI/HTTP/MCP)
- [ ] add `telegram.access.delete_only_session_messages: bool = True` to `config/models.py`
      (**safe default**: out of the box, delete is restricted to messages this server process
      sent; operators opt out by setting it `false` to allow deleting arbitrary messages)
- [ ] CLI: `messages delete --entity/--chat-id --message-id ... [--revoke/--no-revoke]
      [--dry-run] [--force]` in `cli/main.py`
- [ ] HTTP: delete endpoint in `http_api/messages.py` (factory → **503** when backend unavailable;
      AccessDenied → **403**, not-found → **404**, ambiguous → **409**)
- [ ] MCP: `telegram_messages_delete` tool in `http_api/mcp/tools.py`, reusing the same domain op
      and registry; add to `EXPECTED_TOOL_NAMES`
- [ ] write tests: CLI exit codes; HTTP status codes incl. 503/403/404; MCP tool present and
      delegates correctly; `delete_only_session_messages` honored end-to-end through each surface
- [ ] run tests + `ruff` — must pass before next task

### Task 8: `reply_to` on message send (all surfaces)
- [ ] thread a `reply_to_message_id: int | None` through `messages/service.py` send + the
      Telethon backend (`reply_to=`)
- [ ] expose it: CLI `messages send --reply-to`, HTTP send request field, MCP
      `telegram_messages_send` arg, and the skill send flow
- [ ] write tests: reply_to passed to backend for text + media sends; omitted → normal send
- [ ] run tests + `ruff` — must pass before next task

### Task 9: `messages recent --minutes`
- [ ] add a `minutes: int | None` filter to the recent op in `messages/service.py` (return only
      messages newer than `now - minutes`); compose with existing limit
- [ ] expose via CLI `messages recent --minutes`, HTTP request field, and MCP recent tool
- [ ] write tests: messages outside the window excluded; boundary handling; combined with limit
- [ ] run tests + `ruff` — must pass before next task

### Task 10: Reliable `file_urls` upload (download-to-temp)
- [ ] in `messages/attachments.py` (or a new `messages/downloads.py`) add a helper that downloads
      http(s) URLs to a temp file with **size and time limits**, returns the local path, and is
      cleaned up after send (try/finally)
- [ ] route `file_urls` through this helper before handing local paths to Telethon; surface clear
      errors on oversize / timeout / unreachable
- [ ] write tests: successful download→send→cleanup (fake fetcher); size-limit and timeout
      rejection; cleanup on send failure
- [ ] run tests + `ruff` — must pass before next task

### Task 11: Base64 file attachments for MCP send
- [ ] accept base64 attachment input on the send path: `{filename, mime, content_b64}` with a
      configurable **max size** (default **1 MB**) and allowed-type validation in
      `messages/attachments.py`; decode to a temp file, send, clean up
- [ ] expose via MCP `telegram_messages_send` (and HTTP send request) as a new attachments field
- [ ] write tests: valid base64 → temp file sent + cleaned up; oversize rejected; bad base64 /
      missing filename rejected
- [ ] run tests + `ruff` — must pass before next task

### Task 12: Slim `telegram_messages_send` args + disable tools by prefix
- [ ] reduce `telegram_messages_send` args in `http_api/mcp/tools.py`: remove `chat_name`,
      `folder_name`, `folder_id`, and `files`; keep `entity`/`chat_id`, `text`, `file_urls`,
      `reply_to`, and base64 attachments (chat targeting goes through the entity resolver)
- [ ] add `mcp.disabled_tools: list[str]` to `config/models.py`, matching exact names and
      prefixes (`telegram_groups_*`, `telegram_topics_*`, `telegram_members_*`,
      `telegram_folders_*`, `telegram_notifications_*`); filter tools at mount time in
      `mcp/server.py` and re-apply on hot-reload
- [ ] update `EXPECTED_TOOL_NAMES` and the README MCP tool catalog for the reduced/added tools
- [ ] write tests: send tool no longer exposes removed args; disabled prefixes/names are absent
      from the mounted tool list; empty `disabled_tools` exposes the full set
- [ ] run tests + `ruff` — must pass before next task

### Task 13: Quieter `/health` logging
- [ ] in `observability/logging.py` (or app setup) raise the level of / filter Telethon loggers
      (`telethon`, `telethon.network.*`) so `/health` probes don't emit Telethon noise; keep HTTP
      server logs
- [ ] make it not suppress genuine errors elsewhere (scope the filter to the noisy loggers)
- [ ] write tests: Telethon logger output is suppressed at the configured level; HTTP logs remain
- [ ] run tests + `ruff` — must pass before next task

### Task 14: Skill update — conditional health check + prompt for message
- [ ] update the `change` skill flow (referenced in TODO) so it does **not** run a health check
      when there are no issues, and uses AskUser to obtain the message to send
- [ ] reflect any new send options (`reply_to`, attachments) the skill should pass through
- [ ] write/adjust the skill-inventory expectations so `tests/test_skill_inventory.py` stays green
- [ ] run tests + `ruff` — must pass before next task

### Task 15: Extend live e2e scripts
- [ ] add idempotent steps to `scripts/e2e_*.sh` for: delete (revoke + `delete_only_session_messages`
      on/off), `reply_to` send, `messages recent --minutes`, base64 send, and `file_urls` download-send
- [ ] keep steps re-runnable and self-cleaning; document any new required fixtures in the script
      header comments
- [ ] (no unit test) — verify scripts parse with `bash -n scripts/e2e_*.sh`
- [ ] run `bash -n` on all e2e scripts — must pass before next task

### Task 16: Docs + inventory sync
- [ ] update `skills/telegram-assistant/SKILL.md` (new/changed CLI commands, HTTP endpoints, MCP
      tools) and **resync** to `~/.claude/skills/telegram-assistant/SKILL.md`
- [ ] update `README.md` Commands/usage + MCP tool catalog
- [ ] document new config keys: `telegram.access` multi-chat/multi-permission + `delete`,
      `delete_only_session_messages` (default **true**), hot-reload behavior, `mcp.disabled_tools`,
      base64/file_urls limits (base64 default **1 MB**)
- [ ] add **migration notes**: (1) permissions are now independent — `write` no longer implies
      `read`, so update configs to list `read` explicitly where needed; (2) `telegram_messages_send`
      dropped `chat_name`/`folder_name`/`folder_id`/`files`. Update `CLAUDE.md`'s access section to
      match the new independent-capability semantics.
- [ ] run `tests/test_skill_inventory.py` and `tests/test_mcp_mount.py` — must pass

### Task 17: Verify acceptance criteria
- [ ] verify all 14 TODO items are implemented and reflected in `docs/TODO.md` (check off `[x]`)
- [ ] run full unit suite (`pytest`) — all pass
- [ ] run `ruff check src tests` — clean
- [ ] `bash -n` all e2e scripts; record live e2e as run or skipped (no authorized session)
- [ ] confirm backward compatibility: `telegram.access` omitted ⇒ allow-all; legacy single-target
      rules still apply

## Technical Details

- **Independent-capability authorizer**: index becomes `default_caps: set[AccessLevel]`,
  `chat_caps: dict[int, set[AccessLevel]]`, `folder_caps: dict[str, set[AccessLevel]]`.
  **No implication expansion** — each permission maps only to itself (`read→{read}`,
  `write→{write}`, `delete→{delete}`); a rule's caps union across matching rules. `require(level)`
  passes iff `level in effective_caps(chat)`. The previous `WRITE > READ` ordering is removed.
- **AccessRule (extended)**: exactly one target kind among `{chat|chats}`, `folder`, `all`;
  permissions = `permissions or [permission]`; each ∈ `{read,write,delete}`. To grant read **and**
  write to a chat, list both explicitly (`permissions: [read, write]`).
- **Hot-reload**: `watchdog.observers.Observer` + a debouncing handler (2s); reload swaps
  `app.state.config` under a lock; failures keep the last-good config.
- **SentMessageRegistry**: process-global `set[tuple[int,int]]`, ids canonicalised like the
  authorizer; populated by every successful send; consulted by delete when
  `delete_only_session_messages` is true (**default true**).
- **Delete**: Telethon `client.delete_messages(entity, [ids], revoke=True)` by default.
- **file_urls / base64**: download/decode to `tempfile`, enforce size + time limits, send local
  path, `finally` cleanup. Base64 max default **1 MB** (configurable).
- **MCP tool filtering**: applied at mount; recomputed when config hot-reloads.

## Post-Completion

*Manual / external — no checkboxes:*

**Live verification** (requires an authorized Telethon test session + `Clients`/`Client chat test`):
- Run `scripts/e2e_test.sh`, `scripts/e2e_cli_test.sh`, `scripts/e2e_http_extras_test.sh` and
  confirm the new delete / reply_to / recent-minutes / base64 / file_urls steps pass.

**Docker / deploy verification**:
- In the running Docker instance, edit `data/config.yml` access rules and confirm they apply
  within ~2s **without** a container restart (the original hot-reload TODO motivation).

**Consuming projects**:
- MCP clients relying on the old `telegram_messages_send` args (`chat_name`/`folder_name`/
  `folder_id`/`files`) must migrate to `entity`/`file_urls`/base64 — announce the arg reduction.
