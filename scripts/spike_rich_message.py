#!/usr/bin/env python3
"""Spike: send a Telegram rich message (article) via raw MTProto.

Answers the blocking questions of the rich-message plan
(``docs/plans/20260726-rich-message-send.md``, Task 1):

1. Does the server accept ``InputRichMessageMarkdown`` from *this* (non-Premium)
   technical user account, or does it demand Premium?
2. What do success and failure look like — the shape of the returned ``Updates``
   (so the backend knows how to extract a message id) and the exact RPC error
   names for the taxonomy.
3. Which parts of the documented markdown dialect survive the MTProto path,
   including media-by-URL (``![](https://… "caption")``).

This is a *spike*, not part of the shipped surface: it talks to the real
account, so it lives next to ``scripts/e2e_*.sh`` and shares their precondition
of an authorized Telethon session. It sends one real message, by default to
Saved Messages.

Usage::

    .venv/bin/python scripts/spike_rich_message.py
    .venv/bin/python scripts/spike_rich_message.py --entity "Client chat test"
    .venv/bin/python scripts/spike_rich_message.py --dry-run   # no send

Exit codes: 0 = sent (or dry run), 2 = precondition missing (no session, no
config, Telethon too old), 3 = the server rejected the send (RPC error).
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC = REPO_ROOT / "src"
if str(SRC) not in sys.path:  # allow running without an editable install
    sys.path.insert(0, str(SRC))

# Saved Messages, not the usual e2e chat: "Client chat test" currently rejects
# *every* send (plain and rich alike) with ChatRestrictedError, so it cannot
# tell a rich-message failure apart from a chat-level one.
DEFAULT_ENTITY = "me"

# Covers the dialect documented for the Bot API twin (headings, task list,
# aligned table, quote, fenced code, rule, strike/mark/spoiler) plus one
# media-by-URL block, which is the v1 answer to "inline media without
# InputRichFile".
SAMPLE_MARKDOWN = """# Rich message spike

Sent by `scripts/spike_rich_message.py` to probe the MTProto rich-message path.

## Dialect probes

- plain bullet
- ~~strike~~, ==marked==, ||spoiler||
- [ ] unchecked task
- [x] checked task

| Feature | Status | Notes |
|:--------|:------:|------:|
| heading | ok? | h1..h6 |
| table | ok? | aligned |
| quote | ok? | see below |

> A block quote.
> Second line of the same quote.

```python
def hello() -> str:
    return "fenced code with a language tag"
```

---

![](https://telegram.org/img/t_logo.png "media by public URL")

Trailing paragraph after the media block.
"""


def _fail(message: str, code: int = 2) -> int:
    print(f"FAIL: {message}", file=sys.stderr)
    return code


def _describe(obj: Any, indent: int = 0) -> str:
    """Render the Updates result shallowly enough to read message ids off it."""
    pad = "  " * indent
    name = type(obj).__name__
    interesting = ("id", "peer_id", "date", "message", "pts", "random_id")
    fields = []
    for attr in interesting:
        if hasattr(obj, attr):
            value = getattr(obj, attr)
            if attr == "message" and not isinstance(value, (str, int, type(None))):
                value = f"<{type(value).__name__}>"
            elif isinstance(value, str) and len(value) > 60:
                value = value[:60] + "…"
            fields.append(f"{attr}={value!r}")
    line = f"{pad}{name}" + (f" ({', '.join(fields)})" if fields else "")
    lines = [line]
    for attr in ("updates", "update"):
        child = getattr(obj, attr, None)
        if isinstance(child, list):
            lines.extend(_describe(item, indent + 1) for item in child)
        elif child is not None:
            lines.append(_describe(child, indent + 1))
    return "\n".join(lines)


def _extract_message_id(result: Any) -> int | None:
    """Mirror the id-extraction the backend will need."""
    updates = getattr(result, "updates", None)
    candidates = updates if isinstance(updates, list) else [result]
    # UpdateMessageID carries the id for the random_id we just sent; the
    # UpdateNew*Message variants carry the full Message object.
    for update in candidates:
        if type(update).__name__ == "UpdateMessageID":
            return getattr(update, "id", None)
    for update in candidates:
        if type(update).__name__ in ("UpdateNewMessage", "UpdateNewChannelMessage"):
            message = getattr(update, "message", None)
            message_id = getattr(message, "id", None)
            if message_id is not None:
                return int(message_id)
    return getattr(result, "id", None)


async def _run(args: argparse.Namespace) -> int:
    try:
        from telethon.tl import functions, types
    except ImportError as exc:  # pragma: no cover - spike script
        return _fail(f"Telethon is not importable: {exc}")

    rich_type = getattr(types, "InputRichMessageMarkdown", None)
    if rich_type is None:
        return _fail(
            "This Telethon build has no InputRichMessageMarkdown "
            "(layer < 227). Install telethon>=1.44."
        )
    if "rich_message" not in functions.messages.SendMessageRequest.__init__.__annotations__:
        return _fail("SendMessageRequest has no rich_message parameter. Install telethon>=1.44.")

    from telegram_assistant.config.loader import ConfigError, load_config
    from telegram_assistant.entities.service import CachingEntityResolver, EntityError
    from telegram_assistant.entities.telethon_backend import TelethonResolverBackend
    from telegram_assistant.telegram_client.session import TelethonSessionManager

    try:
        config = load_config(args.config)
    except ConfigError as exc:
        return _fail(str(exc))

    markdown = SAMPLE_MARKDOWN
    if args.markdown_file:
        markdown = Path(args.markdown_file).read_text(encoding="utf-8")

    print(f"markdown: {len(markdown)} chars, {len(markdown.splitlines())} lines")
    print(f"session:  {config.telegram.session_path}")
    print(f"target:   {args.entity}")

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
        premium = getattr(me, "premium", None)
        print(f"account:  id={getattr(me, 'id', None)} premium={premium}")

        resolver = CachingEntityResolver(TelethonResolverBackend(client))
        try:
            resolved = await resolver.resolve(args.entity)
        except EntityError as exc:
            return _fail(f"Could not resolve {args.entity!r}: {exc}")
        print(f"resolved: chat_id={resolved.chat_id} title={resolved.title!r}")

        if args.dry_run:
            print("dry-run: not sending")
            return 0

        peer = await client.get_input_entity(resolved.chat_id)
        request = functions.messages.SendMessageRequest(
            peer=peer,
            message="",
            rich_message=rich_type(markdown=markdown),
        )
        try:
            result = await client(request)
        except Exception as exc:  # noqa: BLE001 - the taxonomy is the finding
            print(f"RPC ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
            code = getattr(exc, "code", None)
            message = getattr(exc, "message", None)
            if code is not None or message is not None:
                print(f"           code={code} message={message}", file=sys.stderr)
            return 3

        print("\nresult tree:")
        print(_describe(result))
        message_id = _extract_message_id(result)
        print(f"\nmessage_id: {message_id}")
        return 0
    finally:
        await manager.disconnect()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--entity", default=DEFAULT_ENTITY, help="target chat reference")
    parser.add_argument("--config", default=None, help="path to config.yml")
    parser.add_argument("--markdown-file", default=None, help="send this file instead of the sample")
    parser.add_argument("--dry-run", action="store_true", help="resolve only, do not send")
    return asyncio.run(_run(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
