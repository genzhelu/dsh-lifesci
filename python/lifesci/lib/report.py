"""Evidence-graded Markdown reports with a provenance table.

Every claim in a lifesci report **must** carry one of the evidence grades
``ESTABLISHED`` / ``INFERRED`` / ``UNKNOWN`` and at least one citable reference
(PMID/DOI/URL/title).  The builder refuses to emit a report whose claims lack
either, and it never re-grades a claim: a declared grade is rendered verbatim,
and a claim that carries a conflicting ``computed_grade`` raises an error rather
than being silently downgraded.

Supported ``sections`` shape (from the ``lifesci.report`` command)::

    [{"heading": "Example section",
      "intro": "optional paragraph",
      "claims": [{"id": "C1",
                  "text": "Example claim with a citation.",
                  "grade": "ESTABLISHED",
                  "citations": [{"doi": "10.1000/example.1",
                                 "title": "Example reference"}],
                  "note": "optional note"}]}]

``provenance_table`` is a list of envelope/provenance records
(``kind``, ``time``, ``url``/``path``, ``release``, ``sha256``, ...); when it is
``None`` the context's ``ctx.provenance`` list is used.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any, Mapping, Sequence

from ..errors import DataError, InvalidInputError
from ..util import ensure_dir, json_safe, now_iso, sha256_file
from .evidence import EVIDENCE_GRADES, grade_counts

_GRADE_BADGES = {
    "ESTABLISHED": "ESTABLISHED",
    "INFERRED": "INFERRED",
    "UNKNOWN": "UNKNOWN",
}
_CITATION_KEYS = ("pmid", "doi", "url", "title", "pmcid", "accession")


# ---------------------------------------------------------------------------
# validation
# ---------------------------------------------------------------------------


def _clean_citation(citation: Any) -> dict[str, Any] | None:
    if isinstance(citation, str):
        text = citation.strip()
        if not text:
            return None
        return {"title": text}
    if not isinstance(citation, Mapping):
        return None
    cleaned: dict[str, Any] = {}
    for key in _CITATION_KEYS:
        value = citation.get(key)
        if value not in (None, ""):
            cleaned[key] = str(value)
    return cleaned or None


def _format_citation(citation: Mapping[str, Any]) -> str:
    parts: list[str] = []
    if citation.get("pmid"):
        parts.append(f"PMID:{citation['pmid']}")
    if citation.get("pmcid"):
        parts.append(str(citation["pmcid"]))
    if citation.get("doi"):
        parts.append(f"doi:{citation['doi']}")
    if citation.get("url"):
        parts.append(str(citation["url"]))
    if citation.get("title"):
        parts.append(str(citation["title"]))
    return ", ".join(parts)


def _validate_claim(claim: Any, *, section_index: int, claim_index: int) -> dict[str, Any]:
    if not isinstance(claim, Mapping):
        raise InvalidInputError(
            f"claim {claim_index} in section {section_index} is not a mapping",
            details={"section": section_index, "claim": json_safe(claim)},
        )
    grade = str(claim.get("grade") or "").upper()
    claim_id = str(claim.get("id") or f"S{section_index + 1}.C{claim_index + 1}")
    if grade not in EVIDENCE_GRADES:
        raise DataError(
            f"EVIDENCE_GRADE_MISSING: claim {claim_id} has no valid evidence grade "
            f"(got {claim.get('grade')!r}; expected one of {list(EVIDENCE_GRADES)})",
            code="EVIDENCE_GRADE_MISSING",
            details={
                "claim": claim_id,
                "grade": claim.get("grade"),
                "allowed": list(EVIDENCE_GRADES),
            },
        )
    computed_grade = claim.get("computed_grade")
    if computed_grade is not None:
        computed = str(computed_grade).upper()
        if computed in EVIDENCE_GRADES and computed != grade:
            raise DataError(
                f"EVIDENCE_GRADE_DOWNGRADE: claim {claim_id} declares grade {grade} "
                f"but a computed grade {computed} was supplied; silent re-grading is "
                "refused — reconcile the evidence explicitly",
                code="EVIDENCE_GRADE_DOWNGRADE",
                details={"claim": claim_id, "grade": grade, "computed_grade": computed},
            )
    citations = [
        cleaned
        for cleaned in (
            _clean_citation(citation) for citation in claim.get("citations") or []
        )
        if cleaned
    ]
    if not citations:
        raise DataError(
            f"EVIDENCE_CITATION_MISSING: claim {claim_id} has no citable reference "
            "(need at least one of pmid/doi/url/title)",
            code="EVIDENCE_CITATION_MISSING",
            details={"claim": claim_id},
        )
    text = str(claim.get("text") or claim.get("claim") or "").strip()
    if not text:
        raise InvalidInputError(
            f"claim {claim_id} has no text", details={"claim": claim_id}
        )
    output: dict[str, Any] = {
        "id": claim_id,
        "text": text,
        "grade": grade,
        "citations": citations,
    }
    for extra in ("note", "value", "n", "method", "database", "date", "source"):
        if claim.get(extra) not in (None, ""):
            output[extra] = json_safe(claim[extra])
    return output


def _validate_sections(sections: Sequence[Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    validated_sections: list[dict[str, Any]] = []
    all_claims: list[dict[str, Any]] = []
    for section_index, raw_section in enumerate(sections or []):
        if isinstance(raw_section, str):
            raw_section = {"heading": None, "intro": raw_section, "claims": []}
        if not isinstance(raw_section, Mapping):
            raise InvalidInputError(
                f"report section {section_index} is not a mapping/string"
            )
        claims = [
            _validate_claim(claim, section_index=section_index, claim_index=claim_index)
            for claim_index, claim in enumerate(raw_section.get("claims") or [])
        ]
        all_claims.extend(claims)
        validated_sections.append(
            {
                "heading": None
                if raw_section.get("heading") is None
                else str(raw_section["heading"]),
                "intro": None
                if raw_section.get("intro") is None
                else str(raw_section["intro"]),
                "claims": claims,
            }
        )
    return validated_sections, all_claims


def _normalise_caveats(caveats: Any) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for caveat in caveats or []:
        if isinstance(caveat, str):
            output.append({"message": caveat})
        elif isinstance(caveat, Mapping):
            entry = {
                "code": None if caveat.get("code") is None else str(caveat["code"]),
                "message": str(caveat.get("message") or caveat.get("text") or ""),
            }
            if caveat.get("severity"):
                entry["severity"] = str(caveat["severity"])
            output.append(entry)
        else:
            raise InvalidInputError(
                f"unsupported caveat: {caveat!r}",
                details={"caveat": json_safe(caveat)},
            )
    return [entry for entry in output if entry["message"]]


# ---------------------------------------------------------------------------
# provenance table
# ---------------------------------------------------------------------------


def _source_of(record: Mapping[str, Any]) -> str:
    for key in ("url", "final_url", "path", "source_url"):
        value = record.get(key)
        if value:
            return str(value)
    argv = record.get("argv")
    if argv:
        return " ".join(str(part) for part in argv)
    return ""


def _cell(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).replace("|", "\\|").replace("\n", " ")
    return text


def provenance_rows(records: Sequence[Mapping[str, Any]] | None) -> list[dict[str, Any]]:
    """Normalise provenance records into deterministic table rows."""
    rows: list[dict[str, Any]] = []
    for record in records or []:
        if not isinstance(record, Mapping):
            continue
        digest = record.get("sha256")
        if not digest and record.get("payload_sha256"):
            digest = record["payload_sha256"]
        rows.append(
            {
                "kind": str(record.get("kind") or "record"),
                "database": str(
                    record.get("database") or record.get("tool") or record.get("name") or ""
                ),
                "release": str(record.get("release") or ""),
                "source": _source_of(record),
                "sha256": str(digest or ""),
                "bytes": record.get("bytes"),
                "time": str(record.get("time") or record.get("retrieved_at") or ""),
            }
        )
    return rows


def _provenance_markdown(rows: Sequence[Mapping[str, Any]]) -> list[str]:
    lines = [
        "| # | kind | database/tool | release | source | sha256 | bytes | time |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for index, row in enumerate(rows, 1):
        lines.append(
            "| {index} | {kind} | {database} | {release} | {source} | {sha256} | "
            "{bytes} | {time} |".format(
                index=index,
                kind=_cell(row.get("kind")),
                database=_cell(row.get("database")),
                release=_cell(row.get("release")),
                source=_cell(row.get("source")),
                sha256=_cell(row.get("sha256")),
                bytes=_cell(row.get("bytes")),
                time=_cell(row.get("time")),
            )
        )
    return lines


# ---------------------------------------------------------------------------
# report build
# ---------------------------------------------------------------------------


def _claim_markdown(claim: Mapping[str, Any], number: int) -> list[str]:
    citations = "; ".join(
        _format_citation(citation) for citation in claim["citations"]
    )
    lines = [
        f"{number}. **{_GRADE_BADGES[claim['grade']]}** — {claim['text']} "
        f"[{citations}]"
    ]
    details: list[str] = []
    for key, label in (
        ("value", "value"),
        ("n", "n"),
        ("method", "method"),
        ("database", "database/version"),
        ("date", "date"),
    ):
        if claim.get(key) not in (None, ""):
            details.append(f"{label}: {_cell(claim[key])}")
    if claim.get("note"):
        details.append(f"note: {_cell(claim['note'])}")
    if details:
        lines.append("    - " + "; ".join(details))
    return lines


def build_report(
    ctx: Any,
    *,
    title: str,
    sections: Sequence[Any],
    provenance_table: Sequence[Mapping[str, Any]] | None = None,
    caveats: Sequence[Any] | None = None,
    out_path: str | Path,
    json_path: str | Path | None = None,
    subtitle: str | None = None,
    generated_at: str | None = None,
) -> dict[str, Any]:
    """Assemble an evidence-graded Markdown report plus its JSON companion.

    Parameters
    ----------
    ctx:
        :class:`lifesci.context.Context` (used for provenance when
        ``provenance_table`` is not supplied) or ``None``.
    title:
        Report title (rendered as ``# title``).
    sections:
        ``[{"heading", "intro"?, "claims": [{"text"/"claim", "grade",
        "citations", ...}]}]`` — see module docstring.
    provenance_table:
        Envelope provenance records rendered as a Markdown table; defaults to
        ``ctx.provenance``.
    caveats:
        List of strings or ``{"code","message","severity"?}`` mappings.
    out_path:
        Markdown output path.
    json_path:
        JSON companion path (default: ``out_path`` with ``.json`` suffix).
    subtitle, generated_at:
        Optional subtitle line and deterministic generation timestamp (defaults
        to ``util.now_iso()``; pass an explicit value for reproducible output).

    Returns
    -------
    dict
        ``{"markdown_path", "json_path", "n_claims",
        "grades": {"ESTABLISHED": n, "INFERRED": n, "UNKNOWN": n},
        "provenance_rows", "n_sections"}``.
    """
    if not title or not str(title).strip():
        raise InvalidInputError("report needs a non-empty title")
    validated_sections, claims = _validate_sections(sections)
    normalised_caveats = _normalise_caveats(caveats)
    rows = provenance_rows(
        provenance_table
        if provenance_table is not None
        else (getattr(ctx, "provenance", None) if ctx is not None else None)
    )
    timestamp = generated_at or now_iso()
    target = Path(out_path)
    json_target = (
        Path(json_path) if json_path is not None else target.with_suffix(".json")
    )

    lines: list[str] = [f"# {title}", ""]
    if subtitle:
        lines.extend([str(subtitle), ""])
    lines.extend(
        [
            f"*Generated: {timestamp}*",
            "",
        ]
    )
    if not claims:
        lines.extend(["*No evidence-graded claims in this report.*", ""])
    claim_counter = 0
    for section in validated_sections:
        if section["heading"]:
            lines.extend([f"## {section['heading']}", ""])
        if section["intro"]:
            lines.extend([section["intro"], ""])
        if section["claims"]:
            for claim in section["claims"]:
                claim_counter += 1
                lines.extend(_claim_markdown(claim, claim_counter))
            lines.append("")
    lines.extend(["## Caveats", ""])
    if normalised_caveats:
        for caveat in normalised_caveats:
            prefix = f"**{caveat['code']}** — " if caveat.get("code") else ""
            lines.append(f"- {prefix}{caveat['message']}")
    else:
        lines.append("- None recorded.")
    lines.extend(["", "## Provenance", ""])
    if rows:
        lines.extend(_provenance_markdown(rows))
    else:
        lines.append("- No provenance records supplied.")
    markdown = "\n".join(lines) + "\n"

    ensure_dir(target.parent)
    target.write_text(markdown, encoding="utf-8")
    grades = grade_counts(claim["grade"] for claim in claims)
    json_payload = {
        "title": str(title),
        "subtitle": subtitle,
        "generated_at": timestamp,
        "sections": validated_sections,
        "n_claims": len(claims),
        "grades": grades,
        "caveats": normalised_caveats,
        "provenance": rows,
    }
    ensure_dir(json_target.parent)
    json_target.write_text(
        json_dumps(json_payload), encoding="utf-8"
    )
    sha_md = sha256_file(target)
    sha_json = sha256_file(json_target)
    if ctx is not None and hasattr(ctx, "provenance"):
        for path, digest in ((target, sha_md), (json_target, sha_json)):
            try:
                ctx.provenance.append(
                    {
                        "kind": "local_file",
                        "path": str(path),
                        "sha256": digest,
                        "bytes": path.stat().st_size,
                        "tool": "lifesci.report",
                        "time": now_iso(),
                    }
                )
            except OSError:  # pragma: no cover
                pass
    return {
        "markdown_path": str(target),
        "json_path": str(json_target),
        "n_claims": len(claims),
        "grades": grades,
        "provenance_rows": len(rows),
        "n_sections": len(validated_sections),
        "markdown_sha256": sha_md,
        "json_sha256": sha_json,
    }


def json_dumps(value: Any) -> str:
    """Deterministic pretty JSON used for the report companion file."""
    from ..util import dumps_pretty

    return dumps_pretty(value)
