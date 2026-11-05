"""Opt-in live-portal integration tests.

Every test in this directory is marked ``network`` and is **skipped unless
``LIFESCI_NETWORK=1``** — CI and the default offline suites never contact public
portals.  The tests are fast (a few seconds), non-destructive (they only read
public metadata; no model downloads), and write exclusively to pytest
``tmp_path`` caches/logs.

Run them explicitly::

    LIFESCI_NETWORK=1 PYTHONPATH=python .venv/bin/python -m pytest python/tests/network -q

Without the environment variable the same command collects and skips every test.
"""

from __future__ import annotations

import os

import pytest

from tests.network._live import NETWORK_ENV, network_enabled


@pytest.fixture(scope="session", autouse=True)
def _network_mplconfig(tmp_path_factory: pytest.TempPathFactory):
    """Point matplotlib at a temp config dir so doctor's imports write nothing in $HOME."""
    mpl_dir = tmp_path_factory.mktemp("network-mpl-config")
    previous = os.environ.get("MPLCONFIGDIR")
    os.environ["MPLCONFIGDIR"] = str(mpl_dir)
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("MPLCONFIGDIR", None)
        else:
            os.environ["MPLCONFIGDIR"] = previous


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Skip live tests unless explicitly enabled (belt-and-braces with module markers)."""
    if network_enabled():
        return
    skip = pytest.mark.skip(
        reason=f"live network tests are opt-in; set {NETWORK_ENV}=1 to enable"
    )
    for item in items:
        if "network" in item.keywords:
            item.add_marker(skip)


@pytest.fixture()
def net_ctx(tmp_path):
    """A real (live) :class:`lifesci.context.Context` with temp cache/provenance.

    The default :class:`lifesci.transport.UrllibTransport` is used (no fake
    transport), so these tests genuinely exercise the portal clients.  All HTTP
    state stays inside ``tmp_path``; nothing is written outside it.
    """
    from lifesci.context import Context

    mpl_dir = os.environ.get("MPLCONFIGDIR") or str(tmp_path / "mpl-config")
    return Context.create(
        cache_dir=tmp_path / "cache",
        log_path=tmp_path / "provenance.jsonl",
        timeout=30.0,
        max_retries=2,
        env={"MPLCONFIGDIR": mpl_dir},
    )
