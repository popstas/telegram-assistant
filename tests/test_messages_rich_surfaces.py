"""Surface tests for rich-message sends (``rich_markdown``).

Covers the HTTP surface of ``POST /telegram/messages``: the happy path, the
exclusivity rules encoded in ``MessageSendBody._shape`` (text, attachments,
mass mode), the domain-level length bound, and the shared WRITE gate.

Body-shape violations arrive as FastAPI's ``422`` (the model validator runs
before the route), while domain-level ``ValueError``s (empty / oversize
markdown) map to ``400`` through the route's existing handler.

The CLI section covers ``messages send --rich-markdown <file.md>``: the file is
read as UTF-8 and handed to the same domain op, and every input error (bad
combination, missing/unreadable/empty/oversize file) fails fast with exit code
2 before any backend is opened.
"""

from __future__ import annotations

import json
import tempfile
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
from telegram_assistant.messages import (
    MAX_RICH_MARKDOWN_CHARS,
    RichMediaForbidden,
    RichMessageUnsupported,
)
from telegram_assistant.persistence import OperationStore
from telegram_assistant.topics import TopicSummary

AUTH = {"Authorization": "Bearer secret_token"}

SAMPLE_MARKDOWN = "# Title\n\nBody paragraph.\n\n> quote\n"


class RecordingMessageBackend:
    """MessageBackend fake recording every kwarg the service passes."""

    def __init__(self, *, topics_per_chat: dict[int, list[TopicSummary]] | None = None) -> None:
        self._topics_per_chat = topics_per_chat or {}
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
                "files": tuple(files),
                "schedule_at": schedule_at,
                "reply_to_message_id": reply_to_message_id,
                "rich_markdown": rich_markdown,
            }
        )
        return 777

    async def list_topics(self, *, chat_id: int) -> list[TopicSummary]:
        return list(self._topics_per_chat.get(chat_id, []))


def _config_yaml(access_block: str | None = None) -> str:
    base = textwrap.dedent(
        """
        telegram:
          api_id: 123456
          api_hash: "telegram_api_hash"
          session_path: /data/telegram-assistant.session
          default_chat_folder:
            folder_id: 2
            folder_name: "Planfix clients"
        {access}
        http:
          host: "0.0.0.0"
          port: 8085
          bearer_token: "secret_token"
        logging:
          level: INFO
        """
    )
    indented = ""
    if access_block is not None:
        indented = textwrap.indent(access_block, "  ")
    return base.format(access=indented).strip()


def _make_store() -> OperationStore:
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".db")
    tmp.close()
    return OperationStore(Path(tmp.name))


def _client(
    backend: RecordingMessageBackend,
    *,
    access_block: str | None = None,
    folder_backend: Any = None,
) -> TestClient:
    config = load_config_from_text(_config_yaml(access_block))
    app = create_app(
        config,
        session_manager=None,
        message_backend_factory=lambda _r: backend,
        topic_backend_factory=lambda _r: backend,
        folder_backend_factory=(lambda _r: folder_backend),
        operation_store=_make_store(),
    )
    return TestClient(app)


# ---------------------------------------------------------------------------
# HTTP — happy paths
# ---------------------------------------------------------------------------


def test_http_rich_send_passes_markdown_to_backend() -> None:
    backend = RecordingMessageBackend()
    client = _client(backend)
    resp = client.post(
        "/telegram/messages",
        json={
            "telegram_chat_id": -100,
            "rich_markdown": SAMPLE_MARKDOWN,
            "operation_id": "rich-http-1",
        },
        headers=AUTH,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["mode"] == "targeted"
    assert body["telegram_chat_id"] == -100
    assert body["telegram_message_id"] == 777
    assert backend.sent[0]["rich_markdown"] == SAMPLE_MARKDOWN
    assert backend.sent[0]["text"] == ""


def test_http_rich_send_allows_topic_reply_and_schedule() -> None:
    backend = RecordingMessageBackend()
    client = _client(backend)
    resp = client.post(
        "/telegram/messages",
        json={
            "telegram_chat_id": -100,
            "rich_markdown": SAMPLE_MARKDOWN,
            "telegram_topic_id": 42,
            "reply_to_message_id": 7,
            "schedule_at": "2030-01-01T00:00:00+00:00",
            "operation_id": "rich-http-2",
        },
        headers=AUTH,
    )
    assert resp.status_code == 200, resp.text
    call = backend.sent[0]
    assert call["rich_markdown"] == SAMPLE_MARKDOWN
    assert call["topic_id"] == 42
    assert call["reply_to_message_id"] == 7
    assert call["schedule_at"] is not None


def test_http_plain_send_still_omits_rich_markdown() -> None:
    """A text send must not start passing ``rich_markdown=None`` downstream."""
    backend = RecordingMessageBackend()
    client = _client(backend)
    resp = client.post(
        "/telegram/messages",
        json={"telegram_chat_id": -100, "text": "hi", "operation_id": "plain-1"},
        headers=AUTH,
    )
    assert resp.status_code == 200, resp.text
    assert backend.sent[0]["rich_markdown"] is None


def test_http_rich_send_on_old_telethon_names_the_version() -> None:
    """An old Telethon is a deployment problem: 500 (not the 409
    ``previous_attempt_failed`` taxonomy), but the body must carry the version
    hint — a bare ``RuntimeError`` would surface as Starlette's empty 500 and the
    caller would never learn the fix is upgrading Telethon."""

    class OldTelethonBackend(RecordingMessageBackend):
        async def send_message(self, **kwargs: Any) -> int:
            raise RichMessageUnsupported(
                "rich message send requires telethon>=1.44 (layer 227)"
            )

    client = _client(OldTelethonBackend())
    resp = client.post(
        "/telegram/messages",
        json={
            "telegram_chat_id": -100,
            "rich_markdown": SAMPLE_MARKDOWN,
            "operation_id": "rich-http-old-telethon",
        },
        headers=AUTH,
    )
    assert resp.status_code == 500, resp.text
    detail = resp.json()["detail"]
    assert detail["error"] == "rich_message_unsupported"
    assert "telethon>=1.44" in detail["message"]


# ---------------------------------------------------------------------------
# HTTP — exclusivity (body shape → 422)
# ---------------------------------------------------------------------------


def test_http_rich_send_rejects_text() -> None:
    backend = RecordingMessageBackend()
    client = _client(backend)
    resp = client.post(
        "/telegram/messages",
        json={
            "telegram_chat_id": -100,
            "text": "hi",
            "rich_markdown": SAMPLE_MARKDOWN,
        },
        headers=AUTH,
    )
    assert resp.status_code == 422, resp.text
    assert "rich_markdown" in resp.text
    assert backend.sent == []


def test_http_rich_send_rejects_attachments() -> None:
    backend = RecordingMessageBackend()
    client = _client(backend)
    resp = client.post(
        "/telegram/messages",
        json={
            "telegram_chat_id": -100,
            "rich_markdown": SAMPLE_MARKDOWN,
            "file_urls": ["https://example.com/a.jpg"],
        },
        headers=AUTH,
    )
    assert resp.status_code == 422, resp.text
    assert "rich_markdown" in resp.text
    assert backend.sent == []


def test_http_rich_send_rejects_base64_attachments() -> None:
    backend = RecordingMessageBackend()
    client = _client(backend)
    resp = client.post(
        "/telegram/messages",
        json={
            "telegram_chat_id": -100,
            "rich_markdown": SAMPLE_MARKDOWN,
            "base64_files": [{"filename": "a.txt", "content_b64": "aGk="}],
        },
        headers=AUTH,
    )
    assert resp.status_code == 422, resp.text
    assert "rich_markdown" in resp.text
    assert backend.sent == []


def test_http_rich_send_rejects_mass_mode() -> None:
    backend = RecordingMessageBackend()
    client = _client(backend)
    resp = client.post(
        "/telegram/messages",
        json={
            "folder_name": "Planfix clients",
            "topic_name": "Daily",
            "rich_markdown": SAMPLE_MARKDOWN,
        },
        headers=AUTH,
    )
    assert resp.status_code == 422, resp.text
    assert "rich_markdown" in resp.text
    assert backend.sent == []


# ---------------------------------------------------------------------------
# HTTP — domain-level validation (→ 400)
# ---------------------------------------------------------------------------


def test_http_rich_send_rejects_oversize_markdown() -> None:
    backend = RecordingMessageBackend()
    client = _client(backend)
    resp = client.post(
        "/telegram/messages",
        json={
            "telegram_chat_id": -100,
            "rich_markdown": "x" * (MAX_RICH_MARKDOWN_CHARS + 1),
            "operation_id": "rich-too-long",
        },
        headers=AUTH,
    )
    assert resp.status_code == 400, resp.text
    assert "32768" in resp.text
    assert backend.sent == []


def test_http_rich_send_rejects_blank_markdown() -> None:
    backend = RecordingMessageBackend()
    client = _client(backend)
    resp = client.post(
        "/telegram/messages",
        json={
            "telegram_chat_id": -100,
            "rich_markdown": "   \n",
            "operation_id": "rich-blank",
        },
        headers=AUTH,
    )
    assert resp.status_code == 400, resp.text
    assert backend.sent == []


# ---------------------------------------------------------------------------
# HTTP — access gate
# ---------------------------------------------------------------------------


def test_http_rich_send_denied_without_write() -> None:
    backend = RecordingMessageBackend()
    client = _client(
        backend,
        access_block="access:\n  rules:\n    - all: true\n      permission: read\n",
    )
    resp = client.post(
        "/telegram/messages",
        json={
            "telegram_chat_id": -100,
            "rich_markdown": SAMPLE_MARKDOWN,
            "operation_id": "rich-denied",
        },
        headers=AUTH,
    )
    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"]["error"] == "access_denied"
    assert backend.sent == []


def test_http_rich_send_allowed_by_wildcard_write() -> None:
    backend = RecordingMessageBackend()
    client = _client(
        backend,
        access_block="access:\n  rules:\n    - all: true\n      permission: write\n",
    )
    resp = client.post(
        "/telegram/messages",
        json={
            "telegram_chat_id": -100,
            "rich_markdown": SAMPLE_MARKDOWN,
            "operation_id": "rich-allowed",
        },
        headers=AUTH,
    )
    assert resp.status_code == 200, resp.text
    assert backend.sent[0]["rich_markdown"] == SAMPLE_MARKDOWN


# ---------------------------------------------------------------------------
# CLI — helpers
# ---------------------------------------------------------------------------


class CliLegacyMessageBackend:
    """A backend whose signature predates ``rich_markdown``.

    Pins the only-when-set contract through the CLI: a plain ``--text`` send
    must not start passing ``rich_markdown=None`` down, or this raises
    ``TypeError``.
    """

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def send_message(self, *, chat_id: int, text: str, topic_id: int | None = None) -> int:
        self.sent.append({"chat_id": chat_id, "text": text, "topic_id": topic_id})
        return 555

    async def list_topics(self, *, chat_id: int) -> list[TopicSummary]:
        return []


class CliFolderBackend:
    def __init__(self) -> None:
        self.snapshot = FolderSnapshot(
            folder_id=2,
            folder_name="Planfix clients",
            chats=[FolderChat(chat_id=-100, title="Client chat")],
        )

    async def list_folders(self) -> list[FolderSnapshot]:
        return [self.snapshot]


def _patch_cli_backends(
    monkeypatch: pytest.MonkeyPatch,
    backend: Any,
    store: OperationStore,
) -> None:
    class _FakeManager:
        async def disconnect(self) -> None:
            return None

    def _factory(config_path: Path | None) -> Any:
        from telegram_assistant.config import load_config

        config = load_config(config_path)
        folder_backend = CliFolderBackend()

        async def _open() -> Any:
            # Production returns (message_backend, topic_backend, folder_backend);
            # the fake doubles as message and topic backend.
            return backend, backend, folder_backend

        return config, _FakeManager(), store, _open

    monkeypatch.setattr(cli_main, "_build_message_backends", _factory)


def _write_config(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "config.yml"
    path.write_text(body)
    return path


def _write_markdown(tmp_path: Path, body: str, name: str = "article.md") -> Path:
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return path


def _run_cli(args: list[str]) -> Any:
    return CliRunner().invoke(cli_main.app, args)


def _cli_output(result: Any) -> str:
    """Click 8.3 keeps stderr separate; error messages go there."""
    return (result.stdout or "") + (result.stderr or "")


# ---------------------------------------------------------------------------
# CLI — happy paths
# ---------------------------------------------------------------------------


def test_cli_rich_send_reads_file_and_passes_markdown(
    minimal_config_yaml: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_file = _write_config(tmp_path, minimal_config_yaml)
    # Non-ASCII body: the file must be read as UTF-8, not the platform default.
    markdown = "# Заголовок\n\n> цитата — 🚀\n"
    md_file = _write_markdown(tmp_path, markdown)
    backend = RecordingMessageBackend()
    store = OperationStore(tmp_path / "state.db")
    _patch_cli_backends(monkeypatch, backend, store)

    result = _run_cli(
        [
            "messages",
            "send",
            "--chat-id",
            "-100",
            "--rich-markdown",
            str(md_file),
            "--operation-id",
            "cli-rich-1",
            "--config",
            str(config_file),
        ]
    )
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload["mode"] == "targeted"
    assert payload["telegram_message_id"] == 777
    assert backend.sent[0]["rich_markdown"] == markdown
    assert backend.sent[0]["text"] == ""


def test_cli_rich_send_allows_topic_and_schedule(
    minimal_config_yaml: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_file = _write_config(tmp_path, minimal_config_yaml)
    md_file = _write_markdown(tmp_path, SAMPLE_MARKDOWN)
    backend = RecordingMessageBackend(
        topics_per_chat={-100: [TopicSummary(topic_id=42, title="Documents")]}
    )
    store = OperationStore(tmp_path / "state.db")
    _patch_cli_backends(monkeypatch, backend, store)

    result = _run_cli(
        [
            "messages",
            "send",
            "--chat-id",
            "-100",
            "--topic-name",
            "Documents",
            "--rich-markdown",
            str(md_file),
            "--delay",
            "10m",
            "--operation-id",
            "cli-rich-2",
            "--config",
            str(config_file),
        ]
    )
    assert result.exit_code == 0, result.stdout
    call = backend.sent[0]
    assert call["rich_markdown"] == SAMPLE_MARKDOWN
    assert call["topic_id"] == 42
    assert call["schedule_at"] is not None


def test_cli_plain_send_still_omits_rich_markdown(
    minimal_config_yaml: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_file = _write_config(tmp_path, minimal_config_yaml)
    backend = CliLegacyMessageBackend()
    store = OperationStore(tmp_path / "state.db")
    _patch_cli_backends(monkeypatch, backend, store)

    result = _run_cli(
        [
            "messages",
            "send",
            "--chat-id",
            "-100",
            "--text",
            "hello",
            "--operation-id",
            "cli-plain-1",
            "--config",
            str(config_file),
        ]
    )
    assert result.exit_code == 0, result.stdout
    assert backend.sent[0]["text"] == "hello"


# ---------------------------------------------------------------------------
# CLI — exclusivity (exit code 2, nothing sent)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "extra",
    [
        pytest.param(["--text", "hello"], id="text"),
        pytest.param(["--file", "/tmp/whatever.jpg"], id="file"),
        pytest.param(["--file-url", "https://example.com/a.jpg"], id="file_url"),
    ],
)
def test_cli_rich_send_rejects_conflicting_inputs(
    minimal_config_yaml: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    extra: list[str],
) -> None:
    config_file = _write_config(tmp_path, minimal_config_yaml)
    md_file = _write_markdown(tmp_path, SAMPLE_MARKDOWN)
    backend = RecordingMessageBackend()
    store = OperationStore(tmp_path / "state.db")
    _patch_cli_backends(monkeypatch, backend, store)

    result = _run_cli(
        [
            "messages",
            "send",
            "--chat-id",
            "-100",
            "--rich-markdown",
            str(md_file),
            *extra,
            "--config",
            str(config_file),
        ]
    )
    assert result.exit_code == 2, _cli_output(result)
    assert "--rich-markdown" in _cli_output(result)
    assert backend.sent == []


def test_cli_rich_send_rejects_mass_mode(
    minimal_config_yaml: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_file = _write_config(tmp_path, minimal_config_yaml)
    md_file = _write_markdown(tmp_path, SAMPLE_MARKDOWN)
    backend = RecordingMessageBackend()
    store = OperationStore(tmp_path / "state.db")
    _patch_cli_backends(monkeypatch, backend, store)

    result = _run_cli(
        [
            "messages",
            "send",
            "--topic-name",
            "Daily",
            "--rich-markdown",
            str(md_file),
            "--config",
            str(config_file),
        ]
    )
    assert result.exit_code == 2, _cli_output(result)
    assert "mass mode" in _cli_output(result)
    assert backend.sent == []


# ---------------------------------------------------------------------------
# CLI — file errors (exit code 2, nothing sent)
# ---------------------------------------------------------------------------


def test_cli_rich_send_missing_file_errors(
    minimal_config_yaml: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_file = _write_config(tmp_path, minimal_config_yaml)
    backend = RecordingMessageBackend()
    store = OperationStore(tmp_path / "state.db")
    _patch_cli_backends(monkeypatch, backend, store)

    result = _run_cli(
        [
            "messages",
            "send",
            "--chat-id",
            "-100",
            "--rich-markdown",
            str(tmp_path / "nope.md"),
            "--config",
            str(config_file),
        ]
    )
    assert result.exit_code == 2, _cli_output(result)
    assert "--rich-markdown" in _cli_output(result)
    assert backend.sent == []


def test_cli_rich_send_empty_file_errors(
    minimal_config_yaml: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_file = _write_config(tmp_path, minimal_config_yaml)
    md_file = _write_markdown(tmp_path, "   \n\n")
    backend = RecordingMessageBackend()
    store = OperationStore(tmp_path / "state.db")
    _patch_cli_backends(monkeypatch, backend, store)

    result = _run_cli(
        [
            "messages",
            "send",
            "--chat-id",
            "-100",
            "--rich-markdown",
            str(md_file),
            "--config",
            str(config_file),
        ]
    )
    assert result.exit_code == 2, _cli_output(result)
    assert "empty" in _cli_output(result)
    assert backend.sent == []


def test_cli_rich_send_non_utf8_file_errors(
    minimal_config_yaml: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_file = _write_config(tmp_path, minimal_config_yaml)
    md_file = tmp_path / "latin1.md"
    md_file.write_bytes(b"# Titre\n\ncaf\xe9\n")
    backend = RecordingMessageBackend()
    store = OperationStore(tmp_path / "state.db")
    _patch_cli_backends(monkeypatch, backend, store)

    result = _run_cli(
        [
            "messages",
            "send",
            "--chat-id",
            "-100",
            "--rich-markdown",
            str(md_file),
            "--config",
            str(config_file),
        ]
    )
    assert result.exit_code == 2, _cli_output(result)
    assert "UTF-8" in _cli_output(result)
    assert backend.sent == []


def test_cli_rich_send_oversize_file_errors(
    minimal_config_yaml: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_file = _write_config(tmp_path, minimal_config_yaml)
    md_file = _write_markdown(tmp_path, "x" * (MAX_RICH_MARKDOWN_CHARS + 1))
    backend = RecordingMessageBackend()
    store = OperationStore(tmp_path / "state.db")
    _patch_cli_backends(monkeypatch, backend, store)

    result = _run_cli(
        [
            "messages",
            "send",
            "--chat-id",
            "-100",
            "--rich-markdown",
            str(md_file),
            "--config",
            str(config_file),
        ]
    )
    assert result.exit_code == 2, _cli_output(result)
    assert str(MAX_RICH_MARKDOWN_CHARS) in _cli_output(result)
    assert backend.sent == []


def test_cli_rich_send_strips_utf8_bom(
    minimal_config_yaml: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A BOM-prefixed file must not turn the first heading into plain text."""
    config_file = _write_config(tmp_path, minimal_config_yaml)
    md_file = tmp_path / "bom.md"
    md_file.write_text(SAMPLE_MARKDOWN, encoding="utf-8-sig")
    assert md_file.read_bytes().startswith(b"\xef\xbb\xbf")
    backend = RecordingMessageBackend()
    store = OperationStore(tmp_path / "state.db")
    _patch_cli_backends(monkeypatch, backend, store)

    result = _run_cli(
        [
            "messages",
            "send",
            "--chat-id",
            "-100",
            "--rich-markdown",
            str(md_file),
            "--config",
            str(config_file),
        ]
    )
    assert result.exit_code == 0, _cli_output(result)
    assert backend.sent[0]["rich_markdown"] == SAMPLE_MARKDOWN


def test_cli_rich_send_strips_obsidian_frontmatter(
    minimal_config_yaml: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A real Obsidian note opens with YAML frontmatter, which this dialect
    would otherwise render as a divider plus a heading reading its metadata."""
    config_file = _write_config(tmp_path, minimal_config_yaml)
    md_file = tmp_path / "note.md"
    md_file.write_text(
        "---\ntags: [travel, phuket]\ndate: 2026-07-27\n---\n\n" + SAMPLE_MARKDOWN,
        encoding="utf-8",
    )
    backend = RecordingMessageBackend()
    store = OperationStore(tmp_path / "state.db")
    _patch_cli_backends(monkeypatch, backend, store)

    result = _run_cli(
        [
            "messages",
            "send",
            "--chat-id",
            "-100",
            "--rich-markdown",
            str(md_file),
            "--no-spaced-paragraphs",
            "--config",
            str(config_file),
        ]
    )
    assert result.exit_code == 0, _cli_output(result)
    sent = backend.sent[0]["rich_markdown"]
    assert "tags:" not in sent
    assert sent.lstrip("\n") == SAMPLE_MARKDOWN


def test_cli_rich_send_rejects_a_note_that_is_only_frontmatter(
    minimal_config_yaml: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stripping runs before the emptiness check, so this is 'empty', not a
    send of a bare metadata heading."""
    config_file = _write_config(tmp_path, minimal_config_yaml)
    md_file = tmp_path / "meta.md"
    md_file.write_text("---\ntags: [a]\n---\n", encoding="utf-8")
    backend = RecordingMessageBackend()
    store = OperationStore(tmp_path / "state.db")
    _patch_cli_backends(monkeypatch, backend, store)

    result = _run_cli(
        [
            "messages",
            "send",
            "--chat-id",
            "-100",
            "--rich-markdown",
            str(md_file),
            "--config",
            str(config_file),
        ]
    )
    assert result.exit_code == 2, _cli_output(result)
    assert "is empty" in _cli_output(result)
    assert backend.sent == []


def test_cli_dry_run_without_any_body_errors(
    minimal_config_yaml: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The dry-run emptiness guard now also accepts ``--rich-markdown``; with
    none of the three it must still fail fast."""
    config_file = _write_config(tmp_path, minimal_config_yaml)
    backend = RecordingMessageBackend()
    store = OperationStore(tmp_path / "state.db")
    _patch_cli_backends(monkeypatch, backend, store)

    result = _run_cli(
        [
            "messages",
            "send",
            "--chat-id",
            "-100",
            "--dry-run",
            "--config",
            str(config_file),
        ]
    )
    assert result.exit_code == 2, _cli_output(result)
    assert "--rich-markdown" in _cli_output(result)
    assert backend.sent == []


# ---------------------------------------------------------------------------
# CLI — domain errors are caller input, not crashes (exit 2, never exit 1)
# ---------------------------------------------------------------------------


def test_cli_rich_send_media_forbidden_exits_2(
    minimal_config_yaml: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``RichMediaForbidden`` is a ``ValueError`` precisely so every surface
    reports it as bad input — HTTP 400, CLI exit 2 — with the message naming the
    chat. Landing on the generic handler would exit 1 with a ``messages send
    failed:`` prefix and read as an internal error."""

    class ForbiddenBackend(RecordingMessageBackend):
        async def send_message(self, **kwargs: Any) -> int:
            raise RichMediaForbidden(
                "chat -100 does not allow the media in this rich message: "
                "ChatSendMediaForbiddenError: nope"
            )

    config_file = _write_config(tmp_path, minimal_config_yaml)
    md_file = _write_markdown(tmp_path, SAMPLE_MARKDOWN)
    backend = ForbiddenBackend()
    store = OperationStore(tmp_path / "state.db")
    _patch_cli_backends(monkeypatch, backend, store)

    result = _run_cli(
        [
            "messages",
            "send",
            "--chat-id",
            "-100",
            "--rich-markdown",
            str(md_file),
            "--config",
            str(config_file),
        ]
    )
    assert result.exit_code == 2, _cli_output(result)
    output = _cli_output(result)
    assert "does not allow the media" in output
    assert "messages send failed" not in output


def test_http_rich_send_media_forbidden_is_400() -> None:
    class ForbiddenBackend(RecordingMessageBackend):
        async def send_message(self, **kwargs: Any) -> int:
            raise RichMediaForbidden(
                "chat -100 does not allow the media in this rich message: "
                "ChatSendPhotosForbiddenError: nope"
            )

    backend = ForbiddenBackend()
    client = _client(backend)
    resp = client.post(
        "/telegram/messages",
        json={
            "telegram_chat_id": -100,
            "rich_markdown": SAMPLE_MARKDOWN,
            "operation_id": "rich-forbidden-1",
        },
        headers=AUTH,
    )
    assert resp.status_code == 400, resp.text
    assert "does not allow the media" in resp.text


def test_cli_rich_send_over_limit_after_normalization_exits_2(
    minimal_config_yaml: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A source that fits but grows past the bound once spacers are inserted is
    rejected by the domain op — the same bad-input class as the CLI's own
    pre-check, so the same exit code, and the message must name the pass."""
    config_file = _write_config(tmp_path, minimal_config_yaml)
    # 250 paragraphs: 249 spacers (499 blocks, just inside the 500-block budget
    # that would otherwise roll the pass back) add 3 characters each, which is
    # more than the headroom the source leaves.
    markdown = "\n\n".join("a" * 128 for _ in range(250)) + "\n"
    assert len(markdown) <= MAX_RICH_MARKDOWN_CHARS
    assert len(markdown) + 249 * 3 > MAX_RICH_MARKDOWN_CHARS
    md_file = _write_markdown(tmp_path, markdown)
    backend = RecordingMessageBackend()
    store = OperationStore(tmp_path / "state.db")
    _patch_cli_backends(monkeypatch, backend, store)

    result = _run_cli(
        [
            "messages",
            "send",
            "--chat-id",
            "-100",
            "--rich-markdown",
            str(md_file),
            "--config",
            str(config_file),
        ]
    )
    assert result.exit_code == 2, _cli_output(result)
    output = _cli_output(result)
    assert str(MAX_RICH_MARKDOWN_CHARS) in output
    assert "paragraph spacing" in output
    assert backend.sent == []


# ---------------------------------------------------------------------------
# Local media stays CLI-only
# ---------------------------------------------------------------------------


def test_http_rich_send_never_resolves_a_local_media_path(tmp_path: Path) -> None:
    """A remote caller must not be able to name a server-side file.

    Only the CLI runs ``scan_media``; the HTTP route hands the body down as
    written, so a local path stays a local path (Telegram rejects it) and no
    upload is ever produced. Were the surfaces ever "unified", this would catch
    it: a READ-scoped caller would otherwise gain server-side file reads.
    """
    secret = tmp_path / "secret.png"
    secret.write_bytes(b"\x89PNG")
    markdown = f"# T\n\n![]({secret})\n"
    backend = RecordingMessageBackend()
    client = _client(backend)
    resp = client.post(
        "/telegram/messages",
        json={
            "telegram_chat_id": -100,
            "rich_markdown": markdown,
            "operation_id": "rich-local-media-1",
        },
        headers=AUTH,
    )
    assert resp.status_code == 200, resp.text
    sent = backend.sent[0]["rich_markdown"]
    assert str(secret) in sent
    assert "tg://photo" not in sent
    # The legacy-signature fake would raise TypeError on a ``rich_files`` kwarg;
    # reaching this line at all proves none was passed.
