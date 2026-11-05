"""Europe PMC adapter.

Endpoints::

    https://www.ebi.ac.uk/europepmc/webservices/rest/search
    https://www.ebi.ac.uk/europepmc/webservices/rest/<PMCID>/fullTextXML

Documented quirks: intermittent HTTP 503 (retried with backoff by
:mod:`lifesci.http`); pagination is cursor-mark based (``cursorMark``) and the
API caps ``pageSize`` at 1000.  Full-text XML is available for open-access
records only; a missing full text is reported as a typed not-found failure with
the NCBI PMC OA / publisher fallbacks, not as a crash.
"""

from __future__ import annotations

from typing import Any, Mapping

from .. import http, util
from ..errors import InvalidInputError, LifesciError, NotFoundError, PortalError
from . import _common

BASE = "https://www.ebi.ac.uk/europepmc/webservices/rest"
DATABASE = "europe_pmc"
MAX_PAGE_SIZE = 1000

FALLBACKS: tuple[str, ...] = (
    "PubMed web interface / MEDLINE page",
    "NCBI E-utilities esearch+efetch (lifesci.adapters.ncbi)",
    "retry later (intermittent 503 on the Europe PMC REST service)",
)


def _hit(record: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": _common.first_text(_common.pick(record, "id")),
        "source": _common.first_text(_common.pick(record, "source")),
        "pmid": _common.first_text(_common.pick(record, "pmid")),
        "pmcid": _common.first_text(_common.pick(record, "pmcid")),
        "doi": _common.first_text(_common.pick(record, "doi")),
        "title": _common.first_text(_common.pick(record, "title")),
        "author_string": _common.first_text(_common.pick(record, "authorString")),
        "journal": _common.first_text(_common.pick(record, "journalTitle")),
        "pub_year": _common.first_text(_common.pick(record, "pubYear")),
        "abstract": _common.first_text(_common.pick(record, "abstractText")),
        "cited_by_count": _common.to_int(_common.pick(record, "citedByCount")),
        "is_open_access": bool(_common.pick(record, "isOpenAccess", default=False)),
    }


def search(
    ctx: Any,
    query: str,
    *,
    page_size: int = 25,
    max_pages: int = 2,
    result_type: str = "lite",
    sort: str | None = None,
) -> dict[str, Any]:
    """Search Europe PMC, following ``cursorMark`` pagination up to ``max_pages``."""
    query = str(query or "").strip()
    if not query:
        raise InvalidInputError("Europe PMC search requires a non-empty 'query'")
    page_size = max(1, min(MAX_PAGE_SIZE, int(page_size)))
    max_pages = max(1, int(max_pages))

    hits: list[dict[str, Any]] = []
    cursor = "*"
    pages = 0
    hit_count: int | None = None
    try:
        while pages < max_pages:
            payload, _response = http.get_json(
                ctx,
                f"{BASE}/search",
                params={
                    "query": query,
                    "format": "json",
                    "pageSize": page_size,
                    "resultType": result_type,
                    "cursorMark": cursor,
                    **({"sort": sort} if sort else {}),
                },
                error_code="EUROPEPMC",
                database=DATABASE,
                headers={"accept": "application/json"},
            )
            pages += 1
            data = _common.ensure_mapping(payload, label="Europe PMC search payload")
            hit_count = _common.to_int(_common.pick(data, "hitCount"))
            # Europe PMC nests the records: {"resultList": {"result": [...]}}.
            result_list = _common.pick(data, "resultList", default={})
            if isinstance(result_list, Mapping):
                records = _common.pick(result_list, "result", default=[])
            else:
                records = result_list
            for record in _common.as_list(records):
                if isinstance(record, Mapping):
                    hits.append(_hit(record))
            next_cursor = _common.first_text(_common.pick(data, "nextCursorMark"))
            if not next_cursor or next_cursor == cursor:
                break
            cursor = next_cursor
            if hit_count is not None and len(hits) >= hit_count:
                break
    except PortalError as err:
        raise _common.with_fallbacks(err, FALLBACKS) from err

    truncated = bool(hit_count is not None and len(hits) < hit_count)
    if truncated:
        ctx.warn(
            "EUROPEPMC_RESULTS_TRUNCATED",
            "Only the first pages of Europe PMC results were retrieved; the hit count refers to the full query.",
            context={"query": query, "returned": len(hits), "hit_count": hit_count, "max_pages": max_pages},
        )
    return {
        "query": query,
        "hit_count": hit_count,
        "hits": hits,
        "n_returned": len(hits),
        "pages_fetched": pages,
        "truncated": truncated,
        "database": DATABASE,
        "source": "Europe PMC REST search",
    }


def fulltext_xml(ctx: Any, pmcid: str) -> dict[str, Any]:
    """Fetch the JATS full-text XML for an open-access PMCID (e.g. ``PMC123456``)."""
    pmcid = _common.require_identifier(str(pmcid).strip().upper(), "pmcid")
    url = f"{BASE}/{pmcid}/fullTextXML"
    try:
        xml, response = http.get_text(
            ctx,
            url,
            error_code="EUROPEPMC",
            database=DATABASE,
            headers={"accept": "application/xml, application/xml;q=0.9, text/xml;q=0.8"},
        )
    except PortalError as err:
        if _common.is_http_status(err, *_common.NOT_FOUND_STATUSES):
            raise NotFoundError(
                f"Europe PMC has no full-text XML for {pmcid} (not open access or not indexed)",
                retriable=False,
                details={"pmcid": pmcid, "url": url},
                fallbacks=["NCBI PMC OA service", "publisher page", "use the abstract instead"],
            ) from err
        raise _common.with_fallbacks(err, FALLBACKS) from err
    if not xml.strip():
        raise NotFoundError(
            f"Europe PMC returned an empty full-text body for {pmcid}",
            details={"pmcid": pmcid, "url": url},
        )
    if "<" not in xml[:200]:
        ctx.warn(
            "EUROPEPMC_FULLTEXT_NOT_XML",
            "Europe PMC full-text response does not look like XML; verify the PMCID and content type.",
            context={"pmcid": pmcid, "body_preview": util.truncate(xml, 200)},
        )
    return {
        "pmcid": pmcid,
        "xml": xml,
        "bytes": len(xml.encode("utf-8")),
        "sha256": util.sha256_text(xml),
        "url": url,
        "database": DATABASE,
    }


def fulltext_sections(ctx: Any, pmcid: str) -> dict[str, Any]:
    """Fetch full text and return basic JATS section metadata (title, n_sections)."""
    payload = fulltext_xml(ctx, pmcid)
    xml = payload["xml"]
    import re

    title_match = re.search(r"<article-title[^>]*>(.*?)</article-title>", xml, re.S)
    section_titles = re.findall(r"<title[^>]*>(.*?)</title>", xml, re.S)
    strip = lambda text: re.sub(r"<[^>]+>", "", text or "").strip()  # noqa: E731
    payload.update(
        {
            "title": strip(title_match.group(1)) if title_match else None,
            "n_sections": len(section_titles),
            "section_titles": [strip(title) for title in section_titles[:50]],
        }
    )
    return payload
