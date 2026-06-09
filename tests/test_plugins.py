"""Tests for the optional plugin layer and the Planfix plugin.

These lock in the core contract: with the Planfix plugin OFF the core has zero
Planfix behavior (no ``/task`` message, no welcome cleanup, no ``@planfix_bot``
protection), while ``external_ref`` still anchors idempotency generically. With
the plugin ON the original behavior is restored.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from telegram_assistant.config import load_config_from_text
from telegram_assistant.config.models import PlanfixPluginConfig
from telegram_assistant.groups.service import GroupCreateRequest, create_group
from telegram_assistant.members.service import protected_user_set
from telegram_assistant.persistence import OperationStore
from telegram_assistant.plugins import PluginRegistry, build_registry
from telegram_assistant.plugins.planfix import PlanfixPlugin

# ---------------------------------------------------------------------------
# Minimal fakes
# ---------------------------------------------------------------------------


class FakeGroupBackend:
    def __init__(self, *, recent_messages: list[dict[str, Any]] | None = None) -> None:
        self._recent = recent_messages or []
        self.messages: list[tuple[int, str]] = []
        self.deleted: list[int] = []

    async def create_supergroup(self, *, title: str, about: str | None, enable_topics: bool) -> int:
        return -100123

    async def add_member(self, *, chat_id: int, user: str) -> None:
        pass

    async def promote_admin(self, *, chat_id: int, user: str) -> None:
        pass

    async def create_invite_link(self, *, chat_id: int) -> str:
        return "https://t.me/+x"

    async def send_message(self, *, chat_id: int, text: str) -> int:
        self.messages.append((chat_id, text))
        return 42

    async def get_recent_messages(self, *, chat_id: int, limit: int) -> list[dict[str, Any]]:
        return list(self._recent)

    async def delete_messages(self, *, chat_id: int, message_ids: Any) -> None:
        self.deleted.extend(int(m) for m in message_ids)

    async def set_topics_layout(self, *, chat_id: int, tabs: bool) -> None:
        pass

    async def get_topics_layout(self, *, chat_id: int) -> bool:
        return False

    async def chat_exists(self, *, chat_id: int) -> bool:
        return True

    async def set_default_permissions(
        self, *, chat_id: int, allow_create_topics: bool, allow_pin_messages: bool
    ) -> None:
        pass


class FakeTopicBackend:
    """Topic backend recording sends, message scans, and deletions.

    Hands out sequential message ids so the surviving first message and the
    plugin ``/task`` message are distinguishable.
    """

    def __init__(
        self,
        *,
        recent_messages: list[dict[str, Any]] | None = None,
        next_id: int = 200,
    ) -> None:
        self._recent = recent_messages or []
        self._next_id = next_id
        self.messages: list[tuple[int, str, int | None]] = []
        self.deleted: list[int] = []
        self.recent_calls: list[tuple[int, int, int | None]] = []

    async def send_message(
        self, *, chat_id: int, text: str, topic_id: int | None = None
    ) -> int:
        self.messages.append((chat_id, text, topic_id))
        mid = self._next_id
        self._next_id += 1
        return mid

    async def get_recent_messages(
        self, *, chat_id: int, limit: int, topic_id: int | None = None
    ) -> list[dict[str, Any]]:
        self.recent_calls.append((chat_id, limit, topic_id))
        return list(self._recent)

    async def delete_messages(self, *, chat_id: int, message_ids: Any) -> None:
        self.deleted.extend(int(m) for m in message_ids)


@pytest.fixture()
def store(tmp_path: Path) -> OperationStore:
    return OperationStore(tmp_path / "state.db")


def _config(minimal_config_yaml: str):
    return load_config_from_text(minimal_config_yaml)


def _planfix(**overrides: Any) -> PlanfixPlugin:
    return PlanfixPlugin(PlanfixPluginConfig(enabled=True, **overrides))


# ---------------------------------------------------------------------------
# Plugin unit behavior
# ---------------------------------------------------------------------------


def test_planfix_topic_first_message() -> None:
    p = _planfix()
    assert p.topic_first_message(external_ref=55) == "/task 55"
    assert p.topic_first_message(external_ref=None) is None
    assert p.topic_first_message(external_ref="  ") is None


def test_planfix_group_first_message_requires_bot_member() -> None:
    p = _planfix()
    assert (
        p.group_first_message(external_ref=7, members_added=["@planfix_bot", "@alice"])
        == "/task 7"
    )
    assert p.group_first_message(external_ref=7, members_added=["@alice"]) is None
    assert p.group_first_message(external_ref=None, members_added=["@planfix_bot"]) is None


def test_planfix_protected_accounts_and_postfix() -> None:
    p = _planfix(group_title_postfix=" [client]")
    assert p.protected_accounts() == {"@planfix_bot"}
    assert p.title_postfix() == " [client]"


def test_empty_registry_is_a_noop() -> None:
    reg = PluginRegistry()
    assert reg.title_postfix() == ""
    assert reg.protected_accounts() == set()
    assert reg.topic_first_message(external_ref=1) is None
    assert reg.group_first_message(external_ref=1, members_added=["@planfix_bot"]) is None


def test_build_registry_toggles_on_config(minimal_config_yaml: str) -> None:
    cfg_on = _config(minimal_config_yaml)
    assert cfg_on.plugins.planfix.enabled is True  # conftest enables it
    assert len(build_registry(cfg_on).active) == 1

    cfg_off = _config(minimal_config_yaml)
    cfg_off.plugins.planfix.enabled = False
    assert build_registry(cfg_off).active == ()


# ---------------------------------------------------------------------------
# Core contract: plugin OFF means zero Planfix behavior
# ---------------------------------------------------------------------------


async def test_create_group_plugin_off_sends_no_task_message(
    minimal_config_yaml: str, store: OperationStore
) -> None:
    config = _config(minimal_config_yaml)
    backend = FakeGroupBackend()
    request = GroupCreateRequest(
        title="Acme", external_ref=42, members=["@planfix_bot"], skip_folder=True
    )

    # No plugins passed → empty registry → no /task message, no cleanup.
    result, op = await create_group(
        backend=backend,
        folder_backend=None,
        store=store,
        config=config.telegram,
        request=request,
        plugins=PluginRegistry(),
    )

    assert result.external_ref == 42  # external_ref still recorded
    assert result.task_message_sent is False
    assert backend.messages == []  # no /task command
    assert backend.deleted == []  # no cleanup


async def test_create_group_external_ref_anchors_idempotency_without_plugin(
    minimal_config_yaml: str, store: OperationStore
) -> None:
    config = _config(minimal_config_yaml)

    first, op1 = await create_group(
        backend=FakeGroupBackend(),
        folder_backend=None,
        store=store,
        config=config.telegram,
        request=GroupCreateRequest(title="First name", external_ref=7, skip_folder=True),
        plugins=PluginRegistry(),
    )
    # Same external_ref, different title → replays (key is the external_ref).
    second, op2 = await create_group(
        backend=FakeGroupBackend(),
        folder_backend=None,
        store=store,
        config=config.telegram,
        request=GroupCreateRequest(title="Other name", external_ref=7, skip_folder=True),
        plugins=PluginRegistry(),
    )
    assert first.replayed is False
    assert second.replayed is True
    assert op1.id == op2.id


async def test_create_group_plugin_on_sends_task_and_cleans_up(
    minimal_config_yaml: str, store: OperationStore
) -> None:
    config = _config(minimal_config_yaml)  # conftest: planfix enabled, cleanup on
    recent = [
        {"id": 40, "sender_username": "planfix_bot", "reply_to_msg_id": None, "text": "welcome"},
        {"id": 42, "sender_username": "me", "reply_to_msg_id": None, "text": "/task 9"},
        {"id": 43, "sender_username": "planfix_bot", "reply_to_msg_id": 42, "text": "ok"},
    ]
    backend = FakeGroupBackend(recent_messages=recent)
    request = GroupCreateRequest(
        title="Acme", external_ref=9, members=["@planfix_bot"], skip_folder=True
    )

    result, _ = await create_group(
        backend=backend,
        folder_backend=None,
        store=store,
        config=config.telegram,
        request=request,
        plugins=build_registry(config),
    )

    assert result.task_message_sent is True
    assert backend.messages == [(-100123, "/task 9")]
    # welcome (40), command (42), reply (43) all deleted.
    assert set(backend.deleted) == {40, 42, 43}


# ---------------------------------------------------------------------------
# Planfix topic hook: post /task as a second message + scoped cleanup
# ---------------------------------------------------------------------------


async def test_planfix_after_topic_create_posts_task_and_cleans_up() -> None:
    p = _planfix(cleanup_messages=True, task_reply_wait_seconds=0)
    # The /task message is the first send → id 200. The bot replies to 200.
    recent = [
        {"id": 198, "sender_username": "planfix_bot", "reply_to_msg_id": None, "text": "welcome"},
        {"id": 200, "sender_username": "me", "reply_to_msg_id": 555, "text": "/task 9"},
        {"id": 201, "sender_username": "planfix_bot", "reply_to_msg_id": 200, "text": "ok"},
    ]
    backend = FakeTopicBackend(recent_messages=recent, next_id=200)
    skipped: list[dict[str, Any]] = []

    sent = await p.after_topic_create(
        backend=backend, chat_id=-100, topic_id=555, external_ref=9, skipped=skipped
    )

    assert sent is True
    # `/task` posted into the topic (topic_id threaded through).
    assert backend.messages == [(-100, "/task 9", 555)]
    # Cleanup scan is scoped to the topic.
    assert backend.recent_calls and backend.recent_calls[0][2] == 555
    # welcome (198), command (200), reply (201) deleted; the topic-name message
    # is the core's responsibility and never reaches this hook.
    assert set(backend.deleted) == {198, 200, 201}
    assert skipped == []


async def test_planfix_after_topic_create_no_cleanup_when_disabled() -> None:
    p = _planfix(cleanup_messages=False)
    backend = FakeTopicBackend(next_id=200)
    skipped: list[dict[str, Any]] = []

    sent = await p.after_topic_create(
        backend=backend, chat_id=-100, topic_id=555, external_ref=9, skipped=skipped
    )

    assert sent is True
    assert backend.messages == [(-100, "/task 9", 555)]
    assert backend.deleted == []
    assert backend.recent_calls == []  # cleanup never polled


async def test_planfix_after_topic_create_skips_without_ref() -> None:
    p = _planfix(cleanup_messages=True)
    backend = FakeTopicBackend()

    sent = await p.after_topic_create(
        backend=backend, chat_id=-100, topic_id=555, external_ref=None, skipped=[]
    )

    assert sent is False
    assert backend.messages == []
    assert backend.deleted == []


async def test_planfix_after_topic_create_missing_reply_still_deletes_task() -> None:
    p = _planfix(cleanup_messages=True, task_reply_wait_seconds=0)
    recent = [
        {"id": 198, "sender_username": "planfix_bot", "reply_to_msg_id": None, "text": "welcome"},
        {"id": 200, "sender_username": "me", "reply_to_msg_id": 555, "text": "/task 9"},
    ]
    backend = FakeTopicBackend(recent_messages=recent, next_id=200)
    skipped: list[dict[str, Any]] = []

    sent = await p.after_topic_create(
        backend=backend, chat_id=-100, topic_id=555, external_ref=9, skipped=skipped
    )

    assert sent is True
    # The bot reply never arrived, but the welcome (198) and our command (200)
    # are still deleted; the missing reply is recorded.
    assert set(backend.deleted) == {198, 200}
    assert any(s["step"] == "cleanup_bot_reply" for s in skipped)


async def test_registry_after_topic_create_skips_plugin_without_hook() -> None:
    class BarePlugin:
        name = "bare"

        def title_postfix(self) -> str:
            return ""

        def protected_accounts(self) -> set[str]:
            return set()

        def topic_first_message(self, *, external_ref: Any) -> str | None:
            return None

        def group_first_message(self, *, external_ref: Any, members_added: Any) -> str | None:
            return None

        async def after_group_create(self, **_kwargs: Any) -> bool:
            return False

        # Intentionally no `after_topic_create`.

    reg = PluginRegistry([BarePlugin()])
    sent = await reg.after_topic_create(
        backend=object(), chat_id=1, topic_id=2, external_ref=3, skipped=[]
    )
    assert sent is False


# ---------------------------------------------------------------------------
# Protected accounts gate on the plugin
# ---------------------------------------------------------------------------


def test_protected_user_set_excludes_bot_when_plugin_off(minimal_config_yaml: str) -> None:
    config = _config(minimal_config_yaml)
    # Clear reserves so the only possible source of bot protection is the plugin.
    config.telegram.reserve_members = []
    config.telegram.reserve_admins = []
    protected = protected_user_set(config=config.telegram, plugins=PluginRegistry())
    assert not any("planfix_bot" in p for p in protected)


def test_protected_user_set_includes_bot_when_plugin_on(minimal_config_yaml: str) -> None:
    config = _config(minimal_config_yaml)
    config.telegram.reserve_members = []
    config.telegram.reserve_admins = []
    protected = protected_user_set(
        config=config.telegram, plugins=build_registry(config)
    )
    assert any("planfix_bot" in p for p in protected)
