"""``lifesci.orthology`` — family/orthogroup lookup with an explicit cross-check table.

Queries several independent sources for the same protein and reports where they
agree, disagree, or say nothing:

* **PANTHER** — family entry (PTHR…) via the InterPro entry API, with the UniProt
  PANTHER cross-reference as fallback.
* **OrthoDB** — orthogroup (``…at<taxid>``) via the OrthoDB API, using the
  OrthoDB identifier exposed as a UniProt cross-reference.
* **eggNOG** — eggNOG orthologous-group identifier exposed as a UniProt
  cross-reference (the mapper service is not called).
* **OMA** — OMA group/HOG via the OMA browser API.

A source that is missing from the UniProt cross-references, is down, or does not
cover the protein is reported as ``available: false`` with a reason and a
warning — it is never fatal.  Agreement is computed from the family vocabulary
each source carries (PANTHER family name, OrthoDB description, eggNOG/OMA
labels) against the UniProt protein/gene name, or against an explicit
``expected_family``.  Disagreement is surfaced as a warning **and** a human-review
flag; a single agreeing source is only ``unknown`` confidence, never proof.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Iterable, Mapping

from .. import provenance, util
from ..commands.base import command
from ..errors import InvalidInputError, LifesciError, ToolMissingError

#: Descriptive words that never identify a family on their own.
_STOPWORDS = frozenset(
    {
        "protein",
        "putative",
        "uncharacterized",
        "domain",
        "containing",
        "family",
        "homolog",
        "homologue",
        "subunit",
        "large",
        "small",
        "repeat",
        "region",
        "like",
        "related",
        "isoform",
        "fragment",
    }
)

UNIPROT_ENTRY_URL = "https://rest.uniprot.org/uniprotkb/{accession}.json"
INTERPRO_URL = "https://www.ebi.ac.uk/interpro/api/entry/all/protein/uniprot/{accession}/"
OMA_URL = "https://omabrowser.org/api/protein/{accession}/"
ORTHODB_URL = "https://data.orthodb.org/v102/ogroup/{og_id}"

INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["accessions"],
    "properties": {
        "accessions": {
            "type": "array",
            "minItems": 1,
            "items": {"type": "string", "minLength": 1},
            "description": (
                "UniProt accessions to annotate. Each protein is queried across the selected "
                "orthology sources; missing sources are warnings, not errors."
            ),
        },
        "sources": {
            "type": "array",
            "items": {"type": "string", "enum": ["panther", "orthodb", "eggnog", "oma"]},
            "default": ["panther", "orthodb", "eggnog", "oma"],
            "description": (
                "Which orthology sources to query. PANTHER/eggNOG come from UniProt cross-references "
                "and the InterPro entry API; OrthoDB and OMA come from their own APIs."
            ),
        },
        "expected_family": {
            "type": "string",
            "description": (
                "Optional family label to test every source against. When omitted, the expected "
                "family is derived from the UniProt protein/gene name."
            ),
        },
        "page_size": {
            "type": "integer",
            "minimum": 1,
            "maximum": 200,
            "default": 50,
            "description": "InterPro entry-API page size used for the PANTHER lookup.",
        },
        "max_pages": {
            "type": "integer",
            "minimum": 1,
            "maximum": 20,
            "default": 2,
            "description": "Maximum InterPro result pages fetched per protein.",
        },
        "out": {
            "type": "string",
            "description": "Optional directory to write the orthology record JSON (per accession).",
        },
    },
}


def _resolve_path(ctx: Any, value: str) -> Path:
    target = Path(str(value)).expanduser()
    if not target.is_absolute():
        target = Path(ctx.cwd) / target
    return target.resolve()


def _record_local_file(ctx: Any, path: Path, *, kind: str, notes: Mapping[str, Any] | None = None) -> None:
    log = getattr(ctx, "_provenance_log", None)
    if log is None:
        log = provenance.ProvenanceLog(getattr(ctx, "log_path", None))
        ctx._provenance_log = log
    ctx.provenance.append(
        log.local_file(
            path=str(path),
            sha256=util.sha256_file(path),
            bytes_=path.stat().st_size,
            kind=kind,
            notes=dict(notes or {}),
        )
    )


def _plan(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
    accessions = [str(a) for a in (args.get("accessions") or [])]
    page_size = int(args.get("page_size", 50))
    planned: list[dict[str, Any]] = []
    for accession in accessions:
        planned.append({"method": "GET", "url": UNIPROT_ENTRY_URL.format(accession=accession), "purpose": "uniprot_entry"})
        planned.append(
            {
                "method": "GET",
                "url": INTERPRO_URL.format(accession=accession),
                "params": {"page_size": page_size},
                "purpose": "interpro_entries",
            }
        )
        planned.append({"method": "GET", "url": OMA_URL.format(accession=accession), "purpose": "oma_protein"})
        planned.append(
            {
                "method": "GET",
                "url": ORTHODB_URL.format(og_id="<resolved-from-uniprot-xref>"),
                "purpose": "orthodb_ogroup",
                "note": "orthogroup id resolved at runtime from the UniProt OrthoDB cross-reference",
            }
        )
    return {
        "command": "orthology",
        "dry_run": True,
        "requested": {"accessions": accessions, "sources": list(args.get("sources") or ["panther", "orthodb", "eggnog", "oma"])},
        "planned_requests": planned,
        "proteins": [],
        "cross_checks": [],
        "resolved": {
            "dry_run": True,
            "cache_dir": str(ctx.cache_dir),
            "n_proteins": len(accessions),
            "n_agree": 0,
            "n_disagree": 0,
            "n_unknown": 0,
        },
        "errors": [],
    }


def _cross_references(parsed: Mapping[str, Any] | None) -> list[dict[str, str]]:
    references: list[dict[str, str]] = []
    for item in (parsed or {}).get("cross_references") or []:
        if isinstance(item, Mapping):
            database = item.get("db") or item.get("database")
            identifier = item.get("id") or item.get("identifier")
        elif isinstance(item, (list, tuple)) and len(item) >= 2:
            database, identifier = item[0], item[1]
        else:  # pragma: no cover - defensive
            continue
        if database and identifier:
            references.append({"db": str(database), "id": str(identifier)})
    return references


def _xref_ids(references: Iterable[Mapping[str, str]], *names: str) -> list[str]:
    wanted = {name.lower() for name in names}
    found: list[str] = []
    for reference in references:
        database = str(reference.get("db") or "").lower()
        if any(database == name or database.startswith(name + ":") or database.startswith(name + "_") for name in wanted):
            identifier = str(reference.get("id") or "").strip()
            if identifier and identifier not in found:
                found.append(identifier)
    return found


def _claim_tokens(claims: Iterable[str]) -> set[str]:
    """Flatten one source's claims into comparable lowercased word tokens."""
    tokens: set[str] = set()
    for claim in claims:
        text = str(claim)
        if text.startswith("text:"):
            tokens.update(part for part in text[5:].split("|") if part)
        elif text:
            tokens.add(text.lower())
    return tokens


def _family_claims(texts: Iterable[str]) -> set[str]:
    """Families claimed by a source's free text (normalised significant tokens)."""
    joined = " ".join(str(text or "") for text in texts).lower()
    tokens = {
        token
        for token in re.findall(r"[a-z0-9]{4,}", joined)
        if token not in _STOPWORDS
    }
    if not tokens:
        return set()
    return {"text:" + "|".join(sorted(tokens)[:6])}


def _agreement(reference: set[str], claim: set[str], available: bool) -> str:
    if not available or not claim:
        return "unknown"
    reference_tokens = _claim_tokens(reference)
    claim_tokens = _claim_tokens(claim)
    if reference_tokens and claim_tokens:
        return "agree" if reference_tokens & claim_tokens else "disagree"
    return "unknown"


def _source_record(
    ctx: Any,
    accession: str,
    source: str,
    *,
    available: bool,
    reason: str | None,
    labels: Iterable[str] = (),
    identifiers: Iterable[str] = (),
    details: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    claim = _family_claims(labels)
    unique_identifiers: list[str] = []
    for identifier in identifiers:
        text = str(identifier)
        if text and text not in unique_identifiers:
            unique_identifiers.append(text)
    return {
        "source": source,
        "available": bool(available),
        "reason": reason,
        "identifiers": unique_identifiers,
        "labels": [str(label) for label in labels if str(label or "").strip()],
        "claim": sorted(claim),
        "details": util.json_safe(dict(details or {})),
    }


def _fetch_panther(
    ctx: Any, accession: str, references: list[dict[str, str]], *, page_size: int, max_pages: int
) -> dict[str, Any]:
    identifiers = _xref_ids(references, "panther")
    labels: list[str] = []
    details: dict[str, Any] = {"cross_reference_ids": identifiers, "entries": []}
    try:
        from ..adapters import interpro

        scan = interpro.entry_all_for_protein(ctx, accession, page_size=page_size, max_pages=max_pages)
        for entry in scan.get("entries") or []:
            if str(entry.get("source_database") or "").lower() != "panther":
                continue
            entry_accession = str(entry.get("entry_accession") or "")
            entry_name = str(entry.get("entry_name") or "")
            if entry_accession:
                identifiers.append(entry_accession)
            if entry_name:
                labels.append(entry_name)
            details["entries"].append(
                {
                    "entry_accession": entry_accession,
                    "entry_name": entry_name,
                    "entry_type": entry.get("entry_type"),
                    "n_locations": len(entry.get("locations") or []),
                }
            )
            details["release"] = details.get("release") or scan.get("release")
            details["url"] = details.get("url") or scan.get("url")
    except (LifesciError, ImportError) as err:
        code = getattr(err, "code", "ADAPTER_MISSING")
        message = getattr(err, "message", str(err))
        return _source_record(
            ctx,
            accession,
            "panther",
            available=bool(identifiers),
            reason=None if identifiers else code,
            labels=labels,
            identifiers=identifiers,
            details={**details, "error": message},
        )
    labels = [label for label in labels if label.strip()]
    return _source_record(
        ctx,
        accession,
        "panther",
        available=bool(identifiers or labels),
        reason=None if (identifiers or labels) else "no_panther_annotation",
        labels=labels,
        identifiers=identifiers,
        details=details,
    )


def _fetch_orthodb(ctx: Any, accession: str, references: list[dict[str, str]]) -> dict[str, Any]:
    identifiers = _xref_ids(references, "orthodb", "ortho_db")
    og_id = identifiers[0] if identifiers else None
    details: dict[str, Any] = {"cross_reference_ids": identifiers}
    if not og_id:
        return _source_record(
            ctx,
            accession,
            "orthodb",
            available=False,
            reason="no_uniprot_cross_reference",
            details=details,
        )
    try:
        from ..adapters import orthodb

        record = orthodb.orthogroup(ctx, og_id)
    except (LifesciError, ImportError) as err:
        return _source_record(
            ctx,
            accession,
            "orthodb",
            available=False,
            reason=getattr(err, "code", "ADAPTER_MISSING"),
            identifiers=[og_id],
            details={**details, "error": getattr(err, "message", str(err))},
        )
    description = str(record.get("description") or "")
    details.update(
        {
            "og_id": record.get("og_id") or og_id,
            "level_taxid": record.get("level_taxid"),
            "n_genes": len(record.get("genes") or []) if isinstance(record.get("genes"), list) else record.get("n_genes"),
            "url": record.get("url"),
            "release": record.get("release"),
        }
    )
    return _source_record(
        ctx,
        accession,
        "orthodb",
        available=True,
        reason=None,
        labels=[description] if description else [],
        identifiers=[str(record.get("og_id") or og_id)],
        details=details,
    )


def _fetch_eggnog(ctx: Any, accession: str, references: list[dict[str, str]]) -> dict[str, Any]:
    identifiers = _xref_ids(references, "eggnog")
    if not identifiers:
        return _source_record(
            ctx,
            accession,
            "eggnog",
            available=False,
            reason="no_uniprot_cross_reference",
        )
    return _source_record(
        ctx,
        accession,
        "eggnog",
        available=True,
        reason=None,
        identifiers=identifiers,
        details={"note": "eggNOG orthologous group from the UniProt cross-reference; no mapper call was made"},
    )


def _fetch_oma(ctx: Any, accession: str) -> dict[str, Any]:
    try:
        from ..adapters import oma

        record = oma.protein(ctx, accession)
    except (LifesciError, ImportError) as err:
        return _source_record(
            ctx,
            accession,
            "oma",
            available=False,
            reason=getattr(err, "code", "ADAPTER_MISSING"),
            details={"error": getattr(err, "message", str(err))},
        )
    identifiers = [str(record.get(key)) for key in ("oma_group", "oma_hog") if record.get(key)]
    labels = [str(record.get(key)) for key in ("description", "oma_hog_description") if record.get(key)]
    return _source_record(
        ctx,
        accession,
        "oma",
        available=True,
        reason=None,
        labels=labels,
        identifiers=identifiers,
        details={
            "oma_group": record.get("oma_group"),
            "oma_hog": record.get("oma_hog"),
            "url": record.get("url"),
            "release": record.get("release"),
        },
    )


def _cross_checks_for(accession: str, sources: Mapping[str, dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    names = sorted(sources)
    for index, left_name in enumerate(names):
        for right_name in names[index + 1 :]:
            left = sources[left_name]
            right = sources[right_name]
            left_claim = set(left.get("claim") or [])
            right_claim = set(right.get("claim") or [])
            if not left.get("available") or not right.get("available"):
                agreement = "unknown"
                detail = f"{left_name} or {right_name} unavailable"
            elif not left_claim or not right_claim:
                agreement = "unknown"
                detail = "at least one source carries no family label to compare"
            else:
                left_tokens = _claim_tokens(left_claim)
                right_tokens = _claim_tokens(right_claim)
                shared = sorted(left_tokens & right_tokens)
                if shared:
                    agreement = "agree"
                    detail = f"shared family token(s): {shared}"
                elif left_tokens and right_tokens:
                    agreement = "disagree"
                    detail = f"{left_name}={sorted(left_tokens)} vs {right_name}={sorted(right_tokens)}"
                else:
                    agreement = "unknown"
                    detail = "free-text comparison inconclusive"
            rows.append(
                {
                    "accession": accession,
                    "a": left_name,
                    "b": right_name,
                    "agreement": agreement,
                    "detail": detail,
                }
            )
    return rows


@command(
    name="orthology",
    title="Cross-check protein orthology sources",
    summary="Query PANTHER, OrthoDB, eggNOG and OMA and report an agree/disagree/unknown cross-check table.",
    description=(
        "Look up family/orthogroup membership for one or more proteins across four independent "
        "sources and arbitrate them explicitly: PANTHER (via the InterPro entry API and UniProt "
        "cross-references), OrthoDB (orthogroup API), eggNOG (UniProt cross-reference; no mapper "
        "call) and OMA (OMA browser API). Returns per-source records with identifiers, labels, "
        "availability and reason, a pairwise cross-check table with agree/disagree/unknown, and an "
        "overall agreement per protein. Missing/unreachable sources are warnings, not errors; a "
        "single agreeing source is reported as unknown confidence, and any disagreement is flagged "
        "for human review. Caveats: identifier namespaces are not interchangeable across databases, "
        "agreement here means the sources carry consistent family vocabulary, not that orthology is "
        "proven; divergent fast-evolving families can be annotated by only one source."
    ),
    input_schema=INPUT_SCHEMA,
    output_schema="orthology.schema.json",
    category="sequence",
    timeout_ms=300_000,
    danger="network",
    examples=(
        {"accessions": ["P69905"]},
        {"accessions": ["P69905", "P00698"], "expected_family": "globin"},
    ),
)
def run(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
    """Query orthology sources and build a cross-check table."""
    ctx.validate()
    accessions = [str(item).strip() for item in (args.get("accessions") or []) if str(item).strip()]
    if not accessions:
        raise InvalidInputError("'accessions' must contain at least one UniProt accession")
    requested_sources = list(args.get("sources") or ["panther", "orthodb", "eggnog", "oma"])
    unknown_sources = sorted(set(requested_sources) - {"panther", "orthodb", "eggnog", "oma"})
    if unknown_sources:
        raise InvalidInputError(f"unknown orthology source(s): {unknown_sources}")
    if ctx.dry_run:
        return _plan(args, ctx)

    try:
        from ..adapters import uniprot
    except ImportError as err:  # pragma: no cover - parallel workstream ordering
        raise ToolMissingError(
            "lifesci.adapters.uniprot is not available; orthology cross-references cannot be resolved",
            details={"error": str(err)},
            fallbacks=["retry after the adapters workstream lands", "use lifesci.http for the UniProt entry JSON"],
        ) from err

    page_size = int(args.get("page_size", 50))
    max_pages = int(args.get("max_pages", 2))
    expected_family = str(args.get("expected_family") or "").strip().lower() or None
    out_dir = _resolve_path(ctx, str(args["out"])) if args.get("out") else None
    if out_dir is not None:
        util.ensure_dir(out_dir)

    proteins: list[dict[str, Any]] = []
    cross_checks: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    first_error: LifesciError | None = None
    unreachable_sources: set[str] = set()

    for accession in accessions:
        try:
            entry = uniprot.fetch_entry(ctx, accession)
        except LifesciError as err:
            errors.append({"accession": accession, "code": err.code, "message": err.message})
            first_error = first_error or err
            continue
        parsed = entry.get("parsed") or {}
        references = _cross_references(parsed)
        protein_name = str(parsed.get("protein_name") or "")
        genes = [str(gene.get("gene") or gene.get("name") or "") for gene in (parsed.get("genes") or []) if isinstance(gene, Mapping)]
        organism = parsed.get("organism") or {}
        reference_text = " ".join([protein_name, *genes])
        if expected_family:
            # An explicit expected family replaces the derived claim, so a source
            # carrying a different family vocabulary is reported as disagreement.
            reference_claim = _family_claims([expected_family]) or {
                "text:" + str(expected_family).strip().lower()
            }
        else:
            reference_claim = _family_claims([reference_text])

        sources: dict[str, dict[str, Any]] = {}
        try:
            if "panther" in requested_sources:
                sources["panther"] = _fetch_panther(
                    ctx, accession, references, page_size=page_size, max_pages=max_pages
                )
            if "orthodb" in requested_sources:
                sources["orthodb"] = _fetch_orthodb(ctx, accession, references)
            if "eggnog" in requested_sources:
                sources["eggnog"] = _fetch_eggnog(ctx, accession, references)
            if "oma" in requested_sources:
                sources["oma"] = _fetch_oma(ctx, accession)
        finally:
            pass

        for source_name in requested_sources:
            record = sources.get(source_name)
            if record is None:
                continue
            if not record.get("available"):
                unreachable_sources.add(source_name)
                ctx.warn(
                    "ORTHOLOGY_SOURCE_UNAVAILABLE",
                    f"{source_name} lookup unavailable for {accession} ({record.get('reason')}); "
                    "missing evidence is not absence of orthology.",
                    context={"accession": accession, "source": source_name, "reason": record.get("reason")},
                )
            agreement = _agreement(reference_claim, set(record.get("claim") or []), bool(record.get("available")))
            record["agreement"] = agreement
            record["expected_claim"] = sorted(reference_claim)

        per_source = {name: sources[name] for name in requested_sources if name in sources}
        checks = _cross_checks_for(accession, per_source)
        cross_checks.extend(checks)

        informative = [record["agreement"] for record in per_source.values() if record.get("available")]
        if "disagree" in informative:
            overall = "disagree"
            ctx.warn(
                "ORTHOLOGY_DISAGREEMENT",
                f"Orthology sources disagree for {accession}; arbitrate with domain/family evidence "
                "and inspect each source's identifiers before using either call.",
                context={
                    "accession": accession,
                    "sources": {
                        name: {"agreement": record["agreement"], "claim": record.get("claim"), "identifiers": record.get("identifiers")}
                        for name, record in per_source.items()
                    },
                },
            )
            ctx.review(
                "ORTHOLOGY_DISAGREEMENT_REVIEW",
                f"{accession}: orthology sources disagree; manual arbitration required.",
                context={"accession": accession, "checks": [c for c in checks if c["agreement"] == "disagree"]},
            )
        elif sum(1 for value in informative if value == "agree") >= 2:
            overall = "agree"
        elif sum(1 for value in informative if value == "agree") == 1:
            overall = "unknown"
            ctx.review(
                "SINGLE_WEAK_LINE_EVIDENCE",
                f"{accession}: only one orthology source agrees; treat the family call as weak.",
                context={"accession": accession},
            )
        else:
            overall = "unknown"

        protein = {
            "accession": entry.get("accession") or accession,
            "protein_name": protein_name or None,
            "genes": [gene for gene in genes if gene],
            "organism": util.json_safe(organism),
            "release": entry.get("release"),
            "url": entry.get("url"),
            "expected_claim": sorted(reference_claim),
            "agreement": overall,
            "panther": per_source.get("panther"),
            "orthodb": per_source.get("orthodb"),
            "eggnog": per_source.get("eggnog"),
            "oma": per_source.get("oma"),
            "cross_checks": checks,
        }
        proteins.append(protein)
        if out_dir is not None:
            output_path = out_dir / f"{util.slugify(str(protein['accession']), max_length=80, fallback='protein')}.orthology.json"
            util.atomic_write_json(output_path, protein)
            _record_local_file(ctx, output_path, kind="orthology_record", notes={"accession": protein["accession"]})

    if accessions and not proteins:
        if first_error is not None:
            raise first_error
        raise LifesciError(
            "orthology produced no results for any requested accession",
            code="ORTHOLOGY_FAILED",
            details={"errors": errors},
        )
    if errors:
        ctx.warn(
            "ORTHOLOGY_PARTIAL",
            f"{len(errors)} accession(s) could not be annotated; inspect errors[].",
            context={"errors": errors[:10]},
        )

    releases = provenance.load_releases(ctx.cache_dir)
    agree = sum(1 for protein in proteins if protein["agreement"] == "agree")
    disagree = sum(1 for protein in proteins if protein["agreement"] == "disagree")
    unknown = sum(1 for protein in proteins if protein["agreement"] == "unknown")
    return {
        "dry_run": False,
        "requested": {"accessions": accessions, "sources": requested_sources, "expected_family": expected_family},
        "proteins": proteins,
        "cross_checks": cross_checks,
        "resolved": {
            "cache_dir": str(ctx.cache_dir),
            "out_dir": str(out_dir) if out_dir else None,
            "n_proteins": len(proteins),
            "n_agree": agree,
            "n_disagree": disagree,
            "n_unknown": unknown,
            "sources_requested": requested_sources,
            "sources_unavailable": sorted(unreachable_sources),
            "releases": releases,
        },
        "errors": errors,
    }
