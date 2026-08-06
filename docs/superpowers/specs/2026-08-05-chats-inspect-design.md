# `chats inspect` — read-only chat metadata

**Date:** 2026-08-05
**Status:** approved, not implemented

## Problem

Nothing in the project can answer "what is the auto-delete setting of chat
2305069221". `folders inspect` returns two fields per chat (`chat_id`,
`title`) by design — it builds its list from `InputPeer` ids without a
per-chat lookup — and `groups get-layout` reads one boolean. Everything else
Telegram knows about a chat (TTL, description, slow mode, member counts,
restrictions, our own admin rights) is unreachable without leaving the CLI,
which the operating skill forbids.

The immediate trigger is `ttl_period`, but a single command that answers "what
is this chat" is worth more than a one-field probe: the same two RPCs already
carry the answers to the questions that keep coming up (why does this chat
reject sends, how many members does it have, is it a forum, are we admin).

## Scope

**In:** one read-only CLI command, `chats inspect`, plus the domain module
behind it. Supergroups/channels, legacy basic groups, users and bots.

**Out:** writing any of it back (`set-ttl`, archive, description) — that is a
WRITE operation with its own dry-run and gate, and belongs to a separate
change. No folder-wide sweep: the caller loops over chat ids, same as
`members list --user`.

Delivery is two-phase by explicit request: **phase 1 is CLI-only** and gets
verified against the live account before **phase 2** adds HTTP and MCP.

## Architecture

A new package `src/telegram_assistant/chats/`, shaped like
`members/listing.py`: a READ op with no operation row, no idempotency key and
no `--dry-run`.

```
src/telegram_assistant/chats/
  __init__.py            # re-exports
  service.py             # ChatInfo, ChatInspectBackend, inspect_chat()
  telethon_backend.py    # TelethonChatInspectBackend
```

- `service.py` holds `ChatInfo` (frozen dataclass with `to_dict()`), the
  `ChatInspectBackend` protocol (`inspect_chat(*, chat_id: int, raw: bool) ->
  ChatInfo`) and `inspect_chat(*, backend, chat_id, raw=False,
  authorizer=None)`. The authorizer, when supplied, must grant `READ` on the
  chat, and is checked **before any Telegram call** — the payload carries the
  description, the member list size and the invite link, so a denied caller
  must not cost a round trip nor learn the chat exists.
- `telethon_backend.py` resolves the peer once via `get_entity`, dispatches on
  its type to `GetFullChannelRequest` / `GetFullChatRequest` /
  `GetFullUserRequest`, and maps the pair into `ChatInfo`. Two RPCs maximum,
  no dialog walk. The shallow half of a channel's fields is read from the
  `Channel` inside the Full response's own `chats` list rather than from the
  resolved entity — `forum_tabs` is a flag on `Channel` (flags2.19), not on
  `ChannelFull`, and `groups/telethon_backend.py::get_topics_layout` already
  resolves it that way, matching it by `full_chat.id`.
- The CLI adds a `chats` Typer group with one command, wired by a
  `_build_chat_inspect_backends(config_path)` helper mirroring
  `_build_member_list_backends`.

Splitting this into its own package rather than extending `groups/` is
deliberate: `groups/` is about creating and administering supergroups, while
this command answers for users and channels too. The package is also the
natural home for the chat-wide write ops that are explicitly out of scope now.

## CLI surface

```
telegram-assistant chats inspect \
  (--chat-id N | --chat-name TITLE | --entity REF) \
  [--folder-name NAME] [--folder-id N] [--raw] [--config PATH]
```

Exactly one chat reference, same rule and same wording as `members list`.
`--folder-name` / `--folder-id` only matter for `--chat-name` resolution and
default from `telegram.default_chat_folder`. Output is a single JSON object on
stdout via `json.dumps(payload, sort_keys=True, default=str)`.

## Payload

One flat shape for every chat kind, with `None` in the fields that do not
apply, so `jq .ttl_period` works regardless of what was inspected. Three
values are naturally nested objects: `my_admin_rights`,
`default_banned_rights`, and the notification settings, which are flattened
into `muted` / `muted_until`.

Common to all kinds:

`chat_id` (bare, no `-100` marker, matching `EntityRef.numeric_id`), `kind`
(`user` | `bot` | `basic_group` | `supergroup` | `channel`), `title`,
`username`, `usernames`, `about`, **`ttl_period`**, `pinned_message_id`,
`archived`, `muted`, `muted_until`, `has_scheduled`, `restricted`,
`restriction_reason`, `verified`, `scam`, `fake`, `is_creator`, `left`,
`created_at`, `invite_link`, `my_admin_rights`, `default_banned_rights`.

Supergroups and channels add: `is_forum`, `topics_layout` (`list` | `tabs`,
from `forum_tabs` — the same value `groups get-layout` reports), `broadcast`,
`megagroup`, `gigagroup`, `participants_count`, `admins_count`,
`kicked_count`, `banned_count`, `online_count`, `slowmode_seconds`,
`slowmode_next_send_date`, `linked_chat_id`, `migrated_from_chat_id`,
`hidden_prehistory`, `participants_hidden`, `antispam`,
`can_view_participants`, `can_view_stats`, `can_delete_channel`,
`can_set_username`, `join_to_send`, `join_request`, `requests_pending`,
`noforwards`, `unread_count`, `available_reactions`, `reactions_limit`,
`call_active`.

Legacy basic groups add: `participants_count`, `deactivated`,
`migrated_to_chat_id`, `call_active`, `noforwards`, `requests_pending`,
`available_reactions`, `reactions_limit` — `ChatFull` carries the last four
too, so they are not channel-only.

Users and bots add: `first_name`, `last_name`, `phone`, `is_bot`,
`is_deleted`, `is_premium`, `is_contact`, `is_mutual_contact`, `blocked`,
`common_chats_count`, `birthday`, `personal_channel_id`, `last_seen_status`.

`--raw` adds one extra key, `raw`, holding **both** serialized objects —
`{"entity": …, "full": …}` — because the two carry different halves of the
picture: `forum_tabs`, `restriction_reason` and the banned-rights defaults
live on the shallow `Channel`/`Chat`/`User`, everything else on the `*Full`.
It is an escape hatch for fields the curated set does not name — wallpapers,
themes, sticker sets, boost levels, star pricing, business hours, `stats_dc` —
so a newly interesting field can be read without shipping a release.
`access_hash` is stripped: it is a credential for impersonating the peer
reference, not metadata, and nothing downstream needs it.

The curated set is deliberately not "everything `ChannelFull` has": the raw
object carries 60+ fields that change with the Telegram layer, and pinning
tests to them would make a Telethon upgrade a test failure rather than a
feature.

## Access

`READ` on the chat, enforced in the domain layer on every surface, exactly
like `members list` (and unlike `folders inspect`, which is CLI-ungated). With
no `telegram.access` block configured the authorizer is the usual allow-all
no-op.

The invite link and a user's phone number are returned to any caller holding
`READ` — an explicit decision: `READ` on a chat already means the caller can
read its messages, so withholding the link buys little, and a separate
`--secrets` flag would be one more thing to forget. Revisit if MCP tokens
start being handed to third parties.

## Errors

The same ladder `members list` uses, so a domain rejection never reads as an
internal error:

| situation | exit |
|---|---|
| not exactly one of `--chat-id` / `--chat-name` / `--entity` | 2 |
| `FolderError` (folder or chat-by-name not found) | 2 |
| `EntityNotFoundError` / `AmbiguousEntityError` | 2 |
| `ChannelForbidden` / `ChatForbidden` — peer visible, Full unreachable; raised as `ValueError` naming the chat | 2 |
| `AccessDenied` | 3 |
| `FloodWaitError` and anything else → `chats inspect failed: <msg>` | 1 |

`FloodWaitError` is neither slept through nor retried: this is a one-shot read
with no queue behind it, so the operator decides when to try again.

## Testing

Three files, mirroring the `test_members_list*.py` trio:

- `tests/test_chats_inspect.py` — the service against a fake backend: the READ
  gate fires **before** any RPC (the fake records zero calls on denial), `raw`
  is passed through, `to_dict()` has the documented shape.
- `tests/test_chats_inspect_backend.py` — the Telethon adapter against a fake
  client: mapping for a forum supergroup, a broadcast channel, a legacy basic
  group, a user and a bot; plus one test asserting `access_hash` is absent
  even with `--raw`.
- `tests/test_cli_chats_inspect.py` — flag exclusivity (exit 2), JSON on
  stdout, `access denied` (exit 3), unresolvable entity (exit 2).

`tests/test_skill_inventory.py` fails until `SKILL.md` lists the new command —
that guard is the reason documentation is part of phase 1 rather than a
follow-up.

Live verification (read-only, so it needs no separate approval under the
project's e2e rule) closes phase 1: run the command against chat
`2305069221`, a channel, a private chat and a legacy group, one per mapping
branch.

## Phases

**Phase 1 — CLI only**

1. `chats/` package (`service.py`, `telethon_backend.py`, `__init__.py`) with
   `tests/test_chats_inspect.py` and `tests/test_chats_inspect_backend.py`.
2. `chats inspect` command in `cli/main.py` with
   `tests/test_cli_chats_inspect.py`.
3. `skills/telegram-assistant/SKILL.md` — catalog row, a `chats` / `inspect`
   extraction section, and a scenario; re-sync to
   `~/.claude/skills/telegram-assistant/SKILL.md`; command list in
   `README.md`.
4. Live read-only check across the four peer kinds.

**Phase 2 — remaining surfaces** (decided 2026-08-05, after the phase-1 output
was reviewed)

- HTTP `GET /telegram/chats/inspect`, served through a
  `chat_inspect_backend_factory` on `app.state` that returns `None` (→ 503)
  until the Telethon client is connected.
- MCP tool `telegram_chats_inspect`, plus `EXPECTED_TOOL_NAMES` in
  `tests/test_mcp_mount.py` and the tool catalog in `README.md`.
- `tests/test_chats_inspect_surfaces.py`.

Four decisions settle the places where `members list` is a poor model:

- **Chat reference:** the remote surfaces take the *same* set the CLI does —
  `entity`, `chat_id`, or `chat_name` with `folder_name` / `folder_id` —
  rather than `members list`'s narrower `chat_id`/`entity` pair. `messages
  edit` and `messages pin` already resolve a name over HTTP, so the precedent
  exists, and a surface that cannot address what its own CLI sibling can is a
  gap nobody would defend later.
- **`raw` is CLI-only.** The remote surfaces still *accept* the parameter and
  reject it with 400 / a tool error naming the reason, rather than ignoring it
  — a silently dropped `raw=true` would look like an empty raw payload. The
  curated set is designed to be enough; `raw` carries considerably more (a
  legacy group's whole member roster via `ChatFull.participants`, a user's
  `business_location`, `stories` and `personal_channel_id`), and the project
  already keeps local-only capabilities off the remote surfaces for this
  reason — `scan_media` resolves server-side paths for the CLI alone, and
  `messages download --out` is unconfined only there.
- **`FloodWaitError` is mapped**, not left to fall through as `members list`
  leaves it: HTTP answers **502** with `Retry-After` and a body carrying
  `retry_after_seconds`, MCP reports `needs_review` with the same field. The
  adapter already translates flood-waits at four call sites, so an unmapped
  one would surface as Starlette's empty 500 and tell the caller nothing about
  waiting. This reuses the mapping `messages pin`/`unpin` established.
- **`raw` never reaches the domain call** from a remote surface: the routes
  pass `raw=False` after the rejection above, so there is no path where a
  remote caller's flag reaches `_serialize`.
