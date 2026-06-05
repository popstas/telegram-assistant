"""Tests for Task 7 — group creation (domain, HTTP, CLI).

The fake :class:`FakeGroupBackend` and :class:`FakeFolderBackend` keep the
tests free of Telethon. They record every call so we can assert the order in
which the domain workflow drives the backends.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from telegram_assistant.cli import main as cli_main
from telegram_assistant.config import load_config_from_text
from telegram_assistant.folders import (
    FolderChat,
    FolderSnapshot,
)
from telegram_assistant.groups import (
    GroupCreateFailed,
    GroupCreateNeedsReview,
    GroupCreateRequest,
    create_group,
)
from telegram_assistant.http_api import create_app
from telegram_assistant.persistence import (
    OperationStatus,
    OperationStore,
)

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeGroupBackend:
    """In-memory :class:`GroupBackend` recording every call."""

    def __init__(
        self,
        *,
        chat_id: int = -100123,
        fail_on_add: set[str] | None = None,
        fail_on_promote: set[str] | None = None,
        invite_link: str | None = "https://t.me/+invitehash",
        set_layout_error: Exception | None = None,
        set_permissions_error: Exception | None = None,
        chat_exists_result: bool = True,
        chat_exists_error: Exception | None = None,
        recent_messages: list[dict[str, Any]] | None = None,
        get_messages_error: Exception | None = None,
        delete_messages_error: Exception | None = None,
        sent_message_id: int = 42,
    ) -> None:
        self._chat_id = chat_id
        self._fail_on_add = fail_on_add or set()
        self._fail_on_promote = fail_on_promote or set()
        self._invite_link = invite_link
        self._set_layout_error = set_layout_error
        self._set_permissions_error = set_permissions_error
        self._chat_exists_result = chat_exists_result
        self._chat_exists_error = chat_exists_error
        self._recent_messages = recent_messages or []
        self._get_messages_error = get_messages_error
        self._delete_messages_error = delete_messages_error
        self._sent_message_id = sent_message_id
        self.chat_exists_calls: list[int] = []
        self.created: list[dict[str, Any]] = []
        self.added: list[str] = []
        self.promoted: list[str] = []
        self.messages: list[tuple[int, str]] = []
        self.invite_calls = 0
        self.layout_calls: list[tuple[int, bool]] = []
        self.permission_calls: list[tuple[int, bool, bool]] = []
        self.get_messages_calls: list[tuple[int, int]] = []
        self.deleted_messages: list[int] = []
        self.current_tabs: bool = False

    async def create_supergroup(
        self,
        *,
        title: str,
        about: str | None,
        enable_topics: bool,
    ) -> int:
        self.created.append(
            {"title": title, "about": about, "enable_topics": enable_topics}
        )
        return self._chat_id

    async def add_member(self, *, chat_id: int, user: str) -> None:
        if user in self._fail_on_add:
            raise RuntimeError(f"privacy restricted: {user}")
        self.added.append(user)

    async def promote_admin(self, *, chat_id: int, user: str) -> None:
        if user in self._fail_on_promote:
            raise RuntimeError(f"cannot promote: {user}")
        self.promoted.append(user)

    async def create_invite_link(self, *, chat_id: int) -> str:
        self.invite_calls += 1
        if self._invite_link is None:
            raise RuntimeError("invite-link disabled in this test")
        return self._invite_link

    async def send_message(self, *, chat_id: int, text: str) -> int:
        self.messages.append((chat_id, text))
        return self._sent_message_id

    async def get_recent_messages(
        self, *, chat_id: int, limit: int
    ) -> list[dict[str, Any]]:
        self.get_messages_calls.append((chat_id, limit))
        if self._get_messages_error is not None:
            raise self._get_messages_error
        return list(self._recent_messages)

    async def delete_messages(
        self, *, chat_id: int, message_ids: Any
    ) -> None:
        if self._delete_messages_error is not None:
            raise self._delete_messages_error
        self.deleted_messages.extend(int(m) for m in message_ids)

    async def set_topics_layout(self, *, chat_id: int, tabs: bool) -> None:
        if self._set_layout_error is not None:
            raise self._set_layout_error
        self.layout_calls.append((chat_id, tabs))
        self.current_tabs = tabs

    async def get_topics_layout(self, *, chat_id: int) -> bool:
        return self.current_tabs

    async def chat_exists(self, *, chat_id: int) -> bool:
        self.chat_exists_calls.append(chat_id)
        if self._chat_exists_error is not None:
            raise self._chat_exists_error
        return self._chat_exists_result

    async def set_default_permissions(
        self,
        *,
        chat_id: int,
        allow_create_topics: bool,
        allow_pin_messages: bool,
    ) -> None:
        if self._set_permissions_error is not None:
            raise self._set_permissions_error
        self.permission_calls.append(
            (chat_id, allow_create_topics, allow_pin_messages)
        )


class FakeFolderBackend:
    def __init__(
        self,
        folders: list[FolderSnapshot] | None = None,
        *,
        add_should_fail: bool = False,
    ) -> None:
        self._folders = folders if folders is not None else [_default_folder()]
        self._add_should_fail = add_should_fail
        self.added: list[tuple[int, int]] = []

    async def list_folders(self) -> list[FolderSnapshot]:
        return [
            FolderSnapshot(
                folder_id=f.folder_id,
                folder_name=f.folder_name,
                chats=list(f.chats),
            )
            for f in self._folders
        ]

    async def resolve_chat(self, chat_ref: str | int) -> FolderChat:
        if isinstance(chat_ref, int):
            return FolderChat(chat_id=chat_ref, title=f"Chat {chat_ref}")
        raise LookupError(f"unknown chat ref {chat_ref!r}")

    async def add_chat_to_folder(self, folder_id: int, chat_id: int) -> None:
        if self._add_should_fail:
            raise RuntimeError("telegram refused folder mutation")
        self.added.append((folder_id, chat_id))


def _default_folder() -> FolderSnapshot:
    return FolderSnapshot(
        folder_id=2,
        folder_name="Planfix clients",
        chats=[],
    )


# ---------------------------------------------------------------------------
# Domain tests
# ---------------------------------------------------------------------------


@pytest.fixture()
def store(tmp_path: Path) -> OperationStore:
    return OperationStore(tmp_path / "state.db")


def _config(minimal_config_yaml: str):
    return load_config_from_text(minimal_config_yaml)


async def test_create_group_happy_path(
    minimal_config_yaml: str, store: OperationStore
) -> None:
    config = _config(minimal_config_yaml)
    backend = FakeGroupBackend()
    folder_backend = FakeFolderBackend()
    request = GroupCreateRequest(
        title="Acme",
        planfix_task_id=42,
        admins=["@alice"],
        members=["@bob"],
    )

    result, op = await create_group(
        backend=backend,
        folder_backend=folder_backend,
        store=store,
        config=config.telegram,
        request=request,
    )

    assert op.status is OperationStatus.COMPLETED
    assert result.telegram_chat_id == -100123
    assert result.title == "Acme"
    assert result.planfix_task_id == 42
    assert result.topics_enabled is True
    assert result.invite_link == "https://t.me/+invitehash"
    assert result.folder_name == "Planfix clients"
    assert result.folder_id == 2
    # @bob (member), @planfix_bot (reserve_member), @alice (admin),
    # @reserve_account (reserve_admin)
    assert result.members_added == ["@bob", "@planfix_bot", "@alice", "@reserve_account"]
    assert result.admins_promoted == ["@alice", "@reserve_account"]
    assert result.task_message_sent is True
    assert backend.messages == [(-100123, "/task 42")]
    assert folder_backend.added == [(2, -100123)]
    assert result.replayed is False


async def test_create_group_skip_reserve_uses_only_explicit_lists(
    minimal_config_yaml: str, store: OperationStore
) -> None:
    config = _config(minimal_config_yaml)
    backend = FakeGroupBackend()
    folder_backend = FakeFolderBackend()
    request = GroupCreateRequest(
        title="Beta",
        admins=["@alice"],
        members=["@bob"],
        skip_reserve=True,
    )

    result, _ = await create_group(
        backend=backend,
        folder_backend=folder_backend,
        store=store,
        config=config.telegram,
        request=request,
    )

    assert "@planfix_bot" not in result.members_added
    assert "@reserve_account" not in result.members_added
    assert result.members_added == ["@bob", "@alice"]
    assert result.admins_promoted == ["@alice"]
    assert result.task_message_sent is False


async def test_create_group_idempotent_by_planfix_task_id(
    minimal_config_yaml: str, store: OperationStore
) -> None:
    config = _config(minimal_config_yaml)
    backend1 = FakeGroupBackend(chat_id=-1)
    folder_backend = FakeFolderBackend()
    request = GroupCreateRequest(
        title="Acme",
        planfix_task_id=42,
        admins=[],
        members=[],
        skip_reserve=True,
    )
    first, op1 = await create_group(
        backend=backend1,
        folder_backend=folder_backend,
        store=store,
        config=config.telegram,
        request=request,
    )
    assert first.replayed is False
    assert op1.status is OperationStatus.COMPLETED

    # Re-call with a different backend instance — replay must NOT touch it.
    backend2 = FakeGroupBackend(chat_id=-2)
    second, op2 = await create_group(
        backend=backend2,
        folder_backend=FakeFolderBackend(),
        store=store,
        config=config.telegram,
        request=request,
    )
    assert second.replayed is True
    assert second.telegram_chat_id == -1
    assert op2.id == op1.id
    assert backend2.created == []


async def test_create_group_idempotent_by_title_when_no_task_id(
    minimal_config_yaml: str, store: OperationStore
) -> None:
    config = _config(minimal_config_yaml)
    backend1 = FakeGroupBackend(chat_id=-7)
    folder_backend = FakeFolderBackend()
    request = GroupCreateRequest(
        title="Beta",
        skip_reserve=True,
    )

    first, _ = await create_group(
        backend=backend1,
        folder_backend=folder_backend,
        store=store,
        config=config.telegram,
        request=request,
    )

    backend2 = FakeGroupBackend(chat_id=-99)
    second, _ = await create_group(
        backend=backend2,
        folder_backend=FakeFolderBackend(),
        store=store,
        config=config.telegram,
        request=request,
    )
    assert second.telegram_chat_id == -7
    assert backend2.created == []
    assert second.replayed is True


async def test_create_group_applies_title_postfix(
    minimal_config_yaml: str, store: OperationStore
) -> None:
    config = _config(minimal_config_yaml)
    config.telegram.defaults.group_title_postfix = " [client]"
    backend = FakeGroupBackend()
    folder_backend = FakeFolderBackend()
    request = GroupCreateRequest(title="Acme", skip_reserve=True)

    result, _ = await create_group(
        backend=backend,
        folder_backend=folder_backend,
        store=store,
        config=config.telegram,
        request=request,
    )

    # The postfix lands on the Telegram title and on the returned result.
    assert backend.created[0]["title"] == "Acme [client]"
    assert result.title == "Acme [client]"


async def test_title_postfix_does_not_change_idempotency_key(
    minimal_config_yaml: str, store: OperationStore
) -> None:
    config = _config(minimal_config_yaml)
    config.telegram.defaults.group_title_postfix = " [client]"
    backend1 = FakeGroupBackend(chat_id=-7)
    request = GroupCreateRequest(title="Beta", skip_reserve=True)

    first, _ = await create_group(
        backend=backend1,
        folder_backend=FakeFolderBackend(),
        store=store,
        config=config.telegram,
        request=request,
    )
    assert first.replayed is False

    # A second call with the same raw title replays — the key is on the raw
    # title, never the postfixed one.
    backend2 = FakeGroupBackend(chat_id=-99)
    second, _ = await create_group(
        backend=backend2,
        folder_backend=FakeFolderBackend(),
        store=store,
        config=config.telegram,
        request=request,
    )
    assert second.replayed is True
    assert second.telegram_chat_id == -7
    assert backend2.created == []


async def test_empty_title_postfix_is_noop(
    minimal_config_yaml: str, store: OperationStore
) -> None:
    config = _config(minimal_config_yaml)
    assert config.telegram.defaults.group_title_postfix == ""
    backend = FakeGroupBackend()
    request = GroupCreateRequest(title="Acme", skip_reserve=True)

    result, _ = await create_group(
        backend=backend,
        folder_backend=FakeFolderBackend(),
        store=store,
        config=config.telegram,
        request=request,
    )

    assert backend.created[0]["title"] == "Acme"
    assert result.title == "Acme"


async def test_create_group_missing_folder_marks_needs_review(
    minimal_config_yaml: str, store: OperationStore
) -> None:
    config = _config(minimal_config_yaml)
    backend = FakeGroupBackend()
    # Folder backend without the configured folder present.
    folder_backend = FakeFolderBackend(folders=[])
    request = GroupCreateRequest(
        title="Acme",
        planfix_task_id=1,
        skip_reserve=True,
    )

    with pytest.raises(GroupCreateNeedsReview):
        await create_group(
            backend=backend,
            folder_backend=folder_backend,
            store=store,
            config=config.telegram,
            request=request,
        )
    # Replay should now also surface needs_review.
    with pytest.raises(GroupCreateNeedsReview):
        await create_group(
            backend=backend,
            folder_backend=folder_backend,
            store=store,
            config=config.telegram,
            request=request,
        )


async def test_create_group_folder_mutation_failure_marks_needs_review(
    minimal_config_yaml: str, store: OperationStore
) -> None:
    config = _config(minimal_config_yaml)
    backend = FakeGroupBackend()
    folder_backend = FakeFolderBackend(add_should_fail=True)
    request = GroupCreateRequest(
        title="Acme",
        planfix_task_id=2,
        skip_reserve=True,
    )

    with pytest.raises(GroupCreateNeedsReview):
        await create_group(
            backend=backend,
            folder_backend=folder_backend,
            store=store,
            config=config.telegram,
            request=request,
        )


async def test_create_group_task_message_only_when_bot_present(
    minimal_config_yaml: str, store: OperationStore
) -> None:
    config = _config(minimal_config_yaml)
    backend = FakeGroupBackend()
    folder_backend = FakeFolderBackend()
    # planfix_task_id set, but skip_reserve removes @planfix_bot — so no task
    # message should be sent.
    request = GroupCreateRequest(
        title="Gamma",
        planfix_task_id=77,
        admins=["@alice"],
        skip_reserve=True,
    )
    result, _ = await create_group(
        backend=backend,
        folder_backend=folder_backend,
        store=store,
        config=config.telegram,
        request=request,
    )
    assert result.task_message_sent is False
    assert backend.messages == []


async def test_create_group_skips_blank_member_references(
    minimal_config_yaml: str, store: OperationStore
) -> None:
    config = _config(minimal_config_yaml)
    backend = FakeGroupBackend()
    folder_backend = FakeFolderBackend()
    # A stray empty string and a whitespace-only entry must be dropped instead
    # of crashing the create or reaching the backend.
    request = GroupCreateRequest(
        title="Blanks",
        admins=["@alice", "   "],
        members=["@bob", "", "@carol"],
        skip_reserve=True,
    )

    result, op = await create_group(
        backend=backend,
        folder_backend=folder_backend,
        store=store,
        config=config.telegram,
        request=request,
    )

    assert op.status is OperationStatus.COMPLETED
    assert result.members_added == ["@bob", "@carol", "@alice"]
    assert result.admins_promoted == ["@alice"]
    assert backend.added == ["@bob", "@carol", "@alice"]
    # Blanks are dropped before reaching the backend, so they never produce a
    # skipped entry.
    assert result.skipped == []


async def test_create_group_failure_records_failed(
    minimal_config_yaml: str, store: OperationStore
) -> None:
    config = _config(minimal_config_yaml)

    class _BrokenBackend(FakeGroupBackend):
        async def create_supergroup(self, *, title, about, enable_topics):  # type: ignore[override]
            raise RuntimeError("server overloaded")

    backend = _BrokenBackend()
    folder_backend = FakeFolderBackend()
    request = GroupCreateRequest(title="Delta", skip_reserve=True)

    with pytest.raises(RuntimeError):
        await create_group(
            backend=backend,
            folder_backend=folder_backend,
            store=store,
            config=config.telegram,
            request=request,
        )
    with pytest.raises(GroupCreateFailed):
        await create_group(
            backend=FakeGroupBackend(),
            folder_backend=FakeFolderBackend(),
            store=store,
            config=config.telegram,
            request=request,
        )


def _config_with_layout(minimal_config_yaml: str, layout: str) -> Any:
    """Return a TelegramConfig with topics_layout set explicitly."""
    body = minimal_config_yaml.replace(
        "create_invite_link: true",
        f"create_invite_link: true\n    topics_layout: {layout}",
    )
    return load_config_from_text(body)


async def test_create_group_applies_tabs_layout_after_create(
    minimal_config_yaml: str, store: OperationStore
) -> None:
    config = _config_with_layout(minimal_config_yaml, "tabs")
    backend = FakeGroupBackend()
    folder_backend = FakeFolderBackend()
    request = GroupCreateRequest(title="Acme", planfix_task_id=1, skip_reserve=True)

    result, op = await create_group(
        backend=backend,
        folder_backend=folder_backend,
        store=store,
        config=config.telegram,
        request=request,
    )

    assert op.status is OperationStatus.COMPLETED
    assert result.topics_enabled is True
    assert backend.layout_calls == [(-100123, True)]


async def test_create_group_applies_list_layout_after_create(
    minimal_config_yaml: str, store: OperationStore
) -> None:
    config = _config_with_layout(minimal_config_yaml, "list")
    backend = FakeGroupBackend()
    folder_backend = FakeFolderBackend()
    request = GroupCreateRequest(title="Acme", planfix_task_id=2, skip_reserve=True)

    result, op = await create_group(
        backend=backend,
        folder_backend=folder_backend,
        store=store,
        config=config.telegram,
        request=request,
    )

    assert op.status is OperationStatus.COMPLETED
    assert result.topics_enabled is True
    assert backend.layout_calls == [(-100123, False)]


async def test_create_group_skips_layout_when_topics_disabled(
    minimal_config_yaml: str, store: OperationStore
) -> None:
    config = _config_with_layout(minimal_config_yaml, "tabs")
    backend = FakeGroupBackend()
    folder_backend = FakeFolderBackend()
    request = GroupCreateRequest(
        title="Acme",
        planfix_task_id=3,
        skip_reserve=True,
        enable_topics=False,
    )

    result, op = await create_group(
        backend=backend,
        folder_backend=folder_backend,
        store=store,
        config=config.telegram,
        request=request,
    )

    assert op.status is OperationStatus.COMPLETED
    assert result.topics_enabled is False
    assert backend.layout_calls == []


async def test_create_group_per_request_layout_overrides_config(
    minimal_config_yaml: str, store: OperationStore
) -> None:
    # Config default is "list"; the per-request override asks for "tabs".
    config = _config_with_layout(minimal_config_yaml, "list")
    backend = FakeGroupBackend()
    folder_backend = FakeFolderBackend()
    request = GroupCreateRequest(
        title="Acme",
        planfix_task_id=10,
        skip_reserve=True,
        topics_layout="tabs",
    )

    result, op = await create_group(
        backend=backend,
        folder_backend=folder_backend,
        store=store,
        config=config.telegram,
        request=request,
    )

    assert op.status is OperationStatus.COMPLETED
    assert backend.layout_calls == [(-100123, True)]


async def test_create_group_layout_falls_back_to_config_default(
    minimal_config_yaml: str, store: OperationStore
) -> None:
    # No per-request layout → use the config default ("tabs" here).
    config = _config_with_layout(minimal_config_yaml, "tabs")
    backend = FakeGroupBackend()
    folder_backend = FakeFolderBackend()
    request = GroupCreateRequest(
        title="Acme",
        planfix_task_id=11,
        skip_reserve=True,
        topics_layout=None,
    )

    result, op = await create_group(
        backend=backend,
        folder_backend=folder_backend,
        store=store,
        config=config.telegram,
        request=request,
    )

    assert op.status is OperationStatus.COMPLETED
    assert backend.layout_calls == [(-100123, True)]


async def test_create_group_per_request_layout_skipped_when_topics_disabled(
    minimal_config_yaml: str, store: OperationStore
) -> None:
    config = _config_with_layout(minimal_config_yaml, "list")
    backend = FakeGroupBackend()
    folder_backend = FakeFolderBackend()
    request = GroupCreateRequest(
        title="Acme",
        planfix_task_id=12,
        skip_reserve=True,
        enable_topics=False,
        topics_layout="tabs",
    )

    result, op = await create_group(
        backend=backend,
        folder_backend=folder_backend,
        store=store,
        config=config.telegram,
        request=request,
    )

    assert op.status is OperationStatus.COMPLETED
    assert result.topics_enabled is False
    assert backend.layout_calls == []


async def test_create_group_layout_failure_does_not_fail_create(
    minimal_config_yaml: str,
    store: OperationStore,
    caplog: pytest.LogCaptureFixture,
) -> None:
    import logging as _logging

    config = _config_with_layout(minimal_config_yaml, "tabs")
    backend = FakeGroupBackend(
        set_layout_error=RuntimeError("chat is not a forum"),
    )
    folder_backend = FakeFolderBackend()
    request = GroupCreateRequest(title="Acme", planfix_task_id=4, skip_reserve=True)

    with caplog.at_level(
        _logging.WARNING, logger="telegram_assistant.groups.service"
    ):
        result, op = await create_group(
            backend=backend,
            folder_backend=folder_backend,
            store=store,
            config=config.telegram,
            request=request,
        )

    assert op.status is OperationStatus.COMPLETED
    assert result.topics_enabled is True
    assert backend.layout_calls == []
    warnings = [r for r in caplog.records if "post-create set_topics_layout" in r.message]
    assert warnings, "expected a warning about the post-create layout failure"
    assert "chat is not a forum" in warnings[0].getMessage()


async def test_create_group_layout_flood_wait_promotes_to_needs_review(
    minimal_config_yaml: str,
    store: OperationStore,
) -> None:
    """FLOOD_WAIT from the post-create layout call must surface as needs_review,
    not be silently dropped to a warning log. The chat is already live, but the
    operator still needs to know Telegram is throttling this account.
    """
    from telegram_assistant.worker.queue import FloodWaitError

    config = _config_with_layout(minimal_config_yaml, "tabs")
    backend = FakeGroupBackend(
        set_layout_error=FloodWaitError(seconds=42),
    )
    folder_backend = FakeFolderBackend()
    request = GroupCreateRequest(title="Acme", planfix_task_id=5, skip_reserve=True)

    with pytest.raises(GroupCreateNeedsReview):
        await create_group(
            backend=backend,
            folder_backend=folder_backend,
            store=store,
            config=config.telegram,
            request=request,
        )


async def test_create_group_sets_default_permissions(
    minimal_config_yaml: str, store: OperationStore
) -> None:
    config = _config(minimal_config_yaml)
    backend = FakeGroupBackend()
    folder_backend = FakeFolderBackend()
    request = GroupCreateRequest(title="Acme", planfix_task_id=1, skip_reserve=True)

    result, op = await create_group(
        backend=backend,
        folder_backend=folder_backend,
        store=store,
        config=config.telegram,
        request=request,
    )

    assert op.status is OperationStatus.COMPLETED
    # Defaults grant create_topics and pin_messages to everyone.
    assert backend.permission_calls == [(-100123, True, True)]


async def test_create_group_respects_configured_permissions(
    minimal_config_yaml: str, store: OperationStore
) -> None:
    config = _config(minimal_config_yaml)
    config.telegram.defaults.default_member_permissions.create_topics = False
    config.telegram.defaults.default_member_permissions.pin_messages = True
    backend = FakeGroupBackend()
    folder_backend = FakeFolderBackend()
    request = GroupCreateRequest(title="Acme", planfix_task_id=2, skip_reserve=True)

    result, op = await create_group(
        backend=backend,
        folder_backend=folder_backend,
        store=store,
        config=config.telegram,
        request=request,
    )

    assert op.status is OperationStatus.COMPLETED
    assert backend.permission_calls == [(-100123, False, True)]


async def test_create_group_permissions_failure_recorded_in_skipped(
    minimal_config_yaml: str, store: OperationStore
) -> None:
    config = _config(minimal_config_yaml)
    backend = FakeGroupBackend(
        set_permissions_error=RuntimeError("cannot edit default rights"),
    )
    folder_backend = FakeFolderBackend()
    request = GroupCreateRequest(title="Acme", planfix_task_id=3, skip_reserve=True)

    result, op = await create_group(
        backend=backend,
        folder_backend=folder_backend,
        store=store,
        config=config.telegram,
        request=request,
    )

    # The create still succeeds; the permission failure lands in skipped.
    assert op.status is OperationStatus.COMPLETED
    assert backend.permission_calls == []
    perm_skips = [s for s in result.skipped if s["step"] == "set_default_permissions"]
    assert perm_skips, "expected the permission failure to be recorded in skipped"
    assert "cannot edit default rights" in perm_skips[0]["reason"]


async def test_create_group_permissions_flood_wait_promotes_to_needs_review(
    minimal_config_yaml: str, store: OperationStore
) -> None:
    from telegram_assistant.worker.queue import FloodWaitError

    config = _config(minimal_config_yaml)
    backend = FakeGroupBackend(
        set_permissions_error=FloodWaitError(seconds=30),
    )
    folder_backend = FakeFolderBackend()
    request = GroupCreateRequest(title="Acme", planfix_task_id=4, skip_reserve=True)

    with pytest.raises(GroupCreateNeedsReview):
        await create_group(
            backend=backend,
            folder_backend=folder_backend,
            store=store,
            config=config.telegram,
            request=request,
        )


async def test_cleanup_deletes_welcome_command_and_reply(
    minimal_config_yaml: str, store: OperationStore
) -> None:
    config = _config(minimal_config_yaml)
    # send_message returns id 42; the bot welcome is 40 and its reply is 43.
    recent = [
        {"id": 43, "sender_username": "planfix_bot", "reply_to_msg_id": 42, "text": "ok"},
        {"id": 42, "sender_username": None, "reply_to_msg_id": None, "text": "/task 1"},
        {"id": 40, "sender_username": "planfix_bot", "reply_to_msg_id": None, "text": "hi"},
    ]
    backend = FakeGroupBackend(recent_messages=recent, sent_message_id=42)
    folder_backend = FakeFolderBackend()
    request = GroupCreateRequest(title="Acme", planfix_task_id=1, skip_reserve=False)

    result, op = await create_group(
        backend=backend,
        folder_backend=folder_backend,
        store=store,
        config=config.telegram,
        request=request,
    )

    assert op.status is OperationStatus.COMPLETED
    assert result.task_message_sent is True
    # Welcome (40), command (42), and reply (43) are all deleted.
    assert sorted(backend.deleted_messages) == [40, 42, 43]
    assert not any(s["step"] == "cleanup_bot_reply" for s in result.skipped)


async def test_cleanup_without_reply_still_deletes_welcome_and_command(
    minimal_config_yaml: str, store: OperationStore
) -> None:
    config = _config(minimal_config_yaml)
    # No bot reply present — only the welcome (40) and our command (42).
    recent = [
        {"id": 42, "sender_username": None, "reply_to_msg_id": None, "text": "/task 2"},
        {"id": 40, "sender_username": "planfix_bot", "reply_to_msg_id": None, "text": "hi"},
    ]
    backend = FakeGroupBackend(recent_messages=recent, sent_message_id=42)
    folder_backend = FakeFolderBackend()
    request = GroupCreateRequest(title="Acme", planfix_task_id=2, skip_reserve=False)

    result, op = await create_group(
        backend=backend,
        folder_backend=folder_backend,
        store=store,
        config=config.telegram,
        request=request,
    )

    assert op.status is OperationStatus.COMPLETED
    assert sorted(backend.deleted_messages) == [40, 42]
    missing = [s for s in result.skipped if s["step"] == "cleanup_bot_reply"]
    assert missing, "expected the missing bot reply to be recorded in skipped"


async def test_cleanup_disabled_is_noop(
    minimal_config_yaml: str, store: OperationStore
) -> None:
    config = _config(minimal_config_yaml)
    config.telegram.defaults.cleanup_planfix_messages = False
    recent = [
        {"id": 40, "sender_username": "planfix_bot", "reply_to_msg_id": None, "text": "hi"},
    ]
    backend = FakeGroupBackend(recent_messages=recent, sent_message_id=42)
    folder_backend = FakeFolderBackend()
    request = GroupCreateRequest(title="Acme", planfix_task_id=3, skip_reserve=False)

    result, op = await create_group(
        backend=backend,
        folder_backend=folder_backend,
        store=store,
        config=config.telegram,
        request=request,
    )

    assert op.status is OperationStatus.COMPLETED
    # Cleanup never ran: no message reads and no deletions.
    assert backend.get_messages_calls == []
    assert backend.deleted_messages == []


async def test_cleanup_skipped_when_no_task_message(
    minimal_config_yaml: str, store: OperationStore
) -> None:
    config = _config(minimal_config_yaml)
    backend = FakeGroupBackend(sent_message_id=42)
    folder_backend = FakeFolderBackend()
    # No planfix_task_id → no /task message → nothing to clean up.
    request = GroupCreateRequest(title="Acme", skip_reserve=False)

    result, op = await create_group(
        backend=backend,
        folder_backend=folder_backend,
        store=store,
        config=config.telegram,
        request=request,
    )

    assert op.status is OperationStatus.COMPLETED
    assert result.task_message_sent is False
    assert backend.get_messages_calls == []
    assert backend.deleted_messages == []


async def test_cleanup_delete_failure_recorded_in_skipped(
    minimal_config_yaml: str, store: OperationStore
) -> None:
    config = _config(minimal_config_yaml)
    recent = [
        {"id": 42, "sender_username": None, "reply_to_msg_id": None, "text": "/task 4"},
        {"id": 40, "sender_username": "planfix_bot", "reply_to_msg_id": None, "text": "hi"},
    ]
    backend = FakeGroupBackend(
        recent_messages=recent,
        sent_message_id=42,
        delete_messages_error=RuntimeError("cannot delete"),
    )
    folder_backend = FakeFolderBackend()
    request = GroupCreateRequest(title="Acme", planfix_task_id=4, skip_reserve=False)

    result, op = await create_group(
        backend=backend,
        folder_backend=folder_backend,
        store=store,
        config=config.telegram,
        request=request,
    )

    # A delete failure must not fail the create — it lands in skipped.
    assert op.status is OperationStatus.COMPLETED
    delete_skips = [s for s in result.skipped if s["step"] == "cleanup_delete"]
    assert delete_skips
    assert "cannot delete" in delete_skips[0]["reason"]


async def test_create_group_skip_folder_bypasses_folder_resolution(
    minimal_config_yaml: str, store: OperationStore
) -> None:
    config = _config(minimal_config_yaml)
    backend = FakeGroupBackend()
    folder_backend = FakeFolderBackend(folders=[])
    request = GroupCreateRequest(
        title="Acme",
        planfix_task_id=11,
        skip_reserve=True,
        skip_folder=True,
    )
    result, op = await create_group(
        backend=backend,
        folder_backend=folder_backend,
        store=store,
        config=config.telegram,
        request=request,
    )
    assert op.status is OperationStatus.COMPLETED
    assert result.folder_id is None
    assert folder_backend.added == []


async def test_replay_returns_saved_result_when_chat_exists(
    minimal_config_yaml: str, store: OperationStore
) -> None:
    config = _config(minimal_config_yaml)
    backend1 = FakeGroupBackend(chat_id=-555)
    request = GroupCreateRequest(title="Acme", planfix_task_id=70, skip_reserve=True)

    first, _ = await create_group(
        backend=backend1,
        folder_backend=FakeFolderBackend(),
        store=store,
        config=config.telegram,
        request=request,
    )
    assert first.replayed is False

    # The chat still exists, so the replay returns the saved chat id and never
    # touches the backend's create path.
    backend2 = FakeGroupBackend(chat_id=-999, chat_exists_result=True)
    second, _ = await create_group(
        backend=backend2,
        folder_backend=FakeFolderBackend(),
        store=store,
        config=config.telegram,
        request=request,
    )
    assert second.replayed is True
    assert second.telegram_chat_id == -555
    assert backend2.chat_exists_calls == [-555]
    assert backend2.created == []


async def test_stale_completed_op_dropped_and_recreated_when_chat_gone(
    minimal_config_yaml: str, store: OperationStore
) -> None:
    config = _config(minimal_config_yaml)
    backend1 = FakeGroupBackend(chat_id=-555)
    request = GroupCreateRequest(title="Acme", planfix_task_id=71, skip_reserve=True)

    first, op1 = await create_group(
        backend=backend1,
        folder_backend=FakeFolderBackend(),
        store=store,
        config=config.telegram,
        request=request,
    )
    assert first.telegram_chat_id == -555

    # The saved chat was deleted out-of-band; the next call must drop the stale
    # operation and create a brand-new group with a fresh chat id.
    backend2 = FakeGroupBackend(chat_id=-777, chat_exists_result=False)
    second, op2 = await create_group(
        backend=backend2,
        folder_backend=FakeFolderBackend(),
        store=store,
        config=config.telegram,
        request=request,
    )
    assert second.replayed is False
    assert second.telegram_chat_id == -777
    assert backend2.chat_exists_calls == [-555]
    assert backend2.created, "expected a fresh create when the chat was gone"
    assert op2.status is OperationStatus.COMPLETED
    # A new operation row replaced the stale one.
    assert op2.id != op1.id


async def test_flood_wait_during_existence_check_needs_review(
    minimal_config_yaml: str, store: OperationStore
) -> None:
    from telegram_assistant.worker.queue import FloodWaitError

    config = _config(minimal_config_yaml)
    backend1 = FakeGroupBackend(chat_id=-555)
    request = GroupCreateRequest(title="Acme", planfix_task_id=72, skip_reserve=True)

    await create_group(
        backend=backend1,
        folder_backend=FakeFolderBackend(),
        store=store,
        config=config.telegram,
        request=request,
    )

    # A throttle during the existence check is ambiguous — surface needs_review
    # rather than silently re-creating a possibly-live chat.
    backend2 = FakeGroupBackend(
        chat_id=-777, chat_exists_error=FloodWaitError(seconds=30)
    )
    with pytest.raises(GroupCreateNeedsReview):
        await create_group(
            backend=backend2,
            folder_backend=FakeFolderBackend(),
            store=store,
            config=config.telegram,
            request=request,
        )
    assert backend2.created == []
    # The original completed operation is still intact (not deleted).
    op = store.find_by_idempotency_key("group_create:planfix_task_id=72")
    assert op is not None
    assert op.status is OperationStatus.COMPLETED


def test_delete_operation_removes_row_and_index(
    minimal_config_yaml: str, tmp_path: Path
) -> None:
    from telegram_assistant.persistence import idempotency

    store = OperationStore(tmp_path / "del.db")
    begin = store.begin_operation(
        operation_type=idempotency.GROUP_CREATE,
        idempotency_key="group_create:title:Acme",
        request_payload={"title": "Acme"},
    )
    op_id = begin.operation.id
    store.complete_operation(op_id, {"telegram_chat_id": -1})

    store.delete_operation(op_id)

    assert store.find_by_idempotency_key("group_create:title:Acme") is None
    # The idempotency key is free again — a fresh begin creates a new row.
    again = store.begin_operation(
        operation_type=idempotency.GROUP_CREATE,
        idempotency_key="group_create:title:Acme",
        request_payload={"title": "Acme"},
    )
    assert again.created is True
    assert again.operation.id != op_id


# ---------------------------------------------------------------------------
# HTTP tests
# ---------------------------------------------------------------------------


def _http_client(
    minimal_config_yaml: str,
    backend: FakeGroupBackend,
    folder_backend: FakeFolderBackend | None = None,
    store: OperationStore | None = None,
) -> TestClient:
    config = load_config_from_text(minimal_config_yaml)
    folder_backend = folder_backend if folder_backend is not None else FakeFolderBackend()
    if store is None:
        # An in-memory-ish store is fine for HTTP tests too. We point it at a
        # tmp file via tempfile so the same store handles the entire lifetime
        # of the TestClient.
        import tempfile

        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".db")
        tmp.close()
        store = OperationStore(Path(tmp.name))
    app = create_app(
        config,
        session_manager=None,
        folder_backend_factory=lambda _request: folder_backend,
        group_backend_factory=lambda _request: backend,
        operation_store=store,
    )
    return TestClient(app)


def test_http_create_group_happy_path(minimal_config_yaml: str) -> None:
    backend = FakeGroupBackend()
    client = _http_client(minimal_config_yaml, backend)
    resp = client.post(
        "/telegram/groups",
        json={
            "title": "Acme",
            "planfix_task_id": 42,
            "admins": ["@alice"],
            "members": ["@bob"],
        },
        headers={"Authorization": "Bearer secret_token"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["telegram_chat_id"] == -100123
    assert body["folder_name"] == "Planfix clients"
    assert body["task_message_sent"] is True
    assert body["operation_status"] == "completed"
    assert "operation_id" in body


def test_http_create_group_replay_returns_same_chat_id(
    minimal_config_yaml: str,
) -> None:
    import tempfile

    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".db")
    tmp.close()
    store = OperationStore(Path(tmp.name))

    backend1 = FakeGroupBackend(chat_id=-1)
    client1 = _http_client(minimal_config_yaml, backend1, store=store)
    r1 = client1.post(
        "/telegram/groups",
        json={"title": "Acme", "planfix_task_id": 42},
        headers={"Authorization": "Bearer secret_token"},
    )
    assert r1.status_code == 200, r1.text

    backend2 = FakeGroupBackend(chat_id=-2)
    client2 = _http_client(minimal_config_yaml, backend2, store=store)
    r2 = client2.post(
        "/telegram/groups",
        json={"title": "Acme", "planfix_task_id": 42},
        headers={"Authorization": "Bearer secret_token"},
    )
    assert r2.status_code == 200, r2.text
    body = r2.json()
    assert body["telegram_chat_id"] == -1
    assert body["replayed"] is True
    # backend2 was never touched.
    assert backend2.created == []


def test_http_create_group_requires_auth(minimal_config_yaml: str) -> None:
    backend = FakeGroupBackend()
    client = _http_client(minimal_config_yaml, backend)
    resp = client.post(
        "/telegram/groups",
        json={"title": "X"},
    )
    assert resp.status_code == 401


def test_http_create_group_missing_folder_returns_502(
    minimal_config_yaml: str,
) -> None:
    backend = FakeGroupBackend()
    folder_backend = FakeFolderBackend(folders=[])
    client = _http_client(minimal_config_yaml, backend, folder_backend)
    resp = client.post(
        "/telegram/groups",
        json={"title": "Lonely", "planfix_task_id": 5},
        headers={"Authorization": "Bearer secret_token"},
    )
    assert resp.status_code == 502
    assert resp.json()["detail"]["error"] == "needs_review"


# ---------------------------------------------------------------------------
# CLI tests
# ---------------------------------------------------------------------------


def _write_config(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "config.yml"
    p.write_text(body)
    return p


def _patch_cli_backends(
    monkeypatch: pytest.MonkeyPatch,
    backend: FakeGroupBackend,
    folder_backend: FakeFolderBackend,
    store: OperationStore,
) -> None:
    class _FakeManager:
        async def disconnect(self) -> None:
            return None

    def _factory(config_path: Path | None) -> Any:
        from telegram_assistant.config import load_config

        config = load_config(config_path)

        async def _open() -> Any:
            return backend, folder_backend

        return config, _FakeManager(), store, _open

    monkeypatch.setattr(cli_main, "_build_group_backends", _factory)


def test_cli_groups_create_happy_path(
    minimal_config_yaml: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_file = _write_config(tmp_path, minimal_config_yaml)
    backend = FakeGroupBackend()
    folder_backend = FakeFolderBackend()
    store = OperationStore(tmp_path / "state.db")
    _patch_cli_backends(monkeypatch, backend, folder_backend, store)

    runner = CliRunner()
    result = runner.invoke(
        cli_main.app,
        [
            "groups",
            "create",
            "--title",
            "Acme",
            "--planfix-task-id",
            "42",
            "--admin",
            "@alice",
            "--member",
            "@bob",
            "--config",
            str(config_file),
        ],
    )
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload["telegram_chat_id"] == -100123
    assert payload["task_message_sent"] is True
    # @planfix_bot is in the reserves so it was added via the configured default.
    assert "@planfix_bot" in payload["members_added"]


def test_cli_groups_create_no_reserve_skips_configured_reserves(
    minimal_config_yaml: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_file = _write_config(tmp_path, minimal_config_yaml)
    backend = FakeGroupBackend()
    folder_backend = FakeFolderBackend()
    store = OperationStore(tmp_path / "state.db")
    _patch_cli_backends(monkeypatch, backend, folder_backend, store)

    runner = CliRunner()
    result = runner.invoke(
        cli_main.app,
        [
            "groups",
            "create",
            "--title",
            "Beta",
            "--no-reserve",
            "--config",
            str(config_file),
        ],
    )
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert "@planfix_bot" not in payload["members_added"]
    assert "@reserve_account" not in payload["members_added"]


def test_cli_groups_create_extra_reserve_admin_adds_on_top(
    minimal_config_yaml: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_file = _write_config(tmp_path, minimal_config_yaml)
    backend = FakeGroupBackend()
    folder_backend = FakeFolderBackend()
    store = OperationStore(tmp_path / "state.db")
    _patch_cli_backends(monkeypatch, backend, folder_backend, store)

    runner = CliRunner()
    result = runner.invoke(
        cli_main.app,
        [
            "groups",
            "create",
            "--title",
            "Gamma",
            "--reserve-admin",
            "@extra_admin",
            "--config",
            str(config_file),
        ],
    )
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    # Configured reserve_account is still there, and so is the explicit extra.
    assert "@reserve_account" in payload["admins_promoted"]
    assert "@extra_admin" in payload["admins_promoted"]


def test_cli_groups_create_missing_folder_exit_2(
    minimal_config_yaml: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_file = _write_config(tmp_path, minimal_config_yaml)
    backend = FakeGroupBackend()
    folder_backend = FakeFolderBackend(folders=[])
    store = OperationStore(tmp_path / "state.db")
    _patch_cli_backends(monkeypatch, backend, folder_backend, store)

    runner = CliRunner()
    result = runner.invoke(
        cli_main.app,
        [
            "groups",
            "create",
            "--title",
            "Lonely",
            "--planfix-task-id",
            "5",
            "--config",
            str(config_file),
        ],
    )
    assert result.exit_code == 2
