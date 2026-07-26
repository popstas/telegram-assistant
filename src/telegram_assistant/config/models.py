"""Pydantic models describing the on-disk config (`data/config.yml`)."""

from __future__ import annotations

from typing import Literal
from urllib.parse import urlparse

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

TopicsLayout = Literal["list", "tabs"]
AccessPermission = Literal["read", "write", "delete"]


class DefaultChatFolderConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    folder_name: str = Field(..., min_length=1)
    folder_id: int | None = None


class DefaultMemberPermissions(BaseModel):
    """Default rights granted to ordinary members of a newly created group.

    These map to *allowed* actions: ``True`` means the action is permitted for
    everyone (the corresponding flag is cleared in the chat's default banned
    rights). Other default rights are left untouched.
    """

    model_config = ConfigDict(extra="forbid")

    create_topics: bool = True
    pin_messages: bool = True


class TelegramDefaults(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enable_topics: bool = True
    create_invite_link: bool = True
    topics_layout: TopicsLayout = "list"
    default_member_permissions: DefaultMemberPermissions = Field(
        default_factory=DefaultMemberPermissions
    )


class AccessRule(BaseModel):
    """A single read/write/delete grant for the technical account.

    Exactly one *target kind* must be set per rule:

    * ``chat`` / ``chats`` — one or many entity references (numeric id,
      ``@username``, link, phone, or exact title) resolved via the shared entity
      resolver. ``chat`` (singular) and ``chats`` (list) are the same kind and
      may be combined — their refs union together;
    * ``folder`` — a Telegram chat-folder name (every chat in it inherits the
      grant). Folder titles are **not** unique in Telegram, so a name rule
      unions *every* folder carrying that title;
    * ``folder_id`` — a Telegram chat-folder id, selecting exactly one folder
      even when several share its title;
    * ``all: true`` — a wildcard matching every chat.

    Permissions come from ``permissions`` (a list) when set, otherwise from the
    singular ``permission`` (default ``write``). Capabilities are **independent**
    — ``read``/``write``/``delete`` each grant only themselves (``write`` does
    not imply ``read``). Rules combine as a set-union of capabilities, so a
    wildcard ``all`` + ``read`` baseline can coexist with targeted ``write``
    rules; a chat covered by both ends up with ``{read, write}``. A single rule
    can grant several caps to several chats at once via
    ``chats: [...]`` + ``permissions: [read, write]``.
    """

    model_config = ConfigDict(extra="forbid")

    chat: str | int | None = None
    chats: list[str | int] = Field(default_factory=list)
    folder: str | None = None
    folder_id: int | None = None
    all: bool = False
    permission: AccessPermission = "write"
    permissions: list[AccessPermission] = Field(default_factory=list)
    delete_only_session_messages: bool | None = Field(
        default=None,
        description=(
            "Per-rule override of the policy-level "
            "``AccessConfig.delete_only_session_messages`` for the chats/folders "
            "this rule targets. ``None`` (default) inherits the policy value; "
            "``true``/``false`` overrides it. When several levels match a chat "
            "the most specific override wins (chat rule > folder rule > all rule "
            "> policy default); within one level a restrictive ``true`` wins "
            "over ``false`` on conflict."
        ),
    )
    edit_only_session_messages: bool | None = Field(
        default=None,
        description=(
            "Per-rule override of the policy-level "
            "``AccessConfig.edit_only_session_messages`` for the chats/folders "
            "this rule targets. ``None`` (default) inherits the policy value; "
            "``true``/``false`` overrides it. Resolution mirrors "
            "``delete_only_session_messages`` exactly: chat rule > folder rule > "
            "all rule > policy default, and within one level a restrictive "
            "``true`` wins over ``false`` on conflict."
        ),
    )

    @property
    def chat_refs(self) -> list[str | int]:
        """All chat refs named by this rule (singular ``chat`` plus ``chats``)."""
        refs: list[str | int] = []
        if self.chat is not None:
            refs.append(self.chat)
        refs.extend(self.chats)
        return refs

    @property
    def effective_permissions(self) -> list[AccessPermission]:
        """The permissions this rule grants.

        ``permissions`` when explicitly set, otherwise ``[permission]``.
        """
        return list(self.permissions) if self.permissions else [self.permission]

    @model_validator(mode="after")
    def _exactly_one_target(self) -> AccessRule:
        targets = [
            bool(self.chat_refs),
            self.folder is not None,
            self.folder_id is not None,
            bool(self.all),
        ]
        set_count = sum(targets)
        if set_count != 1:
            raise ValueError(
                "each access rule must set exactly one target kind of "
                "'chat'/'chats' / 'folder' / 'folder_id' / 'all: true'"
            )
        if not self.effective_permissions:
            raise ValueError("access rule must grant at least one permission")
        return self


class AccessConfig(BaseModel):
    """The read/write access policy.

    Absent (``telegram.access is None``) means allow-all (backward compatible).
    Present means deny-by-default: only chats granted by a matching rule may be
    touched, at the granted level.
    """

    model_config = ConfigDict(extra="forbid")

    rules: list[AccessRule] = Field(default_factory=list)
    delete_only_session_messages: bool = Field(
        default=True,
        description=(
            "When true (the safe default), message delete is restricted to "
            "messages this server process sent (tracked in-memory for the "
            "process lifetime). Set false to allow deleting arbitrary messages "
            "the delete permission covers."
        ),
    )
    edit_only_session_messages: bool = Field(
        default=True,
        description=(
            "When true (the safe default), message edit is restricted to "
            "messages this server process sent (tracked in-memory for the "
            "process lifetime). Set false to allow editing arbitrary messages "
            "the write permission covers."
        ),
    )
    folder_cache_ttl: int = Field(
        default=300,
        ge=0,
        description=(
            "TTL in seconds for the persistent folder-membership cache used by "
            "folder-scoped access rules. The membership map (chat -> folders) is "
            "read from SQLite when fresh (age < ttl) and refetched otherwise, "
            "avoiding a full folder scan on every gated operation. Set 0 to "
            "disable persistent caching (always fetch)."
        ),
    )


class TelegramConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    api_id: int
    api_hash: str = Field(..., min_length=1)
    session_path: str = Field(..., min_length=1)
    main_account_label: str = Field(default="telegram-assistant-main", min_length=1)
    reserve_admins: list[str] = Field(default_factory=list)
    reserve_members: list[str] = Field(default_factory=list)
    default_chat_folder: DefaultChatFolderConfig
    defaults: TelegramDefaults = Field(default_factory=TelegramDefaults)
    access: AccessConfig | None = Field(
        default=None,
        description=(
            "Read/write/delete access policy. None (omitted) means allow-all; "
            "present means deny-by-default with independent capabilities "
            "(write does not imply read)."
        ),
    )
    proxy_url: str | None = Field(
        default=None,
        description=(
            "Optional proxy URL for Telethon, e.g. socks5://user:pass@host:1080 "
            "or http://host:8080. Supported schemes: socks5, socks4, http, https."
        ),
    )
    download_root: str | None = Field(
        default=None,
        description=(
            "Server-side root directory that remote (HTTP/MCP) `messages "
            "download` calls are confined to. None (default) means the system "
            "temp directory. A caller-supplied out_dir is resolved against this "
            "root and rejected if it escapes it, so a READ-only remote identity "
            "cannot pick an arbitrary write location. The CLI (local, trusted) "
            "is not confined."
        ),
    )

    @field_validator("proxy_url")
    @classmethod
    def _proxy_url_well_formed(cls, v: str | None) -> str | None:
        if v is None or v == "":
            return None
        from telegram_assistant.telegram_client.proxy import parse_proxy_url

        try:
            parse_proxy_url(v)
        except ValueError as exc:
            raise ValueError(str(exc)) from exc
        return v


class HttpConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    host: str = "0.0.0.0"
    port: int = 8085
    bearer_token: str = Field(..., min_length=1)

    @field_validator("port")
    @classmethod
    def _port_in_range(cls, v: int) -> int:
        if not (1 <= v <= 65535):
            raise ValueError("http.port must be between 1 and 65535")
        return v


class McpConfig(BaseModel):
    """Optional Streamable-HTTP MCP server and local OAuth AS config.

    ``None`` at the app-config level means the MCP interface is absent.
    ``enabled: false`` means the block is present but still disabled, so OAuth
    settings may be omitted for local templates and staged rollout.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    server_url: str | None = Field(default=None, min_length=1)
    issuer_url: str | None = Field(default=None, min_length=1)
    google_client_id: str | None = Field(default=None, min_length=1)
    google_client_secret: str | None = Field(default=None, min_length=1)
    allowed_emails: list[str] = Field(default_factory=list)
    allowed_domains: list[str] = Field(default_factory=list)
    admin_emails: list[str] = Field(default_factory=list)
    admin_domains: list[str] = Field(default_factory=list)
    allowed_redirect_uris: list[str] = Field(default_factory=list)
    allowed_redirect_hosts: list[str] = Field(
        default_factory=lambda: ["localhost", "127.0.0.1", "::1"]
    )
    required_scopes: list[str] = Field(default_factory=lambda: ["mcp"])
    access_token_ttl_seconds: int = Field(default=3600, ge=1)
    refresh_token_ttl_seconds: int = Field(default=2592000, ge=1)
    signing_secret: str | None = Field(default=None, min_length=32)
    # Tool names or prefixes to omit from the mounted MCP surface. An entry
    # ending in ``*`` matches by prefix (e.g. ``telegram_groups_*``); otherwise
    # it matches the exact tool name. Re-applied on config hot-reload.
    disabled_tools: list[str] = Field(default_factory=list)

    @field_validator(
        "allowed_emails",
        "allowed_domains",
        "admin_emails",
        "admin_domains",
        "allowed_redirect_uris",
        "allowed_redirect_hosts",
        "required_scopes",
        "disabled_tools",
    )
    @classmethod
    def _entries_non_empty(cls, v: list[str]) -> list[str]:
        if any(item == "" for item in v):
            raise ValueError("list entries must be non-empty strings")
        return v

    @field_validator("signing_secret")
    @classmethod
    def _signing_secret_strong(cls, v: str | None) -> str | None:
        if v == "replace-with-a-long-random-secret":
            raise ValueError("signing_secret must be a generated secret, not the docs placeholder")
        return v

    @field_validator("server_url", "issuer_url")
    @classmethod
    def _absolute_http_url(cls, v: str | None) -> str | None:
        if v is None:
            return None
        parsed = urlparse(v)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("must be an absolute http(s) URL")
        return v

    @model_validator(mode="after")
    def _enabled_requires_oauth_settings(self) -> McpConfig:
        self.allowed_redirect_hosts = list(
            dict.fromkeys(
                [
                    *self.allowed_redirect_hosts,
                    "localhost",
                    "127.0.0.1",
                    "::1",
                ]
            )
        )
        if not self.enabled:
            return self

        missing = [
            field_name
            for field_name in (
                "server_url",
                "issuer_url",
                "google_client_id",
                "google_client_secret",
                "signing_secret",
            )
            if getattr(self, field_name) is None
        ]
        if not (self.allowed_emails or self.allowed_domains):
            missing.append("allowed_emails or allowed_domains")
        if "telegram:admin" in self.required_scopes and not (
            self.admin_emails or self.admin_domains
        ):
            missing.append("admin_emails or admin_domains for telegram:admin")

        if missing:
            raise ValueError(
                "mcp.enabled requires: " + ", ".join(missing)
            )
        return self


class QueueConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_parallel_telegram_ops: int = Field(default=1, ge=1)
    default_retry_delay_seconds: int = Field(default=30, ge=0)
    flood_wait_safety_margin_seconds: int = Field(default=5, ge=0)


class LoggingConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    level: str = "INFO"
    # Telethon's own logger level. ``None`` caps it at WARNING regardless of
    # ``level`` so per-health-check MTProto chatter stays out of the logs;
    # set explicitly (e.g. "DEBUG") to re-enable Telethon's output.
    telethon_level: str | None = None

    @field_validator("level")
    @classmethod
    def _level_known(cls, v: str) -> str:
        allowed = {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG", "NOTSET"}
        up = v.upper()
        if up not in allowed:
            raise ValueError(f"logging.level must be one of {sorted(allowed)}")
        return up

    @field_validator("telethon_level")
    @classmethod
    def _telethon_level_known(cls, v: str | None) -> str | None:
        if v is None:
            return None
        allowed = {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG", "NOTSET"}
        up = v.upper()
        if up not in allowed:
            raise ValueError(
                f"logging.telethon_level must be one of {sorted(allowed)}"
            )
        return up


class AlertsConfig(BaseModel):
    """Channel-agnostic alert configuration.

    Alerts are always emitted to the JSON log via the default sink; setting
    ``webhook_url`` adds an HTTPS POST sink on top. The numeric knobs let
    operators tune how aggressive the FLOOD_WAIT, stuck-bulk, and error-rate
    triggers are without touching code.
    """

    model_config = ConfigDict(extra="forbid")

    webhook_url: str | None = None
    flood_wait_repeat_threshold: int = Field(default=3, ge=1)
    stuck_bulk_after_seconds: int = Field(default=600, ge=1)
    error_rate_window: int = Field(default=20, ge=1)
    error_rate_threshold: float = Field(default=0.5, gt=0.0, le=1.0)


class PlanfixPluginConfig(BaseModel):
    """Config for the optional Planfix integration plugin (off by default).

    When ``enabled`` is false the core has zero Planfix behavior: ``external_ref``
    still anchors idempotency generically, but the ``/task <id>`` service
    message, ``@planfix_bot`` welcome/reply cleanup, and ``@planfix_bot``
    protected-account guard are all inactive.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    # The Planfix service-bot account. Used to decide whether to send the
    # `/task` message (must be a group member) and as a protected account.
    bot_username: str = Field(default="@planfix_bot", min_length=1)
    # Appended to a new group's Telegram title. Kept out of the idempotency key
    # so replays still match on the raw title.
    group_title_postfix: str = ""
    # Opt-in: after creation, delete the bot's welcome message, our `/task <id>`
    # command, and the bot's reply to it so the new chat starts clean.
    cleanup_messages: bool = False
    # How long to poll for the bot's reply to the `/task` command before giving
    # up and deleting only the welcome + command messages.
    task_reply_wait_seconds: int = Field(default=5, ge=0)


class PluginsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    planfix: PlanfixPluginConfig = Field(default_factory=PlanfixPluginConfig)


class AppConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    telegram: TelegramConfig
    http: HttpConfig
    queue: QueueConfig = Field(default_factory=QueueConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    alerts: AlertsConfig = Field(default_factory=AlertsConfig)
    plugins: PluginsConfig = Field(default_factory=PluginsConfig)
    mcp: McpConfig | None = None
