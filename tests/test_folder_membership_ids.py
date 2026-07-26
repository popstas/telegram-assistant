"""Unit tests for the fast folder-membership path (Task 1).

``TelethonFolderBackend.list_folder_chat_ids`` reads bare peer ids straight from
the ``InputPeer*`` objects in each dialog filter, without any ``get_entity``
round-trips, and the :class:`Authorizer` uses it (when present) to build its
folder-membership map. These tests exercise both with in-memory fakes so no
Telethon traffic is needed.
"""

from __future__ import annotations

from typing import Any

import pytest

from telegram_assistant.access import AccessDenied, AccessLevel, Authorizer
from telegram_assistant.config.models import AccessConfig, AccessRule
from telegram_assistant.folders import FolderChat, FolderChats, FolderSnapshot
from telegram_assistant.folders.telethon_backend import TelethonFolderBackend
from telegram_assistant.worker.queue import FloodWaitError

# ---------------------------------------------------------------------------
# Telethon peer / filter doubles
# ---------------------------------------------------------------------------


class _InputPeerChannel:
    def __init__(self, channel_id: int) -> None:
        self.channel_id = channel_id


class _InputPeerChat:
    def __init__(self, chat_id: int) -> None:
        self.chat_id = chat_id


class _InputPeerUser:
    def __init__(self, user_id: int) -> None:
        self.user_id = user_id


class _InputPeerEmpty:
    """A peer shape carrying none of the id attributes we read."""


class _Filter:
    def __init__(
        self,
        *,
        id: int | None,
        title: str | None,
        include_peers: list[Any] | None = None,
        pinned_peers: list[Any] | None = None,
    ) -> None:
        self.id = id
        self.title = title
        self.include_peers = include_peers or []
        self.pinned_peers = pinned_peers or []


class _DialogFilters:
    def __init__(self, filters: list[Any]) -> None:
        self.filters = filters


class _FakeClient:
    """Callable Telethon client double returning canned dialog filters."""

    def __init__(self, filters: list[Any], *, wrap: bool = True) -> None:
        self._result: Any = _DialogFilters(filters) if wrap else filters
        self.get_entity_calls = 0

    async def __call__(self, request: Any) -> Any:
        return self._result

    async def get_entity(self, peer: Any) -> Any:  # pragma: no cover - must not run
        self.get_entity_calls += 1
        raise AssertionError("list_folder_chat_ids must not call get_entity")


class _TelethonFloodWaitError(Exception):
    def __init__(self, seconds: int) -> None:
        super().__init__(f"flood wait {seconds}")
        self.seconds = seconds


_TelethonFloodWaitError.__name__ = "FloodWaitError"


class _FloodingClient:
    async def __call__(self, request: Any) -> Any:
        raise _TelethonFloodWaitError(7)


# ---------------------------------------------------------------------------
# Backend: list_folder_chat_ids
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_folder_chat_ids_reads_ids_without_get_entity() -> None:
    client = _FakeClient(
        [
            _Filter(
                id=1,
                title="Clients",
                pinned_peers=[_InputPeerChannel(10)],
                include_peers=[_InputPeerChat(11), _InputPeerUser(12)],
            )
        ]
    )
    backend = TelethonFolderBackend(client)

    result = await backend.list_folder_chat_ids()

    assert result == [
        FolderChats(folder_id=1, folder_name="Clients", chat_ids={10, 11, 12})
    ]
    assert client.get_entity_calls == 0


@pytest.mark.asyncio
async def test_list_folder_chat_ids_skips_default_and_unknown_peers() -> None:
    client = _FakeClient(
        [
            # DialogFilterDefault "All chats" — no id/title, skipped.
            _Filter(id=None, title=None, include_peers=[_InputPeerChannel(99)]),
            _Filter(
                id=2,
                title="Work",
                include_peers=[_InputPeerChannel(20), _InputPeerEmpty()],
            ),
        ]
    )
    backend = TelethonFolderBackend(client)

    result = await backend.list_folder_chat_ids()

    assert result == [
        FolderChats(folder_id=2, folder_name="Work", chat_ids={20})
    ]


@pytest.mark.asyncio
async def test_list_folder_chat_ids_handles_bare_list_result() -> None:
    # Older Telethon returns a plain list rather than a DialogFilters wrapper.
    client = _FakeClient(
        [_Filter(id=3, title="Solo", include_peers=[_InputPeerUser(30)])],
        wrap=False,
    )
    backend = TelethonFolderBackend(client)

    assert await backend.list_folder_chat_ids() == [
        FolderChats(folder_id=3, folder_name="Solo", chat_ids={30})
    ]


@pytest.mark.asyncio
async def test_same_title_folders_keep_separate_entries() -> None:
    # Regression (PR #17): Telegram allows two folders with the same title. A
    # title-keyed map dropped the first one, so its chats lost folder-rule
    # access. Both must survive as distinct id-keyed entries.
    client = _FakeClient(
        [
            _Filter(id=1, title="Clients", include_peers=[_InputPeerChannel(10)]),
            _Filter(id=2, title="Clients", include_peers=[_InputPeerChannel(20)]),
        ]
    )
    backend = TelethonFolderBackend(client)

    result = await backend.list_folder_chat_ids()

    assert result == [
        FolderChats(folder_id=1, folder_name="Clients", chat_ids={10}),
        FolderChats(folder_id=2, folder_name="Clients", chat_ids={20}),
    ]


@pytest.mark.asyncio
async def test_list_folder_chat_ids_translates_flood_wait() -> None:
    backend = TelethonFolderBackend(_FloodingClient())

    with pytest.raises(FloodWaitError):
        await backend.list_folder_chat_ids()


# ---------------------------------------------------------------------------
# Authorizer: fast path + fallback + normalisation
# ---------------------------------------------------------------------------


class _FastFolderBackend:
    """Exposes only ``list_folder_chat_ids`` (the fast membership surface)."""

    def __init__(self, folders: list[FolderChats]) -> None:
        self._folders = folders
        self.list_folders_calls = 0

    async def list_folder_chat_ids(self) -> list[FolderChats]:
        return [
            FolderChats(
                folder_id=f.folder_id,
                folder_name=f.folder_name,
                chat_ids=set(f.chat_ids),
            )
            for f in self._folders
        ]

    async def list_folders(self) -> list[FolderSnapshot]:  # pragma: no cover
        self.list_folders_calls += 1
        raise AssertionError("fast path must not fall back to list_folders")


class _LegacyFolderBackend:
    """Only implements the title-resolving ``list_folders`` surface."""

    def __init__(self, folders: list[FolderSnapshot]) -> None:
        self._folders = folders
        self.list_folders_calls = 0

    async def list_folders(self) -> list[FolderSnapshot]:
        self.list_folders_calls += 1
        return list(self._folders)


@pytest.mark.asyncio
async def test_authorizer_uses_fast_path_when_available() -> None:
    backend = _FastFolderBackend(
        [FolderChats(folder_id=1, folder_name="Clients", chat_ids={10, 11})]
    )
    auth = Authorizer(
        AccessConfig(rules=[AccessRule(folder="Clients", permission="write")]),
        folder_backend=backend,
    )

    await auth.require(10, AccessLevel.WRITE)
    await auth.require(11, AccessLevel.WRITE)
    with pytest.raises(AccessDenied):
        await auth.require(99, AccessLevel.WRITE)


@pytest.mark.asyncio
async def test_fast_path_normalises_marked_chat_ids() -> None:
    # Backend reports bare channel id; a request carrying the -100 marked form
    # must still match the same folder rule.
    backend = _FastFolderBackend(
        [FolderChats(folder_id=1, folder_name="Clients", chat_ids={1234567890})]
    )
    auth = Authorizer(
        AccessConfig(rules=[AccessRule(folder="Clients", permission="write")]),
        folder_backend=backend,
    )

    await auth.require(-1001234567890, AccessLevel.WRITE)


@pytest.mark.asyncio
async def test_authorizer_falls_back_to_list_folders() -> None:
    folder = FolderSnapshot(
        folder_id=1,
        folder_name="Clients",
        chats=[FolderChat(chat_id=10, title="A")],
    )
    backend = _LegacyFolderBackend([folder])
    auth = Authorizer(
        AccessConfig(rules=[AccessRule(folder="Clients", permission="write")]),
        folder_backend=backend,
    )

    await auth.require(10, AccessLevel.WRITE)
    assert backend.list_folders_calls == 1
    with pytest.raises(AccessDenied):
        await auth.require(99, AccessLevel.WRITE)


@pytest.mark.asyncio
async def test_name_rule_covers_both_same_named_folders() -> None:
    # The name-targeted rule unions every folder carrying that title, so chats
    # in either same-named folder keep their access.
    backend = _FastFolderBackend(
        [
            FolderChats(folder_id=1, folder_name="Clients", chat_ids={10}),
            FolderChats(folder_id=2, folder_name="Clients", chat_ids={20}),
        ]
    )
    auth = Authorizer(
        AccessConfig(rules=[AccessRule(folder="Clients", permission="write")]),
        folder_backend=backend,
    )

    await auth.require(10, AccessLevel.WRITE)
    await auth.require(20, AccessLevel.WRITE)


@pytest.mark.asyncio
async def test_list_folders_fallback_keeps_same_named_folders() -> None:
    backend = _LegacyFolderBackend(
        [
            FolderSnapshot(
                folder_id=1,
                folder_name="Clients",
                chats=[FolderChat(chat_id=10, title="A")],
            ),
            FolderSnapshot(
                folder_id=2,
                folder_name="Clients",
                chats=[FolderChat(chat_id=20, title="B")],
            ),
        ]
    )
    auth = Authorizer(
        AccessConfig(rules=[AccessRule(folder="Clients", permission="write")]),
        folder_backend=backend,
    )

    await auth.require(10, AccessLevel.WRITE)
    await auth.require(20, AccessLevel.WRITE)


@pytest.mark.asyncio
async def test_fast_path_propagates_flood_wait() -> None:
    class _FloodFastBackend:
        async def list_folder_chat_ids(self) -> list[FolderChats]:
            raise FloodWaitError(5.0)

    auth = Authorizer(
        AccessConfig(rules=[AccessRule(folder="Clients", permission="write")]),
        folder_backend=_FloodFastBackend(),
    )

    with pytest.raises(FloodWaitError):
        await auth.require(10, AccessLevel.WRITE)
