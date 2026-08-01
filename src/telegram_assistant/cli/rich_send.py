"""Rich-message input handling for ``telegram-assistant messages send``.

Everything the ``--rich-markdown`` family of flags needs before the send is
handed to the domain layer lives here, so the command body stays about routing
a send rather than about parsing an article:

* :func:`resolve_rich_send_input` — the flag exclusivity rules, the file read,
  ``--rich-file``/``--media-group`` parsing, local-media resolution and the
  length bound. It owns its own ``typer.Exit(code=2)`` calls, so every rejection
  reads as a caller-input error exactly as it did inline.
* :func:`rich_dry_run_markers` — the ``rich_*`` half of the ``--dry-run``
  payload, including the post-normalization counts a real send would produce.

Both are CLI-only: local media in an article is CLI-only (the CLI is the
trusted, local surface), and HTTP/MCP take a markdown string an agent composed
rather than a note on disk.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import typer

from telegram_assistant.messages import (
    MAX_RICH_MARKDOWN_CHARS,
    MEDIA_GROUP_MODES,
    MediaGroupChoice,
    MediaGroupError,
    MediaResolutionError,
    RichFile,
    RichMarkdownNormalization,
    normalize_rich_markdown,
    scan_media,
    strip_yaml_frontmatter,
)


@dataclass(frozen=True)
class RichSendInput:
    """A validated ``--rich-markdown`` send, ready to hand to the domain layer.

    ``markdown`` is the article body with every *local* media reference already
    rewritten to its ``tg://`` form, ``files`` the uploads those references
    resolved to (markdown order, deduplicated by path), and ``choices`` the
    per-run ``--media-group`` overrides. ``None`` in place of this object means
    the send is a plain one.
    """

    markdown: str
    files: tuple[RichFile, ...] = ()
    choices: tuple[MediaGroupChoice, ...] = ()


@dataclass(frozen=True)
class RichDryRun:
    """The rich half of a targeted ``--dry-run`` report.

    ``markers`` are the ``rich_*`` keys merged into the resolved payload (all
    present for a plain send too, so the shape never depends on the mode),
    ``what`` names the payload in the human-facing ``would`` line, and
    ``warnings`` are the normalization's own notes plus the over-limit note the
    real send would fail on.
    """

    markers: dict[str, object]
    what: str
    warnings: list[str]


def resolve_rich_send_input(
    *,
    rich_markdown: Path | None,
    rich_file: list[str] | None,
    media_group: list[str] | None,
    vault_dir: Path | None,
    spaced_paragraphs: bool | None,
    line_breaks: bool | None,
    text: str,
    has_attachments: bool,
    is_mass: bool,
) -> RichSendInput | None:
    """Validate and load the ``--rich-markdown`` input, or ``None`` if plain.

    A rich message carries its whole body in the markdown source: a caption or
    attachment alongside it would be silently dropped by the server, and mass
    mode has no single-article semantics (same rules as
    ``MessageSendBody._shape``). Read and bound the file here so bad input costs
    no backend connection, in dry-run and real runs alike.
    """
    rich_file_args = list(rich_file or ())
    media_group_args = list(media_group or ())
    if rich_markdown is None and (rich_file_args or vault_dir is not None):
        # Both only steer how local media in the article is resolved; accepting
        # them for a plain send would silently do nothing.
        typer.echo(
            "--rich-file/--vault-dir are only meaningful with --rich-markdown",
            err=True,
        )
        raise typer.Exit(code=2)
    if rich_markdown is None and media_group_args:
        typer.echo(
            "--media-group is only meaningful with --rich-markdown",
            err=True,
        )
        raise typer.Exit(code=2)
    if spaced_paragraphs is not None and rich_markdown is None:
        # The knob only affects how markdown is rewritten before it is parsed
        # server-side; accepting it for a plain send would silently do nothing.
        typer.echo(
            "--spaced-paragraphs/--no-spaced-paragraphs is only meaningful with "
            "--rich-markdown",
            err=True,
        )
        raise typer.Exit(code=2)
    if line_breaks is not None and rich_markdown is None:
        typer.echo(
            "--line-breaks/--no-line-breaks is only meaningful with --rich-markdown",
            err=True,
        )
        raise typer.Exit(code=2)
    if rich_markdown is None:
        return None

    if (text and text.strip()) or has_attachments:
        typer.echo(
            "--rich-markdown cannot be combined with --text, --file, or --file-url",
            err=True,
        )
        raise typer.Exit(code=2)
    if is_mass:
        typer.echo(
            "--rich-markdown is only supported for targeted sends, not mass mode",
            err=True,
        )
        raise typer.Exit(code=2)
    try:
        # ``utf-8-sig`` drops a leading BOM: a BOM-prefixed file would
        # otherwise send "﻿# Title" and silently lose its first
        # heading. Invalid UTF-8 still raises UnicodeDecodeError.
        markdown = rich_markdown.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError as exc:
        typer.echo(
            f"--rich-markdown file is not valid UTF-8: {rich_markdown}",
            err=True,
        )
        raise typer.Exit(code=2) from exc
    except OSError as exc:
        typer.echo(f"--rich-markdown file cannot be read: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    # An Obsidian note opens with YAML frontmatter, which this dialect has
    # no notion of: the scanner reads it as a divider plus a setext heading
    # and the article would start with a rule and a big "tags: [...]"
    # heading. Strip it here, at the same file-read boundary as the BOM —
    # and before the empty check, so a note that is *only* frontmatter is
    # reported as empty rather than sent as a bare heading.
    markdown = strip_yaml_frontmatter(markdown)
    if not markdown.strip():
        typer.echo(f"--rich-markdown file is empty: {rich_markdown}", err=True)
        raise typer.Exit(code=2)
    overrides = _parse_rich_file_overrides(rich_file_args)
    choices = _parse_media_group_choices(media_group_args)
    # Local media is CLI-only (the CLI is the trusted, local surface): the
    # article's own directory is the base, so a note can be sent as written.
    # This runs before the length check so the bound applies to the rewritten
    # body — the tg:// references replace the original paths.
    try:
        media_scan = scan_media(
            markdown,
            base_dir=rich_markdown.parent,
            vault_dir=vault_dir,
            overrides=overrides,
        )
    except MediaResolutionError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc
    markdown = media_scan.markdown
    if choices:
        # Check the indexes against the article's actual media runs here,
        # so a typo costs no backend connection. The modes do not matter
        # for that check, hence the cheapest possible pass.
        try:
            normalize_rich_markdown(
                markdown,
                spaced_paragraphs=False,
                grouping="none",
                media_groups=choices,
            )
        except MediaGroupError as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(code=2) from exc
    if len(markdown) > MAX_RICH_MARKDOWN_CHARS:
        typer.echo(
            f"--rich-markdown exceeds {MAX_RICH_MARKDOWN_CHARS} characters "
            f"({len(markdown)} given)",
            err=True,
        )
        raise typer.Exit(code=2)
    return RichSendInput(
        markdown=markdown,
        files=media_scan.files,
        choices=choices,
    )


def _parse_rich_file_overrides(entries: list[str]) -> dict[str, Path]:
    """Turn ``--rich-file <reference>=<path>`` entries into a lookup."""
    overrides: dict[str, Path] = {}
    for entry in entries:
        reference, sep, override_path = entry.partition("=")
        if not sep or not reference.strip() or not override_path.strip():
            typer.echo(
                f"--rich-file must be <reference>=<path> ({entry!r} given)",
                err=True,
            )
            raise typer.Exit(code=2)
        overrides[reference.strip()] = Path(override_path.strip())
    return overrides


def _parse_media_group_choices(entries: list[str]) -> tuple[MediaGroupChoice, ...]:
    """Turn ``--media-group <index>=<mode>`` entries into domain choices."""
    choices: list[MediaGroupChoice] = []
    for entry in entries:
        raw_index, sep, raw_mode = entry.partition("=")
        mode = raw_mode.strip().lower()
        # ``int`` inside the validation, not after it: ``"²".isdigit()`` is
        # true but ``int("²")`` raises, which would surface as the internal
        # exit 1 instead of this argument error.
        index: int | None = None
        if sep and mode in MEDIA_GROUP_MODES:
            try:
                index = int(raw_index.strip())
            except ValueError:
                index = None
        if index is None or index < 0:
            typer.echo(
                "--media-group must be <index>=<"
                + "|".join(MEDIA_GROUP_MODES)
                + f"> ({entry!r} given)",
                err=True,
            )
            raise typer.Exit(code=2)
        choices.append(MediaGroupChoice(index=index, mode=mode))
    return tuple(choices)


def rich_dry_run_markers(
    rich_input: RichSendInput | None,
    *,
    rich_markdown: Path | None,
    spaced_paragraphs: bool,
    line_breaks: bool,
    media_grouping: str,
) -> RichDryRun:
    """Describe what a targeted ``--dry-run`` would send, rich or plain.

    Preview exactly what the real send would hand to Telegram: the domain
    normalises before its own length check, so the numbers reported here are
    post-normalization.
    """
    normalization: RichMarkdownNormalization | None = (
        normalize_rich_markdown(
            rich_input.markdown,
            spaced_paragraphs=spaced_paragraphs,
            line_breaks=line_breaks,
            grouping=media_grouping,
            media_groups=rich_input.choices,
        )
        if rich_input is not None
        else None
    )
    is_rich = rich_input is not None
    markers: dict[str, object] = {
        # The article body can be up to 32k chars and is the payload of
        # the send, not a routing decision — report a marker + length
        # and the source path, never the markdown itself.
        "rich_markdown": is_rich,
        "rich_markdown_chars": (
            len(normalization.markdown) if normalization is not None else None
        ),
        "rich_markdown_blocks": (
            normalization.blocks if normalization is not None else None
        ),
        "rich_markdown_media": (
            normalization.media if normalization is not None else None
        ),
        "rich_markdown_wikilinks": (
            normalization.wikilinks if normalization is not None else None
        ),
        "rich_markdown_file": (str(rich_markdown) if is_rich else None),
        # Local media the real send would upload. The files are listed,
        # never read — a dry run touches no bytes and no network.
        "rich_files": (
            [
                {
                    "id": rf.id,
                    "path": rf.path,
                    "kind": rf.kind,
                    "caption": rf.caption,
                }
                for rf in rich_input.files
            ]
            if rich_input is not None
            else None
        ),
        # ``spaced_paragraphs`` echoes the *effective* decision: the
        # flag, else the config default. ``spaced`` is what the pass
        # actually did (a block-limit rollback turns it False).
        "spaced_paragraphs": spaced_paragraphs if is_rich else None,
        "line_breaks": line_breaks if is_rich else None,
        "spaced": (normalization.spaced if normalization is not None else None),
        # Every run of 2+ consecutive media, with the mode it would be
        # sent as — this is what a caller asks the human about before
        # re-running with --media-group.
        "media_grouping": media_grouping if is_rich else None,
        "rich_markdown_groups": (
            [
                {
                    "index": group.index,
                    "size": group.size,
                    "mode": group.mode,
                    "preceding_text": group.preceding_text,
                    # The captions of the run's media, joined: what the
                    # group will be captioned with, since Telegram shows
                    # no caption on a grouped medium itself.
                    "caption": group.caption,
                }
                for group in normalization.groups
            ]
            if normalization is not None
            else None
        ),
    }
    what = (
        f"rich message ({len(normalization.markdown)} chars)"
        if normalization is not None
        else "message"
    )
    warnings = list(normalization.warnings if normalization is not None else ())
    if (
        normalization is not None
        and len(normalization.markdown) > MAX_RICH_MARKDOWN_CHARS
    ):
        # The real send would raise on the post-normalization length;
        # say so here rather than reporting a plan that cannot run.
        warnings.append(
            f"rich_markdown exceeds {MAX_RICH_MARKDOWN_CHARS} characters "
            f"({len(normalization.markdown)} after normalization); "
            "the real send would fail"
        )
    return RichDryRun(markers=markers, what=what, warnings=warnings)
