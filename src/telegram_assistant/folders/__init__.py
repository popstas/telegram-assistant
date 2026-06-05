"""Chat folder resolution and folder-membership operations."""

from telegram_assistant.folders.service import (
    AmbiguousChatNameError,
    ChatNotFoundError,
    FolderBackend,
    FolderChat,
    FolderError,
    FolderIdMismatchError,
    FolderNotFoundError,
    FolderPeerFailureError,
    FolderSnapshot,
    add_chat_to_folder,
    inspect_folder,
    resolve_chat_in_folder,
    resolve_folder,
)
from telegram_assistant.folders.telethon_backend import (
    TelethonFolderBackend,
)

__all__ = [
    "AmbiguousChatNameError",
    "ChatNotFoundError",
    "FolderBackend",
    "FolderChat",
    "FolderError",
    "FolderIdMismatchError",
    "FolderNotFoundError",
    "FolderPeerFailureError",
    "FolderSnapshot",
    "TelethonFolderBackend",
    "add_chat_to_folder",
    "inspect_folder",
    "resolve_chat_in_folder",
    "resolve_folder",
]
