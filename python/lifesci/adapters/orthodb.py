"""OrthoDB adapter.

Live-verified 2026-11: the current OrthoDB data/API release is **v13** and the
endpoints changed from the contract's ``/v11/ogroup/<id>`` form::

    https://data.orthodb.org/v13/orthodb_release_id   -> "v13.0"
    https://data.orthodb.org/v13/group?id=<og_id>     OG annotation object
    https://data.orthodb.org/v13/search?query=...&take=N&level=<taxid>
                                                     {"data": [og_ids], "count",
                                                      "bigdata": [metadata]}

``/v11/ogroup/<id>`` returns HTTP 404 ``{}`` for every id we probed (including
``160374at2759`` from the documented example and ``2904121at2759`` from a live
search), and the
OrthoDB documentation states that OG ids are **not inherited** across releases,
so :func:`orthogroup` falls back to a search-by-id when the group endpoint yields
nothing and reports ``Not found in the current release`` rather than mislabelling
the request.

The API version/data release is recorded (``release: "v13.0"`` when
``orthodb_release_id`` is reachable, else ``"v13"`` with a warning).  Orthogroup
membership is transitive homology evidence — callers must combine it with
domain/family evidence before calling orthology.
"""

from __future__ import annotations

from typing import Any, Mapping

from .. import http, provenance
from ..errors import InvalidInputError, LifesciError, NotFoundError, PortalError
from . import _common

BASE = "https://data.orthodb.org/v13"
API_VERSION = "v13"
DATABASE = "orthodb"


def _level_taxid(og_id: str) -> int | None:
    """OG ids encode the level-of-orthology taxid as ``<cluster>at<taxid>``."""
    text = str(og_id or "")
    if "at" in text:
        tail = text.rsplit("at", 1)[-1]
        return _common.to_int(tail)
    return None


def _release(ctx: Any) -> dict[str, Any]:
    """Best-effort data/API release from ``/v13/orthodb_release_id``."""
    url = f"{BASE}/orthodb_release_id"
    try:
        payload, _response = http.get_json(
            ctx, url, error_code="ORTHODB", database=DATABASE,
            headers={"accept": "application/json"},
        )
    except LifesciError:
        ctx.warn(
            "ORTHODB_RELEASE_UNKNOWN",
            "OrthoDB data release could not be determined; the API version 'v13' is recorded "
            "but the data snapshot is unknown.",
            severity="info",
            context={"url": url},
        )
        return {"release": API_VERSION, "api_version": API_VERSION, "data_release": None}
    data_release = _common.first_text(payload)
    if data_release:
        try:
            provenance.record_release(
                ctx.cache_dir, database=DATABASE, release=data_release, source_url=url
            )
        except OSError:  # pragma: no cover - read-only cache locations must not fail work
            pass
    return {
        "release": data_release or API_VERSION,
        "api_version": API_VERSION,
        "data_release": data_release,
    }


def _normalise_group(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Normalise an OrthoDB ``/group`` annotation object (v13 schema)."""
    genes = _common.as_list(_common.pick(payload, "genes", "gene", default=[]))
    og_id = _common.first_text(_common.pick(payload, "public_id", "id", "og_id", "group_id"))
    level_taxid = _common.to_int(_common.pick(payload, "tax_id", "level_taxid", "level"))
    if level_taxid is None and og_id:
        level_taxid = _level_taxid(og_id)
    return {
        "og_id": og_id,
        "level_taxid": level_taxid,
        "level_name": _common.first_text(_common.pick(payload, "level_name", "tax_name")),
        "description": _common.first_text(_common.pick(payload, "name", "description", "annotation")),
        "n_genes": (
            len(genes)
            if genes
            else (_common.to_int(
                _common.pick(
                    _common.pick(payload, "phyletic_profile", default={}) or {},
                    "gene_count",
                )
            )
            or _common.to_int(_common.pick(payload, "n_genes", "gene_count", "count")))
        ),
        "genes": genes[:100] if genes else None,
        "interpro_domains": _common.as_list(payload.get("interpro_domains")),
        "evolutionary_rate": _common.to_float(payload.get("evolutionary_rate")),
        "phyletic_profile": _common.as_dict(payload.get("phyletic_profile")),
        "gene_architecture": _common.as_dict(payload.get("gene_architecture")),
        "ec_numbers": _common.as_list(payload.get("ECnumber")),
    }


def _group_from_search(og_id: str, payload: Mapping[str, Any]) -> dict[str, Any] | None:
    """Build an orthogroup record from the ``/search`` ``bigdata`` metadata."""
    bigdata = [item for item in _common.as_list(payload.get("bigdata")) if isinstance(item, Mapping)]
    chosen: Mapping[str, Any] | None = None
    for item in bigdata:
        if og_id in {
            str(item.get("id") or ""),
            str(item.get("public_id") or ""),
        }:
            chosen = item
            break
    if chosen is None and bigdata:
        chosen = bigdata[0]
    if chosen is None:
        return None
    public_id = _common.first_text(_common.pick(chosen, "public_id", "id"))
    return {
        "og_id": public_id or og_id,
        "level_taxid": _level_taxid(public_id or og_id),
        "level_name": _common.first_text(_common.pick(chosen, "level_name")),
        "description": _common.first_text(_common.pick(chosen, "name", "description")),
        "n_genes": _common.to_int(_common.pick(chosen, "gene_count")),
        "genes": None,
        "present_in": _common.to_int(_common.pick(chosen, "present_in")),
        "relevance": _common.to_float(_common.pick(chosen, "relevance")),
        "d2t": _common.to_int(_common.pick(chosen, "d2t")),
    }


def orthogroup(ctx: Any, og_id: str) -> dict[str, Any]:
    """Fetch one OrthoDB orthogroup by id (e.g. ``5998751at33208``).

    Args:
        ctx: command context.
        og_id: OrthoDB OG id (``<cluster>at<level-taxid>``).

    Returns:
        ``{"og_id", "level_taxid", "level_name", "description", "n_genes",
        "genes", "interpro_domains", "evolutionary_rate", "phyletic_profile",
        "gene_architecture", "release", "api_version", "url", "database",
        "caveat"}``.  ``genes`` is ``None`` when the group endpoint does not
        include member genes; the metadata fields are enough for orthology
        cross-checks and keeping the payload small.

    Raises:
        NotFoundError: the OG id is unknown in the current release (OG ids are
            not inherited across OrthoDB releases).
        LifesciError: portal failure after the retry budget.
    """
    og_id = _common.require_identifier(str(og_id or "").strip(), "orthogroup id")
    url = f"{BASE}/group"
    payload: Any = None
    try:
        payload, _response = http.get_json(
            ctx,
            url,
            params={"id": og_id},
            allow_status=(200, 404),
            error_code="ORTHODB",
            database=DATABASE,
            headers={"accept": "application/json"},
        )
    except PortalError as err:  # pragma: no cover - allow_status covers 404
        raise
    entry: dict[str, Any] | None = None
    if isinstance(payload, Mapping) and payload:
        # v13 wraps the annotation object in {"data": {...}, "status": "ok"}.
        body = payload.get("data") if isinstance(payload.get("data"), Mapping) else payload
        if isinstance(body, Mapping) and body:
            entry = _normalise_group(body)
    if entry is None:
        # Fallback: OG ids are not stable across releases; search by the id.
        search_url = f"{BASE}/search"
        try:
            search_payload, _response = http.get_json(
                ctx,
                search_url,
                params={"query": og_id, "take": 10},
                error_code="ORTHODB",
                database=DATABASE,
                headers={"accept": "application/json"},
            )
        except PortalError as err:
            raise
        if isinstance(search_payload, Mapping):
            entry = _group_from_search(og_id, search_payload)
            if entry is not None:
                ctx.warn(
                    "ORTHODB_OGROUP_FALLBACK_SEARCH",
                    f"OrthoDB /group?id={og_id} returned no record; the OG metadata was recovered "
                    "via /search. OG ids are not inherited across OrthoDB releases, so re-check the "
                    "id against the current release.",
                    context={"og_id": og_id, "search_url": search_url},
                )
    if entry is None:
        raise NotFoundError(
            f"OrthoDB has no orthogroup {og_id!r} in the current release",
            code="ORTHODB_OG_NOT_FOUND",
            details={"og_id": og_id, "url": f"{BASE}/group?id={og_id}"},
            fallbacks=[
                "OrthoDB OG ids are not inherited across releases: resolve the group again by "
                "gene name, protein symbol or UniProt accession with orthodb.search(...).",
                "Check the id against https://www.orthodb.org/ for the current release.",
                "Use OMA or PANTHER via InterPro as an independent orthology source.",
            ],
        )
    entry.update(
        {
            **_release(ctx),
            "url": f"{BASE}/group?id={entry.get('og_id') or og_id}",
            "database": DATABASE,
            "caveat": (
                "OrthoDB orthogroups are inferred homology groups (not curated one-to-one orthologues); "
                "the taxon level of the group (level_taxid) constrains interpretation."
            ),
        }
    )
    return entry


def search(
    ctx: Any,
    query: str,
    *,
    level_taxid: int | None = None,
    limit: int = 10,
) -> dict[str, Any]:
    """Search OrthoDB orthogroups by annotation text / gene name / accession.

    Args:
        ctx: command context.
        query: text query (gene symbol, description, UniProt accession, OG id …).
        level_taxid: optional level-of-orthology NCBI taxid filter.
        limit: maximum OG ids returned (``take`` parameter, 1-100).

    Returns:
        ``{"query", "level_taxid", "results", "n_results", "n_total",
        "truncated", "release", "api_version", "url", "database"}`` where each
        result is ``{"og_id", "name", "level_name", "gene_count", "present_in",
        "relevance", "d2t"}`` (metadata from the ``bigdata`` block).

    Raises:
        LifesciError: portal failure after the retry budget.
    """
    query = str(query or "").strip()
    if not query:
        raise InvalidInputError("OrthoDB search requires a non-empty 'query'")
    take = max(1, min(100, int(limit)))
    params: dict[str, Any] = {"query": query, "take": take}
    if level_taxid is not None:
        params["level"] = int(level_taxid)
    url = f"{BASE}/search"
    try:
        payload, _response = http.get_json(
            ctx,
            url,
            params=params,
            error_code="ORTHODB",
            database=DATABASE,
            headers={"accept": "application/json"},
        )
    except PortalError as err:
        raise
    if not isinstance(payload, Mapping):
        raise PortalError(
            f"OrthoDB returned an unexpected payload type for {query!r}",
            code="ORTHODB_PAYLOAD_INVALID",
            retriable=False,
            details={"query": query, "url": url},
        )
    metadata = {
        str(item.get("id") or item.get("public_id") or ""): item
        for item in _common.as_list(payload.get("bigdata"))
        if isinstance(item, Mapping)
    }
    results: list[dict[str, Any]] = []
    for item in _common.as_list(payload.get("data")):
        og_id = _common.first_text(item)
        if not og_id:
            continue
        info = metadata.get(og_id) or {}
        results.append(
            {
                "og_id": og_id,
                "name": _common.first_text(_common.pick(info, "name")),
                "level_name": _common.first_text(_common.pick(info, "level_name")),
                "gene_count": _common.to_int(_common.pick(info, "gene_count")),
                "present_in": _common.to_int(_common.pick(info, "present_in")),
                "relevance": _common.to_float(_common.pick(info, "relevance")),
                "d2t": _common.to_int(_common.pick(info, "d2t")),
            }
        )
    n_total = _common.to_int(payload.get("count"))
    truncated = n_total is not None and len(results) < n_total
    if truncated:
        ctx.warn_top_hit_cap("orthodb", n_total)
    return {
        "query": query,
        "level_taxid": level_taxid,
        **_release(ctx),
        "results": results,
        "n_results": len(results),
        "n_total": n_total,
        "truncated": truncated,
        "url": url,
        "database": DATABASE,
    }
