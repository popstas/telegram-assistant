"""Dry-run tests for members/messages CLI commands (plan Task 3).

Each test verifies:

* the shared ``--dry-run`` envelope contract;
* no mutating backend method is invoked;
* no operation row is created in the persistence store.
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from telegram_assistant.cli import main as cli_main
from telegram_assistant.folders import (
    FolderChat,
    FolderSnapshot,
)
from telegram_assistant.persistence import OperationStore
from telegram_assistant.topics import TopicSummary

from .test_dry_run_contract import assert_dry_run_envelope


def _operation_count(store: OperationStore) -> int:
    """Direct count of rows in the operations table — dry-run must not insert."""
    with store._connect() as conn:  # noqa: SLF001 (test introspection)
        cursor = conn.execute("SELECT COUNT(*) FROM operations")
        return int(cursor.fetchone()[0])


# ---------------------------------------------------------------------------
# Recording fakes.
# ---------------------------------------------------------------------------


class RecordingMemberBackend:
    def __init__(self) -> None:
        self.added: list[tuple[int, str]] = []
        self.promoted: list[tuple[int, str]] = []

    async def add_member(self, *, chat_id: int, user: str) -> None:  # pragma: no cover
        raise AssertionError("dry-run must not add members")

    async def promote_admin(self, *, chat_id: int, user: str) -> None:  # pragma: no cover
        raise AssertionError("dry-run must not promote admins")


class RecordingMessageBackend:
    """Doubles as both message and topic backend (production reuses the same client)."""

    def __init__(self, *, topics_by_chat: dict[int, list[TopicSummary]] | None = None) -> None:
        self._topics_by_chat = topics_by_chat or {}
        self.sent: list[tuple[int, str, int | None]] = []
        self.list_topics_calls: list[int] = []

    async def send_message(
        self, *, chat_id: int, text: str, topic_id: int | None = None
    ) -> int:  # pragma: no cover
        raise AssertionError("dry-run must not send messages")

    async def list_topics(self, *, chat_id: int) -> list[TopicSummary]:
        self.list_topics_calls.append(chat_id)
        return list(self._topics_by_chat.get(chat_id, []))


class RecordingFolderBackend:
    def __init__(self, folders: list[FolderSnapshot] | None = None) -> None:
        self._folders = folders if folders is not None else [
            FolderSnapshot(folder_id=2, folder_name="Planfix clients", chats=[])
        ]

    async def list_folders(self) -> list[FolderSnapshot]:
        return [
            FolderSnapshot(
                folder_id=f.folder_id,
                folder_name=f.folder_name,
                chats=list(f.chats),
            )
            for f in self._folders
        ]

    async def resolve_chat(self, chat_ref: str | int) -> FolderChat:  # pragma: no cover
        raise NotImplementedError

    async def add_chat_to_folder(
        self, folder_id: int, chat_id: int
    ) -> None:  # pragma: no cover
        raise AssertionError("dry-run must not change folders")


def _write_config(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "config.yml"
    p.write_text(body)
    return p


def _write_member_csv(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "members.csv"
    p.write_text(textwrap.dedent(body))
    return p


def _patch_member_backends(
    monkeypatch: pytest.MonkeyPatch,
    backend: RecordingMemberBackend,
    folder_backend: RecordingFolderBackend,
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


def _patch_message_backends(
    monkeypatch: pytest.MonkeyPatch,
    backend: RecordingMessageBackend,
    folder_backend: RecordingFolderBackend,
    store: OperationStore,
) -> None:
    class _FakeManager:
        async def disconnect(self) -> None:
            return None

    def _factory(config_path: Path | None) -> Any:
        from telegram_assistant.config import load_config

        config = load_config(config_path)

        async def _open() -> Any:
            # Production returns (message_backend, topic_backend, folder_backend);
            # the fake doubles as both message and topic backend.
            return backend, backend, folder_backend

        return config, _FakeManager(), store, _open

    monkeypatch.setattr(cli_main, "_build_message_backends", _factory)


# ---------------------------------------------------------------------------
# members bulk-add
# ---------------------------------------------------------------------------


def test_cli_members_bulk_add_dry_run_envelope(
    minimal_config_yaml: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_file = _write_config(tmp_path, minimal_config_yaml)
    backend = RecordingMemberBackend()
    folder_backend = RecordingFolderBackend()
    store = OperationStore(tmp_path / "state.db")
    _patch_member_backends(monkeypatch, backend, folder_backend, store)

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
            "--dry-run",
            "--config",
            str(config_file),
        ],
    )
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert_dry_run_envelope(
        payload,
        command="members.bulk_add",
        resolved_keys=(
            "telegram_chat_id",
            "items",
            "items_count",
            "continue_on_error",
        ),
    )
    assert payload["resolved"]["telegram_chat_id"] == -100
    items = payload["resolved"]["items"]
    assert len(items) == 2
    actions = {it["normalized_user"]: it["action"] for it in items}
    assert actions["@alice"] == "would_add"
    assert actions["@bob"] == "would_add_and_promote"
    # No mutation occurred.
    assert backend.added == []
    assert backend.promoted == []
    # No operation rows persisted.
    assert _operation_count(store) == 0


def test_cli_members_bulk_add_dry_run_warns_about_protected_account(
    minimal_config_yaml: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_file = _write_config(tmp_path, minimal_config_yaml)
    backend = RecordingMemberBackend()
    folder_backend = RecordingFolderBackend()
    store = OperationStore(tmp_path / "state.db")
    _patch_member_backends(monkeypatch, backend, folder_backend, store)

    runner = CliRunner()
    result = runner.invoke(
        cli_main.app,
        [
            "members",
            "bulk-add",
            "--chat-id",
            "-100",
            "--user",
            "@planfix_bot",
            "--dry-run",
            "--config",
            str(config_file),
        ],
    )
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    items = payload["resolved"]["items"]
    assert any(it["protected"] for it in items)
    assert any("protected" in w for w in payload["warnings"])


def test_cli_members_bulk_add_dry_run_flags_duplicates_in_file(
    minimal_config_yaml: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_file = _write_config(tmp_path, minimal_config_yaml)
    csv_file = _write_member_csv(
        tmp_path,
        """\
        user,role
        @alice,member
        @alice,admin
        """,
    )
    backend = RecordingMemberBackend()
    folder_backend = RecordingFolderBackend()
    store = OperationStore(tmp_path / "state.db")
    _patch_member_backends(monkeypatch, backend, folder_backend, store)

    runner = CliRunner()
    result = runner.invoke(
        cli_main.app,
        [
            "members",
            "bulk-add",
            "--chat-id",
            "-100",
            "--file",
            str(csv_file),
            "--dry-run",
            "--config",
            str(config_file),
        ],
    )
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    items = payload["resolved"]["items"]
    assert items[0]["duplicate_in_file"] is False
    assert items[1]["duplicate_in_file"] is True
    assert any("duplicate user" in w for w in payload["warnings"])


def test_cli_members_bulk_add_dry_run_resolves_chat_name(
    minimal_config_yaml: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_file = _write_config(tmp_path, minimal_config_yaml)
    backend = RecordingMemberBackend()
    folder_backend = RecordingFolderBackend(
        folders=[
            FolderSnapshot(
                folder_id=2,
                folder_name="Planfix clients",
                chats=[FolderChat(chat_id=-500, title="Acme")],
            )
        ]
    )
    store = OperationStore(tmp_path / "state.db")
    _patch_member_backends(monkeypatch, backend, folder_backend, store)

    runner = CliRunner()
    result = runner.invoke(
        cli_main.app,
        [
            "members",
            "bulk-add",
            "--chat-name",
            "Acme",
            "--user",
            "@alice",
            "--dry-run",
            "--config",
            str(config_file),
        ],
    )
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload["resolved"]["telegram_chat_id"] == -500
    assert payload["resolved"]["chat_name"] == "Acme"
    assert payload["resolved"]["folder_name"] == "Planfix clients"


def test_cli_members_bulk_add_dry_run_invalid_role_errors(
    minimal_config_yaml: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_file = _write_config(tmp_path, minimal_config_yaml)
    csv_file = _write_member_csv(
        tmp_path,
        """\
        user,role
        @alice,king
        """,
    )
    backend = RecordingMemberBackend()
    folder_backend = RecordingFolderBackend()
    store = OperationStore(tmp_path / "state.db")
    _patch_member_backends(monkeypatch, backend, folder_backend, store)

    runner = CliRunner()
    result = runner.invoke(
        cli_main.app,
        [
            "members",
            "bulk-add",
            "--chat-id",
            "-100",
            "--file",
            str(csv_file),
            "--dry-run",
            "--config",
            str(config_file),
        ],
    )
    # Live run also exits with 2 for invalid roles — keep the same exit code.
    assert result.exit_code == 2, result.stdout
    assert _operation_count(store) == 0


# ---------------------------------------------------------------------------
# members bulk-remove (already implemented; keep test for chat_name resolved fields)
# ---------------------------------------------------------------------------


def test_cli_members_bulk_remove_dry_run_resolves_chat_name(
    minimal_config_yaml: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_file = _write_config(tmp_path, minimal_config_yaml)

    class _RemoveBackend:
        async def ban_member(self, *, chat_id: int, user: str) -> None:  # pragma: no cover
            raise AssertionError("dry-run must not call backend")

        async def unban_member(self, *, chat_id: int, user: str) -> None:  # pragma: no cover
            raise AssertionError("dry-run must not call backend")

    backend = _RemoveBackend()
    folder_backend = RecordingFolderBackend(
        folders=[
            FolderSnapshot(
                folder_id=2,
                folder_name="Planfix clients",
                chats=[FolderChat(chat_id=-500, title="Acme")],
            )
        ]
    )
    store = OperationStore(tmp_path / "state.db")

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

    runner = CliRunner()
    result = runner.invoke(
        cli_main.app,
        [
            "members",
            "bulk-remove",
            "--chat-name",
            "Acme",
            "--user",
            "@alice",
            "--dry-run",
            "--config",
            str(config_file),
        ],
    )
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload["resolved"]["telegram_chat_id"] == -500
    assert payload["resolved"]["chat_name"] == "Acme"
    assert payload["resolved"]["folder_name"] == "Planfix clients"


def test_cli_members_bulk_remove_dry_run_previews_protected_without_force(
    minimal_config_yaml: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Dry-run with a protected account and no --force must preview (not exit 2)."""
    config_file = _write_config(tmp_path, minimal_config_yaml)

    class _RemoveBackend:
        async def ban_member(self, *, chat_id: int, user: str) -> None:  # pragma: no cover
            raise AssertionError("dry-run must not call backend")

        async def unban_member(self, *, chat_id: int, user: str) -> None:  # pragma: no cover
            raise AssertionError("dry-run must not call backend")

    backend = _RemoveBackend()
    folder_backend = RecordingFolderBackend()
    store = OperationStore(tmp_path / "state.db")

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
            "--config",
            str(config_file),
        ],
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    items = payload["resolved"]["items"]
    assert any(it["protected"] for it in items)
    assert any("protected" in w for w in payload["warnings"])
    assert _operation_count(store) == 0


def test_cli_members_bulk_remove_dry_run_chat_id_skips_open_backends(
    minimal_config_yaml: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With --chat-id, dry-run must NOT open the Telethon backend at all —
    no folder lookup is needed, so the advertised offline preview must work
    even if Telegram is unreachable or the session is unauthorized.
    """
    config_file = _write_config(tmp_path, minimal_config_yaml)
    store = OperationStore(tmp_path / "state.db")

    class _FakeManager:
        async def disconnect(self) -> None:
            return None

    def _factory(config_path: Path | None) -> Any:
        from telegram_assistant.config import load_config

        config = load_config(config_path)

        async def _open() -> Any:  # pragma: no cover
            raise AssertionError(
                "dry-run with --chat-id must not open Telethon backends"
            )

        return config, _FakeManager(), store, _open

    monkeypatch.setattr(cli_main, "_build_member_backends", _factory)

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
    assert result.exit_code == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload["resolved"]["telegram_chat_id"] == -100
    assert _operation_count(store) == 0


def test_cli_members_bulk_remove_dry_run_rejects_invalid_mode(
    minimal_config_yaml: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Dry-run must reject invalid --mode the same way the real run would,
    so dry-run is a faithful preflight (not a permissive preview).
    """
    config_file = _write_config(tmp_path, minimal_config_yaml)
    store = OperationStore(tmp_path / "state.db")

    class _FakeManager:
        async def disconnect(self) -> None:  # pragma: no cover
            return None

    def _factory(config_path: Path | None) -> Any:
        from telegram_assistant.config import load_config

        config = load_config(config_path)

        async def _open() -> Any:  # pragma: no cover
            raise AssertionError("invalid mode must fail before any backend call")

        return config, _FakeManager(), store, _open

    monkeypatch.setattr(cli_main, "_build_member_backends", _factory)

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
            "--mode",
            "purge",
            "--dry-run",
            "--config",
            str(config_file),
        ],
    )
    assert result.exit_code == 2
    assert "invalid mode" in (result.stderr or result.stdout)


def test_cli_members_bulk_remove_protected_case_insensitive(
    minimal_config_yaml: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Protected-account guard must catch case variants — Telegram usernames
    are case-insensitive, so @PlanFix_Bot must be guarded just like @planfix_bot.
    """
    config_file = _write_config(tmp_path, minimal_config_yaml)
    store = OperationStore(tmp_path / "state.db")

    class _FakeManager:
        async def disconnect(self) -> None:  # pragma: no cover
            return None

    def _factory(config_path: Path | None) -> Any:
        from telegram_assistant.config import load_config

        config = load_config(config_path)

        async def _open() -> Any:  # pragma: no cover
            raise AssertionError("guard must reject before opening backends")

        return config, _FakeManager(), store, _open

    monkeypatch.setattr(cli_main, "_build_member_backends", _factory)

    runner = CliRunner()
    result = runner.invoke(
        cli_main.app,
        [
            "members",
            "bulk-remove",
            "--chat-id",
            "-100",
            "--user",
            "@PlanFix_Bot",
            "--yes",
            "--config",
            str(config_file),
        ],
    )
    assert result.exit_code == 2
    assert "protected" in (result.stderr or result.stdout)


# ---------------------------------------------------------------------------
# messages send (targeted)
# ---------------------------------------------------------------------------


def test_cli_messages_send_dry_run_targeted_envelope(
    minimal_config_yaml: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_file = _write_config(tmp_path, minimal_config_yaml)
    backend = RecordingMessageBackend()
    folder_backend = RecordingFolderBackend()
    store = OperationStore(tmp_path / "state.db")
    _patch_message_backends(monkeypatch, backend, folder_backend, store)

    runner = CliRunner()
    result = runner.invoke(
        cli_main.app,
        [
            "messages",
            "send",
            "--chat-id",
            "-100",
            "--text",
            "hello world",
            "--dry-run",
            "--config",
            str(config_file),
        ],
    )
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert_dry_run_envelope(
        payload,
        command="messages.send",
        resolved_keys=(
            "mode",
            "telegram_chat_id",
            "text",
            "is_service_command",
        ),
    )
    assert payload["resolved"]["mode"] == "targeted"
    assert payload["resolved"]["telegram_chat_id"] == -100
    assert payload["resolved"]["telegram_topic_id"] is None
    assert payload["resolved"]["is_service_command"] is False
    assert payload["resolved"]["text"] == "hello world"
    assert backend.sent == []
    assert _operation_count(store) == 0


def test_cli_messages_send_dry_run_redacts_service_command(
    minimal_config_yaml: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_file = _write_config(tmp_path, minimal_config_yaml)
    backend = RecordingMessageBackend()
    folder_backend = RecordingFolderBackend()
    store = OperationStore(tmp_path / "state.db")
    _patch_message_backends(monkeypatch, backend, folder_backend, store)

    runner = CliRunner()
    result = runner.invoke(
        cli_main.app,
        [
            "messages",
            "send",
            "--chat-id",
            "-100",
            "--text",
            "/task 12345",
            "--dry-run",
            "--config",
            str(config_file),
        ],
    )
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload["resolved"]["is_service_command"] is True
    # Trailing argument is redacted, command itself is preserved.
    assert payload["resolved"]["text"] == "/task [redacted]"


def test_cli_messages_send_dry_run_resolves_topic_by_name(
    minimal_config_yaml: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_file = _write_config(tmp_path, minimal_config_yaml)
    backend = RecordingMessageBackend(
        topics_by_chat={-100: [TopicSummary(topic_id=42, title="Documents")]}
    )
    folder_backend = RecordingFolderBackend()
    store = OperationStore(tmp_path / "state.db")
    _patch_message_backends(monkeypatch, backend, folder_backend, store)

    runner = CliRunner()
    result = runner.invoke(
        cli_main.app,
        [
            "messages",
            "send",
            "--chat-id",
            "-100",
            "--topic-name",
            "Documents",
            "--text",
            "hello",
            "--dry-run",
            "--config",
            str(config_file),
        ],
    )
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload["resolved"]["telegram_topic_id"] == 42
    assert payload["resolved"]["topic_name"] == "Documents"


def test_cli_messages_send_dry_run_missing_topic_errors(
    minimal_config_yaml: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_file = _write_config(tmp_path, minimal_config_yaml)
    backend = RecordingMessageBackend(topics_by_chat={-100: []})
    folder_backend = RecordingFolderBackend()
    store = OperationStore(tmp_path / "state.db")
    _patch_message_backends(monkeypatch, backend, folder_backend, store)

    runner = CliRunner()
    result = runner.invoke(
        cli_main.app,
        [
            "messages",
            "send",
            "--chat-id",
            "-100",
            "--topic-name",
            "Nope",
            "--text",
            "hello",
            "--dry-run",
            "--config",
            str(config_file),
        ],
    )
    assert result.exit_code == 2, result.stdout
    assert _operation_count(store) == 0


# ---------------------------------------------------------------------------
# messages send (mass)
# ---------------------------------------------------------------------------


def test_cli_messages_send_dry_run_mass_envelope(
    minimal_config_yaml: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_file = _write_config(tmp_path, minimal_config_yaml)
    backend = RecordingMessageBackend(
        topics_by_chat={
            -500: [TopicSummary(topic_id=42, title="Documents")],
            -501: [TopicSummary(topic_id=43, title="Other")],
        }
    )
    folder_backend = RecordingFolderBackend(
        folders=[
            FolderSnapshot(
                folder_id=2,
                folder_name="Planfix clients",
                chats=[
                    FolderChat(chat_id=-500, title="Client A"),
                    FolderChat(chat_id=-501, title="Client B"),
                ],
            )
        ]
    )
    store = OperationStore(tmp_path / "state.db")
    _patch_message_backends(monkeypatch, backend, folder_backend, store)

    runner = CliRunner()
    result = runner.invoke(
        cli_main.app,
        [
            "messages",
            "send",
            "--topic-name",
            "Documents",
            "--text",
            "broadcast",
            "--dry-run",
            "--config",
            str(config_file),
        ],
    )
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert_dry_run_envelope(
        payload,
        command="messages.send",
        resolved_keys=(
            "mode",
            "folder_name",
            "topic_name",
            "items",
            "items_count",
            "send_count",
            "skip_count",
        ),
    )
    assert payload["resolved"]["mode"] == "mass"
    assert payload["resolved"]["items_count"] == 2
    assert payload["resolved"]["send_count"] == 1
    assert payload["resolved"]["skip_count"] == 1
    items = payload["resolved"]["items"]
    by_chat = {it["telegram_chat_id"]: it for it in items}
    assert by_chat[-500]["action"] == "would_send"
    assert by_chat[-500]["telegram_topic_id"] == 42
    assert by_chat[-501]["action"] == "would_skip"
    assert by_chat[-501]["reason"] == "topic_not_found"
    assert backend.sent == []
    assert _operation_count(store) == 0


def test_cli_messages_send_dry_run_mass_empty_text_errors(
    minimal_config_yaml: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_file = _write_config(tmp_path, minimal_config_yaml)
    backend = RecordingMessageBackend()
    folder_backend = RecordingFolderBackend()
    store = OperationStore(tmp_path / "state.db")
    _patch_message_backends(monkeypatch, backend, folder_backend, store)

    runner = CliRunner()
    result = runner.invoke(
        cli_main.app,
        [
            "messages",
            "send",
            "--topic-name",
            "Documents",
            "--text",
            "   ",
            "--dry-run",
            "--config",
            str(config_file),
        ],
    )
    assert result.exit_code == 2, result.stdout
    assert _operation_count(store) == 0
