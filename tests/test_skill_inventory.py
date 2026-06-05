"""Inventory guard: SKILL.md must list every CLI command and vice versa.

This is a cheap structural check that catches drift between the actual
Typer CLI in ``cli/main.py`` and the resource/action catalog inside
``skills/telegram-assistant/SKILL.md``. Whenever a new CLI
subcommand is added (or removed), the skill catalog must be updated in
the same change.

Two directions are asserted:

1. Every CLI command (top-level + grouped) appears in the SKILL.md
   catalog table, unless it is on the ``EXCLUDED_FROM_SKILL`` allowlist
   (infrastructure-only commands that the agent never invokes).
2. Every SKILL.md catalog row resolves to a real Typer command in the
   CLI — no stale entries.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import typer

from telegram_assistant.cli.main import app

SKILL_PATH = (
    Path(__file__).resolve().parent.parent
    / "skills"
    / "telegram-assistant"
    / "SKILL.md"
)

# CLI commands that intentionally do not appear in the SKILL.md catalog.
# ``version`` is infrastructure (prints the package version) and is not
# part of the Telegram-automation surface the agent drives.
EXCLUDED_FROM_SKILL: frozenset[str] = frozenset({"version"})


def _collect_cli_commands(typer_app: typer.Typer) -> set[str]:
    """Walk a Typer app and yield qualified command names like 'groups create'."""
    commands: set[str] = set()
    for cmd in typer_app.registered_commands:
        name = cmd.name or (cmd.callback.__name__ if cmd.callback else None)
        if name:
            commands.add(name)
    for group in typer_app.registered_groups:
        group_name = group.name
        sub_app = group.typer_instance
        if sub_app is None or group_name is None:
            continue
        for cmd in sub_app.registered_commands:
            name = cmd.name or (cmd.callback.__name__ if cmd.callback else None)
            if name:
                commands.add(f"{group_name} {name}")
    return commands


def _collect_skill_catalog(skill_text: str) -> set[str]:
    """Extract ``resource action`` pairs from the SKILL.md catalog table.

    The catalog lives in the ``## Resources & actions`` section as a
    Markdown table with ``| `resource` | `action` | ... |`` rows.
    """
    start = skill_text.index("## Resources & actions")
    end = skill_text.find("\n## ", start + 1)
    if end == -1:
        end = len(skill_text)
    section = skill_text[start:end]

    row_re = re.compile(r"^\|\s*`([^`]+)`\s*\|\s*`([^`]+)`\s*\|", re.MULTILINE)
    pairs: set[str] = set()
    for resource, action in row_re.findall(section):
        pairs.add(f"{resource} {action}")
    return pairs


@pytest.fixture(scope="module")
def skill_text() -> str:
    assert SKILL_PATH.exists(), f"SKILL.md missing at {SKILL_PATH}"
    return SKILL_PATH.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def cli_commands() -> set[str]:
    return _collect_cli_commands(app)


@pytest.fixture(scope="module")
def skill_catalog(skill_text: str) -> set[str]:
    return _collect_skill_catalog(skill_text)


def test_cli_has_expected_commands(cli_commands: set[str]) -> None:
    # Sanity check that the Typer walk picked up the basics; if this
    # fails, the harness below is broken and the other assertions cannot
    # be trusted.
    for required in (
        "auth",
        "health",
        "version",
        "groups create",
        "groups set-layout",
        "groups get-layout",
    ):
        assert required in cli_commands, (
            f"expected CLI command {required!r} not found via Typer walk"
        )


def test_skill_catalog_parsable(skill_catalog: set[str]) -> None:
    # If we cannot parse rows from the catalog, the rest of the test is
    # silently passing — pin a few known entries.
    for required in ("auth login", "health check", "groups create"):
        assert required in skill_catalog, (
            f"expected SKILL.md catalog row for {required!r} (parser may be broken)"
        )


def test_every_cli_command_is_in_skill_catalog(
    cli_commands: set[str], skill_catalog: set[str]
) -> None:
    # Skill rows are ``resource action`` strings. CLI commands are
    # either top-level (``auth``, ``health``) or ``group cmd``. Match by
    # last word so top-level commands like ``auth`` line up with
    # ``auth login`` rows.
    missing: list[str] = []
    for cmd in sorted(cli_commands):
        if cmd in EXCLUDED_FROM_SKILL:
            continue
        if cmd in skill_catalog:
            continue
        # Top-level command: any row whose first token equals the
        # command counts as covering it (e.g. ``auth`` ↔ ``auth login``).
        if " " not in cmd and any(
            row.split(" ", 1)[0] == cmd for row in skill_catalog
        ):
            continue
        missing.append(cmd)
    assert not missing, (
        "CLI commands missing from SKILL.md catalog (add a row to "
        "``## Resources & actions`` or add to EXCLUDED_FROM_SKILL if it is "
        f"infrastructure-only): {missing}"
    )


def test_every_skill_catalog_row_exists_in_cli(
    cli_commands: set[str], skill_catalog: set[str]
) -> None:
    stale: list[str] = []
    for row in sorted(skill_catalog):
        if row in cli_commands:
            continue
        resource, _, _action = row.partition(" ")
        # Top-level commands appear in the catalog as e.g. ``auth login``
        # but ship as a single Typer command ``auth``. Accept that shape.
        if resource in cli_commands:
            continue
        stale.append(row)
    assert not stale, (
        "SKILL.md catalog rows that no longer exist in the CLI (remove "
        f"the rows or restore the commands): {stale}"
    )
