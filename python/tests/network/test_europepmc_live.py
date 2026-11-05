"""Live Europe PMC integration: literature search returns real, parsed records.

Regression coverage for the nested ``{"resultList": {"result": [...]}}`` parsing
bug: every hit must carry its identity fields (title/PMID/DOI/...), not nulls.
Opt-in only (``LIFESCI_NETWORK=1``); see ``conftest.py``.
"""

from __future__ import annotations

import pytest

from lifesci.adapters import europepmc

from tests.network._live import run_live

pytestmark = pytest.mark.network

QUERY = "protein structure prediction"


def test_europepmc_search_live(net_ctx):
    result = run_live(
        "Europe PMC",
        lambda: europepmc.search(net_ctx, QUERY, page_size=3, max_pages=1),
    )
    assert result["database"] == "europe_pmc"
    assert result["hit_count"] is not None and result["hit_count"] > 0, result
    assert result["n_returned"] == len(result["hits"]) >= 1
    assert result["pages_fetched"] >= 1

    first = result["hits"][0]
    assert first["title"], f"hit title was not parsed (nested resultList.result bug): {first}"
    assert len(first["title"]) >= 10
    # At least one stable publication identifier must be parsed for every hit.
    for hit in result["hits"]:
        assert hit.get("pmid") or hit.get("pmcid") or hit.get("doi") or hit.get("id"), hit
    assert first.get("source") in {"MED", "PMC", "PPR", "AGR", "CBA", "PAT", "HIR", "WEB"}
