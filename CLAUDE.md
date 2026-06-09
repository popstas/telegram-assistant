# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Setup

Python 3.12+ required. Use `.venv` (project convention):

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

All runtime state — `config.yml`, Telethon session, SQLite DB, bearer token — lives in `data/` (gitignored). Nothing sensitive belongs in the repo. The Docker image symlinks `/app/data -> /data`, so the same `data/config.yml` path works in and out of the container.

## Common commands

- Run the API: `uvicorn telegram_assistant.http_api.app:create_app --factory --port 8085`
- Run the CLI: `telegram-assistant <resource> <action> [options]` (e.g. `health`, `auth`, `groups create`, `topics bulk-create`, `members bulk-add`, `messages send`, `messages forward`, `notifications mute`, `folders inspect`, `operations status`)
- Manual MCP smoke: enable `mcp:` in `data/config.yml`, run the API, then use `npx @modelcontextprotocol/inspector` against `http://localhost:8085/mcp` (requires Node.js/npm; see `docs/mcp-inspector-e2e.md`)
- Tests: `pytest` (asyncio mode auto). Single test: `pytest tests/test_groups.py::test_name` or filter with `-k pattern`
- Lint: `ruff check src tests` (line-length 100, py312, ignores E501)
- Generate changelog: `git-cliff -o CHANGELOG.md` (also runs via pre-commit on commit)
- Docker smoke (build + `/health` poll + teardown): `bash scripts/docker-smoke.sh`
- Live e2e against real Telegram: `bash scripts/e2e_test.sh`, `bash scripts/e2e_cli_test.sh`, `bash scripts/e2e_http_extras_test.sh` — require an authorized Telethon session at `data/sessions/expertizemeAssistant/session.session` and a Telegram folder `Clients` containing chat `Client chat test`. These scripts mutate the real test account, including media sends, scheduled sends, reactions, forwarding, notification mute/unmute, and folder remove/add round-trips. Run them only with an authorized test session/account; when that is unavailable, record them as skipped.

The Telethon session is created **only** by `telegram-assistant auth` (interactive — prompts for phone, code, optional 2FA). There is no HTTP endpoint for login. Re-running `auth` for an authorized session prints the bound account and exits without re-prompting.

## Architecture

Runtime surfaces share one domain layer:

- **HTTP API** (`src/telegram_assistant/http_api/`) — FastAPI, bearer-token auth on `/telegram/*`; `/health` is open. Built via `create_app()` factory in `http_api/app.py`.
- **CLI** (`src/telegram_assistant/cli/main.py`) — Typer. Every HTTP endpoint has a CLI analog plus admin commands (`auth`, `operations status`, `operations retry`).
- **MCP server** (`src/telegram_assistant/http_api/mcp/`) — optional Streamable-HTTP FastMCP app mounted at `/mcp` only when `mcp.enabled` is true. It includes a local OAuth Authorization Server (`/.well-known/oauth-authorization-server`, protected-resource metadata, `/register`, `/authorize`, `/token`) that uses Google OIDC as the login gate, then mints local audience-bound tokens. MCP tools reuse the same domain services, backend factories, entity resolver, `OperationStore`, plugin registry, and `telegram.access` gates as HTTP/CLI.
- **Worker/queue** (`src/telegram_assistant/worker/queue.py`) — async, bounded parallelism, handles `FLOOD_WAIT` as a normal pause (sleep + retry, not failure), persists per-item bulk progress so a restart resumes the last incomplete item.

Each domain area (`groups/`, `topics/`, `members/`, `messages/`, `folders/`, `notifications/`) follows the same shape:

- `service.py` — pure domain logic; defines a `Backend` protocol it depends on.
- `telethon_backend.py` — production Telethon adapter implementing that protocol.

`messages/` is split more narrowly: `attachments.py` holds surface-level attachment validation, `service.py` handles send/recent/mass send, `reactions.py` and `forwarding.py` hold their small domain operations, and `messages/telethon_backend.py` contains the send/read/reaction/forward Telethon adapters.

This split is what lets tests inject fakes without spinning up Telethon. The HTTP layer mirrors the pattern via **backend factories** on `app.state.*_backend_factory` (including `message_backend_factory`, `message_read_backend_factory`, `reaction_backend_factory`, `forward_backend_factory`, `notification_backend_factory`, and `resolver_factory`). A factory returns `None` when the Telethon client isn't yet connected; the router then responds **503 Service Unavailable** instead of 500. `TelethonMessageBackend` is the default send backend for text/media/scheduled sends; do not fall back to the topic backend for message sends. When changing how backends are constructed, preserve this contract — `/health` must still respond even with an unauthorized session.

Two shared domain modules sit alongside the per-area ones:

- **`entities/`** — turns any chat reference (numeric id with or without the `-100` marker, `@username`, `t.me`/invite link, phone, exact title) into a numeric `chat_id`. Resolution order: numeric peer probe → `get_entity` → exact-title dialog scan (none → `EntityNotFoundError`, several → `AmbiguousEntityError`). Holds a per-request cache; propagates `FloodWaitError` rather than swallowing it. Surfaced as CLI `--entity` and an HTTP `entity` request field. `EntityRef.numeric_id` strips the `-100` marker to the bare id.
- **`access/`** — a config-driven read/write/delete gate enforced in the domain layer. An optional `Authorizer` is threaded into the mutating/reading domain ops (send, media/scheduled send, reaction, notification mute/unmute, member add/remove, topic create/close, folder add-chat/remove-chat → WRITE on the resolved chat; group create → WRITE on the destination folder; `messages delete` → DELETE on the resolved chat; `messages recent` and forward source → READ; forward target → WRITE). With `telegram.access` absent the authorizer is an allow-all no-op (backward compatible); when present it is deny-by-default. Capabilities are **independent** (`read`, `write`, `delete`): each permission grants **only itself** — `write` does **not** imply `read`, so a chat that should be both readable and writable must list both. Internally the index is a capability **set** per chat/folder (`set[AccessLevel]`); matching rules union their capability sets and `require(level)` passes iff `level` is in the chat's effective set. `require()` normalises the request `chat_id` to the same bare form the rule index uses, so marked and bare ids match the same rule. `AccessDenied` → CLI exit code **3** / HTTP **403**; entity-not-found → exit 2 / HTTP **404**; ambiguous entity → HTTP **409**.

`OperationStore` (SQLite, `src/telegram_assistant/persistence/`) is the source of truth for idempotency and bulk progress. Idempotency keys (full spec in `docs/init-plan.md`):

- groups: `planfix_task_id` if present, else exact `title`
- topics: `planfix_task_id` if present, else `chat_id + topic_name`
- bulk items: `operation_id + per-item key`

Operation states are `pending | completed | failed | needs_review`. `needs_review` is quarantined — the queue does not auto-retry it; an operator must run `operations retry`.

## Config

`data/config.yml` is loaded by `config.loader.load_config()`. Schema (Pydantic) is in `config/models.py`. Notable: `telegram.default_chat_folder.folder_name` is the default for CLI `--folder-name` and for placing newly created groups; chat folders are never auto-created — if the configured folder is missing, the service returns an error.

`telegram.defaults` knobs applied to new groups: `topics_layout` (overridable per call via `groups create --topics-layout` / the `topics_layout` HTTP field) and `default_member_permissions.{create_topics,pin_messages}`, set as the group's default member rights. Planfix-specific title postfix and bot-message cleanup settings live under `plugins.planfix.*`, not `telegram.defaults`. The Planfix plugin posts `/task <external_ref>` after creating a group (when `@planfix_bot` is a member) and after creating a topic; for topics the surviving first message is the **topic name** (core posts it first, the plugin posts `/task` as a second message via the `after_topic_create` hook). `cleanup_messages` deletes the bot welcome/`/task`/reply best-effort — scoped to the topic for topic creation, never deleting the topic-name message.

`telegram.access` (optional, `AccessConfig`) is the read/write/delete policy. **Omitted ⇒ allow-all; present ⇒ deny-by-default** (an empty `rules: []` denies everything). Each `AccessRule` sets exactly one *target kind* — `chat`/`chats` (entity ref or list), `folder` (name), or `all: true` (wildcard) — plus capabilities from `permissions: [read|write|delete]` (a list) or the singular `permission` (default `write`). Capabilities are **independent**: `write` does **not** imply `read`, so to grant both list `permissions: [read, write]` explicitly. A common shape is a wildcard `all: read` baseline layered with targeted `folder`/`chat` `[write]` (or `[write, delete]`) rules. `delete_only_session_messages` (default **true**) restricts `messages delete` to messages this server process sent (tracked in an in-memory `SentMessageRegistry`); set it `false` to allow deleting arbitrary messages. Config changes are hot-reloaded: a `watchdog` observer on `data/config.yml` re-runs `load_config()` with a 2s debounce and atomically swaps `app.state.config` on success (keeping the last-good config on parse/validation error), so access-rule edits apply live without a restart.

`mcp` is optional and disabled by default. When `enabled: true`, the config must include `server_url` (normally the public `/mcp` URL and token audience), `issuer_url` (public OAuth AS base URL), Google OAuth client credentials, `signing_secret` (32+ characters), and at least one allowlist entry in `allowed_emails` or `allowed_domains`. OAuth client redirects must use trusted loopback hosts or configured `allowed_redirect_hosts` / `allowed_redirect_uris`. `required_scopes` defaults to `["mcp"]`; `telegram:admin` is an optional scope required only by MCP operation status/retry tools, and is advertised/granted only when `admin_emails` or `admin_domains` is configured. Access/refresh token TTLs default to 3600/2592000 seconds. `mcp.disabled_tools` (optional `list[str]`) prunes tools from the mounted surface by exact name or `prefix_*` wildcard (e.g. `telegram_groups_*`); the filter is applied at mount and re-applied on hot-reload.

## Updating CLI/HTTP

When you add or change a CLI command, HTTP endpoint, or MCP tool, update `skills/telegram-assistant/SKILL.md` and re-sync it to `~/.claude/skills/telegram-assistant/SKILL.md` in the same change. Update the Commands/usage sections in `README.md` too. When adding/renaming MCP tools, update README's MCP tool catalog and `EXPECTED_TOOL_NAMES` in `tests/test_mcp_mount.py`. The `tests/test_skill_inventory.py` guard will fail when the CLI catalog drifts from the skill.

## Tests

- `tests/conftest.py` exposes a `minimal_config_yaml` fixture used widely.
- Tests under `tests/test_*.py` are unit/integration with fakes — no real Telegram traffic.
- Live e2e lives in `scripts/e2e_*.sh` (bash, hit the running uvicorn against a real account). They are idempotent by design (re-runnable) and listed as required test surface in `docs/plans/20260518-telegram-assistant-mvp.md`.
- `tests/test_docker_image.py` is auto-skipped when Docker isn't available.

## Important constraints

- Bot API cannot do what this project needs (create groups, add users pre-DM, some admin ops). Stay on MTProto/Telethon under the technical user account.
- The spec at `docs/init-plan.md` is in Russian and is authoritative for behavior — including idempotency, dry-run requirements, and the error taxonomy.
- Destructive and mutating CLI commands must support `--dry-run`. Never remove `@planfix_bot` or technical accounts without `--force`.
- Per user global config: use `.venv` for Python virtualenvs.
