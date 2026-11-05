"""Unit tests for :mod:`lifesci.cache` — content-addressed cache, TTL, integrity, seeding."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from lifesci import cache as cache_mod
from lifesci import util
from lifesci.cache import CACHE_FORMAT, Cache, CacheEntry, cache_from_context, cache_key, store_fixture_payload
from lifesci.errors import CacheCorruptError, ConfigError

from tests.conftest import make_context


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _store(cache: Cache, key: str, body: bytes = b'{"value": 1}', **overrides):
    kwargs = dict(
        status=200,
        method="GET",
        url="https://example.org/api/x",
        params={"q": "1"},
        content_type="application/json",
        release="r1",
        database="example",
    )
    kwargs.update(overrides)
    return cache.store(key, body, **kwargs)


def _age_entry(cache: Cache, key: str, *, age_seconds: float, ttl_seconds: float = 60.0) -> CacheEntry:
    """Rewrite an entry's metadata so it is deterministically expired/young."""
    entry = cache.lookup(key, verify=True)
    assert entry is not None
    meta = dict(entry.meta)
    stored = datetime.now(timezone.utc) - timedelta(seconds=age_seconds)
    meta["stored_at"] = stored.isoformat(timespec="seconds").replace("+00:00", "Z")
    meta["ttl_seconds"] = ttl_seconds
    util.atomic_write_json(cache.meta_path_for(key), meta)
    return cache.lookup(key, verify=False)


# ---------------------------------------------------------------------------
# cache_key
# ---------------------------------------------------------------------------


def test_cache_key_is_deterministic_and_param_order_insensitive():
    first = cache_key("GET", "https://example.org/a", params={"b": 2, "a": 1})
    second = cache_key("get", "https://example.org/a", params={"a": 1, "b": 2})
    assert first == second
    assert len(first) == 64


@pytest.mark.parametrize(
    "changed",
    [
        {"url": "https://example.org/b"},
        {"params": {"b": 2, "a": 9}},
        {"method": "POST"},
        {"request_body": b'{"x": 1}'},
        {"request_headers": {"accept": "text/plain"}},
    ],
)
def test_cache_key_changes_when_request_changes(changed):
    base = dict(method="GET", url="https://example.org/a", params={"b": 2, "a": 1})
    base.update(changed)
    assert cache_key(**base) != cache_key("GET", "https://example.org/a", params={"b": 2, "a": 1})


def test_cache_key_ignores_volatile_headers_but_honours_accept_and_content_type():
    base = cache_key("GET", "https://example.org/a")
    assert cache_key("GET", "https://example.org/a", request_headers={"user-agent": "x"}) == base
    assert cache_key("GET", "https://example.org/a", request_headers={"authorization": "secret"}) == base
    assert cache_key("GET", "https://example.org/a", request_headers={"Accept": "application/json"}) != base
    assert (
        cache_key("GET", "https://example.org/a", request_headers={"accept": "application/json"})
        == cache_key("GET", "https://example.org/a", request_headers={"Accept": "application/json"})
    )
    post_json = cache_key("POST", "https://example.org/a", request_headers={"content-type": "application/json"})
    assert post_json != cache_key("POST", "https://example.org/a")


def test_cache_key_sequence_params_are_sorted():
    assert cache_key("GET", "u", params=[("b", 2), ("a", 1)]) == cache_key("GET", "u", params=[("a", 1), ("b", 2)])
    assert cache_key("GET", "u", params=None) == cache_key("GET", "u", params={})


# ---------------------------------------------------------------------------
# layout / roundtrip / integrity
# ---------------------------------------------------------------------------


def test_cache_creates_layout_and_marker(tmp_path: Path):
    cache = Cache(tmp_path / "cache")
    assert (tmp_path / "cache" / "cache.json").is_file()
    marker = util.read_json(tmp_path / "cache" / "cache.json")
    assert marker["format"] == CACHE_FORMAT
    assert (tmp_path / "cache" / "v1" / "objects").is_dir()
    assert cache.stats()["n_entries"] == 0


def test_store_lookup_roundtrip_and_sha_verification(tmp_path: Path):
    cache = Cache(tmp_path / "cache")
    body = b'{"hello": "world"}'
    stored = _store(cache, "a" * 64, body)
    assert stored.key == "a" * 64
    entry = cache.lookup("a" * 64, verify=True)
    assert entry is not None
    assert entry.read_bytes(verify=True) == body
    assert entry.sha256 == util.sha256_bytes(body)
    assert entry.bytes == len(body)
    assert entry.url == "https://example.org/api/x"
    assert entry.status == 200
    assert entry.release == "r1"
    assert entry.database == "example"
    assert entry.content_type == "application/json"
    assert entry.read_json() == {"hello": "world"}
    summary = entry.summary()
    assert summary["cache_hit"] is True
    assert summary["key"] == "a" * 64
    assert cache.has("a" * 64) is True
    assert cache.has("b" * 64) is False
    assert cache.lookup("b" * 64) is None


def test_corrupted_body_detected_by_hash(tmp_path: Path):
    cache = Cache(tmp_path / "cache")
    body = b"original payload"
    _store(cache, "c" * 64, body)
    cache.body_path_for("c" * 64).write_bytes(b"tampered payload!")
    with pytest.raises(CacheCorruptError) as excinfo:
        cache.lookup("c" * 64, verify=True)
    assert excinfo.value.code == "CACHE_CORRUPT"
    assert excinfo.value.details["expected_sha256"] == util.sha256_bytes(body)
    # Without verification the entry is still readable (caller opted out).
    loose = cache.lookup("c" * 64, verify=False)
    assert loose is not None
    with pytest.raises(CacheCorruptError):
        loose.read_bytes(verify=True)
    assert loose.read_bytes(verify=False) == b"tampered payload!"
    # verify_all reports it instead of raising.
    report = cache.verify_all()
    assert len(report) == 1 and report[0]["ok"] is False and "hash mismatch" in report[0]["error"]


def test_corrupt_metadata_and_missing_body_raise(tmp_path: Path):
    cache = Cache(tmp_path / "cache")
    _store(cache, "d" * 64)
    meta_path = cache.meta_path_for("d" * 64)
    meta_path.write_text("{not json", encoding="utf-8")
    with pytest.raises(CacheCorruptError):
        cache.lookup("d" * 64, verify=True)

    _store(cache, "e" * 64)
    cache.body_path_for("e" * 64).unlink()
    with pytest.raises(CacheCorruptError) as excinfo:
        cache.lookup("e" * 64, verify=True)
    assert "missing its body" in excinfo.value.message


def test_unsupported_cache_format_rejected(tmp_path: Path):
    cache = Cache(tmp_path / "cache")
    _store(cache, "f" * 64)
    meta = util.read_json(cache.meta_path_for("f" * 64))
    meta["format"] = 999
    util.atomic_write_json(cache.meta_path_for("f" * 64), meta)
    with pytest.raises(CacheCorruptError) as excinfo:
        cache.lookup("f" * 64, verify=True)
    assert "unsupported format" in excinfo.value.message


# ---------------------------------------------------------------------------
# TTL
# ---------------------------------------------------------------------------


def test_ttl_expiry_and_immutable_entries(tmp_path: Path):
    cache = Cache(tmp_path / "cache", ttl_default=3600.0)
    _store(cache, "1" * 64, ttl_seconds=3600.0)
    fresh = cache.lookup("1" * 64, verify=True)
    assert fresh is not None and fresh.expired is False

    old = _age_entry(cache, "1" * 64, age_seconds=7200.0, ttl_seconds=3600.0)
    assert old.expired is True

    _store(cache, "2" * 64, immutable=True)
    immutable = _age_entry(cache, "2" * 64, age_seconds=10 * 365 * 24 * 3600.0, ttl_seconds=0.0)
    assert immutable.ttl_seconds == 0.0
    assert immutable.expired is False  # ttl 0 == explicitly immutable


def test_stats_entries_prune_and_clear(tmp_path: Path):
    cache = Cache(tmp_path / "cache")
    _store(cache, "1" * 64, b"a" * 10, database="uniprot", release="2026_04")
    _store(cache, "2" * 64, b"b" * 20, database="interpro", release="105.0")
    expired = _store(cache, "3" * 64, b"c" * 5, database="uniprot", release="2026_04")
    _age_entry(cache, expired.key, age_seconds=10_000.0, ttl_seconds=60.0)

    stats = cache.stats()
    assert stats["n_entries"] == 3
    assert stats["n_expired"] == 1
    assert stats["bytes"] == 35
    assert stats["databases"] == {"uniprot": 2, "interpro": 1}
    assert stats["releases"] == {"uniprot": "2026_04", "interpro": "105.0"}
    assert stats["missing_bodies"] == 0

    entries = cache.entries()
    assert len(entries) == 3
    assert {entry.key for entry in entries} == {"1" * 64, "2" * 64, "3" * 64}

    dry = cache.prune_expired(dry_run=True)
    assert dry == {"n_pruned": 0, "n_expired": 1, "bytes_freed": 0, "dry_run": True}
    assert cache.stats()["n_entries"] == 3

    pruned = cache.prune_expired()
    assert pruned["n_pruned"] == 1 and pruned["bytes_freed"] == 5
    assert cache.stats()["n_entries"] == 2

    with pytest.raises(ConfigError):
        cache.clear(confirm=False)
    cleared = cache.clear()
    assert cleared["n_cleared"] == 2 and cleared["bytes_freed"] == 30
    assert cache.stats()["n_entries"] == 0


def test_stats_counts_missing_bodies_without_raising(tmp_path: Path):
    cache = Cache(tmp_path / "cache")
    _store(cache, "a" * 64)
    cache.body_path_for("a" * 64).unlink()
    assert cache.stats()["missing_bodies"] == 1


def test_delete_entry(tmp_path: Path):
    cache = Cache(tmp_path / "cache")
    _store(cache, "a" * 64)
    assert cache.delete("a" * 64) is True
    assert cache.delete("a" * 64) is False
    assert cache.lookup("a" * 64) is None


# ---------------------------------------------------------------------------
# blobs
# ---------------------------------------------------------------------------


def test_blob_store_and_load(tmp_path: Path):
    cache = Cache(tmp_path / "cache")
    record = cache.store_blob("Models/Pfam", "PF00042 hmm", b"HMMER3 data")
    assert record["sha256"] == util.sha256_bytes(b"HMMER3 data")
    assert record["bytes"] == 11
    assert cache.load_blob("Models/Pfam", "PF00042 hmm") == b"HMMER3 data"
    assert cache.load_blob("Models/Pfam", "missing") is None
    # Names are slugified into a safe filesystem path.
    assert "models-pfam" in record["path"]
    assert Path(record["path"]).read_bytes() == b"HMMER3 data"


# ---------------------------------------------------------------------------
# cache_from_context / store_fixture_payload
# ---------------------------------------------------------------------------


def test_cache_from_context_uses_context_ttl(tmp_path: Path):
    ctx = make_context(tmp_path, cache_ttl=1234.0)
    cache = cache_from_context(ctx)
    assert cache.root == ctx.cache_dir.resolve()
    assert cache.ttl_default == 1234.0


def test_store_fixture_payload(tmp_path: Path):
    payload = b'{"results": [{"id": "PF00042"}]}'
    recorded = tmp_path / "recorded" / "interpro.json"
    recorded.parent.mkdir(parents=True)
    recorded.write_bytes(payload)
    cache = Cache(tmp_path / "cache")
    entry = store_fixture_payload(
        cache,
        fixture_path=recorded,
        method="GET",
        url="https://www.ebi.ac.uk/interpro/api/x",
        params={"page_size": 50},
        request_headers={"accept": "application/json"},
        response_headers={"content-type": "application/json"},
        status=200,
        content_type="application/json",
        release="105.0",
        database="interpro",
    )
    expected_key = cache_key(
        "GET",
        "https://www.ebi.ac.uk/interpro/api/x",
        params={"page_size": 50},
        request_headers={"accept": "application/json"},
    )
    assert entry.key == expected_key
    looked_up = cache.lookup(expected_key, verify=True)
    assert looked_up is not None and looked_up.read_bytes() == payload
    assert looked_up.release == "105.0"
    assert looked_up.database == "interpro"
    assert looked_up.meta["notes"] == {"seeded_from_fixture": str(recorded)}


def test_store_fixture_payload_missing_file_raises(tmp_path: Path):
    cache = Cache(tmp_path / "cache")
    with pytest.raises(FileNotFoundError):
        store_fixture_payload(
            cache,
            fixture_path=tmp_path / "missing.json",
            method="GET",
            url="https://example.org/x",
        )


def test_cache_key_stable_after_metadata_write(tmp_path: Path):
    """The key is a function of the request only — never of stored metadata."""
    cache = Cache(tmp_path / "cache")
    key = cache_key("GET", "https://example.org/z", params={"a": "1"})
    _store(cache, key, b"payload")
    assert cache.lookup(key, verify=True) is not None
    assert key == cache_key("GET", "https://example.org/z", params={"a": "1"})


def test_cache_stats_json_safe(tmp_path: Path):
    cache = Cache(tmp_path / "cache")
    _store(cache, "a" * 64)
    encoded = json.dumps(cache.stats())  # must be strictly JSON-serialisable
    assert '"n_entries": 1' in encoded
    assert cache_mod.CACHE_FORMAT == CACHE_FORMAT


def test_cache_metadata_never_stores_credential_headers(tmp_path: Path):
    cache = Cache(tmp_path / "cache")
    entry = _store(
        cache,
        "a" * 64,
        request_headers={
            "accept": "application/json",
            "authorization": "Bearer super-secret",
            "cookie": "sid=1",
            "x-api-key": "key123",
        },
    )
    stored = entry.meta["request_headers"]
    assert stored == {"accept": "application/json"}
    assert "super-secret" not in util.canonical_json(entry.meta)
    assert "sid=1" not in util.canonical_json(entry.meta)


def test_maintenance_lock_can_be_acquired_sequentially(tmp_path: Path):
    """Regression: the lock contextmanager used to yield twice (generator bug)."""
    cache = Cache(tmp_path / "cache")
    lock_path = cache.root / cache_mod.LOCK_NAME
    with cache._maintenance_lock():
        assert lock_path.exists()
    assert not lock_path.exists()
    with cache._maintenance_lock():
        pass
    # prune/clear both acquire the lock; doing them back-to-back must not hang/raise.
    assert cache.prune_expired(dry_run=True)["n_expired"] == 0
    cache.clear()
