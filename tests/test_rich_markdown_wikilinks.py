"""The Obsidian wikilink pass: [[Target|Alias]] becomes plain text.

Telegram has no wikilink syntax, so an unexpanded link reaches the reader as
literal brackets plus the vault's canonical note name instead of the word the
author wrote.
"""

import pytest

from telegram_assistant.messages.rich_markdown import MAX_WIKILINK_PASSES, strip_wikilinks


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("[[Андрей Смирнов]]", "Андрей Смирнов"),
        ("[[Станислав Попов|Стасу]]", "Стасу"),
        ("[[#Спорные моменты]]", "Спорные моменты"),
        ("[[tasks#Настроить statusline]]", "tasks > Настроить statusline"),
        ("[[Note#Heading|Алиас]]", "Алиас"),
        # Only the first pipe separates; the rest belong to the alias.
        ("[[A|B|C]]", "B|C"),
        # An empty half falls back to the other one.
        ("[[Note|]]", "Note"),
        ("[[|Стас]]", "Стас"),
        # A block reference falls out of the same rule with no special case.
        ("[[note#^blk]]", "note > ^blk"),
        # Surrounding prose is untouched, and several links share one line.
        (
            "Отдал [[Денис Баталин|Дэну]] и [[Ирина Шлыкова|Ирине]].",
            "Отдал Дэну и Ирине.",
        ),
    ],
)
def test_expands_wikilinks(source: str, expected: str) -> None:
    assert strip_wikilinks(source) == (expected, source.count("[["))


@pytest.mark.parametrize("source", ["[[]]", "[[|]]", "[[#]]"])
def test_degenerate_link_is_left_verbatim(source: str) -> None:
    """No target and no alias is not a link.

    Collapsing it to an empty string would silently delete characters the
    author typed — worse than leaving a curiosity in the text.
    """
    assert strip_wikilinks(source) == (source, 0)


def test_media_embed_is_left_to_scan_media() -> None:
    """``![[…]]`` is media; ``scan_media`` rewrites it into a ``tg://`` ref."""
    source = "![[Pasted image 1.png|Закат]]"
    assert strip_wikilinks(source) == (source, 0)


def test_embed_and_link_on_one_line() -> None:
    source = "![[shot.png]] обсудили с [[Ольга Цветцых]]"
    assert strip_wikilinks(source) == ("![[shot.png]] обсудили с Ольга Цветцых", 1)


def test_inline_code_span_is_opaque() -> None:
    """An article documenting this dialect writes `[[Note]]` and means the text."""
    source = "Пиши `[[Note]]`, получишь Note."
    assert strip_wikilinks(source) == (source, 0)


def test_code_span_overlapping_a_link_does_not_shield_it() -> None:
    """Containment, not overlap — the rule ``iter_line_media_refs`` uses.

    The code span here starts inside the link and ends outside it. Skipping on
    overlap would ship the raw brackets.
    """
    source = "[[Note|запусти `make]] сначала`"
    text, count = strip_wikilinks(source)
    assert count == 1
    assert "[[" not in text


def test_fenced_code_block_is_opaque() -> None:
    source = "```\n[[Note]]\n```\n"
    assert strip_wikilinks(source) == (source, 0)


def test_fenced_code_inside_a_quote_is_opaque() -> None:
    source = "> ```\n> [[Note]]\n> ```\n"
    assert strip_wikilinks(source) == (source, 0)


def test_link_inside_a_quote_is_expanded() -> None:
    source = "> Сказал [[Андрей Смирнов|Андрей]]\n"
    assert strip_wikilinks(source) == ("> Сказал Андрей\n", 1)


def test_table_cell_pipe_no_longer_breaks_the_row() -> None:
    """``[[A|B]]`` in a table currently splits the cell on its own pipe."""
    source = "| [[Станислав Попов|Стас]] | да |\n"
    assert strip_wikilinks(source) == ("| Стас | да |\n", 1)


def test_returns_input_by_identity_when_unchanged() -> None:
    """Identity is what keeps CRLF and the trailing newline byte-for-byte."""
    source = "# Заголовок\r\n\r\nПростой текст.\r\n"
    text, count = strip_wikilinks(source)
    assert text is source
    assert count == 0


def test_trailing_newline_survives_a_rewrite() -> None:
    source = "Спросил у [[Ольга Андрющенко|Оли]].\n"
    assert strip_wikilinks(source) == ("Спросил у Оли.\n", 1)


def test_is_idempotent() -> None:
    source = "Отдал [[Денис Баталин|Дэну]].\n"
    once, first = strip_wikilinks(source)
    twice, second = strip_wikilinks(once)
    assert first == 1
    assert second == 0
    assert twice is once


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        # A wikilink nested inside another link's target.
        ("[[ [[a]] ]]", "a"),
        # A wikilink nested inside another link's alias.
        ("[[a|[[b]]]]", "b"),
        # A wikilink whose target is itself bracketed twice over.
        ("[[[[a]]]]", "a"),
    ],
)
def test_nested_wikilinks_expand_to_a_fixpoint(source: str, expected: str) -> None:
    """One pass only resolves the innermost link, leaving ``[[``/``]]``
    behind — literal double brackets in a Telegram article are exactly the
    defect this pass exists to eliminate, so a single call must expand all
    the way through."""
    text, count = strip_wikilinks(source)
    assert text == expected
    assert "[[" not in text
    assert count == source.count("[[")


def test_nesting_past_the_cap_stops_and_ships_the_remainder_verbatim() -> None:
    """Structural proof of the ``MAX_WIKILINK_PASSES`` bound (no wall clock):
    an unrealistically deep chain of nested links resolves exactly one level
    per pass (verified by the three-input fixpoint tests above), so a depth
    well past the cap must stop after exactly ``MAX_WIKILINK_PASSES`` passes
    and leave ``[[``/``]]`` behind — the same trade-off ``_scan_nested`` makes
    past ``MAX_BLOCK_NESTING``. This is what keeps the call bounded: a real
    fixpoint loop over this input would need one pass per nesting level.
    """
    depth = MAX_WIKILINK_PASSES * 3
    source = "[[" * depth + "a" + "]]" * depth

    text, count = strip_wikilinks(source)

    # Each pass unwraps exactly one level (4 chars: the removed "[[" + "]]"),
    # so the number of passes actually run is recoverable from the length
    # delta — and it must be capped, not `depth`.
    levels_resolved = (len(source) - len(text)) // 4
    assert levels_resolved == MAX_WIKILINK_PASSES
    assert count == MAX_WIKILINK_PASSES
    assert "[[" in text
    assert text == "[[" * (depth - MAX_WIKILINK_PASSES) + "a" + "]]" * (
        depth - MAX_WIKILINK_PASSES
    )
