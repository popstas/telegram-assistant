"""CLI tests for `chats set-ttl`."""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from telegram_assistant.access import AccessDenied
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
    from telegram_assistant.worker.queue import FloodWaitError

    # NOTE two deviations from a naive transcription of the brief, both
    # required to actually exercise the pacer's exhausted-budget path rather
    # than hang or silently no-op — verified directly against `set_chat_ttl`
    # and against the equivalent `FloodPinBackend` pattern in
    # tests/test_messages_pin_surfaces.py:
    #   1. The current TTL (first read) must differ from the requested one
    #      (here: currently on at 86400s, requesting "off"). With current and
    #      requested both normalising to "off", chats/ttl.py's no-op
    #      short-circuit returns before any write, so `backend.set_ttl` would
    #      never be called and the flood-wait error would never fire.
    #   2. The backend must raise a plain `FloodWaitError`, not a pre-built
    #      `PacedFloodWaitError`: the latter is itself a `FloodWaitError`
    #      subclass, so `Pacer.run`'s own `except FloodWaitError` catches it
    #      and retries through a real `asyncio.sleep` (the pacer has no way to
    #      tell an injected already-paced-out error from a fresh one). Seconds
    #      must exceed `ttl_max_flood_wait_seconds` (default 3600.0, unset in
    #      minimal_config_yaml) so the pacer converts it to
    #      `PacedFloodWaitError` on the very first attempt, without sleeping.
    backend = FakeTtlBackend(
        reads=[86400],
        set_error=FloodWaitError(3600.0),
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
