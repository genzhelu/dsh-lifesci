"""NCBI E-utilities adapter.

Endpoints::

    https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi
    https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi
    https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi

Documented quirks handled here:

* the service is flaky: HTTP 500 HTML pages ("eutils201 WWW Error 500") and
  JSON bodies such as ``{"error": "error forwarding request ...", "type": "ip"}``
  are returned intermittently. :func:`lifesci.http.request` retries with
  exponential backoff; this adapter additionally detects body-level errors and,
  when the retry budget is exhausted, re-raises with explicit fallback advice
  (Europe PMC search / PubMed web interface / PubMed MEDLINE pages).
* NCBI **BLAST** web queues can be too slow for interactive use;
  this adapter intentionally exposes only E-utilities (no BLAST). Homology
  searches belong in :mod:`lifesci.adapters.uniprot` + local pyhmmer.

No API key is used by default; set ``LIFESCI_NCBI_API_KEY`` to raise the
documented 3 req/s limit to 10 req/s.
"""

from __future__ import annotations

import os
import re
from typing import Any, Iterable, Mapping, Sequence

from .. import http, util
from ..errors import InvalidInputError, LifesciError, NotFoundError, PortalError
from . import _common

BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
DATABASE = "ncbi"

#: Fallbacks returned when E-utilities cannot answer after retries.
FALLBACKS: tuple[str, ...] = (
    "Europe PMC search (lifesci.adapters.europepmc.search)",
    "PubMed web interface / MEDLINE page",
    "retry later with a longer backoff",
)

_HTML_ERROR_MARKERS = (
    "www error",
    "eutils",
    "error 500",
    "error 502",
    "error 503",
    "service unavailable",
    "request timed out",
)


def _api_key() -> str | None:
    return os.environ.get("LIFESCI_NCBI_API_KEY") or None


def _common_params(extra: Mapping[str, Any] | None = None) -> dict[str, Any]:
    params: dict[str, Any] = {"tool": "lifesci"}
    if email := os.environ.get("LIFESCI_CONTACT_EMAIL"):
        params["email"] = email
    if key := _api_key():
        params["api_key"] = key
    params.update(extra or {})
    return params


def _detect_error(response: Any) -> str | None:
    """Detect NCBI's 200-with-error payloads and HTML error pages."""
    content_type = (response.headers.get("content-type") or "").lower()
    if "json" in content_type or response.body.lstrip().startswith(b"{"):
        try:
            payload = response.json()
        except Exception:  # noqa: BLE001 - fall through to HTML detection
            payload = None
        if isinstance(payload, Mapping):
            for key in ("error", "ERROR", "Error"):
                if key in payload:
                    return f"NCBI error payload: {payload[key]!r}"
    text = response.body[:4_000].decode("utf-8", errors="replace").lower()
    if any(marker in text for marker in _HTML_ERROR_MARKERS) and "<html" in text:
        title = re.search(r"<title>(.*?)</title>", text, re.S)
        return f"NCBI returned an HTML error page ({title.group(1).strip() if title else 'no title'})"
    return None


def _get(ctx: Any, endpoint: str, params: Mapping[str, Any], *, database_value: str = DATABASE) -> Any:
    url = f"{BASE}/{endpoint}"
    try:
        payload, _response = http.get_json(
            ctx,
            url,
            params=params,
            detect_error=_detect_error,
            error_code="NCBI",
            database=database_value,
            headers={"accept": "application/json"},
        )
        return payload
    except PortalError as err:
        raise _common.with_fallbacks(err, FALLBACKS) from err


def _require_ids(ids: Iterable[Any], *, what: str = "ids") -> list[str]:
    values: list[str] = []
    for item in ids:
        text = str(item).strip()
        if not text:
            raise InvalidInputError(f"empty {what} entry")
        values.append(text)
    if not values:
        raise InvalidInputError(f"at least one of {what} is required")
    return values


# ---------------------------------------------------------------------------
# esearch / esummary / efetch / taxonomy
# ---------------------------------------------------------------------------


def esearch(
    ctx: Any,
    db: str,
    term: str,
    *,
    retmax: int = 20,
    retstart: int = 0,
    sort: str | None = None,
) -> dict[str, Any]:
    """Search a database and return UIDs plus hit count.

    Raises:
        PortalError: E-utilities failed after retries (fallback advice attached).
    """
    db = _common.require_identifier(db, "db")
    term = str(term or "").strip()
    if not term:
        raise InvalidInputError("esearch requires a non-empty 'term'")
    params = _common_params(
        {
            "db": db,
            "term": term,
            "retmode": "json",
            "retmax": int(retmax),
            "retstart": int(retstart),
        }
    )
    if sort:
        params["sort"] = str(sort)
    payload = _get(ctx, "esearch.fcgi", params)
    result = _common.ensure_mapping(_common.pick(payload, "esearchresult") or {}, label="esearchresult")
    if "errorlist" in result or "warninglist" in result:
        for warning in _common.as_list(result.get("warninglist")):
            ctx.warn("NCBI_SEARCH_WARNING", f"NCBI esearch warning: {warning}")
    ids = [str(uid) for uid in _common.as_list(result.get("idlist"))]
    count = _common.to_int(result.get("count"))
    query_translation = _common.first_text(result.get("querytranslation"))
    if query_translation and query_translation.lower() != term.lower():
        ctx.warn(
            "NCBI_QUERY_TRANSLATED",
            "NCBI translated the query; check the reported translation before citing the hit count.",
            context={"term": term, "query_translation": query_translation},
        )
    return {
        "db": db,
        "term": term,
        "count": count,
        "ids": ids,
        "retmax": int(retmax),
        "retstart": int(retstart),
        "query_translation": query_translation,
        "web_env": _common.first_text(result.get("webenv")),
        "query_key": _common.first_text(result.get("querykey")),
        "database": DATABASE,
        "truncated": bool(count is not None and count > int(retstart) + len(ids)),
    }


def esummary(ctx: Any, db: str, ids: Sequence[str]) -> dict[str, Any]:
    """Fetch DocSum records for UIDs."""
    db = _common.require_identifier(db, "db")
    uids = _require_ids(ids, what="ids")
    payload = _get(
        ctx,
        "esummary.fcgi",
        _common_params({"db": db, "id": ",".join(uids), "retmode": "json"}),
    )
    result = _common.ensure_mapping(_common.pick(payload, "result") or {}, label="result")
    uids_returned = [str(uid) for uid in _common.as_list(result.get("uids"))]
    summaries: dict[str, Any] = {}
    for uid in uids_returned or uids:
        record = result.get(uid)
        if isinstance(record, Mapping):
            summaries[uid] = dict(record)
    found = set(summaries)
    missing = [uid for uid in uids if uid not in found]
    if missing:
        ctx.warn(
            "NCBI_ESUMMARY_MISSING",
            f"{len(missing)} requested UID(s) had no summary record.",
            context={"db": db, "missing": missing[:10]},
        )
    return {
        "db": db,
        "ids": uids,
        "summaries": summaries,
        "missing": missing,
        "database": DATABASE,
    }


def efetch_fasta(ctx: Any, db: str, ids: Sequence[str]) -> dict[str, Any]:
    """Fetch FASTA records (e.g. ``db=nuccore`` or ``db=protein``)."""
    db = _common.require_identifier(db, "db")
    uids = _require_ids(ids, what="ids")
    text, response = http.get_text(
        ctx,
        f"{BASE}/efetch.fcgi",
        params=_common_params({"db": db, "id": ",".join(uids), "rettype": "fasta", "retmode": "text"}),
        detect_error=_detect_error,
        error_code="NCBI",
        database=DATABASE,
    )
    try:
        records = util.parse_fasta_text(text)
    except Exception as err:  # noqa: BLE001 - normalise parser errors
        raise PortalError(
            f"NCBI efetch returned a payload that is not FASTA: {err}",
            code="NCBI_FASTA_PARSE",
            retriable=False,
            details={"db": db, "ids": uids, "body_preview": util.truncate(text, 300)},
        ) from err
    if not records:
        raise NotFoundError(
            f"NCBI efetch returned no FASTA records for {db} ids {uids}",
            details={"db": db, "ids": uids, "url": response.url},
        )
    return {
        "db": db,
        "ids": uids,
        "records": [
            {"id": header.split()[0] if header else "", "header": header, "sequence": seq}
            for header, seq in records
        ],
        "n_records": len(records),
        "database": DATABASE,
    }


def efetch_text(
    ctx: Any,
    db: str,
    ids: Sequence[str],
    *,
    rettype: str = "abstract",
    retmode: str = "text",
) -> dict[str, Any]:
    """Fetch free-text records (abstracts, MEDLINE, GenBank flat files…)."""
    db = _common.require_identifier(db, "db")
    uids = _require_ids(ids, what="ids")
    text, response = http.get_text(
        ctx,
        f"{BASE}/efetch.fcgi",
        params=_common_params({"db": db, "id": ",".join(uids), "rettype": rettype, "retmode": retmode}),
        detect_error=_detect_error,
        error_code="NCBI",
        database=DATABASE,
    )
    if not text.strip():
        raise NotFoundError(
            f"NCBI efetch returned an empty {rettype} record for {db} ids {uids}",
            details={"db": db, "ids": uids, "url": response.url},
        )
    return {
        "db": db,
        "ids": uids,
        "rettype": rettype,
        "retmode": retmode,
        "text": text,
        "bytes": len(text.encode("utf-8")),
        "database": DATABASE,
    }


def _taxonomy_lineage(ctx: Any, values: Sequence[str]) -> dict[str, list[dict[str, Any]]]:
    """Fetch lineage nodes from the NCBI taxonomy XML endpoint (opt-in).

    ``efetch.fcgi?db=taxonomy&retmode=json`` is unusable (it returns bare taxid
    lines, not JSON — live-verified), so lineage is taken from the XML route,
    which is the documented structured format for taxonomy.
    """
    import xml.etree.ElementTree as ET

    url = f"{BASE}/efetch.fcgi"
    params = _common_params({"db": "taxonomy", "id": ",".join(values), "retmode": "xml"})
    try:
        xml, _response = http.get_text(
            ctx,
            url,
            params=params,
            error_code="NCBI",
            database=DATABASE,
            headers={"accept": "application/xml"},
        )
    except LifesciError as err:
        raise _common.with_fallbacks(err, FALLBACKS) from err
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        ctx.warn(
            "NCBI_TAXONOMY_LINEAGE_UNAVAILABLE",
            "NCBI taxonomy lineage XML could not be parsed; lineage lists are empty.",
            context={"taxids": values, "body_preview": util.truncate(xml, 200)},
        )
        return {}
    lineages: dict[str, list[dict[str, Any]]] = {}
    for taxon in root.findall("Taxon"):
        taxid = _common.first_text(taxon.findtext("TaxId"))
        if not taxid:
            continue
        lineages[str(taxid)] = [
            {
                "taxid": _common.to_int(node.findtext("TaxId")),
                "name": _common.first_text(node.findtext("ScientificName")),
                "rank": _common.first_text(node.findtext("Rank")),
            }
            for node in taxon.findall("./LineageEx/Taxon")
        ]
    return lineages


def taxonomy(
    ctx: Any,
    taxids: Sequence[int | str],
    *,
    include_lineage: bool = False,
) -> dict[str, Any]:
    """Fetch NCBI Taxonomy records for a list of taxids.

    Quirk handled: NCBI's ``efetch.fcgi?db=taxonomy&retmode=json`` returns plain
    taxid lines (not JSON — live-verified 2026-11), so taxonomy records are read
    from the working ``esummary.fcgi?db=taxonomy&retmode=json`` endpoint.  Full
    lineage nodes need the XML route and are fetched only when
    ``include_lineage=True`` (one extra request).

    Args:
        ctx: command context.
        taxids: NCBI taxonomy ids (ints or numeric strings).
        include_lineage: also fetch and attach lineage node lists (slower).

    Returns:
        ``{"taxids", "taxa", "n_taxa", "database", "source",
        "lineage_included"}`` where each taxon is ``{"taxid",
        "scientific_name", "common_name", "rank", "division", "status",
        "lineage"}`` (lineage empty unless requested).

    Raises:
        NotFoundError: NCBI returned no record for the requested taxids.
        LifesciError: portal failure after the retry budget.
    """
    values = _require_ids(taxids, what="taxids")
    payload = _get(
        ctx,
        "esummary.fcgi",
        _common_params({"db": "taxonomy", "id": ",".join(values), "retmode": "json"}),
    )
    result = _common.as_dict(_common.pick(payload, "result"))
    uids = [str(uid) for uid in _common.as_list(result.get("uids"))] or values
    taxa: list[dict[str, Any]] = []
    for uid in uids:
        item = result.get(uid)
        if not isinstance(item, Mapping):
            continue
        taxa.append(
            {
                "taxid": _common.to_int(item.get("taxid")) or _common.to_int(uid),
                "scientific_name": _common.first_text(item.get("scientificname")),
                "common_name": _common.first_text(item.get("commonname")),
                "rank": _common.first_text(item.get("rank")),
                "division": _common.first_text(item.get("division")),
                "status": _common.first_text(item.get("status")),
                "lineage": [],
            }
        )
    if not taxa:
        raise NotFoundError(
            f"NCBI taxonomy returned no records for {values}",
            code="NCBI_TAXONOMY_NOT_FOUND",
            details={"taxids": values},
            fallbacks=[
                "Verify the taxids are current NCBI Taxonomy ids (not gene/protein ids).",
                "Retry later; E-utilities is intermittently unavailable.",
            ],
        )
    if include_lineage:
        lineages = _taxonomy_lineage(ctx, values)
        for taxon in taxa:
            taxon["lineage"] = lineages.get(str(taxon.get("taxid")), [])
    return {
        "taxids": values,
        "taxa": taxa,
        "n_taxa": len(taxa),
        "database": DATABASE,
        "source": "esummary",
        "lineage_included": bool(include_lineage),
    }
