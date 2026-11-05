"""``lifesci.seq.search`` — homology search and presence/absence curation.

Three modes:

``phmmer``
    Local pyhmmer phmmer of one or more query sequences against one or more
    target proteome FASTAs (``bias_filter=False`` — see warning
    ``PHMMER_BIAS_FILTER_OFF``), with optional reciprocal best-hit checks and an
    optional Pfam/HMM domain scan that turns the search into a curated presence
    matrix (single-sequence search misses extremely divergent homologues, so the
    domain scan is the arbiter).

``hmmsearch``
    Local pyhmmer hmmsearch of profile HMMs against target proteomes, curated
    with caller-supplied factor signatures (``factor_models``); models without a
    declared factor become factors named by the model id, so a presence matrix
    is still produced.

``evidence``
    Offline replay of **recorded** evidence rows: per-model Pfam domain hits
    (``pfam_hits.tsv`` format), optionally sequence-hit rows and
    reciprocal-check rows. Curates a presence matrix with exact evidence strings
    via :func:`lifesci.lib.evidence.curate_cells`, and can verify the result
    against an ``expected`` JSON (matrix + evidence strings).

Every payload carries the mandatory scientific warnings: absence is never proof
(``ABSENCE_NOT_PROOF``), cross-database E-values are not comparable
(``EVALUE_NOT_COMPARABLE_ACROSS_DATABASES``), pyhmmer runs with the compositional
bias filter off (``PHMMER_BIAS_FILTER_OFF``), and weak calls are flagged for
human review (``SINGLE_WEAK_LINE_EVIDENCE``).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Iterable, Mapping

from .. import provenance, util
from ..commands.base import command
from ..errors import DataError, InvalidInputError, LifesciError, ToolMissingError

_UNIPROT_ACC = re.compile(r"(?:sp|tr)\|([^|]+)\|")

INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["mode"],
    "properties": {
        "mode": {
            "type": "string",
            "enum": ["phmmer", "hmmsearch", "evidence"],
            "description": (
                "Search backend. 'phmmer' runs pyhmmer phmmer (bias_filter=False) and optional "
                "reciprocal/domain checks; 'hmmsearch' scans profile HMMs against proteomes; "
                "'evidence' replays recorded per-model hit rows offline and curates a "
                "presence matrix."
            ),
        },
        "queries": {
            "type": "object",
            "description": (
                "phmmer queries as {name: protein_sequence}. Prefer descriptive names such as "
                "'globin:P69905'; pair them with 'factor_queries' for a presence matrix."
            ),
            "additionalProperties": {"type": "string"},
        },
        "query_accessions": {
            "type": "array",
            "items": {"type": "string"},
            "description": "UniProt accessions fetched and used as phmmer queries (one request each).",
        },
        "queries_file": {
            "type": "string",
            "description": "FASTA file of phmmer queries (header first token = query name).",
        },
        "targets": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["fasta"],
                "properties": {
                    "fasta": {
                        "type": "string",
                        "description": "Target proteome FASTA path (local file; .gz accepted).",
                    },
                    "name": {
                        "type": "string",
                        "description": "Target database label, e.g. the organism or proteome id.",
                    },
                    "organism": {
                        "type": "string",
                        "description": "Organism label used in presence cells (defaults to 'name').",
                    },
                    "taxid": {"type": "integer", "description": "NCBI taxid for the target proteome."},
                },
            },
            "description": (
                "Target proteomes/FASTAs for phmmer/hmmsearch. For presence curation each target "
                "should carry an 'organism' label."
            ),
        },
        "hmms": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["path"],
                "properties": {
                    "id": {"type": "string", "description": "Model accession/name, e.g. PF00042."},
                    "path": {"type": "string", "description": "Local HMMER model file path."},
                },
            },
            "description": "Local HMM models for the domain scan (also used by hmmsearch mode).",
        },
        "hmm_ids": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Pfam model accessions (e.g. PF00042) downloaded from InterPro (cached) for the "
                "local domain scan."
            ),
        },
        "E": {
            "type": "number",
            "minimum": 0,
            "default": 10.0,
            "description": "HMMER reporting E-value threshold for sequence/domain searches.",
        },
        "cpus": {
            "type": "integer",
            "minimum": 1,
            "maximum": 64,
            "default": 4,
            "description": "CPU threads for pyhmmer searches.",
        },
        "max_hits": {
            "type": "integer",
            "minimum": 1,
            "maximum": 10000,
            "default": 100,
            "description": "Maximum hits kept per search.",
        },
        "bias_filter": {
            "type": "boolean",
            "default": False,
            "description": (
                "Compositional-bias filter. Keep false (default): the filter can hide genuine "
                "divergent homologues. Setting true emits a stronger warning."
            ),
        },
        "reciprocal": {
            "type": "boolean",
            "default": True,
            "description": (
                "phmmer mode only: run the reverse search of forward best hits against the query "
                "database and report reciprocal best hits. Required for excluding homologues."
            ),
        },
        "factor_queries": {
            "type": "object",
            "description": (
                "Presence-matrix design: {factor: [query name, ...]} mapping the phmmer query names "
                "to factors (e.g. {'globin': ['globin:P69905']}). Required to build cells/matrix "
                "in phmmer mode without a domain scan."
            ),
            "additionalProperties": {"type": "array", "items": {"type": "string"}},
        },
        "factor_models": {
            "type": "object",
            "description": (
                "Optional mapping of factor -> Pfam model accessions used for curation, e.g. "
                "{'globin': ['PF00042']}. There are no built-in factors: supply factor_models "
                "(or factor_queries) to curate a presence matrix."
            ),
            "additionalProperties": {"type": "array", "items": {"type": "string"}},
        },
        "rows": {
            "type": "array",
            "items": {"type": "object"},
            "description": (
                "evidence mode: inline recorded per-model hit rows (pfam_hits.tsv columns: "
                "organism, taxid, proteome, pfam_model, model_name, best_hit, evalue, score, model_cov, "
                "protein_length, description). Multiple rows per organism/model are expected."
            ),
        },
        "rows_file": {
            "description": (
                "evidence mode: TSV/JSON file of recorded per-model hit rows, or a list of such files."
            ),
            "oneOf": [
                {"type": "string"},
                {"type": "array", "items": {"type": "string"}},
            ],
        },
        "cells": {
            "type": "array",
            "items": {"type": "object"},
            "description": (
                "evidence mode: explicit cells [{organism, factor, hits: [{protein, model, evalue, ...}], "
                "sequence_hit?}]. Used instead of grouping flat rows."
            ),
        },
        "sequence_rows": {
            "type": "array",
            "items": {"type": "object"},
            "description": (
                "evidence mode: inline phmmer sequence-hit rows (presence_phmmer.tsv columns: organism, "
                "factor, best_hit, evalue, score, description, verdict) attached to cells as sequence_hit."
            ),
        },
        "sequence_rows_file": {
            "type": "string",
            "description": "evidence mode: TSV file of phmmer sequence-hit rows.",
        },
        "reciprocal_rows": {
            "type": "array",
            "items": {"type": "object"},
            "description": "evidence mode: inline reciprocal-check rows (reciprocal_check.tsv columns).",
        },
        "reciprocal_file": {
            "type": "string",
            "description": "evidence mode: TSV file of reciprocal-check rows (reciprocal_check.tsv format).",
        },
        "expected": {
            "type": "object",
            "description": (
                "evidence mode: expected result to verify reproduction, e.g. {'matrix': {org: {factor: verdict}}, "
                "'evidence': {org: {factor: 'string'}}} or a cells list. Mismatches are reported and warned."
            ),
        },
        "expected_file": {
            "type": "string",
            "description": "evidence mode: JSON file with the expected matrix/evidence.",
        },
        "present_evalue": {
            "type": "number",
            "minimum": 0,
            "default": 0.001,
            "description": "E-value cutoff for a 'present' presence call (sequence-only matrices; default 1e-3).",
        },
        "weak_evalue": {
            "type": "number",
            "minimum": 0,
            "default": 1.0,
            "description": "E-value cutoff below which a call is 'weak' rather than absent.",
        },
        "out": {
            "type": "string",
            "description": "Optional directory for written hits TSV / matrix JSON.",
        },
    },
}


def _resolve_path(ctx: Any, value: str) -> Path:
    target = Path(str(value)).expanduser()
    if not target.is_absolute():
        target = Path(ctx.cwd) / target
    return target.resolve()


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


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
    mode = str(args.get("mode"))
    planned: list[dict[str, Any]] = []
    if mode == "phmmer":
        planned.append({"tool": "pyhmmer.phmmer", "purpose": "forward_search", "params": {"E": args.get("E", 10.0), "bias_filter": False}})
        if args.get("reciprocal", True):
            planned.append({"tool": "pyhmmer.phmmer", "purpose": "reciprocal_search", "params": {"E": args.get("E", 10.0), "bias_filter": False}})
        for accession in args.get("query_accessions") or []:
            planned.append({"method": "GET", "url": f"https://rest.uniprot.org/uniprotkb/{accession}.fasta", "purpose": "query_sequence"})
    elif mode == "hmmsearch":
        planned.append({"tool": "pyhmmer.hmmsearch", "purpose": "hmm_scan", "params": {"E": args.get("E", 10.0), "bias_filter": False}})
    for target in args.get("targets") or []:
        planned.append({"purpose": "read_target_fasta", "path": str(target.get("fasta"))})
    for model in args.get("hmm_ids") or []:
        database = "pfam" if str(model).upper().startswith("PF") else "all"
        planned.append(
            {
                "method": "GET",
                "url": f"https://www.ebi.ac.uk/interpro/wwwapi/entry/{database}/{model}/?annotation=hmm",
                "purpose": "hmm_download",
            }
        )
    for rows_file in _as_list(args.get("rows_file")):
        planned.append({"purpose": "read_recorded_evidence_rows", "path": str(rows_file)})
    if args.get("expected_file"):
        planned.append({"purpose": "read_expected_matrix", "path": str(args["expected_file"])})
    return {
        "command": "seq.search",
        "dry_run": True,
        "mode": mode,
        "planned_commands": planned,
        "queries": [],
        "targets": [],
        "hits": [],
        "reciprocal": {"available": False, "pairs": [], "dry_run": True},
        "domain_scan": [],
        "cells": [],
        "matrix": {},
        "counts": {"n_queries": 0, "n_targets": 0, "n_hits": 0, "n_cells": 0},
        "warnings_predicted": [
            "PHMMER_BIAS_FILTER_OFF",
            "ABSENCE_NOT_PROOF",
            "EVALUE_NOT_COMPARABLE_ACROSS_DATABASES",
            "SINGLE_WEAK_LINE_EVIDENCE",
        ],
    }


# ---------------------------------------------------------------------------
# evidence mode helpers
# ---------------------------------------------------------------------------


def _load_rows(ctx: Any, args: Mapping[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return ``(flat_rows, cells)`` from the evidence-mode inputs."""
    flat_rows: list[dict[str, Any]] = [dict(row) for row in (args.get("rows") or [])]
    cells: list[dict[str, Any]] = [dict(cell) for cell in (args.get("cells") or [])]
    for rows_file in _as_list(args.get("rows_file")):
        path = _resolve_path(ctx, str(rows_file))
        if not path.is_file():
            raise InvalidInputError(f"rows_file not found: {path}")
        _record_local_file(ctx, path, kind="evidence_rows", notes={"command": "seq.search"})
        if path.suffix.lower() == ".json":
            payload = util.read_json(path)
            if isinstance(payload, list):
                flat_rows.extend(dict(item) for item in payload if isinstance(item, Mapping))
            elif isinstance(payload, Mapping):
                if isinstance(payload.get("rows"), list):
                    flat_rows.extend(dict(item) for item in payload["rows"] if isinstance(item, Mapping))
                elif isinstance(payload.get("cells"), list):
                    cells.extend(dict(item) for item in payload["cells"] if isinstance(item, Mapping))
                elif isinstance(payload.get("hits"), list):
                    flat_rows.extend(dict(item) for item in payload["hits"] if isinstance(item, Mapping))
                else:
                    raise DataError(f"unsupported JSON evidence format in {path}")
            else:  # pragma: no cover - defensive
                raise DataError(f"unsupported JSON evidence format in {path}")
        else:
            flat_rows.extend(util.read_tsv(path))
    return flat_rows, cells


def _load_optional_tsv(ctx: Any, path_value: Any, *, kind: str) -> list[dict[str, Any]]:
    if not path_value:
        return []
    path = _resolve_path(ctx, str(path_value))
    if not path.is_file():
        raise InvalidInputError(f"{kind} file not found: {path}")
    _record_local_file(ctx, path, kind=kind, notes={"command": "seq.search"})
    return util.read_tsv(path)


def _normalise_flat_hits(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    hits: list[dict[str, Any]] = []
    for row in rows:
        protein = row.get("best_hit") or row.get("protein") or row.get("accession") or row.get("hit")
        model = row.get("pfam_model") or row.get("model")
        evalue = util.parse_float(row.get("evalue"))
        if not protein or str(protein).upper() in {"NA", "N/A", "NONE", "-"}:
            continue
        if not model or evalue is None:
            continue
        hits.append(
            {
                "organism": str(row.get("organism") or ""),
                "factor": str(row.get("factor") or ""),
                "taxid": util.parse_int(row.get("taxid")),
                "proteome": row.get("proteome"),
                "protein": str(protein),
                "model": str(model).split(".")[0],
                "model_name": row.get("model_name"),
                "evalue": evalue,
                "score": util.parse_float(row.get("score")),
                "model_cov": util.parse_float(row.get("model_cov")),
                "protein_length": util.parse_int(row.get("protein_length")),
                "description": row.get("description"),
                "model_verdict": row.get("model_verdict"),
            }
        )
    hits.sort(
        key=lambda hit: (
            hit["organism"],
            hit.get("factor") or "",
            hit["model"],
            hit["evalue"],
            hit["protein"],
        )
    )
    return hits


def _factor_for_model(model: str | None, signatures: Mapping[str, Mapping[str, Any]]) -> str | None:
    if not model:
        return None
    clean = str(model).split(".")[0]
    for factor, signature in signatures.items():
        if clean in signature.get("models", []):
            return factor
    return None


def _normalise_model_id(value: Any) -> str | None:
    """Normalise one HMM/Pfam model id (strip whitespace and a ``.25`` suffix)."""
    if value is None:
        return None
    text_model = str(value).strip().split(".")[0]
    return text_model or None


def _declared_factor_models(args: Mapping[str, Any]) -> dict[str, list[str]]:
    """Return caller-declared ``{factor: [model, ...]}`` (``factor_models`` input)."""
    declared: dict[str, list[str]] = {}
    for factor, models in (args.get("factor_models") or {}).items():
        cleaned: list[str] = []
        for model in models or []:
            text_model = str(model).split(".")[0].strip()
            if text_model and text_model not in cleaned:
                cleaned.append(text_model)
        declared[str(factor)] = cleaned
    return declared


def _signatures_from_model_map(model_to_factor: Mapping[str, str]) -> dict[str, dict[str, Any]]:
    """Build ``{factor: {"models": [...]}}`` from a model -> factor mapping."""
    signatures: dict[str, dict[str, Any]] = {}
    for model, factor in sorted(model_to_factor.items()):
        signatures.setdefault(str(factor), {"models": []})["models"].append(str(model))
    return signatures


def _sequence_evidence_map(
    sequence_rows: Iterable[Mapping[str, Any]],
) -> dict[tuple[str, str], dict[str, Any]]:
    evidence: dict[tuple[str, str], dict[str, Any]] = {}
    for row in sequence_rows:
        organism = str(row.get("organism") or "").strip()
        factor = str(row.get("factor") or "").strip()
        if not organism or not factor:
            continue
        protein = row.get("best_hit") or row.get("protein")
        has_hit = bool(protein) and str(protein).upper() not in {"NA", "N/A", "NONE", "-"}
        evalue = util.parse_float(row.get("evalue"))
        coverage = util.parse_float(row.get("coverage") or row.get("qcov") or row.get("tcov"))
        evidence[(organism, factor)] = {
            "protein": str(protein) if has_hit else None,
            "evalue": evalue if has_hit else None,
            "score": util.parse_float(row.get("score")) if has_hit else None,
            "coverage": coverage,
            "verdict": row.get("verdict") or ("absent" if not has_hit else None),
            "description": row.get("description"),
            "source": str(row.get("source") or "phmmer"),
        }
    return evidence


def _build_cells_from_flat_rows(
    flat_rows: list[dict[str, Any]],
    *,
    hits: list[dict[str, Any]],
    signatures: Mapping[str, Mapping[str, Any]],
    sequence_evidence: Mapping[tuple[str, str], dict[str, Any]],
    factors: list[str],
) -> list[dict[str, Any]]:
    """Group recorded per-model rows into organism x factor cells."""
    organism_order: list[str] = []
    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    for row in flat_rows:
        organism = str(row.get("organism") or "").strip()
        if not organism:
            continue
        model = row.get("pfam_model") or row.get("model")
        factor = row.get("factor") or _factor_for_model(model, signatures)
        if not factor:
            continue
        protein = row.get("best_hit") or row.get("protein") or row.get("accession")
        has_hit = bool(protein) and str(protein).upper() not in {"NA", "N/A", "NONE", "-"} and util.parse_float(row.get("evalue")) is not None
        key = (organism, str(factor))
        if organism not in organism_order:
            organism_order.append(organism)
        cell = grouped.setdefault(
            key,
            {
                "organism": organism,
                "factor": str(factor),
                "hits": [],
                "taxid": util.parse_int(row.get("taxid")),
                "proteome": row.get("proteome"),
            },
        )
        if has_hit:
            cell["hits"].append(
                {
                    "protein": str(protein),
                    "model": str(model).split(".")[0] if model else None,
                    "evalue": util.parse_float(row.get("evalue")),
                    "score": util.parse_float(row.get("score")),
                    "model_cov": util.parse_float(row.get("model_cov")),
                    "protein_length": util.parse_int(row.get("protein_length")),
                    "description": row.get("description"),
                }
            )
    # Guarantee every requested factor exists for every organism (absent calls).
    for organism in organism_order:
        for factor in factors:
            grouped.setdefault(
                (organism, factor),
                {"organism": organism, "factor": factor, "hits": [], "taxid": None, "proteome": None},
            )
    cells: list[dict[str, Any]] = []
    for (organism, factor), cell in grouped.items():
        # Stable sort by E-value only: ties keep the recorded hit order (protein
        # alphabetical tie-breaking would change the curated evidence accession).
        cell["hits"].sort(key=lambda hit: (hit.get("evalue") is None, hit.get("evalue") or 0.0))
        sequence_hit = sequence_evidence.get((organism, factor))
        if sequence_hit:
            cell["sequence_hit"] = dict(sequence_hit)
        cells.append(cell)
    return cells


def _compare_expected(
    curated: Mapping[str, Any],
    expected: Mapping[str, Any],
) -> dict[str, Any]:
    """Compare curated cells/matrix against an expected structure (flexible shapes)."""
    mismatches: list[dict[str, Any]] = []
    actual_cells = {(cell["organism"], cell["factor"]): cell for cell in curated.get("cells") or []}
    expected_cells: dict[tuple[str, str], Mapping[str, Any]] = {}
    expected_matrix: dict[str, dict[str, str]] = {}
    expected_evidence: dict[tuple[str, str], str] = {}

    matrix = expected.get("matrix")
    if isinstance(matrix, Mapping):
        for organism, row in matrix.items():
            if isinstance(row, Mapping):
                expected_matrix[str(organism)] = {str(factor): str(verdict) for factor, verdict in row.items()}
    evidence_section = expected.get("evidence")
    if isinstance(evidence_section, Mapping):
        for organism, row in evidence_section.items():
            if isinstance(row, Mapping):
                for factor, text in row.items():
                    if isinstance(text, Mapping):
                        expected_cells[(str(organism), str(factor))] = dict(text)
                    else:
                        expected_evidence[(str(organism), str(factor))] = str(text)
    cells_section = expected.get("cells")
    if isinstance(cells_section, list):
        for cell in cells_section:
            if isinstance(cell, Mapping) and cell.get("organism") and cell.get("factor"):
                key = (str(cell["organism"]), str(cell["factor"]))
                expected_cells[key] = cell
                if cell.get("evidence"):
                    expected_evidence[key] = str(cell["evidence"])

    for organism, factors in expected_matrix.items():
        for factor, verdict in factors.items():
            actual = actual_cells.get((organism, factor))
            actual_verdict = actual.get("verdict") if actual else None
            if actual_verdict != verdict:
                mismatches.append(
                    {
                        "organism": organism,
                        "factor": factor,
                        "field": "verdict",
                        "expected": verdict,
                        "actual": actual_verdict,
                    }
                )
    for key, expected_text in expected_evidence.items():
        actual = actual_cells.get(key)
        actual_text = actual.get("evidence") if actual else None
        if actual_text != expected_text:
            mismatches.append(
                {
                    "organism": key[0],
                    "factor": key[1],
                    "field": "evidence",
                    "expected": expected_text,
                    "actual": actual_text,
                }
            )
    for key, cell in expected_cells.items():
        actual = actual_cells.get(key)
        for field in ("verdict", "evidence"):
            if field in cell and actual is not None and cell.get(field) != actual.get(field):
                mismatches.append(
                    {
                        "organism": key[0],
                        "factor": key[1],
                        "field": field,
                        "expected": cell.get(field),
                        "actual": actual.get(field),
                    }
                )
    checked = bool(expected_matrix or expected_evidence or expected_cells)
    return {
        "checked": checked,
        "n_expected_verdicts": sum(len(row) for row in expected_matrix.values()),
        "n_expected_evidence": len(expected_evidence),
        "n_mismatches": len(mismatches),
        "matrix_match": not any(item["field"] == "verdict" for item in mismatches),
        "evidence_match": not any(item["field"] == "evidence" for item in mismatches),
        "mismatches": mismatches[:50],
    }


def _sequence_verdict(evalue: float | None, present_evalue: float, weak_evalue: float) -> str:
    if evalue is None:
        return "absent"
    if evalue < present_evalue:
        return "present"
    if evalue < weak_evalue:
        return "weak"
    return "absent"


def _evidence_mode(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
    """Offline replay of recorded per-model domain hit rows (+ optional phmmer/reciprocal rows)."""
    try:
        from ..lib import evidence as evidence_lib
    except ImportError as err:  # pragma: no cover - dependency ordering
        raise ToolMissingError(
            "lifesci.lib.evidence is not available; seq.search evidence mode cannot curate the matrix",
            details={"error": str(err)},
            fallbacks=["use phmmer/hmmsearch mode once pyhmmer is available"],
        ) from err

    declared = _declared_factor_models(args)
    requested_factors = [str(factor) for factor in (args.get("factor_queries") or {}).keys()]

    flat_rows, explicit_cells = _load_rows(ctx, args)
    # Factors are caller-declared (``factor_models``) and/or inferred from the
    # recorded rows: a row carrying an explicit ``factor`` column maps its model
    # to that factor, so a recorded table curates without extra configuration.
    signatures: dict[str, dict[str, Any]] = {
        factor: {"models": list(models)} for factor, models in declared.items() if models
    }
    for row in flat_rows:
        factor = str(row.get("factor") or "").strip()
        model = _normalise_model_id(row.get("pfam_model") or row.get("model"))
        if not factor or not model:
            continue
        models = signatures.setdefault(factor, {"models": []})["models"]
        if model not in models:
            models.append(model)
    for cell in explicit_cells:
        factor = str(cell.get("factor") or "").strip()
        if not factor:
            continue
        models = signatures.setdefault(factor, {"models": []})["models"]
        for hit in cell.get("hits") or []:
            model = _normalise_model_id(hit.get("model") or hit.get("pfam_model"))
            if model and model not in models:
                models.append(model)
    usable = {factor: signature for factor, signature in signatures.items() if signature["models"]}
    for factor in list(signatures):
        if factor not in usable:
            ctx.warn(
                "FACTOR_MODEL_MAPPING_MISSING",
                f"Declared factor {factor!r} has no model mapping in the supplied rows; its cells "
                "cannot be curated (a domain signature model is required).",
                severity="warning",
                context={"factor": factor},
            )
    signatures = usable
    factors = [factor for factor in requested_factors if factor in signatures]
    factors.extend(factor for factor in signatures if factor not in factors)
    sequence_rows = [dict(row) for row in (args.get("sequence_rows") or [])]
    sequence_rows.extend(_load_optional_tsv(ctx, args.get("sequence_rows_file"), kind="sequence_rows"))
    for cell in explicit_cells:
        if cell.get("sequence_hit") and not any(
            str(row.get("organism") or "").strip() == str(cell.get("organism") or "").strip()
            and str(row.get("factor") or "").strip() == str(cell.get("factor") or "").strip()
            for row in sequence_rows
        ):
            sequence_rows.append(
                {
                    "organism": cell.get("organism"),
                    "factor": cell.get("factor"),
                    **dict(cell["sequence_hit"]),
                }
            )
    reciprocal_rows = [dict(row) for row in (args.get("reciprocal_rows") or [])]
    reciprocal_rows.extend(_load_optional_tsv(ctx, args.get("reciprocal_file"), kind="reciprocal_rows"))

    if not flat_rows and not explicit_cells:
        raise InvalidInputError(
            "evidence mode needs 'rows'/'rows_file' (per-model domain hit rows) or 'cells'"
        )
    if flat_rows and not explicit_cells and not signatures:
        raise InvalidInputError(
            "evidence mode found no factor/model mapping in the recorded rows: declare factors via "
            "'factor_models' ({factor: [model, ...]}) or include a 'factor' column next to a "
            "'pfam_model'/'model' column",
            details={"n_rows": len(flat_rows)},
            fallbacks=[
                "pass factor_models={'factor': ['PF00042']}",
                "add a 'factor' column to the recorded per-model hit rows",
            ],
        )
    flat_hits = _normalise_flat_hits(flat_rows)
    sequence_evidence = _sequence_evidence_map(sequence_rows)
    cells = explicit_cells or _build_cells_from_flat_rows(
        flat_rows,
        hits=flat_hits,
        signatures=signatures,
        sequence_evidence=sequence_evidence,
        factors=factors,
    )
    if not cells:
        raise DataError("evidence mode could not assemble any presence cells from the supplied rows")

    missing_signatures = sorted(
        {str(cell.get("factor") or "") for cell in cells} - set(signatures)
    )
    if missing_signatures:
        if explicit_cells:
            raise InvalidInputError(
                f"evidence mode cannot curate factor(s) {missing_signatures}: no model mapping is "
                "available for them",
                details={"factors": missing_signatures, "known_factors": sorted(signatures)},
                fallbacks=[
                    "supply 'factor_models' ({factor: [model, ...]})",
                    "add a 'model'/'pfam_model' to each cell hit",
                ],
            )
        # Flat rows: factors without a model mapping cannot be curated; omit them
        # (the FACTOR_MODEL_MAPPING_MISSING warning above says why).
        cells = [cell for cell in cells if str(cell.get("factor") or "") in signatures]
    curated = evidence_lib.curate_cells(cells, factor_signatures=signatures)
    matrix = curated.get("matrix") or {}

    # Attach sequence-level evidence to each curated cell for the arbiter.
    present_evalue = float(args.get("present_evalue", 1e-3))
    weak_evalue = float(args.get("weak_evalue", 1.0))
    seq_verdicts: dict[tuple[str, str], str] = {}
    for cell in curated.get("cells") or []:
        key = (cell["organism"], cell["factor"])
        seq = sequence_evidence.get(key)
        if seq is not None:
            seq_verdict = seq.get("verdict") or _sequence_verdict(
                util.parse_float(seq.get("evalue")), present_evalue, weak_evalue
            )
            seq_verdicts[key] = str(seq_verdict)
            cell["sequence_evidence"] = dict(seq, verdict=str(seq_verdict))
        else:
            cell["sequence_evidence"] = None

    # Reciprocal checks (recorded) + domain-vs-sequence arbiter rows.
    reciprocal_payload: dict[str, Any] = {
        "available": bool(reciprocal_rows),
        "rows": [],
        "n_present": 0,
        "n_weak": 0,
        "n_absent": 0,
        "disagreements": [],
    }
    if reciprocal_rows:
        curated_lookup = {(cell["organism"], cell["factor"]): cell for cell in curated.get("cells") or []}
        for row in reciprocal_rows:
            organism = str(row.get("organism") or "").strip()
            factor = str(row.get("factor") or "").strip()
            verdict = str(row.get("verdict") or "").strip() or None
            best_hit = row.get("best_hit")
            has_hit = bool(best_hit) and str(best_hit).upper() not in {"NA", "N/A", "NONE", "-"}
            record = {
                "organism": organism,
                "factor": factor,
                "query": row.get("query"),
                "best_hit": best_hit if has_hit else None,
                "evalue": util.parse_float(row.get("evalue")),
                "score": util.parse_float(row.get("score")),
                "verdict": verdict,
                "description": row.get("description"),
            }
            reciprocal_payload["rows"].append(record)
            if verdict == "present":
                reciprocal_payload["n_present"] += 1
            elif verdict == "weak":
                reciprocal_payload["n_weak"] += 1
            elif verdict == "absent":
                reciprocal_payload["n_absent"] += 1
            curated_cell = curated_lookup.get((organism, factor))
            if curated_cell and verdict and curated_cell["verdict"] != verdict:
                reciprocal_payload["disagreements"].append(
                    {
                        "organism": organism,
                        "factor": factor,
                        "domain_verdict": curated_cell["verdict"],
                        "sequence_verdict": verdict,
                        "sequence_best_hit": record["best_hit"],
                        "sequence_evalue": record["evalue"],
                        "domain_evidence": curated_cell.get("evidence"),
                    }
                )
    else:
        reciprocal_payload["note"] = (
            "no reciprocal-check rows supplied; reciprocal validation was not performed for this replay"
        )
        ctx.warn(
            "RECIPROCAL_EVIDENCE_MISSING",
            "Evidence replay has no reciprocal-check rows; report sequence-level conclusions as "
            "'not reciprocal-checked' and keep the domain curation as the primary evidence.",
        )

    arbiter_rows: list[dict[str, Any]] = []
    for cell in curated.get("cells") or []:
        key = (cell["organism"], cell["factor"])
        seq = sequence_evidence.get(key) or (cell.get("sequence_evidence") or None)
        if seq is None:
            arbiter_rows.append(
                {
                    "organism": cell["organism"],
                    "factor": cell["factor"],
                    "domain_verdict": cell["verdict"],
                    "sequence_verdict": None,
                    "conclusion": "domain_only",
                    "evidence": cell.get("evidence"),
                }
            )
            continue
        seq_verdict = seq_verdicts.get(key) or _sequence_verdict(
            util.parse_float(seq.get("evalue")), present_evalue, weak_evalue
        )
        if cell["verdict"] == "present" and seq_verdict not in {"present", "weak"}:
            conclusion = "domain_arbiter_present_without_sequence_hit"
        elif cell["verdict"] in {"present", "weak"}:
            conclusion = "domain_supported"
        elif seq_verdict in {"present", "weak"}:
            conclusion = "sequence_present_without_domain"
        else:
            conclusion = "domain_absent"
        arbiter_rows.append(
            {
                "organism": cell["organism"],
                "factor": cell["factor"],
                "domain_verdict": cell["verdict"],
                "sequence_verdict": seq_verdict,
                "conclusion": conclusion,
                "evidence": cell.get("evidence"),
                "sequence_evidence": dict(seq),
            }
        )

    # Reproduction check against expected matrix/evidence.
    expected: Mapping[str, Any] | None = args.get("expected")
    if args.get("expected_file"):
        expected_path = _resolve_path(ctx, str(args["expected_file"]))
        if not expected_path.is_file():
            raise InvalidInputError(f"expected_file not found: {expected_path}")
        _record_local_file(ctx, expected_path, kind="expected_matrix", notes={"command": "seq.search"})
        loaded = util.read_json(expected_path)
        expected = loaded if isinstance(loaded, Mapping) else {"cells": loaded}
    reproduction = _compare_expected(curated, expected or {})
    if reproduction["checked"] and reproduction["n_mismatches"]:
        ctx.warn(
            "REPRODUCTION_MISMATCH",
            f"Evidence replay does not reproduce the expected matrix/evidence "
            f"({reproduction['n_mismatches']} mismatch(es)); inspect reproduction.mismatches and the input rows.",
            context={"mismatches": reproduction["mismatches"][:10]},
        )

    # Evidence rows that carry only the best hit per model cannot reproduce full
    # curations (curation merges all top hits per model).
    row_counts: dict[tuple[str, str], int] = {}
    for row in flat_rows:
        organism = str(row.get("organism") or "").strip()
        model = str(row.get("pfam_model") or row.get("model") or "").split(".")[0]
        if organism and model:
            row_counts[(organism, model)] = row_counts.get((organism, model), 0) + 1
    if row_counts and max(row_counts.values()) <= 1:
        ctx.warn(
            "EVIDENCE_ROWS_BEST_ONLY",
            "The recorded evidence appears to contain only one (best) hit per organism x model; "
            "curation merges all significant hits per model, so evidence strings may differ. "
            "Prefer a full per-model hit table (pfam_hits.tsv) for exact reproduction.",
        )

    # ---- mandatory warnings and human-review flags ----
    ctx.warn(
        "PHMMER_BIAS_FILTER_OFF",
        "Sequence searches run with HMMER's compositional-bias filter disabled "
        "(bias_filter=False): the default filter hides genuine divergent homologues. Any sequence-level "
        "evidence replayed here inherits that setting.",
        context={"bias_filter": False},
    )
    ctx.warn_evalue_db_dependence(
        "recorded_domain_evidence",
        "Recorded E-values are per-model HMMER hits against a whole-proteome target database of a given "
        "size; a phmmer E-value or a single-sequence hmmsearch E-value against a different database is not "
        "comparable to them.",
    )
    if curated.get("n_absent"):
        ctx.warn(
            "ABSENCE_NOT_PROOF",
            "Absent calls mean 'no accepted candidate at the recorded parameters and E-value cutoffs'; "
            "they are not proof of absence. Extremely divergent proteins are missed by sequence and "
            "domain searches alike.",
            context={"n_absent": curated.get("n_absent")},
        )
    for cell in curated.get("cells") or []:
        if cell["verdict"] == "weak":
            ctx.review(
                "SINGLE_WEAK_LINE_EVIDENCE",
                f"{cell['organism']} {cell['factor']}: weak call from a single marginal line of evidence "
                f"({cell.get('evidence')}); human review required.",
                context={"organism": cell["organism"], "factor": cell["factor"]},
            )
    for row in arbiter_rows:
        if row["conclusion"] == "domain_arbiter_present_without_sequence_hit":
            ctx.warn(
                "DOMAIN_EVIDENCE_OUTRANKS_SEQUENCE",
                f"{row['organism']} {row['factor']}: domain evidence calls present while the sequence "
                "search found no detectable hit; domain/family evidence outranks a single weak sequence "
                "line (divergent homologues are routinely missed by phmmer).",
                context={"organism": row["organism"], "factor": row["factor"], "sequence_evidence": row.get("sequence_evidence")},
            )
        elif row["conclusion"] == "sequence_present_without_domain":
            ctx.review(
                "SINGLE_WEAK_LINE_EVIDENCE",
                f"{row['organism']} {row['factor']}: sequence hit without supporting domain evidence; "
                "treat as a single weak line and review.",
                context={"organism": row["organism"], "factor": row["factor"]},
            )

    counts = {
        "n_queries": len({(hit.get("factor"), hit.get("model")) for hit in flat_hits}),
        "n_targets": len(matrix),
        "n_hits": len(flat_hits),
        "n_cells": len(curated.get("cells") or []),
        "n_present": int(curated.get("n_present") or 0),
        "n_weak": int(curated.get("n_weak") or 0),
        "n_absent": int(curated.get("n_absent") or 0),
        "n_sequence_rows": len(sequence_rows),
        "n_reciprocal_rows": len(reciprocal_rows),
    }
    out_info = _write_outputs(ctx, args, mode="evidence", curated=curated, hits=flat_hits)
    return {
        "dry_run": False,
        "mode": "evidence",
        "queries": [
            {"factor": factor, "models": sorted(signature.get("models") or [])}
            for factor, signature in sorted(signatures.items())
            if factor in factors or any(hit.get("factor") == factor for hit in flat_hits)
        ],
        "targets": [
            {"organism": organism, "taxid": next((cell.get("taxid") for cell in cells if cell.get("organism") == organism), None)}
            for organism in matrix
        ],
        "hits": flat_hits,
        "reciprocal": reciprocal_payload,
        "domain_scan": [
            {
                "organism": row["organism"],
                "factor": row["factor"],
                "model": row.get("model"),
                "best_evalue": row.get("evalue"),
                "best_hit": row.get("protein"),
            }
            for row in flat_hits
        ],
        "cells": curated.get("cells") or [],
        "matrix": matrix,
        "n_queries": counts["n_queries"],
        "n_targets": counts["n_targets"],
        "n_hits": counts["n_hits"],
        "counts": counts,
        "counts_by_factor": curated.get("counts_by_factor") or {},
        "domain_arbiter": {
            "rows": arbiter_rows,
            "n_domain_only": sum(1 for row in arbiter_rows if row["conclusion"] == "domain_only"),
            "n_domain_supported": sum(1 for row in arbiter_rows if row["conclusion"] == "domain_supported"),
            "n_domain_without_sequence": sum(
                1 for row in arbiter_rows if row["conclusion"] == "domain_arbiter_present_without_sequence_hit"
            ),
            "n_sequence_without_domain": sum(
                1 for row in arbiter_rows if row["conclusion"] == "sequence_present_without_domain"
            ),
            "n_absent": sum(1 for row in arbiter_rows if row["conclusion"] == "domain_absent"),
        },
        "reproduction": reproduction,
        "resolved": {
            "cache_dir": str(ctx.cache_dir),
            "out": out_info,
            "n_evidence_rows": len(flat_rows),
            "factor_signatures": {
                factor: sorted(signature.get("models") or []) for factor, signature in signatures.items()
            },
            "database": "recorded Pfam HMM scan (UniProt reference proteomes)",
            "release": next((row.get("release") for row in flat_rows if row.get("release")), None),
        },
    }


# ---------------------------------------------------------------------------
# live search modes (phmmer / hmmsearch)
# ---------------------------------------------------------------------------


def _extract_sequences(records: Iterable[tuple[str, str]]) -> dict[str, str]:
    sequences: dict[str, str] = {}
    for header, sequence in records:
        token = header.split()[0]
        sequences[token] = sequence
        match = _UNIPROT_ACC.match(header)
        if match:
            sequences.setdefault(match.group(1), sequence)
    return sequences


def _resolve_queries(ctx: Any, args: Mapping[str, Any]) -> dict[str, str]:
    queries: dict[str, str] = {}
    for name, sequence in (args.get("queries") or {}).items():
        queries[str(name)] = str(sequence).strip()
    if args.get("queries_file"):
        path = _resolve_path(ctx, str(args["queries_file"]))
        if not path.is_file():
            raise InvalidInputError(f"queries_file not found: {path}")
        _record_local_file(ctx, path, kind="query_fasta", notes={"command": "seq.search"})
        for header, sequence in util.read_fasta(path):
            queries[header.split()[0]] = sequence
    if args.get("query_accessions"):
        try:
            from ..adapters import uniprot
        except ImportError as err:  # pragma: no cover - parallel workstream ordering
            raise ToolMissingError(
                "lifesci.adapters.uniprot is not available; cannot fetch query accessions",
                details={"error": str(err)},
                fallbacks=["pass query sequences inline via 'queries' or 'queries_file'"],
            ) from err
        for accession in args["query_accessions"]:
            fasta = uniprot.fetch_fasta(ctx, str(accession))
            queries[str(fasta.get("accession") or accession)] = str(fasta.get("sequence") or "")
    return {name: sequence for name, sequence in queries.items() if sequence}


def _resolve_hmms(ctx: Any, args: Mapping[str, Any]) -> list[dict[str, Any]]:
    models: list[dict[str, Any]] = []
    for item in args.get("hmms") or []:
        path = _resolve_path(ctx, str(item.get("path")))
        if not path.is_file():
            raise InvalidInputError(f"HMM file not found: {path}")
        model_id = str(item.get("id") or path.stem).split(".")[0].upper()
        _record_local_file(ctx, path, kind="hmm_model", notes={"command": "seq.search", "model": model_id})
        models.append({"id": model_id, "path": str(path), "source": "local_file"})
    if args.get("hmm_ids"):
        try:
            from ..adapters import interpro
        except ImportError as err:  # pragma: no cover - parallel workstream ordering
            raise ToolMissingError(
                "lifesci.adapters.interpro is not available; cannot download HMM models",
                details={"error": str(err)},
                fallbacks=["pass local HMM files via 'hmms'"],
            ) from err
        for model_id in args["hmm_ids"]:
            clean = str(model_id).split(".")[0]
            if any(model["id"] == clean.upper() for model in models):
                continue
            database = "pfam" if clean.upper().startswith("PF") else "all"
            fetched = interpro.fetch_hmm(ctx, database=database, entry_id=clean)
            models.append(
                {
                    "id": clean.upper(),
                    "path": str(fetched.get("path")) if fetched.get("path") else None,
                    "source": "interpro",
                    "sha256": fetched.get("sha256"),
                    "release": fetched.get("release"),
                }
            )
    return [model for model in models if model.get("path")]


def _resolve_targets(ctx: Any, args: Mapping[str, Any]) -> list[dict[str, Any]]:
    targets: list[dict[str, Any]] = []
    for index, item in enumerate(args.get("targets") or []):
        path = _resolve_path(ctx, str(item.get("fasta")))
        if not path.is_file():
            raise InvalidInputError(f"target FASTA not found: {path}")
        _record_local_file(ctx, path, kind="target_fasta", notes={"command": "seq.search", "index": index})
        records = util.read_fasta(path)
        name = str(item.get("name") or item.get("organism") or path.stem)
        targets.append(
            {
                "name": name,
                "organism": str(item.get("organism") or name),
                "taxid": util.parse_int(item.get("taxid")),
                "path": str(path),
                "n_sequences": len(records),
                "sequences": _extract_sequences(records),
            }
        )
    return targets


def _evalue_key(hit: Mapping[str, Any]) -> float:
    """Sort key for hits: an E-value of 0 is the STRONGEST hit, not a missing one."""
    value = util.parse_float(hit.get("evalue"))
    return float("inf") if value is None else float(value)


def _require_hmmer(ctx: Any) -> Any:
    try:
        from ..lib import hmmer
    except ImportError as err:  # pragma: no cover - dependency ordering
        raise ToolMissingError(
            "lifesci.lib.hmmer is not available; pyhmmer searches cannot run",
            details={"error": str(err)},
            fallbacks=["use seq.search --mode evidence with recorded hit tables"],
        ) from err
    if not hasattr(hmmer, "phmmer") and not hasattr(hmmer, "hmmsearch"):
        raise ToolMissingError(
            "lifesci.lib.hmmer is not implemented yet; pyhmmer searches cannot run",
            fallbacks=["use seq.search --mode evidence with recorded hit tables"],
        )
    return hmmer


def _write_outputs(
    ctx: Any,
    args: Mapping[str, Any],
    *,
    mode: str,
    curated: Mapping[str, Any] | None = None,
    hits: Iterable[Mapping[str, Any]] = (),
    matrix: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    if not args.get("out"):
        return None
    out_dir = util.ensure_dir(_resolve_path(ctx, str(args["out"])))
    info: dict[str, Any] = {"dir": str(out_dir), "hits": None, "matrix": None}
    hit_list = [dict(hit) for hit in hits]
    if hit_list:
        hits_path = out_dir / f"seq.search.{mode}.hits.tsv"
        columns = sorted({key for hit in hit_list for key in hit})
        if "organism" in columns:
            columns.remove("organism")
            columns.insert(0, "organism")
        util.write_tsv(hits_path, hit_list, columns=columns)
        _record_local_file(ctx, hits_path, kind="output_table", notes={"command": "seq.search"})
        info["hits"] = {"path": str(hits_path), "sha256": util.sha256_file(hits_path), "bytes": hits_path.stat().st_size}
    final_matrix = dict(matrix) if matrix is not None else dict((curated or {}).get("matrix") or {})
    if final_matrix:
        matrix_path = out_dir / f"seq.search.{mode}.matrix.json"
        util.atomic_write_json(matrix_path, final_matrix)
        _record_local_file(ctx, matrix_path, kind="output_table", notes={"command": "seq.search"})
        info["matrix"] = {
            "path": str(matrix_path),
            "sha256": util.sha256_file(matrix_path),
            "bytes": matrix_path.stat().st_size,
        }
    return info


def _search_phmmer(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
    hmmer = _require_hmmer(ctx)
    queries = _resolve_queries(ctx, args)
    if not queries:
        raise InvalidInputError("phmmer mode needs 'queries', 'queries_file' or 'query_accessions'")
    targets = _resolve_targets(ctx, args)
    if not targets:
        raise InvalidInputError("phmmer mode needs at least one entry in 'targets'")
    E = float(args.get("E", 10.0))
    cpus = int(args.get("cpus", 4))
    max_hits = int(args.get("max_hits", 100))
    bias_filter = bool(args.get("bias_filter", False))

    forward_hits: list[dict[str, Any]] = []
    for target in targets:
        result = hmmer.phmmer(
            ctx,
            queries=queries,
            targets_fasta=Path(target["path"]),
            E=E,
            cpus=cpus,
            bias_filter=bias_filter,
            max_hits=max_hits,
        )
        for hit in result.get("hits") or []:
            record = dict(hit)
            record["target_db"] = target["name"]
            record["organism"] = target["organism"]
            forward_hits.append(record)

    if not any(warning.get("code") == "PHMMER_BIAS_FILTER_OFF" for warning in ctx.warnings):
        ctx.warn(
            "PHMMER_BIAS_FILTER_OFF",
            "phmmer ran with HMMER's compositional-bias filter disabled (bias_filter=False): the default "
            "filter hides genuine divergent homologues. Do not turn the filter off and then treat "
            "missing hits as absence.",
            context={"bias_filter": bias_filter},
        )
    if bias_filter:
        ctx.warn(
            "PHMMER_BIAS_FILTER_ON_UNEXPECTED",
            "bias_filter=True was explicitly requested; this can hide divergent homologues and is not "
            "the recommended configuration.",
            context={"bias_filter": True},
        )

    # ---- reciprocal best hits ----
    reciprocal_payload: dict[str, Any] = {
        "available": False,
        "pairs": [],
        "n_reciprocal": 0,
        "n_forward_only": 0,
        "n_reverse_only": 0,
    }
    if args.get("reciprocal", True):
        if not hasattr(hmmer, "reciprocal_best"):
            ctx.warn(
                "RECIPROCAL_SEARCH_UNAVAILABLE",
                "lib.hmmer.reciprocal_best is not available; reciprocal validation was skipped. Do not "
                "exclude homologues without the reverse search.",
            )
            reciprocal_payload["note"] = "lib.hmmer.reciprocal_best unavailable"
        elif not forward_hits:
            ctx.warn(
                "RECIPROCAL_SEARCH_NO_FORWARD_HITS",
                "No forward hits passed the E-value cutoff, so there is nothing to validate reciprocally. "
                "This is a negative result, not a reciprocal-search failure; report 'no detectable homolog' "
                "with the E cutoff and do not treat it as proof of absence.",
                context={"E": E},
            )
            reciprocal_payload["note"] = "no forward hits at the reporting cutoff; reciprocal validation not applicable"
        else:
            tmp_dir = util.ensure_dir(Path(ctx.cache_dir) / "tmp")
            queries_fasta = tmp_dir / "seq.search.queries.fasta"
            util.write_fasta(queries_fasta, sorted(queries.items()))
            all_pairs: list[dict[str, Any]] = []
            n_reciprocal = n_forward_only = n_reverse_only = 0
            for target in targets:
                target_hits = [hit for hit in forward_hits if hit.get("target_db") == target["name"]]
                hit_sequences: dict[str, str] = {}
                sequence_lookup = target["sequences"]
                for hit in target_hits:
                    target_name = str(hit.get("target") or "")
                    sequence = sequence_lookup.get(target_name)
                    if sequence is None:
                        match = _UNIPROT_ACC.search(target_name) or re.search(r"([A-Z0-9]{6,10})", target_name)
                        if match:
                            sequence = sequence_lookup.get(match.group(1))
                    if sequence:
                        hit_sequences[target_name] = sequence
                if not hit_sequences:
                    continue
                try:
                    reverse = hmmer.phmmer(
                        ctx,
                        queries=hit_sequences,
                        targets_fasta=queries_fasta,
                        E=E,
                        cpus=cpus,
                        bias_filter=bias_filter,
                        max_hits=max_hits,
                    )
                    pairs = hmmer.reciprocal_best(ctx, target_hits, reverse.get("hits") or [])
                except LifesciError as err:
                    ctx.warn(
                        "RECIPROCAL_SEARCH_FAILED",
                        f"Reciprocal search for {target['name']} failed ({err.code}); reciprocal validation "
                        "is incomplete for that target database.",
                        context={"target": target["name"], "error": err.message},
                    )
                    continue
                for pair in pairs.get("pairs") or []:
                    pairs_record = dict(pair)
                    pairs_record["target_db"] = target["name"]
                    all_pairs.append(pairs_record)
                n_reciprocal += int(pairs.get("n_reciprocal") or 0)
                n_forward_only += int(pairs.get("n_forward_only") or 0)
                n_reverse_only += int(pairs.get("n_reverse_only") or 0)
            reciprocal_payload = {
                "available": True,
                "pairs": all_pairs,
                "n_reciprocal": n_reciprocal,
                "n_forward_only": n_forward_only,
                "n_reverse_only": n_reverse_only,
                "restricted_reverse_database": True,
                "reverse_database": f"supplied query set ({len(queries)} sequence(s))",
                "note": (
                    "The reverse search used only the supplied query sequences, not the full source "
                    "proteome. A forward hit can therefore not lose to an unsearched paralogue of the "
                    "query protein; reciprocal support here validates the query-set relationship, not "
                    "full proteome-wide orthology. Supply a query proteome or treat the result as a "
                    "restricted query-set check."
                ),
            }
            ctx.warn(
                "RECIPROCAL_RESTRICTED_QUERY_DB",
                f"Reciprocal search used only the {len(queries)} supplied query sequence(s) as the reverse "
                "database (not the full source proteome); do not present it as complete reciprocal-best-hit "
                "orthology evidence.",
                context={"n_queries": len(queries)},
            )
    # ---- optional domain scan -> curated presence cells ----
    domain_models = _resolve_hmms(ctx, args)
    domain_scan: list[dict[str, Any]] = []
    if domain_models:
        hmm_paths = [Path(model["path"]) for model in domain_models if model.get("path")]
        for target in targets:
            result = hmmer.hmmsearch(
                ctx,
                hmm_paths=hmm_paths,
                targets_fasta=Path(target["path"]),
                E=E,
                cpus=cpus,
                max_hits=max_hits,
            )
            for hit in result.get("hits") or []:
                record = dict(hit)
                record["target_db"] = target["name"]
                record["organism"] = target["organism"]
                domain_scan.append(record)

    factor_queries = {
        str(factor): [str(name) for name in names] for factor, names in (args.get("factor_queries") or {}).items()
    }
    cells: list[dict[str, Any]] = []
    matrix: dict[str, dict[str, str]] = {}
    counts_by_factor: dict[str, dict[str, int]] = {}
    curated_payload: dict[str, Any] | None = None
    if domain_scan:
        from ..lib import evidence as evidence_lib

        declared = _declared_factor_models(args)
        searched_models = {
            model
            for model in (
                _normalise_model_id(record.get("id")) for record in domain_models if record.get("id")
            )
            if model
        } or {
            model
            for model in (
                _normalise_model_id(hit.get("model")) for hit in domain_scan if hit.get("model")
            )
            if model
        }
        model_to_factor: dict[str, str] = {}
        for factor, declared_models in declared.items():
            for model in declared_models:
                model_to_factor[model] = factor
        # A scanned model without a declared factor becomes a factor named by the
        # model id, so a domain scan always yields a presence matrix.
        for model in searched_models:
            model_to_factor.setdefault(model, model)
        signatures = _signatures_from_model_map(
            {model: factor for model, factor in model_to_factor.items() if model in searched_models}
        )
        cell_hits: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for hit in domain_scan:
            model = _normalise_model_id(hit.get("model"))
            factor = model_to_factor.get(model) if model else None
            if not factor:
                continue
            organism = str(hit.get("organism") or "")
            cell_hits.setdefault((organism, factor), []).append(
                {
                    "protein": hit.get("target"),
                    "model": model,
                    "evalue": hit.get("evalue"),
                    "score": hit.get("score"),
                    "model_cov": hit.get("model_cov"),
                    "protein_length": hit.get("target_length"),
                    "description": hit.get("description"),
                }
            )
        organisms = [target["organism"] for target in targets]
        searched_models = {
            str(model.get("id") or "").strip().upper()
            for model in domain_models
            if model.get("id")
        } or {
            str(hit.get("model") or "").split(".")[0].upper()
            for hit in domain_scan
            if hit.get("model")
        }
        factors = sorted(signatures)
        requested_factors = list(declared)
        not_searched = [factor for factor in requested_factors if factor not in signatures]
        if not_searched:
            ctx.warn(
                "FACTOR_NOT_SEARCHED",
                f"Requested factor(s) {not_searched} have no signature model in the supplied HMMs; "
                "their cells are omitted (or reported as unknown) rather than called absent.",
                context={"requested": requested_factors, "searched_models": sorted(searched_models)},
            )
        for organism in organisms:
            for factor in factors:
                cell = {"organism": organism, "factor": factor, "hits": cell_hits.get((organism, factor), [])}
                queries_in_factor = factor_queries.get(factor) or []
                if queries_in_factor:
                    candidates = [
                        hit
                        for hit in forward_hits
                        if hit.get("target_db") == next(
                            (target["name"] for target in targets if target["organism"] == organism), organism
                        )
                        and str(hit.get("query")) in queries_in_factor
                    ]
                    if candidates:
                        best = min(candidates, key=_evalue_key)
                        cell["sequence_hit"] = {
                            "protein": best.get("target"),
                            "evalue": util.parse_float(best.get("evalue")),
                            "coverage": util.parse_float(best.get("qcov")),
                            "score": util.parse_float(best.get("score")),
                        }
                cells.append(cell)
        curated_payload = evidence_lib.curate_cells(cells, factor_signatures=signatures)
        matrix = curated_payload.get("matrix") or {}
        counts_by_factor = curated_payload.get("counts_by_factor") or {}
        ctx.warn_evalue_db_dependence(
            "local_hmmsearch",
            "Curated domain E-values come from a local hmmsearch against the supplied target proteomes; they "
            "are not comparable to values from other databases or protein sets.",
        )
    elif factor_queries:
        from ..lib import evidence as evidence_lib  # noqa: F401 - ensure wording is consistent
        present_evalue = float(args.get("present_evalue", 1e-3))
        weak_evalue = float(args.get("weak_evalue", 1.0))
        for target in targets:
            for factor, query_names in factor_queries.items():
                candidates = [
                    hit
                    for hit in forward_hits
                    if hit.get("target_db") == target["name"] and str(hit.get("query")) in query_names
                ]
                best = min(candidates, key=_evalue_key) if candidates else None
                evalue = util.parse_float(best.get("evalue")) if best else None
                verdict = _sequence_verdict(evalue, present_evalue, weak_evalue)
                matrix.setdefault(target["organism"], {})[factor] = verdict
                cells.append(
                    {
                        "organism": target["organism"],
                        "factor": factor,
                        "verdict": verdict,
                        "evidence": (
                            f"phmmer best hit E={util.fmt_evalue(evalue)} ({best.get('target')})"
                            if best and evalue is not None
                            else "no phmmer hit"
                        ),
                        "best_hit": best.get("target") if best else None,
                        "evalue": evalue,
                        "sequence_hit": {
                            "protein": best.get("target") if best else None,
                            "evalue": evalue,
                            "coverage": util.parse_float(best.get("qcov")) if best else None,
                        },
                    }
                )
                counts_by_factor.setdefault(factor, {"present": 0, "weak": 0, "absent": 0})[verdict] += 1

    n_present = sum(1 for row in matrix.values() for verdict in row.values() if verdict == "present")
    n_weak = sum(1 for row in matrix.values() for verdict in row.values() if verdict == "weak")
    n_absent = sum(1 for row in matrix.values() for verdict in row.values() if verdict == "absent")
    ctx.warn_evalue_db_dependence(
        "phmmer",
        "E-values from phmmer against one proteome are not comparable with searches against other proteomes or "
        "databases; the target-database size differs.",
    )
    if matrix:
        if n_absent:
            ctx.warn(
                "ABSENCE_NOT_PROOF",
                "No accepted candidate in at least one organism/factor cell; report as 'no detectable homolog', "
                "not as proven absence.",
                context={"n_absent": n_absent},
            )
        for organism, row in matrix.items():
            for factor, verdict in row.items():
                if verdict == "weak":
                    ctx.review(
                        "SINGLE_WEAK_LINE_EVIDENCE",
                        f"{organism} {factor}: weak presence call from a single marginal line; human review required.",
                        context={"organism": organism, "factor": factor},
                    )
    out_info = _write_outputs(
        ctx,
        args,
        mode="phmmer",
        curated=curated_payload,
        hits=forward_hits + domain_scan,
        matrix=matrix,
    )
    return {
        "dry_run": False,
        "mode": "phmmer",
        "queries": [
            {"name": name, "length": len(sequence)} for name, sequence in sorted(queries.items())
        ],
        "targets": [
            {"name": target["name"], "organism": target["organism"], "taxid": target.get("taxid"), "n_sequences": target["n_sequences"]}
            for target in targets
        ],
        "hits": forward_hits,
        "reciprocal": reciprocal_payload,
        "domain_scan": domain_scan,
        "cells": curated_payload.get("cells") if curated_payload else cells,
        "matrix": matrix,
        "n_queries": len(queries),
        "n_targets": len(targets),
        "n_hits": len(forward_hits),
        "counts": {
            "n_queries": len(queries),
            "n_targets": len(targets),
            "n_hits": len(forward_hits),
            "n_domain_hits": len(domain_scan),
            "n_cells": len(curated_payload.get("cells") if curated_payload else cells),
            "n_present": n_present,
            "n_weak": n_weak,
            "n_absent": n_absent,
        },
        "counts_by_factor": counts_by_factor,
        "params": {"E": E, "cpus": cpus, "max_hits": max_hits, "bias_filter": bias_filter},
        "resolved": {
            "cache_dir": str(ctx.cache_dir),
            "out": out_info,
            "hmmer_version": None,
            "domain_models": [model["id"] for model in domain_models],
        },
    }


def _search_hmmsearch(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
    hmmer = _require_hmmer(ctx)
    models = _resolve_hmms(ctx, args)
    if not models:
        raise InvalidInputError("hmmsearch mode needs 'hmms' or 'hmm_ids'")
    targets = _resolve_targets(ctx, args)
    if not targets:
        raise InvalidInputError("hmmsearch mode needs at least one entry in 'targets'")
    E = float(args.get("E", 10.0))
    cpus = int(args.get("cpus", 4))
    max_hits = int(args.get("max_hits", 100))
    hmm_paths = [Path(model["path"]) for model in models if model.get("path")]
    hits: list[dict[str, Any]] = []
    for target in targets:
        result = hmmer.hmmsearch(ctx, hmm_paths=hmm_paths, targets_fasta=Path(target["path"]), E=E, cpus=cpus, max_hits=max_hits)
        for hit in result.get("hits") or []:
            record = dict(hit)
            record["target_db"] = target["name"]
            record["organism"] = target["organism"]
            hits.append(record)

    from ..lib import evidence as evidence_lib

    declared = _declared_factor_models(args)
    searched_models = {
        model
        for model in (
            _normalise_model_id(record.get("id")) for record in models if record.get("id")
        )
        if model
    } or {
        model
        for model in (_normalise_model_id(hit.get("model")) for hit in hits if hit.get("model"))
        if model
    }
    model_to_factor: dict[str, str] = {}
    for factor, declared_models in declared.items():
        for model in declared_models:
            model_to_factor[model] = factor
    # A scanned model without a declared factor becomes a factor named by the
    # model id (e.g. PF00042), so a plain HMM scan still yields a presence matrix.
    for model in searched_models:
        model_to_factor.setdefault(model, model)
    signatures = _signatures_from_model_map(
        {model: factor for model, factor in model_to_factor.items() if model in searched_models}
    )
    cell_hits: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for hit in hits:
        model = _normalise_model_id(hit.get("model"))
        factor = model_to_factor.get(model) if model else None
        if not factor:
            continue
        organism = str(hit.get("organism") or "")
        cell_hits.setdefault((organism, factor), []).append(
            {
                "protein": hit.get("target"),
                "model": model,
                "evalue": hit.get("evalue"),
                "score": hit.get("score"),
                "model_cov": hit.get("model_cov"),
                "protein_length": hit.get("target_length"),
                "description": hit.get("description"),
            }
        )
    # Every scanned model receives a presence/absence verdict: declared factors
    # keep their name, unmapped models are their own factor (named by model id).
    factors = sorted(signatures)
    requested = list(declared)
    not_searched = [factor for factor in requested if factor not in signatures]
    if not_searched:
        ctx.warn(
            "FACTOR_NOT_SEARCHED",
            f"Requested factor(s) {not_searched} have no signature model in the supplied HMMs; their cells "
            "are omitted rather than called absent.",
            context={"requested": requested, "searched_models": sorted(searched_models)},
        )
    cells = []
    for target in targets:
        for factor in factors:
            cells.append(
                {
                    "organism": target["organism"],
                    "factor": factor,
                    "hits": cell_hits.get((target["organism"], factor), []),
                }
            )
    curated = evidence_lib.curate_cells(cells, factor_signatures=signatures)
    matrix = curated.get("matrix") or {}
    n_present = int(curated.get("n_present") or 0)
    n_weak = int(curated.get("n_weak") or 0)
    n_absent = int(curated.get("n_absent") or 0)
    ctx.warn_evalue_db_dependence(
        "local_hmmsearch",
        "Local hmmsearch E-values scale with the number of target sequences; they are not comparable to other "
        "database searches.",
    )
    if n_absent:
        ctx.warn(
            "ABSENCE_NOT_PROOF",
            "No accepted candidate in at least one organism/factor cell; report as 'no detectable homolog', not as "
            "proven absence.",
            context={"n_absent": n_absent},
        )
    for cell in curated.get("cells") or []:
        if cell["verdict"] == "weak":
            ctx.review(
                "SINGLE_WEAK_LINE_EVIDENCE",
                f"{cell['organism']} {cell['factor']}: weak presence call; human review required.",
                context={"organism": cell["organism"], "factor": cell["factor"]},
            )
    out_info = _write_outputs(ctx, args, mode="hmmsearch", curated=curated, hits=hits, matrix=matrix)
    return {
        "dry_run": False,
        "mode": "hmmsearch",
        "queries": [{"name": model["id"], "path": model.get("path")} for model in models],
        "targets": [
            {"name": target["name"], "organism": target["organism"], "taxid": target.get("taxid"), "n_sequences": target["n_sequences"]}
            for target in targets
        ],
        "hits": hits,
        "reciprocal": {"available": False, "pairs": [], "note": "hmmsearch mode does not run reciprocal searches"},
        "domain_scan": hits,
        "cells": curated.get("cells") or [],
        "matrix": matrix,
        "n_queries": len(models),
        "n_targets": len(targets),
        "n_hits": len(hits),
        "counts": {
            "n_queries": len(models),
            "n_targets": len(targets),
            "n_hits": len(hits),
            "n_cells": len(curated.get("cells") or []),
            "n_present": n_present,
            "n_weak": n_weak,
            "n_absent": n_absent,
        },
        "counts_by_factor": curated.get("counts_by_factor") or {},
        "params": {"E": E, "cpus": cpus, "max_hits": max_hits, "bias_filter": False},
        "resolved": {
            "cache_dir": str(ctx.cache_dir),
            "out": out_info,
            "domain_models": [model["id"] for model in models],
        },
    }


@command(
    name="seq.search",
    title="Homolog search and presence matrix",
    summary="pyhmmer phmmer/hmmsearch or offline replay of recorded evidence, curated into a presence matrix.",
    description=(
        "Detect homologs and build a presence/absence matrix with per-cell evidence. Modes: 'phmmer' runs "
        "local pyhmmer phmmer (compositional-bias filter OFF by design; reciprocal best-hit checks) and "
        "can add a Pfam/HMM domain scan that curates cells; 'hmmsearch' scans profile HMMs against "
        "proteomes and curates them with caller-supplied factor signatures (unmapped models become "
        "factors named by model id); 'evidence' replays recorded per-model Pfam hit rows (pfam_hits.tsv "
        "format), optional sequence rows and reciprocal-check rows offline and curates a presence "
        "matrix with evidence strings. Factors and their models are declared via factor_queries / "
        "factor_models. Mandatory caveats are always reported: absent means 'no detectable homolog' not "
        "proof of absence; E-values depend on database size and are not comparable across "
        "proteomes/databases; single-sequence search misses very divergent proteins, so domain/family "
        "evidence is the arbiter; weak calls are flagged for human review. Reciprocal checks are "
        "reported when available; without them sequence-level exclusions are not corroborated."
    ),
    input_schema=INPUT_SCHEMA,
    output_schema="seq.search.schema.json",
    category="sequence",
    timeout_ms=600_000,
    danger="compute",
    examples=(
        {
            "mode": "evidence",
            "rows": [
                {
                    "organism": "H. sapiens",
                    "factor": "globin",
                    "pfam_model": "PF00042",
                    "best_hit": "P69905",
                    "evalue": 1e-30,
                    "score": 120.0,
                    "model_cov": 0.95,
                    "protein_length": 142,
                }
            ],
            "factor_models": {"globin": ["PF00042"]},
        },
        {
            "mode": "phmmer",
            "queries": {
                "globin:P69905": (
                    "MVLSPADKTNVKAAWGKVGAHAGEYGAEALERMFLSFPTTKTYFPHFDLSHGSAQVKGHGKKVADALTNA"
                    "VAHVDDMPNALSALSDLHAHKLRVDPVNFKLLSHCLLVTLAAHLPAEFTPAVHASLDKFLASVSTVLTSKYR"
                )
            },
            "targets": [{"fasta": "proteomes/UP000005640.fasta", "organism": "H. sapiens"}],
            "factor_queries": {"globin": ["globin:P69905"]},
        },
        {
            "mode": "hmmsearch",
            "hmm_ids": ["PF00042"],
            "targets": [{"fasta": "proteomes/UP000005640.fasta", "organism": "H. sapiens"}],
        },
    ),
)
def run(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
    """Run one seq.search mode and return hits, reciprocal checks, cells and matrix."""
    ctx.validate()
    mode = str(args.get("mode") or "").strip().lower()
    if mode not in {"phmmer", "hmmsearch", "evidence"}:
        raise InvalidInputError(f"unknown seq.search mode {mode!r}; expected phmmer, hmmsearch or evidence")
    if ctx.dry_run:
        return _plan(args, ctx)
    if mode == "evidence":
        return _evidence_mode(args, ctx)
    if mode == "phmmer":
        return _search_phmmer(args, ctx)
    return _search_hmmsearch(args, ctx)
