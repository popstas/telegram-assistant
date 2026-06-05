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
- Run the CLI: `telegram-assistant <resource> <action> [options]` (e.g. `health`, `auth`, `groups create`, `topics bulk-create`, `members bulk-add`, `messages send`, `folders inspect`, `operations status`)
- Tests: `pytest` (asyncio mode auto). Single test: `pytest tests/test_groups.py::test_name` or filter with `-k pattern`
- Lint: `ruff check src tests` (line-length 100, py312, ignores E501)
- Generate changelog: `git-cliff -o CHANGELOG.md` (also runs via pre-commit on commit)
- Docker smoke (build + `/health` poll + teardown): `bash scripts/docker-smoke.sh`
- Live e2e against real Telegram: `bash scripts/e2e_test.sh`, `bash scripts/e2e_cli_test.sh`, `bash scripts/e2e_http_extras_test.sh` — require an authorized Telethon session at `data/sessions/expertizemeAssistant/session.session` and a Telegram folder `Clients` containing chat `Client chat test`. These scripts mutate the real test account; per the MVP plan they are mandatory, not skipped.

The Telethon session is created **only** by `telegram-assistant auth` (interactive — prompts for phone, code, optional 2FA). There is no HTTP endpoint for login. Re-running `auth` for an authorized session prints the bound account and exits without re-prompting.

## Architecture

Three interfaces share one domain layer:

- **HTTP API** (`src/telegram_assistant/http_api/`) — FastAPI, bearer-token auth on `/telegram/*`; `/health` is open. Built via `create_app()` factory in `http_api/app.py`.
- **CLI** (`src/telegram_assistant/cli/main.py`) — Typer. Every HTTP endpoint has a CLI analog plus admin commands (`auth`, `operations status`, `operations retry`).
- **Worker/queue** (`src/telegram_assistant/worker/queue.py`) — async, bounded parallelism, handles `FLOOD_WAIT` as a normal pause (sleep + retry, not failure), persists per-item bulk progress so a restart resumes the last incomplete item.

Each domain area (`groups/`, `topics/`, `members/`, `messages/`, `folders/`) follows the same shape:

- `service.py` — pure domain logic; defines a `Backend` protocol it depends on.
- `telethon_backend.py` — production Telethon adapter implementing that protocol.

This split is what lets tests inject fakes without spinning up Telethon. The HTTP layer mirrors the pattern via **backend factories** on `app.state.*_backend_factory`. A factory returns `None` when the Telethon client isn't yet connected; the router then responds **503 Service Unavailable** instead of 500. When changing how backends are constructed, preserve this contract — `/health` must still respond even with an unauthorized session.

`OperationStore` (SQLite, `src/telegram_assistant/persistence/`) is the source of truth for idempotency and bulk progress. Idempotency keys (full spec in `docs/init-plan.md`):

- groups: `planfix_task_id` if present, else exact `title`
- topics: `planfix_task_id` if present, else `chat_id + topic_name`
- bulk items: `operation_id + per-item key`

Operation states are `pending | completed | failed | needs_review`. `needs_review` is quarantined — the queue does not auto-retry it; an operator must run `operations retry`.

## Config

`data/config.yml` is loaded by `config.loader.load_config()`. Schema (Pydantic) is in `config/models.py`. Notable: `telegram.default_chat_folder.folder_name` is the default for CLI `--folder-name` and for placing newly created groups; chat folders are never auto-created — if the configured folder is missing, the service returns an error.

`telegram.defaults` knobs applied to new groups: `topics_layout` (overridable per call via `groups create --topics-layout` / the `topics_layout` HTTP field); `group_title_postfix`, appended to the Telegram title but deliberately kept out of the idempotency key so Planfix replays still match on the raw title; `default_member_permissions.{create_topics,pin_messages}`, set as the group's default member rights; and `cleanup_planfix_messages` (opt-in, default off) / `task_reply_wait_seconds`, which control deleting @planfix_bot's welcome, the `/task <id>` command, and the bot's reply after creation (best-effort — failures land in `skipped`).

## Updating CLI/HTTP

When you add or change a CLI command or HTTP endpoint, update `skills/telegram-assistant/SKILL.md` and re-sync it to `~/.claude/skills/telegram-assistant/SKILL.md` in the same change. Update the Commands section in `README.md` too. The `tests/test_skill_inventory.py` guard will fail otherwise.

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
