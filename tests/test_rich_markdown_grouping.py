"""Tests for the media-grouping half of ``normalize_rich_markdown``.

A run of two or more consecutive media blocks is wrapped in the dialect's
``<tg-collage>``/``<tg-slideshow>`` container so Telegram renders it as one
group instead of a column of separate media.
"""

from __future__ import annotations

import pytest

from telegram_assistant.messages import (
    DEFAULT_MEDIA_GROUP_MODE,
    MAX_RICH_BLOCKS,
    MEDIA_GROUP_MODES,
    MediaGroupChoice,
    MediaGroupError,
    count_blocks,
    normalize_rich_markdown,
    scan_blocks,
)

NBSP = " "

TWO = "![](https://x/a.jpg)\n\n![](https://x/b.jpg)"


def test_defaults_are_the_documented_ones() -> None:
    assert DEFAULT_MEDIA_GROUP_MODE == "collage"
    assert MEDIA_GROUP_MODES == ("collage", "slideshow", "none")


# --- what gets wrapped -------------------------------------------------------


def test_two_consecutive_media_become_a_collage() -> None:
    result = normalize_rich_markdown(TWO)
    assert result.markdown == (
        "<tg-collage>\n\n![](https://x/a.jpg)\n\n![](https://x/b.jpg)\n\n</tg-collage>"
    )
    assert result.grouped is True
    assert [(g.index, g.size, g.mode) for g in result.groups] == [(0, 2, "collage")]


def test_obsidian_style_run_without_blank_lines_is_grouped() -> None:
    source = "intro\n\n![[one.png]]\n![[two.png]]\n![[three.png]]"
    result = normalize_rich_markdown(source)
    assert result.markdown.endswith(
        "<tg-collage>\n\n![[one.png]]\n\n![[two.png]]\n\n![[three.png]]\n\n</tg-collage>"
    )
    assert result.groups[0].size == 3


def test_two_separate_runs_are_each_wrapped() -> None:
    source = f"intro\n\n{TWO}\n\nmiddle\n\n{TWO}\n\noutro"
    result = normalize_rich_markdown(source)
    assert result.markdown.count("<tg-collage>") == 2
    assert result.markdown.count("</tg-collage>") == 2
    assert [g.index for g in result.groups] == [0, 1]
    assert all(g.size == 2 for g in result.groups)


def test_single_media_is_left_alone() -> None:
    source = "intro\n\n![](https://x/a.jpg)\n\noutro"
    result = normalize_rich_markdown(source, spaced_paragraphs=False)
    assert result.markdown == source
    assert result.groups == ()
    assert result.grouped is False


def test_media_separated_by_text_is_not_one_run() -> None:
    source = "![](https://x/a.jpg)\n\nbetween\n\n![](https://x/b.jpg)"
    result = normalize_rich_markdown(source, spaced_paragraphs=False)
    assert result.markdown == source
    assert result.groups == ()


def test_author_written_collage_is_never_regrouped() -> None:
    source = (
        "<tg-collage>\n\n![](https://x/a.jpg)\n\n![](https://x/b.jpg)\n\n</tg-collage>"
    )
    result = normalize_rich_markdown(source)
    assert result.markdown == source
    assert result.groups == ()
    assert result.media == 2


@pytest.mark.parametrize("tag", ["tg-slideshow", "details"])
def test_media_inside_an_author_written_container_is_left_alone(tag: str) -> None:
    source = f"<{tag}>\n\n![](https://x/a.jpg)\n\n![](https://x/b.jpg)\n\n</{tag}>"
    assert normalize_rich_markdown(source).markdown == source


def test_media_inside_a_fence_is_not_grouped() -> None:
    source = "```\n![](https://x/a.jpg)\n\n![](https://x/b.jpg)\n```"
    result = normalize_rich_markdown(source)
    assert result.markdown == source
    assert result.groups == ()
    assert result.media == 0


def test_media_inside_a_quote_is_not_grouped() -> None:
    source = "> ![](https://x/a.jpg)\n>\n> ![](https://x/b.jpg)"
    result = normalize_rich_markdown(source)
    assert result.markdown == source
    assert result.groups == ()


# --- surrounding text --------------------------------------------------------


def test_surrounding_text_survives_the_wrap() -> None:
    source = f"before\n\n{TWO}\n\nafter"
    result = normalize_rich_markdown(source, spaced_paragraphs=False)
    assert result.markdown == (
        "before\n\n<tg-collage>\n\n![](https://x/a.jpg)\n\n"
        "![](https://x/b.jpg)\n\n</tg-collage>\n\nafter"
    )


def test_blank_lines_are_added_around_a_tight_run() -> None:
    # No blank line between the text and the run: the container tags still need
    # one on each side to be parsed as their own block.
    source = "before\n![](https://x/a.jpg)\n![](https://x/b.jpg)\n\nafter"
    result = normalize_rich_markdown(source, spaced_paragraphs=False)
    assert "before\n\n<tg-collage>" in result.markdown
    assert "</tg-collage>\n\nafter" in result.markdown


def test_trailing_newline_survives_grouping() -> None:
    assert normalize_rich_markdown(f"{TWO}\n").markdown.endswith("</tg-collage>\n")


def test_grouping_and_spacing_compose() -> None:
    source = f"intro\n\n{TWO}\n\n# Heading"
    result = normalize_rich_markdown(source)
    assert "<tg-collage>" in result.markdown
    # The heading still gets its spacer; the collage is not spaced apart.
    assert f"\n\n{NBSP}\n\n# Heading" in result.markdown
    assert result.spacers_added is True
    assert result.grouped is True


# --- preceding text ----------------------------------------------------------


def test_preceding_text_is_reported() -> None:
    result = normalize_rich_markdown(f"## Пляж\n\n{TWO}")
    assert result.groups[0].preceding_text == "## Пляж"


def test_preceding_text_is_the_tail_and_marked_when_truncated() -> None:
    long = "x" * 200
    result = normalize_rich_markdown(f"{long}\n\n{TWO}")
    text = result.groups[0].preceding_text
    assert text.startswith("…")
    assert len(text) == 51


def test_preceding_text_is_empty_for_a_leading_run() -> None:
    assert normalize_rich_markdown(TWO).groups[0].preceding_text == ""


def test_preceding_text_skips_spacers_and_earlier_media() -> None:
    source = f"context\n\n{NBSP}\n\n{TWO}"
    assert normalize_rich_markdown(source).groups[0].preceding_text == "context"


# --- overrides ---------------------------------------------------------------


def test_override_to_slideshow() -> None:
    result = normalize_rich_markdown(TWO, media_groups=[MediaGroupChoice(0, "slideshow")])
    assert result.markdown.startswith("<tg-slideshow>")
    assert result.markdown.endswith("</tg-slideshow>")
    assert result.groups[0].mode == "slideshow"


def test_override_to_none_leaves_the_run_ungrouped() -> None:
    result = normalize_rich_markdown(TWO, media_groups={0: "none"})
    assert result.markdown == TWO
    assert result.grouped is False
    assert result.groups[0].mode == "none"


def test_override_one_run_of_two() -> None:
    source = f"{TWO}\n\ntext\n\n{TWO}"
    result = normalize_rich_markdown(
        source, spaced_paragraphs=False, media_groups=[{"index": 1, "mode": "slideshow"}]
    )
    assert result.markdown.count("<tg-collage>") == 1
    assert result.markdown.count("<tg-slideshow>") == 1
    assert [g.mode for g in result.groups] == ["collage", "slideshow"]


def test_override_accepts_plain_pairs() -> None:
    result = normalize_rich_markdown(TWO, media_groups=[(0, "slideshow")])
    assert result.groups[0].mode == "slideshow"


def test_grouping_none_still_reports_the_run() -> None:
    result = normalize_rich_markdown(TWO, grouping="none")
    assert result.markdown == TWO
    assert [(g.index, g.size, g.mode) for g in result.groups] == [(0, 2, "none")]


def test_grouping_none_can_be_overridden_per_group() -> None:
    result = normalize_rich_markdown(TWO, grouping="none", media_groups={0: "collage"})
    assert result.markdown.startswith("<tg-collage>")


def test_unknown_group_index_is_an_error() -> None:
    with pytest.raises(MediaGroupError) as excinfo:
        normalize_rich_markdown(TWO, media_groups={7: "none"})
    assert "7" in str(excinfo.value)
    assert "1 media group(s)" in str(excinfo.value)


def test_override_on_an_article_with_no_groups_is_an_error() -> None:
    with pytest.raises(MediaGroupError) as excinfo:
        normalize_rich_markdown("just text", media_groups={0: "collage"})
    assert "no media groups" in str(excinfo.value)


def test_unknown_mode_is_an_error() -> None:
    with pytest.raises(MediaGroupError):
        normalize_rich_markdown(TWO, media_groups={0: "carousel"})


def test_unknown_default_grouping_is_an_error() -> None:
    with pytest.raises(MediaGroupError):
        normalize_rich_markdown(TWO, grouping="carousel")


def test_non_integer_index_is_an_error() -> None:
    with pytest.raises(MediaGroupError):
        normalize_rich_markdown(TWO, media_groups=[{"index": "first", "mode": "none"}])


# --- counting ----------------------------------------------------------------


def test_grouping_counts_the_container_block() -> None:
    result = normalize_rich_markdown(TWO)
    # the collage itself plus its two media blocks
    assert result.blocks == 3
    assert result.media == 2
    assert count_blocks(scan_blocks(result.markdown)) == result.blocks


def test_grouping_is_idempotent() -> None:
    once = normalize_rich_markdown(f"intro\n\n{TWO}\n\noutro").markdown
    twice = normalize_rich_markdown(once).markdown
    assert once == twice


def test_grouping_stays_inside_the_block_limit_accounting() -> None:
    # 400 media in one run: 1 collage + 400 media = 401 blocks, still under 500.
    source = "\n\n".join(f"![](https://x/{n}.jpg)" for n in range(400))
    result = normalize_rich_markdown(source)
    assert result.blocks == 401
    assert result.blocks <= MAX_RICH_BLOCKS
