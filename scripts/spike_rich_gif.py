#!/usr/bin/env python3
"""Spike: how must an animated GIF be uploaded to attach to a rich message?

``docs/TODO.md`` records that an animated ``.gif`` referenced from an article
does not attach at all, and the ffprobe plan
(``docs/superpowers/plans/2026-07-29-rich-markdown-media-probe.md``) assumes the
fix is to convert it to a silent mp4 marked ``DocumentAttributeAnimated``. That
assumption was never proven on the wire, and the plan's Task 3 introduced a
third, also unproven shape: a raw ``image/gif`` document carrying *both* a
probe-derived ``DocumentAttributeVideo`` and ``DocumentAttributeAnimated``.

So this script uploads the same source ``.gif`` under each candidate document
shape, sends one article per shape, and reads each one back — the read-back is
what proves the media really attached rather than the send merely returning a
message id.

Candidates:

``gif-stub``
    Raw ``image/gif``, ``DocumentAttributeFilename`` + ``DocumentAttributeAnimated``.
    The pre-plan shipped shape — the one TODO says does not attach.
``gif-probed``
    Raw ``image/gif`` plus a probe-derived ``DocumentAttributeVideo``. The shape
    the plan's Task 3 produces today.
``mp4-converted``
    The ``ffmpeg``-converted silent mp4 the plan's Task 5 intends to ship,
    ``video/mp4`` + probed ``DocumentAttributeVideo`` + ``DocumentAttributeAnimated``,
    with the author's own ``<name>.mp4`` filename.

This is a *spike*, not part of the shipped surface: it talks to the real
account and shares ``scripts/spike_rich_media.py``'s precondition of an
authorized Telethon session. It sends one real message **per candidate**, by
default to Saved Messages.

Usage::

    .venv/bin/python scripts/spike_rich_gif.py --file loop.gif
    .venv/bin/python scripts/spike_rich_gif.py --file loop.gif --only gif-probed
    .venv/bin/python scripts/spike_rich_gif.py --file loop.gif --dry-run

Exit codes: 0 = at least one candidate was accepted (or dry run), 2 =
precondition missing (no file, no session, no config, Telethon too old, no
ffmpeg), 3 = the server rejected the upload or send for *every* candidate.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC = REPO_ROOT / "src"
if str(SRC) not in sys.path:  # allow running without an editable install
    sys.path.insert(0, str(SRC))

DEFAULT_ENTITY = "me"
FILE_ID = "loop"


@dataclass(frozen=True)
class Candidate:
    """One document shape to prove or disprove."""

    name: str
    question: str
    mime_type: str
    converted: bool


CANDIDATES: tuple[Candidate, ...] = (
    Candidate(
        name="gif-stub",
        question="does the pre-plan shape (image/gif + Animated only) attach?",
        mime_type="image/gif",
        converted=False,
    ),
    Candidate(
        name="gif-probed",
        question="does image/gif + probed DocumentAttributeVideo + Animated attach?",
        mime_type="image/gif",
        converted=False,
    ),
    Candidate(
        name="mp4-converted",
        question="does the converted silent mp4 + Video + Animated attach?",
        mime_type="video/mp4",
        converted=True,
    ),
)


def _fail(message: str, code: int = 2) -> int:
    print(f"FAIL: {message}", file=sys.stderr)
    return code


def _build_attributes(candidate: Candidate, *, probe: Any, file_name: str) -> list[Any]:
    """Build the document attributes for one candidate, mirroring the backend."""
    from telethon.tl import types

    attributes: list[Any] = [types.DocumentAttributeFilename(file_name=file_name)]
    if candidate.name != "gif-stub" and probe is not None and probe.width and probe.height:
        attributes.append(
            types.DocumentAttributeVideo(
                duration=round(probe.duration),
                w=probe.width,
                h=probe.height,
                supports_streaming=True,
            )
        )
    attributes.append(types.DocumentAttributeAnimated())
    return attributes


def _describe_document(result: Any) -> str:
    """Print the mime and attributes Telegram kept for the uploaded document."""
    document = getattr(result, "document", result)
    mime = getattr(document, "mime_type", None)
    attributes = list(getattr(document, "attributes", None) or [])
    names = []
    for attr in attributes:
        detail = ""
        if hasattr(attr, "w"):
            detail = f"(w={attr.w} h={attr.h} duration={getattr(attr, 'duration', None)})"
        elif hasattr(attr, "file_name"):
            detail = f"({attr.file_name})"
        names.append(f"{type(attr).__name__}{detail}")
    size = getattr(document, "size", None)
    thumbs = getattr(document, "thumbs", None)
    return (
        f"    uploaded document: mime={mime!r} size={size} "
        f"thumbs={len(thumbs) if thumbs else thumbs}\n"
        f"    attributes kept: {', '.join(names) if names else '(none)'}"
    )


async def _send_candidate(
    client: Any,
    *,
    peer: Any,
    candidate: Candidate,
    source: Path,
    probe_of: dict[str, Any],
) -> bool:
    """Upload *source* in this candidate's shape and send one article for it."""
    from telethon import utils
    from telethon.tl import functions, types

    from telegram_assistant.messages import media_probe
    from telegram_assistant.messages.telethon_backend import _extract_rich_message_id

    print(f"\n[{candidate.name}] {candidate.question}")

    temp_path: Path | None = None
    try:
        if candidate.converted:
            temp_path = media_probe.convert_gif_to_mp4(source)
            upload_path = temp_path
            file_name = f"{source.stem}.mp4"
        else:
            upload_path = source
            file_name = source.name

        probe = probe_of.get(str(upload_path))
        if probe is None:
            probe = media_probe.probe_media(upload_path)
            probe_of[str(upload_path)] = probe
        print(f"  probe: {probe}")

        attributes = _build_attributes(candidate, probe=probe, file_name=file_name)
        print(f"  sending: mime={candidate.mime_type!r} name={file_name!r}")
        print(f"  attributes: {', '.join(type(a).__name__ for a in attributes)}")

        handle = await client.upload_file(str(upload_path))
        media = types.InputMediaUploadedDocument(
            file=handle, mime_type=candidate.mime_type, attributes=attributes
        )
        try:
            uploaded = await client(
                functions.messages.UploadMediaRequest(peer=peer, media=media)
            )
        except Exception as exc:  # noqa: BLE001 - the taxonomy is the finding
            print(f"  UPLOAD REJECTED: {type(exc).__name__}: {exc}")
            return False
        print(_describe_document(uploaded))

        rich_file = types.InputRichFileDocument(
            id=FILE_ID,
            document=utils.get_input_document(getattr(uploaded, "document", uploaded)),
        )
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)

    body = f"# {candidate.name}\n\n![]({'tg://video?id='}{FILE_ID})\n\nTrailing paragraph.\n"
    request = functions.messages.SendMessageRequest(
        peer=peer,
        message="",
        rich_message=types.InputRichMessageMarkdown(markdown=body, files=[rich_file]),
    )
    try:
        result = await client(request)
    except Exception as exc:  # noqa: BLE001 - the taxonomy is the finding
        print(f"  SEND REJECTED: {type(exc).__name__}: {exc}")
        return False

    message_id = _extract_rich_message_id(
        result, random_id=getattr(request, "random_id", None)
    )
    print(f"  ACCEPTED: message_id={message_id}")
    if message_id is None:
        print("    (no readable message id — cannot read the article back)")
        return True

    try:
        sent = await client.get_messages(peer, ids=message_id)
    except Exception as exc:  # noqa: BLE001 - read-back is best effort
        print(f"    read-back failed: {type(exc).__name__}: {exc}")
        return True

    rich = getattr(sent, "rich_message", None)
    if rich is None:
        print("    read-back: NO rich_message on the message")
        return True
    blocks = list(getattr(rich, "blocks", None) or [])
    documents = list(getattr(rich, "documents", None) or [])
    photos = list(getattr(rich, "photos", None) or [])
    print(
        f"    read-back: {len(blocks)} blocks, {len(documents)} documents, "
        f"{len(photos)} photos"
    )
    for block in blocks:
        fields = []
        for attr in ("video_id", "photo_id", "audio_id", "url", "caption"):
            value = getattr(block, attr, None)
            if value is None:
                continue
            if not isinstance(value, (str, int, bool)):
                value = f"<{type(value).__name__}>"
            fields.append(f"{attr}={value!r}")
        suffix = f" ({', '.join(fields)})" if fields else ""
        print(f"      {type(block).__name__}{suffix}")
    for document in documents:
        print(_describe_document(document))
    return True


async def _run(args: argparse.Namespace) -> int:
    try:
        from telethon.tl import functions, types
    except ImportError as exc:  # pragma: no cover - spike script
        return _fail(f"Telethon is not importable: {exc}")

    if getattr(types, "InputRichMessageMarkdown", None) is None:
        return _fail("This Telethon build has no InputRichMessageMarkdown. Install telethon>=1.44.")
    if "rich_message" not in functions.messages.SendMessageRequest.__init__.__annotations__:
        return _fail("SendMessageRequest has no rich_message parameter. Install telethon>=1.44.")

    source = Path(args.file).expanduser()
    if not source.is_file():
        return _fail(f"No such file: {source}")
    if source.suffix.lower() != ".gif":
        return _fail(f"{source} is not a .gif — this spike is about GIF attachment only")

    from telegram_assistant.config.loader import ConfigError, load_config
    from telegram_assistant.entities.service import CachingEntityResolver, EntityError
    from telegram_assistant.messages import media_probe
    from telegram_assistant.telegram_client.session import TelethonSessionManager

    if not media_probe.ffmpeg_available() or not media_probe.ffprobe_available():
        return _fail("ffmpeg/ffprobe are not on PATH — every candidate here needs them")

    candidates = CANDIDATES
    if args.only:
        wanted = {name.strip() for name in args.only.split(",") if name.strip()}
        unknown = wanted - {c.name for c in candidates}
        if unknown:
            return _fail(
                f"Unknown candidate(s): {', '.join(sorted(unknown))}. "
                f"Known: {', '.join(c.name for c in candidates)}"
            )
        candidates = tuple(c for c in candidates if c.name in wanted)

    try:
        config = load_config(args.config)
    except ConfigError as exc:
        return _fail(str(exc))

    print(f"file:       {source} ({source.stat().st_size} bytes)")
    print(f"session:    {config.telegram.session_path}")
    print(f"target:     {args.entity}")
    print(f"candidates: {', '.join(c.name for c in candidates)}")

    if args.dry_run:
        for candidate in candidates:
            print(f"\n[{candidate.name}] {candidate.question}")
            print(f"  mime={candidate.mime_type!r} converted={candidate.converted}")
        print("\ndry-run: not uploading, not sending")
        return 0

    from telegram_assistant.entities.telethon_backend import TelethonResolverBackend

    manager = TelethonSessionManager(config.telegram)
    if not manager.session_path_exists():
        return _fail(
            f"No Telethon session at {manager.session_path}. "
            "Run `telegram-assistant auth` first."
        )

    client = await manager.get_client()
    try:
        if not await client.is_user_authorized():
            return _fail(f"Session {manager.session_path} exists but is not authorized.")

        me = await client.get_me()
        print(f"account:    id={getattr(me, 'id', None)}")

        resolver = CachingEntityResolver(TelethonResolverBackend(client))
        try:
            resolved = await resolver.resolve(args.entity)
        except EntityError as exc:
            return _fail(f"Could not resolve {args.entity!r}: {exc}")
        print(f"resolved:   chat_id={resolved.chat_id} title={resolved.title!r}")

        peer = await client.get_input_entity(resolved.chat_id)
        probe_of: dict[str, Any] = {}
        sent = []
        for candidate in candidates:
            try:
                ok = await _send_candidate(
                    client, peer=peer, candidate=candidate, source=source, probe_of=probe_of
                )
            except Exception as exc:  # noqa: BLE001 - the taxonomy is the finding
                print(f"  ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
                ok = False
            if ok:
                sent.append(candidate.name)
        print(f"\nsent: {', '.join(sent) if sent else '(none)'}")
        return 0 if sent else 3
    finally:
        await manager.disconnect()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--file", required=True, help="local animated .gif to probe")
    parser.add_argument("--entity", default=DEFAULT_ENTITY, help="target chat reference")
    parser.add_argument("--config", default=None, help="path to config.yml")
    parser.add_argument("--only", default=None, help="comma-separated candidate names to try")
    parser.add_argument("--dry-run", action="store_true", help="resolve only, do not upload/send")
    return asyncio.run(_run(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
