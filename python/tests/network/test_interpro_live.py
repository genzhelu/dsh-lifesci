"""Live InterPro integration: a globin query (P69905) carries the PF00042 domain.

This is the remote counterpart of the offline domain-arbiter tests.
Opt-in only (``LIFESCI_NETWORK=1``); see ``conftest.py``.
"""

from __future__ import annotations

import pytest

from lifesci.adapters import interpro

from tests.network._live import run_live

pytestmark = pytest.mark.network


def test_interpro_p69905_has_pf00042_live(net_ctx):
    result = run_live(
        "InterPro",
        lambda: interpro.entry_all_for_protein(net_ctx, "P69905", page_size=50, max_pages=2),
    )
    assert result["accession"] == "P69905"
    entries = result["entries"]
    assert result["n_entries"] == len(entries) >= 1
    by_accession = {entry["entry_accession"]: entry for entry in entries}

    # Human hemoglobin subunit alpha is a globin: the PF00042 Globin family
    # must be part of its InterPro annotation.
    assert "PF00042" in by_accession, f"PF00042 missing; got {sorted(by_accession)}"
    globin = by_accession["PF00042"]
    assert globin["source_database"] == "pfam"
    entry_name = str(globin.get("entry_name") or "")
    assert "globin" in entry_name.lower(), entry_name
    locations = globin["locations"]
    assert locations, globin
    evalues = [loc["evalue"] for loc in locations if loc.get("evalue") is not None]
    assert evalues, f"PF00042 locations carry no E-value: {locations}"
    # Strong domain evidence (a globin-family hit on a globin query).
    assert min(evalues) < 1e-5, evalues
    assert 100 <= globin.get("protein_length", 0) <= 300, globin.get("protein_length")
    # ...while the query is processed/annotated (the response is not an error stub).
    assert result["truncated"] is False
