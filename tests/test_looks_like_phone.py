"""Tests for :func:`telegram_assistant.members.service.looks_like_phone`."""

from __future__ import annotations

import pytest

from telegram_assistant.members import looks_like_phone


@pytest.mark.parametrize(
    "value",
    [
        "https://t.me/79222222222",
        "https://t.me/+79222222222",
        "t.me/79222222222",
        "+79222222222",
        "79222222222",
        "89222222222",
        "+7-922-222-22-22",
    ],
)
def test_looks_like_phone_true(value: str) -> None:
    assert looks_like_phone(value) is True


@pytest.mark.parametrize(
    "value",
    [
        "@user",
        "planfix_bot",
        "https://t.me/some_username",
        "123",
        "",
        "   ",
    ],
)
def test_looks_like_phone_false(value: str) -> None:
    assert looks_like_phone(value) is False


def test_looks_like_phone_none() -> None:
    assert looks_like_phone(None) is False  # type: ignore[arg-type]
