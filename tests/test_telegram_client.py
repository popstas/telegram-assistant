"""Tests for the Telethon session wrapper and `auth` CLI command."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest
from typer.testing import CliRunner

from telegram_assistant.cli.main import app as cli_app
from telegram_assistant.config import load_config_from_text
from telegram_assistant.config.loader import ConfigError
from telegram_assistant.telegram_client.proxy import HTTP, SOCKS5
from telegram_assistant.telegram_client.session import (
    TelethonSessionManager,
    _reset_shared_for_tests,
    configure_shared_manager,
    get_shared_manager,
)


@dataclass
class _FakeMe:
    id: int = 42
    username: str = "tech_account"
    phone: str = "+10000000000"
    first_name: str = "Tech"


class FakeTelethonClient:
    """Records every async call we make against it."""

    def __init__(
        self,
        session_path: str,
        api_id: int,
        api_hash: str,
        *,
        already_authorized: bool = False,
        needs_password: bool = False,
        me: _FakeMe | None = None,
    ) -> None:
        self.session_path = session_path
        self.api_id = api_id
        self.api_hash = api_hash
        self.already_authorized = already_authorized
        self.needs_password = needs_password
        self._me = me or _FakeMe()
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
        self.connected = False
        self.disconnected = False
        self.code_requested_for: str | None = None
        self.signed_in_args: list[dict[str, Any]] = []

    async def connect(self) -> None:
        self.connected = True
        self.calls.append(("connect", (), {}))

    async def disconnect(self) -> None:
        self.disconnected = True
        self.calls.append(("disconnect", (), {}))

    async def is_user_authorized(self) -> bool:
        self.calls.append(("is_user_authorized", (), {}))
        return self.already_authorized

    async def get_me(self) -> _FakeMe:
        self.calls.append(("get_me", (), {}))
        return self._me

    async def send_code_request(self, phone: str) -> None:
        self.calls.append(("send_code_request", (phone,), {}))
        self.code_requested_for = phone

    async def sign_in(self, *args: Any, **kwargs: Any) -> _FakeMe:
        self.calls.append(("sign_in", args, kwargs))
        self.signed_in_args.append(kwargs)
        if "code" in kwargs and self.needs_password and "password" not in kwargs:
            raise _SessionPasswordNeededError("2FA required")
        self.already_authorized = True
        return self._me


class _SessionPasswordNeededError(Exception):
    """Local stand-in for telethon.errors.SessionPasswordNeededError."""

    pass


# Patch the matcher in the session module to also recognise our fake error.
@pytest.fixture(autouse=True)
def _patch_password_matcher(monkeypatch: pytest.MonkeyPatch) -> None:
    from telegram_assistant.telegram_client import session as session_mod

    real = session_mod._is_password_needed

    def _matcher(exc: BaseException) -> bool:
        if isinstance(exc, _SessionPasswordNeededError):
            return True
        return real(exc)

    monkeypatch.setattr(session_mod, "_is_password_needed", _matcher)


@pytest.fixture(autouse=True)
def _clean_shared_manager() -> None:
    _reset_shared_for_tests()
    yield
    _reset_shared_for_tests()


def _make_manager(
    minimal_config_yaml: str,
    **client_kwargs: Any,
) -> tuple[TelethonSessionManager, list[FakeTelethonClient]]:
    config = load_config_from_text(minimal_config_yaml)
    created: list[FakeTelethonClient] = []

    def factory(session_path: str, api_id: int, api_hash: str) -> FakeTelethonClient:
        client = FakeTelethonClient(session_path, api_id, api_hash, **client_kwargs)
        created.append(client)
        return client

    return TelethonSessionManager(config.telegram, client_factory=factory), created


async def test_state_detects_unauthorized_session(minimal_config_yaml: str) -> None:
    manager, clients = _make_manager(minimal_config_yaml, already_authorized=False)
    state = await manager.state()

    assert len(clients) == 1
    assert clients[0].connected is True
    assert state.authorized is False
    assert state.me is None
    assert state.account_label == "planfix-assistant-main"
    assert state.session_path == "/data/telegram-assistant.session"


async def test_state_detects_authorized_session(minimal_config_yaml: str) -> None:
    manager, clients = _make_manager(minimal_config_yaml, already_authorized=True)
    state = await manager.state()

    assert state.authorized is True
    assert state.me is not None
    assert state.me.username == "tech_account"
    # connect + is_user_authorized + get_me
    op_names = [name for name, _, _ in clients[0].calls]
    assert op_names == ["connect", "is_user_authorized", "get_me"]


async def test_get_client_reuses_same_instance(minimal_config_yaml: str) -> None:
    manager, clients = _make_manager(minimal_config_yaml)
    a = await manager.get_client()
    b = await manager.get_client()
    assert a is b
    assert len(clients) == 1


async def test_interactive_login_skips_prompts_when_authorized(minimal_config_yaml: str) -> None:
    manager, clients = _make_manager(minimal_config_yaml, already_authorized=True)

    def fail() -> str:
        raise AssertionError("should not be prompted")

    state = await manager.interactive_login(
        phone_prompt=fail,
        code_prompt=fail,
        password_prompt=fail,
    )

    assert state.authorized is True
    assert clients[0].code_requested_for is None
    assert clients[0].signed_in_args == []


async def test_interactive_login_phone_and_code(minimal_config_yaml: str) -> None:
    manager, clients = _make_manager(minimal_config_yaml, already_authorized=False)

    state = await manager.interactive_login(
        phone_prompt=lambda: "+10000000000",
        code_prompt=lambda: "12345",
        password_prompt=lambda: pytest.fail("should not need password"),
    )

    assert state.authorized is True
    assert clients[0].code_requested_for == "+10000000000"
    assert clients[0].signed_in_args == [{"phone": "+10000000000", "code": "12345"}]


async def test_interactive_login_handles_2fa(minimal_config_yaml: str) -> None:
    manager, clients = _make_manager(
        minimal_config_yaml, already_authorized=False, needs_password=True
    )

    state = await manager.interactive_login(
        phone_prompt=lambda: "+10000000000",
        code_prompt=lambda: "12345",
        password_prompt=lambda: "hunter2",
    )

    assert state.authorized is True
    args = clients[0].signed_in_args
    assert args[0] == {"phone": "+10000000000", "code": "12345"}
    assert args[1] == {"password": "hunter2"}


async def test_interactive_login_rejects_empty_phone(minimal_config_yaml: str) -> None:
    manager, _ = _make_manager(minimal_config_yaml, already_authorized=False)
    with pytest.raises(ValueError, match="Phone"):
        await manager.interactive_login(
            phone_prompt=lambda: "",
            code_prompt=lambda: pytest.fail("should not be reached"),
        )


async def test_interactive_login_rejects_empty_code(minimal_config_yaml: str) -> None:
    manager, _ = _make_manager(minimal_config_yaml, already_authorized=False)
    with pytest.raises(ValueError, match="code"):
        await manager.interactive_login(
            phone_prompt=lambda: "+10000000000",
            code_prompt=lambda: "",
        )


def test_shared_manager_is_singleton(minimal_config_yaml: str) -> None:
    config = load_config_from_text(minimal_config_yaml)
    manager = configure_shared_manager(config, client_factory=FakeTelethonClient)
    assert get_shared_manager() is manager
    # Re-configuring replaces the manager.
    manager2 = configure_shared_manager(config, client_factory=FakeTelethonClient)
    assert get_shared_manager() is manager2
    assert manager2 is not manager


def test_get_shared_manager_without_configure_raises() -> None:
    _reset_shared_for_tests()
    with pytest.raises(RuntimeError, match="not been configured"):
        get_shared_manager()


async def test_no_proxy_means_no_proxy_kwarg(minimal_config_yaml: str) -> None:
    """When proxy_url is unset, factory must be called without a proxy kwarg."""
    config = load_config_from_text(minimal_config_yaml)
    recorded: list[dict[str, Any]] = []

    def factory(session_path: str, api_id: int, api_hash: str, **kwargs: Any) -> FakeTelethonClient:
        recorded.append(kwargs)
        return FakeTelethonClient(session_path, api_id, api_hash)

    manager = TelethonSessionManager(config.telegram, client_factory=factory)
    await manager.get_client()

    assert recorded == [{}]


async def test_proxy_url_passed_through_to_factory(minimal_config_yaml: str) -> None:
    yaml_with_proxy = minimal_config_yaml.replace(
        'api_hash: "telegram_api_hash"',
        'api_hash: "telegram_api_hash"\n  proxy_url: "socks5://user:pw@h:1080"',
    )
    config = load_config_from_text(yaml_with_proxy)
    recorded: list[dict[str, Any]] = []

    def factory(session_path: str, api_id: int, api_hash: str, **kwargs: Any) -> FakeTelethonClient:
        recorded.append(kwargs)
        return FakeTelethonClient(session_path, api_id, api_hash)

    manager = TelethonSessionManager(config.telegram, client_factory=factory)
    await manager.get_client()

    assert recorded == [
        {
            "proxy": {
                "proxy_type": SOCKS5,
                "addr": "h",
                "port": 1080,
                "username": "user",
                "password": "pw",
            }
        }
    ]


async def test_http_proxy_url_passed_through_to_factory(minimal_config_yaml: str) -> None:
    yaml_with_proxy = minimal_config_yaml.replace(
        'api_hash: "telegram_api_hash"',
        'api_hash: "telegram_api_hash"\n  proxy_url: "http://u:p@host:3128"',
    )
    config = load_config_from_text(yaml_with_proxy)
    recorded: list[dict[str, Any]] = []

    def factory(session_path: str, api_id: int, api_hash: str, **kwargs: Any) -> FakeTelethonClient:
        recorded.append(kwargs)
        return FakeTelethonClient(session_path, api_id, api_hash)

    manager = TelethonSessionManager(config.telegram, client_factory=factory)
    await manager.get_client()

    assert recorded == [
        {
            "proxy": {
                "proxy_type": HTTP,
                "addr": "host",
                "port": 3128,
                "username": "u",
                "password": "p",
            }
        }
    ]


def test_invalid_proxy_url_fails_at_config_load(minimal_config_yaml: str) -> None:
    yaml_bad = minimal_config_yaml.replace(
        'api_hash: "telegram_api_hash"',
        'api_hash: "telegram_api_hash"\n  proxy_url: "ftp://host"',
    )
    with pytest.raises(ConfigError, match="Unsupported proxy scheme"):
        load_config_from_text(yaml_bad)


def test_auth_cli_reports_already_authorized(
    tmp_path, minimal_config_yaml: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "config.yml"
    config_path.write_text(minimal_config_yaml, encoding="utf-8")

    created: list[FakeTelethonClient] = []

    def factory(session_path: str, api_id: int, api_hash: str) -> FakeTelethonClient:
        client = FakeTelethonClient(session_path, api_id, api_hash, already_authorized=True)
        created.append(client)
        return client

    from telegram_assistant.cli import main as cli_module

    real_build = cli_module._build_session_manager

    def patched(path):
        manager = real_build(path)
        manager._factory = factory  # inject fake client factory
        return manager

    monkeypatch.setattr(cli_module, "_build_session_manager", patched)

    runner = CliRunner()
    result = runner.invoke(cli_app, ["auth", "--config", str(config_path)])
    assert result.exit_code == 0, result.output
    assert "already authorized" in result.output
    assert "tech_account" in result.output
    assert created and created[0].already_authorized is True
