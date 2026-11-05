"""Failure-handling tests: retries and explicit fallbacks across public portals.

Three scenarios are covered:

1. NCBI esearch returns an HTTP 500 HTML page → retries, then the caller falls
   back to an alternate source (Europe PMC);
2. a UniProt proteome REST stream returns 429 → the FTP reference-proteome
   fallback is used;
3. AlphaFold DB returns 404 → ESMFold is attempted, then the structure is
   reported *explicitly unavailable* (never a silent success).

The generic HTTP-layer variants below use :class:`lifesci.transport.FakeTransport`
and assert the exact retry/fallback invariants.  Adapter/command cross-checks
skip **with an explicit reason naming the unavailable module**.
"""

from __future__ import annotations

import json

import pytest

from lifesci import http
from lifesci.errors import LifesciError, PortalError
from lifesci.transport import FakeTransport

from tests.conftest import (
    import_optional,
    make_context,
    require_command_registry,
    warning_codes,
)


# ---------------------------------------------------------------------------
# 1. NCBI 500 HTML page -> retries -> alternate source
# ---------------------------------------------------------------------------

NCBI_ESEARCH = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
EUROPE_PMC = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"


def test_ncbi_500_html_retries_then_alternate_source(tmp_path):
    html_500 = FakeTransport.error_response(
        500,
        body=b"<html><head><title>500 Internal Server Error</title></head></html>",
        headers={"content-type": "text/html"},
    )
    transport = FakeTransport(
        [
            html_500,
            html_500,
            html_500,
            FakeTransport.json_response({"hitCount": 2, "resultList": {"result": []}}),
        ]
    )
    ctx = make_context(tmp_path, transport=transport, max_retries=2)

    with pytest.raises(PortalError) as excinfo:
        http.request(ctx, "GET", NCBI_ESEARCH, params={"db": "pubmed", "retmode": "json"}, error_code="NCBI")
    primary_error = excinfo.value
    assert primary_error.code == "NCBI_500"
    assert primary_error.retriable is True
    assert primary_error.details["attempts"] == 3  # 1 initial + 2 retries
    assert "Internal Server Error" in primary_error.details["body_preview"]
    assert len(transport.calls) == 3
    assert warning_codes(ctx).count("HTTP_TRANSIENT") == 2

    # The adapter/command contract is "retries then alternate source".
    value, response = http.get_json(
        ctx, EUROPE_PMC, params={"query": "globin", "format": "json"}, database="europe_pmc"
    )
    assert value == {"hitCount": 2, "resultList": {"result": []}}
    assert response.attempts == 1
    assert len(transport.calls) == 4
    assert transport.calls[-1]["url"].startswith(EUROPE_PMC)
    # The primary host cooldown never leaks to the alternate host's bucket.
    assert "eutils.ncbi.nlm.nih.gov" not in ctx._limiter.cooldown_until


def test_ncbi_200_error_body_retried_before_alternate_source(tmp_path):
    """NCBI sometimes returns HTTP 200 with ``{"error": ...}`` — also retried."""
    transport = FakeTransport(
        [
            FakeTransport.json_response({"error": "Invalid uid: 999"}),
            FakeTransport.json_response({"error": "Invalid uid: 999"}),
            FakeTransport.json_response({"esearchresult": {"count": 1}}),
        ]
    )
    ctx = make_context(tmp_path, transport=transport, max_retries=2)
    value, response = http.get_json(
        ctx,
        NCBI_ESEARCH,
        params={"db": "pubmed"},
        detect_error=http.error_detector_from_keys("error"),
        error_code="NCBI",
    )
    assert value == {"esearchresult": {"count": 1}}
    assert response.attempts == 3
    assert len(transport.calls) == 3
    assert warning_codes(ctx).count("HTTP_BODY_ERROR_RETRY") == 2


# ---------------------------------------------------------------------------
# 2. UniProt REST 429 -> FTP fallback
# ---------------------------------------------------------------------------

UNIPROT_REST = "https://rest.uniprot.org/uniprotkb/stream"
UNIPROT_FTP = (
    "https://ftp.uniprot.org/pub/databases/uniprot/current_release/knowledgebase/"
    "reference_proteomes/Eukaryota/UP000005640/UP000005640_9606.fasta.gz"
)


def test_uniprot_429_exhaustion_raises_retriable_rate_limit(tmp_path):
    """Generic layer: 429s are retried (honouring Retry-After) then reported.

    The REST->FTP fallback itself lives in ``adapters.uniprot.fetch_proteome``
    (see the adapter-level test below); the http primitive only owes us honest
    retries, a host cooldown, and a retriable error.
    """
    transport = FakeTransport(
        [
            FakeTransport.error_response(429, headers={"retry-after": "3"}, body=b"rate limited"),
            FakeTransport.error_response(429, headers={"retry-after": "3"}, body=b"rate limited"),
            FakeTransport.error_response(429, headers={"retry-after": "3"}, body=b"rate limited"),
        ]
    )
    ctx = make_context(tmp_path, transport=transport, max_retries=2)

    with pytest.raises(PortalError) as excinfo:
        http.request(
            ctx,
            "GET",
            UNIPROT_REST,
            params={"query": "proteome:UP000005640", "format": "fasta", "compressed": "true"},
            error_code="UNIPROT",
        )
    error = excinfo.value
    assert error.code == "UNIPROT_429"
    assert error.retriable is True
    assert error.details["attempts"] == 3
    assert len(transport.calls) == 3
    # Retry-After (3 s) was honoured on both retries and the host was penalised.
    assert ctx._test_clock.sleeps == [pytest.approx(3.0), pytest.approx(3.0)]
    assert "rest.uniprot.org" in ctx._limiter.cooldown_until
    assert warning_codes(ctx).count("HTTP_RATE_LIMITED") == 2
    # The recorded payload can still be replayed from the cache offline later.
    assert not any("ftp.uniprot.org" in call["url"] for call in transport.calls)


# ---------------------------------------------------------------------------
# 3. AlphaFold 404 -> ESMFold attempt -> explicit unavailable
# ---------------------------------------------------------------------------

ALPHAFOLD_API = "https://alphafold.ebi.ac.uk/api/prediction/P69905"
ESMFOLD_API = "https://api.esmatlas.com/foldSequence/v1/pdb/"


def _fetch_structure_with_fallbacks(ctx):
    """Minimal model of the struct.fetch fallback chain."""
    result = {"accession": "P69905", "available": False, "source": None, "warnings": []}
    try:
        payload, _ = http.get_json(ctx, ALPHAFOLD_API, params={"format": "json"}, error_code="ALPHAFOLD")
        return {**result, "available": True, "source": "alphafold", "payload": payload}
    except PortalError as err:
        result["warnings"].append({"code": "AFDB_UNAVAILABLE", "message": err.message, "retriable": err.retriable})
        # AlphaFold 404 is terminal for this item; the command may try ESMFold.
    try:
        http.request(
            ctx,
            "POST",
            ESMFOLD_API,
            body=b"MKWW",
            headers={"content-type": "text/plain"},
            error_code="ESMFOLD",
            idempotent=True,
            retries=1,
        )
        result.update(available=True, source="esmfold")
    except PortalError as err:
        result["warnings"].append({"code": "ESMFOLD_UNAVAILABLE", "message": err.message, "retriable": err.retriable})
        result["reason"] = "no structure prediction available from AlphaFold DB or ESMFold"
    return result


def test_alphafold_404_then_esmfold_failure_reports_explicitly_unavailable(tmp_path):
    transport = FakeTransport(
        [
            FakeTransport.error_response(404, body=b'{"detail": "Not Found"}', headers={"content-type": "application/json"}),
            FakeTransport.error_response(504, body=b"gateway timeout"),
            FakeTransport.error_response(413, body=b"sequence too long"),
        ]
    )
    ctx = make_context(tmp_path, transport=transport, max_retries=2)
    result = _fetch_structure_with_fallbacks(ctx)

    assert result["available"] is False  # explicit, never a silent success
    assert result["source"] is None
    assert "no structure prediction available" in result["reason"]
    codes = [warning["code"] for warning in result["warnings"]]
    assert codes == ["AFDB_UNAVAILABLE", "ESMFOLD_UNAVAILABLE"]
    # The 404 was not retried (terminal for that accession) ...
    assert [call["method"] for call in transport.calls] == ["GET", "POST", "POST"]
    assert transport.calls[0]["url"].startswith(ALPHAFOLD_API)
    # ... while the ESMFold transient 504 was retried once before the 413.
    assert transport.calls[1]["url"] == ESMFOLD_API
    assert transport.calls[2]["url"] == ESMFOLD_API
    assert ctx._test_clock.sleeps  # transient retry slept on the fake clock


def test_alphafold_404_then_esmfold_success(tmp_path):
    transport = FakeTransport(
        [
            FakeTransport.error_response(404, body=b'{"detail": "Not Found"}'),
            FakeTransport.text_response("ATOM      1  N   MET A   1\n"),
        ]
    )
    ctx = make_context(tmp_path, transport=transport, max_retries=2)
    result = _fetch_structure_with_fallbacks(ctx)
    assert result["available"] is True
    assert result["source"] == "esmfold"
    assert len(transport.calls) == 2


# ---------------------------------------------------------------------------
# Adapter-level cross-checks (skip when an adapter is unavailable)
# ---------------------------------------------------------------------------


def test_adapter_ncbi_500_then_success(tmp_path):
    ncbi = import_optional("lifesci.adapters.ncbi")
    transport = FakeTransport(
        [
            FakeTransport.error_response(
                500, body=b"<html>Internal Server Error</html>", headers={"content-type": "text/html"}
            ),
            FakeTransport.json_response({"esearchresult": {"count": 2, "idlist": ["1", "2"]}}),
        ]
    )
    ctx = make_context(tmp_path, transport=transport, max_retries=2)
    result = ncbi.esearch(ctx, "pubmed", "globin")
    assert isinstance(result, dict)
    assert len(transport.calls) == 2
    assert "eutils.ncbi.nlm.nih.gov" in transport.calls[0]["url"]
    assert "HTTP_TRANSIENT" in warning_codes(ctx)


def test_adapter_ncbi_exhaustion_reports_alternate_sources(tmp_path):
    ncbi = import_optional("lifesci.adapters.ncbi")
    transport = FakeTransport([FakeTransport.error_response(500, body=b"<html>boom</html>")], repeat_last=True)
    ctx = make_context(tmp_path, transport=transport, max_retries=2)
    with pytest.raises(LifesciError) as excinfo:
        ncbi.esearch(ctx, "pubmed", "globin")
    error = excinfo.value
    assert error.retriable is True
    joined = " ".join(error.fallbacks).lower()
    assert "europe pmc" in joined or "pubmed" in joined


def test_adapter_uniprot_proteome_429_uses_ftp_fallback(tmp_path):
    """UniProt proteome stream 429 -> FTP reference-proteome fallback."""
    uniprot = import_optional("lifesci.adapters.uniprot")
    fasta = b">sp|P69905|HBA_HUMAN Hemoglobin subunit alpha\nMSEQACGT\n>sp|P00698|LYSC_CHICK Lysozyme C\nMKWW\n"
    # Exhaust the REST retry budget with 429s, then serve the FTP payload.
    transport = FakeTransport(
        [
            FakeTransport.error_response(429, headers={"retry-after": "1"}, body=b"limited"),
            FakeTransport.error_response(429, headers={"retry-after": "1"}, body=b"limited"),
            FakeTransport.error_response(429, headers={"retry-after": "1"}, body=b"limited"),
            FakeTransport.text_response(fasta.decode(), headers={"content-type": "application/gzip"}),
        ]
    )
    ctx = make_context(tmp_path, transport=transport, max_retries=2)
    out_dir = tmp_path / "proteomes"
    result = uniprot.fetch_proteome(ctx, proteome_id="UP000005640", taxid="9606", out_dir=out_dir)
    assert isinstance(result, dict)
    assert result.get("n_sequences") == 2
    assert result.get("sha256")
    # A FALLBACK warning naming FTP must be recorded (no silent source switch).
    joined = " ".join(
        f"{warning.get('code')} {warning.get('message')}" for warning in ctx.warnings
    ).upper()
    assert "FTP" in joined and ("FALLBACK" in joined or "429" in joined)
    urls = [call["url"] for call in transport.calls]
    assert any("rest.uniprot.org" in url for url in urls)
    assert any("ftp.uniprot.org" in url for url in urls)


def test_adapter_alphafold_404_returns_unavailable(tmp_path):
    alphafold = import_optional("lifesci.adapters.alphafold")
    transport = FakeTransport(
        [FakeTransport.error_response(404, body=b'{"detail": "Not Found"}', headers={"content-type": "application/json"})]
    )
    ctx = make_context(tmp_path, transport=transport)
    result = alphafold.prediction(ctx, "P69905")
    assert isinstance(result, dict)
    assert result.get("available") is False
    assert len(transport.calls) == 1  # no retries for a 404


def test_adapter_alphafold_success_marks_available(tmp_path):
    alphafold = import_optional("lifesci.adapters.alphafold")
    payload = [
        {
            "entryId": "AF-P69905-F1",
            "modelEntityId": "P69905",
            "latestVersion": 4,
            "uniprotStart": 1,
            "uniprotEnd": 4,
            "pdbUrl": "https://alphafold.ebi.ac.uk/files/AF-P69905-F1-model_v4.pdb",
            "cifUrl": "https://alphafold.ebi.ac.uk/files/AF-P69905-F1-model_v4.cif",
        }
    ]
    transport = FakeTransport([FakeTransport.json_response(payload)])
    ctx = make_context(tmp_path, transport=transport)
    result = alphafold.prediction(ctx, "P69905")
    assert isinstance(result, dict)
    assert result.get("available") is True


# ---------------------------------------------------------------------------
# Command-level cross-check: struct.fetch AFDB 404 -> ESMFold -> unavailable
# ---------------------------------------------------------------------------


def test_command_struct_fetch_alphafold_404_tries_esmfold_then_unavailable(tmp_path):
    """Command-level: AFDB 404 -> ESMFold attempt -> explicit unavailable.

    Transport script follows the command's documented chain:
      1. GET AlphaFold prediction -> 404 (terminal, adapter reports available:false)
      2. GET UniProt FASTA        -> the sequence needed for the ESMFold fallback
      3. POST ESMFold             -> 504 (retried once)
      4. POST ESMFold             -> 413 (too long) => no model, explicit unavailable
    """
    from lifesci import cli

    import_optional("lifesci.adapters.alphafold")
    import_optional("lifesci.adapters.esmfold")
    import_optional("lifesci.adapters.uniprot")
    import_optional("lifesci.adapters.pdb")
    registry = require_command_registry()
    if "struct.fetch" not in registry:
        pytest.skip("command struct.fetch is not available in this checkout")

    fasta = b">sp|P69905|HBA_HUMAN Hemoglobin subunit alpha\n" + b"MKWW" * 20 + b"\n"
    transport = FakeTransport(
        [
            FakeTransport.error_response(
                404, body=b'{"detail": "Not Found"}', headers={"content-type": "application/json"}
            ),
            FakeTransport.text_response(fasta.decode(), headers={"x-uniprot-release": "2026_04"}),
            FakeTransport.error_response(504, body=b"gateway timeout"),
            FakeTransport.error_response(413, body=b"sequence too long"),
        ]
    )
    ctx = make_context(tmp_path, transport=transport, max_retries=2)
    envelope = cli.run_command("struct.fetch", {"accessions": ["P69905"]}, ctx=ctx)
    assert envelope["ok"] is True, f"struct.fetch failed: {envelope.get('error')}"
    data = envelope["data"] or {}
    # Explicit unavailability: never a silent success, no fabricated structure record.
    structure_records = data.get("structures") or []
    assert structure_records == []
    unavailable = data.get("unavailable") or []
    assert any(record.get("id") == "P69905" for record in unavailable), (
        f"AFDB 404 + ESMFold 413 must be reported in unavailable[], got {data}"
    )
    warning_codes_list = [warning.get("code") for warning in envelope["warnings"]]
    assert "AFDB_UNAVAILABLE_ESMFOLD_FALLBACK" in warning_codes_list
    assert any(code and "ESMFOLD" in code for code in warning_codes_list), warning_codes_list
    # The command walked AFDB -> UniProt sequence -> ESMFold (retried once).
    methods = [call["method"] for call in transport.calls]
    urls = [call["url"] for call in transport.calls]
    assert methods == ["GET", "GET", "POST", "POST"], urls
    assert "alphafold.ebi.ac.uk" in urls[0]
    assert "rest.uniprot.org" in urls[1]
    assert all("api.esmatlas.com" in url for url in urls[2:])
    # The attempts audit trail is present in the payload (reproducibility).
    attempts = data.get("attempted") or data.get("attempts")
    if attempts is not None:
        flattened = json.dumps(attempts).upper()
        assert "ALPHAFOLD" in flattened and "ESMFOLD" in flattened
