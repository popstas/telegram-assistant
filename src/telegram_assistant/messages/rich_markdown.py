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

:func:`normalize_rich_markdown` is the writing half. It inserts
:data:`SPACER_LINE` (a lone U+00A0) as its own block where Telegram would
otherwise render two paragraphs tight against each other — a line holding only
a non-breaking space is *not* blank to this scanner, since the server parses it
as a ``PageBlockParagraph``, which is exactly why it works as a spacer and what
makes normalisation idempotent — and it wraps a run of consecutive media blocks
in ``<tg-collage>``/``<tg-slideshow>`` (:data:`MEDIA_GROUP_MODES`), leaving
media the author already grouped by hand untouched.

:func:`scan_media` is the other writing half: it resolves media that points at a
*local* file and rewrites the reference into the ``tg://`` form MTProto pairs
with ``InputRichMessageMarkdown.files`` (see :data:`RICH_FILE_SCHEMES`).
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import unquote

#: Telegram's ceiling on blocks in one rich message (nested blocks included).
MAX_RICH_BLOCKS = 500

#: Telegram's ceiling on media attachments in one rich message.
MAX_RICH_MEDIA = 50

#: How deep the block scanner nests quotes/HTML containers before it stops
#: recursing and emits the container as a leaf (see :func:`scan_blocks`). Well
#: past any real article — a body 64 quotes deep has spent its 500-block budget
#: — but far enough under Python's ~1000-frame limit that the scan cannot raise
#: ``RecursionError`` on caller input.
MAX_BLOCK_NESTING = 64

#: HTML container tags the dialect defines; their contents are scanned as
#: nested blocks so grouping can tell author-written groups from runs it may
#: wrap itself.
HTML_BLOCK_TAGS = ("details", "tg-collage", "tg-slideshow")

#: The non-breaking space. A line holding only this renders as an empty
#: paragraph block, which is how vertical space is added to an article.
NBSP = "\u00a0"

#: The spacer :func:`normalize_rich_markdown` inserts, alone on its own line.
SPACER_LINE = NBSP

#: How a run of consecutive media blocks may be grouped. ``collage`` and
#: ``slideshow`` are the dialect's own container tags; ``none`` leaves the run
#: as separate media blocks.
MEDIA_GROUP_MODES = ("collage", "slideshow", "none")

#: Grouping applied to every detected run unless a per-group override says
#: otherwise (config: ``telegram.defaults.rich_markdown_grouping``).
DEFAULT_MEDIA_GROUP_MODE = "collage"

#: The container tag each grouping mode wraps a media run in.
MEDIA_GROUP_TAGS = {"collage": "tg-collage", "slideshow": "tg-slideshow"}

#: The tag that gives a container its own caption. Telegram folds it into the
#: group's ``caption`` field rather than rendering it as a block, so it does not
#: count toward the block budget — ``scripts/spike_rich_collage_caption.py``
#: proved both that and that a bare text line inside the tag leaks out as a
#: paragraph *after* the group instead.
FIGCAPTION_TAG = "figcaption"

#: How much of the text before a media run is reported back, so a surface can
#: say *which* run it is asking about without echoing the article.
MEDIA_GROUP_CONTEXT_CHARS = 50

#: How the markdown body names a file carried in
#: ``InputRichMessageMarkdown.files``, per media kind. Proven against the live
#: API (see the Task 5 findings in
#: ``docs/plans/20260727-rich-markdown-spacing-and-media.md``): a plain id, a
#: relative path or a ``tg://file?id=``/``attach://`` reference is rejected with
#: ``RICH_MESSAGE_PHOTO_URL_INVALID``, and the scheme must match the uploaded
#: media (a photo named through ``tg://video`` fails ``RICH_MESSAGE_VIDEO_INVALID``).
RICH_FILE_SCHEMES = {
    "photo": "tg://photo?id=",
    "video": "tg://video?id=",
    "audio": "tg://audio?id=",
}

#: Suffixes Telegram renders as a photo block; everything else is uploaded as a
#: document. ``.gif`` is an animation — a document — so it goes out as a video.
PHOTO_SUFFIXES = frozenset({".jpg", ".jpeg", ".png", ".webp"})
AUDIO_SUFFIXES = frozenset({".mp3", ".ogg", ".oga", ".opus", ".m4a", ".wav", ".flac"})
VIDEO_SUFFIXES = frozenset({".mp4", ".mov", ".webm", ".mkv", ".avi", ".m4v", ".gif"})

#: ``InputRichFile.id`` is a caller-chosen *ASCII identifier*: the server
#: rejects a dot, a space or a non-ASCII character with
#: ``RICH_MESSAGE_FILE_ID_INVALID``. 64 characters are accepted.
RICH_FILE_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")
MAX_RICH_FILE_ID_CHARS = 64

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
# A whole-line ``<figcaption>…</figcaption>``. It is deliberately *not* in
# ``HTML_BLOCK_TAGS``: the scanner should keep reading it as one line inside its
# container, not open a nested block for it.
_FIGCAPTION_RE = re.compile(
    rf"^ {{0,3}}<{FIGCAPTION_TAG}\b[^>]*>.*</{FIGCAPTION_TAG}>\s*$", re.IGNORECASE
)

# The anchored forms classify a *line* (is this block media?); the unanchored
# ones find every reference *within* a line, wherever it sits — a list item, a
# table cell, a footnote, or mid-sentence. Both share one pattern so the two
# never drift apart on what counts as a reference.
# A bare destination admits *balanced* parentheses and backslash escapes, and a
# title admits an escaped copy of its own quote character — both are CommonMark,
# and both name real files (``Screenshot(1).png``, a caption quoting speech). A
# pattern that stopped at the first ``(`` or ``"`` would not recognise the
# reference *at all*, so ``scan_media`` would leave the local path in the
# article and send it to Telegram verbatim — the one silent drop it promises
# never to make. Each alternation branch below starts on a distinct character,
# so the nesting cannot backtrack catastrophically.
_MEDIA_MD_PATTERN = r"""!\[(?P<alt>[^\]]*)\]\(\s*
        (?:<(?P<angle>[^>]*)>
          |(?P<plain>(?:[^()\s\\]|\\.|\((?:[^()\s\\]|\\.)*\))+))
        (?:\s+(?:"(?P<dquote>(?:[^"\\]|\\.)*)"
              |'(?P<squote>(?:[^'\\]|\\.)*)'
              |\((?P<pquote>(?:[^()\\]|\\.)*)\)))?
        \s*\)"""
_MEDIA_OBSIDIAN_PATTERN = r"!\[\[(?P<body>[^\]]+)\]\]"

_MEDIA_MD_RE = re.compile(r"^" + _MEDIA_MD_PATTERN + r"$", re.VERBOSE)
_MEDIA_OBSIDIAN_RE = re.compile(r"^" + _MEDIA_OBSIDIAN_PATTERN + r"$")
_MEDIA_MD_INLINE_RE = re.compile(_MEDIA_MD_PATTERN, re.VERBOSE)
_MEDIA_OBSIDIAN_INLINE_RE = re.compile(_MEDIA_OBSIDIAN_PATTERN)

#: An inline code span: a backtick run, the shortest body, the same run again.
#: Code spans are opaque to media resolution for the same reason fenced code is
#: (see :func:`scan_blocks`) — an article documenting the dialect writes
#: ``` `![](shot.png)` ``` and means the text, not a file to upload.
_CODE_SPAN_RE = re.compile(r"(?P<ticks>`+)(?P<body>.+?)(?P=ticks)")

#: An Obsidian wikilink: ``[[target]]`` or ``[[target|alias]]``. The negative
#: lookbehind is what keeps ``![[file.png]]`` out — that is a media embed, owned
#: by :func:`scan_media`, and expanding it here would strip the file reference
#: down to prose before anything could upload it.
_WIKILINK_RE = re.compile(r"(?<!!)\[\[(?P<body>[^\[\]]*)\]\]")

#: A CommonMark backslash escape: a backslash before ASCII punctuation. A
#: backslash before anything else is a literal backslash, which is what keeps a
#: Windows-style ``C:\Users\me\a.png`` target intact.
_MD_ESCAPE_RE = re.compile(r"\\([!-/:-@\[-`{-~])")

_SIZE_RE = re.compile(r"^\d+(?:x\d+)?$")
_ALIGNMENTS = frozenset({"left", "center", "right"})

#: A YAML mapping entry: a key, a colon, then end of line or whitespace-led
#: value. The leading character excludes whitespace (an indented continuation)
#: and ``#`` (a comment — and, crucially, a markdown ATX heading). Requiring
#: whitespace after the colon is what rejects a bare URL (``https://…``) while
#: still accepting ``time: 10:30``.
_FRONTMATTER_KEY_RE = re.compile(r"^[^\s#][^:]*:(?:\s.*)?$")

#: A YAML sequence item or an indented continuation line.
_FRONTMATTER_CONT_RE = re.compile(r"^(?:-(?:\s|$)|\s+\S)")

#: A YAML comment. Identical in syntax to a markdown ATX heading, which is why
#: it is accepted only *after* the first body line: the "first line must be a
#: mapping entry" rule is what keeps an article opening with a ``---`` rule and
#: a heading from being read as frontmatter, and that rule stays untouched.
_FRONTMATTER_COMMENT_RE = re.compile(r"^\s*#")

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
class MediaGroupChoice:
    """A caller's decision about one detected media run.

    ``index`` is the run's position in :attr:`RichMarkdownNormalization.groups`
    (0-based, document order) and ``mode`` one of :data:`MEDIA_GROUP_MODES`.
    """

    index: int
    mode: str


@dataclass(frozen=True)
class MediaGroup:
    """One run of consecutive media blocks the grouping pass found.

    ``size`` is how many media blocks the run holds, ``mode`` the *effective*
    decision (config default, or the caller's override), and
    ``preceding_text`` the tail of the text right above the run — enough for a
    surface to ask "the media after «…» — collage or slideshow?" without
    echoing the article. ``caption`` is the group caption built from the
    captions of its media (see :func:`_group_caption`); it is reported for every
    run, including one grouped ``none``, and is empty when no member carries a
    caption.
    """

    index: int
    size: int
    preceding_text: str
    mode: str
    caption: str = ""


class MediaGroupError(ValueError):
    """A media-group override named an unknown run or an unknown mode."""


#: What :func:`normalize_rich_markdown` accepts as per-group overrides: the
#: :class:`MediaGroupChoice` sequence the surfaces build (``SendMessageRequest``
#: carries exactly that), or a plain ``{index: mode}`` mapping for a direct
#: call.
MediaGroupOverrides = Mapping[int, str] | Iterable[MediaGroupChoice] | None


@dataclass(frozen=True)
class RichMarkdownNormalization:
    """What :func:`normalize_rich_markdown` decided about one article.

    ``markdown`` is what should actually go to Telegram, ``blocks``/``media``
    are the approximate counts of that text, ``spaced`` says whether the spacer
    pass ran (it is ``False`` when disabled *or* rolled back), ``groups``
    describes every detected media run with the mode it was given, and
    ``warnings`` are operator-facing strings — normalisation only raises for
    caller input it cannot honour (an unknown group index); what Telegram
    accepts is left to Telegram.
    """

    markdown: str
    blocks: int
    media: int
    spaced: bool
    warnings: tuple[str, ...] = ()
    groups: tuple[MediaGroup, ...] = ()
    #: Whether each pass actually rewrote the source. ``spaced`` says the
    #: spacer pass ran; these say it (and the grouping and line-splitting
    #: passes) changed something, which is what a caller reporting a size
    #: increase must name.
    grouped: bool = False
    spacers_added: bool = False
    lines_split: bool = False
    #: How many Obsidian wikilinks :func:`strip_wikilinks` expanded. Unlike the
    #: flags above this is a count, because it is what a surface reports to the
    #: operator — there is no knob to explain, only a number.
    wikilinks: int = 0


class MediaResolutionError(ValueError):
    """A local media reference could not be turned into a file to upload."""


class AmbiguousMediaError(MediaResolutionError):
    """An Obsidian embed matched more than one file, equally close by."""


@dataclass(frozen=True)
class RichFile:
    """One local file to upload alongside a rich message.

    ``id`` is the ASCII identifier the markdown names (see
    :data:`RICH_FILE_ID_RE`), ``path`` the resolved absolute path, ``caption``
    what the first reference asked to show under it, and ``kind`` one of
    ``photo``/``video``/``audio`` — the scheme in the markdown and the upload
    shape must agree, so it is decided once, here.
    """

    id: str
    path: str
    caption: str
    kind: str


@dataclass(frozen=True)
class MediaScan:
    """What :func:`scan_media` made of one article.

    ``markdown`` is the body with every *local* media reference rewritten to
    its ``tg://`` form (returned by identity when nothing was local, so byte
    fidelity survives) and ``files`` are the uploads it needs, in markdown
    order. http(s) targets are left exactly as written for the server to fetch.
    """

    markdown: str
    files: tuple[RichFile, ...] = ()


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

    ``span`` is the half-open ``(start, end)`` of ``raw`` inside the line it was
    found in, set only by :func:`iter_line_media_refs`. The rewrite splices at
    that span rather than searching for ``raw``: an identical reference masked
    out earlier on the same line (inside an inline code span) is *skipped* by
    the sweep, so a first-occurrence search would rewrite the masked copy and
    ship the real one as a literal local path.
    """

    target: str
    alt: str
    caption: str
    is_remote: bool
    obsidian: bool = False
    size: str | None = None
    alignment: str | None = None
    raw: str = ""
    span: tuple[int, int] | None = None


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


def strip_yaml_frontmatter(markdown: str) -> str:
    """Drop a leading ``---`` … ``---`` YAML frontmatter block.

    This is **not** part of :func:`normalize_rich_markdown` — that one only ever
    inserts or wraps lines — and it is deliberately called at the CLI's
    file-read boundary only, next to the ``utf-8-sig`` BOM strip and for the
    same reason: it is where "this is a note file" is known. An Obsidian note
    opens with frontmatter, which no surface of this dialect renders as
    metadata: the scanner reads the opening ``---`` as a divider and the
    ``key: value`` lines as a **setext heading** underlined by the closing
    ``---``, so the article would begin with a rule and a large heading reading
    ``tags: [...] date: ...`` — with a spacer inserted inside it. HTTP/MCP take
    a markdown *string* an agent composed rather than a note, so their input is
    passed through untouched instead of being silently rewritten.

    Only an exact opening ``---`` on the first line starts a block, only a
    matching closing ``---`` ends one, and — the part that keeps this from
    eating real content — the lines **between** them must read as YAML
    (:func:`_is_frontmatter_body`). A document that simply starts with a
    horizontal rule keeps it, whether or not a later ``---`` divider exists:
    matching on the fences alone would treat ``---``, an opening section, then
    the next ``---`` divider as frontmatter and silently drop that whole
    section, which nothing downstream would ever report (the CLI never echoes
    the body). The remainder is returned by slicing the original text, so CRLF
    endings and the trailing newline survive byte-for-byte — the same identity
    contract the normalisation passes keep. Returns the input unchanged when
    there is no frontmatter.
    """

    lines = markdown.splitlines(keepends=True)
    if not lines or lines[0].rstrip() != "---":
        return markdown
    for index in range(1, len(lines)):
        if lines[index].rstrip() != "---":
            continue
        if not _is_frontmatter_body(lines[1:index]):
            return markdown
        return "".join(lines[index + 1 :])
    return markdown


def _is_frontmatter_body(lines: list[str]) -> bool:
    """``True`` when every line between two ``---`` fences reads as YAML.

    The first line must be a mapping entry — that alone rejects the shape this
    guard exists for, a leading ``---`` rule followed by a blank line and prose
    — and every later non-blank line must be a mapping entry, a sequence item,
    an indented continuation, or a comment. An empty block (``---`` directly
    under ``---``) is two rules, not frontmatter.

    A comment is legal YAML and common in an Obsidian note, but its syntax is a
    markdown ATX heading's, so it is accepted only after the first line: on the
    first line it is a heading and the block is not frontmatter. Rejecting it
    outright is worse than either — the note keeps its fences, and the scanner
    then reads the opening ``---`` as a divider and the closing one as a setext
    underline, sending the note's own metadata as a rule and a large heading.
    """

    body = [line.rstrip("\r\n") for line in lines]
    if not body or not _FRONTMATTER_KEY_RE.match(body[0]):
        return False
    return all(
        not line.strip()
        or _FRONTMATTER_KEY_RE.match(line)
        or _FRONTMATTER_CONT_RE.match(line)
        or _FRONTMATTER_COMMENT_RE.match(line)
        for line in body[1:]
    )


def _expand_wikilink(body: str) -> str | None:
    """Return the text a wikilink's body should become, or ``None`` to keep it.

    One rule, no special cases: the alias wins when there is one, otherwise the
    target reads as Obsidian renders it — ``#`` becomes ``>`` and a leading
    ``#`` (a link into the current note) simply drops. Block references
    (``note#^blk``) fall out of that unchanged, which is why they need no
    branch of their own.
    """

    target, _, alias = body.partition("|")
    alias = alias.strip()
    if alias:
        return alias
    target = target.strip()
    if target.startswith("#"):
        target = target[1:]
    if not target:
        # Neither half carries text, so this is not a link. Returning None
        # ships it verbatim: silently deleting characters the author typed is
        # worse than leaving a curiosity in the article.
        return None
    return target.replace("#", " > ")


def _strip_line_wikilinks(line: str) -> tuple[str, int]:
    """Expand every wikilink on one line, leaving inline code spans alone."""

    code_spans = [match.span() for match in _CODE_SPAN_RE.finditer(line)]
    out: list[str] = []
    cursor = 0
    count = 0
    for match in _WIKILINK_RE.finditer(line):
        start, end = match.span()
        # Containment, not overlap — the rule iter_line_media_refs uses. A span
        # that merely overlaps the link (a backtick inside the alias) must not
        # shield it, or the raw brackets ship.
        if any(span_start <= start and end <= span_end for span_start, span_end in code_spans):
            continue
        text = _expand_wikilink(match.group("body"))
        if text is None:
            continue
        out.append(line[cursor:start])
        out.append(text)
        cursor = end
        count += 1
    if not count:
        return line, 0
    out.append(line[cursor:])
    return "".join(out), count


def _code_line_indices(blocks: tuple[Block, ...] | list[Block]) -> set[int]:
    """Document line indices covered by a code block, nesting included."""

    found: set[int] = set()
    for block in blocks:
        if block.kind == "code":
            found.update(range(block.start, block.end))
        elif block.children:
            found.update(_code_line_indices(block.children))
    return found


def strip_wikilinks(markdown: str) -> tuple[str, int]:
    """Expand Obsidian ``[[wikilinks]]`` to plain text; report how many.

    Telegram has no wikilink syntax, so an unexpanded link reaches the reader
    as literal brackets around the vault's canonical note name — not the word
    the author wrote. Unlike :func:`strip_yaml_frontmatter` and
    :func:`scan_media`, which answer "this is a file from a vault" and are
    therefore CLI-only, a wikilink is meaningless in Telegram no matter which
    surface submitted it, so this runs for all three.

    Code is opaque: fenced blocks via :func:`scan_blocks`, inline spans via
    :data:`_CODE_SPAN_RE`. An article documenting this dialect writes
    ``[[Note]]`` inside backticks and means the characters.

    Returns ``(markdown, 0)`` by identity when nothing changed, which is what
    keeps CRLF and the trailing newline intact for a byte-for-byte send.
    """

    if "[[" not in markdown:
        return markdown, 0

    code_lines = _code_line_indices(scan_blocks(markdown))
    lines = split_lines(markdown)
    out: list[str] = []
    total = 0
    for index, line in enumerate(lines):
        if index in code_lines or "[[" not in line:
            out.append(line)
            continue
        rewritten, count = _strip_line_wikilinks(line)
        out.append(rewritten)
        total += count
    if not total:
        return markdown, 0
    text = "\n".join(out)
    return (text + "\n" if markdown.endswith(("\n", "\r")) else text), total


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
        # ``raw`` is the *stripped* line, not the match: the rewrite replaces it
        # inside the original line, so a quoted media line keeps its ``> ``.
        return _obsidian_ref(obsidian, raw=stripped)

    match = _MEDIA_MD_RE.match(stripped)
    if match is None:
        return None
    return _markdown_ref(match, raw=stripped)


def _obsidian_ref(
    match: re.Match[str], *, raw: str, span: tuple[int, int] | None = None
) -> MediaRef | None:
    parts = match.group("body").split("|")
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
        raw=raw,
        span=span,
    )


def _markdown_ref(
    match: re.Match[str], *, raw: str, span: tuple[int, int] | None = None
) -> MediaRef | None:
    target = (match.group("angle") if match.group("angle") is not None else match.group("plain")) or ""
    target = _unescape_markdown(target.strip())
    if not target:
        return None
    title = match.group("dquote")
    if title is None:
        title = match.group("squote")
    if title is None:
        title = match.group("pquote")
    if title is not None:
        title = _unescape_markdown(title)
    alt, size, alignment = _split_markdown_alt(match.group("alt") or "", target)
    return MediaRef(
        target=target,
        alt=alt,
        caption=title if title else alt,
        is_remote=_is_remote(target),
        obsidian=False,
        size=size,
        alignment=alignment,
        raw=raw,
        span=span,
    )


def iter_line_media_refs(line: str) -> Iterator[MediaRef]:
    """Yield every media reference *inside* ``line``, left to right.

    :func:`parse_media_line` answers "is this line one media reference?" — the
    question the block scanner asks. This one answers "what media does this
    line mention?", which is what resolution needs: an Obsidian embed in a
    bullet list, a thumbnail in a table cell, or a reference mid-sentence is
    just as much a local file to upload as one standing on its own line.

    ``MediaRef.raw`` is the exact matched substring here (not the stripped
    line) and ``MediaRef.span`` is where it sits, so the rewrite can splice it
    in place and leave the rest of the line — list marker, table pipes,
    surrounding prose — untouched. The span is what makes the masking below
    effective: searching for ``raw`` instead would rewrite a *skipped* copy of
    the same reference and leave the real one as a literal local path.

    A reference *inside* an inline code span is text, not media: the block
    scanner already treats fenced and indented code as opaque, and prose that
    documents the media syntax would otherwise either upload a file nobody
    asked for or fail the whole send on a path that was never meant to exist.
    The test is **containment**, not overlap: a code span sitting inside a
    reference's own caption (``![](a.png "run `x` first")``, ``![[a.png|`x`]]``)
    only *overlaps* it, and skipping on overlap would leave that reference
    unresolved and ship the local path verbatim — the one silent drop
    :func:`scan_media` promises never to make. A reference the mask keeps is
    still rewritten in place, so the surrounding backticks survive untouched.
    """

    if "![" not in line:
        return
    code_spans = [match.span() for match in _CODE_SPAN_RE.finditer(line)]
    matches: list[tuple[re.Match[str], MediaRef]] = []
    for pattern, build in (
        (_MEDIA_OBSIDIAN_INLINE_RE, _obsidian_ref),
        (_MEDIA_MD_INLINE_RE, _markdown_ref),
    ):
        for match in pattern.finditer(line):
            start, end = match.span()
            if any(span_start <= start and end <= span_end for span_start, span_end in code_spans):
                continue
            ref = build(match, raw=match.group(0), span=match.span())
            if ref is not None:
                matches.append((match, ref))
    # The two dialects cannot overlap in practice (an Obsidian body admits no
    # ``]``), but they are matched independently, so drop any overlap rather
    # than rewrite one reference twice.
    taken: list[tuple[int, int]] = []
    for match, ref in sorted(matches, key=lambda item: item[0].start()):
        start, end = match.span()
        if any(start < prev_end and prev_start < end for prev_start, prev_end in taken):
            continue
        taken.append((start, end))
        yield ref


def scan_blocks(markdown: str) -> tuple[Block, ...]:
    """Split ``markdown`` into typed top-level blocks.

    Blank lines are separators and belong to no block. Fenced and indented
    code is consumed opaquely — its contents are never inspected, so a ``#``,
    a ``|`` or a ``![](…)`` inside a fence never becomes a heading, a table or
    media. ``<details>``/``<tg-collage>``/``<tg-slideshow>`` bodies *are*
    scanned, and land in :attr:`Block.children`.

    Nesting is bounded by :data:`MAX_BLOCK_NESTING`: past that depth a quote or
    an HTML container is emitted as a **leaf** block (its body kept in
    :attr:`Block.lines`, no :attr:`Block.children`) instead of recursing. The
    scanner recurses once per level, so without the bound a single line of
    ``"> " * 600`` — 1.2 KB, far under ``MAX_RICH_MARKDOWN_CHARS`` — raises
    ``RecursionError``. That scan runs on the event loop *ahead* of the WRITE
    gate, so a token holder with no write grant could otherwise crash a send it
    was never authorized to make into an unmapped, empty 500.
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


def is_spacer_line(line: str) -> bool:
    """``True`` for a line whose only content is non-breaking whitespace."""

    return not line.strip() and NBSP in line


def is_spacer_block(block: Block) -> bool:
    """``True`` for a paragraph block that is nothing but spacer lines."""

    return (
        block.kind == "paragraph"
        and bool(block.lines)
        and all(is_spacer_line(line) for line in block.lines)
    )


def normalize_rich_markdown(
    markdown: str,
    *,
    spaced_paragraphs: bool = True,
    line_breaks: bool = True,
    grouping: str = DEFAULT_MEDIA_GROUP_MODE,
    media_groups: MediaGroupOverrides = None,
) -> RichMarkdownNormalization:
    """Group media runs, split soft breaks, insert paragraph spacers, report the budget.

    Telegram renders neighbouring ``PageBlockParagraph``s tight against each
    other, so the blank lines of the source are lost and a long article reads
    as a wall of text. A paragraph holding only :data:`SPACER_LINE` restores
    the gap; this inserts one between two consecutive plain paragraphs and
    before a heading of any level — never after a heading, never inside a
    code, table, list, quote or HTML block, and never next to a spacer the
    author already wrote (so normalising twice is a no-op).

    A run of two or more consecutive media blocks is wrapped in the container
    ``grouping`` names (:data:`MEDIA_GROUP_MODES`), so Telegram renders it as
    one collage/slideshow instead of a column of separate media. Media the
    author already put inside a ``<tg-collage>``/``<tg-slideshow>``/
    ``<details>`` block is left alone — that is an explicit decision, not a run
    to be re-grouped. ``media_groups`` overrides individual runs by index (see
    :attr:`RichMarkdownNormalization.groups`); an index that names no run
    raises :class:`MediaGroupError` rather than being silently dropped.

    ``line_breaks`` (see :func:`_split_paragraph_lines`) splits a top-level
    paragraph's own lines into one paragraph each, so the single newlines an
    Obsidian note writes survive instead of being folded into spaces. It runs
    **after** spacing precisely so the spacer pass never sees the pairs it
    produces: two lines the author wrote under one another belong together and
    must stay tight, while two paragraphs they separated with a blank line get
    the usual spacer.

    Cosmetics must not break a send: when spacing would push the article past
    :data:`MAX_RICH_BLOCKS`, the unspaced markdown is returned with a warning
    instead. Splitting is counted the same way — the blocks it adds are part of
    what the spacing decision weighs, so the two passes cannot together sneak
    the article over the limit that either alone respects.  An article that is
    *already* over the block or media limit is only warned about — the server
    decides.
    """

    # First, and before scan_blocks: this pass edits *inside* lines, so every
    # later pass — and the block count the 500-block rollback weighs — must see
    # the text that will actually be sent. It also settles a table cell whose
    # wikilink pipe would otherwise split it.
    markdown, wikilinks = strip_wikilinks(markdown)
    blocks = scan_blocks(markdown)
    warnings: list[str] = []

    grouped, groups = _apply_grouping(
        markdown, blocks, grouping=grouping, overrides=media_groups
    )
    if grouped is not markdown:
        blocks = scan_blocks(grouped)

    media = count_media(blocks)

    def _split(text: str, text_blocks: tuple[Block, ...]) -> str:
        return _split_paragraph_lines(text, text_blocks) if line_breaks else text

    # The unspaced article, already split: what a rolled-back spacing pass
    # falls back to, and the baseline its block count is compared against.
    unspaced = _split(grouped, blocks)
    unspaced_total = count_blocks(blocks if unspaced is grouped else scan_blocks(unspaced))

    result, total = unspaced, unspaced_total
    spaced = spacers_added = False

    if spaced_paragraphs:
        candidate = _insert_spacers(grouped, blocks)
        if candidate is grouped:
            # Nothing to space (one block, or an already-spaced article): the
            # split article *is* the spaced one.
            candidate_final, candidate_total = unspaced, unspaced_total
        else:
            candidate_blocks = scan_blocks(candidate)
            candidate_final = _split(candidate, candidate_blocks)
            candidate_total = count_blocks(
                candidate_blocks
                if candidate_final is candidate
                else scan_blocks(candidate_final)
            )
        if candidate_total > MAX_RICH_BLOCKS:
            warnings.append(
                f"spaced_paragraphs disabled: {candidate_total} blocks "
                f"would exceed the {MAX_RICH_BLOCKS}-block limit"
            )
        else:
            result = candidate_final
            total = candidate_total
            spaced = True
            spacers_added = candidate is not grouped

    if total > MAX_RICH_BLOCKS:
        warnings.append(
            f"article has {total} blocks, over Telegram's {MAX_RICH_BLOCKS}-block limit"
        )
    if media > MAX_RICH_MEDIA:
        warnings.append(
            f"article has {media} media attachments, over Telegram's {MAX_RICH_MEDIA} limit"
        )

    return RichMarkdownNormalization(
        markdown=result,
        blocks=total,
        media=media,
        spaced=spaced,
        warnings=tuple(warnings),
        groups=groups,
        grouped=grouped is not markdown,
        spacers_added=spacers_added,
        lines_split=unspaced is not grouped,
        wikilinks=wikilinks,
    )


def iter_media(blocks: tuple[Block, ...] | list[Block]) -> Iterator[Block]:
    """Yield every media block in document order, nested blocks included."""

    for block in blocks:
        if block.kind == "media":
            yield block
        if block.children:
            yield from iter_media(block.children)


def media_kind(path: Path | str) -> str:
    """Return ``photo``/``video``/``audio`` for a local file, by suffix.

    Raises :class:`MediaResolutionError` for anything else: a rich message has
    exactly these three media schemes, so a ``.pdf`` has no reference syntax to
    be written into and must be reported rather than silently dropped.
    """

    suffix = Path(path).suffix.lower()
    if suffix in PHOTO_SUFFIXES:
        return "photo"
    if suffix in AUDIO_SUFFIXES:
        return "audio"
    if suffix in VIDEO_SUFFIXES:
        return "video"
    raise MediaResolutionError(
        f"unsupported media type for rich message: {path} "
        f"(a rich message carries photo, video or audio only)"
    )


def rich_file_reference(file_id: str, kind: str) -> str:
    """Return the markdown target naming ``file_id`` as media of ``kind``."""

    try:
        scheme = RICH_FILE_SCHEMES[kind]
    except KeyError:
        raise MediaResolutionError(
            f"unknown media kind {kind!r} (expected one of "
            f"{', '.join(sorted(RICH_FILE_SCHEMES))})"
        ) from None
    return f"{scheme}{file_id}"


def make_rich_file_id(name: str, taken: Iterable[str] = ()) -> str:
    """Derive a server-acceptable file id from a file name.

    The id is written straight into ``![](tg://photo?id=…)``, so a Cyrillic or
    bracketed file name must not leak into it: anything outside
    :data:`RICH_FILE_ID_RE` becomes ``-``, an empty result becomes ``file``, and
    a collision with ``taken`` gets a ``-2``, ``-3``… suffix.
    """

    used = set(taken)
    slug = re.sub(r"[^A-Za-z0-9_-]+", "-", name).strip("-")
    slug = (slug or "file")[:MAX_RICH_FILE_ID_CHARS]
    if slug not in used:
        return slug
    counter = 2
    while True:
        suffix = f"-{counter}"
        candidate = slug[: MAX_RICH_FILE_ID_CHARS - len(suffix)] + suffix
        if candidate not in used:
            return candidate
        counter += 1


def scan_media(
    markdown: str,
    *,
    base_dir: Path | str,
    vault_dir: Path | str | None = None,
    overrides: Mapping[str, Path | str] | None = None,
) -> MediaScan:
    """Resolve local media in ``markdown`` and rewrite it to ``tg://`` references.

    An ``http``/``https`` target is left exactly as written — the server fetches
    it. Everything else names a local file and is resolved in this order: an
    ``overrides`` entry (keyed by the target as written, its URL-decoded form or
    its bare file name), then the path relative to ``base_dir`` (the directory
    of the markdown file), then a by-name search of ``vault_dir`` for Obsidian
    ``![[…]]`` embeds, which carry a file name rather than a path.

    The search picks the match *nearest* ``base_dir``; a tie raises
    :class:`AmbiguousMediaError` rather than guessing, and a target that
    resolves to nothing raises :class:`MediaResolutionError` naming it. An
    override that matched nothing is an error too — a silently ignored
    ``--rich-file`` would send the article without the file it was given.

    The same file referenced twice is uploaded once (one :class:`RichFile`, one
    id); the caption stays per-reference, since it lives in the markdown.

    Every media line is resolved, whether it stands as its own block or opens a
    paragraph because prose follows it on the next line (the common Obsidian
    "embed then caption line" shape) — see :func:`_iter_media_refs`. Media
    inside fenced or indented code is not a reference and is left alone.
    """

    # ``resolve()``: the nearest-match search below compares candidate paths
    # against ``base`` step by step, and the candidates are absolute. A relative
    # ``base_dir`` (``Path("note.md").parent`` is ``Path(".")``, whose ``parts``
    # is empty) would turn that distance into the candidate's absolute depth and
    # pick a file from the wrong directory.
    base = Path(base_dir).expanduser().resolve()
    vault = Path(vault_dir).expanduser().resolve() if vault_dir is not None else None
    override_map = {
        str(key): Path(value).expanduser() for key, value in (overrides or {}).items()
    }
    used_overrides: set[str] = set()

    lines = split_lines(markdown)
    blocks = scan_blocks(markdown)
    refs = list(_iter_media_refs(blocks, lines))
    if not refs and not override_map:
        return MediaScan(markdown=markdown)

    files: list[RichFile] = []
    by_path: dict[str, RichFile] = {}
    taken: set[str] = set()
    # line index -> (start, end, replacement), spliced right-to-left below so
    # earlier spans keep their offsets. ``iter_line_media_refs`` already drops
    # overlapping matches, so no two entries on a line can collide.
    rewrites: dict[int, list[tuple[int, int, str]]] = {}

    for line_index, ref in refs:
        if ref.is_remote:
            continue
        path = _resolve_local_media(
            ref, base=base, vault=vault, overrides=override_map, used=used_overrides
        )
        kind = media_kind(path)
        key = str(path)
        rich_file = by_path.get(key)
        if rich_file is None:
            file_id = make_rich_file_id(path.stem, taken)
            taken.add(file_id)
            rich_file = RichFile(id=file_id, path=key, caption=ref.caption, kind=kind)
            by_path[key] = rich_file
            files.append(rich_file)
        start, end = ref.span if ref.span is not None else (0, len(lines[line_index]))
        rewrites.setdefault(line_index, []).append(
            (start, end, _media_markdown(rich_file.id, kind, alt=ref.alt, caption=ref.caption))
        )

    unused = sorted(set(override_map) - used_overrides)
    if unused:
        raise MediaResolutionError(
            "rich file override(s) match no media in the article: " + ", ".join(unused)
        )

    if not rewrites:
        # Identity, so a media-less (or fully remote) article keeps its exact
        # bytes — line endings and trailing newline included.
        return MediaScan(markdown=markdown)

    for index, spans in rewrites.items():
        line = lines[index]
        for start, end, replacement in sorted(spans, reverse=True):
            line = line[:start] + replacement + line[end:]
        lines[index] = line
    text = "\n".join(lines)
    if markdown.endswith(("\n", "\r")):
        text += "\n"
    return MediaScan(markdown=text, files=tuple(files))


# --- internals ---------------------------------------------------------------


def _iter_media_refs(
    blocks: tuple[Block, ...] | list[Block],
    lines: list[str],
) -> Iterator[tuple[int, MediaRef]]:
    """Yield ``(document line index, reference)`` for every media reference.

    Media standing as its own block is the common case, but a reference is a
    local file to upload wherever it sits: a media line directly followed by
    prose is deliberately *not* a media block (see :func:`_media_block_at`) —
    it opens a paragraph — and an Obsidian embed just as happily lives in a
    bullet list, a table cell, a footnote, or mid-sentence. So every line of
    every block is swept with :func:`iter_line_media_refs`, not just the lines
    that *are* a reference: leaving one as written would send a local path (or
    a literal ``![[…]]``) to Telegram, the one silent drop
    :func:`scan_media` promises never to make.

    Code blocks are opaque, so a ``![](shot.png)`` inside a fence stays text.
    A block with children (a quote, ``<tg-collage>``) is covered by those
    children — whose ``start`` is document-absolute and whose lines are the
    de-prefixed body — so a line a child already owns is not swept a second
    time. The lines a child does *not* own still are: an HTML container's
    children are only its body (``_consume_html`` scans between the tags), so
    ``<details><summary>![](a.png)</summary>`` would otherwise send its literal
    local path — the one silent drop :func:`scan_media` promises never to make.

    The sweep reads ``lines`` — the *document* lines — rather than
    ``Block.lines``, which for a quote child are the de-prefixed body: only the
    document line makes ``MediaRef.span`` an offset the rewrite can splice at
    directly, with the ``> `` marker left where it was. A media block is swept
    like any other line for the same reason; ``Block.media`` comes from
    :func:`parse_media_line`, which reports the *stripped* line and so carries
    no usable span.
    """

    for block in blocks:
        if block.children:
            covered = {
                index for child in block.children for index in range(child.start, child.end)
            }
            for document_index in range(block.start, block.end):
                if document_index in covered:
                    continue
                for ref in iter_line_media_refs(lines[document_index]):
                    yield document_index, ref
            yield from _iter_media_refs(block.children, lines)
            continue
        if block.kind == "code":
            continue
        for document_index in range(block.start, block.end):
            for ref in iter_line_media_refs(lines[document_index]):
                yield document_index, ref


def _is_blank(line: str) -> bool:
    """A block separator: whitespace only, and *not* a spacer line.

    A lone U+00A0 is a paragraph to Telegram, so it must be a block here too —
    otherwise re-normalising an already-spaced article would not see its own
    spacers and would double every one of them.
    """

    return not line.strip() and NBSP not in line


def _is_media_like(block: Block) -> bool:
    """``True`` for a medium, or for the container a media run was grouped into.

    A ``<tg-collage>``/``<tg-slideshow>`` *is* the media as far as the article's
    vertical rhythm goes, so it earns the same trailing spacer a lone photo
    does. ``<details>`` does not — it is a spoiler container that happens to be
    spelled with a tag.
    """

    return block.kind == "media" or (
        block.kind == "html" and block.html_tag in set(MEDIA_GROUP_TAGS.values())
    )


def _needs_spacer_before(previous: Block, block: Block) -> bool:
    if previous.kind == "heading":
        # A heading already sits tight against what follows it by design.
        return False
    if is_spacer_block(previous) or is_spacer_block(block):
        return False
    if _is_media_like(previous):
        # Telegram renders whatever follows a photo/collage hard against it, so
        # every medium gets a spacer *after* it — before text, before another
        # medium, before anything. Nothing is inserted *before* media: an
        # embed's own lead-in line belongs with it.
        return True
    if block.kind == "heading":
        return True
    return previous.kind == "paragraph" and block.kind == "paragraph"


def _insert_spacers(markdown: str, blocks: tuple[Block, ...]) -> str:
    """Return ``markdown`` with spacer paragraphs inserted between top-level blocks.

    The original string is returned unchanged (identity) when nothing needs
    inserting, so an already-spaced document keeps its exact bytes — including
    its line endings — and normalisation is idempotent.
    """

    points = [
        index
        for index in range(1, len(blocks))
        if _needs_spacer_before(blocks[index - 1], blocks[index])
    ]
    if not points:
        return markdown

    lines = split_lines(markdown)
    out: list[str] = []
    cursor = 0
    for index in points:
        start = blocks[index].start
        out.extend(lines[cursor:start])
        if out and not _is_blank(out[-1]):
            # The blocks touched (``para`` directly above ``# Heading``); the
            # spacer needs a blank line on each side to be its own block.
            out.append("")
        out.append(SPACER_LINE)
        out.append("")
        cursor = start
    out.extend(lines[cursor:])
    text = "\n".join(out)
    return text + "\n" if markdown.endswith(("\n", "\r")) else text


def _split_paragraph_lines(markdown: str, blocks: tuple[Block, ...]) -> str:
    """Return ``markdown`` with each top-level paragraph's *own* lines split apart.

    Telegram parses the markdown server-side and, like CommonMark, folds a
    single newline inside a paragraph into a space, so an Obsidian note's::

        Фотоальбом - https://…
        Видео плейлист - https://…

    arrives as one run-on line. The spacer pass cannot help: it inserts blocks
    *between* blocks, and those two lines are one ``PageBlockParagraph``.

    A blank line is inserted between them instead, so each source line becomes
    its own paragraph — which the clients render tight against each other,
    exactly the "two lines, no gap" the author wrote. ``scripts/
    spike_rich_line_breaks.py`` proved that a real in-paragraph hard break
    (two trailing spaces, a trailing ``\\``, ``<br>``) is *also* accepted and
    keeps the pair in one block; the split is emitted anyway because that is
    what reads better in the clients. The cost of that choice is idempotency:
    a split pair is indistinguishable from two paragraphs the author wrote, so
    re-normalising this pass's own output would let the spacer pass push them
    apart. Nothing in a send does that — ``send_message`` normalises once, from
    the source the author supplied.

    Only **top-level paragraphs** are split, mirroring the spacer and grouping
    passes: lines inside a quote, a list item or an HTML container keep their
    author-written shape. The original string is returned by identity when no
    paragraph has a second line, so byte fidelity survives.
    """

    points = [
        index
        for block in blocks
        if block.kind == "paragraph" and len(block.lines) > 1 and not is_spacer_block(block)
        for index in range(block.start + 1, block.end)
    ]
    if not points:
        return markdown

    lines = split_lines(markdown)
    out: list[str] = []
    cursor = 0
    for index in points:
        out.extend(lines[cursor:index])
        out.append("")
        cursor = index
    out.extend(lines[cursor:])
    text = "\n".join(out)
    return text + "\n" if markdown.endswith(("\n", "\r")) else text


def _validate_mode(mode: str) -> str:
    if mode not in MEDIA_GROUP_MODES:
        raise MediaGroupError(
            f"unknown media grouping {mode!r} (expected one of "
            f"{', '.join(MEDIA_GROUP_MODES)})"
        )
    return mode


def _detect_media_runs(blocks: tuple[Block, ...]) -> list[list[int]]:
    """Find runs of 2+ consecutive **top-level** media blocks.

    Only the top level: media the author already placed inside a
    ``<tg-collage>``/``<tg-slideshow>``/``<details>`` (or a quote) lives in
    :attr:`Block.children` and is never re-grouped — the author already said
    how it should render.
    """

    runs: list[list[int]] = []
    current: list[int] = []
    for index, block in enumerate(blocks):
        if block.kind == "media":
            current.append(index)
            continue
        if len(current) > 1:
            runs.append(current)
        current = []
    if len(current) > 1:
        runs.append(current)
    return runs


def _preceding_text(blocks: tuple[Block, ...], run_start: int) -> str:
    """The tail of the last text block above a media run, whitespace-collapsed."""

    for index in range(run_start - 1, -1, -1):
        block = blocks[index]
        if block.kind == "media" or is_spacer_block(block):
            continue
        text = " ".join(block.text.split())
        if not text:
            continue
        if len(text) <= MEDIA_GROUP_CONTEXT_CHARS:
            return text
        return "…" + text[-MEDIA_GROUP_CONTEXT_CHARS:]
    return ""


def _group_caption(blocks: tuple[Block, ...], run: list[int]) -> str:
    """The caption a wrapped run should carry: its members' captions, joined.

    Telegram's clients show **no** caption under an individual medium inside a
    collage or slideshow — only the group's own ``PageBlockCollage.caption`` —
    so the captions the author wrote per image go unseen the moment the pass
    groups them. (They do survive on the wire: a read-back of a grouped article
    has ``PageBlockPhoto.caption`` populated inside the collage. It is the
    rendering that ignores them, which is why the group needs its own.) Members
    without a caption contribute nothing — a run where none has one gets no
    caption at all and is wrapped byte-identically to what it was before this
    existed; duplicates are kept, the text being the author's.
    """

    captions = [
        block.media.caption.strip()
        for index in run
        if (block := blocks[index]).media is not None and block.media.caption.strip()
    ]
    return ", ".join(captions)


def _coerce_overrides(overrides: MediaGroupOverrides, count: int) -> dict[int, str]:
    """Normalise every accepted override shape into ``{index: mode}``.

    An index naming no run is an error: a silently ignored override would send
    an article grouped differently from what the operator asked for — the same
    "no silent drop" rule local media resolution follows. Naming one index
    twice is the same class of mistake and is rejected for the same reason:
    last-win would quietly drop the first mode, so the operator would be told
    nothing while the article went out grouped the other way.
    """

    if overrides is None:
        return {}
    if isinstance(overrides, Mapping):
        pairs: list[tuple[Any, Any]] = list(overrides.items())
    else:
        pairs = [(entry.index, entry.mode) for entry in overrides]

    resolved: dict[int, str] = {}
    for raw_index, raw_mode in pairs:
        try:
            index = int(raw_index)
        except (TypeError, ValueError):
            raise MediaGroupError(
                f"media group index must be an integer ({raw_index!r} given)"
            ) from None
        if not 0 <= index < count:
            found = (
                f"the article has {count} media group(s)"
                if count
                else "the article has no media groups"
            )
            raise MediaGroupError(
                f"unknown media group index {index}: {found}"
            )
        mode = _validate_mode(str(raw_mode))
        previous = resolved.get(index)
        if previous is not None and previous != mode:
            raise MediaGroupError(
                f"media group index {index} given twice with different modes "
                f"({previous!r} and {mode!r})"
            )
        resolved[index] = mode
    return resolved


def _apply_grouping(
    markdown: str,
    blocks: tuple[Block, ...],
    *,
    grouping: str,
    overrides: MediaGroupOverrides,
) -> tuple[str, tuple[MediaGroup, ...]]:
    """Wrap media runs per ``grouping``/``overrides`` and describe every run.

    The markdown is returned by identity when no run is wrapped, so a
    media-less article (or one grouped ``none`` throughout) keeps its exact
    bytes.
    """

    default_mode = _validate_mode(grouping)
    runs = _detect_media_runs(blocks)
    resolved = _coerce_overrides(overrides, len(runs))
    groups = tuple(
        MediaGroup(
            index=index,
            size=len(run),
            preceding_text=_preceding_text(blocks, run[0]),
            mode=resolved.get(index, default_mode),
            caption=_group_caption(blocks, run),
        )
        for index, run in enumerate(runs)
    )
    wrap = [(group, runs[group.index]) for group in groups if group.mode in MEDIA_GROUP_TAGS]
    if not wrap:
        return markdown, groups
    return _wrap_media_runs(markdown, blocks, wrap), groups


def _wrap_media_runs(
    markdown: str,
    blocks: tuple[Block, ...],
    wrap: list[tuple[MediaGroup, list[int]]],
) -> str:
    """Rewrite ``markdown`` with each selected run inside its container tag.

    The dialect wants the media blank-line separated *inside* the tag, so the
    run's own line range is rebuilt: the media lines survive verbatim, the
    whitespace between them does not. A run whose media carry captions also gets
    a :data:`FIGCAPTION_TAG` line as the container's last block — the group
    caption, which is the only caption Telegram shows for grouped media.
    """

    lines = split_lines(markdown)
    out: list[str] = []
    cursor = 0
    for group, run in wrap:
        first = blocks[run[0]]
        last = blocks[run[-1]]
        out.extend(lines[cursor:first.start])
        if out and not _is_blank(out[-1]):
            out.append("")
        tag = MEDIA_GROUP_TAGS[group.mode]
        out.append(f"<{tag}>")
        for block_index in run:
            out.append("")
            out.extend(blocks[block_index].lines)
        if group.caption:
            out.append("")
            out.append(f"<{FIGCAPTION_TAG}>{group.caption}</{FIGCAPTION_TAG}>")
        out.append("")
        out.append(f"</{tag}>")
        cursor = last.end
        if cursor < len(lines) and not _is_blank(lines[cursor]):
            out.append("")
    out.extend(lines[cursor:])
    text = "\n".join(out)
    return text + "\n" if markdown.endswith(("\n", "\r")) else text


def _is_remote(target: str) -> bool:
    return target.lower().startswith(("http://", "https://"))


def _unescape_markdown(value: str) -> str:
    """Undo CommonMark backslash escapes in a media target or title.

    The pattern accepts ``\\(``/``\\"`` so an escaped reference is recognised at
    all; the file to open and the caption to render are the *unescaped* text.
    """

    if "\\" not in value:
        return value
    return _MD_ESCAPE_RE.sub(r"\1", value)


def _quote_title(caption: str) -> str:
    """Render ``caption`` as a markdown media title, quoted so it round-trips.

    Single quotes when that alone keeps the caption bare, double quotes
    otherwise, and whatever the delimiter cannot hold is written with a
    CommonMark backslash escape — the same dialect :data:`_MEDIA_MD_PATTERN`
    accepts and :func:`_unescape_markdown` undoes on input, so the article reads
    back as the caption the author wrote. Rewriting the quotes instead (``"`` →
    ``'``) would send a caption nobody wrote, which is the one thing the media
    rewrite must never do. Backslashes are escaped first and unconditionally: a
    caption ending in one would otherwise escape its own closing delimiter and
    swallow the rest of the reference.
    """

    if not caption:
        return ""
    quote = "'" if '"' in caption and "'" not in caption else '"'
    escaped = caption.replace("\\", "\\\\").replace(quote, f"\\{quote}")
    return f" {quote}{escaped}{quote}"


def _media_markdown(file_id: str, kind: str, *, alt: str, caption: str) -> str:
    """Build the media line naming ``file_id``, keeping alt text and caption."""

    return f"![{alt}]({rich_file_reference(file_id, kind)}{_quote_title(caption)})"


def _distance(candidate: Path, base: Path) -> int:
    """How far ``candidate``'s directory is from ``base``, in path steps."""

    parent = candidate.parent.parts
    root = base.parts
    common = 0
    for left, right in zip(parent, root, strict=False):
        if left != right:
            break
        common += 1
    return (len(parent) - common) + (len(root) - common)


def _search_by_name(root: Path, name: str, suffix_parts: tuple[str, ...]) -> list[Path]:
    """Find files called ``name`` under ``root``.

    ``os.walk`` rather than ``rglob``: an Obsidian file name may contain ``[``
    or ``*``, which a glob would interpret instead of matching. ``suffix_parts``
    lets an embed that carries a partial path (``notes/img.png``) require those
    trailing directories.
    """

    if not root.is_dir():
        return []
    matches: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        if name not in filenames:
            continue
        found = Path(dirpath) / name
        if suffix_parts and found.parts[-len(suffix_parts) :] != suffix_parts:
            continue
        matches.append(found)
    return matches


def _resolve_local_media(
    ref: MediaRef,
    *,
    base: Path,
    vault: Path | None,
    overrides: dict[str, Path],
    used: set[str],
) -> Path:
    decoded = unquote(ref.target)
    name = Path(decoded.replace("\\", "/")).name
    for key in (ref.target, decoded, name):
        if key in overrides:
            used.add(key)
            path = overrides[key]
            if not path.is_file():
                raise MediaResolutionError(
                    f"rich file override {key!r} points at a missing file: {path}"
                )
            return path.resolve()

    candidate = Path(decoded).expanduser()
    if candidate.is_absolute():
        if candidate.is_file():
            return candidate.resolve()
        raise MediaResolutionError(f"media file not found: {candidate}")

    direct = base / candidate
    if direct.is_file():
        return direct.resolve()

    search_root = vault if vault is not None else base
    suffix_parts = tuple(p for p in candidate.parts[:-1] if p not in ("", "."))
    matches = _search_by_name(search_root, candidate.name, suffix_parts + (candidate.name,))
    if not matches:
        where = f" (searched {search_root})" if search_root != base else ""
        raise MediaResolutionError(
            f"media file not found: {ref.target}{where}; pass --rich-file "
            f"{candidate.name}=<path> or --vault-dir"
        )
    resolved = [match.resolve() for match in matches]
    nearest = min(_distance(match, base) for match in resolved)
    closest = sorted({match for match in resolved if _distance(match, base) == nearest})
    if len(closest) > 1:
        listing = ", ".join(str(match) for match in closest)
        raise AmbiguousMediaError(
            f"media reference {ref.target!r} matches {len(closest)} files: {listing}; "
            f"pass --rich-file {candidate.name}=<path> to choose one"
        )
    return closest[0]


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


def _interrupts_paragraph(blocks: list[Block], absolute_index: int) -> bool:
    """True when the line at ``absolute_index`` continues the paragraph above it.

    Indented code cannot interrupt a paragraph — an indented line right under
    prose is continuation text, not a code block. Reading it as code would make
    the scanner opaque to whatever the line holds, and an indented media
    reference would then be shipped as a literal local path instead of being
    resolved and uploaded. A spacer paragraph is not prose, so it never absorbs
    the line below it.

    A ``media`` block counts as a paragraph here: it *is* one — a paragraph
    whose single line happens to be nothing but a media reference — so the line
    under it is continuation text for the same reason. Without that, the
    ``![](a.png)`` / indented ``![](b.png)`` pair (an Obsidian embed followed by
    an indented one) would read as media-then-code and ship ``b.png`` as a
    literal local path, the one silent drop ``scan_media`` promises never to
    make.
    """

    if not blocks:
        return False
    previous = blocks[-1]
    if previous.kind not in ("paragraph", "media") or previous.end != absolute_index:
        return False
    return not is_spacer_line(previous.lines[-1])


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
        or is_spacer_line(line)
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
    if _is_blank(following) or parse_media_line(following) is not None:
        return media
    return media if _starts_new_block(lines, nxt) else None


def _scan_nested(lines: list[str], offset: int, depth: int) -> tuple[Block, ...]:
    """Scan a container's body one level deeper, or stop at the nesting bound.

    Returning ``()`` makes the caller emit a leaf block: its ``lines`` still
    carry the whole body verbatim (nothing is dropped from the send), it just
    weighs 1 and is opaque to the spacing/grouping passes — which is the right
    answer at 64 levels deep, where the 500-block budget is long spent anyway.
    """

    if depth >= MAX_BLOCK_NESTING:
        return ()
    return _scan(lines, offset, depth + 1)


def _scan(lines: list[str], offset: int, depth: int = 0) -> tuple[Block, ...]:
    blocks: list[Block] = []
    index = 0
    total = len(lines)
    while index < total:
        line = lines[index]
        if _is_blank(line):
            index += 1
            continue
        if is_spacer_line(line):
            # Its own paragraph block, however it is surrounded — that is the
            # whole point of the spacer, and it keeps normalisation idempotent.
            blocks.append(Block("paragraph", (line,), offset + index, offset + index + 1))
            index += 1
            continue
        if _is_indented_code(line) and not _interrupts_paragraph(blocks, offset + index):
            index = _consume_indented_code(lines, index, offset, blocks)
            continue
        if _fence_open(line):
            index = _consume_fence(lines, index, offset, blocks)
            continue
        tag = _html_open(line)
        if tag:
            index = _consume_html(lines, index, offset, blocks, tag, depth)
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
            index = _consume_quote(lines, index, offset, blocks, depth)
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
        if _is_blank(line):
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
    lines: list[str], index: int, offset: int, blocks: list[Block], tag: str, depth: int = 0
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
    children = _scan_nested(lines[inner_start:inner_end], offset + inner_start, depth)
    blocks.append(
        Block(
            "html",
            tuple(lines[index:end]),
            offset + index,
            offset + end,
            # A ``<figcaption>`` is this container's caption field, not a block
            # of its own: counting it would over-report the block budget by one
            # per captioned collage and could roll back the spacer pass for
            # blocks Telegram never charges.
            weight=1 + sum(0 if _is_figcaption(child) else child.weight for child in children),
            children=children,
            html_tag=tag,
        )
    )
    return end


def _is_figcaption(block: Block) -> bool:
    return len(block.lines) == 1 and bool(_FIGCAPTION_RE.match(block.lines[0]))


def _consume_table(lines: list[str], index: int, offset: int, blocks: list[Block]) -> int:
    end = index
    rows = 0
    while end < len(lines):
        line = lines[end]
        if _is_blank(line) or "|" not in line:
            break
        if not _TABLE_DELIM_RE.match(line):
            rows += 1
        end += 1
    blocks.append(
        Block("table", tuple(lines[index:end]), offset + index, offset + end, weight=1 + rows)
    )
    return end


def _consume_quote(
    lines: list[str], index: int, offset: int, blocks: list[Block], depth: int = 0
) -> int:
    end = index
    while end < len(lines) and _QUOTE_RE.match(lines[end]):
        end += 1
    inner = [re.sub(r"^ {0,3}> ?", "", line) for line in lines[index:end]]
    children = _scan_nested(inner, offset + index, depth)
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
        if _is_blank(line):
            look = end
            while look < total and _is_blank(lines[look]):
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
        if _is_blank(line):
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
