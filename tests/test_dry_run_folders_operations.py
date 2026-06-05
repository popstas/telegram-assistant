"""Dry-run tests for ``folders add-chat`` and ``operations retry`` (Task 4)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from telegram_assistant.cli import main as cli_main
from telegram_assistant.folders import (
    FolderChat,
    FolderSnapshot,
)
from telegram_assistant.persistence import (
    OperationStatus,
    OperationStore,
    idempotency,
)
from tests.test_dry_run_contract import assert_dry_run_envelope


def _write_config(tmp_path: Path, body: str) -> Path:
    config_file = tmp_path / "config.yml"
    config_file.write_text(body)
    return config_file


# ---------------------------------------------------------------------------
# folders add-chat
# ---------------------------------------------------------------------------


class _FakeFolderBackend:
    """In-memory FolderBackend used to drive the CLI without Telethon."""

    def __init__(self, folders: list[FolderSnapshot]) -> None:
        self._folders = folders
        self.added: list[tuple[int, int]] = []

    async def list_folders(self) -> list[FolderSnapshot]:
        return [
            FolderSnapshot(
                folder_id=f.folder_id,
                folder_name=f.folder_name,
                chats=list(f.chats),
            )
            for f in self._folders
        ]

    async def resolve_chat(self, chat_ref: str | int) -> FolderChat:
        if isinstance(chat_ref, int):
            for f in self._folders:
                for c in f.chats:
                    if c.chat_id == chat_ref:
                        return c
            return FolderChat(chat_id=chat_ref, title=f"Chat {chat_ref}")
        raise LookupError(f"unknown chat ref {chat_ref!r}")

    async def add_chat_to_folder(self, folder_id: int, chat_id: int) -> None:
        # Should not be called during dry-run; make it explicit.
        self.added.append((folder_id, chat_id))


def _patch_folder_backend(
    monkeypatch: pytest.MonkeyPatch, backend: _FakeFolderBackend
) -> None:
    class _FakeManager:
        async def disconnect(self) -> None:
            return None

    def _factory(config_path: Path | None) -> Any:
        async def _open() -> _FakeFolderBackend:
            return backend

        return None, _FakeManager(), _open

    monkeypatch.setattr(cli_main, "_build_folder_backend", _factory)


def _sample_folder(chats: list[FolderChat] | None = None) -> FolderSnapshot:
    return FolderSnapshot(
        folder_id=2,
        folder_name="Planfix clients",
        chats=list(chats) if chats is not None else [
            FolderChat(chat_id=100, title="Acme"),
        ],
    )


def test_folders_add_chat_dry_run_envelope_by_id(
    minimal_config_yaml: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_file = _write_config(tmp_path, minimal_config_yaml)
    backend = _FakeFolderBackend([_sample_folder()])
    _patch_folder_backend(monkeypatch, backend)

    runner = CliRunner()
    result = runner.invoke(
        cli_main.app,
        [
            "folders",
            "add-chat",
            "--folder-name",
            "Planfix clients",
            "--chat-id",
            "300",
            "--dry-run",
            "--config",
            str(config_file),
        ],
    )
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert_dry_run_envelope(
        payload,
        command="folders.add_chat",
        resolved_keys=(
            "folder_id",
            "folder_name",
            "chat_id",
            "already_in_folder",
        ),
    )
    assert payload["resolved"]["chat_id"] == 300
    assert payload["resolved"]["already_in_folder"] is False
    # The backend mutation must not have been called.
    assert backend.added == []
    assert any("add chat 300" in line for line in payload["planned_actions"])


def test_folders_add_chat_dry_run_already_in_folder_warns(
    minimal_config_yaml: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_file = _write_config(
        tmp_path, minimal_config_yaml
    )
    folder = _sample_folder([FolderChat(chat_id=100, title="Acme")])
    backend = _FakeFolderBackend([folder])
    _patch_folder_backend(monkeypatch, backend)

    runner = CliRunner()
    result = runner.invoke(
        cli_main.app,
        [
            "folders",
            "add-chat",
            "--folder-name",
            "Planfix clients",
            "--chat-id",
            "100",
            "--dry-run",
            "--config",
            str(config_file),
        ],
    )
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert_dry_run_envelope(
        payload,
        command="folders.add_chat",
        resolved_keys=("already_in_folder",),
    )
    assert payload["resolved"]["already_in_folder"] is True
    assert payload["warnings"]
    assert any("already in folder" in w for w in payload["warnings"])
    assert backend.added == []


def test_folders_add_chat_dry_run_missing_folder_matches_real_run_error(
    minimal_config_yaml: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Dry-run must surface the same validation error as the real run."""
    config_file = _write_config(tmp_path, minimal_config_yaml)
    backend = _FakeFolderBackend([])
    _patch_folder_backend(monkeypatch, backend)

    runner = CliRunner()
    dry_result = runner.invoke(
        cli_main.app,
        [
            "folders",
            "add-chat",
            "--folder-name",
            "Nope",
            "--chat-id",
            "300",
            "--dry-run",
            "--config",
            str(config_file),
        ],
    )
    real_result = runner.invoke(
        cli_main.app,
        [
            "folders",
            "add-chat",
            "--folder-name",
            "Nope",
            "--chat-id",
            "300",
            "--config",
            str(config_file),
        ],
    )
    assert dry_result.exit_code == 2
    assert real_result.exit_code == 2
    combined_dry = (dry_result.stdout or "") + (dry_result.stderr or "")
    combined_real = (real_result.stdout or "") + (real_result.stderr or "")
    assert "Nope" in combined_dry
    assert "Nope" in combined_real


def test_folders_add_chat_dry_run_requires_chat_ref(
    minimal_config_yaml: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_file = _write_config(tmp_path, minimal_config_yaml)
    backend = _FakeFolderBackend([_sample_folder()])
    _patch_folder_backend(monkeypatch, backend)

    runner = CliRunner()
    result = runner.invoke(
        cli_main.app,
        [
            "folders",
            "add-chat",
            "--folder-name",
            "Planfix clients",
            "--dry-run",
            "--config",
            str(config_file),
        ],
    )
    # Exact same error as the real run when no chat ref is provided.
    assert result.exit_code == 2


# ---------------------------------------------------------------------------
# operations retry
# ---------------------------------------------------------------------------


def _patch_store_path(
    monkeypatch: pytest.MonkeyPatch, db_path: Path
) -> None:
    monkeypatch.setattr(cli_main, "default_database_path", lambda _cfg: db_path)


def test_operations_retry_dry_run_envelope_for_failed_operation(
    minimal_config_yaml: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_file = _write_config(tmp_path, minimal_config_yaml)
    db_path = tmp_path / "state.db"
    _patch_store_path(monkeypatch, db_path)

    store = OperationStore(db_path)
    op = store.begin_operation(
        operation_type=idempotency.TOPIC_BULK_CREATE,
        idempotency_key="topic_bulk_create:retry",
        request_payload={},
    )
    bad, _ = store.begin_item(
        operation_id=op.operation.id,
        idempotency_key="bad",
        request_payload={},
    )
    nr, _ = store.begin_item(
        operation_id=op.operation.id,
        idempotency_key="needs",
        request_payload={},
    )
    good, _ = store.begin_item(
        operation_id=op.operation.id,
        idempotency_key="good",
        request_payload={},
    )
    store.fail_item(bad.id, "boom")
    store.mark_item_needs_review(nr.id, "timeout")
    store.complete_item(good.id, {"ok": True})
    store.fail_operation(op.operation.id, "all together now")

    runner = CliRunner()
    result = runner.invoke(
        cli_main.app,
        [
            "operations",
            "retry",
            "--operation-id",
            op.operation.id,
            "--dry-run",
            "--config",
            str(config_file),
        ],
    )
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert_dry_run_envelope(
        payload,
        command="operations.retry",
        resolved_keys=(
            "operation_id",
            "operation_status",
            "would_reset_operation",
            "items_to_reset",
        ),
    )
    assert payload["resolved"]["operation_id"] == op.operation.id
    assert payload["resolved"]["would_reset_operation"] is True
    keys = {it["idempotency_key"] for it in payload["resolved"]["items_to_reset"]}
    assert keys == {"bad", "needs"}
    # State must not have been touched.
    refreshed_items = {
        it.idempotency_key: it for it in store.list_items(op.operation.id)
    }
    assert refreshed_items["bad"].status is OperationStatus.FAILED
    assert refreshed_items["needs"].status is OperationStatus.NEEDS_REVIEW
    assert refreshed_items["good"].status is OperationStatus.COMPLETED
    refreshed_op = store.get_operation(op.operation.id)
    assert refreshed_op.status is OperationStatus.FAILED


def test_operations_retry_dry_run_completed_matches_real_run_error(
    minimal_config_yaml: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``operations retry --dry-run`` must reject a completed op same as real run."""
    config_file = _write_config(tmp_path, minimal_config_yaml)
    db_path = tmp_path / "state.db"
    _patch_store_path(monkeypatch, db_path)

    store = OperationStore(db_path)
    op = store.begin_operation(
        operation_type=idempotency.GROUP_CREATE,
        idempotency_key="group_create:title=Done",
        request_payload={},
    )
    store.complete_operation(op.operation.id, {"ok": True})

    runner = CliRunner()
    dry_result = runner.invoke(
        cli_main.app,
        [
            "operations",
            "retry",
            "--operation-id",
            op.operation.id,
            "--dry-run",
            "--config",
            str(config_file),
        ],
    )
    real_result = runner.invoke(
        cli_main.app,
        [
            "operations",
            "retry",
            "--operation-id",
            op.operation.id,
            "--config",
            str(config_file),
        ],
    )
    assert dry_result.exit_code == 2
    assert real_result.exit_code == 2
    combined_dry = (dry_result.stdout or "") + (dry_result.stderr or "")
    combined_real = (real_result.stdout or "") + (real_result.stderr or "")
    assert "completed" in combined_dry
    assert "completed" in combined_real


def test_operations_retry_dry_run_unknown_id_matches_real_run_error(
    minimal_config_yaml: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_file = _write_config(tmp_path, minimal_config_yaml)
    db_path = tmp_path / "state.db"
    _patch_store_path(monkeypatch, db_path)
    OperationStore(db_path)  # bootstrap schema

    runner = CliRunner()
    dry_result = runner.invoke(
        cli_main.app,
        [
            "operations",
            "retry",
            "--operation-id",
            "missing",
            "--dry-run",
            "--config",
            str(config_file),
        ],
    )
    real_result = runner.invoke(
        cli_main.app,
        [
            "operations",
            "retry",
            "--operation-id",
            "missing",
            "--config",
            str(config_file),
        ],
    )
    assert dry_result.exit_code == 2
    assert real_result.exit_code == 2
    combined_dry = (dry_result.stdout or "") + (dry_result.stderr or "")
    combined_real = (real_result.stdout or "") + (real_result.stderr or "")
    assert "not found" in combined_dry
    assert "not found" in combined_real


def test_operations_retry_dry_run_pending_no_items_is_noop(
    minimal_config_yaml: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pending op with no failed items: planned_actions is the no-op line."""
    config_file = _write_config(tmp_path, minimal_config_yaml)
    db_path = tmp_path / "state.db"
    _patch_store_path(monkeypatch, db_path)

    store = OperationStore(db_path)
    op = store.begin_operation(
        operation_type=idempotency.GROUP_CREATE,
        idempotency_key="group_create:title=Pending",
        request_payload={},
    )

    runner = CliRunner()
    result = runner.invoke(
        cli_main.app,
        [
            "operations",
            "retry",
            "--operation-id",
            op.operation.id,
            "--dry-run",
            "--config",
            str(config_file),
        ],
    )
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert_dry_run_envelope(
        payload,
        command="operations.retry",
        resolved_keys=("would_reset_operation", "items_to_reset"),
    )
    assert payload["resolved"]["would_reset_operation"] is False
    assert payload["resolved"]["items_to_reset"] == []
    assert payload["warnings"]
    assert any(
        "no-op" in line for line in payload["planned_actions"]
    )


# ---------------------------------------------------------------------------
# Read-only commands must NOT accept --dry-run (regression guard for Task 4).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        ("auth",),
        ("health",),
        ("folders", "inspect"),
        ("operations", "status"),
    ],
)
def test_read_only_commands_reject_dry_run(command: tuple[str, ...]) -> None:
    runner = CliRunner()
    result = runner.invoke(cli_main.app, [*command, "--dry-run"])
    assert result.exit_code != 0, (command, result.stdout)
    combined = (result.stdout or "") + (result.stderr or "")
    assert "--dry-run" in combined or "No such option" in combined, (
        command,
        combined,
    )
