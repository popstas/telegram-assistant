"""Read-only participants listing — the READ op of the members domain.

Kept out of :mod:`telegram_assistant.members.service` (which is ~1150 lines of
bulk add/remove queue logic) the same way ``messages/`` splits ``search.py``
out of its own ``service.py``: this op opens no operation row, has no
idempotency key and no ``--dry-run``. It answers two questions with one shape —
"who is in this chat" (paginated, filtered) and, with ``user``, "is this one
user in this chat" (a single RPC, which is what makes a sweep over dozens of
chats affordable).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from telegram_assistant.access.service import AccessLevel, Authorizer

#: Default page/limit for a listing. Telegram serves participants 200 at a time.
DEFAULT_MEMBER_LIST_LIMIT = 200

#: Filters a caller may ask for. ``all`` uses Telegram's search filter (which is
#: what supports full enumeration); ``admins``/``bots`` use the dedicated ones.
VALID_MEMBER_FILTERS: frozenset[str] = frozenset({"all", "admins", "bots"})

#: Roles Telegram still answers for, but which are *not* current membership.
NON_MEMBER_ROLES: frozenset[str] = frozenset({"left", "banned"})


@dataclass(frozen=True)
class Participant:
    """One chat participant.

    ``role`` is one of ``creator``, ``admin``, ``member``, ``restricted``,
    ``left`` or ``banned``. ``username`` and the name fields are ``None`` when
    Telegram did not supply the user object.
    """

    user_id: int
    username: str | None
    first_name: str | None
    last_name: str | None
    is_bot: bool
    role: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "user_id": self.user_id,
            "username": self.username,
            "first_name": self.first_name,
            "last_name": self.last_name,
            "is_bot": self.is_bot,
            "role": self.role,
        }


@dataclass(frozen=True)
class MemberListResult:
    """Outcome of :func:`list_members`.

    ``participants_count`` is what the chat reports as its total (``None`` when
    unknown, e.g. in ``user`` mode). ``truncated`` says the walk stopped before
    exhausting the chat — because ``limit`` was reached or because Telegram
    stopped serving pages (its ~10k full-enumeration ceiling).
    """

    participants: tuple[Participant, ...]
    participants_count: int | None = None
    truncated: bool = False
    requested_user: str | None = None

    @property
    def is_member(self) -> bool | None:
        """``None`` unless a single ``user`` was asked about.

        A user who left or was banned is *not* a member: Telegram answers
        ``GetParticipant`` for them too, and reporting them as present would
        defeat the membership check this op exists for.
        """
        if self.requested_user is None:
            return None
        return any(p.role not in NON_MEMBER_ROLES for p in self.participants)

    def to_dict(self) -> dict[str, Any]:
        """The payload body shared by the CLI, HTTP and MCP surfaces.

        The ``user``/``is_member`` keys appear only in ``user`` mode, so a plain
        listing keeps its shape.
        """
        payload: dict[str, Any] = {
            "count": len(self.participants),
            "participants": [p.to_dict() for p in self.participants],
            "participants_count": self.participants_count,
            "truncated": self.truncated,
        }
        if self.requested_user is not None:
            payload["user"] = self.requested_user
            payload["is_member"] = self.is_member
        return payload


class MemberListBackend(Protocol):
    """Telethon-facing surface needed to read a chat's participants.

    Production wires this to
    :class:`telegram_assistant.members.telethon_backend.TelethonMemberListBackend`;
    tests inject a fake.
    """

    async def list_participants(
        self, *, chat_id: int, limit: int, query: str | None, filter: str
    ) -> MemberListResult:
        ...

    async def get_participant(
        self, *, chat_id: int, user: str
    ) -> Participant | None:
        ...


async def list_members(
    *,
    backend: MemberListBackend,
    chat_id: int,
    limit: int = DEFAULT_MEMBER_LIST_LIMIT,
    query: str | None = None,
    filter: str = "all",
    user: str | None = None,
    authorizer: Authorizer | None = None,
) -> MemberListResult:
    """List participants of ``chat_id``, or check one ``user``'s membership.

    A READ op: when an ``authorizer`` is supplied it must grant READ on the
    chat, checked before any Telegram call. ``user`` short-circuits the walk
    into a single ``GetParticipant`` and is mutually exclusive with ``query``
    (they answer different questions); ``filter`` is ignored in that mode.
    """
    if limit <= 0:
        raise ValueError("list_members requires a positive limit")
    if filter not in VALID_MEMBER_FILTERS:
        raise ValueError(
            f"unknown filter {filter!r}; expected one of "
            f"{', '.join(sorted(VALID_MEMBER_FILTERS))}"
        )
    if user is not None and query is not None:
        raise ValueError("user and query are mutually exclusive")
    if user is not None and not user.strip():
        raise ValueError("user reference must not be empty")

    if authorizer is not None:
        await authorizer.require(chat_id, AccessLevel.READ)

    if user is not None:
        participant = await backend.get_participant(chat_id=chat_id, user=user)
        return MemberListResult(
            participants=() if participant is None else (participant,),
            participants_count=None,
            truncated=False,
            requested_user=user,
        )

    return await backend.list_participants(
        chat_id=chat_id, limit=limit, query=query, filter=filter
    )


__all__ = [
    "DEFAULT_MEMBER_LIST_LIMIT",
    "NON_MEMBER_ROLES",
    "VALID_MEMBER_FILTERS",
    "MemberListBackend",
    "MemberListResult",
    "Participant",
    "list_members",
]
