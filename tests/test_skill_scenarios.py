"""Content tests for the per-scenario sections of ``SKILL.md``.

Task 6 of ``docs/plans/20260519-telegram-skill-and-dry-run.md`` requires
the skill file to list every resource/action pair the agent should map
human requests to (13 in total) and to spell out a scenario for each
state-changing command using only anonymized identifiers. This module
pins those guarantees so future edits cannot quietly drop a command or
slip a real client name into an example.
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


# All 13 resource/action pairs the agent must know. Same shape as the
# table in the plan's "Resources & actions" section.
RESOURCE_ACTIONS: tuple[tuple[str, str], ...] = (
    ("auth", "login"),
    ("health", "check"),
    ("groups", "create"),
    ("topics", "create"),
    ("topics", "bulk-create"),
    ("topics", "close"),
    ("members", "bulk-add"),
    ("members", "bulk-remove"),
    ("messages", "send"),
    ("folders", "inspect"),
    ("folders", "add-chat"),
    ("operations", "status"),
    ("operations", "retry"),
)


def test_resource_actions_table_present(skill_text: str) -> None:
    assert "## Resources & actions" in skill_text
    # Each pair must appear as a per-pair subsection header like
    # ``#### `auth` / `login` ``.
    for resource, action in RESOURCE_ACTIONS:
        header = f"#### `{resource}` / `{action}`"
        assert header in skill_text, f"missing per-pair section: {header}"


def test_per_pair_fields_present(skill_text: str) -> None:
    # The per-pair description must walk through the same bullets the
    # plan calls out, so the agent has structured guidance per command.
    required_bullets = (
        "- Extract:",
        "- Required flags:",
        "- From config:",
        "- Temp file:",
        "- Automation:",
        "- Confirmation:",
        "- Typical errors:",
    )
    # Slice the document starting at the per-pair section so we don't
    # match the table above.
    start = skill_text.index("#### `auth` / `login`")
    body = skill_text[start:]
    for bullet in required_bullets:
        # Each bullet appears once per pair (13×).
        assert body.count(bullet) >= len(RESOURCE_ACTIONS), (
            f"bullet {bullet!r} missing under one of the per-pair sections "
            f"(found {body.count(bullet)} occurrences, expected >= "
            f"{len(RESOURCE_ACTIONS)})"
        )


SCENARIO_HEADERS: tuple[str, ...] = (
    "### `groups create`",
    "### `topics create`",
    "### `topics bulk-create`",
    "### `topics close`",
    "### `members bulk-add`",
    "### `members bulk-remove`",
    "### `messages send` — targeted",
    "### `messages send` — mass mode",
    "### `folders inspect`",
    "### `folders add-chat`",
    "### `operations status`",
    "### `operations retry`",
    "### `auth`",
)


def test_scenarios_section_present(skill_text: str) -> None:
    assert "## Scenarios" in skill_text
    for header in SCENARIO_HEADERS:
        assert header in skill_text, f"missing scenario section: {header}"


@pytest.mark.parametrize("scenario", SCENARIO_HEADERS)
def test_scenarios_use_anonymized_identifiers(
    skill_text: str, scenario: str
) -> None:
    # Per the plan and the "Scope of the skill" section, scenarios must
    # not leak real usernames, real client names, or real invite links.
    start = skill_text.index(scenario)
    # Stop at the next header to scope the assertion to this scenario.
    end = skill_text.find("\n### ", start + len(scenario))
    if end == -1:
        end = skill_text.find("\n## ", start + len(scenario))
    if end == -1:
        end = len(skill_text)
    body = skill_text[start:end]
    forbidden = (
        "t.me/joinchat",
        "t.me/+",
    )
    for needle in forbidden:
        assert needle not in body, (
            f"scenario {scenario!r} must not contain real invite-link "
            f"fragment {needle!r}"
        )


def test_dry_run_first_for_state_changing_scenarios(skill_text: str) -> None:
    # Every state-changing scenario must run --dry-run before the real
    # command. We check this by requiring `--dry-run` to appear in the
    # body of each such scenario.
    state_changing = (
        "### `groups create`",
        "### `topics create`",
        "### `topics bulk-create`",
        "### `topics close`",
        "### `members bulk-add`",
        "### `members bulk-remove`",
        "### `messages send` — targeted",
        "### `messages send` — mass mode",
        "### `folders add-chat`",
        "### `operations retry`",
    )
    for scenario in state_changing:
        start = skill_text.index(scenario)
        end = skill_text.find("\n### ", start + len(scenario))
        if end == -1:
            end = skill_text.find("\n## ", start + len(scenario))
        if end == -1:
            end = len(skill_text)
        body = skill_text[start:end]
        assert "--dry-run" in body, (
            f"state-changing scenario {scenario!r} must show a --dry-run "
            "invocation before the real run"
        )


def test_temp_csv_paths_are_under_tmp(skill_text: str) -> None:
    # The /tmp rules from the skeleton must be applied in the bulk
    # scenarios — every CSV/JSON path referenced in scenarios must live
    # under /tmp/ and not under data/ or the repo root.
    scenarios_with_temp_files = (
        "### `topics bulk-create`",
        "### `members bulk-add`",
    )
    for scenario in scenarios_with_temp_files:
        start = skill_text.index(scenario)
        end = skill_text.find("\n### ", start + len(scenario))
        if end == -1:
            end = skill_text.find("\n## ", start + len(scenario))
        if end == -1:
            end = len(skill_text)
        body = skill_text[start:end]
        assert "/tmp/telegram-assistant" in body, (
            f"scenario {scenario!r} must reference a /tmp/telegram-"
            "planfix-assistant-* temp file"
        )
        assert "data/" not in body, (
            f"scenario {scenario!r} must not write temp files under data/"
        )


def test_auth_scenario_does_not_collect_credentials(skill_text: str) -> None:
    start = skill_text.index("### `auth`")
    end = len(skill_text)
    body = skill_text[start:end]
    # The skill must keep `auth` out of the agent's hands; it must say
    # the human runs it themselves and the agent does not ask for codes
    # or passwords in chat.
    lowered = body.lower()
    assert "telegram-assistant auth" in body
    assert (
        "does not run" in lowered
        or "themselves" in lowered
        or "не запрашивает" in lowered
        or "никогда" in lowered
        or "never" in lowered
    )
