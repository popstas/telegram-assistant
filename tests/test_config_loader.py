"""Tests for the YAML config loader."""

from __future__ import annotations

from pathlib import Path

import pytest

from telegram_assistant.config import (
    AppConfig,
    ConfigError,
    load_config,
    load_config_from_text,
)
from telegram_assistant.config import loader as loader_module
from telegram_assistant.config.loader import DEFAULT_CONFIG_TEMPLATE


def test_load_valid_config_from_text(minimal_config_yaml: str) -> None:
    config = load_config_from_text(minimal_config_yaml)
    assert isinstance(config, AppConfig)
    assert config.telegram.api_id == 123456
    assert config.telegram.api_hash == "telegram_api_hash"
    assert config.telegram.default_chat_folder.folder_name == "Planfix clients"
    assert config.telegram.default_chat_folder.folder_id == 2
    assert config.telegram.defaults.enable_topics is True
    assert config.telegram.defaults.create_invite_link is True
    assert config.telegram.reserve_admins == ["@reserve_account"]
    assert config.telegram.reserve_members == ["@planfix_bot"]
    assert config.http.host == "0.0.0.0"
    assert config.http.port == 8085
    assert config.http.bearer_token == "secret_token"
    assert config.queue.max_parallel_telegram_ops == 1
    assert config.queue.default_retry_delay_seconds == 30
    assert config.queue.flood_wait_safety_margin_seconds == 5
    assert config.logging.level == "INFO"


def test_load_valid_config_from_file(tmp_path: Path, minimal_config_yaml: str) -> None:
    config_path = tmp_path / "config.yml"
    config_path.write_text(minimal_config_yaml, encoding="utf-8")

    config = load_config(config_path)
    assert config.http.bearer_token == "secret_token"


def test_defaults_applied_when_optional_sections_omitted() -> None:
    minimal = """
telegram:
  api_id: 1
  api_hash: "h"
  session_path: /tmp/s
  default_chat_folder:
    folder_name: "X"
http:
  bearer_token: "tok"
"""
    config = load_config_from_text(minimal)
    assert config.telegram.defaults.enable_topics is True
    assert config.telegram.defaults.create_invite_link is True
    assert config.telegram.defaults.topics_layout == "list"
    assert config.telegram.reserve_admins == []
    assert config.telegram.reserve_members == []
    assert config.http.host == "0.0.0.0"
    assert config.http.port == 8085
    assert config.queue.max_parallel_telegram_ops == 1
    assert config.logging.level == "INFO"
    assert config.mcp is None
    # A config predating the pin pacer keeps loading; the knob defaults to a
    # conservative interval and access stays allow-all when unconfigured.
    assert config.telegram.pin_min_interval_seconds == 2.0
    assert config.telegram.access is None


def test_mcp_disabled_block_parses_without_oauth_settings() -> None:
    src = """
telegram:
  api_id: 1
  api_hash: "h"
  session_path: /tmp/s
  default_chat_folder:
    folder_name: "X"
http:
  bearer_token: "tok"
mcp:
  enabled: false
"""
    config = load_config_from_text(src)
    assert config.mcp is not None
    assert config.mcp.enabled is False
    assert config.mcp.server_url is None
    assert config.mcp.required_scopes == ["mcp"]


def test_mcp_enabled_block_parses_when_complete() -> None:
    src = """
telegram:
  api_id: 1
  api_hash: "h"
  session_path: /tmp/s
  default_chat_folder:
    folder_name: "X"
http:
  bearer_token: "tok"
mcp:
  enabled: true
  server_url: "https://assistant.example.test"
  issuer_url: "https://assistant.example.test"
  google_client_id: "google-client-id"
  google_client_secret: "google-client-secret"
  allowed_emails:
    - "owner@example.test"
  allowed_domains:
    - "example.test"
  admin_emails:
    - "admin@example.test"
  admin_domains:
    - "admins.example.test"
  required_scopes:
    - "mcp"
    - "telegram:read"
  access_token_ttl_seconds: 600
  refresh_token_ttl_seconds: 1200
  signing_secret: "local-token-signing-secret-with-32-chars"
"""
    config = load_config_from_text(src)
    assert config.mcp is not None
    assert config.mcp.enabled is True
    assert config.mcp.server_url == "https://assistant.example.test"
    assert config.mcp.issuer_url == "https://assistant.example.test"
    assert config.mcp.google_client_id == "google-client-id"
    assert config.mcp.google_client_secret == "google-client-secret"
    assert config.mcp.allowed_emails == ["owner@example.test"]
    assert config.mcp.allowed_domains == ["example.test"]
    assert config.mcp.admin_emails == ["admin@example.test"]
    assert config.mcp.admin_domains == ["admins.example.test"]
    assert config.mcp.required_scopes == ["mcp", "telegram:read"]
    assert config.mcp.access_token_ttl_seconds == 600
    assert config.mcp.refresh_token_ttl_seconds == 1200
    assert config.mcp.signing_secret == "local-token-signing-secret-with-32-chars"


@pytest.mark.parametrize(
    "signing_secret",
    [
        "short-secret",
        "replace-with-a-long-random-secret",
    ],
)
def test_mcp_enabled_block_rejects_weak_signing_secret(
    signing_secret: str,
) -> None:
    src = f"""
telegram:
  api_id: 1
  api_hash: "h"
  session_path: /tmp/s
  default_chat_folder:
    folder_name: "X"
http:
  bearer_token: "tok"
mcp:
  enabled: true
  server_url: "https://assistant.example.test"
  issuer_url: "https://assistant.example.test"
  google_client_id: "google-client-id"
  google_client_secret: "google-client-secret"
  allowed_emails:
    - "owner@example.test"
  signing_secret: "{signing_secret}"
"""
    with pytest.raises(ConfigError) as excinfo:
        load_config_from_text(src)

    assert "signing_secret" in str(excinfo.value)


@pytest.mark.parametrize("field_name", ["server_url", "issuer_url"])
def test_mcp_enabled_block_rejects_invalid_urls(field_name: str) -> None:
    src = """
telegram:
  api_id: 1
  api_hash: "h"
  session_path: /tmp/s
  default_chat_folder:
    folder_name: "X"
http:
  bearer_token: "tok"
mcp:
  enabled: true
  server_url: "https://assistant.example.test/mcp"
  issuer_url: "https://assistant.example.test"
  google_client_id: "google-client-id"
  google_client_secret: "google-client-secret"
  allowed_emails:
    - "owner@example.test"
  signing_secret: "local-token-signing-secret-with-32-chars"
"""
    src = src.replace(f'{field_name}: "https://assistant.example.test', f'{field_name}: "not-a-url')

    with pytest.raises(ConfigError) as excinfo:
        load_config_from_text(src)

    msg = str(excinfo.value)
    assert field_name in msg
    assert "absolute http(s) URL" in msg


def test_mcp_enabled_block_requires_authorization_server_settings() -> None:
    src = """
telegram:
  api_id: 1
  api_hash: "h"
  session_path: /tmp/s
  default_chat_folder:
    folder_name: "X"
http:
  bearer_token: "tok"
mcp:
  enabled: true
"""
    with pytest.raises(ConfigError) as excinfo:
        load_config_from_text(src)

    msg = str(excinfo.value)
    assert "mcp" in msg
    assert "server_url" in msg
    assert "issuer_url" in msg
    assert "google_client_id" in msg
    assert "google_client_secret" in msg
    assert "signing_secret" in msg
    assert "allowed_emails or allowed_domains" in msg


def test_mcp_admin_required_scope_requires_admin_allowlist() -> None:
    src = """
telegram:
  api_id: 1
  api_hash: "h"
  session_path: /tmp/s
  default_chat_folder:
    folder_name: "X"
http:
  bearer_token: "tok"
mcp:
  enabled: true
  server_url: "https://assistant.example.test"
  issuer_url: "https://assistant.example.test"
  google_client_id: "google-client-id"
  google_client_secret: "google-client-secret"
  allowed_emails:
    - "owner@example.test"
  required_scopes:
    - "mcp"
    - "telegram:admin"
  signing_secret: "local-token-signing-secret-with-32-chars"
"""
    with pytest.raises(ConfigError) as excinfo:
        load_config_from_text(src)

    assert "admin_emails or admin_domains for telegram:admin" in str(excinfo.value)


def test_topics_layout_tabs_parses() -> None:
    src = """
telegram:
  api_id: 1
  api_hash: "h"
  session_path: /tmp/s
  default_chat_folder:
    folder_name: "X"
  defaults:
    topics_layout: tabs
http:
  bearer_token: "tok"
"""
    config = load_config_from_text(src)
    assert config.telegram.defaults.topics_layout == "tabs"


def test_topics_layout_invalid_value_rejected() -> None:
    bad = """
telegram:
  api_id: 1
  api_hash: "h"
  session_path: /tmp/s
  default_chat_folder:
    folder_name: "X"
  defaults:
    topics_layout: grid
http:
  bearer_token: "tok"
"""
    with pytest.raises(ConfigError) as excinfo:
        load_config_from_text(bad)
    assert "topics_layout" in str(excinfo.value)


def test_missing_required_telegram_keys_raises_config_error() -> None:
    bad = """
telegram:
  api_id: 1
  default_chat_folder:
    folder_name: "X"
http:
  bearer_token: "tok"
"""
    with pytest.raises(ConfigError) as excinfo:
        load_config_from_text(bad)
    msg = str(excinfo.value)
    assert "telegram.api_hash" in msg
    assert "telegram.session_path" in msg


def test_missing_http_bearer_token_raises_config_error() -> None:
    bad = """
telegram:
  api_id: 1
  api_hash: "h"
  session_path: /tmp/s
  default_chat_folder:
    folder_name: "X"
http: {}
"""
    with pytest.raises(ConfigError) as excinfo:
        load_config_from_text(bad)
    assert "http.bearer_token" in str(excinfo.value)


def test_unknown_top_level_key_rejected() -> None:
    bad = """
telegram:
  api_id: 1
  api_hash: "h"
  session_path: /tmp/s
  default_chat_folder:
    folder_name: "X"
http:
  bearer_token: "tok"
oops: true
"""
    with pytest.raises(ConfigError):
        load_config_from_text(bad)


def test_alerts_defaults_applied() -> None:
    minimal = """
telegram:
  api_id: 1
  api_hash: "h"
  session_path: /tmp/s
  default_chat_folder:
    folder_name: "X"
http:
  bearer_token: "tok"
"""
    config = load_config_from_text(minimal)
    assert config.alerts.webhook_url is None
    assert config.alerts.flood_wait_repeat_threshold == 3
    assert config.alerts.stuck_bulk_after_seconds == 600
    assert config.alerts.error_rate_window == 20
    assert config.alerts.error_rate_threshold == 0.5


def test_alerts_section_parsed_when_present() -> None:
    src = """
telegram:
  api_id: 1
  api_hash: "h"
  session_path: /tmp/s
  default_chat_folder:
    folder_name: "X"
http:
  bearer_token: "tok"
alerts:
  webhook_url: "https://example.test/alert"
  flood_wait_repeat_threshold: 5
  stuck_bulk_after_seconds: 30
  error_rate_window: 10
  error_rate_threshold: 0.25
"""
    config = load_config_from_text(src)
    assert config.alerts.webhook_url == "https://example.test/alert"
    assert config.alerts.flood_wait_repeat_threshold == 5
    assert config.alerts.stuck_bulk_after_seconds == 30
    assert config.alerts.error_rate_window == 10
    assert config.alerts.error_rate_threshold == 0.25


def test_alerts_invalid_threshold_rejected() -> None:
    bad = """
telegram:
  api_id: 1
  api_hash: "h"
  session_path: /tmp/s
  default_chat_folder:
    folder_name: "X"
http:
  bearer_token: "tok"
alerts:
  error_rate_threshold: 1.5
"""
    with pytest.raises(ConfigError):
        load_config_from_text(bad)


def test_invalid_logging_level_rejected() -> None:
    bad = """
telegram:
  api_id: 1
  api_hash: "h"
  session_path: /tmp/s
  default_chat_folder:
    folder_name: "X"
http:
  bearer_token: "tok"
logging:
  level: SHOUTING
"""
    with pytest.raises(ConfigError) as excinfo:
        load_config_from_text(bad)
    assert "logging.level" in str(excinfo.value)


def test_telethon_level_defaults_to_none_and_accepts_valid() -> None:
    valid = """
telegram:
  api_id: 1
  api_hash: "h"
  session_path: /tmp/s
  default_chat_folder:
    folder_name: "X"
http:
  bearer_token: "tok"
logging:
  level: INFO
  telethon_level: debug
"""
    config = load_config_from_text(valid)
    assert config.logging.telethon_level == "DEBUG"


def test_invalid_telethon_level_rejected() -> None:
    bad = """
telegram:
  api_id: 1
  api_hash: "h"
  session_path: /tmp/s
  default_chat_folder:
    folder_name: "X"
http:
  bearer_token: "tok"
logging:
  level: INFO
  telethon_level: SHOUTING
"""
    with pytest.raises(ConfigError) as excinfo:
        load_config_from_text(bad)
    assert "logging.telethon_level" in str(excinfo.value)


def test_invalid_http_port_rejected() -> None:
    bad = """
telegram:
  api_id: 1
  api_hash: "h"
  session_path: /tmp/s
  default_chat_folder:
    folder_name: "X"
http:
  bearer_token: "tok"
  port: 0
"""
    with pytest.raises(ConfigError) as excinfo:
        load_config_from_text(bad)
    assert "http.port" in str(excinfo.value)


def test_missing_file_raises_config_error(tmp_path: Path) -> None:
    missing = tmp_path / "nope.yml"
    with pytest.raises(ConfigError) as excinfo:
        load_config(missing)
    assert "not found" in str(excinfo.value)


def test_empty_file_raises_config_error(tmp_path: Path) -> None:
    empty = tmp_path / "empty.yml"
    empty.write_text("", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(empty)


def test_invalid_yaml_raises_config_error(tmp_path: Path) -> None:
    bad = tmp_path / "bad.yml"
    bad.write_text("telegram: [unbalanced", encoding="utf-8")
    with pytest.raises(ConfigError) as excinfo:
        load_config(bad)
    assert "Failed to parse YAML" in str(excinfo.value)


def test_non_mapping_top_level_rejected(tmp_path: Path) -> None:
    f = tmp_path / "list.yml"
    f.write_text("- a\n- b\n", encoding="utf-8")
    with pytest.raises(ConfigError) as excinfo:
        load_config(f)
    assert "mapping" in str(excinfo.value)


# --- lookup order + bootstrap -------------------------------------------------


@pytest.fixture()
def patched_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    cwd_path = tmp_path / "cwd" / "data" / "config.yml"
    user_path = tmp_path / "home" / ".config" / "telegram-assistant" / "config.yml"
    monkeypatch.setattr(loader_module, "CWD_CONFIG_PATH", cwd_path)
    monkeypatch.setattr(loader_module, "USER_CONFIG_PATH", user_path)
    return {"cwd": cwd_path, "user": user_path}


def test_explicit_missing_path_does_not_bootstrap(
    patched_paths: dict[str, Path], tmp_path: Path
) -> None:
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "nowhere.yml")
    assert not patched_paths["user"].exists()


def test_cwd_config_used_when_present(
    patched_paths: dict[str, Path], minimal_config_yaml: str
) -> None:
    patched_paths["cwd"].parent.mkdir(parents=True, exist_ok=True)
    patched_paths["cwd"].write_text(minimal_config_yaml, encoding="utf-8")
    cfg = load_config()
    assert cfg.http.bearer_token == "secret_token"


def test_user_config_used_when_cwd_missing(
    patched_paths: dict[str, Path], minimal_config_yaml: str
) -> None:
    patched_paths["user"].parent.mkdir(parents=True, exist_ok=True)
    patched_paths["user"].write_text(minimal_config_yaml, encoding="utf-8")
    cfg = load_config()
    assert cfg.http.bearer_token == "secret_token"


def test_cwd_takes_precedence_over_user(
    patched_paths: dict[str, Path], minimal_config_yaml: str
) -> None:
    patched_paths["cwd"].parent.mkdir(parents=True, exist_ok=True)
    patched_paths["cwd"].write_text(minimal_config_yaml, encoding="utf-8")
    patched_paths["user"].parent.mkdir(parents=True, exist_ok=True)
    patched_paths["user"].write_text(
        minimal_config_yaml.replace("secret_token", "user_token"), encoding="utf-8"
    )
    cfg = load_config()
    assert cfg.http.bearer_token == "secret_token"


def test_neither_present_bootstraps_template(patched_paths: dict[str, Path]) -> None:
    with pytest.raises(ConfigError) as excinfo:
        load_config()

    user_path = patched_paths["user"]
    assert user_path.exists()
    assert user_path.read_text(encoding="utf-8") == DEFAULT_CONFIG_TEMPLATE
    msg = str(excinfo.value)
    assert str(user_path) in msg
    assert "REPLACE_ME" in msg


def test_template_validates_after_filling_placeholders(
    patched_paths: dict[str, Path],
) -> None:
    with pytest.raises(ConfigError):
        load_config()

    user_path = patched_paths["user"]
    text = user_path.read_text(encoding="utf-8")
    text = text.replace("api_id: 0", "api_id: 123456")
    text = text.replace('api_hash: "REPLACE_ME"', 'api_hash: "real_api_hash"')
    text = text.replace('bearer_token: "REPLACE_ME"', 'bearer_token: "real_bearer"')
    user_path.write_text(text, encoding="utf-8")

    cfg = load_config()
    assert cfg.telegram.api_id == 123456
    assert cfg.telegram.api_hash == "real_api_hash"
    assert cfg.http.bearer_token == "real_bearer"


def test_bootstrap_failure_wraps_oserror(
    patched_paths: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(self: Path, *args: object, **kwargs: object) -> None:
        raise PermissionError("denied")

    monkeypatch.setattr(Path, "mkdir", boom)
    with pytest.raises(ConfigError, match="Could not create template"):
        load_config()
