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

A third, narrower guard covers the rich-send flags of ``messages
send``: they steer what actually reaches Telegram (spacing, media
grouping, local media resolution), so an agent that does not know about
them sends a different article than the operator asked for. Both
SKILL.md and README.md must name each one.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import typer

from telegram_assistant.cli.main import app

REPO_ROOT = Path(__file__).resolve().parent.parent

SKILL_PATH = REPO_ROOT / "skills" / "telegram-assistant" / "SKILL.md"
README_PATH = REPO_ROOT / "README.md"

# Flags of ``messages send`` that change the article the server receives.
# Documented in both the skill (the agent reads it) and the README (a
# human does), so neither can silently fall behind the CLI.
RICH_SEND_FLAGS: tuple[str, ...] = (
    "--rich-markdown",
    "--no-spaced-paragraphs",
    "--rich-file",
    "--vault-dir",
    "--media-group",
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
def readme_text() -> str:
    assert README_PATH.exists(), f"README.md missing at {README_PATH}"
    return README_PATH.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def cli_commands() -> set[str]:
    return _collect_cli_commands(app)


def _messages_send_flags() -> set[str]:
    """Every long option declared on the ``messages send`` command."""
    for group in app.registered_groups:
        if group.name != "messages" or group.typer_instance is None:
            continue
        for cmd in group.typer_instance.registered_commands:
            name = cmd.name or (cmd.callback.__name__ if cmd.callback else None)
            if name != "send":
                continue
            flags: set[str] = set()
            for param in (cmd.callback.__defaults__ or ()) if cmd.callback else ():
                for decl in getattr(param, "param_decls", ()) or ():
                    # ``--spaced-paragraphs/--no-spaced-paragraphs`` is one
                    # declaration; split it into the two flags it defines.
                    flags.update(part for part in decl.split("/") if part.startswith("--"))
            return flags
    raise AssertionError("messages send command not found in the Typer app")


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


def test_rich_send_flags_exist_in_cli() -> None:
    # If the CLI ever renames one of these, the documentation guards
    # below would keep passing against stale docs — pin the source side.
    declared = _messages_send_flags()
    missing = [flag for flag in RICH_SEND_FLAGS if flag not in declared]
    assert not missing, (
        "rich-send flags no longer declared on `messages send` (rename the "
        f"entries in RICH_SEND_FLAGS and update the docs): {missing}"
    )


def test_rich_send_flags_documented_in_skill(skill_text: str) -> None:
    missing = [flag for flag in RICH_SEND_FLAGS if flag not in skill_text]
    assert not missing, (
        "rich-send flags missing from SKILL.md — the agent cannot use a flag "
        f"it has never read about: {missing}"
    )


def test_rich_send_flags_documented_in_readme(readme_text: str) -> None:
    missing = [flag for flag in RICH_SEND_FLAGS if flag not in readme_text]
    assert not missing, f"rich-send flags missing from README.md: {missing}"


def test_skill_documents_the_media_grouping_dialogue(skill_text: str) -> None:
    # The dry-run reports the runs; the skill is what turns that list into
    # a question for the human instead of a silent default.
    for marker in ("rich_markdown_groups", "AskUserQuestion", "Slideshow"):
        assert marker in skill_text, (
            f"SKILL.md no longer documents the media-grouping dialogue ({marker!r} "
            "missing) — a rich send with media runs would be grouped without asking"
        )
