"""Tests for Task 11 — search surfaces (CLI, HTTP).

The search *domain* op is covered in ``test_messages_search.py``; this module
exercises the wiring through the CLI ``messages search`` command and the HTTP
``GET /telegram/messages/search`` endpoint (status codes incl. 503/403/400,
entity resolution, and validation errors). MCP wiring lives in
``test_mcp_tools.py``.
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
from telegram_assistant.entities import ResolvedEntity
from telegram_assistant.http_api import create_app
from telegram_assistant.messages import RecentMessage
from telegram_assistant.persistence import OperationStore

AUTH = {"Authorization": "Bearer secret_token"}


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeSearchBackend:
    def __init__(self, messages: list[RecentMessage]) -> None:
        self._messages = messages
        self.calls: list[dict[str, Any]] = []

    async def search_messages(
        self,
        *,
        chat_id: int,
        query: str,
        from_user: str | int | None = None,
        limit: int = 20,
        topic_id: int | None = None,
    ) -> list[RecentMessage]:
        self.calls.append(
            {
                "chat_id": chat_id,
                "query": query,
                "from_user": from_user,
                "limit": limit,
                "topic_id": topic_id,
            }
        )
        return self._messages[:limit]


class FakeResolver:
    def __init__(self, mapping: dict[str, int]) -> None:
        self._mapping = mapping

    async def resolve(self, ref: object) -> ResolvedEntity:
        return ResolvedEntity(
            chat_id=self._mapping[str(ref)], title=str(ref), kind="channel"
        )


def _messages(n: int) -> list[RecentMessage]:
    return [
        RecentMessage(
            id=i, sender=f"u{i}", date=None, reply_to=None, text=f"needle {i}"
        )
        for i in range(1, n + 1)
    ]


def _config_with_access(access_block: str | None) -> str:
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


_READ_ACCESS = "access:\n  rules:\n    - all: true\n      permission: read\n"
_WRITE_ACCESS = "access:\n  rules:\n    - all: true\n      permission: write\n"


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


def _http_client(
    *,
    access_block: str | None = None,
    search_backend: FakeSearchBackend | None = None,
    resolver: FakeResolver | None = None,
    has_search_factory: bool = True,
) -> TestClient:
    config = load_config_from_text(_config_with_access(access_block))
    app = create_app(
        config,
        session_manager=None,
        search_backend_factory=(
            (lambda _r: search_backend)
            if has_search_factory
            else (lambda _r: None)
        ),
        folder_backend_factory=lambda _r: None,
        resolver_factory=(lambda _r: resolver) if resolver is not None else (lambda _r: None),
        operation_store=_make_store(),
    )
    return TestClient(app)


def test_http_search_returns_rows() -> None:
    backend = FakeSearchBackend(_messages(3))
    client = _http_client(access_block=_READ_ACCESS, search_backend=backend)
    resp = client.get(
        "/telegram/messages/search",
        params={"chat_id": -100123, "query": "needle", "limit": 2},
        headers=AUTH,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["telegram_chat_id"] == -100123
    assert body["query"] == "needle"
    assert body["count"] == 2
    assert backend.calls == [
        {
            "chat_id": -100123,
            "query": "needle",
            "from_user": None,
            "limit": 2,
            "topic_id": None,
        }
    ]


def test_http_search_passes_optional_args() -> None:
    backend = FakeSearchBackend(_messages(3))
    client = _http_client(access_block=_READ_ACCESS, search_backend=backend)
    resp = client.get(
        "/telegram/messages/search",
        params={
            "chat_id": -100123,
            "query": "needle",
            "from_user": "@bob",
            "topic_id": 7,
        },
        headers=AUTH,
    )
    assert resp.status_code == 200, resp.text
    assert backend.calls[-1]["from_user"] == "@bob"
    assert backend.calls[-1]["topic_id"] == 7


def test_http_search_via_entity() -> None:
    backend = FakeSearchBackend(_messages(2))
    resolver = FakeResolver({"@client": -100777})
    client = _http_client(
        access_block=_READ_ACCESS, search_backend=backend, resolver=resolver
    )
    resp = client.get(
        "/telegram/messages/search",
        params={"entity": "@client", "query": "needle"},
        headers=AUTH,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["telegram_chat_id"] == -100777
    assert backend.calls[-1]["chat_id"] == -100777


def test_http_search_requires_exactly_one_ref() -> None:
    backend = FakeSearchBackend(_messages(3))
    client = _http_client(access_block=_READ_ACCESS, search_backend=backend)
    resp = client.get(
        "/telegram/messages/search", params={"query": "needle"}, headers=AUTH
    )
    assert resp.status_code == 400, resp.text
    resp2 = client.get(
        "/telegram/messages/search",
        params={"chat_id": -1, "entity": "@x", "query": "needle"},
        headers=AUTH,
    )
    assert resp2.status_code == 400


def test_http_search_rejects_empty_query() -> None:
    backend = FakeSearchBackend(_messages(3))
    client = _http_client(access_block=_READ_ACCESS, search_backend=backend)
    resp = client.get(
        "/telegram/messages/search",
        params={"chat_id": -100123, "query": "   "},
        headers=AUTH,
    )
    assert resp.status_code == 400, resp.text
    assert backend.calls == []


def test_http_search_missing_query_422() -> None:
    backend = FakeSearchBackend(_messages(3))
    client = _http_client(access_block=_READ_ACCESS, search_backend=backend)
    resp = client.get(
        "/telegram/messages/search",
        params={"chat_id": -100123},
        headers=AUTH,
    )
    assert resp.status_code == 422, resp.text


def test_http_search_rejects_nonpositive_limit() -> None:
    backend = FakeSearchBackend(_messages(3))
    client = _http_client(access_block=_READ_ACCESS, search_backend=backend)
    resp = client.get(
        "/telegram/messages/search",
        params={"chat_id": -100123, "query": "needle", "limit": 0},
        headers=AUTH,
    )
    assert resp.status_code == 400, resp.text
    assert backend.calls == []


def test_http_search_403_without_read() -> None:
    backend = FakeSearchBackend(_messages(3))
    # write-only rule: read is not implied, so READ is denied.
    client = _http_client(access_block=_WRITE_ACCESS, search_backend=backend)
    resp = client.get(
        "/telegram/messages/search",
        params={"chat_id": -100123, "query": "needle"},
        headers=AUTH,
    )
    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"]["error"] == "access_denied"
    assert backend.calls == []


def test_http_search_503_when_backend_unavailable() -> None:
    client = _http_client(access_block=_READ_ACCESS, has_search_factory=False)
    resp = client.get(
        "/telegram/messages/search",
        params={"chat_id": -100123, "query": "needle"},
        headers=AUTH,
    )
    assert resp.status_code == 503, resp.text


def test_http_search_requires_auth() -> None:
    client = _http_client(
        access_block=_READ_ACCESS, search_backend=FakeSearchBackend(_messages(1))
    )
    resp = client.get(
        "/telegram/messages/search",
        params={"chat_id": -100123, "query": "needle"},
    )
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _write_config(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "config.yml"
    p.write_text(body)
    return p


def _patch_cli_search_backends(
    monkeypatch: pytest.MonkeyPatch,
    backend: FakeSearchBackend,
    *,
    resolver: FakeResolver | None = None,
) -> None:
    class _FakeManager:
        async def disconnect(self) -> None:
            return None

        async def get_client(self) -> Any:
            return object()

    def _factory(config_path: Path | None) -> Any:
        from telegram_assistant.config import load_config

        config = load_config(config_path)

        async def _open() -> Any:
            return backend, None, resolver

        return config, _FakeManager(), _open

    monkeypatch.setattr(cli_main, "_build_search_backends", _factory)


def test_cli_search_happy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_file = _write_config(tmp_path, _config_with_access(_READ_ACCESS))
    backend = FakeSearchBackend(_messages(3))
    _patch_cli_search_backends(monkeypatch, backend)

    runner = CliRunner()
    result = runner.invoke(
        cli_main.app,
        [
            "messages",
            "search",
            "--chat-id",
            "-100123",
            "--query",
            "needle",
            "--limit",
            "2",
            "--config",
            str(config_file),
        ],
    )
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload["telegram_chat_id"] == -100123
    assert payload["query"] == "needle"
    assert payload["count"] == 2
    assert backend.calls == [
        {
            "chat_id": -100123,
            "query": "needle",
            "from_user": None,
            "limit": 2,
            "topic_id": None,
        }
    ]


def test_cli_search_passes_from_and_topic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_file = _write_config(tmp_path, _config_with_access(_READ_ACCESS))
    backend = FakeSearchBackend(_messages(3))
    _patch_cli_search_backends(monkeypatch, backend)

    runner = CliRunner()
    result = runner.invoke(
        cli_main.app,
        [
            "messages",
            "search",
            "--chat-id",
            "-100123",
            "--query",
            "needle",
            "--from",
            "@bob",
            "--topic-id",
            "7",
            "--config",
            str(config_file),
        ],
    )
    assert result.exit_code == 0, result.stdout
    assert backend.calls[-1]["from_user"] == "@bob"
    assert backend.calls[-1]["topic_id"] == 7


def test_cli_search_via_entity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_file = _write_config(tmp_path, _config_with_access(_READ_ACCESS))
    backend = FakeSearchBackend(_messages(2))
    resolver = FakeResolver({"@client": -100777})
    _patch_cli_search_backends(monkeypatch, backend, resolver=resolver)

    runner = CliRunner()
    result = runner.invoke(
        cli_main.app,
        [
            "messages",
            "search",
            "--entity",
            "@client",
            "--query",
            "needle",
            "--config",
            str(config_file),
        ],
    )
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload["telegram_chat_id"] == -100777
    assert backend.calls[-1]["chat_id"] == -100777


def test_cli_search_access_denied_exit_3(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_file = _write_config(tmp_path, _config_with_access(_WRITE_ACCESS))
    backend = FakeSearchBackend(_messages(3))
    _patch_cli_search_backends(monkeypatch, backend)

    runner = CliRunner()
    result = runner.invoke(
        cli_main.app,
        [
            "messages",
            "search",
            "--chat-id",
            "-100123",
            "--query",
            "needle",
            "--config",
            str(config_file),
        ],
    )
    assert result.exit_code == 3, result.stdout
    assert backend.calls == []


def test_cli_search_requires_one_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_file = _write_config(tmp_path, _config_with_access(_READ_ACCESS))
    backend = FakeSearchBackend(_messages(3))
    _patch_cli_search_backends(monkeypatch, backend)

    runner = CliRunner()
    result = runner.invoke(
        cli_main.app,
        [
            "messages",
            "search",
            "--query",
            "needle",
            "--config",
            str(config_file),
        ],
    )
    assert result.exit_code == 2


def test_cli_search_rejects_empty_query(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_file = _write_config(tmp_path, _config_with_access(_READ_ACCESS))
    backend = FakeSearchBackend(_messages(3))
    _patch_cli_search_backends(monkeypatch, backend)

    runner = CliRunner()
    result = runner.invoke(
        cli_main.app,
        [
            "messages",
            "search",
            "--chat-id",
            "-100123",
            "--query",
            "   ",
            "--config",
            str(config_file),
        ],
    )
    assert result.exit_code == 2
