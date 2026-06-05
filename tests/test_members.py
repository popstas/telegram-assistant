"""Tests for Task 11 — bulk add members (domain, HTTP, CLI)."""

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
    BulkMemberAddNeedsReview,
    BulkMemberAddRequest,
    BulkMemberItem,
    MemberAlreadyPresentError,
    MemberPrivacyError,
    bulk_add_members,
    normalize_user_ref,
)
from telegram_assistant.persistence import (
    OperationStatus,
    OperationStore,
)
from telegram_assistant.worker import WorkerQueue

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeMemberBackend:
    """Records every Telethon call so tests can assert on side-effects."""

    def __init__(
        self,
        *,
        privacy_for: set[str] | None = None,
        already_present: set[str] | None = None,
        fail_add: dict[str, BaseException] | None = None,
        fail_promote: dict[str, BaseException] | None = None,
    ) -> None:
        self._privacy = privacy_for or set()
        self._already = already_present or set()
        self._fail_add = fail_add or {}
        self._fail_promote = fail_promote or {}
        self.added: list[tuple[int, str]] = []
        self.promoted: list[tuple[int, str]] = []

    async def add_member(self, *, chat_id: int, user: str) -> None:
        if user in self._privacy:
            raise MemberPrivacyError(f"privacy blocked {user}")
        if user in self._already:
            raise MemberAlreadyPresentError(f"already present {user}")
        if user in self._fail_add:
            raise self._fail_add[user]
        self.added.append((chat_id, user))

    async def promote_admin(self, *, chat_id: int, user: str) -> None:
        if user in self._fail_promote:
            raise self._fail_promote[user]
        self.promoted.append((chat_id, user))


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


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


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
# normalize_user_ref
# ---------------------------------------------------------------------------


def test_normalize_user_ref_username_with_at() -> None:
    n = normalize_user_ref("@alice")
    assert n.kind == "username"
    assert n.value == "@alice"


def test_normalize_user_ref_bare_username() -> None:
    n = normalize_user_ref("bob")
    assert n.kind == "username"
    assert n.value == "@bob"


def test_normalize_user_ref_phone() -> None:
    n = normalize_user_ref("+15551234567")
    assert n.kind == "phone"
    assert n.value == "+15551234567"


def test_normalize_user_ref_user_id() -> None:
    n = normalize_user_ref("123456789")
    assert n.kind == "user_id"
    assert n.value == "123456789"


def test_normalize_user_ref_rejects_empty() -> None:
    with pytest.raises(ValueError):
        normalize_user_ref("   ")


def test_normalize_user_ref_lowercases_username() -> None:
    """Telegram usernames are case-insensitive; canonical value must match."""
    assert normalize_user_ref("@PlanFix_Bot").value == "@planfix_bot"
    assert normalize_user_ref("PlanFix_Bot").value == "@planfix_bot"
    # raw input is preserved for audit
    assert normalize_user_ref("@PlanFix_Bot").raw == "@PlanFix_Bot"


# ---------------------------------------------------------------------------
# Domain tests
# ---------------------------------------------------------------------------


async def test_bulk_add_happy_path_adds_and_promotes(
    store: OperationStore,
) -> None:
    backend = FakeMemberBackend()
    queue = _make_queue(store)
    req = BulkMemberAddRequest(
        telegram_chat_id=-100,
        items=(
            BulkMemberItem(user="@alice"),
            BulkMemberItem(user="@bob", role="admin"),
        ),
        operation_id="op-happy",
    )
    result, op = await bulk_add_members(
        backend=backend, store=store, queue=queue, request=req
    )

    assert op.status is OperationStatus.COMPLETED
    assert result.added == 2
    assert result.privacy == 0
    assert result.failed == 0
    assert backend.added == [(-100, "@alice"), (-100, "@bob")]
    assert backend.promoted == [(-100, "@bob")]
    statuses = [it.status for it in result.items]
    assert statuses == ["added", "added"]
    assert result.items[1].promoted is True
    assert result.items[0].promoted is False


async def test_bulk_add_drops_blank_entries(
    store: OperationStore,
) -> None:
    backend = FakeMemberBackend()
    queue = _make_queue(store)
    # A stray empty string and a whitespace-only entry must be dropped before
    # normalization so the rest of the batch still runs.
    req = BulkMemberAddRequest(
        telegram_chat_id=-100,
        items=(
            BulkMemberItem(user="@alice"),
            BulkMemberItem(user=""),
            BulkMemberItem(user="   "),
            BulkMemberItem(user="@bob"),
        ),
        operation_id="op-blanks",
    )
    result, op = await bulk_add_members(
        backend=backend, store=store, queue=queue, request=req
    )

    assert op.status is OperationStatus.COMPLETED
    assert result.added == 2
    assert backend.added == [(-100, "@alice"), (-100, "@bob")]
    assert [it.user for it in result.items] == ["@alice", "@bob"]


async def test_bulk_add_all_blank_entries_raises(
    store: OperationStore,
) -> None:
    backend = FakeMemberBackend()
    queue = _make_queue(store)
    req = BulkMemberAddRequest(
        telegram_chat_id=-100,
        items=(BulkMemberItem(user=""), BulkMemberItem(user="   ")),
        operation_id="op-allblank",
    )
    with pytest.raises(ValueError, match="non-blank"):
        await bulk_add_members(
            backend=backend, store=store, queue=queue, request=req
        )


async def test_bulk_add_privacy_keeps_running_when_continue_on_error_true(
    store: OperationStore,
) -> None:
    backend = FakeMemberBackend(privacy_for={"@blocked"})
    queue = _make_queue(store)
    req = BulkMemberAddRequest(
        telegram_chat_id=-100,
        items=(
            BulkMemberItem(user="@alice"),
            BulkMemberItem(user="@blocked"),
            BulkMemberItem(user="@charlie"),
        ),
        operation_id="op-privacy",
        continue_on_error=True,
    )
    result, op = await bulk_add_members(
        backend=backend, store=store, queue=queue, request=req
    )

    statuses = [it.status for it in result.items]
    assert statuses == ["added", "privacy", "added"]
    assert result.added == 2
    assert result.privacy == 1
    # Privacy is a definitive Telegram outcome — not needs_review for the
    # parent run, just a recorded per-item status with reason.
    assert op.status is OperationStatus.COMPLETED
    assert result.items[1].error and "blocked" in result.items[1].error
    assert backend.added == [(-100, "@alice"), (-100, "@charlie")]


async def test_bulk_add_already_present_treated_as_success(
    store: OperationStore,
) -> None:
    backend = FakeMemberBackend(already_present={"@dup"})
    queue = _make_queue(store)
    req = BulkMemberAddRequest(
        telegram_chat_id=-100,
        items=(
            BulkMemberItem(user="@alice"),
            BulkMemberItem(user="@dup"),
        ),
        operation_id="op-already",
    )
    result, op = await bulk_add_members(
        backend=backend, store=store, queue=queue, request=req
    )
    statuses = [it.status for it in result.items]
    assert statuses == ["added", "already_member"]
    assert result.already_member == 1
    assert op.status is OperationStatus.COMPLETED
    # The "already present" branch must NOT call promote even when role=member;
    # nothing to assert beyond promoted list staying empty.
    assert backend.promoted == []


async def test_bulk_add_admin_promotion_on_already_present(
    store: OperationStore,
) -> None:
    """If a user is already present and role=admin, we still promote them."""
    backend = FakeMemberBackend(already_present={"@dup"})
    queue = _make_queue(store)
    req = BulkMemberAddRequest(
        telegram_chat_id=-100,
        items=(BulkMemberItem(user="@dup", role="admin"),),
        operation_id="op-already-admin",
    )
    result, op = await bulk_add_members(
        backend=backend, store=store, queue=queue, request=req
    )
    assert op.status is OperationStatus.COMPLETED
    assert backend.promoted == [(-100, "@dup")]
    assert result.items[0].promoted is True
    assert result.items[0].status == "already_member"


async def test_bulk_add_continue_on_error_records_generic_failure(
    store: OperationStore,
) -> None:
    backend = FakeMemberBackend(
        fail_add={"@boom": RuntimeError("network failure")}
    )
    queue = _make_queue(store)
    req = BulkMemberAddRequest(
        telegram_chat_id=-100,
        items=(
            BulkMemberItem(user="@alice"),
            BulkMemberItem(user="@boom"),
            BulkMemberItem(user="@charlie"),
        ),
        operation_id="op-fail-continue",
        continue_on_error=True,
    )
    result, op = await bulk_add_members(
        backend=backend, store=store, queue=queue, request=req
    )
    statuses = [it.status for it in result.items]
    assert statuses == ["added", "failed", "added"]
    assert result.failed == 1
    assert op.status is OperationStatus.NEEDS_REVIEW
    assert "network failure" in (result.items[1].error or "")


async def test_bulk_add_stop_on_error_short_circuits(
    store: OperationStore,
) -> None:
    backend = FakeMemberBackend(
        fail_add={"@boom": RuntimeError("network failure")}
    )
    queue = _make_queue(store)
    req = BulkMemberAddRequest(
        telegram_chat_id=-100,
        items=(
            BulkMemberItem(user="@alice"),
            BulkMemberItem(user="@boom"),
            BulkMemberItem(user="@charlie"),
        ),
        operation_id="op-fail-stop",
        continue_on_error=False,
    )
    result, op = await bulk_add_members(
        backend=backend, store=store, queue=queue, request=req
    )
    statuses = [it.status for it in result.items]
    assert statuses == ["added", "failed", "skipped"]
    assert result.skipped == 1
    assert op.status is OperationStatus.NEEDS_REVIEW
    assert backend.added == [(-100, "@alice")]


async def test_bulk_add_replay_returns_existed(store: OperationStore) -> None:
    queue = _make_queue(store)
    req = BulkMemberAddRequest(
        telegram_chat_id=-100,
        items=(BulkMemberItem(user="@alice"),),
        operation_id="op-replay",
    )
    backend1 = FakeMemberBackend()
    first, _ = await bulk_add_members(
        backend=backend1, store=store, queue=queue, request=req
    )
    assert first.added == 1

    backend2 = FakeMemberBackend()
    second, op2 = await bulk_add_members(
        backend=backend2, store=store, queue=queue, request=req
    )
    assert backend2.added == []
    assert second.added == 0
    assert second.existed == 1
    assert op2.status is OperationStatus.COMPLETED


async def test_bulk_add_replay_after_needs_review_raises(
    store: OperationStore,
) -> None:
    backend = FakeMemberBackend(fail_add={"@boom": RuntimeError("boom")})
    queue = _make_queue(store)
    req = BulkMemberAddRequest(
        telegram_chat_id=-100,
        items=(BulkMemberItem(user="@boom"),),
        operation_id="op-nr",
    )
    _, op = await bulk_add_members(
        backend=backend, store=store, queue=queue, request=req
    )
    assert op.status is OperationStatus.NEEDS_REVIEW

    with pytest.raises(BulkMemberAddNeedsReview):
        await bulk_add_members(
            backend=FakeMemberBackend(),
            store=store,
            queue=queue,
            request=req,
        )


async def test_bulk_add_rejects_empty_items(store: OperationStore) -> None:
    queue = _make_queue(store)
    with pytest.raises(ValueError):
        await bulk_add_members(
            backend=FakeMemberBackend(),
            store=store,
            queue=queue,
            request=BulkMemberAddRequest(telegram_chat_id=-100, items=()),
        )


def test_bulk_member_item_rejects_invalid_role() -> None:
    with pytest.raises(ValueError):
        BulkMemberItem(user="@x", role="owner")


async def test_bulk_add_resume_after_retry_reset(store: OperationStore) -> None:
    """After resetting failed items, a retry finishes the rest without re-doing
    completed ones."""
    backend1 = FakeMemberBackend(fail_add={"@boom": RuntimeError("transient")})
    queue = _make_queue(store)
    req = BulkMemberAddRequest(
        telegram_chat_id=-100,
        items=(
            BulkMemberItem(user="@alice"),
            BulkMemberItem(user="@boom"),
        ),
        operation_id="op-resume",
        continue_on_error=True,
    )
    first, op1 = await bulk_add_members(
        backend=backend1, store=store, queue=queue, request=req
    )
    assert first.failed == 1
    assert op1.status is OperationStatus.NEEDS_REVIEW

    store.reset_operation_for_retry(op1.id)
    store.reset_items_for_retry(op1.id)

    backend2 = FakeMemberBackend()
    second, op2 = await bulk_add_members(
        backend=backend2, store=store, queue=queue, request=req
    )
    # Only @boom was retried; @alice stays as "existed".
    assert backend2.added == [(-100, "@boom")]
    assert [it.status for it in second.items] == ["existed", "added"]
    assert op2.status is OperationStatus.COMPLETED


# ---------------------------------------------------------------------------
# HTTP tests
# ---------------------------------------------------------------------------


def _http_client(
    minimal_config_yaml: str,
    backend: FakeMemberBackend,
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
        operation_store=store,
    )
    return TestClient(app)


def test_http_bulk_add_happy_path(minimal_config_yaml: str) -> None:
    backend = FakeMemberBackend()
    client = _http_client(minimal_config_yaml, backend)
    resp = client.post(
        "/telegram/groups/-100/members/bulk-add",
        json={
            "operation_id": "op-http",
            "items": [
                {"user": "@alice"},
                {"user": "@bob", "role": "admin"},
            ],
        },
        headers={"Authorization": "Bearer secret_token"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["added"] == 2
    assert body["operation_status"] == "completed"
    assert body["items"][1]["promoted"] is True


def test_http_bulk_add_requires_auth(minimal_config_yaml: str) -> None:
    backend = FakeMemberBackend()
    client = _http_client(minimal_config_yaml, backend)
    resp = client.post(
        "/telegram/groups/-100/members/bulk-add",
        json={"items": [{"user": "@x"}]},
    )
    assert resp.status_code == 401


def test_http_bulk_add_privacy_reports_per_item(
    minimal_config_yaml: str,
) -> None:
    backend = FakeMemberBackend(privacy_for={"@blocked"})
    client = _http_client(minimal_config_yaml, backend)
    resp = client.post(
        "/telegram/groups/-100/members/bulk-add",
        json={
            "operation_id": "op-http-privacy",
            "items": [
                {"user": "@alice"},
                {"user": "@blocked"},
            ],
        },
        headers={"Authorization": "Bearer secret_token"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["privacy"] == 1
    assert body["added"] == 1
    statuses = [it["status"] for it in body["items"]]
    assert statuses == ["added", "privacy"]


def test_http_bulk_add_rejects_invalid_role(minimal_config_yaml: str) -> None:
    backend = FakeMemberBackend()
    client = _http_client(minimal_config_yaml, backend)
    resp = client.post(
        "/telegram/groups/-100/members/bulk-add",
        json={"items": [{"user": "@x", "role": "owner"}]},
        headers={"Authorization": "Bearer secret_token"},
    )
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# CLI tests
# ---------------------------------------------------------------------------


def _write_config(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "config.yml"
    p.write_text(body)
    return p


def _patch_cli_member_backends(
    monkeypatch: pytest.MonkeyPatch,
    backend: FakeMemberBackend,
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


def test_cli_members_bulk_add_from_csv(
    minimal_config_yaml: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_file = _write_config(tmp_path, minimal_config_yaml)
    csv_path = tmp_path / "members.csv"
    csv_path.write_text(
        textwrap.dedent(
            """\
            user,role
            @alice,member
            @bob,admin
            +15551234567,member
            """
        )
    )

    backend = FakeMemberBackend()
    folder_backend = FakeFolderBackend([])
    store = OperationStore(tmp_path / "state.db")
    _patch_cli_member_backends(monkeypatch, backend, folder_backend, store)

    runner = CliRunner()
    result = runner.invoke(
        cli_main.app,
        [
            "members",
            "bulk-add",
            "--chat-id",
            "-100",
            "--file",
            str(csv_path),
            "--operation-id",
            "op-cli-csv",
            "--config",
            str(config_file),
        ],
    )
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload["added"] == 3
    statuses = [it["status"] for it in payload["items"]]
    assert statuses == ["added", "added", "added"]
    assert backend.promoted == [(-100, "@bob")]


def test_cli_members_bulk_add_from_user_and_admin_flags(
    minimal_config_yaml: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_file = _write_config(tmp_path, minimal_config_yaml)
    backend = FakeMemberBackend()
    folder_backend = FakeFolderBackend([])
    store = OperationStore(tmp_path / "state.db")
    _patch_cli_member_backends(monkeypatch, backend, folder_backend, store)

    runner = CliRunner()
    result = runner.invoke(
        cli_main.app,
        [
            "members",
            "bulk-add",
            "--chat-id",
            "-100",
            "--user",
            "@alice",
            "--admin",
            "@bob",
            "--operation-id",
            "op-cli-flags",
            "--config",
            str(config_file),
        ],
    )
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload["added"] == 2
    assert backend.promoted == [(-100, "@bob")]


def test_cli_members_bulk_add_requires_chat_id_or_name(
    minimal_config_yaml: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_file = _write_config(tmp_path, minimal_config_yaml)
    backend = FakeMemberBackend()
    folder_backend = FakeFolderBackend([])
    store = OperationStore(tmp_path / "state.db")
    _patch_cli_member_backends(monkeypatch, backend, folder_backend, store)

    runner = CliRunner()
    result = runner.invoke(
        cli_main.app,
        [
            "members",
            "bulk-add",
            "--user",
            "@alice",
            "--config",
            str(config_file),
        ],
    )
    assert result.exit_code == 2


def test_cli_members_bulk_add_requires_input(
    minimal_config_yaml: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_file = _write_config(tmp_path, minimal_config_yaml)
    backend = FakeMemberBackend()
    folder_backend = FakeFolderBackend([])
    store = OperationStore(tmp_path / "state.db")
    _patch_cli_member_backends(monkeypatch, backend, folder_backend, store)

    runner = CliRunner()
    result = runner.invoke(
        cli_main.app,
        [
            "members",
            "bulk-add",
            "--chat-id",
            "-100",
            "--config",
            str(config_file),
        ],
    )
    assert result.exit_code == 2
