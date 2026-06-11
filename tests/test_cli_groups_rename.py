"""CLI tests for ``groups rename`` (plan Task 4).

The CLI builds its backends via ``_build_group_backends``; we monkeypatch that
factory to inject fakes so no real Telegram traffic happens.
"""

from __future__ import annotations

import json
import re
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
from telegram_assistant.persistence import OperationStore

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _normalize_cli_output(text: str) -> str:
    """Collapse rich panel formatting so substring checks survive wrapping."""
    return re.sub(r"[^a-z0-9]", "", _ANSI_RE.sub("", text).lower())


def _write_config(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "config.yml"
    p.write_text(body)
    return p


def _config_deny_all(minimal_config_yaml: str) -> str:
    """Insert a present-but-empty access policy (deny-by-default)."""
    access = textwrap.indent("access:\n  rules: []\n", "  ")
    return minimal_config_yaml.replace(
        "  default_chat_folder:",
        access + "  default_chat_folder:",
    )


class FakeGroupBackend:
    """Records set_title calls; raises if unrelated methods are hit."""

    def __init__(self) -> None:
        self.set_title_calls: list[tuple[int, str]] = []

    async def create_supergroup(
        self, *, title: str, about: str | None, enable_topics: bool
    ) -> int:  # pragma: no cover - must not be called
        raise AssertionError("rename must not create supergroups")

    async def add_member(self, *, chat_id: int, user: str) -> None:  # pragma: no cover
        raise AssertionError("rename must not add members")

    async def set_title(self, *, chat_id: int, title: str) -> None:
        self.set_title_calls.append((chat_id, title))


class FakeFolderBackend:
    """No-op folder backend; not used when addressing by --chat-id/--entity."""

    async def list_folders(self) -> list[Any]:  # pragma: no cover
        raise AssertionError("rename by chat-id must not touch folders")


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


class _FakeManager:
    def __init__(self, resolver: FakeResolver | None = None) -> None:
        self._resolver = resolver

    async def get_client(self) -> Any:
        return object()

    async def disconnect(self) -> None:
        return None


def _patch_group_backends(
    monkeypatch: pytest.MonkeyPatch,
    backend: FakeGroupBackend,
    folder_backend: FakeFolderBackend,
    store: OperationStore,
    *,
    resolver: FakeResolver | None = None,
) -> None:
    manager = _FakeManager(resolver)

    def _factory(config_path: Path | None) -> Any:
        from telegram_assistant.config import load_config

        config = load_config(config_path)

        async def _open() -> Any:
            return backend, folder_backend

        return config, manager, store, _open

    monkeypatch.setattr(cli_main, "_build_group_backends", _factory)

    # When a resolver is needed (entity / access policy), the CLI constructs a
    # TelethonEntityResolver from the live client. Swap in the fake instead.
    if resolver is not None:
        import telegram_assistant.entities as entities_mod

        monkeypatch.setattr(
            entities_mod, "TelethonEntityResolver", lambda _client: resolver
        )


def _last_json_line(stdout: str) -> dict[str, Any]:
    return json.loads(stdout.strip().splitlines()[-1])


def _run(args: list[str]) -> Any:
    return CliRunner().invoke(cli_main.app, args)


# ---------------------------------------------------------------------------
# happy path
# ---------------------------------------------------------------------------


def test_rename_success_by_chat_id(
    minimal_config_yaml: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_file = _write_config(tmp_path, minimal_config_yaml)
    backend = FakeGroupBackend()
    store = OperationStore(tmp_path / "state.db")
    _patch_group_backends(monkeypatch, backend, FakeFolderBackend(), store)

    result = _run(
        [
            "groups",
            "rename",
            "--chat-id",
            "-100123",
            "--new-title",
            "Renamed Group",
            "--config",
            str(config_file),
        ]
    )
    assert result.exit_code == 0, result.stdout
    payload = _last_json_line(result.stdout)
    assert payload["telegram_chat_id"] == -100123
    assert payload["new_title"] == "Renamed Group"
    assert payload["status"] == "renamed"
    assert payload["replayed"] is False
    assert payload["operation_status"] == "completed"
    assert backend.set_title_calls == [(-100123, "Renamed Group")]


def test_rename_strips_whitespace_in_backend_call(
    minimal_config_yaml: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_file = _write_config(tmp_path, minimal_config_yaml)
    backend = FakeGroupBackend()
    store = OperationStore(tmp_path / "state.db")
    _patch_group_backends(monkeypatch, backend, FakeFolderBackend(), store)

    result = _run(
        [
            "groups",
            "rename",
            "--chat-id",
            "-100",
            "--new-title",
            "  Spaced  ",
            "--config",
            str(config_file),
        ]
    )
    assert result.exit_code == 0, result.stdout
    assert backend.set_title_calls == [(-100, "Spaced")]


def test_rename_success_via_entity(
    minimal_config_yaml: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_file = _write_config(tmp_path, minimal_config_yaml)
    backend = FakeGroupBackend()
    store = OperationStore(tmp_path / "state.db")
    resolver = FakeResolver(mapping={"@client": -100999})
    _patch_group_backends(
        monkeypatch, backend, FakeFolderBackend(), store, resolver=resolver
    )

    result = _run(
        [
            "groups",
            "rename",
            "--entity",
            "@client",
            "--new-title",
            "Via Entity",
            "--config",
            str(config_file),
        ]
    )
    assert result.exit_code == 0, result.stdout
    assert backend.set_title_calls == [(-100999, "Via Entity")]


def test_rename_replays_same_title(
    minimal_config_yaml: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_file = _write_config(tmp_path, minimal_config_yaml)
    backend = FakeGroupBackend()
    store = OperationStore(tmp_path / "state.db")
    _patch_group_backends(monkeypatch, backend, FakeFolderBackend(), store)

    args = [
        "groups",
        "rename",
        "--chat-id",
        "-100",
        "--new-title",
        "Same",
        "--config",
        str(config_file),
    ]
    first = _run(args)
    assert first.exit_code == 0, first.stdout
    second = _run(args)
    assert second.exit_code == 0, second.stdout
    payload = _last_json_line(second.stdout)
    assert payload["replayed"] is True
    # The second run must not touch the backend again.
    assert backend.set_title_calls == [(-100, "Same")]


# ---------------------------------------------------------------------------
# dry-run
# ---------------------------------------------------------------------------


def test_rename_dry_run_does_not_call_backend(
    minimal_config_yaml: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_file = _write_config(tmp_path, minimal_config_yaml)
    backend = FakeGroupBackend()
    store = OperationStore(tmp_path / "state.db")
    _patch_group_backends(monkeypatch, backend, FakeFolderBackend(), store)

    result = _run(
        [
            "groups",
            "rename",
            "--chat-id",
            "-100",
            "--new-title",
            "Planned",
            "--dry-run",
            "--config",
            str(config_file),
        ]
    )
    assert result.exit_code == 0, result.stdout
    payload = _last_json_line(result.stdout)
    assert payload["status"] == "dry_run"
    assert payload["dry_run"] is True
    assert payload["command"] == "groups.rename"
    assert payload["resolved"]["telegram_chat_id"] == -100
    assert payload["resolved"]["new_title"] == "Planned"
    assert any("Planned" in action for action in payload["planned_actions"])

    # Dry-run must not touch the backend or persist any operation row.
    assert backend.set_title_calls == []
    with store._connect() as conn:  # noqa: SLF001 (test introspection)
        count = int(conn.execute("SELECT COUNT(*) FROM operations").fetchone()[0])
    assert count == 0


# ---------------------------------------------------------------------------
# validation / error exit codes
# ---------------------------------------------------------------------------


def test_rename_missing_new_title_errors() -> None:
    result = _run(["groups", "rename", "--chat-id", "-100"])
    assert result.exit_code != 0
    combined = _normalize_cli_output(result.stdout + (result.stderr or ""))
    assert "newtitle" in combined


def test_rename_blank_new_title_exit_2(
    minimal_config_yaml: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_file = _write_config(tmp_path, minimal_config_yaml)
    backend = FakeGroupBackend()
    store = OperationStore(tmp_path / "state.db")
    _patch_group_backends(monkeypatch, backend, FakeFolderBackend(), store)

    result = _run(
        [
            "groups",
            "rename",
            "--chat-id",
            "-100",
            "--new-title",
            "   ",
            "--config",
            str(config_file),
        ]
    )
    assert result.exit_code == 2
    assert backend.set_title_calls == []


def test_rename_requires_exactly_one_chat_ref(
    minimal_config_yaml: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_file = _write_config(tmp_path, minimal_config_yaml)
    backend = FakeGroupBackend()
    store = OperationStore(tmp_path / "state.db")
    _patch_group_backends(monkeypatch, backend, FakeFolderBackend(), store)

    result = _run(
        ["groups", "rename", "--new-title", "X", "--config", str(config_file)]
    )
    assert result.exit_code == 2
    assert backend.set_title_calls == []


def test_rename_entity_not_found_exit_2(
    minimal_config_yaml: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_file = _write_config(tmp_path, minimal_config_yaml)
    backend = FakeGroupBackend()
    store = OperationStore(tmp_path / "state.db")
    resolver = FakeResolver(error=EntityNotFoundError("no entity"))
    _patch_group_backends(
        monkeypatch, backend, FakeFolderBackend(), store, resolver=resolver
    )

    result = _run(
        [
            "groups",
            "rename",
            "--entity",
            "@ghost",
            "--new-title",
            "X",
            "--config",
            str(config_file),
        ]
    )
    assert result.exit_code == 2
    assert backend.set_title_calls == []


def test_rename_entity_ambiguous_exit_2(
    minimal_config_yaml: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_file = _write_config(tmp_path, minimal_config_yaml)
    backend = FakeGroupBackend()
    store = OperationStore(tmp_path / "state.db")
    resolver = FakeResolver(error=AmbiguousEntityError(ref="Team", matches=[1, 2]))
    _patch_group_backends(
        monkeypatch, backend, FakeFolderBackend(), store, resolver=resolver
    )

    result = _run(
        [
            "groups",
            "rename",
            "--entity",
            "Team",
            "--new-title",
            "X",
            "--config",
            str(config_file),
        ]
    )
    assert result.exit_code == 2
    assert backend.set_title_calls == []


def test_rename_access_denied_exit_3(
    minimal_config_yaml: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_file = _write_config(tmp_path, _config_deny_all(minimal_config_yaml))
    backend = FakeGroupBackend()
    store = OperationStore(tmp_path / "state.db")
    resolver = FakeResolver(mapping={"@client": -100})
    _patch_group_backends(
        monkeypatch, backend, FakeFolderBackend(), store, resolver=resolver
    )

    result = _run(
        [
            "groups",
            "rename",
            "--chat-id",
            "-100",
            "--new-title",
            "Denied",
            "--config",
            str(config_file),
        ]
    )
    assert result.exit_code == cli_main.ACCESS_DENIED_EXIT_CODE
    assert backend.set_title_calls == []
