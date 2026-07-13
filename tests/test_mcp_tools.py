"""Tests for the telegram_ FastMCP tools."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from telegram_assistant.config import load_config_from_text
from telegram_assistant.folders import FolderChat, FolderSnapshot
from telegram_assistant.http_api import create_app
from telegram_assistant.messages import RecentMessage
from telegram_assistant.persistence import OperationStore
from telegram_assistant.topics import TopicSummary
from tests.test_mcp_mount import (
    FakeGoogleOidcProvider,
    FakeSessionManager,
    _enabled_mcp_yaml,
    _initialize_payload,
    _mcp_headers,
    _mint_token,
)


class FakeReadBackend:
    def __init__(self, messages: list[RecentMessage]) -> None:
        self._messages = messages
        self.calls: list[dict[str, int]] = []

    async def get_recent_messages(
        self, *, chat_id: int, limit: int = 5
    ) -> list[RecentMessage]:
        self.calls.append({"chat_id": chat_id, "limit": limit})
        return self._messages[:limit]


class FakeMessageBackend:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def send_message(
        self,
        *,
        chat_id: int,
        text: str,
        topic_id: int | None = None,
        files: tuple[str, ...] = (),
        schedule_at: object | None = None,
    ) -> int:
        self.sent.append(
            {
                "chat_id": chat_id,
                "text": text,
                "topic_id": topic_id,
                "files": files,
                "schedule_at": schedule_at,
            }
        )
        return 777


class FakeLayoutBackend:
    def __init__(self, *, forum_tabs: bool = False) -> None:
        self.forum_tabs = forum_tabs
        self.get_calls: list[int] = []
        self.set_calls: list[tuple[int, bool]] = []
        self.renamed: list[tuple[int, str]] = []

    async def get_topics_layout(self, *, chat_id: int) -> bool:
        self.get_calls.append(chat_id)
        return self.forum_tabs

    async def set_topics_layout(self, *, chat_id: int, tabs: bool) -> None:
        self.set_calls.append((chat_id, tabs))
        self.forum_tabs = tabs

    async def set_title(self, *, chat_id: int, title: str) -> None:
        self.renamed.append((chat_id, title))


class FakeTopicBackend:
    def __init__(self) -> None:
        self.created: list[dict[str, Any]] = []
        self.closed: list[dict[str, int]] = []
        self.renamed: list[dict[str, Any]] = []
        self.sent: list[dict[str, Any]] = []
        self.topics = [
            TopicSummary(topic_id=42, title="Kickoff", closed=False),
        ]

    async def create_topic(self, *, chat_id: int, name: str) -> int:
        topic_id = 1000 + len(self.created)
        self.created.append({"chat_id": chat_id, "name": name, "topic_id": topic_id})
        return topic_id

    async def send_message(
        self, *, chat_id: int, text: str, topic_id: int | None = None
    ) -> int:
        self.sent.append({"chat_id": chat_id, "text": text, "topic_id": topic_id})
        return 2000 + len(self.sent)

    async def close_topic(self, *, chat_id: int, topic_id: int) -> None:
        self.closed.append({"chat_id": chat_id, "topic_id": topic_id})

    async def rename_topic(
        self, *, chat_id: int, topic_id: int, title: str
    ) -> None:
        self.renamed.append(
            {"chat_id": chat_id, "topic_id": topic_id, "title": title}
        )

    async def list_topics(self, *, chat_id: int) -> list[TopicSummary]:
        return list(self.topics)


class FakeMemberBackend:
    def __init__(self) -> None:
        self.added: list[dict[str, Any]] = []
        self.banned: list[dict[str, Any]] = []
        self.unbanned: list[dict[str, Any]] = []

    async def add_member(self, *, chat_id: int, user: str) -> None:
        self.added.append({"chat_id": chat_id, "user": user})

    async def promote_admin(self, *, chat_id: int, user: str) -> None:
        return None

    async def ban_member(self, *, chat_id: int, user: str) -> None:
        self.banned.append({"chat_id": chat_id, "user": user})

    async def unban_member(self, *, chat_id: int, user: str) -> None:
        self.unbanned.append({"chat_id": chat_id, "user": user})


class FakeFolderBackend:
    def __init__(self, folders: list[FolderSnapshot]) -> None:
        self._folders = folders

    async def list_folders(self) -> list[FolderSnapshot]:
        return [
            FolderSnapshot(
                folder_id=folder.folder_id,
                folder_name=folder.folder_name,
                chats=list(folder.chats),
            )
            for folder in self._folders
        ]

    async def resolve_chat(self, chat_ref: str | int) -> FolderChat:
        if isinstance(chat_ref, int):
            return FolderChat(chat_id=chat_ref, title=f"Chat {chat_ref}")
        raise LookupError(f"unknown chat ref {chat_ref!r}")

    async def add_chat_to_folder(self, folder_id: int, chat_id: int) -> None:
        raise NotImplementedError

    async def remove_chat_from_folder(self, folder_id: int, chat_id: int) -> None:
        raise NotImplementedError


def _messages() -> list[RecentMessage]:
    return [
        RecentMessage(id=1, sender="alice", date=None, reply_to=None, text="one"),
        RecentMessage(id=2, sender="bob", date=None, reply_to=1, text="two"),
    ]


def _with_access(minimal_config_yaml: str, access_block: str) -> str:
    return minimal_config_yaml.replace(
        "  defaults:\n",
        f"  access:\n{access_block}  defaults:\n",
        1,
    )


class FakeDeleteBackend:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def delete_messages(
        self, *, chat_id: int, message_ids: tuple[int, ...], revoke: bool = True
    ) -> int:
        message_ids = tuple(message_ids)
        self.calls.append(
            {"chat_id": chat_id, "message_ids": message_ids, "revoke": revoke}
        )
        return len(message_ids)


class FakeEditBackend:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def edit_message(self, *, chat_id: int, message_id: int, text: str) -> int:
        self.calls.append(
            {"chat_id": chat_id, "message_id": message_id, "text": text}
        )
        return message_id


class FakePinBackend:
    def __init__(self) -> None:
        self.pins: list[dict[str, Any]] = []
        self.unpins: list[dict[str, Any]] = []

    async def pin_message(
        self, *, chat_id: int, message_id: int, silent: bool, pm_oneside: bool
    ) -> None:
        self.pins.append(
            {
                "chat_id": chat_id,
                "message_id": message_id,
                "silent": silent,
                "pm_oneside": pm_oneside,
            }
        )

    async def unpin_message(
        self, *, chat_id: int, message_id: int | None
    ) -> None:
        self.unpins.append({"chat_id": chat_id, "message_id": message_id})


def _client(
    config_yaml: str,
    tmp_path: Path,
    *,
    read_backend: FakeReadBackend | None = None,
    message_backend: FakeMessageBackend | None = None,
    delete_backend: FakeDeleteBackend | None = None,
    edit_backend: FakeEditBackend | None = None,
    pin_backend: FakePinBackend | None = None,
    group_backend: FakeLayoutBackend | None = None,
    topic_backend: FakeTopicBackend | None = None,
    member_backend: FakeMemberBackend | None = None,
    folder_backend: FakeFolderBackend | None = None,
    required_scopes: tuple[str, ...] = ("mcp", "telegram:read"),
    admin: str = "",
) -> TestClient:
    config = load_config_from_text(
        _enabled_mcp_yaml(config_yaml, required_scopes=required_scopes, admin=admin)
    )
    app = create_app(
        config,
        session_manager=FakeSessionManager(),  # type: ignore[arg-type]
        mcp_google_provider=FakeGoogleOidcProvider(),
        group_backend_factory=(
            (lambda _r: group_backend) if group_backend is not None else (lambda _r: None)
        ),
        topic_backend_factory=(
            (lambda _r: topic_backend) if topic_backend is not None else (lambda _r: None)
        ),
        member_backend_factory=(
            (lambda _r: member_backend) if member_backend is not None else (lambda _r: None)
        ),
        member_remove_backend_factory=(
            (lambda _r: member_backend) if member_backend is not None else None
        ),
        folder_backend_factory=(
            (lambda _r: folder_backend) if folder_backend is not None else (lambda _r: None)
        ),
        message_read_backend_factory=(
            (lambda _r: read_backend) if read_backend is not None else (lambda _r: None)
        ),
        message_backend_factory=(
            (lambda _r: message_backend)
            if message_backend is not None
            else (lambda _r: None)
        ),
        delete_backend_factory=(
            (lambda _r: delete_backend)
            if delete_backend is not None
            else (lambda _r: None)
        ),
        edit_backend_factory=(
            (lambda _r: edit_backend)
            if edit_backend is not None
            else (lambda _r: None)
        ),
        pin_backend_factory=(
            (lambda _r: pin_backend)
            if pin_backend is not None
            else (lambda _r: None)
        ),
        operation_store=OperationStore(tmp_path / "state.db"),
        resolver_factory=lambda _r: None,
    )
    return TestClient(app)


def _initialize(client: TestClient, token: str) -> None:
    headers = _mcp_headers(token)
    initialize = client.post("/mcp", json=_initialize_payload(), headers=headers)
    assert initialize.status_code == 200, initialize.text
    initialized = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        headers=headers,
    )
    assert initialized.status_code == 202, initialized.text


def _call_tool(
    client: TestClient, token: str, name: str, arguments: dict[str, Any]
) -> dict[str, Any]:
    response = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 7,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
        headers=_mcp_headers(token),
    )
    assert response.status_code == 200, response.text
    return response.json()["result"]


def test_mcp_recent_messages_reads_via_backend(
    minimal_config_yaml: str, tmp_path: Path
) -> None:
    backend = FakeReadBackend(_messages())
    with _client(minimal_config_yaml, tmp_path, read_backend=backend) as client:
        token = _mint_token(client)
        _initialize(client, token)

        result = _call_tool(
            client,
            token,
            "telegram_messages_recent",
            {"chat_id": -100123, "limit": 1},
        )

    assert result["isError"] is False
    payload = result["structuredContent"]
    assert payload["telegram_chat_id"] == -100123
    assert payload["count"] == 1
    assert payload["messages"][0]["text"] == "one"
    assert backend.calls == [{"chat_id": -100123, "limit": 1}]


def test_mcp_send_message_uses_operation_store_idempotency(
    minimal_config_yaml: str, tmp_path: Path
) -> None:
    backend = FakeMessageBackend()
    with _client(minimal_config_yaml, tmp_path, message_backend=backend) as client:
        token = _mint_token(client)
        _initialize(client, token)

        first = _call_tool(
            client,
            token,
            "telegram_messages_send",
            {
                "telegram_chat_id": -100123,
                "text": "hello",
                "operation_id": "mcp-send-1",
            },
        )
        second = _call_tool(
            client,
            token,
            "telegram_messages_send",
            {
                "telegram_chat_id": -100123,
                "text": "hello",
                "operation_id": "mcp-send-1",
            },
        )

    assert first["isError"] is False
    assert second["isError"] is False
    assert first["structuredContent"]["telegram_message_id"] == 777
    assert second["structuredContent"]["replayed"] is True
    assert len(backend.sent) == 1


def test_mcp_send_message_drops_server_local_files_arg(
    minimal_config_yaml: str, tmp_path: Path
) -> None:
    # ``files`` is no longer part of the MCP send surface (Task 12). An extra
    # ``files`` kwarg is not in the tool schema and is ignored, so the text send
    # proceeds with no server-local attachments.
    backend = FakeMessageBackend()
    with _client(minimal_config_yaml, tmp_path, message_backend=backend) as client:
        token = _mint_token(client)
        _initialize(client, token)

        result = _call_tool(
            client,
            token,
            "telegram_messages_send",
            {
                "telegram_chat_id": -100123,
                "text": "secret",
                "files": ["/etc/passwd"],
            },
        )

    assert result["isError"] is False
    assert len(backend.sent) == 1
    assert backend.sent[0]["files"] == ()


def test_mcp_edit_message_edits_via_backend(
    minimal_config_yaml: str, tmp_path: Path
) -> None:
    # A wildcard write rule plus edit_only_session_messages: false lets the
    # tool edit an arbitrary id through the backend.
    backend = FakeEditBackend()
    config_yaml = _with_access(
        minimal_config_yaml,
        "    rules:\n      - all: true\n        permission: \"write\"\n"
        "    edit_only_session_messages: false\n",
    )
    with _client(config_yaml, tmp_path, edit_backend=backend) as client:
        token = _mint_token(client)
        _initialize(client, token)

        result = _call_tool(
            client,
            token,
            "telegram_messages_edit",
            {"telegram_chat_id": -100123, "message_id": 5, "text": "patched"},
        )

    assert result["isError"] is False
    payload = result["structuredContent"]
    assert payload["telegram_message_id"] == 5
    assert payload["text"] == "patched"
    assert backend.calls == [
        {"chat_id": -100123, "message_id": 5, "text": "patched"}
    ]


def test_mcp_edit_message_session_limit_blocks_unsent(
    minimal_config_yaml: str, tmp_path: Path
) -> None:
    # Default edit_only_session_messages (true) + fresh registry -> the unsent
    # id is rejected as edit_forbidden.
    backend = FakeEditBackend()
    with _client(minimal_config_yaml, tmp_path, edit_backend=backend) as client:
        token = _mint_token(client)
        _initialize(client, token)

        result = _call_tool(
            client,
            token,
            "telegram_messages_edit",
            {"telegram_chat_id": -100123, "message_id": 999, "text": "x"},
        )

    assert result["isError"] is True
    text = result["content"][0]["text"]
    assert '"error": "edit_forbidden"' in text
    assert backend.calls == []


def test_mcp_pin_message_pins_via_backend(
    minimal_config_yaml: str, tmp_path: Path
) -> None:
    backend = FakePinBackend()
    config_yaml = _with_access(
        minimal_config_yaml,
        "    rules:\n      - all: true\n        permission: \"write\"\n",
    )
    with _client(config_yaml, tmp_path, pin_backend=backend) as client:
        token = _mint_token(client)
        _initialize(client, token)

        result = _call_tool(
            client,
            token,
            "telegram_messages_pin",
            {"telegram_chat_id": -100123, "message_id": 5, "silent": True},
        )

    assert result["isError"] is False
    payload = result["structuredContent"]
    assert payload["telegram_message_id"] == 5
    assert payload["silent"] is True
    assert backend.pins == [
        {"chat_id": -100123, "message_id": 5, "silent": True, "pm_oneside": False}
    ]


def test_mcp_pin_message_denied_without_write(
    minimal_config_yaml: str, tmp_path: Path
) -> None:
    backend = FakePinBackend()
    config_yaml = _with_access(
        minimal_config_yaml,
        "    rules:\n      - all: true\n        permission: \"read\"\n",
    )
    with _client(config_yaml, tmp_path, pin_backend=backend) as client:
        token = _mint_token(client)
        _initialize(client, token)

        result = _call_tool(
            client,
            token,
            "telegram_messages_pin",
            {"telegram_chat_id": -100123, "message_id": 5},
        )

    assert result["isError"] is True
    text = result["content"][0]["text"]
    assert '"error": "access_denied"' in text
    assert backend.pins == []


def test_mcp_unpin_all_via_backend(
    minimal_config_yaml: str, tmp_path: Path
) -> None:
    backend = FakePinBackend()
    config_yaml = _with_access(
        minimal_config_yaml,
        "    rules:\n      - all: true\n        permission: \"write\"\n",
    )
    with _client(config_yaml, tmp_path, pin_backend=backend) as client:
        token = _mint_token(client)
        _initialize(client, token)

        result = _call_tool(
            client,
            token,
            "telegram_messages_unpin",
            {"telegram_chat_id": -100123, "unpin_all": True},
        )

    assert result["isError"] is False
    payload = result["structuredContent"]
    assert payload["unpinned_all"] is True
    assert payload["telegram_message_id"] is None
    assert backend.unpins == [{"chat_id": -100123, "message_id": None}]


def test_mcp_unpin_requires_id_or_all(
    minimal_config_yaml: str, tmp_path: Path
) -> None:
    backend = FakePinBackend()
    config_yaml = _with_access(
        minimal_config_yaml,
        "    rules:\n      - all: true\n        permission: \"write\"\n",
    )
    with _client(config_yaml, tmp_path, pin_backend=backend) as client:
        token = _mint_token(client)
        _initialize(client, token)

        result = _call_tool(
            client,
            token,
            "telegram_messages_unpin",
            {"telegram_chat_id": -100123},
        )

    assert result["isError"] is True
    assert backend.unpins == []


def test_mcp_tool_maps_access_denied_to_actionable_error(
    minimal_config_yaml: str, tmp_path: Path
) -> None:
    backend = FakeReadBackend(_messages())
    config_yaml = _with_access(minimal_config_yaml, "    rules: []\n")
    with _client(config_yaml, tmp_path, read_backend=backend) as client:
        token = _mint_token(client)
        _initialize(client, token)

        result = _call_tool(
            client,
            token,
            "telegram_messages_recent",
            {"chat_id": -100123},
        )

    assert result["isError"] is True
    text = result["content"][0]["text"]
    assert '"error": "access_denied"' in text
    assert '"status": 403' in text
    assert backend.calls == []


def test_mcp_topics_layout_requires_read_access(
    minimal_config_yaml: str, tmp_path: Path
) -> None:
    backend = FakeLayoutBackend(forum_tabs=True)
    config_yaml = _with_access(minimal_config_yaml, "    rules: []\n")
    with _client(config_yaml, tmp_path, group_backend=backend) as client:
        token = _mint_token(client)
        _initialize(client, token)

        result = _call_tool(
            client,
            token,
            "telegram_topics_layout",
            {"chat_id": -100123},
        )

    assert result["isError"] is True
    text = result["content"][0]["text"]
    assert '"error": "access_denied"' in text
    assert backend.get_calls == []


def test_mcp_topics_layout_requires_write_access(
    minimal_config_yaml: str, tmp_path: Path
) -> None:
    backend = FakeLayoutBackend(forum_tabs=False)
    config_yaml = _with_access(
        minimal_config_yaml,
        '    rules:\n      - all: true\n        permission: "read"\n',
    )
    with _client(config_yaml, tmp_path, group_backend=backend) as client:
        token = _mint_token(client)
        _initialize(client, token)

        result = _call_tool(
            client,
            token,
            "telegram_topics_layout",
            {"chat_id": -100123, "layout": "tabs"},
        )

    assert result["isError"] is True
    text = result["content"][0]["text"]
    assert '"error": "access_denied"' in text
    assert backend.set_calls == []


def test_mcp_topics_bulk_create_accepts_planfix_task_id_alias(
    minimal_config_yaml: str, tmp_path: Path
) -> None:
    backend = FakeTopicBackend()
    with _client(minimal_config_yaml, tmp_path, topic_backend=backend) as client:
        token = _mint_token(client)
        _initialize(client, token)

        result = _call_tool(
            client,
            token,
            "telegram_topics_bulk_create",
            {
                "telegram_chat_id": -100123,
                "items": [{"topic_name": "Alpha", "planfix_task_id": 123}],
                "operation_id": "mcp-topic-alias",
            },
        )

    assert result["isError"] is False
    item = result["structuredContent"]["items"][0]
    assert item["topic_name"] == "Alpha"
    assert item["external_ref"] == 123
    assert backend.created == [
        {"chat_id": -100123, "name": "Alpha", "topic_id": 1000}
    ]


def test_mcp_topics_close_accepts_topic_name(
    minimal_config_yaml: str, tmp_path: Path
) -> None:
    backend = FakeTopicBackend()
    with _client(minimal_config_yaml, tmp_path, topic_backend=backend) as client:
        token = _mint_token(client)
        _initialize(client, token)

        result = _call_tool(
            client,
            token,
            "telegram_topics_close",
            {"telegram_chat_id": -100123, "topic_name": "Kickoff"},
        )

    assert result["isError"] is False
    assert result["structuredContent"]["telegram_topic_id"] == 42
    assert backend.closed == [{"chat_id": -100123, "topic_id": 42}]


def test_mcp_groups_rename_renames_via_backend(
    minimal_config_yaml: str, tmp_path: Path
) -> None:
    backend = FakeLayoutBackend()
    with _client(minimal_config_yaml, tmp_path, group_backend=backend) as client:
        token = _mint_token(client)
        _initialize(client, token)

        result = _call_tool(
            client,
            token,
            "telegram_groups_rename",
            {"telegram_chat_id": -100123, "new_title": "Renamed Group"},
        )

    assert result["isError"] is False, result
    payload = result["structuredContent"]
    assert payload["telegram_chat_id"] == -100123
    assert payload["new_title"] == "Renamed Group"
    assert payload["status"] == "renamed"
    assert backend.renamed == [(-100123, "Renamed Group")]


def test_mcp_groups_rename_replays_same_title(
    minimal_config_yaml: str, tmp_path: Path
) -> None:
    backend = FakeLayoutBackend()
    with _client(minimal_config_yaml, tmp_path, group_backend=backend) as client:
        token = _mint_token(client)
        _initialize(client, token)

        first = _call_tool(
            client,
            token,
            "telegram_groups_rename",
            {"telegram_chat_id": -100123, "new_title": "Same Title"},
        )
        second = _call_tool(
            client,
            token,
            "telegram_groups_rename",
            {"telegram_chat_id": -100123, "new_title": "Same Title"},
        )

    assert first["isError"] is False
    assert second["isError"] is False
    assert second["structuredContent"]["replayed"] is True
    # Replay does not hit the backend a second time.
    assert backend.renamed == [(-100123, "Same Title")]


def test_mcp_groups_rename_requires_write_access(
    minimal_config_yaml: str, tmp_path: Path
) -> None:
    backend = FakeLayoutBackend()
    config_yaml = _with_access(
        minimal_config_yaml,
        '    rules:\n      - all: true\n        permission: "read"\n',
    )
    with _client(config_yaml, tmp_path, group_backend=backend) as client:
        token = _mint_token(client)
        _initialize(client, token)

        result = _call_tool(
            client,
            token,
            "telegram_groups_rename",
            {"telegram_chat_id": -100123, "new_title": "Renamed Group"},
        )

    assert result["isError"] is True
    text = result["content"][0]["text"]
    assert '"error": "access_denied"' in text
    assert backend.renamed == []


def test_mcp_topics_rename_by_id_renames_via_backend(
    minimal_config_yaml: str, tmp_path: Path
) -> None:
    backend = FakeTopicBackend()
    with _client(minimal_config_yaml, tmp_path, topic_backend=backend) as client:
        token = _mint_token(client)
        _initialize(client, token)

        result = _call_tool(
            client,
            token,
            "telegram_topics_rename",
            {"telegram_chat_id": -100123, "topic_id": 42, "new_title": "Renamed"},
        )

    assert result["isError"] is False, result
    payload = result["structuredContent"]
    assert payload["telegram_topic_id"] == 42
    assert payload["new_title"] == "Renamed"
    assert backend.renamed == [
        {"chat_id": -100123, "topic_id": 42, "title": "Renamed"}
    ]


def test_mcp_topics_rename_accepts_topic_name(
    minimal_config_yaml: str, tmp_path: Path
) -> None:
    backend = FakeTopicBackend()
    with _client(minimal_config_yaml, tmp_path, topic_backend=backend) as client:
        token = _mint_token(client)
        _initialize(client, token)

        result = _call_tool(
            client,
            token,
            "telegram_topics_rename",
            {"telegram_chat_id": -100123, "topic_name": "Kickoff", "new_title": "Renamed"},
        )

    assert result["isError"] is False, result
    assert result["structuredContent"]["telegram_topic_id"] == 42
    assert backend.renamed == [
        {"chat_id": -100123, "topic_id": 42, "title": "Renamed"}
    ]


def test_mcp_topics_rename_requires_write_access(
    minimal_config_yaml: str, tmp_path: Path
) -> None:
    backend = FakeTopicBackend()
    config_yaml = _with_access(
        minimal_config_yaml,
        '    rules:\n      - all: true\n        permission: "read"\n',
    )
    with _client(config_yaml, tmp_path, topic_backend=backend) as client:
        token = _mint_token(client)
        _initialize(client, token)

        result = _call_tool(
            client,
            token,
            "telegram_topics_rename",
            {"telegram_chat_id": -100123, "topic_id": 42, "new_title": "Renamed"},
        )

    assert result["isError"] is True
    text = result["content"][0]["text"]
    assert '"error": "access_denied"' in text
    assert backend.renamed == []


def test_mcp_topics_rename_rejects_both_topic_id_and_name(
    minimal_config_yaml: str, tmp_path: Path
) -> None:
    backend = FakeTopicBackend()
    with _client(minimal_config_yaml, tmp_path, topic_backend=backend) as client:
        token = _mint_token(client)
        _initialize(client, token)

        result = _call_tool(
            client,
            token,
            "telegram_topics_rename",
            {
                "telegram_chat_id": -100123,
                "topic_id": 42,
                "topic_name": "Kickoff",
                "new_title": "Renamed",
            },
        )

    assert result["isError"] is True
    text = result["content"][0]["text"]
    assert '"error": "invalid_request"' in text
    assert "exactly one of topic_id or topic_name" in text
    assert backend.renamed == []


def test_mcp_topics_rename_rejects_neither_topic_id_nor_name(
    minimal_config_yaml: str, tmp_path: Path
) -> None:
    backend = FakeTopicBackend()
    with _client(minimal_config_yaml, tmp_path, topic_backend=backend) as client:
        token = _mint_token(client)
        _initialize(client, token)

        result = _call_tool(
            client,
            token,
            "telegram_topics_rename",
            {"telegram_chat_id": -100123, "new_title": "Renamed"},
        )

    assert result["isError"] is True
    text = result["content"][0]["text"]
    assert '"error": "invalid_request"' in text
    assert "exactly one of topic_id or topic_name" in text
    assert backend.renamed == []


def test_mcp_topics_rename_rejects_non_positive_topic_id(
    minimal_config_yaml: str, tmp_path: Path
) -> None:
    backend = FakeTopicBackend()
    with _client(minimal_config_yaml, tmp_path, topic_backend=backend) as client:
        token = _mint_token(client)
        _initialize(client, token)

        result = _call_tool(
            client,
            token,
            "telegram_topics_rename",
            {"telegram_chat_id": -100123, "topic_id": 0, "new_title": "Renamed"},
        )

    assert result["isError"] is True
    text = result["content"][0]["text"]
    assert '"error": "invalid_request"' in text
    assert "topic_id must be a positive integer" in text
    assert backend.renamed == []


def test_mcp_members_add_accepts_generic_chat_resolution_args(
    minimal_config_yaml: str, tmp_path: Path
) -> None:
    backend = FakeMemberBackend()
    with _client(minimal_config_yaml, tmp_path, member_backend=backend) as client:
        token = _mint_token(client)
        _initialize(client, token)

        result = _call_tool(
            client,
            token,
            "telegram_members_add",
            {"telegram_chat_id": -100123, "items": [{"user": "@alice"}]},
        )

    assert result["isError"] is False
    assert backend.added == [{"chat_id": -100123, "user": "@alice"}]


def test_mcp_members_remove_accepts_generic_chat_resolution_args(
    minimal_config_yaml: str, tmp_path: Path
) -> None:
    backend = FakeMemberBackend()
    with _client(minimal_config_yaml, tmp_path, member_backend=backend) as client:
        token = _mint_token(client)
        _initialize(client, token)

        result = _call_tool(
            client,
            token,
            "telegram_members_remove",
            {"telegram_chat_id": -100123, "items": [{"user": "@alice"}]},
        )

    assert result["isError"] is False
    assert backend.banned == [{"chat_id": -100123, "user": "@alice"}]
    assert backend.unbanned == [{"chat_id": -100123, "user": "@alice"}]


def test_mcp_folders_inspect_requires_folder_read_access(
    minimal_config_yaml: str, tmp_path: Path
) -> None:
    folder_backend = FakeFolderBackend(
        [
            FolderSnapshot(
                folder_id=2,
                folder_name="Clients",
                chats=[FolderChat(chat_id=-100123, title="Acme")],
            )
        ]
    )
    config_yaml = _with_access(minimal_config_yaml, "    rules: []\n")
    with _client(config_yaml, tmp_path, folder_backend=folder_backend) as client:
        token = _mint_token(client)
        _initialize(client, token)

        result = _call_tool(
            client,
            token,
            "telegram_folders_inspect",
            {"folder_name": "Clients"},
        )

    assert result["isError"] is True
    text = result["content"][0]["text"]
    assert '"error": "access_denied"' in text
    assert "Acme" not in text


def test_mcp_operations_tools_require_admin_scope(
    minimal_config_yaml: str, tmp_path: Path
) -> None:
    store = OperationStore(tmp_path / "state.db")
    op = store.begin_operation(
        operation_type="message.send",
        idempotency_key="op-1",
        request_payload={"telegram_chat_id": -100123, "text": "hidden"},
    ).operation
    store.fail_operation(op.id, "boom")

    with _client(
        minimal_config_yaml,
        tmp_path,
        admin="""
  admin_emails:
    - "owner@example.test"
""",
    ) as client:
        client.app.state.operation_store = store
        token = _mint_token(client, scope="mcp telegram:read")
        _initialize(client, token)

        status_result = _call_tool(
            client,
            token,
            "telegram_operations_status",
            {"operation_id": op.id},
        )
        retry_result = _call_tool(
            client,
            token,
            "telegram_operations_retry",
            {"operation_id": op.id, "dry_run": True},
        )
        admin_token = _mint_token(client, scope="mcp telegram:read telegram:admin")
        _initialize(client, admin_token)
        admin_status = _call_tool(
            client,
            admin_token,
            "telegram_operations_status",
            {"operation_id": op.id},
        )

    for result in (status_result, retry_result):
        assert result["isError"] is True
        text = result["content"][0]["text"]
        assert '"error": "insufficient_scope"' in text
        assert "hidden" not in text
    assert admin_status["isError"] is False
    assert admin_status["structuredContent"]["request_payload"]["text"] == "hidden"


def test_mcp_tool_maps_backend_unavailable_to_actionable_error(
    minimal_config_yaml: str, tmp_path: Path
) -> None:
    with _client(minimal_config_yaml, tmp_path) as client:
        token = _mint_token(client)
        _initialize(client, token)

        result = _call_tool(
            client,
            token,
            "telegram_messages_recent",
            {"chat_id": -100123},
        )

    assert result["isError"] is True
    text = result["content"][0]["text"]
    assert '"error": "backend_unavailable"' in text
    assert '"status": 503' in text


# ---------------------------------------------------------------------------
# Delete tool (Task 7)
# ---------------------------------------------------------------------------


def test_mcp_delete_session_limit_blocks_unsent_message(
    minimal_config_yaml: str, tmp_path: Path
) -> None:
    # Default config has no access block -> delete_only_session_messages
    # defaults to true; an id this process never sent is rejected.
    backend = FakeDeleteBackend()
    with _client(minimal_config_yaml, tmp_path, delete_backend=backend) as client:
        token = _mint_token(client)
        _initialize(client, token)

        result = _call_tool(
            client,
            token,
            "telegram_messages_delete",
            {"telegram_chat_id": -100123, "message_ids": [999]},
        )

    assert result["isError"] is True
    text = result["content"][0]["text"]
    assert '"error": "delete_forbidden"' in text
    assert '"status": 403' in text
    assert backend.calls == []


def test_mcp_delete_allows_message_this_process_sent(
    minimal_config_yaml: str, tmp_path: Path
) -> None:
    # Send through the MCP send tool first so the id lands in the process
    # registry, then delete it: the session-limit lets it through end-to-end.
    message_backend = FakeMessageBackend()
    delete_backend = FakeDeleteBackend()
    with _client(
        minimal_config_yaml,
        tmp_path,
        message_backend=message_backend,
        delete_backend=delete_backend,
    ) as client:
        token = _mint_token(client)
        _initialize(client, token)

        send = _call_tool(
            client,
            token,
            "telegram_messages_send",
            {"telegram_chat_id": -100123, "text": "hi"},
        )
        assert send["isError"] is False
        sent_id = send["structuredContent"]["telegram_message_id"]

        result = _call_tool(
            client,
            token,
            "telegram_messages_delete",
            {"telegram_chat_id": -100123, "message_ids": [sent_id]},
        )

    assert result["isError"] is False, result
    payload = result["structuredContent"]
    assert payload["deleted"] == 1
    assert payload["revoke"] is True
    assert delete_backend.calls == [
        {"chat_id": -100123, "message_ids": (sent_id,), "revoke": True}
    ]


def test_mcp_delete_session_limit_off_allows_arbitrary_ids(
    minimal_config_yaml: str, tmp_path: Path
) -> None:
    backend = FakeDeleteBackend()
    config_yaml = _with_access(
        minimal_config_yaml,
        "    rules:\n      - all: true\n        permission: delete\n"
        "    delete_only_session_messages: false\n",
    )
    with _client(config_yaml, tmp_path, delete_backend=backend) as client:
        token = _mint_token(client)
        _initialize(client, token)

        result = _call_tool(
            client,
            token,
            "telegram_messages_delete",
            {"telegram_chat_id": -100123, "message_ids": [42], "revoke": False},
        )

    assert result["isError"] is False, result
    payload = result["structuredContent"]
    assert payload["deleted"] == 1
    assert payload["revoke"] is False
    assert backend.calls == [
        {"chat_id": -100123, "message_ids": (42,), "revoke": False}
    ]


def test_mcp_delete_denied_without_delete_permission(
    minimal_config_yaml: str, tmp_path: Path
) -> None:
    backend = FakeDeleteBackend()
    # write-only on the chat plus session-limit off -> access denied (not
    # delete_forbidden) since the policy never grants DELETE.
    config_yaml = _with_access(
        minimal_config_yaml,
        "    rules:\n      - all: true\n        permission: write\n"
        "    delete_only_session_messages: false\n",
    )
    with _client(config_yaml, tmp_path, delete_backend=backend) as client:
        token = _mint_token(client)
        _initialize(client, token)

        result = _call_tool(
            client,
            token,
            "telegram_messages_delete",
            {"telegram_chat_id": -100123, "message_ids": [42]},
        )

    assert result["isError"] is True
    text = result["content"][0]["text"]
    assert '"error": "access_denied"' in text
    assert backend.calls == []


def test_mcp_delete_503_when_backend_unavailable(
    minimal_config_yaml: str, tmp_path: Path
) -> None:
    # No delete backend wired and session-limit off so the backend lookup is
    # reached: surfaces backend_unavailable (503).
    config_yaml = _with_access(
        minimal_config_yaml,
        "    rules:\n      - all: true\n        permission: delete\n"
        "    delete_only_session_messages: false\n",
    )
    with _client(config_yaml, tmp_path) as client:
        token = _mint_token(client)
        _initialize(client, token)

        result = _call_tool(
            client,
            token,
            "telegram_messages_delete",
            {"telegram_chat_id": -100123, "message_ids": [42]},
        )

    assert result["isError"] is True
    text = result["content"][0]["text"]
    assert '"status": 503' in text
