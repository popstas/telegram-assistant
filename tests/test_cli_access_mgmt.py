"""CLI tests for Task 4 — the ``access`` command group.

``access list`` reads the loaded config (no Telegram); ``access check`` resolves
a chat via a monkeypatched resolver/folder backend and reports the grant
verdict + exit code; ``access add`` appends a validated rule to the config file
and round-trips through ``load_config``.
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from telegram_assistant.cli import main as cli_main
from telegram_assistant.config import load_config
from telegram_assistant.entities import EntityNotFoundError, ResolvedEntity


def _write_config(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "config.yml"
    p.write_text(body)
    return p


def _with_access(minimal_config_yaml: str, access_block: str) -> str:
    """Insert a ``telegram.access`` block before ``default_chat_folder``."""
    indented = textwrap.indent(access_block, "  ")
    return minimal_config_yaml.replace(
        "  default_chat_folder:",
        indented + "  default_chat_folder:",
    )


class _FakeManager:
    async def get_client(self) -> Any:  # pragma: no cover - not used by fakes
        return object()

    async def disconnect(self) -> None:
        return None


class FakeFolderBackend:
    async def list_folders(self) -> list:
        return []


class FakeResolver:
    def __init__(self, *, mapping=None, error: Exception | None = None) -> None:
        self._mapping = mapping or {}
        self._error = error

    async def resolve(self, ref) -> ResolvedEntity:
        if self._error is not None:
            raise self._error
        return ResolvedEntity(
            chat_id=self._mapping[str(ref)], title=str(ref), kind="channel"
        )


def _patch_access_resolver(
    monkeypatch: pytest.MonkeyPatch, resolver: FakeResolver
) -> None:
    def _factory(config_path: Path | None) -> Any:
        config = load_config(config_path)

        async def _open() -> Any:
            return resolver, FakeFolderBackend()

        return config, _FakeManager(), _open

    monkeypatch.setattr(cli_main, "_build_access_resolver", _factory)


def _run(args: list[str]) -> Any:
    return CliRunner().invoke(cli_main.app, args)


# ---------------------------------------------------------------------------
# access list
# ---------------------------------------------------------------------------


def test_list_allow_all_when_access_unset(
    minimal_config_yaml: str, tmp_path: Path
) -> None:
    cfg = _write_config(tmp_path, minimal_config_yaml)
    result = _run(["access", "list", "--config", str(cfg)])
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload["policy"] == "allow_all"
    assert payload["rules"] == []


def test_list_deny_by_default_rules(
    minimal_config_yaml: str, tmp_path: Path
) -> None:
    access = textwrap.dedent(
        """
        access:
          rules:
            - all: true
              permission: read
            - chats:
                - "@a"
                - "@b"
              permissions:
                - write
                - delete
        """
    ).strip() + "\n"
    cfg = _write_config(tmp_path, _with_access(minimal_config_yaml, access))
    result = _run(["access", "list", "--config", str(cfg)])
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload["policy"] == "deny_by_default"
    assert payload["rule_count"] == 2
    assert payload["rules"][0]["target"] == {"kind": "all"}
    assert payload["rules"][0]["permissions"] == ["read"]
    assert payload["rules"][1]["target"] == {
        "kind": "chat",
        "chats": ["@a", "@b"],
    }
    assert payload["rules"][1]["permissions"] == ["write", "delete"]


def test_list_renders_folder_and_folder_id_targets(
    minimal_config_yaml: str, tmp_path: Path
) -> None:
    """`access list` is the only inspection surface — it must show id rules.

    A `folder_id` rule used to fall through to the chat branch and print an
    empty chat rule, so an operator auditing an id-scoped policy saw a rule
    targeting nothing.
    """
    access = textwrap.dedent(
        """
        access:
          rules:
            - folder: "Clients"
              permission: read
            - folder_id: 5
              permissions:
                - read
                - write
        """
    ).strip() + "\n"
    cfg = _write_config(tmp_path, _with_access(minimal_config_yaml, access))
    result = _run(["access", "list", "--config", str(cfg)])
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload["rules"][0]["target"] == {"kind": "folder", "folder": "Clients"}
    assert payload["rules"][1]["target"] == {"kind": "folder_id", "folder_id": 5}
    assert payload["rules"][1]["permissions"] == ["read", "write"]


# ---------------------------------------------------------------------------
# access check
# ---------------------------------------------------------------------------


def test_check_granted_exit_0(
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
    _patch_access_resolver(monkeypatch, FakeResolver(mapping={"@client": 555}))

    result = _run(
        ["access", "check", "--entity", "@client", "--permission", "write",
         "--config", str(cfg)]
    )
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload["granted"] is True
    assert payload["matched_rule"] == "chat"
    assert payload["granted_permissions"] == ["read", "write"]


def test_check_denied_exit_3(
    minimal_config_yaml: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # write-only: read is NOT implied (independent capabilities).
    access = textwrap.dedent(
        """
        access:
          rules:
            - chat: "@client"
              permission: write
        """
    ).strip() + "\n"
    cfg = _write_config(tmp_path, _with_access(minimal_config_yaml, access))
    _patch_access_resolver(monkeypatch, FakeResolver(mapping={"@client": 555}))

    result = _run(
        ["access", "check", "--entity", "@client", "--permission", "read",
         "--config", str(cfg)]
    )
    assert result.exit_code == cli_main.ACCESS_DENIED_EXIT_CODE, result.stdout
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload["granted"] is False


def test_check_allow_all_when_access_unset(
    minimal_config_yaml: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _write_config(tmp_path, minimal_config_yaml)
    _patch_access_resolver(monkeypatch, FakeResolver(mapping={"@x": 1}))
    result = _run(
        ["access", "check", "--entity", "@x", "--permission", "delete",
         "--config", str(cfg)]
    )
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload["granted"] is True
    assert payload["matched_rule"] == "allow_all"


def test_check_entity_not_found_exit_2(
    minimal_config_yaml: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _write_config(tmp_path, minimal_config_yaml)
    _patch_access_resolver(
        monkeypatch, FakeResolver(error=EntityNotFoundError("no entity"))
    )
    result = _run(
        ["access", "check", "--entity", "@ghost", "--config", str(cfg)]
    )
    assert result.exit_code == 2


def test_check_invalid_permission_exit_2(
    minimal_config_yaml: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _write_config(tmp_path, minimal_config_yaml)
    _patch_access_resolver(monkeypatch, FakeResolver(mapping={"@x": 1}))
    result = _run(
        ["access", "check", "--entity", "@x", "--permission", "bogus",
         "--config", str(cfg)]
    )
    assert result.exit_code == 2


# ---------------------------------------------------------------------------
# access add
# ---------------------------------------------------------------------------


def test_add_writes_valid_rule_and_roundtrips(
    minimal_config_yaml: str, tmp_path: Path
) -> None:
    cfg = _write_config(tmp_path, minimal_config_yaml)
    result = _run(
        ["access", "add", "--entity", "@client", "--permission", "read,write",
         "--config", str(cfg)]
    )
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload["status"] == "ok"
    assert payload["rule_count"] == 1

    # The written file parses and carries the new rule.
    config = load_config(cfg)
    assert config.telegram.access is not None
    rules = config.telegram.access.rules
    assert len(rules) == 1
    assert rules[0].chat == "@client"
    assert rules[0].effective_permissions == ["read", "write"]


def test_add_dry_run_does_not_write(
    minimal_config_yaml: str, tmp_path: Path
) -> None:
    cfg = _write_config(tmp_path, minimal_config_yaml)
    before = cfg.read_text()
    result = _run(
        ["access", "add", "--folder", "Clients", "--permission", "write",
         "--dry-run", "--config", str(cfg)]
    )
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload["dry_run"] is True
    assert payload["rule"]["folder"] == "Clients"
    # File untouched.
    assert cfg.read_text() == before
    assert load_config(cfg).telegram.access is None


def test_add_appends_to_existing_rules(
    minimal_config_yaml: str, tmp_path: Path
) -> None:
    access = textwrap.dedent(
        """
        access:
          rules:
            - all: true
              permission: read
        """
    ).strip() + "\n"
    cfg = _write_config(tmp_path, _with_access(minimal_config_yaml, access))
    result = _run(
        ["access", "add", "--all", "--permission", "write", "--config", str(cfg)]
    )
    assert result.exit_code == 0, result.stdout
    config = load_config(cfg)
    assert len(config.telegram.access.rules) == 2


def test_add_numeric_entity_stored_as_int(
    minimal_config_yaml: str, tmp_path: Path
) -> None:
    cfg = _write_config(tmp_path, minimal_config_yaml)
    result = _run(
        ["access", "add", "--entity", "-1001234567890", "--permission", "delete",
         "--config", str(cfg)]
    )
    assert result.exit_code == 0, result.stdout
    config = load_config(cfg)
    assert config.telegram.access.rules[0].chat == -1001234567890


def test_add_requires_exactly_one_target(
    minimal_config_yaml: str, tmp_path: Path
) -> None:
    cfg = _write_config(tmp_path, minimal_config_yaml)
    result = _run(
        ["access", "add", "--entity", "@a", "--all", "--config", str(cfg)]
    )
    assert result.exit_code == 2

    result_none = _run(["access", "add", "--config", str(cfg)])
    assert result_none.exit_code == 2


def test_add_empty_permission_rejected(
    minimal_config_yaml: str, tmp_path: Path
) -> None:
    cfg = _write_config(tmp_path, minimal_config_yaml)
    result = _run(
        ["access", "add", "--all", "--permission", ",,", "--config", str(cfg)]
    )
    assert result.exit_code == 2
