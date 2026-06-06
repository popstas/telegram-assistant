"""Tests for Task 7 — message forwarding (domain, Telethon, CLI, HTTP)."""

from __future__ import annotations

import json
import tempfile
import textwrap
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from telegram_assistant.cli import main as cli_main
from telegram_assistant.config import load_config_from_text
from telegram_assistant.http_api import create_app
from telegram_assistant.messages import (
    ForwardMessagesRequest,
    forward_messages,
)
from telegram_assistant.messages.telethon_backend import TelethonForwardBackend
from telegram_assistant.persistence import OperationStore
from telegram_assistant.worker.queue import FloodWaitError

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeForwardBackend:
    def __init__(self, *, returned_ids: list[int] | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self._returned_ids = returned_ids

    async def forward_messages(
        self,
        *,
        from_chat_id: int,
        to_chat_id: int,
        message_ids: tuple[int, ...],
    ) -> list[int]:
        self.calls.append(
            {
                "from_chat_id": from_chat_id,
                "to_chat_id": to_chat_id,
                "message_ids": list(message_ids),
            }
        )
        if self._returned_ids is not None:
            return list(self._returned_ids)
        # Default: pretend the target assigned ids 1000+ in order.
        return [1000 + i for i, _ in enumerate(message_ids)]


# ---------------------------------------------------------------------------
# Domain tests
# ---------------------------------------------------------------------------


async def test_forward_passes_ids_to_backend() -> None:
    backend = FakeForwardBackend(returned_ids=[501, 502])
    result = await forward_messages(
        backend,
        request=ForwardMessagesRequest(
            from_chat_id=-100,
            to_chat_id=-200,
            message_ids=(1, 2),
            from_chat_name="Source",
            to_chat_name="Target",
        ),
    )
    assert result.from_chat_id == -100
    assert result.to_chat_id == -200
    assert result.source_message_ids == [1, 2]
    assert result.telegram_message_ids == [501, 502]
    assert result.from_chat_name == "Source"
    assert result.to_chat_name == "Target"
    assert backend.calls == [
        {"from_chat_id": -100, "to_chat_id": -200, "message_ids": [1, 2]}
    ]


async def test_forward_rejects_empty_message_ids() -> None:
    backend = FakeForwardBackend()
    with pytest.raises(ValueError):
        await forward_messages(
            backend,
            request=ForwardMessagesRequest(
                from_chat_id=-100, to_chat_id=-200, message_ids=()
            ),
        )
    assert backend.calls == []


async def test_forward_rejects_non_positive_message_id() -> None:
    backend = FakeForwardBackend()
    with pytest.raises(ValueError):
        await forward_messages(
            backend,
            request=ForwardMessagesRequest(
                from_chat_id=-100, to_chat_id=-200, message_ids=(1, 0)
            ),
        )
    assert backend.calls == []


class _FakeResolved:
    def __init__(self, chat_id: int) -> None:
        self.chat_id = chat_id


class _FakeResolver:
    """Resolve a rule's ``chat`` ref to the bare id the index keys on.

    Mirrors ``EntityRef.numeric_id`` (and the access layer's
    ``_canonical_chat_id``): the marked ``-200`` rule ref maps to bare ``200``,
    matching how the request's ``-200`` target id canonicalises before lookup.
    """

    async def resolve(self, ref: Any) -> _FakeResolved:
        text = str(ref)
        if text.startswith("-100") and text[4:].isdigit():
            return _FakeResolved(int(text[4:]))
        return _FakeResolved(abs(int(ref)))


async def test_forward_denied_on_source_read() -> None:
    from telegram_assistant.access import AccessDenied, Authorizer
    from telegram_assistant.config.models import AccessConfig, AccessRule

    backend = FakeForwardBackend()
    # WRITE only on the target chat; nothing grants READ on the source.
    authorizer = Authorizer(
        AccessConfig(rules=[AccessRule(chat=-200, permission="write")]),
        resolver=_FakeResolver(),
    )
    with pytest.raises(AccessDenied):
        await forward_messages(
            backend,
            request=ForwardMessagesRequest(
                from_chat_id=-100, to_chat_id=-200, message_ids=(1,)
            ),
            authorizer=authorizer,
        )
    assert backend.calls == []


async def test_forward_denied_on_target_write() -> None:
    from telegram_assistant.access import AccessDenied, Authorizer
    from telegram_assistant.config.models import AccessConfig, AccessRule

    backend = FakeForwardBackend()
    # READ everywhere but only READ on the target -> denied on target WRITE.
    authorizer = Authorizer(
        AccessConfig(rules=[AccessRule(all=True, permission="read")])
    )
    with pytest.raises(AccessDenied):
        await forward_messages(
            backend,
            request=ForwardMessagesRequest(
                from_chat_id=-100, to_chat_id=-200, message_ids=(1,)
            ),
            authorizer=authorizer,
        )
    assert backend.calls == []


async def test_forward_allowed_with_read_source_write_target() -> None:
    from telegram_assistant.access import Authorizer
    from telegram_assistant.config.models import AccessConfig, AccessRule

    backend = FakeForwardBackend(returned_ids=[7])
    authorizer = Authorizer(
        AccessConfig(
            rules=[
                AccessRule(all=True, permission="read"),
                AccessRule(chat=-200, permission="write"),
            ]
        ),
        resolver=_FakeResolver(),
    )
    result = await forward_messages(
        backend,
        request=ForwardMessagesRequest(
            from_chat_id=-100, to_chat_id=-200, message_ids=(1,)
        ),
        authorizer=authorizer,
    )
    assert result.telegram_message_ids == [7]
    assert len(backend.calls) == 1


# ---------------------------------------------------------------------------
# Telethon adapter tests (fake client recording the call)
# ---------------------------------------------------------------------------


class FakeTelethonClient:
    def __init__(self, *, sent: Any = None, raise_on_forward: Exception | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self._sent = sent
        self._raise = raise_on_forward

    async def get_input_entity(self, chat_id: int) -> str:
        return f"peer:{chat_id}"

    async def forward_messages(self, entity: Any, messages: Any, *, from_peer: Any) -> Any:
        if self._raise is not None:
            raise self._raise
        self.calls.append(
            {"entity": entity, "messages": messages, "from_peer": from_peer}
        )
        return self._sent


class _FakeMessage:
    def __init__(self, mid: int) -> None:
        self.id = mid


async def test_telethon_forward_single_message() -> None:
    client = FakeTelethonClient(sent=_FakeMessage(55))
    backend = TelethonForwardBackend(client)
    ids = await backend.forward_messages(
        from_chat_id=-100, to_chat_id=-200, message_ids=(1,)
    )
    assert ids == [55]
    assert client.calls[0]["entity"] == "peer:-200"
    assert client.calls[0]["from_peer"] == "peer:-100"
    assert client.calls[0]["messages"] == [1]


async def test_telethon_forward_album_returns_list() -> None:
    client = FakeTelethonClient(sent=[_FakeMessage(55), _FakeMessage(56)])
    backend = TelethonForwardBackend(client)
    ids = await backend.forward_messages(
        from_chat_id=-100, to_chat_id=-200, message_ids=(1, 2)
    )
    assert ids == [55, 56]


async def test_telethon_forward_translates_flood_wait() -> None:
    class _Flood(Exception):
        def __init__(self) -> None:
            self.seconds = 7

    _Flood.__name__ = "FloodWaitError"

    client = FakeTelethonClient(raise_on_forward=_Flood())
    backend = TelethonForwardBackend(client)
    with pytest.raises(FloodWaitError):
        await backend.forward_messages(
            from_chat_id=-100, to_chat_id=-200, message_ids=(1,)
        )


# ---------------------------------------------------------------------------
# CLI tests
# ---------------------------------------------------------------------------


def _write_config(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "config.yml"
    p.write_text(body)
    return p


def _patch_cli_forward_backends(
    monkeypatch: pytest.MonkeyPatch,
    backend: FakeForwardBackend,
) -> None:
    class _FakeManager:
        async def disconnect(self) -> None:
            return None

        async def get_client(self) -> Any:  # pragma: no cover - unused without entity
            raise NotImplementedError

    def _factory(config_path: Path | None) -> Any:
        from telegram_assistant.config import load_config

        config = load_config(config_path)

        async def _open() -> Any:
            return backend, None

        return config, _FakeManager(), _open

    monkeypatch.setattr(cli_main, "_build_forward_backends", _factory)


def test_cli_forward_dry_run(
    minimal_config_yaml: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_file = _write_config(tmp_path, minimal_config_yaml)
    backend = FakeForwardBackend()
    _patch_cli_forward_backends(monkeypatch, backend)

    runner = CliRunner()
    result = runner.invoke(
        cli_main.app,
        [
            "messages",
            "forward",
            "--from-chat-id",
            "-100",
            "--to-chat-id",
            "-200",
            "--message-id",
            "1",
            "--message-id",
            "2",
            "--dry-run",
            "--config",
            str(config_file),
        ],
    )
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload["dry_run"] is True
    assert payload["command"] == "messages.forward"
    assert payload["resolved"]["from_chat_id"] == -100
    assert payload["resolved"]["to_chat_id"] == -200
    assert payload["resolved"]["message_ids"] == [1, 2]
    assert backend.calls == []


def test_cli_forward_real(
    minimal_config_yaml: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_file = _write_config(tmp_path, minimal_config_yaml)
    backend = FakeForwardBackend(returned_ids=[900, 901])
    _patch_cli_forward_backends(monkeypatch, backend)

    runner = CliRunner()
    result = runner.invoke(
        cli_main.app,
        [
            "messages",
            "forward",
            "--from-chat-id",
            "-100",
            "--to-chat-id",
            "-200",
            "--message-id",
            "1",
            "--message-id",
            "2",
            "--config",
            str(config_file),
        ],
    )
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload["telegram_message_ids"] == [900, 901]
    assert payload["from_chat_id"] == -100
    assert payload["to_chat_id"] == -200
    assert backend.calls == [
        {"from_chat_id": -100, "to_chat_id": -200, "message_ids": [1, 2]}
    ]


def test_cli_forward_requires_message_id(
    minimal_config_yaml: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_file = _write_config(tmp_path, minimal_config_yaml)
    backend = FakeForwardBackend()
    _patch_cli_forward_backends(monkeypatch, backend)

    runner = CliRunner()
    result = runner.invoke(
        cli_main.app,
        [
            "messages",
            "forward",
            "--from-chat-id",
            "-100",
            "--to-chat-id",
            "-200",
            "--config",
            str(config_file),
        ],
    )
    assert result.exit_code == 2


def test_cli_forward_rejects_two_sources(
    minimal_config_yaml: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_file = _write_config(tmp_path, minimal_config_yaml)
    backend = FakeForwardBackend()
    _patch_cli_forward_backends(monkeypatch, backend)

    runner = CliRunner()
    result = runner.invoke(
        cli_main.app,
        [
            "messages",
            "forward",
            "--from-chat-id",
            "-100",
            "--from-entity",
            "@other",
            "--to-chat-id",
            "-200",
            "--message-id",
            "1",
            "--config",
            str(config_file),
        ],
    )
    assert result.exit_code == 2


# ---------------------------------------------------------------------------
# HTTP tests
# ---------------------------------------------------------------------------

AUTH = {"Authorization": "Bearer secret_token"}


def _make_store() -> OperationStore:
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".db")
    tmp.close()
    return OperationStore(Path(tmp.name))


def _config_with_access(access_block: str | None) -> str:
    base = textwrap.dedent(
        """
        telegram:
          api_id: 123456
          api_hash: "telegram_api_hash"
          session_path: /data/telegram-assistant.session
          default_chat_folder:
            folder_id: 2
            folder_name: "Planfix clients"
        {access}
        http:
          host: "0.0.0.0"
          port: 8085
          bearer_token: "secret_token"
        logging:
          level: INFO
        """
    )
    indented = ""
    if access_block is not None:
        indented = textwrap.indent(access_block, "  ")
    return base.format(access=indented).strip()


def _http_client(
    *,
    access_block: str | None = None,
    forward_backend: FakeForwardBackend | None = None,
    has_factory: bool = True,
) -> TestClient:
    config = load_config_from_text(_config_with_access(access_block))
    app = create_app(
        config,
        session_manager=None,
        forward_backend_factory=(
            (lambda _r: forward_backend) if has_factory else (lambda _r: None)
        ),
        folder_backend_factory=lambda _r: None,
        resolver_factory=lambda _r: None,
        operation_store=_make_store(),
    )
    return TestClient(app)


def test_http_forward_success() -> None:
    backend = FakeForwardBackend(returned_ids=[11, 12])
    client = _http_client(forward_backend=backend)
    resp = client.post(
        "/telegram/messages/forward",
        json={
            "from_chat_id": -100,
            "to_chat_id": -200,
            "message_ids": [1, 2],
        },
        headers=AUTH,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["telegram_message_ids"] == [11, 12]
    assert body["source_message_ids"] == [1, 2]
    assert body["from_chat_id"] == -100
    assert body["to_chat_id"] == -200
    assert len(backend.calls) == 1


def test_http_forward_requires_auth() -> None:
    backend = FakeForwardBackend()
    client = _http_client(forward_backend=backend)
    resp = client.post(
        "/telegram/messages/forward",
        json={"from_chat_id": -100, "to_chat_id": -200, "message_ids": [1]},
    )
    assert resp.status_code == 401


def test_http_forward_503_when_backend_unavailable() -> None:
    client = _http_client(has_factory=False)
    resp = client.post(
        "/telegram/messages/forward",
        json={"from_chat_id": -100, "to_chat_id": -200, "message_ids": [1]},
        headers=AUTH,
    )
    assert resp.status_code == 503, resp.text


def test_http_forward_422_empty_ids() -> None:
    backend = FakeForwardBackend()
    client = _http_client(forward_backend=backend)
    resp = client.post(
        "/telegram/messages/forward",
        json={"from_chat_id": -100, "to_chat_id": -200, "message_ids": []},
        headers=AUTH,
    )
    assert resp.status_code == 422, resp.text
    assert backend.calls == []


def test_http_forward_403_when_denied() -> None:
    backend = FakeForwardBackend()
    client = _http_client(
        access_block="access:\n  rules: []\n", forward_backend=backend
    )
    resp = client.post(
        "/telegram/messages/forward",
        json={"from_chat_id": -100, "to_chat_id": -200, "message_ids": [1]},
        headers=AUTH,
    )
    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"]["error"] == "access_denied"
    assert backend.calls == []
