#!/usr/bin/env python3
"""Spike: keep an ``&`` inside a link URL intact in a rich article.

A note written in Obsidian carries ordinary query-string links::

    [Конкурсы и ассоциации (235)](https://example.com/?action=handbooklist&handbook=235)

Sent as an article the link arrives **broken**: the ``&`` comes back
HTML-escaped as ``&amp;``, so the target server sees a parameter named
``amp;handbook`` instead of ``handbook``. Nothing on our side rewrites it —
``normalize_rich_markdown`` leaves the text byte-for-byte — so the escaping
happens inside Telegram's own server-side markdown parser.

This spike asks which spelling of ``&`` survives that parser. It sends **one**
article to Saved Messages carrying one paragraph per candidate, reads the
article back through ``messages.getRichMessage`` (the message's own
``rich_message`` is a truncated ``part=True`` preview), and prints the
``TextUrl.url`` the server actually stored for each one. A candidate is a
**pass** when the stored URL equals the URL the note meant.

It is a *spike*, not part of the shipped surface: it talks to the real account
and shares ``scripts/spike_rich_media.py``'s precondition of an authorized
Telethon session.

Usage::

    .venv/bin/python scripts/spike_rich_link_escaping.py
    .venv/bin/python scripts/spike_rich_link_escaping.py --dry-run
    .venv/bin/python scripts/spike_rich_link_escaping.py --entity me

Exit codes: 0 = the article was accepted (or dry run), 2 = precondition
missing (no session, no config, Telethon too old), 3 = the server rejected the
send.
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

# The shape that breaks: two query parameters joined by a bare ``&``.
WANTED = "https://example.com/?action=handbookdataview&handbook=235&key=269"


@dataclass(frozen=True)
class Candidate:
    """One markdown spelling of a URL carrying a bare ``&``."""

    name: str
    syntax: str
    markdown: str
    # The URL the note meant. A candidate passes when the server stored this.
    wanted: str = WANTED


def build_candidates() -> tuple[Candidate, ...]:
    escaped = WANTED.replace("&", "&amp;")
    backslashed = WANTED.replace("&", "\\&")
    percent = WANTED.replace("&", "%26")
    return (
        Candidate(
            name="plain",
            syntax="bare & in a markdown link (what a note writes today)",
            markdown=f"[plain]({WANTED})",
        ),
        Candidate(
            name="entity",
            syntax="&amp; entity in the destination",
            markdown=f"[entity]({escaped})",
        ),
        Candidate(
            name="backslash",
            syntax="backslash-escaped \\& in the destination",
            markdown=f"[backslash]({backslashed})",
        ),
        Candidate(
            name="angle-dest",
            syntax="CommonMark pointy-bracket destination <...>",
            markdown=f"[angle-dest](<{WANTED}>)",
        ),
        Candidate(
            name="autolink",
            syntax="CommonMark autolink <https://...>",
            markdown=f"<{WANTED}>",
        ),
        Candidate(
            name="bare-url",
            syntax="bare URL, no link syntax at all (autodetected)",
            markdown=WANTED,
        ),
        Candidate(
            name="html-a",
            syntax="inline <a href> inside the markdown dialect",
            markdown=f'<a href="{WANTED}">html-a</a>',
        ),
        Candidate(
            name="percent",
            syntax="%26 instead of & (changes the target's parsing)",
            markdown=f"[percent]({percent})",
        ),
    )


def build_char_candidates() -> tuple[Candidate, ...]:
    """One link per character that the parser might mangle inside a URL.

    ``&`` is the known-broken baseline; the rest are the characters an Obsidian
    note plausibly carries in a query string, plus the two markdown emphasis
    markers, which a naive destination scanner could also eat.
    """

    probes = (
        ("amp", "&"),
        ("lt", "<"),
        ("gt", ">"),
        ("dquote", '"'),
        ("squote", "'"),
        ("plus", "+"),
        ("percent20", "%20"),
        ("hash", "#"),
        ("tilde", "~"),
        ("pipe", "|"),
        ("underscore", "_"),
        ("asterisk", "*"),
        ("cyrillic", "тест"),
    )
    return tuple(
        Candidate(
            name=name,
            syntax=f"URL containing {char!r}",
            markdown=f"[{name}](https://example.com/?q=a{char}b)",
            wanted=f"https://example.com/?q=a{char}b",
        )
        for name, char in probes
    )


def build_article(candidates: tuple[Candidate, ...]) -> str:
    lines = ["# Link escaping spike", ""]
    for candidate in candidates:
        lines.append(f"{candidate.name}: {candidate.markdown}")
        lines.append("")
    return "\n".join(lines)


def _fail(message: str, code: int = 2) -> int:
    print(f"FAIL: {message}", file=sys.stderr)
    return code


def _iter_urls(node: Any) -> list[tuple[str, str]]:
    """Collect every ``(anchor text, url)`` pair in a ``RichText`` tree."""

    if node is None or isinstance(node, str):
        return []
    found: list[tuple[str, str]] = []
    url = getattr(node, "url", None)
    if url is not None:
        found.append((_flatten(getattr(node, "text", None)), url))
    for part in list(getattr(node, "texts", None) or []):
        found.extend(_iter_urls(part))
    inner = getattr(node, "text", None)
    if inner is not None and not isinstance(inner, str):
        found.extend(_iter_urls(inner))
    return found


def _flatten(node: Any) -> str:
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
    return ""


def _report(rich: Any, candidates: tuple[Candidate, ...]) -> None:
    by_name = {candidate.name: candidate for candidate in candidates}
    print(f"\nread-back: part={getattr(rich, 'part', None)!r}")
    seen: dict[str, list[str]] = {}
    for block in list(getattr(rich, "blocks", None) or []):
        text = _flatten(getattr(block, "text", None))
        for anchor, url in _iter_urls(getattr(block, "text", None)):
            name = text.split(":", 1)[0].strip()
            seen.setdefault(name if name in by_name else anchor, []).append(url)

    print()
    for candidate in candidates:
        urls = seen.get(candidate.name) or []
        if not urls:
            print(f"  [{candidate.name:<10}] NO LINK   ({candidate.syntax})")
            continue
        for url in urls:
            verdict = "PASS" if url == candidate.wanted else "FAIL"
            suffix = "" if verdict == "PASS" else f"   (wanted {candidate.wanted})"
            print(f"  [{candidate.name:<10}] {verdict}      {url}{suffix}")


async def _run(args: argparse.Namespace) -> int:
    try:
        from telethon.tl import functions, types
    except ImportError as exc:  # pragma: no cover - spike script
        return _fail(f"Telethon is not importable: {exc}")
    if getattr(types, "InputRichMessageMarkdown", None) is None:
        return _fail("This Telethon build has no InputRichMessageMarkdown (layer < 227).")
    if "rich_message" not in functions.messages.SendMessageRequest.__init__.__annotations__:
        return _fail("SendMessageRequest has no rich_message parameter. Install telethon>=1.44.")

    candidates = build_char_candidates() if args.mode == "chars" else build_candidates()
    article = build_article(candidates)

    if args.dry_run:
        print(article)
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
        request = functions.messages.SendMessageRequest(
            peer=peer,
            message="",
            rich_message=types.InputRichMessageMarkdown(markdown=article),
        )
        try:
            result = await client(request)
        except Exception as exc:  # noqa: BLE001 - the taxonomy is the finding
            print(f"REJECTED: {type(exc).__name__}: {exc}")
            return 3

        from telegram_assistant.messages.telethon_backend import _extract_rich_message_id

        message_id = _extract_rich_message_id(
            result, random_id=getattr(request, "random_id", None)
        )
        print(f"ACCEPTED: message_id={message_id}")
        if message_id is None:
            print("(no readable message id — cannot read the article back)")
            return 0

        full = await client(functions.messages.GetRichMessageRequest(peer=peer, id=message_id))
        message = list(getattr(full, "messages", None) or [None])[0]
        rich = getattr(message, "rich_message", None)
        if rich is None:
            print("(read-back carried no rich_message)")
            return 0
        _report(rich, candidates)
        return 0
    finally:
        await manager.disconnect()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--entity", default=DEFAULT_ENTITY, help="target chat reference")
    parser.add_argument("--config", default=None, help="path to config.yml")
    parser.add_argument(
        "--mode",
        choices=("escaping", "chars"),
        default="escaping",
        help="escaping: spellings of & in one URL; chars: one URL per suspect character",
    )
    parser.add_argument("--dry-run", action="store_true", help="print the article, send nothing")
    return asyncio.run(_run(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
