"""Task 3 — paragraph spacing wired into ``send_message``.

Covers the domain side (normalisation runs before the length check, warnings
reach the result and the persisted payload, ``spaced_paragraphs=False`` is a
byte-for-byte no-op, the only-when-set kwarg contract survives) and the config
knob ``telegram.defaults.rich_markdown_spaced_paragraphs`` as the surfaces read
it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from telegram_assistant.config import load_config_from_text
from telegram_assistant.http_api import create_app
from telegram_assistant.messages import (
    MAX_RICH_BLOCKS,
    MAX_RICH_MARKDOWN_CHARS,
    NBSP,
    SendMessageRequest,
    SendMessageResult,
    send_message,
    spaced_paragraphs_default,
)
from telegram_assistant.persistence import OperationStatus, OperationStore

AUTH = {"Authorization": "Bearer secret_token"}


class RecordingBackend:
    """MessageBackend fake recording every kwarg the service passes."""

    def __init__(self) -> None:
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
        rich_markdown: str | None = None,
    ) -> int:
        self.sent.append(
            {
                "chat_id": chat_id,
                "text": text,
                "topic_id": topic_id,
                "files": tuple(files),
                "rich_markdown": rich_markdown,
            }
        )
        return 777


class LegacyBackend:
    """Backend predating rich sends — its signature has no newer kwargs."""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def send_message(
        self, *, chat_id: int, text: str, topic_id: int | None = None
    ) -> int:
        self.sent.append({"chat_id": chat_id, "text": text, "topic_id": topic_id})
        return 7


@pytest.fixture()
def store(tmp_path: Path) -> OperationStore:
    return OperationStore(tmp_path / "state.db")


def _paragraphs(count: int, width: int = 8) -> str:
    return "\n\n".join(["x" * width] * count)


# ---------------------------------------------------------------------------
# Domain
# ---------------------------------------------------------------------------


async def test_send_message_spaces_paragraphs_by_default(
    store: OperationStore,
) -> None:
    backend = RecordingBackend()
    result, op = await send_message(
        backend=backend,
        store=store,
        request=SendMessageRequest(
            telegram_chat_id=-100,
            text="",
            rich_markdown="one\n\ntwo\n\n## Section\n",
            operation_id="spacing-default",
        ),
    )

    expected = f"one\n\n{NBSP}\n\ntwo\n\n{NBSP}\n\n## Section\n"
    assert backend.sent[0]["rich_markdown"] == expected
    assert result.warnings == ()
    # The audit trail records what actually went to Telegram, spacers included.
    assert op.request_payload["rich_markdown"] == expected
    assert op.request_payload["spaced_paragraphs"] is True


async def test_send_message_spaced_paragraphs_false_sends_byte_for_byte(
    store: OperationStore,
) -> None:
    """Opting out is a strict no-op — CRLF and the trailing newline survive."""
    backend = RecordingBackend()
    markdown = "one\r\n\r\ntwo\r\n\r\n## Section\r\n"
    result, op = await send_message(
        backend=backend,
        store=store,
        request=SendMessageRequest(
            telegram_chat_id=-100,
            text="",
            rich_markdown=markdown,
            spaced_paragraphs=False,
            operation_id="spacing-off",
        ),
    )

    assert backend.sent[0]["rich_markdown"] == markdown
    assert result.warnings == ()
    assert op.request_payload["rich_markdown"] == markdown
    assert op.request_payload["spaced_paragraphs"] is False


async def test_send_message_length_check_runs_after_spacing(
    store: OperationStore,
) -> None:
    """An article that fits only *unspaced* is rejected, naming the size that
    would actually be sent — spacing happens before the bound is applied."""
    # 250 paragraphs stay inside the 500-block budget, so spacing is not rolled
    # back; the 249 spacers push the source past the character limit.
    markdown = _paragraphs(250, width=128)
    assert len(markdown) <= MAX_RICH_MARKDOWN_CHARS
    backend = RecordingBackend()

    with pytest.raises(ValueError) as excinfo:
        await send_message(
            backend=backend,
            store=store,
            request=SendMessageRequest(
                telegram_chat_id=-100,
                text="",
                rich_markdown=markdown,
                operation_id="spacing-too-long",
            ),
        )

    message = str(excinfo.value)
    assert str(MAX_RICH_MARKDOWN_CHARS) in message
    assert "after paragraph spacing" in message
    assert str(len(markdown) + 249 * (len(NBSP) + 2)) in message
    assert backend.sent == []
    # The same article sent unspaced fits and goes through.
    result, _ = await send_message(
        backend=backend,
        store=store,
        request=SendMessageRequest(
            telegram_chat_id=-100,
            text="",
            rich_markdown=markdown,
            spaced_paragraphs=False,
            operation_id="spacing-too-long-off",
        ),
    )
    assert result.telegram_message_id == 777
    assert backend.sent[0]["rich_markdown"] == markdown


async def test_send_message_oversize_without_spacing_omits_spacing_note(
    store: OperationStore,
) -> None:
    """A source that was already too long is not blamed on the spacer pass."""
    backend = RecordingBackend()
    with pytest.raises(ValueError) as excinfo:
        await send_message(
            backend=backend,
            store=store,
            request=SendMessageRequest(
                telegram_chat_id=-100,
                text="",
                rich_markdown="x" * (MAX_RICH_MARKDOWN_CHARS + 1),
                operation_id="spacing-source-too-long",
            ),
        )
    assert "after paragraph spacing" not in str(excinfo.value)
    assert backend.sent == []


async def test_send_message_length_check_names_wikilink_expansion(
    store: OperationStore,
) -> None:
    """Wikilink expansion can grow the source: three-or-more ``#`` in a target
    turns each into ``" > "`` (+2 chars net) once the surrounding ``[[``/``]]``
    (-4 chars) is removed. A source under the limit whose expansion pushes it
    over must name the pass that grew it — not the passes that never ran."""
    # Each unit is net +2 chars after expansion ("[[a#b#c#d]] " -> "a > b > c > d ").
    unit = "[[a#b#c#d]] "
    count = (MAX_RICH_MARKDOWN_CHARS // len(unit)) - 1
    markdown = unit * count
    assert len(markdown) <= MAX_RICH_MARKDOWN_CHARS
    backend = RecordingBackend()

    with pytest.raises(ValueError) as excinfo:
        await send_message(
            backend=backend,
            store=store,
            request=SendMessageRequest(
                telegram_chat_id=-100,
                text="",
                rich_markdown=markdown,
                spaced_paragraphs=False,
                line_breaks=False,
                media_grouping="none",
                operation_id="wikilink-too-long",
            ),
        )

    message = str(excinfo.value)
    assert str(MAX_RICH_MARKDOWN_CHARS) in message
    assert "after wikilink expansion" in message
    assert "paragraph spacing" not in message
    assert backend.sent == []


async def test_send_message_block_limit_fallback_warns_and_still_sends(
    store: OperationStore,
) -> None:
    """Cosmetics must not break a send: spacing is dropped with a warning, the
    article goes out unspaced, and the warning is replayed with the result."""
    markdown = _paragraphs(300)
    backend = RecordingBackend()
    request = SendMessageRequest(
        telegram_chat_id=-100,
        text="",
        rich_markdown=markdown,
        operation_id="spacing-blocks",
    )
    result, op = await send_message(backend=backend, store=store, request=request)

    assert backend.sent[0]["rich_markdown"] == markdown
    assert op.status is OperationStatus.COMPLETED
    assert any(
        "spaced_paragraphs disabled" in w and str(MAX_RICH_BLOCKS) in w
        for w in result.warnings
    ), result.warnings
    assert op.result_payload is not None
    assert op.result_payload["warnings"] == list(result.warnings)

    # A replay of the completed op reports the same warnings.
    replayed, _ = await send_message(
        backend=RecordingBackend(), store=store, request=request
    )
    assert replayed.replayed is True
    assert replayed.warnings == result.warnings


async def test_send_message_plain_send_has_no_warnings(store: OperationStore) -> None:
    backend = RecordingBackend()
    result, _ = await send_message(
        backend=backend,
        store=store,
        request=SendMessageRequest(
            telegram_chat_id=-100, text="hello", operation_id="plain-warnings"
        ),
    )
    assert result.warnings == ()
    assert result.to_dict()["warnings"] == []


async def test_send_message_legacy_backend_never_sees_new_kwargs(
    store: OperationStore,
) -> None:
    """``spaced_paragraphs`` is a request-level knob, not a backend kwarg — a
    backend predating rich sends still receives the original signature."""
    backend = LegacyBackend()
    result, _ = await send_message(
        backend=backend,
        store=store,
        request=SendMessageRequest(
            telegram_chat_id=-100,
            text="hello",
            spaced_paragraphs=False,
            operation_id="legacy-spacing",
        ),
    )
    assert result.telegram_message_id == 7
    assert backend.sent == [{"chat_id": -100, "text": "hello", "topic_id": None}]


def test_send_message_result_warnings_round_trip() -> None:
    result = SendMessageResult(
        telegram_chat_id=-100,
        telegram_topic_id=None,
        telegram_message_id=5,
        is_service_command=False,
        warnings=("careful",),
    )
    assert result.to_dict()["warnings"] == ["careful"]
    assert SendMessageResult.from_dict(result.to_dict()).warnings == ("careful",)
    # A row written before warnings existed simply carries none.
    legacy = dict(result.to_dict())
    legacy.pop("warnings")
    assert SendMessageResult.from_dict(legacy).warnings == ()


# ---------------------------------------------------------------------------
# Config knob
# ---------------------------------------------------------------------------


def _config_yaml(*, spaced: bool | None = None) -> str:
    lines = [
        "telegram:",
        "  api_id: 123456",
        '  api_hash: "telegram_api_hash"',
        "  session_path: /data/telegram-assistant.session",
        "  default_chat_folder:",
        "    folder_id: 2",
        '    folder_name: "Planfix clients"',
        "  defaults:",
        "    enable_topics: true",
    ]
    if spaced is not None:
        lines.append(f"    rich_markdown_spaced_paragraphs: {str(spaced).lower()}")
    lines += [
        "http:",
        '  host: "0.0.0.0"',
        "  port: 8085",
        '  bearer_token: "secret_token"',
        "logging:",
        "  level: INFO",
    ]
    return "\n".join(lines)


@pytest.mark.parametrize(
    ("spaced", "expected"),
    [(None, True), (True, True), (False, False)],
    ids=["absent", "true", "false"],
)
def test_spaced_paragraphs_default_reads_config(
    spaced: bool | None, expected: bool
) -> None:
    config = load_config_from_text(_config_yaml(spaced=spaced))
    assert config.telegram.defaults.rich_markdown_spaced_paragraphs is expected
    assert spaced_paragraphs_default(config) is expected


def test_spaced_paragraphs_default_without_config_is_true() -> None:
    """A missing config (or one predating the knob) keeps the built-in default."""
    assert spaced_paragraphs_default(None) is True
    assert spaced_paragraphs_default(object()) is True


def _client(backend: RecordingBackend, tmp_path: Path, *, spaced: bool | None) -> TestClient:
    app = create_app(
        load_config_from_text(_config_yaml(spaced=spaced)),
        session_manager=None,
        message_backend_factory=lambda _r: backend,
        topic_backend_factory=lambda _r: backend,
        operation_store=OperationStore(tmp_path / "http.db"),
    )
    return TestClient(app)


@pytest.mark.parametrize(
    ("spaced", "spacer_expected"),
    [(None, True), (False, False)],
    ids=["default", "config_off"],
)
def test_http_rich_send_respects_config_default(
    tmp_path: Path, spaced: bool | None, spacer_expected: bool
) -> None:
    backend = RecordingBackend()
    client = _client(backend, tmp_path, spaced=spaced)
    resp = client.post(
        "/telegram/messages",
        json={
            "telegram_chat_id": -100,
            "rich_markdown": "one\n\ntwo\n",
            "operation_id": f"http-spacing-{spaced}",
        },
        headers=AUTH,
    )
    assert resp.status_code == 200, resp.text
    sent = backend.sent[0]["rich_markdown"]
    assert (NBSP in sent) is spacer_expected
    if not spacer_expected:
        assert sent == "one\n\ntwo\n"


@pytest.mark.parametrize(
    ("spaced", "spacer_expected"),
    [(None, True), (False, False)],
    ids=["default", "config_off"],
)
def test_cli_rich_send_respects_config_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    spaced: bool | None,
    spacer_expected: bool,
) -> None:
    from typer.testing import CliRunner

    from telegram_assistant.cli import main as cli_main

    config_file = tmp_path / "config.yml"
    config_file.write_text(_config_yaml(spaced=spaced))
    md_file = tmp_path / "article.md"
    md_file.write_text("one\n\ntwo\n", encoding="utf-8")
    backend = RecordingBackend()
    store = OperationStore(tmp_path / "cli.db")

    class _FakeManager:
        async def disconnect(self) -> None:
            return None

    def _factory(config_path: Path | None) -> Any:
        from telegram_assistant.config import load_config

        async def _open() -> Any:
            return backend, backend, None

        return load_config(config_path), _FakeManager(), store, _open

    monkeypatch.setattr(cli_main, "_build_message_backends", _factory)

    result = CliRunner().invoke(
        cli_main.app,
        [
            "messages",
            "send",
            "--chat-id",
            "-100",
            "--rich-markdown",
            str(md_file),
            "--operation-id",
            f"cli-spacing-{spaced}",
            "--config",
            str(config_file),
        ],
    )
    assert result.exit_code == 0, (result.stdout or "") + (result.stderr or "")
    sent = backend.sent[0]["rich_markdown"]
    assert (NBSP in sent) is spacer_expected
    if not spacer_expected:
        assert sent == "one\n\ntwo\n"
