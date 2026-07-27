"""Task 6 — local media in a rich message: resolution, validation, CLI wiring.

``scan_media`` is the domain half: it classifies each media block as remote
(left byte-for-byte for the server to fetch) or local, resolves the local ones
against the article's directory / an Obsidian vault / explicit overrides, and
rewrites the reference into the ``tg://photo?id=…`` form the live API proved in
Task 5. ``send_message`` is the gate: ``rich_files`` only alongside
``rich_markdown``, files that exist and are readable, at most 50.

The CLI is the only surface that resolves local paths (it is the local, trusted
one), so its ``--rich-file``/``--vault-dir`` flags and the dry-run listing are
covered here too.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from telegram_assistant.cli import main as cli_main
from telegram_assistant.messages import (
    MAX_RICH_MEDIA,
    AmbiguousMediaError,
    MediaResolutionError,
    RichFile,
    SendMessageRequest,
    make_rich_file_id,
    media_kind,
    rich_file_reference,
    scan_media,
    send_message,
)
from telegram_assistant.persistence import OperationStore, idempotency
from telegram_assistant.topics import TopicSummary

PNG = b"\x89PNG\r\n\x1a\n"


def _touch(path: Path, content: bytes = PNG) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("photo.png", "photo"),
        ("PHOTO.JPG", "photo"),
        ("clip.mp4", "video"),
        # An animation is a document, and the dialect has no animation scheme.
        ("loop.gif", "video"),
        ("voice.ogg", "audio"),
        ("song.mp3", "audio"),
    ],
)
def test_media_kind_by_suffix(name: str, expected: str) -> None:
    assert media_kind(name) == expected


def test_media_kind_rejects_a_type_with_no_reference_syntax() -> None:
    with pytest.raises(MediaResolutionError, match="unsupported media type"):
        media_kind("report.pdf")


def test_rich_file_reference_uses_the_proven_scheme() -> None:
    assert rich_file_reference("a1", "photo") == "tg://photo?id=a1"
    assert rich_file_reference("a1", "video") == "tg://video?id=a1"
    assert rich_file_reference("a1", "audio") == "tg://audio?id=a1"


def test_rich_file_reference_rejects_an_unknown_kind() -> None:
    with pytest.raises(MediaResolutionError, match="unknown media kind"):
        rich_file_reference("a1", "sticker")


@pytest.mark.parametrize(
    ("stem", "expected"),
    [
        ("photo", "photo"),
        ("Pasted image 20260101", "Pasted-image-20260101"),
        # The id is written straight into ![](…): non-ASCII must not leak in.
        ("Пхукет", "file"),
        ("!!!", "file"),
    ],
)
def test_make_rich_file_id_slugs(stem: str, expected: str) -> None:
    assert make_rich_file_id(stem) == expected


def test_make_rich_file_id_avoids_collisions() -> None:
    assert make_rich_file_id("photo", {"photo"}) == "photo-2"
    assert make_rich_file_id("photo", {"photo", "photo-2"}) == "photo-3"


# ---------------------------------------------------------------------------
# scan_media — resolution
# ---------------------------------------------------------------------------


def test_relative_path_is_resolved_and_rewritten(tmp_path: Path) -> None:
    _touch(tmp_path / "img" / "shot.png")
    scan = scan_media("# T\n\n![](img/shot.png)\n", base_dir=tmp_path)

    assert scan.markdown == "# T\n\n![](tg://photo?id=shot)\n"
    assert scan.files == (
        RichFile(
            id="shot",
            path=str((tmp_path / "img" / "shot.png").resolve()),
            caption="",
            kind="photo",
        ),
    )
    assert scan.remote == ()


def test_absolute_path_is_resolved(tmp_path: Path) -> None:
    target = _touch(tmp_path / "elsewhere" / "clip.mp4", b"\x00")
    scan = scan_media(f"![]({target})\n", base_dir=tmp_path / "article")

    assert scan.markdown == "![](tg://video?id=clip)\n"
    assert scan.files[0].kind == "video"
    assert scan.files[0].path == str(target.resolve())


def test_percent_encoded_target_is_resolved(tmp_path: Path) -> None:
    _touch(tmp_path / "my shot.png")
    scan = scan_media("![](my%20shot.png)\n", base_dir=tmp_path)

    assert scan.files[0].path == str((tmp_path / "my shot.png").resolve())
    assert scan.markdown == "![](tg://photo?id=my-shot)\n"


def test_obsidian_embed_is_resolved_from_the_vault(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    _touch(vault / "attachments" / "Pasted image 1.png")
    article_dir = vault / "notes"
    article_dir.mkdir(parents=True, exist_ok=True)

    scan = scan_media(
        "![[Pasted image 1.png|Каспий|300]]\n", base_dir=article_dir, vault_dir=vault
    )

    assert scan.files[0].path == str((vault / "attachments" / "Pasted image 1.png").resolve())
    # The |300 size segment is Obsidian-only and never reaches the caption.
    assert scan.files[0].caption == "Каспий"
    assert scan.markdown == '![Каспий](tg://photo?id=Pasted-image-1 "Каспий")\n'


def test_obsidian_embed_prefers_the_nearest_match(tmp_path: Path) -> None:
    """Two files with the same name: the one beside the article wins."""
    vault = tmp_path / "vault"
    article_dir = vault / "notes"
    near = _touch(article_dir / "shot.png")
    _touch(vault / "far" / "deeper" / "shot.png")

    scan = scan_media("![[shot.png]]\n", base_dir=article_dir, vault_dir=vault)

    assert scan.files[0].path == str(near.resolve())


def test_ambiguous_obsidian_embed_is_an_error_not_a_guess(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    article_dir = vault / "notes"
    article_dir.mkdir(parents=True)
    _touch(vault / "a" / "shot.png")
    _touch(vault / "b" / "shot.png")

    with pytest.raises(AmbiguousMediaError, match="matches 2 files"):
        scan_media("![[shot.png]]\n", base_dir=article_dir, vault_dir=vault)


def test_missing_local_media_is_an_error_naming_the_file(tmp_path: Path) -> None:
    with pytest.raises(MediaResolutionError, match="img/gone.png"):
        scan_media("![](img/gone.png)\n", base_dir=tmp_path)


def test_missing_absolute_media_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(MediaResolutionError, match="media file not found"):
        scan_media(f"![]({tmp_path / 'nope.png'})\n", base_dir=tmp_path)


def test_unsupported_local_media_type_is_an_error(tmp_path: Path) -> None:
    _touch(tmp_path / "report.pdf", b"%PDF")
    with pytest.raises(MediaResolutionError, match="unsupported media type"):
        scan_media("![](report.pdf)\n", base_dir=tmp_path)


def test_remote_media_is_left_untouched(tmp_path: Path) -> None:
    """A fully remote article keeps its exact bytes — CRLF included."""
    markdown = "# T\r\n\r\n![alt](https://example.com/a.jpg \"Cap\")\r\n"
    scan = scan_media(markdown, base_dir=tmp_path)

    assert scan.markdown is markdown
    assert scan.files == ()
    assert scan.remote == ("https://example.com/a.jpg",)


def test_media_inside_fenced_code_is_not_resolved(tmp_path: Path) -> None:
    markdown = "```\n![](img/gone.png)\n```\n"
    scan = scan_media(markdown, base_dir=tmp_path)

    assert scan.markdown is markdown
    assert scan.files == ()


def test_media_inside_a_quote_keeps_its_prefix(tmp_path: Path) -> None:
    _touch(tmp_path / "shot.png")
    scan = scan_media("> ![](shot.png)\n", base_dir=tmp_path)

    assert scan.markdown == "> ![](tg://photo?id=shot)\n"
    assert scan.files[0].kind == "photo"


def test_caption_comes_from_the_title_and_falls_back_to_alt(tmp_path: Path) -> None:
    _touch(tmp_path / "a.png")
    _touch(tmp_path / "b.png")
    scan = scan_media(
        '![alt a](a.png "Title A")\n\n![alt b](b.png)\n', base_dir=tmp_path
    )

    assert scan.files[0].caption == "Title A"
    assert scan.files[1].caption == "alt b"
    assert scan.markdown == (
        '![alt a](tg://photo?id=a "Title A")\n\n![alt b](tg://photo?id=b "alt b")\n'
    )


def test_caption_holding_a_double_quote_is_written_single_quoted(
    tmp_path: Path,
) -> None:
    """The rewritten title must still parse: a bare ``"`` would end it early."""
    _touch(tmp_path / "a.png")
    scan = scan_media("![](a.png 'say \"hi\"')\n", base_dir=tmp_path)

    assert scan.markdown == "![](tg://photo?id=a 'say \"hi\"')\n"
    # And it round-trips through the scanner with the caption intact.
    from telegram_assistant.messages import parse_media_line

    reparsed = parse_media_line(scan.markdown.strip())
    assert reparsed is not None
    assert reparsed.caption == 'say "hi"'


def test_the_same_file_twice_is_uploaded_once(tmp_path: Path) -> None:
    _touch(tmp_path / "shot.png")
    scan = scan_media("![](shot.png)\n\n![](./shot.png)\n", base_dir=tmp_path)

    assert len(scan.files) == 1
    assert scan.markdown.count("tg://photo?id=shot") == 2


def test_distinct_files_with_the_same_stem_get_distinct_ids(tmp_path: Path) -> None:
    _touch(tmp_path / "one" / "shot.png")
    _touch(tmp_path / "two" / "shot.png")
    scan = scan_media("![](one/shot.png)\n\n![](two/shot.png)\n", base_dir=tmp_path)

    assert [f.id for f in scan.files] == ["shot", "shot-2"]
    assert "tg://photo?id=shot)" in scan.markdown
    assert "tg://photo?id=shot-2)" in scan.markdown


def test_override_resolves_a_file_outside_the_article_directory(tmp_path: Path) -> None:
    outside = _touch(tmp_path / "outside" / "real.png")
    article_dir = tmp_path / "article"
    article_dir.mkdir()

    scan = scan_media(
        "![](pasted.png)\n", base_dir=article_dir, overrides={"pasted.png": outside}
    )

    assert scan.files[0].path == str(outside.resolve())
    assert scan.markdown == "![](tg://photo?id=real)\n"


def test_override_matching_nothing_is_an_error(tmp_path: Path) -> None:
    outside = _touch(tmp_path / "real.png")
    with pytest.raises(MediaResolutionError, match="match no media"):
        scan_media("no media here\n", base_dir=tmp_path, overrides={"ghost.png": outside})


def test_override_pointing_at_a_missing_file_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(MediaResolutionError, match="missing file"):
        scan_media(
            "![](pasted.png)\n",
            base_dir=tmp_path,
            overrides={"pasted.png": tmp_path / "nope.png"},
        )


# ---------------------------------------------------------------------------
# send_message validation
# ---------------------------------------------------------------------------


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
        rich_files: tuple[RichFile, ...] = (),
    ) -> int:
        self.sent.append(
            {
                "chat_id": chat_id,
                "text": text,
                "rich_markdown": rich_markdown,
                "rich_files": tuple(rich_files),
            }
        )
        return 777

    async def list_topics(self, *, chat_id: int) -> list[TopicSummary]:
        return []


class LegacySendBackend:
    """Backend predating rich media — its signature has no ``rich_files``."""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def send_message(
        self,
        *,
        chat_id: int,
        text: str,
        topic_id: int | None = None,
        rich_markdown: str | None = None,
    ) -> int:
        self.sent.append({"chat_id": chat_id, "rich_markdown": rich_markdown})
        return 5


@pytest.fixture()
def store(tmp_path: Path) -> OperationStore:
    return OperationStore(tmp_path / "state.db")


async def test_rich_files_reach_the_backend_and_the_payload(
    tmp_path: Path, store: OperationStore
) -> None:
    path = _touch(tmp_path / "shot.png")
    backend = RecordingBackend()
    rich_file = RichFile(id="shot", path=str(path), caption="Cap", kind="photo")

    _result, op = await send_message(
        backend=backend,
        store=store,
        request=SendMessageRequest(
            telegram_chat_id=-100,
            text="",
            rich_markdown="![](tg://photo?id=shot)\n",
            rich_files=(rich_file,),
            operation_id="rich-media-ok",
        ),
    )

    assert backend.sent[0]["rich_files"] == (rich_file,)
    # Metadata only: the path and kind, never the bytes, and no caption
    # duplicated out of the markdown that is already recorded.
    assert op.request_payload["rich_files"] == [
        {"id": "shot", "path": str(path), "kind": "photo"}
    ]


async def test_a_media_less_send_omits_the_rich_files_kwarg(
    store: OperationStore,
) -> None:
    """The only-when-set contract: a legacy backend never sees the new kwarg."""
    backend = LegacySendBackend()

    await send_message(
        backend=backend,
        store=store,
        request=SendMessageRequest(
            telegram_chat_id=-100,
            text="",
            rich_markdown="# T\n",
            operation_id="rich-no-media",
        ),
    )

    assert backend.sent[0]["rich_markdown"] == "# T\n"


async def test_rich_files_without_rich_markdown_is_rejected(
    tmp_path: Path, store: OperationStore
) -> None:
    path = _touch(tmp_path / "shot.png")
    with pytest.raises(ValueError, match="rich_files requires rich_markdown"):
        await send_message(
            backend=RecordingBackend(),
            store=store,
            request=SendMessageRequest(
                telegram_chat_id=-100,
                text="hello",
                rich_files=(RichFile(id="s", path=str(path), caption="", kind="photo"),),
                operation_id="rich-files-no-md",
            ),
        )


async def test_more_than_fifty_rich_files_is_rejected(
    tmp_path: Path, store: OperationStore
) -> None:
    path = _touch(tmp_path / "shot.png")
    files = tuple(
        RichFile(id=f"f{i}", path=str(path), caption="", kind="photo")
        for i in range(MAX_RICH_MEDIA + 1)
    )

    with pytest.raises(ValueError, match=f"{MAX_RICH_MEDIA} media attachments"):
        await send_message(
            backend=RecordingBackend(),
            store=store,
            request=SendMessageRequest(
                telegram_chat_id=-100,
                text="",
                rich_markdown="# T\n",
                rich_files=files,
                operation_id="rich-files-too-many",
            ),
        )


async def test_exactly_fifty_rich_files_is_accepted(
    tmp_path: Path, store: OperationStore
) -> None:
    path = _touch(tmp_path / "shot.png")
    files = tuple(
        RichFile(id=f"f{i}", path=str(path), caption="", kind="photo")
        for i in range(MAX_RICH_MEDIA)
    )
    backend = RecordingBackend()

    await send_message(
        backend=backend,
        store=store,
        request=SendMessageRequest(
            telegram_chat_id=-100,
            text="",
            rich_markdown="# T\n",
            rich_files=files,
            operation_id="rich-files-boundary",
        ),
    )

    assert len(backend.sent[0]["rich_files"]) == MAX_RICH_MEDIA


async def test_missing_rich_file_is_rejected_before_the_operation_row(
    tmp_path: Path, store: OperationStore
) -> None:
    ghost = tmp_path / "gone.png"
    with pytest.raises(ValueError, match="is not a file"):
        await send_message(
            backend=RecordingBackend(),
            store=store,
            request=SendMessageRequest(
                telegram_chat_id=-100,
                text="",
                rich_markdown="# T\n",
                rich_files=(RichFile(id="g", path=str(ghost), caption="", kind="photo"),),
                operation_id="rich-files-missing",
            ),
        )
    # Nothing was recorded, so the fixed retry may reuse the same key.
    key = idempotency.message_send_key(
        telegram_chat_id=-100, telegram_topic_id=None, operation_id="rich-files-missing"
    )
    assert store.find_by_idempotency_key(key) is None


@pytest.mark.parametrize("bad_id", ["shot.png", "фото", "a b", ""])
async def test_a_file_id_the_server_would_reject_is_rejected_here(
    tmp_path: Path, store: OperationStore, bad_id: str
) -> None:
    path = _touch(tmp_path / "shot.png")
    with pytest.raises(ValueError, match="rich_files id"):
        await send_message(
            backend=RecordingBackend(),
            store=store,
            request=SendMessageRequest(
                telegram_chat_id=-100,
                text="",
                rich_markdown="# T\n",
                rich_files=(RichFile(id=bad_id, path=str(path), caption="", kind="photo"),),
                operation_id=f"rich-files-badid-{bad_id or 'empty'}",
            ),
        )


async def test_duplicate_rich_file_ids_are_rejected(
    tmp_path: Path, store: OperationStore
) -> None:
    path = _touch(tmp_path / "shot.png")
    entry = RichFile(id="dup", path=str(path), caption="", kind="photo")
    with pytest.raises(ValueError, match="duplicate rich_files id"):
        await send_message(
            backend=RecordingBackend(),
            store=store,
            request=SendMessageRequest(
                telegram_chat_id=-100,
                text="",
                rich_markdown="# T\n",
                rich_files=(entry, entry),
                operation_id="rich-files-dup",
            ),
        )


async def test_an_unknown_rich_file_kind_is_rejected(
    tmp_path: Path, store: OperationStore
) -> None:
    path = _touch(tmp_path / "shot.png")
    with pytest.raises(ValueError, match="rich_files kind"):
        await send_message(
            backend=RecordingBackend(),
            store=store,
            request=SendMessageRequest(
                telegram_chat_id=-100,
                text="",
                rich_markdown="# T\n",
                rich_files=(RichFile(id="s", path=str(path), caption="", kind="sticker"),),
                operation_id="rich-files-badkind",
            ),
        )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


CONFIG_YAML = "\n".join(
    [
        "telegram:",
        "  api_id: 123456",
        '  api_hash: "telegram_api_hash"',
        "  session_path: /data/telegram-assistant.session",
        "  default_chat_folder:",
        "    folder_id: 2",
        '    folder_name: "Planfix clients"',
        "  defaults:",
        "    enable_topics: true",
        "http:",
        '  host: "0.0.0.0"',
        "  port: 8085",
        '  bearer_token: "secret_token"',
        "logging:",
        "  level: INFO",
    ]
)


def _patch_cli(
    monkeypatch: pytest.MonkeyPatch, backend: Any, store: OperationStore
) -> None:
    class _FakeManager:
        async def disconnect(self) -> None:
            return None

    def _factory(config_path: Path | None) -> Any:
        from telegram_assistant.config import load_config

        async def _open() -> Any:
            return backend, backend, None

        return load_config(config_path), _FakeManager(), store, _open

    monkeypatch.setattr(cli_main, "_build_message_backends", _factory)


def _cli_setup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, markdown: str
) -> tuple[Path, Path, RecordingBackend]:
    config_file = tmp_path / "config.yml"
    config_file.write_text(CONFIG_YAML)
    article_dir = tmp_path / "article"
    article_dir.mkdir(exist_ok=True)
    md_file = article_dir / "note.md"
    md_file.write_text(markdown, encoding="utf-8")
    backend = RecordingBackend()
    _patch_cli(monkeypatch, backend, OperationStore(tmp_path / "cli.db"))
    return config_file, md_file, backend


def _run_cli(args: list[str]) -> Any:
    return CliRunner().invoke(cli_main.app, args)


def _cli_output(result: Any) -> str:
    return (result.stdout or "") + (result.stderr or "")


def test_cli_resolves_local_media_next_to_the_article(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_file, md_file, backend = _cli_setup(
        tmp_path, monkeypatch, "# T\n\n![Cap](shot.png)\n"
    )
    _touch(md_file.parent / "shot.png")

    result = _run_cli(
        [
            "messages",
            "send",
            "--chat-id",
            "-100",
            "--rich-markdown",
            str(md_file),
            "--operation-id",
            "cli-media",
            "--config",
            str(config_file),
        ]
    )

    assert result.exit_code == 0, _cli_output(result)
    sent = backend.sent[0]
    assert 'tg://photo?id=shot "Cap"' in sent["rich_markdown"]
    assert [f.path for f in sent["rich_files"]] == [
        str((md_file.parent / "shot.png").resolve())
    ]


def test_cli_rich_file_override_points_outside_the_article_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_file, md_file, backend = _cli_setup(
        tmp_path, monkeypatch, "![](pasted.png)\n"
    )
    outside = _touch(tmp_path / "outside" / "real.png")

    result = _run_cli(
        [
            "messages",
            "send",
            "--chat-id",
            "-100",
            "--rich-markdown",
            str(md_file),
            "--rich-file",
            f"pasted.png={outside}",
            "--operation-id",
            "cli-media-override",
            "--config",
            str(config_file),
        ]
    )

    assert result.exit_code == 0, _cli_output(result)
    assert [f.path for f in backend.sent[0]["rich_files"]] == [str(outside.resolve())]


def test_cli_vault_dir_resolves_an_obsidian_embed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_file, md_file, backend = _cli_setup(
        tmp_path, monkeypatch, "![[Pasted image 7.png]]\n"
    )
    vault = tmp_path / "vault"
    target = _touch(vault / "attachments" / "Pasted image 7.png")

    result = _run_cli(
        [
            "messages",
            "send",
            "--chat-id",
            "-100",
            "--rich-markdown",
            str(md_file),
            "--vault-dir",
            str(vault),
            "--operation-id",
            "cli-media-vault",
            "--config",
            str(config_file),
        ]
    )

    assert result.exit_code == 0, _cli_output(result)
    assert [f.path for f in backend.sent[0]["rich_files"]] == [str(target.resolve())]


def test_cli_missing_media_exits_two(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_file, md_file, backend = _cli_setup(
        tmp_path, monkeypatch, "![](missing.png)\n"
    )

    result = _run_cli(
        [
            "messages",
            "send",
            "--chat-id",
            "-100",
            "--rich-markdown",
            str(md_file),
            "--operation-id",
            "cli-media-missing",
            "--config",
            str(config_file),
        ]
    )

    assert result.exit_code == 2
    assert "media file not found" in _cli_output(result)
    assert backend.sent == []


@pytest.mark.parametrize("bad", ["pasted.png", "=/tmp/a.png", "pasted.png="])
def test_cli_rich_file_parse_error_exits_two(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bad: str
) -> None:
    config_file, md_file, _backend = _cli_setup(tmp_path, monkeypatch, "# T\n")

    result = _run_cli(
        [
            "messages",
            "send",
            "--chat-id",
            "-100",
            "--rich-markdown",
            str(md_file),
            "--rich-file",
            bad,
            "--config",
            str(config_file),
        ]
    )

    assert result.exit_code == 2
    assert "--rich-file must be <reference>=<path>" in _cli_output(result)


@pytest.mark.parametrize(
    "extra", [["--rich-file", "a.png=/tmp/a.png"], ["--vault-dir", "/tmp"]]
)
def test_cli_media_flags_without_rich_markdown_exit_two(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, extra: list[str]
) -> None:
    config_file, _md_file, _backend = _cli_setup(tmp_path, monkeypatch, "# T\n")

    result = _run_cli(
        [
            "messages",
            "send",
            "--chat-id",
            "-100",
            "--text",
            "hello",
            *extra,
            "--config",
            str(config_file),
        ]
    )

    assert result.exit_code == 2
    assert "only meaningful with --rich-markdown" in _cli_output(result)


def test_cli_dry_run_lists_resolved_files_without_uploading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_file, md_file, backend = _cli_setup(
        tmp_path, monkeypatch, "![Cap](shot.png)\n\n![](clip.mp4)\n"
    )
    _touch(md_file.parent / "shot.png")
    _touch(md_file.parent / "clip.mp4", b"\x00")

    result = _run_cli(
        [
            "messages",
            "send",
            "--chat-id",
            "-100",
            "--rich-markdown",
            str(md_file),
            "--dry-run",
            "--config",
            str(config_file),
        ]
    )

    assert result.exit_code == 0, _cli_output(result)
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    listed = payload["resolved"]["rich_files"]
    assert [(f["id"], f["kind"], f["caption"]) for f in listed] == [
        ("shot", "photo", "Cap"),
        ("clip", "video", ""),
    ]
    assert payload["resolved"]["rich_markdown_media"] == 2
    # A dry run never sends and never uploads.
    assert backend.sent == []


def test_cli_dry_run_reports_no_rich_files_for_a_plain_send(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_file, _md_file, _backend = _cli_setup(tmp_path, monkeypatch, "# T\n")

    result = _run_cli(
        [
            "messages",
            "send",
            "--chat-id",
            "-100",
            "--text",
            "hello",
            "--dry-run",
            "--config",
            str(config_file),
        ]
    )

    assert result.exit_code == 0, _cli_output(result)
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload["resolved"]["rich_files"] is None
