"""Tests for Task 9 — download surfaces (CLI, HTTP).

The media-download *domain* op is covered in ``test_messages_download.py``; this
module exercises the wiring through the CLI ``messages download`` command and
the HTTP ``POST /telegram/messages/download`` endpoint (status codes incl.
503/403/400, dry-run, and invalid flag combos).
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
from telegram_assistant.messages import DownloadedMedia, MediaInfo
from telegram_assistant.persistence import OperationStore

AUTH = {"Authorization": "Bearer secret_token"}

_DEFAULT_INFO = MediaInfo(filename="photo.jpg", size=100, mime="image/jpeg")
_UNSET = object()


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeDownloadBackend:
    def __init__(self, *, info: Any = _UNSET) -> None:
        self._info = _DEFAULT_INFO if info is _UNSET else info
        self.probe_calls: list[dict[str, Any]] = []
        self.download_calls: list[dict[str, Any]] = []

    async def probe_media(
        self, *, chat_id: int, message_id: int
    ) -> MediaInfo | None:
        self.probe_calls.append({"chat_id": chat_id, "message_id": message_id})
        return self._info

    async def download_media(
        self, *, chat_id: int, message_id: int, target_path: str
    ) -> DownloadedMedia:
        self.download_calls.append(
            {"chat_id": chat_id, "message_id": message_id, "target_path": target_path}
        )
        return DownloadedMedia(path=target_path, size=100, mime="image/jpeg")


def _config_with_access(
    access_block: str | None, *, download_root: str | None = None
) -> str:
    root_line = (
        f'  download_root: "{download_root}"\n' if download_root is not None else ""
    )
    base = textwrap.dedent(
        """
        telegram:
          api_id: 123456
          api_hash: "telegram_api_hash"
          session_path: /data/telegram-assistant.session
          default_chat_folder:
            folder_id: 2
            folder_name: "Planfix clients"
        {root}{access}
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
    return base.format(root=root_line, access=indented).strip()


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
    download_backend: FakeDownloadBackend | None = None,
    has_download_factory: bool = True,
    download_root: str | None = None,
) -> TestClient:
    config = load_config_from_text(
        _config_with_access(access_block, download_root=download_root)
    )
    app = create_app(
        config,
        session_manager=None,
        download_backend_factory=(
            (lambda _r: download_backend)
            if has_download_factory
            else (lambda _r: None)
        ),
        folder_backend_factory=lambda _r: None,
        resolver_factory=lambda _r: None,
        operation_store=_make_store(),
    )
    return TestClient(app)


def test_http_download_writes_to_out_dir(tmp_path: Path) -> None:
    backend = FakeDownloadBackend()
    client = _http_client(access_block=_READ_ACCESS, download_backend=backend)
    resp = client.post(
        "/telegram/messages/download",
        json={
            "telegram_chat_id": -100123,
            "message_id": 7,
            "out_dir": str(tmp_path),
        },
        headers=AUTH,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["telegram_message_id"] == 7
    assert body["path"] == str(tmp_path / "photo.jpg")
    assert body["size"] == 100
    assert body["dry_run"] is False
    assert backend.download_calls == [
        {
            "chat_id": -100123,
            "message_id": 7,
            "target_path": str(tmp_path / "photo.jpg"),
        }
    ]


def test_http_download_echoes_unique_name_on_collision(tmp_path: Path) -> None:
    (tmp_path / "photo.jpg").write_bytes(b"already here")
    backend = FakeDownloadBackend()
    client = _http_client(access_block=_READ_ACCESS, download_backend=backend)
    resp = client.post(
        "/telegram/messages/download",
        json={
            "telegram_chat_id": -100123,
            "message_id": 7,
            "out_dir": str(tmp_path),
        },
        headers=AUTH,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["path"] == str(tmp_path / "photo (1).jpg")
    assert (tmp_path / "photo.jpg").read_bytes() == b"already here"


def test_http_download_defaults_out_dir_to_tempdir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The download now claims the target name for real, so point the "system
    # temp dir" at a scratch dir instead of littering /tmp across runs.
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    backend = FakeDownloadBackend()
    client = _http_client(access_block=_READ_ACCESS, download_backend=backend)
    resp = client.post(
        "/telegram/messages/download",
        json={"telegram_chat_id": -100123, "message_id": 7},
        headers=AUTH,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["path"].endswith("photo.jpg")
    assert body["path"] == str(Path(tempfile.gettempdir()) / "photo.jpg")


def test_http_download_rejects_out_dir_outside_default_root() -> None:
    # Default root is the system temp dir; an absolute path outside it (a
    # READ-only caller trying to write elsewhere) is rejected with 400.
    backend = FakeDownloadBackend()
    client = _http_client(access_block=_READ_ACCESS, download_backend=backend)
    resp = client.post(
        "/telegram/messages/download",
        json={
            "telegram_chat_id": -100123,
            "message_id": 7,
            "out_dir": "/etc/cron.d",
        },
        headers=AUTH,
    )
    assert resp.status_code == 400, resp.text
    assert "download root" in resp.json()["detail"]
    assert backend.download_calls == []


def test_http_download_rejects_out_dir_escaping_configured_root(
    tmp_path: Path,
) -> None:
    root = tmp_path / "downloads"
    root.mkdir()
    backend = FakeDownloadBackend()
    client = _http_client(
        access_block=_READ_ACCESS,
        download_backend=backend,
        download_root=str(root),
    )
    # A traversal that resolves to the parent of the configured root is denied.
    resp = client.post(
        "/telegram/messages/download",
        json={
            "telegram_chat_id": -100123,
            "message_id": 7,
            "out_dir": str(root / ".." ),
        },
        headers=AUTH,
    )
    assert resp.status_code == 400, resp.text
    assert backend.download_calls == []


def test_http_download_relative_out_dir_joins_into_root(tmp_path: Path) -> None:
    root = tmp_path / "downloads"
    root.mkdir()
    backend = FakeDownloadBackend()
    client = _http_client(
        access_block=_READ_ACCESS,
        download_backend=backend,
        download_root=str(root),
    )
    resp = client.post(
        "/telegram/messages/download",
        json={
            "telegram_chat_id": -100123,
            "message_id": 7,
            "out_dir": "sub",
        },
        headers=AUTH,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["path"] == str(root / "sub" / "photo.jpg")


def test_http_download_configured_root_default_out_dir(tmp_path: Path) -> None:
    root = tmp_path / "downloads"
    root.mkdir()
    backend = FakeDownloadBackend()
    client = _http_client(
        access_block=_READ_ACCESS,
        download_backend=backend,
        download_root=str(root),
    )
    resp = client.post(
        "/telegram/messages/download",
        json={"telegram_chat_id": -100123, "message_id": 7},
        headers=AUTH,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["path"] == str(root / "photo.jpg")


def test_http_download_dry_run_skips_transfer(tmp_path: Path) -> None:
    backend = FakeDownloadBackend()
    client = _http_client(access_block=_READ_ACCESS, download_backend=backend)
    resp = client.post(
        "/telegram/messages/download",
        json={
            "telegram_chat_id": -100123,
            "message_id": 7,
            "out_dir": str(tmp_path),
            "dry_run": True,
        },
        headers=AUTH,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["dry_run"] is True
    assert body["path"] == str(tmp_path / "photo.jpg")
    assert backend.download_calls == []
    assert backend.probe_calls == [{"chat_id": -100123, "message_id": 7}]


def test_http_download_403_without_read() -> None:
    backend = FakeDownloadBackend()
    # write-only rule: read is not implied, so READ is denied.
    client = _http_client(access_block=_WRITE_ACCESS, download_backend=backend)
    resp = client.post(
        "/telegram/messages/download",
        json={"telegram_chat_id": -100123, "message_id": 7},
        headers=AUTH,
    )
    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"]["error"] == "access_denied"
    assert backend.probe_calls == []


def test_http_download_400_no_media() -> None:
    backend = FakeDownloadBackend(info=None)
    client = _http_client(access_block=_READ_ACCESS, download_backend=backend)
    resp = client.post(
        "/telegram/messages/download",
        json={"telegram_chat_id": -100123, "message_id": 7},
        headers=AUTH,
    )
    assert resp.status_code == 400, resp.text
    assert backend.download_calls == []


def test_http_download_400_over_max_bytes() -> None:
    backend = FakeDownloadBackend()
    client = _http_client(access_block=_READ_ACCESS, download_backend=backend)
    resp = client.post(
        "/telegram/messages/download",
        json={"telegram_chat_id": -100123, "message_id": 7, "max_bytes": 10},
        headers=AUTH,
    )
    assert resp.status_code == 400, resp.text
    assert backend.download_calls == []


def test_http_download_503_when_backend_unavailable() -> None:
    client = _http_client(access_block=_READ_ACCESS, has_download_factory=False)
    resp = client.post(
        "/telegram/messages/download",
        json={"telegram_chat_id": -100123, "message_id": 7},
        headers=AUTH,
    )
    assert resp.status_code == 503, resp.text


def test_http_download_422_non_positive_id() -> None:
    backend = FakeDownloadBackend()
    client = _http_client(access_block=_READ_ACCESS, download_backend=backend)
    resp = client.post(
        "/telegram/messages/download",
        json={"telegram_chat_id": -100123, "message_id": 0},
        headers=AUTH,
    )
    assert resp.status_code == 422, resp.text
    assert backend.probe_calls == []


def test_http_download_requires_auth() -> None:
    client = _http_client(
        access_block=_READ_ACCESS, download_backend=FakeDownloadBackend()
    )
    resp = client.post(
        "/telegram/messages/download",
        json={"telegram_chat_id": -100123, "message_id": 1},
    )
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _write_config(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "config.yml"
    p.write_text(body)
    return p


def _patch_cli_download_backends(
    monkeypatch: pytest.MonkeyPatch,
    backend: FakeDownloadBackend,
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

    monkeypatch.setattr(cli_main, "_build_download_backends", _factory)


def test_cli_download_dry_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_file = _write_config(tmp_path, _config_with_access(_READ_ACCESS))
    backend = FakeDownloadBackend()
    _patch_cli_download_backends(monkeypatch, backend)

    runner = CliRunner()
    result = runner.invoke(
        cli_main.app,
        [
            "messages",
            "download",
            "--chat-id",
            "-100123",
            "--message-id",
            "7",
            "--dir",
            str(tmp_path),
            "--dry-run",
            "--config",
            str(config_file),
        ],
    )
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload["dry_run"] is True
    assert payload["path"] == str(tmp_path / "photo.jpg")
    assert backend.download_calls == []


def test_cli_download_real_to_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_file = _write_config(tmp_path, _config_with_access(_READ_ACCESS))
    backend = FakeDownloadBackend()
    _patch_cli_download_backends(monkeypatch, backend)
    out_file = tmp_path / "saved.jpg"

    runner = CliRunner()
    result = runner.invoke(
        cli_main.app,
        [
            "messages",
            "download",
            "--chat-id",
            "-100123",
            "--message-id",
            "7",
            "--out",
            str(out_file),
            "--config",
            str(config_file),
        ],
    )
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload["path"] == str(out_file)
    assert backend.download_calls == [
        {"chat_id": -100123, "message_id": 7, "target_path": str(out_file)}
    ]


def test_cli_download_access_denied_exit_3(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_file = _write_config(tmp_path, _config_with_access(_WRITE_ACCESS))
    backend = FakeDownloadBackend()
    _patch_cli_download_backends(monkeypatch, backend)

    runner = CliRunner()
    result = runner.invoke(
        cli_main.app,
        [
            "messages",
            "download",
            "--chat-id",
            "-100123",
            "--message-id",
            "7",
            "--dir",
            str(tmp_path),
            "--config",
            str(config_file),
        ],
    )
    assert result.exit_code == 3, result.stdout
    assert backend.probe_calls == []


def test_cli_download_requires_out_or_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_file = _write_config(tmp_path, _config_with_access(_READ_ACCESS))
    backend = FakeDownloadBackend()
    _patch_cli_download_backends(monkeypatch, backend)

    runner = CliRunner()
    result = runner.invoke(
        cli_main.app,
        [
            "messages",
            "download",
            "--chat-id",
            "-100123",
            "--message-id",
            "7",
            "--config",
            str(config_file),
        ],
    )
    assert result.exit_code == 2


def test_cli_download_rejects_out_and_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_file = _write_config(tmp_path, _config_with_access(_READ_ACCESS))
    backend = FakeDownloadBackend()
    _patch_cli_download_backends(monkeypatch, backend)

    runner = CliRunner()
    result = runner.invoke(
        cli_main.app,
        [
            "messages",
            "download",
            "--chat-id",
            "-100123",
            "--message-id",
            "7",
            "--out",
            str(tmp_path / "a.jpg"),
            "--dir",
            str(tmp_path),
            "--config",
            str(config_file),
        ],
    )
    assert result.exit_code == 2


def test_cli_download_rejects_two_targets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_file = _write_config(tmp_path, _config_with_access(_READ_ACCESS))
    backend = FakeDownloadBackend()
    _patch_cli_download_backends(monkeypatch, backend)

    runner = CliRunner()
    result = runner.invoke(
        cli_main.app,
        [
            "messages",
            "download",
            "--chat-id",
            "-100123",
            "--entity",
            "@other",
            "--message-id",
            "7",
            "--dir",
            str(tmp_path),
            "--config",
            str(config_file),
        ],
    )
    assert result.exit_code == 2
