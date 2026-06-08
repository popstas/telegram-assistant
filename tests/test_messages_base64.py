"""Unit tests for base64 inline attachments on the send path (Task 11)."""

from __future__ import annotations

import base64
import os
from pathlib import Path
from typing import Any

import pytest

from telegram_assistant.messages import (
    Base64Attachment,
    SendMessageRequest,
    decode_base64_attachment,
    materialize_base64_attachments,
    send_message,
)
from telegram_assistant.messages.attachments import AttachmentError
from telegram_assistant.persistence import OperationStore

# ---------------------------------------------------------------------------
# decode_base64_attachment (pure validation/decoding)
# ---------------------------------------------------------------------------


def test_decode_base64_attachment_returns_bytes() -> None:
    payload = b"hello base64 world"
    encoded = base64.b64encode(payload).decode("ascii")
    data = decode_base64_attachment(
        filename="note.txt", content_b64=encoded, mime="text/plain"
    )
    assert data == payload


def test_decode_base64_attachment_rejects_missing_filename() -> None:
    encoded = base64.b64encode(b"x").decode("ascii")
    with pytest.raises(AttachmentError) as excinfo:
        decode_base64_attachment(filename="  ", content_b64=encoded)
    assert "filename" in str(excinfo.value)


def test_decode_base64_attachment_rejects_bad_base64() -> None:
    with pytest.raises(AttachmentError) as excinfo:
        decode_base64_attachment(filename="a.png", content_b64="not!valid!base64")
    assert "valid base64" in str(excinfo.value)


def test_decode_base64_attachment_rejects_empty_content() -> None:
    with pytest.raises(AttachmentError):
        decode_base64_attachment(filename="a.png", content_b64="   ")


def test_decode_base64_attachment_rejects_oversize_decoded() -> None:
    encoded = base64.b64encode(b"x" * 100).decode("ascii")
    with pytest.raises(AttachmentError) as excinfo:
        decode_base64_attachment(
            filename="big.bin", content_b64=encoded, max_bytes=50
        )
    assert "limit" in str(excinfo.value)


def test_decode_base64_attachment_rejects_oversize_by_encoded_precheck() -> None:
    # A very long encoded string is rejected before it is decoded.
    encoded = base64.b64encode(b"y" * 10_000).decode("ascii")
    with pytest.raises(AttachmentError) as excinfo:
        decode_base64_attachment(
            filename="big.bin", content_b64=encoded, max_bytes=64
        )
    assert "limit" in str(excinfo.value)


def test_decode_base64_attachment_rejects_disallowed_mime() -> None:
    encoded = base64.b64encode(b"x").decode("ascii")
    with pytest.raises(AttachmentError) as excinfo:
        decode_base64_attachment(
            filename="a.bin", content_b64=encoded, mime="weird/thing"
        )
    assert "not allowed" in str(excinfo.value)


def test_decode_base64_attachment_rejects_malformed_mime() -> None:
    encoded = base64.b64encode(b"x").decode("ascii")
    with pytest.raises(AttachmentError):
        decode_base64_attachment(
            filename="a.bin", content_b64=encoded, mime="notamime"
        )


def test_decode_base64_attachment_allows_absent_mime() -> None:
    encoded = base64.b64encode(b"data").decode("ascii")
    assert decode_base64_attachment(filename="a.bin", content_b64=encoded) == b"data"


# ---------------------------------------------------------------------------
# materialize_base64_attachments (temp-file IO)
# ---------------------------------------------------------------------------


def test_materialize_writes_files_preserving_name() -> None:
    atts = [
        Base64Attachment(
            filename="report.pdf",
            content_b64=base64.b64encode(b"PDF-BYTES").decode("ascii"),
            mime="application/pdf",
        ),
        Base64Attachment(
            filename="pic.png",
            content_b64=base64.b64encode(b"PNG-BYTES").decode("ascii"),
        ),
    ]
    tmpdir, paths = materialize_base64_attachments(atts)
    try:
        assert tmpdir is not None and os.path.isdir(tmpdir)
        assert len(paths) == 2
        # Original filenames are preserved (with an index prefix to avoid
        # collisions) inside the temp dir.
        assert os.path.basename(paths[0]).endswith("report.pdf")
        assert os.path.basename(paths[1]).endswith("pic.png")
        with open(paths[0], "rb") as fh:
            assert fh.read() == b"PDF-BYTES"
    finally:
        import shutil

        shutil.rmtree(tmpdir, ignore_errors=True)


def test_materialize_strips_path_traversal_in_filename() -> None:
    """A crafted ``filename`` with directory components must not escape tmpdir."""
    atts = [
        Base64Attachment(
            filename="../../etc/evil.txt",
            content_b64=base64.b64encode(b"EVIL").decode("ascii"),
        ),
    ]
    tmpdir, paths = materialize_base64_attachments(atts)
    try:
        assert tmpdir is not None
        # The written file stays inside tmpdir (in a per-index subdir); no
        # parent escape, and the basename is the real filename so Telegram
        # shows it unprefixed.
        real_tmp = os.path.realpath(tmpdir)
        real_dest = os.path.realpath(paths[0])
        assert os.path.commonpath([real_tmp, real_dest]) == real_tmp
        assert os.path.dirname(real_dest) == os.path.join(real_tmp, "0")
        assert os.path.basename(paths[0]) == "evil.txt"
    finally:
        import shutil

        shutil.rmtree(tmpdir, ignore_errors=True)


def test_materialize_empty_returns_none() -> None:
    tmpdir, paths = materialize_base64_attachments([])
    assert tmpdir is None
    assert paths == []


def test_materialize_cleans_up_on_failure() -> None:
    captured: dict[str, Any] = {}
    import telegram_assistant.messages.attachments as attachments_mod

    real_mkdtemp = attachments_mod.tempfile.mkdtemp

    def _spy(*args: Any, **kwargs: Any) -> str:
        path = real_mkdtemp(*args, **kwargs)
        captured["dir"] = path
        return path

    attachments_mod.tempfile.mkdtemp = _spy  # type: ignore[assignment]
    try:
        bad = [
            Base64Attachment(filename="ok.txt", content_b64="not!base64!"),
        ]
        with pytest.raises(AttachmentError):
            materialize_base64_attachments(bad)
    finally:
        attachments_mod.tempfile.mkdtemp = real_mkdtemp  # type: ignore[assignment]
    # The temp dir created before the failure must be gone.
    assert "dir" in captured
    assert not os.path.exists(captured["dir"])


# ---------------------------------------------------------------------------
# send_message integration
# ---------------------------------------------------------------------------


class _RecordingBackend:
    """Records the file paths and their content at send time."""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self._next_id = 500

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
        files = tuple(files)
        contents = []
        for path in files:
            assert os.path.isfile(path), f"temp file missing at send time: {path}"
            with open(path, "rb") as fh:
                contents.append(fh.read())
        msg_id = self._next_id
        self._next_id += 1
        self.sent.append(
            {
                "chat_id": chat_id,
                "text": text,
                "files": files,
                "contents": contents,
            }
        )
        return msg_id


@pytest.fixture()
def store(tmp_path: Path) -> OperationStore:
    return OperationStore(tmp_path / "state.db")


async def test_send_message_base64_attachment_sent_and_cleaned_up(
    store: OperationStore,
) -> None:
    backend = _RecordingBackend()
    payload = b"inline file payload"
    req = SendMessageRequest(
        telegram_chat_id=42,
        text="caption",
        base64_files=(
            Base64Attachment(
                filename="hello.txt",
                content_b64=base64.b64encode(payload).decode("ascii"),
                mime="text/plain",
            ),
        ),
    )
    result, op = await send_message(backend=backend, store=store, request=req)

    assert result.telegram_message_id == 500
    # The backend saw exactly one temp file whose bytes matched the input.
    sent = backend.sent[0]
    assert len(sent["files"]) == 1
    assert sent["contents"][0] == payload
    # The original filename is preserved in the temp path.
    assert os.path.basename(sent["files"][0]).endswith("hello.txt")
    # The temp dir/file is cleaned up after the send completes.
    assert not os.path.exists(sent["files"][0])
    # Content is never persisted in the operation payload — metadata only.
    assert op.request_payload["base64_files"] == [
        {"filename": "hello.txt", "mime": "text/plain"}
    ]


async def test_send_message_base64_oversize_rejected_before_op(
    store: OperationStore,
) -> None:
    backend = _RecordingBackend()
    big = base64.b64encode(b"z" * 200).decode("ascii")
    req = SendMessageRequest(
        telegram_chat_id=7,
        text="",
        base64_files=(
            Base64Attachment(filename="big.bin", content_b64=big, mime="application/octet-stream"),
        ),
        base64_max_bytes=50,
    )
    with pytest.raises(AttachmentError):
        await send_message(backend=backend, store=store, request=req)
    # Nothing was sent.
    assert backend.sent == []


async def test_send_message_base64_bad_input_rejected(
    store: OperationStore,
) -> None:
    backend = _RecordingBackend()
    req = SendMessageRequest(
        telegram_chat_id=7,
        text="hi",
        base64_files=(
            Base64Attachment(filename="x.png", content_b64="!!notbase64!!"),
        ),
    )
    with pytest.raises(AttachmentError):
        await send_message(backend=backend, store=store, request=req)
    assert backend.sent == []
