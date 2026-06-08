"""Tests for Task 13 — send message / service command (domain, HTTP, CLI)."""

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
from telegram_assistant.http_api import create_app
from telegram_assistant.messages import (
    MassSendRequest,
    MessageSendFailed,
    MessageSendNeedsReview,
    SendMessageRequest,
    is_service_command,
    mass_send_message,
    redact_message_text,
    send_message,
)
from telegram_assistant.persistence import (
    OperationStatus,
    OperationStore,
)
from telegram_assistant.topics import TopicSummary
from telegram_assistant.worker.queue import FloodWaitError


class FakeMessageBackend:
    """In-memory MessageBackend recording send_message calls.

    The signature matches the TopicBackend.send_message contract so the same
    fake doubles as a topic backend when needed.
    """

    def __init__(
        self,
        *,
        topics_per_chat: dict[int, list[TopicSummary]] | None = None,
        fail_send: bool = False,
        next_id: int = 100,
    ) -> None:
        self._topics_per_chat = topics_per_chat or {}
        self._fail_send = fail_send
        self._next_id = next_id
        self.sent: list[dict[str, Any]] = []

    async def send_message(
        self,
        *,
        chat_id: int,
        text: str,
        topic_id: int | None = None,
        files: tuple[str, ...] = (),
        schedule_at: Any = None,
        reply_to_message_id: int | None = None,
    ) -> int | list[int]:
        if self._fail_send:
            raise RuntimeError("telegram error")
        files = tuple(files)
        if len(files) > 1:
            # Album — one id per attachment.
            ids = [self._next_id + i for i in range(len(files))]
            self._next_id += len(files)
            self.sent.append(
                {
                    "chat_id": chat_id,
                    "text": text,
                    "topic_id": topic_id,
                    "files": files,
                    "schedule_at": schedule_at,
                    "reply_to_message_id": reply_to_message_id,
                    "id": ids[0],
                    "ids": ids,
                }
            )
            return ids
        msg_id = self._next_id
        self._next_id += 1
        self.sent.append(
            {
                "chat_id": chat_id,
                "text": text,
                "topic_id": topic_id,
                "files": files,
                "schedule_at": schedule_at,
                "reply_to_message_id": reply_to_message_id,
                "id": msg_id,
            }
        )
        return msg_id

    async def create_topic(self, *, chat_id: int, name: str) -> int:
        raise NotImplementedError

    async def close_topic(self, *, chat_id: int, topic_id: int) -> None:
        raise NotImplementedError

    async def list_topics(self, *, chat_id: int) -> list[TopicSummary]:
        return list(self._topics_per_chat.get(chat_id, []))


class MalformedMessageBackend(FakeMessageBackend):
    def __init__(self, returned: Any) -> None:
        super().__init__()
        self._returned = returned

    async def send_message(
        self,
        *,
        chat_id: int,
        text: str,
        topic_id: int | None = None,
        files: tuple[str, ...] = (),
        schedule_at: Any = None,
    ) -> Any:
        self.sent.append(
            {
                "chat_id": chat_id,
                "text": text,
                "topic_id": topic_id,
                "files": tuple(files),
                "schedule_at": schedule_at,
            }
        )
        return self._returned


class FakeFolderBackend:
    def __init__(self, folders: list[FolderSnapshot]) -> None:
        self._folders = folders

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
        raise NotImplementedError

    async def add_chat_to_folder(self, folder_id: int, chat_id: int) -> None:
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture()
def store(tmp_path: Path) -> OperationStore:
    return OperationStore(tmp_path / "state.db")


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_is_service_command_detects_slash_prefix() -> None:
    assert is_service_command("/task 123")
    assert is_service_command("  /task 123")
    assert not is_service_command("hello")
    assert not is_service_command("")


def test_redact_message_text_redacts_args_only_for_commands() -> None:
    assert redact_message_text("/task 12345") == "/task [redacted]"
    assert redact_message_text("/start") == "/start"
    assert redact_message_text("hello world") == "hello world"


# ---------------------------------------------------------------------------
# Domain tests — targeted send
# ---------------------------------------------------------------------------


async def test_send_message_happy_path(store: OperationStore) -> None:
    backend = FakeMessageBackend()
    req = SendMessageRequest(
        telegram_chat_id=-100, text="hello", operation_id="op-1"
    )
    result, op = await send_message(backend=backend, store=store, request=req)
    assert op.status is OperationStatus.COMPLETED
    assert result.telegram_chat_id == -100
    assert result.telegram_message_id is not None
    assert result.is_service_command is False
    assert result.replayed is False
    assert len(backend.sent) == 1


async def test_send_message_with_topic_id(store: OperationStore) -> None:
    backend = FakeMessageBackend()
    req = SendMessageRequest(
        telegram_chat_id=-100,
        text="topic msg",
        telegram_topic_id=42,
        operation_id="op-topic",
    )
    result, _ = await send_message(backend=backend, store=store, request=req)
    assert result.telegram_topic_id == 42
    assert backend.sent[0]["topic_id"] == 42


async def test_send_message_replays_same_operation_id(
    store: OperationStore,
) -> None:
    backend = FakeMessageBackend()
    req = SendMessageRequest(
        telegram_chat_id=-100, text="hello", operation_id="op-dup"
    )
    first, op1 = await send_message(backend=backend, store=store, request=req)
    assert first.replayed is False

    backend2 = FakeMessageBackend()
    second, op2 = await send_message(backend=backend2, store=store, request=req)
    assert second.replayed is True
    assert op1.id == op2.id
    assert backend2.sent == []
    assert second.telegram_message_id == first.telegram_message_id


async def test_send_message_failure_marks_failed(store: OperationStore) -> None:
    backend = FakeMessageBackend(fail_send=True)
    req = SendMessageRequest(
        telegram_chat_id=-100, text="hi", operation_id="op-fail"
    )
    with pytest.raises(RuntimeError):
        await send_message(backend=backend, store=store, request=req)
    # Second attempt with the same key surfaces the saved failure rather than
    # silently re-sending.
    with pytest.raises(MessageSendFailed):
        await send_message(
            backend=FakeMessageBackend(), store=store, request=req
        )


async def test_send_message_flood_wait_marks_needs_review_not_failed(
    store: OperationStore,
) -> None:
    """FLOOD_WAIT on a single-shot send must be retryable. Marking the row
    ``failed`` would burn the idempotency key on a transient signal — every
    subsequent replay would surface ``MessageSendFailed`` forever.
    """

    class FloodingBackend:
        async def send_message(
            self, *, chat_id: int, text: str, topic_id: int | None = None
        ) -> int:
            raise FloodWaitError(5.0)

    req = SendMessageRequest(
        telegram_chat_id=-100, text="hi", operation_id="op-fw"
    )
    with pytest.raises(MessageSendNeedsReview):
        await send_message(backend=FloodingBackend(), store=store, request=req)

    # Replay must NOT raise MessageSendFailed — the row is needs_review, not
    # terminal-failed.
    with pytest.raises(MessageSendNeedsReview):
        await send_message(backend=FloodingBackend(), store=store, request=req)


async def test_send_message_service_command_marks_flag(
    store: OperationStore,
) -> None:
    backend = FakeMessageBackend()
    req = SendMessageRequest(
        telegram_chat_id=-100, text="/task 12345", operation_id="svc-1"
    )
    result, op = await send_message(backend=backend, store=store, request=req)
    assert result.is_service_command is True
    # Real text reaches Telegram (Planfix bot needs the id).
    assert backend.sent[0]["text"] == "/task 12345"
    # But the persisted request payload is redacted so logs/replays don't
    # leak the id outside the operation_id-bound audit trail.
    assert "[redacted]" in op.request_payload["text"]


async def test_send_message_rejects_empty_text(store: OperationStore) -> None:
    backend = FakeMessageBackend()
    with pytest.raises(ValueError):
        await send_message(
            backend=backend,
            store=store,
            request=SendMessageRequest(
                telegram_chat_id=-100, text="", operation_id="op"
            ),
        )


# ---------------------------------------------------------------------------
# Domain tests — media / scheduled send
# ---------------------------------------------------------------------------


async def test_send_message_text_only_still_omits_media_kwargs(
    store: OperationStore,
) -> None:
    """Backward compatibility: a text-only send must not pass files/schedule to
    the backend so backends predating attachments keep working."""
    backend = FakeMessageBackend()
    req = SendMessageRequest(
        telegram_chat_id=-100, text="plain", operation_id="text-only"
    )
    result, _ = await send_message(backend=backend, store=store, request=req)
    assert result.telegram_message_ids is None
    assert result.scheduled is False
    # No attachments recorded for a plain send.
    assert backend.sent[0]["files"] == ()
    assert backend.sent[0]["schedule_at"] is None


async def test_send_message_media_only(store: OperationStore) -> None:
    backend = FakeMessageBackend()
    req = SendMessageRequest(
        telegram_chat_id=-100,
        text="",
        files=("/data/a.png",),
        operation_id="media-only",
    )
    result, op = await send_message(backend=backend, store=store, request=req)
    assert op.status is OperationStatus.COMPLETED
    assert result.telegram_message_id is not None
    assert result.telegram_message_ids is None
    assert backend.sent[0]["files"] == ("/data/a.png",)
    # Only references are persisted, never contents.
    assert op.request_payload["files"] == ["/data/a.png"]


async def test_send_message_media_with_caption(store: OperationStore) -> None:
    backend = FakeMessageBackend()
    req = SendMessageRequest(
        telegram_chat_id=-100,
        text="look at this",
        files=("/data/a.png",),
        operation_id="media-caption",
    )
    result, _ = await send_message(backend=backend, store=store, request=req)
    assert result.telegram_message_id is not None
    assert backend.sent[0]["text"] == "look at this"
    assert backend.sent[0]["files"] == ("/data/a.png",)


async def test_send_message_album_returns_all_ids(store: OperationStore) -> None:
    backend = FakeMessageBackend()
    req = SendMessageRequest(
        telegram_chat_id=-100,
        text="album caption",
        files=("/data/a.png", "/data/b.png"),
        file_urls=("https://example.com/c.png",),
        operation_id="album-1",
    )
    result, _ = await send_message(backend=backend, store=store, request=req)
    assert result.telegram_message_ids is not None
    assert len(result.telegram_message_ids) == 3
    assert result.telegram_message_id == result.telegram_message_ids[0]
    # files + file_urls combined into one ordered attachment list.
    assert backend.sent[0]["files"] == (
        "/data/a.png",
        "/data/b.png",
        "https://example.com/c.png",
    )


async def test_send_message_downloads_file_urls_to_temp_and_cleans_up(
    store: OperationStore,
) -> None:
    import os

    backend = FakeMessageBackend()
    downloaded: list[str] = []

    async def fake_downloader(url: str) -> str:
        # Stand in for the real download: write the URL to a temp file.
        import tempfile

        fd, path = tempfile.mkstemp(prefix="tg-test-")
        with os.fdopen(fd, "w") as fh:
            fh.write(url)
        downloaded.append(path)
        return path

    req = SendMessageRequest(
        telegram_chat_id=-100,
        text="caption",
        files=("/data/local.png",),
        file_urls=("https://example.com/remote.png",),
        operation_id="dl-1",
    )
    result, op = await send_message(
        backend=backend,
        store=store,
        request=req,
        downloader=fake_downloader,
    )

    assert op.status is OperationStatus.COMPLETED
    assert result.telegram_message_id is not None
    # Backend received the local file path plus the *downloaded* temp path,
    # not the original URL.
    sent_files = backend.sent[0]["files"]
    assert sent_files[0] == "/data/local.png"
    assert sent_files[1] == downloaded[0]
    assert "https://example.com/remote.png" not in sent_files
    # Temp file is cleaned up after a successful send.
    assert not os.path.exists(downloaded[0])
    # The persisted payload still records the original URL, never the temp path.
    assert op.request_payload["file_urls"] == ["https://example.com/remote.png"]


async def test_send_message_cleans_up_temp_on_send_failure(
    store: OperationStore,
) -> None:
    import os

    backend = FakeMessageBackend(fail_send=True)
    downloaded: list[str] = []

    async def fake_downloader(url: str) -> str:
        import tempfile

        fd, path = tempfile.mkstemp(prefix="tg-test-")
        os.close(fd)
        downloaded.append(path)
        return path

    req = SendMessageRequest(
        telegram_chat_id=-100,
        text="caption",
        file_urls=("https://example.com/remote.png",),
        operation_id="dl-fail",
    )
    with pytest.raises(RuntimeError):
        await send_message(
            backend=backend,
            store=store,
            request=req,
            downloader=fake_downloader,
        )

    # The op is marked failed and the temp file is removed even on failure.
    op = store.find_by_idempotency_key("message_send:chat=-100:topic=-:id=dl-fail")
    assert op is not None and op.status is OperationStatus.FAILED
    assert downloaded and not os.path.exists(downloaded[0])


async def test_send_message_download_error_marks_failed(
    store: OperationStore,
) -> None:
    from telegram_assistant.messages.downloads import DownloadError

    backend = FakeMessageBackend()

    async def broken_downloader(url: str) -> str:
        raise DownloadError(f"unreachable: {url}")

    req = SendMessageRequest(
        telegram_chat_id=-100,
        text="caption",
        file_urls=("https://example.com/remote.png",),
        operation_id="dl-broken",
    )
    with pytest.raises(DownloadError):
        await send_message(
            backend=backend,
            store=store,
            request=req,
            downloader=broken_downloader,
        )

    # A download failure never reaches the backend and fails the operation.
    assert backend.sent == []
    op = store.find_by_idempotency_key("message_send:chat=-100:topic=-:id=dl-broken")
    assert op is not None and op.status is OperationStatus.FAILED


async def test_send_message_without_downloader_passes_urls_through(
    store: OperationStore,
) -> None:
    backend = FakeMessageBackend()
    req = SendMessageRequest(
        telegram_chat_id=-100,
        text="caption",
        file_urls=("https://example.com/remote.png",),
        operation_id="dl-passthrough",
    )
    await send_message(backend=backend, store=store, request=req)
    # No downloader → URL handed to the backend unchanged (backward compatible).
    assert backend.sent[0]["files"] == ("https://example.com/remote.png",)


async def test_send_message_rejects_empty_when_no_text_and_no_files(
    store: OperationStore,
) -> None:
    backend = FakeMessageBackend()
    with pytest.raises(ValueError):
        await send_message(
            backend=backend,
            store=store,
            request=SendMessageRequest(
                telegram_chat_id=-100, text="", operation_id="empty"
            ),
        )
    assert backend.sent == []


async def test_send_message_rejects_blank_attachment_ref(
    store: OperationStore,
) -> None:
    backend = FakeMessageBackend()
    with pytest.raises(ValueError):
        await send_message(
            backend=backend,
            store=store,
            request=SendMessageRequest(
                telegram_chat_id=-100,
                text="hi",
                files=("  ",),
                operation_id="blank",
            ),
        )
    assert backend.sent == []


async def test_send_message_schedule_at_sets_scheduled_flag(
    store: OperationStore,
) -> None:
    from datetime import UTC, datetime

    backend = FakeMessageBackend()
    when = datetime(2030, 1, 1, 12, 0, tzinfo=UTC)
    req = SendMessageRequest(
        telegram_chat_id=-100,
        text="later",
        schedule_at=when,
        operation_id="sched-1",
    )
    result, op = await send_message(backend=backend, store=store, request=req)
    assert result.scheduled is True
    assert result.schedule_at == when.isoformat()
    assert backend.sent[0]["schedule_at"] == when
    assert op.request_payload["schedule_at"] == when.isoformat()
    assert op.result_payload["schedule_at"] == when.isoformat()


async def test_send_message_replay_preserves_schedule_at(
    store: OperationStore,
) -> None:
    from datetime import UTC, datetime

    when = datetime(2030, 1, 1, 12, 30, tzinfo=UTC)
    req = SendMessageRequest(
        telegram_chat_id=-100,
        text="later",
        operation_id="sched-replay",
        schedule_at=when,
    )
    first, _ = await send_message(
        backend=FakeMessageBackend(), store=store, request=req
    )
    second, _ = await send_message(
        backend=FakeMessageBackend(), store=store, request=req
    )

    assert first.schedule_at == when.isoformat()
    assert second.replayed is True
    assert second.schedule_at == when.isoformat()


async def test_send_message_reply_to_passed_to_backend(
    store: OperationStore,
) -> None:
    backend = FakeMessageBackend()
    req = SendMessageRequest(
        telegram_chat_id=-100,
        text="a reply",
        reply_to_message_id=4242,
        operation_id="reply-text",
    )
    _, op = await send_message(backend=backend, store=store, request=req)
    assert backend.sent[0]["reply_to_message_id"] == 4242
    assert op.request_payload["reply_to_message_id"] == 4242


async def test_send_message_reply_to_passed_for_media(
    store: OperationStore,
) -> None:
    backend = FakeMessageBackend()
    req = SendMessageRequest(
        telegram_chat_id=-100,
        text="caption",
        files=("/data/a.png",),
        reply_to_message_id=7,
        operation_id="reply-media",
    )
    await send_message(backend=backend, store=store, request=req)
    assert backend.sent[0]["reply_to_message_id"] == 7


async def test_send_message_omits_reply_to_when_not_set(
    store: OperationStore,
) -> None:
    backend = FakeMessageBackend()
    req = SendMessageRequest(
        telegram_chat_id=-100, text="plain", operation_id="no-reply"
    )
    await send_message(backend=backend, store=store, request=req)
    assert backend.sent[0]["reply_to_message_id"] is None


@pytest.mark.parametrize("returned", [None, [], [0]])
async def test_send_message_rejects_missing_backend_message_ids(
    store: OperationStore, returned: Any
) -> None:
    backend = MalformedMessageBackend(returned)
    req = SendMessageRequest(
        telegram_chat_id=-100, text="hello", operation_id=f"bad-id-{returned!r}"
    )

    with pytest.raises(ValueError):
        await send_message(backend=backend, store=store, request=req)

    with pytest.raises(MessageSendFailed):
        await send_message(
            backend=FakeMessageBackend(),
            store=store,
            request=req,
        )


async def test_send_message_media_denied_before_backend_call(
    store: OperationStore,
) -> None:
    from telegram_assistant.access import AccessDenied, Authorizer
    from telegram_assistant.config.models import AccessConfig, AccessRule

    backend = FakeMessageBackend()
    authorizer = Authorizer(AccessConfig(rules=[AccessRule(all=True, permission="read")]))
    req = SendMessageRequest(
        telegram_chat_id=-100,
        text="",
        files=("/data/a.png",),
        operation_id="denied-media",
    )
    with pytest.raises(AccessDenied):
        await send_message(
            backend=backend, store=store, request=req, authorizer=authorizer
        )
    # Denied before any backend traffic.
    assert backend.sent == []


async def test_send_message_media_replays_same_operation_id(
    store: OperationStore,
) -> None:
    backend = FakeMessageBackend()
    req = SendMessageRequest(
        telegram_chat_id=-100,
        text="caption",
        files=("/data/a.png", "/data/b.png"),
        operation_id="media-replay",
    )
    first, _ = await send_message(backend=backend, store=store, request=req)
    assert first.replayed is False
    assert first.telegram_message_ids is not None

    backend2 = FakeMessageBackend()
    second, _ = await send_message(backend=backend2, store=store, request=req)
    assert second.replayed is True
    assert backend2.sent == []
    assert second.telegram_message_ids == first.telegram_message_ids
    assert second.telegram_message_id == first.telegram_message_id


# ---------------------------------------------------------------------------
# Domain tests — mass send
# ---------------------------------------------------------------------------


async def test_mass_send_targets_only_chats_with_topic(
    store: OperationStore,
) -> None:
    folder = FolderSnapshot(
        folder_id=2,
        folder_name="Planfix clients",
        chats=[
            FolderChat(chat_id=-100, title="Acme"),
            FolderChat(chat_id=-200, title="Beta"),
            FolderChat(chat_id=-300, title="Gamma"),
        ],
    )
    topics_per_chat = {
        -100: [TopicSummary(topic_id=11, title="Daily")],
        -200: [TopicSummary(topic_id=22, title="Other")],
        -300: [TopicSummary(topic_id=33, title="Daily")],
    }
    msg_backend = FakeMessageBackend(topics_per_chat=topics_per_chat)
    folder_backend = FakeFolderBackend([folder])

    req = MassSendRequest(
        folder_name="Planfix clients",
        topic_name="Daily",
        text="standup time",
        operation_id="mass-1",
    )
    result = await mass_send_message(
        message_backend=msg_backend,
        topic_backend=msg_backend,
        folder_backend=folder_backend,
        store=store,
        request=req,
    )
    assert result.sent == 2
    assert result.skipped == 1
    assert result.failed == 0

    by_chat = {it.telegram_chat_id: it for it in result.items}
    assert by_chat[-100].status == "sent"
    assert by_chat[-100].telegram_topic_id == 11
    assert by_chat[-100].telegram_message_id is not None
    assert by_chat[-300].status == "sent"
    assert by_chat[-200].status == "skipped"
    assert by_chat[-200].reason == "topic_not_found"

    # Sent twice — once per matching chat.
    assert len(msg_backend.sent) == 2


async def test_mass_send_flood_wait_on_list_topics_reports_failed(
    store: OperationStore,
) -> None:
    """A FLOOD_WAIT raised by ``list_topics`` must NOT be silently turned into
    ``topic_not_found``. The chat should be reported as ``failed`` with a
    distinct reason so the operator knows to retry, rather than concluding the
    topic was permanently missing.
    """

    class FloodingTopicBackend:
        async def list_topics(self, *, chat_id: int) -> list[TopicSummary]:
            raise FloodWaitError(7.0)

    folder = FolderSnapshot(
        folder_id=1,
        folder_name="F",
        chats=[FolderChat(chat_id=-100, title="Acme")],
    )
    folder_backend = FakeFolderBackend([folder])
    msg_backend = FakeMessageBackend()

    req = MassSendRequest(
        folder_name="F", topic_name="Daily", text="hi", operation_id="mfw"
    )
    result = await mass_send_message(
        message_backend=msg_backend,
        topic_backend=FloodingTopicBackend(),
        folder_backend=folder_backend,
        store=store,
        request=req,
    )
    assert result.failed == 1
    assert result.skipped == 0
    assert result.sent == 0
    item = result.items[0]
    assert item.status == "failed"
    assert item.reason == "list_topics_flood_wait"
    assert item.error is not None and "FLOOD_WAIT" in item.error


async def test_mass_send_skips_ambiguous_topic_name(
    store: OperationStore,
) -> None:
    folder = FolderSnapshot(
        folder_id=2,
        folder_name="F",
        chats=[FolderChat(chat_id=-100, title="Acme")],
    )
    topics_per_chat = {
        -100: [
            TopicSummary(topic_id=1, title="Dup"),
            TopicSummary(topic_id=2, title="Dup"),
        ]
    }
    msg_backend = FakeMessageBackend(topics_per_chat=topics_per_chat)
    folder_backend = FakeFolderBackend([folder])

    req = MassSendRequest(
        folder_name="F", topic_name="Dup", text="hi", operation_id="m"
    )
    result = await mass_send_message(
        message_backend=msg_backend,
        topic_backend=msg_backend,
        folder_backend=folder_backend,
        store=store,
        request=req,
    )
    assert result.sent == 0
    assert result.skipped == 1
    assert msg_backend.sent == []


async def test_mass_send_replays_per_chat_with_operation_id(
    store: OperationStore,
) -> None:
    folder = FolderSnapshot(
        folder_id=2,
        folder_name="F",
        chats=[
            FolderChat(chat_id=-100, title="A"),
            FolderChat(chat_id=-200, title="B"),
        ],
    )
    topics_per_chat = {
        -100: [TopicSummary(topic_id=11, title="T")],
        -200: [TopicSummary(topic_id=22, title="T")],
    }
    msg_backend = FakeMessageBackend(topics_per_chat=topics_per_chat)
    folder_backend = FakeFolderBackend([folder])

    req = MassSendRequest(
        folder_name="F", topic_name="T", text="ping", operation_id="run-1"
    )
    r1 = await mass_send_message(
        message_backend=msg_backend,
        topic_backend=msg_backend,
        folder_backend=folder_backend,
        store=store,
        request=req,
    )
    assert r1.sent == 2

    msg_backend2 = FakeMessageBackend(topics_per_chat=topics_per_chat)
    r2 = await mass_send_message(
        message_backend=msg_backend2,
        topic_backend=msg_backend2,
        folder_backend=folder_backend,
        store=store,
        request=req,
    )
    assert r2.sent == 0
    assert r2.existed == 2
    assert msg_backend2.sent == []


async def test_mass_send_service_command_flag(
    store: OperationStore,
) -> None:
    folder = FolderSnapshot(
        folder_id=2,
        folder_name="F",
        chats=[FolderChat(chat_id=-100, title="A")],
    )
    topics_per_chat = {-100: [TopicSummary(topic_id=11, title="T")]}
    msg_backend = FakeMessageBackend(topics_per_chat=topics_per_chat)
    folder_backend = FakeFolderBackend([folder])
    req = MassSendRequest(
        folder_name="F",
        topic_name="T",
        text="/task 9000",
        operation_id="svc-mass",
    )
    result = await mass_send_message(
        message_backend=msg_backend,
        topic_backend=msg_backend,
        folder_backend=folder_backend,
        store=store,
        request=req,
    )
    assert result.is_service_command is True
    assert result.sent == 1


# ---------------------------------------------------------------------------
# HTTP tests
# ---------------------------------------------------------------------------


def _http_client(
    minimal_config_yaml: str,
    *,
    message_backend: FakeMessageBackend,
    topic_backend: FakeMessageBackend | None = None,
    folder_backend: FakeFolderBackend | None = None,
    store: OperationStore | None = None,
) -> TestClient:
    config = load_config_from_text(minimal_config_yaml)
    if store is None:
        import tempfile

        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".db")
        tmp.close()
        store = OperationStore(Path(tmp.name))
    app = create_app(
        config,
        session_manager=None,
        message_backend_factory=lambda _request: message_backend,
        topic_backend_factory=(
            (lambda _request: topic_backend)
            if topic_backend is not None
            else None
        ),
        folder_backend_factory=(
            (lambda _request: folder_backend)
            if folder_backend is not None
            else None
        ),
        operation_store=store,
    )
    return TestClient(app)


def test_http_send_targeted_by_chat_id(minimal_config_yaml: str) -> None:
    backend = FakeMessageBackend()
    client = _http_client(minimal_config_yaml, message_backend=backend)
    resp = client.post(
        "/telegram/messages",
        json={
            "telegram_chat_id": -100,
            "text": "hi",
            "operation_id": "h-1",
        },
        headers={"Authorization": "Bearer secret_token"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["telegram_chat_id"] == -100
    assert body["mode"] == "targeted"
    assert body["operation_status"] == "completed"


def test_http_send_requires_auth(minimal_config_yaml: str) -> None:
    backend = FakeMessageBackend()
    client = _http_client(minimal_config_yaml, message_backend=backend)
    resp = client.post(
        "/telegram/messages",
        json={"telegram_chat_id": -100, "text": "hi"},
    )
    assert resp.status_code == 401


def test_http_send_mass_mode_across_folder(minimal_config_yaml: str) -> None:
    folder = FolderSnapshot(
        folder_id=2,
        folder_name="Planfix clients",
        chats=[
            FolderChat(chat_id=-100, title="Acme"),
            FolderChat(chat_id=-200, title="Beta"),
        ],
    )
    topics_per_chat = {
        -100: [TopicSummary(topic_id=11, title="Daily")],
        -200: [TopicSummary(topic_id=22, title="Other")],
    }
    backend = FakeMessageBackend(topics_per_chat=topics_per_chat)
    folder_backend = FakeFolderBackend([folder])
    client = _http_client(
        minimal_config_yaml,
        message_backend=backend,
        topic_backend=backend,
        folder_backend=folder_backend,
    )
    resp = client.post(
        "/telegram/messages",
        json={
            "folder_name": "Planfix clients",
            "topic_name": "Daily",
            "text": "ping",
            "operation_id": "mass-http",
        },
        headers={"Authorization": "Bearer secret_token"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["mode"] == "mass"
    assert body["sent"] == 1
    assert body["skipped"] == 1
    statuses = {item["telegram_chat_id"]: item["status"] for item in body["items"]}
    assert statuses[-100] == "sent"
    assert statuses[-200] == "skipped"


def test_http_send_service_command(minimal_config_yaml: str) -> None:
    backend = FakeMessageBackend()
    client = _http_client(minimal_config_yaml, message_backend=backend)
    resp = client.post(
        "/telegram/messages",
        json={
            "telegram_chat_id": -100,
            "text": "/task 12345",
            "operation_id": "svc-http",
        },
        headers={"Authorization": "Bearer secret_token"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["is_service_command"] is True
    # Real Telegram call carried the full command (Planfix bot parses the id).
    assert backend.sent[0]["text"] == "/task 12345"


def test_http_send_rejects_missing_target(minimal_config_yaml: str) -> None:
    backend = FakeMessageBackend()
    client = _http_client(minimal_config_yaml, message_backend=backend)
    resp = client.post(
        "/telegram/messages",
        json={"text": "hi"},
        headers={"Authorization": "Bearer secret_token"},
    )
    # Pydantic validation surfaces the model_validator error as 422.
    assert resp.status_code == 422


def test_http_send_resolves_chat_by_name(minimal_config_yaml: str) -> None:
    folder = FolderSnapshot(
        folder_id=2,
        folder_name="Planfix clients",
        chats=[FolderChat(chat_id=-100, title="Acme")],
    )
    backend = FakeMessageBackend()
    folder_backend = FakeFolderBackend([folder])
    client = _http_client(
        minimal_config_yaml,
        message_backend=backend,
        folder_backend=folder_backend,
    )
    resp = client.post(
        "/telegram/messages",
        json={
            "chat_name": "Acme",
            "folder_name": "Planfix clients",
            "text": "hello",
            "operation_id": "name-1",
        },
        headers={"Authorization": "Bearer secret_token"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["telegram_chat_id"] == -100
    assert backend.sent[0]["chat_id"] == -100


# ---------------------------------------------------------------------------
# CLI tests
# ---------------------------------------------------------------------------


def _write_config(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "config.yml"
    p.write_text(body)
    return p


def _patch_cli_message_backends(
    monkeypatch: pytest.MonkeyPatch,
    backend: FakeMessageBackend,
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
            # Production returns (message_backend, topic_backend, folder_backend);
            # the fake doubles as both message and topic backend.
            return backend, backend, folder_backend

        return config, _FakeManager(), store, _open

    monkeypatch.setattr(cli_main, "_build_message_backends", _factory)


def test_cli_messages_send_targeted_by_chat_id(
    minimal_config_yaml: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_file = _write_config(tmp_path, minimal_config_yaml)
    backend = FakeMessageBackend()
    folder_backend = FakeFolderBackend([])
    store = OperationStore(tmp_path / "state.db")
    _patch_cli_message_backends(monkeypatch, backend, folder_backend, store)

    runner = CliRunner()
    result = runner.invoke(
        cli_main.app,
        [
            "messages",
            "send",
            "--text",
            "hi",
            "--chat-id",
            "-100",
            "--operation-id",
            "cli-1",
            "--config",
            str(config_file),
        ],
    )
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload["telegram_chat_id"] == -100
    assert payload["mode"] == "targeted"
    assert backend.sent[0]["chat_id"] == -100


def test_cli_messages_send_mass_mode(
    minimal_config_yaml: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_file = _write_config(tmp_path, minimal_config_yaml)
    folder = FolderSnapshot(
        folder_id=2,
        folder_name="Planfix clients",
        chats=[
            FolderChat(chat_id=-100, title="A"),
            FolderChat(chat_id=-200, title="B"),
        ],
    )
    topics_per_chat = {
        -100: [TopicSummary(topic_id=11, title="Daily")],
        -200: [TopicSummary(topic_id=22, title="Other")],
    }
    backend = FakeMessageBackend(topics_per_chat=topics_per_chat)
    folder_backend = FakeFolderBackend([folder])
    store = OperationStore(tmp_path / "state.db")
    _patch_cli_message_backends(monkeypatch, backend, folder_backend, store)

    runner = CliRunner()
    result = runner.invoke(
        cli_main.app,
        [
            "messages",
            "send",
            "--text",
            "/task 99",
            "--folder-name",
            "Planfix clients",
            "--topic-name",
            "Daily",
            "--operation-id",
            "cli-mass",
            "--config",
            str(config_file),
        ],
    )
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload["mode"] == "mass"
    assert payload["sent"] == 1
    assert payload["skipped"] == 1
    assert payload["is_service_command"] is True


def test_cli_messages_send_targeted_with_topic_name_resolves(
    minimal_config_yaml: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_file = _write_config(tmp_path, minimal_config_yaml)
    topics_per_chat = {
        -100: [
            TopicSummary(topic_id=11, title="Alpha"),
            TopicSummary(topic_id=22, title="Beta"),
        ]
    }
    backend = FakeMessageBackend(topics_per_chat=topics_per_chat)
    folder_backend = FakeFolderBackend([])
    store = OperationStore(tmp_path / "state.db")
    _patch_cli_message_backends(monkeypatch, backend, folder_backend, store)

    runner = CliRunner()
    result = runner.invoke(
        cli_main.app,
        [
            "messages",
            "send",
            "--text",
            "hi topic",
            "--chat-id",
            "-100",
            "--topic-name",
            "Beta",
            "--operation-id",
            "topic-name",
            "--config",
            str(config_file),
        ],
    )
    assert result.exit_code == 0, result.stdout
    assert backend.sent[0]["topic_id"] == 22


def test_cli_messages_send_requires_chat_or_topic_for_mass(
    minimal_config_yaml: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_file = _write_config(tmp_path, minimal_config_yaml)
    backend = FakeMessageBackend()
    folder_backend = FakeFolderBackend([])
    store = OperationStore(tmp_path / "state.db")
    _patch_cli_message_backends(monkeypatch, backend, folder_backend, store)

    runner = CliRunner()
    result = runner.invoke(
        cli_main.app,
        [
            "messages",
            "send",
            "--text",
            "hi",
            "--config",
            str(config_file),
        ],
    )
    # No chat-id and no topic-name → can't decide between targeted and mass.
    assert result.exit_code == 2


# ---------------------------------------------------------------------------
# Scheduling helpers (Task 3)
# ---------------------------------------------------------------------------


def test_parse_delay_units() -> None:
    from telegram_assistant.messages import parse_delay

    assert parse_delay("30s") == 30
    assert parse_delay("10m") == 600
    assert parse_delay("2h") == 7200
    assert parse_delay("1d") == 86400


@pytest.mark.parametrize("bad", ["10x", "abc", "", "-5m", "0h", "m", "1.5h"])
def test_parse_delay_invalid(bad: str) -> None:
    from telegram_assistant.messages import ScheduleError, parse_delay

    with pytest.raises(ScheduleError):
        parse_delay(bad)


def test_resolve_schedule_at_delay_adds_to_now() -> None:
    from datetime import datetime

    from telegram_assistant.messages import resolve_schedule_at

    now = datetime(2026, 1, 1, 12, 0, 0)
    resolved = resolve_schedule_at(delay_seconds=600, now=now)
    assert resolved == datetime(2026, 1, 1, 12, 10, 0)


def test_resolve_schedule_at_delay_defaults_to_utc() -> None:
    from datetime import UTC, datetime, timedelta

    from telegram_assistant.messages import resolve_schedule_at

    before = datetime.now(UTC)
    resolved = resolve_schedule_at(delay_seconds=600)
    after = datetime.now(UTC)
    assert resolved is not None
    assert resolved.tzinfo == UTC
    assert before + timedelta(seconds=600) <= resolved <= after + timedelta(seconds=600)


def test_resolve_schedule_at_delay_overflow_is_schedule_error() -> None:
    from telegram_assistant.messages import ScheduleError, resolve_schedule_at

    with pytest.raises(ScheduleError):
        resolve_schedule_at(delay_seconds=10**20)


def test_resolve_schedule_at_rejects_both_modes() -> None:
    from datetime import datetime

    from telegram_assistant.messages import ScheduleError, resolve_schedule_at

    with pytest.raises(ScheduleError):
        resolve_schedule_at(
            schedule_at=datetime(2030, 1, 1), delay_seconds=60, now=datetime(2026, 1, 1)
        )


def test_resolve_schedule_at_rejects_past() -> None:
    from datetime import datetime

    from telegram_assistant.messages import ScheduleError, resolve_schedule_at

    now = datetime(2026, 1, 1, 12, 0, 0)
    with pytest.raises(ScheduleError):
        resolve_schedule_at(schedule_at=datetime(2026, 1, 1, 11, 0, 0), now=now)


def test_resolve_schedule_at_future_passes_through() -> None:
    from datetime import UTC, datetime

    from telegram_assistant.messages import resolve_schedule_at

    now = datetime(2026, 1, 1, 12, 0, 0)
    future = datetime(2026, 1, 1, 13, 0, 0)
    assert resolve_schedule_at(schedule_at=future, now=now) == future.replace(
        tzinfo=UTC
    )


def test_parse_schedule_at_invalid() -> None:
    from telegram_assistant.messages import ScheduleError, parse_schedule_at

    with pytest.raises(ScheduleError):
        parse_schedule_at("not-a-date")


@pytest.mark.parametrize("url", ["http://", "http:foo", "https:///x"])
def test_validate_file_urls_rejects_missing_host(url: str) -> None:
    from telegram_assistant.messages import AttachmentError, validate_file_urls

    with pytest.raises(AttachmentError):
        validate_file_urls([url])


# ---------------------------------------------------------------------------
# CLI media / scheduled sends (Task 3)
# ---------------------------------------------------------------------------


def test_cli_messages_send_dry_run_includes_attachments_and_schedule(
    minimal_config_yaml: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_file = _write_config(tmp_path, minimal_config_yaml)
    media = tmp_path / "pic.txt"
    media.write_text("hello")
    backend = FakeMessageBackend()
    folder_backend = FakeFolderBackend([])
    store = OperationStore(tmp_path / "state.db")
    _patch_cli_message_backends(monkeypatch, backend, folder_backend, store)

    runner = CliRunner()
    result = runner.invoke(
        cli_main.app,
        [
            "messages",
            "send",
            "--text",
            "caption",
            "--chat-id",
            "-100",
            "--file",
            str(media),
            "--file-url",
            "https://example.com/a.jpg",
            "--delay",
            "10m",
            "--dry-run",
            "--config",
            str(config_file),
        ],
    )
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    resolved = payload["resolved"]
    assert resolved["files"] == [str(media)]
    assert resolved["file_urls"] == ["https://example.com/a.jpg"]
    assert resolved["scheduled"] is True
    assert resolved["schedule_at"] is not None
    # Dry-run must not touch the backend.
    assert backend.sent == []


def test_cli_messages_send_media_real(
    minimal_config_yaml: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_file = _write_config(tmp_path, minimal_config_yaml)
    media = tmp_path / "doc.txt"
    media.write_text("content")
    backend = FakeMessageBackend()
    folder_backend = FakeFolderBackend([])
    store = OperationStore(tmp_path / "state.db")
    _patch_cli_message_backends(monkeypatch, backend, folder_backend, store)

    runner = CliRunner()
    result = runner.invoke(
        cli_main.app,
        [
            "messages",
            "send",
            "--text",
            "",
            "--chat-id",
            "-100",
            "--file",
            str(media),
            "--operation-id",
            "media-1",
            "--config",
            str(config_file),
        ],
    )
    assert result.exit_code == 0, result.stdout
    assert backend.sent[0]["files"] == (str(media),)
    assert backend.sent[0]["chat_id"] == -100


def test_cli_messages_send_media_only_does_not_require_text(
    minimal_config_yaml: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_file = _write_config(tmp_path, minimal_config_yaml)
    media = tmp_path / "doc.txt"
    media.write_text("content")
    backend = FakeMessageBackend()
    folder_backend = FakeFolderBackend([])
    store = OperationStore(tmp_path / "state.db")
    _patch_cli_message_backends(monkeypatch, backend, folder_backend, store)

    runner = CliRunner()
    result = runner.invoke(
        cli_main.app,
        [
            "messages",
            "send",
            "--chat-id",
            "-100",
            "--file",
            str(media),
            "--operation-id",
            "media-no-text",
            "--config",
            str(config_file),
        ],
    )

    assert result.exit_code == 0, result.stdout
    assert backend.sent[0]["text"] == ""
    assert backend.sent[0]["files"] == (str(media),)


def test_cli_messages_send_scheduled_real(
    minimal_config_yaml: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_file = _write_config(tmp_path, minimal_config_yaml)
    backend = FakeMessageBackend()
    folder_backend = FakeFolderBackend([])
    store = OperationStore(tmp_path / "state.db")
    _patch_cli_message_backends(monkeypatch, backend, folder_backend, store)

    runner = CliRunner()
    result = runner.invoke(
        cli_main.app,
        [
            "messages",
            "send",
            "--text",
            "later",
            "--chat-id",
            "-100",
            "--delay",
            "2h",
            "--operation-id",
            "sched-1",
            "--config",
            str(config_file),
        ],
    )
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload["scheduled"] is True
    assert payload["schedule_at"] is not None
    assert backend.sent[0]["schedule_at"] is not None


def test_cli_messages_send_rejects_past_schedule(
    minimal_config_yaml: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_file = _write_config(tmp_path, minimal_config_yaml)
    backend = FakeMessageBackend()
    folder_backend = FakeFolderBackend([])
    store = OperationStore(tmp_path / "state.db")
    _patch_cli_message_backends(monkeypatch, backend, folder_backend, store)

    runner = CliRunner()
    result = runner.invoke(
        cli_main.app,
        [
            "messages",
            "send",
            "--text",
            "hi",
            "--chat-id",
            "-100",
            "--schedule-at",
            "2000-01-01T00:00:00",
            "--config",
            str(config_file),
        ],
    )
    assert result.exit_code == 2
    assert backend.sent == []


def test_cli_messages_send_rejects_invalid_delay(
    minimal_config_yaml: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_file = _write_config(tmp_path, minimal_config_yaml)
    backend = FakeMessageBackend()
    folder_backend = FakeFolderBackend([])
    store = OperationStore(tmp_path / "state.db")
    _patch_cli_message_backends(monkeypatch, backend, folder_backend, store)

    runner = CliRunner()
    result = runner.invoke(
        cli_main.app,
        [
            "messages",
            "send",
            "--text",
            "hi",
            "--chat-id",
            "-100",
            "--delay",
            "10x",
            "--config",
            str(config_file),
        ],
    )
    assert result.exit_code == 2


def test_cli_messages_send_rejects_missing_local_file(
    minimal_config_yaml: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_file = _write_config(tmp_path, minimal_config_yaml)
    backend = FakeMessageBackend()
    folder_backend = FakeFolderBackend([])
    store = OperationStore(tmp_path / "state.db")
    _patch_cli_message_backends(monkeypatch, backend, folder_backend, store)

    runner = CliRunner()
    result = runner.invoke(
        cli_main.app,
        [
            "messages",
            "send",
            "--text",
            "hi",
            "--chat-id",
            "-100",
            "--file",
            str(tmp_path / "missing.bin"),
            "--config",
            str(config_file),
        ],
    )
    assert result.exit_code == 2


# ---------------------------------------------------------------------------
# HTTP media / scheduled sends (Task 3)
# ---------------------------------------------------------------------------


def test_http_send_media_via_urls(minimal_config_yaml: str) -> None:
    import os
    from collections.abc import AsyncIterator

    backend = FakeMessageBackend()
    client = _http_client(minimal_config_yaml, message_backend=backend)

    # Inject a fake fetcher so file_urls are downloaded to temp files without
    # real network traffic; the body of each temp file is the URL itself.
    async def _fetch(url: str, timeout: float) -> AsyncIterator[bytes]:
        yield url.encode()

    client.app.state.attachment_fetcher = _fetch

    resp = client.post(
        "/telegram/messages",
        json={
            "telegram_chat_id": -100,
            "text": "caption",
            "file_urls": [
                "https://example.com/a.jpg",
                "https://example.com/b.jpg",
            ],
            "operation_id": "http-media",
        },
        headers={"Authorization": "Bearer secret_token"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # Two attachments → album → list of ids.
    assert body["telegram_message_ids"] is not None
    assert len(body["telegram_message_ids"]) == 2
    # The backend received local temp paths (downloaded), not the URLs, and the
    # temp files are cleaned up after the send.
    sent_files = backend.sent[0]["files"]
    assert len(sent_files) == 2
    for path in sent_files:
        assert path.startswith("https://") is False
        assert not os.path.exists(path)


def test_http_send_rejects_server_local_files(minimal_config_yaml: str, tmp_path: Path) -> None:
    media = tmp_path / "secret.txt"
    media.write_text("do not upload")
    backend = FakeMessageBackend()
    client = _http_client(minimal_config_yaml, message_backend=backend)

    resp = client.post(
        "/telegram/messages",
        json={
            "telegram_chat_id": -100,
            "files": [str(media)],
            "operation_id": "http-local-file",
        },
        headers={"Authorization": "Bearer secret_token"},
    )

    assert resp.status_code == 400, resp.text
    assert "file_urls" in resp.json()["detail"]
    assert backend.sent == []


def test_http_send_denied_before_local_file_handling(
    tmp_path: Path,
) -> None:
    media = tmp_path / "missing-secret.txt"
    backend = FakeMessageBackend()
    access_config = """
telegram:
  api_id: 123456
  api_hash: "telegram_api_hash"
  session_path: /data/telegram-assistant.session
  default_chat_folder:
    folder_id: 2
    folder_name: "Planfix clients"
  access:
    rules: []
http:
  host: "0.0.0.0"
  port: 8085
  bearer_token: "secret_token"
logging:
  level: INFO
"""
    client = _http_client(access_config, message_backend=backend)

    resp = client.post(
        "/telegram/messages",
        json={
            "telegram_chat_id": -100,
            "files": [str(media)],
            "operation_id": "http-local-file-denied",
        },
        headers={"Authorization": "Bearer secret_token"},
    )

    assert resp.status_code == 403, resp.text
    assert backend.sent == []


def test_http_send_scheduled_via_delay(minimal_config_yaml: str) -> None:
    backend = FakeMessageBackend()
    client = _http_client(minimal_config_yaml, message_backend=backend)
    resp = client.post(
        "/telegram/messages",
        json={
            "telegram_chat_id": -100,
            "text": "later",
            "delay_seconds": 3600,
            "operation_id": "http-sched",
        },
        headers={"Authorization": "Bearer secret_token"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["scheduled"] is True
    assert body["schedule_at"] is not None
    assert backend.sent[0]["schedule_at"] is not None


def test_http_send_rejects_past_schedule(minimal_config_yaml: str) -> None:
    backend = FakeMessageBackend()
    client = _http_client(minimal_config_yaml, message_backend=backend)
    resp = client.post(
        "/telegram/messages",
        json={
            "telegram_chat_id": -100,
            "text": "hi",
            "schedule_at": "2000-01-01T00:00:00",
        },
        headers={"Authorization": "Bearer secret_token"},
    )
    assert resp.status_code == 400, resp.text
    assert backend.sent == []


def test_http_send_rejects_conflicting_schedule_modes(
    minimal_config_yaml: str,
) -> None:
    backend = FakeMessageBackend()
    client = _http_client(minimal_config_yaml, message_backend=backend)
    resp = client.post(
        "/telegram/messages",
        json={
            "telegram_chat_id": -100,
            "text": "hi",
            "schedule_at": "2030-01-01T00:00:00",
            "delay_seconds": 60,
        },
        headers={"Authorization": "Bearer secret_token"},
    )
    assert resp.status_code == 422, resp.text


def test_http_send_rejects_unrepresentable_delay(minimal_config_yaml: str) -> None:
    backend = FakeMessageBackend()
    client = _http_client(minimal_config_yaml, message_backend=backend)
    resp = client.post(
        "/telegram/messages",
        json={
            "telegram_chat_id": -100,
            "text": "hi",
            "delay_seconds": 10**20,
        },
        headers={"Authorization": "Bearer secret_token"},
    )
    assert resp.status_code in {400, 422}, resp.text
    assert backend.sent == []


def test_http_send_rejects_media_in_mass_mode(minimal_config_yaml: str) -> None:
    backend = FakeMessageBackend()
    folder_backend = FakeFolderBackend([])
    client = _http_client(
        minimal_config_yaml,
        message_backend=backend,
        topic_backend=backend,
        folder_backend=folder_backend,
    )
    resp = client.post(
        "/telegram/messages",
        json={
            "folder_name": "Planfix clients",
            "topic_name": "Daily",
            "text": "ping",
            "file_urls": ["https://example.com/a.jpg"],
        },
        headers={"Authorization": "Bearer secret_token"},
    )
    assert resp.status_code == 422, resp.text
