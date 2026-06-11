# telegram-assistant

Telegram automation service for the Planfix ↔ Telegram integration.

Runtime surfaces share one domain layer:

- HTTP API (FastAPI) on port `8085` with bearer-token auth — primary entry point for Planfix and automations.
- MCP server (Streamable HTTP) mounted at `/mcp` when explicitly enabled — for MCP clients such as Claude or MCP Inspector, authenticated through the local OAuth Authorization Server.
- CLI (`telegram-assistant`) — mirrors every HTTP endpoint plus admin commands (`auth`, `operations status`, `operations retry`).
- Worker/queue — performs Telegram operations with throttling and `FLOOD_WAIT` handling.

Runs on MTProto via Telethon under a technical Telegram user account.

## Quick start

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

# Place a config file at data/config.yml (see Configuration below)
telegram-assistant auth      # interactive Telethon login
telegram-assistant health    # show current health
uvicorn telegram_assistant.http_api.app:create_app --factory --port 8085
```

## Commands

Every CLI subcommand maps 1:1 to an HTTP endpoint (except the admin-only commands `auth`, `operations status`, and `operations retry`). Run any command with `--help` for full flag documentation.

Top-level:

- `auth` — interactive Telethon login for the technical account.
- `health` — report service health (Telegram session, database, default folder).
- `version` — print the installed version.

`groups` — manage Telegram supergroups:

- `groups create` — create a Telegram supergroup for a Planfix client. Accepts `--topics-layout list|tabs` to pick the forum layout for this group (defaults to `telegram.defaults.topics_layout`).
- `groups set-layout` — set the topics layout (`list` vs `tabs`) for an existing forum chat.
- `groups get-layout` — read the current topics layout (`list` or `tabs`) for a forum chat.
- `groups rename` — rename an existing supergroup (change its title; WRITE-gated, idempotent by target title).

`topics` — manage forum topics:

- `topics create` — create a single forum topic in an existing supergroup.
- `topics bulk-create` — bulk-create topics from a CSV or JSON file.
- `topics close` — close an existing forum topic (the topic and its history are kept).
- `topics rename` — rename an existing forum topic (`--topic-id` or `--topic-name`; WRITE-gated, idempotent by target title).

`members` — manage group membership:

- `members bulk-add` — bulk-add members to an existing supergroup, optionally promoting to admin.
- `members bulk-remove` — bulk-remove members from a supergroup (kick or permanently ban).

`messages` — send messages and service commands:

- `messages send` — send a message or service command (targeted or folder-wide mass mode). Attach local files with repeated `--file` and/or remote URLs with repeated `--file-url` (multiple attachments send an album); defer delivery with `--schedule-at` (ISO-8601 datetime) or `--delay` (relative duration like `10m`, `2h`, `1d`); thread a reply with `--reply-to <message_id>`. `--text` may be omitted for media-only sends. Attachments, scheduling, and `--reply-to` apply to targeted sends only, not mass mode.
- `messages recent` — read the most recent messages from a chat (READ-gated; `--limit` defaults to 5, optional `--minutes N` keeps only messages newer than `now - N` minutes).
- `messages react` — set (`--emoji`) or clear (`--clear`) an emoji reaction on a message (`--message-id`, WRITE-gated).
- `messages forward` — forward one or more messages (`--message-id`, repeatable) from a source (`--from-chat-id`/`--from-entity`) to a target (`--to-chat-id`/`--to-entity`, or the usual target aliases `--chat-id`/`--chat-name`/`--entity`); READ-gated on the source, WRITE-gated on the target.
- `messages delete` — delete one or more messages (`--message-id`, repeatable) from a chat (DELETE-gated). `--revoke`/`--no-revoke` toggles delete-for-everyone (default revoke); `--dry-run` resolves + authorizes without deleting; `--force` is carried for surface consistency. Honors `telegram.access.delete_only_session_messages` (default `true`): when active, only messages this server process sent may be deleted.

`notifications` — mute and unmute chat/contact notifications:

- `notifications mute` — mute a chat or contact, indefinitely or for `--duration` hours.
- `notifications unmute` — restore normal notifications for a chat or contact.

Most chat-targeting commands accept `--entity` (a numeric id with/without `-100`, `@username`, `t.me`/invite link, phone, or exact title) as a flexible alternative to `--chat-id`/`--chat-name`.

Member references in `members`/`admins` (group create) and in `members bulk-add`/`bulk-remove` may be a `@username`, a bare username, a `t.me` link, or a numeric **Telegram user id** (e.g. `1234556`). A bare number is treated as a user id, not a phone — phone numbers must include a leading `+` (e.g. `+15551234567`). Resolving a bare user id requires that the technical account's session already knows the user (cached access hash); `@username`/`t.me` links are the robust path otherwise.

`folders` — inspect and manage chat folders:

- `folders inspect` — inspect a chat folder and list its chats.
- `folders add-chat` — move an existing chat into a folder.
- `folders remove-chat` — remove a chat from a folder (idempotent: a no-op if the chat is not in the folder).

Mutating CLI commands support `--dry-run` before the real run. Local `--file` attachments must exist, be regular files, and be non-empty. `--file-url` must be a valid `http`/`https` URL with a host. `--schedule-at` and `--delay` are mutually exclusive and must resolve to a future time. `messages react` requires exactly one of `--emoji` or `--clear`; `notifications mute --duration` must be positive. `folders remove-chat` accepts `--chat-id`, `--chat-name`, or `--entity`, plus optional `--folder-id`.

### Access control

`telegram.access` in `data/config.yml` gates which chats/folders this instance may read, write, or delete in. Omitting it means allow-all (backward compatible); once present it is deny-by-default. Capabilities are **independent** — `read`, `write`, and `delete` each grant *only* themselves, so `write` no longer implies `read`. Matching rules combine as a set-union of capabilities. Denials surface as a non-zero CLI exit (code 3) and `HTTP 403` on the API.

Each rule names exactly one *target kind* — a single chat (`chat`/`--entity`), a list of chats (`chats`), a folder (`folder`), or the wildcard (`all`) — and a capability set via `permissions: [read, write, delete]` (or the legacy singular `permission`, default `write`). Legacy single-target / singular-permission rules still parse and apply. A common shape is a wildcard `all: read` baseline layered with targeted `[write]` or `[write, delete]` rules:

```yaml
telegram:
  access:
    delete_only_session_messages: true   # default; only delete messages this process sent
    rules:
      - all: true
        permissions: [read]
      - folder: "Planfix clients"
        permissions: [read, write]
      - chats: ["@some_chat", -1001234567890]
        permissions: [read, write, delete]
```

Config edits are hot-reloaded: a `watchdog` observer on `data/config.yml` re-runs the loader with a 2s debounce and atomically swaps the live config on success (a parse/validation error keeps the last-good config), so access-rule changes apply within ~2s without restarting the server.

> **Migration note (capabilities are now independent):** earlier versions had `write` imply `read`. That implication is gone — a chat granted only `write` is now **denied** `read` (e.g. `messages recent` will fail). Update existing configs to list `read` explicitly wherever it is needed, e.g. `permissions: [read, write]`.

`access` — inspect and edit the access policy (CLI + skill only; not exposed over MCP):

- `access list` — print the effective policy (allow-all, or the deny-by-default rules and the capabilities each grants).
- `access check --entity <ref> --permission read|write|delete` — resolve a chat and report the grant verdict (exit `0` granted, `3` denied, `2` unresolved).
- `access add` — append one rule (`--entity`/`--folder`/`--all` + `--permission read,write,delete`) to `data/config.yml`; the hot-reload watcher then applies it live. `--dry-run` prints the rule without writing.

`operations` — inspect and retry queued operations:

- `operations status` — show the status of an operation, including per-item summary.
- `operations retry` — reset a failed/`needs_review` operation (and its items) back to pending.

Updating this list: descriptions are sourced from each Typer command's docstring in `src/telegram_assistant/cli/main.py`. When you add or rename a command, update this section, `skills/telegram-assistant/SKILL.md`, and re-run `pytest tests/test_skill_inventory.py` — the inventory guard fails if the README/skill catalog drifts from the CLI.

## HTTP API

All `/telegram/*` endpoints require `Authorization: Bearer <token>` and use the same access policy as the CLI.

- `POST /telegram/messages` sends targeted or mass messages. Targeted bodies accept `telegram_chat_id`, `entity`, or `chat_name` + `folder_name`, plus optional `telegram_topic_id`/`topic_name`, `file_urls`, base64 `base64_files` (`{filename, mime, content_b64}`, default max 1 MB each), `reply_to_message_id`, `schedule_at`, `delay_seconds`, and `operation_id`. HTTP server-local `files` are rejected; use `file_urls` (downloaded to a temp file with size/time limits) or `base64_files` for media over HTTP. Responses include `telegram_message_id`, `telegram_message_ids` for albums, `scheduled`, `schedule_at`, `operation_id`, and `operation_status`.
- `POST /telegram/messages/reactions` sets or clears a reaction with `message_id` plus exactly one of `emoji` or `clear=true`.
- `POST /telegram/messages/forward` forwards `message_ids` from `from_chat_id`/`from_entity` to `to_chat_id`/`to_entity`.
- `POST /telegram/messages/delete` deletes `message_ids` from a target chat (DELETE-gated). Optional `revoke` (default `true`), `dry_run`, and `force`. Honors `telegram.access.delete_only_session_messages`; the backend factory returns `503` when the session is not connected.
- `POST /telegram/notifications/mute` and `/telegram/notifications/unmute` mute or unmute a target chat/contact; mute accepts positive `duration_hours`.
- `DELETE /telegram/folders/{folder_name}/chats` removes `chat_id`, `chat_name`, or `entity` from a folder and returns `already_absent` when no change was needed.

## MCP server (optional)

The MCP interface is disabled by default. If the `mcp:` block is absent, or
present with `enabled: false`, no `/mcp` route or OAuth endpoints are mounted
and the app behaves like the HTTP/CLI-only service.

When enabled, `create_app()` mounts the official FastMCP Streamable-HTTP app at
`/mcp` and exposes `telegram_` tools for the same operations as the HTTP API
and CLI: health, messages, groups, topics, members, folders, notifications, and
operation status/retry. The tools reuse the same backend factories, entity
resolver, `OperationStore`, plugin registry, and `telegram.access` policy; MCP
does not create or repair the Telethon session.

MCP clients discover and authenticate through the local OAuth Authorization
Server in the same FastAPI process:

- `/.well-known/oauth-authorization-server`
- `/.well-known/oauth-protected-resource/mcp`
- `/register`
- `/authorize`
- `/token`

Google OAuth/OIDC is used only as a login gate. After Google verifies the user,
the local server enforces `allowed_emails` / `allowed_domains`, then mints
signed, audience-bound MCP access and refresh tokens. The Google allowlist
decides who may connect; `telegram.access` still decides which chats/folders
the tools may read or write. Operation-store tools also require the optional
OAuth scope `telegram:admin`, which is granted only to identities matching
`admin_emails` / `admin_domains`.

MCP tool catalog:

| Tool | Key arguments |
| --- | --- |
| `telegram_health` | none |
| `telegram_messages_recent` | `chat_id`, `limit`, optional `minutes` (only messages newer than `now - minutes`) |
| `telegram_messages_send` | `telegram_chat_id`/`entity`, `text`, `telegram_topic_id`/`topic_name`, `file_urls`, base64 `base64_files`, `schedule_at`, `delay_seconds`, `reply_to_message_id`, `operation_id`; `chat_name`/`folder_name`/`folder_id` and server-local `files` are not part of the MCP surface (target via `entity`) |
| `telegram_messages_forward` | `from_chat_id`/`from_entity`, `to_chat_id`/`to_entity`, `message_ids`, `operation_id` |
| `telegram_messages_delete` | `telegram_chat_id`/`entity`, `message_ids`, `revoke`, `dry_run`, `force`; gated on DELETE, honors `delete_only_session_messages` |
| `telegram_messages_react` | `telegram_chat_id`/`entity`/`chat_name` + `folder_name`, `message_id`, `emoji` or `clear` |
| `telegram_groups_create` | `title`, `about`, `admins`, `members`, `folder_name`/`folder_id`, `external_ref`, `topics_layout`, reserve/skip flags |
| `telegram_groups_rename` | `new_title`, `telegram_chat_id`/`entity`/`chat_name` + `folder_name`/`folder_id`, optional `reason`; WRITE-gated, idempotent by target title |
| `telegram_topics_layout` | `chat_id`, optional `layout` (`list`/`tabs`) |
| `telegram_topics_create` | `topic_name`, `telegram_chat_id`/`entity`/`chat_name` + `folder_name`, `external_ref`, `message` |
| `telegram_topics_bulk_create` | `telegram_chat_id`/`entity`/`chat_name` + `folder_name`, `items`, `mode`, `continue_on_error`, `operation_id` |
| `telegram_topics_close` | `topic_id` or `topic_name`, `telegram_chat_id`/`entity`/`chat_name` + `folder_name`, optional `delete_messages`, `operation_id` |
| `telegram_topics_rename` | `new_title`, `topic_id` or `topic_name`, `telegram_chat_id`/`entity`/`chat_name` + `folder_name`/`folder_id`, optional `reason`; WRITE-gated, idempotent by target title |
| `telegram_members_add` | `telegram_chat_id`/`entity`/`chat_name` + `folder_name`, `items`, `mode`, `continue_on_error`, `operation_id` |
| `telegram_members_remove` | `telegram_chat_id`/`entity`/`chat_name` + `folder_name`, `items`, `mode`, `continue_on_error`, `operation_id` |
| `telegram_folders_inspect` | `folder_name`, optional `folder_id` |
| `telegram_folders_add_chat` | `folder_name`, `chat_id`/`chat_name`/`entity`, optional `folder_id` |
| `telegram_folders_remove_chat` | `folder_name`, `chat_id`/`chat_name`/`entity`, optional `folder_id` |
| `telegram_notifications_mute` | `telegram_chat_id`/`entity`/`chat_name` + `folder_name`, `duration_hours` |
| `telegram_notifications_unmute` | `telegram_chat_id`/`entity`/`chat_name` + `folder_name` |
| `telegram_operations_status` | `operation_id`; requires `telegram:admin` |
| `telegram_operations_retry` | `operation_id`, `dry_run`; requires `telegram:admin` |

OAuth client behavior: `/register` is public Dynamic Client Registration with
`token_endpoint_auth_method=none`; `/authorize` requires PKCE S256 and a
`resource` matching `server_url`; `/token` supports `authorization_code` and
`refresh_token`. Redirect URIs registered by clients must use a trusted
loopback host (`localhost`, `127.0.0.1`, `::1`) or a host/URI configured in
`allowed_redirect_hosts` / `allowed_redirect_uris`. Registered clients, pending
Google states, and authorization codes are process-local memory, so clients
must re-register after process restart. `required_scopes` are required by the
MCP mount; `telegram:admin` is advertised only when `admin_emails` or
`admin_domains` is configured for operation status/retry clients.

Minimal enabled config:

```yaml
mcp:
  enabled: true
  server_url: "https://assistant.example.com/mcp"
  issuer_url: "https://assistant.example.com"
  google_client_id: "GOOGLE_CLIENT_ID"
  google_client_secret: "GOOGLE_CLIENT_SECRET"
  allowed_emails:
    - "owner@example.com"
  allowed_domains: []
  admin_emails: []
  admin_domains: []
  allowed_redirect_hosts: []
  allowed_redirect_uris: []
  required_scopes:
    - "mcp"
  access_token_ttl_seconds: 3600
  refresh_token_ttl_seconds: 2592000
  signing_secret: "<output-of-openssl-rand-base64-32>"
  disabled_tools: []   # e.g. ["telegram_groups_*", "telegram_health"]
```

> **Migration note (`telegram_messages_send` args):** the send tool dropped
> `chat_name`, `folder_name`, `folder_id`, and server-local `files`. Target the
> chat through `entity` (or `telegram_chat_id`) and attach media via `file_urls`
> or base64 `base64_files`. MCP clients passing the removed args must migrate.

`disabled_tools` omits tools from the mounted MCP surface. An entry ending in
`*` matches by prefix (e.g. `telegram_groups_*` hides every group tool);
otherwise it matches the exact tool name. The filter is applied at mount and
re-applied on config hot-reload, so editing `data/config.yml` adds or restores
tools without a restart.

For Google OAuth, create a Web application client and register
`<issuer_url>/authorize` as an authorized redirect URI. If the service is behind
a reverse proxy, `server_url` and `issuer_url` must be the public URLs seen by
the MCP client. `server_url` is the protected resource and token audience; it
normally includes `/mcp`. Keep the Google secret and `signing_secret` out of
version control. `signing_secret` must be at least 32 characters and must not
be a docs placeholder. Rotating `signing_secret` invalidates existing MCP
tokens.

Manual smoke testing is documented in `docs/mcp-inspector-e2e.md`.

## Configuration

Config is read from `data/config.yml` by default. The `data/` directory is excluded from version control and holds the Telethon session, SQLite database, and secrets.

If `./data/config.yml` is absent, the loader falls back to `~/.config/telegram-assistant/config.yml`. On a clean machine, running any CLI command without `--config` will create a template at that path with `REPLACE_ME` placeholders for `api_id`, `api_hash`, and `bearer_token` — fill them in and re-run.

To reach Telegram through a proxy, set `telegram.proxy_url` to a single URL — supported schemes are `socks5`, `socks4`, `http`, and `https`. Credentials and explicit ports are optional:

```yaml
telegram:
  proxy_url: "socks5://user:pass@host:1080"   # or http://host:8080, socks4://host, ...
```

Leave it unset (or remove the line) to connect directly.

Defaults applied to new supergroups live under `telegram.defaults`:

```yaml
telegram:
  defaults:
    enable_topics: true
    create_invite_link: true
    topics_layout: "list"        # "list" | "tabs" — applied after groups create
    default_member_permissions:
      create_topics: true        # let ordinary members create forum topics
      pin_messages: true         # let ordinary members pin messages
```

`topics_layout` controls how the forum opens after `groups create`: `"list"` shows topics as a vertical list (Telegram's default), `"tabs"` shows them as horizontal tabs. The CLI `groups create --topics-layout` and `groups set-layout --layout` flags, and the `POST /telegram/groups` / `POST /telegram/groups/layout` bodies (`topics_layout`), override the default per call.

`default_member_permissions` sets the new group's default banned rights so ordinary members can `create_topics` and `pin_messages`. Other default rights are left untouched.

### MCP config (optional)

`mcp` is optional and disabled by default:

- `enabled` defaults to `false`.
- When `enabled: false`, all other fields may be omitted.
- When `enabled: true`, `server_url`, `issuer_url`, `google_client_id`,
  `google_client_secret`, `signing_secret`, and at least one of
  `allowed_emails` or `allowed_domains` are required.
- OAuth redirect URIs must use a trusted loopback host (`localhost`,
  `127.0.0.1`, `::1`) or match `allowed_redirect_hosts` /
  `allowed_redirect_uris`.
- `required_scopes` defaults to `["mcp"]`; every MCP access token must contain
  these scopes. `telegram:admin` is advertised and granted only when
  `admin_emails` or `admin_domains` is configured, and is required by operation
  status/retry tools.
- `access_token_ttl_seconds` defaults to `3600`; `refresh_token_ttl_seconds`
  defaults to `2592000`.
- `signing_secret` must be at least 32 characters; generate it with a command
  such as `openssl rand -base64 32`.

### Idempotency anchor

Group/topic creation is idempotent on a generic `external_ref` (CLI `--external-ref`, HTTP `external_ref`). For backward compatibility the CLI `--planfix-task-id` flag and the HTTP `planfix_task_id` field are accepted as aliases that map onto `external_ref`. With no `external_ref`, groups key on the exact title and topics key on `chat_id + topic_name`.

### Planfix plugin (optional, off by default)

Planfix-specific behavior lives behind an opt-in plugin. With it disabled the core has **zero Planfix knowledge** — `external_ref` still anchors idempotency, but there is no `/task <id>` service message, no `@planfix_bot` welcome cleanup, and `@planfix_bot` is not treated as a protected account. Enable it under `plugins`:

```yaml
plugins:
  planfix:
    enabled: true                 # turn on Planfix-specific behavior
    bot_username: "@planfix_bot"  # group member that receives the /task command
    group_title_postfix: ""       # appended to the Telegram chat title at creation
    cleanup_messages: false       # delete welcome / /task / bot-reply after creation (opt-in)
    task_reply_wait_seconds: 5    # how long to poll for the bot's /task reply
```

When enabled and `external_ref` is set on a group whose members include `bot_username`, the plugin sends `/task <external_ref>` after creation. For **topics** the plugin behaves the same way, but the surviving first message is the **topic name**: the core posts the topic name first, then the plugin posts `/task <external_ref>` as a second message — so after cleanup the topic still shows its name. `group_title_postfix` is appended to the Telegram chat title at creation time but deliberately kept out of the idempotency key, so a replay of the same `external_ref` still matches on the raw title. `cleanup_messages` (default `false`) deletes the bot's welcome message, the `/task <id>` command, and the bot's reply to it — in topics this is scoped to the topic and never touches the topic-name message; `task_reply_wait_seconds` is how long to poll for that reply before deleting only the welcome + command. All cleanup is best-effort: failures are recorded in the operation's `skipped` list and never fail the create.

See `docs/plans/20260518-telegram-assistant-mvp.md` for the full configuration schema and feature scope.

## Docker

The service ships as a slim Python 3.12 image. Runtime state (Telethon session, SQLite database, `data/config.yml`, bearer token) lives in `/data`, which must be mounted as a volume — nothing sensitive is baked into the image.

Build and run with `docker compose`:

```bash
mkdir -p data
cp path/to/your/config.yml data/config.yml   # fill in api_id, api_hash, bearer_token, etc.
docker compose up -d
curl http://127.0.0.1:8085/health
```

Run a one-shot CLI invocation against the same volume:

```bash
docker compose run --rm telegram-assistant \
    telegram-assistant health
```

The `auth` CLI is interactive (it prompts for phone, code, and optional 2FA password), so run it with a TTY attached:

```bash
docker compose run --rm -it telegram-assistant \
    telegram-assistant auth
```

The Telethon session is written to `/data` and persists across container restarts.

A self-contained smoke script lives at `scripts/docker-smoke.sh`. It builds the image, starts a throwaway container with a temporary `data/config.yml`, polls `GET /health` until it returns `200`, and tears everything down.

```bash
bash scripts/docker-smoke.sh
```

## Tests

```bash
pytest
```
