"""Unit tests for :mod:`lifesci.provenance` — JSONL log, redaction, releases, manifests."""

from __future__ import annotations

import re
from pathlib import Path

from lifesci import provenance, util

from tests.conftest import make_context


# ---------------------------------------------------------------------------
# request ids / redaction
# ---------------------------------------------------------------------------


def test_new_request_id_is_short_hex_and_unique():
    ids = {provenance.new_request_id() for _ in range(100)}
    assert len(ids) == 100
    assert all(re.fullmatch(r"[0-9a-f]{16}", value) for value in ids)


def test_redact_masks_credential_like_values_recursively():
    redacted = provenance.redact(
        {
            "accession": "P69905",
            "token": "super-secret",
            "API_KEY": "abc123",
            "nested": {"authorization": "Bearer xyz", "ok": 1},
            "cookie_jar": "sid=1",
            "path": Path("/tmp/x"),
        }
    )
    assert redacted["accession"] == "P69905"
    assert redacted["token"] == "***redacted***"
    assert redacted["API_KEY"] == "***redacted***"
    assert redacted["nested"] == {"authorization": "***redacted***", "ok": 1}
    assert redacted["cookie_jar"] == "***redacted***"
    assert redacted["path"] == "/tmp/x"
    assert provenance.redact(None) is None
    assert provenance.redact({}) is None


# ---------------------------------------------------------------------------
# ProvenanceLog entry shapes + JSONL persistence
# ---------------------------------------------------------------------------


def test_http_entry_is_appended_to_jsonl(tmp_path: Path):
    log_path = tmp_path / "logs" / "provenance.jsonl"
    log = provenance.ProvenanceLog(log_path)
    entry = log.http(
        method="get",
        url="https://example.org/a",
        params={"token": "secret", "q": "globin"},
        status=200,
        sha256="a" * 64,
        bytes_=42,
        cache="store",
        database="example",
        release="r1",
        retries=1,
        elapsed_ms=12.3456,
        request_id="0123456789abcdef",
        content_type="application/json",
        final_url="https://example.org/a?q=globin",
    )
    assert entry["kind"] == "http"
    assert entry["method"] == "GET"
    assert entry["request_id"] == "0123456789abcdef"
    assert entry["params"]["token"] == "***redacted***"
    assert entry["params"]["q"] == "globin"
    assert entry["retries"] == 1
    assert entry["elapsed_ms"] == 12.3  # rounded
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", entry["time"])
    assert entry["pid"] > 0
    assert entry["lifesci_version"]
    assert len(log.entries) == 1

    records = util.read_jsonl(log_path)
    assert len(records) == 1
    assert records[0]["kind"] == "http"
    assert records[0]["params"]["token"] == "***redacted***"


def test_command_entry_shape_redacts_args(tmp_path: Path):
    log = provenance.ProvenanceLog(tmp_path / "p.jsonl")
    entry = log.command(
        command="seq.fetch",
        args={"accessions": ["P69905"], "api_key": "secret"},
        ok=True,
        started_at="2026-11-05T12:00:00Z",
        finished_at="2026-11-05T12:00:02Z",
        duration_s=1.23456,
        cache_dir=str(tmp_path / "cache"),
        offline=False,
        dry_run=False,
        warnings=2,
    )
    assert entry["kind"] == "command"
    assert entry["command"] == "seq.fetch"
    assert entry["ok"] is True
    assert entry["duration_s"] == 1.235
    assert entry["args"]["api_key"] == "***redacted***"
    assert entry["args"]["accessions"] == ["P69905"]
    assert entry["n_warnings"] == 2
    assert entry["cache_dir"].endswith("cache")
    # Error runs carry the stable error code.
    failed = log.command(
        command="seq.fetch",
        args={},
        ok=False,
        started_at="t0",
        finished_at="t1",
        duration_s=0.0,
        cache_dir="/x",
        offline=True,
        error_code="OFFLINE_CACHE_MISS",
    )
    assert failed["error_code"] == "OFFLINE_CACHE_MISS"


def test_local_file_and_tool_entry_shapes(tmp_path: Path):
    log = provenance.ProvenanceLog(tmp_path / "p.jsonl")
    local = log.local_file(
        path=tmp_path / "input.fa",
        sha256="b" * 64,
        bytes_=10,
        dataset="panel",
        notes={"kind": "fasta", "path": Path("/tmp/x")},
    )
    assert local["kind"] == "local_dataset"
    assert local["path"].endswith("input.fa")
    assert local["dataset"] == "panel"
    assert local["notes"] == {"kind": "fasta", "path": "/tmp/x"}

    tool = log.tool(
        name="foldseek",
        version="9.0",
        argv=["foldseek", "easy-search"],
        exit_code=0,
        elapsed_ms=2.0,
    )
    assert tool["kind"] == "tool"
    assert tool["name"] == "foldseek"
    assert tool["argv"] == ["foldseek", "easy-search"]
    assert tool["exit_code"] == 0


def test_log_without_path_keeps_entries_in_memory_only():
    log = provenance.ProvenanceLog(None)
    log.tool(name="t", version=None)
    assert len(log.entries) == 1


def test_unwritable_log_path_never_fails_the_work(tmp_path: Path):
    # A directory where a file is expected: append must be swallowed, the entry
    # must still be available in memory for the envelope.
    log = provenance.ProvenanceLog(tmp_path)
    entry = log.tool(name="t", version="1")
    assert entry["kind"] == "tool"
    assert len(log.entries) == 1


# ---------------------------------------------------------------------------
# release pinning
# ---------------------------------------------------------------------------


def test_record_and_load_releases(tmp_path: Path):
    cache_dir = tmp_path / "cache"
    assert provenance.load_releases(cache_dir) == {}
    assert provenance.record_release(cache_dir, database="uniprot", release=None) is None
    assert provenance.load_releases(cache_dir) == {}

    first = provenance.record_release(
        cache_dir,
        database="uniprot",
        release="2026_03",
        source_url="https://rest.uniprot.org/x",
        observed_at="2026-11-01T00:00:00Z",
    )
    assert first is not None
    assert first["release"] == "2026_03"
    assert first["observations"] == 1
    assert first["first_seen"] == "2026-11-01T00:00:00Z"

    second = provenance.record_release(
        cache_dir,
        database="uniprot",
        release="2026_04",
        observed_at="2026-11-05T00:00:00Z",
    )
    assert second is not None
    assert second["release"] == "2026_04"
    assert second["observations"] == 2
    assert second["first_seen"] == "2026-11-01T00:00:00Z"  # preserved
    assert second["last_seen"] == "2026-11-05T00:00:00Z"

    releases = provenance.load_releases(cache_dir)
    assert releases["uniprot"]["release"] == "2026_04"
    assert releases["uniprot"]["source_url"] == "https://rest.uniprot.org/x"  # preserved
    assert provenance.release_registry_path(cache_dir).is_file()


# ---------------------------------------------------------------------------
# file manifests
# ---------------------------------------------------------------------------


def test_file_manifest_reports_hashes_and_missing_files(tmp_path: Path):
    present = tmp_path / "a.tsv"
    present.write_text("id\tv\n1\t2\n", encoding="utf-8")
    missing = tmp_path / "missing.tsv"
    manifest = provenance.file_manifest([present, missing], labels={str(present): "panel"})
    assert manifest[0]["path"] == str(present)
    assert manifest[0]["sha256"] == util.sha256_file(present)
    assert manifest[0]["bytes"] == present.stat().st_size
    assert manifest[0]["label"] == "panel"
    assert manifest[1] == {"path": str(missing), "missing": True}


def test_redact_url_masks_userinfo_and_secret_query_values():
    redacted = provenance.redact_url("https://user:pass@example.org/path?token=abc&q=globin")
    assert redacted is not None
    assert "user:pass" not in redacted
    assert "***@example.org" in redacted
    assert "abc" not in redacted
    assert "q=globin" in redacted
    assert provenance.redact_url(None) is None
    assert provenance.redact_url("https://example.org/plain?a=1") == "https://example.org/plain?a=1"


def test_http_entry_redacts_url_credentials_and_nested_params(tmp_path: Path):
    log = provenance.ProvenanceLog(tmp_path / "p.jsonl")
    entry = log.http(
        method="GET",
        url="https://user:pass@example.org/x?api_key=abc",
        params={"nested": {"api_key": "k", "keep": "v"}, "items": [{"secret": "s"}]},
        status=200,
        sha256=None,
        bytes_=0,
        cache="hit",
    )
    assert "user:pass" not in entry["url"]
    assert "abc" not in entry["url"]
    assert entry["params"]["nested"]["api_key"] == "***redacted***"
    assert entry["params"]["nested"]["keep"] == "v"
    assert entry["params"]["items"][0]["secret"] == "***redacted***"
