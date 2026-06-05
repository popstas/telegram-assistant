"""Lifespan-driven session-manager wiring tests.

The FastAPI factory auto-constructs a :class:`TelethonSessionManager` from
config when no manager is passed (the production uvicorn path). The lifespan
context manager then drives ``get_client()`` on startup and ``disconnect()``
on shutdown. Without the auto-construct the service would come up
permanently unauthorized, which is the bug Task 18 surfaced.
"""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from telegram_assistant.config import load_config_from_text
from telegram_assistant.http_api import create_app
from telegram_assistant.telegram_client import session as session_module


class _FakeFolder:
    def __init__(self, folder_id: int, title: str) -> None:
        self.id = folder_id
        self.title = title
        self.include_peers: list[Any] = []
        self.pinned_peers: list[Any] = []


class _FakeFilters:
    def __init__(self, filters: list[Any]) -> None:
        self.filters = filters


class _FakeClient:
    def __init__(self) -> None:
        self.connect_calls = 0
        self.disconnect_calls = 0
        self.authorized = True
        # One filter matching the minimal_config_yaml folder so the health
        # probe's resolve_folder call succeeds.
        self.folders = [_FakeFolder(2, "Planfix clients")]

    async def connect(self) -> None:
        self.connect_calls += 1

    async def disconnect(self) -> None:
        self.disconnect_calls += 1

    async def is_user_authorized(self) -> bool:
        return self.authorized

    async def get_me(self) -> Any:
        return type("Me", (), {"id": 1, "username": "fake"})()

    async def __call__(self, _request: Any) -> Any:
        return _FakeFilters(list(self.folders))


def test_create_app_auto_constructs_session_manager_and_connects_on_startup(
    monkeypatch: Any, minimal_config_yaml: str
) -> None:
    """When no session_manager is injected the factory builds one and the
    lifespan hooks call ``connect`` on startup and ``disconnect`` on shutdown.
    """
    fake = _FakeClient()
    monkeypatch.setattr(
        session_module,
        "_default_factory",
        lambda session_path, api_id, api_hash: fake,
    )

    config = load_config_from_text(minimal_config_yaml)
    app = create_app(config)

    # Lifespan hooks only run inside a TestClient context.
    with TestClient(app) as client:
        # Startup happened: connect was called via get_client().
        assert fake.connect_calls >= 1
        response = client.get("/health")
        assert response.status_code == 200
        body = response.json()
        assert body["telegram_session"] == "authorized"
        assert body["default_folder"] == "ok"

    # After the context exits the shutdown hook disconnected the client.
    assert fake.disconnect_calls >= 1


def test_create_app_schedules_retry_when_initial_connect_fails(
    monkeypatch: Any, minimal_config_yaml: str
) -> None:
    """A transient Telegram outage at startup must not lock the service into
    permanent 503 mode. When the first ``get_client()`` raises, the lifespan
    spawns a background retry task that eventually recovers the connection.
    """

    class _FlakyClient(_FakeClient):
        def __init__(self, fail_first_n: int) -> None:
            super().__init__()
            self._remaining_failures = fail_first_n

        async def connect(self) -> None:
            if self._remaining_failures > 0:
                self._remaining_failures -= 1
                raise RuntimeError("telegram unreachable")
            await super().connect()

    flaky = _FlakyClient(fail_first_n=1)
    monkeypatch.setattr(
        session_module,
        "_default_factory",
        lambda session_path, api_id, api_hash: flaky,
    )

    config = load_config_from_text(minimal_config_yaml)
    app = create_app(config)

    with TestClient(app) as client:
        # Startup connect raised, so the cached client must be cleared so the
        # background retry can re-create it.
        assert flaky.connect_calls == 0

        # Drive the event loop forward so the retry task gets a chance to
        # run. The retry waits ~1s on the first attempt, then succeeds.
        import time as _time

        deadline = _time.monotonic() + 5.0
        while _time.monotonic() < deadline:
            if flaky.connect_calls >= 1:
                break
            _time.sleep(0.1)
            # Hit /health to keep the loop active.
            client.get("/health")

        assert flaky.connect_calls >= 1


def test_create_app_skips_lifespan_when_explicit_session_manager_passed(
    minimal_config_yaml: str,
) -> None:
    """If the caller wires their own session_manager (the test path) the
    factory must not auto-construct another one — the explicit manager is
    authoritative and lifespan stays a no-op.
    """
    config = load_config_from_text(minimal_config_yaml)

    # Passing session_manager=None *implicitly* triggers auto-construction.
    # Passing an actual manager opts out. We assert opt-out by checking that
    # the app boots without invoking any Telethon factory.
    class _Sentinel:
        def __init__(self) -> None:
            self.connect_attempts = 0

        async def get_client(self) -> Any:
            self.connect_attempts += 1
            return self

    sentinel = _Sentinel()
    app = create_app(config, session_manager=sentinel)  # type: ignore[arg-type]
    with TestClient(app) as client:
        response = client.get("/health")
        assert response.status_code == 200

    # The lifespan does not call get_client on explicitly-supplied managers.
    assert sentinel.connect_attempts == 0
