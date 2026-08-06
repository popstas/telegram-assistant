# `chats set-ttl` — write a chat's auto-delete period

**Date:** 2026-08-06
**Status:** approved, not implemented

## Problem

`chats inspect` reports `ttl_period` but nothing can change it. The gap was
found the hard way: disabling auto-delete across the 78-chat `Агентства`
folder on 2026-08-05 needed an ad-hoc `messages.SetHistoryTTL` script run
outside the CLI, bypassing `telegram.access`, `OperationStore` and
`--dry-run`. The `chats inspect` spec deliberately left this out of scope as
"a WRITE operation with its own dry-run and gate, belonging to a separate
change" — this is that change.

That run also produced three facts this design is built on, none of them
guessable from the API docs:

1. **`SetHistoryTTL` is flood-waited hard, and the waits escalate.** Observed
   pauses on one account within one hour: 261s, 703s, 866s.
2. **The RPC's return value cannot be trusted.** The last chat failed with
   Telethon's `TypeNotFoundError` (constructor `e4e0b29d`, newer than the
   installed layer) — yet the write had applied. Only a read-back showed it.
3. **Every successful set posts a service message into the chat**, visible to
   all members, including a set that changes nothing.

## Scope

**In:** one CLI command, `chats set-ttl`, plus the domain module behind it.
Any peer `chats inspect` handles — supergroups, channels, legacy basic
groups, users.

**Out:** HTTP and MCP, by explicit request — CLI only. The domain layer stays
surface-agnostic, so adding them later is wiring, not redesign. Also out: a
folder-wide sweep; the caller loops over chats, as with `members list --user`.

## CLI surface

```
telegram-assistant chats set-ttl \
  (--chat-id N | --chat-name TITLE | --entity REF) \
  --ttl off|<duration> \
  [--folder-name NAME] [--folder-id N] [--dry-run] [--config PATH]
```

Exactly one chat reference, same rule and same wording as `chats inspect`.
`--folder-name` / `--folder-id` matter only for `--chat-name` resolution and
default from `telegram.default_chat_folder`.

`--ttl` accepts:

- `off` or `0` → period `0` (auto-delete disabled)
- `<integer><unit>` with unit `s` | `m` | `h` | `d` | `w` (e.g. `1d`, `93d`,
  `24h`, `2w`)
- a bare integer → seconds

Rejected at parse time with exit 2: negative values, non-integer values, an
unknown unit, an empty string, and anything exceeding `2**31 - 1` seconds
(the wire field is a 32-bit int). There is deliberately **no** allow-list of
preset durations: Telegram's clients offer only day/week/month, but the
`Агентства` folder held live chats at 31, 93 and 180 days, so arbitrary
periods clearly pass. The server is the authority on what it accepts; a
rejection surfaces as its own error rather than being pre-empted by a guess.

Output is a single JSON object on stdout via
`json.dumps(payload, sort_keys=True, default=str)`:

`chat_id` (bare, no `-100` marker), `title`, `requested_ttl_seconds`,
`previous_ttl_seconds`, `ttl_period` (the value read back from the server —
the authoritative one), `changed`, `dry_run`.

`previous_ttl_seconds` and `ttl_period` are `null` when auto-delete is off,
never `0` — that is what `chats inspect` already reports for the same field,
and the two commands must not disagree about the same chat. `off` therefore
means "make `ttl_period` null", so `--ttl off` against a chat that already
has no TTL is a no-op (`changed: false`, no write, no service message).

## Architecture

A new module `src/telegram_assistant/chats/ttl.py` beside the existing
`service.py`, mirroring how `members/` splits `listing.py` out of
`service.py`: one operation per file, READ and WRITE not mixed.

```
src/telegram_assistant/chats/
  service.py             # unchanged — ChatInfo, inspect_chat (READ)
  ttl.py                 # NEW — parse_ttl, SetTtlRequest/Result, set_chat_ttl
  telethon_backend.py    # TelethonChatInspectBackend + TelethonChatTtlBackend
```

`ttl.py` holds:

- `parse_ttl(value: str) -> int` — pure, no I/O, raises `ValueError` with the
  offending text. Tested as a table.
- `ChatTtlBackend` protocol:
  `set_ttl(*, chat_id: int, period: int) -> None` and
  `get_ttl(*, chat_id: int) -> int | None`.
  `get_ttl` is deliberately narrow rather than reusing `inspect_chat`: the
  full payload costs peer-kind dispatch, serialization and `access_hash`
  redaction for one field, and a fake for it in tests would have to be a
  whole `ChatInfo`.
- `SetTtlRequest` / `SetTtlResult`, frozen dataclasses; `SetTtlResult` has
  `to_dict()` producing the payload above.
- `set_chat_ttl(*, backend, chat_id, period, chat_name=None, authorizer=None,
  pacer=None, dry_run=False) -> SetTtlResult`.

`TelethonChatTtlBackend` sits at the bottom of `chats/telethon_backend.py`,
resolves the peer once, and dispatches on its type exactly as the inspect
adapter does — `get_ttl` reads `full_chat.ttl_period` from
`GetFullChannel` / `GetFullChat` / `GetFullUser`; `set_ttl` issues
`messages.SetHistoryTTLRequest(peer=…, period=…)`.

## Order of operations

`set_chat_ttl` runs, in this order:

1. **WRITE gate** — `authorizer.require(chat_id, AccessLevel.WRITE)`, before
   any Telegram call. A denied caller costs no round trip and learns nothing
   about the chat.
2. **Read the current period** via `get_ttl`.
3. **No-op short-circuit.** If the current period already equals the
   requested one, return with `changed: false` **without writing**. This is a
   correctness requirement, not an optimization: Telegram posts a service
   message on every successful `SetHistoryTTL`, including one that changes
   nothing, so a re-run over a folder would otherwise spam every chat in it.
4. **`--dry-run`** returns here, reporting `previous_ttl_seconds`, the
   `changed` it would produce, and `ttl_period` equal to the current value.
5. **Write**, through the pacer (below).
6. **Read back** via `get_ttl`, unconditionally. The reported `ttl_period` is
   this value, never the requested one.
7. **Mismatch is an error.** If the read-back differs from the requested
   period, raise `ValueError` naming both. A server that silently clamped or
   rejected the value must not be reported as success.

No `OperationStore` row and no idempotency key: this is a single-step write
with a naturally idempotent target, shaped like `notifications mute`.

## Pacing and flood waits

The write goes through `messages/pacing.py`'s `Pacer` with key
`ttl:<EntityRef.numeric_id>` — its own row in the shared `RateGateStore`, not
shared with the pin gate, since these are different Telegram limits. Keying
on the bare id (not the `-100`-marked form) matches `pin_pacing_key` and
keeps one gate row per chat.

Two new config keys under `telegram`:

- `ttl_min_interval_seconds` — default `2.0`, `0` disables. The shared
  minimum interval between two TTL writes on one chat, honoured across CLI
  processes.
- `ttl_max_flood_wait_seconds` — default `3600`. High enough to sit through
  the observed 866s waits (the decision was "wait as long as it takes"), but
  finite so a stuck call cannot hang forever unnoticed.
- `ttl_max_flood_wait_retries` — default `5`. `Pacer`'s own default is `3`,
  which is too few here: the waits escalate, so a single chat can plausibly
  spend two or three of them before succeeding, and exhausting the count
  reports a flood wait as a failure on a call that was about to work.

Both caps bind independently: the pacer gives up when either the retry count
is exhausted or one requested wait exceeds `ttl_max_flood_wait_seconds`.

Every flood-wait pause logs a `WARNING` naming the chat and the wait — a
15-minute silent process is indistinguishable from a hang. Exhausting the cap
raises `PacedFloodWaitError`, whose `retry_after_seconds` reaches the
operator.

**`TypeNotFoundError` during the write is not a failure.** It means Telethon
could not parse the response, not that the write failed — proven live. The
adapter catches it and falls through to the read-back, which decides. Any
other `RPCError` propagates.

## Access

WRITE on the chat, enforced in the domain layer. With no `telegram.access`
block configured the authorizer is the usual allow-all no-op. Note that
`write` does **not** imply `read` in this project's policy, and this command
needs only WRITE — the `get_ttl` calls are part of the write operation, not a
separate READ grant.

## Errors

| situation | exit |
|---|---|
| not exactly one of `--chat-id` / `--chat-name` / `--entity` | 2 |
| unparseable `--ttl` | 2 |
| `FolderError` (folder or chat-by-name not found) | 2 |
| `EntityNotFoundError` / `AmbiguousEntityError` | 2 |
| `ChannelForbidden` / `ChatForbidden`, raised as `ValueError` naming the chat | 2 |
| read-back disagrees with the requested period | 2 |
| `AccessDenied` | 3 |
| `PacedFloodWaitError` (cap exhausted) — message carries `retry_after_seconds` | 1 |
| anything else → `chats set-ttl failed: <msg>` | 1 |

## Testing

Three files, mirroring the `test_chats_inspect*.py` trio:

- `tests/test_chats_set_ttl.py` — the service against a fake backend:
  `parse_ttl` as a table (valid forms, every rejection), the WRITE gate fires
  before any RPC (the fake records zero calls on denial), the no-op
  short-circuit issues no write, `--dry-run` writes nothing, the read-back
  value wins over the requested one, a mismatch raises, and a backend raising
  `TypeNotFoundError` still succeeds when the read-back confirms.
- `tests/test_chats_set_ttl_backend.py` — the Telethon adapter against a fake
  client: peer-kind dispatch for `get_ttl`, the `SetHistoryTTLRequest`
  arguments, and `TypeNotFoundError` tolerance.
- `tests/test_cli_chats_set_ttl.py` — flag exclusivity (exit 2), `--ttl`
  parsing failures (exit 2), JSON on stdout, `--dry-run` payload shape,
  access denied (exit 3).

`skills/telegram-assistant/SKILL.md` (catalog row, extraction section,
scenario), the re-sync to `~/.claude/skills/telegram-assistant/SKILL.md`, and
the command list in `README.md` are part of the change, not a follow-up —
`tests/test_skill_inventory.py` fails until the catalog matches.

Live verification is **mutating** and therefore needs explicit human approval
under the project's e2e rule. The proposed check, once approved, is Saved
Messages (`me`) only: set a short TTL, read it back, set `off`, read back,
re-run `off` to confirm the no-op short-circuit issues no write and posts no
service message.
