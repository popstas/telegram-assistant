"""Task 4 — the ``spaced_paragraphs`` knob on the CLI, HTTP and MCP surfaces.

Task 3 wired normalisation into ``send_message`` and made the config knob
``telegram.defaults.rich_markdown_spaced_paragraphs`` the default. This file
covers the *per-call* override on every surface:

* CLI ``--spaced-paragraphs`` / ``--no-spaced-paragraphs`` (an error without
  ``--rich-markdown``), the dry-run markers it reports, and the warnings a real
  run echoes to stderr.
* HTTP ``spaced_paragraphs`` on ``MessageSendBody`` (422 without
  ``rich_markdown``, matching the other shape rules).
* MCP ``telegram_messages_send(spaced_paragraphs=…)``, which shares that body.

The flag layers *over* the config default, so each surface is exercised against
a config that says the opposite of the flag.
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
from telegram_assistant.messages import MAX_RICH_BLOCKS, NBSP
from telegram_assistant.persistence import OperationStore
from telegram_assistant.topics import TopicSummary
from tests.test_mcp_tools import (
    FakeMessageBackend,
    FakeRichMessageBackend,
    _call_tool,
    _initialize,
    _mint_token,
)
from tests.test_mcp_tools import (
    _client as _mcp_client,
)

AUTH = {"Authorization": "Bearer secret_token"}

# Two paragraphs: exactly one insertion point, so the spacer is unambiguous.
TWO_PARAGRAPHS = "one\n\ntwo\n"

# Past the 500-block budget: the spacer pass rolls back *and* the article is
# reported as over-limit, which is the only way to get warnings without a real
# Telegram round-trip.
OVERSIZE_ARTICLE = "\n\n".join(f"p{i}" for i in range(MAX_RICH_BLOCKS + 1)) + "\n"


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
        self.sent.append(
            {
                "chat_id": chat_id,
                "text": text,
                "topic_id": topic_id,
                "rich_markdown": rich_markdown,
            }
        )
        return 777

    async def list_topics(self, *, chat_id: int) -> list[TopicSummary]:
        return []


def _config_yaml(*, spaced: bool | None = None) -> str:
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
    if spaced is not None:
        lines.append(f"    rich_markdown_spaced_paragraphs: {str(spaced).lower()}")
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
    config_spaced: bool | None = None,
    markdown: str = TWO_PARAGRAPHS,
) -> tuple[Path, Path, RecordingBackend]:
    config_file = tmp_path / "config.yml"
    config_file.write_text(_config_yaml(spaced=config_spaced))
    md_file = tmp_path / "article.md"
    md_file.write_text(markdown, encoding="utf-8")
    backend = RecordingBackend()
    _patch_cli(monkeypatch, backend, OperationStore(tmp_path / "cli.db"))
    return config_file, md_file, backend


def _run_cli(args: list[str]) -> Any:
    return CliRunner().invoke(cli_main.app, args)


def _cli_output(result: Any) -> str:
    """Click 8.3 keeps stderr separate; error messages go there."""
    return (result.stdout or "") + (result.stderr or "")


def test_cli_no_spaced_paragraphs_sends_byte_for_byte(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The flag beats the config default (here: spacing explicitly on)."""
    config_file, md_file, backend = _cli_setup(
        tmp_path, monkeypatch, config_spaced=True
    )

    result = _run_cli(
        [
            "messages",
            "send",
            "--chat-id",
            "-100",
            "--rich-markdown",
            str(md_file),
            "--no-spaced-paragraphs",
            "--operation-id",
            "cli-flag-off",
            "--config",
            str(config_file),
        ]
    )

    assert result.exit_code == 0, _cli_output(result)
    assert backend.sent[0]["rich_markdown"] == TWO_PARAGRAPHS


def test_cli_no_spaced_paragraphs_still_expands_wikilinks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Byte-for-byte via ``--no-spaced-paragraphs`` still expands wikilinks —
    there is no knob for that pass. Same proof as the HTTP/MCP surfaces
    (``test_http_rich_send_expands_wikilinks``,
    ``test_mcp_send_rich_markdown_expands_wikilinks``), pinned here for the
    CLI's own ``send_message`` wiring."""
    config_file, md_file, backend = _cli_setup(
        tmp_path,
        monkeypatch,
        config_spaced=True,
        markdown="Отдал [[Денис Баталин|Дэну]].\n",
    )

    result = _run_cli(
        [
            "messages",
            "send",
            "--chat-id",
            "-100",
            "--rich-markdown",
            str(md_file),
            "--no-spaced-paragraphs",
            "--operation-id",
            "cli-flag-off-wikilink",
            "--config",
            str(config_file),
        ]
    )

    assert result.exit_code == 0, _cli_output(result)
    sent = backend.sent[0]["rich_markdown"]
    assert sent == "Отдал Дэну.\n"
    assert "[[" not in sent


def test_cli_spaced_paragraphs_overrides_config_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_file, md_file, backend = _cli_setup(
        tmp_path, monkeypatch, config_spaced=False
    )

    result = _run_cli(
        [
            "messages",
            "send",
            "--chat-id",
            "-100",
            "--rich-markdown",
            str(md_file),
            "--spaced-paragraphs",
            "--operation-id",
            "cli-flag-on",
            "--config",
            str(config_file),
        ]
    )

    assert result.exit_code == 0, _cli_output(result)
    assert NBSP in backend.sent[0]["rich_markdown"]


@pytest.mark.parametrize(
    "flag", ["--spaced-paragraphs", "--no-spaced-paragraphs"], ids=["on", "off"]
)
def test_cli_spaced_paragraphs_without_rich_markdown_errors(
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


def test_cli_dry_run_reports_normalization_markers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The preview reports the *post-normalization* size, never the body."""
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
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    resolved = payload["resolved"]
    assert resolved["spaced_paragraphs"] is True
    assert resolved["spaced"] is True
    # source: "one\n\ntwo\n" → 3 blocks (two paragraphs + the spacer).
    assert resolved["rich_markdown_blocks"] == 3
    assert resolved["rich_markdown_media"] == 0
    assert resolved["rich_markdown_chars"] > len(TWO_PARAGRAPHS)
    assert f"{resolved['rich_markdown_chars']} chars" in payload["would"]
    assert payload["warnings"] == []
    # The body is still never echoed back.
    assert "one" not in json.dumps(payload["resolved"])
    assert backend.sent == []


def test_cli_dry_run_no_spaced_paragraphs_reports_source_size(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_file, md_file, _backend = _cli_setup(tmp_path, monkeypatch)

    result = _run_cli(
        [
            "messages",
            "send",
            "--chat-id",
            "-100",
            "--rich-markdown",
            str(md_file),
            "--no-spaced-paragraphs",
            "--dry-run",
            "--config",
            str(config_file),
        ]
    )

    assert result.exit_code == 0, _cli_output(result)
    resolved = json.loads(result.stdout.strip().splitlines()[-1])["resolved"]
    assert resolved["spaced_paragraphs"] is False
    assert resolved["spaced"] is False
    assert resolved["rich_markdown_chars"] == len(TWO_PARAGRAPHS)
    assert resolved["rich_markdown_blocks"] == 2


def test_cli_dry_run_plain_send_has_no_spacing_markers(
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
    assert resolved["rich_markdown"] is False
    assert resolved["spaced_paragraphs"] is None
    assert resolved["rich_markdown_blocks"] is None
    assert resolved["rich_markdown_media"] is None


def test_cli_dry_run_reports_expanded_wikilinks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_file, md_file, backend = _cli_setup(
        tmp_path, monkeypatch, markdown="Отдал [[Денис Баталин|Дэну]].\n"
    )

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
    assert resolved["rich_markdown_wikilinks"] == 1
    assert backend.sent == []


def test_cli_dry_run_reports_unwrapped_links(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_file, md_file, backend = _cli_setup(
        tmp_path,
        monkeypatch,
        markdown="[269 - AWRA](https://example.com/?action=view&key=269)\n",
    )

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
    assert resolved["rich_markdown_unwrapped_links"] == 1
    assert backend.sent == []


def test_cli_dry_run_plain_send_has_no_wikilink_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The marker shape must never depend on the mode."""
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
    assert resolved["rich_markdown_wikilinks"] is None
    assert resolved["rich_markdown_unwrapped_links"] is None


def test_cli_dry_run_reports_block_limit_warnings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_file, md_file, _backend = _cli_setup(
        tmp_path, monkeypatch, markdown=OVERSIZE_ARTICLE
    )

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
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    warnings = payload["warnings"]
    assert any("spaced_paragraphs disabled" in w for w in warnings), warnings
    assert any(f"{MAX_RICH_BLOCKS}-block limit" in w for w in warnings), warnings
    # The rollback is visible in the markers, not only in the prose.
    assert payload["resolved"]["spaced_paragraphs"] is True
    assert payload["resolved"]["spaced"] is False


def test_cli_real_send_echoes_warnings_to_stderr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Warnings ride the result JSON, but a human must not have to parse it."""
    config_file, md_file, backend = _cli_setup(
        tmp_path, monkeypatch, markdown=OVERSIZE_ARTICLE
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
            "cli-warnings",
            "--config",
            str(config_file),
        ]
    )

    assert result.exit_code == 0, _cli_output(result)
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert any("spaced_paragraphs disabled" in w for w in payload["warnings"])
    assert "warning: spaced_paragraphs disabled" in _cli_output(result)
    # Rolled back, so the article went out unspaced.
    assert NBSP not in backend.sent[0]["rich_markdown"]


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


def _http_client(
    backend: RecordingBackend, tmp_path: Path, *, config_spaced: bool | None = None
) -> TestClient:
    app = create_app(
        load_config_from_text(_config_yaml(spaced=config_spaced)),
        session_manager=None,
        message_backend_factory=lambda _r: backend,
        topic_backend_factory=lambda _r: backend,
        operation_store=OperationStore(tmp_path / "http.db"),
    )
    return TestClient(app)


def test_http_spaced_paragraphs_false_sends_byte_for_byte(tmp_path: Path) -> None:
    backend = RecordingBackend()
    client = _http_client(backend, tmp_path, config_spaced=True)

    resp = client.post(
        "/telegram/messages",
        json={
            "telegram_chat_id": -100,
            "rich_markdown": TWO_PARAGRAPHS,
            "spaced_paragraphs": False,
            "operation_id": "http-flag-off",
        },
        headers=AUTH,
    )

    assert resp.status_code == 200, resp.text
    assert backend.sent[0]["rich_markdown"] == TWO_PARAGRAPHS


def test_http_spaced_paragraphs_true_overrides_config_off(tmp_path: Path) -> None:
    backend = RecordingBackend()
    client = _http_client(backend, tmp_path, config_spaced=False)

    resp = client.post(
        "/telegram/messages",
        json={
            "telegram_chat_id": -100,
            "rich_markdown": TWO_PARAGRAPHS,
            "spaced_paragraphs": True,
            "operation_id": "http-flag-on",
        },
        headers=AUTH,
    )

    assert resp.status_code == 200, resp.text
    assert NBSP in backend.sent[0]["rich_markdown"]


@pytest.mark.parametrize("value", [True, False], ids=["on", "off"])
def test_http_spaced_paragraphs_without_rich_markdown_is_422(
    tmp_path: Path, value: bool
) -> None:
    backend = RecordingBackend()
    client = _http_client(backend, tmp_path)

    resp = client.post(
        "/telegram/messages",
        json={
            "telegram_chat_id": -100,
            "text": "hi",
            "spaced_paragraphs": value,
        },
        headers=AUTH,
    )

    assert resp.status_code == 422, resp.text
    assert "spaced_paragraphs" in resp.text
    assert backend.sent == []


def test_http_rich_send_warnings_reach_the_response(tmp_path: Path) -> None:
    backend = RecordingBackend()
    client = _http_client(backend, tmp_path)

    resp = client.post(
        "/telegram/messages",
        json={
            "telegram_chat_id": -100,
            "rich_markdown": OVERSIZE_ARTICLE,
            "operation_id": "http-warnings",
        },
        headers=AUTH,
    )

    assert resp.status_code == 200, resp.text
    warnings = resp.json()["warnings"]
    assert any("spaced_paragraphs disabled" in w for w in warnings), warnings


# ---------------------------------------------------------------------------
# MCP
# ---------------------------------------------------------------------------


def test_mcp_spaced_paragraphs_false_sends_byte_for_byte(
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
                "rich_markdown": TWO_PARAGRAPHS,
                "spaced_paragraphs": False,
                "operation_id": "mcp-flag-off",
            },
        )

    assert result["isError"] is False, result
    assert backend.sent[0]["rich_markdown"] == TWO_PARAGRAPHS


def test_mcp_spaced_paragraphs_default_inserts_spacer(
    minimal_config_yaml: str, tmp_path: Path
) -> None:
    """Leaving the kwarg unset follows the server default (on)."""
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
                "rich_markdown": TWO_PARAGRAPHS,
                "operation_id": "mcp-flag-default",
            },
        )

    assert result["isError"] is False, result
    assert NBSP in backend.sent[0]["rich_markdown"]


@pytest.mark.parametrize("value", [True, False], ids=["on", "off"])
def test_mcp_spaced_paragraphs_without_rich_markdown_errors(
    minimal_config_yaml: str, tmp_path: Path, value: bool
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
                "text": "hi",
                "spaced_paragraphs": value,
            },
        )

    assert result["isError"] is True, result
    assert "spaced_paragraphs" in result["content"][0]["text"], result
    assert backend.sent == []


def test_mcp_plain_send_still_omits_rich_kwargs(
    minimal_config_yaml: str, tmp_path: Path
) -> None:
    """``FakeMessageBackend`` predates the rich kwarg: a plain send that started
    passing it (because the tool grew a spacing knob) would raise TypeError."""
    backend = FakeMessageBackend()
    with _mcp_client(minimal_config_yaml, tmp_path, message_backend=backend) as client:
        token = _mint_token(client)
        _initialize(client, token)

        result = _call_tool(
            client,
            token,
            "telegram_messages_send",
            {"telegram_chat_id": -100123, "text": "plain"},
        )

    assert result["isError"] is False, result
    assert len(backend.sent) == 1
