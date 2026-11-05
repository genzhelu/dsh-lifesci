"""Shared helpers for the opt-in live-portal tests.

Kept deliberately tiny: these helpers exist so a live test can *skip* (rather
than fail) when a portal is transiently unreachable from the machine, while a
genuine protocol/data bug still fails loudly.
"""

from __future__ import annotations

import os
from typing import Any, Callable

import pytest

#: Environment variable that opts in to live network tests.
NETWORK_ENV = "LIFESCI_NETWORK"

_TRUTHY = frozenset({"1", "true", "yes", "on", "y"})


def network_enabled(environ: dict[str, str] | None = None) -> bool:
    """Return True when live network tests were explicitly enabled."""
    source = os.environ if environ is None else environ
    return str(source.get(NETWORK_ENV, "")).strip().lower() in _TRUTHY


def run_live(portal: str, call: Callable[[], Any]) -> Any:
    """Run a live portal call, skipping when the portal is unreachable.

    A *retriable* :class:`lifesci.errors.PortalError` (connection error, timeout,
    5xx after retries) means this machine/network cannot reach the portal right
    now -> skip with a clear reason.  Non-retriable failures (404 for a required
    record, DataError, schema mismatch) are genuine problems and are re-raised.
    """
    from lifesci.errors import PortalError

    try:
        return call()
    except PortalError as err:
        if err.retriable:
            pytest.skip(f"{portal} is unreachable from this machine ({err.code}): {err.message}")
        raise
