"""Demoting links whose URL Telegram's markdown parser would mangle.

Telegram HTML-escapes ``&`` and ``'`` inside a link destination, so an
ordinary query-string link arrives pointing at ``?a=1&amp;k=2``. Only a bare
URL in the text survives (proven live — see ``UNSAFE_LINK_URL_CHARS``), so
``unwrap_unsafe_links`` rewrites ``[text](url)`` to ``text: url`` for exactly
those links and leaves every other link a markdown link.
"""

from __future__ import annotations

import pytest

from telegram_assistant.messages.rich_markdown import (
    UNSAFE_LINK_URL_CHARS,
    normalize_rich_markdown,
    unwrap_unsafe_links,
)

UNSAFE = "https://example.com/?action=view&handbook=235"
SAFE = "https://example.com/plain/page"


def test_link_with_ampersand_is_demoted_to_text_and_bare_url() -> None:
    text, count = unwrap_unsafe_links(f"[269 - AWRA]({UNSAFE})")
    assert text == f"269 - AWRA: {UNSAFE}"
    assert count == 1


def test_link_with_apostrophe_is_demoted() -> None:
    url = "https://example.com/?q=o'brien"
    text, count = unwrap_unsafe_links(f"[name]({url})")
    assert text == f"name: {url}"
    assert count == 1


def test_safe_link_is_returned_by_identity() -> None:
    source = f"See [the page]({SAFE}) for details.\n"
    text, count = unwrap_unsafe_links(source)
    assert text is source
    assert count == 0


def test_document_without_unsafe_characters_is_returned_by_identity() -> None:
    source = "Just [a link](https://example.com/a) and prose.\r\n"
    text, count = unwrap_unsafe_links(source)
    assert text is source
    assert count == 0


def test_only_the_unsafe_link_is_rewritten() -> None:
    source = f"[safe]({SAFE}) and [unsafe]({UNSAFE})"
    text, count = unwrap_unsafe_links(source)
    assert text == f"[safe]({SAFE}) and unsafe: {UNSAFE}"
    assert count == 1


def test_empty_anchor_text_collapses_to_the_url() -> None:
    text, count = unwrap_unsafe_links(f"[]({UNSAFE})")
    assert text == UNSAFE
    assert count == 1


def test_anchor_text_equal_to_the_url_is_not_repeated() -> None:
    text, count = unwrap_unsafe_links(f"[{UNSAFE}]({UNSAFE})")
    assert text == UNSAFE
    assert count == 1


def test_title_is_dropped() -> None:
    text, count = unwrap_unsafe_links(f'[name]({UNSAFE} "a tooltip")')
    assert text == f"name: {UNSAFE}"
    assert count == 1


def test_angle_bracket_destination_is_unwrapped_without_its_brackets() -> None:
    text, count = unwrap_unsafe_links(f"[name](<{UNSAFE}>)")
    assert text == f"name: {UNSAFE}"
    assert count == 1


def test_media_embed_is_never_demoted() -> None:
    source = "![alt](https://example.com/a.png?w=1&h=2)"
    text, count = unwrap_unsafe_links(source)
    assert text is source
    assert count == 0


def test_obsidian_media_embed_is_untouched() -> None:
    source = "![[shot.png|a & b]]"
    text, count = unwrap_unsafe_links(source)
    assert text is source
    assert count == 0


def test_inline_code_span_containing_a_link_is_untouched() -> None:
    source = f"Write `[name]({UNSAFE})` to link."
    text, count = unwrap_unsafe_links(source)
    assert text is source
    assert count == 0


def test_code_span_inside_the_anchor_text_does_not_shield_the_link() -> None:
    # Containment, not overlap: the span only overlaps the link, so skipping
    # would ship the broken URL — the defect this pass exists to remove.
    text, count = unwrap_unsafe_links(f"[run `make` first]({UNSAFE})")
    assert text == f"run `make` first: {UNSAFE}"
    assert count == 1


def test_fenced_code_block_is_untouched() -> None:
    source = f"```\n[name]({UNSAFE})\n```\n"
    text, count = unwrap_unsafe_links(source)
    assert text is source
    assert count == 0


def test_links_inside_lists_quotes_and_tables_are_rewritten_in_place() -> None:
    source = (
        f"- item [a]({UNSAFE}) tail\n"
        f"> quoted [b]({UNSAFE}) tail\n"
        f"| cell [c]({UNSAFE}) | second |\n"
    )
    text, count = unwrap_unsafe_links(source)
    assert count == 3
    assert text == (
        f"- item a: {UNSAFE} tail\n"
        f"> quoted b: {UNSAFE} tail\n"
        f"| cell c: {UNSAFE} | second |\n"
    )


def test_two_links_on_one_line_are_both_rewritten() -> None:
    text, count = unwrap_unsafe_links(f"[a]({UNSAFE}), [b]({UNSAFE})")
    assert text == f"a: {UNSAFE}, b: {UNSAFE}"
    assert count == 2


def test_trailing_newline_and_crlf_survive_a_rewrite() -> None:
    text, count = unwrap_unsafe_links(f"[a]({UNSAFE})\r\nsecond line\r\n")
    assert count == 1
    assert text == f"a: {UNSAFE}\nsecond line\n"


def test_rewrite_never_grows_the_text() -> None:
    source = f'[a very long anchor]({UNSAFE} "and a title")\n'
    text, _ = unwrap_unsafe_links(source)
    assert len(text) <= len(source)


def test_pass_is_idempotent() -> None:
    once, first = unwrap_unsafe_links(f"[a]({UNSAFE})")
    twice, second = unwrap_unsafe_links(once)
    assert twice is once
    assert first == 1
    assert second == 0


@pytest.mark.parametrize("char", UNSAFE_LINK_URL_CHARS)
def test_every_unsafe_character_triggers_the_rewrite(char: str) -> None:
    url = f"https://example.com/?q=a{char}b"
    _, count = unwrap_unsafe_links(f"[name]({url})")
    assert count == 1


@pytest.mark.parametrize("char", ["+", "#", "~", "|", "_", "*", "%20", "тест"])
def test_characters_telegram_preserves_do_not_trigger_the_rewrite(char: str) -> None:
    source = f"[name](https://example.com/?q=a{char}b)"
    text, count = unwrap_unsafe_links(source)
    assert text is source
    assert count == 0


def test_normalize_reports_the_count_and_rewrites_the_article() -> None:
    result = normalize_rich_markdown(f"# Title\n\n[269 - AWRA]({UNSAFE})\n")
    assert result.unwrapped_links == 1
    assert f"269 - AWRA: {UNSAFE}" in result.markdown
    assert "](" not in result.markdown


def test_normalize_reports_zero_for_an_article_without_unsafe_links() -> None:
    result = normalize_rich_markdown(f"Just [a link]({SAFE}).\n")
    assert result.unwrapped_links == 0
    assert f"[a link]({SAFE})" in result.markdown


def test_wikilink_expansion_runs_before_this_pass() -> None:
    # A wikilink alias can carry the anchor text of a link, so the wikilink
    # pass must have finished before the link pattern sees the line.
    result = normalize_rich_markdown(f"[[Note|Alias]] and [x]({UNSAFE})\n")
    assert result.wikilinks == 1
    assert result.unwrapped_links == 1
    assert f"Alias and x: {UNSAFE}" in result.markdown
