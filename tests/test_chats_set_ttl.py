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
