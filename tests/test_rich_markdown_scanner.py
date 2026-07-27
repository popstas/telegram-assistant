"""Tests for the rich-markdown block scanner (``messages/rich_markdown.py``)."""

from __future__ import annotations

import pytest

from telegram_assistant.messages import (
    MAX_BLOCK_NESTING,
    MAX_RICH_BLOCKS,
    MAX_RICH_MEDIA,
    SPACER_LINE,
    count_blocks,
    count_media,
    iter_media,
    parse_media_line,
    scan_blocks,
    split_lines,
    strip_yaml_frontmatter,
)


def kinds(markdown: str) -> list[str]:
    return [block.kind for block in scan_blocks(markdown)]


# --- line splitting ----------------------------------------------------------


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("", []),
        ("a\nb", ["a", "b"]),
        ("a\r\nb\r\n", ["a", "b"]),
        ("a\rb", ["a", "b"]),
        ("a\n", ["a"]),
        ("a\n\n", ["a", ""]),
    ],
)
def test_split_lines_normalizes_newlines(source: str, expected: list[str]) -> None:
    assert split_lines(source) == expected


def test_crlf_input_scans_like_lf() -> None:
    body = "# Title\n\npara one\n\npara two\n"
    assert kinds(body) == kinds(body.replace("\n", "\r\n"))


# --- block typing ------------------------------------------------------------


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("just a paragraph", ["paragraph"]),
        ("# Heading", ["heading"]),
        ("###### Six", ["heading"]),
        ("#NotAHeading", ["paragraph"]),
        ("####### Seven hashes", ["paragraph"]),
        ("---", ["divider"]),
        ("***", ["divider"]),
        ("___", ["divider"]),
        ("> quoted", ["quote"]),
        ("- one\n- two", ["list"]),
        ("1. one\n2. two", ["list"]),
        ("[^a]: a footnote", ["footnote"]),
        ("    indented code", ["code"]),
        ("Title\n=====", ["heading"]),
        ("Title\n-----", ["heading"]),
        ("a\n\nb", ["paragraph", "paragraph"]),
        ("para\n# Heading", ["paragraph", "heading"]),
    ],
)
def test_scanner_types_blocks(source: str, expected: list[str]) -> None:
    assert kinds(source) == expected


def test_indented_code_does_not_interrupt_a_paragraph() -> None:
    # An indented line right under prose is continuation text, not a code
    # block — reading it as code would make the scanner opaque to a media
    # reference sitting there and ship it as a literal local path.
    assert kinds("Some paragraph\n    ![[a.png]]") == ["paragraph", "media"]
    # A line the paragraph itself absorbs stays continuation text either way.
    assert scan_blocks("Some paragraph\n    plain text")[0].lines == (
        "Some paragraph",
        "    plain text",
    )


def test_indented_code_does_not_interrupt_a_media_line() -> None:
    # A media block *is* a paragraph — one whose only line is a media
    # reference — so the indented line under it is continuation text too.
    # Reading it as code would ship the second reference as a literal local
    # path, the one silent drop `scan_media` promises never to make.
    assert kinds("![[a.png]]\n    ![[b.png]]") == ["media", "media"]
    # The embed-then-caption shape survives the indentation the same way.
    assert scan_blocks("![[a.png]]\n    caption")[0].lines == (
        "![[a.png]]",
        "    caption",
    )


def test_indented_code_after_a_blank_line_or_a_heading_is_still_code() -> None:
    assert kinds("para\n\n    ![[a.png]]") == ["paragraph", "code"]
    assert kinds("# Head\n    ![[a.png]]") == ["heading", "code"]
    assert kinds(f"{SPACER_LINE}\n    ![[a.png]]") == ["paragraph", "code"]


def test_blank_lines_belong_to_no_block() -> None:
    blocks = scan_blocks("\n\n\nonly\n\n\n")
    assert [block.kind for block in blocks] == ["paragraph"]
    assert blocks[0].lines == ("only",)


def test_heading_level_is_reported() -> None:
    blocks = scan_blocks("# one\n\n### three\n\nSetext\n===\n\nOther\n---")
    assert [block.level for block in blocks] == [1, 3, 1, 2]


def test_trailing_whitespace_is_preserved_verbatim() -> None:
    blocks = scan_blocks("text with trailing   \n")
    assert blocks[0].lines == ("text with trailing   ",)
    assert blocks[0].text == "text with trailing   "


# --- fenced code -------------------------------------------------------------


def test_fenced_code_contents_are_never_inspected() -> None:
    source = "```python\n# not a heading\n| not | a table |\n![](https://x/y.jpg)\n- not a list\n```"
    blocks = scan_blocks(source)
    assert [block.kind for block in blocks] == ["code"]
    assert len(blocks[0].lines) == 6
    assert count_media(blocks) == 0


def test_tilde_fence_ignores_backtick_fence_inside() -> None:
    source = "~~~\n```\nstill code\n```\n~~~\n\nafter"
    assert kinds(source) == ["code", "paragraph"]


def test_unterminated_fence_runs_to_end_of_document() -> None:
    blocks = scan_blocks("```\nno closing fence\n\n# not a heading")
    assert [block.kind for block in blocks] == ["code"]
    assert blocks[0].end == 4


def test_fence_with_info_string_does_not_close_itself() -> None:
    blocks = scan_blocks("```js\nlet a = 1;\n```")
    assert [block.kind for block in blocks] == ["code"]


def test_indented_code_stops_at_unindented_text() -> None:
    blocks = scan_blocks("    code line\n    more code\n\ntext")
    assert [block.kind for block in blocks] == ["code", "paragraph"]
    assert blocks[0].lines == ("    code line", "    more code")


# --- tables ------------------------------------------------------------------


def test_table_is_one_block_with_rows_counted() -> None:
    source = "| a | b |\n| --- | --- |\n| 1 | 2 |\n| 3 | 4 |\n\nafter"
    blocks = scan_blocks(source)
    assert [block.kind for block in blocks] == ["table", "paragraph"]
    # header + two body rows + the table block itself
    assert blocks[0].weight == 4


def test_pipe_line_without_delimiter_row_is_a_paragraph() -> None:
    assert kinds("| a | b |\nplain text") == ["paragraph"]


def test_paragraph_breaks_before_a_table() -> None:
    assert kinds("intro\n| a | b |\n| --- | --- |\n| 1 | 2 |") == ["paragraph", "table"]


# --- lists -------------------------------------------------------------------


def test_nested_list_items_each_count() -> None:
    source = "- one\n    - nested a\n    - nested b\n- two"
    blocks = scan_blocks(source)
    assert [block.kind for block in blocks] == ["list"]
    assert blocks[0].weight == 5  # list + 4 items


def test_list_continues_across_a_blank_line_when_the_next_item_follows() -> None:
    blocks = scan_blocks("- one\n\n- two\n\nnot a list")
    assert [block.kind for block in blocks] == ["list", "paragraph"]
    assert blocks[0].weight == 3


def test_list_continuation_line_is_not_an_item() -> None:
    blocks = scan_blocks("- one\n  continued here\n- two")
    assert blocks[0].weight == 3


# --- quotes ------------------------------------------------------------------


def test_quote_nests_its_children() -> None:
    blocks = scan_blocks("> para one\n>\n> para two")
    assert [block.kind for block in blocks] == ["quote"]
    assert [child.kind for child in blocks[0].children] == ["paragraph", "paragraph"]
    assert blocks[0].weight == 3


def test_nested_quote_terminates() -> None:
    blocks = scan_blocks("> > deep")
    assert blocks[0].kind == "quote"
    assert blocks[0].children[0].kind == "quote"


def test_quote_stops_at_a_blank_line() -> None:
    assert kinds("> quoted\n\nplain") == ["quote", "paragraph"]


# --- html blocks -------------------------------------------------------------


def test_details_block_with_blank_lines_inside_is_one_block() -> None:
    source = (
        "<details>\n<summary>More</summary>\n\nhidden para\n\nsecond para\n\n</details>\n\nafter"
    )
    blocks = scan_blocks(source)
    assert [block.kind for block in blocks] == ["html", "paragraph"]
    assert blocks[0].html_tag == "details"
    assert [child.kind for child in blocks[0].children] == [
        "paragraph",
        "paragraph",
        "paragraph",
    ]


def test_tg_collage_children_are_media() -> None:
    source = (
        "<tg-collage>\n\n"
        "![](https://x/a.jpg)\n\n"
        "![](https://x/b.jpg)\n\n"
        "</tg-collage>"
    )
    blocks = scan_blocks(source)
    assert blocks[0].kind == "html"
    assert blocks[0].html_tag == "tg-collage"
    assert count_media(blocks) == 2
    assert blocks[0].weight == 3


def test_tg_slideshow_is_recognised() -> None:
    blocks = scan_blocks("<tg-slideshow>\n\n![](https://x/a.jpg)\n\n</tg-slideshow>")
    assert blocks[0].html_tag == "tg-slideshow"


def test_unclosed_html_block_runs_to_end_of_document() -> None:
    blocks = scan_blocks("<details>\n\nstill inside\n")
    assert [block.kind for block in blocks] == ["html"]


def test_unknown_html_tag_is_not_a_block() -> None:
    assert kinds("<div>\nplain\n</div>") == ["paragraph"]


# --- media recognition -------------------------------------------------------


@pytest.mark.parametrize(
    ("line", "target", "caption", "remote"),
    [
        ("![](https://x/a.jpg)", "https://x/a.jpg", "", True),
        ("![alt text](https://x/a.jpg)", "https://x/a.jpg", "alt text", True),
        ('![alt](https://x/a.jpg "Caption")', "https://x/a.jpg", "Caption", True),
        ("![alt](https://x/a.jpg 'Caption')", "https://x/a.jpg", "Caption", True),
        ("![](pics/local.png)", "pics/local.png", "", False),
        ("![](<pics/with space.png>)", "pics/with space.png", "", False),
        ("![[Pasted image.png]]", "Pasted image.png", "", False),
        ("![[a.png|My caption]]", "a.png", "My caption", False),
        ("![[a.png|My caption|150]]", "a.png", "My caption", False),
        ("![[a.png|My caption|center]]", "a.png", "My caption", False),
        ("![[a.png|My caption|right|150]]", "a.png", "My caption", False),
        ("![[a.png|150]]", "a.png", "", False),
        ("![[a.png|150x200]]", "a.png", "", False),
        ("![[dir/a.png|%]]", "dir/a.png", "a", False),
        ("![[dir/a.png|%.%]]", "dir/a.png", "a.png", False),
        ("![Cap|150](a.png)", "a.png", "Cap", False),
        ("![HTTPS://X/A.JPG](HTTPS://X/A.JPG)", "HTTPS://X/A.JPG", "HTTPS://X/A.JPG", True),
    ],
)
def test_parse_media_line(line: str, target: str, caption: str, remote: bool) -> None:
    media = parse_media_line(line)
    assert media is not None
    assert media.target == target
    assert media.caption == caption
    assert media.is_remote is remote


@pytest.mark.parametrize(
    "line",
    [
        "not media",
        "text ![](a.png) more text",
        "[link](a.png)",
        "![](  )",
        "![[]]",
        "![alt](a.png) trailing",
    ],
)
def test_parse_media_line_rejects_non_media(line: str) -> None:
    assert parse_media_line(line) is None


def test_obsidian_size_and_alignment_are_split_out() -> None:
    media = parse_media_line("![[a.png|Cap|right|150x200]]")
    assert media is not None
    assert media.caption == "Cap"
    assert media.size == "150x200"
    assert media.alignment == "right"
    assert media.obsidian is True


def test_markdown_title_wins_over_alt_as_caption() -> None:
    media = parse_media_line('![alt](a.png "title")')
    assert media is not None
    assert media.alt == "alt"
    assert media.caption == "title"


def test_media_run_without_blank_lines_is_several_media_blocks() -> None:
    blocks = scan_blocks("![[a.png]]\n![[b.png]]\n![[c.png]]")
    assert [block.kind for block in blocks] == ["media", "media", "media"]
    assert [block.media.target for block in blocks] == ["a.png", "b.png", "c.png"]


def test_media_followed_by_prose_stays_inline_in_the_paragraph() -> None:
    blocks = scan_blocks("![](a.png)\nsome prose right after")
    assert [block.kind for block in blocks] == ["paragraph"]


def test_paragraph_breaks_before_a_standalone_media_block() -> None:
    assert kinds("prose\n![](a.png)\n\nmore") == ["paragraph", "media", "paragraph"]


def test_iter_media_walks_nested_blocks_in_order() -> None:
    source = (
        "![](https://x/a.jpg)\n\n"
        "<tg-collage>\n\n![](https://x/b.jpg)\n\n![](https://x/c.jpg)\n\n</tg-collage>\n\n"
        "> ![](https://x/d.jpg)\n"
    )
    targets = [block.media.target for block in iter_media(scan_blocks(source))]
    assert targets == [
        "https://x/a.jpg",
        "https://x/b.jpg",
        "https://x/c.jpg",
        "https://x/d.jpg",
    ]


# --- counting ----------------------------------------------------------------


def test_count_blocks_sums_weights() -> None:
    source = "# H\n\npara\n\n- a\n- b\n\n| x | y |\n| --- | --- |\n| 1 | 2 |"
    blocks = scan_blocks(source)
    # heading 1 + paragraph 1 + list (1+2 items) + table (1+2 rows)
    assert count_blocks(blocks) == 8


def test_count_blocks_of_empty_document_is_zero() -> None:
    assert count_blocks(scan_blocks("")) == 0
    assert count_media(scan_blocks("")) == 0


def test_count_blocks_at_the_500_boundary() -> None:
    exactly = "\n\n".join(f"para {n}" for n in range(MAX_RICH_BLOCKS))
    assert count_blocks(scan_blocks(exactly)) == MAX_RICH_BLOCKS

    over = "\n\n".join(f"para {n}" for n in range(MAX_RICH_BLOCKS + 1))
    assert count_blocks(scan_blocks(over)) == MAX_RICH_BLOCKS + 1


def test_count_media_at_the_50_boundary() -> None:
    exactly = "\n\n".join(f"![](https://x/{n}.jpg)" for n in range(MAX_RICH_MEDIA))
    assert count_media(scan_blocks(exactly)) == MAX_RICH_MEDIA

    over = "\n\n".join(f"![](https://x/{n}.jpg)" for n in range(MAX_RICH_MEDIA + 1))
    assert count_media(scan_blocks(over)) == MAX_RICH_MEDIA + 1


def test_media_inside_code_does_not_count() -> None:
    source = "```\n![](https://x/a.jpg)\n```\n\n![](https://x/b.jpg)"
    assert count_media(scan_blocks(source)) == 1


# --- round-trip --------------------------------------------------------------


def test_blocks_preserve_every_source_line() -> None:
    source = (
        "# Title\n\n"
        "para one\n\n"
        "- a\n- b\n\n"
        "> quoted\n\n"
        "```\ncode\n```\n\n"
        "![](https://x/a.jpg)\n\n"
        "<details>\n\ninside\n\n</details>\n"
    )
    lines = split_lines(source)
    blocks = scan_blocks(source)
    for block in blocks:
        assert block.lines == tuple(lines[block.start : block.end])
    assert [block.start for block in blocks] == sorted(block.start for block in blocks)


# --- nesting bound -----------------------------------------------------------


def test_deeply_nested_quote_does_not_recurse_without_bound() -> None:
    """A 1.2 KB one-liner is far under the char limit but 600 levels deep.

    The scan runs on the event loop ahead of the WRITE gate, so a
    ``RecursionError`` here is reachable by a caller with no write grant.
    """
    blocks = scan_blocks("> " * 600 + "x")
    assert [block.kind for block in blocks] == ["quote"]


def test_deeply_nested_html_does_not_recurse_without_bound() -> None:
    # Each unclosed container consumes the rest of the document as its body.
    blocks = scan_blocks("<details>\n" * 1200)
    assert blocks[0].kind == "html"


def test_nesting_bound_emits_a_leaf_but_keeps_every_line() -> None:
    source = "> " * (MAX_BLOCK_NESTING + 5) + "x"
    block = scan_blocks(source)[0]
    assert block.lines == tuple(split_lines(source))
    deepest = block
    depth = 0
    while deepest.children:
        deepest = deepest.children[0]
        depth += 1
    assert depth == MAX_BLOCK_NESTING
    assert deepest.children == ()


def test_nesting_below_the_bound_still_scans_children() -> None:
    block = scan_blocks("> quoted\n> \n> ![](https://x/a.jpg)\n")[0]
    assert [child.kind for child in block.children] == ["paragraph", "media"]


# --- YAML frontmatter --------------------------------------------------------


def test_frontmatter_is_stripped_with_line_endings_intact() -> None:
    assert strip_yaml_frontmatter("---\ntags: [a]\n---\n\n# T\n") == "\n# T\n"
    assert strip_yaml_frontmatter("---\r\ntags: [a]\r\n---\r\n\r\n# T\r\n") == "\r\n# T\r\n"


def test_frontmatter_stripper_leaves_everything_else_by_identity() -> None:
    for source in (
        "# T\n\n---\n\nrule\n",  # a divider that is not at the top
        "---\ntags: [a]\n",  # unterminated: not frontmatter
        "---\n\nhr then text\n",  # a leading horizontal rule
        "",
    ):
        assert strip_yaml_frontmatter(source) is source


def test_leading_rule_plus_a_later_divider_is_not_frontmatter() -> None:
    """A closed pair of ``---`` fences is not enough: matching on the fences
    alone would drop the whole opening section of an article that starts with a
    rule and uses ``---`` dividers later, and nothing downstream reports it."""
    for source in (
        "---\n\n# T\n\npara\n\n---\n\nrest\n",  # rule, section, divider
        "---\n# Title\n---\nbody\n",  # rule, heading, divider
        "---\n\nintro\n\n---\n",
        "---\n---\nbody\n",  # two rules, not an empty frontmatter block
        "---\nhttps://example.com\n---\nbody\n",  # a colon, but not a YAML key
        "---\n![[shot.png]]\n---\nbody\n",  # an embed must never be dropped
    ):
        assert strip_yaml_frontmatter(source) is source


def test_frontmatter_with_nested_lists_and_blank_lines_is_still_stripped() -> None:
    assert (
        strip_yaml_frontmatter("---\ntags:\n  - a\n  - b\ntime: 10:30\n---\n\n# T\n")
        == "\n# T\n"
    )
    assert strip_yaml_frontmatter("---\ntags: [a]\n\nfoo: b\n---\nbody\n") == "body\n"


def test_frontmatter_comments_do_not_defeat_the_strip() -> None:
    """A comment is legal YAML and common in an Obsidian note. Rejecting the
    block over one would leave the fences in, and the note's own metadata would
    go out as a rule plus a large heading — the shape the strip exists to
    prevent, with nothing downstream to report it."""
    assert strip_yaml_frontmatter("---\ntitle: X\n# comment\n---\n\n# T\n") == "\n# T\n"
    assert (
        strip_yaml_frontmatter("---\ntags:\n  - a\n  # why\nfoo: b\n---\nbody\n")
        == "body\n"
    )


def test_a_leading_comment_still_reads_as_a_heading_not_frontmatter() -> None:
    """The "first line must be a mapping entry" rule is what keeps an article
    opening with a rule and a heading intact, so a comment is accepted only
    after it."""
    for source in (
        "---\n# my note\ntitle: X\n---\n\n# T\n",
        "---\n# Title\n---\nbody\n",
    ):
        assert strip_yaml_frontmatter(source) is source


def test_frontmatter_without_it_scans_as_a_divider_and_setext_heading() -> None:
    """Why the strip exists: unstripped, a note opens with a rule and a big
    heading reading its own metadata."""
    assert kinds("---\ntags: [a]\ndate: x\n---\n\n# T\n") == [
        "divider",
        "heading",
        "heading",
    ]
    assert kinds(strip_yaml_frontmatter("---\ntags: [a]\ndate: x\n---\n\n# T\n")) == [
        "heading"
    ]
