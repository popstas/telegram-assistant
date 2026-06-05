"""Dry-run tests for groups/topics CLI commands (plan Task 2).

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
# Fakes that record mutations so we can assert "no side effects".
# ---------------------------------------------------------------------------


class RecordingGroupBackend:
    def __init__(self) -> None:
        self.created: list[Any] = []
        self.added: list[str] = []
        self.promoted: list[str] = []
        self.invite_calls = 0
        self.messages: list[tuple[int, str]] = []

    async def create_supergroup(
        self, *, title: str, about: str | None, enable_topics: bool
    ) -> int:  # pragma: no cover - must not be called
        raise AssertionError("dry-run must not create supergroup")

    async def add_member(self, *, chat_id: int, user: str) -> None:  # pragma: no cover
        raise AssertionError("dry-run must not add members")

    async def promote_admin(self, *, chat_id: int, user: str) -> None:  # pragma: no cover
        raise AssertionError("dry-run must not promote admins")

    async def create_invite_link(self, *, chat_id: int) -> str:  # pragma: no cover
        raise AssertionError("dry-run must not create invite link")

    async def send_message(self, *, chat_id: int, text: str) -> int:  # pragma: no cover
        raise AssertionError("dry-run must not send messages")


class RecordingFolderBackend:
    def __init__(self, folders: list[FolderSnapshot] | None = None) -> None:
        self._folders = folders if folders is not None else [
            FolderSnapshot(folder_id=2, folder_name="Planfix clients", chats=[])
        ]
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

    async def resolve_chat(self, chat_ref: str | int) -> FolderChat:  # pragma: no cover
        raise NotImplementedError

    async def add_chat_to_folder(
        self, folder_id: int, chat_id: int
    ) -> None:  # pragma: no cover
        raise AssertionError("dry-run must not change folders")


class RecordingTopicBackend:
    def __init__(self, *, topics: list[TopicSummary] | None = None) -> None:
        self._topics = list(topics or [])
        self.created: list[tuple[int, str]] = []
        self.messages: list[tuple[int, str, int | None]] = []
        self.closed: list[tuple[int, int]] = []

    async def create_topic(self, *, chat_id: int, name: str) -> int:  # pragma: no cover
        raise AssertionError("dry-run must not create topic")

    async def send_message(
        self, *, chat_id: int, text: str, topic_id: int | None = None
    ) -> int:  # pragma: no cover
        raise AssertionError("dry-run must not send messages")

    async def close_topic(self, *, chat_id: int, topic_id: int) -> None:  # pragma: no cover
        raise AssertionError("dry-run must not close topic")

    async def list_topics(self, *, chat_id: int) -> list[TopicSummary]:
        return list(self._topics)


def _write_config(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "config.yml"
    p.write_text(body)
    return p


def _patch_group_backends(
    monkeypatch: pytest.MonkeyPatch,
    backend: RecordingGroupBackend,
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

    monkeypatch.setattr(cli_main, "_build_group_backends", _factory)


def _patch_topic_backends(
    monkeypatch: pytest.MonkeyPatch,
    backend: RecordingTopicBackend,
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

    monkeypatch.setattr(cli_main, "_build_topic_backends", _factory)


# ---------------------------------------------------------------------------
# groups create
# ---------------------------------------------------------------------------


def test_cli_groups_create_dry_run_envelope(
    minimal_config_yaml: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_file = _write_config(tmp_path, minimal_config_yaml)
    backend = RecordingGroupBackend()
    folder_backend = RecordingFolderBackend()
    store = OperationStore(tmp_path / "state.db")
    _patch_group_backends(monkeypatch, backend, folder_backend, store)

    runner = CliRunner()
    result = runner.invoke(
        cli_main.app,
        [
            "groups",
            "create",
            "--title",
            "Acme",
            "--planfix-task-id",
            "42",
            "--admin",
            "@alice",
            "--member",
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
        command="groups.create",
        resolved_keys=(
            "title",
            "external_ref",
            "admins",
            "members",
            "reserve_admins",
            "reserve_members",
            "planned_members",
            "planned_admins",
            "folder",
            "enable_topics",
            "create_invite_link",
        ),
    )
    # No supergroup or folder mutation happened.
    assert backend.created == []
    assert folder_backend.added == []
    # No operation rows persisted.
    assert _operation_count(store) == 0
    # @planfix_bot is in the configured reserves and so triggers a /task line.
    assert "@planfix_bot" in payload["resolved"]["planned_members"]
    assert payload["resolved"]["folder"] is not None
    assert payload["resolved"]["folder"]["folder_name"] == "Planfix clients"
    # /task message is planned because @planfix_bot is in the planned member list.
    assert any("/task 42" in action for action in payload["planned_actions"])


def test_cli_groups_create_dry_run_missing_folder_warns(
    minimal_config_yaml: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_file = _write_config(tmp_path, minimal_config_yaml)
    backend = RecordingGroupBackend()
    folder_backend = RecordingFolderBackend(folders=[])
    store = OperationStore(tmp_path / "state.db")
    _patch_group_backends(monkeypatch, backend, folder_backend, store)

    runner = CliRunner()
    result = runner.invoke(
        cli_main.app,
        [
            "groups",
            "create",
            "--title",
            "Acme",
            "--planfix-task-id",
            "42",
            "--dry-run",
            "--config",
            str(config_file),
        ],
    )
    # Missing folder is the same error as the live run: exit 2.
    assert result.exit_code == 2, result.stdout
    assert _operation_count(store) == 0


def test_cli_groups_create_dry_run_skip_folder_emits_warning(
    minimal_config_yaml: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_file = _write_config(tmp_path, minimal_config_yaml)
    backend = RecordingGroupBackend()
    folder_backend = RecordingFolderBackend()
    store = OperationStore(tmp_path / "state.db")
    _patch_group_backends(monkeypatch, backend, folder_backend, store)

    runner = CliRunner()
    result = runner.invoke(
        cli_main.app,
        [
            "groups",
            "create",
            "--title",
            "Acme",
            "--planfix-task-id",
            "42",
            "--skip-folder",
            "--dry-run",
            "--config",
            str(config_file),
        ],
    )
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload["resolved"]["folder"] is None
    assert any("skip-folder" in w for w in payload["warnings"])


# ---------------------------------------------------------------------------
# topics create
# ---------------------------------------------------------------------------


def test_cli_topics_create_dry_run_with_chat_id(
    minimal_config_yaml: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_file = _write_config(tmp_path, minimal_config_yaml)
    backend = RecordingTopicBackend()
    folder_backend = RecordingFolderBackend()
    store = OperationStore(tmp_path / "state.db")
    _patch_topic_backends(monkeypatch, backend, folder_backend, store)

    runner = CliRunner()
    result = runner.invoke(
        cli_main.app,
        [
            "topics",
            "create",
            "--topic-name",
            "Demo",
            "--chat-id",
            "-100",
            "--planfix-task-id",
            "55",
            "--dry-run",
            "--config",
            str(config_file),
        ],
    )
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert_dry_run_envelope(
        payload,
        command="topics.create",
        resolved_keys=(
            "telegram_chat_id",
            "topic_name",
            "external_ref",
            "first_message_kind",
            "first_message_text",
            "existing_topic_ids",
        ),
    )
    assert payload["resolved"]["telegram_chat_id"] == -100
    assert payload["resolved"]["first_message_kind"] == "task"
    assert payload["resolved"]["first_message_text"] == "/task 55"
    assert payload["resolved"]["existing_topic_ids"] == []
    assert backend.created == []
    assert _operation_count(store) == 0


def test_cli_topics_create_dry_run_warns_about_existing_topic_name(
    minimal_config_yaml: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_file = _write_config(tmp_path, minimal_config_yaml)
    backend = RecordingTopicBackend(
        topics=[TopicSummary(topic_id=909, title="Demo")]
    )
    folder_backend = RecordingFolderBackend()
    store = OperationStore(tmp_path / "state.db")
    _patch_topic_backends(monkeypatch, backend, folder_backend, store)

    runner = CliRunner()
    result = runner.invoke(
        cli_main.app,
        [
            "topics",
            "create",
            "--topic-name",
            "Demo",
            "--chat-id",
            "-100",
            "--dry-run",
            "--config",
            str(config_file),
        ],
    )
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert 909 in payload["resolved"]["existing_topic_ids"]
    assert any("already exists" in w for w in payload["warnings"])


def test_cli_topics_create_dry_run_resolves_chat_name(
    minimal_config_yaml: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_file = _write_config(tmp_path, minimal_config_yaml)
    backend = RecordingTopicBackend()
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
    _patch_topic_backends(monkeypatch, backend, folder_backend, store)

    runner = CliRunner()
    result = runner.invoke(
        cli_main.app,
        [
            "topics",
            "create",
            "--topic-name",
            "Demo",
            "--chat-name",
            "Acme",
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


# ---------------------------------------------------------------------------
# topics bulk-create
# ---------------------------------------------------------------------------


def _write_bulk_csv(tmp_path: Path, body: str) -> Path:
    f = tmp_path / "topics.csv"
    f.write_text(textwrap.dedent(body))
    return f


def test_cli_topics_bulk_create_dry_run_envelope(
    minimal_config_yaml: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_file = _write_config(tmp_path, minimal_config_yaml)
    csv_file = _write_bulk_csv(
        tmp_path,
        """\
        planfix_task_id,topic_name,message
        100,Documents,
        101,Payment,Pay here
        """,
    )
    backend = RecordingTopicBackend()
    folder_backend = RecordingFolderBackend()
    store = OperationStore(tmp_path / "state.db")
    _patch_topic_backends(monkeypatch, backend, folder_backend, store)

    runner = CliRunner()
    result = runner.invoke(
        cli_main.app,
        [
            "topics",
            "bulk-create",
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
    assert_dry_run_envelope(
        payload,
        command="topics.bulk_create",
        resolved_keys=(
            "telegram_chat_id",
            "items",
            "items_count",
            "file",
            "continue_on_error",
        ),
    )
    assert payload["resolved"]["telegram_chat_id"] == -100
    assert payload["resolved"]["items_count"] == 2
    assert backend.created == []
    assert _operation_count(store) == 0


def test_cli_topics_bulk_create_dry_run_flags_duplicates(
    minimal_config_yaml: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_file = _write_config(tmp_path, minimal_config_yaml)
    csv_file = _write_bulk_csv(
        tmp_path,
        """\
        planfix_task_id,topic_name,message
        100,Documents,
        100,Other,
        200,Documents,
        """,
    )
    backend = RecordingTopicBackend(
        topics=[TopicSummary(topic_id=42, title="Documents")]
    )
    folder_backend = RecordingFolderBackend()
    store = OperationStore(tmp_path / "state.db")
    _patch_topic_backends(monkeypatch, backend, folder_backend, store)

    runner = CliRunner()
    result = runner.invoke(
        cli_main.app,
        [
            "topics",
            "bulk-create",
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
    # row 2 duplicates external_ref
    assert items[1]["duplicate_external_ref_in_file"] is True
    # row 3 duplicates topic_name
    assert items[2]["duplicate_topic_name_in_file"] is True
    # row 1 and row 3 reference an existing topic name on Telegram
    assert items[0]["existing_topic_ids"] == [42]
    assert items[2]["existing_topic_ids"] == [42]
    assert any("duplicate external_ref" in w for w in payload["warnings"])
    assert any("duplicate topic_name" in w for w in payload["warnings"])
    assert any("already exists" in w for w in payload["warnings"])


# ---------------------------------------------------------------------------
# topics close
# ---------------------------------------------------------------------------


def test_cli_topics_close_dry_run_with_topic_id(
    minimal_config_yaml: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_file = _write_config(tmp_path, minimal_config_yaml)
    backend = RecordingTopicBackend(
        topics=[TopicSummary(topic_id=42, title="Demo", closed=False)]
    )
    folder_backend = RecordingFolderBackend()
    store = OperationStore(tmp_path / "state.db")
    _patch_topic_backends(monkeypatch, backend, folder_backend, store)

    runner = CliRunner()
    result = runner.invoke(
        cli_main.app,
        [
            "topics",
            "close",
            "--chat-id",
            "-100",
            "--topic-id",
            "42",
            "--dry-run",
            "--config",
            str(config_file),
        ],
    )
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert_dry_run_envelope(
        payload,
        command="topics.close",
        resolved_keys=(
            "telegram_chat_id",
            "telegram_topic_id",
            "topic_name",
            "already_closed",
        ),
    )
    assert payload["resolved"]["telegram_topic_id"] == 42
    assert payload["resolved"]["already_closed"] is False
    assert backend.closed == []
    assert _operation_count(store) == 0


def test_cli_topics_close_dry_run_warns_when_already_closed(
    minimal_config_yaml: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_file = _write_config(tmp_path, minimal_config_yaml)
    backend = RecordingTopicBackend(
        topics=[TopicSummary(topic_id=42, title="Demo", closed=True)]
    )
    folder_backend = RecordingFolderBackend()
    store = OperationStore(tmp_path / "state.db")
    _patch_topic_backends(monkeypatch, backend, folder_backend, store)

    runner = CliRunner()
    result = runner.invoke(
        cli_main.app,
        [
            "topics",
            "close",
            "--chat-id",
            "-100",
            "--topic-name",
            "Demo",
            "--dry-run",
            "--config",
            str(config_file),
        ],
    )
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload["resolved"]["already_closed"] is True
    assert any("already closed" in w for w in payload["warnings"])


def test_cli_topics_close_dry_run_unknown_topic_id_errors(
    minimal_config_yaml: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_file = _write_config(tmp_path, minimal_config_yaml)
    backend = RecordingTopicBackend(topics=[])
    folder_backend = RecordingFolderBackend()
    store = OperationStore(tmp_path / "state.db")
    _patch_topic_backends(monkeypatch, backend, folder_backend, store)

    runner = CliRunner()
    result = runner.invoke(
        cli_main.app,
        [
            "topics",
            "close",
            "--chat-id",
            "-100",
            "--topic-id",
            "999",
            "--dry-run",
            "--config",
            str(config_file),
        ],
    )
    assert result.exit_code == 2, result.stdout
    assert _operation_count(store) == 0
