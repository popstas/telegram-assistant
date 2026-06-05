"""Tests for Task 12 — bulk remove members (domain, HTTP, CLI)."""

from __future__ import annotations

import json
import textwrap
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from telegram_assistant.cli import main as cli_main
from telegram_assistant.config import load_config_from_text
from telegram_assistant.folders import FolderChat, FolderSnapshot
from telegram_assistant.http_api import create_app
from telegram_assistant.members import (
    BulkMemberRemoveItem,
    BulkMemberRemoveNeedsReview,
    BulkMemberRemoveRequest,
    MemberNotPresentError,
    bulk_remove_members,
    protected_user_set,
)
from telegram_assistant.persistence import (
    OperationStatus,
    OperationStore,
)
from telegram_assistant.worker import WorkerQueue


class FakeMemberRemoveBackend:
    def __init__(
        self,
        *,
        not_present: set[str] | None = None,
        fail_ban: dict[str, BaseException] | None = None,
        fail_unban: dict[str, BaseException] | None = None,
    ) -> None:
        self._not_present = not_present or set()
        self._fail_ban = fail_ban or {}
        self._fail_unban = fail_unban or {}
        self.banned: list[tuple[int, str]] = []
        self.unbanned: list[tuple[int, str]] = []

    async def ban_member(self, *, chat_id: int, user: str) -> None:
        if user in self._not_present:
            raise MemberNotPresentError(f"not in chat {user}")
        if user in self._fail_ban:
            raise self._fail_ban[user]
        self.banned.append((chat_id, user))

    async def unban_member(self, *, chat_id: int, user: str) -> None:
        if user in self._fail_unban:
            raise self._fail_unban[user]
        self.unbanned.append((chat_id, user))


class FakeFolderBackend:
    def __init__(self, folders: list[FolderSnapshot]) -> None:
        self._folders = folders

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
        raise NotImplementedError

    async def add_chat_to_folder(self, folder_id: int, chat_id: int) -> None:
        raise NotImplementedError


@pytest.fixture()
def store(tmp_path: Path) -> OperationStore:
    return OperationStore(tmp_path / "state.db")


def _make_queue(store: OperationStore) -> WorkerQueue:
    async def fake_sleep(seconds: float) -> None:
        return None

    return WorkerQueue(
        store,
        max_parallel=1,
        flood_wait_safety_margin_seconds=1.0,
        sleep=fake_sleep,
    )


# ---------------------------------------------------------------------------
# Domain
# ---------------------------------------------------------------------------


async def test_bulk_remove_happy_path_ban_unban_kicks(
    store: OperationStore,
) -> None:
    backend = FakeMemberRemoveBackend()
    queue = _make_queue(store)
    req = BulkMemberRemoveRequest(
        telegram_chat_id=-100,
        items=(
            BulkMemberRemoveItem(user="@alice"),
            BulkMemberRemoveItem(user="@bob"),
        ),
        operation_id="op-rm-happy",
    )
    result, op = await bulk_remove_members(
        backend=backend, store=store, queue=queue, request=req
    )

    assert op.status is OperationStatus.COMPLETED
    assert result.removed == 2
    assert result.not_present == 0
    # ban_unban: both ban and unban called per user, restoring default rights
    # so the user can be re-invited later.
    assert backend.banned == [(-100, "@alice"), (-100, "@bob")]
    assert backend.unbanned == [(-100, "@alice"), (-100, "@bob")]
    statuses = [it.status for it in result.items]
    assert statuses == ["removed", "removed"]


async def test_bulk_remove_ban_only_does_not_unban(store: OperationStore) -> None:
    backend = FakeMemberRemoveBackend()
    queue = _make_queue(store)
    req = BulkMemberRemoveRequest(
        telegram_chat_id=-100,
        items=(BulkMemberRemoveItem(user="@alice"),),
        mode="ban",
        operation_id="op-rm-ban",
    )
    result, op = await bulk_remove_members(
        backend=backend, store=store, queue=queue, request=req
    )
    assert op.status is OperationStatus.COMPLETED
    assert result.removed == 1
    assert backend.banned == [(-100, "@alice")]
    assert backend.unbanned == []


async def test_bulk_remove_not_present_treated_as_success(
    store: OperationStore,
) -> None:
    backend = FakeMemberRemoveBackend(not_present={"@ghost"})
    queue = _make_queue(store)
    req = BulkMemberRemoveRequest(
        telegram_chat_id=-100,
        items=(
            BulkMemberRemoveItem(user="@alice"),
            BulkMemberRemoveItem(user="@ghost"),
        ),
        operation_id="op-rm-notpresent",
    )
    result, op = await bulk_remove_members(
        backend=backend, store=store, queue=queue, request=req
    )
    statuses = [it.status for it in result.items]
    assert statuses == ["removed", "not_present"]
    assert result.not_present == 1
    assert result.removed == 1
    assert op.status is OperationStatus.COMPLETED
    assert backend.banned == [(-100, "@alice")]
    assert backend.unbanned == [(-100, "@alice")]


async def test_bulk_remove_continue_on_error_records_failure(
    store: OperationStore,
) -> None:
    backend = FakeMemberRemoveBackend(
        fail_ban={"@boom": RuntimeError("rpc error")}
    )
    queue = _make_queue(store)
    req = BulkMemberRemoveRequest(
        telegram_chat_id=-100,
        items=(
            BulkMemberRemoveItem(user="@alice"),
            BulkMemberRemoveItem(user="@boom"),
            BulkMemberRemoveItem(user="@charlie"),
        ),
        operation_id="op-rm-continue",
        continue_on_error=True,
    )
    result, op = await bulk_remove_members(
        backend=backend, store=store, queue=queue, request=req
    )
    statuses = [it.status for it in result.items]
    assert statuses == ["removed", "failed", "removed"]
    assert result.failed == 1
    assert op.status is OperationStatus.NEEDS_REVIEW
    assert "rpc error" in (result.items[1].error or "")


async def test_bulk_remove_stop_on_error_short_circuits(
    store: OperationStore,
) -> None:
    backend = FakeMemberRemoveBackend(
        fail_ban={"@boom": RuntimeError("rpc error")}
    )
    queue = _make_queue(store)
    req = BulkMemberRemoveRequest(
        telegram_chat_id=-100,
        items=(
            BulkMemberRemoveItem(user="@alice"),
            BulkMemberRemoveItem(user="@boom"),
            BulkMemberRemoveItem(user="@charlie"),
        ),
        operation_id="op-rm-stop",
        continue_on_error=False,
    )
    result, op = await bulk_remove_members(
        backend=backend, store=store, queue=queue, request=req
    )
    statuses = [it.status for it in result.items]
    assert statuses == ["removed", "failed", "skipped"]
    assert op.status is OperationStatus.NEEDS_REVIEW
    assert backend.banned == [(-100, "@alice")]


async def test_bulk_remove_replay_returns_existed(store: OperationStore) -> None:
    queue = _make_queue(store)
    req = BulkMemberRemoveRequest(
        telegram_chat_id=-100,
        items=(BulkMemberRemoveItem(user="@alice"),),
        operation_id="op-rm-replay",
    )
    backend1 = FakeMemberRemoveBackend()
    first, _ = await bulk_remove_members(
        backend=backend1, store=store, queue=queue, request=req
    )
    assert first.removed == 1

    backend2 = FakeMemberRemoveBackend()
    second, op2 = await bulk_remove_members(
        backend=backend2, store=store, queue=queue, request=req
    )
    assert backend2.banned == []
    assert second.removed == 0
    assert second.existed == 1
    assert op2.status is OperationStatus.COMPLETED


async def test_bulk_remove_replay_after_needs_review_raises(
    store: OperationStore,
) -> None:
    backend = FakeMemberRemoveBackend(fail_ban={"@boom": RuntimeError("boom")})
    queue = _make_queue(store)
    req = BulkMemberRemoveRequest(
        telegram_chat_id=-100,
        items=(BulkMemberRemoveItem(user="@boom"),),
        operation_id="op-rm-nr",
    )
    _, op = await bulk_remove_members(
        backend=backend, store=store, queue=queue, request=req
    )
    assert op.status is OperationStatus.NEEDS_REVIEW

    with pytest.raises(BulkMemberRemoveNeedsReview):
        await bulk_remove_members(
            backend=FakeMemberRemoveBackend(),
            store=store,
            queue=queue,
            request=req,
        )


async def test_bulk_remove_rejects_empty_items(store: OperationStore) -> None:
    queue = _make_queue(store)
    with pytest.raises(ValueError):
        await bulk_remove_members(
            backend=FakeMemberRemoveBackend(),
            store=store,
            queue=queue,
            request=BulkMemberRemoveRequest(telegram_chat_id=-100, items=()),
        )


def test_bulk_remove_request_rejects_invalid_mode() -> None:
    with pytest.raises(ValueError):
        BulkMemberRemoveRequest(
            telegram_chat_id=-100,
            items=(BulkMemberRemoveItem(user="@x"),),
            mode="purge",
        )


def test_protected_user_set_includes_reserves_and_planfix_bot(
    minimal_config_yaml: str,
) -> None:
    config = load_config_from_text(minimal_config_yaml)
    protected = protected_user_set(config=config.telegram)
    # @planfix_bot is always protected, regardless of config contents.
    assert "@planfix_bot" in protected
    # reserve_admins from the fixture
    assert "@reserve_account" in protected


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


def _http_client(
    minimal_config_yaml: str,
    backend: FakeMemberRemoveBackend,
    *,
    store: OperationStore | None = None,
) -> TestClient:
    config = load_config_from_text(minimal_config_yaml)
    if store is None:
        import tempfile

        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".db")
        tmp.close()
        store = OperationStore(Path(tmp.name))
    app = create_app(
        config,
        session_manager=None,
        member_backend_factory=lambda _req: backend,
        member_remove_backend_factory=lambda _req: backend,
        operation_store=store,
    )
    return TestClient(app)


def test_http_bulk_remove_happy_path(minimal_config_yaml: str) -> None:
    backend = FakeMemberRemoveBackend()
    client = _http_client(minimal_config_yaml, backend)
    resp = client.post(
        "/telegram/groups/-100/members/bulk-remove",
        json={
            "operation_id": "op-http-rm",
            "items": [{"user": "@alice"}, {"user": "@bob"}],
        },
        headers={"Authorization": "Bearer secret_token"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["removed"] == 2
    assert body["mode"] == "ban_unban"
    assert body["operation_status"] == "completed"


def test_http_bulk_remove_requires_auth(minimal_config_yaml: str) -> None:
    backend = FakeMemberRemoveBackend()
    client = _http_client(minimal_config_yaml, backend)
    resp = client.post(
        "/telegram/groups/-100/members/bulk-remove",
        json={"items": [{"user": "@x"}]},
    )
    assert resp.status_code == 401


def test_http_bulk_remove_rejects_invalid_mode(
    minimal_config_yaml: str,
) -> None:
    backend = FakeMemberRemoveBackend()
    client = _http_client(minimal_config_yaml, backend)
    resp = client.post(
        "/telegram/groups/-100/members/bulk-remove",
        json={"items": [{"user": "@x"}], "mode": "purge"},
        headers={"Authorization": "Bearer secret_token"},
    )
    assert resp.status_code == 400


def test_http_bulk_remove_refuses_protected_without_force(
    minimal_config_yaml: str,
) -> None:
    backend = FakeMemberRemoveBackend()
    client = _http_client(minimal_config_yaml, backend)
    resp = client.post(
        "/telegram/groups/-100/members/bulk-remove",
        json={
            "operation_id": "op-prot",
            "items": [{"user": "@alice"}, {"user": "@planfix_bot"}],
        },
        headers={"Authorization": "Bearer secret_token"},
    )
    assert resp.status_code == 409, resp.text
    body = resp.json()
    assert body["detail"]["error"] == "protected_accounts"
    assert body["detail"]["users"] == ["@planfix_bot"]


def test_http_bulk_remove_allows_protected_with_force(
    minimal_config_yaml: str,
) -> None:
    backend = FakeMemberRemoveBackend()
    client = _http_client(minimal_config_yaml, backend)
    resp = client.post(
        "/telegram/groups/-100/members/bulk-remove",
        json={
            "operation_id": "op-force",
            "items": [{"user": "@planfix_bot"}],
            "force": True,
        },
        headers={"Authorization": "Bearer secret_token"},
    )
    assert resp.status_code == 200, resp.text


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _write_config(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "config.yml"
    p.write_text(body)
    return p


def _patch_cli_member_backends(
    monkeypatch: pytest.MonkeyPatch,
    backend: FakeMemberRemoveBackend,
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

    monkeypatch.setattr(cli_main, "_build_member_backends", _factory)


def test_cli_members_bulk_remove_happy_path(
    minimal_config_yaml: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_file = _write_config(tmp_path, minimal_config_yaml)
    backend = FakeMemberRemoveBackend()
    folder_backend = FakeFolderBackend([])
    store = OperationStore(tmp_path / "state.db")
    _patch_cli_member_backends(monkeypatch, backend, folder_backend, store)

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
            "--user",
            "@bob",
            "--yes",
            "--operation-id",
            "op-cli-rm",
            "--config",
            str(config_file),
        ],
    )
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload["removed"] == 2
    assert backend.banned == [(-100, "@alice"), (-100, "@bob")]


def test_cli_members_bulk_remove_dry_run_does_not_call_backend(
    minimal_config_yaml: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_file = _write_config(tmp_path, minimal_config_yaml)
    backend = FakeMemberRemoveBackend()
    folder_backend = FakeFolderBackend([])
    store = OperationStore(tmp_path / "state.db")
    _patch_cli_member_backends(monkeypatch, backend, folder_backend, store)

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
    assert payload["dry_run"] is True
    assert payload["telegram_chat_id"] == -100
    assert payload["items"][0]["user"] == "@alice"
    assert payload["items"][0]["action"] == "would_remove"
    # Backend must NOT be called in dry-run.
    assert backend.banned == []
    assert backend.unbanned == []


def test_cli_members_bulk_remove_refuses_without_yes(
    minimal_config_yaml: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_file = _write_config(tmp_path, minimal_config_yaml)
    backend = FakeMemberRemoveBackend()
    folder_backend = FakeFolderBackend([])
    store = OperationStore(tmp_path / "state.db")
    _patch_cli_member_backends(monkeypatch, backend, folder_backend, store)

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
            "--config",
            str(config_file),
        ],
    )
    assert result.exit_code == 2
    assert "without --yes" in result.stdout + result.stderr
    assert backend.banned == []


def test_cli_members_bulk_remove_blocks_protected_without_force(
    minimal_config_yaml: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """@planfix_bot and configured reserve accounts require --force."""
    config_file = _write_config(tmp_path, minimal_config_yaml)
    backend = FakeMemberRemoveBackend()
    folder_backend = FakeFolderBackend([])
    store = OperationStore(tmp_path / "state.db")
    _patch_cli_member_backends(monkeypatch, backend, folder_backend, store)

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
            "--yes",
            "--config",
            str(config_file),
        ],
    )
    assert result.exit_code == 2
    combined = result.stdout + result.stderr
    assert "protected" in combined
    assert backend.banned == []


def test_cli_members_bulk_remove_force_allows_protected(
    minimal_config_yaml: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_file = _write_config(tmp_path, minimal_config_yaml)
    backend = FakeMemberRemoveBackend()
    folder_backend = FakeFolderBackend([])
    store = OperationStore(tmp_path / "state.db")
    _patch_cli_member_backends(monkeypatch, backend, folder_backend, store)

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
            "--yes",
            "--force",
            "--operation-id",
            "op-cli-force",
            "--config",
            str(config_file),
        ],
    )
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload["removed"] == 1
    assert backend.banned == [(-100, "@planfix_bot")]


def test_cli_members_bulk_remove_continue_on_error(
    minimal_config_yaml: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_file = _write_config(tmp_path, minimal_config_yaml)
    backend = FakeMemberRemoveBackend(
        fail_ban={"@boom": RuntimeError("rpc error")}
    )
    folder_backend = FakeFolderBackend([])
    store = OperationStore(tmp_path / "state.db")
    _patch_cli_member_backends(monkeypatch, backend, folder_backend, store)

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
            "--user",
            "@boom",
            "--user",
            "@charlie",
            "--yes",
            "--continue-on-error",
            "--operation-id",
            "op-cli-cont",
            "--config",
            str(config_file),
        ],
    )
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    statuses = [it["status"] for it in payload["items"]]
    assert statuses == ["removed", "failed", "removed"]
    assert payload["failed"] == 1
    assert payload["operation_status"] == "needs_review"


def test_cli_members_bulk_remove_from_csv(
    minimal_config_yaml: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_file = _write_config(tmp_path, minimal_config_yaml)
    csv_path = tmp_path / "members.csv"
    csv_path.write_text(
        textwrap.dedent(
            """\
            user
            @alice
            @bob
            """
        )
    )
    backend = FakeMemberRemoveBackend()
    folder_backend = FakeFolderBackend([])
    store = OperationStore(tmp_path / "state.db")
    _patch_cli_member_backends(monkeypatch, backend, folder_backend, store)

    runner = CliRunner()
    result = runner.invoke(
        cli_main.app,
        [
            "members",
            "bulk-remove",
            "--chat-id",
            "-100",
            "--file",
            str(csv_path),
            "--yes",
            "--operation-id",
            "op-cli-csv-rm",
            "--config",
            str(config_file),
        ],
    )
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload["removed"] == 2
