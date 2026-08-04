"""CLI tests for `chats inspect`."""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from telegram_assistant.access import AccessDenied, AccessLevel
from telegram_assistant.chats import ChatInfo
from telegram_assistant.cli import main as cli_main
from telegram_assistant.entities import EntityNotFoundError

runner = CliRunner()


class FakeChatBackend:
    def __init__(self, info: ChatInfo | None = None, error: Exception | None = None):
        self.info = info or ChatInfo(chat_id=5, kind="supergroup", title="Team")
        self.error = error
        self.calls: list[dict[str, object]] = []

    async def inspect_chat(self, *, chat_id: int, raw: bool) -> ChatInfo:
        self.calls.append({"chat_id": chat_id, "raw": raw})
        if self.error is not None:
            raise self.error
        return self.info


class FakeResolved:
    def __init__(self, chat_id: int) -> None:
        self.chat_id = chat_id


class FakeResolver:
    def __init__(self, chat_id: int = 5, error: Exception | None = None) -> None:
        self.chat_id = chat_id
        self.error = error

    async def resolve(self, ref: str):
        if self.error is not None:
            raise self.error
        return FakeResolved(self.chat_id)


class FakeManager:
    async def disconnect(self) -> None:
        return None


@pytest.fixture
def wire(monkeypatch, minimal_config_yaml, tmp_path):
    """Patch the backend builder; return a helper that installs fakes."""

    config_path = tmp_path / "config.yml"
    config_path.write_text(minimal_config_yaml, encoding="utf-8")

    def _install(backend, resolver=None, authorizer=None):
        config = cli_main._load_config_or_exit(config_path)

        def _build(_path):
            async def _open():
                return backend, object(), resolver or FakeResolver()

            return config, FakeManager(), _open

        monkeypatch.setattr(cli_main, "_build_chat_inspect_backends", _build)
        if authorizer is not None:
            monkeypatch.setattr(cli_main, "_cli_authorizer", lambda *a, **k: authorizer)
        return config_path

    return _install


def test_requires_exactly_one_reference(wire):
    config_path = wire(FakeChatBackend())

    result = runner.invoke(
        cli_main.app, ["chats", "inspect", "--config", str(config_path)]
    )

    assert result.exit_code == 2
    assert "exactly one of --chat-id, --chat-name, or --entity" in result.output


def test_rejects_two_references(wire):
    config_path = wire(FakeChatBackend())

    result = runner.invoke(
        cli_main.app,
        [
            "chats", "inspect",
            "--chat-id", "5",
            "--entity", "@team",
            "--config", str(config_path),
        ],
    )

    assert result.exit_code == 2


def test_prints_payload_json(wire):
    backend = FakeChatBackend(
        ChatInfo(chat_id=5, kind="supergroup", title="Team", ttl_period=86400)
    )
    config_path = wire(backend)

    result = runner.invoke(
        cli_main.app,
        ["chats", "inspect", "--chat-id", "5", "--config", str(config_path)],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["chat_id"] == 5
    assert payload["kind"] == "supergroup"
    assert payload["ttl_period"] == 86400
    assert "raw" not in payload
    assert backend.calls == [{"chat_id": 5, "raw": False}]


def test_entity_reference_is_resolved(wire):
    backend = FakeChatBackend()
    config_path = wire(backend, resolver=FakeResolver(chat_id=77))

    result = runner.invoke(
        cli_main.app,
        ["chats", "inspect", "--entity", "@team", "--config", str(config_path)],
    )

    assert result.exit_code == 0, result.output
    assert backend.calls == [{"chat_id": 77, "raw": False}]


def test_raw_flag_is_passed_through(wire):
    backend = FakeChatBackend(
        ChatInfo(chat_id=5, kind="supergroup", raw={"entity": {}, "full": {}})
    )
    config_path = wire(backend)

    result = runner.invoke(
        cli_main.app,
        ["chats", "inspect", "--chat-id", "5", "--raw", "--config", str(config_path)],
    )

    assert result.exit_code == 0, result.output
    assert backend.calls == [{"chat_id": 5, "raw": True}]
    assert json.loads(result.output)["raw"] == {"entity": {}, "full": {}}


def test_access_denied_exits_3(wire):
    # AccessDenied is keyword-only (chat_ref, required_level, ...) in
    # telegram_assistant.access.service — not the positional-message form the
    # brief sketched. Constructed here to match the real signature; the CLI's
    # own "access denied: {exc}" wrapping is what the assertion below checks.
    backend = FakeChatBackend(
        error=AccessDenied(chat_ref=5, required_level=AccessLevel.READ)
    )
    config_path = wire(backend)

    result = runner.invoke(
        cli_main.app,
        ["chats", "inspect", "--chat-id", "5", "--config", str(config_path)],
    )

    assert result.exit_code == 3
    assert "access denied" in result.output


def test_unresolvable_entity_exits_2(wire):
    config_path = wire(
        FakeChatBackend(), resolver=FakeResolver(error=EntityNotFoundError("no such chat"))
    )

    result = runner.invoke(
        cli_main.app,
        ["chats", "inspect", "--entity", "@ghost", "--config", str(config_path)],
    )

    assert result.exit_code == 2
    assert "no such chat" in result.output


def test_domain_value_error_exits_2(wire):
    backend = FakeChatBackend(error=ValueError("chat 5 cannot be inspected"))
    config_path = wire(backend)

    result = runner.invoke(
        cli_main.app,
        ["chats", "inspect", "--chat-id", "5", "--config", str(config_path)],
    )

    assert result.exit_code == 2
    assert "cannot be inspected" in result.output


def test_unexpected_error_exits_1(wire):
    backend = FakeChatBackend(error=RuntimeError("boom"))
    config_path = wire(backend)

    result = runner.invoke(
        cli_main.app,
        ["chats", "inspect", "--chat-id", "5", "--config", str(config_path)],
    )

    assert result.exit_code == 1
    assert "chats inspect failed: boom" in result.output
