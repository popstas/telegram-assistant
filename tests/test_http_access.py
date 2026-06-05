"""HTTP tests for Task 4 — entity resolution, the get-recent read op, and the
access gate surfaced over HTTP.

Covers:

* ``GET /telegram/messages/recent`` (limit default/override, 400 on bad refs);
* ``AccessDenied`` → 403 on a deny-by-default policy (read and write);
* entity not-found → 404 and ambiguous → 409 on the resolver path;
* ``POST /telegram/messages`` resolving an ``entity`` reference.
"""

from __future__ import annotations

import tempfile
import textwrap
from pathlib import Path

from fastapi.testclient import TestClient

from telegram_assistant.config import load_config_from_text
from telegram_assistant.entities import (
    AmbiguousEntityError,
    EntityNotFoundError,
    ResolvedEntity,
)
from telegram_assistant.http_api import create_app
from telegram_assistant.messages import RecentMessage
from telegram_assistant.persistence import OperationStore

AUTH = {"Authorization": "Bearer secret_token"}


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


def _make_store() -> OperationStore:
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".db")
    tmp.close()
    return OperationStore(Path(tmp.name))


class FakeReadBackend:
    def __init__(self, messages: list[RecentMessage]) -> None:
        self._messages = messages
        self.calls: list[dict[str, int]] = []

    async def get_recent_messages(
        self, *, chat_id: int, limit: int = 5
    ) -> list[RecentMessage]:
        self.calls.append({"chat_id": chat_id, "limit": limit})
        return self._messages[:limit]


class FakeMessageBackend:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str, int | None]] = []

    async def send_message(
        self, *, chat_id: int, text: str, topic_id: int | None = None
    ) -> int:
        self.sent.append((chat_id, text, topic_id))
        return 555


class FakeResolver:
    def __init__(self, *, mapping=None, error: Exception | None = None) -> None:
        self._mapping = mapping or {}
        self._error = error

    async def resolve(self, ref) -> ResolvedEntity:
        if self._error is not None:
            raise self._error
        chat_id = self._mapping[str(ref)]
        return ResolvedEntity(chat_id=chat_id, title=str(ref), kind="channel")


def _messages(n: int) -> list[RecentMessage]:
    return [
        RecentMessage(id=i, sender=f"u{i}", date=None, reply_to=None, text=f"m{i}")
        for i in range(1, n + 1)
    ]


def _client(
    access_block: str | None,
    *,
    read_backend=None,
    message_backend=None,
    resolver=None,
    store=None,
) -> TestClient:
    config = load_config_from_text(_config_with_access(access_block))
    app = create_app(
        config,
        session_manager=None,
        folder_backend_factory=lambda _r: None,
        message_backend_factory=(lambda _r: message_backend)
        if message_backend is not None
        else None,
        message_read_backend_factory=(lambda _r: read_backend)
        if read_backend is not None
        else (lambda _r: None),
        resolver_factory=(lambda _r: resolver)
        if resolver is not None
        else (lambda _r: None),
        operation_store=store or _make_store(),
    )
    return TestClient(app)


# ---------------------------------------------------------------------------
# get-recent (allow-all)
# ---------------------------------------------------------------------------


def test_recent_default_limit_five() -> None:
    backend = FakeReadBackend(_messages(10))
    client = _client(None, read_backend=backend)
    resp = client.get("/telegram/messages/recent", params={"chat_id": -100}, headers=AUTH)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["count"] == 5
    assert backend.calls == [{"chat_id": -100, "limit": 5}]


def test_recent_limit_override() -> None:
    backend = FakeReadBackend(_messages(10))
    client = _client(None, read_backend=backend)
    resp = client.get(
        "/telegram/messages/recent",
        params={"chat_id": -100, "limit": 3},
        headers=AUTH,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["count"] == 3
    assert backend.calls[-1]["limit"] == 3


def test_recent_requires_exactly_one_ref() -> None:
    backend = FakeReadBackend(_messages(3))
    client = _client(None, read_backend=backend)
    resp = client.get("/telegram/messages/recent", headers=AUTH)
    assert resp.status_code == 400, resp.text
    resp2 = client.get(
        "/telegram/messages/recent",
        params={"chat_id": -1, "entity": "@x"},
        headers=AUTH,
    )
    assert resp2.status_code == 400


def test_recent_rejects_nonpositive_limit() -> None:
    backend = FakeReadBackend(_messages(3))
    client = _client(None, read_backend=backend)
    resp = client.get(
        "/telegram/messages/recent",
        params={"chat_id": -1, "limit": 0},
        headers=AUTH,
    )
    assert resp.status_code == 400


def test_recent_via_entity() -> None:
    backend = FakeReadBackend(_messages(2))
    resolver = FakeResolver(mapping={"@client": -100777})
    client = _client(None, read_backend=backend, resolver=resolver)
    resp = client.get(
        "/telegram/messages/recent", params={"entity": "@client"}, headers=AUTH
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["telegram_chat_id"] == -100777
    assert backend.calls[-1]["chat_id"] == -100777


# ---------------------------------------------------------------------------
# access gate
# ---------------------------------------------------------------------------


def test_recent_denied_when_policy_present() -> None:
    backend = FakeReadBackend(_messages(5))
    # Deny-by-default: a present-but-empty policy grants nothing.
    client = _client("access:\n  rules: []\n", read_backend=backend)
    resp = client.get(
        "/telegram/messages/recent", params={"chat_id": -100}, headers=AUTH
    )
    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"]["error"] == "access_denied"
    assert resp.json()["detail"]["required_level"] == "read"
    assert backend.calls == []


def test_recent_allowed_by_wildcard_read() -> None:
    backend = FakeReadBackend(_messages(5))
    access = "access:\n  rules:\n    - all: true\n      permission: read\n"
    client = _client(access, read_backend=backend)
    resp = client.get(
        "/telegram/messages/recent", params={"chat_id": -100}, headers=AUTH
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["count"] == 5


def test_send_denied_when_policy_present() -> None:
    msg = FakeMessageBackend()
    client = _client("access:\n  rules: []\n", message_backend=msg)
    resp = client.post(
        "/telegram/messages",
        json={"text": "hi", "telegram_chat_id": -100},
        headers=AUTH,
    )
    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"]["error"] == "access_denied"
    assert resp.json()["detail"]["required_level"] == "write"
    assert msg.sent == []


def test_send_read_only_rule_denies_write() -> None:
    msg = FakeMessageBackend()
    access = "access:\n  rules:\n    - all: true\n      permission: read\n"
    client = _client(access, message_backend=msg)
    resp = client.post(
        "/telegram/messages",
        json={"text": "hi", "telegram_chat_id": -100},
        headers=AUTH,
    )
    assert resp.status_code == 403, resp.text
    assert msg.sent == []


def test_send_allowed_by_wildcard_write() -> None:
    msg = FakeMessageBackend()
    access = "access:\n  rules:\n    - all: true\n      permission: write\n"
    client = _client(access, message_backend=msg)
    resp = client.post(
        "/telegram/messages",
        json={"text": "hi", "telegram_chat_id": -100},
        headers=AUTH,
    )
    assert resp.status_code == 200, resp.text
    assert msg.sent == [(-100, "hi", None)]


# ---------------------------------------------------------------------------
# entity errors
# ---------------------------------------------------------------------------


def test_send_entity_not_found_404() -> None:
    msg = FakeMessageBackend()
    resolver = FakeResolver(error=EntityNotFoundError("nope"))
    client = _client(None, message_backend=msg, resolver=resolver)
    resp = client.post(
        "/telegram/messages",
        json={"text": "hi", "entity": "@ghost"},
        headers=AUTH,
    )
    assert resp.status_code == 404, resp.text
    assert msg.sent == []


def test_send_entity_ambiguous_409() -> None:
    msg = FakeMessageBackend()
    resolver = FakeResolver(error=AmbiguousEntityError(ref="Team", matches=[1, 2]))
    client = _client(None, message_backend=msg, resolver=resolver)
    resp = client.post(
        "/telegram/messages",
        json={"text": "hi", "entity": "Team"},
        headers=AUTH,
    )
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["error"] == "ambiguous_entity"
    assert detail["matches"] == [1, 2]


def test_send_via_entity_resolves_and_sends() -> None:
    msg = FakeMessageBackend()
    resolver = FakeResolver(mapping={"@client": -100222})
    client = _client(None, message_backend=msg, resolver=resolver)
    resp = client.post(
        "/telegram/messages",
        json={"text": "hi", "entity": "@client"},
        headers=AUTH,
    )
    assert resp.status_code == 200, resp.text
    assert msg.sent == [(-100222, "hi", None)]


def test_recent_503_when_read_backend_unavailable() -> None:
    client = _client(None, read_backend=None)
    resp = client.get(
        "/telegram/messages/recent", params={"chat_id": -1}, headers=AUTH
    )
    assert resp.status_code == 503, resp.text
