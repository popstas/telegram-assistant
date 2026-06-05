"""Idempotency-key derivation for each operation type.

The plan's Technical Details section defines the canonical key shape per
operation. These helpers centralize that mapping so HTTP, CLI, and the worker
queue can never disagree on what counts as "the same call".
"""

from __future__ import annotations

OperationType = str

GROUP_CREATE = "group_create"
GROUP_LAYOUT_SET = "group_layout_set"
TOPIC_CREATE = "topic_create"
TOPIC_BULK_CREATE = "topic_bulk_create"
TOPIC_CLOSE = "topic_close"
MEMBER_BULK_ADD = "member_bulk_add"
MEMBER_BULK_REMOVE = "member_bulk_remove"
MESSAGE_SEND = "message_send"
FOLDER_ADD_CHAT = "folder_add_chat"


def group_create_key(*, planfix_task_id: int | str | None, title: str | None) -> str:
    if planfix_task_id is not None and str(planfix_task_id).strip():
        return f"{GROUP_CREATE}:planfix_task_id={planfix_task_id}"
    if title is None or not title.strip():
        raise ValueError("group_create requires planfix_task_id or title")
    return f"{GROUP_CREATE}:title={title.strip()}"


def group_layout_set_key(*, telegram_chat_id: int | str, layout: str) -> str:
    if not str(telegram_chat_id).strip():
        raise ValueError("group_layout_set requires a non-empty telegram_chat_id")
    if layout not in ("list", "tabs"):
        raise ValueError(f"group_layout_set layout must be 'list' or 'tabs', got {layout!r}")
    return f"{GROUP_LAYOUT_SET}:chat={telegram_chat_id}:layout={layout}"


def topic_create_key(
    *,
    planfix_task_id: int | str | None,
    telegram_chat_id: int | str | None,
    topic_name: str | None,
) -> str:
    if planfix_task_id is not None and str(planfix_task_id).strip():
        return f"{TOPIC_CREATE}:planfix_task_id={planfix_task_id}"
    if telegram_chat_id is None or topic_name is None or not str(topic_name).strip():
        raise ValueError(
            "topic_create requires planfix_task_id, or telegram_chat_id + topic_name"
        )
    return f"{TOPIC_CREATE}:chat={telegram_chat_id}:name={topic_name.strip()}"


def topic_close_key(*, telegram_chat_id: int | str, telegram_topic_id: int | str) -> str:
    return f"{TOPIC_CLOSE}:chat={telegram_chat_id}:topic={telegram_topic_id}"


def member_add_key(*, telegram_chat_id: int | str, user: str) -> str:
    return f"{MEMBER_BULK_ADD}:chat={telegram_chat_id}:user={user}"


def member_remove_key(*, telegram_chat_id: int | str, user: str) -> str:
    return f"{MEMBER_BULK_REMOVE}:chat={telegram_chat_id}:user={user}"


def message_send_key(
    *,
    telegram_chat_id: int | str,
    telegram_topic_id: int | str | None,
    operation_id: str | None,
) -> str:
    """Key for a single message send.

    Messages have no intrinsic Planfix anchor (a single Planfix task may send
    many messages over its lifetime), so the caller-supplied ``operation_id``
    is the only stable idempotency anchor. When no ``operation_id`` is given,
    a fresh UUID is minted so each call is independent.
    """
    if operation_id is not None and str(operation_id).strip():
        oid = str(operation_id).strip()
    else:
        import uuid

        oid = uuid.uuid4().hex
    topic_part = telegram_topic_id if telegram_topic_id is not None else "-"
    return (
        f"{MESSAGE_SEND}:chat={telegram_chat_id}:"
        f"topic={topic_part}:id={oid}"
    )


def bulk_item_key(*, operation_id: str, per_item_key: str) -> str:
    """Per-item key within a bulk operation, scoped by the parent operation_id."""
    if not operation_id:
        raise ValueError("bulk_item_key requires a non-empty operation_id")
    if not per_item_key:
        raise ValueError("bulk_item_key requires a non-empty per_item_key")
    return f"{operation_id}:{per_item_key}"
