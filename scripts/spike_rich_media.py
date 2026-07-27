#!/usr/bin/env python3
"""Spike: reference a *local* file from a Telegram rich message (article).

Answers the blocking question of the rich-markdown media plan
(``docs/plans/20260727-rich-markdown-spacing-and-media.md``, Task 5): the Bot
API twin can only reference media by public HTTPS URL, but MTProto's
``InputRichMessageMarkdown`` carries ``files: list[InputRichFile]``, where each
entry is ``InputRichFilePhoto(id=<str>, photo=…)`` /
``InputRichFileDocument(id=<str>, document=…)``. The ``id`` is a caller-chosen
string, and **nothing local documents how the markdown refers back to it** —
neither Telethon's stubs nor ``@grammyjs/types/rich.d.ts`` (which describes the
Bot API dialect, where media blocks "support only HTTP and HTTPS URLs").

So this script uploads one local file, builds the ``InputRichFile`` for it, and
sends one article per candidate reference syntax, reporting which ones the
server accepts. Each accepted message is read back and its ``RichMessage``
block list printed, so the answer is *proven* (the media block is really there
and really points at the upload) rather than inferred from a 200.

This is a *spike*, not part of the shipped surface: it talks to the real
account, so it lives next to ``scripts/spike_rich_message.py`` and shares its
precondition of an authorized Telethon session. It sends one real message **per
candidate**, by default to Saved Messages.

Usage::

    .venv/bin/python scripts/spike_rich_media.py --file ~/Pictures/photo.png
    .venv/bin/python scripts/spike_rich_media.py --file clip.mp4 --entity me
    .venv/bin/python scripts/spike_rich_media.py --file photo.png --dry-run
    .venv/bin/python scripts/spike_rich_media.py --file photo.png --only bare-id

Exit codes: 0 = at least one candidate was accepted (or dry run), 2 =
precondition missing (no file, no session, no config, Telethon too old), 3 =
the server rejected the upload or *every* candidate.
"""

from __future__ import annotations

import argparse
import asyncio
import mimetypes
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC = REPO_ROOT / "src"
if str(SRC) not in sys.path:  # allow running without an editable install
    sys.path.insert(0, str(SRC))

# Saved Messages, not the usual e2e chat: "Client chat test" currently rejects
# *every* send with ChatRestrictedError, so it cannot tell a media-rights
# failure apart from a chat-level one.
DEFAULT_ENTITY = "me"

# Suffixes Telegram treats as a photo; everything else is uploaded as a
# document (video, audio, animation), which is also what the shipped backend
# will have to decide. Kept deliberately narrow — a ``.gif`` is an animation,
# i.e. a document, not a photo.
PHOTO_SUFFIXES = frozenset({".jpg", ".jpeg", ".png", ".webp"})

DEFAULT_ALT = "spike alt"
DEFAULT_CAPTION = "spike caption"


@dataclass(frozen=True)
class Candidate:
    """One guess at how the markdown refers to an ``InputRichFile.id``."""

    name: str
    kind: str  # "markdown" | "html" — which InputRichMessage* carries the body
    syntax: str  # the reference form, with the id substituted in
    body: str  # the whole article sent for this candidate


def classify_file(path: Path | str) -> str:
    """Return ``"photo"`` or ``"document"`` for a local file, by suffix."""
    suffix = Path(path).suffix.lower()
    return "photo" if suffix in PHOTO_SUFFIXES else "document"


def default_file_id(path: Path | str) -> str:
    """Derive a safe ``InputRichFile.id`` from a file name.

    The id doubles as the markdown reference, so it must survive being written
    inside ``![](…)`` — spaces, brackets and quotes are replaced rather than
    escaped, and a name that reduces to nothing falls back to ``file1``.
    """
    stem = Path(path).stem
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", stem).strip("-.")
    return slug or "file1"


def build_candidates(
    file_id: str,
    *,
    alt: str = DEFAULT_ALT,
    caption: str = DEFAULT_CAPTION,
) -> tuple[Candidate, ...]:
    """Every reference syntax worth trying, in the order the plan lists them.

    Pure on purpose: the network half of this spike is manual, but the variant
    list and the id substitution are what a later task reuses, so they are unit
    tested. Each article names its own candidate in a paragraph, so a read-back
    (or a scroll through Saved Messages) says which syntax produced it.
    """
    markdown_refs = (
        ("bare-id", f"![]({file_id})"),
        ("tg-file-url", f"![](tg://file?id={file_id})"),
        ("attach-scheme", f"![](attach://{file_id})"),
        ("alt-and-caption", f'![{alt}]({file_id} "{caption}")'),
    )
    candidates = [
        Candidate(
            name=name,
            kind="markdown",
            syntax=reference,
            body=(
                f"# rich media spike: {name}\n"
                "\n"
                f"Reference syntax: `{reference}`\n"
                "\n"
                f"{reference}\n"
                "\n"
                "Trailing paragraph after the media block.\n"
            ),
        )
        for name, reference in markdown_refs
    ]
    html_reference = f'<img src="{file_id}"/>'
    candidates.append(
        Candidate(
            name="html-img",
            kind="html",
            syntax=html_reference,
            body=(
                "<h1>rich media spike: html-img</h1>\n"
                f"<p>Reference syntax: <code>&lt;img src=&quot;{file_id}&quot;/&gt;</code></p>\n"
                f"{html_reference}\n"
                "<p>Trailing paragraph after the media block.</p>\n"
            ),
        )
    )
    return tuple(candidates)


def _fail(message: str, code: int = 2) -> int:
    print(f"FAIL: {message}", file=sys.stderr)
    return code


def _describe_rich_message(message: Any) -> str:
    """Print the read-back ``RichMessage`` shallowly enough to see the media."""
    rich = getattr(message, "rich_message", None)
    if rich is None:
        return "    (no rich_message on the read-back message)"
    blocks = list(getattr(rich, "blocks", None) or [])
    photos = list(getattr(rich, "photos", None) or [])
    documents = list(getattr(rich, "documents", None) or [])
    lines = [
        f"    {type(rich).__name__}: {len(blocks)} blocks, "
        f"{len(photos)} photos, {len(documents)} documents"
    ]
    interesting = ("photo_id", "video_id", "audio_id", "url", "caption", "level")
    for block in blocks:
        fields = []
        for attr in interesting:
            if not hasattr(block, attr):
                continue
            value = getattr(block, attr)
            if value is None:
                continue
            if not isinstance(value, (str, int, bool)):
                value = f"<{type(value).__name__}>"
            elif isinstance(value, str) and len(value) > 60:
                value = value[:60] + "…"
            fields.append(f"{attr}={value!r}")
        suffix = f" ({', '.join(fields)})" if fields else ""
        lines.append(f"      {type(block).__name__}{suffix}")
    return "\n".join(lines)


def _extract_message_id(result: Any, *, random_id: int | None) -> int | None:
    """Report the id exactly as the shipped backend would (imported, not copied)."""
    from telegram_assistant.messages.telethon_backend import _extract_rich_message_id

    return _extract_rich_message_id(result, random_id=random_id)


async def _upload_rich_file(client: Any, *, peer: Any, path: Path, file_id: str) -> Any:
    """Upload ``path`` and wrap it as an ``InputRichFile`` keyed by ``file_id``."""
    from telethon import utils
    from telethon.tl import functions, types

    uploaded = await client.upload_file(path)
    kind = classify_file(path)
    if kind == "photo":
        media: Any = types.InputMediaUploadedPhoto(file=uploaded)
    else:
        mime_type, _ = mimetypes.guess_type(path.name)
        media = types.InputMediaUploadedDocument(
            file=uploaded,
            mime_type=mime_type or "application/octet-stream",
            attributes=[types.DocumentAttributeFilename(file_name=path.name)],
        )
    result = await client(functions.messages.UploadMediaRequest(peer=peer, media=media))
    print(f"uploaded: kind={kind} result={type(result).__name__}")

    if kind == "photo":
        photo = getattr(result, "photo", result)
        return types.InputRichFilePhoto(id=file_id, photo=utils.get_input_photo(photo))
    document = getattr(result, "document", result)
    return types.InputRichFileDocument(id=file_id, document=utils.get_input_document(document))


async def _try_candidate(
    client: Any,
    *,
    peer: Any,
    candidate: Candidate,
    rich_file: Any,
) -> bool:
    """Send one candidate; return whether the server accepted it."""
    from telethon.tl import functions, types

    if candidate.kind == "html":
        rich_message = types.InputRichMessageHTML(html=candidate.body, files=[rich_file])
    else:
        rich_message = types.InputRichMessageMarkdown(markdown=candidate.body, files=[rich_file])

    request = functions.messages.SendMessageRequest(
        peer=peer,
        message="",
        rich_message=rich_message,
    )
    print(f"\n[{candidate.name}] {candidate.syntax}")
    try:
        result = await client(request)
    except Exception as exc:  # noqa: BLE001 - the taxonomy is the finding
        print(f"  REJECTED: {type(exc).__name__}: {exc}")
        code = getattr(exc, "code", None)
        message = getattr(exc, "message", None)
        if code is not None or message is not None:
            print(f"            code={code} message={message}")
        return False

    message_id = _extract_message_id(result, random_id=getattr(request, "random_id", None))
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

    missing = [
        name
        for name in (
            "InputRichMessageMarkdown",
            "InputRichMessageHTML",
            "InputRichFilePhoto",
            "InputRichFileDocument",
        )
        if getattr(types, name, None) is None
    ]
    if missing:
        return _fail(
            f"This Telethon build has no {', '.join(missing)} (layer < 227). "
            "Install telethon>=1.44."
        )
    if "files" not in types.InputRichMessageMarkdown.__init__.__annotations__:
        return _fail("InputRichMessageMarkdown has no files parameter. Install telethon>=1.44.")
    if "rich_message" not in functions.messages.SendMessageRequest.__init__.__annotations__:
        return _fail("SendMessageRequest has no rich_message parameter. Install telethon>=1.44.")

    path = Path(args.file).expanduser()
    if not path.is_file():
        return _fail(f"No such file: {path}")

    file_id = args.file_id or default_file_id(path)
    candidates = build_candidates(file_id, alt=args.alt, caption=args.caption)
    if args.only:
        wanted = {name.strip() for name in args.only.split(",") if name.strip()}
        unknown = wanted - {candidate.name for candidate in candidates}
        if unknown:
            return _fail(
                f"Unknown candidate(s): {', '.join(sorted(unknown))}. "
                f"Known: {', '.join(candidate.name for candidate in candidates)}"
            )
        candidates = tuple(c for c in candidates if c.name in wanted)

    from telegram_assistant.config.loader import ConfigError, load_config
    from telegram_assistant.entities.service import CachingEntityResolver, EntityError
    from telegram_assistant.entities.telethon_backend import TelethonResolverBackend
    from telegram_assistant.telegram_client.session import TelethonSessionManager

    try:
        config = load_config(args.config)
    except ConfigError as exc:
        return _fail(str(exc))

    print(f"file:     {path} ({path.stat().st_size} bytes, kind={classify_file(path)})")
    print(f"file_id:  {file_id}")
    print(f"session:  {config.telegram.session_path}")
    print(f"target:   {args.entity}")
    print(f"candidates: {', '.join(candidate.name for candidate in candidates)}")

    manager = TelethonSessionManager(config.telegram)
    if not manager.session_path_exists():
        return _fail(
            f"No Telethon session at {manager.session_path}. "
            "Run `telegram-assistant auth` first (see scripts/e2e_test.sh preconditions)."
        )

    client = await manager.get_client()
    try:
        if not await client.is_user_authorized():
            return _fail(f"Session {manager.session_path} exists but is not authorized.")

        me = await client.get_me()
        print(f"account:  id={getattr(me, 'id', None)} premium={getattr(me, 'premium', None)}")

        resolver = CachingEntityResolver(TelethonResolverBackend(client))
        try:
            resolved = await resolver.resolve(args.entity)
        except EntityError as exc:
            return _fail(f"Could not resolve {args.entity!r}: {exc}")
        print(f"resolved: chat_id={resolved.chat_id} title={resolved.title!r}")

        if args.dry_run:
            for candidate in candidates:
                print(f"\n[{candidate.name}] ({candidate.kind}) {candidate.syntax}")
                print(candidate.body)
            print("dry-run: not uploading, not sending")
            return 0

        peer = await client.get_input_entity(resolved.chat_id)
        try:
            rich_file = await _upload_rich_file(client, peer=peer, path=path, file_id=file_id)
        except Exception as exc:  # noqa: BLE001 - the taxonomy is the finding
            print(f"UPLOAD ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 3

        accepted = [
            candidate.name
            for candidate in candidates
            if await _try_candidate(client, peer=peer, candidate=candidate, rich_file=rich_file)
        ]
        print(f"\naccepted: {', '.join(accepted) if accepted else '(none)'}")
        return 0 if accepted else 3
    finally:
        await manager.disconnect()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--file", required=True, help="local image/video to reference")
    parser.add_argument("--file-id", default=None, help="InputRichFile id (default: from name)")
    parser.add_argument("--entity", default=DEFAULT_ENTITY, help="target chat reference")
    parser.add_argument("--config", default=None, help="path to config.yml")
    parser.add_argument("--alt", default=DEFAULT_ALT, help="alt text for the alt-and-caption probe")
    parser.add_argument(
        "--caption", default=DEFAULT_CAPTION, help="caption for the alt-and-caption probe"
    )
    parser.add_argument("--only", default=None, help="comma-separated candidate names to try")
    parser.add_argument("--dry-run", action="store_true", help="resolve only, do not upload/send")
    return asyncio.run(_run(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
