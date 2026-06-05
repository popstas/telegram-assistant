"""CLI tests for Task 4 — the ``messages recent`` read op, ``--entity``
resolution, and access-denied / entity-error exit codes.

The CLI builds its backends via the ``_build_message_read_backends`` /
``_build_message_backends`` factories; we monkeypatch those to inject fakes so
no real Telegram traffic happens.
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from telegram_assistant.cli import main as cli_main
from telegram_assistant.entities import (
    AmbiguousEntityError,
    EntityNotFoundError,
    ResolvedEntity,
)
from telegram_assistant.messages import RecentMessage


def _write_config(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "config.yml"
    p.write_text(body)
    return p


class _FakeManager:
    async def get_client(self) -> Any:  # pragma: no cover - not used by fakes
        return object()

    async def disconnect(self) -> None:
        return None


class FakeReadBackend:
    def __init__(self, messages: list[RecentMessage]) -> None:
        self._messages = messages
        self.calls: list[dict[str, int]] = []

    async def get_recent_messages(
        self, *, chat_id: int, limit: int = 5
    ) -> list[RecentMessage]:
        self.calls.append({"chat_id": chat_id, "limit": limit})
        return self._messages[:limit]


class FakeFolderBackend:
    async def list_folders(self) -> list:
        return []


class FakeResolver:
    def __init__(self, *, mapping=None, error: Exception | None = None) -> None:
        self._mapping = mapping or {}
        self._error = error

    async def resolve(self, ref) -> ResolvedEntity:
        if self._error is not None:
            raise self._error
        return ResolvedEntity(
            chat_id=self._mapping[str(ref)], title=str(ref), kind="channel"
        )


def _patch_read_backends(
    monkeypatch: pytest.MonkeyPatch,
    read_backend: FakeReadBackend,
    resolver: FakeResolver,
) -> None:
    def _factory(config_path: Path | None) -> Any:
        from telegram_assistant.config import load_config

        config = load_config(config_path)

        async def _open() -> Any:
            return read_backend, FakeFolderBackend(), resolver

        return config, _FakeManager(), _open

    monkeypatch.setattr(cli_main, "_build_message_read_backends", _factory)


def _messages(n: int) -> list[RecentMessage]:
    return [
        RecentMessage(id=i, sender=None, date=None, reply_to=None, text=f"m{i}")
        for i in range(1, n + 1)
    ]


def _run(args: list[str]) -> Any:
    return CliRunner().invoke(cli_main.app, args)


# ---------------------------------------------------------------------------
# messages recent
# ---------------------------------------------------------------------------


def test_recent_default_limit(
    minimal_config_yaml: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _write_config(tmp_path, minimal_config_yaml)
    backend = FakeReadBackend(_messages(10))
    _patch_read_backends(monkeypatch, backend, FakeResolver())

    result = _run(
        ["messages", "recent", "--chat-id", "-100", "--config", str(cfg)]
    )
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload["count"] == 5
    assert backend.calls == [{"chat_id": -100, "limit": 5}]


def test_recent_limit_override(
    minimal_config_yaml: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _write_config(tmp_path, minimal_config_yaml)
    backend = FakeReadBackend(_messages(10))
    _patch_read_backends(monkeypatch, backend, FakeResolver())

    result = _run(
        ["messages", "recent", "--chat-id", "-100", "--limit", "2", "--config", str(cfg)]
    )
    assert result.exit_code == 0, result.stdout
    assert backend.calls[-1]["limit"] == 2


def test_recent_via_entity(
    minimal_config_yaml: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _write_config(tmp_path, minimal_config_yaml)
    backend = FakeReadBackend(_messages(3))
    resolver = FakeResolver(mapping={"@client": -100999})
    _patch_read_backends(monkeypatch, backend, resolver)

    result = _run(
        ["messages", "recent", "--entity", "@client", "--config", str(cfg)]
    )
    assert result.exit_code == 0, result.stdout
    assert backend.calls[-1]["chat_id"] == -100999


def test_recent_requires_one_ref(
    minimal_config_yaml: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _write_config(tmp_path, minimal_config_yaml)
    _patch_read_backends(monkeypatch, FakeReadBackend([]), FakeResolver())
    result = _run(["messages", "recent", "--config", str(cfg)])
    assert result.exit_code == 2


def test_recent_entity_not_found_exit_2(
    minimal_config_yaml: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _write_config(tmp_path, minimal_config_yaml)
    resolver = FakeResolver(error=EntityNotFoundError("no entity"))
    _patch_read_backends(monkeypatch, FakeReadBackend([]), resolver)
    result = _run(
        ["messages", "recent", "--entity", "@ghost", "--config", str(cfg)]
    )
    assert result.exit_code == 2


def test_recent_entity_ambiguous_exit_2(
    minimal_config_yaml: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _write_config(tmp_path, minimal_config_yaml)
    resolver = FakeResolver(error=AmbiguousEntityError(ref="Team", matches=[1, 2]))
    _patch_read_backends(monkeypatch, FakeReadBackend([]), resolver)
    result = _run(["messages", "recent", "--entity", "Team", "--config", str(cfg)])
    assert result.exit_code == 2


# ---------------------------------------------------------------------------
# access-denied exit code
# ---------------------------------------------------------------------------


def _config_deny_all(minimal_config_yaml: str) -> str:
    # Insert a present-but-empty access policy (deny-by-default) under telegram.
    access = textwrap.indent("access:\n  rules: []\n", "  ")
    return minimal_config_yaml.replace(
        "  default_chat_folder:",
        access + "  default_chat_folder:",
    )


def test_recent_access_denied_exit_3(
    minimal_config_yaml: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _write_config(tmp_path, _config_deny_all(minimal_config_yaml))
    backend = FakeReadBackend(_messages(5))
    _patch_read_backends(monkeypatch, backend, FakeResolver())

    result = _run(
        ["messages", "recent", "--chat-id", "-100", "--config", str(cfg)]
    )
    assert result.exit_code == cli_main.ACCESS_DENIED_EXIT_CODE
    assert backend.calls == []
