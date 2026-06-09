"""Plugin protocol and registry.

A :class:`Plugin` exposes a handful of optional hook points the core calls at
well-defined moments. Every hook has a neutral default behavior (empty postfix,
empty protected set, ``None`` first message, ``False`` task-sent) so a plugin
only implements what it cares about, and an empty registry is a perfect no-op.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from telegram_assistant.config.models import AppConfig


@runtime_checkable
class Plugin(Protocol):
    """Hook points an integration plugin may implement."""

    name: str

    def title_postfix(self) -> str:
        """Text appended to a new group's title (kept out of the idempotency key)."""

    def protected_accounts(self) -> set[str]:
        """Account references the ``--force`` member-removal guard must protect."""

    def topic_first_message(self, *, external_ref: int | str | None) -> str | None:
        """First message to post into a newly created topic, or ``None`` to defer."""

    def group_first_message(
        self, *, external_ref: int | str | None, members_added: Sequence[str]
    ) -> str | None:
        """Service message to send in a new group, or ``None`` if none applies.

        Pure (no I/O) so callers can preview it during ``--dry-run``.
        """

    async def after_group_create(
        self,
        *,
        backend: Any,
        chat_id: int,
        external_ref: int | str | None,
        members_added: Sequence[str],
        skipped: list[dict[str, Any]],
    ) -> bool:
        """Run post-create side effects (send the service message, clean up).

        Returns ``True`` if a service message was sent. Best-effort: failures
        are appended to ``skipped`` and never raise (the chat already exists).
        """

    async def after_topic_create(
        self,
        *,
        backend: Any,
        chat_id: int,
        topic_id: int,
        external_ref: int | str | None,
        skipped: list[dict[str, Any]],
    ) -> bool:
        """Run post-create side effects for a new topic (service message, cleanup).

        Mirrors :meth:`after_group_create` but scoped to a forum topic. Returns
        ``True`` if a service message was sent. Best-effort: failures are
        appended to ``skipped`` and never raise (the topic already exists).
        """


class PluginRegistry:
    """Aggregates active plugins behind one façade used by domain services."""

    def __init__(self, plugins: Sequence[Plugin] = ()) -> None:
        self._plugins: tuple[Plugin, ...] = tuple(plugins)

    @property
    def active(self) -> tuple[Plugin, ...]:
        return self._plugins

    def title_postfix(self) -> str:
        return "".join(p.title_postfix() for p in self._plugins)

    def protected_accounts(self) -> set[str]:
        out: set[str] = set()
        for p in self._plugins:
            out |= p.protected_accounts()
        return out

    def topic_first_message(self, *, external_ref: int | str | None) -> str | None:
        for p in self._plugins:
            msg = p.topic_first_message(external_ref=external_ref)
            if msg is not None:
                return msg
        return None

    def group_first_message(
        self, *, external_ref: int | str | None, members_added: Sequence[str]
    ) -> str | None:
        for p in self._plugins:
            msg = p.group_first_message(
                external_ref=external_ref, members_added=members_added
            )
            if msg is not None:
                return msg
        return None

    async def after_group_create(
        self,
        *,
        backend: Any,
        chat_id: int,
        external_ref: int | str | None,
        members_added: Sequence[str],
        skipped: list[dict[str, Any]],
    ) -> bool:
        sent = False
        for p in self._plugins:
            if await p.after_group_create(
                backend=backend,
                chat_id=chat_id,
                external_ref=external_ref,
                members_added=members_added,
                skipped=skipped,
            ):
                sent = True
        return sent

    async def after_topic_create(
        self,
        *,
        backend: Any,
        chat_id: int,
        topic_id: int,
        external_ref: int | str | None,
        skipped: list[dict[str, Any]],
    ) -> bool:
        sent = False
        for p in self._plugins:
            # The hook is optional; skip plugins that don't implement it so a
            # partial plugin (or a test fake) never breaks topic creation.
            hook = getattr(p, "after_topic_create", None)
            if hook is None:
                continue
            if await hook(
                backend=backend,
                chat_id=chat_id,
                topic_id=topic_id,
                external_ref=external_ref,
                skipped=skipped,
            ):
                sent = True
        return sent


def build_registry(config: AppConfig) -> PluginRegistry:
    """Construct the active plugin registry from app config.

    Concrete plugins are imported lazily so the core never imports a plugin
    module unless it is enabled.
    """
    plugins: list[Plugin] = []
    if config.plugins.planfix.enabled:
        from telegram_assistant.plugins.planfix import PlanfixPlugin

        plugins.append(PlanfixPlugin(config.plugins.planfix))
    return PluginRegistry(plugins)
