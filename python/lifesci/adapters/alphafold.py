"""AlphaFold DB adapter.

Endpoint (live-verified):

``GET https://alphafold.ebi.ac.uk/api/prediction/{acc}`` returns a **list** of
prediction records for the UniProt accession.  The first record carries the
model metadata used here: ``modelEntityId`` (``AF-P69905-F1``),
``latestVersion`` (``6``), ``sequenceStart``/``sequenceEnd`` (the modelled UniProt
interval), and ``pdbUrl``/``cifUrl``/``bcifUrl`` download links.

Quirk: some TrEMBL entries have **no** AlphaFold model and the API answers HTTP
404 with a tiny ``{}`` body.  That is a normal,
non-fatal outcome for this adapter: :func:`prediction` returns
``available: false`` with a warning instead of raising, and the command layer
decides whether to fall back to ESMFold (the adapter stays single-portal).
A 404 is *not* retried.

The AlphaFold DB model version (``latestVersion``) is pinned as the release
(``v6``) and recorded in the cache release registry, because the API has no
``x-...-release`` header.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse

from .. import http, provenance, util
from ..errors import DataError, InvalidInputError, LifesciError, NotFoundError
from . import _common

#: Database label recorded in the cache/provenance/release registry.
DATABASE = "alphafold"

#: AlphaFold DB REST API base.
API_BASE = "https://alphafold.ebi.ac.uk/api"

#: Accepted model file formats for :func:`download_model`.
SUPPORTED_FORMATS: tuple[str, ...] = ("pdb", "cif")

#: Download URL field per format.
_URL_FIELD = {"pdb": "pdb_url", "cif": "cif_url"}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _entry_id(record: Mapping[str, Any], accession: str) -> str | None:
    """Return the AlphaFold entry id (``AF-P69905-F1``)."""
    for key in ("modelEntityId", "entryId"):
        value = record.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    if record.get("uniprotAccession"):
        return f"AF-{accession}-F1"
    return None


def _release_for(record: Mapping[str, Any]) -> str | None:
    """Pin the AlphaFold DB model version as the release string (``v6``)."""
    version = _common.to_int(record.get("latestVersion"))
    return f"v{version}" if version is not None else None


def _record_release(ctx: Any, release: str | None, url: str) -> None:
    """Record the AlphaFold model version in the cache release registry."""
    if not release:
        return
    try:
        provenance.record_release(ctx.cache_dir, database=DATABASE, release=release, source_url=url)
    except OSError:  # pragma: no cover - read-only cache locations must not fail work
        pass


def _unavailable(
    ctx: Any,
    accession: str,
    *,
    url: str,
    reason: str,
    sha256: str | None = None,
    warn_code: str = "ALPHAFOLD_UNAVAILABLE",
) -> dict[str, Any]:
    """Build the explicit ``available: false`` payload and warn about it."""
    ctx.warn(
        warn_code,
        f"AlphaFold DB has no usable model for {accession}: {reason}. This is an explicit "
        "'model unavailable' outcome, not evidence of absence; ESMFold or an experimental "
        "structure may still be available (the command decides the fallback).",
        context={"accession": accession, "url": url},
    )
    return {
        "available": False,
        "accession": accession,
        "entry_id": None,
        "model_version": None,
        "pdb_url": None,
        "cif_url": None,
        "uniprot_start": None,
        "uniprot_end": None,
        "release": None,
        "url": url,
        "sha256": sha256,
        "reason": reason,
        "from_cache": False,
    }


# ---------------------------------------------------------------------------
# prediction metadata
# ---------------------------------------------------------------------------


def prediction(ctx: Any, accession: str) -> dict[str, Any]:
    """Fetch AlphaFold DB prediction metadata for a UniProt accession.

    Args:
        ctx: command context.
        accession: UniProtKB accession (reviewed or TrEMBL).

    Returns:
        ``{"available", "accession", "entry_id", "model_version", "pdb_url",
        "cif_url", "uniprot_start", "uniprot_end", "release", "url", "sha256",
        "from_cache", "reason"?}``.  ``available`` is ``False`` (never an error)
        when AlphaFold DB has no prediction for the accession (HTTP 404); the
        record also carries the model interval and protein/organism metadata when
        present.

    Raises:
        LifesciError: portal failure other than a clean 404, after retries.
    """
    accession = _common.require_identifier(accession, "accession")
    url = f"{API_BASE}/prediction/{accession}"
    try:
        payload, response = http.get_json(
            ctx,
            url,
            allow_status=(200, 404),
            database=DATABASE,
            error_code="ALPHAFOLD",
        )
    except LifesciError as err:
        if _common.is_http_status(err, 404):
            return _unavailable(
                ctx,
                accession,
                url=url,
                reason="not present in AlphaFold DB (HTTP 404)",
                warn_code="ALPHAFOLD_HTTP_404",
            )
        raise
    if response.dry_run:
        return {
            "available": None,
            "accession": accession,
            "url": url,
            "dry_run": True,
        }
    if response.status == 404:
        return _unavailable(
            ctx,
            accession,
            url=url,
            reason="not present in AlphaFold DB (HTTP 404)",
            sha256=_common.sha_of_response(response),
            warn_code="ALPHAFOLD_HTTP_404",
        )
    records: list[Any]
    if isinstance(payload, Mapping):
        records = [payload]
    else:
        records = _common.as_list(payload)
    record = records[0] if records else None
    if not isinstance(record, Mapping) or not record:
        return _unavailable(
            ctx,
            accession,
            url=url,
            reason="AlphaFold DB returned no prediction record",
            sha256=_common.sha_of_response(response),
            warn_code="ALPHAFOLD_EMPTY_PREDICTION",
        )
    if record.get("error") or record.get("detail"):
        return _unavailable(
            ctx,
            accession,
            url=url,
            reason=f"AlphaFold DB returned an error record: {record.get('error') or record.get('detail')}",
            sha256=_common.sha_of_response(response),
            warn_code="ALPHAFOLD_ERROR_RECORD",
        )
    release = _release_for(record)
    _record_release(ctx, release, url)
    model_version = _common.to_int(record.get("latestVersion"))
    return {
        "available": True,
        "accession": str(record.get("uniprotAccession") or accession),
        "entry_id": _entry_id(record, accession),
        "model_version": model_version,
        "pdb_url": record.get("pdbUrl"),
        "cif_url": record.get("cifUrl"),
        "bcif_url": record.get("bcifUrl"),
        "uniprot_start": _common.to_int(
            _common.pick(record, "sequenceStart", "uniprotStart", "uniprot_start")
        ),
        "uniprot_end": _common.to_int(
            _common.pick(record, "sequenceEnd", "uniprotEnd", "uniprot_end")
        ),
        "release": release,
        "url": url,
        "sha256": _common.sha_of_response(response),
        "from_cache": bool(response.from_cache),
        "gene": record.get("gene"),
        "uniprot_description": record.get("uniprotDescription"),
        "organism": record.get("organismScientificName"),
        "taxid": _common.to_int(record.get("taxId")),
        "sequence_checksum": record.get("sequenceChecksum"),
        "plddt_global": _common.to_float(record.get("globalMetricValue")),
        "tool_used": record.get("toolUsed"),
        "reason": None,
    }


# ---------------------------------------------------------------------------
# model download
# ---------------------------------------------------------------------------


def _validate_model_bytes(body: bytes, fmt: str, *, accession: str, url: str) -> None:
    """Reject HTML/error payloads masquerading as structure files."""
    text = body.decode("utf-8", errors="replace")
    stripped = text.lstrip()
    if not stripped:
        raise DataError(
            f"AlphaFold model download for {accession} was empty",
            code="ALPHAFOLD_MODEL_INVALID",
            details={"accession": accession, "url": url, "format": fmt},
        )
    if fmt == "pdb":
        first = stripped.splitlines()[0]
        allowed = ("ATOM", "HETATM", "HEADER", "TITLE", "REMARK", "MODEL", "CRYST1", "COMPND", "SEQRES")
        if not first.startswith(allowed):
            raise DataError(
                f"AlphaFold PDB download for {accession} is not a PDB file",
                code="ALPHAFOLD_MODEL_INVALID",
                details={"accession": accession, "url": url, "preview": util.truncate(stripped, 160)},
            )
    elif not stripped.startswith("data_"):
        raise DataError(
            f"AlphaFold mmCIF download for {accession} is not an mmCIF file",
            code="ALPHAFOLD_MODEL_INVALID",
            details={"accession": accession, "url": url, "preview": util.truncate(stripped, 160)},
        )


def download_model(
    ctx: Any,
    accession: str,
    *,
    out_dir: str | Path,
    fmt: str = "pdb",
) -> dict[str, Any]:
    """Download an AlphaFold DB model file and pin it with a SHA-256.

    Args:
        ctx: command context.
        accession: UniProtKB accession.
        out_dir: directory the model file is written to (created).
        fmt: ``pdb`` (default) or ``cif`` (``mmcif`` accepted as an alias).

    Returns:
        ``{"path", "sha256", "bytes", "available", "reason", "url", "release",
        "model_version", "format", "from_cache"}``.  When AlphaFold DB has no
        model the function returns ``available: False`` with a reason and no
        error (the command decides whether to try ESMFold).

    Raises:
        InvalidInputError: unsupported ``fmt``.
        NotFoundError: the model file advertised by the API is gone (HTTP 404 is
            returned as ``available: False`` instead).
        DataError: the downloaded bytes are not a structure file.
        LifesciError: portal failure after the retry budget.
    """
    accession = _common.require_identifier(accession, "accession")
    normalised_fmt = str(fmt or "pdb").strip().lower()
    if normalised_fmt == "mmcif":
        normalised_fmt = "cif"
    if normalised_fmt not in SUPPORTED_FORMATS:
        raise InvalidInputError(
            f"unsupported AlphaFold model format {fmt!r}",
            details={"format": fmt, "supported": list(SUPPORTED_FORMATS)},
        )
    target_dir = Path(out_dir).expanduser()
    util.ensure_dir(target_dir)

    meta = prediction(ctx, accession)
    if not meta.get("available"):
        return {
            "path": None,
            "sha256": None,
            "bytes": None,
            "available": False,
            "reason": meta.get("reason") or "no AlphaFold prediction available",
            "url": meta.get("url"),
            "release": meta.get("release"),
            "model_version": meta.get("model_version"),
            "format": normalised_fmt,
            "from_cache": False,
        }
    url = meta.get(_URL_FIELD[normalised_fmt])
    if not url:
        return {
            "path": None,
            "sha256": None,
            "bytes": None,
            "available": False,
            "reason": f"AlphaFold DB record has no {normalised_fmt} URL",
            "url": meta.get("url"),
            "release": meta.get("release"),
            "model_version": meta.get("model_version"),
            "format": normalised_fmt,
            "from_cache": False,
        }

    try:
        raw, response = http.get_bytes(
            ctx,
            url,
            allow_status=(200, 404),
            database=DATABASE,
            error_code="ALPHAFOLD",
        )
    except LifesciError as err:
        if _common.is_http_status(err, 404):
            ctx.warn(
                "ALPHAFOLD_MODEL_DOWNLOAD_404",
                f"AlphaFold DB advertised a model URL for {accession} but it returned HTTP 404.",
                context={"accession": accession, "url": url},
            )
            return {
                "path": None,
                "sha256": None,
                "bytes": None,
                "available": False,
                "reason": "advertised model file returned HTTP 404",
                "url": url,
                "release": meta.get("release"),
                "model_version": meta.get("model_version"),
                "format": normalised_fmt,
                "from_cache": False,
            }
        raise
    if response.dry_run:
        return {
            "path": None,
            "sha256": None,
            "bytes": None,
            "available": None,
            "url": url,
            "format": normalised_fmt,
            "dry_run": True,
        }
    if response.status == 404 or not raw:
        raise NotFoundError(
            f"AlphaFold DB model file for {accession} was not found (HTTP {response.status})",
            code="ALPHAFOLD_MODEL_NOT_FOUND",
            details={"accession": accession, "url": url},
            fallbacks=[
                "Retry later; AlphaFold DB file URLs occasionally lag the API metadata.",
                "Request the other format (pdb vs cif) as a cross-check.",
                "Fall back to ESMFold or an experimental structure if a model is essential.",
            ],
        )
    _validate_model_bytes(raw, normalised_fmt, accession=accession, url=url)
    basename = Path(urlparse(str(url)).path).name or f"AF-{accession}-F1.{normalised_fmt}"
    if not basename.lower().endswith(f".{normalised_fmt}"):
        basename = f"{basename}.{normalised_fmt}"
    out_path = target_dir / basename
    util.atomic_write_bytes(out_path, raw)
    sha256 = util.sha256_bytes(raw)
    _common.log_local_file(
        ctx,
        out_path,
        kind="afdb_model",
        notes={"accession": accession, "url": url, "format": normalised_fmt},
    )
    return {
        "path": str(out_path),
        "sha256": sha256,
        "bytes": len(raw),
        "available": True,
        "reason": None,
        "url": url,
        "release": meta.get("release"),
        "model_version": meta.get("model_version"),
        "format": normalised_fmt,
        "from_cache": bool(response.from_cache),
    }


__all__ = [
    "API_BASE",
    "DATABASE",
    "SUPPORTED_FORMATS",
    "download_model",
    "prediction",
]
