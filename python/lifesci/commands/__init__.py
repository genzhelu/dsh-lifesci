"""Command discovery and registry access.

Each module in this package defines exactly one :class:`lifesci.commands.base.Command`
and registers it at import time.  Discovery is lazy and sorted so a broken module
surfaces as a single clear configuration error instead of an opaque import crash.
"""

from __future__ import annotations

import importlib
import pkgutil
from typing import Any

from .base import (  # noqa: F401  (re-exported for command modules)
    EMPTY_INPUT,
    REGISTRY,
    Command,
    command,
    register,
)

_SKIP_MODULES = {"base"}
_loaded = False


def discover(*, force: bool = False) -> dict[str, Command]:
    """Import every command module once and return the populated registry."""
    global _loaded
    if _loaded and not force:
        return dict(REGISTRY)

    failures: list[str] = []
    module_names = sorted(
        info.name
        for info in pkgutil.iter_modules(__path__)
        if info.name not in _SKIP_MODULES and not info.name.startswith("_")
    )
    for module_name in module_names:
        full_name = f"{__name__}.{module_name}"
        if full_name in list(__import__("sys").modules):
            continue
        try:
            importlib.import_module(full_name)
        except Exception as err:  # noqa: BLE001 - aggregate for a clear report
            failures.append(f"{full_name}: {type(err).__name__}: {err}")
    _loaded = True
    if failures:
        from ..errors import ConfigError

        raise ConfigError(
            "failed to import lifesci command modules",
            details={"failures": failures},
        )
    return dict(REGISTRY)


def get_command(name: str) -> Command:
    registry = discover()
    try:
        return registry[name]
    except KeyError as err:
        known = ", ".join(sorted(registry))
        raise KeyError(f"unknown lifesci command {name!r}; known commands: {known}") from err


def command_names() -> list[str]:
    return sorted(discover())


def tool_specs() -> list[dict[str, Any]]:
    """Model-facing specs for every registered command, deterministically sorted."""
    return [registry_cmd.tool_spec() for registry_cmd in sorted(discover().values(), key=lambda c: c.name)]
