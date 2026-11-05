"""Small dependency-light utilities shared by every lifesci module.

Only the Python standard library is imported at module import time; heavy
optional libraries (numpy) are imported lazily inside helpers that need them.
"""

from __future__ import annotations

import contextlib
import gzip
import hashlib
import json
import math
import os
import re
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Sequence

from .errors import DataError


# ---------------------------------------------------------------------------
# time
# ---------------------------------------------------------------------------


def now_iso() -> str:
    """Current UTC time as ISO-8601 with a ``Z`` suffix (second precision ok)."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def utc_tag() -> str:
    """Compact UTC tag suitable for directory/file names (``20261105T120000Z``)."""
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


@contextlib.contextmanager
def timer() -> Iterator[dict[str, float]]:
    """Context manager yielding a dict filled with ``elapsed_ms`` on exit."""
    box: dict[str, float] = {"start": time.monotonic()}
    try:
        yield box
    finally:
        box["elapsed_ms"] = (time.monotonic() - box["start"]) * 1000.0


def human_duration(seconds: float) -> str:
    """Format a duration like ``3.2 s`` / ``4m 12s`` / ``1h 03m``."""
    if seconds < 60:
        return f"{seconds:.1f} s"
    minutes, sec = divmod(int(round(seconds)), 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes:02d}m"
    return f"{minutes}m {sec:02d}s"


# ---------------------------------------------------------------------------
# JSON
# ---------------------------------------------------------------------------


def json_safe(value: Any) -> Any:
    """Recursively convert common scientific Python objects to JSON values.

    Handles numpy scalars/arrays, ``Path`` objects, bytes (as UTF-8 or base64-ish
    latin-1 fallback), sets/tuples, and dataclasses.  NaN/inf become ``None``
    because strict JSON has no such values.
    """

    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (bytes, bytearray)):
        try:
            return bytes(value).decode("utf-8")
        except UnicodeDecodeError:
            return bytes(value).decode("latin-1")
    if isinstance(value, Mapping):
        return {str(k): json_safe(v) for k, v in value.items()}
    if isinstance(value, (set, frozenset)):
        # Sets have no meaningful order; sort by canonical JSON so
        # canonical_json()/cache keys are stable across hash seeds.
        return sorted(
            (json_safe(v) for v in value),
            key=lambda item: json.dumps(item, sort_keys=True, ensure_ascii=False, default=str),
        )
    if isinstance(value, (list, tuple)):
        return [json_safe(v) for v in value]

    # numpy / scipy scalars and arrays, without importing numpy eagerly.
    module = type(value).__module__
    if module.startswith("numpy"):
        if hasattr(value, "tolist"):
            return json_safe(value.tolist())
        if hasattr(value, "item"):
            return json_safe(value.item())
    if hasattr(value, "__dataclass_fields__"):
        from dataclasses import asdict

        return json_safe(asdict(value))
    return str(value)


def canonical_json(value: Any) -> str:
    """Stable canonical JSON used for cache keys and fingerprints."""
    return json.dumps(
        json_safe(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )


def dumps_pretty(value: Any) -> str:
    """Human/model-readable deterministic JSON (2-space indent, sorted keys)."""
    return json.dumps(json_safe(value), sort_keys=True, indent=2, ensure_ascii=False) + "\n"


def dumps_compact(value: Any) -> str:
    return json.dumps(json_safe(value), sort_keys=True, separators=(",", ":"))


def read_json(path: str | os.PathLike[str], default: Any = None) -> Any:
    """Read JSON, returning ``default`` when the file does not exist."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return default
    except json.JSONDecodeError as err:
        raise DataError(
            f"failed to parse JSON file {path}: {err}",
            details={"path": str(path), "line": err.lineno, "column": err.colno},
        ) from err


def deep_merge(base: Mapping[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    """Recursively merge ``override`` into a copy of ``base`` (dicts merge)."""
    out: dict[str, Any] = dict(base)
    for key, value in override.items():
        if key in out and isinstance(out[key], Mapping) and isinstance(value, Mapping):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def get_dotted(mapping: Mapping[str, Any], dotted: str, default: Any = None) -> Any:
    """Look up ``a.b.c`` in nested mappings."""
    node: Any = mapping
    for part in dotted.split("."):
        if not isinstance(node, Mapping) or part not in node:
            return default
        node = node[part]
    return node


# ---------------------------------------------------------------------------
# hashing
# ---------------------------------------------------------------------------


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_file(path: str | os.PathLike[str], chunk_size: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            block = fh.read(chunk_size)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def short_hash(value: str | bytes, length: int = 12) -> str:
    data = value.encode("utf-8") if isinstance(value, str) else value
    return sha256_bytes(data)[:length]


# ---------------------------------------------------------------------------
# atomic file IO
# ---------------------------------------------------------------------------


def ensure_dir(path: str | os.PathLike[str]) -> Path:
    target = Path(path)
    target.mkdir(parents=True, exist_ok=True)
    return target


def atomic_write_bytes(path: str | os.PathLike[str], data: bytes) -> Path:
    """Write bytes atomically (temp file in the same directory + ``os.replace``)."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent)
    )
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, target)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp_name)
        raise
    return target


def atomic_write_text(path: str | os.PathLike[str], text: str) -> Path:
    return atomic_write_bytes(path, text.encode("utf-8"))


def atomic_write_json(path: str | os.PathLike[str], value: Any) -> Path:
    return atomic_write_text(path, dumps_pretty(value))


def read_text(path: str | os.PathLike[str]) -> str:
    return Path(path).read_text(encoding="utf-8")


def append_jsonl(path: str | os.PathLike[str], record: Mapping[str, Any]) -> None:
    """Append one JSON line to ``path``; atomic within a line via ``O_APPEND``."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    line = dumps_compact(record) + "\n"
    with open(target, "a", encoding="utf-8") as fh:
        fh.write(line)


def read_jsonl(path: str | os.PathLike[str]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for lineno, line in enumerate(fh, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError as err:
                    raise DataError(
                        f"corrupt JSONL line in {path}:{lineno}: {err}",
                        details={"path": str(path), "line": lineno},
                    ) from err
    except FileNotFoundError:
        return records
    return records


# ---------------------------------------------------------------------------
# gzip / compression
# ---------------------------------------------------------------------------


def is_gzip_bytes(data: bytes) -> bool:
    return len(data) >= 2 and data[0] == 0x1F and data[1] == 0x8B


def gzip_decompress(data: bytes) -> bytes:
    try:
        return gzip.decompress(data)
    except OSError as err:
        raise DataError(
            "payload advertised as gzip could not be decompressed",
            details={"error": str(err), "bytes": len(data)},
        ) from err


def gzip_compress(data: bytes, mtime: int = 0) -> bytes:
    import io

    buf = io.BytesIO()
    # mtime=0 makes the compressed bytes reproducible for checksums.
    with gzip.GzipFile(fileobj=buf, mode="wb", mtime=mtime) as gz:
        gz.write(data)
    return buf.getvalue()


def read_maybe_gzip(path: str | os.PathLike[str]) -> bytes:
    """Read a file, transparently decompressing ``.gz`` / gzip-magic content."""
    raw = Path(path).read_bytes()
    if is_gzip_bytes(raw):
        return gzip_decompress(raw)
    return raw


# ---------------------------------------------------------------------------
# FASTA / TSV
# ---------------------------------------------------------------------------


def read_fasta(path: str | os.PathLike[str]) -> list[tuple[str, str]]:
    """Read FASTA preserving order.  Returns ``[(header_without_gt, sequence)]``.

    Handles gzip-compressed files and blank lines.  Raises :class:`DataError`
    when the file contains no records.
    """
    text = read_maybe_gzip(path).decode("utf-8", errors="replace")
    records: list[tuple[str, str]] = []
    name: str | None = None
    buf: list[str] = []
    for line in text.splitlines():
        if line.startswith(">"):
            if name is not None:
                records.append((name, "".join(buf)))
            name = line[1:].strip()
            buf = []
        elif line.strip():
            if name is None:
                continue
            buf.append(line.strip())
    if name is not None:
        records.append((name, "".join(buf)))
    if not records:
        raise DataError(f"no FASTA records found in {path}", details={"path": str(path)})
    return records


def write_fasta(path: str | os.PathLike[str], records: Iterable[tuple[str, str]]) -> Path:
    lines: list[str] = []
    for header, seq in records:
        if not header.startswith(">"):
            header = ">" + header
        lines.append(header)
        lines.append(seq)
    return atomic_write_text(path, "\n".join(lines) + "\n")


def parse_fasta_text(text: str) -> list[tuple[str, str]]:
    """Parse FASTA content already in memory (used for ESMFold/PDB payloads)."""
    records: list[tuple[str, str]] = []
    name: str | None = None
    buf: list[str] = []
    for line in text.splitlines():
        if line.startswith(">"):
            if name is not None:
                records.append((name, "".join(buf)))
            name = line[1:].strip()
            buf = []
        elif line.strip() and name is not None:
            buf.append(line.strip())
    if name is not None:
        records.append((name, "".join(buf)))
    if not records:
        raise DataError("no FASTA records found in provided text")
    return records


def read_tsv(
    path: str | os.PathLike[str],
    *,
    delimiter: str = "\t",
    comment: str = "#",
) -> list[dict[str, str]]:
    """Read a TSV/CSV with a header row into a list of dicts."""
    text = read_text(path)
    lines = [ln for ln in text.splitlines() if ln.strip() and not ln.startswith(comment)]
    if not lines:
        return []
    header = lines[0].split(delimiter)
    rows: list[dict[str, str]] = []
    for lineno, line in enumerate(lines[1:], 2):
        fields = line.split(delimiter)
        if len(fields) != len(header):
            raise DataError(
                f"{path}:{lineno}: expected {len(header)} columns, got {len(fields)}",
                details={"path": str(path), "line": lineno},
            )
        rows.append(dict(zip(header, fields)))
    return rows


def write_tsv(
    path: str | os.PathLike[str],
    rows: Sequence[Mapping[str, Any]],
    columns: Sequence[str] | None = None,
) -> Path:
    if not rows:
        return atomic_write_text(path, "")
    cols = list(columns) if columns else list(rows[0].keys())
    lines = ["\t".join(cols)]
    for row in rows:
        lines.append("\t".join(_cell(row.get(col, "")) for col in cols))
    return atomic_write_text(path, "\n".join(lines) + "\n")


def _cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        return fmt_float(value)
    if isinstance(value, (dict, list, tuple)):
        return canonical_json(value)
    return str(value)


# ---------------------------------------------------------------------------
# numbers / formatting
# ---------------------------------------------------------------------------


def fmt_float(value: float | None, digits: int = 4) -> str:
    """Format a float compactly: 6 significant digits, no trailing zeros."""
    if value is None:
        return "NA"
    if not math.isfinite(value):
        return "NA"
    if value == int(value) and abs(value) < 1e15:
        return str(int(value))
    return f"{value:.{digits}g}"


def fmt_evalue(value: float | None) -> str:
    """Format an E-value conventionally (``1.5e-26``)."""
    if value is None or not math.isfinite(value):
        return "NA"
    if value == 0:
        return "0"
    return f"{value:.2g}"


def parse_float(value: Any, default: float | None = None) -> float | None:
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text or text.upper() in {"NA", "N/A", "NAN", "NONE", "-"}:
        return default
    try:
        return float(text)
    except ValueError:
        return default


def parse_int(value: Any, default: int | None = None) -> int | None:
    number = parse_float(value)
    return default if number is None else int(number)


def safe_div(numerator: float, denominator: float, default: float = 0.0) -> float:
    return default if denominator == 0 else numerator / denominator


def percentile(sorted_values: Sequence[float], fraction: float) -> float:
    """Linear-interpolated percentile of an already-sorted sequence."""
    if not sorted_values:
        raise ValueError("percentile of empty sequence")
    if len(sorted_values) == 1:
        return float(sorted_values[0])
    pos = max(0.0, min(1.0, fraction)) * (len(sorted_values) - 1)
    low = int(math.floor(pos))
    high = int(math.ceil(pos))
    if low == high:
        return float(sorted_values[low])
    weight = pos - low
    return float(sorted_values[low] * (1 - weight) + sorted_values[high] * weight)


def median(values: Sequence[float]) -> float | None:
    ordered = sorted(values)
    if not ordered:
        return None
    return percentile(ordered, 0.5)


# ---------------------------------------------------------------------------
# text
# ---------------------------------------------------------------------------


_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slugify(text: str, *, max_length: int = 60, fallback: str = "item") -> str:
    slug = _SLUG_RE.sub("-", text.lower()).strip("-")[:max_length].strip("-")
    return slug or fallback


def truncate(text: str, limit: int = 120, suffix: str = "…") -> str:
    if len(text) <= limit:
        return text
    return text[: max(0, limit - len(suffix))] + suffix


def indent_lines(text: str, prefix: str = "  ") -> str:
    return "\n".join(prefix + line if line else line for line in text.splitlines())


def parse_retry_after(value: str | None, default: float = 2.0) -> float:
    """Parse a ``Retry-After`` header (delta-seconds or HTTP-date)."""
    if not value:
        return default
    value = value.strip()
    try:
        return max(0.0, float(value))
    except ValueError:
        from email.utils import parsedate_to_datetime

        try:
            when = parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return default
        if when is None:
            return default
        delta = (when - datetime.now(timezone.utc)).total_seconds()
        return max(0.0, delta)


# ---------------------------------------------------------------------------
# collections
# ---------------------------------------------------------------------------


def stable_sorted(records: Iterable[Mapping[str, Any]], keys: Sequence[str]) -> list[dict[str, Any]]:
    """Sort records by ``keys`` with ``None`` last and no locale dependence."""
    def sort_key(record: Mapping[str, Any]) -> tuple[Any, ...]:
        parts: list[Any] = []
        for key in keys:
            value = record.get(key)
            if value is None:
                parts.append((1, ""))
            elif isinstance(value, (int, float)):
                parts.append((0, float(value)))
            else:
                parts.append((0, str(value)))
        return tuple(parts)

    return sorted((dict(r) for r in records), key=sort_key)


def chunked(items: Sequence[Any], size: int) -> Iterator[Sequence[Any]]:
    for start in range(0, len(items), max(1, size)):
        yield items[start : start + size]


def unique_preserving_order(items: Iterable[Any]) -> list[Any]:
    seen: set[Any] = set()
    out: list[Any] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


# ---------------------------------------------------------------------------
# environment helpers
# ---------------------------------------------------------------------------


def env_str(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    return default if value is None or value == "" else value


def env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on", "y"}


def env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, ""))
    except ValueError:
        return default


def env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, ""))
    except ValueError:
        return default


def env_json(name: str, default: Any = None) -> Any:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return default
