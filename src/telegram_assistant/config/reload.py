"""Hot-reload support for ``data/config.yml``.

A :class:`ConfigWatcher` observes the live config file (resolved the same way
:func:`load_config` resolves it) and, after a short debounce, invokes a reload
callback. The intended callback re-runs :func:`load_config` and atomically
swaps the config object held on ``app.state`` under a lock so that policy
changes (notably ``telegram.access``) take effect without a server restart.

A parse/validation error during reload is logged and the previous (last-good)
config is kept, so a bad edit can never take the service down.

The watchdog observer and the debounce timer are deliberately separated from
the swap logic: :meth:`ConfigWatcher.trigger` is callable directly, which keeps
the debounce behavior unit-testable without touching the filesystem.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from telegram_assistant.config.loader import ConfigError, load_config
from telegram_assistant.config.models import AppConfig
from telegram_assistant.observability.logging import get_logger

logger = get_logger(__name__)

DEFAULT_DEBOUNCE_SECONDS = 2.0


class _ConfigFileEventHandler(FileSystemEventHandler):
    """Forward filesystem events touching the config file to a callback.

    We watch the *parent directory* (recursive=False) rather than the file
    itself, because many editors and atomic writers replace the file via a
    rename, which would break a file-targeted watch. Events whose source or
    destination path matches the config file name trigger the callback.
    """

    def __init__(self, path: Path, on_event: Callable[[], None]) -> None:
        self._path = path
        self._name = path.name
        self._on_event = on_event

    def _matches(self, event: FileSystemEvent) -> bool:
        for raw in (event.src_path, getattr(event, "dest_path", "")):
            if not raw:
                continue
            if Path(raw).name == self._name:
                return True
        return False

    def on_any_event(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        if self._matches(event):
            self._on_event()


class ConfigWatcher:
    """Debounced filesystem watcher for the config file.

    ``on_reload`` is called (off the watchdog thread, via a debounce timer)
    after edits settle. Rapid successive edits coalesce into a single reload.
    """

    def __init__(
        self,
        path: str | Path,
        on_reload: Callable[[], None],
        *,
        debounce_seconds: float = DEFAULT_DEBOUNCE_SECONDS,
    ) -> None:
        self._path = Path(path)
        self._on_reload = on_reload
        self._debounce_seconds = debounce_seconds
        self._lock = threading.Lock()
        self._timer: threading.Timer | None = None
        self._observer: Any = None
        self._started = False

    @property
    def path(self) -> Path:
        return self._path

    @property
    def started(self) -> bool:
        return self._started

    def trigger(self) -> None:
        """(Re)start the debounce timer; coalesces rapid edits into one reload."""
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
            timer = threading.Timer(self._debounce_seconds, self._fire)
            timer.daemon = True
            self._timer = timer
            timer.start()

    def _fire(self) -> None:
        with self._lock:
            self._timer = None
        try:
            self._on_reload()
        except Exception:  # pragma: no cover - defensive; callback logs its own
            logger.exception("config reload callback raised")

    def start(self) -> None:
        """Begin watching. No-op (with a warning) when the file path is absent."""
        if self._started:
            return
        directory = self._path.parent
        if not directory.exists():
            logger.warning(
                "config hot-reload disabled: directory does not exist",
                directory=str(directory),
            )
            return
        handler = _ConfigFileEventHandler(self._path, self.trigger)
        observer = Observer()
        observer.schedule(handler, str(directory), recursive=False)
        observer.start()
        self._observer = observer
        self._started = True
        logger.info("config hot-reload watching", path=str(self._path))

    def stop(self) -> None:
        """Stop the observer and cancel any pending debounce timer."""
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
        observer = self._observer
        self._observer = None
        self._started = False
        if observer is not None:
            try:
                observer.stop()
                observer.join(timeout=5)
            except Exception:  # pragma: no cover - shutdown best-effort
                logger.exception("config watcher observer failed to stop cleanly")


def reload_config_into_state(
    state: Any,
    path: str | Path,
    *,
    lock: threading.Lock | None = None,
    on_swap: Callable[[AppConfig], None] | None = None,
) -> bool:
    """Reload config from ``path`` and atomically swap it onto ``state``.

    On a successful load the new :class:`AppConfig` replaces ``state.config``
    under ``lock`` and ``on_swap`` (if given) runs to rebuild config-derived
    state. On a :class:`ConfigError` the previous config is kept and the error
    is logged. Returns ``True`` when a new config was applied, ``False`` when
    the reload failed and the previous config was retained.
    """
    try:
        new_config = load_config(path)
    except ConfigError as exc:
        logger.warning(
            "config reload failed; keeping previous config",
            path=str(path),
            error=str(exc),
        )
        return False
    except Exception as exc:  # pragma: no cover - unexpected loader failure
        logger.warning(
            "config reload raised; keeping previous config",
            path=str(path),
            error=str(exc),
        )
        return False

    def _swap() -> None:
        state.config = new_config
        if on_swap is not None:
            on_swap(new_config)

    if lock is not None:
        with lock:
            _swap()
    else:
        _swap()
    logger.info("config reloaded", path=str(path))
    return True


__all__ = [
    "ConfigWatcher",
    "DEFAULT_DEBOUNCE_SECONDS",
    "reload_config_into_state",
]
