"""Tests for Task 7 — delete surfaces (config flag, CLI, HTTP).

The delete-message *domain* op is covered in ``test_messages_delete.py``; this
module exercises the wiring through each runtime surface:

* the ``telegram.access.delete_only_session_messages`` config flag (default
  true) and its safe-default behaviour when ``access`` is omitted;
* the CLI ``messages delete`` command (exit codes, dry-run, session-limit);
* the HTTP ``POST /telegram/messages/delete`` endpoint (status codes incl.
  503/403/404, end-to-end session-limit honoring a freshly-sent message).
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
from telegram_assistant.config.models import AccessConfig
from telegram_assistant.http_api import create_app
from telegram_assistant.persistence import OperationStore

AUTH = {"Authorization": "Bearer secret_token"}


# ---------------------------------------------------------------------------
# Config flag
# ---------------------------------------------------------------------------


def test_delete_only_session_messages_defaults_true() -> None:
    assert AccessConfig().delete_only_session_messages is True


def test_delete_only_session_messages_round_trips_false() -> None:
    cfg = load_config_from_text(
        _config_with_access("access:\n  rules: []\n  delete_only_session_messages: false\n")
    )
    assert cfg.telegram.access is not None
    assert cfg.telegram.access.delete_only_session_messages is False


def test_access_rule_delete_only_override_defaults_none() -> None:
    from telegram_assistant.config.models import AccessRule

    assert AccessRule(chat=1).delete_only_session_messages is None


def test_access_rule_delete_only_override_round_trips_false() -> None:
    cfg = load_config_from_text(
        _config_with_access(
            "access:\n  rules:\n    - chat: 123\n      permission: delete\n"
            "      delete_only_session_messages: false\n"
        )
    )
    assert cfg.telegram.access is not None
    assert cfg.telegram.access.rules[0].delete_only_session_messages is False


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeDeleteBackend:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def delete_messages(
        self, *, chat_id: int, message_ids: tuple[int, ...], revoke: bool = True
    ) -> int:
        message_ids = tuple(message_ids)
        self.calls.append(
            {"chat_id": chat_id, "message_ids": message_ids, "revoke": revoke}
        )
        return len(message_ids)


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
    delete_backend: FakeDeleteBackend | None = None,
    message_backend: FakeMessageBackend | None = None,
    has_delete_factory: bool = True,
) -> TestClient:
    config = load_config_from_text(_config_with_access(access_block))
    app = create_app(
        config,
        session_manager=None,
        delete_backend_factory=(
            (lambda _r: delete_backend) if has_delete_factory else (lambda _r: None)
        ),
        message_backend_factory=(
            (lambda _r: message_backend) if message_backend is not None else (lambda _r: None)
        ),
        folder_backend_factory=lambda _r: None,
        resolver_factory=lambda _r: None,
        operation_store=_make_store(),
    )
    return TestClient(app)


def test_http_delete_session_limit_blocks_unsent_message() -> None:
    # No access block -> allow-all authorizer, but delete_only_session_messages
    # defaults to true: an id this process never sent is rejected with 403.
    backend = FakeDeleteBackend()
    client = _http_client(delete_backend=backend)
    resp = client.post(
        "/telegram/messages/delete",
        json={"telegram_chat_id": -100123, "message_ids": [999]},
        headers=AUTH,
    )
    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"]["error"] == "delete_forbidden"
    assert resp.json()["detail"]["message_ids"] == [999]
    assert backend.calls == []


def test_http_delete_end_to_end_allows_sent_message() -> None:
    # Send a message through the same app (records the id in the process
    # registry), then delete it: the default session-limit lets it through.
    message_backend = FakeMessageBackend(message_id=555)
    delete_backend = FakeDeleteBackend()
    client = _http_client(
        delete_backend=delete_backend, message_backend=message_backend
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
        "/telegram/messages/delete",
        json={"telegram_chat_id": -100123, "message_ids": [sent_id]},
        headers=AUTH,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["deleted"] == 1
    assert body["revoke"] is True
    assert delete_backend.calls == [
        {"chat_id": -100123, "message_ids": (sent_id,), "revoke": True}
    ]


def test_http_delete_flag_off_allows_arbitrary_ids() -> None:
    backend = FakeDeleteBackend()
    client = _http_client(
        access_block=(
            "access:\n  rules:\n    - all: true\n      permission: delete\n"
            "  delete_only_session_messages: false\n"
        ),
        delete_backend=backend,
    )
    resp = client.post(
        "/telegram/messages/delete",
        json={"telegram_chat_id": -100123, "message_ids": [42], "revoke": False},
        headers=AUTH,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["deleted"] == 1
    assert backend.calls == [
        {"chat_id": -100123, "message_ids": (42,), "revoke": False}
    ]


def test_http_delete_rule_level_override_allows_arbitrary_ids() -> None:
    # Policy default stays true (safe), but the matching rule opts out via a
    # per-rule delete_only_session_messages: false, so an unsent id goes through.
    backend = FakeDeleteBackend()
    client = _http_client(
        access_block=(
            "access:\n  rules:\n    - all: true\n      permission: delete\n"
            "      delete_only_session_messages: false\n"
        ),
        delete_backend=backend,
    )
    resp = client.post(
        "/telegram/messages/delete",
        json={"telegram_chat_id": -100123, "message_ids": [42], "revoke": False},
        headers=AUTH,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["deleted"] == 1
    assert backend.calls == [
        {"chat_id": -100123, "message_ids": (42,), "revoke": False}
    ]


def test_http_delete_rule_level_override_restricts_over_policy_default() -> None:
    # Policy default false (would allow arbitrary ids), but the matching rule
    # re-imposes the session limit via a per-rule override — the unsent id is
    # rejected with 403.
    backend = FakeDeleteBackend()
    client = _http_client(
        access_block=(
            "access:\n  rules:\n    - all: true\n      permission: delete\n"
            "      delete_only_session_messages: true\n"
            "  delete_only_session_messages: false\n"
        ),
        delete_backend=backend,
    )
    resp = client.post(
        "/telegram/messages/delete",
        json={"telegram_chat_id": -100123, "message_ids": [999]},
        headers=AUTH,
    )
    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"]["error"] == "delete_forbidden"
    assert backend.calls == []


def test_http_delete_dry_run_does_not_call_backend() -> None:
    backend = FakeDeleteBackend()
    client = _http_client(
        access_block=(
            "access:\n  rules:\n    - all: true\n      permission: delete\n"
            "  delete_only_session_messages: false\n"
        ),
        delete_backend=backend,
    )
    resp = client.post(
        "/telegram/messages/delete",
        json={"telegram_chat_id": -100123, "message_ids": [42], "dry_run": True},
        headers=AUTH,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["dry_run"] is True
    assert body["deleted"] == 0
    assert backend.calls == []


def test_http_delete_403_access_denied_without_delete_permission() -> None:
    backend = FakeDeleteBackend()
    client = _http_client(
        access_block=(
            "access:\n  rules:\n    - all: true\n      permission: write\n"
            "  delete_only_session_messages: false\n"
        ),
        delete_backend=backend,
    )
    resp = client.post(
        "/telegram/messages/delete",
        json={"telegram_chat_id": -100123, "message_ids": [42]},
        headers=AUTH,
    )
    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"]["error"] == "access_denied"
    assert backend.calls == []


def test_http_delete_503_when_backend_unavailable() -> None:
    client = _http_client(
        access_block=(
            "access:\n  rules:\n    - all: true\n      permission: delete\n"
            "  delete_only_session_messages: false\n"
        ),
        has_delete_factory=False,
    )
    resp = client.post(
        "/telegram/messages/delete",
        json={"telegram_chat_id": -100123, "message_ids": [42]},
        headers=AUTH,
    )
    assert resp.status_code == 503, resp.text


def test_http_delete_requires_auth() -> None:
    client = _http_client(delete_backend=FakeDeleteBackend())
    resp = client.post(
        "/telegram/messages/delete",
        json={"telegram_chat_id": -100123, "message_ids": [1]},
    )
    assert resp.status_code == 401


def test_http_delete_422_empty_ids() -> None:
    backend = FakeDeleteBackend()
    client = _http_client(delete_backend=backend)
    resp = client.post(
        "/telegram/messages/delete",
        json={"telegram_chat_id": -100123, "message_ids": []},
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


def _patch_cli_delete_backends(
    monkeypatch: pytest.MonkeyPatch,
    backend: FakeDeleteBackend,
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

    monkeypatch.setattr(cli_main, "_build_delete_backends", _factory)


def _cli_config(flag: bool | None) -> str:
    if flag is None:
        return _config_with_access(None)
    return _config_with_access(
        "access:\n  rules:\n    - all: true\n      permission: delete\n"
        f"  delete_only_session_messages: {'true' if flag else 'false'}\n"
    )


def test_cli_delete_dry_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_file = _write_config(tmp_path, _cli_config(False))
    backend = FakeDeleteBackend()
    _patch_cli_delete_backends(monkeypatch, backend)

    runner = CliRunner()
    result = runner.invoke(
        cli_main.app,
        [
            "messages",
            "delete",
            "--chat-id",
            "-100123",
            "--message-id",
            "1",
            "--message-id",
            "2",
            "--dry-run",
            "--config",
            str(config_file),
        ],
    )
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload["dry_run"] is True
    assert payload["deleted"] == 0
    assert payload["message_ids"] == [1, 2]
    assert backend.calls == []


def test_cli_delete_real_with_flag_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_file = _write_config(tmp_path, _cli_config(False))
    backend = FakeDeleteBackend()
    _patch_cli_delete_backends(monkeypatch, backend)

    runner = CliRunner()
    result = runner.invoke(
        cli_main.app,
        [
            "messages",
            "delete",
            "--chat-id",
            "-100123",
            "--message-id",
            "1",
            "--no-revoke",
            "--config",
            str(config_file),
        ],
    )
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload["deleted"] == 1
    assert payload["revoke"] is False
    assert backend.calls == [
        {"chat_id": -100123, "message_ids": (1,), "revoke": False}
    ]


def test_cli_delete_session_limit_blocks_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Default flag (true) + a fresh CLI process with an empty registry means
    # every id is unrecorded -> MessageDeleteForbidden -> exit code 3.
    config_file = _write_config(tmp_path, _cli_config(None))
    backend = FakeDeleteBackend()
    _patch_cli_delete_backends(monkeypatch, backend)

    runner = CliRunner()
    result = runner.invoke(
        cli_main.app,
        [
            "messages",
            "delete",
            "--chat-id",
            "-100123",
            "--message-id",
            "1",
            "--config",
            str(config_file),
        ],
    )
    assert result.exit_code == 3, result.stdout
    assert backend.calls == []


def test_cli_delete_access_denied_exit_3(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_file = _write_config(
        tmp_path,
        _config_with_access(
            "access:\n  rules:\n    - all: true\n      permission: write\n"
            "  delete_only_session_messages: false\n"
        ),
    )
    backend = FakeDeleteBackend()
    _patch_cli_delete_backends(monkeypatch, backend)

    runner = CliRunner()
    result = runner.invoke(
        cli_main.app,
        [
            "messages",
            "delete",
            "--chat-id",
            "-100123",
            "--message-id",
            "1",
            "--config",
            str(config_file),
        ],
    )
    assert result.exit_code == 3, result.stdout
    assert backend.calls == []


def test_cli_delete_requires_message_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_file = _write_config(tmp_path, _cli_config(False))
    backend = FakeDeleteBackend()
    _patch_cli_delete_backends(monkeypatch, backend)

    runner = CliRunner()
    result = runner.invoke(
        cli_main.app,
        [
            "messages",
            "delete",
            "--chat-id",
            "-100123",
            "--config",
            str(config_file),
        ],
    )
    assert result.exit_code == 2


def test_cli_delete_rejects_two_targets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_file = _write_config(tmp_path, _cli_config(False))
    backend = FakeDeleteBackend()
    _patch_cli_delete_backends(monkeypatch, backend)

    runner = CliRunner()
    result = runner.invoke(
        cli_main.app,
        [
            "messages",
            "delete",
            "--chat-id",
            "-100123",
            "--entity",
            "@other",
            "--message-id",
            "1",
            "--config",
            str(config_file),
        ],
    )
    assert result.exit_code == 2
