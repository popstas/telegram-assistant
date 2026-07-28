"""Surface tests for `members list` — CLI and HTTP wiring.

The domain op is covered by ``test_members_list.py`` and the adapter by
``test_members_list_backend.py``; this module checks flag/param validation,
payload shape and the status/exit-code taxonomy.
"""

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
from telegram_assistant.entities import ResolvedEntity
from telegram_assistant.http_api import create_app
from telegram_assistant.members import MemberListResult, Participant
from telegram_assistant.persistence import OperationStore

AUTH = {"Authorization": "Bearer secret_token"}


class FakeListBackend:
    def __init__(
        self, participants: list[Participant], *, found: Participant | None = None
    ) -> None:
        self._participants = participants
        self._found = found
        self.list_calls: list[dict[str, Any]] = []
        self.get_calls: list[dict[str, Any]] = []

    async def list_participants(
        self, *, chat_id: int, limit: int, query: str | None, filter: str
    ) -> MemberListResult:
        self.list_calls.append(
            {"chat_id": chat_id, "limit": limit, "query": query, "filter": filter}
        )
        return MemberListResult(
            participants=tuple(self._participants[:limit]),
            participants_count=len(self._participants),
            truncated=len(self._participants) > limit,
        )

    async def get_participant(self, *, chat_id: int, user: str) -> Participant | None:
        self.get_calls.append({"chat_id": chat_id, "user": user})
        return self._found


class FakeResolver:
    def __init__(self, mapping: dict[str, int]) -> None:
        self._mapping = mapping

    async def resolve(self, ref: object) -> ResolvedEntity:
        return ResolvedEntity(
            chat_id=self._mapping[str(ref)], title=str(ref), kind="channel"
        )


class FakeFolderBackend:
    pass


def _participant(uid: int, *, role: str = "member", is_bot: bool = False) -> Participant:
    return Participant(
        user_id=uid,
        username=f"user{uid}",
        first_name=f"First{uid}",
        last_name=None,
        is_bot=is_bot,
        role=role,
    )


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


_READ_ACCESS = "access:\n  rules:\n    - all: true\n      permission: read\n"
_WRITE_ACCESS = "access:\n  rules:\n    - all: true\n      permission: write\n"


def _make_store() -> OperationStore:
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".db")
    tmp.close()
    return OperationStore(Path(tmp.name))


def _write_config(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "config.yml"
    path.write_text(text, encoding="utf-8")
    return path


class _DummyManager:
    async def disconnect(self) -> None:
        return None


def _patch_cli_backends(
    monkeypatch: pytest.MonkeyPatch,
    backend: FakeListBackend,
    *,
    resolver: FakeResolver | None = None,
) -> None:
    def _factory(config_path):
        config = cli_main._load_config_or_exit(config_path)

        async def _open():
            return (backend, FakeFolderBackend(), resolver or FakeResolver({}))

        return config, _DummyManager(), _open

    monkeypatch.setattr(cli_main, "_build_member_list_backends", _factory)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_cli_members_list_happy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_file = _write_config(tmp_path, _config_with_access(_READ_ACCESS))
    backend = FakeListBackend([_participant(i) for i in range(1, 4)])
    _patch_cli_backends(monkeypatch, backend)

    result = CliRunner().invoke(
        cli_main.app,
        [
            "members",
            "list",
            "--chat-id",
            "-100123",
            "--limit",
            "2",
            "--config",
            str(config_file),
        ],
    )

    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload["telegram_chat_id"] == -100123
    assert payload["count"] == 2
    assert payload["participants_count"] == 3
    assert payload["truncated"] is True
    assert payload["filter"] == "all"
    assert backend.list_calls == [
        {"chat_id": -100123, "limit": 2, "query": None, "filter": "all"}
    ]


def test_cli_members_list_user_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_file = _write_config(tmp_path, _config_with_access(_READ_ACCESS))
    backend = FakeListBackend([], found=_participant(7, is_bot=True))
    _patch_cli_backends(monkeypatch, backend)

    result = CliRunner().invoke(
        cli_main.app,
        [
            "members",
            "list",
            "--chat-id",
            "-100123",
            "--user",
            "@pressfinity_news_bot",
            "--config",
            str(config_file),
        ],
    )

    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload["is_member"] is True
    assert payload["user"] == "@pressfinity_news_bot"
    assert backend.get_calls == [{"chat_id": -100123, "user": "@pressfinity_news_bot"}]


def test_cli_members_list_resolves_entity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_file = _write_config(tmp_path, _config_with_access(_READ_ACCESS))
    backend = FakeListBackend([_participant(1)])
    _patch_cli_backends(monkeypatch, backend, resolver=FakeResolver({"@team": -100999}))

    result = CliRunner().invoke(
        cli_main.app,
        ["members", "list", "--entity", "@team", "--config", str(config_file)],
    )

    assert result.exit_code == 0, result.stdout
    assert backend.list_calls[-1]["chat_id"] == -100999


def test_cli_members_list_requires_exactly_one_ref(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_file = _write_config(tmp_path, _config_with_access(_READ_ACCESS))
    _patch_cli_backends(monkeypatch, FakeListBackend([]))

    result = CliRunner().invoke(
        cli_main.app, ["members", "list", "--config", str(config_file)]
    )

    assert result.exit_code == 2


def test_cli_members_list_rejects_user_with_query(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_file = _write_config(tmp_path, _config_with_access(_READ_ACCESS))
    backend = FakeListBackend([])
    _patch_cli_backends(monkeypatch, backend)

    result = CliRunner().invoke(
        cli_main.app,
        [
            "members",
            "list",
            "--chat-id",
            "-100123",
            "--user",
            "@bot",
            "--query",
            "bot",
            "--config",
            str(config_file),
        ],
    )

    assert result.exit_code == 2
    assert backend.get_calls == []


def test_cli_members_list_rejects_unknown_filter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_file = _write_config(tmp_path, _config_with_access(_READ_ACCESS))
    backend = FakeListBackend([])
    _patch_cli_backends(monkeypatch, backend)

    result = CliRunner().invoke(
        cli_main.app,
        [
            "members",
            "list",
            "--chat-id",
            "-100123",
            "--filter",
            "kicked",
            "--config",
            str(config_file),
        ],
    )

    assert result.exit_code == 2
    assert backend.list_calls == []


def test_cli_members_list_access_denied_exits_3(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_file = _write_config(tmp_path, _config_with_access(_WRITE_ACCESS))
    backend = FakeListBackend([_participant(1)])
    _patch_cli_backends(monkeypatch, backend)

    result = CliRunner().invoke(
        cli_main.app,
        ["members", "list", "--chat-id", "-100123", "--config", str(config_file)],
    )

    assert result.exit_code == 3
    assert backend.list_calls == []


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


def _http_client(
    *,
    access_block: str | None = None,
    backend: FakeListBackend | None = None,
    resolver: FakeResolver | None = None,
    has_factory: bool = True,
) -> TestClient:
    config = load_config_from_text(_config_with_access(access_block))
    app = create_app(
        config,
        session_manager=None,
        member_list_backend_factory=(
            (lambda _r: backend) if has_factory else (lambda _r: None)
        ),
        folder_backend_factory=lambda _r: None,
        resolver_factory=(
            (lambda _r: resolver) if resolver is not None else (lambda _r: None)
        ),
        operation_store=_make_store(),
    )
    return TestClient(app)


def test_http_members_list_returns_rows() -> None:
    backend = FakeListBackend([_participant(i) for i in range(1, 4)])
    client = _http_client(access_block=_READ_ACCESS, backend=backend)

    resp = client.get(
        "/telegram/members/list",
        params={"chat_id": -100123, "limit": 2},
        headers=AUTH,
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["telegram_chat_id"] == -100123
    assert body["count"] == 2
    assert body["participants_count"] == 3
    assert body["truncated"] is True
    assert body["participants"][0]["user_id"] == 1
    assert "is_member" not in body


def test_http_members_list_user_check() -> None:
    backend = FakeListBackend([], found=_participant(7, is_bot=True))
    client = _http_client(access_block=_READ_ACCESS, backend=backend)

    resp = client.get(
        "/telegram/members/list",
        params={"chat_id": -100123, "user": "@pressfinity_news_bot"},
        headers=AUTH,
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["is_member"] is True


def test_http_members_list_resolves_entity() -> None:
    backend = FakeListBackend([_participant(1)])
    client = _http_client(
        access_block=_READ_ACCESS,
        backend=backend,
        resolver=FakeResolver({"@team": -100999}),
    )

    resp = client.get(
        "/telegram/members/list", params={"entity": "@team"}, headers=AUTH
    )

    assert resp.status_code == 200, resp.text
    assert backend.list_calls[-1]["chat_id"] == -100999


def test_http_members_list_requires_exactly_one_ref() -> None:
    client = _http_client(access_block=_READ_ACCESS, backend=FakeListBackend([]))
    resp = client.get("/telegram/members/list", headers=AUTH)
    assert resp.status_code == 400


def test_http_members_list_rejects_unknown_filter() -> None:
    backend = FakeListBackend([])
    client = _http_client(access_block=_READ_ACCESS, backend=backend)

    resp = client.get(
        "/telegram/members/list",
        params={"chat_id": -100123, "filter": "kicked"},
        headers=AUTH,
    )

    assert resp.status_code == 400
    assert backend.list_calls == []


def test_http_members_list_denied_without_read() -> None:
    backend = FakeListBackend([_participant(1)])
    client = _http_client(access_block=_WRITE_ACCESS, backend=backend)

    resp = client.get(
        "/telegram/members/list", params={"chat_id": -100123}, headers=AUTH
    )

    assert resp.status_code == 403
    assert backend.list_calls == []


def test_http_members_list_503_without_backend() -> None:
    client = _http_client(access_block=_READ_ACCESS, has_factory=False)
    resp = client.get(
        "/telegram/members/list", params={"chat_id": -100123}, headers=AUTH
    )
    assert resp.status_code == 503
