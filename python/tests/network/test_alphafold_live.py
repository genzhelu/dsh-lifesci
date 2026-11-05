"""Live AlphaFold DB integration: prediction metadata only (no model download).

Opt-in only (``LIFESCI_NETWORK=1``); see ``conftest.py``.
"""

from __future__ import annotations

import pytest

from lifesci.adapters import alphafold

from tests.network._live import run_live

pytestmark = pytest.mark.network


def test_alphafold_prediction_live(net_ctx, tmp_path):
    result = run_live("AlphaFold DB", lambda: alphafold.prediction(net_ctx, "P69905"))
    assert result["available"] is True, result
    assert result["accession"] == "P69905"
    # AlphaFold DB entry ids follow the ``AF-<accession>-F1`` convention; assert
    # the accession-bearing prefix rather than pinning the whole id.
    assert str(result["entry_id"]).startswith("AF-P69905"), result["entry_id"]
    # AlphaFold DB model versions are monotonically increasing (v4 for years).
    assert isinstance(result["model_version"], int) and result["model_version"] >= 4
    assert str(result["pdb_url"]).startswith("https://alphafold.ebi.ac.uk/")
    assert str(result["cif_url"]).startswith("https://alphafold.ebi.ac.uk/")
    assert result["from_cache"] is False
    # Metadata-only: the adapter must not have downloaded a model structure.
    assert list(tmp_path.rglob("*.pdb")) == []
    assert list(tmp_path.rglob("*.cif")) == []
