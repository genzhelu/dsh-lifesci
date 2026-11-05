"""Generic offline golden tests for ``contacts``.

The structure is built with ``gemmi`` inside ``tmp_path`` (no repository
fixtures): three chains of single-``CA`` glycines at known x-coordinates, so the
minimum atom-atom distance between any feature pair is known exactly. Tests
assert residue-selection handling, the minimum distance and threshold counts,
explicit missing-feature reporting (never a fabricated zero distance) and
determinism.
"""

from __future__ import annotations

import pytest

gemmi = pytest.importorskip("gemmi")

from tests.golden.golden_helpers import (  # noqa: E402 - after importorskip
    build_tiny_mmcif,
    run_golden,
    warning_codes,
)

#: Chains in ``build_tiny_mmcif``: A has residues 1 (x=0.0) and 2 (x=1.5),
#: B has residue 1 (x=3.0), C has residue 1 (x=20.0).
STRUCTURE = [{"id": "S1", "path": "{cif}"}]


@pytest.fixture()
def structure_payload(tmp_path):
    cif = build_tiny_mmcif(tmp_path / "tiny.cif")
    return [{"id": "S1", "path": str(cif)}]


def _payload(structure_payload, features, pairs, thresholds=(4.0, 10.0)):
    return {
        "structures": structure_payload,
        "features": features,
        "pairs": pairs,
        "thresholds": list(thresholds),
    }


def test_residue_selections_and_minimum_distance(golden_ctx, structure_payload):
    envelope = run_golden(
        golden_ctx,
        "contacts",
        _payload(
            structure_payload,
            [
                {"id": "feature_a", "structure": "S1", "chain": "A", "residues": "1-2"},
                {"id": "feature_b", "structure": "S1", "chain": "B", "residues": "1"},
            ],
            [["feature_a", "feature_b"]],
        ),
        schema="contacts.schema.json",
    )
    data = envelope["data"]

    structure = data["structures"][0]
    assert structure["id"] == "S1"
    assert structure["n_chains"] == 3
    assert structure["format"] == "mmcif"
    assert len(structure["sha256"]) == 64

    features = {feature["id"]: feature for feature in data["features"]}
    assert features["feature_a"]["status"] == "ok"
    assert features["feature_a"]["n_residues"] == 2
    assert features["feature_b"]["status"] == "ok"
    assert features["feature_b"]["n_residues"] == 1

    pair = data["pairs"][0]
    assert pair["a"] == "feature_a"
    assert pair["b"] == "feature_b"
    # Nearest pair is A:2 (x=1.5) - B:1 (x=3.0).
    assert pair["min_distance"] == pytest.approx(1.5)
    assert pair["closest"]["a"]["seqid"] == "2"
    assert pair["closest"]["b"]["seqid"] == "1"
    assert pair["closest"]["a"]["atom"] == "CA"
    assert pair["n_under"]["4"] == 2  # A:1-B:1 (3.0 A) and A:2-B:1 (1.5 A)
    assert pair["n_under"]["10"] == 2
    assert data["missing"] == []
    assert data["skipped_pairs"] == []
    assert "DISTANCE_ATOM_SELECTION" in set(warning_codes(envelope))


def test_missing_feature_is_reported_not_zero_distance(golden_ctx, structure_payload):
    envelope = run_golden(
        golden_ctx,
        "contacts",
        _payload(
            structure_payload,
            [
                {"id": "feature_a", "structure": "S1", "chain": "A", "residues": "1"},
                {"id": "feature_missing", "structure": "S1", "chain": "Z", "residues": "1"},
            ],
            [["feature_a", "feature_missing"]],
        ),
        schema="contacts.schema.json",
    )
    data = envelope["data"]

    assert data["pairs"] == [], "a pair without atoms must not produce a distance"
    missing = {item["feature"]: item for item in data["missing"]}
    assert missing["feature_missing"]["status"] == "missing_chain"
    assert data["feature_status"]["S1"]["feature_missing"] == "missing_chain"
    skipped = data["skipped_pairs"][0]
    assert {skipped["a"], skipped["b"]} == {"feature_a", "feature_missing"}
    assert "FEATURE_MISSING" in set(warning_codes(envelope))


def test_contacts_payload_is_deterministic(golden_ctx, structure_payload):
    payload = _payload(
        structure_payload,
        [
            {"id": "feature_a", "structure": "S1", "chain": "A", "residues": "1-2"},
            {"id": "feature_b", "structure": "S1", "chain": "B", "residues": "1"},
        ],
        [["feature_a", "feature_b"]],
    )
    first = run_golden(golden_ctx, "contacts", payload, schema="contacts.schema.json")
    second = run_golden(golden_ctx, "contacts", payload, schema="contacts.schema.json")
    assert first["data"] == second["data"], "repeated runs must produce identical payloads"
