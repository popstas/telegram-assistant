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

:func:`normalize_rich_markdown` is the writing half: it inserts
:data:`SPACER_LINE` (a lone U+00A0) as its own block where Telegram would
otherwise render two paragraphs tight against each other. A line holding only a
non-breaking space is *not* blank to this scanner — the server parses it as a
``PageBlockParagraph``, which is exactly why it works as a spacer, and treating
it as a real block is what makes normalisation idempotent.

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
from urllib.parse import unquote

#: Telegram's ceiling on blocks in one rich message (nested blocks included).
MAX_RICH_BLOCKS = 500

#: Telegram's ceiling on media attachments in one rich message.
MAX_RICH_MEDIA = 50

#: HTML container tags the dialect defines; their contents are scanned as
#: nested blocks so grouping can tell author-written groups from runs it may
#: wrap itself.
HTML_BLOCK_TAGS = ("details", "tg-collage", "tg-slideshow")

#: The non-breaking space. A line holding only this renders as an empty
#: paragraph block, which is how vertical space is added to an article.
NBSP = "\u00a0"

#: The spacer :func:`normalize_rich_markdown` inserts, alone on its own line.
SPACER_LINE = NBSP

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
class RichMarkdownNormalization:
    """What :func:`normalize_rich_markdown` decided about one article.

    ``markdown`` is what should actually go to Telegram, ``blocks``/``media``
    are the approximate counts of that text, ``spaced`` says whether the spacer
    pass ran (it is ``False`` when disabled *or* rolled back), and ``warnings``
    are operator-facing strings — normalisation never raises: Telegram is the
    authority on what it accepts.
    """

    markdown: str
    blocks: int
    media: int
    spaced: bool
    warnings: tuple[str, ...] = ()


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
    fidelity survives), ``files`` are the uploads it needs in markdown order,
    and ``remote`` lists the http(s) targets left untouched for the server to
    fetch itself.
    """

    markdown: str
    files: tuple[RichFile, ...] = ()
    remote: tuple[str, ...] = ()


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
    markdown: str, *, spaced_paragraphs: bool = True
) -> RichMarkdownNormalization:
    """Insert paragraph spacers and report the article's block/media budget.

    Telegram renders neighbouring ``PageBlockParagraph``s tight against each
    other, so the blank lines of the source are lost and a long article reads
    as a wall of text. A paragraph holding only :data:`SPACER_LINE` restores
    the gap; this inserts one between two consecutive plain paragraphs and
    before a heading of any level — never after a heading, never inside a
    code, table, list, quote or HTML block, and never next to a spacer the
    author already wrote (so normalising twice is a no-op).

    Cosmetics must not break a send: when spacing would push the article past
    :data:`MAX_RICH_BLOCKS`, the unspaced markdown is returned with a warning
    instead. An article that is *already* over the block or media limit is
    also only warned about — the server decides.
    """

    blocks = scan_blocks(markdown)
    media = count_media(blocks)
    warnings: list[str] = []

    result = markdown
    total = count_blocks(blocks)
    spaced = False

    if spaced_paragraphs:
        candidate = _insert_spacers(markdown, blocks)
        candidate_total = total if candidate is markdown else count_blocks(scan_blocks(candidate))
        if candidate_total > MAX_RICH_BLOCKS:
            warnings.append(
                f"spaced_paragraphs disabled: {candidate_total} blocks "
                f"would exceed the {MAX_RICH_BLOCKS}-block limit"
            )
        else:
            result = candidate
            total = candidate_total
            spaced = True

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
    """

    base = Path(base_dir).expanduser()
    vault = Path(vault_dir).expanduser() if vault_dir is not None else None
    override_map = {
        str(key): Path(value).expanduser() for key, value in (overrides or {}).items()
    }
    used_overrides: set[str] = set()

    blocks = scan_blocks(markdown)
    media_blocks = [block for block in iter_media(blocks) if block.media is not None]
    if not media_blocks and not override_map:
        return MediaScan(markdown=markdown)

    lines = split_lines(markdown)
    files: list[RichFile] = []
    remote: list[str] = []
    by_path: dict[str, RichFile] = {}
    taken: set[str] = set()
    rewritten: dict[int, str] = {}

    for block in media_blocks:
        ref = block.media
        assert ref is not None  # filtered above
        if ref.is_remote:
            remote.append(ref.target)
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
        original = lines[block.start]
        rewritten[block.start] = original.replace(
            ref.raw, _media_markdown(rich_file.id, kind, alt=ref.alt, caption=ref.caption), 1
        )

    unused = sorted(set(override_map) - used_overrides)
    if unused:
        raise MediaResolutionError(
            "rich file override(s) match no media in the article: " + ", ".join(unused)
        )

    if not rewritten:
        # Identity, so a media-less (or fully remote) article keeps its exact
        # bytes — line endings and trailing newline included.
        return MediaScan(markdown=markdown, files=(), remote=tuple(remote))

    for index, line in rewritten.items():
        lines[index] = line
    text = "\n".join(lines)
    if markdown.endswith(("\n", "\r")):
        text += "\n"
    return MediaScan(markdown=text, files=tuple(files), remote=tuple(remote))


# --- internals ---------------------------------------------------------------


def _is_blank(line: str) -> bool:
    """A block separator: whitespace only, and *not* a spacer line.

    A lone U+00A0 is a paragraph to Telegram, so it must be a block here too —
    otherwise re-normalising an already-spaced article would not see its own
    spacers and would double every one of them.
    """

    return not line.strip() and NBSP not in line


def _needs_spacer_before(previous: Block, block: Block) -> bool:
    if previous.kind == "heading":
        # A heading already sits tight against what follows it by design.
        return False
    if is_spacer_block(previous) or is_spacer_block(block):
        return False
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


def _is_remote(target: str) -> bool:
    return target.lower().startswith(("http://", "https://"))


def _quote_title(caption: str) -> str:
    """Render ``caption`` as a markdown media title, quoted so it round-trips."""

    if not caption:
        return ""
    if '"' not in caption:
        return f' "{caption}"'
    if "'" not in caption:
        return f" '{caption}'"
    return ' "' + caption.replace('"', "'") + '"'


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


def _scan(lines: list[str], offset: int) -> tuple[Block, ...]:
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
        if _is_blank(line) or "|" not in line:
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
