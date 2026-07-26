"""Shared test fixtures."""

from __future__ import annotations

import textwrap

import pytest


@pytest.fixture(autouse=True)
def isolated_default_database(tmp_path_factory, monkeypatch) -> object:
    """Keep the *default* SQLite path inside this test's tmp dir.

    Most test configs (including :func:`minimal_config_yaml`) carry a
    ``/data/...`` session path, so ``default_database_path()`` resolves to
    ``/data/state.db`` — a path outside the test sandbox. Both outcomes are
    bad: where ``/data`` is not writable the HTTP app and the CLI silently fall
    back to *no* operation store / folder cache / pin rate gate, so tests that
    look like they cover the paced path do not; where it is writable (this
    project's own image symlinks ``/app/data -> /data``) every test shares one
    real database, and a rate-gate row written by a FLOOD_WAIT test would make
    a later pin test sleep for minutes.

    Only the two call sites that *derive* a default are redirected — anything
    passing an explicit ``database_path``/``operation_store`` is untouched, as
    is ``health.default_database_path`` itself (tests assert on its result).
    """
    db_path = tmp_path_factory.mktemp("default-db") / "state.db"
    for module in ("telegram_assistant.http_api.app", "telegram_assistant.cli.main"):
        monkeypatch.setattr(
            f"{module}.default_database_path", lambda config, _p=db_path: _p
        )
    return db_path


@pytest.fixture()
def minimal_config_yaml() -> str:
    """A minimal but valid `data/config.yml` body."""
    return textwrap.dedent(
        """
        telegram:
          api_id: 123456
          api_hash: "telegram_api_hash"
          session_path: /data/telegram-assistant.session
          main_account_label: telegram-assistant-main
          reserve_admins:
            - "@reserve_account"
          reserve_members:
            - "@planfix_bot"
          default_chat_folder:
            folder_id: 2
            folder_name: "Planfix clients"
          defaults:
            enable_topics: true
            create_invite_link: true

        plugins:
          planfix:
            enabled: true
            cleanup_messages: true
            task_reply_wait_seconds: 0

        http:
          host: "0.0.0.0"
          port: 8085
          bearer_token: "secret_token"

        queue:
          max_parallel_telegram_ops: 1
          default_retry_delay_seconds: 30
          flood_wait_safety_margin_seconds: 5

        logging:
          level: INFO
        """
    ).strip()
