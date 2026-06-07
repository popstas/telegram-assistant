"""Tests for the /health endpoint, `health` CLI, and shared probes."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from telegram_assistant.cli import main as cli_main
from telegram_assistant.config import load_config_from_text
from telegram_assistant.health import (
    DATABASE_ERROR,
    DATABASE_OK,
    FOLDER_MISSING,
    FOLDER_OK,
    SESSION_AUTHORIZED,
    SESSION_UNAUTHORIZED,
    HealthReport,
    collect_health,
    default_database_path,
    probe_database,
    probe_default_folder,
    probe_telegram_session,
)
from telegram_assistant.http_api import create_app


@dataclass
class _FakeMe:
    id: int = 1
    username: str = "tech"


class _FakeFolder:
    def __init__(self, *, folder_id: int, title: str) -> None:
        self.id = folder_id
        self.title = title
        self.include_peers: list[Any] = []
        self.pinned_peers: list[Any] = []


class _FakeFilters:
    def __init__(self, filters: list[Any]) -> None:
        self.filters = filters


class _FakeFolderClient:
    """Telethon client double serving GetDialogFiltersRequest only."""

    def __init__(self, folders: list[_FakeFolder]) -> None:
        self._folders = folders

    async def __call__(self, _request: Any) -> Any:
        return _FakeFilters(list(self._folders))


class _FakeManager:
    """Mimics the slice of TelethonSessionManager that health probes touch."""

    def __init__(
        self,
        *,
        authorized: bool,
        raise_on_state: bool = False,
        folders: list[_FakeFolder] | None = None,
    ) -> None:
        self._authorized = authorized
        self._raise = raise_on_state
        self.state_calls = 0
        # Default to one folder matching ``minimal_config_yaml`` so the real
        # folder probe succeeds without per-test wiring.
        self._folders = (
            folders
            if folders is not None
            else [_FakeFolder(folder_id=2, title="Planfix clients")]
        )

    async def state(self) -> Any:
        self.state_calls += 1
        if self._raise:
            raise RuntimeError("telegram unreachable")
        from telegram_assistant.telegram_client.session import SessionState

        return SessionState(
            authorized=self._authorized,
            account_label="test",
            session_path="/tmp/test.session",
            me=_FakeMe() if self._authorized else None,
        )

    async def get_client(self) -> Any:
        return _FakeFolderClient(self._folders)


# ---------------------------------------------------------------------------
# Individual probe behavior
# ---------------------------------------------------------------------------


async def test_probe_telegram_session_returns_unauthorized_without_manager() -> None:
    assert await probe_telegram_session(None) == SESSION_UNAUTHORIZED


async def test_probe_telegram_session_authorized() -> None:
    assert (
        await probe_telegram_session(_FakeManager(authorized=True)) == SESSION_AUTHORIZED
    )


async def test_probe_telegram_session_unauthorized() -> None:
    assert (
        await probe_telegram_session(_FakeManager(authorized=False)) == SESSION_UNAUTHORIZED
    )


async def test_probe_telegram_session_swallows_errors() -> None:
    assert (
        await probe_telegram_session(_FakeManager(authorized=False, raise_on_state=True))
        == SESSION_UNAUTHORIZED
    )


async def test_probe_database_ok_creates_parent(tmp_path: Path) -> None:
    db_path = tmp_path / "nested" / "state.db"
    assert await probe_database(db_path) == DATABASE_OK
    assert db_path.exists()


async def test_probe_database_error_on_invalid_path(tmp_path: Path) -> None:
    # A directory at the file path makes sqlite3.connect fail to open.
    bad_path = tmp_path / "state.db"
    bad_path.mkdir()
    assert await probe_database(bad_path) == DATABASE_ERROR


async def test_probe_default_folder_missing_without_manager() -> None:
    assert await probe_default_folder(None, "Planfix clients") == FOLDER_MISSING


async def test_probe_default_folder_missing_when_session_unauthorized() -> None:
    manager = _FakeManager(authorized=False)
    assert await probe_default_folder(manager, "Planfix clients") == FOLDER_MISSING


async def test_probe_default_folder_ok_when_session_authorized() -> None:
    manager = _FakeManager(authorized=True)
    assert await probe_default_folder(manager, "Planfix clients") == FOLDER_OK


async def test_probe_default_folder_missing_on_error() -> None:
    manager = _FakeManager(authorized=True, raise_on_state=True)
    assert await probe_default_folder(manager, "Planfix clients") == FOLDER_MISSING


# ---------------------------------------------------------------------------
# collect_health aggregation
# ---------------------------------------------------------------------------


async def test_collect_health_all_ok(minimal_config_yaml: str, tmp_path: Path) -> None:
    config = load_config_from_text(minimal_config_yaml)
    db_path = tmp_path / "state.db"
    manager = _FakeManager(authorized=True)

    report = await collect_health(
        config,
        session_manager=manager,
        database_path=db_path,
    )

    assert report == HealthReport(
        status="ok",
        telegram_session=SESSION_AUTHORIZED,
        database=DATABASE_OK,
        default_folder=FOLDER_OK,
    )


async def test_collect_health_fetches_session_state_once(
    minimal_config_yaml: str, tmp_path: Path
) -> None:
    """The production path must not re-probe the session per sub-check."""
    config = load_config_from_text(minimal_config_yaml)
    manager = _FakeManager(authorized=True)

    report = await collect_health(
        config,
        session_manager=manager,
        database_path=tmp_path / "state.db",
    )

    assert manager.state_calls == 1
    assert report.telegram_session == SESSION_AUTHORIZED
    assert report.default_folder == FOLDER_OK


async def test_collect_health_times_out_slow_session_probe(
    minimal_config_yaml: str, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    class _SlowManager(_FakeManager):
        async def state(self) -> Any:
            await asyncio.sleep(10)
            return await super().state()

    config = load_config_from_text(minimal_config_yaml)
    with caplog.at_level("WARNING", logger="telegram_assistant.health"):
        report = await collect_health(
            config,
            session_manager=_SlowManager(authorized=True),
            database_path=tmp_path / "state.db",
            telegram_probe_timeout_seconds=0.01,
        )

    assert "telegram health session state timeout" in caplog.text
    assert report.status == "ok"
    assert report.telegram_session == SESSION_UNAUTHORIZED
    assert report.default_folder == FOLDER_MISSING


async def test_collect_health_preserves_authorized_session_when_folder_probe_times_out(
    minimal_config_yaml: str, tmp_path: Path
) -> None:
    class _SlowFolderClient(_FakeFolderClient):
        async def __call__(self, _request: Any) -> Any:
            await asyncio.sleep(10)
            return await super().__call__(_request)

    class _SlowFolderManager(_FakeManager):
        async def get_client(self) -> Any:
            return _SlowFolderClient(self._folders)

    config = load_config_from_text(minimal_config_yaml)
    report = await collect_health(
        config,
        session_manager=_SlowFolderManager(authorized=True),
        database_path=tmp_path / "state.db",
        telegram_probe_timeout_seconds=0.01,
    )

    assert report.telegram_session == SESSION_AUTHORIZED
    assert report.default_folder == FOLDER_MISSING


async def test_collect_health_unauthorized_session(
    minimal_config_yaml: str, tmp_path: Path
) -> None:
    config = load_config_from_text(minimal_config_yaml)
    report = await collect_health(
        config,
        session_manager=_FakeManager(authorized=False),
        database_path=tmp_path / "state.db",
    )
    assert report.status == "ok"
    assert report.telegram_session == SESSION_UNAUTHORIZED
    assert report.database == DATABASE_OK
    assert report.default_folder == FOLDER_MISSING


async def test_collect_health_database_error(
    minimal_config_yaml: str, tmp_path: Path
) -> None:
    bad_db = tmp_path / "state.db"
    bad_db.mkdir()
    config = load_config_from_text(minimal_config_yaml)
    report = await collect_health(
        config,
        session_manager=_FakeManager(authorized=True),
        database_path=bad_db,
    )
    assert report.database == DATABASE_ERROR
    assert report.telegram_session == SESSION_AUTHORIZED
    assert report.default_folder == FOLDER_OK


async def test_collect_health_uses_default_database_path(
    minimal_config_yaml: str,
) -> None:
    config = load_config_from_text(minimal_config_yaml)
    expected = default_database_path(config)
    assert expected.name == "state.db"


async def test_collect_health_respects_injected_probes(minimal_config_yaml: str) -> None:
    config = load_config_from_text(minimal_config_yaml)

    async def session() -> str:
        return SESSION_UNAUTHORIZED

    async def database() -> str:
        return DATABASE_ERROR

    async def folder() -> str:
        return FOLDER_MISSING

    report = await collect_health(
        config,
        session_probe=session,
        database_probe=database,
        folder_probe=folder,
    )
    assert report.telegram_session == SESSION_UNAUTHORIZED
    assert report.database == DATABASE_ERROR
    assert report.default_folder == FOLDER_MISSING


# ---------------------------------------------------------------------------
# HTTP /health endpoint
# ---------------------------------------------------------------------------


def _make_client(
    minimal_config_yaml: str,
    *,
    session_manager: Any = None,
    database_path: Path | None = None,
) -> TestClient:
    config = load_config_from_text(minimal_config_yaml)
    return TestClient(
        create_app(
            config,
            session_manager=session_manager,
            database_path=database_path,
        )
    )


def test_health_endpoint_reports_all_fields_unauthorized_without_manager(
    minimal_config_yaml: str, tmp_path: Path
) -> None:
    client = _make_client(
        minimal_config_yaml,
        session_manager=None,
        database_path=tmp_path / "state.db",
    )
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["telegram_session"] == SESSION_UNAUTHORIZED
    assert body["database"] == DATABASE_OK
    assert body["default_folder"] == FOLDER_MISSING
    assert body["version"]


def test_health_endpoint_reports_authorized(
    minimal_config_yaml: str, tmp_path: Path
) -> None:
    client = _make_client(
        minimal_config_yaml,
        session_manager=_FakeManager(authorized=True),
        database_path=tmp_path / "state.db",
    )
    body = client.get("/health").json()
    assert body["telegram_session"] == SESSION_AUTHORIZED
    assert body["default_folder"] == FOLDER_OK


def test_health_endpoint_reports_database_error(
    minimal_config_yaml: str, tmp_path: Path
) -> None:
    bad_db = tmp_path / "state.db"
    bad_db.mkdir()
    client = _make_client(
        minimal_config_yaml,
        session_manager=_FakeManager(authorized=True),
        database_path=bad_db,
    )
    body = client.get("/health").json()
    assert body["database"] == DATABASE_ERROR


def test_health_endpoint_responds_even_when_session_probe_raises(
    minimal_config_yaml: str, tmp_path: Path
) -> None:
    client = _make_client(
        minimal_config_yaml,
        session_manager=_FakeManager(authorized=True, raise_on_state=True),
        database_path=tmp_path / "state.db",
    )
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["telegram_session"] == SESSION_UNAUTHORIZED
    assert body["default_folder"] == FOLDER_MISSING


# ---------------------------------------------------------------------------
# CLI `health`
# ---------------------------------------------------------------------------


def _write_config(tmp_path: Path, body: str) -> Path:
    config_file = tmp_path / "config.yml"
    config_file.write_text(body)
    return config_file


@pytest.mark.parametrize(
    "authorized,expected_session,expected_folder",
    [
        (True, SESSION_AUTHORIZED, FOLDER_OK),
        (False, SESSION_UNAUTHORIZED, FOLDER_MISSING),
    ],
)
def test_cli_health_emits_json_payload(
    minimal_config_yaml: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    authorized: bool,
    expected_session: str,
    expected_folder: str,
) -> None:
    config_file = _write_config(tmp_path, minimal_config_yaml)
    db_path = tmp_path / "state.db"

    fake_manager = _FakeManager(authorized=authorized)

    def fake_manager_factory(telegram_config: Any) -> Any:
        # Attach a disconnect coroutine so the CLI's cleanup branch is exercised.
        async def _disconnect() -> None:
            return None

        fake_manager.disconnect = _disconnect  # type: ignore[attr-defined]
        return fake_manager

    monkeypatch.setattr(cli_main, "TelethonSessionManager", fake_manager_factory)
    monkeypatch.setattr(cli_main, "default_database_path", lambda cfg: db_path, raising=False)

    # Force the database probe to a known path by patching collect_health's
    # default through the module the CLI imports.
    from telegram_assistant import health as health_mod

    monkeypatch.setattr(health_mod, "default_database_path", lambda cfg: db_path)

    runner = CliRunner()
    result = runner.invoke(cli_main.app, ["health", "--config", str(config_file)])
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload["status"] == "ok"
    assert payload["telegram_session"] == expected_session
    assert payload["default_folder"] == expected_folder
    assert payload["database"] == DATABASE_OK


def test_cli_health_matches_http_payload(
    minimal_config_yaml: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_file = _write_config(tmp_path, minimal_config_yaml)
    db_path = tmp_path / "state.db"

    fake_manager = _FakeManager(authorized=True)

    async def _disconnect() -> None:
        return None

    fake_manager.disconnect = _disconnect  # type: ignore[attr-defined]

    monkeypatch.setattr(
        cli_main, "TelethonSessionManager", lambda _telegram_config: fake_manager
    )
    from telegram_assistant import health as health_mod

    monkeypatch.setattr(health_mod, "default_database_path", lambda cfg: db_path)

    runner = CliRunner()
    cli_result = runner.invoke(cli_main.app, ["health", "--config", str(config_file)])
    assert cli_result.exit_code == 0, cli_result.stdout
    cli_payload = json.loads(cli_result.stdout.strip().splitlines()[-1])

    http_client = _make_client(
        minimal_config_yaml,
        session_manager=fake_manager,
        database_path=db_path,
    )
    http_payload = http_client.get("/health").json()
    http_payload.pop("version", None)

    assert cli_payload == http_payload


def test_cli_health_missing_config_exits_with_error(tmp_path: Path) -> None:
    runner = CliRunner()
    missing = tmp_path / "nope.yml"
    result = runner.invoke(cli_main.app, ["health", "--config", str(missing)])
    assert result.exit_code == 2
