"""Tests for Task 5 — edit surfaces (CLI + HTTP).

The edit-message *domain* op, the ``edit_only_session_messages`` config flag,
and the authorizer resolution are covered in ``test_messages_edit.py`` /
``test_access.py``; this module exercises the wiring through each runtime
surface:

* the CLI ``messages edit`` command (exit codes, dry-run, session-limit);
* the HTTP ``POST /telegram/messages/edit`` endpoint (status codes incl.
  503/403/400, end-to-end session-limit honoring a freshly-sent message).

MCP wiring is exercised in ``test_mcp_tools.py`` /
``test_mcp_mount.py``.
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
from telegram_assistant.http_api import create_app
from telegram_assistant.persistence import OperationStore

AUTH = {"Authorization": "Bearer secret_token"}


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeEditBackend:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def edit_message(self, *, chat_id: int, message_id: int, text: str) -> int:
        self.calls.append(
            {"chat_id": chat_id, "message_id": message_id, "text": text}
        )
        return message_id


class FakeMessageBackend:
    def __init__(self, *, message_id: int = 777) -> None:
        self.sent: list[dict[str, Any]] = []
        self._message_id = message_id

    async def send_message(
        self,
        *,
        chat_id: int,
        text: str,
        topic_id: int | None = None,
        files: tuple[str, ...] = (),
        schedule_at: object | None = None,
    ) -> int:
        self.sent.append({"chat_id": chat_id, "text": text})
        return self._message_id


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


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


def _http_client(
    *,
    access_block: str | None = None,
    edit_backend: FakeEditBackend | None = None,
    message_backend: FakeMessageBackend | None = None,
    has_edit_factory: bool = True,
) -> TestClient:
    config = load_config_from_text(_config_with_access(access_block))
    app = create_app(
        config,
        session_manager=None,
        edit_backend_factory=(
            (lambda _r: edit_backend) if has_edit_factory else (lambda _r: None)
        ),
        message_backend_factory=(
            (lambda _r: message_backend) if message_backend is not None else (lambda _r: None)
        ),
        folder_backend_factory=lambda _r: None,
        resolver_factory=lambda _r: None,
        operation_store=_make_store(),
    )
    return TestClient(app)


def test_http_edit_session_limit_blocks_unsent_message() -> None:
    # No access block -> allow-all authorizer, but edit_only_session_messages
    # defaults to true: an id this process never sent is rejected with 403.
    backend = FakeEditBackend()
    client = _http_client(edit_backend=backend)
    resp = client.post(
        "/telegram/messages/edit",
        json={"telegram_chat_id": -100123, "message_id": 999, "text": "new"},
        headers=AUTH,
    )
    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"]["error"] == "edit_forbidden"
    assert resp.json()["detail"]["message_id"] == 999
    assert backend.calls == []


def test_http_edit_end_to_end_allows_sent_message() -> None:
    # Send a message through the same app (records the id in the process
    # registry), then edit it: the default session-limit lets it through.
    message_backend = FakeMessageBackend(message_id=555)
    edit_backend = FakeEditBackend()
    client = _http_client(
        edit_backend=edit_backend, message_backend=message_backend
    )

    send = client.post(
        "/telegram/messages",
        json={"telegram_chat_id": -100123, "text": "hello"},
        headers=AUTH,
    )
    assert send.status_code == 200, send.text
    sent_id = send.json()["telegram_message_id"]
    assert sent_id == 555

    resp = client.post(
        "/telegram/messages/edit",
        json={"telegram_chat_id": -100123, "message_id": sent_id, "text": "bye"},
        headers=AUTH,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["telegram_message_id"] == sent_id
    assert body["text"] == "bye"
    assert body["dry_run"] is False
    assert edit_backend.calls == [
        {"chat_id": -100123, "message_id": sent_id, "text": "bye"}
    ]


def test_http_edit_flag_off_allows_arbitrary_ids() -> None:
    backend = FakeEditBackend()
    client = _http_client(
        access_block=(
            "access:\n  rules:\n    - all: true\n      permission: write\n"
            "  edit_only_session_messages: false\n"
        ),
        edit_backend=backend,
    )
    resp = client.post(
        "/telegram/messages/edit",
        json={"telegram_chat_id": -100123, "message_id": 42, "text": "patched"},
        headers=AUTH,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["text"] == "patched"
    assert backend.calls == [
        {"chat_id": -100123, "message_id": 42, "text": "patched"}
    ]


def test_http_edit_rule_level_override_restricts_over_policy_default() -> None:
    # Policy default false (would allow arbitrary ids), but the matching rule
    # re-imposes the session limit via a per-rule override — the unsent id is
    # rejected with 403.
    backend = FakeEditBackend()
    client = _http_client(
        access_block=(
            "access:\n  rules:\n    - all: true\n      permission: write\n"
            "      edit_only_session_messages: true\n"
            "  edit_only_session_messages: false\n"
        ),
        edit_backend=backend,
    )
    resp = client.post(
        "/telegram/messages/edit",
        json={"telegram_chat_id": -100123, "message_id": 999, "text": "x"},
        headers=AUTH,
    )
    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"]["error"] == "edit_forbidden"
    assert backend.calls == []


def test_http_edit_dry_run_does_not_call_backend() -> None:
    backend = FakeEditBackend()
    client = _http_client(
        access_block=(
            "access:\n  rules:\n    - all: true\n      permission: write\n"
            "  edit_only_session_messages: false\n"
        ),
        edit_backend=backend,
    )
    resp = client.post(
        "/telegram/messages/edit",
        json={
            "telegram_chat_id": -100123,
            "message_id": 42,
            "text": "x",
            "dry_run": True,
        },
        headers=AUTH,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["dry_run"] is True
    assert backend.calls == []


def test_http_edit_403_access_denied_without_write_permission() -> None:
    backend = FakeEditBackend()
    client = _http_client(
        access_block=(
            "access:\n  rules:\n    - all: true\n      permission: read\n"
            "  edit_only_session_messages: false\n"
        ),
        edit_backend=backend,
    )
    resp = client.post(
        "/telegram/messages/edit",
        json={"telegram_chat_id": -100123, "message_id": 42, "text": "x"},
        headers=AUTH,
    )
    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"]["error"] == "access_denied"
    assert backend.calls == []


def test_http_edit_503_when_backend_unavailable() -> None:
    client = _http_client(
        access_block=(
            "access:\n  rules:\n    - all: true\n      permission: write\n"
            "  edit_only_session_messages: false\n"
        ),
        has_edit_factory=False,
    )
    resp = client.post(
        "/telegram/messages/edit",
        json={"telegram_chat_id": -100123, "message_id": 42, "text": "x"},
        headers=AUTH,
    )
    assert resp.status_code == 503, resp.text


def test_http_edit_requires_auth() -> None:
    client = _http_client(edit_backend=FakeEditBackend())
    resp = client.post(
        "/telegram/messages/edit",
        json={"telegram_chat_id": -100123, "message_id": 1, "text": "x"},
    )
    assert resp.status_code == 401


def test_http_edit_422_empty_text() -> None:
    backend = FakeEditBackend()
    client = _http_client(edit_backend=backend)
    resp = client.post(
        "/telegram/messages/edit",
        json={"telegram_chat_id": -100123, "message_id": 1, "text": "   "},
        headers=AUTH,
    )
    assert resp.status_code == 422, resp.text
    assert backend.calls == []


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _write_config(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "config.yml"
    p.write_text(body)
    return p


def _patch_cli_edit_backends(
    monkeypatch: pytest.MonkeyPatch,
    backend: FakeEditBackend,
) -> None:
    class _FakeManager:
        async def disconnect(self) -> None:
            return None

        async def get_client(self) -> Any:
            # A configured access policy makes the command build a resolver even
            # for --chat-id targets; the resolver wraps the client but never
            # calls resolve() on the numeric-id path, so a sentinel suffices.
            return object()

    def _factory(config_path: Path | None) -> Any:
        from telegram_assistant.config import load_config

        config = load_config(config_path)

        async def _open() -> Any:
            return backend, None

        return config, _FakeManager(), _open

    monkeypatch.setattr(cli_main, "_build_edit_backends", _factory)


def _cli_config(flag: bool | None) -> str:
    if flag is None:
        return _config_with_access(None)
    return _config_with_access(
        "access:\n  rules:\n    - all: true\n      permission: write\n"
        f"  edit_only_session_messages: {'true' if flag else 'false'}\n"
    )


def test_cli_edit_dry_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_file = _write_config(tmp_path, _cli_config(False))
    backend = FakeEditBackend()
    _patch_cli_edit_backends(monkeypatch, backend)

    runner = CliRunner()
    result = runner.invoke(
        cli_main.app,
        [
            "messages",
            "edit",
            "--chat-id",
            "-100123",
            "--message-id",
            "5",
            "--text",
            "updated",
            "--dry-run",
            "--config",
            str(config_file),
        ],
    )
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload["dry_run"] is True
    assert payload["telegram_message_id"] == 5
    assert payload["text"] == "updated"
    assert backend.calls == []


def test_cli_edit_real_with_flag_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_file = _write_config(tmp_path, _cli_config(False))
    backend = FakeEditBackend()
    _patch_cli_edit_backends(monkeypatch, backend)

    runner = CliRunner()
    result = runner.invoke(
        cli_main.app,
        [
            "messages",
            "edit",
            "--chat-id",
            "-100123",
            "--message-id",
            "5",
            "--text",
            "updated",
            "--config",
            str(config_file),
        ],
    )
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload["telegram_message_id"] == 5
    assert payload["text"] == "updated"
    assert backend.calls == [
        {"chat_id": -100123, "message_id": 5, "text": "updated"}
    ]


def test_cli_edit_session_limit_blocks_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Default flag (true) + a fresh CLI process with an empty registry means
    # every id is unrecorded -> MessageEditForbidden -> exit code 3.
    config_file = _write_config(tmp_path, _cli_config(None))
    backend = FakeEditBackend()
    _patch_cli_edit_backends(monkeypatch, backend)

    runner = CliRunner()
    result = runner.invoke(
        cli_main.app,
        [
            "messages",
            "edit",
            "--chat-id",
            "-100123",
            "--message-id",
            "5",
            "--text",
            "updated",
            "--config",
            str(config_file),
        ],
    )
    assert result.exit_code == 3, result.stdout
    assert backend.calls == []


def test_cli_edit_access_denied_exit_3(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_file = _write_config(
        tmp_path,
        _config_with_access(
            "access:\n  rules:\n    - all: true\n      permission: read\n"
            "  edit_only_session_messages: false\n"
        ),
    )
    backend = FakeEditBackend()
    _patch_cli_edit_backends(monkeypatch, backend)

    runner = CliRunner()
    result = runner.invoke(
        cli_main.app,
        [
            "messages",
            "edit",
            "--chat-id",
            "-100123",
            "--message-id",
            "5",
            "--text",
            "updated",
            "--config",
            str(config_file),
        ],
    )
    assert result.exit_code == 3, result.stdout
    assert backend.calls == []


def test_cli_edit_requires_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_file = _write_config(tmp_path, _cli_config(False))
    backend = FakeEditBackend()
    _patch_cli_edit_backends(monkeypatch, backend)

    runner = CliRunner()
    result = runner.invoke(
        cli_main.app,
        [
            "messages",
            "edit",
            "--chat-id",
            "-100123",
            "--message-id",
            "5",
            "--text",
            "   ",
            "--config",
            str(config_file),
        ],
    )
    assert result.exit_code == 2


def test_cli_edit_rejects_two_targets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_file = _write_config(tmp_path, _cli_config(False))
    backend = FakeEditBackend()
    _patch_cli_edit_backends(monkeypatch, backend)

    runner = CliRunner()
    result = runner.invoke(
        cli_main.app,
        [
            "messages",
            "edit",
            "--chat-id",
            "-100123",
            "--entity",
            "@other",
            "--message-id",
            "5",
            "--text",
            "updated",
            "--config",
            str(config_file),
        ],
    )
    assert result.exit_code == 2
