"""Domain-based presence/absence curation and evidence grading.

Pfam profile-HMM hits are curated per factor into ``present`` / ``weak`` /
``absent`` calls with an exact, auditable evidence string.

Model-level call
----------------
``present`` when the best model E-value ``< 1e-3``, ``weak`` when
``1e-3 <= E < 1.0``, ``absent`` otherwise.  Hits with ``E >= 1.0`` are not
reported hits for curation purposes (searches may report at ``E=10`` but
curation only consumes hits below ``1.0``).

Factor-level curation
---------------------
Factors are **caller-supplied**: there are no built-in factor signatures.  A
factor definition is a mapping with a non-empty ``models`` list (the Pfam/HMM
accessions that define the factor) and optional validation constraints:

* ``length_min`` / ``length_max`` — protein-length window; applied only when
  present in the definition;
* ``present_coverage`` / ``weak_coverage`` — minimum model coverage for a
  present / weak call; applied only when present;
* ``min_significant_models`` — how many significant signature models a single
  protein must carry for a present call (default 1).

Generic rules when no constraint is declared: any significant model hit
(``E < 1e-3``) => ``present``; any marginal hit (``1e-3 <= E < 1``) => ``weak``;
none => ``absent``.

Evidence strings are deterministic and audit-friendly, e.g.::

    PF00042 E=2.2e-30 len=142 mcov=0.991 (P69905)
    significant domain(s) ['PF00042', 'PF00043']; len=147 (P00698)
    marginal evidence ['PF00042'] len=147 (P00698)
    only implausible hit(s) ['PF00042'] len=147 (P00698)
    no domain hit

Input flexibility
-----------------
:func:`curate_cells` accepts **either**

* a list of cells ``{"organism", "factor", "hits": [...]}`` where ``hits`` are
  per-model hit records (for one organism x factor), or
* a flat list of hit rows carrying ``organism`` and a ``pfam_model``/``model``
  column; rows are grouped by ``(organism, factor)`` using the caller-supplied
  factor signatures.

Each hit record's accepted keys: ``protein``/``accession``/``best_hit``
(accession string; ``sp|ACC|NAME``-style names are also parsed),
``model``/``pfam_model``,
``evalue``, ``score``, ``model_cov``/``coverage``, ``protein_length``,
``description`` and optional ``name``.  A cell may also carry an optional
``sequence_hit`` ``{"protein", "evalue", "coverage"}`` (sequence-search
evidence is reported but never overrides domain evidence).
"""

from __future__ import annotations

import math
import re
from typing import Any, Iterable, Mapping, Sequence

from ..errors import DataError, InvalidInputError
from ..util import fmt_evalue, json_safe

#: E-value below which a model hit is considered significant ("present").
MODEL_PRESENT_EVALUE: float = 1e-3
#: E-value below which a model hit is at least marginal ("weak"); hits with a
#: larger E-value are not curated at all.
MODEL_WEAK_EVALUE: float = 1.0

EVIDENCE_GRADES: tuple[str, ...] = ("ESTABLISHED", "INFERRED", "UNKNOWN")

#: Built-in factor name -> domain-signature definition.  Intentionally empty:
#: factors and their models are supplied by the caller (``factor_models`` /
#: ``factor_signatures``), so the library carries no organism- or family-specific
#: assumptions of its own.
FACTOR_SIGNATURES: dict[str, dict[str, Any]] = {}

#: All signature model accessions declared by the built-in signatures.
SIGNATURE_MODELS: tuple[str, ...] = tuple(
    model
    for factor in FACTOR_SIGNATURES.values()
    for model in factor.get("models", [])
)

_ACCESSION_RE = re.compile(r"^(?:sp|tr)\|([^|]+)\|")
_MODEL_VERSION_RE = re.compile(r"\.\d+$")
_WEAK_HEADER = "marginal evidence"


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------


def _as_float(value: Any) -> float | None:
    """Parse a float from a number or numeric string (``NA`` -> ``None``)."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text or text.upper() in {"NA", "N/A", "NAN", "NONE", "-"}:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _as_int(value: Any) -> int | None:
    number = _as_float(value)
    return None if number is None else int(number)


def _model_key(value: Any) -> str | None:
    """Normalise a model identifier (strip a ``.25`` version suffix)."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return _MODEL_VERSION_RE.sub("", text)


def _accession_from_name(name: Any) -> str | None:
    if not name:
        return None
    match = _ACCESSION_RE.match(str(name))
    return match.group(1) if match else None


def _fmt_length(length: int | None) -> str:
    return "NA" if length is None else str(int(length))


def _fmt_cov(coverage: float | None) -> str:
    """Format model coverage with three decimal places (``0.0`` for missing)."""
    value = 0.0 if coverage is None else round(float(coverage), 3)
    return str(value)


def _merged_factor_signatures(
    overrides: Mapping[str, Mapping[str, Any]] | None,
) -> dict[str, dict[str, Any]]:
    """Return the built-in signatures updated with caller-supplied definitions.

    Caller definitions must carry a non-empty ``models`` list; every other key
    (``length_min``, ``length_max``, ``present_coverage``, ``weak_coverage``,
    ``min_significant_models``, free-text metadata) is optional and is applied
    only when present.
    """
    merged: dict[str, dict[str, Any]] = {
        name: {
            key: (list(value) if isinstance(value, list) else value)
            for key, value in signature.items()
        }
        for name, signature in FACTOR_SIGNATURES.items()
    }
    for name, override in (overrides or {}).items():
        base = dict(merged.get(name, {}))
        base.update(override)
        models = [
            str(model).split(".")[0]
            for model in (base.get("models") or [])
            if str(model).strip()
        ]
        if not models:
            raise InvalidInputError(
                f"factor signature {name!r} needs a non-empty 'models' list"
            )
        base["models"] = models
        merged[str(name)] = base
    return merged


def _factor_for_model(
    model: str | None, signatures: Mapping[str, Mapping[str, Any]]
) -> str | None:
    for factor, signature in signatures.items():
        if model in signature.get("models", []):
            return factor
    return None


def _length_ok(signature: Mapping[str, Any], length: int | None) -> bool:
    """Apply the optional protein-length window (always true if not declared)."""
    if "length_min" not in signature and "length_max" not in signature:
        return True
    if length is None:
        return False
    minimum = signature.get("length_min")
    maximum = signature.get("length_max")
    if minimum is not None and int(length) < int(minimum):
        return False
    if maximum is not None and int(length) > int(maximum):
        return False
    return True


def _coverage_ok(
    signature: Mapping[str, Any], coverage: float | None, key: str
) -> bool:
    """Apply one optional coverage cutoff (always true if not declared)."""
    threshold = signature.get(key)
    if threshold is None:
        return True
    return coverage is not None and float(coverage) >= float(threshold)


def _normalise_hit(hit: Mapping[str, Any]) -> dict[str, Any] | None:
    """Normalise one per-model hit record; return ``None`` if it is not a hit."""
    evalue = _as_float(hit.get("evalue"))
    if evalue is None:
        return None
    accession = (
        hit.get("protein")
        or hit.get("accession")
        or hit.get("best_hit")
        or hit.get("hit")
    )
    name = hit.get("name")
    if not accession:
        accession = _accession_from_name(name) or (str(name) if name else None)
    if not accession:
        return None
    model = _model_key(hit.get("model") or hit.get("pfam_model"))
    return {
        "protein": str(accession),
        "name": str(name) if name else str(accession),
        "model": model,
        "evalue": evalue,
        "score": _as_float(hit.get("score")),
        "model_cov": _as_float(hit.get("model_cov", hit.get("coverage"))),
        "protein_length": _as_int(hit.get("protein_length")),
        "description": str(hit.get("description") or ""),
    }


def _merge_hits_into_candidates(
    hits: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Group hit records by protein, keeping the best hit per model.

    Only hits with ``E < 1.0`` are curated; proteins are keyed by accession;
    the lowest-E hit wins per model.
    """
    merged: dict[str, dict[str, Any]] = {}
    for hit in hits:
        normalised = _normalise_hit(hit)
        if normalised is None or normalised["model"] is None:
            continue
        if normalised["evalue"] >= MODEL_WEAK_EVALUE:
            continue
        accession = normalised["protein"]
        candidate = merged.setdefault(
            accession,
            {
                "accession": accession,
                "description": normalised["description"],
                "protein_length": normalised["protein_length"],
                "models": {},
            },
        )
        # Keep the first non-empty description / length we see.
        if not candidate["description"] and normalised["description"]:
            candidate["description"] = normalised["description"]
        if candidate["protein_length"] is None and normalised["protein_length"] is not None:
            candidate["protein_length"] = normalised["protein_length"]
        model = normalised["model"]
        current = candidate["models"].get(model)
        if current is None or normalised["evalue"] < current["evalue"]:
            candidate["models"][model] = normalised
    candidates = list(merged.values())
    for candidate in candidates:
        candidate["n_significant"] = sum(
            1
            for hit in candidate["models"].values()
            if hit["evalue"] < MODEL_PRESENT_EVALUE
        )
    return candidates


def _support(candidate: Mapping[str, Any], model: str | None = None) -> dict[str, Any]:
    """Best (lowest-E) model hit supporting a candidate protein."""
    models: Mapping[str, Mapping[str, Any]] = candidate["models"]
    if model is None or model not in models:
        model = min(models, key=lambda key: models[key]["evalue"])
    hit = models[model]
    return {
        "accession": candidate.get("accession"),
        "model": model,
        "evalue": hit["evalue"],
        "score": hit.get("score"),
        "model_cov": hit.get("model_cov"),
        "description": candidate.get("description") or hit.get("description") or "",
        "protein_length": candidate.get("protein_length"),
    }


def _candidates_for_factor(
    factor: str,
    all_candidates: Sequence[Mapping[str, Any]],
    signatures: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    signature_models = set(signatures[factor].get("models", []))
    candidates: list[dict[str, Any]] = []
    for candidate in all_candidates:
        factor_models = {
            model: hit
            for model, hit in candidate["models"].items()
            if model in signature_models
        }
        if not factor_models:
            continue
        candidates.append(
            {
                "accession": candidate["accession"],
                "description": candidate.get("description") or "",
                "protein_length": candidate.get("protein_length"),
                "models": factor_models,
            }
        )
    candidates.sort(
        key=lambda candidate: (
            -sum(
                1
                for hit in candidate["models"].values()
                if hit["evalue"] < MODEL_PRESENT_EVALUE
            ),
            min(hit["evalue"] for hit in candidate["models"].values()),
        )
    )
    return candidates


# ---------------------------------------------------------------------------
# model-level and factor-level curation
# ---------------------------------------------------------------------------


def model_verdict(evalue: float | None) -> str:
    """Call one model hit: ``present``, ``weak`` or ``absent`` (see module doc)."""
    if evalue is None or not math.isfinite(float(evalue)):
        return "absent"
    value = float(evalue)
    if value < MODEL_PRESENT_EVALUE:
        return "present"
    if value < MODEL_WEAK_EVALUE:
        return "weak"
    return "absent"


def _curate_generic(
    signature: Mapping[str, Any],
    candidates: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Curate one factor with the generic signature rules.

    Only constraints that exist in ``signature`` are applied; a signature with
    just a ``models`` list accepts any significant hit as present and any
    marginal hit as weak.
    """
    if not candidates:
        return {"verdict": "absent", "evidence": "no domain hit", "best": None}
    min_significant = int(signature.get("min_significant_models", 1) or 1)
    present_coverage = signature.get("present_coverage")
    weak_coverage = signature.get("weak_coverage", present_coverage)

    def significant_models(candidate: Mapping[str, Any]) -> list[str]:
        return sorted(
            model
            for model, hit in candidate["models"].items()
            if hit["evalue"] < MODEL_PRESENT_EVALUE
        )

    def marginal_models(candidate: Mapping[str, Any]) -> list[str]:
        return sorted(
            model
            for model, hit in candidate["models"].items()
            if MODEL_PRESENT_EVALUE <= hit["evalue"] < MODEL_WEAK_EVALUE
        )

    def accepted_significant(candidate: Mapping[str, Any]) -> list[str]:
        """Significant models that also pass the optional coverage cutoff."""
        return sorted(
            model
            for model in significant_models(candidate)
            if _coverage_ok(signature, candidate["models"][model].get("model_cov"), "present_coverage")
        )

    # Pass 1: a present call.
    for candidate in candidates:
        significant = significant_models(candidate)
        if len(significant) < min_significant:
            continue
        if not _length_ok(signature, candidate["protein_length"]):
            continue
        accepted = accepted_significant(candidate)
        if len(accepted) < min_significant:
            continue
        if len(significant) == 1 and len(signature.get("models", [])) == 1:
            model = significant[0]
            hit = candidate["models"][model]
            evidence = (
                f"{model} E={fmt_evalue(hit['evalue'])} "
                f"len={_fmt_length(candidate['protein_length'])} "
                f"mcov={_fmt_cov(hit.get('model_cov'))} ({candidate['accession']})"
            )
            return {
                "verdict": "present",
                "evidence": evidence,
                "best": _support(candidate, model),
            }
        return {
            "verdict": "present",
            "evidence": (
                f"significant domain(s) {significant}; "
                f"len={_fmt_length(candidate['protein_length'])} "
                f"({candidate['accession']})"
            ),
            "best": _support(candidate),
        }

    # Pass 2: weak/uncertain evidence: marginal hits, or significant hits that
    # fall short of the caller's validation constraints.
    for candidate in candidates:
        marginal = marginal_models(candidate)
        significant = significant_models(candidate)
        marginal_accepted = [
            model
            for model in marginal
            if _coverage_ok(signature, candidate["models"][model].get("model_cov"), "weak_coverage")
        ]
        length_ok = _length_ok(signature, candidate["protein_length"])
        if marginal_accepted and length_ok:
            return {
                "verdict": "weak",
                "evidence": (
                    f"{_WEAK_HEADER} {sorted(significant + marginal)} "
                    f"len={_fmt_length(candidate['protein_length'])} "
                    f"({candidate['accession']})"
                ),
                "best": _support(candidate),
            }
        if significant and (not length_ok or len(accepted_significant(candidate)) < min_significant):
            return {
                "verdict": "weak",
                "evidence": (
                    f"{_WEAK_HEADER} {sorted(significant + marginal)} "
                    f"len={_fmt_length(candidate['protein_length'])} "
                    f"({candidate['accession']})"
                ),
                "best": _support(candidate),
            }

    # Pass 3: nothing accepted.
    best_candidate = candidates[0]
    return {
        "verdict": "absent",
        "evidence": (
            f"only implausible hit(s) {sorted(best_candidate['models'])} "
            f"len={_fmt_length(best_candidate['protein_length'])} "
            f"({best_candidate['accession']})"
        ),
        "best": _support(best_candidate),
    }


def curate_factor(
    factor: str,
    candidates: Sequence[Mapping[str, Any]],
    *,
    factor_signatures: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Curate one factor from already-merged protein candidates.

    ``candidates`` is the output-shaped structure of
    :func:`_merge_hits_into_candidates` (one entry per protein with a
    ``models`` mapping).  Prefer :func:`curate_cells` for public use; this
    helper is exposed for tests and advanced callers.
    """
    signatures = _merged_factor_signatures(factor_signatures)
    if factor not in signatures:
        raise InvalidInputError(
            f"unknown factor {factor!r}; known factors: {sorted(signatures)}",
            details={"factor": factor, "known": sorted(signatures)},
        )
    signature = signatures[factor]
    ordered = _candidates_for_factor(factor, candidates, signatures)
    result = _curate_generic(signature, ordered)
    result["factor"] = factor
    result["models"] = sorted(
        {
            model
            for candidate in ordered
            for model in candidate["models"]
        }
    )
    result["n_candidates"] = len(ordered)
    return result


# ---------------------------------------------------------------------------
# cell / matrix curation
# ---------------------------------------------------------------------------


def _cell_key(cell: Mapping[str, Any]) -> tuple[str, str]:
    organism = str(cell.get("organism") or cell.get("org") or "").strip()
    factor = str(cell.get("factor") or "").strip()
    if not organism or not factor:
        raise InvalidInputError(
            "each presence cell needs 'organism' and 'factor'",
            details={"cell": json_safe(dict(cell))},
        )
    return organism, factor


def _looks_like_flat_row(record: Mapping[str, Any]) -> bool:
    """Heuristic: a flat per-model hit row rather than an organism x factor cell."""
    if "organism" not in record:
        return False
    if "hits" in record:
        return False
    model = record.get("pfam_model") or record.get("model")
    return model is not None and "evalue" in record


def _normalise_cells(
    cells: Sequence[Mapping[str, Any]] | Mapping[str, Any],
    signatures: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Normalise cell-shaped or flat-row input into ordered cell dicts."""
    if isinstance(cells, Mapping):
        cells = [cells]
    records = list(cells)
    if not records:
        return []
    flat = all(_looks_like_flat_row(record) for record in records)
    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    if flat:
        for row in records:
            model = _model_key(row.get("pfam_model") or row.get("model"))
            factor = row.get("factor") or _factor_for_model(model, signatures)
            if not factor:
                continue  # hit on a non-signature model: not a presence row
            organism = str(row.get("organism") or row.get("org") or "").strip()
            if not organism:
                raise InvalidInputError(
                    "presence rows need an 'organism' column",
                    details={"row": json_safe(dict(row))},
                )
            key = (organism, str(factor))
            cell = grouped.setdefault(
                key,
                {
                    "organism": organism,
                    "factor": str(factor),
                    "hits": [],
                    "taxid": row.get("taxid"),
                    "proteome": row.get("proteome"),
                },
            )
            cell["hits"].append(dict(row))
        return list(grouped.values())

    for cell in records:
        organism, factor = _cell_key(cell)
        if factor not in signatures:
            raise InvalidInputError(
                f"unknown factor {factor!r} in presence cell; known: {sorted(signatures)}",
                details={"organism": organism, "factor": factor},
            )
        normalised = {
            "organism": organism,
            "factor": factor,
            "hits": [dict(hit) for hit in cell.get("hits") or []],
        }
        for extra in ("taxid", "proteome", "sequence_hit"):
            if extra in cell:
                normalised[extra] = cell[extra]
        grouped[key := (organism, factor)] = normalised  # noqa: F841 - keep order
    return list(grouped.values())


def sequence_evidence_warning(
    sequence_hit: Mapping[str, Any] | None,
    *,
    domain_verdict: str,
) -> list[dict[str, Any]]:
    """Warn when sequence and domain evidence disagree (domain evidence wins)."""
    if not sequence_hit or domain_verdict != "present":
        return []
    evalue = _as_float(sequence_hit.get("evalue"))
    coverage = _as_float(sequence_hit.get("coverage"))
    if evalue is not None and evalue < MODEL_PRESENT_EVALUE and (coverage or 0) >= 0.3:
        return []
    return [
        {
            "code": "DOMAIN_OUTRANKS_SEQUENCE",
            "severity": "warning",
            "message": (
                "Domain evidence calls the factor present while the single-sequence "
                "search evidence is absent or marginal; highly divergent homologues "
                "are routinely missed by single-sequence search (domain/family "
                "evidence takes precedence over a single weak sequence hit)."
            ),
            "context": json_safe(dict(sequence_hit)),
        }
    ]


def presence_warnings(
    cell_or_verdict: Mapping[str, Any] | str,
    *,
    factor: str | None = None,
    organism: str | None = None,
) -> list[dict[str, Any]]:
    """Return the mandatory cautionary warnings for one curated cell.

    * ``ABSENCE_NOT_PROOF`` — an ``absent`` call means "no accepted candidate",
      never proof of absence.
    * ``SINGLE_WEAK_LINE_EVIDENCE`` — a ``weak`` call rests on a single,
      marginal line of evidence and is flagged for human review.
    """
    if isinstance(cell_or_verdict, Mapping):
        verdict = str(cell_or_verdict.get("verdict") or "")
        factor = str(cell_or_verdict.get("factor") or factor or "factor")
        organism = str(cell_or_verdict.get("organism") or organism or "this organism")
        evidence = str(cell_or_verdict.get("evidence") or "")
        sequence_hit = cell_or_verdict.get("sequence_hit")
    else:
        verdict = str(cell_or_verdict)
        factor = str(factor or "factor")
        organism = str(organism or "this organism")
        evidence = ""
        sequence_hit = None
    warnings: list[dict[str, Any]] = []
    if verdict == "absent":
        warnings.append(
            {
                "code": "ABSENCE_NOT_PROOF",
                "severity": "warning",
                "message": (
                    f"No accepted candidate for {factor} in {organism}"
                    + (f" ({evidence})" if evidence else "")
                    + "; report this as 'no detectable homolog', not as proven "
                    "absence — domain/sequence searches miss extremely divergent "
                    "proteins."
                ),
                "context": {"organism": organism, "factor": factor},
            }
        )
    elif verdict == "weak":
        warnings.append(
            {
                "code": "SINGLE_WEAK_LINE_EVIDENCE",
                "severity": "required",
                "message": (
                    f"{factor} in {organism} is called from a single weak line of "
                    f"evidence ({evidence or 'marginal hit'}); treat the call as "
                    "uncertain and human-review it."
                ),
                "context": {"organism": organism, "factor": factor},
            }
        )
    warnings.extend(
        sequence_evidence_warning(sequence_hit, domain_verdict=verdict)
    )
    return warnings


def _curate_one_cell(
    cell: Mapping[str, Any],
    signatures: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    organism, factor = _cell_key(cell)
    candidates = _merge_hits_into_candidates(cell.get("hits") or [])
    result = curate_factor(factor, candidates, factor_signatures=signatures)
    best = result.get("best") or {}
    output: dict[str, Any] = {
        "organism": organism,
        "factor": factor,
        "verdict": result["verdict"],
        "evidence": result["evidence"],
        "best_hit": best.get("accession"),
        "evalue": best.get("evalue"),
        "model": best.get("model"),
        "score": best.get("score"),
        "model_cov": best.get("model_cov"),
        "protein_length": best.get("protein_length"),
        "description": best.get("description") or "",
        "models": result.get("models", []),
        "n_candidates": result.get("n_candidates", 0),
        "best": best or None,
    }
    for extra in ("taxid", "proteome", "sequence_hit"):
        if extra in cell:
            output[extra] = cell[extra]
    warnings = presence_warnings(output)
    output["warnings"] = warnings
    human_review = [w for w in warnings if w.get("severity") == "required"]
    for review in result.get("review") or []:
        if review not in human_review:
            human_review.append(review)
    output["human_review"] = human_review
    return output


def curate_cells(
    cells: Sequence[Mapping[str, Any]] | Mapping[str, Any],
    *,
    factor_signatures: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Curate presence/absence cells into a presence matrix.

    Parameters
    ----------
    cells:
        Either a list of ``{"organism", "factor", "hits": [...]}`` cells or a
        flat list of per-model hit rows (with ``organism`` and
        ``pfam_model``/``model`` columns), which are grouped by
        ``(organism, factor)`` using the supplied factor signatures.
    factor_signatures:
        Mapping of ``factor -> {"models": [...], ...optional constraints}``.
        There are no built-in factors, so factors must be supplied here.

    Returns
    -------
    dict
        ``{"cells": [...], "matrix": {organism: {factor: verdict}},
        "n_present", "n_weak", "n_absent", "warnings", "human_review"}``.
        Each curated cell carries ``organism``, ``factor``, ``verdict``,
        ``evidence`` (exact evidence string), ``best_hit``, ``evalue``,
        ``model``, ``models``, ``score``, ``model_cov``, ``protein_length``,
        ``description``, ``n_candidates``, ``warnings`` and ``human_review``.
    """
    signatures = _merged_factor_signatures(factor_signatures)
    normalised = _normalise_cells(cells, signatures)
    curated: list[dict[str, Any]] = []
    matrix: dict[str, dict[str, str]] = {}
    counts = {"present": 0, "weak": 0, "absent": 0}
    for cell in normalised:
        output = _curate_one_cell(cell, signatures)
        curated.append(output)
        matrix.setdefault(output["organism"], {})[output["factor"]] = output["verdict"]
        counts[output["verdict"]] += 1
    warnings: list[dict[str, Any]] = []
    human_review: list[dict[str, Any]] = []
    for output in curated:
        for warning in output["warnings"]:
            if warning not in warnings:
                warnings.append(warning)
        for review in output["human_review"]:
            if review not in human_review:
                human_review.append(review)
    # Factor presence per organism summary, deterministically ordered.
    counts_by_factor: dict[str, dict[str, int]] = {}
    for output in curated:
        bucket = counts_by_factor.setdefault(
            output["factor"], {"present": 0, "weak": 0, "absent": 0}
        )
        bucket[output["verdict"]] += 1
    return {
        "cells": curated,
        "matrix": matrix,
        "n_present": counts["present"],
        "n_weak": counts["weak"],
        "n_absent": counts["absent"],
        "counts": dict(counts),
        "counts_by_factor": counts_by_factor,
        "warnings": warnings,
        "human_review": human_review,
    }


# ---------------------------------------------------------------------------
# evidence grading
# ---------------------------------------------------------------------------


def evidence_grade_kind(text: str | None) -> str:
    """Classify an evidence string as ``ESTABLISHED``/``INFERRED``/``UNKNOWN``.

    Rules (conservative):

    * strong domain evidence — a signature model call with ``E < 1e-3`` or a
      "significant domain(s)" statement — is ``ESTABLISHED``;
    * marginal / weak evidence is ``INFERRED``;
    * "no domain hit"/"only implausible ..." means no accepted evidence and is
      ``UNKNOWN`` (the absence itself is not established).
    """
    if not text:
        return "UNKNOWN"
    lowered = str(text).lower()
    if "no domain hit" in lowered or "no signature-model hit" in lowered:
        return "UNKNOWN"
    if "only implausible" in lowered:
        return "UNKNOWN"
    if _WEAK_HEADER in lowered or lowered.startswith("weak"):
        return "INFERRED"
    if "significant domain(s)" in lowered:
        return "ESTABLISHED"
    # Single-signature evidence strings look like "PF00042 E=2.2e-30 ...".
    match = re.search(r"E=([0-9.eE+\-]+)", text)
    if match is not None:
        try:
            evalue = float(match.group(1))
        except ValueError:
            return "INFERRED"
        return "ESTABLISHED" if evalue < MODEL_PRESENT_EVALUE else "INFERRED"
    return "INFERRED"


def grade_counts(grades: Iterable[str]) -> dict[str, int]:
    """Count evidence grades into a ``{grade: n}`` mapping (all keys present)."""
    counts = {grade: 0 for grade in EVIDENCE_GRADES}
    for grade in grades:
        value = str(grade).upper()
        if value not in counts:
            raise DataError(
                f"unknown evidence grade {grade!r}; expected one of {EVIDENCE_GRADES}",
                details={"grade": grade},
            )
        counts[value] += 1
    return counts
