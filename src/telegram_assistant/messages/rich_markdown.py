"""Line-based block scanner for the Telegram *rich message* markdown dialect.

The rich-send path (:attr:`SendMessageRequest.rich_markdown`) hands markdown to
Telegram, which parses it **server-side** into ``PageBlock``s. Two things this
project wants to do — insert blank-looking spacer paragraphs and group
consecutive media into ``<tg-collage>`` — need to know where the block
boundaries are, so this module reconstructs them locally.

It deliberately does **not** use a CommonMark parser. Telegram's dialect
diverges (``==mark==``, ``||spoiler||``, ``<tg-collage>``, footnotes) and a
parse → render round-trip would rewrite the author's text. The scanner only
classifies line ranges; every byte of the source is preserved in
:attr:`Block.lines`, so callers can rebuild the document by concatenation and
touch only what they mean to touch.

Block counting mirrors Telegram's documented limit ("500 blocks, including
nested blocks, list items, table rows, quotation and details blocks"): a list
counts as itself plus one per item, a table as itself plus one per row, and a
quote/HTML block as itself plus everything nested inside it. It is an
approximation of a server-side rule — used to *warn*, never to reject.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

#: Telegram's ceiling on blocks in one rich message (nested blocks included).
MAX_RICH_BLOCKS = 500

#: Telegram's ceiling on media attachments in one rich message.
MAX_RICH_MEDIA = 50

#: HTML container tags the dialect defines; their contents are scanned as
#: nested blocks so grouping can tell author-written groups from runs it may
#: wrap itself.
HTML_BLOCK_TAGS = ("details", "tg-collage", "tg-slideshow")

_FENCE_RE = re.compile(r"^ {0,3}(?P<char>`{3,}|~{3,})(?P<info>.*)$")
_ATX_RE = re.compile(r"^ {0,3}(?P<hashes>#{1,6})(?:\s|$)")
_SETEXT_RE = re.compile(r"^ {0,3}(?:=+|-+)\s*$")
_DIVIDER_RE = re.compile(r"^ {0,3}(?:(?:-\s*){3,}|(?:\*\s*){3,}|(?:_\s*){3,})$")
_QUOTE_RE = re.compile(r"^ {0,3}>")
_LIST_RE = re.compile(r"^ {0,3}(?:[-*+]|\d{1,9}[.)])(?:\s|$)")
_ANY_LIST_RE = re.compile(r"^\s*(?:[-*+]|\d{1,9}[.)])(?:\s|$)")
_FOOTNOTE_RE = re.compile(r"^ {0,3}\[\^[^\]]+\]:")
_TABLE_DELIM_RE = re.compile(r"^ {0,3}\|?(?:\s*:?-{1,}:?\s*\|)+\s*:?-*:?\s*\|?\s*$")
_HTML_OPEN_RE = re.compile(
    r"^ {0,3}</?(?P<tag>" + "|".join(HTML_BLOCK_TAGS) + r")\b[^>]*>", re.IGNORECASE
)

_MEDIA_MD_RE = re.compile(
    r"""^!\[(?P<alt>[^\]]*)\]\(\s*
        (?:<(?P<angle>[^>]*)>|(?P<plain>[^()\s]+))
        (?:\s+(?:"(?P<dquote>[^"]*)"|'(?P<squote>[^']*)'|\((?P<pquote>[^)]*)\)))?
        \s*\)$""",
    re.VERBOSE,
)
_MEDIA_OBSIDIAN_RE = re.compile(r"^!\[\[(?P<body>[^\]]+)\]\]$")

_SIZE_RE = re.compile(r"^\d+(?:x\d+)?$")
_ALIGNMENTS = frozenset({"left", "center", "right"})

#: Every block kind the scanner emits.
BLOCK_KINDS = (
    "paragraph",
    "heading",
    "list",
    "table",
    "quote",
    "code",
    "divider",
    "media",
    "html",
    "footnote",
)


@dataclass(frozen=True)
class MediaRef:
    """One media reference standing alone on its own line.

    ``target`` is the reference as written (an http(s) URL, a relative or
    absolute local path, or an Obsidian vault file name). ``alt`` is the alt
    text with any trailing Obsidian size/alignment segments removed, and
    ``caption`` is what should be shown under the media: the markdown title
    (``![alt](url "caption")``) when present, else the alt text. ``is_remote``
    is ``True`` only for an ``http``/``https`` target — everything else needs
    local resolution and an upload.
    """

    target: str
    alt: str
    caption: str
    is_remote: bool
    obsidian: bool = False
    size: str | None = None
    alignment: str | None = None
    raw: str = ""


@dataclass(frozen=True)
class Block:
    """One top-level block of the source, as a half-open line range.

    ``lines`` holds the source lines verbatim (newline-normalised), so
    ``"\\n".join(lines)`` reproduces them. ``start``/``end`` index into the
    normalised line list of the *whole* document, so nested blocks stay
    addressable. ``weight`` is this block's contribution to Telegram's
    500-block budget, children included.
    """

    kind: str
    lines: tuple[str, ...]
    start: int
    end: int
    weight: int = 1
    media: MediaRef | None = None
    children: tuple[Block, ...] = field(default_factory=tuple)
    level: int | None = None
    html_tag: str | None = None

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


def split_lines(markdown: str) -> list[str]:
    """Normalise newlines (CRLF/CR → LF) and split into lines.

    A trailing newline does **not** produce a trailing empty line, so a
    document ending in ``"\\n"`` and one that does not scan identically.
    """

    normalized = markdown.replace("\r\n", "\n").replace("\r", "\n")
    if not normalized:
        return []
    lines = normalized.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return lines


def parse_media_line(line: str) -> MediaRef | None:
    """Parse a line that is *entirely* one media reference, else ``None``.

    Both dialects are recognised: markdown ``![alt](target "caption")`` (the
    form Telegram documents) and Obsidian's ``![[file.png|caption|150]]``
    embed. Obsidian's size (``150``, ``150x200``) and alignment
    (``left``/``center``/``right``) segments are split out of the caption;
    ``%`` and ``%.%`` expand to the file name without/with its extension, as
    the obsidian-image-captions plugin defines them.
    """

    stripped = line.strip()
    if not stripped.startswith("!["):
        return None

    obsidian = _MEDIA_OBSIDIAN_RE.match(stripped)
    if obsidian:
        parts = obsidian.group("body").split("|")
        target = parts[0].strip()
        if not target:
            return None
        caption, size, alignment = _split_obsidian_segments(parts[1:], target)
        return MediaRef(
            target=target,
            alt=caption,
            caption=caption,
            is_remote=_is_remote(target),
            obsidian=True,
            size=size,
            alignment=alignment,
            raw=stripped,
        )

    match = _MEDIA_MD_RE.match(stripped)
    if not match:
        return None
    target = (match.group("angle") if match.group("angle") is not None else match.group("plain")) or ""
    target = target.strip()
    if not target:
        return None
    title = match.group("dquote")
    if title is None:
        title = match.group("squote")
    if title is None:
        title = match.group("pquote")
    alt, size, alignment = _split_markdown_alt(match.group("alt") or "", target)
    return MediaRef(
        target=target,
        alt=alt,
        caption=title if title else alt,
        is_remote=_is_remote(target),
        obsidian=False,
        size=size,
        alignment=alignment,
        raw=stripped,
    )


def scan_blocks(markdown: str) -> tuple[Block, ...]:
    """Split ``markdown`` into typed top-level blocks.

    Blank lines are separators and belong to no block. Fenced and indented
    code is consumed opaquely — its contents are never inspected, so a ``#``,
    a ``|`` or a ``![](…)`` inside a fence never becomes a heading, a table or
    media. ``<details>``/``<tg-collage>``/``<tg-slideshow>`` bodies *are*
    scanned, and land in :attr:`Block.children`.
    """

    return _scan(split_lines(markdown), 0)


def count_blocks(blocks: tuple[Block, ...] | list[Block]) -> int:
    """Approximate the block count Telegram charges against its 500 limit."""

    return sum(block.weight for block in blocks)


def count_media(blocks: tuple[Block, ...] | list[Block]) -> int:
    """Count media blocks, including those nested inside HTML/quote blocks."""

    total = 0
    for block in blocks:
        if block.kind == "media":
            total += 1
        if block.children:
            total += count_media(block.children)
    return total


def iter_media(blocks: tuple[Block, ...] | list[Block]):
    """Yield every media block in document order, nested blocks included."""

    for block in blocks:
        if block.kind == "media":
            yield block
        if block.children:
            yield from iter_media(block.children)


# --- internals ---------------------------------------------------------------


def _is_remote(target: str) -> bool:
    return target.lower().startswith(("http://", "https://"))


def _classify_segment(segment: str) -> str:
    value = segment.strip()
    if _SIZE_RE.match(value):
        return "size"
    if value.lower() in _ALIGNMENTS:
        return "alignment"
    return "caption"


def _split_obsidian_segments(
    segments: list[str], target: str
) -> tuple[str, str | None, str | None]:
    caption = ""
    size: str | None = None
    alignment: str | None = None
    for segment in segments:
        kind = _classify_segment(segment)
        if kind == "size" and size is None:
            size = segment.strip()
        elif kind == "alignment" and alignment is None:
            alignment = segment.strip().lower()
        elif kind == "caption" and not caption:
            caption = segment.strip()
    return _expand_filename_caption(caption, target), size, alignment


def _split_markdown_alt(alt: str, target: str) -> tuple[str, str | None, str | None]:
    """Strip trailing Obsidian size/alignment segments off a markdown alt text."""

    parts = alt.split("|")
    size: str | None = None
    alignment: str | None = None
    while len(parts) > 1:
        kind = _classify_segment(parts[-1])
        if kind == "size" and size is None:
            size = parts.pop().strip()
        elif kind == "alignment" and alignment is None:
            alignment = parts.pop().strip().lower()
        else:
            break
    return _expand_filename_caption("|".join(parts).strip(), target), size, alignment


def _expand_filename_caption(caption: str, target: str) -> str:
    if caption not in {"%", "%.%"}:
        return caption
    name = target.replace("\\", "/").rsplit("/", 1)[-1]
    if caption == "%.%":
        return name
    stem, _, _ = name.rpartition(".")
    return stem or name


def _fence_open(line: str) -> str | None:
    match = _FENCE_RE.match(line)
    if not match:
        return None
    return match.group("char")


def _fence_closes(line: str, opener: str) -> bool:
    match = _FENCE_RE.match(line)
    if not match:
        return False
    closer = match.group("char")
    return closer[0] == opener[0] and len(closer) >= len(opener) and not match.group("info").strip()


def _html_open(line: str) -> str | None:
    match = _HTML_OPEN_RE.match(line)
    if not match or line.lstrip().startswith("</"):
        return None
    return match.group("tag").lower()


def _html_closed_on(line: str, tag: str) -> bool:
    return f"</{tag}>" in line.lower()


def _is_indented_code(line: str) -> bool:
    return line.startswith("    ") or line.startswith("\t")


def _leading_ws(line: str) -> int:
    return len(line) - len(line.lstrip())


def _is_table_start(lines: list[str], index: int) -> bool:
    if "|" not in lines[index]:
        return False
    nxt = index + 1
    return nxt < len(lines) and "|" in lines[nxt] and bool(_TABLE_DELIM_RE.match(lines[nxt]))


def _starts_new_block(lines: list[str], index: int) -> bool:
    line = lines[index]
    return bool(
        _fence_open(line)
        or _html_open(line)
        or _DIVIDER_RE.match(line)
        or _ATX_RE.match(line)
        or _QUOTE_RE.match(line)
        or _LIST_RE.match(line)
        or _FOOTNOTE_RE.match(line)
        or _is_table_start(lines, index)
        or parse_media_line(line) is not None
    )


def _media_block_at(lines: list[str], index: int) -> MediaRef | None:
    """Return the media on line ``index`` when it stands as its own block.

    A media line directly followed by prose is inline markdown, not a media
    block, so it stays part of the paragraph. A run of media lines with no
    blank line between them (the shape Obsidian produces) *is* a run of media
    blocks, which is what the collage grouping later keys on.
    """

    media = parse_media_line(lines[index])
    if media is None:
        return None
    nxt = index + 1
    if nxt >= len(lines):
        return media
    following = lines[nxt]
    if not following.strip() or parse_media_line(following) is not None:
        return media
    return media if _starts_new_block(lines, nxt) else None


def _scan(lines: list[str], offset: int) -> tuple[Block, ...]:
    blocks: list[Block] = []
    index = 0
    total = len(lines)
    while index < total:
        line = lines[index]
        if not line.strip():
            index += 1
            continue
        if _is_indented_code(line):
            index = _consume_indented_code(lines, index, offset, blocks)
            continue
        if _fence_open(line):
            index = _consume_fence(lines, index, offset, blocks)
            continue
        tag = _html_open(line)
        if tag:
            index = _consume_html(lines, index, offset, blocks, tag)
            continue
        if _is_table_start(lines, index):
            index = _consume_table(lines, index, offset, blocks)
            continue
        if _DIVIDER_RE.match(line):
            blocks.append(Block("divider", (line,), offset + index, offset + index + 1))
            index += 1
            continue
        atx = _ATX_RE.match(line)
        if atx:
            blocks.append(
                Block(
                    "heading",
                    (line,),
                    offset + index,
                    offset + index + 1,
                    level=len(atx.group("hashes")),
                )
            )
            index += 1
            continue
        if _QUOTE_RE.match(line):
            index = _consume_quote(lines, index, offset, blocks)
            continue
        if _LIST_RE.match(line):
            index = _consume_list(lines, index, offset, blocks)
            continue
        if _FOOTNOTE_RE.match(line):
            index = _consume_footnote(lines, index, offset, blocks)
            continue
        media = _media_block_at(lines, index)
        if media is not None:
            blocks.append(
                Block("media", (line,), offset + index, offset + index + 1, media=media)
            )
            index += 1
            continue
        index = _consume_paragraph(lines, index, offset, blocks)
    return tuple(blocks)


def _consume_indented_code(
    lines: list[str], index: int, offset: int, blocks: list[Block]
) -> int:
    end = index
    last_content = index
    while end < len(lines):
        line = lines[end]
        if not line.strip():
            end += 1
            continue
        if not _is_indented_code(line):
            break
        last_content = end
        end += 1
    end = last_content + 1
    blocks.append(Block("code", tuple(lines[index:end]), offset + index, offset + end))
    return end


def _consume_fence(lines: list[str], index: int, offset: int, blocks: list[Block]) -> int:
    opener = _fence_open(lines[index]) or "```"
    end = index + 1
    while end < len(lines) and not _fence_closes(lines[end], opener):
        end += 1
    if end < len(lines):
        end += 1
    blocks.append(Block("code", tuple(lines[index:end]), offset + index, offset + end))
    return end


def _consume_html(
    lines: list[str], index: int, offset: int, blocks: list[Block], tag: str
) -> int:
    if _html_closed_on(lines[index], tag):
        end = index + 1
        inner_start = inner_end = index + 1
    else:
        end = index + 1
        while end < len(lines) and not _html_closed_on(lines[end], tag):
            end += 1
        inner_start = index + 1
        inner_end = min(end, len(lines))
        if end < len(lines):
            end += 1
    children = _scan(lines[inner_start:inner_end], offset + inner_start)
    blocks.append(
        Block(
            "html",
            tuple(lines[index:end]),
            offset + index,
            offset + end,
            weight=1 + count_blocks(children),
            children=children,
            html_tag=tag,
        )
    )
    return end


def _consume_table(lines: list[str], index: int, offset: int, blocks: list[Block]) -> int:
    end = index
    rows = 0
    while end < len(lines):
        line = lines[end]
        if not line.strip() or "|" not in line:
            break
        if not _TABLE_DELIM_RE.match(line):
            rows += 1
        end += 1
    blocks.append(
        Block("table", tuple(lines[index:end]), offset + index, offset + end, weight=1 + rows)
    )
    return end


def _consume_quote(lines: list[str], index: int, offset: int, blocks: list[Block]) -> int:
    end = index
    while end < len(lines) and _QUOTE_RE.match(lines[end]):
        end += 1
    inner = [re.sub(r"^ {0,3}> ?", "", line) for line in lines[index:end]]
    children = _scan(inner, offset + index)
    blocks.append(
        Block(
            "quote",
            tuple(lines[index:end]),
            offset + index,
            offset + end,
            weight=1 + count_blocks(children),
            children=children,
        )
    )
    return end


def _consume_list(lines: list[str], index: int, offset: int, blocks: list[Block]) -> int:
    end = index
    items = 0
    total = len(lines)
    while end < total:
        line = lines[end]
        if not line.strip():
            look = end
            while look < total and not lines[look].strip():
                look += 1
            if look < total and (
                _ANY_LIST_RE.match(lines[look]) or _leading_ws(lines[look]) >= 2
            ):
                end = look
                continue
            break
        if _ANY_LIST_RE.match(line):
            items += 1
            end += 1
            continue
        if _leading_ws(line) >= 2:
            end += 1
            continue
        break
    blocks.append(
        Block("list", tuple(lines[index:end]), offset + index, offset + end, weight=1 + items)
    )
    return end


def _consume_footnote(lines: list[str], index: int, offset: int, blocks: list[Block]) -> int:
    end = index + 1
    total = len(lines)
    while end < total and lines[end].strip() and _leading_ws(lines[end]) >= 2:
        end += 1
    blocks.append(Block("footnote", tuple(lines[index:end]), offset + index, offset + end))
    return end


def _consume_paragraph(lines: list[str], index: int, offset: int, blocks: list[Block]) -> int:
    end = index + 1
    total = len(lines)
    kind = "paragraph"
    level: int | None = None
    while end < total:
        line = lines[end]
        if not line.strip():
            break
        if _SETEXT_RE.match(line):
            # ``Title`` followed by ``===``/``---`` is a setext heading, not a
            # paragraph plus a divider.
            level = 1 if line.lstrip().startswith("=") else 2
            end += 1
            kind = "heading"
            break
        if _starts_new_block(lines, end):
            break
        end += 1
    blocks.append(
        Block(kind, tuple(lines[index:end]), offset + index, offset + end, level=level)
    )
    return end
