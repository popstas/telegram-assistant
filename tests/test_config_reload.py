"""Tests for config hot-reload (watchdog watcher + atomic swap)."""

from __future__ import annotations

import threading
import time
import types
from pathlib import Path

import pytest

from telegram_assistant.config import (
    AppConfig,
    ConfigWatcher,
    load_config,
    reload_config_into_state,
)
from telegram_assistant.config.loader import resolve_config_path


def _write(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


def test_reload_swaps_config_on_valid_edit(tmp_path: Path, minimal_config_yaml: str) -> None:
    cfg_path = tmp_path / "config.yml"
    _write(cfg_path, minimal_config_yaml)
    original = load_config(cfg_path)
    state = types.SimpleNamespace(config=original)

    # Edit: bump the logging level (DEBUG) so we can observe the swap.
    edited = minimal_config_yaml.replace("level: INFO", "level: DEBUG")
    _write(cfg_path, edited)

    applied = reload_config_into_state(state, cfg_path)

    assert applied is True
    assert isinstance(state.config, AppConfig)
    assert state.config is not original
    assert state.config.logging.level == "DEBUG"


def test_reload_keeps_old_config_on_invalid_edit(
    tmp_path: Path, minimal_config_yaml: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg_path = tmp_path / "config.yml"
    _write(cfg_path, minimal_config_yaml)
    original = load_config(cfg_path)
    state = types.SimpleNamespace(config=original)

    # Corrupt the file: not a mapping / invalid YAML structure.
    _write(cfg_path, "telegram: [this is not valid")

    # Capture the structured warning instead of relying on the stderr sink.
    from telegram_assistant.config import reload as reload_mod

    warnings: list[tuple] = []
    monkeypatch.setattr(
        reload_mod.logger,
        "warning",
        lambda *a, **k: warnings.append((a, k)),
    )

    applied = reload_config_into_state(state, cfg_path)

    assert applied is False
    # Previous config retained unchanged.
    assert state.config is original
    # The failure is logged.
    assert warnings
    assert "keeping previous config" in warnings[0][0][0]


def test_reload_runs_on_swap_callback(tmp_path: Path, minimal_config_yaml: str) -> None:
    cfg_path = tmp_path / "config.yml"
    _write(cfg_path, minimal_config_yaml)
    state = types.SimpleNamespace(config=load_config(cfg_path))
    seen: list[AppConfig] = []

    applied = reload_config_into_state(
        state, cfg_path, lock=threading.Lock(), on_swap=seen.append
    )

    assert applied is True
    assert seen == [state.config]


def test_reload_does_not_run_on_swap_when_invalid(
    tmp_path: Path, minimal_config_yaml: str
) -> None:
    cfg_path = tmp_path / "config.yml"
    _write(cfg_path, minimal_config_yaml)
    state = types.SimpleNamespace(config=load_config(cfg_path))
    _write(cfg_path, "= broken =")
    called: list[AppConfig] = []

    applied = reload_config_into_state(state, cfg_path, on_swap=called.append)

    assert applied is False
    assert called == []


def test_watcher_debounce_coalesces_rapid_edits() -> None:
    calls: list[int] = []
    watcher = ConfigWatcher(
        Path("/nonexistent/config.yml"),
        lambda: calls.append(1),
        debounce_seconds=0.15,
    )

    # Fire several triggers faster than the debounce window.
    for _ in range(5):
        watcher.trigger()
        time.sleep(0.01)

    time.sleep(0.3)
    assert calls == [1]


def test_watcher_separate_bursts_fire_separately() -> None:
    calls: list[int] = []
    watcher = ConfigWatcher(
        Path("/nonexistent/config.yml"),
        lambda: calls.append(1),
        debounce_seconds=0.1,
    )

    watcher.trigger()
    time.sleep(0.25)
    watcher.trigger()
    time.sleep(0.25)

    assert calls == [1, 1]


def test_watcher_start_stop_lifecycle(tmp_path: Path, minimal_config_yaml: str) -> None:
    cfg_path = tmp_path / "config.yml"
    _write(cfg_path, minimal_config_yaml)
    calls: list[int] = []
    watcher = ConfigWatcher(cfg_path, lambda: calls.append(1), debounce_seconds=0.1)

    watcher.start()
    try:
        assert watcher.started is True
        # start() is idempotent.
        watcher.start()
        assert watcher.started is True
        # A manual trigger still flows through the debounce timer.
        watcher.trigger()
        time.sleep(0.25)
        assert calls == [1]
    finally:
        watcher.stop()
    assert watcher.started is False


def test_watcher_start_noop_when_directory_missing() -> None:
    watcher = ConfigWatcher(
        Path("/nonexistent-dir-xyz/config.yml"), lambda: None
    )
    watcher.start()
    assert watcher.started is False
    watcher.stop()  # no-op, must not raise


def test_watcher_detects_real_file_change(
    tmp_path: Path, minimal_config_yaml: str
) -> None:
    cfg_path = tmp_path / "config.yml"
    _write(cfg_path, minimal_config_yaml)
    fired = threading.Event()
    watcher = ConfigWatcher(cfg_path, fired.set, debounce_seconds=0.1)

    watcher.start()
    try:
        time.sleep(0.1)
        _write(cfg_path, minimal_config_yaml.replace("level: INFO", "level: DEBUG"))
        assert fired.wait(timeout=5.0) is True
    finally:
        watcher.stop()


def test_resolve_config_path_explicit(tmp_path: Path) -> None:
    p = tmp_path / "x.yml"
    assert resolve_config_path(p) == p


def test_resolve_config_path_none_when_absent(tmp_path: Path, monkeypatch) -> None:
    # No data/config.yml in an empty CWD and a nonexistent user path.
    monkeypatch.chdir(tmp_path)
    from telegram_assistant.config import loader as loader_mod

    monkeypatch.setattr(
        loader_mod, "USER_CONFIG_PATH", tmp_path / "nope" / "config.yml"
    )
    assert resolve_config_path() is None


def test_watched_directory_resolves_symlinked_parent(tmp_path: Path) -> None:
    """The watch must resolve to the real dir when the parent is a symlink.

    Mirrors Docker's ``/app/data -> /data`` symlink: an inotify watch scheduled
    on the symlink path receives no events, so ConfigWatcher resolves to the
    real directory before scheduling.
    """
    real = tmp_path / "real_data"
    real.mkdir()
    (real / "config.yml").write_text("x", encoding="utf-8")
    link = tmp_path / "linked_data"
    link.symlink_to(real, target_is_directory=True)

    watcher = ConfigWatcher(link / "config.yml", lambda: None)
    assert watcher.watched_directory == real.resolve()


def test_watcher_fires_reload_through_symlinked_dir(tmp_path: Path) -> None:
    """End-to-end: an in-place edit via a symlinked dir triggers on_reload."""
    real = tmp_path / "real_data"
    real.mkdir()
    cfg = real / "config.yml"
    cfg.write_text("a: 1\n", encoding="utf-8")
    link = tmp_path / "linked_data"
    link.symlink_to(real, target_is_directory=True)

    fired = threading.Event()
    watcher = ConfigWatcher(link / "config.yml", fired.set, debounce_seconds=0.2)
    watcher.start()
    try:
        # in-place modify of the real file (same inode), as a host edit does
        with cfg.open("a", encoding="utf-8") as fh:
            fh.write("# edit\n")
        assert fired.wait(timeout=5.0), "reload did not fire on symlinked-dir edit"
    finally:
        watcher.stop()


def test_handler_ignores_read_access_events(tmp_path: Path) -> None:
    """Read-access events (opened) must not trigger a reload.

    Regression: the reload reads config.yml, emitting opened/closed_no_write
    events. Reacting to those re-armed the debounce and reloaded forever in a
    ~debounce-period cycle. Only content changes should trigger.
    """
    from watchdog.events import FileModifiedEvent, FileOpenedEvent

    from telegram_assistant.config.reload import _ConfigFileEventHandler

    cfg = tmp_path / "config.yml"
    calls: list[int] = []
    handler = _ConfigFileEventHandler(cfg, lambda: calls.append(1))

    # read-access (from the reload's own open/read) must NOT trigger
    handler.on_any_event(FileOpenedEvent(str(cfg)))
    assert calls == []

    # a content change MUST trigger
    handler.on_any_event(FileModifiedEvent(str(cfg)))
    assert calls == [1]


def test_watcher_does_not_self_retrigger_on_reload_read(
    tmp_path: Path, minimal_config_yaml: str
) -> None:
    """End-to-end: a reload that reads the file must not start a new cycle.

    Start the watcher, fire one real edit, let it reload (which reads the
    file), then confirm no further reloads occur once edits stop.
    """
    cfg = tmp_path / "config.yml"
    _write(cfg, minimal_config_yaml)
    state = types.SimpleNamespace(config=load_config(cfg))
    reloads = 0
    lock = threading.Lock()

    def on_reload() -> None:
        nonlocal reloads
        with lock:
            reloads += 1
        reload_config_into_state(state, cfg)  # reads the file (open/close)

    watcher = ConfigWatcher(cfg, on_reload, debounce_seconds=0.2)
    watcher.start()
    try:
        with cfg.open("a", encoding="utf-8") as fh:
            fh.write("\n# edit\n")
        time.sleep(1.0)  # allow the edit-triggered reload to run
        with lock:
            after_edit = reloads
        assert after_edit >= 1, "edit did not trigger a reload"
        time.sleep(1.5)  # no further edits — must not self-retrigger
        with lock:
            final = reloads
        assert final == after_edit, f"watcher self-retriggered: {after_edit} -> {final}"
    finally:
        watcher.stop()


# ---------------------------------------------------------------------------
# App-level `on_swap`: config-derived state rebuilt on hot-reload
#
# `create_app` builds the folder-membership cache and the pin rate gate once,
# at startup, from the config it was given. The watcher's `on_swap` closure is
# what keeps them honest afterwards — it clears a cache keyed to the old access
# rules, and builds either store when the edit switches its feature on. All
# three paths swallow their failures, so without a test they would degrade
# silently for the rest of the process' life.
# ---------------------------------------------------------------------------


def _watched_config(
    *, session: Path, access: str = "", pin_interval: float = 0.0
) -> str:
    return (
        "telegram:\n"
        "  api_id: 1\n"
        "  api_hash: h\n"
        f"  session_path: {session}\n"
        f"  pin_min_interval_seconds: {pin_interval}\n"
        "  default_chat_folder:\n"
        "    folder_id: 2\n"
        "    folder_name: F\n"
        f"{access}"
        "http:\n"
        "  host: 0.0.0.0\n"
        "  port: 8085\n"
        "  bearer_token: t\n"
    )


_ACCESS_BLOCK = (
    "  access:\n"
    "    rules:\n"
    "      - folder: F\n"
    "        permission: write\n"
)


def _watched_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: str):
    """`create_app()` reading `data/config.yml` from a tmp CWD (watcher on).

    Config hot-reload is only wired when `create_app` loads the config from
    disk itself, so the app cannot take an injected `AppConfig` here. Telethon
    is stubbed out so no session manager (and no connect-retry task) is built.
    """
    from telegram_assistant.http_api import app as app_module

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    cfg_path = data_dir / "config.yml"
    _write(cfg_path, body)
    monkeypatch.chdir(tmp_path)

    def _no_telethon(*args, **kwargs):
        raise RuntimeError("no Telethon session manager in tests")

    monkeypatch.setattr(app_module, "TelethonSessionManager", _no_telethon)
    app = app_module.create_app(database_path=tmp_path / "state.db")
    return app, cfg_path


def _swap(app, cfg_path: Path, body: str) -> None:
    """Write a new config and run the app's reload callback synchronously.

    Calls the watcher's callback directly rather than waiting on inotify + the
    2s debounce — the watcher's own plumbing is covered by the tests above; the
    subject here is what `on_swap` does to `app.state`.
    """
    _write(cfg_path, body)
    watcher = app.state.config_watcher
    assert watcher is not None, "config watcher was not started"
    watcher._on_reload()


def test_swap_clears_folder_membership_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Access-rule edits must not be answered from a map built under old rules."""
    from fastapi.testclient import TestClient

    from telegram_assistant.persistence import FolderMembershipCache

    session = tmp_path / "sess.session"
    body = _watched_config(session=session, access=_ACCESS_BLOCK)
    app, cfg_path = _watched_app(tmp_path, monkeypatch, body)

    with TestClient(app):
        cache = app.state.folder_membership_cache
        assert cache is not None
        cache.save({2: ("F", {10, 11})}, fetched_at=time.time())
        assert cache.load() is not None

        _swap(
            app,
            cfg_path,
            _watched_config(
                session=session,
                access=_ACCESS_BLOCK.replace("permission: write", "permission: read"),
            ),
        )

        assert app.state.config.telegram.access.rules[0].effective_permissions == [
            "read"
        ]
        assert FolderMembershipCache(tmp_path / "state.db").load() is None


def test_swap_builds_folder_membership_cache_when_access_added(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A policy added at runtime gets its cache, not live fetches forever."""
    from fastapi.testclient import TestClient

    session = tmp_path / "sess.session"
    app, cfg_path = _watched_app(
        tmp_path, monkeypatch, _watched_config(session=session)
    )

    with TestClient(app):
        assert app.state.folder_membership_cache is None
        _swap(
            app,
            cfg_path,
            _watched_config(session=session, access=_ACCESS_BLOCK),
        )
        assert app.state.folder_membership_cache is not None


def test_swap_builds_rate_gate_when_pacing_enabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Raising `pin_min_interval_seconds` from 0 must not need a restart."""
    from fastapi.testclient import TestClient

    session = tmp_path / "sess.session"
    app, cfg_path = _watched_app(
        tmp_path, monkeypatch, _watched_config(session=session, pin_interval=0.0)
    )

    with TestClient(app):
        assert app.state.rate_gate_store is None
        _swap(
            app,
            cfg_path,
            _watched_config(session=session, pin_interval=2.0),
        )
        assert app.state.rate_gate_store is not None
        assert app.state.config.telegram.pin_min_interval_seconds == 2.0
