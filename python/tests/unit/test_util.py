"""Unit tests for :mod:`lifesci.util` — canonical JSON, hashing, IO, numerics, text."""

from __future__ import annotations

import dataclasses
import gzip
import math
import re
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path

import pytest

from lifesci import util
from lifesci.errors import DataError


# ---------------------------------------------------------------------------
# JSON
# ---------------------------------------------------------------------------


def test_canonical_json_is_deterministic_across_insertion_order():
    first = {"b": [2, 1], "a": {"y": True, "x": None}}
    second = {"a": {"x": None, "y": True}, "b": [2, 1]}
    assert util.canonical_json(first) == util.canonical_json(second)
    assert util.canonical_json(first) == '{"a":{"x":null,"y":true},"b":[2,1]}'


def test_canonical_json_preserves_unicode_and_stringifies_mapping_keys():
    payload = {"é": "ünïcode", 1: "one"}
    encoded = util.canonical_json(payload)
    assert "ünïcode" in encoded  # ensure_ascii=False
    assert '"1":"one"' in encoded
    # A round-trip through json.loads gives the canonical structure back.
    import json

    assert json.loads(encoded) == {"é": "ünïcode", "1": "one"}


def test_json_safe_converts_paths_bytes_sets_and_dataclasses():
    @dataclasses.dataclass
    class Record:
        name: str
        path: Path

    safe = util.json_safe(
        {
            "path": Path("/tmp/x"),
            "utf8": b"hello",
            "latin1": b"\xff\xfe",
            "set": {"only"},
            "tuple": (1, 2),
            "record": Record("r", Path("/tmp/y")),
            7: "seven",
        }
    )
    assert safe["path"] == "/tmp/x"
    assert safe["utf8"] == "hello"
    assert safe["latin1"] == "\xff\xfe"
    assert safe["set"] == ["only"]
    assert safe["tuple"] == [1, 2]
    assert safe["record"] == {"name": "r", "path": "/tmp/y"}
    assert safe["7"] == "seven"
    util.canonical_json(safe)  # must always serialise


def test_json_safe_maps_nan_and_infinity_to_null():
    safe = util.json_safe({"nan": math.nan, "inf": math.inf, "ninf": -math.inf, "ok": 1.5})
    assert safe == {"nan": None, "inf": None, "ninf": None, "ok": 1.5}
    import json

    assert json.loads(util.dumps_compact(safe)) == safe


def test_json_safe_handles_numpy_scalars_and_arrays():
    numpy = pytest.importorskip("numpy")
    safe = util.json_safe(
        {
            "int": numpy.int64(7),
            "float": numpy.float64(2.5),
            "array": numpy.array([1.0, numpy.nan]),
            "ints": numpy.array([1, 2], dtype=numpy.int16),
        }
    )
    assert safe["int"] == 7 and isinstance(safe["int"], int)
    assert safe["float"] == 2.5 and isinstance(safe["float"], float)
    assert safe["array"] == [1.0, None]  # NaN is not valid JSON
    assert safe["ints"] == [1, 2]
    util.canonical_json(safe)


def test_dumps_pretty_is_sorted_and_newline_terminated():
    text = util.dumps_pretty({"b": 1, "a": 2})
    assert text.startswith("{\n")
    assert text.index('"a"') < text.index('"b"')
    assert text.endswith("\n")


# ---------------------------------------------------------------------------
# hashing
# ---------------------------------------------------------------------------


def test_sha256_helpers_agree(tmp_path: Path):
    data = b"lifesci\x00payload"
    path = tmp_path / "blob.bin"
    path.write_bytes(data)
    assert util.sha256_bytes(data) == util.sha256_file(path)
    assert util.sha256_bytes(data) == util.sha256_text(data.decode("utf-8"))
    assert util.sha256_bytes(b"") == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    with pytest.raises(FileNotFoundError):
        util.sha256_file(tmp_path / "missing.bin")


def test_short_hash_truncates_deterministically():
    assert util.short_hash("abc", 6) == util.sha256_text("abc")[:6]
    assert len(util.short_hash(b"abc")) == 12


# ---------------------------------------------------------------------------
# atomic IO / JSON / JSONL
# ---------------------------------------------------------------------------


def test_atomic_write_bytes_creates_parents_and_replaces(tmp_path: Path):
    target = tmp_path / "deep" / "nested" / "file.bin"
    util.atomic_write_bytes(target, b"first")
    assert target.read_bytes() == b"first"
    util.atomic_write_bytes(target, b"second")
    assert target.read_bytes() == b"second"
    # No temp files left behind after successful writes.
    assert list(target.parent.glob(".*.tmp")) == []


def test_atomic_write_text_and_json(tmp_path: Path):
    text_path = util.atomic_write_text(tmp_path / "a.txt", "héllo")
    assert text_path.read_text(encoding="utf-8") == "héllo"
    json_path = util.atomic_write_json(tmp_path / "a.json", {"b": 1, "a": [True, None]})
    assert util.read_json(json_path) == {"b": 1, "a": [True, None]}


def test_read_json_missing_returns_default_and_invalid_raises(tmp_path: Path):
    assert util.read_json(tmp_path / "missing.json") is None
    assert util.read_json(tmp_path / "missing.json", {"x": 1}) == {"x": 1}
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    with pytest.raises(DataError) as excinfo:
        util.read_json(bad)
    assert excinfo.value.code == "DATA_INVALID"
    assert excinfo.value.details["line"] == 1


def test_append_read_jsonl_roundtrip(tmp_path: Path):
    path = tmp_path / "log.jsonl"
    assert util.read_jsonl(path) == []  # missing file -> empty
    util.append_jsonl(path, {"kind": "a", "n": 1})
    util.append_jsonl(path, {"kind": "b", "n": 2, "path": Path("/tmp/x")})
    records = util.read_jsonl(path)
    assert [record["kind"] for record in records] == ["a", "b"]
    assert records[1]["path"] == "/tmp/x"
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2 and all(line.startswith("{") for line in lines)


def test_read_jsonl_corrupt_line_reports_line_number(tmp_path: Path):
    path = tmp_path / "log.jsonl"
    path.write_text('{"ok": 1}\nnot-json\n', encoding="utf-8")
    with pytest.raises(DataError) as excinfo:
        util.read_jsonl(path)
    assert excinfo.value.details["line"] == 2


def test_deep_merge_and_get_dotted():
    base = {"a": {"b": 1, "c": 2}, "d": 4}
    merged = util.deep_merge(base, {"a": {"c": 9, "e": 5}})
    assert merged == {"a": {"b": 1, "c": 9, "e": 5}, "d": 4}
    assert base["a"]["c"] == 2  # not mutated
    assert util.get_dotted(merged, "a.e") == 5
    assert util.get_dotted(merged, "a.missing", "fallback") == "fallback"
    assert util.get_dotted(merged, "d") == 4


# ---------------------------------------------------------------------------
# gzip
# ---------------------------------------------------------------------------


def test_gzip_compress_is_reproducible_and_roundtrips():
    data = b"ACGT" * 1000
    first = util.gzip_compress(data)
    second = util.gzip_compress(data)
    assert first == second  # deterministic bytes (mtime pinned)
    assert util.is_gzip_bytes(first)
    assert util.gzip_decompress(first) == data


def test_gzip_decompress_invalid_raises_data_error():
    with pytest.raises(DataError):
        util.gzip_decompress(b"definitely not gzip")


def test_read_maybe_gzip_detects_magic_not_extension(tmp_path: Path):
    path = tmp_path / "payload.noext"
    path.write_bytes(util.gzip_compress(b"compressed text"))
    assert util.read_maybe_gzip(path) == b"compressed text"
    plain = tmp_path / "plain.bin"
    plain.write_bytes(b"plain")
    assert util.read_maybe_gzip(plain) == b"plain"


# ---------------------------------------------------------------------------
# FASTA / TSV
# ---------------------------------------------------------------------------


def test_fasta_write_read_roundtrip(tmp_path: Path):
    path = tmp_path / "seqs.fa"
    util.write_fasta(path, [("P69905", "MSEQ"), (">P00698 desc", "MK")])
    assert util.read_fasta(path) == [("P69905", "MSEQ"), ("P00698 desc", "MK")]


def test_read_fasta_handles_blank_lines_and_gzip(tmp_path: Path):
    text = "\n>a first\nAC\n\nGT\n>b\nTT\n"
    path = tmp_path / "in.fa.gz"
    path.write_bytes(util.gzip_compress(text.encode()))
    assert util.read_fasta(path) == [("a first", "ACGT"), ("b", "TT")]


def test_read_fasta_rejects_empty(tmp_path: Path):
    path = tmp_path / "empty.fa"
    path.write_text("\n\n;nope\n", encoding="utf-8")
    with pytest.raises(DataError):
        util.read_fasta(path)


def test_write_tsv_read_tsv_roundtrip(tmp_path: Path):
    path = tmp_path / "t.tsv"
    rows = [{"id": "a", "n": 1}, {"id": "b", "n": 2.5}]
    util.write_tsv(path, rows, columns=["id", "n"])
    assert util.read_tsv(path) == [{"id": "a", "n": "1"}, {"id": "b", "n": "2.5"}]
    # Comments and blank lines in an input TSV are ignored.
    path.write_text("# comment\nid\tn\n\na\t1\n", encoding="utf-8")
    assert util.read_tsv(path) == [{"id": "a", "n": "1"}]


def test_write_tsv_empty_rows_writes_empty_file(tmp_path: Path):
    path = tmp_path / "empty.tsv"
    util.write_tsv(path, [])
    assert path.read_text(encoding="utf-8") == ""
    assert util.read_tsv(path) == []


def test_read_tsv_rejects_ragged_rows(tmp_path: Path):
    path = tmp_path / "bad.tsv"
    path.write_text("a\tb\n1\t2\t3\n", encoding="utf-8")
    with pytest.raises(DataError) as excinfo:
        util.read_tsv(path)
    assert excinfo.value.details["line"] == 2


# ---------------------------------------------------------------------------
# numbers / formatting
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, "NA"),
        (0, "0"),
        (1.5e-26, "1.5e-26"),
        (3.9e-112, "3.9e-112"),
        (2.2e-30, "2.2e-30"),
        (0.063, "0.063"),
        (1.5, "1.5"),
        (math.inf, "NA"),
        (math.nan, "NA"),
    ],
)
def test_fmt_evalue(value, expected):
    assert util.fmt_evalue(value) == expected


def test_fmt_float():
    assert util.fmt_float(None) == "NA"
    assert util.fmt_float(math.nan) == "NA"
    assert util.fmt_float(3.0) == "3"
    assert util.fmt_float(1.23456789, digits=4) == "1.235"


def test_parse_float_and_parse_int():
    assert util.parse_float("1.5e-3") == pytest.approx(1.5e-3)
    assert util.parse_float("NA") is None
    assert util.parse_float("", default=2.0) == 2.0
    assert util.parse_float("garbage", default=-1.0) == -1.0
    assert util.parse_int("42.9") == 42
    assert util.parse_int(None, default=7) == 7


def test_percentile_is_linear_interpolated_and_clamped():
    values = [1.0, 2.0, 3.0, 4.0]
    assert util.percentile(values, 0.0) == 1.0
    assert util.percentile(values, 0.5) == 2.5
    assert util.percentile(values, 1.0) == 4.0
    assert util.percentile(values, 2.0) == 4.0  # clamp high
    assert util.percentile(values, -1.0) == 1.0  # clamp low
    assert util.percentile([5.0], 0.9) == 5.0
    with pytest.raises(ValueError):
        util.percentile([], 0.5)


def test_median():
    assert util.median([3, 1, 2]) == 2.0
    assert util.median([4, 1, 2, 3]) == 2.5
    assert util.median([]) is None
    # Does not mutate the caller's sequence.
    values = [3.0, 1.0]
    util.median(values)
    assert values == [3.0, 1.0]


def test_safe_div():
    assert util.safe_div(4, 2) == 2.0
    assert util.safe_div(4, 0, default=-1.0) == -1.0


# ---------------------------------------------------------------------------
# retry-after parsing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(None, 2.0), ("", 2.0), ("5", 5.0), ("5.5", 5.5), ("-3", 0.0), ("garbage", 2.0)],
)
def test_parse_retry_after_delta_seconds(raw, expected):
    assert util.parse_retry_after(raw) == expected


def test_parse_retry_after_http_date():
    future = datetime.now(timezone.utc) + timedelta(seconds=60)
    parsed = util.parse_retry_after(format_datetime(future))
    assert 50.0 <= parsed <= 70.0
    past = datetime.now(timezone.utc) - timedelta(seconds=60)
    assert util.parse_retry_after(format_datetime(past)) == 0.0


# ---------------------------------------------------------------------------
# text / collections / env / time
# ---------------------------------------------------------------------------


def test_stable_sorted_none_last_numeric_and_string():
    numeric = util.stable_sorted([{"v": 10}, {"v": None}, {"v": 2}], ["v"])
    assert [record["v"] for record in numeric] == [2, 10, None]
    strings = util.stable_sorted([{"v": "b"}, {"v": "a"}], ["v"])
    assert [record["v"] for record in strings] == ["a", "b"]
    # A secondary key breaks ties deterministically.
    tied = util.stable_sorted([{"a": 1, "b": 2}, {"a": 1, "b": 1}], ["a", "b"])
    assert [record["b"] for record in tied] == [1, 2]


def test_stable_sorted_returns_copies():
    source = [{"v": 2}, {"v": 1}]
    ordered = util.stable_sorted(source, ["v"])
    ordered[0]["v"] = 99
    assert source[0]["v"] == 2


def test_unique_preserving_order_and_chunked():
    assert util.unique_preserving_order([1, 2, 1, 3, 2]) == [1, 2, 3]
    assert list(util.chunked([1, 2, 3, 4, 5], 2)) == [[1, 2], [3, 4], [5]]
    assert list(util.chunked([], 3)) == []


def test_slugify_and_truncate():
    assert util.slugify("Hello, World!") == "hello-world"
    assert util.slugify("!!!", fallback="item") == "item"
    assert util.slugify("abc", max_length=2) == "ab"
    assert util.truncate("short", limit=10) == "short"
    truncated = util.truncate("a" * 20, limit=10)
    assert truncated == "a" * 9 + "…"
    assert len(truncated) == 10


def test_human_duration():
    assert util.human_duration(3.24) == "3.2 s"
    assert util.human_duration(252) == "4m 12s"
    assert util.human_duration(3780) == "1h 03m"


def test_now_iso_and_utc_tag_formats():
    assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$", util.now_iso())
    assert re.match(r"^\d{8}T\d{6}Z$", util.utc_tag())


def test_timer_records_elapsed_ms():
    with util.timer() as box:
        pass
    assert box["elapsed_ms"] >= 0.0


def test_env_helpers(monkeypatch):
    assert util.env_str("LIFESCI_TEST_MISSING", "default") == "default"
    monkeypatch.setenv("LIFESCI_TEST_EMPTY", "")
    assert util.env_str("LIFESCI_TEST_EMPTY", "default") == "default"
    monkeypatch.setenv("LIFESCI_TEST_STR", "value")
    assert util.env_str("LIFESCI_TEST_STR") == "value"

    monkeypatch.setenv("LIFESCI_TEST_BOOL", "YES")
    assert util.env_bool("LIFESCI_TEST_BOOL") is True
    monkeypatch.setenv("LIFESCI_TEST_BOOL", "0")
    assert util.env_bool("LIFESCI_TEST_BOOL", default=True) is False
    assert util.env_bool("LIFESCI_TEST_MISSING", default=True) is True

    monkeypatch.setenv("LIFESCI_TEST_INT", "nope")
    assert util.env_int("LIFESCI_TEST_INT", 3) == 3
    monkeypatch.setenv("LIFESCI_TEST_INT", "42")
    assert util.env_int("LIFESCI_TEST_INT", 3) == 42

    monkeypatch.setenv("LIFESCI_TEST_FLOAT", "1.25")
    assert util.env_float("LIFESCI_TEST_FLOAT", 0.0) == 1.25
    monkeypatch.setenv("LIFESCI_TEST_FLOAT", "bad")
    assert util.env_float("LIFESCI_TEST_FLOAT", 0.0) == 0.0

    monkeypatch.setenv("LIFESCI_TEST_JSON", '{"a": 1}')
    assert util.env_json("LIFESCI_TEST_JSON") == {"a": 1}
    monkeypatch.setenv("LIFESCI_TEST_JSON", "{bad")
    assert util.env_json("LIFESCI_TEST_JSON", default={"d": 1}) == {"d": 1}


def test_parse_fasta_text():
    records = util.parse_fasta_text(">a\nAC\n>b x\nTT\n")
    assert records == [("a", "AC"), ("b x", "TT")]
    with pytest.raises(DataError):
        util.parse_fasta_text("no records here")


def test_is_gzip_bytes_is_extension_agnostic():
    assert util.is_gzip_bytes(gzip.compress(b"x"))
    assert not util.is_gzip_bytes(b"")
    assert not util.is_gzip_bytes(b"PK")


def test_json_safe_serialises_sets_deterministically():
    """Sets are unordered in Python but must produce stable JSON/cache keys."""
    first = util.json_safe({"items": {"beta", "alpha", "gamma"}})
    second = util.json_safe({"items": {"gamma", "beta", "alpha"}})
    assert first == second == {"items": ["alpha", "beta", "gamma"]}
    assert util.canonical_json({"x": {"b", "a"}}) == util.canonical_json({"x": {"a", "b"}})
    nested = util.json_safe({"outer": [{"inner": frozenset({"z", "y"})}]})
    assert nested == {"outer": [{"inner": ["y", "z"]}]}
