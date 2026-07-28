"""Top-level Typer application wiring all subcommand groups."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

import typer

from telegram_assistant import __version__
from telegram_assistant.config import ConfigError, load_config
from telegram_assistant.health import collect_health, default_database_path
from telegram_assistant.members import DEFAULT_MEMBER_LIST_LIMIT
from telegram_assistant.observability.logging import configure_logging
from telegram_assistant.persistence import (
    OperationNotFoundError,
    OperationStatus,
    OperationStore,
)
from telegram_assistant.telegram_client.session import (
    TelethonSessionManager,
)


def _apply_logging_from_config(config_path: Path | None) -> None:
    """Honor `logging.level` from config when present; ignore failures."""
    try:
        config = load_config(config_path)
    except Exception:
        return
    try:
        configure_logging(
            level=config.logging.level,
            telethon_level=config.logging.telethon_level,
            force=True,
        )
    except Exception:
        return

app = typer.Typer(
    name="telegram-assistant",
    help="Telegram automation service (MTProto/Telethon).",
    no_args_is_help=True,
    add_completion=False,
)


# --- auth -------------------------------------------------------------------


def _format_me(me: object) -> str:
    parts: list[str] = []
    for attr in ("id", "username", "phone", "first_name", "last_name"):
        value = getattr(me, attr, None)
        if value:
            parts.append(f"{attr}={value}")
    return ", ".join(parts) if parts else repr(me)


def _build_session_manager(config_path: Path | None) -> TelethonSessionManager:
    try:
        config = load_config(config_path)
    except ConfigError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc
    try:
        configure_logging(
            level=config.logging.level,
            telethon_level=config.logging.telethon_level,
            force=True,
        )
    except Exception:
        pass
    return TelethonSessionManager(config.telegram)


@app.command("auth")
def auth_cmd(
    config_path: Path | None = typer.Option(  # noqa: B008
        None,
        "--config",
        "-c",
        help="Path to config.yml (defaults: ./data/config.yml, then ~/.config/telegram-assistant/config.yml).",
        exists=False,
    ),
) -> None:
    """Interactive Telethon login for the technical account."""
    manager = _build_session_manager(config_path)

    async def _run() -> None:
        state = await manager.state()
        if state.authorized:
            typer.echo(
                "Telethon session already authorized: "
                f"{_format_me(state.me)} "
                f"(label={state.account_label}, session={state.session_path})"
            )
            await manager.disconnect()
            return

        typer.echo(
            f"Starting interactive login for label '{state.account_label}'. "
            f"Session will be stored at: {state.session_path}"
        )
        new_state = await manager.interactive_login()
        typer.echo(
            "Login successful: "
            f"{_format_me(new_state.me)} (session={new_state.session_path})"
        )
        await manager.disconnect()

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:  # pragma: no cover - interactive only
        typer.echo("Aborted.", err=True)
        raise typer.Exit(code=130) from None
    except Exception as exc:  # pragma: no cover - depends on Telegram
        typer.echo(f"Auth failed: {exc}", err=True)
        raise typer.Exit(code=1) from exc


# --- health -----------------------------------------------------------------


def _load_config_or_exit(config_path: Path | None):
    try:
        return load_config(config_path)
    except ConfigError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc


# Exit code reserved for a loud access-control denial, kept distinct from the
# generic validation (2) and unexpected-failure (1) codes so callers can tell
# "the policy forbids this" apart from "the request was malformed".
ACCESS_DENIED_EXIT_CODE = 3


def _raise_for_access_or_entity_error(exc: BaseException) -> None:
    """Translate access/entity-resolution errors into clear CLI exits.

    * :class:`AccessDenied` → a loud non-zero exit (``ACCESS_DENIED_EXIT_CODE``)
    * entity not-found / ambiguous → exit code 2 with the resolver's message

    Anything else is left for the caller's generic handler.
    """
    from telegram_assistant.access import AccessDenied
    from telegram_assistant.entities import (
        AmbiguousEntityError,
        EntityNotFoundError,
    )

    if isinstance(exc, AccessDenied):
        typer.echo(f"access denied: {exc}", err=True)
        raise typer.Exit(code=ACCESS_DENIED_EXIT_CODE) from exc
    if isinstance(exc, (AmbiguousEntityError, EntityNotFoundError)):
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc


def _cli_folder_membership_cache(config):
    """Build the persistent folder-membership cache for a CLI invocation.

    The CLI is one process per call, so without persistence every folder-gated
    command pays for the ``GetDialogFiltersRequest`` fetch again. Returns a
    :class:`FolderMembershipCache` on the config DB path so a fresh process
    reuses a still-fresh map. Returns ``None`` when there is no access policy
    (folder rules can't exist, so the cache is never consulted) or when the DB
    can't be opened (best-effort: fall back to live fetches).
    """
    access = getattr(config.telegram, "access", None) if config is not None else None
    if access is None:
        return None
    from telegram_assistant.persistence.folder_cache import FolderMembershipCache

    try:
        return FolderMembershipCache(default_database_path(config))
    except Exception:
        return None


def _cli_pin_pacer(config):
    """Build the pin/unpin pacer for a CLI invocation.

    The gate lives in SQLite so this one-shot process paces against the running
    server (and against the previous CLI call) — the burst Telegram punishes is
    per account, not per process. Best-effort: when the DB can't be opened the
    pacer still retries FLOOD_WAIT, just without cross-process spacing.
    """
    from telegram_assistant.messages import Pacer

    interval = float(getattr(config.telegram, "pin_min_interval_seconds", 0.0))
    gate = None
    if interval > 0:
        from telegram_assistant.persistence.rate_gate import RateGateStore

        try:
            gate = RateGateStore(default_database_path(config))
        except Exception:
            gate = None
    return Pacer(gate, min_interval_seconds=interval)


def _raise_for_flood_wait(exc: BaseException, action: str) -> None:
    """Report a paced-out FLOOD_WAIT with the time of the next allowed attempt.

    Only fires for errors carrying retry-after information (pacing gave up);
    plain flood-wait errors fall through to the caller's generic handler.
    """
    from telegram_assistant.messages import retry_after_details

    details = retry_after_details(exc)
    if details is None:
        return
    message = (
        f"{action} rate-limited by Telegram: {exc}. "
        f"Retry after {details['retry_after_seconds']:.0f}s"
    )
    retry_at = details.get("retry_at")
    if retry_at is not None:
        when = datetime.fromtimestamp(retry_at, tz=UTC).isoformat()
        message += f" (next attempt at {when})"
    typer.echo(message, err=True)
    raise typer.Exit(code=1)


def _cli_authorizer(config, *, resolver=None, folder_backend=None):
    """Build an :class:`Authorizer` from loaded config for a CLI invocation.

    No-op sentinel when ``telegram.access`` is unset (allow-all). When a policy
    is present the caller must supply a resolver (for ``chat`` rules) and/or a
    folder backend (for ``folder`` rules) as needed. A persistent
    :class:`FolderMembershipCache` (on the config DB path) is wired in so folder
    membership survives across CLI processes.
    """
    from telegram_assistant.access import Authorizer

    return Authorizer(
        config.telegram.access,
        resolver=resolver,
        folder_backend=folder_backend,
        cache=_cli_folder_membership_cache(config),
    )


async def _cli_resolve_chat_and_authorizer(
    *,
    manager,
    config,
    folder_backend,
    chat_id=None,
    chat_name=None,
    entity=None,
    folder_name=None,
    folder_id=None,
):
    """Resolve a chat reference (id / name / entity) and build the authorizer.

    A Telethon-backed resolver is only constructed when ``--entity`` is used or
    a policy with ``chat`` rules is configured, so the common allow-all path
    never needs the live client. Returns ``(resolved_chat_id, authorizer)``.
    """
    from telegram_assistant.folders import resolve_chat_in_folder

    resolver = None
    if config.telegram.access is not None or entity is not None:
        from telegram_assistant.entities import TelethonEntityResolver

        resolver = TelethonEntityResolver(await manager.get_client())

    if entity is not None:
        resolved_chat_id = (await resolver.resolve(entity)).chat_id
    elif chat_id is not None:
        resolved_chat_id = chat_id
    else:
        resolved = await resolve_chat_in_folder(
            folder_backend,
            folder_name=folder_name or "",
            chat_name=chat_name or "",
            folder_id=folder_id,
        )
        resolved_chat_id = resolved.chat_id

    authorizer = _cli_authorizer(
        config, resolver=resolver, folder_backend=folder_backend
    )
    return resolved_chat_id, authorizer


@app.command("health")
def health_cmd(
    config_path: Path | None = typer.Option(  # noqa: B008
        None,
        "--config",
        "-c",
        help="Path to config.yml (defaults: ./data/config.yml, then ~/.config/telegram-assistant/config.yml).",
        exists=False,
    ),
) -> None:
    """Report service health (telegram session, database, default folder)."""
    config = _load_config_or_exit(config_path)
    manager = TelethonSessionManager(config.telegram)

    async def _run() -> dict[str, str]:
        try:
            report = await collect_health(config, session_manager=manager)
        finally:
            try:
                await manager.disconnect()
            except Exception:
                pass
        return report.to_dict()

    try:
        payload = asyncio.run(_run())
    except Exception as exc:  # pragma: no cover - defensive guard
        typer.echo(f"Health check failed: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    typer.echo(json.dumps(payload, sort_keys=True))


# --- version ----------------------------------------------------------------


@app.command("version")
def version_cmd() -> None:
    """Print the installed version."""
    typer.echo(__version__)


# --- groups -----------------------------------------------------------------

groups_app = typer.Typer(help="Manage Telegram supergroups.", no_args_is_help=True)
app.add_typer(groups_app, name="groups")


def _build_group_backends(config_path: Path | None):
    """Open the Telethon-backed group + folder backends + store for the CLI.

    Mirrors :func:`_build_folder_backend` but for group creation: we lazily
    import the Telethon adapter so `groups create --help` works in
    environments where Telethon is partially installed, and so the placeholder
    commands stay cheap.
    """
    config = _load_config_or_exit(config_path)
    manager = TelethonSessionManager(config.telegram)
    store = OperationStore(default_database_path(config))

    async def _open():
        from telegram_assistant.folders import TelethonFolderBackend
        from telegram_assistant.groups.telethon_backend import (
            TelethonGroupBackend,
        )

        client = await manager.get_client()
        if not await client.is_user_authorized():
            raise RuntimeError(
                "Telethon session is not authorized; run "
                "`telegram-assistant auth` first."
            )
        return TelethonGroupBackend(client), TelethonFolderBackend(client)

    return config, manager, store, _open


@groups_app.command("create")
def groups_create(
    title: str = typer.Option(..., "--title", help="Group title."),
    external_ref: str | None = typer.Option(
        None,
        "--external-ref",
        help="External reference; used as the primary idempotency key when set.",
    ),
    planfix_task_id: str | None = typer.Option(
        None,
        "--planfix-task-id",
        help="Backward-compat alias for --external-ref (Planfix task id).",
    ),
    about: str | None = typer.Option(
        None,
        "--about",
        help="Optional 'about' text for the supergroup.",
    ),
    admin: list[str] | None = typer.Option(  # noqa: B008
        None,
        "--admin",
        help="User to add as admin (repeat for multiple).",
    ),
    member: list[str] | None = typer.Option(  # noqa: B008
        None,
        "--member",
        help="User to add as a regular member (repeat for multiple).",
    ),
    manager_refs: list[str] | None = typer.Option(  # noqa: B008
        None,
        "--manager",
        help="User to add as a regular member (alias of --member; repeat for multiple).",
    ),
    contact: list[str] | None = typer.Option(  # noqa: B008
        None,
        "--contact",
        help=(
            'Member given by "<phone>|<name>" (repeat for multiple). The phone '
            "is normalised (dirty formats and t.me links accepted) and the user "
            "is imported to contacts before being added as a regular member."
        ),
    ),
    reserve_admin: list[str] | None = typer.Option(  # noqa: B008
        None,
        "--reserve-admin",
        help="Extra reserve-admin to add on top of the configured defaults.",
    ),
    reserve_member: list[str] | None = typer.Option(  # noqa: B008
        None,
        "--reserve-member",
        help="Extra reserve-member to add on top of the configured defaults.",
    ),
    no_reserve: bool = typer.Option(
        False,
        "--no-reserve",
        help="Skip the configured reserve_admins / reserve_members.",
    ),
    no_invite_link: bool = typer.Option(
        False,
        "--no-invite-link",
        help="Do not create an invite link even if defaults allow it.",
    ),
    no_topics: bool = typer.Option(
        False,
        "--no-topics",
        help="Do not enable topics even if defaults allow it.",
    ),
    topics_layout: str | None = typer.Option(
        None,
        "--topics-layout",
        help="Topics layout for the new group: 'list' or 'tabs' "
        "(defaults to telegram.defaults.topics_layout).",
    ),
    folder_name: str | None = typer.Option(
        None,
        "--folder-name",
        help="Target folder (defaults to telegram.default_chat_folder.folder_name).",
    ),
    folder_id: int | None = typer.Option(
        None,
        "--folder-id",
        help="Optional folder id cross-check.",
    ),
    skip_folder: bool = typer.Option(
        False,
        "--skip-folder",
        help="Do not place the new group into any folder.",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Validate the request and report the plan without creating the group.",
    ),
    config_path: Path | None = typer.Option(  # noqa: B008
        None,
        "--config",
        "-c",
        help="Path to config.yml (defaults: ./data/config.yml, then ~/.config/telegram-assistant/config.yml).",
        exists=False,
    ),
) -> None:
    """Create a Telegram supergroup."""
    from telegram_assistant.folders import (
        FolderError,
        resolve_folder,
    )
    from telegram_assistant.groups import (
        GroupCreateFailed,
        GroupCreateNeedsReview,
        GroupCreatePending,
        GroupCreateRequest,
        create_group,
        destination_folder,
    )
    from telegram_assistant.groups.service import (
        ContactSpec,
        _dedupe,
        _resolved_reserves,
    )
    from telegram_assistant.plugins import build_registry

    config, manager, store, open_backends = _build_group_backends(config_path)
    plugins = build_registry(config)
    # Generic external_ref wins; --planfix-task-id remains a backward-compat alias.
    effective_ref = external_ref if external_ref is not None else planfix_task_id

    if topics_layout is not None and topics_layout not in ("list", "tabs"):
        typer.echo(
            f"invalid --topics-layout {topics_layout!r}: expected 'list' or 'tabs'",
            err=True,
        )
        raise typer.Exit(code=2)

    # Parse `--contact "<phone>|<name>"` pairs. Both sides are required; the
    # phone is normalised later in the domain layer.
    contacts_arg: list[ContactSpec] = []
    for raw in contact or []:
        phone, sep, name = raw.partition("|")
        if not sep or not phone.strip() or not name.strip():
            typer.echo(
                f'invalid --contact {raw!r}: expected "<phone>|<name>"',
                err=True,
            )
            raise typer.Exit(code=2)
        contacts_arg.append(ContactSpec(phone=phone.strip(), name=name.strip()))

    # Merge configured reserves with extra ones from CLI: extras add on top.
    extra_admins = list(reserve_admin or [])
    extra_members = list(reserve_member or [])
    if no_reserve:
        reserve_admins_arg: list[str] | None = list(extra_admins)
        reserve_members_arg: list[str] | None = list(extra_members)
    elif extra_admins or extra_members:
        reserve_admins_arg = list(config.telegram.reserve_admins) + extra_admins
        reserve_members_arg = list(config.telegram.reserve_members) + extra_members
    else:
        reserve_admins_arg = None
        reserve_members_arg = None

    request = GroupCreateRequest(
        title=title,
        external_ref=effective_ref,
        about=about,
        admins=list(admin or []),
        members=list(member or []),
        managers=list(manager_refs or []),
        contacts=contacts_arg,
        reserve_admins=reserve_admins_arg,
        reserve_members=reserve_members_arg,
        skip_reserve=no_reserve and not (extra_admins or extra_members),
        enable_topics=False if no_topics else None,
        topics_layout=topics_layout,
        create_invite_link=False if no_invite_link else None,
        folder_name=folder_name,
        folder_id=folder_id,
        skip_folder=skip_folder,
    )

    if dry_run:
        if not request.title.strip() and request.external_ref is None:
            typer.echo(
                "group create requires external_ref or non-empty title",
                err=True,
            )
            raise typer.Exit(code=2)

        effective_title = f"{request.title}{plugins.title_postfix()}"
        enable_topics_eff = (
            request.enable_topics
            if request.enable_topics is not None
            else config.telegram.defaults.enable_topics
        )
        topics_layout_eff = (
            request.topics_layout or config.telegram.defaults.topics_layout
        )
        create_link_eff = (
            request.create_invite_link
            if request.create_invite_link is not None
            else config.telegram.defaults.create_invite_link
        )
        reserve_admins_eff = _resolved_reserves(
            request.reserve_admins,
            fallback=config.telegram.reserve_admins,
            skip=request.skip_reserve,
        )
        reserve_members_eff = _resolved_reserves(
            request.reserve_members,
            fallback=config.telegram.reserve_members,
            skip=request.skip_reserve,
        )
        planned_members = _dedupe(
            [
                *request.members,
                *request.managers,
                *reserve_members_eff,
                *request.admins,
                *reserve_admins_eff,
            ]
        )
        planned_admins = _dedupe([*request.admins, *reserve_admins_eff])
        # Preview any plugin-provided service message (e.g. Planfix's /task).
        plugin_first_message = plugins.group_first_message(
            external_ref=request.external_ref, members_added=planned_members
        )

        folder_payload: dict[str, object] | None = None
        warnings: list[str] = []
        if not request.skip_folder:
            # Same helper the real run uses, so the preview resolves the very
            # same destination folder (a divergence would report a folder error
            # the actual create never hits, and hide the ones it does).
            target_folder_name, target_folder_id = destination_folder(
                config=config.telegram, request=request
            )

            async def _check_folder() -> dict[str, object]:
                try:
                    _, folder_backend = await open_backends()
                    snapshot = await resolve_folder(
                        folder_backend,
                        folder_name=target_folder_name,
                        folder_id=target_folder_id,
                    )
                    return {
                        "folder_id": snapshot.folder_id,
                        "folder_name": snapshot.folder_name,
                    }
                finally:
                    try:
                        await manager.disconnect()
                    except Exception:
                        pass

            try:
                folder_payload = asyncio.run(_check_folder())
            except FolderError as exc:
                typer.echo(str(exc), err=True)
                raise typer.Exit(code=2) from exc
            except Exception as exc:
                typer.echo(f"groups create failed: {exc}", err=True)
                raise typer.Exit(code=1) from exc
        else:
            warnings.append("--skip-folder: new group will not be placed into any folder")

        # Normalise contact phones for the preview; a malformed phone aborts the
        # dry-run the same way the real run would reject it before creation.
        from telegram_assistant.members.service import normalize_phone

        normalized_contacts: list[dict[str, str]] = []
        for spec in request.contacts:
            try:
                normalized_contacts.append(
                    {"phone": normalize_phone(spec.phone), "name": spec.name}
                )
            except ValueError as exc:
                typer.echo(str(exc), err=True)
                raise typer.Exit(code=2) from exc

        planned_actions: list[str] = [
            f"create supergroup title={effective_title!r} enable_topics={enable_topics_eff}",
        ]
        if enable_topics_eff:
            planned_actions.append(
                f"set topics layout to {topics_layout_eff!r}"
            )
        for c in normalized_contacts:
            planned_actions.append(
                f"import contact {c['name']!r} {c['phone']} and add to chat"
            )
        for u in planned_members:
            planned_actions.append(f"add member {u}")
        for u in planned_admins:
            planned_actions.append(f"promote admin {u}")
        if create_link_eff:
            planned_actions.append("create invite link")
        if folder_payload is not None:
            planned_actions.append(
                f"place chat into folder {folder_payload['folder_name']!r}"
            )
        if plugin_first_message is not None:
            planned_actions.append(
                f"send {plugin_first_message!r} service message"
            )
        elif request.external_ref is not None and plugins.active:
            warnings.append(
                "external_ref is set but no plugin will send a service message "
                "for this group (e.g. the required bot is not a planned member)"
            )

        resolved: dict[str, object] = {
            "title": request.title,
            "effective_title": effective_title,
            "external_ref": request.external_ref,
            "enable_topics": enable_topics_eff,
            "topics_layout": topics_layout_eff if enable_topics_eff else None,
            "create_invite_link": create_link_eff,
            "admins": list(request.admins),
            "members": list(request.members),
            "managers": list(request.managers),
            "contacts": normalized_contacts,
            "reserve_admins": list(reserve_admins_eff),
            "reserve_members": list(reserve_members_eff),
            "planned_members": planned_members,
            "planned_admins": planned_admins,
            "folder": folder_payload,
        }
        payload = {
            "status": "dry_run",
            "dry_run": True,
            "command": "groups.create",
            "would": (
                f"create supergroup {effective_title!r} with "
                f"{len(planned_members)} member(s) and "
                f"{len(planned_admins)} admin(s)"
            ),
            "resolved": resolved,
            "planned_actions": planned_actions,
            "warnings": warnings,
        }
        typer.echo(json.dumps(payload, sort_keys=True, default=str))
        return

    async def _run() -> dict[str, object]:
        try:
            group_backend, folder_backend = await open_backends()
            # Group create is gated by WRITE on the *destination folder*; the
            # authorizer only needs a resolver when the policy has chat rules.
            resolver = None
            if config.telegram.access is not None:
                from telegram_assistant.entities import TelethonEntityResolver

                resolver = TelethonEntityResolver(await manager.get_client())
            authorizer = _cli_authorizer(
                config, resolver=resolver, folder_backend=folder_backend
            )
            result, op = await create_group(
                backend=group_backend,
                folder_backend=folder_backend,
                store=store,
                config=config.telegram,
                request=request,
                plugins=plugins,
                authorizer=authorizer,
            )
            payload = result.to_dict()
            payload["operation_id"] = op.id
            payload["operation_status"] = op.status.value
            return payload
        finally:
            try:
                await manager.disconnect()
            except Exception:
                pass

    try:
        payload = asyncio.run(_run())
    except (GroupCreatePending, GroupCreateNeedsReview, GroupCreateFailed) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc
    except Exception as exc:
        _raise_for_access_or_entity_error(exc)
        typer.echo(f"groups create failed: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    typer.echo(json.dumps(payload, sort_keys=True, default=str))


@groups_app.command("set-layout")
def groups_set_layout(
    chat_id: int = typer.Option(
        ...,
        "--chat-id",
        help="Numeric Telegram chat id of the forum supergroup.",
    ),
    layout: str | None = typer.Option(
        None,
        "--layout",
        help="Topics layout: 'list' or 'tabs' "
        "(defaults to telegram.defaults.topics_layout).",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Validate the request and report the plan without changing the layout.",
    ),
    config_path: Path | None = typer.Option(  # noqa: B008
        None,
        "--config",
        "-c",
        help="Path to config.yml (defaults: ./data/config.yml, then ~/.config/telegram-assistant/config.yml).",
        exists=False,
    ),
) -> None:
    """Set the topics layout (list vs tabs) for an existing forum chat."""
    from telegram_assistant.groups import (
        GroupLayoutSetFailed,
        GroupLayoutSetNeedsReview,
        GroupLayoutSetPending,
        LayoutSetRequest,
        set_topics_layout,
    )

    config, manager, store, open_backends = _build_group_backends(config_path)

    effective_layout = (
        layout if layout is not None else config.telegram.defaults.topics_layout
    )
    if effective_layout not in ("list", "tabs"):
        typer.echo(
            f"invalid --layout {effective_layout!r}: expected 'list' or 'tabs'",
            err=True,
        )
        raise typer.Exit(code=2)

    if dry_run:
        resolved_payload: dict[str, object] = {
            "telegram_chat_id": chat_id,
            "layout": effective_layout,
            "layout_source": "cli" if layout is not None else "config",
        }
        planned_actions = [
            f"set topics layout to {effective_layout!r} for chat {chat_id}"
        ]
        payload = {
            "status": "dry_run",
            "dry_run": True,
            "command": "groups.set-layout",
            "would": (
                f"set topics layout for chat {chat_id} to {effective_layout!r}"
            ),
            "resolved": resolved_payload,
            "planned_actions": planned_actions,
            "warnings": [],
        }
        typer.echo(json.dumps(payload, sort_keys=True, default=str))
        return

    async def _run() -> dict[str, object]:
        try:
            group_backend, _folder_backend = await open_backends()
            request = LayoutSetRequest(
                telegram_chat_id=chat_id,
                layout=effective_layout,  # type: ignore[arg-type]
            )
            result, op = await set_topics_layout(
                backend=group_backend, store=store, request=request
            )
            payload = result.to_dict()
            payload["operation_id"] = op.id
            payload["operation_status"] = op.status.value
            return payload
        finally:
            try:
                await manager.disconnect()
            except Exception:
                pass

    try:
        payload = asyncio.run(_run())
    except (
        GroupLayoutSetPending,
        GroupLayoutSetNeedsReview,
        GroupLayoutSetFailed,
    ) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc
    except Exception as exc:
        typer.echo(f"groups set-layout failed: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    typer.echo(json.dumps(payload, sort_keys=True, default=str))


@groups_app.command("get-layout")
def groups_get_layout(
    chat_id: int = typer.Option(
        ...,
        "--chat-id",
        help="Numeric Telegram chat id of the forum supergroup.",
    ),
    config_path: Path | None = typer.Option(  # noqa: B008
        None,
        "--config",
        "-c",
        help="Path to config.yml (defaults: ./data/config.yml, then ~/.config/telegram-assistant/config.yml).",
        exists=False,
    ),
) -> None:
    """Read the current topics layout ('list' or 'tabs') for a forum chat."""
    from telegram_assistant.groups import get_topics_layout

    _config, manager, _store, open_backends = _build_group_backends(config_path)

    async def _run() -> str:
        try:
            group_backend, _folder_backend = await open_backends()
            return await get_topics_layout(
                backend=group_backend, telegram_chat_id=chat_id
            )
        finally:
            try:
                await manager.disconnect()
            except Exception:
                pass

    try:
        layout = asyncio.run(_run())
    except Exception as exc:
        typer.echo(f"groups get-layout failed: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    typer.echo(layout)


@groups_app.command("rename")
def groups_rename(
    new_title: str = typer.Option(..., "--new-title", help="New group title."),
    chat_id: int | None = typer.Option(
        None,
        "--chat-id",
        help="Numeric Telegram chat id of the supergroup.",
    ),
    chat_name: str | None = typer.Option(
        None,
        "--chat-name",
        help="Chat title (resolved within --folder-name).",
    ),
    entity: str | None = typer.Option(
        None,
        "--entity",
        help="Flexible entity reference (numeric id, @username, link, phone, "
        "or exact title) resolved via the shared resolver.",
    ),
    folder_name: str | None = typer.Option(
        None,
        "--folder-name",
        help="Folder used for --chat-name lookup "
        "(defaults to telegram.default_chat_folder.folder_name).",
    ),
    folder_id: int | None = typer.Option(
        None,
        "--folder-id",
        help="Optional folder id cross-check.",
    ),
    reason: str | None = typer.Option(
        None,
        "--reason",
        help="Optional human-readable reason; passed through to logs.",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Validate the request and report the plan without renaming the group.",
    ),
    config_path: Path | None = typer.Option(  # noqa: B008
        None,
        "--config",
        "-c",
        help="Path to config.yml (defaults: ./data/config.yml, then ~/.config/telegram-assistant/config.yml).",
        exists=False,
    ),
) -> None:
    """Rename an existing supergroup (change its title)."""
    from telegram_assistant.folders import (
        FolderError,
    )
    from telegram_assistant.groups import (
        GroupRenameFailed,
        GroupRenameNeedsReview,
        GroupRenamePending,
        GroupRenameRequest,
        rename_group,
    )

    if sum([chat_id is not None, chat_name is not None, entity is not None]) != 1:
        typer.echo(
            "exactly one of --chat-id, --chat-name, or --entity must be supplied",
            err=True,
        )
        raise typer.Exit(code=2)
    if not new_title.strip():
        typer.echo("group rename requires a non-empty --new-title", err=True)
        raise typer.Exit(code=2)

    config, manager, store, open_backends = _build_group_backends(config_path)

    if chat_name is not None:
        resolved_folder_name, default_fid, _ = _resolve_folder_name(
            folder_name, config_path
        )
        effective_folder_id = folder_id if folder_id is not None else default_fid
    else:
        resolved_folder_name = folder_name
        effective_folder_id = folder_id

    if dry_run:
        async def _resolve_rename() -> int:
            try:
                _group_backend, folder_backend = await open_backends()
                resolved_chat_id, _ = await _cli_resolve_chat_and_authorizer(
                    manager=manager,
                    config=config,
                    folder_backend=folder_backend,
                    chat_id=chat_id,
                    chat_name=chat_name,
                    entity=entity,
                    folder_name=resolved_folder_name,
                    folder_id=effective_folder_id,
                )
                return resolved_chat_id
            finally:
                try:
                    await manager.disconnect()
                except Exception:
                    pass

        try:
            resolved_chat_id = asyncio.run(_resolve_rename())
        except FolderError as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(code=2) from exc
        except Exception as exc:
            _raise_for_access_or_entity_error(exc)
            typer.echo(f"groups rename failed: {exc}", err=True)
            raise typer.Exit(code=1) from exc

        resolved_payload: dict[str, object] = {
            "telegram_chat_id": resolved_chat_id,
            "new_title": new_title,
            "reason": reason,
        }
        if chat_name is not None:
            resolved_payload["chat_name"] = chat_name
            resolved_payload["folder_name"] = resolved_folder_name
        would = f"rename chat {resolved_chat_id} to {new_title!r}"
        payload = {
            "status": "dry_run",
            "dry_run": True,
            "command": "groups.rename",
            "would": would,
            "resolved": resolved_payload,
            "planned_actions": [would],
            "warnings": [],
        }
        typer.echo(json.dumps(payload, sort_keys=True, default=str))
        return

    async def _run() -> dict[str, object]:
        try:
            group_backend, folder_backend = await open_backends()
            resolved_chat_id, authorizer = await _cli_resolve_chat_and_authorizer(
                manager=manager,
                config=config,
                folder_backend=folder_backend,
                chat_id=chat_id,
                chat_name=chat_name,
                entity=entity,
                folder_name=resolved_folder_name,
                folder_id=effective_folder_id,
            )

            request = GroupRenameRequest(
                telegram_chat_id=resolved_chat_id,
                new_title=new_title,
                reason=reason,
            )
            result, op = await rename_group(
                backend=group_backend,
                store=store,
                request=request,
                authorizer=authorizer,
            )
            payload = result.to_dict()
            payload["operation_id"] = op.id
            payload["operation_status"] = op.status.value
            return payload
        finally:
            try:
                await manager.disconnect()
            except Exception:
                pass

    try:
        payload = asyncio.run(_run())
    except (GroupRenamePending, GroupRenameNeedsReview, GroupRenameFailed) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc
    except FolderError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc
    except Exception as exc:
        _raise_for_access_or_entity_error(exc)
        typer.echo(f"groups rename failed: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    typer.echo(json.dumps(payload, sort_keys=True, default=str))


# --- topics -----------------------------------------------------------------

topics_app = typer.Typer(help="Manage forum topics.", no_args_is_help=True)
app.add_typer(topics_app, name="topics")


def _build_topic_backends(config_path: Path | None):
    """Open the Telethon-backed topic + folder backends + store for the CLI.

    Mirrors :func:`_build_group_backends`: lazy Telethon imports keep
    ``topics create --help`` cheap.
    """
    config = _load_config_or_exit(config_path)
    manager = TelethonSessionManager(config.telegram)
    store = OperationStore(default_database_path(config))

    async def _open():
        from telegram_assistant.folders import TelethonFolderBackend
        from telegram_assistant.topics.telethon_backend import (
            TelethonTopicBackend,
        )

        client = await manager.get_client()
        if not await client.is_user_authorized():
            raise RuntimeError(
                "Telethon session is not authorized; run "
                "`telegram-assistant auth` first."
            )
        return TelethonTopicBackend(client), TelethonFolderBackend(client)

    return config, manager, store, _open


@topics_app.command("create")
def topics_create(
    topic_name: str = typer.Option(..., "--topic-name", help="Topic title."),
    chat_id: int | None = typer.Option(
        None,
        "--chat-id",
        help="Numeric Telegram chat id of the supergroup.",
    ),
    chat_name: str | None = typer.Option(
        None,
        "--chat-name",
        help="Chat title (resolved within --folder-name).",
    ),
    entity: str | None = typer.Option(
        None,
        "--entity",
        help="Flexible entity reference (numeric id, @username, link, phone, "
        "or exact title) resolved via the shared resolver.",
    ),
    folder_name: str | None = typer.Option(
        None,
        "--folder-name",
        help="Folder used for --chat-name lookup "
        "(defaults to telegram.default_chat_folder.folder_name).",
    ),
    folder_id: int | None = typer.Option(
        None,
        "--folder-id",
        help="Optional folder id cross-check.",
    ),
    external_ref: str | None = typer.Option(
        None,
        "--external-ref",
        help="External reference; primary idempotency key and plugin service-message trigger.",
    ),
    planfix_task_id: str | None = typer.Option(
        None,
        "--planfix-task-id",
        help="Backward-compat alias for --external-ref (Planfix task id).",
    ),
    message: str | None = typer.Option(
        None,
        "--message",
        help="Optional first message text (overrides topic-name duplication).",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Validate the request and report the plan without creating the topic.",
    ),
    config_path: Path | None = typer.Option(  # noqa: B008
        None,
        "--config",
        "-c",
        help="Path to config.yml (defaults: ./data/config.yml, then ~/.config/telegram-assistant/config.yml).",
        exists=False,
    ),
) -> None:
    """Create a single forum topic in an existing supergroup."""
    from telegram_assistant.folders import (
        FolderError,
    )
    from telegram_assistant.plugins import build_registry
    from telegram_assistant.topics import (
        TopicCreateFailed,
        TopicCreateNeedsReview,
        TopicCreatePending,
        TopicCreateRequest,
        create_topic,
    )
    from telegram_assistant.topics.service import _first_message

    if sum([chat_id is not None, chat_name is not None, entity is not None]) != 1:
        typer.echo(
            "exactly one of --chat-id, --chat-name, or --entity must be supplied",
            err=True,
        )
        raise typer.Exit(code=2)

    config, manager, store, open_backends = _build_topic_backends(config_path)
    plugins = build_registry(config)
    # Generic external_ref wins; --planfix-task-id remains a backward-compat alias.
    effective_ref = external_ref if external_ref is not None else planfix_task_id

    if chat_name is not None:
        resolved_folder_name, default_fid, _ = _resolve_folder_name(
            folder_name, config_path
        )
        effective_folder_id = folder_id if folder_id is not None else default_fid
    else:
        resolved_folder_name = folder_name
        effective_folder_id = folder_id

    if dry_run:
        if not topic_name.strip():
            typer.echo("topic create requires non-empty topic_name", err=True)
            raise typer.Exit(code=2)

        async def _resolve() -> tuple[int, list, str | None]:
            try:
                topic_backend, folder_backend = await open_backends()
                resolved_chat_id, _ = await _cli_resolve_chat_and_authorizer(
                    manager=manager,
                    config=config,
                    folder_backend=folder_backend,
                    chat_id=chat_id,
                    chat_name=chat_name,
                    entity=entity,
                    folder_name=resolved_folder_name,
                    folder_id=effective_folder_id,
                )
                list_error: str | None = None
                try:
                    summaries = list(
                        await topic_backend.list_topics(chat_id=resolved_chat_id)
                    )
                except Exception as exc:
                    summaries = []
                    list_error = str(exc) or type(exc).__name__
                return resolved_chat_id, summaries, list_error
            finally:
                try:
                    await manager.disconnect()
                except Exception:
                    pass

        try:
            resolved_chat_id, summaries, list_error = asyncio.run(_resolve())
        except FolderError as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(code=2) from exc
        except Exception as exc:
            _raise_for_access_or_entity_error(exc)
            typer.echo(f"topics create failed: {exc}", err=True)
            raise typer.Exit(code=1) from exc

        existing = [t for t in summaries if t.title == topic_name]
        existing_ids = [t.topic_id for t in existing]
        request_preview = TopicCreateRequest(
            telegram_chat_id=resolved_chat_id,
            topic_name=topic_name,
            external_ref=effective_ref,
            message=message,
        )
        kind, text, task_pending = _first_message(
            request=request_preview, plugins=plugins
        )
        warnings: list[str] = []
        if list_error is not None:
            warnings.append(
                f"could not list existing topics in chat {resolved_chat_id} "
                f"({list_error}); duplicate detection skipped"
            )
        if existing:
            warnings.append(
                f"topic name {topic_name!r} already exists in chat {resolved_chat_id} "
                f"(topic_ids={existing_ids}); real run will be idempotent and may replay"
            )
        planned_actions = [
            f"create topic {topic_name!r} in chat {resolved_chat_id}",
            f"send first message ({kind}): {text!r}",
        ]
        if task_pending:
            task_preview = plugins.topic_first_message(external_ref=effective_ref)
            planned_actions.append(
                f"send plugin service message: {task_preview!r}"
            )
        resolved_payload: dict[str, object] = {
            "telegram_chat_id": resolved_chat_id,
            "topic_name": topic_name,
            "external_ref": effective_ref,
            "first_message_kind": kind,
            "first_message_text": text,
            "existing_topic_ids": existing_ids,
        }
        if chat_name is not None:
            resolved_payload["chat_name"] = chat_name
            resolved_payload["folder_name"] = resolved_folder_name
        payload = {
            "status": "dry_run",
            "dry_run": True,
            "command": "topics.create",
            "would": (
                f"create topic {topic_name!r} in chat {resolved_chat_id}"
            ),
            "resolved": resolved_payload,
            "planned_actions": planned_actions,
            "warnings": warnings,
        }
        typer.echo(json.dumps(payload, sort_keys=True, default=str))
        return

    async def _run() -> dict[str, object]:
        try:
            topic_backend, folder_backend = await open_backends()
            resolved_chat_id, authorizer = await _cli_resolve_chat_and_authorizer(
                manager=manager,
                config=config,
                folder_backend=folder_backend,
                chat_id=chat_id,
                chat_name=chat_name,
                entity=entity,
                folder_name=resolved_folder_name,
                folder_id=effective_folder_id,
            )

            request = TopicCreateRequest(
                telegram_chat_id=resolved_chat_id,
                topic_name=topic_name,
                external_ref=effective_ref,
                message=message,
            )
            result, op = await create_topic(
                backend=topic_backend,
                store=store,
                request=request,
                plugins=plugins,
                authorizer=authorizer,
            )
            payload = result.to_dict()
            payload["operation_id"] = op.id
            payload["operation_status"] = op.status.value
            return payload
        finally:
            try:
                await manager.disconnect()
            except Exception:
                pass

    try:
        payload = asyncio.run(_run())
    except (TopicCreatePending, TopicCreateNeedsReview, TopicCreateFailed) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc
    except FolderError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc
    except Exception as exc:
        _raise_for_access_or_entity_error(exc)
        typer.echo(f"topics create failed: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    typer.echo(json.dumps(payload, sort_keys=True, default=str))


def _parse_bulk_topics_csv(path: Path) -> list[dict[str, object]]:
    """Read an `external_ref,topic_name,message` CSV into bulk items.

    ``external_ref`` (legacy alias ``planfix_task_id``) and ``message`` may be
    empty per row; ``topic_name`` is required. Lines starting with ``#`` and
    blank rows are ignored so operators can keep notes alongside the data.
    """
    import csv

    out: list[dict[str, object]] = []
    with path.open(newline="", encoding="utf-8") as fp:
        reader = csv.DictReader(fp)
        required = {"topic_name"}
        if reader.fieldnames is None or not required.issubset(set(reader.fieldnames)):
            raise typer.BadParameter(
                f"CSV must have at least column 'topic_name'; got {reader.fieldnames!r}"
            )
        for row in reader:
            name = (row.get("topic_name") or "").strip()
            if not name:
                continue
            ref = (row.get("external_ref") or row.get("planfix_task_id") or "").strip() or None
            message = row.get("message")
            if message is not None:
                message = message.strip() or None
            out.append(
                {
                    "topic_name": name,
                    "external_ref": ref,
                    "message": message,
                }
            )
    if not out:
        raise typer.BadParameter("CSV produced zero items")
    return out


def _parse_bulk_topics_json(path: Path) -> list[dict[str, object]]:
    """Read a JSON list of `{topic_name, external_ref?, message?}`.

    ``planfix_task_id`` is accepted as a backward-compat alias for ``external_ref``.
    """
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise typer.BadParameter("JSON file must contain a top-level list")
    out: list[dict[str, object]] = []
    for entry in raw:
        if not isinstance(entry, dict):
            raise typer.BadParameter("each JSON entry must be an object")
        name = str(entry.get("topic_name") or "").strip()
        if not name:
            raise typer.BadParameter("each entry needs a non-empty topic_name")
        ref = entry.get("external_ref")
        if ref is None:
            ref = entry.get("planfix_task_id")
        out.append(
            {
                "topic_name": name,
                "external_ref": ref,
                "message": entry.get("message"),
            }
        )
    if not out:
        raise typer.BadParameter("JSON file produced zero items")
    return out


@topics_app.command("bulk-create")
def topics_bulk_create(
    chat_id: int | None = typer.Option(
        None,
        "--chat-id",
        help="Numeric Telegram chat id of the supergroup.",
    ),
    chat_name: str | None = typer.Option(
        None,
        "--chat-name",
        help="Chat title (resolved within --folder-name).",
    ),
    entity: str | None = typer.Option(
        None,
        "--entity",
        help="Flexible entity reference (numeric id, @username, link, phone, "
        "or exact title) resolved via the shared resolver.",
    ),
    folder_name: str | None = typer.Option(
        None,
        "--folder-name",
        help="Folder used for --chat-name lookup "
        "(defaults to telegram.default_chat_folder.folder_name).",
    ),
    folder_id: int | None = typer.Option(
        None,
        "--folder-id",
        help="Optional folder id cross-check.",
    ),
    file: Path | None = typer.Option(  # noqa: B008
        None,
        "--file",
        help="CSV (external_ref,topic_name,message) or JSON list of items.",
        exists=False,
    ),
    operation_id: str | None = typer.Option(
        None,
        "--operation-id",
        help="Idempotency id for the bulk; rerunning with the same value resumes the batch.",
    ),
    continue_on_error: bool = typer.Option(
        True,
        "--continue-on-error/--stop-on-error",
        help="Continue the batch after a single-item failure (default true).",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Validate the file and report the plan without creating topics.",
    ),
    config_path: Path | None = typer.Option(  # noqa: B008
        None,
        "--config",
        "-c",
        help="Path to config.yml (defaults: ./data/config.yml, then ~/.config/telegram-assistant/config.yml).",
        exists=False,
    ),
) -> None:
    """Bulk-create topics from a CSV or JSON file."""
    from telegram_assistant.folders import (
        FolderError,
    )
    from telegram_assistant.plugins import build_registry
    from telegram_assistant.topics import (
        BulkTopicCreateFailed,
        BulkTopicCreateNeedsReview,
        BulkTopicCreatePending,
        BulkTopicCreateRequest,
        BulkTopicItem,
        bulk_create_topics,
    )
    from telegram_assistant.worker.queue import WorkerQueue

    if sum([chat_id is not None, chat_name is not None, entity is not None]) != 1:
        typer.echo(
            "exactly one of --chat-id, --chat-name, or --entity must be supplied",
            err=True,
        )
        raise typer.Exit(code=2)
    if file is None:
        typer.echo("--file is required (CSV or JSON)", err=True)
        raise typer.Exit(code=2)
    if not file.exists():
        typer.echo(f"--file path does not exist: {file}", err=True)
        raise typer.Exit(code=2)

    raw_items = (
        _parse_bulk_topics_json(file)
        if file.suffix.lower() == ".json"
        else _parse_bulk_topics_csv(file)
    )

    config, manager, store, open_backends = _build_topic_backends(config_path)
    plugins = build_registry(config)

    if chat_name is not None:
        resolved_folder_name, default_fid, _ = _resolve_folder_name(
            folder_name, config_path
        )
        effective_folder_id = folder_id if folder_id is not None else default_fid
    else:
        resolved_folder_name = folder_name
        effective_folder_id = folder_id

    if dry_run:
        async def _resolve_bulk() -> tuple[int, list, str | None]:
            try:
                topic_backend, folder_backend = await open_backends()
                resolved_chat_id, _ = await _cli_resolve_chat_and_authorizer(
                    manager=manager,
                    config=config,
                    folder_backend=folder_backend,
                    chat_id=chat_id,
                    chat_name=chat_name,
                    entity=entity,
                    folder_name=resolved_folder_name,
                    folder_id=effective_folder_id,
                )
                list_error: str | None = None
                try:
                    summaries = list(
                        await topic_backend.list_topics(chat_id=resolved_chat_id)
                    )
                except Exception as exc:
                    summaries = []
                    list_error = str(exc) or type(exc).__name__
                return resolved_chat_id, summaries, list_error
            finally:
                try:
                    await manager.disconnect()
                except Exception:
                    pass

        try:
            resolved_chat_id, summaries, list_error = asyncio.run(_resolve_bulk())
        except FolderError as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(code=2) from exc
        except Exception as exc:
            _raise_for_access_or_entity_error(exc)
            typer.echo(f"topics bulk-create failed: {exc}", err=True)
            raise typer.Exit(code=1) from exc

        existing_titles: dict[str, list[int]] = {}
        for t in summaries:
            existing_titles.setdefault(t.title, []).append(t.topic_id)

        seen_names: dict[str, int] = {}
        seen_refs: dict[str, int] = {}
        items_out: list[dict[str, object]] = []
        warnings: list[str] = []
        if list_error is not None:
            warnings.append(
                f"could not list existing topics in chat {resolved_chat_id} "
                f"({list_error}); duplicate detection skipped"
            )
        for idx, it in enumerate(raw_items):
            name = str(it["topic_name"])
            ref = it.get("external_ref")
            ref_key = str(ref) if ref is not None else None
            dup_in_file_name = name in seen_names
            dup_in_file_ref = ref_key is not None and ref_key in seen_refs
            existing_ids = list(existing_titles.get(name, []))
            row: dict[str, object] = {
                "topic_name": name,
                "external_ref": ref,
                "message": it.get("message"),
                "duplicate_topic_name_in_file": dup_in_file_name,
                "duplicate_external_ref_in_file": dup_in_file_ref,
                "existing_topic_ids": existing_ids,
            }
            if dup_in_file_name:
                warnings.append(
                    f"row {idx + 1}: duplicate topic_name {name!r} "
                    f"(first at row {seen_names[name] + 1})"
                )
            if dup_in_file_ref:
                warnings.append(
                    f"row {idx + 1}: duplicate external_ref {ref!r} "
                    f"(first at row {seen_refs[ref_key] + 1})"
                )
            if existing_ids:
                warnings.append(
                    f"row {idx + 1}: topic_name {name!r} already exists in chat "
                    f"{resolved_chat_id} (topic_ids={existing_ids})"
                )
            items_out.append(row)
            seen_names.setdefault(name, idx)
            if ref_key is not None:
                seen_refs.setdefault(ref_key, idx)

        planned_actions = [
            f"create topic {row['topic_name']!r} in chat {resolved_chat_id}"
            for row in items_out
        ]
        resolved_payload: dict[str, object] = {
            "telegram_chat_id": resolved_chat_id,
            "items": items_out,
            "items_count": len(items_out),
            "file": str(file),
            "operation_id": operation_id,
            "continue_on_error": continue_on_error,
        }
        if chat_name is not None:
            resolved_payload["chat_name"] = chat_name
            resolved_payload["folder_name"] = resolved_folder_name
        payload = {
            "status": "dry_run",
            "dry_run": True,
            "command": "topics.bulk_create",
            "would": (
                f"create {len(items_out)} topic(s) in chat {resolved_chat_id}"
            ),
            "resolved": resolved_payload,
            "planned_actions": planned_actions,
            "warnings": warnings,
        }
        typer.echo(json.dumps(payload, sort_keys=True, default=str))
        return

    queue = WorkerQueue(
        store,
        max_parallel=config.queue.max_parallel_telegram_ops,
        flood_wait_safety_margin_seconds=config.queue.flood_wait_safety_margin_seconds,
        default_retry_delay_seconds=config.queue.default_retry_delay_seconds,
    )

    async def _run() -> dict[str, object]:
        try:
            topic_backend, folder_backend = await open_backends()
            resolved_chat_id, authorizer = await _cli_resolve_chat_and_authorizer(
                manager=manager,
                config=config,
                folder_backend=folder_backend,
                chat_id=chat_id,
                chat_name=chat_name,
                entity=entity,
                folder_name=resolved_folder_name,
                folder_id=effective_folder_id,
            )

            req = BulkTopicCreateRequest(
                telegram_chat_id=resolved_chat_id,
                items=tuple(
                    BulkTopicItem(
                        topic_name=str(it["topic_name"]),
                        external_ref=it.get("external_ref"),
                        message=it.get("message"),
                    )
                    for it in raw_items
                ),
                continue_on_error=continue_on_error,
                operation_id=operation_id,
            )
            result, op = await bulk_create_topics(
                backend=topic_backend,
                store=store,
                queue=queue,
                request=req,
                plugins=plugins,
                authorizer=authorizer,
            )
            payload = result.to_dict()
            payload["operation_status"] = op.status.value
            return payload
        finally:
            try:
                await manager.disconnect()
            except Exception:
                pass

    try:
        payload = asyncio.run(_run())
    except (
        BulkTopicCreatePending,
        BulkTopicCreateNeedsReview,
        BulkTopicCreateFailed,
    ) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc
    except FolderError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc
    except Exception as exc:
        _raise_for_access_or_entity_error(exc)
        typer.echo(f"topics bulk-create failed: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    typer.echo(json.dumps(payload, sort_keys=True, default=str))


@topics_app.command("close")
def topics_close(
    topic_id: int | None = typer.Option(
        None,
        "--topic-id",
        help="Numeric forum topic id to close.",
    ),
    topic_name: str | None = typer.Option(
        None,
        "--topic-name",
        help="Topic title to resolve within the chat (alternative to --topic-id).",
    ),
    chat_id: int | None = typer.Option(
        None,
        "--chat-id",
        help="Numeric Telegram chat id of the supergroup.",
    ),
    chat_name: str | None = typer.Option(
        None,
        "--chat-name",
        help="Chat title (resolved within --folder-name).",
    ),
    entity: str | None = typer.Option(
        None,
        "--entity",
        help="Flexible entity reference (numeric id, @username, link, phone, "
        "or exact title) resolved via the shared resolver.",
    ),
    folder_name: str | None = typer.Option(
        None,
        "--folder-name",
        help="Folder used for --chat-name lookup "
        "(defaults to telegram.default_chat_folder.folder_name).",
    ),
    folder_id: int | None = typer.Option(
        None,
        "--folder-id",
        help="Optional folder id cross-check.",
    ),
    reason: str | None = typer.Option(
        None,
        "--reason",
        help="Optional human-readable reason; passed through to logs.",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Validate the request and report the plan without closing the topic.",
    ),
    config_path: Path | None = typer.Option(  # noqa: B008
        None,
        "--config",
        "-c",
        help="Path to config.yml (defaults: ./data/config.yml, then ~/.config/telegram-assistant/config.yml).",
        exists=False,
    ),
) -> None:
    """Close an existing forum topic (the topic and its history are kept)."""
    from telegram_assistant.folders import (
        FolderError,
    )
    from telegram_assistant.topics import (
        AmbiguousTopicNameError,
        TopicCloseFailed,
        TopicCloseNeedsReview,
        TopicClosePending,
        TopicCloseRequest,
        TopicNotFoundError,
        close_topic,
        resolve_topic_id_by_name,
    )

    if sum([chat_id is not None, chat_name is not None, entity is not None]) != 1:
        typer.echo(
            "exactly one of --chat-id, --chat-name, or --entity must be supplied",
            err=True,
        )
        raise typer.Exit(code=2)
    if (topic_id is None) == (topic_name is None):
        typer.echo(
            "exactly one of --topic-id or --topic-name must be supplied", err=True
        )
        raise typer.Exit(code=2)

    config, manager, store, open_backends = _build_topic_backends(config_path)

    if chat_name is not None:
        resolved_folder_name, default_fid, _ = _resolve_folder_name(
            folder_name, config_path
        )
        effective_folder_id = folder_id if folder_id is not None else default_fid
    else:
        resolved_folder_name = folder_name
        effective_folder_id = folder_id

    if dry_run:
        async def _resolve_close() -> tuple[int, int, bool, str | None]:
            try:
                topic_backend, folder_backend = await open_backends()
                resolved_chat_id, _ = await _cli_resolve_chat_and_authorizer(
                    manager=manager,
                    config=config,
                    folder_backend=folder_backend,
                    chat_id=chat_id,
                    chat_name=chat_name,
                    entity=entity,
                    folder_name=resolved_folder_name,
                    folder_id=effective_folder_id,
                )

                summaries = list(
                    await topic_backend.list_topics(chat_id=resolved_chat_id)
                )
                if topic_id is not None:
                    effective_topic_id = topic_id
                    matches = [t for t in summaries if t.topic_id == topic_id]
                    title = matches[0].title if matches else None
                    if not matches:
                        raise TopicNotFoundError(
                            f"topic id {topic_id} not found in chat {resolved_chat_id}"
                        )
                else:
                    effective_topic_id = await resolve_topic_id_by_name(
                        backend=topic_backend,
                        telegram_chat_id=resolved_chat_id,
                        topic_name=topic_name or "",
                    )
                    title = topic_name
                already_closed = any(
                    t.topic_id == effective_topic_id and t.closed for t in summaries
                )
                return resolved_chat_id, effective_topic_id, already_closed, title
            finally:
                try:
                    await manager.disconnect()
                except Exception:
                    pass

        try:
            (
                resolved_chat_id,
                effective_topic_id,
                already_closed,
                resolved_topic_title,
            ) = asyncio.run(_resolve_close())
        except (AmbiguousTopicNameError, TopicNotFoundError) as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(code=2) from exc
        except FolderError as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(code=2) from exc
        except Exception as exc:
            _raise_for_access_or_entity_error(exc)
            typer.echo(f"topics close failed: {exc}", err=True)
            raise typer.Exit(code=1) from exc

        warnings: list[str] = []
        if already_closed:
            warnings.append(
                f"topic {effective_topic_id} is already closed; real run is a no-op (TOPIC_NOT_MODIFIED)"
            )
        resolved_payload: dict[str, object] = {
            "telegram_chat_id": resolved_chat_id,
            "telegram_topic_id": effective_topic_id,
            "topic_name": resolved_topic_title,
            "already_closed": already_closed,
            "reason": reason,
        }
        if chat_name is not None:
            resolved_payload["chat_name"] = chat_name
            resolved_payload["folder_name"] = resolved_folder_name
        planned_actions = [
            f"close topic {effective_topic_id} in chat {resolved_chat_id} (history preserved)"
        ]
        payload = {
            "status": "dry_run",
            "dry_run": True,
            "command": "topics.close",
            "would": (
                f"close topic {effective_topic_id} in chat {resolved_chat_id}"
            ),
            "resolved": resolved_payload,
            "planned_actions": planned_actions,
            "warnings": warnings,
        }
        typer.echo(json.dumps(payload, sort_keys=True, default=str))
        return

    async def _run() -> dict[str, object]:
        try:
            topic_backend, folder_backend = await open_backends()
            resolved_chat_id, authorizer = await _cli_resolve_chat_and_authorizer(
                manager=manager,
                config=config,
                folder_backend=folder_backend,
                chat_id=chat_id,
                chat_name=chat_name,
                entity=entity,
                folder_name=resolved_folder_name,
                folder_id=effective_folder_id,
            )

            if topic_id is not None:
                effective_topic_id = topic_id
            else:
                effective_topic_id = await resolve_topic_id_by_name(
                    backend=topic_backend,
                    telegram_chat_id=resolved_chat_id,
                    topic_name=topic_name or "",
                )

            request = TopicCloseRequest(
                telegram_chat_id=resolved_chat_id,
                telegram_topic_id=effective_topic_id,
                reason=reason,
            )
            result, op = await close_topic(
                backend=topic_backend,
                store=store,
                request=request,
                authorizer=authorizer,
            )
            payload = result.to_dict()
            payload["operation_id"] = op.id
            payload["operation_status"] = op.status.value
            return payload
        finally:
            try:
                await manager.disconnect()
            except Exception:
                pass

    try:
        payload = asyncio.run(_run())
    except (TopicClosePending, TopicCloseNeedsReview, TopicCloseFailed) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc
    except (AmbiguousTopicNameError, TopicNotFoundError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc
    except FolderError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc
    except Exception as exc:
        _raise_for_access_or_entity_error(exc)
        typer.echo(f"topics close failed: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    typer.echo(json.dumps(payload, sort_keys=True, default=str))


@topics_app.command("open")
def topics_open(
    topic_id: int | None = typer.Option(
        None,
        "--topic-id",
        help="Numeric forum topic id to reopen.",
    ),
    topic_name: str | None = typer.Option(
        None,
        "--topic-name",
        help="Topic title to resolve within the chat (alternative to --topic-id).",
    ),
    chat_id: int | None = typer.Option(
        None,
        "--chat-id",
        help="Numeric Telegram chat id of the supergroup.",
    ),
    chat_name: str | None = typer.Option(
        None,
        "--chat-name",
        help="Chat title (resolved within --folder-name).",
    ),
    entity: str | None = typer.Option(
        None,
        "--entity",
        help="Flexible entity reference (numeric id, @username, link, phone, "
        "or exact title) resolved via the shared resolver.",
    ),
    folder_name: str | None = typer.Option(
        None,
        "--folder-name",
        help="Folder used for --chat-name lookup "
        "(defaults to telegram.default_chat_folder.folder_name).",
    ),
    folder_id: int | None = typer.Option(
        None,
        "--folder-id",
        help="Optional folder id cross-check.",
    ),
    reason: str | None = typer.Option(
        None,
        "--reason",
        help="Optional human-readable reason; passed through to logs.",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Validate the request and report the plan without reopening the topic.",
    ),
    config_path: Path | None = typer.Option(  # noqa: B008
        None,
        "--config",
        "-c",
        help="Path to config.yml (defaults: ./data/config.yml, then ~/.config/telegram-assistant/config.yml).",
        exists=False,
    ),
) -> None:
    """Reopen a closed forum topic (the topic and its history are kept)."""
    from telegram_assistant.folders import (
        FolderError,
    )
    from telegram_assistant.topics import (
        AmbiguousTopicNameError,
        TopicNotFoundError,
        TopicOpenFailed,
        TopicOpenNeedsReview,
        TopicOpenPending,
        TopicOpenRequest,
        open_topic,
        resolve_topic_id_by_name,
    )

    if sum([chat_id is not None, chat_name is not None, entity is not None]) != 1:
        typer.echo(
            "exactly one of --chat-id, --chat-name, or --entity must be supplied",
            err=True,
        )
        raise typer.Exit(code=2)
    if (topic_id is None) == (topic_name is None):
        typer.echo(
            "exactly one of --topic-id or --topic-name must be supplied", err=True
        )
        raise typer.Exit(code=2)

    config, manager, store, open_backends = _build_topic_backends(config_path)

    if chat_name is not None:
        resolved_folder_name, default_fid, _ = _resolve_folder_name(
            folder_name, config_path
        )
        effective_folder_id = folder_id if folder_id is not None else default_fid
    else:
        resolved_folder_name = folder_name
        effective_folder_id = folder_id

    if dry_run:
        async def _resolve_open() -> tuple[int, int, bool, str | None]:
            try:
                topic_backend, folder_backend = await open_backends()
                resolved_chat_id, _ = await _cli_resolve_chat_and_authorizer(
                    manager=manager,
                    config=config,
                    folder_backend=folder_backend,
                    chat_id=chat_id,
                    chat_name=chat_name,
                    entity=entity,
                    folder_name=resolved_folder_name,
                    folder_id=effective_folder_id,
                )

                summaries = list(
                    await topic_backend.list_topics(chat_id=resolved_chat_id)
                )
                if topic_id is not None:
                    effective_topic_id = topic_id
                    matches = [t for t in summaries if t.topic_id == topic_id]
                    title = matches[0].title if matches else None
                    if not matches:
                        raise TopicNotFoundError(
                            f"topic id {topic_id} not found in chat {resolved_chat_id}"
                        )
                else:
                    effective_topic_id = await resolve_topic_id_by_name(
                        backend=topic_backend,
                        telegram_chat_id=resolved_chat_id,
                        topic_name=topic_name or "",
                    )
                    title = topic_name
                already_open = any(
                    t.topic_id == effective_topic_id and not t.closed
                    for t in summaries
                )
                return resolved_chat_id, effective_topic_id, already_open, title
            finally:
                try:
                    await manager.disconnect()
                except Exception:
                    pass

        try:
            (
                resolved_chat_id,
                effective_topic_id,
                already_open,
                resolved_topic_title,
            ) = asyncio.run(_resolve_open())
        except (AmbiguousTopicNameError, TopicNotFoundError) as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(code=2) from exc
        except FolderError as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(code=2) from exc
        except Exception as exc:
            _raise_for_access_or_entity_error(exc)
            typer.echo(f"topics open failed: {exc}", err=True)
            raise typer.Exit(code=1) from exc

        warnings: list[str] = []
        if already_open:
            warnings.append(
                f"topic {effective_topic_id} is already open; real run is a no-op (TOPIC_NOT_MODIFIED)"
            )
        resolved_payload: dict[str, object] = {
            "telegram_chat_id": resolved_chat_id,
            "telegram_topic_id": effective_topic_id,
            "topic_name": resolved_topic_title,
            "already_open": already_open,
            "reason": reason,
        }
        if chat_name is not None:
            resolved_payload["chat_name"] = chat_name
            resolved_payload["folder_name"] = resolved_folder_name
        planned_actions = [
            f"reopen topic {effective_topic_id} in chat {resolved_chat_id} (history preserved)"
        ]
        payload = {
            "status": "dry_run",
            "dry_run": True,
            "command": "topics.open",
            "would": (
                f"reopen topic {effective_topic_id} in chat {resolved_chat_id}"
            ),
            "resolved": resolved_payload,
            "planned_actions": planned_actions,
            "warnings": warnings,
        }
        typer.echo(json.dumps(payload, sort_keys=True, default=str))
        return

    async def _run() -> dict[str, object]:
        try:
            topic_backend, folder_backend = await open_backends()
            resolved_chat_id, authorizer = await _cli_resolve_chat_and_authorizer(
                manager=manager,
                config=config,
                folder_backend=folder_backend,
                chat_id=chat_id,
                chat_name=chat_name,
                entity=entity,
                folder_name=resolved_folder_name,
                folder_id=effective_folder_id,
            )

            if topic_id is not None:
                effective_topic_id = topic_id
            else:
                effective_topic_id = await resolve_topic_id_by_name(
                    backend=topic_backend,
                    telegram_chat_id=resolved_chat_id,
                    topic_name=topic_name or "",
                )

            request = TopicOpenRequest(
                telegram_chat_id=resolved_chat_id,
                telegram_topic_id=effective_topic_id,
                reason=reason,
            )
            result, op = await open_topic(
                backend=topic_backend,
                store=store,
                request=request,
                authorizer=authorizer,
            )
            payload = result.to_dict()
            payload["operation_id"] = op.id
            payload["operation_status"] = op.status.value
            return payload
        finally:
            try:
                await manager.disconnect()
            except Exception:
                pass

    try:
        payload = asyncio.run(_run())
    except (TopicOpenPending, TopicOpenNeedsReview, TopicOpenFailed) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc
    except (AmbiguousTopicNameError, TopicNotFoundError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc
    except FolderError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc
    except Exception as exc:
        _raise_for_access_or_entity_error(exc)
        typer.echo(f"topics open failed: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    typer.echo(json.dumps(payload, sort_keys=True, default=str))


@topics_app.command("rename")
def topics_rename(
    new_title: str = typer.Option(..., "--new-title", help="New topic title."),
    topic_id: int | None = typer.Option(
        None,
        "--topic-id",
        help="Numeric forum topic id to rename.",
    ),
    topic_name: str | None = typer.Option(
        None,
        "--topic-name",
        help="Current topic title to resolve within the chat (alternative to --topic-id).",
    ),
    chat_id: int | None = typer.Option(
        None,
        "--chat-id",
        help="Numeric Telegram chat id of the supergroup.",
    ),
    chat_name: str | None = typer.Option(
        None,
        "--chat-name",
        help="Chat title (resolved within --folder-name).",
    ),
    entity: str | None = typer.Option(
        None,
        "--entity",
        help="Flexible entity reference (numeric id, @username, link, phone, "
        "or exact title) resolved via the shared resolver.",
    ),
    folder_name: str | None = typer.Option(
        None,
        "--folder-name",
        help="Folder used for --chat-name lookup "
        "(defaults to telegram.default_chat_folder.folder_name).",
    ),
    folder_id: int | None = typer.Option(
        None,
        "--folder-id",
        help="Optional folder id cross-check.",
    ),
    reason: str | None = typer.Option(
        None,
        "--reason",
        help="Optional human-readable reason; passed through to logs.",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Validate the request and report the plan without renaming the topic.",
    ),
    config_path: Path | None = typer.Option(  # noqa: B008
        None,
        "--config",
        "-c",
        help="Path to config.yml (defaults: ./data/config.yml, then ~/.config/telegram-assistant/config.yml).",
        exists=False,
    ),
) -> None:
    """Rename an existing forum topic (change its title)."""
    from telegram_assistant.folders import (
        FolderError,
    )
    from telegram_assistant.topics import (
        AmbiguousTopicNameError,
        TopicNotFoundError,
        TopicRenameFailed,
        TopicRenameNeedsReview,
        TopicRenamePending,
        TopicRenameRequest,
        rename_topic,
        resolve_topic_id_by_name,
    )

    if sum([chat_id is not None, chat_name is not None, entity is not None]) != 1:
        typer.echo(
            "exactly one of --chat-id, --chat-name, or --entity must be supplied",
            err=True,
        )
        raise typer.Exit(code=2)
    if (topic_id is None) == (topic_name is None):
        typer.echo(
            "exactly one of --topic-id or --topic-name must be supplied", err=True
        )
        raise typer.Exit(code=2)
    if not new_title.strip():
        typer.echo("topic rename requires a non-empty --new-title", err=True)
        raise typer.Exit(code=2)

    config, manager, store, open_backends = _build_topic_backends(config_path)

    if chat_name is not None:
        resolved_folder_name, default_fid, _ = _resolve_folder_name(
            folder_name, config_path
        )
        effective_folder_id = folder_id if folder_id is not None else default_fid
    else:
        resolved_folder_name = folder_name
        effective_folder_id = folder_id

    if dry_run:
        async def _resolve_rename() -> tuple[int, int, str | None]:
            try:
                topic_backend, folder_backend = await open_backends()
                resolved_chat_id, _ = await _cli_resolve_chat_and_authorizer(
                    manager=manager,
                    config=config,
                    folder_backend=folder_backend,
                    chat_id=chat_id,
                    chat_name=chat_name,
                    entity=entity,
                    folder_name=resolved_folder_name,
                    folder_id=effective_folder_id,
                )

                summaries = list(
                    await topic_backend.list_topics(chat_id=resolved_chat_id)
                )
                if topic_id is not None:
                    effective_topic_id = topic_id
                    matches = [t for t in summaries if t.topic_id == topic_id]
                    title = matches[0].title if matches else None
                    if not matches:
                        raise TopicNotFoundError(
                            f"topic id {topic_id} not found in chat {resolved_chat_id}"
                        )
                else:
                    effective_topic_id = await resolve_topic_id_by_name(
                        backend=topic_backend,
                        telegram_chat_id=resolved_chat_id,
                        topic_name=topic_name or "",
                    )
                    title = topic_name
                return resolved_chat_id, effective_topic_id, title
            finally:
                try:
                    await manager.disconnect()
                except Exception:
                    pass

        try:
            (
                resolved_chat_id,
                effective_topic_id,
                resolved_topic_title,
            ) = asyncio.run(_resolve_rename())
        except (AmbiguousTopicNameError, TopicNotFoundError) as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(code=2) from exc
        except FolderError as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(code=2) from exc
        except Exception as exc:
            _raise_for_access_or_entity_error(exc)
            typer.echo(f"topics rename failed: {exc}", err=True)
            raise typer.Exit(code=1) from exc

        resolved_payload: dict[str, object] = {
            "telegram_chat_id": resolved_chat_id,
            "telegram_topic_id": effective_topic_id,
            "old_title": resolved_topic_title,
            "new_title": new_title,
            "reason": reason,
        }
        if chat_name is not None:
            resolved_payload["chat_name"] = chat_name
            resolved_payload["folder_name"] = resolved_folder_name
        would = (
            f"rename topic {effective_topic_id} in chat {resolved_chat_id} "
            f"to {new_title!r}"
        )
        payload = {
            "status": "dry_run",
            "dry_run": True,
            "command": "topics.rename",
            "would": would,
            "resolved": resolved_payload,
            "planned_actions": [would],
            "warnings": [],
        }
        typer.echo(json.dumps(payload, sort_keys=True, default=str))
        return

    async def _run() -> dict[str, object]:
        try:
            topic_backend, folder_backend = await open_backends()
            resolved_chat_id, authorizer = await _cli_resolve_chat_and_authorizer(
                manager=manager,
                config=config,
                folder_backend=folder_backend,
                chat_id=chat_id,
                chat_name=chat_name,
                entity=entity,
                folder_name=resolved_folder_name,
                folder_id=effective_folder_id,
            )

            if topic_id is not None:
                effective_topic_id = topic_id
            else:
                effective_topic_id = await resolve_topic_id_by_name(
                    backend=topic_backend,
                    telegram_chat_id=resolved_chat_id,
                    topic_name=topic_name or "",
                )

            request = TopicRenameRequest(
                telegram_chat_id=resolved_chat_id,
                telegram_topic_id=effective_topic_id,
                new_title=new_title,
                reason=reason,
            )
            result, op = await rename_topic(
                backend=topic_backend,
                store=store,
                request=request,
                authorizer=authorizer,
            )
            payload = result.to_dict()
            payload["operation_id"] = op.id
            payload["operation_status"] = op.status.value
            return payload
        finally:
            try:
                await manager.disconnect()
            except Exception:
                pass

    try:
        payload = asyncio.run(_run())
    except (TopicRenamePending, TopicRenameNeedsReview, TopicRenameFailed) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc
    except (AmbiguousTopicNameError, TopicNotFoundError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc
    except FolderError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc
    except Exception as exc:
        _raise_for_access_or_entity_error(exc)
        typer.echo(f"topics rename failed: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    typer.echo(json.dumps(payload, sort_keys=True, default=str))


# --- members ----------------------------------------------------------------

members_app = typer.Typer(help="Manage group membership.", no_args_is_help=True)
app.add_typer(members_app, name="members")


def _build_member_backends(config_path: Path | None):
    """Open the Telethon-backed member + folder backends + store for the CLI.

    Mirrors :func:`_build_topic_backends`: lazy Telethon imports keep
    ``members bulk-add --help`` cheap.
    """
    config = _load_config_or_exit(config_path)
    manager = TelethonSessionManager(config.telegram)
    store = OperationStore(default_database_path(config))

    async def _open():
        from telegram_assistant.folders import TelethonFolderBackend
        from telegram_assistant.members.telethon_backend import (
            TelethonMemberBackend,
        )

        client = await manager.get_client()
        if not await client.is_user_authorized():
            raise RuntimeError(
                "Telethon session is not authorized; run "
                "`telegram-assistant auth` first."
            )
        return TelethonMemberBackend(client), TelethonFolderBackend(client)

    return config, manager, store, _open


def _parse_bulk_members_csv(path: Path) -> list[dict[str, object]]:
    """Read a `user,role` CSV into bulk items.

    ``role`` defaults to ``"member"`` when the column is missing or empty.
    Lines starting with ``#`` and blank rows are ignored so operators can
    keep notes alongside the data.
    """
    import csv

    out: list[dict[str, object]] = []
    with path.open(newline="", encoding="utf-8") as fp:
        reader = csv.DictReader(fp)
        if reader.fieldnames is None or "user" not in set(reader.fieldnames):
            raise typer.BadParameter(
                f"CSV must have at least column 'user'; got {reader.fieldnames!r}"
            )
        for row in reader:
            user = (row.get("user") or "").strip()
            if not user or user.startswith("#"):
                continue
            role = (row.get("role") or "member").strip() or "member"
            out.append({"user": user, "role": role})
    if not out:
        raise typer.BadParameter("CSV produced zero items")
    return out


def _parse_bulk_members_json(path: Path) -> list[dict[str, object]]:
    """Read a JSON list of `{user, role?}`."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise typer.BadParameter("JSON file must contain a top-level list")
    out: list[dict[str, object]] = []
    for entry in raw:
        if not isinstance(entry, dict):
            raise typer.BadParameter("each JSON entry must be an object")
        user = str(entry.get("user") or "").strip()
        if not user:
            raise typer.BadParameter("each entry needs a non-empty user")
        role = str(entry.get("role") or "member").strip() or "member"
        out.append({"user": user, "role": role})
    if not out:
        raise typer.BadParameter("JSON file produced zero items")
    return out


@members_app.command("bulk-add")
def members_bulk_add(
    chat_id: int | None = typer.Option(
        None,
        "--chat-id",
        help="Numeric Telegram chat id of the supergroup.",
    ),
    chat_name: str | None = typer.Option(
        None,
        "--chat-name",
        help="Chat title (resolved within --folder-name).",
    ),
    entity: str | None = typer.Option(
        None,
        "--entity",
        help="Flexible entity reference (numeric id, @username, link, phone, "
        "or exact title) resolved via the shared resolver.",
    ),
    folder_name: str | None = typer.Option(
        None,
        "--folder-name",
        help="Folder used for --chat-name lookup "
        "(defaults to telegram.default_chat_folder.folder_name).",
    ),
    folder_id: int | None = typer.Option(
        None,
        "--folder-id",
        help="Optional folder id cross-check.",
    ),
    file: Path | None = typer.Option(  # noqa: B008
        None,
        "--file",
        help="CSV (user,role) or JSON list of items.",
        exists=False,
    ),
    user: list[str] | None = typer.Option(  # noqa: B008
        None,
        "--user",
        help="User to add (repeat for multiple). Use --admin to mark as admin.",
    ),
    admin: list[str] | None = typer.Option(  # noqa: B008
        None,
        "--admin",
        help="User to add as admin (repeat for multiple).",
    ),
    operation_id: str | None = typer.Option(
        None,
        "--operation-id",
        help="Idempotency id for the bulk; rerunning with the same value resumes the batch.",
    ),
    continue_on_error: bool = typer.Option(
        True,
        "--continue-on-error/--stop-on-error",
        help="Continue the batch after a single-item failure (default true).",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Validate the request and report the plan without adding members.",
    ),
    config_path: Path | None = typer.Option(  # noqa: B008
        None,
        "--config",
        "-c",
        help="Path to config.yml (defaults: ./data/config.yml, then ~/.config/telegram-assistant/config.yml).",
        exists=False,
    ),
) -> None:
    """Bulk-add members to an existing supergroup, optionally promoting to admin."""
    from telegram_assistant.folders import (
        FolderError,
    )
    from telegram_assistant.members import (
        BulkMemberAddFailed,
        BulkMemberAddNeedsReview,
        BulkMemberAddPending,
        BulkMemberAddRequest,
        BulkMemberItem,
        bulk_add_members,
        normalize_user_ref,
        protected_user_set,
    )
    from telegram_assistant.plugins import build_registry
    from telegram_assistant.worker.queue import WorkerQueue

    if sum([chat_id is not None, chat_name is not None, entity is not None]) != 1:
        typer.echo(
            "exactly one of --chat-id, --chat-name, or --entity must be supplied",
            err=True,
        )
        raise typer.Exit(code=2)

    raw_items: list[dict[str, object]] = []
    if file is not None:
        if not file.exists():
            typer.echo(f"--file path does not exist: {file}", err=True)
            raise typer.Exit(code=2)
        raw_items.extend(
            _parse_bulk_members_json(file)
            if file.suffix.lower() == ".json"
            else _parse_bulk_members_csv(file)
        )
    for u in user or []:
        raw_items.append({"user": u, "role": "member"})
    for u in admin or []:
        raw_items.append({"user": u, "role": "admin"})

    if not raw_items:
        typer.echo(
            "no users supplied: use --file, --user, or --admin", err=True
        )
        raise typer.Exit(code=2)

    config, manager, store, open_backends = _build_member_backends(config_path)

    if chat_name is not None:
        resolved_folder_name, default_fid, _ = _resolve_folder_name(
            folder_name, config_path
        )
        effective_folder_id = folder_id if folder_id is not None else default_fid
    else:
        resolved_folder_name = folder_name
        effective_folder_id = folder_id

    if dry_run:
        # Validate items shape (roles) up-front, same error path as live run.
        try:
            items_validated = tuple(
                BulkMemberItem(user=str(it["user"]), role=str(it["role"]))
                for it in raw_items
            )
        except ValueError as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(code=2) from exc

        protected = protected_user_set(
            config=config.telegram, plugins=build_registry(config)
        )
        try:
            normalized_inputs = [
                (it, normalize_user_ref(it.user)) for it in items_validated
            ]
        except ValueError as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(code=2) from exc

        async def _resolve_add() -> int:
            try:
                _, folder_backend = await open_backends()
                resolved_chat_id, _ = await _cli_resolve_chat_and_authorizer(
                    manager=manager,
                    config=config,
                    folder_backend=folder_backend,
                    chat_id=chat_id,
                    chat_name=chat_name,
                    entity=entity,
                    folder_name=resolved_folder_name,
                    folder_id=effective_folder_id,
                )
                return resolved_chat_id
            finally:
                try:
                    await manager.disconnect()
                except Exception:
                    pass

        try:
            resolved_chat_id = asyncio.run(_resolve_add())
        except FolderError as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(code=2) from exc
        except Exception as exc:
            _raise_for_access_or_entity_error(exc)
            typer.echo(f"members bulk-add failed: {exc}", err=True)
            raise typer.Exit(code=1) from exc

        items_out: list[dict[str, object]] = []
        planned: list[str] = []
        warnings: list[str] = []
        seen_users: dict[str, int] = {}
        for idx, (item, n) in enumerate(normalized_inputs):
            is_protected = n.value in protected
            is_duplicate = n.value in seen_users
            items_out.append(
                {
                    "user": item.user,
                    "role": item.role,
                    "normalized_user": n.value,
                    "user_kind": n.kind,
                    "action": "would_add_and_promote" if item.role == "admin" else "would_add",
                    "protected": is_protected,
                    "duplicate_in_file": is_duplicate,
                }
            )
            verb = (
                f"would add+promote {n.value} in chat {resolved_chat_id}"
                if item.role == "admin"
                else f"would add {n.value} to chat {resolved_chat_id}"
            )
            planned.append(verb)
            if is_duplicate:
                warnings.append(
                    f"row {idx + 1}: duplicate user {n.value} "
                    f"(first at row {seen_users[n.value] + 1})"
                )
            if is_protected:
                warnings.append(
                    f"{n.value} is a protected/technical account; "
                    "re-adding it has no effect on a real run"
                )
            seen_users.setdefault(n.value, idx)

        resolved_payload: dict[str, object] = {
            "telegram_chat_id": resolved_chat_id,
            "items": items_out,
            "items_count": len(items_out),
            "continue_on_error": continue_on_error,
            "operation_id": operation_id,
        }
        if chat_name is not None:
            resolved_payload["chat_name"] = chat_name
            resolved_payload["folder_name"] = resolved_folder_name
        payload = {
            "status": "dry_run",
            "dry_run": True,
            "command": "members.bulk_add",
            "would": (
                f"add {len(items_out)} user(s) to chat {resolved_chat_id}"
            ),
            "resolved": resolved_payload,
            "planned_actions": planned,
            "warnings": warnings,
        }
        typer.echo(json.dumps(payload, sort_keys=True, default=str))
        return

    queue = WorkerQueue(
        store,
        max_parallel=config.queue.max_parallel_telegram_ops,
        flood_wait_safety_margin_seconds=config.queue.flood_wait_safety_margin_seconds,
        default_retry_delay_seconds=config.queue.default_retry_delay_seconds,
    )

    async def _run() -> dict[str, object]:
        try:
            member_backend, folder_backend = await open_backends()
            resolved_chat_id, authorizer = await _cli_resolve_chat_and_authorizer(
                manager=manager,
                config=config,
                folder_backend=folder_backend,
                chat_id=chat_id,
                chat_name=chat_name,
                entity=entity,
                folder_name=resolved_folder_name,
                folder_id=effective_folder_id,
            )

            try:
                items = tuple(
                    BulkMemberItem(user=str(it["user"]), role=str(it["role"]))
                    for it in raw_items
                )
            except ValueError as exc:
                typer.echo(str(exc), err=True)
                raise typer.Exit(code=2) from exc
            req = BulkMemberAddRequest(
                telegram_chat_id=resolved_chat_id,
                items=items,
                continue_on_error=continue_on_error,
                operation_id=operation_id,
            )
            result, op = await bulk_add_members(
                backend=member_backend,
                store=store,
                queue=queue,
                request=req,
                authorizer=authorizer,
            )
            payload = result.to_dict()
            payload["operation_status"] = op.status.value
            return payload
        finally:
            try:
                await manager.disconnect()
            except Exception:
                pass

    try:
        payload = asyncio.run(_run())
    except (
        BulkMemberAddPending,
        BulkMemberAddNeedsReview,
        BulkMemberAddFailed,
    ) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc
    except FolderError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc
    except Exception as exc:
        _raise_for_access_or_entity_error(exc)
        typer.echo(f"members bulk-add failed: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    typer.echo(json.dumps(payload, sort_keys=True, default=str))


def _parse_bulk_remove_csv(path: Path) -> list[str]:
    """Read a `user` CSV into bulk-remove items.

    Lines starting with ``#`` and blank rows are ignored so operators can
    keep notes alongside the data.
    """
    import csv

    out: list[str] = []
    with path.open(newline="", encoding="utf-8") as fp:
        reader = csv.DictReader(fp)
        if reader.fieldnames is None or "user" not in set(reader.fieldnames):
            raise typer.BadParameter(
                f"CSV must have at least column 'user'; got {reader.fieldnames!r}"
            )
        for row in reader:
            user = (row.get("user") or "").strip()
            if not user or user.startswith("#"):
                continue
            out.append(user)
    if not out:
        raise typer.BadParameter("CSV produced zero items")
    return out


def _parse_bulk_remove_json(path: Path) -> list[str]:
    """Read a JSON list of users (strings or `{user: ...}` objects)."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise typer.BadParameter("JSON file must contain a top-level list")
    out: list[str] = []
    for entry in raw:
        if isinstance(entry, str):
            user = entry.strip()
        elif isinstance(entry, dict):
            user = str(entry.get("user") or "").strip()
        else:
            raise typer.BadParameter(
                "each JSON entry must be a string or {user: ...} object"
            )
        if not user:
            raise typer.BadParameter("each entry needs a non-empty user")
        out.append(user)
    if not out:
        raise typer.BadParameter("JSON file produced zero items")
    return out


@members_app.command("bulk-remove")
def members_bulk_remove(
    chat_id: int | None = typer.Option(
        None,
        "--chat-id",
        help="Numeric Telegram chat id of the supergroup.",
    ),
    chat_name: str | None = typer.Option(
        None,
        "--chat-name",
        help="Chat title (resolved within --folder-name).",
    ),
    entity: str | None = typer.Option(
        None,
        "--entity",
        help="Flexible entity reference (numeric id, @username, link, phone, "
        "or exact title) resolved via the shared resolver.",
    ),
    folder_name: str | None = typer.Option(
        None,
        "--folder-name",
        help="Folder used for --chat-name lookup "
        "(defaults to telegram.default_chat_folder.folder_name).",
    ),
    folder_id: int | None = typer.Option(
        None,
        "--folder-id",
        help="Optional folder id cross-check.",
    ),
    file: Path | None = typer.Option(  # noqa: B008
        None,
        "--file",
        help="CSV (user) or JSON list of users.",
        exists=False,
    ),
    user: list[str] | None = typer.Option(  # noqa: B008
        None,
        "--user",
        help="User to remove (repeat for multiple).",
    ),
    mode: str = typer.Option(
        "ban_unban",
        "--mode",
        help="Removal mode: ban_unban (kick, default) or ban (permanent blacklist).",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Report the intended action per user without performing it.",
    ),
    yes: bool = typer.Option(
        False,
        "--yes",
        help="Confirm destructive bulk removal (required unless --dry-run).",
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help="Allow removing configured reserve accounts or plugin-protected accounts.",
    ),
    operation_id: str | None = typer.Option(
        None,
        "--operation-id",
        help="Idempotency id for the bulk; rerunning with the same value resumes the batch.",
    ),
    continue_on_error: bool = typer.Option(
        True,
        "--continue-on-error/--stop-on-error",
        help="Continue the batch after a single-item failure (default true).",
    ),
    config_path: Path | None = typer.Option(  # noqa: B008
        None,
        "--config",
        "-c",
        help="Path to config.yml (defaults: ./data/config.yml, then ~/.config/telegram-assistant/config.yml).",
        exists=False,
    ),
) -> None:
    """Bulk-remove members from a supergroup (kick or permanently ban)."""
    from telegram_assistant.folders import (
        FolderError,
    )
    from telegram_assistant.members import (
        BulkMemberRemoveFailed,
        BulkMemberRemoveItem,
        BulkMemberRemoveNeedsReview,
        BulkMemberRemovePending,
        BulkMemberRemoveRequest,
        bulk_remove_members,
        normalize_user_ref,
        protected_user_set,
    )
    from telegram_assistant.members.service import VALID_REMOVE_MODES
    from telegram_assistant.plugins import build_registry
    from telegram_assistant.worker.queue import WorkerQueue

    if sum([chat_id is not None, chat_name is not None, entity is not None]) != 1:
        typer.echo(
            "exactly one of --chat-id, --chat-name, or --entity must be supplied",
            err=True,
        )
        raise typer.Exit(code=2)

    if mode not in VALID_REMOVE_MODES:
        typer.echo(
            f"invalid mode {mode!r}; expected one of {sorted(VALID_REMOVE_MODES)}",
            err=True,
        )
        raise typer.Exit(code=2)

    raw_users: list[str] = []
    if file is not None:
        if not file.exists():
            typer.echo(f"--file path does not exist: {file}", err=True)
            raise typer.Exit(code=2)
        raw_users.extend(
            _parse_bulk_remove_json(file)
            if file.suffix.lower() == ".json"
            else _parse_bulk_remove_csv(file)
        )
    for u in user or []:
        raw_users.append(u)

    if not raw_users:
        typer.echo("no users supplied: use --file or --user", err=True)
        raise typer.Exit(code=2)

    if not dry_run and not yes:
        typer.echo(
            "refusing to remove without --yes (or use --dry-run to preview)",
            err=True,
        )
        raise typer.Exit(code=2)

    config, manager, store, open_backends = _build_member_backends(config_path)
    protected = protected_user_set(
        config=config.telegram, plugins=build_registry(config)
    )
    try:
        normalized_inputs = [
            (raw, normalize_user_ref(raw)) for raw in raw_users
        ]
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc

    if not force and not dry_run:
        blocked = [
            raw for raw, n in normalized_inputs if n.value in protected
        ]
        if blocked:
            typer.echo(
                "refusing to remove protected accounts without --force: "
                + ", ".join(blocked),
                err=True,
            )
            raise typer.Exit(code=2)

    if chat_name is not None:
        resolved_folder_name, default_fid, _ = _resolve_folder_name(
            folder_name, config_path
        )
        effective_folder_id = folder_id if folder_id is not None else default_fid
    else:
        resolved_folder_name = folder_name
        effective_folder_id = folder_id

    if dry_run:
        if chat_id is not None and entity is None:
            resolved_chat_id = chat_id
        else:
            async def _resolve_chat() -> int:
                try:
                    _, folder_backend = await open_backends()
                    resolved_id, _ = await _cli_resolve_chat_and_authorizer(
                        manager=manager,
                        config=config,
                        folder_backend=folder_backend,
                        chat_id=chat_id,
                        chat_name=chat_name,
                        entity=entity,
                        folder_name=resolved_folder_name,
                        folder_id=effective_folder_id,
                    )
                    return resolved_id
                finally:
                    try:
                        await manager.disconnect()
                    except Exception:
                        pass

            try:
                resolved_chat_id = asyncio.run(_resolve_chat())
            except FolderError as exc:
                typer.echo(str(exc), err=True)
                raise typer.Exit(code=2) from exc
            except Exception as exc:
                _raise_for_access_or_entity_error(exc)
                typer.echo(f"members bulk-remove failed: {exc}", err=True)
                raise typer.Exit(code=1) from exc

        items_out: list[dict[str, object]] = []
        planned: list[str] = []
        warnings: list[str] = []
        for raw, n in normalized_inputs:
            is_protected = n.value in protected
            items_out.append(
                {
                    "user": raw,
                    "normalized_user": n.value,
                    "user_kind": n.kind,
                    "action": "would_remove",
                    "protected": is_protected,
                }
            )
            planned.append(
                f"would remove {n.value} from chat {resolved_chat_id} via {mode}"
            )
            if is_protected and not force:
                warnings.append(
                    f"{n.value} is a protected account; real run requires --force"
                )
        resolved_payload: dict[str, object] = {
            "telegram_chat_id": resolved_chat_id,
            "mode": mode,
            "items": items_out,
        }
        if chat_name is not None:
            resolved_payload["chat_name"] = chat_name
            resolved_payload["folder_name"] = resolved_folder_name
        payload: dict[str, object] = {
            "status": "dry_run",
            "dry_run": True,
            "command": "members.bulk_remove",
            "would": (
                f"remove {len(items_out)} user(s) from chat {resolved_chat_id} via {mode}"
            ),
            "resolved": resolved_payload,
            "planned_actions": planned,
            "warnings": warnings,
            "telegram_chat_id": resolved_chat_id,
            "mode": mode,
            "items": items_out,
        }
        typer.echo(json.dumps(payload, sort_keys=True, default=str))
        return

    queue = WorkerQueue(
        store,
        max_parallel=config.queue.max_parallel_telegram_ops,
        flood_wait_safety_margin_seconds=config.queue.flood_wait_safety_margin_seconds,
        default_retry_delay_seconds=config.queue.default_retry_delay_seconds,
    )

    async def _run() -> dict[str, object]:
        try:
            member_backend, folder_backend = await open_backends()
            resolved_chat_id, authorizer = await _cli_resolve_chat_and_authorizer(
                manager=manager,
                config=config,
                folder_backend=folder_backend,
                chat_id=chat_id,
                chat_name=chat_name,
                entity=entity,
                folder_name=resolved_folder_name,
                folder_id=effective_folder_id,
            )

            try:
                items = tuple(
                    BulkMemberRemoveItem(user=raw)
                    for raw, _ in normalized_inputs
                )
                req = BulkMemberRemoveRequest(
                    telegram_chat_id=resolved_chat_id,
                    items=items,
                    mode=mode,
                    continue_on_error=continue_on_error,
                    operation_id=operation_id,
                )
            except ValueError as exc:
                typer.echo(str(exc), err=True)
                raise typer.Exit(code=2) from exc
            result, op = await bulk_remove_members(
                backend=member_backend,
                store=store,
                queue=queue,
                request=req,
                authorizer=authorizer,
            )
            payload = result.to_dict()
            payload["operation_status"] = op.status.value
            return payload
        finally:
            try:
                await manager.disconnect()
            except Exception:
                pass

    try:
        payload = asyncio.run(_run())
    except (
        BulkMemberRemovePending,
        BulkMemberRemoveNeedsReview,
        BulkMemberRemoveFailed,
    ) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc
    except FolderError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc
    except typer.Exit:
        raise
    except Exception as exc:
        _raise_for_access_or_entity_error(exc)
        typer.echo(f"members bulk-remove failed: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    typer.echo(json.dumps(payload, sort_keys=True, default=str))


def _build_member_list_backends(config_path: Path | None):
    """Open the Telethon-backed participants-list + folder backends + resolver.

    Mirrors :func:`_build_message_read_backends`: a read op needs the read
    backend, the folder backend (for ``--chat-name`` and folder access rules)
    and a shared entity resolver so ``--entity`` works. Tests monkeypatch this
    to inject fakes.
    """
    config = _load_config_or_exit(config_path)
    manager = TelethonSessionManager(config.telegram)

    async def _open():
        from telegram_assistant.entities import TelethonEntityResolver
        from telegram_assistant.folders import TelethonFolderBackend
        from telegram_assistant.members.telethon_backend import (
            TelethonMemberListBackend,
        )

        client = await manager.get_client()
        if not await client.is_user_authorized():
            raise RuntimeError(
                "Telethon session is not authorized; run "
                "`telegram-assistant auth` first."
            )
        return (
            TelethonMemberListBackend(client),
            TelethonFolderBackend(client),
            TelethonEntityResolver(client),
        )

    return config, manager, _open


@members_app.command("list")
def members_list(
    chat_id: int | None = typer.Option(
        None,
        "--chat-id",
        help="Numeric Telegram chat id to read.",
    ),
    chat_name: str | None = typer.Option(
        None,
        "--chat-name",
        help="Chat title (resolved within --folder-name).",
    ),
    entity: str | None = typer.Option(
        None,
        "--entity",
        help="Flexible entity reference (numeric id, @username, t.me/invite link, "
        "phone, or exact title) resolved via the shared resolver.",
    ),
    folder_name: str | None = typer.Option(
        None,
        "--folder-name",
        help="Folder used for --chat-name lookup "
        "(defaults to telegram.default_chat_folder.folder_name).",
    ),
    folder_id: int | None = typer.Option(
        None,
        "--folder-id",
        help="Optional folder id cross-check.",
    ),
    limit: int = typer.Option(
        DEFAULT_MEMBER_LIST_LIMIT,
        "--limit",
        help="Maximum number of participants to return (default 200).",
    ),
    query: str | None = typer.Option(
        None,
        "--query",
        help="Substring match on username/first/last name (server-side for the "
        "default filter).",
    ),
    filter: str = typer.Option(
        "all",
        "--filter",
        help="Which participants to list: all|admins|bots.",
    ),
    user: str | None = typer.Option(
        None,
        "--user",
        help="Check one user's membership with a single request instead of "
        "listing (mutually exclusive with --query).",
    ),
    config_path: Path | None = typer.Option(  # noqa: B008
        None,
        "--config",
        "-c",
        help="Path to config.yml (defaults: ./data/config.yml, then ~/.config/telegram-assistant/config.yml).",
        exists=False,
    ),
) -> None:
    """List a chat's participants, or check one user's membership (READ-gated)."""
    from telegram_assistant.folders import (
        FolderError,
        resolve_chat_in_folder,
    )
    from telegram_assistant.members import list_members

    refs = sum([chat_id is not None, chat_name is not None, entity is not None])
    if refs != 1:
        typer.echo(
            "exactly one of --chat-id, --chat-name, or --entity must be supplied",
            err=True,
        )
        raise typer.Exit(code=2)

    config, manager, open_backends = _build_member_list_backends(config_path)

    if chat_name is not None:
        resolved_folder_name, default_fid, _ = _resolve_folder_name(
            folder_name, config_path
        )
        effective_folder_id = folder_id if folder_id is not None else default_fid
    else:
        resolved_folder_name = folder_name
        effective_folder_id = folder_id

    async def _run() -> dict[str, object]:
        try:
            list_backend, folder_backend, resolver = await open_backends()
            if entity is not None:
                resolved_chat_id = (await resolver.resolve(entity)).chat_id
            elif chat_id is not None:
                resolved_chat_id = chat_id
            else:
                resolved = await resolve_chat_in_folder(
                    folder_backend,
                    folder_name=resolved_folder_name or "",
                    chat_name=chat_name or "",
                    folder_id=effective_folder_id,
                )
                resolved_chat_id = resolved.chat_id

            authorizer = _cli_authorizer(
                config, resolver=resolver, folder_backend=folder_backend
            )
            result = await list_members(
                backend=list_backend,
                chat_id=resolved_chat_id,
                limit=limit,
                query=query,
                filter=filter,
                user=user,
                authorizer=authorizer,
            )
            return {
                "telegram_chat_id": resolved_chat_id,
                "limit": limit,
                "query": query,
                "filter": filter,
                **result.to_dict(),
            }
        finally:
            try:
                await manager.disconnect()
            except Exception:
                pass

    try:
        payload = asyncio.run(_run())
    except FolderError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc
    except ValueError as exc:
        # Bad caller input (limit, filter, user+query) — exit 2 like the rest of
        # the domain rejections. AccessDenied/EntityError are RuntimeErrors, so
        # they fall through to the access/entity mapping below.
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc
    except Exception as exc:
        _raise_for_access_or_entity_error(exc)
        typer.echo(f"members list failed: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    typer.echo(json.dumps(payload, sort_keys=True, default=str))


# --- messages ---------------------------------------------------------------

messages_app = typer.Typer(help="Send messages and service commands.", no_args_is_help=True)
app.add_typer(messages_app, name="messages")


def _build_message_backends(config_path: Path | None):
    """Open the Telethon-backed message + topic + folder backends + store.

    ``_open`` returns ``(message_backend, topic_backend, folder_backend)``. The
    dedicated :class:`TelethonMessageBackend` handles text/media/scheduled sends
    while the topic backend still serves topic-name resolution and mass send;
    the folder backend resolves chats and folders.
    """
    config = _load_config_or_exit(config_path)
    manager = TelethonSessionManager(config.telegram)
    store = OperationStore(default_database_path(config))

    async def _open():
        from telegram_assistant.folders import TelethonFolderBackend
        from telegram_assistant.messages.telethon_backend import (
            TelethonMessageBackend,
        )
        from telegram_assistant.topics.telethon_backend import (
            TelethonTopicBackend,
        )

        client = await manager.get_client()
        if not await client.is_user_authorized():
            raise RuntimeError(
                "Telethon session is not authorized; run "
                "`telegram-assistant auth` first."
            )
        message_backend = TelethonMessageBackend(client)
        topic_backend = TelethonTopicBackend(client)
        return message_backend, topic_backend, TelethonFolderBackend(client)

    return config, manager, store, _open


@messages_app.command("send")
def messages_send(
    text: str = typer.Option("", "--text", help="Message text or service command."),
    chat_id: int | None = typer.Option(
        None,
        "--chat-id",
        help="Numeric Telegram chat id (targeted send).",
    ),
    chat_name: str | None = typer.Option(
        None,
        "--chat-name",
        help="Chat title (resolved within --folder-name).",
    ),
    entity: str | None = typer.Option(
        None,
        "--entity",
        help="Flexible entity reference (numeric id, @username, t.me/invite link, "
        "phone, or exact title) resolved via the shared resolver (targeted send).",
    ),
    folder_name: str | None = typer.Option(
        None,
        "--folder-name",
        help="Folder used for chat resolution or mass mode "
        "(defaults to telegram.default_chat_folder.folder_name).",
    ),
    folder_id: int | None = typer.Option(
        None,
        "--folder-id",
        help="Optional folder id cross-check.",
    ),
    topic_id: int | None = typer.Option(
        None,
        "--topic-id",
        help="Numeric forum topic id (targeted send into a topic).",
    ),
    topic_name: str | None = typer.Option(
        None,
        "--topic-name",
        help="Topic title; resolves to --topic-id in targeted mode, "
        "or triggers mass send when no chat is given.",
    ),
    mass: bool = typer.Option(
        False,
        "--mass",
        help="Force mass mode: send to every chat in --folder-name that has --topic-name.",
    ),
    operation_id: str | None = typer.Option(
        None,
        "--operation-id",
        help="Idempotency anchor; reuse to replay the saved result instead of re-sending.",
    ),
    file: list[str] = typer.Option(  # noqa: B008
        None,
        "--file",
        help="Local server-side file to attach (repeatable; many files send an album).",
    ),
    file_url: list[str] = typer.Option(  # noqa: B008
        None,
        "--file-url",
        help="http(s) URL to attach, handed to Telegram as-is (repeatable).",
    ),
    schedule_at: str | None = typer.Option(
        None,
        "--schedule-at",
        help="Defer delivery to an ISO-8601 datetime (mutually exclusive with --delay).",
    ),
    delay: str | None = typer.Option(
        None,
        "--delay",
        help="Defer delivery by a relative duration like 10m, 2h, 1d "
        "(mutually exclusive with --schedule-at).",
    ),
    reply_to: int | None = typer.Option(
        None,
        "--reply-to",
        help="Thread the send as a reply to an existing message id (targeted send).",
    ),
    rich_markdown: Path | None = typer.Option(  # noqa: B008
        None,
        "--rich-markdown",
        help="Path to a UTF-8 markdown file sent as a Telegram rich message "
        "(article: headings, tables, quotes, code, media by https URL). "
        "Targeted sends only; mutually exclusive with --text/--file/--file-url.",
        exists=False,
    ),
    spaced_paragraphs: bool | None = typer.Option(
        None,
        "--spaced-paragraphs/--no-spaced-paragraphs",
        help="Insert a U+00A0 spacer paragraph between paragraphs and before "
        "headings so the article is not rendered as a wall of text "
        "(default: on, or telegram.defaults.rich_markdown_spaced_paragraphs). "
        "Only meaningful with --rich-markdown.",
    ),
    line_breaks: bool | None = typer.Option(
        None,
        "--line-breaks/--no-line-breaks",
        help="Split each line of a paragraph into its own paragraph, so the "
        "single newlines the article's author wrote survive instead of being "
        "folded into spaces by Telegram's markdown parser "
        "(default: on, or telegram.defaults.rich_markdown_line_breaks). "
        "Only meaningful with --rich-markdown.",
    ),
    media_group: list[str] = typer.Option(  # noqa: B008
        None,
        "--media-group",
        help="Override how one run of consecutive media is grouped, as "
        "<index>=<collage|slideshow|none> (repeatable). The index is the "
        "0-based position reported as rich_markdown_groups by --dry-run; the "
        "default comes from telegram.defaults.rich_markdown_grouping. "
        "Only valid with --rich-markdown.",
    ),
    rich_file: list[str] = typer.Option(  # noqa: B008
        None,
        "--rich-file",
        help="Map a media reference in the article to a local file, as "
        "<reference>=<path> (repeatable). The reference is the target written "
        "in the markdown, its URL-decoded form, or its bare file name; use it "
        "for files outside the article's directory. Only valid with "
        "--rich-markdown.",
    ),
    vault_dir: Path | None = typer.Option(  # noqa: B008
        None,
        "--vault-dir",
        help="Directory tree searched by file name for article media that is "
        "not next to the article — Obsidian ![[embed.png]] embeds and plain "
        "![](photo.png) targets alike. Only valid with --rich-markdown.",
        exists=False,
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Validate the request and report the plan without sending any message.",
    ),
    config_path: Path | None = typer.Option(  # noqa: B008
        None,
        "--config",
        "-c",
        help="Path to config.yml (defaults: ./data/config.yml, then ~/.config/telegram-assistant/config.yml).",
        exists=False,
    ),
) -> None:
    """Send a message or service command (targeted or folder-wide mass mode)."""
    from telegram_assistant.cli.rich_send import (
        resolve_rich_send_input,
        rich_dry_run_markers,
    )
    from telegram_assistant.folders import (
        FolderError,
        resolve_chat_in_folder,
        resolve_folder,
    )
    from telegram_assistant.messages import (
        AttachmentError,
        MassSendRequest,
        MessageSendFailed,
        MessageSendNeedsReview,
        MessageSendPending,
        ScheduleError,
        SendMessageRequest,
        is_service_command,
        line_breaks_default,
        make_url_downloader,
        mass_send_message,
        media_grouping_default,
        parse_delay,
        parse_schedule_at,
        redact_message_text,
        resolve_schedule_at,
        send_message,
        spaced_paragraphs_default,
        validate_file_urls,
        validate_local_files,
    )
    from telegram_assistant.topics import (
        AmbiguousTopicNameError,
        TopicNotFoundError,
        resolve_topic_id_by_name,
    )

    if mass and (chat_id is not None or chat_name is not None or entity is not None):
        typer.echo(
            "--mass cannot be combined with --chat-id, --chat-name, or --entity; "
            "mass mode sends to every chat in --folder-name that has --topic-name.",
            err=True,
        )
        raise typer.Exit(code=2)
    targeted_refs = sum(
        [chat_id is not None, chat_name is not None, entity is not None]
    )
    is_mass = mass or targeted_refs == 0
    if is_mass and topic_name is None:
        typer.echo(
            "mass mode requires --topic-name (and --folder-name resolves the folder)",
            err=True,
        )
        raise typer.Exit(code=2)
    if not is_mass and targeted_refs != 1:
        typer.echo(
            "targeted send requires exactly one of --chat-id, --chat-name, or --entity",
            err=True,
        )
        raise typer.Exit(code=2)

    files = tuple(file or ())
    file_urls = tuple(file_url or ())
    has_attachments = bool(files or file_urls)
    has_schedule_input = schedule_at is not None or delay is not None

    # Everything --rich-markdown needs — flag exclusivity, the file read, the
    # --rich-file/--media-group parsing, local-media resolution and the length
    # bound — lives in cli.rich_send; ``None`` means this is a plain send. It
    # runs before any backend is opened, so bad input costs no connection in
    # dry-run and real runs alike.
    rich_input = resolve_rich_send_input(
        rich_markdown=rich_markdown,
        rich_file=rich_file,
        media_group=media_group,
        vault_dir=vault_dir,
        spaced_paragraphs=spaced_paragraphs,
        line_breaks=line_breaks,
        text=text,
        has_attachments=has_attachments,
        is_mass=is_mass,
    )

    # Media, scheduling, and reply threading are targeted-only; mass mode
    # iterates many chats and has no single attachment/schedule/reply semantics.
    if is_mass and (has_attachments or has_schedule_input or reply_to is not None):
        typer.echo(
            "--file/--file-url/--schedule-at/--delay/--reply-to are only supported "
            "for targeted sends, not mass mode",
            err=True,
        )
        raise typer.Exit(code=2)
    if reply_to is not None and reply_to <= 0:
        typer.echo("--reply-to must be a positive integer", err=True)
        raise typer.Exit(code=2)

    # Resolve scheduling (rejects conflicting modes and past absolute times) and
    # validate attachments before opening any backend, so bad input fails fast
    # with exit code 2 in both dry-run and real runs.
    try:
        parsed_schedule_at = (
            parse_schedule_at(schedule_at) if schedule_at is not None else None
        )
        delay_seconds = parse_delay(delay) if delay is not None else None
        resolved_schedule_at = resolve_schedule_at(
            schedule_at=parsed_schedule_at,
            delay_seconds=delay_seconds,
        )
    except ScheduleError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc

    try:
        validate_local_files(files)
        validate_file_urls(file_urls)
    except AttachmentError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc

    config, manager, store, open_backends = _build_message_backends(config_path)

    # An explicit flag wins over telegram.defaults.rich_markdown_spaced_paragraphs,
    # which in turn wins over the built-in default (on).
    effective_spaced_paragraphs = (
        spaced_paragraphs
        if spaced_paragraphs is not None
        else spaced_paragraphs_default(config)
    )
    # Same rule for the line-splitting pass: an explicit flag wins over
    # telegram.defaults.rich_markdown_line_breaks.
    effective_line_breaks = (
        line_breaks if line_breaks is not None else line_breaks_default(config)
    )
    # Grouping has no all-or-nothing flag: the config sets the default mode and
    # --media-group overrides individual runs.
    effective_media_grouping = media_grouping_default(config)

    # Mass mode and chat_name resolution both need the folder default.
    if is_mass or chat_name is not None:
        resolved_folder_name, default_fid, _ = _resolve_folder_name(
            folder_name, config_path
        )
        effective_folder_id = folder_id if folder_id is not None else default_fid
    else:
        resolved_folder_name = folder_name
        effective_folder_id = folder_id

    if dry_run:
        if not (text and text.strip()) and not has_attachments and rich_input is None:
            typer.echo(
                "messages send requires non-empty --text, --rich-markdown, or at "
                "least one --file/--file-url attachment",
                err=True,
            )
            raise typer.Exit(code=2)
        if is_mass and not (topic_name or "").strip():
            typer.echo(
                "mass mode requires --topic-name (and --folder-name resolves the folder)",
                err=True,
            )
            raise typer.Exit(code=2)

        service = is_service_command(text)
        display_text = redact_message_text(text) if service else text

        async def _resolve_send() -> dict[str, object]:
            try:
                _msg_backend, topic_backend, folder_backend = await open_backends()
                if is_mass:
                    snapshot = await resolve_folder(
                        folder_backend,
                        folder_name=resolved_folder_name or "",
                        folder_id=effective_folder_id,
                    )
                    chat_rows: list[dict[str, object]] = []
                    planned_local: list[str] = []
                    local_warnings: list[str] = []
                    for chat in snapshot.chats:
                        try:
                            topics = list(
                                await topic_backend.list_topics(chat_id=chat.chat_id)
                            )
                        except Exception as exc:
                            chat_rows.append(
                                {
                                    "telegram_chat_id": chat.chat_id,
                                    "chat_name": chat.title,
                                    "topic_name": topic_name,
                                    "telegram_topic_id": None,
                                    "action": "would_skip",
                                    "reason": f"list_topics_failed: {exc}",
                                }
                            )
                            local_warnings.append(
                                f"chat {chat.chat_id}: list_topics failed ({exc}); "
                                "real run would mark this chat failed"
                            )
                            continue
                        matches = [t for t in topics if t.title == topic_name]
                        if len(matches) == 0:
                            chat_rows.append(
                                {
                                    "telegram_chat_id": chat.chat_id,
                                    "chat_name": chat.title,
                                    "topic_name": topic_name,
                                    "telegram_topic_id": None,
                                    "action": "would_skip",
                                    "reason": "topic_not_found",
                                }
                            )
                            continue
                        if len(matches) > 1:
                            chat_rows.append(
                                {
                                    "telegram_chat_id": chat.chat_id,
                                    "chat_name": chat.title,
                                    "topic_name": topic_name,
                                    "telegram_topic_id": None,
                                    "action": "would_skip",
                                    "reason": "topic_ambiguous",
                                }
                            )
                            local_warnings.append(
                                f"chat {chat.chat_id} ({chat.title!r}): "
                                f"topic name {topic_name!r} matches "
                                f"{len(matches)} topics; rename one to disambiguate"
                            )
                            continue
                        match = matches[0]
                        chat_rows.append(
                            {
                                "telegram_chat_id": chat.chat_id,
                                "chat_name": chat.title,
                                "topic_name": topic_name,
                                "telegram_topic_id": match.topic_id,
                                "action": "would_send",
                                "reason": None,
                            }
                        )
                        planned_local.append(
                            f"would send to chat {chat.chat_id} ({chat.title!r}) "
                            f"topic {match.topic_id} ({topic_name!r})"
                        )
                    return {
                        "mode": "mass",
                        "items": chat_rows,
                        "planned": planned_local,
                        "warnings": local_warnings,
                        "folder_id": snapshot.folder_id,
                    }
                # Targeted send.
                if entity is not None:
                    from telegram_assistant.entities import (
                        TelethonEntityResolver,
                    )

                    resolved_entity = await TelethonEntityResolver(
                        await manager.get_client()
                    ).resolve(entity)
                    rch_id = resolved_entity.chat_id
                    rch_name: str | None = resolved_entity.title
                elif chat_id is not None:
                    rch_id = chat_id
                    rch_name = None
                else:
                    resolved = await resolve_chat_in_folder(
                        folder_backend,
                        folder_name=resolved_folder_name or "",
                        chat_name=chat_name or "",
                        folder_id=effective_folder_id,
                    )
                    rch_id = resolved.chat_id
                    rch_name = resolved.title

                rtopic_id = topic_id
                if rtopic_id is None and topic_name is not None:
                    rtopic_id = await resolve_topic_id_by_name(
                        backend=topic_backend,
                        telegram_chat_id=rch_id,
                        topic_name=topic_name,
                    )
                return {
                    "mode": "targeted",
                    "telegram_chat_id": rch_id,
                    "chat_name": rch_name,
                    "telegram_topic_id": rtopic_id,
                }
            finally:
                try:
                    await manager.disconnect()
                except Exception:
                    pass

        try:
            info = asyncio.run(_resolve_send())
        except (AmbiguousTopicNameError, TopicNotFoundError) as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(code=2) from exc
        except FolderError as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(code=2) from exc
        except Exception as exc:
            _raise_for_access_or_entity_error(exc)
            typer.echo(f"messages send failed: {exc}", err=True)
            raise typer.Exit(code=1) from exc

        if info["mode"] == "mass":
            items_out = info["items"]
            warnings = list(info["warnings"])
            planned = list(info["planned"])
            send_count = sum(
                1 for it in items_out if it["action"] == "would_send"
            )
            skip_count = sum(
                1 for it in items_out if it["action"] == "would_skip"
            )
            resolved_payload: dict[str, object] = {
                "mode": "mass",
                "folder_name": resolved_folder_name,
                "folder_id": info["folder_id"],
                "topic_name": topic_name,
                "text": display_text,
                "is_service_command": service,
                "items": items_out,
                "items_count": len(items_out),
                "send_count": send_count,
                "skip_count": skip_count,
                "operation_id": operation_id,
            }
            payload = {
                "status": "dry_run",
                "dry_run": True,
                "command": "messages.send",
                "would": (
                    f"send to {send_count} chat(s) in folder "
                    f"{resolved_folder_name!r} (topic {topic_name!r}); "
                    f"{skip_count} would be skipped"
                ),
                "resolved": resolved_payload,
                "planned_actions": planned,
                "warnings": warnings,
            }
        else:
            rch_id = info["telegram_chat_id"]
            rch_name = info.get("chat_name")
            rtopic_id = info["telegram_topic_id"]
            rich_report = rich_dry_run_markers(
                rich_input,
                rich_markdown=rich_markdown,
                spaced_paragraphs=effective_spaced_paragraphs,
                line_breaks=effective_line_breaks,
                media_grouping=effective_media_grouping,
            )
            resolved_payload = {
                "mode": "targeted",
                "telegram_chat_id": rch_id,
                "chat_name": rch_name,
                "telegram_topic_id": rtopic_id,
                "topic_name": topic_name,
                "text": display_text,
                "is_service_command": service,
                "operation_id": operation_id,
                "files": list(files),
                "file_urls": list(file_urls),
                "schedule_at": (
                    resolved_schedule_at.isoformat()
                    if resolved_schedule_at is not None
                    else None
                ),
                "scheduled": resolved_schedule_at is not None,
                "reply_to_message_id": reply_to,
                **rich_report.markers,
            }
            if chat_name is not None:
                resolved_payload["folder_name"] = resolved_folder_name
            target = (
                f"chat {rch_id} topic {rtopic_id}"
                if rtopic_id is not None
                else f"chat {rch_id}"
            )
            what = rich_report.what
            payload = {
                "status": "dry_run",
                "dry_run": True,
                "command": "messages.send",
                "would": f"send {what} to {target}",
                "resolved": resolved_payload,
                "planned_actions": [f"would send {what} to {target}"],
                "warnings": rich_report.warnings,
            }
        typer.echo(json.dumps(payload, sort_keys=True, default=str))
        return

    async def _run() -> dict[str, object]:
        try:
            message_backend, topic_backend, folder_backend = await open_backends()

            # Build a resolver only when the policy or --entity actually needs
            # one, so the common allow-all/no-entity path never touches the
            # Telethon client beyond what backend resolution already did.
            resolver = None
            if config.telegram.access is not None or entity is not None:
                from telegram_assistant.entities import TelethonEntityResolver

                resolver = TelethonEntityResolver(await manager.get_client())
            authorizer = _cli_authorizer(
                config, resolver=resolver, folder_backend=folder_backend
            )

            if is_mass:
                req = MassSendRequest(
                    folder_name=resolved_folder_name or "",
                    topic_name=topic_name or "",
                    text=text,
                    folder_id=effective_folder_id,
                    operation_id=operation_id,
                )
                result = await mass_send_message(
                    message_backend=message_backend,
                    topic_backend=topic_backend,
                    folder_backend=folder_backend,
                    store=store,
                    request=req,
                    authorizer=authorizer,
                )
                payload = result.to_dict()
                payload["mode"] = "mass"
                return payload

            # Targeted send.
            if entity is not None:
                assert resolver is not None  # built above when entity is set
                resolved_entity = await resolver.resolve(entity)
                resolved_chat_id = resolved_entity.chat_id
                resolved_chat_name: str | None = resolved_entity.title
            elif chat_id is not None:
                resolved_chat_id = chat_id
                resolved_chat_name = None
            else:
                resolved = await resolve_chat_in_folder(
                    folder_backend,
                    folder_name=resolved_folder_name or "",
                    chat_name=chat_name or "",
                    folder_id=effective_folder_id,
                )
                resolved_chat_id = resolved.chat_id
                resolved_chat_name = resolved.title

            resolved_topic_id = topic_id
            resolved_topic_name: str | None = None
            if resolved_topic_id is None and topic_name is not None:
                resolved_topic_id = await resolve_topic_id_by_name(
                    backend=topic_backend,
                    telegram_chat_id=resolved_chat_id,
                    topic_name=topic_name,
                )
                resolved_topic_name = topic_name

            req_single = SendMessageRequest(
                telegram_chat_id=resolved_chat_id,
                text=text,
                telegram_topic_id=resolved_topic_id,
                operation_id=operation_id,
                chat_name=resolved_chat_name,
                topic_name=resolved_topic_name,
                files=files,
                file_urls=file_urls,
                schedule_at=resolved_schedule_at,
                reply_to_message_id=reply_to,
                rich_markdown=rich_input.markdown if rich_input is not None else None,
                spaced_paragraphs=effective_spaced_paragraphs,
                line_breaks=effective_line_breaks,
                media_grouping=effective_media_grouping,
                media_groups=rich_input.choices if rich_input is not None else (),
                rich_files=rich_input.files if rich_input is not None else (),
            )
            result_single, op = await send_message(
                backend=message_backend,
                store=store,
                request=req_single,
                authorizer=authorizer,
                downloader=make_url_downloader() if file_urls else None,
            )
            payload = result_single.to_dict()
            payload["operation_id"] = op.id
            payload["operation_status"] = op.status.value
            payload["mode"] = "targeted"
            return payload
        finally:
            try:
                await manager.disconnect()
            except Exception:
                pass

    try:
        payload = asyncio.run(_run())
    except (
        MessageSendPending,
        MessageSendNeedsReview,
        MessageSendFailed,
    ) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc
    except (AmbiguousTopicNameError, TopicNotFoundError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc
    except FolderError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc
    except ValueError as exc:
        # Bad caller input, not an unexpected failure — exit 2 like every other
        # `messages` command and like the rich pre-checks above. Covers
        # ``MediaGroupError``, the media-resolution errors and the over-limit
        # ``ValueError`` normalization raises, plus ``RichMediaForbidden``,
        # whose contract (and README/SKILL) promise HTTP 400 / exit 2.
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc
    except Exception as exc:
        _raise_for_access_or_entity_error(exc)
        typer.echo(f"messages send failed: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    # Non-fatal notes (block/media budget, a rolled-back spacer pass) ride the
    # result payload; echo them to stderr too so a human sees them without
    # having to read the JSON.
    for warning in payload.get("warnings") or ():
        typer.echo(f"warning: {warning}", err=True)
    typer.echo(json.dumps(payload, sort_keys=True, default=str))


def _build_message_read_backends(config_path: Path | None):
    """Open the Telethon-backed read + folder backends + resolver for reads.

    Mirrors :func:`_build_message_backends` but returns the read backend used
    by the get-recent op plus a shared entity resolver so ``--entity`` works.
    Tests monkeypatch this to inject fakes.
    """
    config = _load_config_or_exit(config_path)
    manager = TelethonSessionManager(config.telegram)

    async def _open():
        from telegram_assistant.entities import TelethonEntityResolver
        from telegram_assistant.folders import TelethonFolderBackend
        from telegram_assistant.messages.telethon_backend import (
            TelethonMessageReadBackend,
        )

        client = await manager.get_client()
        if not await client.is_user_authorized():
            raise RuntimeError(
                "Telethon session is not authorized; run "
                "`telegram-assistant auth` first."
            )
        return (
            TelethonMessageReadBackend(client),
            TelethonFolderBackend(client),
            TelethonEntityResolver(client),
        )

    return config, manager, _open


@messages_app.command("recent")
def messages_recent(
    chat_id: int | None = typer.Option(
        None,
        "--chat-id",
        help="Numeric Telegram chat id to read.",
    ),
    chat_name: str | None = typer.Option(
        None,
        "--chat-name",
        help="Chat title (resolved within --folder-name).",
    ),
    entity: str | None = typer.Option(
        None,
        "--entity",
        help="Flexible entity reference (numeric id, @username, t.me/invite link, "
        "phone, or exact title) resolved via the shared resolver.",
    ),
    folder_name: str | None = typer.Option(
        None,
        "--folder-name",
        help="Folder used for --chat-name lookup "
        "(defaults to telegram.default_chat_folder.folder_name).",
    ),
    folder_id: int | None = typer.Option(
        None,
        "--folder-id",
        help="Optional folder id cross-check.",
    ),
    limit: int = typer.Option(
        5,
        "--limit",
        help="Maximum number of recent messages to return (default 5).",
    ),
    minutes: int | None = typer.Option(
        None,
        "--minutes",
        help="Only return messages newer than now - MINUTES (composed with --limit).",
    ),
    config_path: Path | None = typer.Option(  # noqa: B008
        None,
        "--config",
        "-c",
        help="Path to config.yml (defaults: ./data/config.yml, then ~/.config/telegram-assistant/config.yml).",
        exists=False,
    ),
) -> None:
    """Read the most recent messages from a chat (READ-gated)."""
    from telegram_assistant.folders import (
        FolderError,
        resolve_chat_in_folder,
    )
    from telegram_assistant.messages import get_recent_messages

    refs = sum([chat_id is not None, chat_name is not None, entity is not None])
    if refs != 1:
        typer.echo(
            "exactly one of --chat-id, --chat-name, or --entity must be supplied",
            err=True,
        )
        raise typer.Exit(code=2)
    if limit <= 0:
        typer.echo("--limit must be a positive integer", err=True)
        raise typer.Exit(code=2)
    if minutes is not None and minutes <= 0:
        typer.echo("--minutes must be a positive integer", err=True)
        raise typer.Exit(code=2)

    config, manager, open_backends = _build_message_read_backends(config_path)

    if chat_name is not None:
        resolved_folder_name, default_fid, _ = _resolve_folder_name(
            folder_name, config_path
        )
        effective_folder_id = folder_id if folder_id is not None else default_fid
    else:
        resolved_folder_name = folder_name
        effective_folder_id = folder_id

    async def _run() -> dict[str, object]:
        try:
            read_backend, folder_backend, resolver = await open_backends()
            if entity is not None:
                resolved_chat_id = (await resolver.resolve(entity)).chat_id
            elif chat_id is not None:
                resolved_chat_id = chat_id
            else:
                resolved = await resolve_chat_in_folder(
                    folder_backend,
                    folder_name=resolved_folder_name or "",
                    chat_name=chat_name or "",
                    folder_id=effective_folder_id,
                )
                resolved_chat_id = resolved.chat_id

            authorizer = _cli_authorizer(
                config, resolver=resolver, folder_backend=folder_backend
            )
            messages = await get_recent_messages(
                backend=read_backend,
                chat_id=resolved_chat_id,
                limit=limit,
                minutes=minutes,
                authorizer=authorizer,
            )
            return {
                "telegram_chat_id": resolved_chat_id,
                "limit": limit,
                "minutes": minutes,
                "count": len(messages),
                "messages": [m.to_dict() for m in messages],
            }
        finally:
            try:
                await manager.disconnect()
            except Exception:
                pass

    try:
        payload = asyncio.run(_run())
    except FolderError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc
    except Exception as exc:
        _raise_for_access_or_entity_error(exc)
        typer.echo(f"messages recent failed: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    typer.echo(json.dumps(payload, sort_keys=True, default=str))


def _build_search_backends(config_path: Path | None):
    """Open the Telethon-backed search + folder backends + resolver for searches.

    Mirrors :func:`_build_message_read_backends` but returns the search backend
    used by the search op plus a shared entity resolver so ``--entity`` works.
    Tests monkeypatch this to inject fakes.
    """
    config = _load_config_or_exit(config_path)
    manager = TelethonSessionManager(config.telegram)

    async def _open():
        from telegram_assistant.entities import TelethonEntityResolver
        from telegram_assistant.folders import TelethonFolderBackend
        from telegram_assistant.messages.telethon_backend import (
            TelethonSearchBackend,
        )

        client = await manager.get_client()
        if not await client.is_user_authorized():
            raise RuntimeError(
                "Telethon session is not authorized; run "
                "`telegram-assistant auth` first."
            )
        return (
            TelethonSearchBackend(client),
            TelethonFolderBackend(client),
            TelethonEntityResolver(client),
        )

    return config, manager, _open


@messages_app.command("search")
def messages_search(
    query: str = typer.Option(
        ...,
        "--query",
        help="Text to search for inside the chat (server-side search).",
    ),
    chat_id: int | None = typer.Option(
        None,
        "--chat-id",
        help="Numeric Telegram chat id to search.",
    ),
    chat_name: str | None = typer.Option(
        None,
        "--chat-name",
        help="Chat title (resolved within --folder-name).",
    ),
    entity: str | None = typer.Option(
        None,
        "--entity",
        help="Flexible entity reference (numeric id, @username, t.me/invite link, "
        "phone, or exact title) resolved via the shared resolver.",
    ),
    from_user: str | None = typer.Option(
        None,
        "--from",
        help="Optional sender reference to narrow the search to one user.",
    ),
    folder_name: str | None = typer.Option(
        None,
        "--folder-name",
        help="Folder used for --chat-name lookup "
        "(defaults to telegram.default_chat_folder.folder_name).",
    ),
    folder_id: int | None = typer.Option(
        None,
        "--folder-id",
        help="Optional folder id cross-check.",
    ),
    limit: int = typer.Option(
        20,
        "--limit",
        help="Maximum number of matches to return (default 20).",
    ),
    minutes: int | None = typer.Option(
        None,
        "--minutes",
        help="Only return matches newer than now - MINUTES (composed with --limit).",
    ),
    topic_id: int | None = typer.Option(
        None,
        "--topic-id",
        help="Scope the search to one forum topic id.",
    ),
    from_date: str | None = typer.Option(
        None,
        "--from-date",
        help="Start of a fixed, inclusive date range (ISO-8601 with timezone, "
        "e.g. 2026-07-01T00:00:00+03:00). Requires --to-date; not combinable "
        "with --minutes.",
    ),
    to_date: str | None = typer.Option(
        None,
        "--to-date",
        help="End of the fixed, inclusive date range (ISO-8601 with timezone). "
        "Requires --from-date.",
    ),
    config_path: Path | None = typer.Option(  # noqa: B008
        None,
        "--config",
        "-c",
        help="Path to config.yml (defaults: ./data/config.yml, then ~/.config/telegram-assistant/config.yml).",
        exists=False,
    ),
) -> None:
    """Text-search a chat's messages, newest-first (READ-gated)."""
    from telegram_assistant.folders import (
        FolderError,
        resolve_chat_in_folder,
    )
    from telegram_assistant.messages import normalize_search_range, search_messages

    refs = sum([chat_id is not None, chat_name is not None, entity is not None])
    if refs != 1:
        typer.echo(
            "exactly one of --chat-id, --chat-name, or --entity must be supplied",
            err=True,
        )
        raise typer.Exit(code=2)
    if not query or not query.strip():
        typer.echo("--query must be a non-empty string", err=True)
        raise typer.Exit(code=2)
    if limit <= 0:
        typer.echo("--limit must be a positive integer", err=True)
        raise typer.Exit(code=2)
    if minutes is not None and minutes <= 0:
        typer.echo("--minutes must be a positive integer", err=True)
        raise typer.Exit(code=2)

    def _parse_iso(option: str, raw: str | None) -> datetime | None:
        if raw is None:
            return None
        try:
            return datetime.fromisoformat(raw)
        except ValueError:
            typer.echo(
                f"{option} must be an ISO-8601 timestamp with timezone (got {raw!r})",
                err=True,
            )
            raise typer.Exit(code=2) from None

    parsed_from = _parse_iso("--from-date", from_date)
    parsed_to = _parse_iso("--to-date", to_date)
    # Shared domain validation (both bounds, aware, ordered, not with --minutes)
    # runs here too so the CLI fails fast with exit code 2 and the same message
    # the other surfaces use — and so the applied UTC bounds can be echoed.
    try:
        applied_range = normalize_search_range(
            from_date=parsed_from, to_date=parsed_to, minutes=minutes
        )
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc

    config, manager, open_backends = _build_search_backends(config_path)

    if chat_name is not None:
        resolved_folder_name, default_fid, _ = _resolve_folder_name(
            folder_name, config_path
        )
        effective_folder_id = folder_id if folder_id is not None else default_fid
    else:
        resolved_folder_name = folder_name
        effective_folder_id = folder_id

    async def _run() -> dict[str, object]:
        try:
            search_backend, folder_backend, resolver = await open_backends()
            if entity is not None:
                resolved_chat_id = (await resolver.resolve(entity)).chat_id
            elif chat_id is not None:
                resolved_chat_id = chat_id
            else:
                resolved = await resolve_chat_in_folder(
                    folder_backend,
                    folder_name=resolved_folder_name or "",
                    chat_name=chat_name or "",
                    folder_id=effective_folder_id,
                )
                resolved_chat_id = resolved.chat_id

            authorizer = _cli_authorizer(
                config, resolver=resolver, folder_backend=folder_backend
            )
            messages = await search_messages(
                backend=search_backend,
                chat_id=resolved_chat_id,
                query=query,
                from_user=from_user,
                limit=limit,
                minutes=minutes,
                topic_id=topic_id,
                from_date=parsed_from,
                to_date=parsed_to,
                authorizer=authorizer,
            )
            return {
                "telegram_chat_id": resolved_chat_id,
                "query": query,
                "from_user": from_user,
                "limit": limit,
                "minutes": minutes,
                "topic_id": topic_id,
                "from_date": (
                    applied_range[0].isoformat() if applied_range is not None else None
                ),
                "to_date": (
                    applied_range[1].isoformat() if applied_range is not None else None
                ),
                "count": len(messages),
                "messages": [m.to_dict() for m in messages],
            }
        finally:
            try:
                await manager.disconnect()
            except Exception:
                pass

    try:
        payload = asyncio.run(_run())
    except FolderError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc
    except ValueError as exc:
        # Bad input (an unresolvable `--from`, a rejected range re-validated in
        # the domain) exits 2 like the other message commands, not 1.
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc
    except Exception as exc:
        _raise_for_access_or_entity_error(exc)
        typer.echo(f"messages search failed: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    typer.echo(json.dumps(payload, sort_keys=True, default=str))


def _build_reaction_backends(config_path: Path | None):
    """Open the Telethon-backed reaction + folder backends.

    ``_open`` returns ``(reaction_backend, folder_backend)``. The folder backend
    resolves ``--chat-name`` and feeds the authorizer's folder rules. Tests
    monkeypatch this to inject fakes.
    """
    config = _load_config_or_exit(config_path)
    manager = TelethonSessionManager(config.telegram)

    async def _open():
        from telegram_assistant.folders import TelethonFolderBackend
        from telegram_assistant.messages.telethon_backend import (
            TelethonReactionBackend,
        )

        client = await manager.get_client()
        if not await client.is_user_authorized():
            raise RuntimeError(
                "Telethon session is not authorized; run "
                "`telegram-assistant auth` first."
            )
        return (
            TelethonReactionBackend(client),
            TelethonFolderBackend(client),
        )

    return config, manager, _open


@messages_app.command("react")
def messages_react(
    message_id: int = typer.Option(
        ..., "--message-id", help="Numeric id of the message to react to."
    ),
    emoji: str | None = typer.Option(
        None, "--emoji", help="Reaction emoji to set (mutually exclusive with --clear)."
    ),
    clear: bool = typer.Option(
        False,
        "--clear",
        help="Remove the existing reaction instead of setting one.",
    ),
    chat_id: int | None = typer.Option(
        None, "--chat-id", help="Numeric Telegram chat id."
    ),
    chat_name: str | None = typer.Option(
        None, "--chat-name", help="Chat title (resolved within --folder-name)."
    ),
    entity: str | None = typer.Option(
        None,
        "--entity",
        help="Flexible entity reference (numeric id, @username, link, phone, "
        "or exact title) resolved via the shared resolver.",
    ),
    folder_name: str | None = typer.Option(
        None,
        "--folder-name",
        help="Folder used for --chat-name lookup "
        "(defaults to telegram.default_chat_folder.folder_name).",
    ),
    folder_id: int | None = typer.Option(
        None, "--folder-id", help="Optional folder id cross-check."
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Validate the request and report the plan without changing the reaction.",
    ),
    config_path: Path | None = typer.Option(  # noqa: B008
        None,
        "--config",
        "-c",
        help="Path to config.yml (defaults: ./data/config.yml, then ~/.config/telegram-assistant/config.yml).",
        exists=False,
    ),
) -> None:
    """Set or clear an emoji reaction on a message (WRITE-gated)."""
    from telegram_assistant.folders import FolderError
    from telegram_assistant.messages import (
        SendReactionRequest,
        set_message_reaction,
    )

    if sum([chat_id is not None, chat_name is not None, entity is not None]) != 1:
        typer.echo(
            "exactly one of --chat-id, --chat-name, or --entity must be supplied",
            err=True,
        )
        raise typer.Exit(code=2)
    if message_id <= 0:
        typer.echo("--message-id must be a positive integer", err=True)
        raise typer.Exit(code=2)
    has_emoji = bool(emoji and emoji.strip())
    if has_emoji and clear:
        typer.echo("provide either --emoji or --clear, not both", err=True)
        raise typer.Exit(code=2)
    if not has_emoji and not clear:
        typer.echo("provide either --emoji to set or --clear to remove", err=True)
        raise typer.Exit(code=2)

    config, manager, open_backends = _build_reaction_backends(config_path)
    if chat_name is not None:
        resolved_folder_name, default_fid, _ = _resolve_folder_name(
            folder_name, config_path
        )
        effective_folder_id = folder_id if folder_id is not None else default_fid
    else:
        resolved_folder_name = folder_name
        effective_folder_id = folder_id

    async def _resolve_target(folder_backend):
        from telegram_assistant.folders import resolve_chat_in_folder

        resolver = None
        if config.telegram.access is not None or entity is not None:
            from telegram_assistant.entities import TelethonEntityResolver

            resolver = TelethonEntityResolver(await manager.get_client())

        if entity is not None:
            assert resolver is not None
            resolved_entity = await resolver.resolve(entity)
            resolved_chat_id = resolved_entity.chat_id
            chat_name_for_log = resolved_entity.title
        elif chat_id is not None:
            resolved_chat_id = chat_id
            chat_name_for_log = None
        else:
            resolved = await resolve_chat_in_folder(
                folder_backend,
                folder_name=resolved_folder_name or "",
                chat_name=chat_name or "",
                folder_id=effective_folder_id,
            )
            resolved_chat_id = resolved.chat_id
            chat_name_for_log = resolved.title

        authorizer = _cli_authorizer(
            config, resolver=resolver, folder_backend=folder_backend
        )
        return resolved_chat_id, chat_name_for_log, authorizer

    if dry_run:

        async def _resolve_only() -> tuple[int, str | None]:
            try:
                _backend, folder_backend = await open_backends()
                tid, name, _auth = await _resolve_target(folder_backend)
                return tid, name
            finally:
                try:
                    await manager.disconnect()
                except Exception:
                    pass

        try:
            tid, name = asyncio.run(_resolve_only())
        except FolderError as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(code=2) from exc
        except Exception as exc:
            _raise_for_access_or_entity_error(exc)
            typer.echo(f"messages react failed: {exc}", err=True)
            raise typer.Exit(code=1) from exc

        action = "clear reaction" if clear else f"set reaction {emoji!r}"
        payload = {
            "status": "dry_run",
            "dry_run": True,
            "command": "messages.react",
            "would": f"{action} on message {message_id} in chat {tid}",
            "resolved": {
                "telegram_chat_id": tid,
                "chat_name": name,
                "telegram_message_id": message_id,
                "emoji": None if clear else emoji,
                "cleared": clear,
            },
            "planned_actions": [
                f"{action} on message {message_id} in chat {tid}"
            ],
            "warnings": [],
        }
        typer.echo(json.dumps(payload, sort_keys=True, default=str))
        return

    async def _run() -> dict[str, object]:
        try:
            backend, folder_backend = await open_backends()
            tid, name, authorizer = await _resolve_target(folder_backend)
            result = await set_message_reaction(
                backend,
                request=SendReactionRequest(
                    telegram_chat_id=tid,
                    message_id=message_id,
                    emoji=emoji,
                    clear=clear,
                    chat_name=name,
                ),
                authorizer=authorizer,
            )
            return result.to_dict()
        finally:
            try:
                await manager.disconnect()
            except Exception:
                pass

    try:
        payload = asyncio.run(_run())
    except FolderError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc
    except Exception as exc:
        _raise_for_access_or_entity_error(exc)
        typer.echo(f"messages react failed: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    typer.echo(json.dumps(payload, sort_keys=True, default=str))


def _build_forward_backends(config_path: Path | None):
    """Open the Telethon-backed forward + folder backends.

    ``_open`` returns ``(forward_backend, folder_backend)``. The folder backend
    feeds the authorizer's folder rules. Tests monkeypatch this to inject fakes.
    """
    config = _load_config_or_exit(config_path)
    manager = TelethonSessionManager(config.telegram)

    async def _open():
        from telegram_assistant.folders import TelethonFolderBackend
        from telegram_assistant.messages.telethon_backend import (
            TelethonForwardBackend,
        )

        client = await manager.get_client()
        if not await client.is_user_authorized():
            raise RuntimeError(
                "Telethon session is not authorized; run "
                "`telegram-assistant auth` first."
            )
        return (
            TelethonForwardBackend(client),
            TelethonFolderBackend(client),
        )

    return config, manager, _open


@messages_app.command("forward")
def messages_forward(
    message_id: list[int] = typer.Option(  # noqa: B008
        None,
        "--message-id",
        help="Numeric id of a message to forward (repeatable).",
    ),
    from_chat_id: int | None = typer.Option(
        None, "--from-chat-id", help="Numeric source Telegram chat id."
    ),
    from_entity: str | None = typer.Option(
        None,
        "--from-entity",
        help="Flexible source entity reference (numeric id, @username, link, "
        "phone, or exact title) resolved via the shared resolver.",
    ),
    to_chat_id: int | None = typer.Option(
        None, "--to-chat-id", help="Numeric target Telegram chat id."
    ),
    to_entity: str | None = typer.Option(
        None,
        "--to-entity",
        help="Flexible target entity reference (numeric id, @username, link, "
        "phone, or exact title) resolved via the shared resolver.",
    ),
    chat_id: int | None = typer.Option(
        None,
        "--chat-id",
        help="Numeric target Telegram chat id (alias for --to-chat-id).",
    ),
    chat_name: str | None = typer.Option(
        None,
        "--chat-name",
        help="Target chat title (resolved within --folder-name).",
    ),
    entity: str | None = typer.Option(
        None,
        "--entity",
        help="Flexible target entity reference (alias for --to-entity).",
    ),
    folder_name: str | None = typer.Option(
        None,
        "--folder-name",
        help="Folder used for --chat-name target lookup "
        "(defaults to telegram.default_chat_folder.folder_name).",
    ),
    folder_id: int | None = typer.Option(
        None, "--folder-id", help="Optional target folder id cross-check."
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Validate the request and report the plan without forwarding.",
    ),
    config_path: Path | None = typer.Option(  # noqa: B008
        None,
        "--config",
        "-c",
        help="Path to config.yml (defaults: ./data/config.yml, then ~/.config/telegram-assistant/config.yml).",
        exists=False,
    ),
) -> None:
    """Forward messages from a source chat to a target chat.

    READ-gated on the source, WRITE-gated on the target.
    """
    from telegram_assistant.messages import (
        ForwardMessagesRequest,
        forward_messages,
    )

    message_ids = tuple(message_id or ())
    if not message_ids:
        typer.echo("at least one --message-id is required", err=True)
        raise typer.Exit(code=2)
    if any(mid <= 0 for mid in message_ids):
        typer.echo("every --message-id must be a positive integer", err=True)
        raise typer.Exit(code=2)
    if sum([from_chat_id is not None, from_entity is not None]) != 1:
        typer.echo(
            "exactly one of --from-chat-id or --from-entity must be supplied",
            err=True,
        )
        raise typer.Exit(code=2)
    target_refs = sum(
        [
            to_chat_id is not None,
            to_entity is not None,
            chat_id is not None,
            chat_name is not None,
            entity is not None,
        ]
    )
    if target_refs != 1:
        typer.echo(
            "exactly one target must be supplied: --to-chat-id, --to-entity, "
            "--chat-id, --chat-name, or --entity",
            err=True,
        )
        raise typer.Exit(code=2)

    config, manager, open_backends = _build_forward_backends(config_path)
    if chat_name is not None:
        resolved_folder_name, default_fid, _ = _resolve_folder_name(
            folder_name, config_path
        )
        effective_folder_id = folder_id if folder_id is not None else default_fid
    else:
        resolved_folder_name = folder_name
        effective_folder_id = folder_id

    async def _resolve_target(folder_backend):
        from telegram_assistant.folders import resolve_chat_in_folder

        resolver = None
        if (
            config.telegram.access is not None
            or from_entity is not None
            or to_entity is not None
            or entity is not None
        ):
            from telegram_assistant.entities import TelethonEntityResolver

            resolver = TelethonEntityResolver(await manager.get_client())

        if from_entity is not None:
            assert resolver is not None
            resolved = await resolver.resolve(from_entity)
            src_id = resolved.chat_id
            src_name = resolved.title
        else:
            src_id = from_chat_id
            src_name = None

        if to_entity is not None or entity is not None:
            assert resolver is not None
            resolved = await resolver.resolve(
                to_entity if to_entity is not None else entity
            )
            tgt_id = resolved.chat_id
            tgt_name = resolved.title
        elif to_chat_id is not None:
            tgt_id = to_chat_id
            tgt_name = None
        elif chat_id is not None:
            tgt_id = chat_id
            tgt_name = None
        else:
            if folder_backend is None:
                raise ValueError("--chat-name target requires a folder backend")
            resolved = await resolve_chat_in_folder(
                folder_backend,
                folder_name=resolved_folder_name or "",
                chat_name=chat_name or "",
                folder_id=effective_folder_id,
            )
            tgt_id = resolved.chat_id
            tgt_name = resolved.title

        authorizer = _cli_authorizer(
            config, resolver=resolver, folder_backend=folder_backend
        )
        return src_id, src_name, tgt_id, tgt_name, authorizer

    if dry_run:

        async def _resolve_only():
            try:
                _backend, folder_backend = await open_backends()
                src_id, src_name, tgt_id, tgt_name, _auth = await _resolve_target(
                    folder_backend
                )
                return src_id, src_name, tgt_id, tgt_name
            finally:
                try:
                    await manager.disconnect()
                except Exception:
                    pass

        try:
            src_id, src_name, tgt_id, tgt_name = asyncio.run(_resolve_only())
        except Exception as exc:
            _raise_for_access_or_entity_error(exc)
            typer.echo(f"messages forward failed: {exc}", err=True)
            raise typer.Exit(code=1) from exc

        would = (
            f"forward {list(message_ids)} from chat {src_id} to chat {tgt_id}"
        )
        payload = {
            "status": "dry_run",
            "dry_run": True,
            "command": "messages.forward",
            "would": would,
            "resolved": {
                "from_chat_id": src_id,
                "from_chat_name": src_name,
                "to_chat_id": tgt_id,
                "to_chat_name": tgt_name,
                "message_ids": list(message_ids),
            },
            "planned_actions": [would],
            "warnings": [],
        }
        typer.echo(json.dumps(payload, sort_keys=True, default=str))
        return

    async def _run() -> dict[str, object]:
        try:
            backend, folder_backend = await open_backends()
            src_id, src_name, tgt_id, tgt_name, authorizer = (
                await _resolve_target(folder_backend)
            )
            result = await forward_messages(
                backend,
                request=ForwardMessagesRequest(
                    from_chat_id=src_id,
                    to_chat_id=tgt_id,
                    message_ids=message_ids,
                    from_chat_name=src_name,
                    to_chat_name=tgt_name,
                ),
                authorizer=authorizer,
            )
            return result.to_dict()
        finally:
            try:
                await manager.disconnect()
            except Exception:
                pass

    try:
        payload = asyncio.run(_run())
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc
    except Exception as exc:
        _raise_for_access_or_entity_error(exc)
        typer.echo(f"messages forward failed: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    typer.echo(json.dumps(payload, sort_keys=True, default=str))


def _build_delete_backends(config_path: Path | None):
    """Open the Telethon-backed delete + folder backends.

    ``_open`` returns ``(delete_backend, folder_backend)``. The folder backend
    resolves ``--chat-name`` and feeds the authorizer's folder rules. Tests
    monkeypatch this to inject fakes.
    """
    config = _load_config_or_exit(config_path)
    manager = TelethonSessionManager(config.telegram)

    async def _open():
        from telegram_assistant.folders import TelethonFolderBackend
        from telegram_assistant.messages.telethon_backend import (
            TelethonDeleteBackend,
        )

        client = await manager.get_client()
        if not await client.is_user_authorized():
            raise RuntimeError(
                "Telethon session is not authorized; run "
                "`telegram-assistant auth` first."
            )
        return (
            TelethonDeleteBackend(client),
            TelethonFolderBackend(client),
        )

    return config, manager, _open


@messages_app.command("delete")
def messages_delete(
    message_id: list[int] = typer.Option(  # noqa: B008
        None,
        "--message-id",
        help="Numeric id of a message to delete (repeatable).",
    ),
    chat_id: int | None = typer.Option(
        None, "--chat-id", help="Numeric Telegram chat id."
    ),
    chat_name: str | None = typer.Option(
        None, "--chat-name", help="Chat title (resolved within --folder-name)."
    ),
    entity: str | None = typer.Option(
        None,
        "--entity",
        help="Flexible entity reference (numeric id, @username, link, phone, "
        "or exact title) resolved via the shared resolver.",
    ),
    folder_name: str | None = typer.Option(
        None,
        "--folder-name",
        help="Folder used for --chat-name lookup "
        "(defaults to telegram.default_chat_folder.folder_name).",
    ),
    folder_id: int | None = typer.Option(
        None, "--folder-id", help="Optional folder id cross-check."
    ),
    revoke: bool = typer.Option(
        True,
        "--revoke/--no-revoke",
        help="Delete for everyone (default) or only the technical account's copy.",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Validate + authorize the request and report the plan without deleting.",
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help="Carried for surface consistency; message delete has no protected-chat "
        "registry today, so it currently has no gating effect.",
    ),
    config_path: Path | None = typer.Option(  # noqa: B008
        None,
        "--config",
        "-c",
        help="Path to config.yml (defaults: ./data/config.yml, then ~/.config/telegram-assistant/config.yml).",
        exists=False,
    ),
) -> None:
    """Delete messages from a chat (DELETE-gated).

    Honors ``telegram.access.delete_only_session_messages`` (default true): when
    active, only messages this process sent can be deleted. A fresh CLI process
    has an empty sent registry, so deleting arbitrary messages requires setting
    ``delete_only_session_messages: false`` in config.
    """
    from telegram_assistant.messages import (
        DeleteMessagesRequest,
        MessageDeleteForbidden,
        SentMessageRegistry,
        delete_messages,
    )

    message_ids = tuple(message_id or ())
    if not message_ids:
        typer.echo("at least one --message-id is required", err=True)
        raise typer.Exit(code=2)
    if any(mid <= 0 for mid in message_ids):
        typer.echo("every --message-id must be a positive integer", err=True)
        raise typer.Exit(code=2)
    if sum([chat_id is not None, chat_name is not None, entity is not None]) != 1:
        typer.echo(
            "exactly one of --chat-id, --chat-name, or --entity must be supplied",
            err=True,
        )
        raise typer.Exit(code=2)

    config, manager, open_backends = _build_delete_backends(config_path)
    if chat_name is not None:
        resolved_folder_name, default_fid, _ = _resolve_folder_name(
            folder_name, config_path
        )
        effective_folder_id = folder_id if folder_id is not None else default_fid
    else:
        resolved_folder_name = folder_name
        effective_folder_id = folder_id

    access = config.telegram.access
    only_session_default = (
        access.delete_only_session_messages if access is not None else True
    )
    # A fresh CLI process has no send history; an empty registry means the
    # session-limit (when enabled) blocks every id.
    registry = SentMessageRegistry()

    async def _resolve_target(folder_backend):
        from telegram_assistant.folders import resolve_chat_in_folder

        resolver = None
        if config.telegram.access is not None or entity is not None:
            from telegram_assistant.entities import TelethonEntityResolver

            resolver = TelethonEntityResolver(await manager.get_client())

        if entity is not None:
            assert resolver is not None
            resolved_entity = await resolver.resolve(entity)
            resolved_chat_id = resolved_entity.chat_id
            chat_name_for_log = resolved_entity.title
        elif chat_id is not None:
            resolved_chat_id = chat_id
            chat_name_for_log = None
        else:
            resolved = await resolve_chat_in_folder(
                folder_backend,
                folder_name=resolved_folder_name or "",
                chat_name=chat_name or "",
                folder_id=effective_folder_id,
            )
            resolved_chat_id = resolved.chat_id
            chat_name_for_log = resolved.title

        authorizer = _cli_authorizer(
            config, resolver=resolver, folder_backend=folder_backend
        )
        return resolved_chat_id, chat_name_for_log, authorizer

    async def _run() -> dict[str, object]:
        try:
            backend, folder_backend = await open_backends()
            tid, name, authorizer = await _resolve_target(folder_backend)
            only_session = await authorizer.delete_only_session_messages(
                tid, default=only_session_default
            )
            result = await delete_messages(
                backend,
                request=DeleteMessagesRequest(
                    telegram_chat_id=tid,
                    message_ids=message_ids,
                    revoke=revoke,
                    dry_run=dry_run,
                    force=force,
                    chat_name=name,
                ),
                authorizer=authorizer,
                sent_registry=registry,
                only_session_messages=only_session,
            )
            return result.to_dict()
        finally:
            try:
                await manager.disconnect()
            except Exception:
                pass

    from telegram_assistant.folders import FolderError

    try:
        payload = asyncio.run(_run())
    except FolderError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc
    except MessageDeleteForbidden as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=3) from exc
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc
    except Exception as exc:
        _raise_for_access_or_entity_error(exc)
        typer.echo(f"messages delete failed: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    typer.echo(json.dumps(payload, sort_keys=True, default=str))


def _build_edit_backends(config_path: Path | None):
    """Open the Telethon-backed edit + folder backends.

    ``_open`` returns ``(edit_backend, folder_backend)``. The folder backend
    resolves ``--chat-name`` and feeds the authorizer's folder rules. Tests
    monkeypatch this to inject fakes.
    """
    config = _load_config_or_exit(config_path)
    manager = TelethonSessionManager(config.telegram)

    async def _open():
        from telegram_assistant.folders import TelethonFolderBackend
        from telegram_assistant.messages.telethon_backend import (
            TelethonEditBackend,
        )

        client = await manager.get_client()
        if not await client.is_user_authorized():
            raise RuntimeError(
                "Telethon session is not authorized; run "
                "`telegram-assistant auth` first."
            )
        return (
            TelethonEditBackend(client),
            TelethonFolderBackend(client),
        )

    return config, manager, _open


@messages_app.command("edit")
def messages_edit(
    message_id: int = typer.Option(
        ..., "--message-id", help="Numeric id of the message to edit."
    ),
    text: str = typer.Option(
        ..., "--text", help="New text/caption for the message (non-empty)."
    ),
    chat_id: int | None = typer.Option(
        None, "--chat-id", help="Numeric Telegram chat id."
    ),
    chat_name: str | None = typer.Option(
        None, "--chat-name", help="Chat title (resolved within --folder-name)."
    ),
    entity: str | None = typer.Option(
        None,
        "--entity",
        help="Flexible entity reference (numeric id, @username, link, phone, "
        "or exact title) resolved via the shared resolver.",
    ),
    folder_name: str | None = typer.Option(
        None,
        "--folder-name",
        help="Folder used for --chat-name lookup "
        "(defaults to telegram.default_chat_folder.folder_name).",
    ),
    folder_id: int | None = typer.Option(
        None, "--folder-id", help="Optional folder id cross-check."
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Validate + authorize the request and report the plan without editing.",
    ),
    config_path: Path | None = typer.Option(  # noqa: B008
        None,
        "--config",
        "-c",
        help="Path to config.yml (defaults: ./data/config.yml, then ~/.config/telegram-assistant/config.yml).",
        exists=False,
    ),
) -> None:
    """Edit the text/caption of a sent message (WRITE-gated).

    Honors ``telegram.access.edit_only_session_messages`` (default true): when
    active, only messages this process sent can be edited. A fresh CLI process
    has an empty sent registry, so editing arbitrary messages requires setting
    ``edit_only_session_messages: false`` in config.
    """
    from telegram_assistant.messages import (
        MessageEditForbidden,
        MessageEditRejected,
        MessageEditRequest,
        SentMessageRegistry,
        edit_message,
    )

    if message_id <= 0:
        typer.echo("--message-id must be a positive integer", err=True)
        raise typer.Exit(code=2)
    if not text or not text.strip():
        typer.echo("--text must be a non-empty string", err=True)
        raise typer.Exit(code=2)
    if sum([chat_id is not None, chat_name is not None, entity is not None]) != 1:
        typer.echo(
            "exactly one of --chat-id, --chat-name, or --entity must be supplied",
            err=True,
        )
        raise typer.Exit(code=2)

    config, manager, open_backends = _build_edit_backends(config_path)
    if chat_name is not None:
        resolved_folder_name, default_fid, _ = _resolve_folder_name(
            folder_name, config_path
        )
        effective_folder_id = folder_id if folder_id is not None else default_fid
    else:
        resolved_folder_name = folder_name
        effective_folder_id = folder_id

    access = config.telegram.access
    only_session_default = (
        access.edit_only_session_messages if access is not None else True
    )
    # A fresh CLI process has no send history; an empty registry means the
    # session-limit (when enabled) blocks every id.
    registry = SentMessageRegistry()

    async def _resolve_target(folder_backend):
        from telegram_assistant.folders import resolve_chat_in_folder

        resolver = None
        if config.telegram.access is not None or entity is not None:
            from telegram_assistant.entities import TelethonEntityResolver

            resolver = TelethonEntityResolver(await manager.get_client())

        if entity is not None:
            assert resolver is not None
            resolved_entity = await resolver.resolve(entity)
            resolved_chat_id = resolved_entity.chat_id
            chat_name_for_log = resolved_entity.title
        elif chat_id is not None:
            resolved_chat_id = chat_id
            chat_name_for_log = None
        else:
            resolved = await resolve_chat_in_folder(
                folder_backend,
                folder_name=resolved_folder_name or "",
                chat_name=chat_name or "",
                folder_id=effective_folder_id,
            )
            resolved_chat_id = resolved.chat_id
            chat_name_for_log = resolved.title

        authorizer = _cli_authorizer(
            config, resolver=resolver, folder_backend=folder_backend
        )
        return resolved_chat_id, chat_name_for_log, authorizer

    async def _run() -> dict[str, object]:
        try:
            backend, folder_backend = await open_backends()
            tid, name, authorizer = await _resolve_target(folder_backend)
            only_session = await authorizer.edit_only_session_messages(
                tid, default=only_session_default
            )
            result = await edit_message(
                backend,
                request=MessageEditRequest(
                    telegram_chat_id=tid,
                    message_id=message_id,
                    text=text,
                    dry_run=dry_run,
                    chat_name=name,
                ),
                authorizer=authorizer,
                sent_registry=registry,
                only_session_messages=only_session,
            )
            return result.to_dict()
        finally:
            try:
                await manager.disconnect()
            except Exception:
                pass

    from telegram_assistant.folders import FolderError

    try:
        payload = asyncio.run(_run())
    except FolderError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc
    except MessageEditForbidden as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=3) from exc
    except MessageEditRejected as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc
    except Exception as exc:
        _raise_for_access_or_entity_error(exc)
        typer.echo(f"messages edit failed: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    typer.echo(json.dumps(payload, sort_keys=True, default=str))


def _build_pin_backends(config_path: Path | None):
    """Open the Telethon-backed pin + folder backends.

    ``_open`` returns ``(pin_backend, folder_backend)``. The folder backend
    resolves ``--chat-name`` and feeds the authorizer's folder rules. Tests
    monkeypatch this to inject fakes.
    """
    config = _load_config_or_exit(config_path)
    manager = TelethonSessionManager(config.telegram)

    async def _open():
        from telegram_assistant.folders import TelethonFolderBackend
        from telegram_assistant.messages.telethon_backend import (
            TelethonPinBackend,
        )

        client = await manager.get_client()
        if not await client.is_user_authorized():
            raise RuntimeError(
                "Telethon session is not authorized; run "
                "`telegram-assistant auth` first."
            )
        return (
            TelethonPinBackend(client),
            TelethonFolderBackend(client),
        )

    return config, manager, _open


@messages_app.command("pin")
def messages_pin(
    message_id: int = typer.Option(
        ..., "--message-id", help="Numeric id of the message to pin."
    ),
    silent: bool = typer.Option(
        False, "--silent", help="Suppress the pin service notification."
    ),
    pm_oneside: bool = typer.Option(
        False,
        "--pm-oneside",
        help="In a private chat, pin only on the acting side.",
    ),
    chat_id: int | None = typer.Option(
        None, "--chat-id", help="Numeric Telegram chat id."
    ),
    chat_name: str | None = typer.Option(
        None, "--chat-name", help="Chat title (resolved within --folder-name)."
    ),
    entity: str | None = typer.Option(
        None,
        "--entity",
        help="Flexible entity reference (numeric id, @username, link, phone, "
        "or exact title) resolved via the shared resolver.",
    ),
    folder_name: str | None = typer.Option(
        None,
        "--folder-name",
        help="Folder used for --chat-name lookup "
        "(defaults to telegram.default_chat_folder.folder_name).",
    ),
    folder_id: int | None = typer.Option(
        None, "--folder-id", help="Optional folder id cross-check."
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Validate + authorize the request and report the plan without pinning.",
    ),
    config_path: Path | None = typer.Option(  # noqa: B008
        None,
        "--config",
        "-c",
        help="Path to config.yml (defaults: ./data/config.yml, then ~/.config/telegram-assistant/config.yml).",
        exists=False,
    ),
) -> None:
    """Pin a message in a chat (WRITE-gated)."""
    from telegram_assistant.messages import PinMessageRequest, pin_message

    if message_id <= 0:
        typer.echo("--message-id must be a positive integer", err=True)
        raise typer.Exit(code=2)
    if sum([chat_id is not None, chat_name is not None, entity is not None]) != 1:
        typer.echo(
            "exactly one of --chat-id, --chat-name, or --entity must be supplied",
            err=True,
        )
        raise typer.Exit(code=2)

    config, manager, open_backends = _build_pin_backends(config_path)
    if chat_name is not None:
        resolved_folder_name, default_fid, _ = _resolve_folder_name(
            folder_name, config_path
        )
        effective_folder_id = folder_id if folder_id is not None else default_fid
    else:
        resolved_folder_name = folder_name
        effective_folder_id = folder_id

    async def _resolve_target(folder_backend):
        from telegram_assistant.folders import resolve_chat_in_folder

        resolver = None
        if config.telegram.access is not None or entity is not None:
            from telegram_assistant.entities import TelethonEntityResolver

            resolver = TelethonEntityResolver(await manager.get_client())

        if entity is not None:
            assert resolver is not None
            resolved_entity = await resolver.resolve(entity)
            resolved_chat_id = resolved_entity.chat_id
            chat_name_for_log = resolved_entity.title
        elif chat_id is not None:
            resolved_chat_id = chat_id
            chat_name_for_log = None
        else:
            resolved = await resolve_chat_in_folder(
                folder_backend,
                folder_name=resolved_folder_name or "",
                chat_name=chat_name or "",
                folder_id=effective_folder_id,
            )
            resolved_chat_id = resolved.chat_id
            chat_name_for_log = resolved.title

        authorizer = _cli_authorizer(
            config, resolver=resolver, folder_backend=folder_backend
        )
        return resolved_chat_id, chat_name_for_log, authorizer

    async def _run() -> dict[str, object]:
        try:
            backend, folder_backend = await open_backends()
            tid, name, authorizer = await _resolve_target(folder_backend)
            result = await pin_message(
                backend,
                request=PinMessageRequest(
                    telegram_chat_id=tid,
                    message_id=message_id,
                    silent=silent,
                    pm_oneside=pm_oneside,
                    dry_run=dry_run,
                    chat_name=name,
                ),
                authorizer=authorizer,
                pacer=_cli_pin_pacer(config),
            )
            return result.to_dict()
        finally:
            try:
                await manager.disconnect()
            except Exception:
                pass

    from telegram_assistant.folders import FolderError

    try:
        payload = asyncio.run(_run())
    except FolderError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc
    except Exception as exc:
        _raise_for_access_or_entity_error(exc)
        _raise_for_flood_wait(exc, "messages pin")
        typer.echo(f"messages pin failed: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    typer.echo(json.dumps(payload, sort_keys=True, default=str))


@messages_app.command("unpin")
def messages_unpin(
    message_id: int | None = typer.Option(
        None,
        "--message-id",
        help="Numeric id of the message to unpin (omit with --all to unpin all).",
    ),
    unpin_all: bool = typer.Option(
        False, "--all", help="Unpin every pinned message in the chat."
    ),
    chat_id: int | None = typer.Option(
        None, "--chat-id", help="Numeric Telegram chat id."
    ),
    chat_name: str | None = typer.Option(
        None, "--chat-name", help="Chat title (resolved within --folder-name)."
    ),
    entity: str | None = typer.Option(
        None,
        "--entity",
        help="Flexible entity reference (numeric id, @username, link, phone, "
        "or exact title) resolved via the shared resolver.",
    ),
    folder_name: str | None = typer.Option(
        None,
        "--folder-name",
        help="Folder used for --chat-name lookup "
        "(defaults to telegram.default_chat_folder.folder_name).",
    ),
    folder_id: int | None = typer.Option(
        None, "--folder-id", help="Optional folder id cross-check."
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Validate + authorize the request and report the plan without unpinning.",
    ),
    config_path: Path | None = typer.Option(  # noqa: B008
        None,
        "--config",
        "-c",
        help="Path to config.yml (defaults: ./data/config.yml, then ~/.config/telegram-assistant/config.yml).",
        exists=False,
    ),
) -> None:
    """Unpin a message (or all pinned messages) in a chat (WRITE-gated)."""
    from telegram_assistant.messages import UnpinMessageRequest, unpin_message

    if unpin_all and message_id is not None:
        typer.echo("provide either --message-id or --all, not both", err=True)
        raise typer.Exit(code=2)
    if not unpin_all and message_id is None:
        typer.echo("provide a --message-id to unpin or --all", err=True)
        raise typer.Exit(code=2)
    if message_id is not None and message_id <= 0:
        typer.echo("--message-id must be a positive integer", err=True)
        raise typer.Exit(code=2)
    if sum([chat_id is not None, chat_name is not None, entity is not None]) != 1:
        typer.echo(
            "exactly one of --chat-id, --chat-name, or --entity must be supplied",
            err=True,
        )
        raise typer.Exit(code=2)

    config, manager, open_backends = _build_pin_backends(config_path)
    if chat_name is not None:
        resolved_folder_name, default_fid, _ = _resolve_folder_name(
            folder_name, config_path
        )
        effective_folder_id = folder_id if folder_id is not None else default_fid
    else:
        resolved_folder_name = folder_name
        effective_folder_id = folder_id

    async def _resolve_target(folder_backend):
        from telegram_assistant.folders import resolve_chat_in_folder

        resolver = None
        if config.telegram.access is not None or entity is not None:
            from telegram_assistant.entities import TelethonEntityResolver

            resolver = TelethonEntityResolver(await manager.get_client())

        if entity is not None:
            assert resolver is not None
            resolved_entity = await resolver.resolve(entity)
            resolved_chat_id = resolved_entity.chat_id
            chat_name_for_log = resolved_entity.title
        elif chat_id is not None:
            resolved_chat_id = chat_id
            chat_name_for_log = None
        else:
            resolved = await resolve_chat_in_folder(
                folder_backend,
                folder_name=resolved_folder_name or "",
                chat_name=chat_name or "",
                folder_id=effective_folder_id,
            )
            resolved_chat_id = resolved.chat_id
            chat_name_for_log = resolved.title

        authorizer = _cli_authorizer(
            config, resolver=resolver, folder_backend=folder_backend
        )
        return resolved_chat_id, chat_name_for_log, authorizer

    async def _run() -> dict[str, object]:
        try:
            backend, folder_backend = await open_backends()
            tid, name, authorizer = await _resolve_target(folder_backend)
            result = await unpin_message(
                backend,
                request=UnpinMessageRequest(
                    telegram_chat_id=tid,
                    message_id=None if unpin_all else message_id,
                    dry_run=dry_run,
                    chat_name=name,
                ),
                authorizer=authorizer,
                pacer=_cli_pin_pacer(config),
            )
            return result.to_dict()
        finally:
            try:
                await manager.disconnect()
            except Exception:
                pass

    from telegram_assistant.folders import FolderError

    try:
        payload = asyncio.run(_run())
    except FolderError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc
    except Exception as exc:
        _raise_for_access_or_entity_error(exc)
        _raise_for_flood_wait(exc, "messages unpin")
        typer.echo(f"messages unpin failed: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    typer.echo(json.dumps(payload, sort_keys=True, default=str))


def _build_download_backends(config_path: Path | None):
    """Open the Telethon-backed media-download + folder backends.

    ``_open`` returns ``(download_backend, folder_backend)``. The folder backend
    resolves ``--chat-name`` and feeds the authorizer's folder rules. Tests
    monkeypatch this to inject fakes.
    """
    config = _load_config_or_exit(config_path)
    manager = TelethonSessionManager(config.telegram)

    async def _open():
        from telegram_assistant.folders import TelethonFolderBackend
        from telegram_assistant.messages.telethon_backend import (
            TelethonMediaDownloadBackend,
        )

        client = await manager.get_client()
        if not await client.is_user_authorized():
            raise RuntimeError(
                "Telethon session is not authorized; run "
                "`telegram-assistant auth` first."
            )
        return (
            TelethonMediaDownloadBackend(client),
            TelethonFolderBackend(client),
        )

    return config, manager, _open


@messages_app.command("download")
def messages_download(
    message_id: int = typer.Option(
        ..., "--message-id", help="Numeric id of the message to download from."
    ),
    out: Path | None = typer.Option(  # noqa: B008
        None,
        "--out",
        help="Explicit destination file (mutually exclusive with --dir).",
    ),
    directory: Path | None = typer.Option(  # noqa: B008
        None,
        "--dir",
        help="Destination directory; the original filename is joined in "
        "(mutually exclusive with --out).",
    ),
    max_bytes: int | None = typer.Option(
        None, "--max-bytes", help="Reject the download if larger than this many bytes."
    ),
    chat_id: int | None = typer.Option(
        None, "--chat-id", help="Numeric Telegram chat id."
    ),
    chat_name: str | None = typer.Option(
        None, "--chat-name", help="Chat title (resolved within --folder-name)."
    ),
    entity: str | None = typer.Option(
        None,
        "--entity",
        help="Flexible entity reference (numeric id, @username, link, phone, "
        "or exact title) resolved via the shared resolver.",
    ),
    folder_name: str | None = typer.Option(
        None,
        "--folder-name",
        help="Folder used for --chat-name lookup "
        "(defaults to telegram.default_chat_folder.folder_name).",
    ),
    folder_id: int | None = typer.Option(
        None, "--folder-id", help="Optional folder id cross-check."
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Validate + authorize + probe the media and report the plan "
        "without transferring.",
    ),
    config_path: Path | None = typer.Option(  # noqa: B008
        None,
        "--config",
        "-c",
        help="Path to config.yml (defaults: ./data/config.yml, then ~/.config/telegram-assistant/config.yml).",
        exists=False,
    ),
) -> None:
    """Download the media of an existing message to a local file (READ-gated)."""
    from telegram_assistant.messages import MediaDownloadRequest, download_media

    if message_id <= 0:
        typer.echo("--message-id must be a positive integer", err=True)
        raise typer.Exit(code=2)
    if (out is None) == (directory is None):
        typer.echo("provide exactly one of --out or --dir", err=True)
        raise typer.Exit(code=2)
    if max_bytes is not None and max_bytes <= 0:
        typer.echo("--max-bytes must be a positive integer", err=True)
        raise typer.Exit(code=2)
    if sum([chat_id is not None, chat_name is not None, entity is not None]) != 1:
        typer.echo(
            "exactly one of --chat-id, --chat-name, or --entity must be supplied",
            err=True,
        )
        raise typer.Exit(code=2)

    config, manager, open_backends = _build_download_backends(config_path)
    if chat_name is not None:
        resolved_folder_name, default_fid, _ = _resolve_folder_name(
            folder_name, config_path
        )
        effective_folder_id = folder_id if folder_id is not None else default_fid
    else:
        resolved_folder_name = folder_name
        effective_folder_id = folder_id

    async def _resolve_target(folder_backend):
        from telegram_assistant.folders import resolve_chat_in_folder

        resolver = None
        if config.telegram.access is not None or entity is not None:
            from telegram_assistant.entities import TelethonEntityResolver

            resolver = TelethonEntityResolver(await manager.get_client())

        if entity is not None:
            assert resolver is not None
            resolved_entity = await resolver.resolve(entity)
            resolved_chat_id = resolved_entity.chat_id
            chat_name_for_log = resolved_entity.title
        elif chat_id is not None:
            resolved_chat_id = chat_id
            chat_name_for_log = None
        else:
            resolved = await resolve_chat_in_folder(
                folder_backend,
                folder_name=resolved_folder_name or "",
                chat_name=chat_name or "",
                folder_id=effective_folder_id,
            )
            resolved_chat_id = resolved.chat_id
            chat_name_for_log = resolved.title

        authorizer = _cli_authorizer(
            config, resolver=resolver, folder_backend=folder_backend
        )
        return resolved_chat_id, chat_name_for_log, authorizer

    async def _run() -> dict[str, object]:
        try:
            backend, folder_backend = await open_backends()
            tid, name, authorizer = await _resolve_target(folder_backend)
            result = await download_media(
                backend,
                request=MediaDownloadRequest(
                    telegram_chat_id=tid,
                    message_id=message_id,
                    out_path=str(out) if out is not None else None,
                    out_dir=str(directory) if directory is not None else None,
                    max_bytes=max_bytes,
                    dry_run=dry_run,
                    chat_name=name,
                ),
                authorizer=authorizer,
            )
            return result.to_dict()
        finally:
            try:
                await manager.disconnect()
            except Exception:
                pass

    from telegram_assistant.folders import FolderError

    try:
        payload = asyncio.run(_run())
    except FolderError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc
    except Exception as exc:
        _raise_for_access_or_entity_error(exc)
        typer.echo(f"messages download failed: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    typer.echo(json.dumps(payload, sort_keys=True, default=str))


# --- notifications ----------------------------------------------------------

notifications_app = typer.Typer(
    help="Mute and unmute chat/contact notifications.", no_args_is_help=True
)
app.add_typer(notifications_app, name="notifications")


def _build_notification_backends(config_path: Path | None):
    """Open the Telethon-backed notification + folder backends.

    ``_open`` returns ``(notification_backend, folder_backend)``. The folder
    backend resolves ``--chat-name`` and feeds the authorizer's folder rules.
    Tests monkeypatch this to inject fakes.
    """
    config = _load_config_or_exit(config_path)
    manager = TelethonSessionManager(config.telegram)

    async def _open():
        from telegram_assistant.folders import TelethonFolderBackend
        from telegram_assistant.notifications import (
            TelethonNotificationBackend,
        )

        client = await manager.get_client()
        if not await client.is_user_authorized():
            raise RuntimeError(
                "Telethon session is not authorized; run "
                "`telegram-assistant auth` first."
            )
        return (
            TelethonNotificationBackend(client),
            TelethonFolderBackend(client),
        )

    return config, manager, _open


async def _resolve_notification_target(
    *,
    manager,
    config,
    folder_backend,
    chat_id,
    chat_name,
    entity,
    folder_name,
    folder_id,
):
    """Resolve the mute/unmute target and build the authorizer.

    Returns ``(telegram_chat_id, chat_name_for_log, authorizer)``. A resolver is
    constructed only when ``--entity`` is used or a policy with ``chat`` rules is
    configured, matching the lazy pattern used by ``messages send``.
    """
    from telegram_assistant.folders import resolve_chat_in_folder

    resolver = None
    if config.telegram.access is not None or entity is not None:
        from telegram_assistant.entities import TelethonEntityResolver

        resolver = TelethonEntityResolver(await manager.get_client())

    if entity is not None:
        assert resolver is not None
        resolved_entity = await resolver.resolve(entity)
        resolved_chat_id = resolved_entity.chat_id
        chat_name_for_log = resolved_entity.title
    elif chat_id is not None:
        resolved_chat_id = chat_id
        chat_name_for_log = None
    else:
        resolved = await resolve_chat_in_folder(
            folder_backend,
            folder_name=folder_name or "",
            chat_name=chat_name or "",
            folder_id=folder_id,
        )
        resolved_chat_id = resolved.chat_id
        chat_name_for_log = resolved.title

    authorizer = _cli_authorizer(
        config, resolver=resolver, folder_backend=folder_backend
    )
    return resolved_chat_id, chat_name_for_log, authorizer


@notifications_app.command("mute")
def notifications_mute(
    chat_id: int | None = typer.Option(
        None, "--chat-id", help="Numeric Telegram chat id."
    ),
    chat_name: str | None = typer.Option(
        None, "--chat-name", help="Chat title (resolved within --folder-name)."
    ),
    entity: str | None = typer.Option(
        None,
        "--entity",
        help="Flexible entity reference (numeric id, @username, link, phone, "
        "or exact title) resolved via the shared resolver.",
    ),
    folder_name: str | None = typer.Option(
        None,
        "--folder-name",
        help="Folder used for --chat-name lookup "
        "(defaults to telegram.default_chat_folder.folder_name).",
    ),
    folder_id: int | None = typer.Option(
        None, "--folder-id", help="Optional folder id cross-check."
    ),
    duration: int | None = typer.Option(
        None,
        "--duration",
        help="Mute for this many hours; omit to mute indefinitely (forever).",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Validate the request and report the plan without muting.",
    ),
    config_path: Path | None = typer.Option(  # noqa: B008
        None,
        "--config",
        "-c",
        help="Path to config.yml (defaults: ./data/config.yml, then ~/.config/telegram-assistant/config.yml).",
        exists=False,
    ),
) -> None:
    """Mute a chat or contact (forever, or for --duration hours)."""
    from telegram_assistant.folders import FolderError
    from telegram_assistant.notifications import MuteRequest, mute_chat

    if sum([chat_id is not None, chat_name is not None, entity is not None]) != 1:
        typer.echo(
            "exactly one of --chat-id, --chat-name, or --entity must be supplied",
            err=True,
        )
        raise typer.Exit(code=2)
    if duration is not None and duration <= 0:
        typer.echo("--duration must be a positive number of hours", err=True)
        raise typer.Exit(code=2)

    config, manager, open_backends = _build_notification_backends(config_path)
    if chat_name is not None:
        resolved_folder_name, default_fid, _ = _resolve_folder_name(
            folder_name, config_path
        )
        effective_folder_id = folder_id if folder_id is not None else default_fid
    else:
        resolved_folder_name = folder_name
        effective_folder_id = folder_id

    async def _resolve_only() -> tuple[int, str | None]:
        try:
            _backend, folder_backend = await open_backends()
            tid, name, _auth = await _resolve_notification_target(
                manager=manager,
                config=config,
                folder_backend=folder_backend,
                chat_id=chat_id,
                chat_name=chat_name,
                entity=entity,
                folder_name=resolved_folder_name,
                folder_id=effective_folder_id,
            )
            return tid, name
        finally:
            try:
                await manager.disconnect()
            except Exception:
                pass

    if dry_run:
        try:
            tid, name = asyncio.run(_resolve_only())
        except FolderError as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(code=2) from exc
        except Exception as exc:
            _raise_for_access_or_entity_error(exc)
            typer.echo(f"notifications mute failed: {exc}", err=True)
            raise typer.Exit(code=1) from exc

        scope = f"for {duration}h" if duration is not None else "forever"
        payload = {
            "status": "dry_run",
            "dry_run": True,
            "command": "notifications.mute",
            "would": f"mute chat {tid} {scope}",
            "resolved": {
                "telegram_chat_id": tid,
                "chat_name": name,
                "duration_hours": duration,
                "forever": duration is None,
            },
            "planned_actions": [f"mute chat {tid} {scope}"],
            "warnings": [],
        }
        typer.echo(json.dumps(payload, sort_keys=True, default=str))
        return

    async def _run() -> dict[str, object]:
        try:
            backend, folder_backend = await open_backends()
            tid, name, authorizer = await _resolve_notification_target(
                manager=manager,
                config=config,
                folder_backend=folder_backend,
                chat_id=chat_id,
                chat_name=chat_name,
                entity=entity,
                folder_name=resolved_folder_name,
                folder_id=effective_folder_id,
            )
            result = await mute_chat(
                backend,
                request=MuteRequest(
                    telegram_chat_id=tid,
                    duration_hours=duration,
                    chat_name=name,
                ),
                authorizer=authorizer,
            )
            return result.to_dict()
        finally:
            try:
                await manager.disconnect()
            except Exception:
                pass

    try:
        payload = asyncio.run(_run())
    except FolderError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc
    except Exception as exc:
        _raise_for_access_or_entity_error(exc)
        typer.echo(f"notifications mute failed: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    typer.echo(json.dumps(payload, sort_keys=True, default=str))


@notifications_app.command("unmute")
def notifications_unmute(
    chat_id: int | None = typer.Option(
        None, "--chat-id", help="Numeric Telegram chat id."
    ),
    chat_name: str | None = typer.Option(
        None, "--chat-name", help="Chat title (resolved within --folder-name)."
    ),
    entity: str | None = typer.Option(
        None,
        "--entity",
        help="Flexible entity reference (numeric id, @username, link, phone, "
        "or exact title) resolved via the shared resolver.",
    ),
    folder_name: str | None = typer.Option(
        None,
        "--folder-name",
        help="Folder used for --chat-name lookup "
        "(defaults to telegram.default_chat_folder.folder_name).",
    ),
    folder_id: int | None = typer.Option(
        None, "--folder-id", help="Optional folder id cross-check."
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Validate the request and report the plan without unmuting.",
    ),
    config_path: Path | None = typer.Option(  # noqa: B008
        None,
        "--config",
        "-c",
        help="Path to config.yml (defaults: ./data/config.yml, then ~/.config/telegram-assistant/config.yml).",
        exists=False,
    ),
) -> None:
    """Restore normal notifications for a chat or contact."""
    from telegram_assistant.folders import FolderError
    from telegram_assistant.notifications import MuteRequest, unmute_chat

    if sum([chat_id is not None, chat_name is not None, entity is not None]) != 1:
        typer.echo(
            "exactly one of --chat-id, --chat-name, or --entity must be supplied",
            err=True,
        )
        raise typer.Exit(code=2)

    config, manager, open_backends = _build_notification_backends(config_path)
    if chat_name is not None:
        resolved_folder_name, default_fid, _ = _resolve_folder_name(
            folder_name, config_path
        )
        effective_folder_id = folder_id if folder_id is not None else default_fid
    else:
        resolved_folder_name = folder_name
        effective_folder_id = folder_id

    async def _resolve_only() -> tuple[int, str | None]:
        try:
            _backend, folder_backend = await open_backends()
            tid, name, _auth = await _resolve_notification_target(
                manager=manager,
                config=config,
                folder_backend=folder_backend,
                chat_id=chat_id,
                chat_name=chat_name,
                entity=entity,
                folder_name=resolved_folder_name,
                folder_id=effective_folder_id,
            )
            return tid, name
        finally:
            try:
                await manager.disconnect()
            except Exception:
                pass

    if dry_run:
        try:
            tid, name = asyncio.run(_resolve_only())
        except FolderError as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(code=2) from exc
        except Exception as exc:
            _raise_for_access_or_entity_error(exc)
            typer.echo(f"notifications unmute failed: {exc}", err=True)
            raise typer.Exit(code=1) from exc

        payload = {
            "status": "dry_run",
            "dry_run": True,
            "command": "notifications.unmute",
            "would": f"unmute chat {tid}",
            "resolved": {"telegram_chat_id": tid, "chat_name": name},
            "planned_actions": [f"unmute chat {tid}"],
            "warnings": [],
        }
        typer.echo(json.dumps(payload, sort_keys=True, default=str))
        return

    async def _run() -> dict[str, object]:
        try:
            backend, folder_backend = await open_backends()
            tid, name, authorizer = await _resolve_notification_target(
                manager=manager,
                config=config,
                folder_backend=folder_backend,
                chat_id=chat_id,
                chat_name=chat_name,
                entity=entity,
                folder_name=resolved_folder_name,
                folder_id=effective_folder_id,
            )
            result = await unmute_chat(
                backend,
                request=MuteRequest(telegram_chat_id=tid, chat_name=name),
                authorizer=authorizer,
            )
            return result.to_dict()
        finally:
            try:
                await manager.disconnect()
            except Exception:
                pass

    try:
        payload = asyncio.run(_run())
    except FolderError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc
    except Exception as exc:
        _raise_for_access_or_entity_error(exc)
        typer.echo(f"notifications unmute failed: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    typer.echo(json.dumps(payload, sort_keys=True, default=str))


# --- folders ----------------------------------------------------------------

folders_app = typer.Typer(help="Inspect and manage chat folders.", no_args_is_help=True)
app.add_typer(folders_app, name="folders")


def _build_folder_backend(config_path: Path | None):
    """Construct a Telethon-backed folder backend for CLI invocations.

    Importing the adapter lazily keeps `folders --help` working in
    environments where Telethon is partially unavailable, and keeps the
    placeholder commands cheap.
    """
    config = _load_config_or_exit(config_path)
    manager = TelethonSessionManager(config.telegram)
    from telegram_assistant.folders import TelethonFolderBackend

    async def _open():
        client = await manager.get_client()
        if not await client.is_user_authorized():
            raise RuntimeError(
                "Telethon session is not authorized; run "
                "`telegram-assistant auth` first."
            )
        return TelethonFolderBackend(client)

    return config, manager, _open


def _resolve_folder_name(
    explicit: str | None,
    config_path: Path | None,
) -> tuple[str, int | None, Path | None]:
    """Resolve `--folder-name`, falling back to the configured default folder."""
    if explicit:
        return explicit, None, config_path
    config = _load_config_or_exit(config_path)
    default = config.telegram.default_chat_folder
    return default.folder_name, default.folder_id, config_path


@folders_app.command("inspect")
def folders_inspect(
    folder_name: str | None = typer.Option(
        None,
        "--folder-name",
        help="Folder name to inspect (defaults to telegram.default_chat_folder.folder_name).",
    ),
    folder_id: int | None = typer.Option(
        None,
        "--folder-id",
        help="Optional folder id cross-check.",
    ),
    config_path: Path | None = typer.Option(  # noqa: B008
        None,
        "--config",
        "-c",
        help="Path to config.yml (defaults: ./data/config.yml, then ~/.config/telegram-assistant/config.yml).",
        exists=False,
    ),
) -> None:
    """Inspect a chat folder and list its chats."""
    from telegram_assistant.folders import (
        FolderError,
        inspect_folder,
    )

    resolved_name, default_fid, cfg_path = _resolve_folder_name(
        folder_name, config_path
    )
    effective_fid = folder_id if folder_id is not None else default_fid
    _config, manager, open_backend = _build_folder_backend(cfg_path)

    async def _run() -> dict[str, object]:
        try:
            backend = await open_backend()
            snapshot = await inspect_folder(
                backend, folder_name=resolved_name, folder_id=effective_fid
            )
            return snapshot.to_dict()
        finally:
            try:
                await manager.disconnect()
            except Exception:
                pass

    try:
        payload = asyncio.run(_run())
    except FolderError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc
    except Exception as exc:
        typer.echo(f"folders inspect failed: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    typer.echo(json.dumps(payload, sort_keys=True))


@folders_app.command("add-chat")
def folders_add_chat(
    folder_name: str | None = typer.Option(
        None,
        "--folder-name",
        help="Folder name (defaults to telegram.default_chat_folder.folder_name).",
    ),
    chat_name: str | None = typer.Option(
        None,
        "--chat-name",
        help="Chat title (must match exactly one existing chat).",
    ),
    chat_id: int | None = typer.Option(
        None,
        "--chat-id",
        help="Numeric Telegram chat id.",
    ),
    entity: str | None = typer.Option(
        None,
        "--entity",
        help="Flexible entity reference (numeric id, @username, link, phone, "
        "or exact title) resolved via the shared resolver.",
    ),
    folder_id: int | None = typer.Option(
        None,
        "--folder-id",
        help="Optional folder id cross-check.",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Validate the request and report the plan without moving the chat.",
    ),
    config_path: Path | None = typer.Option(  # noqa: B008
        None,
        "--config",
        "-c",
        help="Path to config.yml (defaults: ./data/config.yml, then ~/.config/telegram-assistant/config.yml).",
        exists=False,
    ),
) -> None:
    """Move an existing chat into a folder."""
    from telegram_assistant.folders import (
        FolderError,
        add_chat_to_folder,
        resolve_chat_in_folder,
        resolve_folder,
    )

    if sum([chat_id is not None, chat_name is not None, entity is not None]) != 1:
        typer.echo(
            "exactly one of --chat-id, --chat-name, or --entity must be supplied",
            err=True,
        )
        raise typer.Exit(code=2)

    resolved_name, default_fid, cfg_path = _resolve_folder_name(
        folder_name, config_path
    )
    effective_fid = folder_id if folder_id is not None else default_fid
    _config, manager, open_backend = _build_folder_backend(cfg_path)

    if dry_run:
        async def _preview() -> dict[str, object]:
            try:
                backend = await open_backend()
                snapshot = await resolve_folder(
                    backend,
                    folder_name=resolved_name,
                    folder_id=effective_fid,
                )
                if entity is not None:
                    from telegram_assistant.entities import (
                        TelethonEntityResolver,
                    )

                    resolved_entity = await TelethonEntityResolver(
                        await manager.get_client()
                    ).resolve(entity)
                    chat = await backend.resolve_chat(resolved_entity.chat_id)
                elif chat_id is not None:
                    chat = await backend.resolve_chat(chat_id)
                else:
                    chat = await resolve_chat_in_folder(
                        backend,
                        folder_name=resolved_name,
                        chat_name=chat_name or "",
                        folder_id=effective_fid,
                    )
                already = any(c.chat_id == chat.chat_id for c in snapshot.chats)
                return {
                    "folder_id": snapshot.folder_id,
                    "folder_name": snapshot.folder_name,
                    "chat_id": chat.chat_id,
                    "chat_title": chat.title,
                    "already_in_folder": already,
                }
            finally:
                try:
                    await manager.disconnect()
                except Exception:
                    pass

        try:
            preview = asyncio.run(_preview())
        except FolderError as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(code=2) from exc
        except Exception as exc:
            _raise_for_access_or_entity_error(exc)
            typer.echo(f"folders add-chat failed: {exc}", err=True)
            raise typer.Exit(code=1) from exc

        warnings: list[str] = []
        if preview["already_in_folder"]:
            warnings.append(
                f"chat {preview['chat_id']} is already in folder "
                f"{preview['folder_name']!r}; real run would be a no-op"
            )
            planned_actions: list[str] = [
                f"no-op: chat {preview['chat_id']} already in folder "
                f"{preview['folder_name']!r}"
            ]
        else:
            planned_actions = [
                f"add chat {preview['chat_id']} ({preview['chat_title']!r}) to "
                f"folder {preview['folder_name']!r} (folder_id={preview['folder_id']})"
            ]
        resolved_payload: dict[str, object] = {
            "folder_id": preview["folder_id"],
            "folder_name": preview["folder_name"],
            "chat_id": preview["chat_id"],
            "chat_title": preview["chat_title"],
            "already_in_folder": preview["already_in_folder"],
        }
        if chat_name is not None:
            resolved_payload["chat_name"] = chat_name
        payload = {
            "status": "dry_run",
            "dry_run": True,
            "command": "folders.add_chat",
            "would": (
                f"add chat {preview['chat_id']} to folder "
                f"{preview['folder_name']!r}"
            ),
            "resolved": resolved_payload,
            "planned_actions": planned_actions,
            "warnings": warnings,
        }
        typer.echo(json.dumps(payload, sort_keys=True, default=str))
        return

    async def _run() -> dict[str, object]:
        try:
            from telegram_assistant.access import Authorizer

            backend = await open_backend()
            # ``_config`` may be ``None`` when a test injects a bare backend
            # factory; derive the access policy defensively.
            access_cfg = (
                getattr(_config.telegram, "access", None)
                if _config is not None
                else None
            )
            resolver = None
            if access_cfg is not None or entity is not None:
                from telegram_assistant.entities import TelethonEntityResolver

                resolver = TelethonEntityResolver(await manager.get_client())
            if entity is not None:
                assert resolver is not None
                chat_ref: str | int = (await resolver.resolve(entity)).chat_id
            elif chat_id is not None:
                chat_ref = chat_id
            else:
                # chat_name -> chat_id resolution shares the same code path as
                # the HTTP endpoint so ambiguous names fail loudly the same way.
                resolved = await resolve_chat_in_folder(
                    backend,
                    folder_name=resolved_name,
                    chat_name=chat_name or "",
                    folder_id=effective_fid,
                )
                chat_ref = resolved.chat_id
            authorizer = Authorizer(
                access_cfg,
                resolver=resolver,
                folder_backend=backend,
                cache=(
                    _cli_folder_membership_cache(_config)
                    if _config is not None
                    else None
                ),
            )
            return await add_chat_to_folder(
                backend,
                folder_name=resolved_name,
                chat_ref=chat_ref,
                folder_id=effective_fid,
                authorizer=authorizer,
            )
        finally:
            try:
                await manager.disconnect()
            except Exception:
                pass

    try:
        payload = asyncio.run(_run())
    except FolderError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc
    except Exception as exc:
        _raise_for_access_or_entity_error(exc)
        typer.echo(f"folders add-chat failed: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    typer.echo(json.dumps(payload, sort_keys=True))


@folders_app.command("remove-chat")
def folders_remove_chat(
    folder_name: str | None = typer.Option(
        None,
        "--folder-name",
        help="Folder name (defaults to telegram.default_chat_folder.folder_name).",
    ),
    chat_name: str | None = typer.Option(
        None,
        "--chat-name",
        help="Chat title (must match exactly one chat already in the folder).",
    ),
    chat_id: int | None = typer.Option(
        None,
        "--chat-id",
        help="Numeric Telegram chat id.",
    ),
    entity: str | None = typer.Option(
        None,
        "--entity",
        help="Flexible entity reference (numeric id, @username, link, phone, "
        "or exact title) resolved via the shared resolver.",
    ),
    folder_id: int | None = typer.Option(
        None,
        "--folder-id",
        help="Optional folder id cross-check.",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Validate the request and report the plan without moving the chat.",
    ),
    config_path: Path | None = typer.Option(  # noqa: B008
        None,
        "--config",
        "-c",
        help="Path to config.yml (defaults: ./data/config.yml, then ~/.config/telegram-assistant/config.yml).",
        exists=False,
    ),
) -> None:
    """Remove a chat from a folder (inverse of ``add-chat``)."""
    from telegram_assistant.folders import (
        FolderError,
        remove_chat_from_folder,
        resolve_chat_in_folder,
        resolve_folder,
    )

    if sum([chat_id is not None, chat_name is not None, entity is not None]) != 1:
        typer.echo(
            "exactly one of --chat-id, --chat-name, or --entity must be supplied",
            err=True,
        )
        raise typer.Exit(code=2)

    resolved_name, default_fid, cfg_path = _resolve_folder_name(
        folder_name, config_path
    )
    effective_fid = folder_id if folder_id is not None else default_fid
    _config, manager, open_backend = _build_folder_backend(cfg_path)

    if dry_run:
        async def _preview() -> dict[str, object]:
            try:
                backend = await open_backend()
                snapshot = await resolve_folder(
                    backend,
                    folder_name=resolved_name,
                    folder_id=effective_fid,
                )
                if entity is not None:
                    from telegram_assistant.entities import (
                        TelethonEntityResolver,
                    )

                    resolved_entity = await TelethonEntityResolver(
                        await manager.get_client()
                    ).resolve(entity)
                    chat = await backend.resolve_chat(resolved_entity.chat_id)
                elif chat_id is not None:
                    chat = await backend.resolve_chat(chat_id)
                else:
                    chat = await resolve_chat_in_folder(
                        backend,
                        folder_name=resolved_name,
                        chat_name=chat_name or "",
                        folder_id=effective_fid,
                    )
                present = any(c.chat_id == chat.chat_id for c in snapshot.chats)
                return {
                    "folder_id": snapshot.folder_id,
                    "folder_name": snapshot.folder_name,
                    "chat_id": chat.chat_id,
                    "chat_title": chat.title,
                    "already_absent": not present,
                }
            finally:
                try:
                    await manager.disconnect()
                except Exception:
                    pass

        try:
            preview = asyncio.run(_preview())
        except FolderError as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(code=2) from exc
        except Exception as exc:
            _raise_for_access_or_entity_error(exc)
            typer.echo(f"folders remove-chat failed: {exc}", err=True)
            raise typer.Exit(code=1) from exc

        warnings: list[str] = []
        if preview["already_absent"]:
            warnings.append(
                f"chat {preview['chat_id']} is not in folder "
                f"{preview['folder_name']!r}; real run would be a no-op"
            )
            planned_actions: list[str] = [
                f"no-op: chat {preview['chat_id']} not in folder "
                f"{preview['folder_name']!r}"
            ]
        else:
            planned_actions = [
                f"remove chat {preview['chat_id']} ({preview['chat_title']!r}) from "
                f"folder {preview['folder_name']!r} (folder_id={preview['folder_id']})"
            ]
        resolved_payload: dict[str, object] = {
            "folder_id": preview["folder_id"],
            "folder_name": preview["folder_name"],
            "chat_id": preview["chat_id"],
            "chat_title": preview["chat_title"],
            "already_absent": preview["already_absent"],
        }
        if chat_name is not None:
            resolved_payload["chat_name"] = chat_name
        payload = {
            "status": "dry_run",
            "dry_run": True,
            "command": "folders.remove_chat",
            "would": (
                f"remove chat {preview['chat_id']} from folder "
                f"{preview['folder_name']!r}"
            ),
            "resolved": resolved_payload,
            "planned_actions": planned_actions,
            "warnings": warnings,
        }
        typer.echo(json.dumps(payload, sort_keys=True, default=str))
        return

    async def _run() -> dict[str, object]:
        try:
            from telegram_assistant.access import Authorizer

            backend = await open_backend()
            access_cfg = (
                getattr(_config.telegram, "access", None)
                if _config is not None
                else None
            )
            resolver = None
            if access_cfg is not None or entity is not None:
                from telegram_assistant.entities import TelethonEntityResolver

                resolver = TelethonEntityResolver(await manager.get_client())
            if entity is not None:
                assert resolver is not None
                chat_ref: str | int = (await resolver.resolve(entity)).chat_id
            elif chat_id is not None:
                chat_ref = chat_id
            else:
                resolved = await resolve_chat_in_folder(
                    backend,
                    folder_name=resolved_name,
                    chat_name=chat_name or "",
                    folder_id=effective_fid,
                )
                chat_ref = resolved.chat_id
            authorizer = Authorizer(
                access_cfg,
                resolver=resolver,
                folder_backend=backend,
                cache=(
                    _cli_folder_membership_cache(_config)
                    if _config is not None
                    else None
                ),
            )
            return await remove_chat_from_folder(
                backend,
                folder_name=resolved_name,
                chat_ref=chat_ref,
                folder_id=effective_fid,
                authorizer=authorizer,
            )
        finally:
            try:
                await manager.disconnect()
            except Exception:
                pass

    try:
        payload = asyncio.run(_run())
    except FolderError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc
    except Exception as exc:
        _raise_for_access_or_entity_error(exc)
        typer.echo(f"folders remove-chat failed: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    typer.echo(json.dumps(payload, sort_keys=True))


# --- operations -------------------------------------------------------------

operations_app = typer.Typer(help="Inspect and retry queued operations.", no_args_is_help=True)
app.add_typer(operations_app, name="operations")


def _open_store(config_path: Path | None) -> OperationStore:
    config = _load_config_or_exit(config_path)
    return OperationStore(default_database_path(config))


def _operation_summary(store: OperationStore, operation_id: str) -> dict[str, object]:
    try:
        op = store.get_operation(operation_id)
    except OperationNotFoundError as exc:
        typer.echo(f"operation {operation_id} not found", err=True)
        raise typer.Exit(code=2) from exc
    items = store.list_items(op.id)
    by_status: dict[str, int] = {}
    for it in items:
        by_status[it.status.value] = by_status.get(it.status.value, 0) + 1
    payload: dict[str, object] = op.to_dict()
    payload["items"] = {
        "total": len(items),
        "by_status": by_status,
        "needs_review": [
            {
                "id": it.id,
                "idempotency_key": it.idempotency_key,
                "error": it.error,
            }
            for it in items
            if it.status is OperationStatus.NEEDS_REVIEW
        ],
    }
    return payload


@operations_app.command("status")
def operations_status(
    operation_id: str = typer.Option(
        ...,
        "--operation-id",
        help="ID of the operation to inspect.",
    ),
    config_path: Path | None = typer.Option(  # noqa: B008
        None,
        "--config",
        "-c",
        help="Path to config.yml (defaults: ./data/config.yml, then ~/.config/telegram-assistant/config.yml).",
        exists=False,
    ),
) -> None:
    """Show the status of an operation, including per-item summary."""
    store = _open_store(config_path)
    payload = _operation_summary(store, operation_id)
    typer.echo(json.dumps(payload, sort_keys=True, default=str))


@operations_app.command("retry")
def operations_retry(
    operation_id: str = typer.Option(
        ...,
        "--operation-id",
        help="ID of the operation to retry.",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Validate the retry and report the plan without resetting state.",
    ),
    config_path: Path | None = typer.Option(  # noqa: B008
        None,
        "--config",
        "-c",
        help="Path to config.yml (defaults: ./data/config.yml, then ~/.config/telegram-assistant/config.yml).",
        exists=False,
    ),
) -> None:
    """Reset a failed/needs_review operation (and its items) back to pending.

    The actual re-execution is performed by the worker queue when it next
    picks the operation up — this command only flips state so it becomes
    eligible for retry.
    """
    store = _open_store(config_path)
    try:
        op = store.get_operation(operation_id)
    except OperationNotFoundError as exc:
        typer.echo(f"operation {operation_id} not found", err=True)
        raise typer.Exit(code=2) from exc
    if op.status is OperationStatus.COMPLETED:
        typer.echo(
            f"operation {operation_id} is completed; nothing to retry", err=True
        )
        raise typer.Exit(code=2)

    if dry_run:
        items = store.list_items(operation_id)
        eligible = [
            it
            for it in items
            if it.status in (OperationStatus.FAILED, OperationStatus.NEEDS_REVIEW)
        ]
        would_reset_operation = op.status is not OperationStatus.PENDING
        item_summary = [
            {
                "id": it.id,
                "idempotency_key": it.idempotency_key,
                "status": it.status.value,
                "error": it.error,
            }
            for it in eligible
        ]
        warnings: list[str] = []
        if op.status is OperationStatus.PENDING and not eligible:
            warnings.append(
                f"operation {operation_id} is already pending and has no "
                "failed/needs_review items; retry would be a no-op"
            )
        planned_actions: list[str] = []
        if would_reset_operation:
            planned_actions.append(
                f"reset operation {operation_id} from "
                f"{op.status.value} to pending"
            )
        for it in eligible:
            planned_actions.append(
                f"reset item {it.id} (key={it.idempotency_key!r}, "
                f"status={it.status.value}) to pending"
            )
        if not planned_actions:
            planned_actions.append(
                f"no-op: nothing to reset for operation {operation_id}"
            )
        payload = {
            "status": "dry_run",
            "dry_run": True,
            "command": "operations.retry",
            "would": (
                f"reset operation {operation_id} (and "
                f"{len(eligible)} item(s)) for retry"
            ),
            "resolved": {
                "operation_id": operation_id,
                "operation_status": op.status.value,
                "operation_type": op.type,
                "would_reset_operation": would_reset_operation,
                "items_to_reset": item_summary,
            },
            "planned_actions": planned_actions,
            "warnings": warnings,
        }
        typer.echo(json.dumps(payload, sort_keys=True, default=str))
        return

    reset_items = store.reset_items_for_retry(operation_id)
    if op.status is not OperationStatus.PENDING:
        store.reset_operation_for_retry(operation_id)
    typer.echo(
        json.dumps(
            {
                "operation_id": operation_id,
                "operation_reset": op.status is not OperationStatus.PENDING,
                "items_reset": [it.id for it in reset_items],
            },
            sort_keys=True,
        )
    )


# --- access -----------------------------------------------------------------

access_app = typer.Typer(
    help="Inspect and manage the access policy (telegram.access).",
    no_args_is_help=True,
)
app.add_typer(access_app, name="access")


def _build_access_resolver(config_path: Path | None):
    """Open the Telethon-backed resolver + folder backend for access checks.

    Mirrors :func:`_build_message_read_backends`: lazy Telethon imports keep
    ``access check --help`` cheap. Tests monkeypatch this to inject fakes.
    """
    config = _load_config_or_exit(config_path)
    manager = TelethonSessionManager(config.telegram)

    async def _open():
        from telegram_assistant.entities import TelethonEntityResolver
        from telegram_assistant.folders import TelethonFolderBackend

        client = await manager.get_client()
        if not await client.is_user_authorized():
            raise RuntimeError(
                "Telethon session is not authorized; run "
                "`telegram-assistant auth` first."
            )
        return TelethonEntityResolver(client), TelethonFolderBackend(client)

    return config, manager, _open


def _access_rule_target(rule) -> dict[str, object]:
    """Summarise the target kind of an :class:`AccessRule` for display."""
    if rule.all:
        return {"kind": "all"}
    if rule.folder is not None:
        return {"kind": "folder", "folder": rule.folder}
    if rule.folder_id is not None:
        return {"kind": "folder_id", "folder_id": rule.folder_id}
    return {"kind": "chat", "chats": [str(ref) for ref in rule.chat_refs]}


@access_app.command("list")
def access_list(
    config_path: Path | None = typer.Option(  # noqa: B008
        None,
        "--config",
        "-c",
        help="Path to config.yml (defaults: ./data/config.yml, then ~/.config/telegram-assistant/config.yml).",
        exists=False,
    ),
) -> None:
    """Print the effective access rules from the loaded config.

    With ``telegram.access`` unset the policy is allow-all; otherwise each rule
    is listed with its target kind and the (independent) capabilities it grants.
    """
    config = _load_config_or_exit(config_path)
    access = config.telegram.access
    if access is None:
        payload: dict[str, object] = {
            "policy": "allow_all",
            "rules": [],
            "note": (
                "telegram.access is not set; every chat is allowed "
                "(read/write/delete)."
            ),
        }
        typer.echo(json.dumps(payload, sort_keys=True))
        return

    rules_out = [
        {
            "target": _access_rule_target(rule),
            "permissions": list(rule.effective_permissions),
        }
        for rule in access.rules
    ]
    payload = {
        "policy": "deny_by_default",
        "rules": rules_out,
        "rule_count": len(rules_out),
    }
    typer.echo(json.dumps(payload, sort_keys=True))


@access_app.command("check")
def access_check(
    entity: str = typer.Option(
        ...,
        "--entity",
        help="Entity reference (numeric id, @username, link, phone, or exact "
        "title) resolved via the shared resolver.",
    ),
    permission: str = typer.Option(
        "read",
        "--permission",
        help="Capability to check: read | write | delete.",
    ),
    config_path: Path | None = typer.Option(  # noqa: B008
        None,
        "--config",
        "-c",
        help="Path to config.yml (defaults: ./data/config.yml, then ~/.config/telegram-assistant/config.yml).",
        exists=False,
    ),
) -> None:
    """Resolve a chat and report whether the policy grants a permission.

    Exits 0 when granted, ``ACCESS_DENIED_EXIT_CODE`` (3) when denied, and 2
    when the entity cannot be resolved (or the permission is invalid).
    """
    from telegram_assistant.access.service import _PERMISSION_TO_LEVEL

    perm = permission.strip().lower()
    if perm not in _PERMISSION_TO_LEVEL:
        typer.echo(
            f"invalid --permission {permission!r}: expected read|write|delete",
            err=True,
        )
        raise typer.Exit(code=2)
    level = _PERMISSION_TO_LEVEL[perm]

    config, manager, open_backends = _build_access_resolver(config_path)

    async def _run():
        try:
            resolver, folder_backend = await open_backends()
            resolved = await resolver.resolve(entity)
            authorizer = _cli_authorizer(
                config, resolver=resolver, folder_backend=folder_backend
            )
            caps, matched = await authorizer.describe(resolved.chat_id)
            return resolved, caps, matched, level in caps
        finally:
            try:
                await manager.disconnect()
            except Exception:
                pass

    try:
        resolved, caps, matched, granted = asyncio.run(_run())
    except Exception as exc:
        _raise_for_access_or_entity_error(exc)
        typer.echo(f"access check failed: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    payload = {
        "entity": entity,
        "telegram_chat_id": resolved.chat_id,
        "permission": perm,
        "granted": granted,
        "granted_permissions": sorted(c.name.lower() for c in caps),
        "matched_rule": matched,
    }
    typer.echo(json.dumps(payload, sort_keys=True))
    if not granted:
        raise typer.Exit(code=ACCESS_DENIED_EXIT_CODE)


@access_app.command("add")
def access_add(
    entity: str | None = typer.Option(
        None,
        "--entity",
        help="Grant on a single chat (numeric id, @username, link, phone, "
        "or exact title).",
    ),
    folder: str | None = typer.Option(
        None,
        "--folder",
        help="Grant on every chat in this Telegram chat-folder.",
    ),
    all_chats: bool = typer.Option(
        False,
        "--all",
        help="Grant on every chat (wildcard baseline).",
    ),
    permission: str = typer.Option(
        "write",
        "--permission",
        help="Comma-separated capabilities to grant: read,write,delete.",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Print the resulting rule without writing the config.",
    ),
    config_path: Path | None = typer.Option(  # noqa: B008
        None,
        "--config",
        "-c",
        help="Path to config.yml (defaults: ./data/config.yml, then ~/.config/telegram-assistant/config.yml).",
        exists=False,
    ),
) -> None:
    """Append an access rule to the config file.

    Exactly one target (``--entity`` / ``--folder`` / ``--all``) must be given.
    The rule is validated (same model validator as the loader) before writing;
    the hot-reload watcher then applies it live. ``--dry-run`` prints the rule
    without touching the file. Note: adding the first rule to a config with no
    ``telegram.access`` block switches the policy from allow-all to
    deny-by-default.
    """
    import yaml

    from telegram_assistant.config import (
        load_config_from_text,
        resolve_config_path,
    )
    from telegram_assistant.config.models import AccessRule

    target_count = sum(
        [entity is not None, folder is not None, bool(all_chats)]
    )
    if target_count != 1:
        typer.echo(
            "exactly one of --entity, --folder, or --all must be supplied",
            err=True,
        )
        raise typer.Exit(code=2)

    perms = [p.strip().lower() for p in permission.split(",") if p.strip()]
    if not perms:
        typer.echo(
            "--permission must list at least one of read,write,delete", err=True
        )
        raise typer.Exit(code=2)

    rule_data: dict[str, object] = {"permissions": perms}
    if entity is not None:
        ref: str | int = entity
        stripped = entity[1:] if entity.startswith("-") else entity
        if stripped.isdigit():
            ref = int(entity)
        rule_data["chat"] = ref
    elif folder is not None:
        rule_data["folder"] = folder
    else:
        rule_data["all"] = True

    # Validate via the shared model validator (rejects bad target / permission).
    try:
        AccessRule.model_validate(rule_data)
    except Exception as exc:
        typer.echo(f"invalid access rule: {exc}", err=True)
        raise typer.Exit(code=2) from exc

    if dry_run:
        payload = {
            "status": "dry_run",
            "dry_run": True,
            "command": "access.add",
            "rule": rule_data,
        }
        typer.echo(json.dumps(payload, sort_keys=True))
        return

    target_path = Path(config_path) if config_path is not None else resolve_config_path()
    if target_path is None or not target_path.exists():
        typer.echo("no config file found to update", err=True)
        raise typer.Exit(code=2)

    raw = yaml.safe_load(target_path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        typer.echo(f"config at {target_path} is not a YAML mapping", err=True)
        raise typer.Exit(code=2)
    telegram = raw.setdefault("telegram", {})
    access = telegram.get("access")
    if access is None:
        access = {"rules": []}
        telegram["access"] = access
    access.setdefault("rules", [])
    access["rules"].append(rule_data)

    new_text = yaml.safe_dump(raw, sort_keys=False, allow_unicode=True)
    # Re-validate the whole resulting config before persisting.
    try:
        load_config_from_text(new_text, source=str(target_path))
    except ConfigError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc

    target_path.write_text(new_text, encoding="utf-8")
    payload = {
        "status": "ok",
        "command": "access.add",
        "rule": rule_data,
        "config_path": str(target_path),
        "rule_count": len(access["rules"]),
    }
    typer.echo(json.dumps(payload, sort_keys=True))


if __name__ == "__main__":  # pragma: no cover
    app()
