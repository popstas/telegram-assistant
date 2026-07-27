"""The ``line_breaks`` knob on the CLI, HTTP and MCP surfaces.

Telegram parses a rich message's markdown server-side and, like CommonMark,
folds a single newline inside a paragraph into a space — so an Obsidian note's
two lines under one another arrive as one run-on line. The line-splitting pass
turns each into its own paragraph (which the clients render tight against each
other), and this file covers the *per-call* override of it:

* CLI ``--line-breaks`` / ``--no-line-breaks`` (an error without
  ``--rich-markdown``) and the dry-run marker it reports.
* HTTP ``line_breaks`` on ``MessageSendBody`` (422 without ``rich_markdown``).
* MCP ``telegram_messages_send(line_breaks=…)``, which shares that body.

Each surface is exercised against a config that says the opposite of the flag,
so "the flag layers over the config default" is what is actually pinned.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from telegram_assistant.cli import main as cli_main
from telegram_assistant.config import load_config_from_text
from telegram_assistant.http_api import create_app
from telegram_assistant.messages import NBSP
from telegram_assistant.persistence import OperationStore
from telegram_assistant.topics import TopicSummary
from tests.test_mcp_tools import (
    FakeRichMessageBackend,
    _call_tool,
    _initialize,
    _mint_token,
)
from tests.test_mcp_tools import (
    _client as _mcp_client,
)

AUTH = {"Authorization": "Bearer secret_token"}

# One paragraph, two lines: exactly one split point, and — because the pass
# leaves no spacer between the halves — an unambiguous expected output.
SOFT_BREAK = "Фотоальбом - A\nВидео плейлист - B\n"
SPLIT = "Фотоальбом - A\n\nВидео плейлист - B\n"


class RecordingBackend:
    """MessageBackend fake recording every kwarg the service passes."""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def send_message(
        self,
        *,
        chat_id: int,
        text: str,
        topic_id: int | None = None,
        files: tuple[str, ...] = (),
        schedule_at: Any = None,
        reply_to_message_id: int | None = None,
        rich_markdown: str | None = None,
    ) -> int:
        self.sent.append({"chat_id": chat_id, "rich_markdown": rich_markdown})
        return 777

    async def list_topics(self, *, chat_id: int) -> list[TopicSummary]:
        return []


def _config_yaml(*, line_breaks: bool | None = None) -> str:
    lines = [
        "telegram:",
        "  api_id: 123456",
        '  api_hash: "telegram_api_hash"',
        "  session_path: /data/telegram-assistant.session",
        "  default_chat_folder:",
        "    folder_id: 2",
        '    folder_name: "Planfix clients"',
        "  defaults:",
        "    enable_topics: true",
    ]
    if line_breaks is not None:
        lines.append(f"    rich_markdown_line_breaks: {str(line_breaks).lower()}")
    lines += [
        "http:",
        '  host: "0.0.0.0"',
        "  port: 8085",
        '  bearer_token: "secret_token"',
        "logging:",
        "  level: INFO",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# the pass itself, through the domain layer
# ---------------------------------------------------------------------------


def test_the_split_halves_get_no_spacer_between_them() -> None:
    """The point of the whole feature: two lines, no blank line between."""
    from telegram_assistant.messages import normalize_rich_markdown

    result = normalize_rich_markdown(SOFT_BREAK)
    assert result.markdown == SPLIT
    assert NBSP not in result.markdown


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _patch_cli(
    monkeypatch: pytest.MonkeyPatch, backend: Any, store: OperationStore
) -> None:
    class _FakeManager:
        async def disconnect(self) -> None:
            return None

    def _factory(config_path: Path | None) -> Any:
        from telegram_assistant.config import load_config

        async def _open() -> Any:
            return backend, backend, None

        return load_config(config_path), _FakeManager(), store, _open

    monkeypatch.setattr(cli_main, "_build_message_backends", _factory)


def _cli_setup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    config_line_breaks: bool | None = None,
) -> tuple[Path, Path, RecordingBackend]:
    config_file = tmp_path / "config.yml"
    config_file.write_text(_config_yaml(line_breaks=config_line_breaks))
    md_file = tmp_path / "article.md"
    md_file.write_text(SOFT_BREAK, encoding="utf-8")
    backend = RecordingBackend()
    _patch_cli(monkeypatch, backend, OperationStore(tmp_path / "cli.db"))
    return config_file, md_file, backend


def _run_cli(args: list[str]) -> Any:
    return CliRunner().invoke(cli_main.app, args)


def _cli_output(result: Any) -> str:
    """Click 8.3 keeps stderr separate; error messages go there."""
    return (result.stdout or "") + (result.stderr or "")


def test_cli_no_line_breaks_sends_the_paragraph_unsplit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The flag beats the config default (here: splitting explicitly on)."""
    config_file, md_file, backend = _cli_setup(
        tmp_path, monkeypatch, config_line_breaks=True
    )

    result = _run_cli(
        [
            "messages",
            "send",
            "--chat-id",
            "-100",
            "--rich-markdown",
            str(md_file),
            "--no-line-breaks",
            "--operation-id",
            "cli-breaks-off",
            "--config",
            str(config_file),
        ]
    )

    assert result.exit_code == 0, _cli_output(result)
    assert backend.sent[0]["rich_markdown"] == SOFT_BREAK


def test_cli_line_breaks_overrides_config_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_file, md_file, backend = _cli_setup(
        tmp_path, monkeypatch, config_line_breaks=False
    )

    result = _run_cli(
        [
            "messages",
            "send",
            "--chat-id",
            "-100",
            "--rich-markdown",
            str(md_file),
            "--line-breaks",
            "--operation-id",
            "cli-breaks-on",
            "--config",
            str(config_file),
        ]
    )

    assert result.exit_code == 0, _cli_output(result)
    assert backend.sent[0]["rich_markdown"] == SPLIT


def test_cli_config_off_is_honoured_without_a_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_file, md_file, backend = _cli_setup(
        tmp_path, monkeypatch, config_line_breaks=False
    )

    result = _run_cli(
        [
            "messages",
            "send",
            "--chat-id",
            "-100",
            "--rich-markdown",
            str(md_file),
            "--operation-id",
            "cli-breaks-config-off",
            "--config",
            str(config_file),
        ]
    )

    assert result.exit_code == 0, _cli_output(result)
    assert backend.sent[0]["rich_markdown"] == SOFT_BREAK


@pytest.mark.parametrize("flag", ["--line-breaks", "--no-line-breaks"], ids=["on", "off"])
def test_cli_line_breaks_without_rich_markdown_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, flag: str
) -> None:
    """The knob rewrites markdown; on a plain send it would silently do nothing."""
    config_file, _md_file, backend = _cli_setup(tmp_path, monkeypatch)

    result = _run_cli(
        [
            "messages",
            "send",
            "--chat-id",
            "-100",
            "--text",
            "hello",
            flag,
            "--config",
            str(config_file),
        ]
    )

    assert result.exit_code == 2, _cli_output(result)
    assert "--rich-markdown" in _cli_output(result)
    assert backend.sent == []


def test_cli_dry_run_reports_the_line_breaks_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_file, md_file, backend = _cli_setup(tmp_path, monkeypatch)

    result = _run_cli(
        [
            "messages",
            "send",
            "--chat-id",
            "-100",
            "--rich-markdown",
            str(md_file),
            "--dry-run",
            "--config",
            str(config_file),
        ]
    )

    assert result.exit_code == 0, _cli_output(result)
    resolved = json.loads(result.stdout.strip().splitlines()[-1])["resolved"]
    assert resolved["line_breaks"] is True
    # One paragraph became two; no spacer joins them, so the count is exactly 2.
    assert resolved["rich_markdown_blocks"] == 2
    # The body is still never echoed back.
    assert "Фотоальбом" not in json.dumps(resolved)
    assert backend.sent == []


def test_cli_dry_run_plain_send_has_no_line_breaks_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_file, _md_file, _backend = _cli_setup(tmp_path, monkeypatch)

    result = _run_cli(
        [
            "messages",
            "send",
            "--chat-id",
            "-100",
            "--text",
            "hello",
            "--dry-run",
            "--config",
            str(config_file),
        ]
    )

    assert result.exit_code == 0, _cli_output(result)
    resolved = json.loads(result.stdout.strip().splitlines()[-1])["resolved"]
    assert resolved["line_breaks"] is None


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


def _http_client(
    backend: RecordingBackend, tmp_path: Path, *, config_line_breaks: bool | None = None
) -> TestClient:
    app = create_app(
        load_config_from_text(_config_yaml(line_breaks=config_line_breaks)),
        session_manager=None,
        message_backend_factory=lambda _r: backend,
        topic_backend_factory=lambda _r: backend,
        operation_store=OperationStore(tmp_path / "http.db"),
    )
    return TestClient(app)


def test_http_line_breaks_false_sends_the_paragraph_unsplit(tmp_path: Path) -> None:
    backend = RecordingBackend()
    client = _http_client(backend, tmp_path, config_line_breaks=True)

    resp = client.post(
        "/telegram/messages",
        json={
            "telegram_chat_id": -100,
            "rich_markdown": SOFT_BREAK,
            "line_breaks": False,
            "operation_id": "http-breaks-off",
        },
        headers=AUTH,
    )

    assert resp.status_code == 200, resp.text
    assert backend.sent[0]["rich_markdown"] == SOFT_BREAK


def test_http_line_breaks_true_overrides_config_off(tmp_path: Path) -> None:
    backend = RecordingBackend()
    client = _http_client(backend, tmp_path, config_line_breaks=False)

    resp = client.post(
        "/telegram/messages",
        json={
            "telegram_chat_id": -100,
            "rich_markdown": SOFT_BREAK,
            "line_breaks": True,
            "operation_id": "http-breaks-on",
        },
        headers=AUTH,
    )

    assert resp.status_code == 200, resp.text
    assert backend.sent[0]["rich_markdown"] == SPLIT


@pytest.mark.parametrize("value", [True, False], ids=["on", "off"])
def test_http_line_breaks_without_rich_markdown_is_422(
    tmp_path: Path, value: bool
) -> None:
    backend = RecordingBackend()
    client = _http_client(backend, tmp_path)

    resp = client.post(
        "/telegram/messages",
        json={"telegram_chat_id": -100, "text": "hi", "line_breaks": value},
        headers=AUTH,
    )

    assert resp.status_code == 422, resp.text
    assert "line_breaks" in resp.text
    assert backend.sent == []


# ---------------------------------------------------------------------------
# MCP
# ---------------------------------------------------------------------------


def test_mcp_line_breaks_false_sends_the_paragraph_unsplit(
    minimal_config_yaml: str, tmp_path: Path
) -> None:
    backend = FakeRichMessageBackend()
    with _mcp_client(minimal_config_yaml, tmp_path, message_backend=backend) as client:
        token = _mint_token(client)
        _initialize(client, token)

        result = _call_tool(
            client,
            token,
            "telegram_messages_send",
            {
                "telegram_chat_id": -100123,
                "rich_markdown": SOFT_BREAK,
                "line_breaks": False,
                "operation_id": "mcp-breaks-off",
            },
        )

    assert result["isError"] is False, result
    assert backend.sent[0]["rich_markdown"] == SOFT_BREAK


def test_mcp_line_breaks_defaults_to_splitting(
    minimal_config_yaml: str, tmp_path: Path
) -> None:
    backend = FakeRichMessageBackend()
    with _mcp_client(minimal_config_yaml, tmp_path, message_backend=backend) as client:
        token = _mint_token(client)
        _initialize(client, token)

        result = _call_tool(
            client,
            token,
            "telegram_messages_send",
            {
                "telegram_chat_id": -100123,
                "rich_markdown": SOFT_BREAK,
                "operation_id": "mcp-breaks-default",
            },
        )

    assert result["isError"] is False, result
    assert backend.sent[0]["rich_markdown"] == SPLIT


def test_mcp_line_breaks_without_rich_markdown_is_an_error(
    minimal_config_yaml: str, tmp_path: Path
) -> None:
    backend = FakeRichMessageBackend()
    with _mcp_client(minimal_config_yaml, tmp_path, message_backend=backend) as client:
        token = _mint_token(client)
        _initialize(client, token)

        result = _call_tool(
            client,
            token,
            "telegram_messages_send",
            {"telegram_chat_id": -100123, "text": "hi", "line_breaks": True},
        )

    assert result["isError"] is True, result
    assert backend.sent == []
