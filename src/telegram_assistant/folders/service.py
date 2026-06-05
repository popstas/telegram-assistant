"""Folder-domain functions shared by HTTP, CLI, and the worker.

The functions in this module never auto-create a folder. Missing folders raise
:class:`FolderNotFoundError`; ``folder_id`` cross-check mismatches raise
:class:`FolderIdMismatchError`. Per-peer folder failures (e.g. the underlying
``UpdateDialogFilterRequest`` raising mid-batch) raise
:class:`FolderPeerFailureError`, which the worker layer turns into a
``needs_review`` outcome rather than a silent success.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from telegram_assistant.access.service import AccessLevel, Authorizer
from telegram_assistant.worker.queue import NeedsReviewError


class FolderError(RuntimeError):
    """Base class for folder-resolution failures."""


class FolderNotFoundError(FolderError):
    """The named folder does not exist in Telegram's folder list."""


class FolderIdMismatchError(FolderError):
    """The folder exists but its ``folder_id`` differs from the configured value."""


class ChatNotFoundError(FolderError):
    """No chat with the requested name exists inside the folder."""


class AmbiguousChatNameError(FolderError):
    """More than one chat in the folder shares the requested name."""

    def __init__(
        self,
        *,
        chat_name: str,
        folder_name: str,
        matches: list[int],
    ) -> None:
        super().__init__(
            f"chat name {chat_name!r} matches {len(matches)} chats in folder "
            f"{folder_name!r}: {matches!r}"
        )
        self.chat_name = chat_name
        self.folder_name = folder_name
        self.matches = list(matches)


class FolderPeerFailureError(NeedsReviewError):
    """Adding the chat to the folder failed in a way we can't auto-retry."""


@dataclass(frozen=True)
class FolderChat:
    """A single chat surfaced inside a folder."""

    chat_id: int
    title: str


@dataclass
class FolderSnapshot:
    """Read-model for a Telegram folder and its current chat list."""

    folder_id: int
    folder_name: str
    chats: list[FolderChat] = field(default_factory=list)

    @property
    def chats_count(self) -> int:
        return len(self.chats)

    def to_dict(self) -> dict[str, Any]:
        return {
            "folder_id": self.folder_id,
            "folder_name": self.folder_name,
            "chats_count": self.chats_count,
            "chats": [
                {"chat_id": c.chat_id, "title": c.title} for c in self.chats
            ],
        }


class FolderBackend(Protocol):
    """Abstraction over the Telethon-specific calls these helpers depend on.

    A production implementation lives in :mod:`telethon_backend`; tests inject
    fakes so they never touch Telethon types directly.
    """

    async def list_folders(self) -> list[FolderSnapshot]:
        ...

    async def resolve_chat(self, chat_ref: str | int) -> FolderChat:
        ...

    async def add_chat_to_folder(self, folder_id: int, chat_id: int) -> None:
        ...


async def resolve_folder(
    backend: FolderBackend,
    *,
    folder_name: str,
    folder_id: int | None = None,
) -> FolderSnapshot:
    """Return the folder whose name matches ``folder_name``.

    Cross-checks ``folder_id`` when supplied: even a single-name match must
    line up with the configured id, otherwise we refuse to act on a stale
    folder reference.
    """
    snapshots = await backend.list_folders()
    matches = [s for s in snapshots if s.folder_name == folder_name]
    if not matches:
        raise FolderNotFoundError(
            f"folder {folder_name!r} not found in Telegram folder list"
        )
    if len(matches) > 1:
        if folder_id is None:
            raise FolderNotFoundError(
                f"folder {folder_name!r} matches {len(matches)} folders; "
                "set telegram.default_chat_folder.folder_id to disambiguate"
            )
        for snapshot in matches:
            if snapshot.folder_id == folder_id:
                return snapshot
        raise FolderIdMismatchError(
            f"no folder named {folder_name!r} has id {folder_id}; "
            f"present ids: {[s.folder_id for s in matches]!r}"
        )
    snapshot = matches[0]
    if folder_id is not None and snapshot.folder_id != folder_id:
        raise FolderIdMismatchError(
            f"folder {folder_name!r} has id {snapshot.folder_id}, not {folder_id}"
        )
    return snapshot


async def inspect_folder(
    backend: FolderBackend,
    *,
    folder_name: str,
    folder_id: int | None = None,
) -> FolderSnapshot:
    """Read-only view of a folder. Thin alias kept for surface symmetry."""
    return await resolve_folder(
        backend, folder_name=folder_name, folder_id=folder_id
    )


async def resolve_chat_in_folder(
    backend: FolderBackend,
    *,
    folder_name: str,
    chat_name: str,
    folder_id: int | None = None,
) -> FolderChat:
    """Map a ``--chat-name`` reference to a single chat inside the folder.

    Ambiguous matches fail loudly with the list of candidate chat ids so the
    operator can disambiguate, rather than picking one silently.
    """
    snapshot = await resolve_folder(
        backend, folder_name=folder_name, folder_id=folder_id
    )
    matches = [c for c in snapshot.chats if c.title == chat_name]
    if not matches:
        raise ChatNotFoundError(
            f"chat {chat_name!r} not found in folder {folder_name!r}"
        )
    if len(matches) > 1:
        raise AmbiguousChatNameError(
            chat_name=chat_name,
            folder_name=folder_name,
            matches=[c.chat_id for c in matches],
        )
    return matches[0]


async def add_chat_to_folder(
    backend: FolderBackend,
    *,
    folder_name: str,
    chat_ref: str | int,
    folder_id: int | None = None,
    authorizer: Authorizer | None = None,
) -> dict[str, Any]:
    """Move ``chat_ref`` into ``folder_name``.

    Returns a serialisable result describing the outcome. If the chat is
    already in the folder, returns ``already_in_folder=True`` without calling
    the backend mutation — this keeps the call idempotent. Backend errors
    during the mutation surface as :class:`FolderPeerFailureError` so the
    worker layer marks the operation ``needs_review`` instead of treating it
    as a silent success.
    """
    snapshot = await resolve_folder(
        backend, folder_name=folder_name, folder_id=folder_id
    )
    chat = await backend.resolve_chat(chat_ref)
    # Moving a chat into a folder mutates the chat — gate WRITE on the resolved
    # chat. Checked after resolution so we authorize the real numeric id.
    if authorizer is not None:
        await authorizer.require(chat.chat_id, AccessLevel.WRITE)
    already = any(c.chat_id == chat.chat_id for c in snapshot.chats)
    if already:
        return {
            "folder_id": snapshot.folder_id,
            "folder_name": snapshot.folder_name,
            "chat_id": chat.chat_id,
            "title": chat.title,
            "already_in_folder": True,
        }
    try:
        await backend.add_chat_to_folder(snapshot.folder_id, chat.chat_id)
    except FolderError:
        raise
    except Exception as exc:
        raise FolderPeerFailureError(
            f"failed to add chat {chat.chat_id} to folder {folder_name!r}: {exc}"
        ) from exc
    return {
        "folder_id": snapshot.folder_id,
        "folder_name": snapshot.folder_name,
        "chat_id": chat.chat_id,
        "title": chat.title,
        "already_in_folder": False,
    }
