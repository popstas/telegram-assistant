"""CLI tests for `operations status` and `operations retry` (Task 5)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from telegram_assistant.cli import main as cli_main
from telegram_assistant.persistence import (
    OperationStatus,
    OperationStore,
    idempotency,
)


def _write_config(tmp_path: Path, body: str) -> Path:
    config_file = tmp_path / "config.yml"
    config_file.write_text(body)
    return config_file


def _patch_store_path(
    monkeypatch: pytest.MonkeyPatch, db_path: Path
) -> None:
    monkeypatch.setattr(cli_main, "default_database_path", lambda _cfg: db_path)


def test_operations_status_reports_completed(
    minimal_config_yaml: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_file = _write_config(tmp_path, minimal_config_yaml)
    db_path = tmp_path / "state.db"
    _patch_store_path(monkeypatch, db_path)

    store = OperationStore(db_path)
    op = store.begin_operation(
        operation_type=idempotency.GROUP_CREATE,
        idempotency_key="group_create:title=A",
        request_payload={"title": "A"},
    )
    store.complete_operation(op.operation.id, {"telegram_chat_id": -100})

    runner = CliRunner()
    result = runner.invoke(
        cli_main.app,
        [
            "operations",
            "status",
            "--operation-id",
            op.operation.id,
            "--config",
            str(config_file),
        ],
    )
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload["status"] == OperationStatus.COMPLETED.value
    assert payload["result_payload"] == {"telegram_chat_id": -100}
    assert payload["items"]["total"] == 0
    assert payload["items"]["needs_review"] == []


def test_operations_status_surfaces_needs_review_items(
    minimal_config_yaml: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_file = _write_config(tmp_path, minimal_config_yaml)
    db_path = tmp_path / "state.db"
    _patch_store_path(monkeypatch, db_path)

    store = OperationStore(db_path)
    op = store.begin_operation(
        operation_type=idempotency.TOPIC_BULK_CREATE,
        idempotency_key="topic_bulk_create:batch",
        request_payload={},
    )
    done, _ = store.begin_item(
        operation_id=op.operation.id,
        idempotency_key="ok",
        request_payload={"i": 1},
    )
    store.complete_item(done.id, {"ok": True})
    nr, _ = store.begin_item(
        operation_id=op.operation.id,
        idempotency_key="needs-look",
        request_payload={"i": 2},
    )
    store.mark_item_needs_review(nr.id, "undefined outcome")

    runner = CliRunner()
    result = runner.invoke(
        cli_main.app,
        [
            "operations",
            "status",
            "--operation-id",
            op.operation.id,
            "--config",
            str(config_file),
        ],
    )
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload["items"]["total"] == 2
    assert payload["items"]["by_status"][OperationStatus.COMPLETED.value] == 1
    assert payload["items"]["by_status"][OperationStatus.NEEDS_REVIEW.value] == 1
    nr_items = payload["items"]["needs_review"]
    assert len(nr_items) == 1
    assert nr_items[0]["idempotency_key"] == "needs-look"
    assert nr_items[0]["error"] == "undefined outcome"


def test_operations_status_unknown_id_exits_nonzero(
    minimal_config_yaml: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_file = _write_config(tmp_path, minimal_config_yaml)
    db_path = tmp_path / "state.db"
    _patch_store_path(monkeypatch, db_path)
    OperationStore(db_path)  # bootstrap schema

    runner = CliRunner()
    result = runner.invoke(
        cli_main.app,
        [
            "operations",
            "status",
            "--operation-id",
            "missing",
            "--config",
            str(config_file),
        ],
    )
    assert result.exit_code == 2


def test_operations_retry_resets_failed_operation_and_items(
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
            "--config",
            str(config_file),
        ],
    )
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload["operation_reset"] is True
    assert sorted(payload["items_reset"]) == sorted([bad.id, nr.id])

    refreshed_items = {it.idempotency_key: it for it in store.list_items(op.operation.id)}
    assert refreshed_items["bad"].status is OperationStatus.PENDING
    assert refreshed_items["bad"].error is None
    assert refreshed_items["needs"].status is OperationStatus.PENDING
    assert refreshed_items["needs"].error is None
    assert refreshed_items["good"].status is OperationStatus.COMPLETED
    assert refreshed_items["good"].result_payload == {"ok": True}

    refreshed_op = store.get_operation(op.operation.id)
    assert refreshed_op.status is OperationStatus.PENDING
    assert refreshed_op.error is None


def test_operations_retry_refuses_completed(
    minimal_config_yaml: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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
    result = runner.invoke(
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
    assert result.exit_code == 2


def test_operations_retry_unknown_id_exits_nonzero(
    minimal_config_yaml: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_file = _write_config(tmp_path, minimal_config_yaml)
    db_path = tmp_path / "state.db"
    _patch_store_path(monkeypatch, db_path)
    OperationStore(db_path)

    runner = CliRunner()
    result = runner.invoke(
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
    assert result.exit_code == 2
