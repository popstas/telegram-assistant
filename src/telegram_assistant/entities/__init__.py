"""Shared entity resolution across HTTP, CLI, and the worker."""

from telegram_assistant.entities.service import (
    AmbiguousEntityError,
    CachingEntityResolver,
    EntityError,
    EntityNotFoundError,
    EntityRef,
    EntityRefInput,
    EntityResolver,
    ResolvedEntity,
    ResolverBackend,
)
from telegram_assistant.entities.telethon_backend import (
    TelethonEntityResolver,
    TelethonResolverBackend,
)

__all__ = [
    "AmbiguousEntityError",
    "CachingEntityResolver",
    "EntityError",
    "EntityNotFoundError",
    "EntityRef",
    "EntityRefInput",
    "EntityResolver",
    "ResolvedEntity",
    "ResolverBackend",
    "TelethonEntityResolver",
    "TelethonResolverBackend",
]
