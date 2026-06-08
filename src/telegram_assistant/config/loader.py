"""YAML config loader with friendly error messages."""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import ValidationError

from telegram_assistant.config.models import AppConfig

CWD_CONFIG_PATH = Path("data/config.yml")
USER_CONFIG_PATH = Path.home() / ".config" / "telegram-assistant" / "config.yml"

DEFAULT_CONFIG_TEMPLATE = """\
# Telegram Assistant config. Replace REPLACE_ME values before first run.
# See docs/plans/completed/20260518-telegram-assistant-mvp.md for the full spec.

telegram:
  api_id: 0                       # REPLACE_ME — int from https://my.telegram.org
  api_hash: "REPLACE_ME"          # REPLACE_ME — string from https://my.telegram.org
  # proxy_url: "socks5://user:pass@host:1080"  # optional; supports socks5/socks4/http/https
  session_path: "~/.config/telegram-assistant/sessions/telegram-assistant-main/session.session"
  main_account_label: telegram-assistant-main
  reserve_admins:
    - "@reserve_account"
  reserve_members: []
  default_chat_folder:
    folder_name: "Clients"          # must already exist in your Telegram account
  defaults:
    enable_topics: true
    create_invite_link: true
    topics_layout: list             # "list" (default) or "tabs"
  # Read/write access policy. Omitting `access` entirely keeps the default
  # allow-all behavior. Adding it switches to deny-by-default: only chats
  # granted by a matching rule may be touched. Rules combine as a union and the
  # highest level wins; `write` implies `read`. Each rule sets exactly one of
  # `chat` / `folder` / `all`.
  # access:
  #   rules:
  #     - all: true                 # read every chat (wildcard baseline)
  #       permission: read
  #     - folder: "Clients"         # manage members/topics + send in this folder
  #       permission: write
  #     - chat: "@client_chat"      # write to one specific chat by @username
  #       permission: write

http:
  host: "127.0.0.1"
  port: 8085
  bearer_token: "REPLACE_ME"      # REPLACE_ME — any long random string

queue:
  max_parallel_telegram_ops: 1
  default_retry_delay_seconds: 30
  flood_wait_safety_margin_seconds: 5

logging:
  level: INFO
  # telethon_level: WARNING  # uncomment to control Telethon's own log level

# Optional integrations. Off by default; with planfix disabled the core has
# zero Planfix knowledge (external_ref still anchors idempotency generically).
# plugins:
#   planfix:
#     enabled: true                 # turn on Planfix-specific behavior
#     bot_username: "@planfix_bot"  # group member that receives the /task command
#     group_title_postfix: ""       # appended to new group titles (out of idempotency key)
#     cleanup_messages: false       # delete the bot's welcome/reply + the /task command
#     task_reply_wait_seconds: 5    # how long to wait for the bot reply before cleanup
"""


class ConfigError(Exception):
    """Raised when the config file is missing, unreadable, or invalid."""


def _format_validation_error(exc: ValidationError, source: str) -> str:
    lines = [f"Invalid configuration in {source}:"]
    for err in exc.errors():
        loc = ".".join(str(p) for p in err["loc"]) or "<root>"
        lines.append(f"  - {loc}: {err['msg']}")
    return "\n".join(lines)


def load_config_from_text(text: str, *, source: str = "<string>") -> AppConfig:
    """Parse and validate config from a YAML string."""
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"Failed to parse YAML in {source}: {exc}") from exc

    if data is None:
        raise ConfigError(f"Config file {source} is empty")
    if not isinstance(data, dict):
        raise ConfigError(f"Config file {source} must be a YAML mapping at the top level")

    try:
        return AppConfig.model_validate(data)
    except ValidationError as exc:
        raise ConfigError(_format_validation_error(exc, source)) from exc


def _load_from(config_path: Path) -> AppConfig:
    if not config_path.exists():
        raise ConfigError(
            f"Config file not found at {config_path}. "
            "Create it (see docs/plans/ for the expected shape)."
        )
    if not config_path.is_file():
        raise ConfigError(f"Config path {config_path} is not a regular file")

    try:
        text = config_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"Could not read config file {config_path}: {exc}") from exc

    return load_config_from_text(text, source=str(config_path))


def _write_default_template(target: Path) -> None:
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(DEFAULT_CONFIG_TEMPLATE, encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"Could not create template at {target}: {exc}") from exc


def resolve_config_path(path: str | Path | None = None) -> Path | None:
    """Return the config path that :func:`load_config` would read, or ``None``.

    Mirrors :func:`load_config`'s lookup order without loading or writing
    anything, so callers (e.g. the hot-reload watcher) can learn which file is
    the live config. Returns ``None`` when no config file currently exists.
    """
    if path is not None:
        return Path(path)
    if CWD_CONFIG_PATH.exists():
        return CWD_CONFIG_PATH
    if USER_CONFIG_PATH.exists():
        return USER_CONFIG_PATH
    return None


def load_config(path: str | Path | None = None) -> AppConfig:
    """Load and validate the application config from disk.

    Lookup order when no explicit `path` is given:
      1. ./data/config.yml (relative to CWD; preserves Docker /app/data -> /data symlink)
      2. ~/.config/telegram-assistant/config.yml

    If neither exists, a template is written to the user path and ConfigError is raised
    asking the operator to fill in REPLACE_ME values.
    """
    if path is not None:
        return _load_from(Path(path))

    if CWD_CONFIG_PATH.exists():
        return _load_from(CWD_CONFIG_PATH)
    if USER_CONFIG_PATH.exists():
        return _load_from(USER_CONFIG_PATH)

    _write_default_template(USER_CONFIG_PATH)
    raise ConfigError(
        f"No config file found. A template was created at {USER_CONFIG_PATH}.\n"
        f"Edit it (replace REPLACE_ME values) and re-run.\n"
        f"Alternatively, create {CWD_CONFIG_PATH} relative to the working directory."
    )
