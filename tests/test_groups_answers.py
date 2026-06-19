"""Unit tests for the localized group-create answer helpers."""

import pytest

from telegram_assistant.groups.answers import answer, normalize_lang


@pytest.mark.parametrize(
    "value,expected",
    [
        ("ru", "ru"),
        ("en", "en"),
        ("RU", "ru"),
        ("En", "en"),
        ("  ru  ", "ru"),
        (["en"], "en"),
        (["RU", "en"], "ru"),
        ([], "ru"),
        (None, "ru"),
        ("", "ru"),
        ("fr", "ru"),
        ("garbage", "ru"),
        ([""], "ru"),
    ],
)
def test_normalize_lang(value, expected):
    assert normalize_lang(value) == expected


def test_answer_group_created():
    assert answer("ru", "group_created") == "Группа создана"
    assert answer("en", "group_created") == "Group created"


def test_answer_group_created_client_added():
    assert answer("ru", "group_created_client_added") == "Группа создана, клиент добавлен"
    assert answer("en", "group_created_client_added") == "Group created, client added"


def test_answer_client_phone_no_telegram_id_ru():
    expected = (
        "Клиента невозможно подключить по номеру телефона без telegram id. "
        "Впишите telegram id в контакт, после этого отправьте клиенту инвайт"
    )
    assert answer("ru", "client_phone_no_telegram_id") == expected


def test_answer_client_phone_no_telegram_id_en():
    result = answer("en", "client_phone_no_telegram_id")
    assert "telegram id" in result
    assert result != answer("ru", "client_phone_no_telegram_id")


def test_answer_unknown_lang_falls_back_to_ru():
    assert answer("fr", "group_created") == "Группа создана"


def test_answer_unknown_key_returns_key():
    assert answer("ru", "nonexistent_key") == "nonexistent_key"
