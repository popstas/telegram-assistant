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

- `messages send` — send a message or service command (targeted or folder-wide mass mode).

`folders` — inspect and manage chat folders:

- `folders inspect` — inspect a chat folder and list its chats.
- `folders add-chat` — move an existing chat into a folder.

`operations` — inspect and retry queued operations:

- `operations status` — show the status of an operation, including per-item summary.
- `operations retry` — reset a failed/`needs_review` operation (and its items) back to pending.

Updating this list: descriptions are sourced from each Typer command's docstring in `src/telegram_assistant/cli/main.py`. When you add or rename a command, update this section, `skills/telegram-assistant/SKILL.md`, and re-run `pytest tests/test_skill_inventory.py` — the inventory guard fails if the README/skill catalog drifts from the CLI.

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
    group_title_postfix: ""      # appended to the Telegram chat title at creation
    cleanup_planfix_messages: false  # delete welcome / /task / bot-reply after creation (opt-in)
    task_reply_wait_seconds: 5       # how long to poll for @planfix_bot's /task reply
    default_member_permissions:
      create_topics: true        # let ordinary members create forum topics
      pin_messages: true         # let ordinary members pin messages
```

`topics_layout` controls how the forum opens after `groups create`: `"list"` shows topics as a vertical list (Telegram's default), `"tabs"` shows them as horizontal tabs. The CLI `groups create --topics-layout` and `groups set-layout --layout` flags, and the `POST /telegram/groups` / `POST /telegram/groups/layout` bodies (`topics_layout`), override the default per call.

`group_title_postfix` is appended to the Telegram chat title at creation time (e.g. a shared suffix Planfix scenarios would otherwise add). It is deliberately kept out of the idempotency key, so a Planfix replay still matches on the raw title.

`default_member_permissions` sets the new group's default banned rights so ordinary members can `create_topics` and `pin_messages`. Other default rights are left untouched.

`cleanup_planfix_messages` (default `false`, opt-in) deletes @planfix_bot's welcome message, the `/task <id>` service command, and the bot's reply to it after the group is populated, so the chat starts clean. `task_reply_wait_seconds` is how long the worker polls for the bot's reply before deleting only the welcome + command. All cleanup is best-effort: failures are recorded in the operation's `skipped` list and never fail the create.

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
