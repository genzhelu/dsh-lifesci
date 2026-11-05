"""OMA (Orthologous MAtrix) adapter.

Endpoints (live-verified 2026-11):

    https://omabrowser.org/api/protein/<acc>/     flat protein record
    https://omabrowser.org/api/version/           {"oma_version": "All.May2026",
                                                   "api_version": "1.11"}

The protein payload is **flat** (not nested under ``protein``)::

    {"entry_nr": 123456, "omaid": "HUMAN12345", "canonicalid": "HBA_HUMAN",
     "sequence_length": 142,
     "species": {"code": "HUMAN", "taxon_id": 9606, "species": "..."},
     "oma_group": 100000, "oma_hog_id": "HOG:0000000", "roothog_id": 100000,
     "hog_levels": [...], "description": "...", "oma_group_url": "...",
     "oma_hog_members": "...", "orthologs": "...", "sequence": "..."}

so ``oma_hog`` comes from ``oma_hog_id`` (a ``HOG:`` string) and the UniProt
accession is the caller's, with ``canonicalid``/``omaid`` carried alongside.  The
data release (``All.May2026``) is not in the protein payload; it is fetched
best-effort from ``/api/version/`` and recorded in the release registry.  Unknown
accessions return non-200 error bodies, translated to a typed not-found error.

OMA groups and HOGs are inferred orthologue sets, not curated one-to-one
orthology calls — callers must report which level they used.
"""

from __future__ import annotations

from typing import Any, Mapping

from .. import http, provenance, util
from ..errors import InvalidInputError, LifesciError, NotFoundError, PortalError
from . import _common

BASE = "https://omabrowser.org/api"
DATABASE = "oma"


def _release(ctx: Any) -> str | None:
    """Best-effort OMA data release from ``/api/version/`` (never fatal)."""
    url = f"{BASE}/version/"
    try:
        payload, _response = http.get_json(
            ctx, url, error_code="OMA", database=DATABASE, headers={"accept": "application/json"}
        )
    except LifesciError:
        ctx.warn(
            "OMA_RELEASE_UNKNOWN",
            "OMA data release could not be determined (the /api/version/ endpoint was "
            "unavailable); results cannot be release-pinned.",
            severity="info",
            context={"url": url},
        )
        return None
    record = _common.as_dict(payload)
    release = _common.first_text(record.get("oma_version"))
    if release:
        try:
            provenance.record_release(ctx.cache_dir, database=DATABASE, release=release, source_url=url)
        except OSError:  # pragma: no cover - read-only cache locations must not fail work
            pass
    return release


def _normalise_entry(payload: Mapping[str, Any], accession: str | None = None) -> dict[str, Any]:
    """Normalise the flat OMA ``/api/protein/<acc>/`` record (live-verified)."""
    protein = _common.pick(payload, "protein")
    protein = protein if isinstance(protein, Mapping) else payload
    species_value = _common.pick(protein, "species")
    species = None
    taxid = None
    if isinstance(species_value, Mapping):
        species = _common.first_text(_common.pick(species_value, "species", "name"))
        taxid = _common.to_int(_common.pick(species_value, "taxon_id", "taxid"))
    elif species_value is not None:
        species = _common.first_text(species_value)
    group = _common.pick(protein, "oma_group", "group")
    hog = _common.pick(protein, "oma_hog_id", "oma_hog", "hog")
    resolved_accession = _common.first_text(
        _common.pick(protein, "entry_nr", "id", "uniprot_accession", "uniprot_entry")
    )
    canonical = _common.first_text(_common.pick(protein, "canonicalid", "canonical_id"))
    return {
        # ``accession`` is the requested UniProt accession; the
        # numeric OMA entry number is exposed separately.
        "accession": accession or resolved_accession,
        "oma_entry_nr": resolved_accession,
        "description": canonical or _common.first_text(_common.pick(protein, "omaid")),
        "uniprot_accession": _common.first_text(
            _common.pick(protein, "uniprot_accession", "uniprot_entry")
        ),
        "canonical_id": _common.first_text(_common.pick(protein, "canonicalid", "canonical_id")),
        "oma_id": _common.first_text(_common.pick(protein, "omaid")),
        "oma_group": _common.to_int(group) if not isinstance(group, Mapping) else _common.to_int(_common.pick(group, "group_nr", "id")),
        "oma_hog": _common.first_text(hog) if not isinstance(hog, Mapping) else _common.first_text(_common.pick(hog, "hog_nr", "id", "hog_id")),
        "roothog_id": _common.to_int(_common.pick(protein, "roothog_id")),
        "species": species,
        "taxid": taxid,
        "sequence_length": _common.to_int(_common.pick(protein, "sequence_length", "length")),
        "chromosome": _common.first_text(_common.pick(protein, "chromosome")),
        "is_main_isoform": protein.get("is_main_isoform"),
        "hog_levels": _common.as_list(_common.pick(protein, "hog_levels"))[:20] or None,
        "sequence_md5": _common.first_text(_common.pick(protein, "sequence_md5")),
    }


def protein(ctx: Any, accession: str) -> dict[str, Any]:
    """Return OMA group/HOG membership for a UniProt accession.

    Args:
        ctx: command context.
        accession: UniProtKB accession (``P69905``).

    Returns:
        ``{"accession", "canonicalid", "omaid", "entry_nr", "oma_group",
        "oma_hog", "roothog_id", "hog_levels", "species", "species_code",
        "taxon_id", "sequence_length", "description", "oma_group_url",
        "hog_members_url", "orthologs_url", "release", "url", "database",
        "caveat"}``.  ``oma_hog`` keeps the ``HOG:`` prefix; ``hog_members`` is
        ``None`` (the payload only carries the members URL, not the list).

    Raises:
        NotFoundError: OMA has no record for the accession (HTTP 404).
        LifesciError: portal failure after the retry budget.
    """
    accession = _common.require_identifier(str(accession or "").strip(), "accession")
    url = f"{BASE}/protein/{accession}/"
    try:
        payload, _response = http.get_json(
            ctx,
            url,
            error_code="OMA",
            database=DATABASE,
            headers={"accept": "application/json"},
        )
    except PortalError as err:
        if _common.is_http_status(err, *_common.NOT_FOUND_STATUSES):
            raise NotFoundError(
                f"OMA has no record for {accession}",
                code="OMA_NOT_FOUND",
                details={"accession": accession, "url": url},
                fallbacks=[
                    "OMA group browser (https://omabrowser.org/oma/)",
                    "OrthoDB (lifesci.adapters.orthodb) or PANTHER via InterPro",
                ],
            ) from err
        raise
    if not isinstance(payload, Mapping):
        raise PortalError(
            f"OMA returned an unexpected payload type for {accession}",
            code="OMA_PAYLOAD_INVALID",
            retriable=False,
            details={"accession": accession, "url": url},
        )
    entry = _normalise_entry(payload, accession)
    if not entry.get("oma_group") and not entry.get("oma_hog"):
        ctx.warn(
            "OMA_NO_GROUP",
            f"OMA record for {accession} has no OMA group/HOG id; it may be unassigned.",
            context={"accession": accession},
        )
    entry.update(
        {
            "release": _release(ctx),
            "url": url,
            "database": DATABASE,
            "caveat": (
                "OMA groups are strict orthologue sets; HOGs are hierarchical orthologous groups. "
                "Neither is a curated one-to-one orthology statement."
            ),
        }
    )
    return entry
