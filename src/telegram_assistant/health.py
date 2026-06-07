"""Service-wide health probes used by both `GET /health` and `health` CLI."""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from collections.abc import Awaitable, Callable
from contextlib import closing
from dataclasses import asdict, dataclass
from pathlib import Path

from telegram_assistant.config.models import AppConfig
from telegram_assistant.telegram_client.session import (
    TelethonSessionManager,
)

logger = logging.getLogger(__name__)

SessionProbe = Callable[[], Awaitable[str]]
DatabaseProbe = Callable[[], Awaitable[str]]
FolderProbe = Callable[[], Awaitable[str]]

SESSION_AUTHORIZED = "authorized"
SESSION_UNAUTHORIZED = "unauthorized"
DATABASE_OK = "ok"
DATABASE_ERROR = "error"
FOLDER_OK = "ok"
FOLDER_MISSING = "missing"
DEFAULT_TELEGRAM_PROBE_TIMEOUT_SECONDS = 10.0


@dataclass(frozen=True)
class HealthReport:
    """Result of a single `/health` probe sweep."""

    status: str
    telegram_session: str
    database: str
    default_folder: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


def default_database_path(config: AppConfig) -> Path:
    """Database file path derived from the Telethon session location."""
    session = Path(config.telegram.session_path).expanduser()
    return session.parent / "state.db"


async def probe_telegram_session(manager: TelethonSessionManager | None) -> str:
    """Return ``authorized``/``unauthorized`` without raising."""
    if manager is None:
        return SESSION_UNAUTHORIZED
    try:
        state = await manager.state()
    except Exception:
        return SESSION_UNAUTHORIZED
    return SESSION_AUTHORIZED if state.authorized else SESSION_UNAUTHORIZED


async def probe_database(database_path: Path) -> str:
    """Verify the SQLite database file is reachable and queryable."""
    try:
        database_path.parent.mkdir(parents=True, exist_ok=True)
        # `with sqlite3.connect(...)` only commits/rolls back on exit; it does
        # not close the connection. Wrap in `closing(...)` so liveness-probe
        # traffic doesn't leak descriptors until GC finalizes them.
        with closing(sqlite3.connect(str(database_path))) as conn:
            conn.execute("SELECT 1").fetchone()
        return DATABASE_OK
    except Exception:
        return DATABASE_ERROR


async def probe_default_folder(
    manager: TelethonSessionManager | None,
    folder_name: str,
    *,
    folder_id: int | None = None,
) -> str:
    """Probe whether the configured chat folder exists in Telegram.

    Reports ``missing`` when the Telethon session is unauthorized, when the
    folder cannot be listed, or when no folder matches ``folder_name`` (and
    ``folder_id``, when supplied). The probe never auto-creates a folder.
    """
    if manager is None:
        return FOLDER_MISSING
    try:
        state = await manager.state()
    except Exception:
        return FOLDER_MISSING
    if not state.authorized:
        return FOLDER_MISSING
    return await _resolve_folder_status(
        manager, folder_name, folder_id=folder_id
    )


async def _resolve_folder_status(
    manager: TelethonSessionManager,
    folder_name: str,
    *,
    folder_id: int | None = None,
) -> str:
    """Resolve folder presence on an already-known-authorized session.

    Split out from :func:`probe_default_folder` so :func:`collect_health` can
    skip the redundant ``state()`` round-trip when it has already confirmed the
    session is authorized.
    """
    try:
        client = await manager.get_client()
    except Exception:
        return FOLDER_MISSING
    try:
        from telegram_assistant.folders import (
            TelethonFolderBackend,
            resolve_folder,
        )

        backend = TelethonFolderBackend(client)
        await resolve_folder(
            backend, folder_name=folder_name, folder_id=folder_id
        )
    except Exception:
        return FOLDER_MISSING
    return FOLDER_OK


async def _probe_session_and_folder(
    manager: TelethonSessionManager | None,
    folder_name: str,
    *,
    folder_id: int | None = None,
    timeout_seconds: float = DEFAULT_TELEGRAM_PROBE_TIMEOUT_SECONDS,
) -> tuple[str, str]:
    """Return ``(session_status, folder_status)`` from a single ``state()`` call.

    Used by :func:`collect_health` on the production path so one ``/health``
    poll makes one Telegram round-trip instead of three.
    """
    if manager is None:
        logger.info("telegram health probe skipped reason=no_session_manager")
        return SESSION_UNAUTHORIZED, FOLDER_MISSING
    logger.info("telegram health probe start folder_name=%s folder_id=%s", folder_name, folder_id)
    try:
        state = await asyncio.wait_for(manager.state(), timeout=timeout_seconds)
    except TimeoutError:
        logger.warning(
            "telegram health session state timeout timeout_seconds=%s",
            timeout_seconds,
        )
        return SESSION_UNAUTHORIZED, FOLDER_MISSING
    except Exception as exc:
        logger.warning(
            "telegram health session state failed error_type=%s error=%s",
            type(exc).__name__,
            exc,
        )
        return SESSION_UNAUTHORIZED, FOLDER_MISSING
    logger.info(
        "telegram health session state result authorized=%s",
        bool(state.authorized),
    )
    if not state.authorized:
        return SESSION_UNAUTHORIZED, FOLDER_MISSING
    logger.info("telegram health folder probe start folder_name=%s folder_id=%s", folder_name, folder_id)
    try:
        folder_status = await asyncio.wait_for(
            _resolve_folder_status(manager, folder_name, folder_id=folder_id),
            timeout=timeout_seconds,
        )
    except TimeoutError:
        logger.warning(
            "telegram health folder probe timeout timeout_seconds=%s folder_name=%s folder_id=%s",
            timeout_seconds,
            folder_name,
            folder_id,
        )
        folder_status = FOLDER_MISSING
    logger.info("telegram health folder probe result status=%s", folder_status)
    return SESSION_AUTHORIZED, folder_status


async def collect_health(
    config: AppConfig,
    *,
    session_manager: TelethonSessionManager | None = None,
    database_path: Path | None = None,
    session_probe: SessionProbe | None = None,
    database_probe: DatabaseProbe | None = None,
    folder_probe: FolderProbe | None = None,
    telegram_probe_timeout_seconds: float = DEFAULT_TELEGRAM_PROBE_TIMEOUT_SECONDS,
) -> HealthReport:
    """Run the three probes concurrently-safe and bundle their results.

    `status` is always ``ok`` so the service stays reachable during startup
    even when Telegram is unauthorized — individual fields carry the detail.
    """
    db_path = database_path if database_path is not None else default_database_path(config)
    folder_name = config.telegram.default_chat_folder.folder_name
    folder_id = config.telegram.default_chat_folder.folder_id

    database_status = await (
        database_probe() if database_probe is not None else probe_database(db_path)
    )

    # Production path (no injected probes): fetch the session state exactly once
    # and reuse it for both the session and folder checks. This avoids the
    # redundant `state()`/`get_me()` round-trips a `/health` poll otherwise
    # makes every interval.
    if session_probe is None and folder_probe is None:
        try:
            session_status, folder_status = await _probe_session_and_folder(
                session_manager,
                folder_name,
                folder_id=folder_id,
                timeout_seconds=telegram_probe_timeout_seconds,
            )
        except Exception as exc:
            logger.warning(
                "telegram health probe failed error_type=%s error=%s",
                type(exc).__name__,
                exc,
            )
            session_status, folder_status = SESSION_UNAUTHORIZED, FOLDER_MISSING
    else:
        try:
            session_status = await asyncio.wait_for(
                session_probe()
                if session_probe is not None
                else probe_telegram_session(session_manager),
                timeout=telegram_probe_timeout_seconds,
            )
        except Exception:
            session_status = SESSION_UNAUTHORIZED
        try:
            folder_status = await asyncio.wait_for(
                folder_probe()
                if folder_probe is not None
                else probe_default_folder(
                    session_manager, folder_name, folder_id=folder_id
                ),
                timeout=telegram_probe_timeout_seconds,
            )
        except Exception:
            folder_status = FOLDER_MISSING

    return HealthReport(
        status="ok",
        telegram_session=session_status,
        database=database_status,
        default_folder=folder_status,
    )
