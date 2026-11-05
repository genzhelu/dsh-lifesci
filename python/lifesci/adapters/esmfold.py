"""ESMFold / ESM Atlas adapter.

Endpoint::

    POST https://api.esmatlas.com/foldSequence/v1/pdb/
    Content-Type: text/plain
    body = raw amino-acid sequence

returns PDB text for small proteins.  It is a **single-sequence** prediction:
lower confidence than AlphaFold DB and not an experimental structure, so callers
must label it as such.

Documented quirks (all handled here as ``available: false`` outcomes, never a
crash):

* **HTTP 413** for long sequences (observed failures at 1640 and 1775 aa) — the
  service rejects them outright (not retried);
* **HTTP 504** when the service is busy — retried once (``retries=1``,
  ``idempotent=True``) and then reported unavailable;
* during live probing in this development session the service answered **504 for
  even a 40-aa sequence**, i.e. it can be entirely down.  The adapter therefore
  treats any exhausted portal failure as an explicit "unavailable" verdict; it
  never fabricates a model and never crashes the structure-fetch fallback chain.
"""

from __future__ import annotations

import re
from typing import Any

from .. import http, util
from ..errors import InvalidInputError, LifesciError
from . import _common

#: Database/service label recorded in the cache/provenance registry.
DATABASE = "esmatlas"

#: ESMFold folding endpoint.
FOLD_URL = "https://api.esmatlas.com/foldSequence/v1/pdb/"

#: Above this length ESMFold becomes unreliable / tends to return HTTP 413.
DEFAULT_MAX_LENGTH_WARN = 1400

#: One-letter amino-acid alphabet accepted before upload (includes ambiguous codes).
_AA_RE = re.compile(r"^[ACDEFGHIKLMNPQRSTVWYBXZUOJ*]+$", re.IGNORECASE)


# ---------------------------------------------------------------------------
# sequence validation
# ---------------------------------------------------------------------------


def _clean_sequence(sequence: Any) -> str:
    """Strip whitespace and validate the one-letter amino-acid alphabet."""
    if not isinstance(sequence, str):
        raise InvalidInputError(
            f"sequence must be a string, got {type(sequence).__name__}",
            details={"type": type(sequence).__name__},
        )
    cleaned = "".join(sequence.split()).upper()
    if not cleaned:
        raise InvalidInputError("sequence must contain at least one residue")
    if not _AA_RE.match(cleaned):
        bad = sorted({char for char in cleaned if not re.match(r"[ACDEFGHIKLMNPQRSTVWYBXZUOJ*]", char)})
        raise InvalidInputError(
            "sequence contains characters that are not one-letter amino-acid codes",
            details={"invalid_characters": bad[:20], "length": len(cleaned)},
        )
    return cleaned


# ---------------------------------------------------------------------------
# folding
# ---------------------------------------------------------------------------


def fold(ctx: Any, sequence: str, *, max_length_warn: int = DEFAULT_MAX_LENGTH_WARN) -> dict[str, Any]:
    """Fold one sequence with ESMFold and return the PDB text.

    Args:
        ctx: command context (cache/retry/offline/provenance).
        sequence: raw one-letter amino-acid sequence (whitespace is ignored).
        max_length_warn: length above which a long-sequence warning is recorded
            (ESMFold is unreliable above ~1400 aa and usually answers HTTP 413).

    Returns:
        ``{"available", "pdb", "status", "reason", "length", "url", "sha256",
        "retries", "release", "from_cache"}``.  On the documented 413/504 (and any
        other exhausted portal failure) the result is ``available: False`` with a
        human-readable ``reason`` and a warning — this function does not raise for
        service unavailability, by design, so the caller can report an explicit
        "no model available" verdict.

    Raises:
        InvalidInputError: the sequence is empty or contains non-amino-acid
            characters.

    Warning codes:
        ``ESMFOLD_LONG_SEQUENCE`` (potential 413),
        ``ESMFOLD_SEQUENCE_TOO_LONG`` (HTTP 413),
        ``ESMFOLD_UNAVAILABLE`` (HTTP 504/timeout/other service failure),
        ``ESMFOLD_INVALID_PAYLOAD`` (the service returned something that is not
        PDB text).
    """
    cleaned = _clean_sequence(sequence)
    length = len(cleaned)
    if length > int(max_length_warn):
        ctx.warn(
            "ESMFOLD_LONG_SEQUENCE",
            f"Sequence length {length} aa exceeds the reliable ESMFold range (>~{max_length_warn} aa); "
            "the service frequently rejects long sequences with HTTP 413. The result may be unavailable.",
            context={"length": length, "max_length_warn": int(max_length_warn)},
        )
    try:
        response = http.request(
            ctx,
            "POST",
            FOLD_URL,
            body=cleaned.encode("ascii"),
            headers={"content-type": "text/plain", "accept": "text/plain"},
            database=DATABASE,
            error_code="ESMFOLD",
            idempotent=True,  # folding is a pure function of the sequence
            retries=1,  # retry a busy service (504) exactly once
        )
    except LifesciError as err:
        status = _common.http_status(err)
        attempts = int((getattr(err, "details", None) or {}).get("attempts") or 1)
        if status == 413:
            ctx.warn(
                "ESMFOLD_SEQUENCE_TOO_LONG",
                f"ESMFold rejected the {length} aa sequence with HTTP 413 (too long). "
                "Report 'ESMFold unavailable for this length'; do not treat it as sequence absence.",
                context={"length": length, "url": FOLD_URL},
            )
            reason = f"sequence too long for ESMFold (HTTP 413, {length} aa)"
        else:
            detail = "timed out (HTTP 504)" if status == 504 else f"failed ({err.code})"
            ctx.warn(
                "ESMFOLD_UNAVAILABLE",
                f"ESMFold service {detail} after {attempts} attempt(s); no model was produced. "
                "This is a service-availability outcome, not evidence about the protein.",
                context={
                    "length": length,
                    "url": FOLD_URL,
                    "status": status,
                    "attempts": attempts,
                    "error": err.message[:300],
                },
            )
            reason = f"ESMFold service unavailable ({err.code}" + (f", HTTP {status}" if status else "") + ")"
        return {
            "available": False,
            "pdb": None,
            "status": "unavailable",
            "reason": reason,
            "length": length,
            "url": FOLD_URL,
            "sha256": None,
            "retries": max(0, attempts - 1),
            "release": None,
            "from_cache": False,
        }

    if response.dry_run:
        return {
            "available": None,
            "pdb": None,
            "status": "dry_run",
            "reason": "dry-run: request was not sent",
            "length": length,
            "url": FOLD_URL,
            "sha256": None,
            "retries": 0,
            "release": None,
            "from_cache": False,
            "dry_run": True,
        }

    text = response.text()
    stripped = text.strip()
    valid = "ATOM" in stripped and ("END" in stripped or "TER" in stripped)
    if not valid:
        preview = util.truncate(" ".join(stripped.split()), 200)
        ctx.warn(
            "ESMFOLD_INVALID_PAYLOAD",
            "ESMFold returned a payload that is not PDB text; no model is reported.",
            context={"length": length, "url": FOLD_URL, "body_preview": preview},
        )
        return {
            "available": False,
            "pdb": None,
            "status": "invalid_payload",
            "reason": "ESMFold returned a non-PDB payload",
            "length": length,
            "url": FOLD_URL,
            "sha256": _common.sha_of_response(response),
            "retries": max(0, int(response.attempts) - 1),
            "release": None,
            "from_cache": bool(response.from_cache),
        }
    return {
        "available": True,
        "pdb": text,
        "status": "ok",
        "reason": None,
        "length": length,
        "url": FOLD_URL,
        "sha256": util.sha256_text(text),
        "retries": max(0, int(response.attempts) - 1),
        "release": None,
        "from_cache": bool(response.from_cache),
    }


__all__ = ["DATABASE", "DEFAULT_MAX_LENGTH_WARN", "FOLD_URL", "fold"]
