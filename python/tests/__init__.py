"""Offline test suite for the ``lifesci`` science layer.

* ``python/tests/unit/`` — fast, deterministic, no-network unit tests.
* ``python/tests/golden/`` — end-to-end command tests on synthetic inline data.
* ``python/tests/network/`` — opt-in live-portal tests (``@pytest.mark.network``).

Run the offline suite with::

    cd <repo> && PYTHONPATH=python .venv/bin/python -m pytest python/tests/unit python/tests/golden -q -m "not network"
"""
