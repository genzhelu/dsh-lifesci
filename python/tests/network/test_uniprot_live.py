"""Live UniProt REST integration: FASTA fetch + entry metadata (release pinned).

Opt-in only (``LIFESCI_NETWORK=1``); see ``conftest.py``.
"""

from __future__ import annotations

import re

import pytest

from lifesci import provenance
from lifesci.adapters import uniprot

from tests.network._live import run_live

pytestmark = pytest.mark.network

_RELEASE_RE = re.compile(r"^\d{4}_\d{2}$")


def test_uniprot_fetch_fasta_live(net_ctx):
    """``P69905`` (human hemoglobin subunit alpha) FASTA fetch returns a real sequence + release."""
    result = run_live("UniProt REST", lambda: uniprot.fetch_fasta(net_ctx, "P69905"))
    assert result["accession"] == "P69905"
    sequence = result["sequence"]
    assert isinstance(sequence, str) and len(sequence) > 100
    assert result["length"] == len(sequence)
    assert sequence.isalpha() and sequence.startswith("M")
    # A small, well-curated single-chain protein from a public reference proteome.
    assert 100 <= result["length"] <= 300, result["length"]
    assert _RELEASE_RE.fullmatch(str(result["release"] or "")), result["release"]
    assert result["from_cache"] is False
    # The release was pinned in the provenance registry for reproducibility.
    releases = provenance.load_releases(net_ctx.cache_dir)
    assert releases["uniprot"]["release"] == result["release"]


def test_uniprot_fetch_entry_live(net_ctx):
    """``P00698`` (chicken lysozyme C) entry metadata: accession, organism taxid, length."""
    result = run_live("UniProt REST", lambda: uniprot.fetch_entry(net_ctx, "P00698"))
    assert result["accession"] == "P00698"
    parsed = result["parsed"]
    assert parsed["primaryAccession"] == "P00698"
    organism = parsed["organism"]
    assert organism["taxon_id"] == 9031  # Gallus gallus
    assert "Gallus" in str(organism["scientific_name"])
    sequence = parsed["sequence"]
    assert 100 <= sequence["length"] <= 300, sequence["length"]
    # The length agrees with the raw (unmodified) UniProt sequence in the payload.
    raw_sequence = result["entry"]["sequence"]["value"]
    assert sequence["length"] == len(raw_sequence)
    assert _RELEASE_RE.fullmatch(str(result["release"] or "")), result["release"]
