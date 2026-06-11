"""Tests for :func:`telegram_assistant.members.service.normalize_phone`.

A user's phone arrives "dirty" — with dashes, a leading ``8``, or wrapped in a
``t.me`` link. The normaliser collapses every shape of the same number to one
canonical ``+<digits>`` form so it can be imported as a contact.
"""

from __future__ import annotations

import pytest

from telegram_assistant.members.service import normalize_phone


@pytest.mark.parametrize(
    "raw",
    [
        "+79222222222",
        "79222222222",
        "89222222222",
        "https://t.me/+79222222222",
        "https://t.me/79222222222",
        "+7-922-222-22-22",
        "  +7 (922) 222-22-22 ",
        "t.me/89222222222",
    ],
)
def test_dirty_variants_collapse_to_canonical(raw: str) -> None:
    assert normalize_phone(raw) == "+79222222222"


def test_leading_eight_rewritten_only_for_eleven_digits() -> None:
    # 11-digit trunk form → +7...
    assert normalize_phone("89991234567") == "+79991234567"
    # A 10-digit number starting with 8 is left as-is (no trunk rewrite).
    assert normalize_phone("8912345678") == "+8912345678"


def test_already_canonical_passes_through() -> None:
    assert normalize_phone("+15551234567") == "+15551234567"


@pytest.mark.parametrize("raw", ["12345", "", "   ", "abc", "+7-922"])
def test_malformed_phone_raises(raw: str) -> None:
    with pytest.raises(ValueError):
        normalize_phone(raw)


def test_too_many_digits_raises() -> None:
    with pytest.raises(ValueError):
        normalize_phone("1234567890123456")
