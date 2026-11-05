"""UniProt REST/FTP adapter.

Endpoints (live-verified):

* ``GET https://rest.uniprot.org/uniprotkb/{acc}.fasta`` — sequence fetch.
* ``GET https://rest.uniprot.org/uniprotkb/{acc}.json`` — entry metadata.
* ``GET https://rest.uniprot.org/uniprotkb/search?query=…&format=tsv&fields=…``
  — bulk metadata; pagination via the ``Link: <…>; rel="next"`` header and
  ``x-total-results`` for the total count.
* ``GET https://rest.uniprot.org/proteomes/search?query=organism_id:<taxid>&format=json``
  — proteome discovery (``proteomeType`` distinguishes ``Reference proteome``
  from ``Excluded``/``Other``).
* ``GET https://rest.uniprot.org/uniprotkb/stream?query=proteome:<ID>&format=fasta&compressed=true``
  — proteome FASTA stream; **frequently rate-limited (HTTP 429)** and, during
  probing, occasionally hanging.  The contract fallback is the release-pinned
  EBI/UniProt FTP reference-proteome file::

      https://ftp.uniprot.org/pub/databases/uniprot/current_release/knowledgebase/
          reference_proteomes/Eukaryota/<PROTID>/<PROTID>_<TAXID>.fasta.gz

  Only *reference* proteomes are mirrored there: a 404 means the identifier is
  not a reference proteome (or the taxid in the filename does not match), and
  the error says so explicitly instead of pretending the proteome is empty.

The database release is pinned from the ``x-uniprot-release`` response header
(e.g. ``2026_04``) on every REST response; FTP-fallback payloads cannot expose
it, so a ``DATABASE_RELEASE_UNKNOWN``-style warning is emitted rather than
silently returning unpinned data.
"""

from __future__ import annotations

import csv
import io
import re
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .. import http, util
from ..errors import DataError, InvalidInputError, LifesciError, NotFoundError
from . import _common

#: Database label recorded in the cache/provenance/release registry.
DATABASE = "uniprot"

#: Response header carrying the UniProt Knowledgebase release (``2026_04``).
RELEASE_HEADER = "x-uniprot-release"

#: Base URLs (kept as module constants so tests can monkeypatch/assert them).
REST_BASE = "https://rest.uniprot.org"
UNIPROTKB_BASE = f"{REST_BASE}/uniprotkb"
PROTEOMES_BASE = f"{REST_BASE}/proteomes"
STREAM_URL = f"{UNIPROTKB_BASE}/stream"
SEARCH_URL = f"{UNIPROTKB_BASE}/search"
FTP_PROTEOMES_BASE = (
    "https://ftp.uniprot.org/pub/databases/uniprot/current_release/knowledgebase/reference_proteomes"
)

#: Default TSV field set for :func:`search`.
DEFAULT_SEARCH_FIELDS: tuple[str, ...] = (
    "accession",
    "id",
    "protein_name",
    "organism_name",
    "length",
)

#: UniProt search accepts at most 500 rows per page.
MAX_PAGE_SIZE = 500

#: Parses the ``next`` relation out of a pagination ``Link`` header.
_LINK_NEXT_RE = re.compile(r'<([^>]+)>\s*;\s*rel="?next"?', re.IGNORECASE)

#: Extracts the accession/entry-name pair from a UniProt FASTA header.
_FASTA_HEADER_RE = re.compile(r"^(?:[a-z]{2}\|)?([A-Za-z0-9_-]+)\|([A-Za-z0-9_-]+)")


# ---------------------------------------------------------------------------
# internal helpers
# ---------------------------------------------------------------------------


def _release_from(ctx: Any, response: Any, *, url: str) -> str | None:
    """Return the pinned UniProt release and warn honestly when it is absent."""
    release = getattr(response, "release", None)
    if not release:
        ctx.warn(
            "DATABASE_RELEASE_UNKNOWN",
            "UniProt did not expose an x-uniprot-release header for this payload; downstream "
            "E-values computed against it cannot be release-pinned.",
            context={"url": url, "final_url": getattr(response, "final_url", url)},
        )
    return release


def _raise_not_found(err: LifesciError, *, what: str, identifier: str, url: str) -> None:
    """Translate a portal 404 into an actionable :class:`NotFoundError`."""
    raise NotFoundError(
        f"UniProt {what} {identifier!r} was not found (HTTP 404)",
        code=f"UNIPROT_{what.upper()}_NOT_FOUND",
        details={"identifier": identifier, "url": url, "portal_error": getattr(err, "code", None)},
        fallbacks=[
            "Verify the identifier; primary/secondary accessions and gene names differ.",
            "Use UniProt search (format=tsv) or the UniProt website to resolve the current identifier.",
            "The entry may have been deleted, merged, or replaced in a newer release.",
        ],
    )


def _fasta_header_parts(header: str) -> tuple[str | None, str | None]:
    """Split ``sp|P69905|HBA_HUMAN …`` into ``(accession, entry_name)``."""
    match = _FASTA_HEADER_RE.match(header.strip())
    if match:
        return match.group(1), match.group(2)
    token = header.split()[0] if header.split() else ""
    return (token or None), None


def _coerce_taxid(value: Any) -> int | None:
    """Validate an optional NCBI taxonomy id used in the FTP filename."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    number = util.parse_int(value)
    if number is None or number <= 0:
        raise InvalidInputError(
            f"taxid must be a positive integer, got {value!r}",
            details={"taxid": value},
        )
    return number


def _dry_run_fasta(accession: str, url: str) -> dict[str, Any]:
    """Placeholder payload for ``--dry-run`` (no network was touched)."""
    return {
        "accession": accession,
        "header": None,
        "sequence": None,
        "length": None,
        "url": url,
        "sha256": None,
        "release": None,
        "dry_run": True,
    }


def _dry_run_entry(accession: str, url: str) -> dict[str, Any]:
    return {
        "accession": accession,
        "entry": None,
        "parsed": None,
        "release": None,
        "url": url,
        "sha256": None,
        "dry_run": True,
    }


# ---------------------------------------------------------------------------
# sequence fetch
# ---------------------------------------------------------------------------


def fetch_fasta(ctx: Any, accession: str) -> dict[str, Any]:
    """Fetch one UniProtKB entry as FASTA and parse it.

    Args:
        ctx: command context (cache/retry/offline/provenance).
        accession: UniProtKB accession (primary or secondary), e.g. ``P69905``.

    Returns:
        ``{"accession", "primary_accession", "entry_name", "header", "sequence",
        "length", "url", "final_url", "sha256", "release", "from_cache"}``.

    Raises:
        NotFoundError: the accession does not exist (HTTP 404).
        DataError: the payload is not a parseable single-record FASTA.
        LifesciError: portal failures after the retry budget.
    """
    accession = _common.require_identifier(accession, "accession")
    url = f"{UNIPROTKB_BASE}/{accession}.fasta"
    try:
        text, response = http.get_text(
            ctx,
            url,
            database=DATABASE,
            release_header=RELEASE_HEADER,
            error_code="UNIPROT",
        )
    except LifesciError as err:
        if _common.is_http_status(err, 404):
            _raise_not_found(err, what="accession", identifier=accession, url=url)
        raise
    if response.dry_run:
        return _dry_run_fasta(accession, url)

    records = _common.parse_fasta_payload(text.encode("utf-8"), label=f"UniProt FASTA {accession}")
    if len(records) > 1:
        ctx.warn(
            "UNIPROT_MULTIPLE_FASTA_RECORDS",
            f"UniProt returned {len(records)} FASTA records for {accession}; only the first is used.",
            context={"accession": accession, "n_records": len(records)},
        )
    header, sequence = records[0]
    sequence = sequence.strip()
    if not sequence:
        raise DataError(
            f"UniProt FASTA for {accession} has an empty sequence",
            details={"accession": accession, "url": url},
        )
    primary_accession, entry_name = _fasta_header_parts(header)
    if primary_accession and primary_accession.upper() != accession.upper():
        ctx.warn(
            "UNIPROT_ACCESSION_RESOLVED",
            f"Requested accession {accession!r} resolved to {primary_accession!r} "
            "(secondary accession or entry merge).",
            context={"requested": accession, "resolved": primary_accession},
        )
    return {
        "accession": primary_accession or accession,
        "primary_accession": primary_accession,
        "entry_name": entry_name,
        "header": header,
        "sequence": sequence,
        "length": len(sequence),
        "url": url,
        "final_url": response.final_url,
        "sha256": _common.sha_of_response(response),
        "release": _release_from(ctx, response, url=url),
        "from_cache": bool(response.from_cache),
    }


# ---------------------------------------------------------------------------
# entry metadata
# ---------------------------------------------------------------------------


def _protein_name(entry: Mapping[str, Any]) -> str | None:
    """Extract the recommended protein name, falling back to submitted names."""
    description = entry.get("proteinDescription") or {}
    recommended = _common.pick(description, "recommendedName.fullName.value", "recommendedName.fullName")
    if isinstance(recommended, str) and recommended.strip():
        return recommended.strip()
    submissions = _common.as_list(description.get("submissionNames"))
    for submission in submissions:
        value = _common.pick(submission, "fullName.value", "fullName")
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _gene_names(entry: Mapping[str, Any]) -> list[str]:
    """Flatten gene names (primary, synonyms, ORF names, ordered-locus names)."""
    names: list[str] = []
    for gene in _common.as_list(entry.get("genes")):
        for key in ("geneName", "synonyms", "orfNames", "orderedLocusNames"):
            for item in _common.as_list(gene.get(key)):
                value = item.get("value") if isinstance(item, Mapping) else item
                if isinstance(value, str) and value.strip():
                    names.append(value.strip())
    return _common.dedupe(names)


def _parse_entry(entry: Mapping[str, Any]) -> dict[str, Any]:
    """Normalise a raw UniProtKB JSON entry into the normalised subset."""
    organism = entry.get("organism") or {}
    sequence = entry.get("sequence") or {}
    entry_type = str(entry.get("entryType") or "")
    cross_references: list[dict[str, str]] = []
    for reference in _common.as_list(entry.get("uniProtKBCrossReferences")):
        database = str(reference.get("database") or "").strip()
        identifier = str(reference.get("id") or "").strip()
        if database and identifier:
            cross_references.append({"db": database, "id": identifier})
    features: list[dict[str, Any]] = []
    for feature in _common.as_list(entry.get("features")):
        location = feature.get("location") or {}
        start = _common.to_int(_common.pick(location, "start.value", "start"))
        end = _common.to_int(_common.pick(location, "end.value", "end"))
        features.append(
            {
                "type": feature.get("type"),
                "start": start,
                "end": end,
                "description": feature.get("description"),
            }
        )
    comments: list[dict[str, Any]] = []
    for comment in _common.as_list(entry.get("comments")):
        texts = [
            str(text.get("value"))
            for text in _common.as_list(comment.get("texts"))
            if isinstance(text, Mapping) and text.get("value")
        ]
        if not texts:
            single = comment.get("text")
            if isinstance(single, str) and single:
                texts = [single]
        comments.append({"commentType": comment.get("commentType"), "texts": texts})
    keywords = [
        str(keyword.get("name"))
        for keyword in _common.as_list(entry.get("keywords"))
        if isinstance(keyword, Mapping) and keyword.get("name")
    ]
    references: list[dict[str, Any]] = []
    for reference in _common.as_list(entry.get("references")):
        citation = reference.get("citation") or {}
        pmid = None
        doi = None
        for crossref in _common.as_list(citation.get("citationCrossReferences")):
            database = str(crossref.get("database") or "").lower()
            identifier = crossref.get("id")
            if database == "pubmed" and identifier:
                pmid = str(identifier)
            elif database == "doi" and identifier:
                doi = str(identifier)
        if pmid is None and citation.get("id"):
            pmid = str(citation.get("id"))
        year = citation.get("publicationDate")
        references.append(
            {
                "pmid": pmid,
                "doi": doi,
                "title": citation.get("title"),
                "journal": citation.get("journalTitle"),
                "year": str(year)[:4] if year else None,
            }
        )
    return {
        "primaryAccession": entry.get("primaryAccession"),
        "secondaryAccessions": [str(item) for item in _common.as_list(entry.get("secondaryAccessions"))],
        "entryType": entry_type,
        "reviewed": "reviewed" in entry_type.lower(),
        "protein_name": _protein_name(entry),
        "genes": _gene_names(entry),
        "organism": {
            "taxon_id": _common.to_int(organism.get("taxonId")),
            "scientific_name": organism.get("scientificName"),
            "common_name": organism.get("commonName"),
        },
        "sequence": {
            "length": _common.to_int(sequence.get("length")),
            "mol_weight": _common.to_int(sequence.get("molWeight")),
            "checksum": sequence.get("crc64") or sequence.get("md5"),
        },
        "cross_references": cross_references,
        "features": features,
        "comments": comments,
        "keywords": keywords,
        "references": references,
    }


def fetch_entry(ctx: Any, accession: str) -> dict[str, Any]:
    """Fetch and normalise one UniProtKB JSON entry.

    Args:
        ctx: command context.
        accession: UniProtKB accession.

    Returns:
        ``{"accession", "entry", "parsed", "release", "url", "sha256"}`` where
        ``parsed`` is the documented normalised subset
        (accessions, entry type, protein name, genes, organism, sequence,
        cross-references, features, comments, keywords, references).  The raw
        ``entry`` dict is returned unchanged so nothing is lost by the subset.

    Raises:
        NotFoundError: the accession does not exist (HTTP 404).
        LifesciError: portal failures after the retry budget.
    """
    accession = _common.require_identifier(accession, "accession")
    url = f"{UNIPROTKB_BASE}/{accession}.json"
    try:
        entry, response = http.get_json(
            ctx,
            url,
            database=DATABASE,
            release_header=RELEASE_HEADER,
            error_code="UNIPROT",
        )
    except LifesciError as err:
        if _common.is_http_status(err, 404):
            _raise_not_found(err, what="accession", identifier=accession, url=url)
        raise
    if response.dry_run:
        return _dry_run_entry(accession, url)
    entry = _common.ensure_mapping(entry, label=f"UniProt entry {accession}")
    parsed = _parse_entry(entry)
    primary = str(parsed.get("primaryAccession") or accession)
    if primary.upper() != accession.upper():
        ctx.warn(
            "UNIPROT_ACCESSION_RESOLVED",
            f"Requested accession {accession!r} resolved to {primary!r}.",
            context={"requested": accession, "resolved": primary},
        )
    return {
        "accession": primary,
        "entry": entry,
        "parsed": parsed,
        "release": _release_from(ctx, response, url=url),
        "url": url,
        "final_url": response.final_url,
        "sha256": _common.sha_of_response(response),
        "from_cache": bool(response.from_cache),
    }


# ---------------------------------------------------------------------------
# bulk search
# ---------------------------------------------------------------------------


def _parse_tsv_rows(text: str, fields: Sequence[str]) -> list[dict[str, Any]]:
    """Parse a UniProt TSV page into row dicts keyed by the requested field ids."""
    reader = csv.reader(io.StringIO(text), delimiter="\t")
    rows: list[list[str]] = []
    for raw in reader:
        if not raw or (len(raw) == 1 and not raw[0].strip()):
            continue
        rows.append(raw)
    if not rows:
        return []
    header = rows[0]
    keyed_by_request = len(header) == len(fields)
    output: list[dict[str, Any]] = []
    for row in rows[1:]:
        if len(row) != len(header):
            continue
        if keyed_by_request:
            output.append({field: value for field, value in zip(fields, row)})
        else:
            output.append({label: value for label, value in zip(header, row)})
    return output


def _next_link(response: Any) -> str | None:
    """Extract the ``rel="next"`` pagination URL from a response's Link header."""
    for key, value in (getattr(response, "headers", None) or {}).items():
        if key.lower() != "link" or not value:
            continue
        match = _LINK_NEXT_RE.search(value)
        if match:
            return match.group(1)
    return None


def search(
    ctx: Any,
    query: str,
    *,
    fields: Sequence[str] = DEFAULT_SEARCH_FIELDS,
    size: int = MAX_PAGE_SIZE,
    max_pages: int = 5,
) -> dict[str, Any]:
    """Search UniProtKB and return TSV rows with release and pagination metadata.

    Args:
        ctx: command context.
        query: UniProt query syntax (``accession:P69905``, ``organism_id:9606``…).
        fields: TSV field ids to request (default: accession, id, protein_name,
            organism_name, length).
        size: rows per page (1–500; UniProt caps the page at 500).
        max_pages: maximum pages to fetch; when the result set is larger the
            payload is marked ``truncated`` and ``TOP_HIT_LIST_CAPPED`` warned.

    Returns:
        ``{"query", "fields", "rows", "n_total", "n_rows", "truncated", "pages",
        "release", "url", "page_sha256"}``.  Rows are keyed by the requested
        field ids when the response column count matches, otherwise by the
        server's column labels (``column_labels`` reports them).

    Raises:
        InvalidInputError: empty query / invalid page parameters.
        LifesciError: portal failure after the retry budget.
    """
    if not str(query or "").strip():
        raise InvalidInputError("UniProt search requires a non-empty query")
    fields = tuple(str(field) for field in (fields or DEFAULT_SEARCH_FIELDS))
    if not fields:
        raise InvalidInputError("UniProt search requires at least one field")
    page_size = max(1, min(MAX_PAGE_SIZE, int(size)))
    max_pages = max(1, int(max_pages))

    params: dict[str, Any] = {
        "query": str(query),
        "format": "tsv",
        "fields": ",".join(fields),
        "size": page_size,
    }
    rows: list[dict[str, Any]] = []
    page_sha: list[str] = []
    pages = 0
    n_total: int | None = None
    column_labels: list[str] = []
    release: str | None = None
    next_url: str | None = None
    while pages < max_pages:
        if pages == 0:
            text, response = http.get_text(
                ctx,
                SEARCH_URL,
                params=params,
                database=DATABASE,
                release_header=RELEASE_HEADER,
                error_code="UNIPROT",
            )
        else:
            text, response = http.get_text(
                ctx,
                next_url,
                database=DATABASE,
                release_header=RELEASE_HEADER,
                error_code="UNIPROT",
            )
        if response.dry_run:
            return {
                "query": str(query),
                "fields": list(fields),
                "rows": [],
                "n_total": None,
                "n_rows": 0,
                "truncated": False,
                "pages": 0,
                "release": None,
                "url": SEARCH_URL,
                "page_sha256": [],
                "dry_run": True,
            }
        pages += 1
        page_sha.append(_common.sha_of_response(response))
        release = release or _release_from(ctx, response, url=SEARCH_URL)
        if n_total is None:
            n_total = util.parse_int(
                (getattr(response, "headers", None) or {}).get("x-total-results")
            )
        page_rows = _parse_tsv_rows(text, fields)
        if page_rows and not column_labels:
            first_line = text.splitlines()[0] if text.splitlines() else ""
            column_labels = first_line.split("\t")
        rows.extend(page_rows)
        next_url = _next_link(response)
        if not next_url:
            break
        if n_total is not None and len(rows) >= n_total:
            break

    truncated = bool(next_url) and (n_total is None or len(rows) < (n_total or 0))
    if truncated:
        ctx.warn_top_hit_cap("uniprot", n_total)
        ctx.warn(
            "UNIPROT_SEARCH_TRUNCATED",
            f"UniProt search returned {len(rows)} of {n_total if n_total is not None else '?'} "
            f"matching entries across {pages} page(s); refine the query or raise max_pages.",
            severity="warning",
            context={
                "query": str(query),
                "rows": len(rows),
                "n_total": n_total,
                "pages": pages,
                "max_pages": max_pages,
            },
        )
    return {
        "query": str(query),
        "fields": list(fields),
        "column_labels": column_labels,
        "rows": rows,
        "n_total": n_total,
        "n_rows": len(rows),
        "truncated": truncated,
        "pages": pages,
        "release": release,
        "url": SEARCH_URL,
        "page_sha256": page_sha,
    }


# ---------------------------------------------------------------------------
# proteome discovery / download
# ---------------------------------------------------------------------------


def proteome_id(ctx: Any, taxid: int | str) -> dict[str, Any]:
    """Resolve the (reference) UniProt proteome identifier for a taxonomy id.

    Args:
        ctx: command context.
        taxid: NCBI taxonomy id, e.g. ``559292`` for S. cerevisiae S288C.

    Returns:
        ``{"proteome_id", "taxid", "name", "reference", "n_proteins", "release",
        "proteome_type", "superkingdom", "common_name", "modified", "url"}``.
        A reference proteome is preferred over other/excluded proteomes for the
        same organism, and an ambiguity warning is recorded when several
        reference candidates exist.

    Raises:
        InvalidInputError: the taxid is not a positive integer.
        NotFoundError: UniProt lists no proteome for the taxid.
        LifesciError: portal failure after the retry budget.
    """
    taxid_int = _coerce_taxid(taxid)
    if taxid_int is None:
        raise InvalidInputError("proteome_id requires a positive taxid")
    url = f"{PROTEOMES_BASE}/search"
    params = {"query": f"organism_id:{taxid_int}", "format": "json"}
    try:
        payload, response = http.get_json(
            ctx,
            url,
            params=params,
            database=DATABASE,
            release_header=RELEASE_HEADER,
            error_code="UNIPROT",
        )
    except LifesciError as err:
        if _common.is_http_status(err, 404):
            _raise_not_found(err, what="proteome", identifier=str(taxid_int), url=url)
        raise
    if response.dry_run:
        return {
            "proteome_id": None,
            "taxid": taxid_int,
            "name": None,
            "reference": None,
            "n_proteins": None,
            "release": None,
            "url": url,
            "dry_run": True,
        }
    results = _common.data_list(payload, "results")
    if not results:
        raise NotFoundError(
            f"UniProt has no proteome for taxid {taxid_int}",
            code="UNIPROT_PROTEOME_NOT_FOUND",
            details={"taxid": taxid_int, "url": url},
            fallbacks=[
                "Check that the taxid is a valid NCBI taxonomy id for a sequenced organism.",
                "Search proteomes by name at https://www.uniprot.org/proteomes.",
                "Some organisms only have non-reference/'redundant' proteomes.",
            ],
        )
    candidates = [item for item in results if isinstance(item, Mapping)]
    candidates.sort(
        key=lambda item: (
            0 if "reference" in str(item.get("proteomeType") or "").lower() else 1,
            -(_common.to_int(item.get("proteinCount")) or 0),
            str(item.get("id") or ""),
        )
    )
    chosen = candidates[0]
    if len(candidates) > 1:
        ctx.warn(
            "UNIPROT_MULTIPLE_PROTEOMES",
            f"UniProt lists {len(candidates)} proteomes for taxid {taxid_int}; the "
            f"{chosen.get('proteomeType') or 'first'} one ({chosen.get('id')}) was selected.",
            severity="info",
            context={
                "taxid": taxid_int,
                "candidates": [
                    {"proteome_id": item.get("id"), "type": item.get("proteomeType")}
                    for item in candidates[:10]
                ],
            },
        )
    taxonomy = chosen.get("taxonomy") or {}
    proteome_type = str(chosen.get("proteomeType") or "")
    return {
        "proteome_id": chosen.get("id"),
        "taxid": _common.to_int(taxonomy.get("taxonId")) or taxid_int,
        "name": taxonomy.get("scientificName") or chosen.get("id"),
        "common_name": taxonomy.get("commonName"),
        "reference": "reference" in proteome_type.lower(),
        "proteome_type": proteome_type,
        "n_proteins": _common.to_int(chosen.get("proteinCount")),
        "superkingdom": chosen.get("superkingdom"),
        "modified": chosen.get("modified"),
        "release": _release_from(ctx, response, url=url),
        "url": url,
    }


def _taxid_for_proteome(ctx: Any, proteome_id_value: str) -> int | None:
    """Best-effort lookup of a proteome's taxid (needed for the FTP filename)."""
    url = f"{PROTEOMES_BASE}/{proteome_id_value}"
    try:
        payload, _response = http.get_json(
            ctx,
            url,
            database=DATABASE,
            release_header=RELEASE_HEADER,
            error_code="UNIPROT",
        )
    except LifesciError:
        return None
    if not isinstance(payload, Mapping):
        return None
    return _common.to_int(_common.pick(payload, "taxonomy.taxonId"))


def _ftp_proteome_url(proteome_id_value: str, taxid: int) -> str:
    """Build the release-pinned EBI/UniProt FTP reference-proteome FASTA URL."""
    return (
        f"{FTP_PROTEOMES_BASE}/Eukaryota/{proteome_id_value}/"
        f"{proteome_id_value}_{taxid}.fasta.gz"
    )


def _proteome_not_found(
    ctx: Any,
    *,
    proteome_id_value: str,
    taxid: int,
    ftp_url: str,
    rest_error: LifesciError | None,
) -> NotFoundError:
    """Build the explicit 404 error for a non-reference/non-mirrored proteome."""
    return NotFoundError(
        f"UniProt reference-proteome FTP file not found for {proteome_id_value} "
        f"(taxid {taxid}): only reference proteomes are mirrored under "
        f"'reference_proteomes/Eukaryota', and the filename must match "
        f"{proteome_id_value}_{taxid}.fasta.gz",
        code="UNIPROT_PROTEOME_NOT_FOUND",
        details={
            "proteome_id": proteome_id_value,
            "taxid": taxid,
            "ftp_url": ftp_url,
            "rest_error": getattr(rest_error, "code", None),
        },
        fallbacks=[
            "Confirm this is the *reference* proteome for the organism "
            f"(lifesci.seq.fetch with proteomes=[{{'taxid': {taxid}}}] resolves it).",
            "Retry the REST stream endpoint (uniprotkb/stream) with backoff; 429s are often "
            "transient and the stream also serves non-reference proteomes.",
            "Choose a different proteome for the organism, or register a local FASTA dataset.",
        ],
    )


def fetch_proteome(
    ctx: Any,
    *,
    proteome_id: str,
    taxid: int | str | None = None,
    out_dir: str | Path,
) -> dict[str, Any]:
    """Download a proteome FASTA, preferring the REST stream and falling back to FTP.

    The REST stream (``uniprotkb/stream?query=proteome:<ID>&format=fasta&compressed=true``)
    is tried first; on rate limiting (HTTP 429), 5xx/transport failure, or an
    empty payload the adapter falls back to the release-pinned EBI/UniProt FTP
    reference-proteome file and records an explicit
    ``UNIPROT_STREAM_FALLBACK_FTP`` warning (never a silent source switch).  Only
    reference proteomes exist on FTP, so a 404 there raises
    :class:`NotFoundError` with fallback advice.

    Args:
        ctx: command context.
        proteome_id: UniProt proteome identifier, e.g. ``UP000005640``.
        taxid: NCBI taxonomy id of the proteome (needed to build the FTP path;
            resolved from the proteomes endpoint when omitted and fallback is
            required).  Strings are accepted for CLI convenience.
        out_dir: directory the decompressed FASTA is written to (created).

    Returns:
        ``{"path", "sha256", "bytes", "n_sequences", "release", "source",
        "reference_proteome", "url", "proteome_id", "taxid", "from_cache"}``.
        ``source`` is ``"rest"`` or ``"ftp"`` and always matches the URL used.

    Raises:
        NotFoundError: FTP 404 (non-reference proteome / wrong taxid).
        DataError: payload is not FASTA.
        LifesciError: portal failures after the retry budget.
    """
    proteome_id_value = _common.require_identifier(proteome_id, "proteome_id")
    taxid_int = _coerce_taxid(taxid)
    target_dir = Path(out_dir).expanduser()
    util.ensure_dir(target_dir)

    params: dict[str, Any] = {
        "query": f"proteome:{proteome_id_value}",
        "format": "fasta",
        "compressed": "true",
    }
    payload: bytes | None = None
    fasta_bytes: bytes | None = None
    n_sequences: int | None = None
    source: str | None = None
    release: str | None = None
    final_url = STREAM_URL
    from_cache = False
    rest_error: LifesciError | None = None
    fallback_reason: str | None = None

    # ---- 1. REST stream ------------------------------------------------
    try:
        data, response = http.get_bytes(
            ctx,
            STREAM_URL,
            params=params,
            database=DATABASE,
            release_header=RELEASE_HEADER,
            error_code="UNIPROT",
        )
        if response.dry_run:
            return {
                "path": None,
                "sha256": None,
                "bytes": None,
                "n_sequences": None,
                "release": None,
                "source": None,
                "reference_proteome": None,
                "url": STREAM_URL,
                "proteome_id": proteome_id_value,
                "taxid": taxid_int,
                "dry_run": True,
            }
        if data and data.strip():
            candidate = _common.decode_payload(
                data, label=f"UniProt proteome stream {proteome_id_value}"
            )
            candidate_count = _common.count_fasta_records(
                candidate, label=f"UniProt proteome stream {proteome_id_value}"
            )
            if candidate_count > 0:
                payload = data
                fasta_bytes = candidate
                n_sequences = candidate_count
                source = "rest"
                release = response.release
                final_url = response.final_url
                from_cache = bool(response.from_cache)
            else:
                fallback_reason = "the REST stream payload contained no FASTA records"
        else:
            fallback_reason = "the REST stream returned an empty payload"
            ctx.warn(
                "UNIPROT_STREAM_EMPTY",
                "UniProt REST stream returned an empty payload; falling back to the FTP "
                "reference-proteome file.",
                context={"proteome_id": proteome_id_value, "taxid": taxid_int},
            )
    except LifesciError as err:
        rest_error = err
        status = _common.http_status(err)
        if status in {400, 401, 403}:
            raise
        fallback_reason = f"REST stream failed with {err.code}"

    # ---- 2. FTP fallback ----------------------------------------------
    if payload is None:
        if taxid_int is None:
            taxid_int = _taxid_for_proteome(ctx, proteome_id_value)
        if taxid_int is None:
            raise InvalidInputError(
                f"cannot build the UniProt FTP reference-proteome path for "
                f"{proteome_id_value!r} without a taxid; pass taxid explicitly",
                code="UNIPROT_TAXID_REQUIRED",
                details={"proteome_id": proteome_id_value, "rest_error": getattr(rest_error, "code", None)},
                fallbacks=[
                    "Resolve the proteome first with lifesci.seq.fetch proteomes=[{taxid: …}].",
                    "Retry the REST stream endpoint later; it also serves non-reference proteomes.",
                ],
            )
        ftp_url = _ftp_proteome_url(proteome_id_value, taxid_int)
        try:
            data, response = http.get_bytes(
                ctx,
                ftp_url,
                database=DATABASE,
                error_code="UNIPROT",
            )
        except LifesciError as err:
            if _common.is_http_status(err, *tuple(_common.NOT_FOUND_STATUSES)):
                raise _proteome_not_found(
                    ctx,
                    proteome_id_value=proteome_id_value,
                    taxid=taxid_int,
                    ftp_url=ftp_url,
                    rest_error=rest_error,
                ) from err
            raise
        if response.dry_run:
            return {
                "path": None,
                "sha256": None,
                "bytes": None,
                "n_sequences": None,
                "release": None,
                "source": None,
                "reference_proteome": None,
                "url": ftp_url,
                "proteome_id": proteome_id_value,
                "taxid": taxid_int,
                "dry_run": True,
            }
        ctx.warn(
            "UNIPROT_STREAM_FALLBACK_FTP",
            f"UniProt REST proteome stream failed ({fallback_reason}); fell back to the FTP "
            f"reference-proteome FASTA {ftp_url}. Only reference proteomes are mirrored on FTP, "
            "and the FTP 'current_release' path moves as UniProt releases change.",
            context={
                "proteome_id": proteome_id_value,
                "taxid": taxid_int,
                "http_status": _common.http_status(rest_error) if rest_error else None,
                "rest_error": getattr(rest_error, "code", None),
                "ftp_url": ftp_url,
            },
        )
        payload = data
        source = "ftp"
        final_url = response.final_url
        from_cache = bool(response.from_cache)
        release = None
        ctx.warn(
            "DATABASE_RELEASE_UNKNOWN",
            "The FTP-fallback proteome FASTA does not carry a UniProt release header; record the "
            "retrieval date and verify the release via a REST call before comparing E-values.",
            context={"proteome_id": proteome_id_value, "url": ftp_url},
        )

    # ---- 3. decode / validate / write ---------------------------------
    if fasta_bytes is None:
        fasta_bytes = _common.decode_payload(
            payload or b"", label=f"UniProt proteome {proteome_id_value}"
        )
    if n_sequences is None:
        n_sequences = _common.count_fasta_records(
            fasta_bytes, label=f"UniProt proteome {proteome_id_value}"
        )
    if n_sequences == 0:
        raise DataError(
            f"UniProt proteome payload for {proteome_id_value} contained no sequences",
            code="UNIPROT_PROTEOME_EMPTY",
            details={"proteome_id": proteome_id_value, "taxid": taxid_int, "url": final_url},
        )
    filename = (
        f"{proteome_id_value}_{taxid_int}.fasta"
        if taxid_int is not None
        else f"{proteome_id_value}.fasta"
    )
    out_path = target_dir / filename
    util.atomic_write_bytes(out_path, fasta_bytes)
    sha256 = util.sha256_bytes(fasta_bytes)
    return {
        "path": str(out_path),
        "sha256": sha256,
        "bytes": len(fasta_bytes),
        "n_sequences": n_sequences,
        "release": release,
        "source": source,
        "reference_proteome": True if source == "ftp" else None,
        "url": final_url,
        "proteome_id": proteome_id_value,
        "taxid": taxid_int,
        "from_cache": from_cache,
        "name": None,
    }


# ---------------------------------------------------------------------------
# literature helper (used by the report command)
# ---------------------------------------------------------------------------


def citations_for(ctx: Any, accession: str) -> dict[str, Any]:
    """Return the literature citations attached to a UniProt entry.

    This is a thin convenience wrapper over :func:`fetch_entry` so the report
    command does not need to know the UniProt JSON layout.  Each citation keeps
    its PMID/DOI so literature claims can be graded ``[ESTABLISHED]`` with a
    citable identifier.

    Args:
        ctx: command context.
        accession: UniProtKB accession.

    Returns:
        ``{"accession", "citations", "n_citations", "release", "url"}`` where each
        citation is ``{"pmid", "doi", "title", "journal", "year"}``.
    """
    entry = fetch_entry(ctx, accession)
    parsed = entry.get("parsed") or {}
    references: Iterable[Mapping[str, Any]] = parsed.get("references") or []
    citations = [
        {
            "pmid": reference.get("pmid"),
            "doi": reference.get("doi"),
            "title": reference.get("title"),
            "journal": reference.get("journal"),
            "year": reference.get("year"),
        }
        for reference in references
    ]
    return {
        "accession": entry.get("accession") or accession,
        "citations": citations,
        "n_citations": len(citations),
        "release": entry.get("release"),
        "url": entry.get("url"),
    }


__all__ = [
    "DEFAULT_SEARCH_FIELDS",
    "FTP_PROTEOMES_BASE",
    "PROTEOMES_BASE",
    "RELEASE_HEADER",
    "SEARCH_URL",
    "STREAM_URL",
    "UNIPROTKB_BASE",
    "citations_for",
    "fetch_entry",
    "fetch_fasta",
    "fetch_proteome",
    "proteome_id",
    "search",
]
