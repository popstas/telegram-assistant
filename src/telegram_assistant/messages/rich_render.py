"""Render a received rich message (a Telegram *article*) back to markdown.

The read-side twin of :mod:`rich_markdown`. A message sent through
``InputRichMessageMarkdown`` arrives with an **empty** ``message`` and no
``media``: its body lives in ``Message.rich_message`` — a
``RichMessage(blocks, photos, documents, rtl, part)`` whose ``blocks`` are the
server-parsed ``PageBlock*`` tree and whose inline text is a ``RichText`` tree
(``TextPlain``, ``TextBold``, ``TextConcat``, ``TextUrl``, …). Reading
``message`` alone therefore reports an article as ``""``.

:func:`render_rich_message` walks that tree and emits the Telegram markdown
dialect the send side accepts (headings, lists, task lists, tables with
alignment, quotes, fenced code, ``$math$``, ``~~strike~~``, ``==mark==``,
``||spoiler||``, ``<u>``, ``<details>``, ``<tg-collage>``/``<tg-slideshow>``
with a ``<figcaption>``), so an article can be read, edited and sent again.
It is deliberately **close to, not byte-identical with**, the source:

* the U+00A0-only spacer paragraphs the send-side normaliser inserts are
  dropped — a re-send would add them back, and as text they are noise;
* plain text is **not** escaped: an article that reads ``2*3`` stays readable,
  at the cost of re-parsing as emphasis in the rare case it would;
* media cannot be re-referenced (the photo/document ids belong to Telegram,
  not to a ``tg://photo?id=`` upload of ours), so each one is a placeholder in
  the ``[photo]`` style ``messages recent`` already uses for media messages —
  ``[photo: caption]``, ``[video]``, ``[audio]``.

Like the rest of the read path it never raises: an unknown block or text type
degrades to its inner ``text``/``blocks`` or a ``[name]`` placeholder, and
nesting past :data:`MAX_RENDER_DEPTH` is cut off, because one unparseable
article must not fail ``messages recent`` for a whole chat. No Telethon
imports: dispatch is on the class name, the way ``_media_summary`` does it, so
tests can build fixtures from real ``telethon.tl.types`` objects or stand-ins.
"""

from __future__ import annotations

from typing import Any

#: Recursion bound for nested blocks/text. The server caps an article at 16
#: levels; this is generous headroom that still keeps a malformed tree from
#: exhausting the stack.
MAX_RENDER_DEPTH = 64

#: Placeholder text when a whole article could not be rendered at all.
UNRENDERABLE = "[rich message]"

_NBSP = " "

_HEADINGS = {
    "PageBlockHeading1": "#",
    "PageBlockHeading2": "##",
    "PageBlockHeading3": "###",
    "PageBlockHeading4": "####",
    "PageBlockHeading5": "#####",
    "PageBlockHeading6": "######",
    # Instant-View style headers; the markdown dialect has no own syntax for
    # them, so they read as the nearest heading level.
    "PageBlockTitle": "#",
    "PageBlockHeader": "##",
    "PageBlockSubtitle": "##",
    "PageBlockSubheader": "###",
}

_MEDIA_BLOCKS = {
    "PageBlockPhoto": "photo",
    "PageBlockVideo": "video",
    "PageBlockAudio": "audio",
}

# Inline wrappers whose dialect form is a symmetric marker around the content.
_WRAPPERS = {
    "TextBold": ("**", "**"),
    "TextItalic": ("*", "*"),
    "TextStrike": ("~~", "~~"),
    "TextMarked": ("==", "=="),
    "TextSpoiler": ("||", "||"),
    "TextUnderline": ("<u>", "</u>"),
    "TextSubscript": ("<sub>", "</sub>"),
    "TextSuperscript": ("<sup>", "</sup>"),
}

# Auto-detected entities: the server found them in plain text, so the plain
# text is exactly what re-detects them on a re-send.
_TRANSPARENT_TEXT = {
    "TextAutoUrl",
    "TextAutoEmail",
    "TextAutoPhone",
    "TextHashtag",
    "TextCashtag",
    "TextBotCommand",
    "TextBankCard",
    "TextMention",
    "TextAnchor",
    "TextDate",
}


def is_rich_message(rich: Any) -> bool:
    """Return whether ``rich`` looks like a ``RichMessage`` with a block list.

    Checks the shape, not mere truthiness: a test double (or a future Telethon
    attribute) that answers every ``getattr`` must not turn a plain message
    into an empty article.
    """
    if rich is None:
        return False
    return isinstance(getattr(rich, "blocks", None), (list, tuple))


def render_rich_message(rich: Any) -> str:
    """Render a ``RichMessage`` to Telegram-dialect markdown (never raises)."""
    try:
        blocks = getattr(rich, "blocks", None) or ()
        return _render_blocks(blocks, 0).strip("\n")
    except Exception:  # pragma: no cover - defensive: the read path never fails
        return UNRENDERABLE


# ---------------------------------------------------------------------------
# Blocks
# ---------------------------------------------------------------------------


def _render_blocks(blocks: Any, depth: int) -> str:
    # A block's own leading/trailing newlines (a paragraph whose text ends in
    # "\n") would otherwise stack onto the blank-line separator.
    parts = [_render_block(block, depth).strip("\n") for block in (blocks or ())]
    return "\n\n".join(part for part in parts if part)


def _render_block(block: Any, depth: int) -> str:
    if block is None:
        return ""
    if depth > MAX_RENDER_DEPTH:
        return "[…]"
    name = type(block).__name__
    nxt = depth + 1

    if name == "PageBlockParagraph":
        text = _text(getattr(block, "text", None), nxt)
        # The send-side spacer: a paragraph that is only NBSP/whitespace.
        if not text.replace(_NBSP, "").strip():
            return ""
        return text
    if name in _HEADINGS:
        text = _single_line(_text(getattr(block, "text", None), nxt))
        return f"{_HEADINGS[name]} {text}" if text else ""
    if name in ("PageBlockKicker", "PageBlockFooter", "PageBlockThinking"):
        return _text(getattr(block, "text", None), nxt)
    if name == "PageBlockDivider":
        return "---"
    if name == "PageBlockAnchor":
        anchor = getattr(block, "name", "") or ""
        return f'<a name="{anchor}"></a>' if anchor else ""
    if name == "PageBlockPreformatted":
        return _render_code(block, nxt)
    if name == "PageBlockMath":
        source = getattr(block, "source", "") or ""
        return f"$$\n{source}\n$$" if source else ""
    if name == "PageBlockList":
        return _render_list(getattr(block, "items", None), ordered=False, depth=nxt)
    if name == "PageBlockOrderedList":
        return _render_list(getattr(block, "items", None), ordered=True, depth=nxt)
    if name == "PageBlockBlockquote":
        body = _text(getattr(block, "text", None), nxt)
        caption = _text(getattr(block, "caption", None), nxt)
        return _quote("\n\n".join(p for p in (body, caption) if p))
    if name == "PageBlockBlockquoteBlocks":
        body = _render_blocks(getattr(block, "blocks", None), nxt)
        caption = _caption(getattr(block, "caption", None), nxt)
        return _quote("\n\n".join(p for p in (body, caption) if p))
    if name == "PageBlockPullquote":
        body = _text(getattr(block, "text", None), nxt)
        cite = _text(getattr(block, "caption", None), nxt)
        inner = body + (f"<cite>{cite}</cite>" if cite else "")
        return f"<aside>{inner}</aside>" if inner else ""
    if name == "PageBlockTable":
        return _render_table(block, nxt)
    if name in _MEDIA_BLOCKS:
        return _media_placeholder(_MEDIA_BLOCKS[name], getattr(block, "caption", None), nxt)
    if name in ("PageBlockCollage", "PageBlockSlideshow"):
        return _render_group(block, name, nxt)
    if name == "PageBlockDetails":
        return _render_details(block, nxt)
    if name == "PageBlockCover":
        return _render_block(getattr(block, "cover", None), nxt)
    if name == "PageBlockEmbed":
        url = getattr(block, "url", None) or ""
        caption = _caption(getattr(block, "caption", None), nxt)
        label = f"embed: {caption}" if caption else "embed"
        return f"[{label}]({url})" if url else f"[{label}]"
    if name == "PageBlockEmbedPost":
        url = getattr(block, "url", None) or ""
        author = getattr(block, "author", None) or ""
        head = f"[post: {author}]({url})" if url else f"[post: {author}]"
        body = _render_blocks(getattr(block, "blocks", None), nxt)
        return _quote("\n\n".join(p for p in (head, body) if p))
    if name == "PageBlockMap":
        geo = getattr(block, "geo", None)
        lat, lng = getattr(geo, "lat", None), getattr(geo, "long", None)
        return f"[map: {lat}, {lng}]" if lat is not None and lng is not None else "[map]"
    if name == "PageBlockChannel":
        title = getattr(getattr(block, "channel", None), "title", None)
        return f"[channel: {title}]" if title else "[channel]"
    if name == "PageBlockAuthorDate":
        author = _text(getattr(block, "author", None), nxt)
        date = getattr(block, "published_date", None)
        stamp = date.isoformat() if hasattr(date, "isoformat") else ""
        return ", ".join(p for p in (author, stamp) if p)
    if name == "PageBlockRelatedArticles":
        return _render_related(block, nxt)
    if name == "PageBlockUnsupported":
        return "[unsupported block]"
    return _fallback_block(block, name, nxt)


def _fallback_block(block: Any, name: str, depth: int) -> str:
    """An unknown block: keep whatever text it carries, else name it."""
    inner = getattr(block, "blocks", None)
    if isinstance(inner, (list, tuple)):
        return _render_blocks(inner, depth)
    if hasattr(block, "text"):
        return _text(getattr(block, "text", None), depth)
    label = name[len("PageBlock") :] if name.startswith("PageBlock") else name
    return f"[{label.lower() or 'block'}]"


def _render_code(block: Any, depth: int) -> str:
    code = _plain(getattr(block, "text", None), depth)
    language = getattr(block, "language", "") or ""
    # A fence longer than any backtick run inside keeps the body opaque.
    longest = run = 0
    for char in code:
        run = run + 1 if char == "`" else 0
        longest = max(longest, run)
    fence = "`" * max(3, longest + 1)
    return f"{fence}{language}\n{code}\n{fence}"


def _render_list(items: Any, *, ordered: bool, depth: int) -> str:
    lines: list[str] = []
    for index, item in enumerate(items or (), start=1):
        if depth > MAX_RENDER_DEPTH:
            lines.append("- […]")
            break
        if ordered:
            num = getattr(item, "num", None) or str(index)
            marker = f"{num}. "
        else:
            marker = "- "
        if getattr(item, "checkbox", False):
            marker += "[x] " if getattr(item, "checked", False) else "[ ] "
        if hasattr(item, "blocks"):
            body = _render_blocks(getattr(item, "blocks", None), depth + 1)
        else:
            body = _text(getattr(item, "text", None), depth + 1)
        indent = " " * len(marker.split("[", 1)[0] if ordered else "- ")
        body_lines = body.split("\n") if body else [""]
        lines.append(marker + body_lines[0])
        lines.extend((indent + line) if line else "" for line in body_lines[1:])
    return "\n".join(lines)


def _render_table(block: Any, depth: int) -> str:
    rows: list[list[str]] = []
    header_aligns: list[str] = []
    body_aligns: list[str] = []
    for row in getattr(block, "rows", None) or ():
        cells: list[str] = []
        row_aligns: list[str] = []
        is_header = False
        for cell in getattr(row, "cells", None) or ():
            text = _single_line(_text(getattr(cell, "text", None), depth)).replace("|", "\\|")
            if getattr(cell, "align_center", False):
                align = ":---:"
            elif getattr(cell, "align_right", False):
                align = "---:"
            else:
                align = "---"
            is_header = is_header or bool(getattr(cell, "header", False))
            span = getattr(cell, "colspan", None) or 1
            cells.append(text)
            row_aligns.append(align)
            for _ in range(max(0, int(span) - 1)):
                cells.append("")
                row_aligns.append(align)
        rows.append(cells)
        # Telegram centres header cells whatever the source said, so the
        # column alignment the author wrote is read off the first body row.
        if is_header and not header_aligns:
            header_aligns = row_aligns
        elif not is_header and not body_aligns:
            body_aligns = row_aligns
    if not rows:
        return ""
    width = max(len(r) for r in rows)
    aligns = body_aligns or header_aligns
    aligns = (aligns + ["---"] * width)[:width]
    out = []
    title = _text(getattr(block, "title", None), depth)
    if title:
        out.append(f"**{_single_line(title)}**\n")
    out.append(_table_row(rows[0], width))
    out.append(_table_row(aligns, width))
    out.extend(_table_row(r, width) for r in rows[1:])
    return "\n".join(out)


def _table_row(cells: list[str], width: int) -> str:
    padded = cells + [""] * (width - len(cells))
    return "| " + " | ".join(padded) + " |"


def _render_group(block: Any, name: str, depth: int) -> str:
    tag = "tg-collage" if name == "PageBlockCollage" else "tg-slideshow"
    body = "\n".join(
        part
        for part in (_render_block(item, depth) for item in getattr(block, "items", None) or ())
        if part
    )
    caption = _caption(getattr(block, "caption", None), depth)
    lines = [f"<{tag}>", ""]
    if body:
        lines.append(body)
    if caption:
        lines.append(f"<figcaption>{caption}</figcaption>")
    lines.extend(["", f"</{tag}>"])
    return "\n".join(lines)


def _render_details(block: Any, depth: int) -> str:
    title = _single_line(_text(getattr(block, "title", None), depth))
    body = _render_blocks(getattr(block, "blocks", None), depth)
    opening = "<details open>" if getattr(block, "open", False) else "<details>"
    lines = [f"{opening}<summary>{title}</summary>", ""]
    if body:
        lines.extend([body, ""])
    lines.append("</details>")
    return "\n".join(lines)


def _render_related(block: Any, depth: int) -> str:
    title = _single_line(_text(getattr(block, "title", None), depth))
    items = []
    for article in getattr(block, "articles", None) or ():
        url = getattr(article, "url", None) or ""
        label = getattr(article, "title", None) or url
        items.append(f"- [{label}]({url})" if url else f"- {label}")
    head = f"**{title}**" if title else ""
    return "\n\n".join(p for p in (head, "\n".join(items)) if p)


def _media_placeholder(kind: str, caption: Any, depth: int) -> str:
    text = _single_line(_caption(caption, depth))
    return f"[{kind}: {text}]" if text else f"[{kind}]"


def _caption(caption: Any, depth: int) -> str:
    """A ``PageCaption(text, credit)`` as one string (``text — credit``)."""
    if caption is None:
        return ""
    if type(caption).__name__ != "PageCaption":
        return _text(caption, depth)
    text = _text(getattr(caption, "text", None), depth)
    credit = _text(getattr(caption, "credit", None), depth)
    return " — ".join(p for p in (text, credit) if p)


def _quote(body: str) -> str:
    if not body:
        return ""
    return "\n".join(f"> {line}" if line else ">" for line in body.split("\n"))


def _single_line(text: str) -> str:
    return " ".join(text.split("\n")).strip()


# ---------------------------------------------------------------------------
# Inline text
# ---------------------------------------------------------------------------


def _text(node: Any, depth: int) -> str:
    """Render a ``RichText`` node to inline markdown."""
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    if depth > MAX_RENDER_DEPTH:
        return ""
    name = type(node).__name__
    nxt = depth + 1

    if name == "TextPlain":
        return getattr(node, "text", "") or ""
    if name == "TextEmpty":
        return ""
    if name == "TextConcat":
        return "".join(_text(part, nxt) for part in getattr(node, "texts", None) or ())
    if name in _WRAPPERS:
        inner = _text(getattr(node, "text", None), nxt)
        if not inner.strip():
            return inner
        opening, closing = _WRAPPERS[name]
        return f"{opening}{inner}{closing}"
    if name == "TextFixed":
        return _code_span(_plain(getattr(node, "text", None), nxt))
    if name == "TextMath":
        source = getattr(node, "source", "") or ""
        return f"${source}$" if source else ""
    if name == "TextUrl":
        return _link(_text(getattr(node, "text", None), nxt), getattr(node, "url", "") or "")
    if name == "TextEmail":
        email = getattr(node, "email", "") or ""
        return _link(_text(getattr(node, "text", None), nxt), f"mailto:{email}", bare=email)
    if name == "TextPhone":
        phone = getattr(node, "phone", "") or ""
        return _link(_text(getattr(node, "text", None), nxt), f"tel:{phone}", bare=phone)
    if name == "TextMentionName":
        user_id = getattr(node, "user_id", None)
        label = _text(getattr(node, "text", None), nxt)
        return f"[{label}](tg://user?id={user_id})" if user_id else label
    if name == "TextCustomEmoji":
        return getattr(node, "alt", "") or ""
    if name == "TextImage":
        return ""
    if name in _TRANSPARENT_TEXT:
        return _text(getattr(node, "text", None), nxt)
    # Unknown inline node: keep its text if it has any.
    inner = getattr(node, "text", None)
    if inner is not None:
        return _text(inner, nxt)
    texts = getattr(node, "texts", None)
    if isinstance(texts, (list, tuple)):
        return "".join(_text(part, nxt) for part in texts)
    return ""


def _plain(node: Any, depth: int) -> str:
    """The text of a ``RichText`` node with every formatting marker dropped."""
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    if depth > MAX_RENDER_DEPTH:
        return ""
    name = type(node).__name__
    if name == "TextPlain":
        return getattr(node, "text", "") or ""
    if name == "TextConcat":
        return "".join(_plain(p, depth + 1) for p in getattr(node, "texts", None) or ())
    if name == "TextMath":
        return getattr(node, "source", "") or ""
    if name == "TextCustomEmoji":
        return getattr(node, "alt", "") or ""
    inner = getattr(node, "text", None)
    return _plain(inner, depth + 1) if inner is not None else ""


def _link(label: str, url: str, *, bare: str | None = None) -> str:
    if not url:
        return label
    if not label or label == url or (bare is not None and label == bare):
        return bare if bare else url
    return f"[{label}]({url})"


def _code_span(code: str) -> str:
    if not code:
        return ""
    longest = run = 0
    for char in code:
        run = run + 1 if char == "`" else 0
        longest = max(longest, run)
    ticks = "`" * (longest + 1)
    pad = " " if code.startswith("`") or code.endswith("`") else ""
    return f"{ticks}{pad}{code}{pad}{ticks}"
