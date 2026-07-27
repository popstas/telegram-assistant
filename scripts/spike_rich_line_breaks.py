#!/usr/bin/env python3
"""Spike: keep a *single* line break inside a paragraph visible in an article.

An Obsidian note writes two related lines under one another with no blank line
between them::

    Фотоальбом - https://photos.app.goo.gl/…
    Видео плейлист - https://www.youtube.com/…

Telegram parses the markdown server-side and — like CommonMark — folds that
*soft* break into a space, so the article shows one run-on line. The shipped
spacer pass cannot help: it inserts blocks *between* blocks, and those two lines
are one ``PageBlockParagraph``.

There are two ways out, and which one the server actually honours is the
question this spike answers:

* a real **hard break** inside the paragraph (``two trailing spaces``, a
  trailing ``\\``, or ``<br>``) — one block, no interaction with the spacer
  pass, and normalisation stays idempotent;
* splitting the paragraph in two with a blank line — two ``PageBlockParagraph``s
  which Telegram renders tight against each other (the very reason the U+00A0
  spacer exists), but a re-normalisation could no longer tell them from two
  paragraphs the author wrote.

It sends one article **per candidate** to Saved Messages, reads each accepted
article back, and prints the resulting blocks with their text ``repr()`` so a
surviving ``\\n`` is visible. It is a *spike*, not part of the shipped surface:
it talks to the real account and shares ``scripts/spike_rich_media.py``'s
precondition of an authorized Telethon session.

Findings (2026-07-27, Saved Messages, layer 227). Every candidate was
**accepted**; what differs is the block tree that comes back:

* ``soft-break`` — one ``PageBlockParagraph`` holding
  ``'…/album Видео плейлист - …'``: the newline is folded into a space, which
  is the bug.
* ``two-spaces``, ``backslash``, ``br-tag``, ``br-self-closing``,
  ``br-own-line`` — all four spellings of a hard break *work*: one
  ``PageBlockParagraph`` whose text carries a real ``\\n``. So the dialect does
  support an in-paragraph line break, and it costs no extra block.
* ``blank-line`` — two ``PageBlockParagraph``s, which the clients render tight
  against each other.

The shipped pass emits ``blank-line`` regardless, on the author's reading of
the rendered messages: a hard break inside one paragraph looks worse in the
Telegram clients than two tight paragraphs. The hard-break facts are recorded
here anyway — they are the reason the choice is a preference and not a
constraint, and they are what a future pass would use to keep normalisation
idempotent (see ``_split_paragraph_lines`` in ``messages/rich_markdown.py``).

Usage::

    .venv/bin/python scripts/spike_rich_line_breaks.py
    .venv/bin/python scripts/spike_rich_line_breaks.py --dry-run
    .venv/bin/python scripts/spike_rich_line_breaks.py --only br-tag,backslash

Exit codes: 0 = at least one candidate was accepted (or dry run), 2 =
precondition missing (no session, no config, Telethon too old), 3 = the server
rejected *every* candidate.
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
FIRST = "Фотоальбом - https://example.com/album"
SECOND = "Видео плейлист - https://example.com/playlist"
NBSP = " "


@dataclass(frozen=True)
class Candidate:
    """One markdown spelling of "these two lines are two lines"."""

    name: str
    syntax: str
    body: str


def build_candidates() -> tuple[Candidate, ...]:
    return (
        Candidate(
            name="soft-break",
            syntax="plain newline (the current, broken behaviour)",
            body=f"Заголовок абзаца.\n\n{FIRST}\n{SECOND}\n",
        ),
        Candidate(
            name="two-spaces",
            syntax="CommonMark hard break: two trailing spaces",
            body=f"Заголовок абзаца.\n\n{FIRST}  \n{SECOND}\n",
        ),
        Candidate(
            name="backslash",
            syntax="CommonMark hard break: trailing backslash",
            body=f"Заголовок абзаца.\n\n{FIRST}\\\n{SECOND}\n",
        ),
        Candidate(
            name="br-tag",
            syntax="inline <br> on the break",
            body=f"Заголовок абзаца.\n\n{FIRST}<br>{SECOND}\n",
        ),
        Candidate(
            name="br-self-closing",
            syntax="inline <br/> on the break",
            body=f"Заголовок абзаца.\n\n{FIRST}<br/>{SECOND}\n",
        ),
        Candidate(
            name="br-own-line",
            syntax="<br> alone at the end of the first line",
            body=f"Заголовок абзаца.\n\n{FIRST}<br>\n{SECOND}\n",
        ),
        Candidate(
            name="blank-line",
            syntax="split into two paragraphs with a blank line",
            body=f"Заголовок абзаца.\n\n{FIRST}\n\n{SECOND}\n",
        ),
        Candidate(
            name="blank-line-spaced",
            syntax="two paragraphs with a U+00A0 spacer (what spacing does today)",
            body=f"Заголовок абзаца.\n\n{FIRST}\n\n{NBSP}\n\n{SECOND}\n",
        ),
    )


def _fail(message: str, code: int = 2) -> int:
    print(f"FAIL: {message}", file=sys.stderr)
    return code


def _flatten(node: Any) -> str:
    """Flatten a ``RichText`` tree into a plain string."""

    if node is None:
        return ""
    if isinstance(node, str):
        return node
    parts = getattr(node, "texts", None)
    if parts:
        return "".join(_flatten(part) for part in parts)
    text = getattr(node, "text", None)
    if text is not None:
        return _flatten(text)
    # TextBr and friends carry no payload; name them so a break is visible.
    return f"<{type(node).__name__}>"


def _describe_rich_message(message: Any) -> str:
    rich = getattr(message, "rich_message", None)
    if rich is None:
        return "    (no rich_message on the read-back message)"
    lines: list[str] = []
    for block in list(getattr(rich, "blocks", None) or []):
        lines.append(f"      {type(block).__name__}: {_flatten(getattr(block, 'text', None))!r}")
    return "\n".join(lines) or "      (no blocks)"


async def _try_candidate(client: Any, *, peer: Any, candidate: Candidate) -> bool:
    from telethon.tl import functions, types

    request = functions.messages.SendMessageRequest(
        peer=peer,
        message="",
        rich_message=types.InputRichMessageMarkdown(markdown=candidate.body),
    )
    print(f"\n[{candidate.name}] {candidate.syntax}")
    print(f"  sent: {candidate.body!r}")
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
    if getattr(types, "InputRichMessageMarkdown", None) is None:
        return _fail("This Telethon build has no InputRichMessageMarkdown (layer < 227).")
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
            print(f"\n[{candidate.name}] {candidate.syntax}\n{candidate.body!r}")
        print("\ndry-run: not sending")
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
    try:
        if not await client.is_user_authorized():
            return _fail(f"Session {manager.session_path} exists but is not authorized.")
        resolver = CachingEntityResolver(TelethonResolverBackend(client))
        try:
            resolved = await resolver.resolve(args.entity)
        except EntityError as exc:
            return _fail(f"Could not resolve {args.entity!r}: {exc}")
        print(f"resolved: chat_id={resolved.chat_id} title={resolved.title!r}")

        peer = await client.get_input_entity(resolved.chat_id)
        accepted = [
            candidate.name
            for candidate in candidates
            if await _try_candidate(client, peer=peer, candidate=candidate)
        ]
        print(f"\naccepted: {', '.join(accepted) if accepted else '(none)'}")
        return 0 if accepted else 3
    finally:
        await manager.disconnect()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--entity", default=DEFAULT_ENTITY, help="target chat reference")
    parser.add_argument("--config", default=None, help="path to config.yml")
    parser.add_argument("--only", default=None, help="comma-separated candidate names to try")
    parser.add_argument("--dry-run", action="store_true", help="print candidates, send nothing")
    return asyncio.run(_run(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
