"""Structural tests for ``skills/telegram-assistant/SKILL.md``.

Task 5 of ``docs/plans/20260519-telegram-skill-and-dry-run.md`` introduces
the agent skill. This module pins the skeleton that later tasks (6, 7,
8) will build on: the YAML front-matter must be present and have the
required fields, and the body must cover the foundational rules — config
location, ``health`` liveness check, CLI-only interface, the 11-step
algorithm, ``/tmp`` rules for bulk files, and the confirmation policy.

Per-scenario content for individual resource/action pairs is asserted in
later task-specific tests, so this module intentionally checks only the
skeleton.
"""

from __future__ import annotations

from pathlib import Path

import pytest

SKILL_PATH = (
    Path(__file__).resolve().parent.parent
    / "skills"
    / "telegram-assistant"
    / "SKILL.md"
)


@pytest.fixture(scope="module")
def skill_text() -> str:
    assert SKILL_PATH.exists(), f"SKILL.md missing at {SKILL_PATH}"
    return SKILL_PATH.read_text(encoding="utf-8")


def _front_matter(text: str) -> str:
    assert text.startswith("---\n"), "SKILL.md must start with YAML front-matter"
    end = text.find("\n---\n", 4)
    assert end != -1, "SKILL.md front-matter must be terminated with `---`"
    return text[4:end]


def test_front_matter_has_name_and_description(skill_text: str) -> None:
    fm = _front_matter(skill_text)
    assert "name: telegram-assistant" in fm
    assert "description:" in fm
    desc_line = next(line for line in fm.splitlines() if line.startswith("description:"))
    description = desc_line.split(":", 1)[1].strip()
    assert len(description) >= 40, "description must be specific enough to match user intent"


def test_cli_is_primary_interface(skill_text: str) -> None:
    # The skill must say the agent drives the CLI and not Telethon directly.
    assert "telegram-assistant" in skill_text
    assert "Telethon" in skill_text
    assert "CLI" in skill_text


def test_config_location_documented(skill_text: str) -> None:
    assert "data/config.yml" in skill_text
    assert "default_chat_folder" in skill_text or "folder_name" in skill_text


def test_health_check_required_before_changes(skill_text: str) -> None:
    assert "telegram-assistant health" in skill_text
    # The skeleton must say health is a pre-flight check.
    lowered = skill_text.lower()
    assert "health" in lowered
    assert "before" in lowered or "перед" in lowered


def test_eleven_step_algorithm_present(skill_text: str) -> None:
    # All 11 numbered steps must appear in order.
    cursor = 0
    for n in range(1, 12):
        marker = f"{n}."
        idx = skill_text.find(marker, cursor)
        assert idx != -1, f"step {n}. missing from the 11-step algorithm"
        cursor = idx + len(marker)


def test_tmp_file_rules(skill_text: str) -> None:
    assert "/tmp" in skill_text
    # Must explicitly forbid writing temp files inside the repo / data/.
    lowered = skill_text.lower()
    assert "repo" in lowered or "repository" in lowered or "репозит" in lowered


def test_confirmation_required_for_destructive_and_bulk(skill_text: str) -> None:
    lowered = skill_text.lower()
    assert "confirm" in lowered or "подтвержд" in lowered
    assert "dry-run" in lowered
    # Destructive / protected accounts called out.
    assert "@planfix_bot" in skill_text
    assert "--force" in skill_text


def test_dry_run_command_set(skill_text: str) -> None:
    # The skeleton must enumerate which commands support --dry-run so the
    # agent does not try the flag on read-only commands.
    for cmd in (
        "groups create",
        "topics create",
        "topics bulk-create",
        "topics close",
        "members bulk-add",
        "members bulk-remove",
        "messages send",
        "folders add-chat",
        "operations retry",
    ):
        assert cmd in skill_text, f"--dry-run command `{cmd}` not listed in SKILL.md"


def test_read_only_commands_listed(skill_text: str) -> None:
    for cmd in ("health", "folders inspect", "operations status"):
        assert cmd in skill_text, f"read-only command `{cmd}` not mentioned"
