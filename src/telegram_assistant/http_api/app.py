"""FastAPI application factory."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import AsyncIterator, Callable
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path

from fastapi import APIRouter, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from telegram_assistant import __version__
from telegram_assistant.config import (
    AppConfig,
    ConfigWatcher,
    load_config,
    reload_config_into_state,
    resolve_config_path,
)
from telegram_assistant.entities import EntityResolver
from telegram_assistant.folders import FolderBackend
from telegram_assistant.groups import GroupBackend
from telegram_assistant.health import collect_health, default_database_path
from telegram_assistant.http_api.auth import BearerAuth
from telegram_assistant.http_api.folders import build_router as build_folders_router
from telegram_assistant.http_api.groups import build_router as build_groups_router
from telegram_assistant.http_api.mcp import (
    GoogleOidcProvider,
    OAuthAuthorizationServer,
    build_fastmcp_server,
    build_oauth_router,
    configure_mcp_tools,
)
from telegram_assistant.http_api.members import build_router as build_members_router
from telegram_assistant.http_api.messages import build_router as build_messages_router
from telegram_assistant.http_api.notifications import (
    build_router as build_notifications_router,
)
from telegram_assistant.http_api.topics import build_router as build_topics_router
from telegram_assistant.members import (
    MemberAddBackend,
    MemberListBackend,
    MemberRemoveBackend,
)
from telegram_assistant.messages import (
    DeleteBackend,
    EditBackend,
    ForwardBackend,
    MediaDownloadBackend,
    MessageBackend,
    MessageReadBackend,
    PinBackend,
    ReactionBackend,
    SearchBackend,
    SentMessageRegistry,
)
from telegram_assistant.notifications import NotificationBackend
from telegram_assistant.observability.logging import configure_logging, get_logger
from telegram_assistant.persistence.folder_cache import FolderMembershipCache
from telegram_assistant.persistence.rate_gate import RateGateStore
from telegram_assistant.persistence.store import OperationStore
from telegram_assistant.plugins import build_registry
from telegram_assistant.telegram_client.session import (
    TelethonSessionManager,
)
from telegram_assistant.topics import TopicBackend

_log = get_logger(__name__)

FolderBackendFactory = Callable[[Request], FolderBackend | None]
GroupBackendFactory = Callable[[Request], GroupBackend | None]
TopicBackendFactory = Callable[[Request], TopicBackend | None]
MemberBackendFactory = Callable[[Request], MemberAddBackend | None]
MemberRemoveBackendFactory = Callable[[Request], MemberRemoveBackend | None]
MemberListBackendFactory = Callable[[Request], MemberListBackend | None]
MessageBackendFactory = Callable[[Request], MessageBackend | None]
MessageReadBackendFactory = Callable[[Request], MessageReadBackend | None]
SearchBackendFactory = Callable[[Request], SearchBackend | None]
ReactionBackendFactory = Callable[[Request], ReactionBackend | None]
ForwardBackendFactory = Callable[[Request], ForwardBackend | None]
DeleteBackendFactory = Callable[[Request], DeleteBackend | None]
EditBackendFactory = Callable[[Request], EditBackend | None]
PinBackendFactory = Callable[[Request], PinBackend | None]
DownloadBackendFactory = Callable[[Request], MediaDownloadBackend | None]
NotificationBackendFactory = Callable[[Request], NotificationBackend | None]
ResolverFactory = Callable[[Request], EntityResolver | None]


def _build_health_router() -> APIRouter:
    router = APIRouter()

    @router.get("/health")
    async def health(request: Request) -> dict[str, str]:
        report = await collect_health(
            request.app.state.config,
            session_manager=request.app.state.session_manager,
            database_path=request.app.state.database_path,
        )
        payload = report.to_dict()
        payload["version"] = __version__
        return payload

    return router


def _build_protected_router() -> APIRouter:
    """Placeholder router for endpoints requiring bearer-token auth.

    Real routes (groups, topics, members, messages, folders) are added in
    later tasks. The router is mounted here so auth is wired up from day one.
    """
    router = APIRouter(dependencies=[BearerAuth])

    @router.get("/whoami")
    async def whoami() -> dict[str, str]:
        return {"status": "authenticated"}

    return router


def _default_folder_backend_factory(
    session_manager: TelethonSessionManager | None,
) -> FolderBackendFactory:
    """Build a backend factory bound to the configured Telethon session.

    The factory returns ``None`` when no session manager is wired up, which the
    folders router translates into ``503 Service Unavailable``. This keeps the
    HTTP surface honest about not being able to talk to Telegram instead of
    silently 500ing.
    """

    def _factory(_request: Request) -> FolderBackend | None:
        if session_manager is None:
            return None
        client = getattr(session_manager, "_client", None)
        if client is None:
            # The session has not been used yet this process, so we have no
            # connected client to wrap. Returning None makes the folders
            # router emit 503 rather than racing a connect() on the request
            # thread.
            return None
        from telegram_assistant.folders import TelethonFolderBackend

        return TelethonFolderBackend(client)

    return _factory


def _default_topic_backend_factory(
    session_manager: TelethonSessionManager | None,
) -> TopicBackendFactory:
    """Build a Telethon-backed topic backend factory.

    Mirrors :func:`_default_folder_backend_factory`: returns ``None`` until a
    Telethon client is available so the HTTP layer can return 503 rather than
    silently 500.
    """

    def _factory(_request: Request) -> TopicBackend | None:
        if session_manager is None:
            return None
        client = getattr(session_manager, "_client", None)
        if client is None:
            return None
        from telegram_assistant.topics.telethon_backend import (
            TelethonTopicBackend,
        )

        return TelethonTopicBackend(client)

    return _factory


def _default_member_backend_factory(
    session_manager: TelethonSessionManager | None,
) -> MemberBackendFactory:
    """Build a Telethon-backed member backend factory.

    The same Telethon adapter implements both the add and remove protocols,
    so the remove endpoint reuses this factory when no dedicated remove
    factory is supplied.
    """

    def _factory(_request: Request) -> MemberAddBackend | None:
        if session_manager is None:
            return None
        client = getattr(session_manager, "_client", None)
        if client is None:
            return None
        from telegram_assistant.members.telethon_backend import (
            TelethonMemberBackend,
        )

        return TelethonMemberBackend(client)

    return _factory


def _default_member_list_backend_factory(
    session_manager: TelethonSessionManager | None,
) -> MemberListBackendFactory:
    """Build a Telethon-backed participants-list factory for the read op.

    Mirrors :func:`_default_message_read_backend_factory`: returns ``None``
    until a Telethon client is available so the endpoint can return 503.
    """

    def _factory(_request: Request) -> MemberListBackend | None:
        if session_manager is None:
            return None
        client = getattr(session_manager, "_client", None)
        if client is None:
            return None
        from telegram_assistant.members.telethon_backend import (
            TelethonMemberListBackend,
        )

        return TelethonMemberListBackend(client)

    return _factory


def _default_group_backend_factory(
    session_manager: TelethonSessionManager | None,
) -> GroupBackendFactory:
    """Build a Telethon-backed group backend factory.

    Mirrors :func:`_default_folder_backend_factory`: returns ``None`` until a
    Telethon client is available so the HTTP layer can return 503 rather than
    silently 500.
    """

    def _factory(_request: Request) -> GroupBackend | None:
        if session_manager is None:
            return None
        client = getattr(session_manager, "_client", None)
        if client is None:
            return None
        from telegram_assistant.groups.telethon_backend import (
            TelethonGroupBackend,
        )

        return TelethonGroupBackend(client)

    return _factory


def _default_message_backend_factory(
    session_manager: TelethonSessionManager | None,
) -> MessageBackendFactory:
    """Build a Telethon-backed message-send backend factory.

    Mirrors :func:`_default_folder_backend_factory`: returns ``None`` until a
    Telethon client is available so the messages router can return 503. Uses
    the dedicated :class:`TelethonMessageBackend` (text, media, scheduled
    sends) rather than the topic backend's text-only fallback.
    """

    def _factory(_request: Request) -> MessageBackend | None:
        if session_manager is None:
            return None
        client = getattr(session_manager, "_client", None)
        if client is None:
            return None
        from telegram_assistant.messages.telethon_backend import (
            TelethonMessageBackend,
        )

        return TelethonMessageBackend(client)

    return _factory


def _default_message_read_backend_factory(
    session_manager: TelethonSessionManager | None,
) -> MessageReadBackendFactory:
    """Build a Telethon-backed message-read backend factory for the get-recent op.

    Mirrors :func:`_default_folder_backend_factory`: returns ``None`` until a
    Telethon client is available so the read endpoint can return 503.
    """

    def _factory(_request: Request) -> MessageReadBackend | None:
        if session_manager is None:
            return None
        client = getattr(session_manager, "_client", None)
        if client is None:
            return None
        from telegram_assistant.messages.telethon_backend import (
            TelethonMessageReadBackend,
        )

        return TelethonMessageReadBackend(client)

    return _factory


def _default_search_backend_factory(
    session_manager: TelethonSessionManager | None,
) -> SearchBackendFactory:
    """Build a Telethon-backed message-search backend factory.

    Mirrors :func:`_default_message_read_backend_factory`: returns ``None``
    until a Telethon client is available so the search endpoint can return 503.
    """

    def _factory(_request: Request) -> SearchBackend | None:
        if session_manager is None:
            return None
        client = getattr(session_manager, "_client", None)
        if client is None:
            return None
        from telegram_assistant.messages.telethon_backend import (
            TelethonSearchBackend,
        )

        return TelethonSearchBackend(client)

    return _factory


def _default_reaction_backend_factory(
    session_manager: TelethonSessionManager | None,
) -> ReactionBackendFactory:
    """Build a Telethon-backed message-reaction backend factory.

    Mirrors :func:`_default_folder_backend_factory`: returns ``None`` until a
    Telethon client is available so the reactions endpoint can return 503.
    """

    def _factory(_request: Request) -> ReactionBackend | None:
        if session_manager is None:
            return None
        client = getattr(session_manager, "_client", None)
        if client is None:
            return None
        from telegram_assistant.messages.telethon_backend import (
            TelethonReactionBackend,
        )

        return TelethonReactionBackend(client)

    return _factory


def _default_forward_backend_factory(
    session_manager: TelethonSessionManager | None,
) -> ForwardBackendFactory:
    """Build a Telethon-backed message-forward backend factory.

    Mirrors :func:`_default_folder_backend_factory`: returns ``None`` until a
    Telethon client is available so the forward endpoint can return 503.
    """

    def _factory(_request: Request) -> ForwardBackend | None:
        if session_manager is None:
            return None
        client = getattr(session_manager, "_client", None)
        if client is None:
            return None
        from telegram_assistant.messages.telethon_backend import (
            TelethonForwardBackend,
        )

        return TelethonForwardBackend(client)

    return _factory


def _default_delete_backend_factory(
    session_manager: TelethonSessionManager | None,
) -> DeleteBackendFactory:
    """Build a Telethon-backed message-delete backend factory.

    Mirrors :func:`_default_forward_backend_factory`: returns ``None`` until a
    Telethon client is available so the delete endpoint can return 503.
    """

    def _factory(_request: Request) -> DeleteBackend | None:
        if session_manager is None:
            return None
        client = getattr(session_manager, "_client", None)
        if client is None:
            return None
        from telegram_assistant.messages.telethon_backend import (
            TelethonDeleteBackend,
        )

        return TelethonDeleteBackend(client)

    return _factory


def _default_edit_backend_factory(
    session_manager: TelethonSessionManager | None,
) -> EditBackendFactory:
    """Build a Telethon-backed message-edit backend factory.

    Mirrors :func:`_default_delete_backend_factory`: returns ``None`` until a
    Telethon client is available so the edit endpoint can return 503.
    """

    def _factory(_request: Request) -> EditBackend | None:
        if session_manager is None:
            return None
        client = getattr(session_manager, "_client", None)
        if client is None:
            return None
        from telegram_assistant.messages.telethon_backend import (
            TelethonEditBackend,
        )

        return TelethonEditBackend(client)

    return _factory


def _default_pin_backend_factory(
    session_manager: TelethonSessionManager | None,
) -> PinBackendFactory:
    """Build a Telethon-backed message pin/unpin backend factory.

    Mirrors :func:`_default_edit_backend_factory`: returns ``None`` until a
    Telethon client is available so the pin/unpin endpoints can return 503.
    """

    def _factory(_request: Request) -> PinBackend | None:
        if session_manager is None:
            return None
        client = getattr(session_manager, "_client", None)
        if client is None:
            return None
        from telegram_assistant.messages.telethon_backend import (
            TelethonPinBackend,
        )

        return TelethonPinBackend(client)

    return _factory


def _default_download_backend_factory(
    session_manager: TelethonSessionManager | None,
) -> DownloadBackendFactory:
    """Build a Telethon-backed message media-download backend factory.

    Mirrors :func:`_default_message_read_backend_factory`: returns ``None``
    until a Telethon client is available so the download endpoint can return
    503.
    """

    def _factory(_request: Request) -> MediaDownloadBackend | None:
        if session_manager is None:
            return None
        client = getattr(session_manager, "_client", None)
        if client is None:
            return None
        from telegram_assistant.messages.telethon_backend import (
            TelethonMediaDownloadBackend,
        )

        return TelethonMediaDownloadBackend(client)

    return _factory


def _default_notification_backend_factory(
    session_manager: TelethonSessionManager | None,
) -> NotificationBackendFactory:
    """Build a Telethon-backed notification (mute/unmute) backend factory.

    Mirrors :func:`_default_folder_backend_factory`: returns ``None`` until a
    Telethon client is available so the notifications router can return 503.
    """

    def _factory(_request: Request) -> NotificationBackend | None:
        if session_manager is None:
            return None
        client = getattr(session_manager, "_client", None)
        if client is None:
            return None
        from telegram_assistant.notifications import (
            TelethonNotificationBackend,
        )

        return TelethonNotificationBackend(client)

    return _factory


def _default_resolver_factory(
    session_manager: TelethonSessionManager | None,
) -> ResolverFactory:
    """Build a Telethon-backed entity-resolver factory.

    Mirrors :func:`_default_folder_backend_factory`: returns ``None`` until a
    Telethon client is available so callers needing entity resolution can
    return 503 rather than silently 500.
    """

    def _factory(_request: Request) -> EntityResolver | None:
        if session_manager is None:
            return None
        client = getattr(session_manager, "_client", None)
        if client is None:
            return None
        from telegram_assistant.entities import TelethonEntityResolver

        return TelethonEntityResolver(client)

    return _factory


def _ensure_state_stores(
    app: FastAPI, config: AppConfig, database_path: Path | None
) -> None:
    """(Re)build the config-derived SQLite stores on ``app.state``.

    Two slots, both built once rather than per request and both conditional on
    config:

    * ``folder_membership_cache`` — shared by every per-request ``Authorizer``
      (read-through with TTL + stale fallback). Only built when an access policy
      is active: with allow-all there are no folder rules, so the cache is never
      consulted and building the DB would only create stray files.
    * ``rate_gate_store`` — the cross-process pin/unpin pacing gate (a CLI
      one-shot and this server drive the same account, hence SQLite rather than
      in-memory state). Only built when ``pin_min_interval_seconds > 0``.

    Called at startup *and* from the hot-reload swap, because an edit can switch
    on an access policy or pacing that was off when the process started; a slot
    already filled is left alone. Both are best-effort — an unopenable DB (a
    read-only mount, no write permission) leaves the slot empty so folder rules
    fall back to live fetches and pin falls back to unpaced calls with
    FLOOD_WAIT retries — but the failure is logged: otherwise the only symptom
    is "folder rules are slow" or "pin isn't paced" with nothing to explain it.
    """
    db_path = (
        database_path if database_path is not None else default_database_path(config)
    )

    if (
        getattr(app.state, "folder_membership_cache", None) is None
        and getattr(config.telegram, "access", None) is not None
    ):
        try:
            app.state.folder_membership_cache = FolderMembershipCache(db_path)
        except Exception as exc:
            _log.warning(
                "folder_membership_cache_unavailable",
                error=str(exc),
                database_path=str(db_path),
            )
            app.state.folder_membership_cache = None

    if (
        getattr(app.state, "rate_gate_store", None) is None
        and float(getattr(config.telegram, "pin_min_interval_seconds", 0.0)) > 0
    ):
        try:
            app.state.rate_gate_store = RateGateStore(db_path)
        except Exception as exc:
            _log.warning(
                "rate_gate_store_unavailable",
                error=str(exc),
                database_path=str(db_path),
            )
            app.state.rate_gate_store = None


def create_app(
    config: AppConfig | None = None,
    *,
    session_manager: TelethonSessionManager | None = None,
    database_path: Path | None = None,
    folder_backend_factory: FolderBackendFactory | None = None,
    group_backend_factory: GroupBackendFactory | None = None,
    topic_backend_factory: TopicBackendFactory | None = None,
    member_backend_factory: MemberBackendFactory | None = None,
    member_remove_backend_factory: MemberRemoveBackendFactory | None = None,
    member_list_backend_factory: MemberListBackendFactory | None = None,
    message_backend_factory: MessageBackendFactory | None = None,
    message_read_backend_factory: MessageReadBackendFactory | None = None,
    search_backend_factory: SearchBackendFactory | None = None,
    reaction_backend_factory: ReactionBackendFactory | None = None,
    forward_backend_factory: ForwardBackendFactory | None = None,
    delete_backend_factory: DeleteBackendFactory | None = None,
    edit_backend_factory: EditBackendFactory | None = None,
    pin_backend_factory: PinBackendFactory | None = None,
    download_backend_factory: DownloadBackendFactory | None = None,
    notification_backend_factory: NotificationBackendFactory | None = None,
    resolver_factory: ResolverFactory | None = None,
    operation_store: OperationStore | None = None,
    mcp_google_provider: GoogleOidcProvider | None = None,
) -> FastAPI:
    """Build a FastAPI instance.

    Loads config from `data/config.yml` when none is supplied, so this factory
    is usable both from production (`uvicorn ... --factory`) and from tests
    that inject an `AppConfig` directly. ``session_manager`` and
    ``database_path`` are optional so the service can come up — and respond
    to ``GET /health`` — before the Telethon session has been authorized.
    ``folder_backend_factory`` / ``group_backend_factory`` let tests inject
    fakes without spinning up Telethon. ``operation_store`` lets tests share a
    store between requests; in production we open one rooted at
    :func:`default_database_path` so HTTP requests can replay completed
    operations from the same SQLite file the worker writes to.
    """
    # Only production (`uvicorn ... --factory`) leaves config unset; that is the
    # path where live config hot-reload makes sense. Tests inject an AppConfig
    # directly and should not have a background observer swapping it out.
    config_was_loaded_from_disk = config is None
    if config is None:
        config = load_config()

    # Honor the operator-configured log level for both stdlib and structlog
    # output. Without this the first `get_logger()` call would auto-configure
    # at INFO regardless of what `data/config.yml` requested.
    configure_logging(
        level=config.logging.level,
        telethon_level=config.logging.telethon_level,
        force=True,
    )

    # Auto-construct a Telethon session manager from config when the caller did
    # not supply one. This is the production path: `uvicorn ... --factory`
    # invokes `create_app()` with no arguments, and without this the service
    # would come up permanently unauthorized. Tests inject their own manager
    # (or explicitly pass None to opt out).
    auto_constructed_manager = False
    if session_manager is None:
        try:
            session_manager = TelethonSessionManager(config.telegram)
            auto_constructed_manager = True
        except Exception:
            session_manager = None

    mcp_app_ref: dict[str, FastAPI] = {}

    def _mcp_app_state() -> object:
        app_ref = mcp_app_ref.get("app")
        if app_ref is None:
            raise RuntimeError("FastAPI app state is not available yet")
        return app_ref.state

    mcp_oauth_server: OAuthAuthorizationServer | None = None
    mcp_fastmcp_server = None
    mcp_asgi_app = None
    if config.mcp is not None and config.mcp.enabled:
        mcp_oauth_server = OAuthAuthorizationServer(
            config.mcp,
            google_provider=mcp_google_provider,
        )
        mcp_fastmcp_server = build_fastmcp_server(
            config,
            oauth_server=mcp_oauth_server,
            app_state_provider=_mcp_app_state,
        )
        mcp_asgi_app = mcp_fastmcp_server.streamable_http_app()

    lifespan: Callable[[FastAPI], AsyncIterator[None]] | None = None
    if auto_constructed_manager and session_manager is not None:
        manager_ref = session_manager

        async def _retry_connect_until_ready() -> None:
            # Exponential backoff so a Telegram outage at startup self-heals
            # without operator intervention. Without this, the cached client
            # stays None and the Telegram routes return 503 until the process
            # is restarted.
            backoff = 1.0
            while True:
                try:
                    await manager_ref.get_client()
                    return
                except asyncio.CancelledError:
                    raise
                except Exception:
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 60.0)

        @asynccontextmanager
        async def _lifespan(_app: FastAPI) -> AsyncIterator[None]:
            # Service startup must not wait on Telegram connectivity. The
            # background task connects immediately when possible and retries
            # with backoff when Telegram or the container network is down.
            retry_task: asyncio.Task[None] | None = asyncio.create_task(
                _retry_connect_until_ready()
            )
            await asyncio.sleep(0)
            try:
                yield
            finally:
                if retry_task is not None and not retry_task.done():
                    retry_task.cancel()
                    try:
                        await retry_task
                    except (asyncio.CancelledError, Exception):
                        pass
                try:
                    await manager_ref.disconnect()
                except Exception:
                    pass

        lifespan = _lifespan

    if mcp_fastmcp_server is not None:
        telegram_lifespan = lifespan

        @asynccontextmanager
        async def _combined_lifespan(app: FastAPI) -> AsyncIterator[None]:
            async with AsyncExitStack() as stack:
                if telegram_lifespan is not None:
                    await stack.enter_async_context(telegram_lifespan(app))
                await stack.enter_async_context(
                    mcp_fastmcp_server.session_manager.run()
                )
                yield

        lifespan = _combined_lifespan

    if config_was_loaded_from_disk:
        inner_lifespan = lifespan

        @asynccontextmanager
        async def _with_config_watcher(app: FastAPI) -> AsyncIterator[None]:
            # Live config hot-reload: watch the resolved config file and swap
            # `app.state.config` (plus config-derived state) on a debounced,
            # validation-gated reload. A bad edit keeps the last-good config.
            config_path = resolve_config_path()
            watcher: ConfigWatcher | None = None
            if config_path is not None:

                def _on_swap(new_config: AppConfig) -> None:
                    # Rebuild config-derived state that is constructed once
                    # rather than per-request. The Authorizer reads
                    # `app.state.config` lazily, so it picks up the swap
                    # automatically; the MCP tool surface is registered once, so
                    # re-apply `mcp.disabled_tools` explicitly here.
                    app.state.plugin_registry = build_registry(new_config)
                    # Access rules (including folder targets) may have changed;
                    # drop the persistent membership cache so the next gated op
                    # rebuilds it under the new policy instead of serving a map
                    # keyed to the old one.
                    fmc = getattr(app.state, "folder_membership_cache", None)
                    if fmc is not None:
                        try:
                            fmc.clear()
                        except Exception as exc:
                            _log.warning(
                                "folder_membership_cache_clear_failed", error=str(exc)
                            )
                    # An access policy — or pin pacing — may have been switched
                    # on by this very edit; the stores are only built at startup
                    # when enabled, so build them now rather than leaving the
                    # feature off for the rest of the process lifetime.
                    _ensure_state_stores(app, new_config, database_path)
                    if mcp_fastmcp_server is not None and new_config.mcp is not None:
                        configure_mcp_tools(
                            mcp_fastmcp_server,
                            _mcp_app_state,
                            new_config.mcp.disabled_tools,
                        )

                def _on_reload() -> None:
                    reload_config_into_state(
                        app.state,
                        config_path,
                        lock=app.state.config_lock,
                        on_swap=_on_swap,
                    )

                watcher = ConfigWatcher(config_path, _on_reload)
                watcher.start()
                app.state.config_watcher = watcher
            try:
                async with AsyncExitStack() as stack:
                    if inner_lifespan is not None:
                        await stack.enter_async_context(inner_lifespan(app))
                    yield
            finally:
                if watcher is not None:
                    watcher.stop()
                    app.state.config_watcher = None

        lifespan = _with_config_watcher

    app = FastAPI(
        title="telegram-assistant",
        version=__version__,
        lifespan=lifespan,
    )
    if mcp_oauth_server is not None:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_methods=["GET", "POST", "OPTIONS"],
            allow_headers=["*"],
            expose_headers=["WWW-Authenticate", "Mcp-Session-Id"],
        )
    mcp_app_ref["app"] = app
    app.state.config = config
    # Guards atomic swaps of `app.state.config` performed by the hot-reload
    # watcher (Task 1). Readers that snapshot config under this lock see a
    # consistent object; routers reading `app.state.config` directly get either
    # the old or the new config, never a partially-applied one.
    app.state.config_lock = threading.Lock()
    app.state.config_watcher = None
    app.state.plugin_registry = build_registry(config)
    # One registry per server process, reachable from HTTP/MCP via app.state.
    # Tracks the ids this process has sent so the session-limited delete op
    # (Tasks 6/7) can recognise them; cleared on restart.
    app.state.sent_message_registry = SentMessageRegistry()
    app.state.session_manager = session_manager
    app.state.database_path = database_path
    app.state.folder_backend_factory = (
        folder_backend_factory
        if folder_backend_factory is not None
        else _default_folder_backend_factory(session_manager)
    )
    app.state.group_backend_factory = (
        group_backend_factory
        if group_backend_factory is not None
        else _default_group_backend_factory(session_manager)
    )
    app.state.topic_backend_factory = (
        topic_backend_factory
        if topic_backend_factory is not None
        else _default_topic_backend_factory(session_manager)
    )
    app.state.member_backend_factory = (
        member_backend_factory
        if member_backend_factory is not None
        else _default_member_backend_factory(session_manager)
    )
    app.state.member_remove_backend_factory = member_remove_backend_factory
    app.state.member_list_backend_factory = (
        member_list_backend_factory
        if member_list_backend_factory is not None
        else _default_member_list_backend_factory(session_manager)
    )
    app.state.resolver_factory = (
        resolver_factory
        if resolver_factory is not None
        else _default_resolver_factory(session_manager)
    )
    app.state.message_backend_factory = (
        message_backend_factory
        if message_backend_factory is not None
        else _default_message_backend_factory(session_manager)
    )
    app.state.message_read_backend_factory = (
        message_read_backend_factory
        if message_read_backend_factory is not None
        else _default_message_read_backend_factory(session_manager)
    )
    app.state.search_backend_factory = (
        search_backend_factory
        if search_backend_factory is not None
        else _default_search_backend_factory(session_manager)
    )
    app.state.reaction_backend_factory = (
        reaction_backend_factory
        if reaction_backend_factory is not None
        else _default_reaction_backend_factory(session_manager)
    )
    app.state.forward_backend_factory = (
        forward_backend_factory
        if forward_backend_factory is not None
        else _default_forward_backend_factory(session_manager)
    )
    app.state.delete_backend_factory = (
        delete_backend_factory
        if delete_backend_factory is not None
        else _default_delete_backend_factory(session_manager)
    )
    app.state.edit_backend_factory = (
        edit_backend_factory
        if edit_backend_factory is not None
        else _default_edit_backend_factory(session_manager)
    )
    app.state.pin_backend_factory = (
        pin_backend_factory
        if pin_backend_factory is not None
        else _default_pin_backend_factory(session_manager)
    )
    app.state.download_backend_factory = (
        download_backend_factory
        if download_backend_factory is not None
        else _default_download_backend_factory(session_manager)
    )
    app.state.notification_backend_factory = (
        notification_backend_factory
        if notification_backend_factory is not None
        else _default_notification_backend_factory(session_manager)
    )
    if operation_store is not None:
        app.state.operation_store = operation_store
    else:
        db_path = (
            database_path
            if database_path is not None
            else default_database_path(config)
        )
        try:
            app.state.operation_store = OperationStore(db_path)
        except Exception:
            # If the store can't be opened (e.g. read-only mount during a
            # smoke test) leave the slot empty — the groups router will return
            # 503 on demand rather than failing at app startup.
            app.state.operation_store = None

    app.state.folder_membership_cache = None
    app.state.rate_gate_store = None
    _ensure_state_stores(app, config, database_path)

    app.state.mcp_oauth_server = mcp_oauth_server
    app.state.mcp_fastmcp_server = mcp_fastmcp_server
    app.state.mcp_asgi_app = mcp_asgi_app
    if mcp_oauth_server is not None:
        app.include_router(build_oauth_router(mcp_oauth_server))

    app.include_router(_build_health_router())
    app.include_router(_build_protected_router(), prefix="/telegram")
    app.include_router(build_folders_router(), prefix="/telegram")
    app.include_router(build_groups_router(), prefix="/telegram")
    app.include_router(build_topics_router(), prefix="/telegram")
    app.include_router(build_members_router(), prefix="/telegram")
    app.include_router(build_messages_router(), prefix="/telegram")
    app.include_router(build_notifications_router(), prefix="/telegram")
    if mcp_asgi_app is not None:
        app.mount("/", mcp_asgi_app)

    return app
