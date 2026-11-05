"""Small shared helpers for the portal adapters.

This module intentionally contains no portal-specific logic: it only holds the
tiny utilities every adapter needs (safe value extraction, gzip/FASTA payload
decoding, stable error translation, provenance helpers) so that behaviour stays
consistent across uniprot/interpro/ncbi/foldseek/pdb/local_dataset/...

Design rules:

* helpers are pure unless they take ``ctx``; the provenance helpers append to
  ``ctx.provenance`` using the same lazy log object as :mod:`lifesci.http`;
* every helper that can fail raises a :class:`lifesci.errors.LifesciError`
  subclass with a stable ``code`` — adapters must never leak ``KeyError``,
  ``ValueError`` or ``UnicodeDecodeError`` to a command;
* no module-level mutable state and no I/O at import time.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .. import provenance, util
from ..errors import DataError, InvalidInputError, LifesciError, NotFoundError, PortalError

#: Identifier syntax accepted for accessions/ORF names/OG ids before URL
#: construction: no whitespace, no path separators, no query characters.
_SAFE_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:+-]{0,79}$")

#: HTTP statuses that mean "the record does not exist" on the studied portals.
NOT_FOUND_STATUSES: frozenset[int] = frozenset({404, 410})


# ---------------------------------------------------------------------------
# provenance helpers
# ---------------------------------------------------------------------------


def provenance_log(ctx: Any) -> provenance.ProvenanceLog:
    """Return (creating lazily) the context's provenance log object."""
    log = getattr(ctx, "_provenance_log", None)
    if log is None:
        log = provenance.ProvenanceLog(getattr(ctx, "log_path", None))
        ctx._provenance_log = log
    return log


def log_local_file(
    ctx: Any,
    path: str | Path,
    *,
    dataset: str | None = None,
    kind: str = "local_file",
    notes: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Record a local-file read (sha256 + byte count) in the envelope provenance.

    Returns the provenance entry, or ``None`` when the path is not a regular
    file (callers that require the file validate it separately).
    """
    target = Path(path)
    if not target.is_file():
        return None
    entry = provenance_log(ctx).local_file(
        path=target,
        sha256=util.sha256_file(target),
        bytes_=target.stat().st_size,
        dataset=dataset,
        kind=kind,
        notes=notes,
    )
    ctx.provenance.append(entry)
    return entry


def log_tool(
    ctx: Any,
    name: str,
    *,
    version: str | None,
    argv: Sequence[str] | None = None,
    exit_code: int | None = None,
    elapsed_ms: float | None = None,
    notes: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Record an external tool run (foldseek, ssh, …) in the envelope provenance."""
    entry = provenance_log(ctx).tool(
        name=name,
        version=version,
        argv=list(argv) if argv is not None else None,
        exit_code=exit_code,
        elapsed_ms=elapsed_ms,
        notes=notes,
    )
    ctx.provenance.append(entry)
    return entry


# ---------------------------------------------------------------------------
# response helpers
# ---------------------------------------------------------------------------


def sha_of_response(response: Any) -> str:
    """SHA-256 of the decoded response body (the bytes stored in the cache)."""
    return util.sha256_bytes(response.body)


def decode_payload(data: bytes, *, label: str) -> bytes:
    """Transparently gunzip ``data`` when it carries gzip magic bytes.

    Portal quirks covered: UniProt FTP ``.fasta.gz`` downloads and InterPro
    ``?annotation=hmm`` payloads arrive as raw gzip bodies without a
    ``Content-Encoding: gzip`` header, so :mod:`lifesci.http` does not decompress
    them.  A payload that claims gzip in its content-type but has no gzip magic is
    returned unchanged (some mirrors/recordings serve plain text); no error is
    raised for that tolerant case.
    """
    if util.is_gzip_bytes(data):
        try:
            return util.gzip_decompress(data)
        except DataError as err:
            raise DataError(
                f"{label} advertised gzip but could not be decompressed",
                details={"label": label, "bytes": len(data), "error": err.message},
            ) from err
    return data


def parse_fasta_payload(data: bytes, *, label: str) -> list[tuple[str, str]]:
    """Parse a FASTA payload (plain or gzipped) into ``[(header, sequence)]``."""
    text = decode_payload(data, label=label).decode("utf-8", errors="replace")
    try:
        return util.parse_fasta_text(text)
    except DataError as err:
        preview = util.truncate(" ".join(text.split()), 200)
        raise DataError(
            f"{label} did not contain a parseable FASTA record",
            details={"label": label, "bytes": len(data), "body_preview": preview},
        ) from err


def count_fasta_records(data: bytes, *, label: str = "FASTA") -> int:
    """Count ``>``-prefixed record headers in a (possibly gzipped) FASTA payload.

    Used for large proteome downloads where building all ``(header, sequence)``
    tuples is unnecessary; the count is validated against
    :func:`parse_fasta_payload` only when the payload is small.
    """
    text = decode_payload(data, label=label).decode("utf-8", errors="replace")
    count = 0
    for line in text.splitlines():
        if line.startswith(">"):
            count += 1
        elif line.strip() and count == 0:
            raise DataError(
                f"{label} is not FASTA: content appears before the first '>' header",
                details={"label": label, "body_preview": util.truncate(line, 160)},
            )
    return count


def first_fasta_record(data: bytes, *, label: str) -> tuple[str, str]:
    """Return the first ``(header, sequence)`` from a FASTA payload."""
    records = parse_fasta_payload(data, label=label)
    if not records:
        raise DataError(f"{label} contained no FASTA records", details={"label": label})
    return records[0]


def unwrap_fasta(data: bytes, *, label: str) -> tuple[str, list[tuple[str, str]]]:
    """Return the decoded FASTA text and its records for a (possibly gzipped) payload."""
    text = decode_payload(data, label=label).decode("utf-8", errors="replace")
    try:
        records = util.parse_fasta_text(text)
    except DataError as err:
        raise DataError(
            f"{label} did not contain a parseable FASTA record",
            details={"label": label, "body_preview": util.truncate(" ".join(text.split()), 200)},
        ) from err
    return text, records


# ---------------------------------------------------------------------------
# value extraction
# ---------------------------------------------------------------------------


def pick(mapping: Any, *keys: str, default: Any = None) -> Any:
    """Return the first present, non-``None`` value for ``keys`` in ``mapping``.

    Keys may be dotted (``"metadata.accession"``); a missing intermediate or a
    non-mapping node yields ``default`` rather than raising.
    """
    if not isinstance(mapping, Mapping):
        return default
    for key in keys:
        node: Any = mapping
        found = True
        for part in key.split("."):
            if not isinstance(node, Mapping) or part not in node:
                found = False
                break
            node = node[part]
        if found and node is not None:
            return node
    return default


def as_list(value: Any) -> list[Any]:
    """Coerce ``None``→``[]``, mappings→``[mapping]``, sequences→``list``."""
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, Mapping):
        return [value]
    return [value]


def as_dict(value: Any) -> dict[str, Any]:
    """Return a shallow dict copy, or ``{}`` for non-mappings."""
    return dict(value) if isinstance(value, Mapping) else {}


def to_int(value: Any) -> int | None:
    """Parse an int-ish value, returning ``None`` when that is impossible."""
    return util.parse_int(value)


def to_float(value: Any) -> float | None:
    """Parse a float-ish value, returning ``None`` when that is impossible."""
    return util.parse_float(value)


def first_text(value: Any) -> str | None:
    """Extract the first ``.value``/``.text``/string from a portal value object.

    Numeric identifiers (e.g. SGD's ``reference.pubmed_id`` integer) are
    rendered as text so callers do not silently lose ids; booleans are not
    identifiers and are ignored.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return str(int(value)) if isinstance(value, int) else str(value)
    if isinstance(value, str):
        return value.strip() or None
    for item in as_list(value):
        if isinstance(item, Mapping):
            for key in ("value", "text", "name", "id"):
                candidate = item.get(key)
                if isinstance(candidate, str) and candidate.strip():
                    return candidate.strip()
        elif isinstance(item, str) and item.strip():
            return item.strip()
    return None


#: Keys under which portalled record lists commonly live (used by :func:`data_list`).
_RECORD_LIST_KEYS: tuple[str, ...] = (
    "results",
    "result",
    "data",
    "items",
    "records",
    "interactions",
    "jobs",
    "alignments",
    "taxonomy",
)


def data_list(payload: Any, *keys: str) -> list[Any]:
    """Normalise a portal payload whose records live under a known key.

    Accepts a bare list, the ``{"data": ...}`` PostgREST envelope used by
    OrthoDB, and one level of wrapper nesting (e.g. Europe PMC's
    ``{"resultList": {"result": [...]}}`` or SGD's ``{"interactions": {...}}``).
    A mapping that is itself a single record is returned as ``[mapping]``.

    The explicit ``keys`` are tried first, then ::data:`_RECORD_LIST_KEYS`, so a
    caller can pass the envelope name (``"resultList"``) and still get the inner
    record list.
    """
    if payload is None:
        return []
    if isinstance(payload, (list, tuple)):
        return list(payload)
    if not isinstance(payload, Mapping):
        return []
    candidates = tuple(dict.fromkeys((*keys, *_RECORD_LIST_KEYS)))
    for key in candidates:
        if key not in payload:
            continue
        value = payload[key]
        if isinstance(value, (list, tuple)):
            return list(value)
        if isinstance(value, Mapping):
            # Unwrap one envelope level: {"resultList": {"result": [...]}}.
            for inner_key in candidates:
                if inner_key not in value:
                    continue
                inner = value[inner_key]
                if isinstance(inner, (list, tuple)):
                    return list(inner)
                if isinstance(inner, Mapping):
                    return [dict(inner)]
            return [dict(value)]
        return [value]
    return []


# ---------------------------------------------------------------------------
# validation
# ---------------------------------------------------------------------------


def require_identifier(value: Any, label: str, *, max_length: int = 80) -> str:
    """Validate a portal identifier used in a URL path and return it as text.

    Raises:
        InvalidInputError: the identifier is empty or contains characters that
            are unsafe in a URL path (whitespace, ``/``, ``?``, ``#``, quotes).
    """
    text = str(value or "").strip()
    if not text:
        raise InvalidInputError(f"{label} must be a non-empty string")
    if len(text) > max_length or not _SAFE_IDENTIFIER_RE.match(text):
        raise InvalidInputError(
            f"{label} {text!r} is not a valid identifier (allowed: letters, digits and "
            ". _ : + - up to 80 characters)",
            details={"label": label, "value": text},
        )
    return text


def require_choice(value: Any, allowed: Sequence[str], label: str) -> str:
    """Validate an enumerated option value; raises :class:`InvalidInputError`."""
    text = str(value or "").strip().lower()
    allowed_text = [str(item).lower() for item in allowed]
    if text not in allowed_text:
        raise InvalidInputError(
            f"unsupported {label} {value!r}",
            details={"label": label, "value": value, "allowed": list(allowed)},
        )
    return allowed_text[allowed_text.index(text)]


# ---------------------------------------------------------------------------
# error translation
# ---------------------------------------------------------------------------


def http_status(err: LifesciError) -> int | None:
    """Extract the HTTP status an adapter error was derived from, if any."""
    details = getattr(err, "details", None) or {}
    status = details.get("status")
    if isinstance(status, int):
        return status
    code = str(getattr(err, "code", "") or "")
    match = re.search(r"_(\d{3})$", code)
    return int(match.group(1)) if match else None


def is_http_status(err: LifesciError, *statuses: int) -> bool:
    """True when ``err`` was raised because the portal returned one of ``statuses``."""
    return http_status(err) in statuses


def is_transient(err: LifesciError) -> bool:
    """True for retriable portal-layer errors (rate limits, 5xx, transport)."""
    status = http_status(err)
    return bool(getattr(err, "retriable", False)) or status in {429, 500, 502, 503, 504}


def not_found(
    message: str,
    *,
    code: str,
    details: Mapping[str, Any] | None = None,
    fallbacks: Sequence[str] | None = None,
) -> NotFoundError:
    """Build a :class:`NotFoundError` with adapter-appropriate metadata."""
    return NotFoundError(message, code=code, details=dict(details or {}), fallbacks=list(fallbacks or []))


def with_fallbacks(err: LifesciError, fallbacks: Sequence[str], *, message: str | None = None) -> PortalError:
    """Re-raise an exhausted portal error with explicit alternate-source advice.

    Preserves the original code (``NCBI_500`` …), ``retriable`` flag and details
    so callers can still branch programmatically; only ``fallbacks`` (and
    optionally the message prefix) are added.
    """
    text = err.message if message is None else f"{message} ({err.message})"
    return PortalError(
        text,
        code=err.code,
        retriable=bool(getattr(err, "retriable", True)),
        details=dict(getattr(err, "details", None) or {}),
        fallbacks=list(fallbacks),
    )


def ensure_mapping(value: Any, *, label: str) -> dict[str, Any]:
    """Return ``value`` as a dict, raising :class:`DataError` otherwise."""
    if isinstance(value, Mapping):
        return dict(value)
    raise DataError(
        f"{label} response was not a JSON object",
        details={"label": label, "type": type(value).__name__},
    )


def dedupe(items: Iterable[str]) -> list[str]:
    """Order-preserving de-duplication of strings (stable, deterministic)."""
    return util.unique_preserving_order([item for item in items if item])
