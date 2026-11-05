"""Fixtures for the offline generic golden suite.

The suite has no shared expensive resource: every test writes its synthetic
inputs under pytest's ``tmp_path`` and every command runs with an injected,
hermetic context. Helpers live in ``golden_helpers.py``; the common session
guards (no network, fake clock, ``MPLCONFIGDIR`` pinning) come from
``tests/conftest.py``.
"""

from __future__ import annotations

import pytest

from tests.conftest import make_context


@pytest.fixture()
def golden_ctx(tmp_path):
    """A hermetic command context with cache/provenance under ``tmp_path``."""
    return make_context(tmp_path)
