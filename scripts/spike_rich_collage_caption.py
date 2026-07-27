#!/usr/bin/env python3
"""Spike: give a ``<tg-collage>``/``<tg-slideshow>`` its own caption from markdown.

The grouping pass wraps a run of consecutive media blocks in ``<tg-collage>``,
and Telegram's clients then show **no caption under an individual medium inside
the group** — only the group's own ``PageBlockCollage.caption``. (The item
captions do reach the server: a read-back of a grouped article has
``PageBlockPhoto.caption`` populated inside the collage. It is the rendering
that ignores them.) So a grouped run needs a caption of its own. The Bot API
type reference (``@grammyjs/types/rich.d.ts``) documents that caption for the
*HTML* dialect only — ``<tg-collage><img …/><figcaption>Caption</figcaption>
</tg-collage>`` — and its markdown example shows a collage with no caption at
all, so the markdown spelling is unknown.

This script sends one article per candidate spelling to Saved Messages, reads
each accepted article back, and prints the resulting ``PageBlockCollage``
caption (plus the item captions, to confirm they stay empty). The winner is the
spelling the shipped grouping pass should emit.

It is a *spike*, not part of the shipped surface: it talks to the real account
and shares ``scripts/spike_rich_media.py``'s precondition of an authorized
Telethon session. It sends one real message **per candidate**.

Usage::

    .venv/bin/python scripts/spike_rich_collage_caption.py
    .venv/bin/python scripts/spike_rich_collage_caption.py --dry-run
    .venv/bin/python scripts/spike_rich_collage_caption.py --only figcaption-block
    .venv/bin/python scripts/spike_rich_collage_caption.py --file a.png --file b.png

Exit codes: 0 = at least one candidate was accepted (or dry run), 2 =
precondition missing (no session, no config, Telethon too old), 3 = the server
rejected the upload or *every* candidate.
"""

from __future__ import annotations

import argparse
import asyncio
import struct
import sys
import zlib
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC = REPO_ROOT / "src"
if str(SRC) not in sys.path:  # allow running without an editable install
    sys.path.insert(0, str(SRC))

DEFAULT_ENTITY = "me"
CAPTION = "Подпись группы"
FIRST_ID = "spike-collage-a"
SECOND_ID = "spike-collage-b"


@dataclass(frozen=True)
class Candidate:
    """One markdown spelling of a group caption."""

    name: str
    syntax: str
    body: str


def _tiny_png(red: int, green: int, blue: int) -> bytes:
    """A valid 1×1 PNG of the given colour, so the spike needs no fixtures."""

    def chunk(tag: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + tag
            + payload
            + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)
        )

    header = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    raw = bytes([0, red, green, blue])
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


def build_candidates() -> tuple[Candidate, ...]:
    media = f"![](tg://photo?id={FIRST_ID})\n![](tg://photo?id={SECOND_ID})"
    return (
        Candidate(
            name="figcaption-block",
            syntax="<figcaption> as its own block inside <tg-collage>",
            body=(
                f"Text above the collage.\n\n<tg-collage>\n\n{media}\n\n"
                f"<figcaption>{CAPTION}</figcaption>\n\n</tg-collage>\n"
            ),
        ),
        Candidate(
            name="figcaption-attached",
            syntax="<figcaption> on the line right after the last media",
            body=(
                f"Text above the collage.\n\n<tg-collage>\n\n{media}\n"
                f"<figcaption>{CAPTION}</figcaption>\n\n</tg-collage>\n"
            ),
        ),
        Candidate(
            name="figcaption-close-line",
            syntax="<figcaption> on the closing-tag line",
            body=(
                f"Text above the collage.\n\n<tg-collage>\n\n{media}\n\n"
                f"<figcaption>{CAPTION}</figcaption></tg-collage>\n"
            ),
        ),
        Candidate(
            name="plain-line",
            syntax="bare text line inside <tg-collage>",
            body=(
                f"Text above the collage.\n\n<tg-collage>\n\n{media}\n\n"
                f"{CAPTION}\n\n</tg-collage>\n"
            ),
        ),
        Candidate(
            name="caption-attribute",
            syntax='<tg-collage caption="…">',
            body=(
                f'Text above the collage.\n\n<tg-collage caption="{CAPTION}">\n\n'
                f"{media}\n\n</tg-collage>\n"
            ),
        ),
        Candidate(
            name="slideshow-figcaption-block",
            syntax="<figcaption> as its own block inside <tg-slideshow>",
            body=(
                f"Text above the slideshow.\n\n<tg-slideshow>\n\n{media}\n\n"
                f"<figcaption>{CAPTION}</figcaption>\n\n</tg-slideshow>\n"
            ),
        ),
    )


def _fail(message: str, code: int = 2) -> int:
    print(f"FAIL: {message}", file=sys.stderr)
    return code


def _caption_text(caption: Any) -> str | None:
    """Flatten a ``PageCaption``'s text into a plain string (``None`` when empty)."""

    def flatten(node: Any) -> str:
        if node is None:
            return ""
        text = getattr(node, "text", None)
        if isinstance(text, str):
            return text
        if text is not None:
            return flatten(text)
        parts = getattr(node, "texts", None)
        if parts:
            return "".join(flatten(part) for part in parts)
        return ""

    if caption is None:
        return None
    value = flatten(getattr(caption, "text", None))
    return value or None


def _describe_rich_message(message: Any) -> str:
    """Print the group block, its caption, and the caption of every item."""

    rich = getattr(message, "rich_message", None)
    if rich is None:
        return "    (no rich_message on the read-back message)"
    lines: list[str] = []
    for block in list(getattr(rich, "blocks", None) or []):
        name = type(block).__name__
        items = list(getattr(block, "items", None) or [])
        if not items:
            lines.append(f"      {name}")
            continue
        lines.append(
            f"      {name}: {len(items)} items, "
            f"caption={_caption_text(getattr(block, 'caption', None))!r}"
        )
        for item in items:
            lines.append(
                f"        {type(item).__name__} "
                f"caption={_caption_text(getattr(item, 'caption', None))!r}"
            )
    return "\n".join(lines)


async def _upload_photo(client: Any, *, peer: Any, path: Path, file_id: str) -> Any:
    from telethon import utils
    from telethon.tl import functions, types

    uploaded = await client.upload_file(path)
    result = await client(
        functions.messages.UploadMediaRequest(
            peer=peer, media=types.InputMediaUploadedPhoto(file=uploaded)
        )
    )
    photo = getattr(result, "photo", result)
    return types.InputRichFilePhoto(id=file_id, photo=utils.get_input_photo(photo))


async def _try_candidate(
    client: Any, *, peer: Any, candidate: Candidate, files: list[Any]
) -> bool:
    from telethon.tl import functions, types

    request = functions.messages.SendMessageRequest(
        peer=peer,
        message="",
        rich_message=types.InputRichMessageMarkdown(markdown=candidate.body, files=files),
    )
    print(f"\n[{candidate.name}] {candidate.syntax}")
    try:
        result = await client(request)
    except Exception as exc:  # noqa: BLE001 - the taxonomy is the finding
        print(f"  REJECTED: {type(exc).__name__}: {exc}")
        return False

    from telegram_assistant.messages.telethon_backend import _extract_rich_message_id

    message_id = _extract_rich_message_id(result, random_id=getattr(request, "random_id", None))
    print(f"  ACCEPTED: message_id={message_id}")
    if message_id is None:
        print("    (no readable message id — cannot read the article back)")
        return True
    try:
        sent = await client.get_messages(peer, ids=message_id)
    except Exception as exc:  # noqa: BLE001 - read-back is best effort
        print(f"    read-back failed: {type(exc).__name__}: {exc}")
        return True
    print(_describe_rich_message(sent))
    return True


async def _run(args: argparse.Namespace) -> int:
    try:
        from telethon.tl import functions, types
    except ImportError as exc:  # pragma: no cover - spike script
        return _fail(f"Telethon is not importable: {exc}")
    if getattr(types, "InputRichFilePhoto", None) is None:
        return _fail("This Telethon build has no InputRichFilePhoto (layer < 227).")
    if "rich_message" not in functions.messages.SendMessageRequest.__init__.__annotations__:
        return _fail("SendMessageRequest has no rich_message parameter. Install telethon>=1.44.")

    candidates = build_candidates()
    if args.only:
        wanted = {name.strip() for name in args.only.split(",") if name.strip()}
        unknown = wanted - {candidate.name for candidate in candidates}
        if unknown:
            return _fail(f"Unknown candidate(s): {', '.join(sorted(unknown))}")
        candidates = tuple(c for c in candidates if c.name in wanted)

    if args.dry_run:
        for candidate in candidates:
            print(f"\n[{candidate.name}] {candidate.syntax}\n{candidate.body}")
        print("dry-run: not uploading, not sending")
        return 0

    from telegram_assistant.config.loader import ConfigError, load_config
    from telegram_assistant.entities.service import CachingEntityResolver, EntityError
    from telegram_assistant.entities.telethon_backend import TelethonResolverBackend
    from telegram_assistant.telegram_client.session import TelethonSessionManager

    try:
        config = load_config(args.config)
    except ConfigError as exc:
        return _fail(str(exc))

    manager = TelethonSessionManager(config.telegram)
    if not manager.session_path_exists():
        return _fail(
            f"No Telethon session at {manager.session_path}. Run `telegram-assistant auth` first."
        )

    client = await manager.get_client()
    with TemporaryDirectory() as tmp:
        try:
            if not await client.is_user_authorized():
                return _fail(f"Session {manager.session_path} exists but is not authorized.")
            resolver = CachingEntityResolver(TelethonResolverBackend(client))
            try:
                resolved = await resolver.resolve(args.entity)
            except EntityError as exc:
                return _fail(f"Could not resolve {args.entity!r}: {exc}")
            print(f"resolved: chat_id={resolved.chat_id} title={resolved.title!r}")

            paths = [Path(name) for name in args.file]
            if not paths:
                paths = []
                for index, colour in enumerate(((220, 40, 40), (40, 80, 220))):
                    path = Path(tmp) / f"spike-collage-{index}.png"
                    path.write_bytes(_tiny_png(*colour))
                    paths.append(path)
            if len(paths) != 2:
                return _fail("pass exactly two --file values, or none to generate them")

            peer = await client.get_input_entity(resolved.chat_id)
            try:
                files = [
                    await _upload_photo(client, peer=peer, path=path, file_id=file_id)
                    for path, file_id in zip(paths, (FIRST_ID, SECOND_ID), strict=True)
                ]
            except Exception as exc:  # noqa: BLE001 - the taxonomy is the finding
                print(f"UPLOAD ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
                return 3

            accepted = [
                candidate.name
                for candidate in candidates
                if await _try_candidate(client, peer=peer, candidate=candidate, files=files)
            ]
            print(f"\naccepted: {', '.join(accepted) if accepted else '(none)'}")
            return 0 if accepted else 3
        finally:
            await manager.disconnect()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--entity", default=DEFAULT_ENTITY, help="target chat reference")
    parser.add_argument("--config", default=None, help="path to config.yml")
    parser.add_argument(
        "--file", action="append", default=[], help="image to upload (pass twice; default: generated)"
    )
    parser.add_argument("--only", default=None, help="comma-separated candidate names to try")
    parser.add_argument("--dry-run", action="store_true", help="print candidates, send nothing")
    return asyncio.run(_run(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
