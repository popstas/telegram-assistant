"""Task 8 — media grouping on the config, the service and the CLI.

``normalize_rich_markdown``'s grouping pass is covered in
``test_rich_markdown_grouping.py``; this file covers the wiring around it:

* ``telegram.defaults.rich_markdown_grouping`` (the knob the live
  ``data/config.yml`` already sets) and ``media_grouping_default``;
* ``SendMessageRequest.media_grouping``/``media_groups`` and what the operation
  payload records;
* CLI ``--media-group <index>=<mode>``, its input errors, and the groups the
  dry-run reports;
* HTTP, where grouping is a config decision with no per-group override.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from telegram_assistant.cli import main as cli_main
from telegram_assistant.config import ConfigError, load_config_from_text
from telegram_assistant.http_api import create_app
from telegram_assistant.messages import (
    DEFAULT_MEDIA_GROUP_MODE,
    MediaGroupChoice,
    SendMessageRequest,
    media_grouping_default,
    send_message,
)
from telegram_assistant.persistence import OperationStore
from telegram_assistant.topics import TopicSummary

AUTH = {"Authorization": "Bearer secret_token"}

# A run of two consecutive remote media, with text on each side so the run is
# unambiguous and no local file has to exist.
ARTICLE = (
    "Пляж был пустой\n\n"
    "![](https://x/a.jpg)\n\n"
    "![](https://x/b.jpg)\n\n"
    "и мы остались\n"
)


class RecordingBackend:
    """MessageBackend fake recording the markdown the service passes down."""

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


def _config_yaml(*, grouping: str | None = None) -> str:
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
    if grouping is not None:
        lines.append(f"    rich_markdown_grouping: {grouping}")
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
# Config knob
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "grouping", [None, "collage", "slideshow", "none"], ids=["absent", *"csn"]
)
def test_config_accepts_the_grouping_knob(grouping: str | None) -> None:
    """The live config.yml already sets this key; extra="forbid" must allow it."""
    config = load_config_from_text(_config_yaml(grouping=grouping))
    expected = grouping or DEFAULT_MEDIA_GROUP_MODE
    assert config.telegram.defaults.rich_markdown_grouping == expected
    assert media_grouping_default(config) == expected


def test_config_rejects_an_unknown_grouping() -> None:
    with pytest.raises(ConfigError):
        load_config_from_text(_config_yaml(grouping="carousel"))


def test_media_grouping_default_without_config() -> None:
    """A missing config (or one predating the knob) keeps the built-in default."""
    assert media_grouping_default(None) == DEFAULT_MEDIA_GROUP_MODE
    assert media_grouping_default(object()) == DEFAULT_MEDIA_GROUP_MODE


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


@pytest.fixture()
def store(tmp_path: Path) -> OperationStore:
    return OperationStore(tmp_path / "ops.db")


async def test_send_message_groups_media_by_default(store: OperationStore) -> None:
    backend = RecordingBackend()
    await send_message(
        backend=backend,
        store=store,
        request=SendMessageRequest(
            telegram_chat_id=-100,
            text="",
            rich_markdown=ARTICLE,
            operation_id="group-default",
        ),
    )
    assert "<tg-collage>" in backend.sent[0]["rich_markdown"]


async def test_send_message_grouping_none_sends_byte_for_byte(
    store: OperationStore,
) -> None:
    backend = RecordingBackend()
    await send_message(
        backend=backend,
        store=store,
        request=SendMessageRequest(
            telegram_chat_id=-100,
            text="",
            rich_markdown=ARTICLE,
            spaced_paragraphs=False,
            media_grouping="none",
            operation_id="group-off",
        ),
    )
    assert backend.sent[0]["rich_markdown"] == ARTICLE


async def test_send_message_per_group_override(store: OperationStore) -> None:
    backend = RecordingBackend()
    await send_message(
        backend=backend,
        store=store,
        request=SendMessageRequest(
            telegram_chat_id=-100,
            text="",
            rich_markdown=ARTICLE,
            media_groups=(MediaGroupChoice(0, "slideshow"),),
            operation_id="group-slideshow",
        ),
    )
    assert "<tg-slideshow>" in backend.sent[0]["rich_markdown"]


def test_payload_records_the_grouping_decision() -> None:
    payload = SendMessageRequest(
        telegram_chat_id=-100,
        text="",
        rich_markdown=ARTICLE,
        media_grouping="slideshow",
        media_groups=(MediaGroupChoice(0, "none"),),
    ).to_payload()
    assert payload["media_grouping"] == "slideshow"
    assert payload["media_groups"] == [{"index": 0, "mode": "none"}]


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
    grouping: str | None = None,
    markdown: str = ARTICLE,
) -> tuple[Path, Path, RecordingBackend]:
    config_file = tmp_path / "config.yml"
    config_file.write_text(_config_yaml(grouping=grouping))
    md_file = tmp_path / "article.md"
    md_file.write_text(markdown, encoding="utf-8")
    backend = RecordingBackend()
    _patch_cli(monkeypatch, backend, OperationStore(tmp_path / "cli.db"))
    return config_file, md_file, backend


def _run_cli(args: list[str]) -> Any:
    return CliRunner().invoke(cli_main.app, args)


def _cli_output(result: Any) -> str:
    return (result.stdout or "") + (result.stderr or "")


def _send_args(config_file: Path, md_file: Path, *extra: str) -> list[str]:
    return [
        "messages",
        "send",
        "--chat-id",
        "-100",
        "--rich-markdown",
        str(md_file),
        "--config",
        str(config_file),
        *extra,
    ]


@pytest.mark.parametrize(
    ("grouping", "expected"),
    [(None, "<tg-collage>"), ("slideshow", "<tg-slideshow>")],
    ids=["default", "config_slideshow"],
)
def test_cli_rich_send_respects_config_grouping(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, grouping: str | None, expected: str
) -> None:
    config_file, md_file, backend = _cli_setup(
        tmp_path, monkeypatch, grouping=grouping
    )

    result = _run_cli(
        _send_args(config_file, md_file, "--operation-id", f"cli-group-{grouping}")
    )

    assert result.exit_code == 0, _cli_output(result)
    assert expected in backend.sent[0]["rich_markdown"]


def test_cli_config_grouping_none_leaves_media_ungrouped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_file, md_file, backend = _cli_setup(tmp_path, monkeypatch, grouping="none")

    result = _run_cli(
        _send_args(
            config_file,
            md_file,
            "--no-spaced-paragraphs",
            "--operation-id",
            "cli-group-none",
        )
    )

    assert result.exit_code == 0, _cli_output(result)
    assert backend.sent[0]["rich_markdown"] == ARTICLE


@pytest.mark.parametrize(
    ("mode", "expected"),
    [("slideshow", "<tg-slideshow>"), ("collage", "<tg-collage>")],
)
def test_cli_media_group_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str, expected: str
) -> None:
    config_file, md_file, backend = _cli_setup(tmp_path, monkeypatch, grouping="none")

    result = _run_cli(
        _send_args(
            config_file,
            md_file,
            "--media-group",
            f"0={mode}",
            "--operation-id",
            f"cli-override-{mode}",
        )
    )

    assert result.exit_code == 0, _cli_output(result)
    assert expected in backend.sent[0]["rich_markdown"]


def test_cli_media_group_none_beats_the_config_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_file, md_file, backend = _cli_setup(tmp_path, monkeypatch)

    result = _run_cli(
        _send_args(
            config_file,
            md_file,
            "--media-group",
            "0=none",
            "--no-spaced-paragraphs",
            "--operation-id",
            "cli-override-none",
        )
    )

    assert result.exit_code == 0, _cli_output(result)
    assert backend.sent[0]["rich_markdown"] == ARTICLE


def test_cli_media_group_without_rich_markdown_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_file, _md_file, backend = _cli_setup(tmp_path, monkeypatch)

    result = _run_cli(
        [
            "messages",
            "send",
            "--chat-id",
            "-100",
            "--text",
            "hello",
            "--media-group",
            "0=none",
            "--config",
            str(config_file),
        ]
    )

    assert result.exit_code == 2, _cli_output(result)
    assert "--rich-markdown" in _cli_output(result)
    assert backend.sent == []


@pytest.mark.parametrize(
    "entry", ["0", "0=carousel", "first=none", "=none"], ids=["no-mode", "bad-mode", "bad-index", "empty"]
)
def test_cli_media_group_bad_syntax_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, entry: str
) -> None:
    config_file, md_file, backend = _cli_setup(tmp_path, monkeypatch)

    result = _run_cli(_send_args(config_file, md_file, "--media-group", entry))

    assert result.exit_code == 2, _cli_output(result)
    assert "--media-group" in _cli_output(result)
    assert backend.sent == []


def test_cli_media_group_unknown_index_errors_before_sending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The article has one run; index 3 names nothing and must not be dropped."""
    config_file, md_file, backend = _cli_setup(tmp_path, monkeypatch)

    result = _run_cli(_send_args(config_file, md_file, "--media-group", "3=none"))

    assert result.exit_code == 2, _cli_output(result)
    assert "unknown media group index 3" in _cli_output(result)
    assert backend.sent == []


def test_cli_dry_run_lists_media_groups(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_file, md_file, backend = _cli_setup(tmp_path, monkeypatch)

    result = _run_cli(
        _send_args(config_file, md_file, "--media-group", "0=slideshow", "--dry-run")
    )

    assert result.exit_code == 0, _cli_output(result)
    resolved = json.loads(result.stdout.strip().splitlines()[-1])["resolved"]
    assert resolved["media_grouping"] == "collage"
    assert resolved["rich_markdown_groups"] == [
        {
            "index": 0,
            "size": 2,
            "mode": "slideshow",
            "preceding_text": "Пляж был пустой",
        }
    ]
    assert resolved["rich_markdown_media"] == 2
    assert backend.sent == []


def test_cli_dry_run_plain_send_has_no_group_markers(
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
    assert resolved["media_grouping"] is None
    assert resolved["rich_markdown_groups"] is None


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("grouping", "grouped"), [(None, True), ("none", False)], ids=["default", "config_off"]
)
def test_http_rich_send_respects_config_grouping(
    tmp_path: Path, grouping: str | None, grouped: bool
) -> None:
    backend = RecordingBackend()
    app = create_app(
        load_config_from_text(_config_yaml(grouping=grouping)),
        session_manager=None,
        message_backend_factory=lambda _r: backend,
        topic_backend_factory=lambda _r: backend,
        operation_store=OperationStore(tmp_path / "http.db"),
    )
    client = TestClient(app)

    resp = client.post(
        "/telegram/messages",
        json={
            "telegram_chat_id": -100,
            "rich_markdown": ARTICLE,
            "spaced_paragraphs": False,
            "operation_id": f"http-group-{grouping}",
        },
        headers=AUTH,
    )

    assert resp.status_code == 200, resp.text
    sent = backend.sent[0]["rich_markdown"]
    assert ("<tg-collage>" in sent) is grouped
    if not grouped:
        assert sent == ARTICLE
