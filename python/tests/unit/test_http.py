"""Unit tests for :mod:`lifesci.http` — retries, offline replay, caching, dry-run, gzip.

Every test injects :class:`lifesci.transport.FakeTransport` and a fake clock, so
the suite exercises the real retry/backoff/cache code paths with no network and
no real sleeping.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import pytest

from lifesci import http, util
from lifesci.cache import Cache, cache_key
from lifesci.errors import CacheCorruptError, DataError, OfflineCacheMiss, PortalError
from lifesci.transport import FakeTransport, TransportError, TransportResponse, multipart_encode

from tests.conftest import FakeClock, assert_warning, make_context, warning_codes


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _engine_payload() -> dict:
    return {"esearchresult": {"count": 2, "idlist": ["1", "2"]}}


def _age_cached_entry(cache: Cache, key: str, *, age_seconds: float, ttl_seconds: float) -> None:
    meta = util.read_json(cache.meta_path_for(key))
    stored = datetime.now(timezone.utc) - timedelta(seconds=age_seconds)
    meta["stored_at"] = stored.isoformat(timespec="seconds").replace("+00:00", "Z")
    meta["ttl_seconds"] = ttl_seconds
    util.atomic_write_json(cache.meta_path_for(key), meta)


# ---------------------------------------------------------------------------
# URL construction
# ---------------------------------------------------------------------------


def test_build_url_sorts_and_encodes_params():
    assert http.build_url("https://x.org/a", {"b": "2", "a": "1"}) == "https://x.org/a?a=1&b=2"
    assert http.build_url("https://x.org/a?z=0", {"a": 1}) == "https://x.org/a?z=0&a=1"


def test_build_url_skips_none_and_handles_empty():
    assert http.build_url("https://x.org/a", None) == "https://x.org/a"
    assert http.build_url("https://x.org/a", {}) == "https://x.org/a"
    assert http.build_url("https://x.org/a", {"a": None, "b": "2"}) == "https://x.org/a?b=2"


# ---------------------------------------------------------------------------
# retries
# ---------------------------------------------------------------------------


def test_retry_500_html_then_success(tmp_path):
    transport = FakeTransport(
        [
            FakeTransport.error_response(
                500, body=b"<html><body>Internal Server Error</body></html>", headers={"content-type": "text/html"}
            ),
            FakeTransport.json_response(_engine_payload()),
        ]
    )
    ctx = make_context(tmp_path, transport=transport, max_retries=2)
    value, response = http.get_json(
        ctx,
        "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi",
        params={"db": "pubmed", "term": "globin"},
        error_code="NCBI",
    )
    assert value == _engine_payload()
    assert response.attempts == 2
    assert response.from_cache is False
    assert response.status == 200
    assert len(transport.calls) == 2
    assert transport.calls[0]["url"].endswith("?db=pubmed&term=globin")
    assert_warning(ctx, "HTTP_TRANSIENT")
    assert ctx._test_clock.sleeps, "retry must sleep (on the fake clock)"
    assert all(seconds > 0 for seconds in ctx._test_clock.sleeps)


def test_retry_500_exhausted_raises_retriable_portal_error(tmp_path):
    transport = FakeTransport(
        [
            FakeTransport.error_response(500, body=b"<html>Internal Server Error</html>"),
            FakeTransport.error_response(500, body=b"<html>Internal Server Error</html>"),
            FakeTransport.error_response(500, body=b"<html>Internal Server Error</html>"),
        ]
    )
    ctx = make_context(tmp_path, transport=transport, max_retries=2)
    with pytest.raises(PortalError) as excinfo:
        http.request(ctx, "GET", "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi")
    error = excinfo.value
    assert error.code == "HTTP_ERROR_500"
    assert error.retriable is True
    assert error.details["attempts"] == 3
    assert error.details["status"] == 500
    assert len(error.details["diagnostics"]) == 2  # one per retried attempt
    assert "Internal Server Error" in error.details["body_preview"]
    assert len(transport.calls) == 3
    assert warning_codes(ctx) == ["HTTP_TRANSIENT", "HTTP_TRANSIENT"]


def test_429_honours_retry_after_and_penalises_host(tmp_path):
    transport = FakeTransport(
        [
            FakeTransport.error_response(429, headers={"retry-after": "7"}, body=b"slow down"),
            FakeTransport.json_response({"ok": True}),
        ]
    )
    ctx = make_context(tmp_path, transport=transport, max_retries=2)
    value, response = http.get_json(ctx, "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi")
    assert value == {"ok": True}
    assert response.attempts == 2
    # Retry-After (7 s) beats the jittered exponential backoff (~1 s).
    assert ctx._test_clock.sleeps == [pytest.approx(7.0)]
    assert_warning(ctx, "HTTP_RATE_LIMITED")
    # The host-wide cooldown was applied by the limiter.
    assert "eutils.ncbi.nlm.nih.gov" in ctx._limiter.cooldown_until


@pytest.mark.parametrize(
    ("error_code", "expected_code"),
    [(None, "HTTP_ERROR_404"), ("UNIPROT", "UNIPROT_404")],
)
def test_404_is_not_retried(tmp_path, error_code, expected_code):
    transport = FakeTransport(
        [FakeTransport.error_response(404, body=b'{"error": "not found"}', headers={"content-type": "application/json"})]
    )
    ctx = make_context(tmp_path, transport=transport, max_retries=4)
    with pytest.raises(PortalError) as excinfo:
        http.request(
            ctx,
            "GET",
            "https://rest.uniprot.org/uniprotkb/XXXX.json",
            error_code=error_code,
        )
    error = excinfo.value
    assert error.code == expected_code
    assert error.retriable is False
    assert error.details["attempts"] == 1
    assert error.details["status"] == 404
    assert len(transport.calls) == 1
    assert ctx.warnings == []


def test_transport_error_exhaustion_raises_retriable_portal_error(tmp_path):
    transport = FakeTransport(
        [
            TransportError("connection reset"),
            TransportError("timeout"),
            TransportError("dns failure"),
        ]
    )
    ctx = make_context(tmp_path, transport=transport, max_retries=2)
    with pytest.raises(PortalError) as excinfo:
        http.request(ctx, "GET", "https://example.org/x")
    error = excinfo.value
    assert error.code == "HTTP_ERROR"
    assert error.retriable is True
    assert error.details["attempts"] == 3
    assert error.details["status"] is None
    assert len(error.details["diagnostics"]) == 3
    assert len(transport.calls) == 3
    assert warning_codes(ctx) == ["HTTP_RETRY", "HTTP_RETRY"]


def test_non_retriable_transport_error_stops_immediately(tmp_path):
    transport = FakeTransport([TransportError("TLS configuration error", retriable=False)])
    ctx = make_context(tmp_path, transport=transport, max_retries=4)
    with pytest.raises(PortalError) as excinfo:
        http.request(ctx, "GET", "https://example.org/x")
    assert excinfo.value.retriable is False
    assert excinfo.value.details["attempts"] == 1
    assert len(transport.calls) == 1


# ---------------------------------------------------------------------------
# body-level portal errors (NCBI-style 200 {"error": ...})
# ---------------------------------------------------------------------------


def test_body_level_error_is_retried(tmp_path):
    transport = FakeTransport(
        [
            FakeTransport.json_response({"error": "Invalid uid"}),
            FakeTransport.json_response(_engine_payload()),
        ]
    )
    ctx = make_context(tmp_path, transport=transport, max_retries=2)
    value, response = http.get_json(
        ctx,
        "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi",
        detect_error=http.error_detector_from_keys("error"),
        error_code="NCBI",
    )
    assert value == _engine_payload()
    assert response.attempts == 2
    assert len(transport.calls) == 2
    assert_warning(ctx, "HTTP_BODY_ERROR_RETRY")
    assert ctx._test_clock.sleeps


def test_body_level_error_non_retriable_detector_stops(tmp_path):
    transport = FakeTransport([FakeTransport.json_response({"x": 1})])
    ctx = make_context(tmp_path, transport=transport, max_retries=4)
    with pytest.raises(PortalError) as excinfo:
        http.request(
            ctx,
            "GET",
            "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi",
            detect_error=lambda response: ("permanent portal error", False),
        )
    assert excinfo.value.details["attempts"] == 1
    assert len(transport.calls) == 1


def test_error_detector_from_keys_shapes():
    detect = http.error_detector_from_keys("error")
    assert detect(TransportResponse(status=200, headers={"content-type": "application/json"}, body=b'{"error":"bad"}', url="")) == "error: bad"
    assert detect(TransportResponse(status=200, headers={"content-type": "application/json"}, body=b'{"error":{"message":"nested"}}', url="")) == "error: nested"
    assert detect(TransportResponse(status=200, headers={"content-type": "application/json"}, body=b'{"ok":1}', url="")) is None
    # A real HTML error page is not treated as a JSON-in-200 portal error.
    assert detect(TransportResponse(status=200, headers={"content-type": "text/html"}, body=b"<html>bad</html>", url="")) is None
    assert detect(TransportResponse(status=404, headers={"content-type": "application/json"}, body=b'{"error":"bad"}', url="")) is None


# ---------------------------------------------------------------------------
# offline mode
# ---------------------------------------------------------------------------


def test_offline_miss_raises_precise_error_without_touching_transport(tmp_path):
    transport = FakeTransport([])
    ctx = make_context(tmp_path, transport=transport, offline=True)
    with pytest.raises(OfflineCacheMiss) as excinfo:
        http.request(ctx, "GET", "https://example.org/a", params={"x": "1"})
    error = excinfo.value
    assert error.code == "OFFLINE_CACHE_MISS"
    assert error.exit_code == 4
    assert error.details["cache_key"] and len(error.details["cache_key"]) == 64
    hint = error.details["hint"]
    assert hint and "cache" in hint.lower(), hint
    assert transport.calls == []


def test_offline_hit_replays_cached_payload(tmp_path):
    url = "https://www.ebi.ac.uk/interpro/api/entry/pfam/PF00042/"
    transport = FakeTransport([FakeTransport.json_response({"results": ["PF00042"]})])
    online = make_context(tmp_path, transport=transport)
    value, cached_response = http.get_json(online, url)
    assert value == {"results": ["PF00042"]}

    replay_transport = FakeTransport([])
    offline = make_context(
        tmp_path,
        cache_dir=online.cache_dir,
        log_path=tmp_path / "replay.jsonl",
        offline=True,
        transport=replay_transport,
    )
    replayed, response = http.get_json(offline, url)
    assert replayed == {"results": ["PF00042"]}
    assert response.from_cache is True
    assert response.attempts == 0
    assert response.body == util.canonical_json({"results": ["PF00042"]}).encode() or json.loads(response.body)
    assert replay_transport.calls == []
    assert offline.warnings == []


def test_offline_corrupt_cache_raises_instead_of_network(tmp_path):
    url = "https://example.org/corrupt"
    online = make_context(tmp_path, transport=FakeTransport([FakeTransport.json_response({"a": 1})]))
    _, response = http.get_json(online, url)
    assert response.cache_key is not None
    Cache(online.cache_dir).body_path_for(response.cache_key).write_bytes(b"tampered")
    offline = make_context(tmp_path, cache_dir=online.cache_dir, offline=True, transport=FakeTransport([]))
    with pytest.raises(CacheCorruptError):
        http.get_json(offline, url)


# ---------------------------------------------------------------------------
# dry run / caching behaviour
# ---------------------------------------------------------------------------


def test_dry_run_makes_no_transport_calls_and_returns_plan(tmp_path):
    transport = FakeTransport([])  # would raise AssertionError if called
    ctx = make_context(tmp_path, transport=transport, dry_run=True)
    value, response = http.get_json(ctx, "https://rest.uniprot.org/uniprotkb/P69905.fasta", params={"format": "json"})
    assert value is None
    assert response.dry_run is True
    assert response.status is None
    assert response.attempts == 0
    assert response.cache_key is not None
    assert response.final_url.endswith("?format=json")
    assert transport.calls == []
    assert response.provenance_entry["cache"] == "dry_run"
    assert Cache(ctx.cache_dir).stats()["n_entries"] == 0
    # The other wrappers behave consistently in dry-run mode.
    assert http.get_text(ctx, "https://example.org/t")[0] == ""
    assert http.get_bytes(ctx, "https://example.org/b")[0] == b""


def test_second_identical_request_is_served_from_cache(tmp_path):
    url = "https://example.org/cached"
    transport = FakeTransport([FakeTransport.json_response({"value": 1})])
    ctx = make_context(tmp_path, transport=transport)
    first, first_response = http.get_json(ctx, url)
    second, second_response = http.get_json(ctx, url)
    assert first == second == {"value": 1}
    assert first_response.from_cache is False
    assert second_response.from_cache is True
    assert second_response.attempts == 0
    assert len(transport.calls) == 1
    hit = [entry for entry in ctx.provenance if entry.get("kind") == "http"]
    assert hit[0]["cache"] == "store" and hit[-1]["cache"] == "hit"


def test_cache_false_bypasses_cache_reads_and_writes(tmp_path):
    url = "https://example.org/poll"
    transport = FakeTransport(
        [FakeTransport.json_response({"n": 1}), FakeTransport.json_response({"n": 2})]
    )
    ctx = make_context(tmp_path, transport=transport)
    first, _ = http.get_json(ctx, url, cache=False)
    second, response = http.get_json(ctx, url, cache=False)
    assert (first, second) == ({"n": 1}, {"n": 2})
    assert response.from_cache is False
    assert len(transport.calls) == 2
    assert Cache(ctx.cache_dir).stats()["n_entries"] == 0


def test_cache_ttl_override_is_recorded(tmp_path):
    url = "https://example.org/ttl"
    ctx = make_context(tmp_path, transport=FakeTransport([FakeTransport.json_response({"v": 1})]))
    _, response = http.get_json(ctx, url, cache_ttl=123.0)
    assert response.cache_key is not None
    entry = Cache(ctx.cache_dir).lookup(response.cache_key, verify=True)
    assert entry is not None and entry.ttl_seconds == 123.0


def test_corrupt_cache_is_discarded_and_refetched_online(tmp_path):
    url = "https://example.org/repair"
    transport = FakeTransport([FakeTransport.json_response({"v": 1})])
    first_ctx = make_context(tmp_path, transport=transport)
    _, response = http.get_json(first_ctx, url)
    assert response.cache_key is not None
    Cache(first_ctx.cache_dir).body_path_for(response.cache_key).write_bytes(b"tampered")

    repair_transport = FakeTransport([FakeTransport.json_response({"v": 2})])
    ctx = make_context(tmp_path, cache_dir=first_ctx.cache_dir, transport=repair_transport)
    value, repaired = http.get_json(ctx, url)
    assert value == {"v": 2}
    assert repaired.from_cache is False
    assert len(repair_transport.calls) == 1
    assert_warning(ctx, "CACHE_CORRUPT")


def test_expired_cache_is_refetched_online(tmp_path):
    url = "https://example.org/expiry"
    first_ctx = make_context(tmp_path, transport=FakeTransport([FakeTransport.json_response({"v": "old"})]))
    _, response = http.get_json(first_ctx, url)
    assert response.cache_key is not None
    cache = Cache(first_ctx.cache_dir)
    _age_cached_entry(cache, response.cache_key, age_seconds=7200.0, ttl_seconds=3600.0)

    ctx = make_context(
        tmp_path,
        cache_dir=first_ctx.cache_dir,
        transport=FakeTransport([FakeTransport.json_response({"v": "new"})]),
    )
    value, refreshed = http.get_json(ctx, url)
    assert value == {"v": "new"}
    assert refreshed.from_cache is False
    assert_warning(ctx, "CACHE_ENTRY_EXPIRED")


def test_expired_cache_replayed_offline_with_historical_warning(tmp_path):
    url = "https://example.org/historical"
    first_ctx = make_context(tmp_path, transport=FakeTransport([FakeTransport.json_response({"v": "old"})]))
    _, response = http.get_json(first_ctx, url)
    assert response.cache_key is not None
    _age_cached_entry(Cache(first_ctx.cache_dir), response.cache_key, age_seconds=7200.0, ttl_seconds=3600.0)

    ctx = make_context(tmp_path, cache_dir=first_ctx.cache_dir, offline=True, transport=FakeTransport([]))
    value, replayed = http.get_json(ctx, url)
    assert value == {"v": "old"}
    assert replayed.from_cache is True
    assert_warning(ctx, "CACHE_EXPIRED_OFFLINE_REPLAY")


# ---------------------------------------------------------------------------
# methods / bodies / idempotency
# ---------------------------------------------------------------------------


def test_post_is_not_retried_by_default(tmp_path):
    transport = FakeTransport(
        [FakeTransport.error_response(500, body=b"boom"), FakeTransport.json_response({"ok": True})]
    )
    ctx = make_context(tmp_path, transport=transport, max_retries=4)
    with pytest.raises(PortalError) as excinfo:
        http.request(ctx, "POST", "https://example.org/write", json_body={"x": 1})
    assert excinfo.value.details["attempts"] == 1
    assert len(transport.calls) == 1


def test_post_is_retried_when_marked_idempotent(tmp_path):
    transport = FakeTransport(
        [FakeTransport.error_response(500, body=b"boom"), FakeTransport.json_response({"ok": True})]
    )
    ctx = make_context(tmp_path, transport=transport, max_retries=4)
    response = http.request(
        ctx, "POST", "https://example.org/write", json_body={"x": 1}, idempotent=True
    )
    assert response.status == 200
    assert response.json() == {"ok": True}
    assert response.attempts == 2
    assert len(transport.calls) == 2


def test_json_body_is_canonical_and_typed(tmp_path):
    transport = FakeTransport([FakeTransport.json_response({"ok": 1})])
    ctx = make_context(tmp_path, transport=transport)
    http.request(ctx, "POST", "https://example.org/json", json_body={"b": 1, "a": [2]})
    call = transport.calls[0]
    assert call["body"] == util.canonical_json({"b": 1, "a": [2]}).encode()
    assert call["headers"]["content-type"] == "application/json"


def test_form_fields_are_urlencoded(tmp_path):
    transport = FakeTransport([FakeTransport.json_response({"ok": 1})])
    ctx = make_context(tmp_path, transport=transport)
    fields = {"b": "2", "a": "1"}
    http.request(ctx, "POST", "https://example.org/form", form_fields=fields)
    call = transport.calls[0]
    assert call["body"] == urlencode(fields).encode()
    assert call["headers"]["content-type"] == "application/x-www-form-urlencoded"


def test_multipart_upload_encoding(tmp_path):
    transport = FakeTransport([FakeTransport.json_response({"ok": 1})])
    ctx = make_context(tmp_path, transport=transport)
    http.request(
        ctx,
        "POST",
        "https://search.foldseek.com/api/ticket",
        files=[("file", "query.pdb", "application/octet-stream", b"ATOM 1 N MET")],
    )
    call = transport.calls[0]
    assert b'filename="query.pdb"' in call["body"]
    assert b"ATOM 1 N MET" in call["body"]
    assert call["headers"]["content-type"].startswith("multipart/form-data; boundary=")


def test_request_headers_include_user_agent_and_contact_email(tmp_path):
    transport = FakeTransport([FakeTransport.json_response({"ok": 1})])
    ctx = make_context(tmp_path, transport=transport, contact_email="dev@example.org")
    http.request(ctx, "GET", "https://example.org/ua")
    headers = transport.calls[0]["headers"]
    assert headers["user-agent"] == ctx.user_agent
    assert headers["from"] == "dev@example.org"


# ---------------------------------------------------------------------------
# response decoding
# ---------------------------------------------------------------------------


def test_gzip_content_encoding_is_decoded_before_caching(tmp_path):
    url = "https://example.org/gzip"
    compressed = util.gzip_compress(b'{"value": 42}')
    transport = FakeTransport(
        [
            TransportResponse(
                status=200,
                headers={"content-type": "application/json", "content-encoding": "gzip"},
                body=compressed,
                url=url,
            )
        ]
    )
    ctx = make_context(tmp_path, transport=transport)
    value, response = http.get_json(ctx, url)
    assert value == {"value": 42}
    assert response.body == b'{"value": 42}'
    assert response.headers["content-encoding"] == "gzip"
    assert response.cache_key is not None
    # Cache stores decoded bodies, exactly like the online path.
    assert Cache(ctx.cache_dir).lookup(response.cache_key, verify=True).read_bytes() == b'{"value": 42}'
    # Offline replay must also return the decoded payload (no double decompress).
    offline = make_context(tmp_path, cache_dir=ctx.cache_dir, offline=True, transport=FakeTransport([]))
    assert http.get_json(offline, url)[0] == {"value": 42}


def test_get_json_get_text_get_bytes(tmp_path):
    transport = FakeTransport(
        [
            FakeTransport.json_response({"a": 1}),
            FakeTransport.text_response("plain text"),
            TransportResponse(
                status=200, headers={"content-type": "application/octet-stream"}, body=b"\x00\x01\x02", url=""
            ),
        ]
    )
    ctx = make_context(tmp_path, transport=transport)
    value, json_response = http.get_json(ctx, "https://example.org/j")
    text, text_response = http.get_text(ctx, "https://example.org/t")
    raw, bytes_response = http.get_bytes(ctx, "https://example.org/b")
    assert value == {"a": 1} and json_response.status == 200
    assert text == "plain text" and text_response.content_type.startswith("text/plain")
    assert raw == b"\x00\x01\x02" and bytes_response.status == 200


def test_invalid_json_raises_data_error_with_preview(tmp_path):
    transport = FakeTransport([FakeTransport.text_response("<html>not json</html>")])
    ctx = make_context(tmp_path, transport=transport)
    _, response = http.get_text(ctx, "https://example.org/html")
    # get_text does not parse; the response object raises with diagnostics.
    with pytest.raises(DataError) as excinfo:
        response.json()
    assert excinfo.value.code == "DATA_INVALID"
    assert "not json" in excinfo.value.details["body_preview"]


def test_release_header_is_recorded_in_registry_and_cache(tmp_path):
    from lifesci import provenance

    url = "https://rest.uniprot.org/uniprotkb/stream"
    transport = FakeTransport(
        [FakeTransport.json_response({"results": []}, headers={"x-uniprot-release": "2026_04"})]
    )
    ctx = make_context(tmp_path, transport=transport)
    response = http.request(
        ctx,
        "GET",
        url,
        database="uniprot",
        release_header="x-uniprot-release",
    )
    assert response.release == "2026_04"
    assert provenance.load_releases(ctx.cache_dir)["uniprot"]["release"] == "2026_04"
    assert response.cache_key is not None
    entry = Cache(ctx.cache_dir).lookup(response.cache_key, verify=True)
    assert entry is not None and entry.release == "2026_04"


def test_allow_status_returns_response_without_raising(tmp_path):
    transport = FakeTransport([FakeTransport.error_response(404, body=b"missing")])
    ctx = make_context(tmp_path, transport=transport)
    response = http.request(ctx, "GET", "https://example.org/check", allow_status=(200, 404))
    assert response.status == 404
    assert response.ok is False
    assert len(transport.calls) == 1
    assert ctx.warnings == []


# ---------------------------------------------------------------------------
# response object / provenance
# ---------------------------------------------------------------------------


def test_http_response_helpers():
    response = http.HttpResponse(
        method="GET",
        url="https://example.org/a",
        final_url="https://example.org/a",
        status=200,
        headers={"content-type": "application/json"},
        body=b'{"a": 1}',
        from_cache=False,
        cache_key="k" * 64,
        attempts=1,
        elapsed_ms=12.34,
        request_id="abcdef0123456789",
    )
    assert response.ok is True
    assert response.text() == '{"a": 1}'
    assert response.json() == {"a": 1}
    description = response.describe()
    assert description["attempts"] == 1
    assert description["bytes"] == 8
    assert description["cache_key"] == "k" * 64
    assert description["release"] is None
    assert description["dry_run"] is False
    failed = http.HttpResponse(
        method="GET",
        url="u",
        final_url="u",
        status=500,
        headers={},
        body=b"",
        from_cache=False,
        cache_key=None,
        attempts=1,
        elapsed_ms=0.0,
        request_id="x",
    )
    assert failed.ok is False
    dry = http.HttpResponse(
        method="GET",
        url="u",
        final_url="u",
        status=None,
        headers={},
        body=b"",
        from_cache=False,
        cache_key=None,
        attempts=0,
        elapsed_ms=0.0,
        request_id="x",
        dry_run=True,
    )
    assert dry.ok is True


def test_provenance_entry_recorded_for_successful_request(tmp_path):
    payload = {"results": [1, 2, 3]}
    transport = FakeTransport([FakeTransport.json_response(payload)])
    ctx = make_context(tmp_path, transport=transport)
    _, response = http.get_json(ctx, "https://example.org/prov", database="example")
    entries = [entry for entry in ctx.provenance if entry.get("kind") == "http"]
    assert len(entries) == 1
    entry = entries[0]
    assert entry["method"] == "GET"
    assert entry["status"] == 200
    assert entry["sha256"] == util.sha256_bytes(response.body)
    assert entry["bytes"] == len(response.body)
    assert entry["request_id"] == response.request_id
    assert entry["cache"] == "store"
    assert entry["retries"] == 0
    assert entry["database"] == "example"
    # The JSONL provenance log is written beside the cache.
    records = util.read_jsonl(ctx.log_path)
    assert len(records) == 1
    assert records[-1]["kind"] == "http"
    assert records[-1]["request_id"] == response.request_id


# ---------------------------------------------------------------------------
# cached-payload rules (foundation hardening)
# ---------------------------------------------------------------------------


def _store_cached_response(
    ctx,
    *,
    url: str,
    body: bytes = b"{}",
    status: int = 200,
    content_type: str = "application/json",
    params=None,
) -> str:
    """Write a synthetic cache entry the way a previous request would have."""
    key = cache_key("GET", url, params=params, request_headers={"accept": "application/json"})
    Cache(ctx.cache_dir).store(
        key,
        body,
        status=status,
        method="GET",
        url=url,
        params=params,
        request_headers={"accept": "application/json"},
        response_headers={"content-type": content_type},
        content_type=content_type,
    )
    return key


def test_cached_non_allowed_status_is_refetched_online(tmp_path):
    url = "https://example.org/stale-500"
    ctx = make_context(tmp_path, transport=FakeTransport([FakeTransport.json_response({"fresh": True})]))
    _store_cached_response(ctx, url=url, status=500, body=b'{"error": "down"}')
    value, response = http.get_json(ctx, url)
    assert value == {"fresh": True}
    assert response.from_cache is False
    assert len(ctx.transport.calls) == 1
    assert_warning(ctx, "CACHE_STATUS_NOT_ALLOWED")


def test_cached_non_allowed_status_is_rejected_offline(tmp_path):
    url = "https://example.org/stale-500-offline"
    ctx = make_context(tmp_path, transport=FakeTransport([]))
    _store_cached_response(ctx, url=url, status=500, body=b'{"error": "down"}')
    offline = make_context(
        tmp_path, cache_dir=ctx.cache_dir, offline=True, transport=FakeTransport([])
    )
    with pytest.raises(PortalError) as excinfo:
        http.get_json(offline, url)
    error = excinfo.value
    assert error.code == "CACHE_STATUS_NOT_ALLOWED"
    assert error.retriable is False
    assert offline.transport.calls == []


def test_cached_body_error_is_rejected_by_detector_online(tmp_path):
    url = "https://example.org/stale-body-error"
    ctx = make_context(tmp_path, transport=FakeTransport([FakeTransport.json_response({"esearchresult": {"count": 1}})]))
    _store_cached_response(ctx, url=url, body=b'{"error": "Invalid uid"}')
    value, response = http.get_json(
        ctx, url, detect_error=http.error_detector_from_keys("error")
    )
    assert value == {"esearchresult": {"count": 1}}
    assert response.from_cache is False
    assert_warning(ctx, "CACHE_ENTRY_REJECTED")


def test_cached_body_error_is_rejected_offline(tmp_path):
    url = "https://example.org/stale-body-error-offline"
    ctx = make_context(tmp_path, transport=FakeTransport([]))
    _store_cached_response(ctx, url=url, body=b'{"error": "Invalid uid"}')
    offline = make_context(tmp_path, cache_dir=ctx.cache_dir, offline=True, transport=FakeTransport([]))
    with pytest.raises(PortalError) as excinfo:
        http.get_json(offline, url, detect_error=http.error_detector_from_keys("error"))
    assert excinfo.value.code == "CACHE_ENTRY_REJECTED"


def test_body_error_retry_honours_retry_after(tmp_path):
    transport = FakeTransport(
        [
            FakeTransport.json_response({"error": "try later"}, headers={"retry-after": "4"}),
            FakeTransport.json_response({"ok": True}),
        ]
    )
    ctx = make_context(tmp_path, transport=transport, max_retries=2)
    value, response = http.get_json(
        ctx,
        "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi",
        detect_error=http.error_detector_from_keys("error"),
    )
    assert value == {"ok": True}
    assert response.attempts == 2
    assert ctx._test_clock.sleeps == [pytest.approx(4.0)]
    assert warning_codes(ctx) == ["HTTP_BODY_ERROR_RETRY"]


def test_final_429_penalises_host_even_after_retry_budget(tmp_path):
    transport = FakeTransport([FakeTransport.error_response(429, body=b"slow down")] * 3)
    ctx = make_context(tmp_path, transport=transport, max_retries=2)
    with pytest.raises(PortalError) as excinfo:
        http.request(ctx, "GET", "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi")
    assert excinfo.value.code == "HTTP_ERROR_429"
    assert excinfo.value.retriable is True
    host = "eutils.ncbi.nlm.nih.gov"
    assert host in ctx._limiter.cooldown_until
    # The final 429 applies ctx.retry_backoff_max (30 s) as the host cooldown.
    assert ctx._limiter.cooldown_until[host] - ctx._test_clock.now == pytest.approx(30.0)


def test_gzip_cache_metadata_does_not_advertise_gzip(tmp_path):
    url = "https://example.org/gzip-meta"
    compressed = util.gzip_compress(b'{"value": 1}')
    transport = FakeTransport(
        [
            TransportResponse(
                status=200,
                headers={"content-type": "application/json", "content-encoding": "gzip"},
                body=compressed,
                url=url,
            )
        ]
    )
    ctx = make_context(tmp_path, transport=transport)
    _, response = http.get_json(ctx, url)
    assert response.cache_key is not None
    entry = Cache(ctx.cache_dir).lookup(response.cache_key, verify=True)
    assert entry is not None
    cached_header_names = {name.lower() for name in entry.meta["response_headers"]}
    assert "content-encoding" not in cached_header_names
    assert entry.read_bytes(verify=True) == b'{"value": 1}'


def test_multipart_encoding_is_deterministic():
    fields = {"b": "2", "a": "1"}
    files = [("file", "query.pdb", "application/octet-stream", b"ATOM 1 N MET")]
    first_body, first_type = multipart_encode(fields, files)
    second_body, second_type = multipart_encode(dict(fields), list(files))
    assert first_body == second_body
    assert first_type == second_type
    boundary = first_type.split("boundary=", 1)[1]
    assert boundary.encode() in first_body
    fixed_body, fixed_type = multipart_encode(fields, files, boundary="fixed-boundary")
    assert fixed_type == "multipart/form-data; boundary=fixed-boundary"
    assert b"--fixed-boundary" in fixed_body
