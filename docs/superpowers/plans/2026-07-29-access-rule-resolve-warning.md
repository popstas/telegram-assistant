# Access-Rule Resolve Warning Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A stale or ambiguous `chat:` rule in `telegram.access` must be skipped with a warning instead of aborting every gated command.

**Architecture:** `Authorizer._ensure_index()` currently awaits `self._resolver.resolve(ref)` for every `chat:` rule ref with no guard, so one `EntityNotFoundError` kills the whole index build. The fix wraps that single await in `try/except EntityError`, logs a structured warning, records the ref on the authorizer, and continues with the next ref. `access check` then reports the recorded refs in its JSON payload. Skipping only ever narrows rights under deny-by-default, so the worst case is a loud 403, never a silent grant.

**Tech Stack:** Python 3.12, structlog (JSON to stderr), Typer CLI, pytest + pytest-asyncio (asyncio mode auto), ruff.

## Global Constraints

- Use the project venv: `source .venv/bin/activate` (or call `.venv/bin/pytest` / `.venv/bin/ruff` directly).
- Lint must pass: `ruff check src tests` (line-length 100, py312, ignores E501).
- The exception caught around rule resolution is **`EntityError` only** — never `except Exception`. `TelethonResolverBackend` re-raises a translated `worker.queue.FloodWaitError` through this call path; swallowing it would turn a transient throttle into a wrong authorization decision.
- Import `EntityError` from `telegram_assistant.entities.service`, **not** from the `telegram_assistant.entities` package `__init__` — the package init imports `telethon_backend`, and the domain layer must stay free of Telethon imports.
- No Telegram traffic in tests; all tests use in-memory fakes.
- `access list` stays offline and pure-config. Do not add resolution to it.
- HTTP and MCP payloads are not touched by this change.

---

### Task 1: Skip unresolvable `chat:` rule refs in the authorizer

**Files:**
- Modify: `src/telegram_assistant/access/service.py` (imports at top; `Authorizer.__init__` ~line 189; `_ensure_index` ref loop ~lines 277-287 and the assignment block ~lines 288-300; `__all__` ~line 717)
- Modify: `src/telegram_assistant/access/__init__.py`
- Modify: `CLAUDE.md` (the `telegram.access` paragraph, line 89)
- Test: `tests/test_access.py` (append a new section at the end of the file)

**Interfaces:**
- Consumes: `telegram_assistant.entities.service.EntityError`; the existing `_log` structlog logger in `access/service.py`.
- Produces:
  - `telegram_assistant.access.UnresolvedAccessRef` — frozen dataclass with fields `ref: str`, `error: str` and a method `to_dict(self) -> dict[str, str]` returning `{"ref": ..., "error": ...}`.
  - `Authorizer.unresolved_refs` — read-only property returning `tuple[UnresolvedAccessRef, ...]`. Empty until the index is built (lazily, on the first `require` / `describe` / `allows` call). Task 2 reads this.
  - Log event name `"access_rule_ref_unresolved"` with keys `ref`, `error`, `permissions`.

- [ ] **Step 1: Write the failing tests**

Add to the top-level imports of `tests/test_access.py` — change the existing entities import line:

```python
from telegram_assistant.entities import EntityNotFoundError, ResolvedEntity
```

and the existing access import line:

```python
from telegram_assistant.access import (
    AccessDenied,
    AccessLevel,
    Authorizer,
    UnresolvedAccessRef,
)
```

Then append this section to the **end** of `tests/test_access.py` (after the observability section, so the `_restore_logging` fixture defined at line ~436 is in scope):

```python
# ---------------------------------------------------------------------------
# Unresolvable `chat:` rules are skipped, not fatal
# ---------------------------------------------------------------------------


class PartialResolver:
    """Resolver whose lookup table may be missing refs.

    A miss raises :class:`EntityNotFoundError` — exactly what the Telethon
    resolver does for a stale ``chat:`` rule pointing at a chat that no longer
    exists (or was never reachable from this account).
    """

    def __init__(self, mapping: dict[object, int]) -> None:
        self._mapping = mapping
        self.calls: list[object] = []

    async def resolve(self, ref: object) -> ResolvedEntity:
        self.calls.append(ref)
        if ref not in self._mapping:
            raise EntityNotFoundError(f"no entity found for reference {ref!r}")
        return ResolvedEntity(
            chat_id=self._mapping[ref], title=str(ref), kind="channel"
        )


@pytest.mark.asyncio
async def test_unresolvable_chat_rule_does_not_break_the_policy() -> None:
    # One dead rule must not take the whole policy — and therefore every gated
    # command — down with it.
    auth = Authorizer(
        AccessConfig(
            rules=[
                AccessRule(chat="ghost", permission="write"),
                AccessRule(chat="@live", permission="write"),
            ]
        ),
        resolver=PartialResolver({"@live": 42}),
    )
    # The live rule still grants...
    await auth.require(42, AccessLevel.WRITE)
    # ...and the dead one grants nothing rather than exploding.
    caps, matched = await auth.describe(999)
    assert caps == frozenset()
    assert matched is None


@pytest.mark.asyncio
async def test_unresolvable_ref_does_not_cost_its_siblings_their_grant() -> None:
    # A rule may carry several refs; a dead one must be skipped per-ref, not
    # take the whole rule (and its live siblings) with it.
    auth = Authorizer(
        AccessConfig(
            rules=[AccessRule(chats=["@good", "ghost"], permission="write")]
        ),
        resolver=PartialResolver({"@good": 7}),
    )
    await auth.require(7, AccessLevel.WRITE)
    assert [u.ref for u in auth.unresolved_refs] == ["ghost"]
    assert isinstance(auth.unresolved_refs[0], UnresolvedAccessRef)
    assert "no entity found" in auth.unresolved_refs[0].error
    assert auth.unresolved_refs[0].to_dict() == {
        "ref": "ghost",
        "error": auth.unresolved_refs[0].error,
    }


@pytest.mark.asyncio
async def test_ambiguous_chat_rule_is_skipped_too() -> None:
    from telegram_assistant.entities import AmbiguousEntityError

    class AmbiguousResolver:
        async def resolve(self, ref: object) -> ResolvedEntity:
            raise AmbiguousEntityError(ref=str(ref), matches=[1, 2])

    auth = Authorizer(
        AccessConfig(rules=[AccessRule(chat="Team", permission="write")]),
        resolver=AmbiguousResolver(),
    )
    caps, matched = await auth.describe(1)
    assert caps == frozenset()
    assert matched is None
    assert [u.ref for u in auth.unresolved_refs] == ["Team"]


@pytest.mark.asyncio
async def test_flood_wait_during_rule_resolution_still_propagates() -> None:
    # The narrow `except EntityError` exists to protect this: swallowing a
    # throttle would silently deny a chat instead of letting the queue
    # pause-and-retry.
    from telegram_assistant.worker.queue import FloodWaitError

    class FloodingResolver:
        async def resolve(self, ref: object) -> ResolvedEntity:
            raise FloodWaitError(30)

    auth = Authorizer(
        AccessConfig(rules=[AccessRule(chat="@x", permission="write")]),
        resolver=FloodingResolver(),
    )
    with pytest.raises(FloodWaitError):
        await auth.require(1, AccessLevel.READ)


@pytest.mark.asyncio
async def test_unresolvable_rule_emits_structured_warning(_restore_logging) -> None:
    buf = io.StringIO()
    configure_logging(level="DEBUG", stream=buf, force=True)
    auth = Authorizer(
        AccessConfig(
            rules=[AccessRule(chat="ghost", permissions=["read", "write"])]
        ),
        resolver=PartialResolver({}),
    )
    await auth.describe(1)
    skipped = [
        r
        for r in _capture_access_log(buf)
        if r.get("event") == "access_rule_ref_unresolved"
    ]
    assert skipped, "expected an access_rule_ref_unresolved log line"
    record = skipped[-1]
    assert record["ref"] == "ghost"
    assert "no entity found" in record["error"]
    assert record["permissions"] == ["read", "write"]
    assert record["level"] == "warning"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/test_access.py -k "unresolvable or ambiguous_chat_rule or flood_wait_during" -v`

Expected: FAIL — `ImportError: cannot import name 'UnresolvedAccessRef' from 'telegram_assistant.access'` at collection time.

- [ ] **Step 3: Add the `UnresolvedAccessRef` dataclass and the authorizer state**

In `src/telegram_assistant/access/service.py`, the import block becomes (ruff's isort puts straight imports first, then from-imports alphabetically within each section — `dataclasses` before `typing`, `config` before `entities` before `observability`):

```python
import enum
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from telegram_assistant.config.models import AccessConfig
from telegram_assistant.entities.service import EntityError
from telegram_assistant.observability.logging import get_logger
```

Import `EntityError` from `entities.service`, not the `entities` package root. Verified: this pulls in `entities/__init__` → `telethon_backend` → `telegram_client.errors` → `worker.queue`, none of which import Telethon at module load or import back from `access`, so there is no cycle and `telethon` stays out of `sys.modules`.

Add the dataclass just above `class AccessDenied`:

```python
@dataclass(frozen=True)
class UnresolvedAccessRef:
    """A ``chat:`` rule reference that could not be resolved at index build.

    Recorded and logged rather than raised: one stale ref must not take the
    whole policy — and therefore every gated command — down with it. Skipping a
    ref only ever *narrows* rights under deny-by-default, so the failure mode is
    a loud 403 on a chat that should have been granted, never a silent grant.
    """

    ref: str
    error: str

    def to_dict(self) -> dict[str, str]:
        return {"ref": self.ref, "error": self.error}
```

In `Authorizer.__init__`, add one line next to the other index state (immediately after `self._memberships: dict[int, set[Membership]] | None = None`):

```python
        # `chat:` rule refs skipped during the index build (stale/ambiguous).
        self._unresolved_refs: tuple[UnresolvedAccessRef, ...] = ()
```

Add the property immediately after the existing `enabled` property:

```python
    @property
    def unresolved_refs(self) -> tuple[UnresolvedAccessRef, ...]:
        """``chat:`` rule refs skipped during the last index build.

        Empty until the index is built — that happens lazily, on the first
        ``require`` / ``allows`` / ``describe`` call. Surfaced by ``access
        check`` so a stale rule is named rather than left for the operator to
        find in the logs.
        """
        return self._unresolved_refs
```

Add `"UnresolvedAccessRef"` to the module `__all__` list at the bottom of the file (keep it sorted — it goes after `"Authorizer"`).

In `src/telegram_assistant/access/__init__.py`, extend both the import and `__all__`:

```python
"""Config-driven read/write access control for the technical account."""

from telegram_assistant.access.service import (
    AccessDenied,
    AccessLevel,
    Authorizer,
    UnresolvedAccessRef,
)

__all__ = [
    "AccessDenied",
    "AccessLevel",
    "Authorizer",
    "UnresolvedAccessRef",
]
```

- [ ] **Step 4: Guard the ref resolution in `_ensure_index`**

In `src/telegram_assistant/access/service.py::_ensure_index`, declare the accumulator alongside the other locals (next to `folder_id_edit_only: dict[int, bool] = {}`):

```python
        unresolved: list[UnresolvedAccessRef] = []
```

Replace the `for ref in refs:` loop body (currently starting `resolved = await self._resolver.resolve(ref)`) with:

```python
                for ref in refs:
                    try:
                        resolved = await self._resolver.resolve(ref)
                    except EntityError as exc:
                        # A stale or ambiguous ref must not abort the index
                        # build — that would take the whole policy, and with it
                        # every gated command, down over one dead config line.
                        # Skipping narrows rights (deny-by-default), so the
                        # worst case is a loud 403. Deliberately narrow: a
                        # translated FloodWaitError is *not* an EntityError and
                        # keeps propagating, so the queue can pause-and-retry
                        # instead of denying a chat during a throttle.
                        _log.warning(
                            "access_rule_ref_unresolved",
                            ref=str(ref),
                            error=str(exc),
                            permissions=sorted(rule.effective_permissions),
                        )
                        unresolved.append(
                            UnresolvedAccessRef(ref=str(ref), error=str(exc))
                        )
                        continue
                    chat_caps.setdefault(resolved.chat_id, set()).update(levels)
                    if delete_override is not None:
                        chat_delete_only[resolved.chat_id] = _merge_only(
                            chat_delete_only.get(resolved.chat_id), delete_override
                        )
                    if edit_override is not None:
                        chat_edit_only[resolved.chat_id] = _merge_only(
                            chat_edit_only.get(resolved.chat_id), edit_override
                        )
```

Then, in the assignment block at the end of the method, add one line before `self._built = True` (alongside the other `self._... = local` assignments, so a build that raises part-way never leaves half-populated state):

```python
        self._unresolved_refs = tuple(unresolved)
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_access.py -v`

Expected: PASS — all pre-existing tests plus the five new ones.

- [ ] **Step 6: Run the wider access suite and the linter**

Run: `.venv/bin/pytest tests/test_access.py tests/test_access_enforcement.py tests/test_access_folder_cache.py tests/test_http_access.py tests/test_cli_access.py -q && .venv/bin/ruff check src tests`

Expected: all pass, no lint findings.

- [ ] **Step 7: Document the behaviour in CLAUDE.md**

In `CLAUDE.md`, in the `telegram.access` paragraph (line 89), find the sentence that ends the paragraph:

> Caller-supplied `folder_memberships=["Name"]` (bare names, id `None`) can satisfy only name rules.

Append immediately after it, in the same paragraph:

> A `chat:` rule ref that fails to resolve (stale entry, ambiguous title) is **skipped with a warning**, not raised: `_ensure_index` catches `EntityError` per *ref* — not per rule, so a dead entry in `chats: [...]` does not cost its siblings their grant — logs `access_rule_ref_unresolved` (ref, reason, permissions) and records it on `Authorizer.unresolved_refs`, which `access check` echoes. Without that, one stale line aborted the index build and every gated command with it. The `except` is deliberately narrow: a translated `FloodWaitError` is not an `EntityError` and keeps propagating, so a throttle pauses-and-retries instead of silently denying a chat. Skipping only ever narrows rights under deny-by-default, so the failure mode is a loud 403, never a silent grant.

- [ ] **Step 8: Commit**

```bash
git add src/telegram_assistant/access/service.py src/telegram_assistant/access/__init__.py tests/test_access.py CLAUDE.md
git commit -m "fix(access): skip unresolvable chat rules with a warning

A stale or ambiguous chat: rule aborted the whole index build, so every
gated command failed with the resolver's error. Catch EntityError per ref,
log access_rule_ref_unresolved and record it on Authorizer.unresolved_refs.
FloodWaitError still propagates.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 2: Report skipped refs in `access check`

**Files:**
- Modify: `src/telegram_assistant/cli/main.py` (`access_check`, lines 7341-7412 — the `_run` coroutine's return, its unpacking, and the payload dict)
- Modify: `README.md` (the `access check` bullet, line 201)
- Modify: `skills/telegram-assistant/SKILL.md` (the `access` / `check` catalog row, line 241)
- Modify: `docs/TODO.md` (tick the first item)
- Test: `tests/test_cli_access_mgmt.py` (append to the `access check` section)

**Interfaces:**
- Consumes: `Authorizer.unresolved_refs -> tuple[UnresolvedAccessRef, ...]` with fields `ref: str` / `error: str`, from Task 1.
- Produces: the `access check` JSON payload gains key `unresolved_refs`: a list of `{"ref": str, "error": str}` objects sorted by `ref`. Always present — an empty list when nothing was skipped.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_cli_access_mgmt.py`, at the end of the `access check` section (before the `access add` section header):

```python
class SelectiveFakeResolver:
    """Resolves refs in ``mapping``; every other ref is not found.

    Mirrors the Telethon resolver's behaviour for a stale ``chat:`` rule while
    still resolving the entity the command was asked about.
    """

    def __init__(self, mapping: dict[str, int]) -> None:
        self._mapping = mapping

    async def resolve(self, ref) -> ResolvedEntity:
        key = str(ref)
        if key not in self._mapping:
            raise EntityNotFoundError(f"no entity found for reference {key!r}")
        return ResolvedEntity(
            chat_id=self._mapping[key], title=key, kind="channel"
        )


def test_check_reports_unresolved_rule_refs(
    minimal_config_yaml: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A stale rule must not turn a grant verdict into an exit-2 resolver error.
    access = textwrap.dedent(
        """
        access:
          rules:
            - chat: "ghost"
              permission: write
            - chat: "@client"
              permissions:
                - read
                - write
        """
    ).strip() + "\n"
    cfg = _write_config(tmp_path, _with_access(minimal_config_yaml, access))
    _patch_access_resolver(
        monkeypatch, SelectiveFakeResolver({"@client": 555})
    )

    result = _run(
        ["access", "check", "--entity", "@client", "--permission", "write",
         "--config", str(cfg)]
    )
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload["granted"] is True
    assert payload["unresolved_refs"] == [
        {"ref": "ghost", "error": "no entity found for reference 'ghost'"}
    ]


def test_check_unresolved_refs_empty_when_policy_is_clean(
    minimal_config_yaml: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    access = textwrap.dedent(
        """
        access:
          rules:
            - chat: "@client"
              permissions:
                - read
                - write
        """
    ).strip() + "\n"
    cfg = _write_config(tmp_path, _with_access(minimal_config_yaml, access))
    _patch_access_resolver(
        monkeypatch, SelectiveFakeResolver({"@client": 555})
    )

    result = _run(
        ["access", "check", "--entity", "@client", "--permission", "read",
         "--config", str(cfg)]
    )
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload["unresolved_refs"] == []
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/bin/pytest tests/test_cli_access_mgmt.py -k "unresolved" -v`

Expected: FAIL with `KeyError: 'unresolved_refs'`.

- [ ] **Step 3: Thread `unresolved_refs` into the payload**

In `src/telegram_assistant/cli/main.py::access_check`, change the `_run` coroutine's return statement from:

```python
            caps, matched = await authorizer.describe(resolved.chat_id)
            return resolved, caps, matched, level in caps
```

to:

```python
            caps, matched = await authorizer.describe(resolved.chat_id)
            # `describe` builds the rule index, so any `chat:` rule ref that
            # failed to resolve has been recorded by now. Report it: the rule
            # was skipped (deny-by-default narrows rights), and a silently
            # missing grant is exactly what this command exists to explain.
            return resolved, caps, matched, level in caps, authorizer.unresolved_refs
```

Change the unpacking from:

```python
        resolved, caps, matched, granted = asyncio.run(_run())
```

to:

```python
        resolved, caps, matched, granted, unresolved = asyncio.run(_run())
```

Add one key to the `payload` dict, after `"matched_rule": matched,`:

```python
        "unresolved_refs": [
            entry.to_dict() for entry in sorted(unresolved, key=lambda e: e.ref)
        ],
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_cli_access_mgmt.py -v`

Expected: PASS — the two new tests plus every pre-existing `access list` / `access check` / `access add` test.

- [ ] **Step 5: Update README and SKILL.md**

In `README.md` line 201, replace the bullet:

```markdown
- `access check --entity <ref> --permission read|write|delete` — resolve a chat and report the grant verdict (exit `0` granted, `3` denied, `2` unresolved).
```

with:

```markdown
- `access check --entity <ref> --permission read|write|delete` — resolve a chat and report the grant verdict (exit `0` granted, `3` denied, `2` unresolved). The payload's `unresolved_refs` names any `chat:` rule ref that could not be resolved — those rules are skipped with a warning rather than failing the command, so a stale entry shows up here instead of breaking every gated call.
```

In `skills/telegram-assistant/SKILL.md` line 241, replace the row:

```markdown
| `access` | `check` | Resolve a chat and report whether the policy grants `read`/`write`/`delete` (exit 0 granted, 3 denied, 2 unresolved). | `telegram-assistant access check --entity <ref> --permission read\|write\|delete` |
```

with:

```markdown
| `access` | `check` | Resolve a chat and report whether the policy grants `read`/`write`/`delete` (exit 0 granted, 3 denied, 2 unresolved). `unresolved_refs` in the payload names stale `chat:` rules, which are skipped with a warning rather than failing the command. | `telegram-assistant access check --entity <ref> --permission read\|write\|delete` |
```

Then re-sync the skill:

```bash
cp skills/telegram-assistant/SKILL.md ~/.claude/skills/telegram-assistant/SKILL.md
```

- [ ] **Step 6: Tick the TODO item**

In `docs/TODO.md`, change the first item's checkbox from `- [ ]` to `- [x]` and append one sub-bullet under it, after the existing `**Обвязка:**` line:

```markdown
  - **Сделано 2026-07-29:** `_ensure_index` ловит `EntityError` **по каждому ref** (а не по правилу), пишет warning `access_rule_ref_unresolved` (ref, причина, permissions) и складывает в `Authorizer.unresolved_refs`; `access check` печатает их в поле `unresolved_refs`. `FloodWaitError` по-прежнему пробрасывается — `except` намеренно узкий. Расхождения с `access list` не было: он вообще не строит индекс и резолвер не создаёт, поэтому падал только `access check` (ровно так же, как `messages recent`).
```

- [ ] **Step 7: Run the full suite and the linter**

Run: `.venv/bin/pytest -q && .venv/bin/ruff check src tests`

Expected: the whole suite passes (including `tests/test_skill_inventory.py`, which checks command names only, so the reworded descriptions are safe) and there are no lint findings.

- [ ] **Step 8: Commit**

```bash
git add src/telegram_assistant/cli/main.py tests/test_cli_access_mgmt.py README.md skills/telegram-assistant/SKILL.md docs/TODO.md
git commit -m "feat(access): report skipped chat rules in access check

access check now echoes Authorizer.unresolved_refs so a stale chat: rule is
named in the payload instead of leaving the operator to grep stderr.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```
