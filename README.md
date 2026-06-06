# telegram-assistant

Telegram automation service for the Planfix ↔ Telegram integration.

Three interfaces share one domain layer:

- HTTP API (FastAPI) on port `8085` with bearer-token auth — primary entry point for Planfix and automations.
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

`topics` — manage forum topics:

- `topics create` — create a single forum topic in an existing supergroup.
- `topics bulk-create` — bulk-create topics from a CSV or JSON file.
- `topics close` — close an existing forum topic (the topic and its history are kept).

`members` — manage group membership:

- `members bulk-add` — bulk-add members to an existing supergroup, optionally promoting to admin.
- `members bulk-remove` — bulk-remove members from a supergroup (kick or permanently ban).

`messages` — send messages and service commands:

- `messages send` — send a message or service command (targeted or folder-wide mass mode). Attach local files with repeated `--file` and/or remote URLs with repeated `--file-url` (multiple attachments send an album); defer delivery with `--schedule-at` (ISO-8601 datetime) or `--delay` (relative duration like `10m`, `2h`, `1d`). `--text` may be omitted for media-only sends. Attachments and scheduling apply to targeted sends only, not mass mode.
- `messages recent` — read the most recent messages from a chat (READ-gated; `--limit` defaults to 5).
- `messages react` — set (`--emoji`) or clear (`--clear`) an emoji reaction on a message (`--message-id`, WRITE-gated).
- `messages forward` — forward one or more messages (`--message-id`, repeatable) from a source (`--from-chat-id`/`--from-entity`) to a target (`--to-chat-id`/`--to-entity`, or the usual target aliases `--chat-id`/`--chat-name`/`--entity`); READ-gated on the source, WRITE-gated on the target.

`notifications` — mute and unmute chat/contact notifications:

- `notifications mute` — mute a chat or contact, indefinitely or for `--duration` hours.
- `notifications unmute` — restore normal notifications for a chat or contact.

Most chat-targeting commands accept `--entity` (a numeric id with/without `-100`, `@username`, `t.me`/invite link, phone, or exact title) as a flexible alternative to `--chat-id`/`--chat-name`.

`folders` — inspect and manage chat folders:

- `folders inspect` — inspect a chat folder and list its chats.
- `folders add-chat` — move an existing chat into a folder.
- `folders remove-chat` — remove a chat from a folder (idempotent: a no-op if the chat is not in the folder).

Mutating CLI commands support `--dry-run` before the real run. Local `--file` attachments must exist, be regular files, and be non-empty. `--file-url` must be a valid `http`/`https` URL with a host. `--schedule-at` and `--delay` are mutually exclusive and must resolve to a future time. `messages react` requires exactly one of `--emoji` or `--clear`; `notifications mute --duration` must be positive. `folders remove-chat` accepts `--chat-id`, `--chat-name`, or `--entity`, plus optional `--folder-id`.

### Access control

`telegram.access` in `data/config.yml` gates which chats/folders this instance may read or write. Omitting it means allow-all (backward compatible); once present it is deny-by-default, `write` implies `read`, and rules combine as a union with the highest level winning. Denials surface as a non-zero CLI exit (code 3) and `HTTP 403` on the API.

`operations` — inspect and retry queued operations:

- `operations status` — show the status of an operation, including per-item summary.
- `operations retry` — reset a failed/`needs_review` operation (and its items) back to pending.

Updating this list: descriptions are sourced from each Typer command's docstring in `src/telegram_assistant/cli/main.py`. When you add or rename a command, update this section, `skills/telegram-assistant/SKILL.md`, and re-run `pytest tests/test_skill_inventory.py` — the inventory guard fails if the README/skill catalog drifts from the CLI.

## HTTP API

All `/telegram/*` endpoints require `Authorization: Bearer <token>` and use the same access policy as the CLI.

- `POST /telegram/messages` sends targeted or mass messages. Targeted bodies accept `telegram_chat_id`, `entity`, or `chat_name` + `folder_name`, plus optional `telegram_topic_id`/`topic_name`, `file_urls`, `schedule_at`, `delay_seconds`, and `operation_id`. HTTP server-local `files` are rejected; use `file_urls` for media over HTTP. Responses include `telegram_message_id`, `telegram_message_ids` for albums, `scheduled`, `schedule_at`, `operation_id`, and `operation_status`.
- `POST /telegram/messages/reactions` sets or clears a reaction with `message_id` plus exactly one of `emoji` or `clear=true`.
- `POST /telegram/messages/forward` forwards `message_ids` from `from_chat_id`/`from_entity` to `to_chat_id`/`to_entity`.
- `POST /telegram/notifications/mute` and `/telegram/notifications/unmute` mute or unmute a target chat/contact; mute accepts positive `duration_hours`.
- `DELETE /telegram/folders/{folder_name}/chats` removes `chat_id`, `chat_name`, or `entity` from a folder and returns `already_absent` when no change was needed.

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

When enabled and `external_ref` is set on a group whose members include `bot_username`, the plugin sends `/task <external_ref>` after creation. `group_title_postfix` is appended to the Telegram chat title at creation time but deliberately kept out of the idempotency key, so a replay of the same `external_ref` still matches on the raw title. `cleanup_messages` (default `false`) deletes the bot's welcome message, the `/task <id>` command, and the bot's reply to it; `task_reply_wait_seconds` is how long to poll for that reply before deleting only the welcome + command. All cleanup is best-effort: failures are recorded in the operation's `skipped` list and never fail the create.

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
