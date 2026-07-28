# Design: skip unresolvable `chat:` access rules with a warning

**Date:** 2026-07-28
**Status:** approved, ready for planning

## Problem

A stale `chat:` rule in `telegram.access` breaks **every** gated command.

Symptom: with `- chat: expertizemeAssistant` in `data/config.yml`, running
`telegram-assistant messages recent --entity @wyrtensi` fails with
`no entity found for reference 'expertizemeAssistant'` and exit 2 — even though
`@wyrtensi` has its own granting rule and the requested chat resolves fine.

Cause: `Authorizer._ensure_index()` (`src/telegram_assistant/access/service.py`,
the `for ref in refs:` loop) awaits `self._resolver.resolve(ref)` with no guard.
`EntityNotFoundError` / `AmbiguousEntityError` propagate out of the index build
and abort the whole command. One dead rule takes the entire policy with it.

### Why `access list` / `access check` looked different

`access list` (`cli/main.py`) never builds the index — it prints
`config.telegram.access.rules` verbatim and constructs no resolver at all, so it
was never affected. `access check` **does** build the index (via
`authorizer.describe()`) and therefore fails identically to `messages recent`.
There is no behavioural divergence to reconcile beyond this fix; `access list`
stays a pure, offline config dump.

## Design

### 1. Per-ref skip in `_ensure_index`

Wrap the single `await self._resolver.resolve(ref)` in `try/except`. On failure:
log a warning, record the ref, `continue` to the next ref.

Granularity is **per-ref, not per-rule**: a rule may carry
`chats: [a, b, c]`, and a dead `b` must not cost `a` and `c` their grants. The
existing loop already iterates refs, so this is the natural seam.

### 2. Caught exception: `EntityError` only

Catch `telegram_assistant.entities.service.EntityError` — the base class of
`EntityNotFoundError` and `AmbiguousEntityError`.

Deliberately **not** `except Exception`. `TelethonResolverBackend` swallows
benign lookup failures (returns `None`) and re-raises only a *translated*
`FloodWaitError` through this call path. Swallowing that would silently deny a
chat during a throttle instead of letting the worker queue pause-and-retry —
turning a transient throttle into a wrong authorization decision.

Import `EntityError` from `telegram_assistant.entities.service` directly (not
the package `__init__`, which pulls in `telethon_backend`), keeping the domain
layer free of Telethon imports.

### 3. Fail-safe direction

Skipping a ref **narrows** rights under deny-by-default: an unresolved ref
grants nothing. The worst case is a false 403, which is loud and traceable. The
alternative — guessing an id, or keeping the rule as a wildcard — would silently
*widen* access, which is not acceptable for a security gate.

### 4. Visibility

Two levels, both required (a silent narrowing of rights is the failure mode this
whole change must avoid):

1. **Warning log** at index-build time:
   `_log.warning("access_rule_ref_unresolved", ref=…, error=…, permissions=…)`.
   Structlog renders JSON to stderr; WARNING passes the default INFO level, so
   it appears on every CLI run and in server logs. Fires once per authorizer
   (the index is built once) — i.e. once per CLI process, once per HTTP request.

2. **Recorded on the authorizer**: `unresolved_refs`, a read-only property
   returning `tuple[UnresolvedAccessRef, ...]` where `UnresolvedAccessRef` is a
   frozen dataclass of `ref: str` and `error: str`. Populated into a local list
   during the build and assigned at the end alongside the other index maps, so a
   partial build never leaves half-populated state.

3. **`access check` payload** gains an `unresolved_refs` key: a sorted list of
   `{"ref": …, "error": …}` objects. The diagnostic command names the broken
   rules directly instead of leaving the operator to grep stderr.

### 5. Deliberately unchanged

- **`access list`** stays offline and pure-config. Resolving there would require
  a live authorized Telethon session for what is currently a `--help`-cheap
  config dump.
- **HTTP / MCP payloads** are untouched. The warning already reaches server
  logs; attaching diagnostics to every gated response is noise.
- **`AccessDenied`** message and fields are unchanged.
- **Folder rules** (`folder:` / `folder_id:`) are not resolved through the
  entity resolver and need no equivalent handling.
- The existing `RuntimeError` for "chat rules present but no resolver injected"
  stays a hard error — that is a wiring bug, not a stale config entry.

## Testing

`tests/test_access.py`:

- an unresolvable `chat:` ref → the index builds, other rules still gate
  correctly, and the bad ref grants nothing
- mixed `chats: [good, bad]` → `good` keeps its grant, `bad` is skipped
- a `FloodWaitError` raised by the resolver still propagates (the invariant the
  narrow `except EntityError` exists to protect)
- the warning is emitted carrying the ref text and the reason
- `unresolved_refs` reports exactly the skipped refs

`tests/test_cli_access.py`:

- `access check` payload carries `unresolved_refs` when a rule ref is dead, and
  the command still exits 0/3 on the grant decision rather than 2

## Documentation

- `CLAUDE.md` — one sentence in the `telegram.access` paragraph recording the
  skip-with-warning behaviour and the fail-safe direction
- `README.md` — the `access check` bullet gains the `unresolved_refs` field
- `skills/telegram-assistant/SKILL.md` — the `access check` row, then re-sync to
  `~/.claude/skills/telegram-assistant/SKILL.md`
- `docs/TODO.md` — mark the item done
