"""Generic offline golden tests for ``seq.search`` in evidence-replay mode.

Recorded per-model hit rows are synthetic: two organisms x two caller-supplied
factors, with one present, one weak and one absent call. No HMMER or network
call is involved in evidence mode. The tests assert the presence matrix, the
per-factor counts, the honesty warnings, whether unsearched factors are omitted
from the matrix, and the expected-matrix reproduction check.
"""

from __future__ import annotations

from tests.golden.golden_helpers import run_golden, warning_codes

FACTOR_MODELS = {"factor_a": ["PF00042"], "factor_b": ["PF00043"]}

CELLS = [
    {
        "organism": "org_a",
        "factor": "factor_a",
        "hits": [
            {
                "protein": "P69905",
                "model": "PF00042",
                "evalue": 1e-30,
                "model_cov": 0.99,
                "protein_length": 142,
            }
        ],
    },
    {
        "organism": "org_a",
        "factor": "factor_b",
        "hits": [
            {
                "protein": "P69905",
                "model": "PF00043",
                "evalue": 2e-3,
                "model_cov": 0.80,
                "protein_length": 142,
            }
        ],
    },
    {"organism": "org_b", "factor": "factor_a", "hits": []},
    {
        "organism": "org_b",
        "factor": "factor_b",
        "hits": [
            {
                "protein": "P00698",
                "model": "PF00043",
                "evalue": 1e-12,
                "model_cov": 0.95,
                "protein_length": 147,
            }
        ],
    },
]

EXPECTED_MATRIX = {
    "org_a": {"factor_a": "present", "factor_b": "weak"},
    "org_b": {"factor_a": "absent", "factor_b": "present"},
}


def test_evidence_cells_form_present_weak_absent_matrix(golden_ctx):
    envelope = run_golden(
        golden_ctx,
        "seq.search",
        {"mode": "evidence", "cells": CELLS, "factor_models": FACTOR_MODELS},
        schema="seq.search.schema.json",
    )
    data = envelope["data"]

    assert data["matrix"] == EXPECTED_MATRIX
    assert data["counts"]["n_cells"] == 4
    assert data["counts_by_factor"] == {
        "factor_a": {"present": 1, "weak": 0, "absent": 1},
        "factor_b": {"present": 1, "weak": 1, "absent": 0},
    }

    cells = {(cell["organism"], cell["factor"]): cell for cell in data["cells"]}
    assert cells[("org_a", "factor_a")]["verdict"] == "present"
    assert "PF00042" in cells[("org_a", "factor_a")]["evidence"]
    weak = cells[("org_a", "factor_b")]
    assert weak["verdict"] == "weak"
    assert any(item["code"] == "SINGLE_WEAK_LINE_EVIDENCE" for item in weak["human_review"])
    absent = cells[("org_b", "factor_a")]
    assert absent["verdict"] == "absent"
    assert any(item["code"] == "ABSENCE_NOT_PROOF" for item in absent["warnings"])

    codes = set(warning_codes(envelope))
    assert "ABSENCE_NOT_PROOF" in codes
    assert "EVALUE_NOT_COMPARABLE_ACROSS_DATABASES" in codes


def test_flat_rows_are_grouped_by_caller_factor_models(golden_ctx):
    rows = [
        {
            "organism": "org_a",
            "pfam_model": "PF00042",
            "best_hit": "sp|P69905|HBA_HUMAN",
            "evalue": 1e-30,
            "model_cov": 0.99,
            "protein_length": 142,
        },
        {
            "organism": "org_a",
            "pfam_model": "PF00043",
            "best_hit": "sp|P69905|HBA_HUMAN",
            "evalue": 2e-3,
            "model_cov": 0.80,
            "protein_length": 142,
        },
        {
            "organism": "org_b",
            "pfam_model": "PF00043",
            "best_hit": "sp|P00698|LYSC_CHICK",
            "evalue": 1e-12,
            "model_cov": 0.95,
            "protein_length": 147,
        },
    ]
    envelope = run_golden(
        golden_ctx,
        "seq.search",
        {"mode": "evidence", "rows": rows, "factor_models": FACTOR_MODELS},
        schema="seq.search.schema.json",
    )
    data = envelope["data"]
    assert data["matrix"] == EXPECTED_MATRIX
    assert "EVIDENCE_ROWS_BEST_ONLY" in set(warning_codes(envelope))


def test_unsearched_factor_is_not_reported(golden_ctx):
    cells = [
        {
            "organism": "org_a",
            "factor": "factor_a",
            "hits": [
                {
                    "protein": "P69905",
                    "model": "PF00042",
                    "evalue": 1e-30,
                    "protein_length": 142,
                }
            ],
        },
        {"organism": "org_b", "factor": "factor_a", "hits": []},
    ]
    envelope = run_golden(
        golden_ctx,
        "seq.search",
        {
            "mode": "evidence",
            "cells": cells,
            # factor_b is declared but no cells/hits were searched for it.
            "factor_models": {"factor_a": ["PF00042"], "factor_b": ["PF00043"]},
        },
        schema="seq.search.schema.json",
    )
    data = envelope["data"]
    assert data["matrix"] == {
        "org_a": {"factor_a": "present"},
        "org_b": {"factor_a": "absent"},
    }
    assert set(data["counts_by_factor"]) == {"factor_a"}
    for row in data["matrix"].values():
        assert "factor_b" not in row, "an unsearched factor must not be reported as absent"


def test_expected_matrix_reproduction_is_checked(golden_ctx):
    matching = run_golden(
        golden_ctx,
        "seq.search",
        {
            "mode": "evidence",
            "cells": CELLS,
            "factor_models": FACTOR_MODELS,
            "expected": {"matrix": EXPECTED_MATRIX},
        },
        schema="seq.search.schema.json",
    )
    reproduction = matching["data"]["reproduction"]
    assert reproduction["checked"] is True
    assert reproduction["n_mismatches"] == 0
    assert reproduction["matrix_match"] is True
    assert "REPRODUCTION_MISMATCH" not in set(warning_codes(matching))

    mismatching = run_golden(
        golden_ctx,
        "seq.search",
        {
            "mode": "evidence",
            "cells": CELLS,
            "factor_models": FACTOR_MODELS,
            "expected": {"matrix": {"org_a": {"factor_a": "absent"}}},
        },
        schema="seq.search.schema.json",
    )
    reproduction = mismatching["data"]["reproduction"]
    assert reproduction["checked"] is True
    assert reproduction["n_mismatches"] == 1
    assert reproduction["matrix_match"] is False
    assert reproduction["mismatches"][0]["expected"] == "absent"
    assert reproduction["mismatches"][0]["actual"] == "present"
    assert "REPRODUCTION_MISMATCH" in set(warning_codes(mismatching))
