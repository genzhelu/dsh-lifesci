"""Generic offline golden tests for ``domains`` in recorded-evidence mode.

Two synthetic recorded domain-evidence rows per protein (different proteome
database sizes) exercise the generic present/weak/absent arbitration without any
network or HMMER call. The tests assert the per-domain verdict, the absence
honesty warning and the cross-database-size E-value comparability warning.
"""

from __future__ import annotations

from tests.golden.golden_helpers import run_golden, warning_codes

DOMAIN = "PF00042"

#: P69905: strong PF00042 hit -> present.
#: P00698: marginal PF00042 hit -> weak.
#: P11111: no PF00042 hit at all (only an unrelated model) -> absent.
#: target_db_size differs between the two recorded sources, which must raise the
#: E-value comparability warning.
EVIDENCE_ROWS = [
    {
        "protein": "P69905",
        "pfam_model": "PF00042",
        "evalue": 1e-30,
        "score": 100.0,
        "model_cov": 0.99,
        "protein_length": 142,
        "target_db_size": 1_000,
        "database": "proteome-a",
    },
    {
        "protein": "P00698",
        "pfam_model": "PF00042",
        "evalue": 2e-2,
        "score": 20.0,
        "model_cov": 0.50,
        "protein_length": 147,
        "target_db_size": 1_000,
        "database": "proteome-a",
    },
    {
        "protein": "P11111",
        "pfam_model": "PF00099",
        "evalue": 1e-5,
        "score": 50.0,
        "model_cov": 0.80,
        "protein_length": 200,
        "target_db_size": 100_000,
        "database": "proteome-b",
    },
]


def test_recorded_domain_verdicts_and_comparability_warning(golden_ctx):
    envelope = run_golden(
        golden_ctx,
        "domains",
        {"evidence_rows": EVIDENCE_ROWS, "domains": [DOMAIN]},
        schema="domains.schema.json",
    )
    data = envelope["data"]

    proteins = {protein["accession"]: protein for protein in data["proteins"]}
    assert set(proteins) == {"P69905", "P00698", "P11111"}
    verdicts = {
        accession: values["domains"][DOMAIN]["verdict"]
        for accession, values in proteins.items()
    }
    assert verdicts == {"P69905": "present", "P00698": "weak", "P11111": "absent"}

    counts = data["domain_counts"]
    assert counts["present"] == 1
    assert counts["weak"] == 1
    assert counts["absent"] == 1
    assert counts["unknown"] == 0

    # Absence is reported explicitly as absent domain evidence, with the
    # domain-absent warning; target-db-size differences raise the comparability
    # warning.
    codes = set(warning_codes(envelope))
    assert "DOMAIN_ABSENT" in codes
    assert "EVALUE_NOT_COMPARABLE_ACROSS_DATABASES" in codes


def test_recorded_absence_is_explicit_for_each_protein(golden_ctx):
    envelope = run_golden(
        golden_ctx,
        "domains",
        {"evidence_rows": EVIDENCE_ROWS, "domains": [DOMAIN]},
        schema="domains.schema.json",
    )
    proteins = {protein["accession"]: protein for protein in envelope["data"]["proteins"]}
    absent = proteins["P11111"]["domains"][DOMAIN]
    assert absent["verdict"] == "absent"
    assert absent["domain_absent"] is True
    assert "no PF00042 annotation" in absent["evidence"]
    # The unrelated hit is not silently promoted to the requested domain.
    assert absent["model"] == DOMAIN
