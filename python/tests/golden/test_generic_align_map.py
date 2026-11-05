"""Generic offline golden tests for ``align.map``.

A tiny synthetic MSA and synthetic query sequences are written to ``tmp_path``:
feature spans are defined on alignment columns, and the tests assert the
reported identity/coverage, per-feature residue counts, the low-identity flags
(``LOW_IDENTITY_MAPPING`` + ``FEATURE_SPAN_UNSTABLE``) and that
``reference_row='auto'`` picks the highest-identity reference row.
"""

from __future__ import annotations

import pytest

from tests.golden.golden_helpers import run_golden, warning_codes

#: A 64-nt synthetic reference: a tandem repeat with a distinct second half.
REF_A = "ACGT" * 8 + "GCTA" * 8
#: Second reference row: REF_A with an internal substitution block.
REF_B = REF_A[:20] + "TTTT" + REF_A[24:]
#: Divergent query: identical flanks, 20-nt mismatching block, same length.
LOW_IDENTITY_QUERY = REF_A[:8] + "T" * 20 + REF_A[28:]

FEATURE_SPANS = {"feature_a": [[0, 15]], "feature_b": [[40, 60]]}


def _write_inputs(tmp_path):
    alignment = tmp_path / "tiny_msa.fasta"
    alignment.write_text(f">ref_a\n{REF_A}\n>ref_b\n{REF_B}\n", encoding="utf-8")
    queries = tmp_path / "queries.fasta"
    queries.write_text(
        f">identical_to_ref_a\n{REF_A}\n>identical_to_ref_b\n{REF_B}\n>low_identity\n{LOW_IDENTITY_QUERY}\n",
        encoding="utf-8",
    )
    return alignment, queries


def test_feature_spans_identity_and_coverage(golden_ctx, tmp_path):
    alignment, queries = _write_inputs(tmp_path)
    envelope = run_golden(
        golden_ctx,
        "align.map",
        {
            "alignment": str(alignment),
            "query_fasta": str(queries),
            "reference_row": "ref_a",
            "feature_spans": FEATURE_SPANS,
        },
        schema="align.map.schema.json",
    )
    data = envelope["data"]

    assert data["alignment"]["reference_row"] == "ref_a"
    assert data["alignment"]["n_sequences"] == 2
    assert data["alignment"]["length"] == len(REF_A)
    assert len(data["alignment"]["sha256"]) == 64
    assert set(data["features"]) == set(FEATURE_SPANS)

    mapped = {query["name"]: query for query in data["queries"]}
    assert set(mapped) == {"identical_to_ref_a", "identical_to_ref_b", "low_identity"}
    exact = mapped["identical_to_ref_a"]
    assert exact["query_length"] == len(REF_A)
    assert exact["n_aligned"] == len(REF_A)
    assert exact["identity"] == pytest.approx(1.0)
    assert exact["coverage"] == pytest.approx(1.0)
    assert exact["low_identity"] is False
    # Column span [0, 15] covers 16 reference columns; [40, 60] covers 21.
    assert exact["lengths"]["feature_a"] == 16
    assert exact["lengths"]["feature_b"] == 21
    for feature, residues in exact["mapped_residues"].items():
        assert residues == sorted(residues), f"{feature} residue map must be ordered"
        assert len(residues) == exact["lengths"][feature]


def test_reference_row_auto_picks_the_best_row(golden_ctx, tmp_path):
    alignment, queries = _write_inputs(tmp_path)
    envelope = run_golden(
        golden_ctx,
        "align.map",
        {
            "alignment": str(alignment),
            "query_fasta": str(queries),
            "reference_row": "auto",
            "feature_spans": {"feature_a": [[0, 3]]},
        },
        schema="align.map.schema.json",
    )
    data = envelope["data"]
    assert data["alignment"]["reference_row"] == "auto"
    assert data["alignment"]["reference_candidates"] == ["ref_a", "ref_b"]
    assert data["alignment"]["n_reference_candidates"] == 2

    chosen = {query["name"]: query["reference_row"] for query in data["queries"]}
    assert chosen["identical_to_ref_a"] == "ref_a"
    assert chosen["identical_to_ref_b"] == "ref_b"
    identities = {query["name"]: query for query in data["queries"]}
    for name in ("identical_to_ref_a", "identical_to_ref_b"):
        assert identities[name]["identity"] == pytest.approx(1.0)
        assert identities[name]["low_identity"] is False


def test_low_identity_flags_and_feature_span_caveat(golden_ctx, tmp_path):
    alignment, queries = _write_inputs(tmp_path)
    envelope = run_golden(
        golden_ctx,
        "align.map",
        {
            "alignment": str(alignment),
            "query_fasta": str(queries),
            "reference_row": "ref_a",
            "feature_spans": FEATURE_SPANS,
            "min_identity": 0.9,
        },
        schema="align.map.schema.json",
    )
    data = envelope["data"]
    flagged = {query["name"]: query for query in data["queries"]}["low_identity"]
    assert flagged["low_identity"] is True
    assert flagged["identity"] < 0.9
    assert "LOW_IDENTITY_MAPPING" in flagged["flags"]
    assert "LOW_IDENTITY" in flagged["flags"]

    assert [item["query"] for item in data["low_identity"]] == ["low_identity"]
    assert data["low_identity"][0]["identity"] == pytest.approx(flagged["identity"])

    codes = set(warning_codes(envelope))
    assert "LOW_IDENTITY_MAPPING" in codes
    # Feature-span counts are unreliable at low identity: the caveat must fire.
    assert "FEATURE_SPAN_UNSTABLE" in codes
