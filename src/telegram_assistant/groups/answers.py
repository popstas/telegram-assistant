"""Localized ``answer`` strings for the HTTP group-create flow.

Mirrors the ``translate`` / ``_apply_lang`` pattern used by the
``google-drive-access`` project: every group-create response carries a
human-readable ``answer`` string, and a ``lang`` request field switches it
between Russian (default) and English.
"""

from __future__ import annotations

DEFAULT_LANG = "ru"
SUPPORTED_LANGS = ("ru", "en")

# key -> {lang -> template}
MESSAGES: dict[str, dict[str, str]] = {
    "group_created": {
        "ru": "Группа создана: {title}",
        "en": "Group created: {title}",
    },
    "group_created_client_added": {
        "ru": "Группа создана: {title}, клиент добавлен",
        "en": "Group created: {title}, client added",
    },
    "client_phone_no_telegram_id": {
        "ru": (
            "Клиента невозможно подключить по номеру телефона без telegram id. "
            "Впишите telegram id в контакт, после этого отправьте клиенту инвайт"
        ),
        "en": (
            "The client cannot be connected by phone number without a telegram id. "
            "Fill in the telegram id in the contact, then send the client an invite"
        ),
    },
}


def normalize_lang(value: str | list[str] | None) -> str:
    """Normalize a ``lang`` request value to ``"ru"`` or ``"en"``.

    Accepts a bare string or a list of strings (the first element is used),
    lowercases it, and returns a supported language. Missing or unrecognized
    input falls back to :data:`DEFAULT_LANG` (``"ru"``).
    """

    if isinstance(value, list):
        value = value[0] if value else None
    if isinstance(value, str):
        candidate = value.strip().lower()
        if candidate in SUPPORTED_LANGS:
            return candidate
    return DEFAULT_LANG


def answer(lang: str, key: str, **ctx: object) -> str:
    """Return the localized message for ``key`` in ``lang``.

    Falls back to the Russian text when ``lang`` is unknown, and to ``key``
    itself when the key is not in :data:`MESSAGES`. Templates may contain
    ``{placeholder}`` fields (e.g. ``{title}``) filled from ``ctx``; formatting
    is applied only when the chosen text actually contains a brace, so
    brace-free messages are returned verbatim regardless of ``ctx``.
    """

    templates = MESSAGES.get(key)
    if templates is None:
        return key
    text = templates.get(lang) or templates.get(DEFAULT_LANG, key)
    if ctx and "{" in text:
        return text.format(**ctx)
    return text
