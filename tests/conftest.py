"""Shared test fixtures."""

from __future__ import annotations

import textwrap

import pytest


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
