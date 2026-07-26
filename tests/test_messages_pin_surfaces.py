"""Tests for Task 7 — pin/unpin surfaces (CLI, HTTP).

The pin/unpin *domain* op is covered in ``test_messages_pin.py``; this module
exercises the wiring through the CLI ``messages pin`` / ``messages unpin``
commands and the HTTP ``POST /telegram/messages/pin`` / ``/unpin`` endpoints
(status codes incl. 503/403/400, dry-run, and unpin-all vs unpin-one).
"""

from __future__ import annotations

import json
import tempfile
import textwrap
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from telegram_assistant.cli import main as cli_main
from telegram_assistant.config import load_config_from_text
from telegram_assistant.http_api import create_app
from telegram_assistant.persistence import OperationStore
from telegram_assistant.worker.queue import FloodWaitError

AUTH = {"Authorization": "Bearer secret_token"}


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakePinBackend:
    def __init__(self) -> None:
        self.pins: list[dict[str, Any]] = []
        self.unpins: list[dict[str, Any]] = []

    async def pin_message(
        self, *, chat_id: int, message_id: int, silent: bool, pm_oneside: bool
    ) -> None:
        self.pins.append(
            {
                "chat_id": chat_id,
                "message_id": message_id,
                "silent": silent,
                "pm_oneside": pm_oneside,
            }
        )

    async def unpin_message(
        self, *, chat_id: int, message_id: int | None
    ) -> None:
        self.unpins.append({"chat_id": chat_id, "message_id": message_id})


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


_WRITE_ACCESS = "access:\n  rules:\n    - all: true\n      permission: write\n"
_READ_ACCESS = "access:\n  rules:\n    - all: true\n      permission: read\n"


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


def _http_client(
    *,
    access_block: str | None = None,
    pin_backend: FakePinBackend | None = None,
    has_pin_factory: bool = True,
) -> TestClient:
    config = load_config_from_text(_config_with_access(access_block))
    app = create_app(
        config,
        session_manager=None,
        pin_backend_factory=(
            (lambda _r: pin_backend) if has_pin_factory else (lambda _r: None)
        ),
        folder_backend_factory=lambda _r: None,
        resolver_factory=lambda _r: None,
        operation_store=_make_store(),
    )
    return TestClient(app)


def test_http_pin_calls_backend() -> None:
    backend = FakePinBackend()
    client = _http_client(access_block=_WRITE_ACCESS, pin_backend=backend)
    resp = client.post(
        "/telegram/messages/pin",
        json={"telegram_chat_id": -100123, "message_id": 7, "silent": True},
        headers=AUTH,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["telegram_message_id"] == 7
    assert body["silent"] is True
    assert backend.pins == [
        {"chat_id": -100123, "message_id": 7, "silent": True, "pm_oneside": False}
    ]


def test_http_pin_dry_run_skips_backend() -> None:
    backend = FakePinBackend()
    client = _http_client(access_block=_WRITE_ACCESS, pin_backend=backend)
    resp = client.post(
        "/telegram/messages/pin",
        json={"telegram_chat_id": -100123, "message_id": 7, "dry_run": True},
        headers=AUTH,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["dry_run"] is True
    assert backend.pins == []


def test_http_pin_403_without_write() -> None:
    backend = FakePinBackend()
    client = _http_client(access_block=_READ_ACCESS, pin_backend=backend)
    resp = client.post(
        "/telegram/messages/pin",
        json={"telegram_chat_id": -100123, "message_id": 7},
        headers=AUTH,
    )
    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"]["error"] == "access_denied"
    assert backend.pins == []


def test_http_pin_503_when_backend_unavailable() -> None:
    client = _http_client(access_block=_WRITE_ACCESS, has_pin_factory=False)
    resp = client.post(
        "/telegram/messages/pin",
        json={"telegram_chat_id": -100123, "message_id": 7},
        headers=AUTH,
    )
    assert resp.status_code == 503, resp.text


def test_http_pin_422_non_positive_id() -> None:
    backend = FakePinBackend()
    client = _http_client(access_block=_WRITE_ACCESS, pin_backend=backend)
    resp = client.post(
        "/telegram/messages/pin",
        json={"telegram_chat_id": -100123, "message_id": 0},
        headers=AUTH,
    )
    assert resp.status_code == 422, resp.text
    assert backend.pins == []


def test_http_pin_requires_auth() -> None:
    client = _http_client(access_block=_WRITE_ACCESS, pin_backend=FakePinBackend())
    resp = client.post(
        "/telegram/messages/pin",
        json={"telegram_chat_id": -100123, "message_id": 1},
    )
    assert resp.status_code == 401


def test_http_unpin_one() -> None:
    backend = FakePinBackend()
    client = _http_client(access_block=_WRITE_ACCESS, pin_backend=backend)
    resp = client.post(
        "/telegram/messages/unpin",
        json={"telegram_chat_id": -100123, "message_id": 7},
        headers=AUTH,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["unpinned_all"] is False
    assert body["telegram_message_id"] == 7
    assert backend.unpins == [{"chat_id": -100123, "message_id": 7}]


def test_http_unpin_all() -> None:
    backend = FakePinBackend()
    client = _http_client(access_block=_WRITE_ACCESS, pin_backend=backend)
    resp = client.post(
        "/telegram/messages/unpin",
        json={"telegram_chat_id": -100123, "unpin_all": True},
        headers=AUTH,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["unpinned_all"] is True
    assert body["telegram_message_id"] is None
    assert backend.unpins == [{"chat_id": -100123, "message_id": None}]


def test_http_unpin_422_requires_id_or_all() -> None:
    backend = FakePinBackend()
    client = _http_client(access_block=_WRITE_ACCESS, pin_backend=backend)
    resp = client.post(
        "/telegram/messages/unpin",
        json={"telegram_chat_id": -100123},
        headers=AUTH,
    )
    assert resp.status_code == 422, resp.text
    assert backend.unpins == []


def test_http_unpin_422_both_id_and_all() -> None:
    backend = FakePinBackend()
    client = _http_client(access_block=_WRITE_ACCESS, pin_backend=backend)
    resp = client.post(
        "/telegram/messages/unpin",
        json={"telegram_chat_id": -100123, "message_id": 7, "unpin_all": True},
        headers=AUTH,
    )
    assert resp.status_code == 422, resp.text
    assert backend.unpins == []


def test_http_unpin_403_without_write() -> None:
    backend = FakePinBackend()
    client = _http_client(access_block=_READ_ACCESS, pin_backend=backend)
    resp = client.post(
        "/telegram/messages/unpin",
        json={"telegram_chat_id": -100123, "unpin_all": True},
        headers=AUTH,
    )
    assert resp.status_code == 403, resp.text
    assert backend.unpins == []


def test_http_unpin_503_when_backend_unavailable() -> None:
    client = _http_client(access_block=_WRITE_ACCESS, has_pin_factory=False)
    resp = client.post(
        "/telegram/messages/unpin",
        json={"telegram_chat_id": -100123, "unpin_all": True},
        headers=AUTH,
    )
    assert resp.status_code == 503, resp.text


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _write_config(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "config.yml"
    p.write_text(body)
    return p


def _patch_cli_pin_backends(
    monkeypatch: pytest.MonkeyPatch,
    backend: FakePinBackend,
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
            return backend, None

        return config, _FakeManager(), _open

    monkeypatch.setattr(cli_main, "_build_pin_backends", _factory)


def test_cli_pin_dry_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_file = _write_config(tmp_path, _config_with_access(_WRITE_ACCESS))
    backend = FakePinBackend()
    _patch_cli_pin_backends(monkeypatch, backend)

    runner = CliRunner()
    result = runner.invoke(
        cli_main.app,
        [
            "messages",
            "pin",
            "--chat-id",
            "-100123",
            "--message-id",
            "7",
            "--silent",
            "--dry-run",
            "--config",
            str(config_file),
        ],
    )
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload["dry_run"] is True
    assert payload["telegram_message_id"] == 7
    assert payload["silent"] is True
    assert backend.pins == []


def test_cli_pin_real(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_file = _write_config(tmp_path, _config_with_access(_WRITE_ACCESS))
    backend = FakePinBackend()
    _patch_cli_pin_backends(monkeypatch, backend)

    runner = CliRunner()
    result = runner.invoke(
        cli_main.app,
        [
            "messages",
            "pin",
            "--chat-id",
            "-100123",
            "--message-id",
            "7",
            "--config",
            str(config_file),
        ],
    )
    assert result.exit_code == 0, result.stdout
    assert backend.pins == [
        {"chat_id": -100123, "message_id": 7, "silent": False, "pm_oneside": False}
    ]


def test_cli_pin_access_denied_exit_3(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_file = _write_config(tmp_path, _config_with_access(_READ_ACCESS))
    backend = FakePinBackend()
    _patch_cli_pin_backends(monkeypatch, backend)

    runner = CliRunner()
    result = runner.invoke(
        cli_main.app,
        [
            "messages",
            "pin",
            "--chat-id",
            "-100123",
            "--message-id",
            "7",
            "--config",
            str(config_file),
        ],
    )
    assert result.exit_code == 3, result.stdout
    assert backend.pins == []


def test_cli_pin_requires_message_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_file = _write_config(tmp_path, _config_with_access(_WRITE_ACCESS))
    backend = FakePinBackend()
    _patch_cli_pin_backends(monkeypatch, backend)

    runner = CliRunner()
    result = runner.invoke(
        cli_main.app,
        [
            "messages",
            "pin",
            "--chat-id",
            "-100123",
            "--config",
            str(config_file),
        ],
    )
    assert result.exit_code == 2


def test_cli_pin_rejects_two_targets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_file = _write_config(tmp_path, _config_with_access(_WRITE_ACCESS))
    backend = FakePinBackend()
    _patch_cli_pin_backends(monkeypatch, backend)

    runner = CliRunner()
    result = runner.invoke(
        cli_main.app,
        [
            "messages",
            "pin",
            "--chat-id",
            "-100123",
            "--entity",
            "@other",
            "--message-id",
            "7",
            "--config",
            str(config_file),
        ],
    )
    assert result.exit_code == 2


def test_cli_unpin_all(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_file = _write_config(tmp_path, _config_with_access(_WRITE_ACCESS))
    backend = FakePinBackend()
    _patch_cli_pin_backends(monkeypatch, backend)

    runner = CliRunner()
    result = runner.invoke(
        cli_main.app,
        [
            "messages",
            "unpin",
            "--chat-id",
            "-100123",
            "--all",
            "--config",
            str(config_file),
        ],
    )
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload["unpinned_all"] is True
    assert backend.unpins == [{"chat_id": -100123, "message_id": None}]


def test_cli_unpin_one(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_file = _write_config(tmp_path, _config_with_access(_WRITE_ACCESS))
    backend = FakePinBackend()
    _patch_cli_pin_backends(monkeypatch, backend)

    runner = CliRunner()
    result = runner.invoke(
        cli_main.app,
        [
            "messages",
            "unpin",
            "--chat-id",
            "-100123",
            "--message-id",
            "7",
            "--config",
            str(config_file),
        ],
    )
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload["unpinned_all"] is False
    assert payload["telegram_message_id"] == 7
    assert backend.unpins == [{"chat_id": -100123, "message_id": 7}]


def test_cli_unpin_requires_id_or_all(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_file = _write_config(tmp_path, _config_with_access(_WRITE_ACCESS))
    backend = FakePinBackend()
    _patch_cli_pin_backends(monkeypatch, backend)

    runner = CliRunner()
    result = runner.invoke(
        cli_main.app,
        [
            "messages",
            "unpin",
            "--chat-id",
            "-100123",
            "--config",
            str(config_file),
        ],
    )
    assert result.exit_code == 2
    assert backend.unpins == []


def test_cli_unpin_rejects_id_and_all(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_file = _write_config(tmp_path, _config_with_access(_WRITE_ACCESS))
    backend = FakePinBackend()
    _patch_cli_pin_backends(monkeypatch, backend)

    runner = CliRunner()
    result = runner.invoke(
        cli_main.app,
        [
            "messages",
            "unpin",
            "--chat-id",
            "-100123",
            "--message-id",
            "7",
            "--all",
            "--config",
            str(config_file),
        ],
    )
    assert result.exit_code == 2
    assert backend.unpins == []


# ---------------------------------------------------------------------------
# Pacing + FLOOD_WAIT retry-after across surfaces
# ---------------------------------------------------------------------------


class FloodPinBackend:
    """Always answers FLOOD_WAIT, with a wait too long for pacing to absorb."""

    def __init__(self, seconds: float = 600.0) -> None:
        self.seconds = seconds
        self.attempts = 0

    async def pin_message(
        self, *, chat_id: int, message_id: int, silent: bool, pm_oneside: bool
    ) -> None:
        self.attempts += 1
        raise FloodWaitError(self.seconds)

    async def unpin_message(
        self, *, chat_id: int, message_id: int | None
    ) -> None:
        self.attempts += 1
        raise FloodWaitError(self.seconds)


def _config_with_pacing(interval: float, session_dir: Path) -> str:
    return textwrap.dedent(
        f"""
        telegram:
          api_id: 123456
          api_hash: "telegram_api_hash"
          session_path: {session_dir}/telegram-assistant.session
          pin_min_interval_seconds: {interval}
          default_chat_folder:
            folder_id: 2
            folder_name: "Planfix clients"
          access:
            rules:
              - all: true
                permission: write
        http:
          host: "0.0.0.0"
          port: 8085
          bearer_token: "secret_token"
        logging:
          level: INFO
        """
    ).strip()


def test_http_pin_paces_rapid_calls(tmp_path: Path) -> None:
    """The second pin on the same chat waits out the configured interval."""
    backend = FakePinBackend()
    config = load_config_from_text(_config_with_pacing(0.4, tmp_path))
    app = create_app(
        config,
        session_manager=None,
        pin_backend_factory=lambda _r: backend,
        folder_backend_factory=lambda _r: None,
        resolver_factory=lambda _r: None,
        database_path=tmp_path / "state.db",
    )
    client = TestClient(app)
    body = {"telegram_chat_id": -100123, "message_id": 7}

    started = time.monotonic()
    for _ in range(2):
        resp = client.post("/telegram/messages/pin", json=body, headers=AUTH)
        assert resp.status_code == 200, resp.text
    elapsed = time.monotonic() - started

    assert elapsed >= 0.4
    assert len(backend.pins) == 2


def test_http_pin_no_pacing_when_interval_zero(tmp_path: Path) -> None:
    backend = FakePinBackend()
    config = load_config_from_text(_config_with_pacing(0, tmp_path))
    app = create_app(
        config,
        session_manager=None,
        pin_backend_factory=lambda _r: backend,
        folder_backend_factory=lambda _r: None,
        resolver_factory=lambda _r: None,
        database_path=tmp_path / "state.db",
    )
    client = TestClient(app)
    body = {"telegram_chat_id": -100123, "message_id": 7}

    started = time.monotonic()
    for _ in range(3):
        assert (
            client.post("/telegram/messages/pin", json=body, headers=AUTH).status_code
            == 200
        )
    assert time.monotonic() - started < 0.4
    assert len(backend.pins) == 3


def test_http_pin_flood_wait_reports_retry_after() -> None:
    backend = FloodPinBackend(seconds=600.0)
    client = _http_client(access_block=_WRITE_ACCESS, pin_backend=backend)  # type: ignore[arg-type]
    resp = client.post(
        "/telegram/messages/pin",
        json={"telegram_chat_id": -100123, "message_id": 7},
        headers=AUTH,
    )
    assert resp.status_code == 502, resp.text
    detail = resp.json()["detail"]
    assert detail["error"] == "needs_review"
    # 600s FLOOD_WAIT + the 5s safety margin.
    assert detail["retry_after_seconds"] == 605.0
    assert detail["retry_at"] > 0
    assert resp.headers["Retry-After"] == "605"


def test_http_unpin_flood_wait_reports_retry_after() -> None:
    backend = FloodPinBackend(seconds=600.0)
    client = _http_client(access_block=_WRITE_ACCESS, pin_backend=backend)  # type: ignore[arg-type]
    resp = client.post(
        "/telegram/messages/unpin",
        json={"telegram_chat_id": -100123, "unpin_all": True},
        headers=AUTH,
    )
    assert resp.status_code == 502, resp.text
    assert resp.json()["detail"]["retry_after_seconds"] == 605.0
    assert resp.headers["Retry-After"] == "605"


def test_cli_pin_flood_wait_reports_next_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_file = _write_config(tmp_path, _config_with_access(_WRITE_ACCESS))
    backend = FloodPinBackend(seconds=600.0)
    _patch_cli_pin_backends(monkeypatch, backend)  # type: ignore[arg-type]

    runner = CliRunner()
    result = runner.invoke(
        cli_main.app,
        [
            "messages",
            "pin",
            "--chat-id",
            "-100123",
            "--message-id",
            "7",
            "--config",
            str(config_file),
        ],
    )
    assert result.exit_code == 1, result.output
    assert "Retry after 605s" in result.output
    assert "next attempt at" in result.output


def test_cli_unpin_flood_wait_reports_next_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_file = _write_config(tmp_path, _config_with_access(_WRITE_ACCESS))
    backend = FloodPinBackend(seconds=600.0)
    _patch_cli_pin_backends(monkeypatch, backend)  # type: ignore[arg-type]

    runner = CliRunner()
    result = runner.invoke(
        cli_main.app,
        [
            "messages",
            "unpin",
            "--chat-id",
            "-100123",
            "--all",
            "--config",
            str(config_file),
        ],
    )
    assert result.exit_code == 1, result.output
    assert "Retry after 605s" in result.output


def test_cli_pin_paces_across_processes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two CLI invocations share the SQLite gate, so the second one waits."""
    config_file = _write_config(tmp_path, _config_with_pacing(0.4, tmp_path))
    backend = FakePinBackend()
    _patch_cli_pin_backends(monkeypatch, backend)

    runner = CliRunner()
    args = [
        "messages",
        "pin",
        "--chat-id",
        "-100123",
        "--message-id",
        "7",
        "--config",
        str(config_file),
    ]
    started = time.monotonic()
    for _ in range(2):
        result = runner.invoke(cli_main.app, args)
        assert result.exit_code == 0, result.output
    assert time.monotonic() - started >= 0.4
    assert len(backend.pins) == 2
