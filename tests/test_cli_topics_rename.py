"""CLI tests for ``topics rename`` (plan Task 5).

The CLI builds its backends via ``_build_topic_backends``; we monkeypatch that
factory to inject fakes so no real Telegram traffic happens.
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from telegram_assistant.cli import main as cli_main
from telegram_assistant.persistence import OperationStore
from telegram_assistant.topics import TopicSummary


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


class FakeTopicBackend:
    """In-memory TopicBackend recording rename/list calls."""

    def __init__(self, *, topics: list[TopicSummary] | None = None) -> None:
        self._topics = list(topics or [])
        self.renamed: list[tuple[int, int, str]] = []

    async def create_topic(self, *, chat_id: int, name: str) -> int:  # pragma: no cover
        raise NotImplementedError

    async def send_message(  # pragma: no cover
        self, *, chat_id: int, text: str, topic_id: int | None = None
    ) -> int:
        raise NotImplementedError

    async def close_topic(self, *, chat_id: int, topic_id: int) -> None:  # pragma: no cover
        raise AssertionError("rename must not close topics")

    async def rename_topic(self, *, chat_id: int, topic_id: int, title: str) -> None:
        self.renamed.append((chat_id, topic_id, title))
        for idx, t in enumerate(self._topics):
            if t.topic_id == topic_id:
                self._topics[idx] = TopicSummary(
                    topic_id=t.topic_id, title=title, closed=t.closed
                )
                break

    async def list_topics(self, *, chat_id: int) -> list[TopicSummary]:
        return list(self._topics)


class FakeFolderBackend:
    """No-op folder backend; not used when addressing by --chat-id."""

    async def list_folders(self) -> list[Any]:  # pragma: no cover
        raise AssertionError("rename by chat-id must not touch folders")


def _patch_cli_topic_backends(
    monkeypatch: pytest.MonkeyPatch,
    backend: FakeTopicBackend,
    folder_backend: FakeFolderBackend,
    store: OperationStore,
) -> None:
    class _FakeManager:
        async def get_client(self) -> Any:
            return object()

        async def disconnect(self) -> None:
            return None

    def _factory(config_path: Path | None) -> Any:
        from telegram_assistant.config import load_config

        config = load_config(config_path)

        async def _open() -> Any:
            return backend, folder_backend

        return config, _FakeManager(), store, _open

    monkeypatch.setattr(cli_main, "_build_topic_backends", _factory)


def _last_json_line(stdout: str) -> dict[str, Any]:
    return json.loads(stdout.strip().splitlines()[-1])


def _run(args: list[str]) -> Any:
    return CliRunner().invoke(cli_main.app, args)


# ---------------------------------------------------------------------------
# happy path
# ---------------------------------------------------------------------------


def test_rename_success_by_topic_id(
    minimal_config_yaml: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_file = _write_config(tmp_path, minimal_config_yaml)
    backend = FakeTopicBackend(topics=[TopicSummary(topic_id=42, title="Old")])
    store = OperationStore(tmp_path / "state.db")
    _patch_cli_topic_backends(monkeypatch, backend, FakeFolderBackend(), store)

    result = _run(
        [
            "topics",
            "rename",
            "--topic-id",
            "42",
            "--chat-id",
            "-100",
            "--new-title",
            "Renamed Topic",
            "--reason",
            "tidy",
            "--config",
            str(config_file),
        ]
    )
    assert result.exit_code == 0, result.stdout
    payload = _last_json_line(result.stdout)
    assert payload["telegram_chat_id"] == -100
    assert payload["telegram_topic_id"] == 42
    assert payload["new_title"] == "Renamed Topic"
    assert payload["status"] == "renamed"
    assert payload["replayed"] is False
    assert payload["operation_status"] == "completed"
    assert backend.renamed == [(-100, 42, "Renamed Topic")]


def test_rename_success_by_topic_name(
    minimal_config_yaml: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_file = _write_config(tmp_path, minimal_config_yaml)
    backend = FakeTopicBackend(
        topics=[
            TopicSummary(topic_id=1, title="Alpha"),
            TopicSummary(topic_id=2, title="Beta"),
        ]
    )
    store = OperationStore(tmp_path / "state.db")
    _patch_cli_topic_backends(monkeypatch, backend, FakeFolderBackend(), store)

    result = _run(
        [
            "topics",
            "rename",
            "--topic-name",
            "Beta",
            "--chat-id",
            "-100",
            "--new-title",
            "Beta v2",
            "--config",
            str(config_file),
        ]
    )
    assert result.exit_code == 0, result.stdout
    payload = _last_json_line(result.stdout)
    assert payload["telegram_topic_id"] == 2
    assert backend.renamed == [(-100, 2, "Beta v2")]


def test_rename_strips_whitespace_in_backend_call(
    minimal_config_yaml: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_file = _write_config(tmp_path, minimal_config_yaml)
    backend = FakeTopicBackend(topics=[TopicSummary(topic_id=7, title="Old")])
    store = OperationStore(tmp_path / "state.db")
    _patch_cli_topic_backends(monkeypatch, backend, FakeFolderBackend(), store)

    result = _run(
        [
            "topics",
            "rename",
            "--topic-id",
            "7",
            "--chat-id",
            "-100",
            "--new-title",
            "  Spaced  ",
            "--config",
            str(config_file),
        ]
    )
    assert result.exit_code == 0, result.stdout
    assert backend.renamed == [(-100, 7, "Spaced")]


def test_rename_replays_same_title(
    minimal_config_yaml: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_file = _write_config(tmp_path, minimal_config_yaml)
    backend = FakeTopicBackend(topics=[TopicSummary(topic_id=42, title="Old")])
    store = OperationStore(tmp_path / "state.db")
    _patch_cli_topic_backends(monkeypatch, backend, FakeFolderBackend(), store)

    args = [
        "topics",
        "rename",
        "--topic-id",
        "42",
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
    assert backend.renamed == [(-100, 42, "Same")]


# ---------------------------------------------------------------------------
# dry-run
# ---------------------------------------------------------------------------


def test_rename_dry_run_does_not_call_backend(
    minimal_config_yaml: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_file = _write_config(tmp_path, minimal_config_yaml)
    backend = FakeTopicBackend(topics=[TopicSummary(topic_id=42, title="Old")])
    store = OperationStore(tmp_path / "state.db")
    _patch_cli_topic_backends(monkeypatch, backend, FakeFolderBackend(), store)

    result = _run(
        [
            "topics",
            "rename",
            "--topic-id",
            "42",
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
    assert payload["command"] == "topics.rename"
    assert payload["resolved"]["telegram_chat_id"] == -100
    assert payload["resolved"]["telegram_topic_id"] == 42
    assert payload["resolved"]["new_title"] == "Planned"
    assert payload["resolved"]["old_title"] == "Old"
    assert any("Planned" in action for action in payload["planned_actions"])

    # Dry-run must not touch the backend or persist any operation row.
    assert backend.renamed == []
    with store._connect() as conn:  # noqa: SLF001 (test introspection)
        count = int(conn.execute("SELECT COUNT(*) FROM operations").fetchone()[0])
    assert count == 0


def test_rename_dry_run_topic_id_not_found_exit_2(
    minimal_config_yaml: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_file = _write_config(tmp_path, minimal_config_yaml)
    backend = FakeTopicBackend(topics=[TopicSummary(topic_id=1, title="Only")])
    store = OperationStore(tmp_path / "state.db")
    _patch_cli_topic_backends(monkeypatch, backend, FakeFolderBackend(), store)

    result = _run(
        [
            "topics",
            "rename",
            "--topic-id",
            "999",
            "--chat-id",
            "-100",
            "--new-title",
            "Nope",
            "--dry-run",
            "--config",
            str(config_file),
        ]
    )
    assert result.exit_code == 2
    assert backend.renamed == []


# ---------------------------------------------------------------------------
# validation / error exit codes
# ---------------------------------------------------------------------------


def test_rename_blank_new_title_exit_2(
    minimal_config_yaml: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_file = _write_config(tmp_path, minimal_config_yaml)
    backend = FakeTopicBackend(topics=[TopicSummary(topic_id=42, title="Old")])
    store = OperationStore(tmp_path / "state.db")
    _patch_cli_topic_backends(monkeypatch, backend, FakeFolderBackend(), store)

    result = _run(
        [
            "topics",
            "rename",
            "--topic-id",
            "42",
            "--chat-id",
            "-100",
            "--new-title",
            "   ",
            "--config",
            str(config_file),
        ]
    )
    assert result.exit_code == 2
    assert backend.renamed == []


def test_rename_requires_topic_id_or_name(
    minimal_config_yaml: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_file = _write_config(tmp_path, minimal_config_yaml)
    backend = FakeTopicBackend()
    store = OperationStore(tmp_path / "state.db")
    _patch_cli_topic_backends(monkeypatch, backend, FakeFolderBackend(), store)

    result = _run(
        [
            "topics",
            "rename",
            "--chat-id",
            "-100",
            "--new-title",
            "X",
            "--config",
            str(config_file),
        ]
    )
    assert result.exit_code == 2
    assert backend.renamed == []


def test_rename_requires_chat_ref(
    minimal_config_yaml: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_file = _write_config(tmp_path, minimal_config_yaml)
    backend = FakeTopicBackend()
    store = OperationStore(tmp_path / "state.db")
    _patch_cli_topic_backends(monkeypatch, backend, FakeFolderBackend(), store)

    result = _run(
        [
            "topics",
            "rename",
            "--topic-id",
            "42",
            "--new-title",
            "X",
            "--config",
            str(config_file),
        ]
    )
    assert result.exit_code == 2
    assert backend.renamed == []


def test_rename_ambiguous_topic_name_exit_2(
    minimal_config_yaml: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_file = _write_config(tmp_path, minimal_config_yaml)
    backend = FakeTopicBackend(
        topics=[
            TopicSummary(topic_id=1, title="Same"),
            TopicSummary(topic_id=2, title="Same"),
        ]
    )
    store = OperationStore(tmp_path / "state.db")
    _patch_cli_topic_backends(monkeypatch, backend, FakeFolderBackend(), store)

    result = _run(
        [
            "topics",
            "rename",
            "--topic-name",
            "Same",
            "--chat-id",
            "-100",
            "--new-title",
            "X",
            "--config",
            str(config_file),
        ]
    )
    assert result.exit_code == 2
    assert backend.renamed == []


def test_rename_topic_name_not_found_exit_2(
    minimal_config_yaml: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_file = _write_config(tmp_path, minimal_config_yaml)
    backend = FakeTopicBackend(topics=[TopicSummary(topic_id=1, title="Only")])
    store = OperationStore(tmp_path / "state.db")
    _patch_cli_topic_backends(monkeypatch, backend, FakeFolderBackend(), store)

    result = _run(
        [
            "topics",
            "rename",
            "--topic-name",
            "Ghost",
            "--chat-id",
            "-100",
            "--new-title",
            "X",
            "--config",
            str(config_file),
        ]
    )
    assert result.exit_code == 2
    assert backend.renamed == []


def test_rename_access_denied_exit_3(
    minimal_config_yaml: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_file = _write_config(tmp_path, _config_deny_all(minimal_config_yaml))
    backend = FakeTopicBackend(topics=[TopicSummary(topic_id=42, title="Old")])
    store = OperationStore(tmp_path / "state.db")
    _patch_cli_topic_backends(monkeypatch, backend, FakeFolderBackend(), store)

    result = _run(
        [
            "topics",
            "rename",
            "--topic-id",
            "42",
            "--chat-id",
            "-100",
            "--new-title",
            "Denied",
            "--config",
            str(config_file),
        ]
    )
    assert result.exit_code == cli_main.ACCESS_DENIED_EXIT_CODE
    assert backend.renamed == []
