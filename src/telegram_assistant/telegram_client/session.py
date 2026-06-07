"""Telethon session wrapper used by HTTP, CLI, and worker surfaces."""

from __future__ import annotations

import asyncio
import getpass
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from telegram_assistant.config.models import AppConfig, TelegramConfig
from telegram_assistant.telegram_client.proxy import parse_proxy_url

logger = logging.getLogger(__name__)


class _TelethonLike(Protocol):
    """Minimal slice of :class:`telethon.TelegramClient` we rely on."""

    async def connect(self) -> Any: ...

    async def disconnect(self) -> Any: ...

    async def is_user_authorized(self) -> bool: ...

    async def get_me(self) -> Any: ...

    async def send_code_request(self, phone: str) -> Any: ...

    async def sign_in(self, *args: Any, **kwargs: Any) -> Any: ...


ClientFactory = Callable[..., _TelethonLike]


@dataclass(frozen=True)
class SessionState:
    """Snapshot of the Telethon session for callers that only need status."""

    authorized: bool
    account_label: str
    session_path: str
    me: Any | None = None


def _default_factory(
    session_path: str,
    api_id: int,
    api_hash: str,
    *,
    proxy: dict | None = None,
) -> _TelethonLike:
    # Imported lazily so tests do not require Telethon to be installed or for
    # the import to succeed before they are able to swap in a mock factory.
    from telethon import TelegramClient

    kwargs: dict = {}
    if proxy is not None:
        try:
            import python_socks  # noqa: F401
        except ImportError as exc:
            raise ImportError(
                "Proxy support requires python-socks[asyncio]. "
                "Install with: pip install 'python-socks[asyncio]'"
            ) from exc
        kwargs["proxy"] = proxy
    return TelegramClient(session_path, api_id, api_hash, **kwargs)


class TelethonSessionManager:
    """Owns one Telethon client across the process lifecycle."""

    def __init__(
        self,
        telegram_config: TelegramConfig,
        *,
        client_factory: ClientFactory | None = None,
    ) -> None:
        self._config = telegram_config
        self._factory: ClientFactory = client_factory or _default_factory
        self._client: _TelethonLike | None = None

    @property
    def config(self) -> TelegramConfig:
        return self._config

    @property
    def session_path(self) -> str:
        return self._config.session_path

    @property
    def account_label(self) -> str:
        return self._config.main_account_label

    async def get_client(self) -> _TelethonLike:
        """Return a connected client, creating one on first use.

        If ``connect()`` raises, the cached client is dropped so a later call
        retries from scratch (and so HTTP backend factories that gate on
        ``_client is None`` correctly observe the disconnected state instead
        of handing out a client that never finished connecting).
        """
        session_path = str(Path(self._config.session_path).expanduser())
        client_created = False
        if self._client is None:
            proxy = parse_proxy_url(self._config.proxy_url)
            factory_kwargs: dict = {"proxy": proxy} if proxy is not None else {}
            try:
                Path(session_path).parent.mkdir(parents=True, exist_ok=True)
            except OSError:
                pass
            self._client = self._factory(
                session_path,
                self._config.api_id,
                self._config.api_hash,
                **factory_kwargs,
            )
            client_created = True
        started = time.perf_counter()
        logger.info(
            "telethon connect start account_label=%s session_path=%s client_created=%s",
            self.account_label,
            session_path,
            client_created,
        )
        try:
            await self._client.connect()
        except asyncio.CancelledError:
            duration_ms = int((time.perf_counter() - started) * 1000)
            logger.warning(
                "telethon connect cancelled account_label=%s session_path=%s duration_ms=%s",
                self.account_label,
                session_path,
                duration_ms,
            )
            self._client = None
            raise
        except Exception as exc:
            duration_ms = int((time.perf_counter() - started) * 1000)
            logger.warning(
                "telethon connect failed account_label=%s session_path=%s duration_ms=%s error_type=%s error=%s",
                self.account_label,
                session_path,
                duration_ms,
                type(exc).__name__,
                exc,
            )
            self._client = None
            raise
        duration_ms = int((time.perf_counter() - started) * 1000)
        logger.info(
            "telethon connect success account_label=%s session_path=%s duration_ms=%s",
            self.account_label,
            session_path,
            duration_ms,
        )
        return self._client

    async def disconnect(self) -> None:
        if self._client is not None:
            await self._client.disconnect()

    async def reset(self) -> None:
        """Drop the in-memory client (next call will reconnect)."""
        await self.disconnect()
        self._client = None

    def session_path_exists(self) -> bool:
        return Path(self._config.session_path).expanduser().exists()

    async def state(self) -> SessionState:
        """Probe the client and return a snapshot of its authorization state."""
        logger.info("telethon state start account_label=%s", self.account_label)
        client = await self.get_client()
        authorized = await client.is_user_authorized()
        logger.info(
            "telethon state authorization account_label=%s authorized=%s",
            self.account_label,
            bool(authorized),
        )
        me = await client.get_me() if authorized else None
        if me is not None:
            logger.info(
                "telethon get_me success account_label=%s user_id=%s username=%s",
                self.account_label,
                getattr(me, "id", None),
                getattr(me, "username", None),
            )
        return SessionState(
            authorized=bool(authorized),
            account_label=self.account_label,
            session_path=self.session_path,
            me=me,
        )

    async def interactive_login(
        self,
        *,
        phone_prompt: Callable[[], str] | None = None,
        code_prompt: Callable[[], str] | None = None,
        password_prompt: Callable[[], str] | None = None,
    ) -> SessionState:
        """Run an interactive Telethon login if the session is not yet authorized.

        Returns the resulting :class:`SessionState`. When the session is already
        authorized the prompts are not invoked and the existing identity is
        returned unchanged.
        """
        phone_prompt = phone_prompt or (lambda: input("Phone (international format, +...): ").strip())
        code_prompt = code_prompt or (lambda: input("Login code: ").strip())
        password_prompt = password_prompt or (lambda: getpass.getpass("2FA password: "))

        client = await self.get_client()
        if await client.is_user_authorized():
            me = await client.get_me()
            return SessionState(
                authorized=True,
                account_label=self.account_label,
                session_path=self.session_path,
                me=me,
            )

        phone = phone_prompt()
        if not phone:
            raise ValueError("Phone number is required to authenticate.")
        await client.send_code_request(phone)
        code = code_prompt()
        if not code:
            raise ValueError("Login code is required to authenticate.")

        # Telethon raises SessionPasswordNeededError when 2FA is enabled. We
        # import it lazily so the module does not require telethon at import
        # time (tests inject a fake client).
        try:
            await client.sign_in(phone=phone, code=code)
        except Exception as exc:  # pragma: no cover - branch covered via test double
            if not _is_password_needed(exc):
                raise
            password = password_prompt()
            if not password:
                raise ValueError("2FA password is required to authenticate.") from exc
            await client.sign_in(password=password)

        me = await client.get_me()
        return SessionState(
            authorized=True,
            account_label=self.account_label,
            session_path=self.session_path,
            me=me,
        )


def _is_password_needed(exc: BaseException) -> bool:
    """Return True if ``exc`` indicates that Telegram 2FA is required."""
    name = type(exc).__name__
    if name == "SessionPasswordNeededError":
        return True
    try:  # pragma: no cover - import guarded
        from telethon.errors import SessionPasswordNeededError

        return isinstance(exc, SessionPasswordNeededError)
    except Exception:  # pragma: no cover
        return False


_SHARED_MANAGER: TelethonSessionManager | None = None


def configure_shared_manager(
    config: AppConfig,
    *,
    client_factory: ClientFactory | None = None,
) -> TelethonSessionManager:
    """Install a process-wide session manager built from ``config``."""
    global _SHARED_MANAGER
    _SHARED_MANAGER = TelethonSessionManager(config.telegram, client_factory=client_factory)
    return _SHARED_MANAGER


def get_shared_manager() -> TelethonSessionManager:
    if _SHARED_MANAGER is None:
        raise RuntimeError(
            "Telethon session manager has not been configured. Call "
            "configure_shared_manager(config) at startup before requesting the client."
        )
    return _SHARED_MANAGER


async def get_client() -> _TelethonLike:
    """Return the shared Telethon client (connecting it if necessary)."""
    return await get_shared_manager().get_client()


async def session_state() -> SessionState:
    """Return the shared session state snapshot."""
    return await get_shared_manager().state()


async def interactive_login(
    *,
    phone_prompt: Callable[[], str] | None = None,
    code_prompt: Callable[[], str] | None = None,
    password_prompt: Callable[[], str] | None = None,
) -> SessionState:
    """Run interactive login against the shared manager."""
    return await get_shared_manager().interactive_login(
        phone_prompt=phone_prompt,
        code_prompt=code_prompt,
        password_prompt=password_prompt,
    )


async def reset_client() -> None:
    """Drop the shared client (next call reconnects)."""
    if _SHARED_MANAGER is not None:
        await _SHARED_MANAGER.reset()


def _reset_shared_for_tests() -> None:
    """Test-only helper to clear the module-level singleton."""
    global _SHARED_MANAGER
    _SHARED_MANAGER = None
