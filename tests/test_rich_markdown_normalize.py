"""Tests for ``normalize_rich_markdown`` (spacer insertion + budget warnings)."""

from __future__ import annotations

import pytest

from telegram_assistant.messages import (
    MAX_RICH_BLOCKS,
    MAX_RICH_MEDIA,
    SPACER_LINE,
    count_blocks,
    is_spacer_block,
    is_spacer_line,
    normalize_rich_markdown,
    scan_blocks,
)

NBSP = "\u00a0"


def spacers(markdown: str) -> int:
    return sum(1 for line in markdown.split("\n") if is_spacer_line(line))


# --- the spacer itself -------------------------------------------------------


def test_spacer_line_is_a_lone_non_breaking_space() -> None:
    assert SPACER_LINE == NBSP


@pytest.mark.parametrize(
    ("line", "expected"),
    [(NBSP, True), (f"  {NBSP} ", True), ("", False), ("   ", False), ("text", False)],
)
def test_is_spacer_line(line: str, expected: bool) -> None:
    assert is_spacer_line(line) is expected


def test_spacer_line_scans_as_its_own_paragraph_block() -> None:
    blocks = scan_blocks(f"one\n\n{NBSP}\n\ntwo")
    assert [block.kind for block in blocks] == ["paragraph", "paragraph", "paragraph"]
    assert is_spacer_block(blocks[1])
    assert not is_spacer_block(blocks[0])
    assert count_blocks(blocks) == 3


# --- where spacers go --------------------------------------------------------


def test_spacer_inserted_between_two_paragraphs() -> None:
    result = normalize_rich_markdown("one\n\ntwo")
    assert result.markdown == f"one\n\n{NBSP}\n\ntwo"
    assert result.spaced is True
    assert result.blocks == 3
    assert result.warnings == ()


def test_spacer_inserted_before_a_heading() -> None:
    result = normalize_rich_markdown("intro\n\n## Section\n\nbody")
    assert result.markdown == f"intro\n\n{NBSP}\n\n## Section\n\nbody"


def test_spacer_inserted_before_a_heading_with_no_blank_line() -> None:
    assert normalize_rich_markdown("intro\n# Title").markdown == f"intro\n\n{NBSP}\n\n# Title"


def test_never_inserted_after_a_heading() -> None:
    assert normalize_rich_markdown("# Title\n\nbody").markdown == "# Title\n\nbody"


def test_never_inserted_between_two_headings() -> None:
    assert normalize_rich_markdown("# One\n\n## Two").markdown == "# One\n\n## Two"


def test_never_inserted_before_the_first_block() -> None:
    assert normalize_rich_markdown("# Title").markdown == "# Title"
    assert normalize_rich_markdown("only a paragraph").markdown == "only a paragraph"


def test_spacer_before_every_heading_level() -> None:
    source = "\n\n".join(["intro", *(f"{'#' * level} H{level}" for level in range(1, 7))])
    result = normalize_rich_markdown(source)
    # one before each of the six headings, none between the headings themselves
    assert spacers(result.markdown) == 1
    assert result.markdown.startswith(f"intro\n\n{NBSP}\n\n# H1")


def test_setext_heading_is_not_split_by_a_spacer() -> None:
    result = normalize_rich_markdown("intro\n\nTitle\n=====\n\nbody")
    assert result.markdown == f"intro\n\n{NBSP}\n\nTitle\n=====\n\nbody"


def test_several_paragraphs_get_one_spacer_each() -> None:
    result = normalize_rich_markdown("a\n\nb\n\nc\n\nd")
    assert result.markdown == f"a\n\n{NBSP}\n\nb\n\n{NBSP}\n\nc\n\n{NBSP}\n\nd"
    assert result.blocks == 7


# --- where spacers must not go ----------------------------------------------


@pytest.mark.parametrize(
    "source",
    [
        "```\nline one\n\nline two\n```",
        "    code one\n\n    code two",
        "- one\n\n- two",
        "| a | b |\n| --- | --- |\n| 1 | 2 |\n| 3 | 4 |",
        "> para one\n>\n> para two",
        "<details>\n\nhidden one\n\nhidden two\n\n</details>",
        "<tg-collage>\n\n![](https://x/a.jpg)\n\n![](https://x/b.jpg)\n\n</tg-collage>",
    ],
)
def test_no_spacers_inside_non_paragraph_blocks(source: str) -> None:
    assert normalize_rich_markdown(source).markdown == source


def test_media_blocks_are_not_spaced_apart() -> None:
    # ``grouping="none"`` isolates the spacer pass — the collage default would
    # otherwise rewrite this run (covered in test_rich_markdown_grouping.py).
    source = "![](https://x/a.jpg)\n\n![](https://x/b.jpg)"
    assert normalize_rich_markdown(source, grouping="none").markdown == source


def test_paragraph_next_to_a_list_is_not_spaced() -> None:
    source = "intro\n\n- one\n- two\n\noutro"
    assert normalize_rich_markdown(source).markdown == source


def test_media_inside_a_fence_is_not_treated_as_media() -> None:
    result = normalize_rich_markdown("```\n![](https://x/a.jpg)\n```")
    assert result.media == 0


# --- idempotency -------------------------------------------------------------


def test_normalizing_twice_yields_the_same_text() -> None:
    source = "one\n\ntwo\n\n# Head\n\nthree\n\nfour"
    once = normalize_rich_markdown(source).markdown
    twice = normalize_rich_markdown(once).markdown
    assert once == twice
    assert spacers(once) == 3


def test_author_written_spacer_is_not_doubled() -> None:
    source = f"one\n\n{NBSP}\n\ntwo"
    result = normalize_rich_markdown(source)
    assert result.markdown == source
    assert spacers(result.markdown) == 1


def test_unchanged_document_keeps_its_exact_bytes() -> None:
    source = "# Title\r\n\r\nbody\r\n"
    assert normalize_rich_markdown(source).markdown == source


def test_trailing_newline_survives_insertion() -> None:
    assert normalize_rich_markdown("one\n\ntwo\n").markdown.endswith("two\n")


def test_crlf_input_is_normalized_to_lf_when_spacers_are_inserted() -> None:
    result = normalize_rich_markdown("one\r\n\r\ntwo")
    assert result.markdown == f"one\n\n{NBSP}\n\ntwo"


# --- disabled ----------------------------------------------------------------


def test_disabled_returns_the_input_unchanged() -> None:
    source = "one\r\n\r\ntwo\r\n"
    result = normalize_rich_markdown(source, spaced_paragraphs=False)
    assert result.markdown == source
    assert result.spaced is False
    assert result.blocks == 2
    assert result.warnings == ()


def test_empty_document() -> None:
    result = normalize_rich_markdown("")
    assert result.markdown == ""
    assert result.blocks == 0
    assert result.media == 0
    assert result.spaced is True


# --- counts and warnings -----------------------------------------------------


def test_media_is_counted_including_nested() -> None:
    source = (
        "![](https://x/a.jpg)\n\n"
        "<tg-collage>\n\n![](https://x/b.jpg)\n\n![](https://x/c.jpg)\n\n</tg-collage>"
    )
    assert normalize_rich_markdown(source).media == 3


def test_block_limit_fallback_keeps_the_unspaced_markdown() -> None:
    source = "\n\n".join(f"para {n}" for n in range(300))
    result = normalize_rich_markdown(source)
    assert result.markdown == source
    assert result.spaced is False
    assert result.blocks == 300
    assert result.warnings == (
        f"spaced_paragraphs disabled: 599 blocks would exceed the {MAX_RICH_BLOCKS}-block limit",
    )


def test_spacing_is_kept_right_at_the_block_limit() -> None:
    # 250 paragraphs + 249 spacers = 499 blocks, one under the limit.
    source = "\n\n".join(f"para {n}" for n in range(250))
    result = normalize_rich_markdown(source)
    assert result.spaced is True
    assert result.blocks == 499
    assert result.warnings == ()


def test_unspaced_article_over_the_block_limit_only_warns() -> None:
    source = "\n\n".join(f"![](https://x/{n}.jpg)" for n in range(MAX_RICH_BLOCKS + 1))
    result = normalize_rich_markdown(source, grouping="none")
    assert result.markdown == source
    assert result.blocks == MAX_RICH_BLOCKS + 1
    assert any("over Telegram's 500-block limit" in warning for warning in result.warnings)


def test_media_over_the_limit_only_warns() -> None:
    source = "\n\n".join(f"![](https://x/{n}.jpg)" for n in range(MAX_RICH_MEDIA + 1))
    result = normalize_rich_markdown(source)
    assert result.media == MAX_RICH_MEDIA + 1
    assert result.warnings == (
        f"article has {MAX_RICH_MEDIA + 1} media attachments, "
        f"over Telegram's {MAX_RICH_MEDIA} limit",
    )


def test_media_exactly_at_the_limit_does_not_warn() -> None:
    source = "\n\n".join(f"![](https://x/{n}.jpg)" for n in range(MAX_RICH_MEDIA))
    assert normalize_rich_markdown(source).warnings == ()
