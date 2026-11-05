"""InterPro / Pfam adapter.

Endpoints (live-verified against InterPro 111.0 / Pfam 39.0):

* ``GET https://www.ebi.ac.uk/interpro/api/entry/all/protein/uniprot/{acc}/?page_size=50``
  — every domain/family/repeat annotation for one protein, paginated through the
  JSON ``next`` URL.  Each result carries ``metadata`` (accession, name, type,
  source database, member databases) and ``proteins[*].entry_protein_locations``
  with ``fragments`` (residue ranges), ``model`` and the match ``score``.
* ``GET https://www.ebi.ac.uk/interpro/api/entry/{database}/{entry_id}/`` — the
  family/domain definition (name, type, description, GO terms, member DBs).
* ``GET https://www.ebi.ac.uk/interpro/wwwapi/entry/{database}/{entry_id}/?annotation=hmm``
  — the gzipped HMMER3 model (``HMMER3/f`` text inside a gzip body, not a tar).

Release handling: entry payloads do **not** carry a release header and the
cache may store ``release: null`` for them, so adapters return
``release: None`` plus an ``INTERPRO_RELEASE_UNKNOWN`` warning rather than
inventing a version.  :func:`release_info` performs the one extra call needed to
pin InterPro/Pfam/PANTHER/UniProt versions when a caller explicitly needs them.

Score-vs-E-value quirk: the InterPro API exposes a single generic ``score`` field
per match.  For Pfam/PANTHER/PRINTS this value **is** the match E-value, so the
adapter reports it as both
``evalue`` (interpreted) and ``score`` (raw), which is exactly what the
``domains`` command disambiguates with ``_looks_like_evalue``.  The raw value is
never silently discarded.
"""

from __future__ import annotations

import io
import re
import tarfile
from pathlib import Path
from typing import Any, Mapping, Sequence

from .. import http, provenance, util
from ..errors import DataError, InvalidInputError, LifesciError, NotFoundError
from . import _common

#: Database label recorded in the cache/provenance/release registry.
DATABASE = "interpro"

#: InterPro REST API base (JSON).
API_BASE = "https://www.ebi.ac.uk/interpro/api"

#: InterPro ``wwwapi`` base (raw annotations, e.g. HMM models).
WWWAPI_BASE = "https://www.ebi.ac.uk/interpro/wwwapi"

#: Cache sub-directory (under ``ctx.cache_dir``) for downloaded HMM models.
MODELS_SUBDIR = "models"

#: Databases whose generic InterPro ``score`` field is a match E-value.
EVALUE_SCORE_DATABASES = frozenset({"pfam", "panther", "prints", "prosite", "smart", "cdd"})

_TAG_RE = re.compile(r"<[^>]+>")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _plain_text(value: Any) -> str | None:
    """Extract display text from InterPro metadata values (names, descriptions)."""
    if value is None:
        return None
    if isinstance(value, str):
        text = _TAG_RE.sub(" ", value)
        return " ".join(text.split()) or None
    if isinstance(value, Mapping):
        for key in ("name", "text", "value"):
            text = _plain_text(value.get(key))
            if text:
                return text
        return None
    if isinstance(value, (list, tuple)):
        parts = [_plain_text(item) for item in value]
        joined = " ".join(part for part in parts if part)
        return joined or None
    return str(value)


def _entry_name(metadata: Mapping[str, Any]) -> str | None:
    """InterPro ``metadata.name`` is a string for integrated entries but a
    ``{"name", "short"}`` object for member-database entries (e.g. Pfam)."""
    return _plain_text(metadata.get("name"))


def _warn_release_unknown(ctx: Any, *, url: str, kind: str = "entry") -> None:
    """Warn that InterPro payloads cannot pin their own release."""
    ctx.warn(
        "INTERPRO_RELEASE_UNKNOWN",
        f"InterPro {kind} payloads do not carry a database release; treat annotations as "
        "release-dependent and pin it explicitly with interpro.release_info(...) when needed.",
        severity="info",
        context={"url": url, "pin_with": f"{API_BASE}/"},
    )


def _normalise_locations(protein: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Flatten ``entry_protein_locations`` into normalised location records."""
    locations: list[dict[str, Any]] = []
    for location in _common.as_list(protein.get("entry_protein_locations")):
        if not isinstance(location, Mapping):
            continue
        fragments = [
            {
                "start": _common.to_int(fragment.get("start")),
                "end": _common.to_int(fragment.get("end")),
                "dc_status": fragment.get("dc-status"),
            }
            for fragment in _common.as_list(location.get("fragments"))
            if isinstance(fragment, Mapping)
        ]
        raw_score = _common.to_float(location.get("score"))
        if fragments:
            for fragment in fragments:
                locations.append(
                    {
                        "start": fragment["start"],
                        "end": fragment["end"],
                        "evalue": raw_score,
                        "score": raw_score,
                        "model": location.get("model"),
                        "representative": location.get("representative"),
                        "fragments": [dict(fragment)],
                    }
                )
        elif raw_score is not None:
            locations.append(
                {
                    "start": None,
                    "end": None,
                    "evalue": raw_score,
                    "score": raw_score,
                    "model": location.get("model"),
                    "representative": location.get("representative"),
                    "fragments": [],
                }
            )
    return locations


def _normalise_entry(result: Mapping[str, Any]) -> dict[str, Any] | None:
    """Convert one ``results[]`` item into the contract entry record."""
    metadata = result.get("metadata")
    if not isinstance(metadata, Mapping):
        return None
    source_database = str(metadata.get("source_database") or "").lower()
    locations: list[dict[str, Any]] = []
    protein_length: int | None = None
    for protein in _common.as_list(result.get("proteins")):
        if not isinstance(protein, Mapping):
            continue
        if protein_length is None:
            protein_length = _common.to_int(protein.get("protein_length"))
        locations.extend(_normalise_locations(protein))
    return {
        "source_database": source_database or None,
        "entry_accession": metadata.get("accession"),
        "entry_name": _entry_name(metadata),
        "entry_type": metadata.get("type"),
        "integrated": metadata.get("integrated"),
        "member_databases": metadata.get("member_databases"),
        "go_terms": metadata.get("go_terms"),
        "protein_length": protein_length,
        "locations": locations,
    }


def _raise_not_found(err: LifesciError, *, what: str, identifier: str, url: str) -> None:
    """Translate an InterPro 404 into an actionable :class:`NotFoundError`."""
    raise NotFoundError(
        f"InterPro {what} {identifier!r} was not found (HTTP 404)",
        code=f"INTERPRO_{what.upper()}_NOT_FOUND",
        details={"identifier": identifier, "url": url, "portal_error": getattr(err, "code", None)},
        fallbacks=[
            "Check the identifier against the InterPro website (entry accessions like "
            "IPR000001/PF00042, UniProt accessions like P69905).",
            "The entry may be obsolete or not integrated in the current InterPro release.",
            "Retry later; InterPro occasionally returns transient errors for valid records.",
        ],
    )


# ---------------------------------------------------------------------------
# per-protein domain annotation
# ---------------------------------------------------------------------------


def entry_all_for_protein(
    ctx: Any,
    accession: str,
    *,
    page_size: int = 50,
    max_pages: int = 5,
) -> dict[str, Any]:
    """Fetch every InterPro/member-database annotation for one protein.

    Args:
        ctx: command context.
        accession: UniProt accession (``P69905``); the API also accepts entry
            names but accessions are what the commands use.
        page_size: results per page (the API caps this at 200; default 50).
        max_pages: pagination safety cap; truncation is warned and flagged.

    Returns:
        ``{"accession", "entries", "n_entries", "n_total", "release", "url",
        "sha256", "pages", "truncated", "from_cache"}`` where each entry is
        ``{"source_database", "entry_accession", "entry_name", "entry_type",
        "integrated", "member_databases", "go_terms", "locations": [
        {"start", "end", "evalue", "score", "model", "representative",
        "fragments"}]}``.  ``release`` is ``None`` with an
        ``INTERPRO_RELEASE_UNKNOWN`` warning unless pinned via
        :func:`release_info` (recorded payloads have no release).

    Raises:
        NotFoundError: the protein is unknown to InterPro (HTTP 404).
        DataError: the payload is not the expected JSON shape.
        LifesciError: portal failure after the retry budget.
    """
    accession = _common.require_identifier(accession, "accession")
    page_size = max(1, min(200, int(page_size)))
    max_pages = max(1, int(max_pages))
    url = f"{API_BASE}/entry/all/protein/uniprot/{accession}/"
    entries: list[dict[str, Any]] = []
    page_sha: list[str] = []
    pages = 0
    n_total: int | None = None
    next_url: str | None = None
    from_cache = False
    while pages < max_pages:
        try:
            if pages == 0:
                payload, response = http.get_json(
                    ctx,
                    url,
                    params={"page_size": page_size},
                    database=DATABASE,
                    error_code="INTERPRO",
                )
            else:
                payload, response = http.get_json(
                    ctx,
                    str(next_url),
                    database=DATABASE,
                    error_code="INTERPRO",
                )
        except LifesciError as err:
            if _common.is_http_status(err, 404):
                _raise_not_found(err, what="protein", identifier=accession, url=url)
            raise
        if response.dry_run:
            return {
                "accession": accession,
                "entries": [],
                "n_entries": 0,
                "n_total": None,
                "release": None,
                "url": url,
                "sha256": None,
                "pages": 0,
                "truncated": False,
                "from_cache": False,
                "dry_run": True,
            }
        pages += 1
        page_sha.append(_common.sha_of_response(response))
        from_cache = bool(response.from_cache)
        payload = _common.ensure_mapping(payload, label=f"InterPro entry/all {accession}")
        if n_total is None:
            n_total = _common.to_int(payload.get("count"))
        for result in _common.as_list(payload.get("results")):
            if isinstance(result, Mapping):
                entry = _normalise_entry(result)
                if entry is not None:
                    entries.append(entry)
        next_url = payload.get("next")
        if not next_url:
            break
        if n_total is not None and len(entries) >= n_total:
            break

    truncated = bool(next_url) and (n_total is None or len(entries) < (n_total or 0))
    if truncated:
        ctx.warn_top_hit_cap("interpro", n_total)
    _warn_release_unknown(ctx, url=url)
    return {
        "accession": accession,
        "entries": entries,
        "n_entries": len(entries),
        "n_total": n_total,
        "release": None,
        "url": url,
        "sha256": util.sha256_bytes("".join(page_sha).encode("utf-8")) if page_sha else None,
        "pages": pages,
        "truncated": truncated,
        "from_cache": from_cache,
    }


# ---------------------------------------------------------------------------
# entry metadata
# ---------------------------------------------------------------------------


def entry_meta(ctx: Any, database: str, entry_id: str) -> dict[str, Any]:
    """Fetch the family/domain definition for one InterPro or member-DB entry.

    Args:
        ctx: command context.
        database: InterPro source database (``interpro``, ``pfam``, ``panther``…).
        entry_id: entry accession (``IPR000001``, ``PF00042``, ``PTHR10000``).

    Returns:
        ``{"accession", "name", "type", "description", "description_html",
        "source_database", "integrated", "go_terms", "member_databases",
        "release", "url", "sha256", "from_cache"}``.  ``description`` is a
        whitespace-normalised plain-text rendering of the HTML description.

    Raises:
        NotFoundError: the entry does not exist (HTTP 404).
        LifesciError: portal failure after the retry budget.
    """
    database = _common.require_identifier(str(database or "").lower(), "database")
    entry_id = _common.require_identifier(entry_id, "entry_id")
    url = f"{API_BASE}/entry/{database}/{entry_id}/"
    try:
        payload, response = http.get_json(
            ctx, url, database=DATABASE, error_code="INTERPRO"
        )
    except LifesciError as err:
        if _common.is_http_status(err, 404):
            _raise_not_found(err, what="entry", identifier=f"{database}/{entry_id}", url=url)
        raise
    if response.dry_run:
        return {
            "accession": entry_id,
            "name": None,
            "type": None,
            "description": None,
            "go_terms": None,
            "release": None,
            "url": url,
            "sha256": None,
            "dry_run": True,
        }
    payload = _common.ensure_mapping(payload, label=f"InterPro entry {database}/{entry_id}")
    metadata = payload.get("metadata")
    metadata = metadata if isinstance(metadata, Mapping) else payload
    raw_description = _common.as_list(metadata.get("description"))
    description_text = " ".join(
        _plain_text(item.get("text") if isinstance(item, Mapping) else item) or ""
        for item in raw_description
    ).strip()
    go_terms = [
        {
            "identifier": term.get("identifier"),
            "name": term.get("name"),
            "category": term.get("category"),
        }
        for term in _common.as_list(metadata.get("go_terms"))
        if isinstance(term, Mapping)
    ]
    _warn_release_unknown(ctx, url=url, kind="entry metadata")
    return {
        "accession": metadata.get("accession") or entry_id,
        "name": _entry_name(metadata),
        "type": metadata.get("type"),
        "description": description_text or None,
        "description_html": [
            item.get("text") if isinstance(item, Mapping) else str(item)
            for item in raw_description
        ],
        "source_database": str(metadata.get("source_database") or database).lower(),
        "integrated": metadata.get("integrated"),
        "go_terms": go_terms,
        "member_databases": metadata.get("member_databases"),
        "counters": metadata.get("counters"),
        "release": None,
        "url": url,
        "sha256": _common.sha_of_response(response),
        "from_cache": bool(response.from_cache),
    }


def pfam_proteins(
    ctx: Any,
    pfam_id: str,
    accession: str,
    *,
    page_size: int = 50,
) -> dict[str, Any]:
    """Fetch the Pfam matches of one Pfam entry against one protein.

    This is the narrow form of :func:`entry_all_for_protein` and is convenient
    when only one model's residue ranges/E-value are needed (e.g. PF00042).

    Args:
        ctx: command context.
        pfam_id: Pfam accession (``PF00042``).
        accession: UniProt accession to annotate.
        page_size: matches per page.

    Returns:
        ``{"pfam_id", "accession", "entry_name", "entry_type", "locations",
        "n_matches", "release", "url", "sha256"}``; ``locations`` uses the same
        shape as :func:`entry_all_for_protein`.

    Raises:
        NotFoundError: entry/protein unknown (HTTP 404).
        LifesciError: portal failure after the retry budget.
    """
    pfam_id = _common.require_identifier(pfam_id, "pfam_id")
    accession = _common.require_identifier(accession, "accession")
    url = f"{API_BASE}/entry/pfam/{pfam_id}/protein/uniprot/{accession}/"
    try:
        payload, response = http.get_json(
            ctx,
            url,
            params={"page_size": max(1, min(200, int(page_size)))},
            database=DATABASE,
            error_code="INTERPRO",
        )
    except LifesciError as err:
        if _common.is_http_status(err, 404):
            _raise_not_found(err, what="pfam match", identifier=f"{pfam_id}/{accession}", url=url)
        raise
    if response.dry_run:
        return {
            "pfam_id": pfam_id,
            "accession": accession,
            "locations": [],
            "n_matches": 0,
            "release": None,
            "url": url,
            "dry_run": True,
        }
    payload = _common.ensure_mapping(payload, label=f"InterPro Pfam {pfam_id}/{accession}")
    metadata = payload.get("metadata")
    metadata = metadata if isinstance(metadata, Mapping) else {}
    locations: list[dict[str, Any]] = []
    for protein in _common.as_list(payload.get("proteins")):
        if isinstance(protein, Mapping):
            locations.extend(_normalise_locations(protein))
    _warn_release_unknown(ctx, url=url, kind="Pfam-match")
    return {
        "pfam_id": metadata.get("accession") or pfam_id,
        "accession": accession,
        "entry_name": _entry_name(metadata),
        "entry_type": metadata.get("type"),
        "locations": locations,
        "n_matches": len(locations),
        "release": None,
        "url": url,
        "sha256": _common.sha_of_response(response),
        "from_cache": bool(response.from_cache),
    }


# ---------------------------------------------------------------------------
# HMM model download
# ---------------------------------------------------------------------------


def _extract_hmm_body(raw: bytes) -> tuple[bytes, bool]:
    """Decode the ``wwwapi?annotation=hmm`` payload into HMMER text.

    The live payload is a gzip body containing a plain ``HMMER3/f`` model.  A
    tar container is tolerated for robustness (some deployments bundle the model
    with metadata); in that case the first ``.hmm`` member is returned and the
    caller records the quirk.
    """
    body = util.gzip_decompress(raw) if util.is_gzip_bytes(raw) else raw
    if body.startswith(b"HMMER3/"):
        return body, False
    if body[:2] == b"\x1f\x8b":  # double-gzipped (defensive)
        body = util.gzip_decompress(body)
        if body.startswith(b"HMMER3/"):
            return body, False
    if tarfile.is_tarfile(io.BytesIO(body)):
        try:
            with tarfile.open(fileobj=io.BytesIO(body), mode="r:*") as archive:
                for member in archive.getmembers():
                    if not member.isfile() or not member.name.endswith(".hmm"):
                        continue
                    extracted = archive.extractfile(member)
                    if extracted is None:
                        continue
                    candidate = extracted.read()
                    if candidate.startswith(b"HMMER3/"):
                        return candidate, True
        except (tarfile.TarError, OSError) as err:
            raise DataError(
                "InterPro HMM payload was a tar archive but no HMMER model could be extracted",
                details={"error": str(err)},
            ) from err
    preview = util.truncate(body.decode("utf-8", errors="replace"), 200)
    raise DataError(
        "InterPro HMM payload did not contain a HMMER3 model",
        code="INTERPRO_HMM_INVALID",
        details={"bytes": len(raw), "body_preview": preview},
    )


def fetch_hmm(ctx: Any, *, database: str = "pfam", entry_id: str) -> dict[str, Any]:
    """Download one InterPro/Pfam HMMER model and return the decompressed path.

    The raw ``.hmm.gz`` payload is kept under ``ctx.cache_dir/"models"`` next to
    the decompressed ``.hmm`` file; the returned SHA-256 pins the
    model bytes so an HMM scan can be reproduced exactly.

    Args:
        ctx: command context.
        database: entry source database (``pfam`` for Pfam accessions).
        entry_id: entry accession (``PF00042``).

    Returns:
        ``{"path", "sha256", "bytes", "release", "url", "database",
        "entry_id", "raw_path", "raw_sha256", "model_version", "from_cache"}``.
        ``path`` is the local ``.hmm`` file (HMMER3 text); ``release`` is
        ``None`` (InterPro does not expose a release header) and an
        ``INTERPRO_RELEASE_UNKNOWN`` warning is recorded.

    Raises:
        NotFoundError: the entry or HMM annotation does not exist.
        DataError: the payload is not a HMMER3 model.
        LifesciError: portal failure after the retry budget.
    """
    database = _common.require_identifier(str(database or "pfam").lower(), "database")
    entry_id = _common.require_identifier(entry_id, "entry_id")
    url = f"{WWWAPI_BASE}/entry/{database}/{entry_id}/"
    try:
        raw, response = http.get_bytes(
            ctx,
            url,
            params={"annotation": "hmm"},
            database=DATABASE,
            error_code="INTERPRO",
        )
    except LifesciError as err:
        if _common.is_http_status(err, 404):
            _raise_not_found(err, what="hmm", identifier=f"{database}/{entry_id}", url=url)
        raise
    if response.dry_run:
        return {
            "path": None,
            "sha256": None,
            "bytes": None,
            "release": None,
            "url": url,
            "database": database,
            "entry_id": entry_id,
            "raw_path": None,
            "raw_sha256": None,
            "model_version": None,
            "dry_run": True,
        }
    if not raw:
        raise DataError(
            f"InterPro HMM payload for {database}/{entry_id} was empty",
            code="INTERPRO_HMM_INVALID",
            details={"url": url},
        )
    body, from_tar = _extract_hmm_body(raw)
    if from_tar:
        ctx.warn(
            "INTERPRO_HMM_TAR_CONTAINER",
            "The InterPro HMM payload was a tar archive; the first HMMER model was extracted.",
            context={"database": database, "entry_id": entry_id, "url": url},
        )
    model_version: str | None = None
    for line in body.decode("utf-8", errors="replace").splitlines():
        if line.startswith("ACC "):
            model_version = line.split(None, 1)[1].strip() if len(line.split(None, 1)) > 1 else None
            break
    models_dir = Path(ctx.cache_dir) / MODELS_SUBDIR
    util.ensure_dir(models_dir)
    stem = f"{database}_{entry_id}"
    raw_path = models_dir / f"{stem}.hmm.gz"
    hmm_path = models_dir / f"{stem}.hmm"
    raw_gz = raw if util.is_gzip_bytes(raw) else util.gzip_compress(raw)
    util.atomic_write_bytes(raw_path, raw_gz)
    util.atomic_write_bytes(hmm_path, body)
    _warn_release_unknown(ctx, url=url, kind="HMM model")
    return {
        "path": str(hmm_path),
        "sha256": util.sha256_bytes(body),
        "bytes": len(body),
        "release": None,
        "url": url,
        "database": database,
        "entry_id": entry_id,
        "raw_path": str(raw_path),
        "raw_sha256": util.sha256_bytes(raw_gz),
        "model_version": model_version,
        "from_cache": bool(response.from_cache),
    }


# ---------------------------------------------------------------------------
# explicit release pinning
# ---------------------------------------------------------------------------


def release_info(ctx: Any) -> dict[str, Any]:
    """Pin the InterPro ecosystem releases (InterPro, Pfam, PANTHER, UniProt).

    This performs the one extra API call (``GET /interpro/api/``) needed to pin
    releases, because entry payloads do not expose them.  It is optional: the
    annotation functions never call it implicitly, so offline replay of a
    recorded entry payload stays a single-request operation.

    Args:
        ctx: command context.

    Returns:
        ``{"release", "interpro", "pfam", "panther", "uniprot", "released_at",
        "url", "sha256"}`` where ``release`` is the InterPro version string
        (e.g. ``"111.0"``); a ``record_release`` entry is added to the cache
        release registry so later results can cite it.

    Raises:
        LifesciError: portal failure after the retry budget.
    """
    url = f"{API_BASE}/"
    payload, response = http.get_json(ctx, url, database=DATABASE, error_code="INTERPRO")
    if response.dry_run:
        return {"release": None, "url": url, "dry_run": True}
    payload = _common.ensure_mapping(payload, label="InterPro API root")
    databases = payload.get("databases")
    databases = databases if isinstance(databases, Mapping) else {}
    versions: dict[str, Any] = {}
    released_at: str | None = None
    for name in ("interpro", "pfam", "panther", "uniprot"):
        record = databases.get(name)
        if not isinstance(record, Mapping):
            continue
        versions[name] = {
            "version": record.get("version"),
            "release_date": record.get("releaseDate"),
        }
        if name == "interpro":
            released_at = record.get("releaseDate")
    release = (versions.get("interpro") or {}).get("version")
    if release:
        provenance.record_release(
            ctx.cache_dir,
            database=DATABASE,
            release=str(release),
            source_url=url,
        )
    return {
        "release": release,
        "interpro": (versions.get("interpro") or {}).get("version"),
        "pfam": (versions.get("pfam") or {}).get("version"),
        "panther": (versions.get("panther") or {}).get("version"),
        "uniprot": (versions.get("uniprot") or {}).get("version"),
        "released_at": released_at,
        "url": url,
        "sha256": _common.sha_of_response(response),
    }


__all__ = [
    "API_BASE",
    "DATABASE",
    "MODELS_SUBDIR",
    "WWWAPI_BASE",
    "entry_all_for_protein",
    "entry_meta",
    "fetch_hmm",
    "pfam_proteins",
    "release_info",
]
