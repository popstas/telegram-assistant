"""Surface tests for rich-message sends (``rich_markdown``).

Covers the HTTP surface of ``POST /telegram/messages``: the happy path, the
exclusivity rules encoded in ``MessageSendBody._shape`` (text, attachments,
mass mode), the domain-level length bound, and the shared WRITE gate.

Body-shape violations arrive as FastAPI's ``422`` (the model validator runs
before the route), while domain-level ``ValueError``s (empty / oversize
markdown) map to ``400`` through the route's existing handler.
"""

from __future__ import annotations

import tempfile
import textwrap
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from telegram_assistant.config import load_config_from_text
from telegram_assistant.http_api import create_app
from telegram_assistant.messages import MAX_RICH_MARKDOWN_CHARS
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
