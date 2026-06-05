"""Tests for the error/clarification/scope guidance in ``SKILL.md``.

Task 7 of ``docs/plans/20260519-telegram-skill-and-dry-run.md`` adds
three new sections to the skill: a list of situations where the agent
must stop and ask, a short catalogue of clarification templates, and an
explicit "out of scope" list. This module pins those sections so future
edits cannot quietly drop them or let a real identifier slip into an
example.
"""

from __future__ import annotations

import re
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


def _section(text: str, header: str) -> str:
    start = text.index(header)
    end = text.find("\n## ", start + len(header))
    if end == -1:
        end = len(text)
    return text[start:end]


def test_stop_and_ask_section_lists_all_required_situations(
    skill_text: str,
) -> None:
    body = _section(skill_text, "## When the agent must stop and ask")
    lowered = body.lower()
    # Resource/action unclear.
    assert "resource" in lowered or "ресурс" in lowered or "action" in lowered
    # Missing required parameters by name.
    for needle in (
        "username",
        "chat",
        "topic",
        "text",
        "operation-id",
    ):
        assert needle in lowered, f"stop-and-ask must mention missing {needle!r}"
    # Ambiguous matches.
    assert "ambig" in lowered or "несколько" in lowered
    # health problems.
    assert "health" in lowered
    # dry-run errors.
    assert "dry-run" in lowered
    # Protected/technical accounts.
    assert (
        "protected" in lowered
        or "technical" in lowered
        or "технич" in lowered
        or "@planfix_bot" in body
    )
    # Action outside the resource/action table.
    assert "table" in lowered or "catalogue" in lowered or "таблиц" in lowered


def test_clarification_templates_section_present(skill_text: str) -> None:
    body = _section(skill_text, "## Clarification templates")
    # The templates are short Russian prompts — at least the ones the
    # plan calls out by example must be present verbatim or close to it.
    required_phrases = (
        "не вижу username",
        "несколько чатов",
        "dry-run",
    )
    lowered = body.lower()
    for phrase in required_phrases:
        assert phrase in lowered, (
            f"clarification templates must include a prompt for {phrase!r}"
        )
    # Templates must be quoted as user-facing strings (use either «»
    # or "" so the reader can copy them).
    assert "«" in body or '"' in body


def test_what_is_out_of_scope_section_present(skill_text: str) -> None:
    body = _section(skill_text, "## What is out of scope")
    lowered = body.lower()
    # The plan enumerates concrete out-of-scope items. We assert the
    # important ones so the section cannot quietly shrink.
    for needle in (
        "bot",
        "http",
        "planfix",
        "telethon",
        "confirm",
        "guess",
        "force",
        "real",
    ):
        assert needle in lowered, f"out-of-scope section must mention {needle!r}"


# Patterns that look like real Telegram usernames / invite links and
# must not appear in scenarios. We accept the documented anonymized
# placeholders explicitly.
_ALLOWED_USERNAMES = {
    "@employee_username",
    "@manager_username",
    "@member_username",
    "@planfix_bot",
    "@username",
}


def test_no_real_usernames_in_skill(skill_text: str) -> None:
    # Pull every @<word> token and assert it is one of the documented
    # placeholders. This is the lint-style guard mentioned in the plan.
    found = set(re.findall(r"@[A-Za-z][A-Za-z0-9_]{2,}", skill_text))
    leaked = found - _ALLOWED_USERNAMES
    assert not leaked, (
        "SKILL.md must only use the documented anonymized usernames; "
        f"found unexpected tokens: {sorted(leaked)}"
    )


def test_no_real_invite_links_in_skill(skill_text: str) -> None:
    # Real Telegram invite links use t.me/... or t.me/+...; none should
    # appear in the skill.
    forbidden_fragments = ("t.me/", "https://t.me", "telegram.me/")
    for fragment in forbidden_fragments:
        assert fragment not in skill_text, (
            f"SKILL.md must not contain real invite-link fragment "
            f"{fragment!r}"
        )


def test_anonymized_client_and_folder_names(skill_text: str) -> None:
    # The plan mandates the anonymized labels "Клиент / проект" for
    # client groups and "Planfix clients" for the default folder.
    assert "Клиент / проект" in skill_text
    assert "Planfix clients" in skill_text
