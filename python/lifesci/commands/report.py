"""``lifesci.report`` — assemble an evidence-graded Markdown report with provenance.

Every scientific claim in a lifesci report must carry an explicit evidence grade
(``ESTABLISHED`` / ``INFERRED`` / ``UNKNOWN``) and, for graded claims, at least
one citation (PMID / DOI / URL).  The command **refuses** to produce a report
when a claim has no grade (``EVIDENCE_GRADE_MISSING``) or a non-UNKNOWN claim has
no citation (``EVIDENCE_CITATION_MISSING``) — an ungraded claim could silently be
read as established, which is exactly the failure mode evidence grading guards
against.  Grades are never silently downgraded.

The report includes:

* the title and per-section claims rendered with grade tags and citations;
* an explicit caveats list;
* a provenance table built from the command envelope's provenance records plus
  any extra records supplied in the input (URLs, hashes, release dates, local
  files, tool runs), so a reviewer can re-derive every number.

Output is deterministic for identical input.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .. import util
from ..commands.base import command
from ..errors import ConfigError, DataError, InvalidInputError, UsageError

INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "title": {
            "type": "string",
            "description": "Report title.",
        },
        "sections": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["heading"],
                "properties": {
                    "heading": {"type": "string", "description": "Section heading."},
                    "intro": {"type": "string", "description": "Optional introductory paragraph."},
                    "claims": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["text"],
                            "properties": {
                                "id": {
                                    "type": "string",
                                    "description": "Optional short claim id (e.g. 'C1') used in cross-references.",
                                },
                                "text": {
                                    "type": "string",
                                    "description": (
                                        "The claim. Quantitative claims must state value, n, test, "
                                        "measure definition, database/version, and date."
                                    ),
                                },
                                "grade": {
                                    "type": "string",
                                    "enum": ["ESTABLISHED", "INFERRED", "UNKNOWN"],
                                    "description": (
                                        "Evidence grade. ESTABLISHED: directly shown by data/literature; "
                                        "INFERRED: supported by indirect or single-line evidence; UNKNOWN: "
                                        "not determined (explicitly unresolved). Missing/invalid grades "
                                        "are refused with EVIDENCE_GRADE_MISSING."
                                    ),
                                },
                                "citations": {
                                    "type": "array",
                                    "items": {
                                        "type": "object",
                                        "additionalProperties": False,
                                        "properties": {
                                            "pmid": {"type": "string", "description": "PubMed ID."},
                                            "doi": {"type": "string", "description": "DOI."},
                                            "url": {"type": "string", "description": "Any other stable URL (e.g. PDB entry)."},
                                            "title": {"type": "string", "description": "Optional citation title."},
                                        },
                                    },
                                    "description": (
                                        "Citations (PMID/DOI/URL) for the claim. Required for every "
                                        "claim, including UNKNOWN claims (cite the observation that "
                                        "leaves the question unresolved)."
                                    ),
                                },
                                "note": {
                                    "type": "string",
                                    "description": "Optional reviewer note (e.g. why a claim is INFERRED).",
                                },
                            },
                        },
                        "description": "Claims in this section (may be empty for an intro-only section).",
                    },
                },
            },
            "description": "Report sections, each with graded claims.",
        },
        "caveats": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Explicit caveats/limitations (e.g. 'no detectable homolog is not proof of "
                "absence', 'correlation is not causation'). Recommended; a warning is emitted "
                "when empty."
            ),
        },
        "provenance": {
            "type": "array",
            "items": {"type": "object", "additionalProperties": True},
            "description": "Additional provenance records (kind/time/url/sha256/release...) to include in the table.",
        },
        "include_envelope_provenance": {
            "type": "boolean",
            "default": True,
            "description": "Include the current command envelope's provenance records in the report provenance table.",
        },
        "generated_at": {
            "type": "string",
            "description": (
                "Deterministic generation timestamp rendered in the report. Default: the "
                "latest timestamp among the included provenance records, else 'not-recorded'. "
                "Pass an explicit ISO-8601 value for reproducible report bytes."
            ),
        },
        "out": {
            "type": "string",
            "description": "Markdown output path (default: <ctx.cwd>/lifesci-report.md).",
        },
    },
    "required": ["title", "sections"],
}

_GRADE_ORDER = ("ESTABLISHED", "INFERRED", "UNKNOWN")


def _import_lib() -> tuple[Any, Any]:
    import importlib

    try:
        evidence = importlib.import_module("lifesci.lib.evidence")
        report_lib = importlib.import_module("lifesci.lib.report")
    except ImportError as err:  # pragma: no cover - parallel-workstream guard
        raise ConfigError(
            "report generation requires python/lifesci/lib/report.py and lib/evidence.py, "
            "which are not available in this checkout",
            details={"import_error": f"{type(err).__name__}: {err}"},
        ) from err
    return evidence, report_lib


def _validate_sections(sections: list[dict[str, Any]]) -> dict[str, Any]:
    """Validate every claim's grade/citations; return counts by grade."""
    counts = {grade: 0 for grade in _GRADE_ORDER}
    seen_ids: set[str] = set()
    for section_index, section in enumerate(sections):
        heading = str(section.get("heading") or "")
        if not heading:
            raise InvalidInputError(
                f"section #{section_index + 1} has an empty heading",
                details={"section_index": section_index},
            )
        for claim_index, claim in enumerate(section.get("claims") or []):
            path = f"sections[{section_index}].claims[{claim_index}]"
            if not isinstance(claim, dict):
                raise InvalidInputError(f"{path} must be an object", details={"path": path})
            grade = claim.get("grade")
            if grade not in _GRADE_ORDER:
                raise UsageError(
                    f"{path}: evidence grade is missing or invalid ({grade!r}); every claim must "
                    "be graded ESTABLISHED, INFERRED, or UNKNOWN. Refusing to emit an ungraded claim.",
                    code="EVIDENCE_GRADE_MISSING",
                    details={"path": path, "grade": grade, "allowed": list(_GRADE_ORDER)},
                )
            counts[grade] += 1
            text = str(claim.get("text") or "").strip()
            if not text:
                raise InvalidInputError(f"{path}: claim text is empty", details={"path": path})
            citations = claim.get("citations")
            usable = (
                [
                    citation
                    for citation in citations
                    if isinstance(citation, dict)
                    and any(str(citation.get(key) or "").strip() for key in ("pmid", "doi", "url"))
                ]
                if isinstance(citations, list)
                else []
            )
            if not usable:
                raise UsageError(
                    f"{path}: every claim needs at least one citation with pmid, doi, or url "
                    f"(grade {grade}). Refusing to emit an uncited claim.",
                    code="EVIDENCE_CITATION_MISSING",
                    details={"path": path, "grade": grade},
                )
            claim_id = claim.get("id")
            if claim_id:
                claim_id = str(claim_id)
                if claim_id in seen_ids:
                    raise InvalidInputError(
                        f"duplicate claim id {claim_id!r}", details={"claim_id": claim_id}
                    )
                seen_ids.add(claim_id)
    return counts


def _resolve_out(ctx: Any, value: Any) -> Path:
    if value:
        target = Path(str(value)).expanduser()
        if not target.is_absolute():
            target = (Path(ctx.cwd) / target).resolve()
        return target
    return (Path(ctx.cwd) / "lifesci-report.md").resolve()


def _ensure_provenance_timestamps(ctx: Any) -> None:
    """Fill ``time`` on provenance records appended directly by library helpers."""
    for entry in getattr(ctx, "provenance", []) or []:
        if isinstance(entry, dict) and "time" not in entry:
            entry["time"] = util.now_iso()


def _record_output(ctx: Any, path: str | Path, *, role: str) -> None:
    from .. import provenance as provenance_mod

    target = Path(path)
    if not target.is_file():
        return
    log = getattr(ctx, "_provenance_log", None)
    if log is None:
        log = provenance_mod.ProvenanceLog(ctx.log_path)
        ctx._provenance_log = log
    entry = log.local_file(
        path=target, sha256=util.sha256_file(target), bytes_=target.stat().st_size, notes={"role": role}
    )
    ctx.provenance.append(entry)


@command(
    name="report",
    title="Assemble an evidence-graded Markdown report",
    summary="Render graded claims (ESTABLISHED/INFERRED/UNKNOWN + PMID/DOI) with caveats and a provenance table.",
    description=(
        "Builds a reproducible Markdown report from structured, evidence-graded claims. Every "
        "claim MUST carry a grade (ESTABLISHED, INFERRED, UNKNOWN) and ESTABLISHED/INFERRED "
        "claims MUST carry at least one PMID/DOI/URL citation; the command refuses otherwise "
        "(EVIDENCE_GRADE_MISSING / EVIDENCE_CITATION_MISSING) and never silently downgrades a "
        "grade. Output includes the per-section claims, an explicit caveats list, and a "
        "provenance table assembled from the command envelope provenance (HTTP fetches with "
        "URL/SHA-256/release, local file hashes, tool runs) plus any records supplied in "
        "'provenance'. Deterministic for identical input. Quantitative claims should state "
        "value, n, test, measure definition, database/version, and date."
    ),
    input_schema=INPUT_SCHEMA,
    output_schema="report.schema.json",
    category="reporting",
    timeout_ms=120_000,
    examples=(
        {
            "title": "Example evidence-graded report",
            "sections": [
                {
                    "heading": "Example section",
                    "claims": [
                        {
                            "id": "C1",
                            "text": "Example claim supported by one citation.",
                            "grade": "ESTABLISHED",
                            "citations": [{"doi": "10.1000/example.1", "title": "Example reference"}],
                        }
                    ],
                }
            ],
            "caveats": ["Example caveat: correlation is not causation."],
        },
    ),
    danger="write",
)
def run(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
    ctx.validate()
    evidence_lib, report_lib = _import_lib()
    allowed_grades = tuple(getattr(evidence_lib, "EVIDENCE_GRADES", _GRADE_ORDER))
    title = str(args.get("title") or "").strip()
    if not title:
        raise InvalidInputError("report requires a non-empty title")
    sections = args.get("sections")
    if not isinstance(sections, list) or not sections:
        raise InvalidInputError("report requires a non-empty 'sections' array")
    grade_counts = _validate_sections(sections)
    for grade in grade_counts:
        if grade not in allowed_grades:  # pragma: no cover - defensive
            raise UsageError(
                f"grade {grade!r} is not part of EVIDENCE_GRADES {allowed_grades}",
                code="EVIDENCE_GRADE_MISSING",
            )

    caveats = [str(caveat) for caveat in (args.get("caveats") or [])]
    if not caveats:
        ctx.warn(
            "CAVEATS_MISSING",
            "The report was assembled with no caveats list; list known limitations explicitly "
            "(absence-not-proof, sample size, phylogenetic non-independence, method assumptions).",
        )

    provenance_table: list[dict[str, Any]] = []
    if bool(args.get("include_envelope_provenance", True)):
        provenance_table.extend(util.json_safe(list(ctx.provenance or [])))
    supplied = args.get("provenance") or []
    for record in supplied:
        if isinstance(record, dict):
            provenance_table.append(util.json_safe(dict(record)))
    if not provenance_table:
        ctx.warn(
            "PROVENANCE_TABLE_EMPTY",
            "No provenance records were available for the report; the reviewer cannot re-derive "
            "the claims from recorded URLs/hashes/releases alone.",
        )
    out_path = _resolve_out(ctx, args.get("out"))
    n_claims = sum(grade_counts.values())

    if bool(ctx.dry_run):
        ctx.warn(
            "REPORT_DRY_RUN",
            "Report was validated but not written because --dry-run was set.",
            severity="info",
        )
        return {
            "markdown_path": None,
            "json_path": None,
            "out": str(out_path),
            "title": title,
            "n_claims": n_claims,
            "grades": grade_counts,
            "caveats": caveats,
            "provenance_table": provenance_table,
            "dry_run": True,
        }

    generated_at = args.get("generated_at")
    if not generated_at:
        timestamps = [
            str(record.get("time") or record.get("started_at") or "")
            for record in provenance_table
            if isinstance(record, dict)
        ]
        timestamps = [value for value in timestamps if value]
        generated_at = max(timestamps) if timestamps else "not-recorded"
    result = report_lib.build_report(
        ctx,
        title=title,
        sections=sections,
        provenance_table=provenance_table,
        caveats=caveats,
        out_path=str(out_path),
        generated_at=str(generated_at),
    )
    result = util.json_safe(result) or {}
    markdown_path = result.get("markdown_path") or result.get("path")
    json_path = result.get("json_path")
    if not markdown_path:
        raise DataError(
            "lib.report.build_report did not return a markdown_path",
            code="REPORT_BUILD_FAILED",
        )
    if Path(str(markdown_path)).is_dir():  # pragma: no cover - defensive
        markdown_path = str(Path(str(markdown_path)) / "report.md")
    _record_output(ctx, str(markdown_path), role="report_markdown")
    if json_path:
        _record_output(ctx, str(json_path), role="report_json")
    grades = result.get("grades") or grade_counts
    if not isinstance(grades, dict):
        grades = grade_counts
    _ensure_provenance_timestamps(ctx)
    payload: dict[str, Any] = {
        "markdown_path": str(markdown_path),
        "json_path": str(json_path) if json_path else None,
        "title": title,
        "n_claims": int(result.get("n_claims") or n_claims),
        "grades": {str(key): int(value) for key, value in grades.items()},
        "caveats": caveats,
        "provenance_table": provenance_table,
        "n_provenance": len(provenance_table),
        "generated_at": str(generated_at),
        "dry_run": False,
    }
    return payload
