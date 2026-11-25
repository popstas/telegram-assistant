"""Reading a rich message (Telegram *article*) back as markdown.

Fixtures are real ``telethon.tl.types`` objects (layer 227) with invented text,
shaped like what ``Message.rich_message`` carries for an article sent through
``InputRichMessageMarkdown``: an empty ``message``, no ``media``, and the body
as a ``PageBlock*`` tree.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from telethon.tl import functions, types

from telegram_assistant.messages.rich_render import (
    UNRENDERABLE,
    is_rich_message,
    render_rich_message,
)
from telegram_assistant.messages.service import RecentMessage
from telegram_assistant.messages.telethon_backend import (
    RICH_PARTIAL_MARKER,
    TelethonMessageReadBackend,
    TelethonSearchBackend,
)
from telegram_assistant.worker.queue import FloodWaitError


def plain(text: str) -> types.TextPlain:
    return types.TextPlain(text=text)


def para(*texts: Any) -> types.PageBlockParagraph:
    body = texts[0] if len(texts) == 1 else types.TextConcat(texts=list(texts))
    if isinstance(body, str):
        body = plain(body)
    return types.PageBlockParagraph(text=body)


def item(text: Any, **kw: Any) -> types.PageListItemText:
    return types.PageListItemText(
        text=plain(text) if isinstance(text, str) else text,
        checkbox=kw.get("checkbox", False),
        checked=kw.get("checked", False),
    )


def cell(text: str, *, header: bool = False, center: bool = False, right: bool = False):
    return types.PageTableCell(
        text=plain(text),
        header=header,
        align_center=center,
        align_right=right,
        valign_middle=True,
        valign_bottom=False,
    )


def caption(text: str, credit: str | None = None) -> types.PageCaption:
    return types.PageCaption(
        text=plain(text) if text else types.TextEmpty(),
        credit=plain(credit) if credit else types.TextEmpty(),
    )


def rich(*blocks: Any, part: bool = False) -> types.RichMessage:
    return types.RichMessage(blocks=list(blocks), photos=[], documents=[], part=part)


# ---------------------------------------------------------------------------
# Renderer
# ---------------------------------------------------------------------------


def test_renders_headings_paragraphs_and_lists() -> None:
    article = rich(
        types.PageBlockHeading1(text=plain("Release notes")),
        para("What changed this week."),
        types.PageBlockParagraph(text=plain(" ")),  # send-side spacer
        types.PageBlockHeading2(text=plain("Uploads")),
        types.PageBlockList(items=[item("PDF and DOCX"), item("Up to 3 files")]),
        types.PageBlockOrderedList(
            items=[
                types.PageListOrderedItemText(
                    text=plain("Attach"), checkbox=False, checked=False, num="1"
                ),
                types.PageListOrderedItemText(
                    text=plain("Send"), checkbox=False, checked=False, num="2"
                ),
            ]
        ),
        types.PageBlockList(
            items=[item("done", checkbox=True, checked=True), item("todo", checkbox=True)]
        ),
        types.PageBlockHeading3(text=plain("Small")),
    )

    assert render_rich_message(article) == (
        "# Release notes\n\n"
        "What changed this week.\n\n"
        "## Uploads\n\n"
        "- PDF and DOCX\n- Up to 3 files\n\n"
        "1. Attach\n2. Send\n\n"
        "- [x] done\n- [ ] todo\n\n"
        "### Small"
    )


def test_spacer_paragraphs_are_dropped() -> None:
    article = rich(para("a"), para(" "), para("   "), para("b"))
    assert render_rich_message(article) == "a\n\nb"


def test_inline_formatting_round_trips_to_the_dialect() -> None:
    article = rich(
        para(
            types.TextBold(text=plain("bold")),
            plain(" "),
            types.TextItalic(text=plain("italic")),
            plain(" "),
            types.TextStrike(text=plain("gone")),
            plain(" "),
            types.TextMarked(text=plain("hi")),
            plain(" "),
            types.TextSpoiler(text=plain("secret")),
            plain(" "),
            types.TextUnderline(text=plain("under")),
            plain(" "),
            types.TextFixed(text=plain("code")),
            plain(" "),
            types.TextMath(source="x^2"),
            plain(" "),
            types.TextSuperscript(text=plain("2")),
        )
    )
    assert render_rich_message(article) == (
        "**bold** *italic* ~~gone~~ ==hi== ||secret|| <u>under</u> `code` $x^2$ <sup>2</sup>"
    )


def test_links_keep_label_and_collapse_bare_urls() -> None:
    article = rich(
        para(
            types.TextUrl(text=plain("docs"), url="https://example.com/docs", webpage_id=0),
            plain(" "),
            types.TextUrl(
                text=plain("https://example.com"), url="https://example.com", webpage_id=0
            ),
            plain(" "),
            types.TextAutoUrl(text=plain("https://auto.example.com")),
            plain(" "),
            types.TextEmail(text=plain("a@example.com"), email="a@example.com"),
            plain(" "),
            types.TextMentionName(text=plain("Bob"), user_id=42),
        )
    )
    assert render_rich_message(article) == (
        "[docs](https://example.com/docs) https://example.com https://auto.example.com "
        "a@example.com [Bob](tg://user?id=42)"
    )


def test_nested_formatting() -> None:
    article = rich(
        para(types.TextBold(text=types.TextConcat(texts=[plain("a "), types.TextItalic(text=plain("b"))])))
    )
    assert render_rich_message(article) == "**a *b***"


def test_table_alignment_comes_from_the_body_row() -> None:
    table = types.PageBlockTable(
        title=types.TextEmpty(),
        rows=[
            types.PageTableRow(
                cells=[
                    cell("Name", header=True, center=True),
                    cell("Count", header=True, center=True),
                    cell("Note", header=True, center=True),
                ]
            ),
            types.PageTableRow(cells=[cell("a|b"), cell("3", right=True), cell("x", center=True)]),
        ],
    )
    assert render_rich_message(rich(table)) == (
        "| Name | Count | Note |\n"
        "| --- | ---: | :---: |\n"
        "| a\\|b | 3 | x |"
    )


def test_code_quote_divider_and_math_block() -> None:
    article = rich(
        types.PageBlockPreformatted(text=plain("print('hi')\n```"), language="python"),
        types.PageBlockBlockquote(text=plain("line one\nline two"), caption=types.TextEmpty()),
        types.PageBlockDivider(),
        types.PageBlockMath(source="E = mc^2"),
    )
    assert render_rich_message(article) == (
        "````python\nprint('hi')\n```\n````\n\n"
        "> line one\n> line two\n\n"
        "---\n\n"
        "$$\nE = mc^2\n$$"
    )


def test_media_become_placeholders_and_groups_keep_their_caption() -> None:
    article = rich(
        types.PageBlockPhoto(photo_id=1, caption=caption("Screenshot")),
        types.PageBlockVideo(video_id=2, caption=caption("")),
        types.PageBlockAudio(audio_id=3, caption=caption("Track", "Author")),
        types.PageBlockCollage(
            items=[
                types.PageBlockPhoto(photo_id=4, caption=caption("")),
                types.PageBlockPhoto(photo_id=5, caption=caption("")),
            ],
            caption=caption("Before, after"),
        ),
    )
    assert render_rich_message(article) == (
        "[photo: Screenshot]\n\n"
        "[video]\n\n"
        "[audio: Track — Author]\n\n"
        "<tg-collage>\n\n[photo]\n[photo]\n<figcaption>Before, after</figcaption>\n\n</tg-collage>"
    )


def test_details_and_nested_list_blocks() -> None:
    article = rich(
        types.PageBlockDetails(
            blocks=[para("hidden")], title=plain("More"), open=True
        ),
        types.PageBlockList(
            items=[
                types.PageListItemBlocks(
                    blocks=[
                        para("outer"),
                        types.PageBlockList(items=[item("inner")]),
                    ]
                )
            ]
        ),
    )
    assert render_rich_message(article) == (
        "<details open><summary>More</summary>\n\nhidden\n\n</details>\n\n"
        "- outer\n\n  - inner"
    )


def test_unknown_types_degrade_instead_of_raising() -> None:
    class PageBlockFromTheFuture:
        text = types.TextPlain(text="kept")

    class PageBlockOpaque:
        pass

    class TextFromTheFuture:
        text = types.TextPlain(text="inline")

    article = rich(
        PageBlockFromTheFuture(),
        PageBlockOpaque(),
        types.PageBlockUnsupported(),
        para(TextFromTheFuture()),
    )
    assert render_rich_message(article) == (
        "kept\n\n[opaque]\n\n[unsupported block]\n\ninline"
    )


def test_deep_nesting_is_cut_off_not_a_recursion_error() -> None:
    node: Any = plain("deep")
    for _ in range(2000):
        node = types.TextBold(text=node)
    block: Any = types.PageBlockParagraph(text=node)
    for _ in range(2000):
        block = types.PageBlockBlockquoteBlocks(blocks=[block], caption=caption(""))
    out = render_rich_message(rich(block))
    assert isinstance(out, str) and out


def test_render_never_raises() -> None:
    class Exploding:
        @property
        def blocks(self):  # noqa: D401
            raise RuntimeError("boom")

    assert render_rich_message(Exploding()) == UNRENDERABLE


def test_is_rich_message_checks_shape() -> None:
    assert is_rich_message(rich(para("x")))
    assert not is_rich_message(None)

    class AnswersEverything:
        def __getattr__(self, name: str) -> Any:
            return object()

    assert not is_rich_message(AnswersEverything())


# ---------------------------------------------------------------------------
# RecentMessage payload
# ---------------------------------------------------------------------------


def test_to_dict_adds_rich_keys_only_for_articles() -> None:
    plain_row = RecentMessage(id=1, sender=None, date=None, reply_to=None, text="hi")
    assert "rich" not in plain_row.to_dict()
    assert "rich_partial" not in plain_row.to_dict()

    article = RecentMessage(
        id=2, sender=None, date=None, reply_to=None, text="# T", rich=True
    )
    assert article.to_dict()["rich"] is True
    assert "rich_partial" not in article.to_dict()

    partial = RecentMessage(
        id=3, sender=None, date=None, reply_to=None, text="# T", rich=True, rich_partial=True
    )
    assert partial.to_dict()["rich_partial"] is True


# ---------------------------------------------------------------------------
# Backends: recent + search
# ---------------------------------------------------------------------------


_WHEN = datetime(2026, 11, 25, 12, 0, tzinfo=UTC)


def _article_message(msg_id: int, body: types.RichMessage, *, message: str = "") -> Any:
    return types.Message(
        id=msg_id,
        peer_id=types.PeerChannel(channel_id=777),
        date=_WHEN,
        message=message,
        rich_message=body,
    )


_PREVIEW = rich(types.PageBlockHeading1(text=plain("Long article")), part=True)
_FULL = rich(
    types.PageBlockHeading1(text=plain("Long article")),
    para("first"),
    para("second"),
)


class _ReadClient:
    """``iter_messages`` + raw-RPC double; ``full`` answers getRichMessage."""

    def __init__(self, msgs: list[Any], *, full: Any = None, fail: BaseException | None = None):
        self._msgs = msgs
        self._full = full
        self._fail = fail
        self.requests: list[Any] = []

    async def get_input_entity(self, ref: Any) -> Any:
        return f"peer:{ref}"

    def iter_messages(self, peer: Any, limit: int):
        msgs = self._msgs[:limit]

        async def _gen():
            for m in msgs:
                yield m

        return _gen()

    async def __call__(self, request: Any) -> Any:
        self.requests.append(request)
        if isinstance(request, functions.messages.GetRichMessageRequest):
            if self._fail is not None:
                raise self._fail
            return types.messages.Messages(
                messages=[_article_message(request.id, self._full)], chats=[], users=[], topics=[]
            )
        if isinstance(request, functions.messages.SearchRequest):
            return types.messages.Messages(messages=list(self._msgs), chats=[], users=[], topics=[])
        raise AssertionError(f"unexpected request {request!r}")


@pytest.mark.asyncio
async def test_recent_renders_an_article_and_flags_it() -> None:
    client = _ReadClient(
        [
            _article_message(57, rich(types.PageBlockHeading1(text=plain("Notes")), para("body"))),
            types.Message(
                id=56, peer_id=types.PeerChannel(channel_id=777), date=_WHEN, message="plain"
            ),
        ]
    )
    rows = await TelethonMessageReadBackend(client).get_recent_messages(chat_id=-100777, limit=5)

    assert rows[0].text == "# Notes\n\nbody"
    assert rows[0].rich is True and rows[0].rich_partial is False
    assert rows[1].text == "plain"
    assert rows[1].rich is False
    assert "rich" not in rows[1].to_dict()
    assert client.requests == []  # a complete article needs no extra RPC


@pytest.mark.asyncio
async def test_recent_fetches_the_full_body_of_a_truncated_article() -> None:
    client = _ReadClient([_article_message(35, _PREVIEW)], full=_FULL)
    rows = await TelethonMessageReadBackend(client).get_recent_messages(chat_id=-100777, limit=1)

    assert rows[0].text == "# Long article\n\nfirst\n\nsecond"
    assert rows[0].rich_partial is False
    (request,) = client.requests
    assert isinstance(request, functions.messages.GetRichMessageRequest)
    assert request.peer == "peer:-100777"
    assert request.id == 35


@pytest.mark.asyncio
async def test_recent_falls_back_to_the_preview_when_the_full_fetch_fails() -> None:
    client = _ReadClient([_article_message(35, _PREVIEW)], fail=RuntimeError("RPC down"))
    rows = await TelethonMessageReadBackend(client).get_recent_messages(chat_id=-100777, limit=1)

    assert rows[0].rich is True and rows[0].rich_partial is True
    assert rows[0].text == f"# Long article\n\n{RICH_PARTIAL_MARKER}"
    assert rows[0].to_dict()["rich_partial"] is True


@pytest.mark.asyncio
async def test_recent_full_fetch_flood_wait_propagates() -> None:
    """A throttle pauses the read; it never silently degrades to the preview."""
    telethon_flood = type("FloodWaitError", (Exception,), {"seconds": 7})()

    client = _ReadClient([_article_message(35, _PREVIEW)], fail=telethon_flood)
    with pytest.raises(FloodWaitError) as exc_info:
        await TelethonMessageReadBackend(client).get_recent_messages(chat_id=-100777, limit=1)
    assert exc_info.value.seconds == 7


@pytest.mark.asyncio
async def test_plain_text_wins_over_a_rich_body() -> None:
    client = _ReadClient([_article_message(1, rich(para("ignored")), message="caption")])
    rows = await TelethonMessageReadBackend(client).get_recent_messages(chat_id=1, limit=1)
    assert rows[0].text == "caption"
    assert rows[0].rich is False


@pytest.mark.asyncio
async def test_search_renders_articles_the_same_way() -> None:
    client = _ReadClient([_article_message(35, _PREVIEW)], full=_FULL)
    rows = await TelethonSearchBackend(client).search_messages(
        chat_id=-100777, query="Long", limit=5
    )

    assert [r.id for r in rows] == [35]
    assert rows[0].text == "# Long article\n\nfirst\n\nsecond"
    assert rows[0].rich is True
    assert any(isinstance(r, functions.messages.GetRichMessageRequest) for r in client.requests)
