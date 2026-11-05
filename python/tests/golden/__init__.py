"""Generic, synthetic-data golden tests for the ``lifesci`` command layer.

Every test in this package drives a real command through
:func:`lifesci.cli.run_command` with small synthetic inputs written to
``tmp_path``. There are no repository fixtures, no recorded HTTP payloads and no
network access; payloads are validated against the shipped JSON schemas where a
schema exists. Shared helpers live in :mod:`tests.golden.golden_helpers`.
"""
