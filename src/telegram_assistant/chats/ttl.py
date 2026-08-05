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

    async def get_ttl(self, *, chat_id: int) -> int | None: ...

    async def set_ttl(self, *, chat_id: int, period: int) -> None: ...


def _reported(period: int | None) -> int | None:
    """Normalise a wire value to the reported one: ``0`` and ``None`` are off."""
    return period or None


def ttl_gate_key(chat_id: int) -> str:
    """Gate key for TTL writes.

    Delegates to :func:`telegram_assistant.messages.pacing.ttl_pacing_key` so
    every gate key lives in one module; imported lazily to keep this module free
    of an import-time dependency on ``messages``.
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
