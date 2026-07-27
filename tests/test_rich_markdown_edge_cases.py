"""Task 10 — the acceptance edge cases of the rich-markdown normalization.

Each test here pins one case the plan calls out for verification: an article
with no paragraphs at all, an article that is only media, media written inside
a fenced code block, a U+00A0 the author put in running text, spacing turned
off while the article still carries media, and the exact 500-block / 50-media
boundaries. The individual passes are covered in the scanner/normalize/grouping
suites; this file is the combined "does the whole thing behave at the edges"
check.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from telegram_assistant.messages import (
    MAX_RICH_BLOCKS,
    MAX_RICH_MEDIA,
    RichFile,
    SendMessageRequest,
    normalize_rich_markdown,
    scan_media,
    send_message,
)
from telegram_assistant.messages.rich_markdown import iter_line_media_refs
from telegram_assistant.persistence import OperationStore

NBSP = "\u00a0"
PNG = b"\x89PNG\r\n\x1a\n"


# ---------------------------------------------------------------------------
# Articles with nothing to space
# ---------------------------------------------------------------------------


def test_article_with_no_paragraphs_at_all_is_returned_unchanged() -> None:
    """Nothing pairs, so the byte-fidelity identity return holds."""
    source = (
        "# Title\n\n"
        "- one\n- two\n\n"
        "| a | b |\n| --- | --- |\n| 1 | 2 |\n\n"
        "```\ncode\n```\n"
    )
    result = normalize_rich_markdown(source)

    assert result.markdown is source
    assert result.spaced is True
    assert result.warnings == ()


def test_article_of_only_headings_stays_tight() -> None:
    source = "# One\n\n## Two\n\n### Three\n"
    assert normalize_rich_markdown(source).markdown is source


def test_empty_and_blank_only_articles_are_untouched() -> None:
    for source in ("", "\n", "   \n\n  \n"):
        result = normalize_rich_markdown(source)
        assert result.markdown is source
        assert result.blocks == 0
        assert result.media == 0


# ---------------------------------------------------------------------------
# An article that is only media
# ---------------------------------------------------------------------------


def test_article_that_is_only_media_becomes_one_collage() -> None:
    source = "![](https://x/a.jpg)\n\n![](https://x/b.jpg)\n"
    result = normalize_rich_markdown(source)

    assert result.markdown == (
        "<tg-collage>\n\n![](https://x/a.jpg)\n\n![](https://x/b.jpg)\n\n</tg-collage>\n"
    )
    # The container costs a block of its own on top of its two media.
    assert result.blocks == 3
    assert result.media == 2
    assert [(group.index, group.size, group.mode) for group in result.groups] == [
        (0, 2, "collage")
    ]
    # No spacer anywhere: there is no paragraph and no heading to pair.
    assert NBSP not in result.markdown


def test_a_single_media_article_is_left_exactly_as_written() -> None:
    source = "![](https://x/a.jpg)\n"
    result = normalize_rich_markdown(source)

    assert result.markdown is source
    assert result.groups == ()
    assert result.media == 1


# ---------------------------------------------------------------------------
# Media inside fenced code is not media
# ---------------------------------------------------------------------------


def test_media_in_a_fence_is_neither_spaced_grouped_nor_resolved(tmp_path: Path) -> None:
    (tmp_path / "shot.png").write_bytes(PNG)
    source = (
        "How to embed an image:\n\n"
        "```markdown\n"
        "![](shot.png)\n"
        "![](shot.png)\n"
        "```\n\n"
        "That is all.\n"
    )

    result = normalize_rich_markdown(source)
    assert result.media == 0
    assert result.groups == ()
    assert "<tg-collage>" not in result.markdown
    assert "```markdown\n![](shot.png)\n![](shot.png)\n```" in result.markdown

    # The same blindness on the resolution side: no upload is planned and the
    # fence keeps the literal path the author typed.
    scan = scan_media(result.markdown, base_dir=tmp_path)
    assert scan.files == ()
    assert scan.markdown is result.markdown


# ---------------------------------------------------------------------------
# U+00A0 the author wrote
# ---------------------------------------------------------------------------


def test_nbsp_inside_running_text_is_not_a_spacer() -> None:
    source = f"one{NBSP}two\n\nthree{NBSP}four\n"
    result = normalize_rich_markdown(source)

    # Two real paragraphs, one inserted spacer between them — the NBSPs in the
    # text neither suppress the insertion nor count as blocks of their own.
    assert result.markdown == f"one{NBSP}two\n\n{NBSP}\n\nthree{NBSP}four\n"
    assert result.blocks == 3
    assert normalize_rich_markdown(result.markdown).markdown == result.markdown


def test_an_article_of_author_written_spacers_is_idempotent() -> None:
    source = f"one\n\n{NBSP}\n\ntwo\n\n{NBSP}\n\n# Head\n"
    result = normalize_rich_markdown(source)

    assert result.markdown is source
    assert result.blocks == 5


# ---------------------------------------------------------------------------
# spaced_paragraphs=False plus media
# ---------------------------------------------------------------------------


def test_spacing_off_still_groups_media() -> None:
    """The two passes are independent: turning spacing off keeps grouping on."""
    source = "Intro\n\n![](https://x/a.jpg)\n![](https://x/b.jpg)\n\nOutro\n"
    result = normalize_rich_markdown(source, spaced_paragraphs=False)

    assert result.spaced is False
    assert NBSP not in result.markdown
    assert "<tg-collage>" in result.markdown
    assert result.groups[0].preceding_text == "Intro"


def test_spacing_off_and_nothing_to_group_is_byte_for_byte() -> None:
    source = "Intro\r\n\r\n![](https://x/a.jpg)\r\n\r\nOutro\r\n"
    result = normalize_rich_markdown(source, spaced_paragraphs=False, grouping="none")

    assert result.markdown is source
    assert result.spaced is False
    assert result.warnings == ()


async def test_spacing_off_with_local_media_still_uploads_the_files(
    tmp_path: Path,
) -> None:
    """The end-to-end pairing: byte-for-byte markdown, files still passed down."""

    class RecordingBackend:
        def __init__(self) -> None:
            self.sent: list[dict[str, Any]] = []

        async def send_message(
            self,
            *,
            chat_id: int,
            text: str,
            topic_id: int | None = None,
            rich_markdown: str | None = None,
            rich_files: tuple[RichFile, ...] = (),
        ) -> int:
            self.sent.append({"rich_markdown": rich_markdown, "rich_files": tuple(rich_files)})
            return 42

    path = tmp_path / "shot.png"
    path.write_bytes(PNG)
    rich_file = RichFile(id="shot", path=str(path), caption=None, kind="photo")
    # CRLF and no trailing newline: whatever survives the domain is what the
    # backend must see when both cosmetic passes are off.
    markdown = "Intro\r\n\r\n![](tg://photo?id=shot)"
    backend = RecordingBackend()

    await send_message(
        backend=backend,
        store=OperationStore(tmp_path / "state.db"),
        request=SendMessageRequest(
            telegram_chat_id=-100,
            text="",
            rich_markdown=markdown,
            rich_files=(rich_file,),
            spaced_paragraphs=False,
            media_grouping="none",
            operation_id="edge-spacing-off-media",
        ),
    )

    assert backend.sent[0]["rich_markdown"] == markdown
    assert backend.sent[0]["rich_files"] == (rich_file,)


# ---------------------------------------------------------------------------
# Exactly 500 blocks / exactly 50 media
# ---------------------------------------------------------------------------


def test_spacing_is_kept_when_it_lands_exactly_on_the_block_limit() -> None:
    # 250 paragraphs + 249 spacers = 499, plus a fenced block that pairs with
    # nothing = exactly 500. The rollback is "over the limit", not "at it".
    source = "\n\n".join(f"para {n}" for n in range(250)) + "\n\n```\ncode\n```"
    result = normalize_rich_markdown(source)

    assert result.blocks == MAX_RICH_BLOCKS
    assert result.spaced is True
    assert result.warnings == ()


def test_spacing_rolls_back_one_block_over_the_limit() -> None:
    source = "\n\n".join(f"para {n}" for n in range(251))
    result = normalize_rich_markdown(source)

    assert result.markdown == source
    assert result.spaced is False
    assert result.blocks == 251
    assert result.warnings == (
        f"spaced_paragraphs disabled: {MAX_RICH_BLOCKS + 1} blocks "
        f"would exceed the {MAX_RICH_BLOCKS}-block limit",
    )


def test_an_unspaced_article_exactly_at_the_block_limit_does_not_warn() -> None:
    source = "\n\n".join(f"![](https://x/{n}.jpg)" for n in range(MAX_RICH_BLOCKS))
    result = normalize_rich_markdown(source, grouping="none")

    assert result.blocks == MAX_RICH_BLOCKS
    assert not any("500-block limit" in warning for warning in result.warnings)


def test_fifty_media_in_one_collage_does_not_warn() -> None:
    source = "\n".join(f"![](https://x/{n}.jpg)" for n in range(MAX_RICH_MEDIA))
    result = normalize_rich_markdown(source)

    assert result.media == MAX_RICH_MEDIA
    assert result.blocks == MAX_RICH_MEDIA + 1  # the collage container
    assert result.warnings == ()


# ---------------------------------------------------------------------------
# References the pattern must not miss (a miss ships the local path verbatim)
# ---------------------------------------------------------------------------


def test_balanced_parentheses_in_a_bare_target_are_resolved(tmp_path: Path) -> None:
    (tmp_path / "shot(1).png").write_bytes(PNG)
    source = "![](shot(1).png)\n"

    scan = scan_media(source, base_dir=tmp_path)

    assert [file.path for file in scan.files] == [str(tmp_path / "shot(1).png")]
    assert "shot(1).png" not in scan.markdown
    assert "tg://photo?id=" in scan.markdown


def test_escaped_parentheses_in_a_target_are_resolved(tmp_path: Path) -> None:
    (tmp_path / "shot(1).png").write_bytes(PNG)
    source = r"![](shot\(1\).png)" + "\n"

    scan = scan_media(source, base_dir=tmp_path)

    assert [file.path for file in scan.files] == [str(tmp_path / "shot(1).png")]
    assert "tg://photo?id=" in scan.markdown


def test_escaped_quotes_in_a_title_keep_the_media_resolvable(tmp_path: Path) -> None:
    (tmp_path / "shot.png").write_bytes(PNG)
    source = '![](shot.png "he said \\"hi\\"")\n'

    scan = scan_media(source, base_dir=tmp_path)

    assert [file.caption for file in scan.files] == ['he said "hi"']
    assert "shot.png" not in scan.markdown
    assert scan.markdown == "![](tg://photo?id=shot 'he said \"hi\"')\n"


def test_a_caption_with_both_quote_characters_keeps_them_both(tmp_path: Path) -> None:
    """Neither bare quoting form fits, so the rewrite escapes rather than swaps.

    Replacing ``"`` with ``'`` would send a caption the author never wrote.
    """
    (tmp_path / "shot.png").write_bytes(PNG)
    source = '![](shot.png "he said \\"it\'s ok\\"")\n'

    scan = scan_media(source, base_dir=tmp_path)

    assert [file.caption for file in scan.files] == ['he said "it\'s ok"']
    assert scan.markdown == '![](tg://photo?id=shot "he said \\"it\'s ok\\"")\n'
    # The rewritten article reads back as the caption that went in.
    (ref,) = iter_line_media_refs(scan.markdown.rstrip("\n"))
    assert ref.caption == 'he said "it\'s ok"'


def test_a_caption_ending_in_a_backslash_escapes_it_before_the_quote(
    tmp_path: Path,
) -> None:
    """Unescaped, the trailing backslash would escape the closing quote away."""
    (tmp_path / "shot.png").write_bytes(PNG)
    source = '![](shot.png "a \\"b\\" c\\\\")\n'

    scan = scan_media(source, base_dir=tmp_path)

    assert [file.caption for file in scan.files] == ['a "b" c\\']
    (ref,) = iter_line_media_refs(scan.markdown.rstrip("\n"))
    assert ref.caption == 'a "b" c\\'


def test_a_quoteless_caption_ending_in_a_backslash_round_trips(tmp_path: Path) -> None:
    (tmp_path / "shot.png").write_bytes(PNG)
    source = '![](shot.png "path C:\\\\")\n'

    scan = scan_media(source, base_dir=tmp_path)

    assert [file.caption for file in scan.files] == ["path C:\\"]
    (ref,) = iter_line_media_refs(scan.markdown.rstrip("\n"))
    assert ref.caption == "path C:\\"


def test_a_backslash_before_a_letter_stays_part_of_the_target() -> None:
    # Only ASCII punctuation is escapable in CommonMark, so a Windows-style
    # target keeps its separators instead of losing them to the unescape — the
    # name resolution then splits on is the one the author wrote.
    (ref,) = iter_line_media_refs(r"![](sub\shot.png)")

    assert ref.target == r"sub\shot.png"
