# `chats set-ttl` Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a CLI-only, WRITE-gated `chats set-ttl` command that changes a chat's auto-delete period, so operators stop reaching for ad-hoc Telethon scripts.

**Architecture:** A new `chats/ttl.py` module beside the existing read-only `chats/service.py` (the split `members/listing.py` established), holding a pure `parse_ttl`, a narrow `ChatTtlBackend` protocol, and `set_chat_ttl` which gates, reads the current period, short-circuits a no-op, writes through the shared `Pacer`, and re-reads to decide success. A `TelethonChatTtlBackend` is appended to `chats/telethon_backend.py`; the CLI wires them together in the existing `chats` Typer group.

**Tech Stack:** Python 3.12, Telethon >= 1.44 (`messages.SetHistoryTTLRequest`), Typer, pytest (asyncio mode auto), ruff (line-length 100, py312, E501 ignored), Pydantic config models.

**Spec:** `docs/superpowers/specs/2026-08-06-chats-set-ttl-design.md`

## Global Constraints

- **CLI only.** No HTTP route, no MCP tool, no backend factory on `app.state`. The domain layer must stay surface-agnostic so those can be added later, but this plan adds none of them.
- **`chats/ttl.py` must not import telethon.** Same rule the existing `chats/service.py` follows; the adapter owns every Telethon import, and Telethon symbols are imported *inside* functions (the established pattern in this repo).
- **Use `.venv` for everything.** `.venv/bin/pytest`, `.venv/bin/ruff`. Never a system Python.
- **The WRITE gate runs before any Telegram call.** `authorizer.require(chat_id, AccessLevel.WRITE)` is the first statement that can raise.
- **Setting the value a chat already has must issue no write.** Telegram posts a member-visible service message on every successful `SetHistoryTTL`, including a no-op one.
- **The read-back is the authority.** Never report the requested period as the result; report what `get_ttl` returned after the write.
- **`ttl_period` is `null` when auto-delete is off, never `0`** — matching what `chats inspect` already reports for the same field.
- **Exit codes:** caller input / domain rejection / read-back mismatch → 2, `AccessDenied` → 3, exhausted flood-wait cap and anything else → 1. `AccessDenied`, `EntityNotFoundError` and `AmbiguousEntityError` are `RuntimeError` subclasses, so `except ValueError` cannot catch them — the existing `_raise_for_access_or_entity_error` helper handles them.
- **No `OperationStore` row and no idempotency key.** Shaped like `notifications mute`.
- **Never run mutating live e2e on your own initiative.** No `scripts/e2e_*.sh`, no `scripts/spike_rich_*.py`, no ad-hoc send/react/set probe against the real account. Read-only live checks are allowed. The spec's live verification step is explicitly out of this plan.
- **Commit after every task**, with the task's tests passing and `ruff check src tests` clean.

---

## File Structure

| File | Responsibility |
|---|---|
| `src/telegram_assistant/chats/ttl.py` | **Create.** `parse_ttl`, `SetTtlRequest`, `SetTtlResult`, `ChatTtlBackend`, `set_chat_ttl`. Pure domain, no Telethon. |
| `src/telegram_assistant/chats/__init__.py` | **Modify.** Re-export the new names beside the inspect ones. |
| `src/telegram_assistant/chats/telethon_backend.py` | **Modify.** Append `TelethonChatTtlBackend` and extend `__all__`. |
| `src/telegram_assistant/config/models.py` | **Modify.** Three `TelegramConfig` fields for pacing. |
| `src/telegram_assistant/messages/pacing.py` | **Modify.** Add `ttl_pacing_key`. |
| `src/telegram_assistant/messages/__init__.py` | **Modify.** Export `ttl_pacing_key`. |
| `src/telegram_assistant/cli/main.py` | **Modify.** `_build_chat_ttl_backends`, `_cli_ttl_pacer`, the `chats set-ttl` command. |
| `tests/test_chats_set_ttl.py` | **Create.** Domain against a fake backend. |
| `tests/test_chats_set_ttl_backend.py` | **Create.** Telethon adapter against a fake client. |
| `tests/test_cli_chats_set_ttl.py` | **Create.** CLI flags, payloads, exit codes. |
| `skills/telegram-assistant/SKILL.md` + `~/.claude/skills/telegram-assistant/SKILL.md` | **Modify.** Catalog row, extraction section, confirmation bucket, dry-run list, scenario. |
| `README.md` | **Modify.** Command list entry. |

Task boundaries follow that table: Task 1 is the pure domain (testable alone), Task 2 the adapter (testable alone against a fake client), Task 3 config + pacing key (a small, separately rejectable unit the CLI depends on), Task 4 the CLI, Task 5 docs (whose failure mode — the `test_skill_inventory.py` guard — is its own gate).

---

### Task 1: Domain module — `parse_ttl` and `set_chat_ttl`

**Files:**
- Create: `src/telegram_assistant/chats/ttl.py`
- Modify: `src/telegram_assistant/chats/__init__.py`
- Test: `tests/test_chats_set_ttl.py`

**Interfaces:**
- Consumes: `telegram_assistant.access.service.AccessLevel`, `Authorizer` (already used by `chats/service.py:21`). `Authorizer.require(chat_id: int, level: AccessLevel)` is a coroutine.
- Produces, for Tasks 2 and 4:
  - `parse_ttl(value: str) -> int`
  - `ChatTtlBackend` protocol: `async def get_ttl(self, *, chat_id: int) -> int | None` and `async def set_ttl(self, *, chat_id: int, period: int) -> None`
  - `SetTtlRequest(telegram_chat_id: int, period: int, chat_name: str | None = None)` (frozen)
  - `SetTtlResult(chat_id, requested_ttl_seconds, previous_ttl_seconds, ttl_period, changed, dry_run, chat_name)` (frozen) with `to_dict() -> dict[str, Any]`
  - `async def set_chat_ttl(*, backend: ChatTtlBackend, request: SetTtlRequest, authorizer: Authorizer | None = None, pacer: Any | None = None, dry_run: bool = False) -> SetTtlResult`
  - `MAX_TTL_SECONDS = 2**31 - 1`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_chats_set_ttl.py`:

```python
"""Domain tests for `chats set-ttl`."""

from __future__ import annotations

import pytest

from telegram_assistant.access import AccessDenied, AccessLevel
from telegram_assistant.chats.ttl import (
    MAX_TTL_SECONDS,
    SetTtlRequest,
    parse_ttl,
    set_chat_ttl,
)


class FakeTtlBackend:
    """Records calls; ``reads`` is the queue of values ``get_ttl`` returns."""

    def __init__(self, reads: list[int | None] | None = None) -> None:
        # Default: currently off, and off again after any write.
        self._reads = list(reads if reads is not None else [None, None])
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def get_ttl(self, *, chat_id: int) -> int | None:
        self.calls.append(("get_ttl", {"chat_id": chat_id}))
        return self._reads.pop(0) if self._reads else None

    async def set_ttl(self, *, chat_id: int, period: int) -> None:
        self.calls.append(("set_ttl", {"chat_id": chat_id, "period": period}))

    @property
    def writes(self) -> list[dict[str, object]]:
        return [args for name, args in self.calls if name == "set_ttl"]


class DenyingAuthorizer:
    def __init__(self) -> None:
        self.checked: list[tuple[int, AccessLevel]] = []

    async def require(self, chat_id: int, level: AccessLevel) -> None:
        self.checked.append((chat_id, level))
        raise AccessDenied(chat_ref=chat_id, required_level=level)


class AllowingAuthorizer:
    def __init__(self) -> None:
        self.checked: list[tuple[int, AccessLevel]] = []

    async def require(self, chat_id: int, level: AccessLevel) -> None:
        self.checked.append((chat_id, level))


# --- parse_ttl --------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("off", 0),
        ("OFF", 0),
        ("0", 0),
        ("30s", 30),
        ("5m", 300),
        ("24h", 86400),
        ("1d", 86400),
        ("31d", 2678400),
        ("93d", 8035200),
        ("180d", 15552000),
        ("2w", 1209600),
        ("86400", 86400),
        ("  1d  ", 86400),
    ],
)
def test_parse_ttl_accepts(text: str, expected: int) -> None:
    assert parse_ttl(text) == expected


@pytest.mark.parametrize(
    "text",
    ["", "   ", "-1", "-1d", "1.5d", "1y", "d", "1 d", "abc", "1dd", "1d2h"],
)
def test_parse_ttl_rejects(text: str) -> None:
    with pytest.raises(ValueError):
        parse_ttl(text)


def test_parse_ttl_rejects_over_int32() -> None:
    with pytest.raises(ValueError) as exc:
        parse_ttl(str(MAX_TTL_SECONDS + 1))
    assert "too large" in str(exc.value)


def test_parse_ttl_error_names_the_offending_text() -> None:
    with pytest.raises(ValueError) as exc:
        parse_ttl("1y")
    assert "1y" in str(exc.value)


# --- the gate ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_write_gate_fires_before_any_rpc() -> None:
    backend = FakeTtlBackend()
    authorizer = DenyingAuthorizer()

    with pytest.raises(AccessDenied):
        await set_chat_ttl(
            backend=backend,
            request=SetTtlRequest(telegram_chat_id=5, period=0),
            authorizer=authorizer,
        )

    assert backend.calls == []
    assert authorizer.checked == [(5, AccessLevel.WRITE)]


@pytest.mark.asyncio
async def test_gate_is_write_not_read() -> None:
    backend = FakeTtlBackend(reads=[None, 86400])
    authorizer = AllowingAuthorizer()

    await set_chat_ttl(
        backend=backend,
        request=SetTtlRequest(telegram_chat_id=5, period=86400),
        authorizer=authorizer,
    )

    assert authorizer.checked == [(5, AccessLevel.WRITE)]


# --- the no-op short-circuit ------------------------------------------------


@pytest.mark.asyncio
async def test_setting_the_same_period_issues_no_write() -> None:
    backend = FakeTtlBackend(reads=[86400])

    result = await set_chat_ttl(
        backend=backend,
        request=SetTtlRequest(telegram_chat_id=5, period=86400),
    )

    assert backend.writes == []
    assert result.changed is False
    assert result.previous_ttl_seconds == 86400
    assert result.ttl_period == 86400


@pytest.mark.asyncio
async def test_turning_off_an_already_off_chat_issues_no_write() -> None:
    backend = FakeTtlBackend(reads=[None])

    result = await set_chat_ttl(
        backend=backend,
        request=SetTtlRequest(telegram_chat_id=5, period=0),
    )

    assert backend.writes == []
    assert result.changed is False
    assert result.previous_ttl_seconds is None
    assert result.ttl_period is None


# --- dry run ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_dry_run_reads_but_never_writes() -> None:
    backend = FakeTtlBackend(reads=[86400])

    result = await set_chat_ttl(
        backend=backend,
        request=SetTtlRequest(telegram_chat_id=5, period=0),
        dry_run=True,
    )

    assert backend.writes == []
    assert result.dry_run is True
    assert result.changed is True
    assert result.previous_ttl_seconds == 86400
    assert result.ttl_period == 86400  # unchanged: nothing was written


@pytest.mark.asyncio
async def test_dry_run_still_runs_the_gate() -> None:
    backend = FakeTtlBackend(reads=[86400])
    authorizer = DenyingAuthorizer()

    with pytest.raises(AccessDenied):
        await set_chat_ttl(
            backend=backend,
            request=SetTtlRequest(telegram_chat_id=5, period=0),
            authorizer=authorizer,
            dry_run=True,
        )

    assert backend.calls == []


# --- the write and the read-back --------------------------------------------


@pytest.mark.asyncio
async def test_write_then_read_back_reports_the_server_value() -> None:
    backend = FakeTtlBackend(reads=[None, 86400])

    result = await set_chat_ttl(
        backend=backend,
        request=SetTtlRequest(telegram_chat_id=5, period=86400, chat_name="Team"),
    )

    assert backend.writes == [{"chat_id": 5, "period": 86400}]
    assert result.previous_ttl_seconds is None
    assert result.ttl_period == 86400
    assert result.requested_ttl_seconds == 86400
    assert result.changed is True
    assert result.chat_name == "Team"
    assert result.dry_run is False


@pytest.mark.asyncio
async def test_turning_off_reports_null_not_zero() -> None:
    backend = FakeTtlBackend(reads=[86400, None])

    result = await set_chat_ttl(
        backend=backend,
        request=SetTtlRequest(telegram_chat_id=5, period=0),
    )

    assert result.ttl_period is None
    assert result.requested_ttl_seconds == 0
    assert result.changed is True


@pytest.mark.asyncio
async def test_read_back_mismatch_raises() -> None:
    # Server clamped or ignored the value: 93d asked, 31d stored.
    backend = FakeTtlBackend(reads=[None, 2678400])

    with pytest.raises(ValueError) as exc:
        await set_chat_ttl(
            backend=backend,
            request=SetTtlRequest(telegram_chat_id=5, period=8035200),
        )

    message = str(exc.value)
    assert "8035200" in message
    assert "2678400" in message


@pytest.mark.asyncio
async def test_a_silent_write_is_still_judged_by_the_read_back() -> None:
    """The domain never inspects what ``set_ttl`` returned.

    Task 2's adapter swallows the ``TypeNotFoundError`` Telegram can answer with
    while the write applies; the domain's half of that contract is that a
    ``set_ttl`` returning nothing at all is fine, because only the read-back
    decides. A backend whose ``set_ttl`` is a no-op therefore still yields
    ``changed: True`` when the read-back agrees with the request.
    """

    class SilentBackend(FakeTtlBackend):
        async def set_ttl(self, *, chat_id: int, period: int) -> None:
            self.calls.append(("set_ttl", {"chat_id": chat_id, "period": period}))
            return None

    backend = SilentBackend(reads=[86400, None])

    result = await set_chat_ttl(
        backend=backend,
        request=SetTtlRequest(telegram_chat_id=5, period=0),
    )

    assert backend.writes == [{"chat_id": 5, "period": 0}]
    assert result.ttl_period is None
    assert result.changed is True


# --- pacing -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_pacer_wraps_only_the_write() -> None:
    calls: list[str] = []

    class RecordingPacer:
        async def run(self, key, op):
            calls.append(key)
            return await op()

    backend = FakeTtlBackend(reads=[None, 86400])

    await set_chat_ttl(
        backend=backend,
        request=SetTtlRequest(telegram_chat_id=5, period=86400),
        pacer=RecordingPacer(),
    )

    assert calls == ["ttl:5"]


@pytest.mark.asyncio
async def test_no_pacer_calls_the_backend_directly() -> None:
    backend = FakeTtlBackend(reads=[None, 86400])

    await set_chat_ttl(
        backend=backend,
        request=SetTtlRequest(telegram_chat_id=5, period=86400),
    )

    assert backend.writes == [{"chat_id": 5, "period": 86400}]


@pytest.mark.asyncio
async def test_no_op_short_circuit_never_touches_the_pacer() -> None:
    class ExplodingPacer:
        async def run(self, key, op):
            raise AssertionError("pacer must not be used for a no-op")

    backend = FakeTtlBackend(reads=[86400])

    result = await set_chat_ttl(
        backend=backend,
        request=SetTtlRequest(telegram_chat_id=5, period=86400),
        pacer=ExplodingPacer(),
    )

    assert result.changed is False


# --- payload ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_to_dict_shape() -> None:
    backend = FakeTtlBackend(reads=[None, 86400])

    result = await set_chat_ttl(
        backend=backend,
        request=SetTtlRequest(telegram_chat_id=5, period=86400, chat_name="Team"),
    )

    assert result.to_dict() == {
        "chat_id": 5,
        "chat_name": "Team",
        "changed": True,
        "dry_run": False,
        "previous_ttl_seconds": None,
        "requested_ttl_seconds": 86400,
        "ttl_period": 86400,
    }


@pytest.mark.asyncio
async def test_marked_chat_id_is_reported_bare() -> None:
    backend = FakeTtlBackend(reads=[None, 86400])

    result = await set_chat_ttl(
        backend=backend,
        request=SetTtlRequest(telegram_chat_id=-1002305069221, period=86400),
    )

    assert result.chat_id == 2305069221
    # The backend still receives what the caller resolved.
    assert backend.writes == [{"chat_id": -1002305069221, "period": 86400}]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/test_chats_set_ttl.py -q`
Expected: collection error — `ModuleNotFoundError: No module named 'telegram_assistant.chats.ttl'`.

- [ ] **Step 3: Write `src/telegram_assistant/chats/ttl.py`**

```python
"""Auto-delete period writes — the set-ttl op of the chats domain.

Kept out of :mod:`telegram_assistant.chats.service` (which is the read-only
inspect op) the same way ``members/`` splits ``listing.py`` out of its own
``service.py``: one operation per module, READ and WRITE not mixed. Like
``notifications mute`` this opens no operation row and has no idempotency key —
the target is naturally idempotent.

Three Telegram facts shape the order of operations here, all proven live on
2026-08-05 (see the spec):

* every successful ``SetHistoryTTL`` posts a member-visible service message,
  **including one that changes nothing** — hence the no-op short-circuit;
* the RPC's response may fail to parse while the write applied — hence the
  unconditional read-back, which is the only authority on the result;
* the flood waits on this method escalate into the hundreds of seconds — hence
  the pacer around the write.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Protocol

from telegram_assistant.access.service import AccessLevel, Authorizer
from telegram_assistant.entities import EntityRef

#: Largest period the wire accepts — ``ttl_period`` is a 32-bit int.
MAX_TTL_SECONDS = 2**31 - 1

#: Suffix multipliers for :func:`parse_ttl`. No ``y``/``mo``: a month is not a
#: fixed number of seconds, and guessing one silently would be worse than
#: making the caller write ``31d``.
_TTL_UNITS: dict[str, int] = {
    "s": 1,
    "m": 60,
    "h": 3600,
    "d": 86400,
    "w": 604800,
}

_TTL_PATTERN = re.compile(r"^(\d+)([smhdw]?)$")


def parse_ttl(value: str) -> int:
    """Parse a CLI ``--ttl`` value into seconds.

    Accepts ``off`` (case-insensitive) and ``0`` for "auto-delete disabled",
    ``<int><unit>`` with unit ``s``/``m``/``h``/``d``/``w``, and a bare integer
    read as seconds. Everything else raises :class:`ValueError` naming the
    offending text.

    There is deliberately no allow-list of preset durations: Telegram's clients
    offer only day/week/month, but real chats were found at 31, 93 and 180 days,
    so arbitrary periods pass. The server is the authority on what it accepts.
    """
    text = (value or "").strip()
    if not text:
        raise ValueError("--ttl must not be empty; use 'off' or a duration like 1d")
    if text.lower() == "off":
        return 0

    match = _TTL_PATTERN.match(text)
    if match is None:
        raise ValueError(
            f"cannot parse --ttl {value!r}; expected 'off' or <integer><unit> "
            f"with unit one of {', '.join(sorted(_TTL_UNITS))} (e.g. 1d, 24h, 93d)"
        )

    amount = int(match.group(1))
    unit = match.group(2) or "s"
    seconds = amount * _TTL_UNITS[unit]
    if seconds > MAX_TTL_SECONDS:
        raise ValueError(
            f"--ttl {value!r} is too large; the maximum is {MAX_TTL_SECONDS} seconds"
        )
    return seconds


@dataclass(frozen=True)
class SetTtlRequest:
    """Input to :func:`set_chat_ttl`.

    ``telegram_chat_id`` is the resolved numeric id in whatever shape the
    surface produced it (marked ``-100…`` or bare) — the backend gets it
    verbatim, the payload reports it bare. ``period`` is seconds, ``0`` meaning
    auto-delete off. ``chat_name`` is carried through for the payload.
    """

    telegram_chat_id: int
    period: int
    chat_name: str | None = None


@dataclass(frozen=True)
class SetTtlResult:
    """Outcome of :func:`set_chat_ttl`.

    ``previous_ttl_seconds`` and ``ttl_period`` are ``None`` when auto-delete is
    off, never ``0`` — that is what ``chats inspect`` reports for the same
    field, and the two commands must not disagree about one chat. ``ttl_period``
    is what the server returned on the read-back, never the requested value.
    """

    chat_id: int
    requested_ttl_seconds: int
    previous_ttl_seconds: int | None
    ttl_period: int | None
    changed: bool
    dry_run: bool = False
    chat_name: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "chat_id": self.chat_id,
            "chat_name": self.chat_name,
            "changed": self.changed,
            "dry_run": self.dry_run,
            "previous_ttl_seconds": self.previous_ttl_seconds,
            "requested_ttl_seconds": self.requested_ttl_seconds,
            "ttl_period": self.ttl_period,
        }


class ChatTtlBackend(Protocol):
    """Telethon-facing surface needed to read and write a chat's TTL.

    Deliberately narrower than ``ChatInspectBackend``: reading the whole
    ``ChatInfo`` for one field would cost peer-kind dispatch, serialization and
    ``access_hash`` redaction, and a test fake would have to be a whole
    ``ChatInfo``.
    """

    async def get_ttl(self, *, chat_id: int) -> int | None:
        ...

    async def set_ttl(self, *, chat_id: int, period: int) -> None:
        ...


def _reported(period: int | None) -> int | None:
    """Normalise a wire value to the reported one: ``0`` and ``None`` are off."""
    return period or None


def ttl_gate_key(chat_id: int) -> str:
    """Gate key for TTL writes, kept next to the request that uses it.

    Re-exported from :mod:`telegram_assistant.messages.pacing` as
    ``ttl_pacing_key``; defined there so all gate keys live together.
    """
    from telegram_assistant.messages import ttl_pacing_key

    return ttl_pacing_key(chat_id)


async def set_chat_ttl(
    *,
    backend: ChatTtlBackend,
    request: SetTtlRequest,
    authorizer: Authorizer | None = None,
    pacer: Any | None = None,
    dry_run: bool = False,
) -> SetTtlResult:
    """Set ``request.telegram_chat_id``'s auto-delete period.

    A WRITE op: when an ``authorizer`` is supplied it must grant WRITE on the
    chat, checked before any Telegram call.

    Order: gate → read current → short-circuit when already equal → return on
    ``dry_run`` → write (through ``pacer`` when supplied) → read back. The
    read-back decides the result; a value disagreeing with the request raises
    :class:`ValueError` naming both, because a server that clamped or dropped
    the value must not be reported as success.
    """
    if request.period < 0:
        raise ValueError("ttl period must not be negative")
    if request.period > MAX_TTL_SECONDS:
        raise ValueError(
            f"ttl period {request.period} is too large; "
            f"the maximum is {MAX_TTL_SECONDS} seconds"
        )

    chat_id = request.telegram_chat_id
    if authorizer is not None:
        await authorizer.require(chat_id, AccessLevel.WRITE)

    bare_id = EntityRef(raw=int(chat_id)).numeric_id
    current = _reported(await backend.get_ttl(chat_id=chat_id))
    wanted = _reported(request.period)

    if current == wanted:
        # Telegram posts a service message on *every* successful set, so
        # re-applying the value a chat already has is not free: a re-run over a
        # folder would spam every chat in it.
        return SetTtlResult(
            chat_id=bare_id,
            requested_ttl_seconds=request.period,
            previous_ttl_seconds=current,
            ttl_period=current,
            changed=False,
            dry_run=dry_run,
            chat_name=request.chat_name,
        )

    if dry_run:
        return SetTtlResult(
            chat_id=bare_id,
            requested_ttl_seconds=request.period,
            previous_ttl_seconds=current,
            ttl_period=current,
            changed=True,
            dry_run=True,
            chat_name=request.chat_name,
        )

    async def _call() -> None:
        await backend.set_ttl(chat_id=chat_id, period=request.period)

    if pacer is not None:
        await pacer.run(ttl_gate_key(chat_id), _call)
    else:
        await _call()

    stored = _reported(await backend.get_ttl(chat_id=chat_id))
    if stored != wanted:
        raise ValueError(
            f"chat {bare_id}: requested ttl {request.period} but the server "
            f"stored {stored if stored is not None else 0}"
        )

    return SetTtlResult(
        chat_id=bare_id,
        requested_ttl_seconds=request.period,
        previous_ttl_seconds=current,
        ttl_period=stored,
        changed=True,
        dry_run=False,
        chat_name=request.chat_name,
    )


__all__ = [
    "MAX_TTL_SECONDS",
    "ChatTtlBackend",
    "SetTtlRequest",
    "SetTtlResult",
    "parse_ttl",
    "set_chat_ttl",
    "ttl_gate_key",
]
```

Note: `ttl_gate_key` imports `ttl_pacing_key` from `telegram_assistant.messages` — which Task 3 adds. Until Task 3 lands, the pacing tests in this task fail on import. To keep Task 1 independently green, **implement `ttl_gate_key` inline for now** and switch it to the re-export in Task 3:

```python
def ttl_gate_key(chat_id: int) -> str:
    """Gate key for TTL writes — bare id, so marked and bare ids share one row."""
    return f"ttl:{EntityRef(raw=int(chat_id)).numeric_id}"
```

Use the inline body in Task 1. Task 3 replaces it with the re-export.

- [ ] **Step 4: Extend `src/telegram_assistant/chats/__init__.py`**

Replace the whole file with:

```python
"""Chat-wide operations: metadata inspection (read) and auto-delete TTL (write)."""

from telegram_assistant.chats.service import (
    CHAT_KINDS,
    ChatInfo,
    ChatInspectBackend,
    inspect_chat,
)
from telegram_assistant.chats.ttl import (
    MAX_TTL_SECONDS,
    ChatTtlBackend,
    SetTtlRequest,
    SetTtlResult,
    parse_ttl,
    set_chat_ttl,
)

__all__ = [
    "CHAT_KINDS",
    "MAX_TTL_SECONDS",
    "ChatInfo",
    "ChatInspectBackend",
    "ChatTtlBackend",
    "SetTtlRequest",
    "SetTtlResult",
    "inspect_chat",
    "parse_ttl",
    "set_chat_ttl",
]
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_chats_set_ttl.py -q`
Expected: PASS, all tests.

- [ ] **Step 6: Confirm the module imports no telethon**

Run: `.venv/bin/python -c "import ast,sys; src=open('src/telegram_assistant/chats/ttl.py').read(); assert 'telethon' not in src, 'ttl.py must not reference telethon'; print('ok')"`
Expected: `ok`

- [ ] **Step 7: Lint**

Run: `.venv/bin/ruff check src tests`
Expected: `All checks passed!`

- [ ] **Step 8: Full suite (nothing else may break)**

Run: `.venv/bin/pytest -q`
Expected: PASS.

- [ ] **Step 9: Commit**

```bash
git add src/telegram_assistant/chats/ttl.py src/telegram_assistant/chats/__init__.py tests/test_chats_set_ttl.py
git commit -m "feat(chats): add the set-ttl domain op with a no-op short-circuit"
```

---

### Task 2: Telethon adapter — `TelethonChatTtlBackend`

**Files:**
- Modify: `src/telegram_assistant/chats/telethon_backend.py` (append the class before `__all__` at line 464, and extend `__all__`)
- Test: `tests/test_chats_set_ttl_backend.py`

**Interfaces:**
- Consumes from Task 1: nothing at runtime — the adapter satisfies `ChatTtlBackend` structurally (`get_ttl(*, chat_id) -> int | None`, `set_ttl(*, chat_id, period) -> None`).
- Consumes from the existing module: `translate_flood_wait` (already imported at `telethon_backend.py:17`).
- Produces for Task 4: `TelethonChatTtlBackend(client)`.

Peer dispatch mirrors `TelethonChatInspectBackend.inspect_chat` (`telethon_backend.py:230-251`): `get_input_entity`, then branch on `type(peer).__name__` — `InputPeerChannel` → `channels.GetFullChannelRequest`, `InputPeerChat` → `messages.GetFullChatRequest`, `InputPeerUser`/`InputPeerSelf` → `users.GetFullUserRequest`. `ttl_period` lives on `.full_chat` for the first two and on `.full_user` for the third.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_chats_set_ttl_backend.py`:

```python
"""Tests for the Telethon set-ttl adapter.

Fakes are stand-ins whose class *names* match Telethon's, because that is what
the peer dispatch keys on — the same convention as
``tests/test_members_list_backend.py``.
"""

from __future__ import annotations

import pytest

from telegram_assistant.chats.telethon_backend import TelethonChatTtlBackend


class InputPeerChannel:
    def __init__(self, channel_id: int) -> None:
        self.channel_id = channel_id


class InputPeerChat:
    def __init__(self, chat_id: int) -> None:
        self.chat_id = chat_id


class InputPeerUser:
    def __init__(self, user_id: int) -> None:
        self.user_id = user_id


class _FullChat:
    def __init__(self, ttl_period) -> None:
        self.ttl_period = ttl_period


class FullChannelResult:
    def __init__(self, ttl_period) -> None:
        self.full_chat = _FullChat(ttl_period)


class FullUserResult:
    def __init__(self, ttl_period) -> None:
        self.full_user = _FullChat(ttl_period)


class TypeNotFoundError(Exception):
    """Name-matched by the adapter; Telethon raises this when a response
    carries a constructor newer than the installed layer."""


class FakeClient:
    def __init__(self, *, peer, full=None, set_error=None) -> None:
        self._peer = peer
        self._full = full
        self._set_error = set_error
        self.requests: list[object] = []

    async def get_input_entity(self, ref):
        return self._peer

    async def __call__(self, request):
        self.requests.append(request)
        name = type(request).__name__
        if name in {"GetFullChannelRequest", "GetFullChatRequest", "GetFullUserRequest"}:
            return self._full
        if name == "SetHistoryTTLRequest":
            if self._set_error is not None:
                raise self._set_error
            return object()
        raise AssertionError(f"unexpected request {name}")

    @property
    def request_names(self) -> list[str]:
        return [type(r).__name__ for r in self.requests]


# --- get_ttl ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_ttl_reads_a_supergroup() -> None:
    client = FakeClient(peer=InputPeerChannel(7), full=FullChannelResult(86400))
    backend = TelethonChatTtlBackend(client)

    assert await backend.get_ttl(chat_id=-1007) == 86400
    assert client.request_names == ["GetFullChannelRequest"]


@pytest.mark.asyncio
async def test_get_ttl_reads_a_basic_group() -> None:
    client = FakeClient(peer=InputPeerChat(55), full=FullChannelResult(2678400))
    backend = TelethonChatTtlBackend(client)

    assert await backend.get_ttl(chat_id=-55) == 2678400
    assert client.request_names == ["GetFullChatRequest"]


@pytest.mark.asyncio
async def test_get_ttl_reads_a_user() -> None:
    client = FakeClient(peer=InputPeerUser(9), full=FullUserResult(604800))
    backend = TelethonChatTtlBackend(client)

    assert await backend.get_ttl(chat_id=9) == 604800
    assert client.request_names == ["GetFullUserRequest"]


@pytest.mark.asyncio
async def test_get_ttl_returns_none_when_off() -> None:
    client = FakeClient(peer=InputPeerChannel(7), full=FullChannelResult(None))
    backend = TelethonChatTtlBackend(client)

    assert await backend.get_ttl(chat_id=-1007) is None


@pytest.mark.asyncio
async def test_unsupported_peer_raises_value_error() -> None:
    class InputPeerEmpty:
        pass

    client = FakeClient(peer=InputPeerEmpty())
    backend = TelethonChatTtlBackend(client)

    with pytest.raises(ValueError) as exc:
        await backend.get_ttl(chat_id=1)
    assert "1" in str(exc.value)


# --- set_ttl ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_set_ttl_sends_the_request_with_the_period() -> None:
    client = FakeClient(peer=InputPeerChannel(7), full=FullChannelResult(None))
    backend = TelethonChatTtlBackend(client)

    await backend.set_ttl(chat_id=-1007, period=86400)

    assert client.request_names == ["SetHistoryTTLRequest"]
    assert client.requests[0].period == 86400


@pytest.mark.asyncio
async def test_set_ttl_zero_disables() -> None:
    client = FakeClient(peer=InputPeerChannel(7), full=FullChannelResult(None))
    backend = TelethonChatTtlBackend(client)

    await backend.set_ttl(chat_id=-1007, period=0)

    assert client.requests[0].period == 0


@pytest.mark.asyncio
async def test_unparseable_response_is_swallowed() -> None:
    """Proven live 2026-08-05: the write applied, only the response failed to
    parse. Raising here would report a successful change as a failure — the
    domain's read-back is what decides."""
    client = FakeClient(
        peer=InputPeerChannel(7),
        full=FullChannelResult(None),
        set_error=TypeNotFoundError("Could not find a matching Constructor ID"),
    )
    backend = TelethonChatTtlBackend(client)

    await backend.set_ttl(chat_id=-1007, period=0)  # must not raise


@pytest.mark.asyncio
async def test_other_errors_propagate() -> None:
    client = FakeClient(
        peer=InputPeerChannel(7),
        full=FullChannelResult(None),
        set_error=RuntimeError("CHAT_ADMIN_REQUIRED"),
    )
    backend = TelethonChatTtlBackend(client)

    with pytest.raises(RuntimeError):
        await backend.set_ttl(chat_id=-1007, period=0)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/test_chats_set_ttl_backend.py -q`
Expected: `ImportError: cannot import name 'TelethonChatTtlBackend'`.

- [ ] **Step 3: Append the adapter to `src/telegram_assistant/chats/telethon_backend.py`**

Insert immediately before the final `__all__` line:

```python
class TelethonChatTtlBackend:
    """Adapter from the Telethon ``TelegramClient`` to ``ChatTtlBackend``.

    Two RPCs at most per call, and the peer dispatch is the same shape as
    :class:`TelethonChatInspectBackend` — the ``ttl_period`` field lives on
    ``full_chat`` for channels and basic groups, on ``full_user`` for users.
    """

    def __init__(self, client: Any) -> None:
        self._client = client

    async def _peer(self, chat_id: int) -> tuple[Any, str]:
        try:
            peer = await self._client.get_input_entity(chat_id)
        except Exception as exc:
            translated = translate_flood_wait(exc)
            if translated is not exc:
                raise translated from exc
            raise
        kind = type(peer).__name__
        if kind not in {
            "InputPeerChannel",
            "InputPeerChat",
            "InputPeerUser",
            "InputPeerSelf",
        }:
            raise ValueError(
                f"chat {chat_id} has no auto-delete setting (resolved to {kind})"
            )
        return peer, kind

    async def get_ttl(self, *, chat_id: int) -> int | None:
        from telethon.errors import ChannelPrivateError
        from telethon.tl import functions

        peer, kind = await self._peer(chat_id)
        if kind == "InputPeerChannel":
            request = functions.channels.GetFullChannelRequest(channel=peer)
            attr = "full_chat"
        elif kind == "InputPeerChat":
            request = functions.messages.GetFullChatRequest(chat_id=peer.chat_id)
            attr = "full_chat"
        else:
            request = functions.users.GetFullUserRequest(id=peer)
            attr = "full_user"

        try:
            result = await self._client(request)
        except Exception as exc:
            translated = translate_flood_wait(exc)
            if translated is not exc:
                raise translated from exc
            # Mirrors the inspect adapter: the peer resolved but Telegram
            # refuses the Full fetch, which is caller-input-shaped (exit 2),
            # not an internal error.
            if isinstance(exc, ChannelPrivateError):
                raise ValueError(f"chat {chat_id} is private or inaccessible") from exc
            raise

        return getattr(getattr(result, attr, None), "ttl_period", None)

    async def set_ttl(self, *, chat_id: int, period: int) -> None:
        from telethon.tl import functions

        peer, _kind = await self._peer(chat_id)
        try:
            await self._client(
                functions.messages.SetHistoryTTLRequest(peer=peer, period=period)
            )
        except Exception as exc:
            translated = translate_flood_wait(exc)
            if translated is not exc:
                raise translated from exc
            # Proven live 2026-08-05 (Migragate): Telegram answered with a
            # constructor newer than the installed layer, Telethon could not
            # read it — and the write had applied. Treating that as a failure
            # would report a successful change as an error; the domain's
            # read-back is what decides. Matched by class *name* so no import
            # of a Telethon-version-specific symbol is needed.
            if type(exc).__name__ == "TypeNotFoundError":
                return
            raise
```

Then change the last line from `__all__ = ["TelethonChatInspectBackend"]` to:

```python
__all__ = ["TelethonChatInspectBackend", "TelethonChatTtlBackend"]
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_chats_set_ttl_backend.py -q`
Expected: PASS.

- [ ] **Step 5: Confirm the adapter really satisfies the protocol**

Run:
```bash
.venv/bin/python -c "
from telegram_assistant.chats import ChatTtlBackend
from telegram_assistant.chats.telethon_backend import TelethonChatTtlBackend
import inspect
for name in ('get_ttl', 'set_ttl'):
    assert hasattr(TelethonChatTtlBackend, name), name
    assert inspect.iscoroutinefunction(getattr(TelethonChatTtlBackend, name)), name
print('ok')
"
```
Expected: `ok`

- [ ] **Step 6: Lint and full suite**

Run: `.venv/bin/ruff check src tests && .venv/bin/pytest -q`
Expected: `All checks passed!` then PASS.

- [ ] **Step 7: Commit**

```bash
git add src/telegram_assistant/chats/telethon_backend.py tests/test_chats_set_ttl_backend.py
git commit -m "feat(chats): add the Telethon set-ttl adapter, tolerating unparseable responses"
```

---

### Task 3: Config keys and the TTL gate key

**Files:**
- Modify: `src/telegram_assistant/messages/pacing.py` (add `ttl_pacing_key` next to `pin_pacing_key` at line 225, extend `__all__`)
- Modify: `src/telegram_assistant/messages/__init__.py` (import at line ~48, export at line ~237)
- Modify: `src/telegram_assistant/config/models.py` (three fields after `pin_min_interval_seconds`, line 257)
- Modify: `src/telegram_assistant/chats/ttl.py` (switch `ttl_gate_key` to the re-export)
- Test: `tests/test_chats_set_ttl.py` (append), `tests/test_config_models.py` if it exists — otherwise the config assertions go in `tests/test_chats_set_ttl.py`

**Interfaces:**
- Consumes: `EntityRef` from `telegram_assistant.entities` (already imported in `pacing.py` for `pin_pacing_key`).
- Produces for Task 4: `ttl_pacing_key(chat_id: int) -> str`, and `TelegramConfig.ttl_min_interval_seconds: float`, `TelegramConfig.ttl_max_flood_wait_seconds: float`, `TelegramConfig.ttl_max_flood_wait_retries: int`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_chats_set_ttl.py`:

```python
# --- gate key and config ----------------------------------------------------


def test_ttl_pacing_key_uses_the_bare_id() -> None:
    from telegram_assistant.messages import ttl_pacing_key

    assert ttl_pacing_key(-1002305069221) == "ttl:2305069221"
    assert ttl_pacing_key(2305069221) == "ttl:2305069221"


def test_ttl_gate_key_matches_the_pacing_key() -> None:
    from telegram_assistant.chats.ttl import ttl_gate_key
    from telegram_assistant.messages import ttl_pacing_key

    assert ttl_gate_key(-1002305069221) == ttl_pacing_key(-1002305069221)


def test_ttl_gate_key_does_not_collide_with_the_pin_gate() -> None:
    from telegram_assistant.messages import pin_pacing_key, ttl_pacing_key

    assert ttl_pacing_key(5) != pin_pacing_key(5)


def test_config_defaults_for_ttl_pacing(minimal_config_yaml) -> None:
    from telegram_assistant.config.loader import load_config_from_text

    config = load_config_from_text(minimal_config_yaml, source="test")

    assert config.telegram.ttl_min_interval_seconds == 2.0
    assert config.telegram.ttl_max_flood_wait_seconds == 3600.0
    assert config.telegram.ttl_max_flood_wait_retries == 5


def test_config_rejects_negative_ttl_interval(minimal_config_yaml) -> None:
    from telegram_assistant.config.loader import ConfigError, load_config_from_text

    # The fixture's `telegram:` line is followed by 2-space-indented keys, so
    # inserting one right after the header keeps the YAML valid.
    text = minimal_config_yaml.replace(
        "telegram:", "telegram:\n  ttl_min_interval_seconds: -1", 1
    )
    with pytest.raises((ConfigError, ValueError)):
        load_config_from_text(text, source="test")
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/pytest tests/test_chats_set_ttl.py -q -k "ttl_pacing_key or gate_key or config"`
Expected: `ImportError: cannot import name 'ttl_pacing_key'` and attribute errors on the config.

- [ ] **Step 3: Add `ttl_pacing_key` to `src/telegram_assistant/messages/pacing.py`**

Insert immediately after the `pin_pacing_key` function (which ends at line 237 with `return f"pin:{EntityRef(raw=int(chat_id)).numeric_id}"`):

```python
def ttl_pacing_key(chat_id: int) -> str:
    """Gate key for auto-delete (TTL) writes — a different Telegram limit than pins.

    Its own gate row rather than sharing ``pin:``: Telegram meters
    ``messages.SetHistoryTTL`` separately, and observed waits on it escalate far
    past anything pins produce (261s, 703s and 866s within one hour on one
    account, 2026-08-05). Sharing a row would let a slow TTL sweep throttle
    unrelated pins and vice versa.

    The id is reduced to its bare form for the same reason ``pin_pacing_key``
    does it: an explicit ``--chat-id -1001234567890`` keeps the marker while an
    ``--entity`` lookup yields the bare id, and keying on the raw value would
    open two independent rows for one chat.
    """
    return f"ttl:{EntityRef(raw=int(chat_id)).numeric_id}"
```

Add `"ttl_pacing_key"` to `pacing.py`'s `__all__`, keeping it alphabetically placed next to `"retry_after_details"`.

- [ ] **Step 4: Export it from `src/telegram_assistant/messages/__init__.py`**

Add `ttl_pacing_key` to the `from telegram_assistant.messages.pacing import (...)` block (after `retry_after_details`, line ~49) and `"ttl_pacing_key"` to the module `__all__` (after `"retry_after_details"`, line ~238).

- [ ] **Step 5: Add the three config fields**

In `src/telegram_assistant/config/models.py`, insert after the `pin_min_interval_seconds` field (ends line 257) and before `download_root`:

```python
    ttl_min_interval_seconds: float = Field(
        default=2.0,
        ge=0.0,
        description=(
            "Minimum seconds between two `chats set-ttl` writes on the same "
            "chat, paced through the same shared SQLite gate as pins but on a "
            "separate row (Telegram meters SetHistoryTTL separately). "
            "0 disables pacing."
        ),
    )
    ttl_max_flood_wait_seconds: float = Field(
        default=3600.0,
        ge=0.0,
        description=(
            "Longest single FLOOD_WAIT `chats set-ttl` will sleep through. "
            "Waits on SetHistoryTTL escalate into the hundreds of seconds "
            "(261s, 703s and 866s observed within one hour), so the default is "
            "far above the 60s used elsewhere — but finite, so a stuck call "
            "cannot hang unnoticed forever."
        ),
    )
    ttl_max_flood_wait_retries: int = Field(
        default=5,
        ge=1,
        description=(
            "How many FLOOD_WAIT pauses `chats set-ttl` will sit through before "
            "giving up. Above the pacer's own default of 3 because the waits "
            "escalate: one chat can plausibly spend two or three of them, and "
            "running out reports a flood wait as a failure on a call that was "
            "about to succeed."
        ),
    )
```

- [ ] **Step 6: Switch `ttl_gate_key` in `chats/ttl.py` to the re-export**

Replace the inline body written in Task 1 with:

```python
def ttl_gate_key(chat_id: int) -> str:
    """Gate key for TTL writes.

    Delegates to :func:`telegram_assistant.messages.pacing.ttl_pacing_key` so
    every gate key lives in one module; imported lazily to keep this module free
    of an import-time dependency on ``messages``.
    """
    from telegram_assistant.messages import ttl_pacing_key

    return ttl_pacing_key(chat_id)
```

- [ ] **Step 7: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_chats_set_ttl.py -q`
Expected: PASS (including the Task 1 tests, which must still pass with the re-export in place).

- [ ] **Step 8: Lint and full suite**

Run: `.venv/bin/ruff check src tests && .venv/bin/pytest -q`
Expected: `All checks passed!` then PASS.

- [ ] **Step 9: Commit**

```bash
git add src/telegram_assistant/messages/pacing.py src/telegram_assistant/messages/__init__.py src/telegram_assistant/config/models.py src/telegram_assistant/chats/ttl.py tests/test_chats_set_ttl.py
git commit -m "feat(config): add ttl pacing knobs and a dedicated ttl gate key"
```

---

### Task 4: The `chats set-ttl` CLI command

**Files:**
- Modify: `src/telegram_assistant/cli/main.py` (new `_build_chat_ttl_backends` and `_cli_ttl_pacer` beside `_build_chat_inspect_backends` at line 3413; the command after `chats_inspect`, which ends line 3556)
- Test: `tests/test_cli_chats_set_ttl.py`

**Interfaces:**
- Consumes from Task 1: `parse_ttl`, `SetTtlRequest`, `set_chat_ttl` from `telegram_assistant.chats`.
- Consumes from Task 2: `TelethonChatTtlBackend`.
- Consumes from Task 3: `config.telegram.ttl_min_interval_seconds`, `ttl_max_flood_wait_seconds`, `ttl_max_flood_wait_retries`.
- Consumes from the existing CLI: `_load_config_or_exit`, `_resolve_folder_name`, `_cli_authorizer`, `_raise_for_access_or_entity_error`, `_raise_for_flood_wait`, `default_database_path`, `TelethonSessionManager`.
- Produces: nothing consumed by later tasks except the command name `chats set-ttl`, which Task 5 documents.

The `--dry-run` payload uses the project-wide envelope (`{"status": "dry_run", "dry_run": true, "command": ..., "would": ..., "resolved": {...}, "planned_actions": [...], "warnings": []}`) that all 20 other dry-run sites in `cli/main.py` emit — the skill tells the agent to look for `status = dry_run`. The domain's `SetTtlResult` fields go inside `resolved`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_cli_chats_set_ttl.py`:

```python
"""CLI tests for `chats set-ttl`."""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from telegram_assistant.access import AccessDenied, AccessLevel
from telegram_assistant.cli import main as cli_main
from telegram_assistant.entities import EntityNotFoundError

runner = CliRunner()


class FakeTtlBackend:
    def __init__(self, reads=None, set_error=None) -> None:
        self._reads = list(reads if reads is not None else [None, None])
        self.set_error = set_error
        self.calls: list[tuple[str, dict]] = []

    async def get_ttl(self, *, chat_id: int):
        self.calls.append(("get_ttl", {"chat_id": chat_id}))
        return self._reads.pop(0) if self._reads else None

    async def set_ttl(self, *, chat_id: int, period: int) -> None:
        self.calls.append(("set_ttl", {"chat_id": chat_id, "period": period}))
        if self.set_error is not None:
            raise self.set_error

    @property
    def writes(self):
        return [args for name, args in self.calls if name == "set_ttl"]


class FakeResolved:
    def __init__(self, chat_id: int) -> None:
        self.chat_id = chat_id


class FakeResolver:
    def __init__(self, chat_id: int = 5, error: Exception | None = None) -> None:
        self.chat_id = chat_id
        self.error = error

    async def resolve(self, ref: str):
        if self.error is not None:
            raise self.error
        return FakeResolved(self.chat_id)


class FakeManager:
    async def disconnect(self) -> None:
        return None


@pytest.fixture
def wire(monkeypatch, minimal_config_yaml, tmp_path):
    config_path = tmp_path / "config.yml"
    config_path.write_text(minimal_config_yaml, encoding="utf-8")

    def _install(backend, resolver=None, authorizer=None):
        config = cli_main._load_config_or_exit(config_path)

        def _build(_path):
            async def _open():
                return backend, object(), resolver or FakeResolver()

            return config, FakeManager(), _open

        monkeypatch.setattr(cli_main, "_build_chat_ttl_backends", _build)
        if authorizer is not None:
            monkeypatch.setattr(cli_main, "_cli_authorizer", lambda *a, **k: authorizer)
        return config_path

    return _install


# --- flag validation --------------------------------------------------------


def test_requires_exactly_one_reference(wire):
    config_path = wire(FakeTtlBackend())

    result = runner.invoke(
        cli_main.app,
        ["chats", "set-ttl", "--ttl", "off", "--config", str(config_path)],
    )

    assert result.exit_code == 2
    assert "exactly one of --chat-id, --chat-name, or --entity" in result.output


def test_rejects_two_references(wire):
    config_path = wire(FakeTtlBackend())

    result = runner.invoke(
        cli_main.app,
        [
            "chats", "set-ttl",
            "--chat-id", "5",
            "--entity", "@team",
            "--ttl", "off",
            "--config", str(config_path),
        ],
    )

    assert result.exit_code == 2


def test_unparseable_ttl_exits_2_before_any_rpc(wire):
    backend = FakeTtlBackend()
    config_path = wire(backend)

    result = runner.invoke(
        cli_main.app,
        [
            "chats", "set-ttl",
            "--chat-id", "5",
            "--ttl", "1y",
            "--config", str(config_path),
        ],
    )

    assert result.exit_code == 2
    assert "1y" in result.output
    assert backend.calls == []


# --- happy paths ------------------------------------------------------------


def test_sets_a_period_and_prints_the_payload(wire):
    backend = FakeTtlBackend(reads=[None, 8035200])
    config_path = wire(backend)

    result = runner.invoke(
        cli_main.app,
        [
            "chats", "set-ttl",
            "--chat-id", "5",
            "--ttl", "93d",
            "--config", str(config_path),
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["chat_id"] == 5
    assert payload["requested_ttl_seconds"] == 8035200
    assert payload["previous_ttl_seconds"] is None
    assert payload["ttl_period"] == 8035200
    assert payload["changed"] is True
    assert payload["dry_run"] is False
    assert backend.writes == [{"chat_id": 5, "period": 8035200}]


def test_off_reports_null_ttl(wire):
    backend = FakeTtlBackend(reads=[2678400, None])
    config_path = wire(backend)

    result = runner.invoke(
        cli_main.app,
        [
            "chats", "set-ttl",
            "--chat-id", "5",
            "--ttl", "off",
            "--config", str(config_path),
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["ttl_period"] is None
    assert payload["previous_ttl_seconds"] == 2678400
    assert payload["changed"] is True


def test_no_op_reports_unchanged_without_writing(wire):
    backend = FakeTtlBackend(reads=[86400])
    config_path = wire(backend)

    result = runner.invoke(
        cli_main.app,
        [
            "chats", "set-ttl",
            "--chat-id", "5",
            "--ttl", "1d",
            "--config", str(config_path),
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["changed"] is False
    assert backend.writes == []


def test_entity_reference_is_resolved(wire):
    backend = FakeTtlBackend(reads=[None, 86400])
    config_path = wire(backend, resolver=FakeResolver(chat_id=77))

    result = runner.invoke(
        cli_main.app,
        [
            "chats", "set-ttl",
            "--entity", "@team",
            "--ttl", "1d",
            "--config", str(config_path),
        ],
    )

    assert result.exit_code == 0, result.output
    assert backend.writes == [{"chat_id": 77, "period": 86400}]


# --- dry run ----------------------------------------------------------------


def test_dry_run_emits_the_standard_envelope_and_writes_nothing(wire):
    backend = FakeTtlBackend(reads=[2678400])
    config_path = wire(backend)

    result = runner.invoke(
        cli_main.app,
        [
            "chats", "set-ttl",
            "--chat-id", "5",
            "--ttl", "off",
            "--dry-run",
            "--config", str(config_path),
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["status"] == "dry_run"
    assert payload["dry_run"] is True
    assert payload["command"] == "chats.set-ttl"
    assert payload["resolved"]["previous_ttl_seconds"] == 2678400
    assert payload["resolved"]["requested_ttl_seconds"] == 0
    assert payload["resolved"]["changed"] is True
    assert payload["planned_actions"]
    assert backend.writes == []


def test_dry_run_of_a_no_op_says_so(wire):
    backend = FakeTtlBackend(reads=[None])
    config_path = wire(backend)

    result = runner.invoke(
        cli_main.app,
        [
            "chats", "set-ttl",
            "--chat-id", "5",
            "--ttl", "off",
            "--dry-run",
            "--config", str(config_path),
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["resolved"]["changed"] is False
    assert payload["planned_actions"] == []
    assert payload["warnings"]


# --- error ladder -----------------------------------------------------------


def test_access_denied_exits_3(wire):
    class Denying:
        async def require(self, chat_id, level):
            raise AccessDenied(chat_ref=chat_id, required_level=level)

    backend = FakeTtlBackend()
    config_path = wire(backend, authorizer=Denying())

    result = runner.invoke(
        cli_main.app,
        [
            "chats", "set-ttl",
            "--chat-id", "5",
            "--ttl", "off",
            "--config", str(config_path),
        ],
    )

    assert result.exit_code == 3
    assert "access denied" in result.output


def test_unresolvable_entity_exits_2(wire):
    config_path = wire(
        FakeTtlBackend(),
        resolver=FakeResolver(error=EntityNotFoundError("no such chat")),
    )

    result = runner.invoke(
        cli_main.app,
        [
            "chats", "set-ttl",
            "--entity", "@ghost",
            "--ttl", "off",
            "--config", str(config_path),
        ],
    )

    assert result.exit_code == 2
    assert "no such chat" in result.output


def test_read_back_mismatch_exits_2(wire):
    backend = FakeTtlBackend(reads=[None, 2678400])
    config_path = wire(backend)

    result = runner.invoke(
        cli_main.app,
        [
            "chats", "set-ttl",
            "--chat-id", "5",
            "--ttl", "93d",
            "--config", str(config_path),
        ],
    )

    assert result.exit_code == 2
    assert "8035200" in result.output
    assert "2678400" in result.output


def test_paced_flood_wait_exits_1_with_retry_after(wire):
    from telegram_assistant.messages import PacedFloodWaitError

    backend = FakeTtlBackend(
        reads=[None, None],
        set_error=PacedFloodWaitError(
            866, retry_after_seconds=871.0, retry_at=1.0, attempts=5
        ),
    )
    config_path = wire(backend)

    result = runner.invoke(
        cli_main.app,
        [
            "chats", "set-ttl",
            "--chat-id", "5",
            "--ttl", "off",
            "--config", str(config_path),
        ],
    )

    assert result.exit_code == 1
    assert "Retry after" in result.output


def test_unexpected_error_exits_1(wire):
    backend = FakeTtlBackend(reads=[None, None], set_error=RuntimeError("boom"))
    config_path = wire(backend)

    result = runner.invoke(
        cli_main.app,
        [
            "chats", "set-ttl",
            "--chat-id", "5",
            "--ttl", "1d",
            "--config", str(config_path),
        ],
    )

    assert result.exit_code == 1
    assert "chats set-ttl failed: boom" in result.output
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/pytest tests/test_cli_chats_set_ttl.py -q`
Expected: `AttributeError: module ... has no attribute '_build_chat_ttl_backends'`.

- [ ] **Step 3: Widen the `chats` group help and add the builders**

In `src/telegram_assistant/cli/main.py`, change the group help at line 3407-3409 from `help="Read chat metadata."` to:

```python
chats_app = typer.Typer(
    help="Read chat metadata and set the auto-delete period.", no_args_is_help=True
)
```

Then add, immediately after `_build_chat_inspect_backends` (which ends line 3442):

```python
def _build_chat_ttl_backends(config_path: Path | None):
    """Open the Telethon-backed chat-TTL + folder backends + resolver.

    Same shape as :func:`_build_chat_inspect_backends`, with the write adapter
    in place of the read one. Tests monkeypatch this to inject fakes.
    """
    config = _load_config_or_exit(config_path)
    manager = TelethonSessionManager(config.telegram)

    async def _open():
        from telegram_assistant.chats.telethon_backend import TelethonChatTtlBackend
        from telegram_assistant.entities import TelethonEntityResolver
        from telegram_assistant.folders import TelethonFolderBackend

        client = await manager.get_client()
        if not await client.is_user_authorized():
            raise RuntimeError(
                "Telethon session is not authorized; run "
                "`telegram-assistant auth` first."
            )
        return (
            TelethonChatTtlBackend(client),
            TelethonFolderBackend(client),
            TelethonEntityResolver(client),
        )

    return config, manager, _open


def _cli_ttl_pacer(config):
    """Build the auto-delete pacer for a CLI invocation.

    Mirrors :func:`_cli_pin_pacer` but on the TTL gate row and with the far
    higher wait ceiling this method needs: FLOOD_WAITs on SetHistoryTTL run into
    the hundreds of seconds, and the operator's choice was to sit through them.
    """
    from telegram_assistant.messages import Pacer

    interval = float(getattr(config.telegram, "ttl_min_interval_seconds", 0.0))
    max_wait = float(getattr(config.telegram, "ttl_max_flood_wait_seconds", 3600.0))
    retries = int(getattr(config.telegram, "ttl_max_flood_wait_retries", 5))
    gate = None
    if interval > 0:
        from telegram_assistant.persistence.rate_gate import RateGateStore

        try:
            gate = RateGateStore(default_database_path(config))
        except Exception:
            gate = None
    return Pacer(
        gate,
        min_interval_seconds=interval,
        max_flood_wait_seconds=max_wait,
        max_flood_wait_retries=retries,
    )
```

- [ ] **Step 4: Add the command after `chats_inspect`**

Append after `chats_inspect` (which ends at line 3556):

```python
@chats_app.command("set-ttl")
def chats_set_ttl(
    ttl: str = typer.Option(
        ...,
        "--ttl",
        help="New auto-delete period: 'off' (or 0), or <integer><unit> with "
        "unit s/m/h/d/w (e.g. 1d, 24h, 93d). A bare integer is seconds.",
    ),
    chat_id: int | None = typer.Option(
        None,
        "--chat-id",
        help="Numeric Telegram chat id to change.",
    ),
    chat_name: str | None = typer.Option(
        None,
        "--chat-name",
        help="Chat title (resolved within --folder-name).",
    ),
    entity: str | None = typer.Option(
        None,
        "--entity",
        help="Flexible entity reference (numeric id, @username, t.me/invite link, "
        "phone, or exact title) resolved via the shared resolver.",
    ),
    folder_name: str | None = typer.Option(
        None,
        "--folder-name",
        help="Folder used for --chat-name lookup "
        "(defaults to telegram.default_chat_folder.folder_name).",
    ),
    folder_id: int | None = typer.Option(
        None,
        "--folder-id",
        help="Optional folder id cross-check.",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Resolve the chat and report the change without writing.",
    ),
    config_path: Path | None = typer.Option(  # noqa: B008
        None,
        "--config",
        "-c",
        help="Path to config.yml (defaults: ./data/config.yml, then ~/.config/telegram-assistant/config.yml).",
        exists=False,
    ),
) -> None:
    """Set one chat's auto-delete period (WRITE-gated).

    Setting the period a chat already has writes nothing: Telegram posts a
    member-visible service message on every successful change, including a
    no-op one.
    """
    from telegram_assistant.chats import SetTtlRequest, parse_ttl, set_chat_ttl
    from telegram_assistant.folders import FolderError, resolve_chat_in_folder

    refs = sum([chat_id is not None, chat_name is not None, entity is not None])
    if refs != 1:
        typer.echo(
            "exactly one of --chat-id, --chat-name, or --entity must be supplied",
            err=True,
        )
        raise typer.Exit(code=2)

    try:
        period = parse_ttl(ttl)
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc

    config, manager, open_backends = _build_chat_ttl_backends(config_path)

    if chat_name is not None:
        resolved_folder_name, default_fid, _ = _resolve_folder_name(
            folder_name, config_path
        )
        effective_folder_id = folder_id if folder_id is not None else default_fid
    else:
        resolved_folder_name = folder_name
        effective_folder_id = folder_id

    async def _run() -> dict[str, object]:
        try:
            ttl_backend, folder_backend, resolver = await open_backends()
            if entity is not None:
                resolved_chat_id = (await resolver.resolve(entity)).chat_id
                resolved_name = entity
            elif chat_id is not None:
                resolved_chat_id = chat_id
                resolved_name = None
            else:
                resolved = await resolve_chat_in_folder(
                    folder_backend,
                    folder_name=resolved_folder_name or "",
                    chat_name=chat_name or "",
                    folder_id=effective_folder_id,
                )
                resolved_chat_id = resolved.chat_id
                resolved_name = chat_name

            authorizer = _cli_authorizer(
                config, resolver=resolver, folder_backend=folder_backend
            )
            result = await set_chat_ttl(
                backend=ttl_backend,
                request=SetTtlRequest(
                    telegram_chat_id=resolved_chat_id,
                    period=period,
                    chat_name=resolved_name,
                ),
                authorizer=authorizer,
                pacer=_cli_ttl_pacer(config),
                dry_run=dry_run,
            )
            return result.to_dict()
        finally:
            try:
                await manager.disconnect()
            except Exception:
                pass

    try:
        payload = asyncio.run(_run())
    except FolderError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc
    except ValueError as exc:
        # Bad input, an uninspectable peer, or a read-back that disagreed with
        # the request — all caller-facing, so exit 2 rather than the internal
        # exit 1. AccessDenied/EntityError are RuntimeErrors and fall through.
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc
    except Exception as exc:
        _raise_for_access_or_entity_error(exc)
        _raise_for_flood_wait(exc, "chats set-ttl")
        typer.echo(f"chats set-ttl failed: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    if dry_run:
        target = payload["chat_id"]
        scope = "off" if period == 0 else f"{period}s"
        changed = bool(payload["changed"])
        action = f"set auto-delete of chat {target} to {scope}"
        envelope = {
            "status": "dry_run",
            "dry_run": True,
            "command": "chats.set-ttl",
            "would": action if changed else f"leave chat {target} unchanged",
            "resolved": payload,
            "planned_actions": [action] if changed else [],
            "warnings": (
                []
                if changed
                else [
                    f"chat {target} already has this auto-delete period; "
                    "no write would be issued (Telegram posts a visible service "
                    "message on every successful change)"
                ]
            ),
        }
        typer.echo(json.dumps(envelope, sort_keys=True, default=str))
        return

    typer.echo(json.dumps(payload, sort_keys=True, default=str))
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_cli_chats_set_ttl.py -q`
Expected: PASS.

- [ ] **Step 6: Confirm the command is registered**

Run: `.venv/bin/telegram-assistant chats --help`
Expected: output lists both `inspect` and `set-ttl`.

- [ ] **Step 7: Confirm `--ttl` is required**

Run: `.venv/bin/telegram-assistant chats set-ttl --chat-id 5; echo "exit=$?"`
Expected: a Typer "Missing option '--ttl'" message and `exit=2`.

- [ ] **Step 8: Lint and full suite**

Run: `.venv/bin/ruff check src tests && .venv/bin/pytest -q`
Expected: `All checks passed!`; the suite passes **except** `tests/test_skill_inventory.py`, which now fails because the CLI has a command SKILL.md does not list. That is expected and is closed by Task 5.

- [ ] **Step 9: Commit**

```bash
git add src/telegram_assistant/cli/main.py tests/test_cli_chats_set_ttl.py
git commit -m "feat(cli): add chats set-ttl with dry-run and ttl pacing"
```

---

### Task 5: Documentation — SKILL.md, README, skill sync

**Files:**
- Modify: `skills/telegram-assistant/SKILL.md` (catalog row after line 223; confirmation bucket 2 at lines 74-84; the `--dry-run` supported set in algorithm step 7; a `#### chats / set-ttl` extraction section after the `chats / inspect` one ending line 580; a scenario after `### chats inspect` at line 1495)
- Modify: `README.md` (after the `chats inspect` bullet, line 105)
- Copy: `~/.claude/skills/telegram-assistant/SKILL.md`
- Test: `tests/test_skill_inventory.py` (existing guard — no new test file)

**Interfaces:**
- Consumes: the command name `chats set-ttl` and its flags from Task 4.
- Produces: nothing consumed by later tasks.

- [ ] **Step 1: Run the guard to see it fail**

Run: `.venv/bin/pytest tests/test_skill_inventory.py -q`
Expected: FAIL — `chats set-ttl` is in the CLI but not in the SKILL.md catalog.

- [ ] **Step 2: Add the catalog row**

In `skills/telegram-assistant/SKILL.md`, immediately after the `chats` / `inspect` row (line 223), add:

```markdown
| `chats` | `set-ttl` | Change a chat's auto-delete period (`--ttl off\|1d\|93d`, WRITE-gated; `--dry-run`). Setting the period the chat already has writes nothing. | `telegram-assistant chats set-ttl ...` |
```

- [ ] **Step 3: Add it to confirmation bucket 2**

In the bucket-2 list (lines 74-80), add `chats set-ttl` after `folders remove-chat`:

```markdown
2. **State-changing, single object** — `groups create`, `groups set-layout`,
   `groups rename`, `topics create`, `topics close`, `topics open`,
   `topics rename`,
   `messages send` (single chat),
   `messages react`, `messages forward`, `notifications mute`,
   `notifications unmute`, `folders add-chat`, `folders remove-chat`,
   `chats set-ttl`,
   `operations retry`. Always:
```

- [ ] **Step 4: Add it to the `--dry-run` supported set in algorithm step 7**

Find the sentence beginning "The supported set is: `groups create`," and add `chats set-ttl` to that list, after `folders remove-chat`.

- [ ] **Step 5: Add the extraction section**

After the `#### chats / inspect` section (which ends around line 580), add:

```markdown
#### `chats` / `set-ttl`

- Extract: chat reference (`--chat-id` / `--chat-name` / `--entity`) and the new
  period (`--ttl`).
- Required flags: exactly one chat reference, plus `--ttl`.
- From config: `--folder-name` default when resolving `--chat-name`.
- Temp file: no.
- Automation: none. This is a bucket-2 state change — always `--dry-run` first,
  show the plan, confirm via `AskUserQuestion`, then run for real.
- `--ttl` values: `off` (or `0`) disables auto-delete; otherwise
  `<integer><unit>` with unit `s`/`m`/`h`/`d`/`w` (`1d`, `24h`, `93d`, `2w`); a
  bare integer is seconds. Telegram's own clients offer only day/week/month, but
  arbitrary periods are accepted — real chats have been found at 31, 93 and 180
  days. There is no preset allow-list; an unacceptable value is rejected by the
  server, not by the CLI.
- Payload: `chat_id` (bare), `chat_name`, `requested_ttl_seconds`,
  `previous_ttl_seconds`, `ttl_period`, `changed`, `dry_run`.
  `previous_ttl_seconds` and `ttl_period` are `null` when auto-delete is off,
  never `0` — the same spelling `chats inspect` uses. `ttl_period` is what the
  server reported **after** the write, not what was asked for.
- **Every successful change posts a service message into the chat**, visible to
  all members. Say so in the plan before asking for confirmation — in a client
  chat that message is seen by the client.
- Setting the period a chat already has is a no-op: `changed: false`, no write,
  no service message. The `--dry-run` envelope says so in `warnings` and leaves
  `planned_actions` empty. Do not "re-apply to be sure" — that is exactly what
  would spam the chat.
- Slowness is expected. Telegram flood-waits this method hard and the waits
  escalate (261s, 703s and 866s were observed within one hour on one account).
  The command sits through them by design, up to
  `telegram.ttl_max_flood_wait_seconds` (default 3600) and
  `telegram.ttl_max_flood_wait_retries` (default 5). A command that appears to
  hang for minutes is normal — do not kill and retry it, and never run two at
  once against the same account.
- Sweeping a folder: loop the command over the chat ids from `folders inspect`,
  one chat per call, sequentially. There is no bulk mode and no folder flag —
  and running several in parallel makes the flood waits worse.
- Other surfaces: none. This is **CLI-only** — there is no HTTP route and no MCP
  tool for it.
- Typical errors: `cannot parse --ttl ...` (exit 2), `access denied ...`
  (exit 3), `chat N: requested ttl X but the server stored Y` (exit 2 — the
  server refused or clamped the value), `chats set-ttl rate-limited by
  Telegram ... Retry after Ns` (exit 1).
```

- [ ] **Step 6: Add the scenario**

After the `### chats inspect` scenario block, add:

```markdown
### `chats set-ttl`

1. Resource/action: `chats` / `set-ttl`. **Bucket 2** — never run it without a
   confirmed dry-run.
2. Read the current state first with `chats inspect` and show it: the human
   should see what the period is now before deciding what it becomes.
3. Dry-run:

```bash
telegram-assistant chats set-ttl --entity 2305069221 --ttl off --dry-run
```

4. Show the plan with three things named explicitly: the chat, the old and new
   periods, and that a service message will appear in the chat for everyone to
   see. If the dry-run reports `changed: false`, stop — there is nothing to do,
   and re-applying would post that message for no reason.
5. Confirm via `AskUserQuestion`, then re-run without `--dry-run`.
6. Expect it to be slow. Flood waits on this method run into minutes; the
   command waits them out on purpose. Do not start a second one in parallel.
7. For several chats, loop one call per chat over the ids from `folders
   inspect`, sequentially — and say up front how many chats will each get a
   service message.
```

- [ ] **Step 7: Add the README entry**

In `README.md`, after the `chats inspect` bullet (line 105), add:

```markdown
- `chats set-ttl` — set a chat's auto-delete period (WRITE-gated, supports `--dry-run`). Target with `--chat-id`/`--chat-name`/`--entity`; `--ttl` takes `off` (or `0`) or `<integer><unit>` with unit `s`/`m`/`h`/`d`/`w` (`1d`, `24h`, `93d`), a bare integer being seconds. Telegram accepts arbitrary periods, not just the day/week/month its clients offer, so there is no preset allow-list — the server rejects what it will not take. Returns `{chat_id, chat_name, requested_ttl_seconds, previous_ttl_seconds, ttl_period, changed, dry_run}`, where `ttl_period` is re-read from the server after the write rather than echoed from the request (Telegram's response to this call does not always parse, while the write still applies). Setting the period a chat already has issues **no write at all**: every successful change posts a service message visible to every member, so a re-run over a folder would otherwise spam it. Flood waits on this method escalate into the hundreds of seconds; the command sits through them, paced through the shared SQLite gate on its own row and bounded by `telegram.ttl_min_interval_seconds` (default 2.0), `telegram.ttl_max_flood_wait_seconds` (default 3600) and `telegram.ttl_max_flood_wait_retries` (default 5). CLI-only — there is no HTTP route or MCP tool.
```

Also update the last sentence of the `chats inspect` bullet, which currently reads "It reads only — there is no command to change any of these settings." Replace it with: "It reads only; `chats set-ttl` is the one write counterpart, and it covers `ttl_period` alone."

- [ ] **Step 8: Sync the skill**

```bash
cp skills/telegram-assistant/SKILL.md ~/.claude/skills/telegram-assistant/SKILL.md
diff -q skills/telegram-assistant/SKILL.md ~/.claude/skills/telegram-assistant/SKILL.md
```
Expected: no output from `diff` (files identical).

- [ ] **Step 9: Run the guard to verify it passes**

Run: `.venv/bin/pytest tests/test_skill_inventory.py -q`
Expected: PASS.

- [ ] **Step 10: Full suite and lint**

Run: `.venv/bin/ruff check src tests && .venv/bin/pytest -q`
Expected: `All checks passed!` then PASS, whole suite green.

- [ ] **Step 11: Commit**

```bash
git add skills/telegram-assistant/SKILL.md README.md
git commit -m "docs: document chats set-ttl in the skill catalog and README"
```

---

## Out of scope (deliberately)

- **HTTP route and MCP tool.** The user's constraint. `EXPECTED_TOOL_NAMES` in `tests/test_mcp_mount.py` is not touched.
- **Bulk / folder sweep.** The caller loops.
- **Live verification.** The spec's Saved-Messages check is mutating and needs explicit human approval; it is not part of this plan and must not be run on the implementer's initiative.
- **A `--wait` / `--max-wait` flag.** The decision was to always wait; the ceiling is config, not a flag.

## Self-review notes

Checked against the spec, 2026-08-06:

- Every spec section maps to a task: CLI surface → Task 4; `parse_ttl` and payload → Task 1; module layout → Tasks 1-2; order of operations (gate, read, no-op, dry-run, write, read-back, mismatch) → Task 1 with tests per clause; pacing keys and the three config knobs → Task 3; `TypeNotFoundError` tolerance → Task 2; access → Task 1; error ladder → Task 4; testing trio → Tasks 1, 2, 4; docs → Task 5.
- One layering the spec left implicit is resolved here: the domain returns a `SetTtlResult` with `dry_run=True`, and the CLI wraps it in the project-wide `status: dry_run` envelope that the other 20 dry-run sites emit, with the result under `resolved`. Emitting the bare result would have broken the "look for `status = dry_run`" instruction the skill gives the agent.
- `ttl_gate_key` is written inline in Task 1 and switched to a re-export in Task 3, so each task is independently green rather than Task 1 depending on a module Task 3 creates.
- Task 4 knowingly leaves `tests/test_skill_inventory.py` red; Task 5 closes it. That is stated in Task 4 Step 8 so a reviewer does not read it as a regression.
