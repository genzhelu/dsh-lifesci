"""Regression tests for defects found by the final independent code review.

Every test here corresponds to one reported finding; the finding is described in
the test docstring so a future maintainer can see *why* the assertion exists.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from lifesci import util
from lifesci.cli import run_command
from lifesci.errors import InvalidInputError, LifesciError
from lifesci.lib import evidence, pymol_session
from lifesci.transport import FakeTransport, TransportResponse

from tests.conftest import make_context, warning_codes


# ---------------------------------------------------------------------------
# 1. PyMOL metadata must never become executable script lines
# ---------------------------------------------------------------------------


def test_pymol_metadata_rejects_newline_injection():
    """Review #1: a session/object/selection name with newlines injected PyMOL code."""
    with pytest.raises(InvalidInputError):
        pymol_session._quote("x\npython\nprint('INJECTED')\npython end")
    with pytest.raises(InvalidInputError):
        pymol_session._safe_text("scene\npython end", label="name")
    with pytest.raises(InvalidInputError):
        pymol_session._safe_color("red, obj; python end", label="color")


def test_pymol_build_session_rejects_control_characters(tmp_path):
    spec = {
        "name": "session\npython\nprint('INJECTED')\npython end",
        "objects": [],
    }
    ctx = make_context(tmp_path)
    with pytest.raises(InvalidInputError):
        pymol_session.build_session(ctx, spec=spec, out_dir=tmp_path)


# ---------------------------------------------------------------------------
# 2/3. PDB adapter: no NameError before HTTP; command-level PDB fetch works
# ---------------------------------------------------------------------------


def test_pdb_download_structure_uses_defined_database(tmp_path):
    """Review #3: download_structure referenced an undefined ``database`` variable."""
    from lifesci.adapters import pdb as pdb_adapter

    body = b"data_1UBQ\n#\n"
    ctx = make_context(tmp_path, transport=FakeTransport([
        TransportResponse(status=200, headers={"content-type": "chemical/x-mmcif"}, body=body, url="u")
    ]))
    result = pdb_adapter.download_structure(ctx, "1UBQ", out_dir=tmp_path / "out")
    assert result["pdb_id"] == "1UBQ"
    assert Path(result["path"]).read_bytes() == body
    assert result["sha256"] == util.sha256_bytes(body)


# ---------------------------------------------------------------------------
# 5. Reciprocal search honesty: restricted reverse database is labelled
# ---------------------------------------------------------------------------


def test_reciprocal_payload_marks_restricted_reverse_database(tmp_path, monkeypatch):
    """Review #4: single-query reciprocal search was reported as complete orthology validation."""
    hmmer = pytest.importorskip("lifesci.lib.hmmer")
    from lifesci.commands import seq_search as seq_search_cmd

    query_seq = "MABCDEFGHIJKLMNOPQRSTUVWXYZABCDEFGHIJKLMNOPQRSTUVWXYZABCDEFGH"
    target_fasta = tmp_path / "targets.fasta"
    target_fasta.write_text(">T1\n" + "MABCDEFGHIJKLMNOPQRSTUVWXYZABCDEFGHIJKLMNOPQRSTUVWXYZABCDEF" + "\n")
    forward = {"hits": [{"query": "q1", "target": "T1", "evalue": 1e-30, "score": 100.0, "qcov": 1.0, "target_length": 60}]}

    calls: list[dict[str, Any]] = []

    def fake_phmmer(ctx: Any, **kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        if len(calls) == 1:
            return forward
        return {"hits": [{"query": "T1", "target": "q1", "evalue": 1e-30, "score": 100.0, "qcov": 1.0}]}

    monkeypatch.setattr(hmmer, "phmmer", fake_phmmer)
    monkeypatch.setattr(hmmer, "reciprocal_best", lambda ctx, fwd, rev: {
        "pairs": [{"query": "q1", "target": "T1", "reciprocal": True}],
        "n_reciprocal": 1, "n_forward_only": 0, "n_reverse_only": 0,
    })
    ctx = make_context(tmp_path)
    envelope = run_command(
        "seq.search",
        {
            "mode": "phmmer",
            "queries": {"q1": query_seq},
            "targets": [{"fasta": str(target_fasta), "name": "t", "organism": "o"}],
            "reciprocal": True,
            "cpus": 1,
        },
        ctx=ctx,
    )
    assert envelope["ok"] is True
    reciprocal = envelope["data"]["reciprocal"]
    assert reciprocal["available"] is True
    assert reciprocal["restricted_reverse_database"] is True
    assert "RECIPROCAL_RESTRICTED_QUERY_DB" in warning_codes(ctx)


# ---------------------------------------------------------------------------
# 6. Factors that were not searched must not be called absent
# ---------------------------------------------------------------------------


def test_unsearched_factor_not_called_absent(tmp_path, monkeypatch):
    """Review #5: a factor that was never searched must not be reported as absent."""
    hmmer = pytest.importorskip("lifesci.lib.hmmer")

    target_fasta = tmp_path / "targets.fasta"
    target_fasta.write_text(
        ">T1\nMABCDEFGHIJKLMNOPQRSTUVWXYZABCDEFGHIJKLMNOPQRSTUVWXYZABCDEF\n"
    )
    hmm = tmp_path / "PF00042.hmm"
    hmm.write_text("HMMER3/f\nNAME PF00042\nLENG 10\n//\n")

    def fake_hmmsearch(ctx: Any, **kwargs: Any) -> dict[str, Any]:
        return {
            "hits": [{"model": "PF00042", "target": "T1", "evalue": 1e-30, "score": 100.0,
                      "model_cov": 0.99, "target_length": 180, "description": "globin"}],
            "version": "3",
            "target_db_size": 2,
            "E_cutoff": 10.0,
            "bias_filter": False,
        }

    monkeypatch.setattr(hmmer, "hmmsearch", fake_hmmsearch)
    ctx = make_context(tmp_path)
    envelope = run_command(
        "seq.search",
        {
            "mode": "hmmsearch",
            "hmms": [{"id": "PF00042", "path": str(hmm)}],
            "targets": [{"fasta": str(target_fasta), "name": "t", "organism": "org_a"}],
            # factor_b is declared, but no model for it was scanned.
            "factor_models": {"factor_a": ["PF00042"], "factor_b": ["PF00043"]},
        },
        ctx=ctx,
    )
    assert envelope["ok"] is True, envelope.get("error")
    matrix = envelope["data"].get("matrix") or {}
    for row in matrix.values():
        assert "factor_b" not in row, "an unsearched factor must not be reported (absent)"
    assert set(envelope["data"].get("counts_by_factor") or {}) == {"factor_a"}
    assert "FACTOR_NOT_SEARCHED" in warning_codes(ctx)


# ---------------------------------------------------------------------------
# 7/8. Evidence curation: all candidates, caller-supplied constraints
# ---------------------------------------------------------------------------


def test_single_model_checks_all_candidates():
    """Review #7: an implausible top hit must not hide a valid candidate below it.

    The caller-supplied length window rejects the stronger hit; curation must
    keep walking the candidate list instead of stopping at the first protein.
    """
    signatures = {"factor_a": {"models": ["PF00042"], "length_min": 900, "length_max": 2500}}
    candidates = [
        {"accession": "BAD", "description": "", "protein_length": 300,
         "models": {"PF00042": {"evalue": 1e-40, "model_cov": 0.95, "protein_length": 300}}},
        {"accession": "GOOD", "description": "", "protein_length": 1200,
         "models": {"PF00042": {"evalue": 1e-30, "model_cov": 0.95, "protein_length": 1200}}},
    ]
    result = evidence.curate_factor("factor_a", candidates, factor_signatures=signatures)
    assert result["verdict"] == "present"
    assert result["best"]["accession"] == "GOOD"
    assert "GOOD" in result["evidence"]
    assert result["n_candidates"] == 2


def test_single_domain_coverage_constraint_is_enforced():
    """Review #6: a significant hit below the caller's coverage cutoff is not 'present'.

    There are no built-in coverage floors any more: the constraint is applied
    only when the caller declares ``present_coverage`` in the factor signature.
    """
    signatures = {"factor_a": {"models": ["PF00042"], "present_coverage": 0.5}}
    below = [{
        "accession": "LOW_COVERAGE", "description": "", "protein_length": 1200,
        "models": {"PF00042": {"evalue": 1e-30, "model_cov": 0.01, "protein_length": 1200}},
    }]
    result = evidence.curate_factor("factor_a", below, factor_signatures=signatures)
    assert result["verdict"] == "weak"

    accepted = [{
        "accession": "OK_COVERAGE", "description": "", "protein_length": 1200,
        "models": {"PF00042": {"evalue": 1e-30, "model_cov": 0.90, "protein_length": 1200}},
    }]
    result = evidence.curate_factor("factor_a", accepted, factor_signatures=signatures)
    assert result["verdict"] == "present"


# ---------------------------------------------------------------------------
# 9. E-value 0 sorts as the strongest hit
# ---------------------------------------------------------------------------


def test_evalue_zero_is_strongest_not_missing():
    """Review #8: ``value or 1e300`` treated E=0 as if it were a missing E-value."""
    from lifesci.commands.seq_search import _evalue_key

    assert _evalue_key({"evalue": 0}) < _evalue_key({"evalue": 0.1})
    assert _evalue_key({"evalue": 0.1}) < _evalue_key({"evalue": None})


# ---------------------------------------------------------------------------
# 10. Remote execution: offline guard and argv credential redaction
# ---------------------------------------------------------------------------


def test_remote_execution_forbidden_offline(tmp_path, monkeypatch):
    """Review #9: an offline context still reached ssh execution."""
    from lifesci.adapters import remote as remote_adapter

    monkeypatch.setenv(
        "LIFESCI_REMOTE_HOSTS",
        json.dumps({"hosts": {"h": {"host": "example.org", "user": "u", "allowed_commands": ["nvidia-smi"]}}}),
    )
    called: list[Any] = []
    monkeypatch.setattr(remote_adapter.subprocess, "run", lambda *a, **k: called.append((a, k)))

    ctx = make_context(tmp_path)
    ctx.offline = True
    with pytest.raises(LifesciError) as excinfo:
        remote_adapter.run(ctx, host="h", argv=["nvidia-smi"], allow=True)
    assert excinfo.value.code == "REMOTE_OFFLINE_FORBIDDEN"
    assert called == [], "offline mode must reject before spawning ssh"


def test_remote_argv_credentials_redacted():
    """Review #10: --api-key/token values leaked into plans, results and provenance."""
    from lifesci.adapters.remote import _redact_argv

    redacted = _redact_argv(["tool", "--api-key", "SECRET1", "--token=SECRET2", "--password", "SECRET3", "ok"])
    joined = " ".join(redacted)
    for secret in ("SECRET1", "SECRET2", "SECRET3"):
        assert secret not in joined
    assert "ok" in redacted


# ---------------------------------------------------------------------------
# 12. align.map object-form feature definitions must not crash
# ---------------------------------------------------------------------------


def test_align_map_object_form_features(tmp_path):
    """Review #12: object-form feature files referenced an unimported ``Mapping``."""
    alignment = tmp_path / "tiny_msa.fasta"
    alignment.write_text(
        ">r1\nACGTACGTACGTACGT\n>r2\nACGTACGTACGTACGT\n",
        encoding="utf-8",
    )
    queries = tmp_path / "queries.fasta"
    queries.write_text(">q1\nACGTACGTACGT\n", encoding="utf-8")
    features = tmp_path / "features.json"
    features.write_text(json.dumps({"features": {"feature_a": {"columns": [[0, 3]]}}}))
    ctx = make_context(tmp_path)
    data = run_command(
        "align.map",
        {
            "alignment": str(alignment),
            "query_fasta": str(queries),
            "features_file": str(features),
        },
        ctx=ctx,
        validate=True,
    )
    assert data["ok"] is True, data.get("error")
    assert data["data"]["n_queries"] >= 1


# ---------------------------------------------------------------------------
# 13. domains: recorded evidence from different database sizes warns
# ---------------------------------------------------------------------------


def test_domains_warns_when_recorded_database_sizes_differ(tmp_path):
    """Review #2b: recorded-only comparisons omitted the database-dependence warning."""
    ctx = make_context(tmp_path)
    data = run_command(
        "domains",
        {
            "evidence_rows": [
                {"protein": "A", "pfam_model": "PF00042", "evalue": 1e-30,
                 "model_cov": 0.99, "protein_length": 1200, "target_db_size": 1,
                 "database": "proteome-1"},
                {"protein": "B", "pfam_model": "PF00042", "evalue": 1e-5,
                 "model_cov": 0.8, "protein_length": 1100, "target_db_size": 100000,
                 "database": "proteome-100k"},
            ],
            "domains": ["PF00042"],
        },
        ctx=ctx,
        validate=True,
    )
    assert data["ok"] is True
    assert "EVALUE_NOT_COMPARABLE_ACROSS_DATABASES" in warning_codes(ctx)
