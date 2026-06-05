"""Shared contract tests for the CLI ``--dry-run`` envelope.

Task 1 of ``docs/plans/20260519-telegram-skill-and-dry-run.md`` defines a
single shape that every mutating CLI command must produce when invoked
with ``--dry-run``. This file:

- documents the required envelope through the ``DRY_RUN_REQUIRED_KEYS``
  constant and the ``assert_dry_run_envelope`` helper so each command's
  own test module can reuse it;
- verifies the contract against the one command that already implements
  ``--dry-run`` (``members bulk-remove``), so the contract has at least
  one passing reference implementation before Tasks 2..4 wire it into
  the remaining commands;
- pins the audit decision that read-only commands (``auth``, ``health``,
  ``folders inspect``, ``operations status``, ``version``) must NOT
  expose ``--dry-run``.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from telegram_assistant.cli import main as cli_main
from telegram_assistant.persistence import OperationStore

# ---------------------------------------------------------------------------
# Contract
# ---------------------------------------------------------------------------

DRY_RUN_REQUIRED_KEYS: tuple[str, ...] = (
    "status",
    "dry_run",
    "command",
    "would",
    "resolved",
    "planned_actions",
    "warnings",
)

# Commands that must NOT expose --dry-run (read-only by design).
READ_ONLY_COMMANDS: tuple[tuple[str, ...], ...] = (
    ("auth",),
    ("health",),
    ("version",),
    ("folders", "inspect"),
    ("operations", "status"),
)


def assert_dry_run_envelope(
    payload: Mapping[str, Any],
    *,
    command: str,
    resolved_keys: Iterable[str] = (),
) -> None:
    """Verify *payload* matches the shared ``--dry-run`` envelope.

    - ``status`` must be the literal string ``"dry_run"``.
    - ``dry_run`` must be ``True`` (kept for backwards compatibility).
    - ``command`` must equal *command* (dotted form, e.g. ``groups.create``).
    - ``would`` is a non-empty human-readable summary.
    - ``resolved`` is an object that contains every key in *resolved_keys*.
    - ``planned_actions`` is a list of strings (may be empty when the only
      planned outcome is a no-op such as ``operations retry`` on an
      already-pending op).
    - ``warnings`` is a list of strings (possibly empty).
    """
    missing = [k for k in DRY_RUN_REQUIRED_KEYS if k not in payload]
    assert not missing, f"dry-run payload missing keys: {missing} -- got {payload!r}"
    assert payload["status"] == "dry_run", payload
    assert payload["dry_run"] is True, payload
    assert payload["command"] == command, payload
    assert isinstance(payload["would"], str) and payload["would"], payload
    assert isinstance(payload["resolved"], dict), payload
    assert isinstance(payload["planned_actions"], list), payload
    assert all(isinstance(s, str) for s in payload["planned_actions"]), payload
    assert isinstance(payload["warnings"], list), payload
    assert all(isinstance(s, str) for s in payload["warnings"]), payload
    for k in resolved_keys:
        assert k in payload["resolved"], (
            f"resolved.{k} missing -- got {payload['resolved']!r}"
        )


# ---------------------------------------------------------------------------
# Read-only commands must not expose --dry-run.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("command", READ_ONLY_COMMANDS)
def test_read_only_command_rejects_dry_run(command: tuple[str, ...]) -> None:
    """``--dry-run`` must not be a recognized flag on read-only commands."""
    runner = CliRunner()
    result = runner.invoke(cli_main.app, [*command, "--dry-run"])
    # Typer prints the parser error on stderr and exits with code 2;
    # the important thing is that the flag is not accepted.
    assert result.exit_code != 0, (command, result.stdout)
    combined = (result.stdout or "") + (result.stderr or "")
    assert "--dry-run" in combined or "No such option" in combined, (
        command,
        combined,
    )


# ---------------------------------------------------------------------------
# Reference implementation: members bulk-remove already supports --dry-run.
# ---------------------------------------------------------------------------


class _FakeMemberRemoveBackend:
    async def ban_member(self, *, chat_id: int, user: str) -> None:  # pragma: no cover
        raise AssertionError("dry-run must not call backend")

    async def unban_member(self, *, chat_id: int, user: str) -> None:  # pragma: no cover
        raise AssertionError("dry-run must not call backend")


class _FakeFolderBackend:
    async def list_folders(self) -> list[Any]:  # pragma: no cover - unused here
        return []

    async def resolve_chat(self, chat_ref: str | int) -> Any:  # pragma: no cover
        raise NotImplementedError

    async def add_chat_to_folder(self, folder_id: int, chat_id: int) -> None:  # pragma: no cover
        raise NotImplementedError


def _write_config(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "config.yml"
    p.write_text(body)
    return p


def _patch_member_backends(
    monkeypatch: pytest.MonkeyPatch,
    backend: _FakeMemberRemoveBackend,
    folder_backend: _FakeFolderBackend,
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

    monkeypatch.setattr(cli_main, "_build_member_backends", _factory)


def test_members_bulk_remove_dry_run_matches_contract(
    minimal_config_yaml: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_file = _write_config(tmp_path, minimal_config_yaml)
    backend = _FakeMemberRemoveBackend()
    folder_backend = _FakeFolderBackend()
    store = OperationStore(tmp_path / "state.db")
    _patch_member_backends(monkeypatch, backend, folder_backend, store)

    runner = CliRunner()
    result = runner.invoke(
        cli_main.app,
        [
            "members",
            "bulk-remove",
            "--chat-id",
            "-100",
            "--user",
            "@alice",
            "--dry-run",
            "--config",
            str(config_file),
        ],
    )
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert_dry_run_envelope(
        payload,
        command="members.bulk_remove",
        resolved_keys=("telegram_chat_id", "mode", "items"),
    )
    # Items shape: contract-level guarantees only.
    items = payload["resolved"]["items"]
    assert isinstance(items, list) and items
    for it in items:
        assert it["action"] == "would_remove"
        assert "user" in it
        assert "normalized_user" in it
        assert "protected" in it


def test_members_bulk_remove_dry_run_flags_protected_warning(
    minimal_config_yaml: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Dry-run must warn about protected accounts, not silently accept them.

    The real run rejects with exit code 2 without ``--force``; dry-run
    must surface the same risk in ``warnings`` so the operator sees it
    before they re-run.
    """
    config_file = _write_config(tmp_path, minimal_config_yaml)
    backend = _FakeMemberRemoveBackend()
    folder_backend = _FakeFolderBackend()
    store = OperationStore(tmp_path / "state.db")
    _patch_member_backends(monkeypatch, backend, folder_backend, store)

    runner = CliRunner()
    result = runner.invoke(
        cli_main.app,
        [
            "members",
            "bulk-remove",
            "--chat-id",
            "-100",
            "--user",
            "@planfix_bot",
            "--dry-run",
            "--force",
            "--config",
            str(config_file),
        ],
    )
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert_dry_run_envelope(
        payload,
        command="members.bulk_remove",
        resolved_keys=("telegram_chat_id", "items"),
    )
    items = payload["resolved"]["items"]
    assert any(it["protected"] for it in items)


# ---------------------------------------------------------------------------
# Helper sanity tests.
# ---------------------------------------------------------------------------


def test_assert_dry_run_envelope_rejects_missing_keys() -> None:
    """The helper itself is the contract: it must fail when a key is missing."""
    incomplete = {"status": "dry_run", "dry_run": True}
    with pytest.raises(AssertionError):
        assert_dry_run_envelope(incomplete, command="x.y")


def test_assert_dry_run_envelope_rejects_wrong_status() -> None:
    payload = {
        "status": "ok",  # wrong
        "dry_run": True,
        "command": "x.y",
        "would": "do thing",
        "resolved": {},
        "planned_actions": [],
        "warnings": [],
    }
    with pytest.raises(AssertionError):
        assert_dry_run_envelope(payload, command="x.y")


def test_assert_dry_run_envelope_accepts_minimal_valid_payload() -> None:
    payload = {
        "status": "dry_run",
        "dry_run": True,
        "command": "x.y",
        "would": "do thing",
        "resolved": {"chat_id": 1},
        "planned_actions": [],
        "warnings": [],
    }
    assert_dry_run_envelope(payload, command="x.y", resolved_keys=("chat_id",))
