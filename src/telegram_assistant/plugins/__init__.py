"""Optional integration-plugin layer.

The core domain (groups, topics, members) has zero knowledge of any specific
integration such as Planfix. Integration-specific behavior is expressed through
the small :class:`Plugin` protocol and activated via config
(``plugins.<name>.enabled``). A :class:`PluginRegistry` aggregates the active
plugins so domain services can call one façade regardless of how many plugins
are enabled (usually zero or one).

Core code imports only :mod:`telegram_assistant.plugins.base`; concrete plugins
(e.g. :mod:`telegram_assistant.plugins.planfix`) are imported lazily by
:func:`build_registry`, so the core import graph never references them.
"""

from __future__ import annotations

from telegram_assistant.plugins.base import Plugin, PluginRegistry, build_registry

__all__ = ["Plugin", "PluginRegistry", "build_registry"]
