"""Tests for the telegram_ FastMCP tools."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from telegram_assistant.config import load_config_from_text
from telegram_assistant.http_api import create_app
from telegram_assistant.messages import RecentMessage
from telegram_assistant.persistence import OperationStore
from tests.test_mcp_mount import (
    FakeGoogleOidcProvider,
    FakeSessionManager,
    _enabled_mcp_yaml,
    _initialize_payload,
    _mcp_headers,
    _mint_token,
)


class FakeReadBackend:
    def __init__(self, messages: list[RecentMessage]) -> None:
        self._messages = messages
        self.calls: list[dict[str, int]] = []

    async def get_recent_messages(
        self, *, chat_id: int, limit: int = 5
    ) -> list[RecentMessage]:
        self.calls.append({"chat_id": chat_id, "limit": limit})
        return self._messages[:limit]


class FakeMessageBackend:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def send_message(
        self,
        *,
        chat_id: int,
        text: str,
        topic_id: int | None = None,
        files: tuple[str, ...] = (),
        schedule_at: object | None = None,
    ) -> int:
        self.sent.append(
            {
                "chat_id": chat_id,
                "text": text,
                "topic_id": topic_id,
                "files": files,
                "schedule_at": schedule_at,
            }
        )
        return 777


def _messages() -> list[RecentMessage]:
    return [
        RecentMessage(id=1, sender="alice", date=None, reply_to=None, text="one"),
        RecentMessage(id=2, sender="bob", date=None, reply_to=1, text="two"),
    ]


def _with_access(minimal_config_yaml: str, access_block: str) -> str:
    return minimal_config_yaml.replace(
        "  defaults:\n",
        f"  access:\n{access_block}  defaults:\n",
        1,
    )


def _client(
    config_yaml: str,
    tmp_path: Path,
    *,
    read_backend: FakeReadBackend | None = None,
    message_backend: FakeMessageBackend | None = None,
) -> TestClient:
    config = load_config_from_text(_enabled_mcp_yaml(config_yaml))
    app = create_app(
        config,
        session_manager=FakeSessionManager(),  # type: ignore[arg-type]
        mcp_google_provider=FakeGoogleOidcProvider(),
        folder_backend_factory=lambda _r: None,
        message_read_backend_factory=(
            (lambda _r: read_backend) if read_backend is not None else (lambda _r: None)
        ),
        message_backend_factory=(
            (lambda _r: message_backend)
            if message_backend is not None
            else (lambda _r: None)
        ),
        operation_store=OperationStore(tmp_path / "state.db"),
        resolver_factory=lambda _r: None,
    )
    return TestClient(app)


def _initialize(client: TestClient, token: str) -> None:
    headers = _mcp_headers(token)
    initialize = client.post("/mcp", json=_initialize_payload(), headers=headers)
    assert initialize.status_code == 200, initialize.text
    initialized = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        headers=headers,
    )
    assert initialized.status_code == 202, initialized.text


def _call_tool(
    client: TestClient, token: str, name: str, arguments: dict[str, Any]
) -> dict[str, Any]:
    response = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 7,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
        headers=_mcp_headers(token),
    )
    assert response.status_code == 200, response.text
    return response.json()["result"]


def test_mcp_recent_messages_reads_via_backend(
    minimal_config_yaml: str, tmp_path: Path
) -> None:
    backend = FakeReadBackend(_messages())
    with _client(minimal_config_yaml, tmp_path, read_backend=backend) as client:
        token = _mint_token(client)
        _initialize(client, token)

        result = _call_tool(
            client,
            token,
            "telegram_messages_recent",
            {"chat_id": -100123, "limit": 1},
        )

    assert result["isError"] is False
    payload = result["structuredContent"]
    assert payload["telegram_chat_id"] == -100123
    assert payload["count"] == 1
    assert payload["messages"][0]["text"] == "one"
    assert backend.calls == [{"chat_id": -100123, "limit": 1}]


def test_mcp_send_message_uses_operation_store_idempotency(
    minimal_config_yaml: str, tmp_path: Path
) -> None:
    backend = FakeMessageBackend()
    with _client(minimal_config_yaml, tmp_path, message_backend=backend) as client:
        token = _mint_token(client)
        _initialize(client, token)

        first = _call_tool(
            client,
            token,
            "telegram_messages_send",
            {
                "telegram_chat_id": -100123,
                "text": "hello",
                "operation_id": "mcp-send-1",
            },
        )
        second = _call_tool(
            client,
            token,
            "telegram_messages_send",
            {
                "telegram_chat_id": -100123,
                "text": "hello",
                "operation_id": "mcp-send-1",
            },
        )

    assert first["isError"] is False
    assert second["isError"] is False
    assert first["structuredContent"]["telegram_message_id"] == 777
    assert second["structuredContent"]["replayed"] is True
    assert len(backend.sent) == 1


def test_mcp_tool_maps_access_denied_to_actionable_error(
    minimal_config_yaml: str, tmp_path: Path
) -> None:
    backend = FakeReadBackend(_messages())
    config_yaml = _with_access(minimal_config_yaml, "    rules: []\n")
    with _client(config_yaml, tmp_path, read_backend=backend) as client:
        token = _mint_token(client)
        _initialize(client, token)

        result = _call_tool(
            client,
            token,
            "telegram_messages_recent",
            {"chat_id": -100123},
        )

    assert result["isError"] is True
    text = result["content"][0]["text"]
    assert '"error": "access_denied"' in text
    assert '"status": 403' in text
    assert backend.calls == []


def test_mcp_tool_maps_backend_unavailable_to_actionable_error(
    minimal_config_yaml: str, tmp_path: Path
) -> None:
    with _client(minimal_config_yaml, tmp_path) as client:
        token = _mint_token(client)
        _initialize(client, token)

        result = _call_tool(
            client,
            token,
            "telegram_messages_recent",
            {"chat_id": -100123},
        )

    assert result["isError"] is True
    text = result["content"][0]["text"]
    assert '"error": "backend_unavailable"' in text
    assert '"status": 503' in text
