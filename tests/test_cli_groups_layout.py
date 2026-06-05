"""CLI tests for ``groups set-layout`` and ``groups get-layout`` (plan Task 4)."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from telegram_assistant.cli import main as cli_main
from telegram_assistant.persistence import OperationStore

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _normalize_cli_output(text: str) -> str:
    """Collapse rich panel formatting so substring checks survive line wrapping.

    Typer renders the "Missing option '--chat-id'" error inside a rich panel and
    may word-wrap the option name across a hyphen, inserting box borders and
    whitespace mid-token. Strip ANSI codes and everything but alphanumerics so
    ``--chat-id`` reliably reduces to ``chatid``.
    """
    return re.sub(r"[^a-z0-9]", "", _ANSI_RE.sub("", text).lower())


def _write_config(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "config.yml"
    p.write_text(body)
    return p


def _config_with_layout(minimal_config_yaml: str, layout: str | None) -> str:
    if layout is None:
        return minimal_config_yaml
    # Insert under the `defaults:` block (4-space indent under telegram:).
    return minimal_config_yaml.replace(
        "  defaults:\n",
        f"  defaults:\n    topics_layout: {layout}\n",
    )


class FakeGroupBackend:
    """Records layout calls; raises if unrelated GroupBackend methods are hit."""

    def __init__(self, *, forum_tabs: bool = False) -> None:
        self.forum_tabs = forum_tabs
        self.set_calls: list[tuple[int, bool]] = []
        self.get_calls: list[int] = []

    async def create_supergroup(
        self, *, title: str, about: str | None, enable_topics: bool
    ) -> int:  # pragma: no cover - must not be called
        raise AssertionError("layout commands must not create supergroups")

    async def add_member(self, *, chat_id: int, user: str) -> None:  # pragma: no cover
        raise AssertionError("layout commands must not add members")

    async def promote_admin(
        self, *, chat_id: int, user: str
    ) -> None:  # pragma: no cover
        raise AssertionError("layout commands must not promote admins")

    async def create_invite_link(self, *, chat_id: int) -> str:  # pragma: no cover
        raise AssertionError("layout commands must not create invite links")

    async def send_message(self, *, chat_id: int, text: str) -> int:  # pragma: no cover
        raise AssertionError("layout commands must not send messages")

    async def set_topics_layout(self, *, chat_id: int, tabs: bool) -> None:
        self.set_calls.append((chat_id, tabs))
        self.forum_tabs = tabs

    async def get_topics_layout(self, *, chat_id: int) -> bool:
        self.get_calls.append(chat_id)
        return self.forum_tabs


class FakeFolderBackend:
    """No-op folder backend; never used by the layout commands."""

    async def list_folders(self) -> list[Any]:  # pragma: no cover
        raise AssertionError("layout commands must not touch folders")

    async def resolve_chat(self, chat_ref: str | int) -> Any:  # pragma: no cover
        raise AssertionError("layout commands must not resolve chats")

    async def add_chat_to_folder(
        self, folder_id: int, chat_id: int
    ) -> None:  # pragma: no cover
        raise AssertionError("layout commands must not modify folders")


def _patch_group_backends(
    monkeypatch: pytest.MonkeyPatch,
    backend: FakeGroupBackend,
    folder_backend: FakeFolderBackend,
    store: OperationStore,
) -> None:
    class _FakeManager:
        async def disconnect(self) -> None:
            return None

    def _factory(config_path: Path | None) -> Any:
        from telegram_assistant.config import load_config

        config = load_config(config_path)

        async def _open() -> Any:
            return backend, folder_backend

        return config, _FakeManager(), store, _open

    monkeypatch.setattr(cli_main, "_build_group_backends", _factory)


def _last_json_line(stdout: str) -> dict[str, Any]:
    return json.loads(stdout.strip().splitlines()[-1])


# ---------------------------------------------------------------------------
# groups set-layout
# ---------------------------------------------------------------------------


def test_set_layout_success_with_explicit_layout(
    minimal_config_yaml: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_file = _write_config(tmp_path, minimal_config_yaml)
    backend = FakeGroupBackend(forum_tabs=False)
    folder_backend = FakeFolderBackend()
    store = OperationStore(tmp_path / "state.db")
    _patch_group_backends(monkeypatch, backend, folder_backend, store)

    runner = CliRunner()
    result = runner.invoke(
        cli_main.app,
        [
            "groups",
            "set-layout",
            "--chat-id",
            "-100123",
            "--layout",
            "tabs",
            "--config",
            str(config_file),
        ],
    )
    assert result.exit_code == 0, result.stdout
    payload = _last_json_line(result.stdout)
    assert payload["layout"] == "tabs"
    assert payload["telegram_chat_id"] == -100123
    assert payload["replayed"] is False
    assert payload["operation_status"] == "completed"
    assert backend.set_calls == [(-100123, True)]


def test_set_layout_uses_config_default_when_flag_omitted(
    minimal_config_yaml: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_file = _write_config(
        tmp_path, _config_with_layout(minimal_config_yaml, "tabs")
    )
    backend = FakeGroupBackend(forum_tabs=False)
    folder_backend = FakeFolderBackend()
    store = OperationStore(tmp_path / "state.db")
    _patch_group_backends(monkeypatch, backend, folder_backend, store)

    runner = CliRunner()
    result = runner.invoke(
        cli_main.app,
        [
            "groups",
            "set-layout",
            "--chat-id",
            "-100",
            "--config",
            str(config_file),
        ],
    )
    assert result.exit_code == 0, result.stdout
    payload = _last_json_line(result.stdout)
    assert payload["layout"] == "tabs"
    assert backend.set_calls == [(-100, True)]


def test_set_layout_dry_run_does_not_call_backend(
    minimal_config_yaml: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_file = _write_config(tmp_path, minimal_config_yaml)
    backend = FakeGroupBackend()
    folder_backend = FakeFolderBackend()
    store = OperationStore(tmp_path / "state.db")
    _patch_group_backends(monkeypatch, backend, folder_backend, store)

    runner = CliRunner()
    result = runner.invoke(
        cli_main.app,
        [
            "groups",
            "set-layout",
            "--chat-id",
            "-100",
            "--layout",
            "tabs",
            "--dry-run",
            "--config",
            str(config_file),
        ],
    )
    assert result.exit_code == 0, result.stdout
    payload = _last_json_line(result.stdout)
    assert payload["status"] == "dry_run"
    assert payload["dry_run"] is True
    assert payload["command"] == "groups.set-layout"
    assert payload["resolved"]["layout"] == "tabs"
    assert payload["resolved"]["telegram_chat_id"] == -100
    assert payload["resolved"]["layout_source"] == "cli"
    assert any("tabs" in action for action in payload["planned_actions"])

    # Dry-run must not touch the backend or persist any operation row.
    assert backend.set_calls == []
    assert backend.get_calls == []
    with store._connect() as conn:  # noqa: SLF001 (test introspection)
        count = int(conn.execute("SELECT COUNT(*) FROM operations").fetchone()[0])
    assert count == 0


def test_set_layout_dry_run_uses_config_default(
    minimal_config_yaml: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_file = _write_config(
        tmp_path, _config_with_layout(minimal_config_yaml, "tabs")
    )
    backend = FakeGroupBackend()
    folder_backend = FakeFolderBackend()
    store = OperationStore(tmp_path / "state.db")
    _patch_group_backends(monkeypatch, backend, folder_backend, store)

    runner = CliRunner()
    result = runner.invoke(
        cli_main.app,
        [
            "groups",
            "set-layout",
            "--chat-id",
            "-100",
            "--dry-run",
            "--config",
            str(config_file),
        ],
    )
    assert result.exit_code == 0, result.stdout
    payload = _last_json_line(result.stdout)
    assert payload["resolved"]["layout"] == "tabs"
    assert payload["resolved"]["layout_source"] == "config"


def test_set_layout_rejects_invalid_layout_value(
    minimal_config_yaml: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_file = _write_config(tmp_path, minimal_config_yaml)
    backend = FakeGroupBackend()
    folder_backend = FakeFolderBackend()
    store = OperationStore(tmp_path / "state.db")
    _patch_group_backends(monkeypatch, backend, folder_backend, store)

    runner = CliRunner()
    result = runner.invoke(
        cli_main.app,
        [
            "groups",
            "set-layout",
            "--chat-id",
            "-100",
            "--layout",
            "grid",
            "--config",
            str(config_file),
        ],
    )
    assert result.exit_code == 2
    assert "grid" in result.stdout or "grid" in (result.stderr or "")
    assert backend.set_calls == []


def test_set_layout_missing_chat_id_errors() -> None:
    runner = CliRunner()
    result = runner.invoke(cli_main.app, ["groups", "set-layout", "--layout", "tabs"])
    assert result.exit_code != 0
    combined = _normalize_cli_output(result.stdout + (result.stderr or ""))
    assert "chatid" in combined


# ---------------------------------------------------------------------------
# groups get-layout
# ---------------------------------------------------------------------------


def test_get_layout_returns_tabs(
    minimal_config_yaml: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_file = _write_config(tmp_path, minimal_config_yaml)
    backend = FakeGroupBackend(forum_tabs=True)
    folder_backend = FakeFolderBackend()
    store = OperationStore(tmp_path / "state.db")
    _patch_group_backends(monkeypatch, backend, folder_backend, store)

    runner = CliRunner()
    result = runner.invoke(
        cli_main.app,
        [
            "groups",
            "get-layout",
            "--chat-id",
            "-100",
            "--config",
            str(config_file),
        ],
    )
    assert result.exit_code == 0, result.stdout
    assert result.stdout.strip().splitlines()[-1] == "tabs"
    assert backend.get_calls == [-100]


def test_get_layout_returns_list(
    minimal_config_yaml: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_file = _write_config(tmp_path, minimal_config_yaml)
    backend = FakeGroupBackend(forum_tabs=False)
    folder_backend = FakeFolderBackend()
    store = OperationStore(tmp_path / "state.db")
    _patch_group_backends(monkeypatch, backend, folder_backend, store)

    runner = CliRunner()
    result = runner.invoke(
        cli_main.app,
        [
            "groups",
            "get-layout",
            "--chat-id",
            "-100",
            "--config",
            str(config_file),
        ],
    )
    assert result.exit_code == 0, result.stdout
    assert result.stdout.strip().splitlines()[-1] == "list"
    assert backend.get_calls == [-100]


def test_get_layout_missing_chat_id_errors() -> None:
    runner = CliRunner()
    result = runner.invoke(cli_main.app, ["groups", "get-layout"])
    assert result.exit_code != 0
    combined = _normalize_cli_output(result.stdout + (result.stderr or ""))
    assert "chatid" in combined
