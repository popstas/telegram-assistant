"""Model-level coverage for the group-create ``lang``/``telegram_id``/``answer`` fields."""

from __future__ import annotations

from telegram_assistant.groups.service import GroupCreateRequest, GroupCreateResult
from telegram_assistant.http_api.groups import GroupCreateBody


def _result(**overrides) -> GroupCreateResult:
    base = dict(
        telegram_chat_id=123,
        title="Acme",
        external_ref=7,
        invite_link=None,
        folder_id=None,
        folder_name=None,
        topics_enabled=False,
    )
    base.update(overrides)
    return GroupCreateResult(**base)


def test_result_to_dict_includes_answer() -> None:
    result = _result(answer="Группа создана")
    assert result.to_dict()["answer"] == "Группа создана"


def test_result_round_trip_preserves_answer() -> None:
    result = _result(answer="Группа создана, клиент добавлен")
    restored = GroupCreateResult.from_dict(result.to_dict())
    assert restored.answer == "Группа создана, клиент добавлен"


def test_result_from_dict_legacy_record_defaults_answer_blank() -> None:
    payload = _result().to_dict()
    payload.pop("answer")
    assert GroupCreateResult.from_dict(payload).answer == ""


def test_request_to_payload_includes_lang_and_telegram_id() -> None:
    request = GroupCreateRequest(title="Acme", lang="en", telegram_id="555")
    payload = request.to_payload()
    assert payload["lang"] == "en"
    assert payload["telegram_id"] == "555"


def test_request_to_payload_defaults_none() -> None:
    payload = GroupCreateRequest(title="Acme").to_payload()
    assert payload["lang"] is None
    assert payload["telegram_id"] is None


def test_body_lang_string() -> None:
    body = GroupCreateBody(title="Acme", lang="en")
    assert body.effective_lang == "en"


def test_body_lang_list_takes_first() -> None:
    body = GroupCreateBody(title="Acme", lang=["en", "ru"])
    assert body.effective_lang == "en"


def test_body_lang_missing_is_none() -> None:
    assert GroupCreateBody(title="Acme").effective_lang is None


def test_body_telegram_id_string() -> None:
    body = GroupCreateBody(title="Acme", telegram_id="555")
    assert body.effective_telegram_id == "555"


def test_body_telegram_id_int() -> None:
    body = GroupCreateBody(title="Acme", telegram_id=555)
    assert body.effective_telegram_id == "555"


def test_body_telegram_id_list_takes_first_non_empty() -> None:
    body = GroupCreateBody(title="Acme", telegram_id=["", "555"])
    assert body.effective_telegram_id == "555"


def test_body_telegram_id_blank_is_none() -> None:
    assert GroupCreateBody(title="Acme", telegram_id="").effective_telegram_id is None
    assert GroupCreateBody(title="Acme", telegram_id="   ").effective_telegram_id is None


def test_body_telegram_id_empty_list_is_none() -> None:
    assert GroupCreateBody(title="Acme", telegram_id=[]).effective_telegram_id is None
