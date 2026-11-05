"""``lifesci.align.map`` — map sequences onto a reference MSA row and measure features.

This is the alignment-mapping recipe: a novel sequence is aligned to a
reference row of an MSA with tuned Biopython ``PairwiseAligner`` settings
(match +2, mismatch -1, open -8, extend -0.5, cheap end gaps), giving a
residue→alignment-column map.  Predefined column spans (features) are then
counted in the query, so per-sequence feature lengths are measured in a common
coordinate system instead of by sequence annotation.

Correctness caveats are surfaced automatically:

* ``LOW_IDENTITY_MAPPING`` — identity below ``min_identity`` (default 0.60): the
  residue assignment may be wrong;
* ``FEATURE_SPAN_UNSTABLE`` — feature spans evolve by indels and column-span
  counts are unreliable at low identity, so report the identity alongside every
  length.

Feature definitions are accepted in two coordinate systems: alignment **column
spans** (0-based inclusive, directly comparable across rows) or **reference row**
coordinates (1-based ungapped positions, converted through the reference row).
``reference_row: "auto"`` tries every candidate reference row per query and
keeps the row with the highest identity (ties keep the first row in file order);
the chosen row is reported per query.  Restrict the candidates with
``reference_rows`` for large alignments.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Mapping

from .. import util
from ..commands.base import command
from ..errors import DataError, InvalidInputError, NotFoundError, UsageError

INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "oneOf": [{"required": ["queries"]}, {"required": ["query_fasta"]}],
    "properties": {
        "alignment": {
            "type": "string",
            "description": (
                "Path to the reference alignment (FASTA, Stockholm, or Clustal; gzip ok). "
                "Rows may carry '/start-end' coordinate suffixes, which are matched leniently."
            ),
        },
        "reference_row": {
            "type": "string",
            "description": (
                "Name of the alignment row used as the reference coordinate system; the "
                "'/start-end' suffix may be omitted. Defaults to the first row. Use 'auto' to "
                "align each query to every candidate row and keep the row with the highest "
                "identity (ties keep the first row in file order) — the chosen row is reported "
                "per query in data.queries[].reference_row."
            ),
        },
        "reference_rows": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Only with reference_row='auto': restrict the candidate reference rows to this "
                "list of names (recommended for large alignments; a warning is emitted when more "
                "than 50 candidates are tried)."
            ),
        },
        "queries": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["name", "sequence"],
                "properties": {
                    "name": {"type": "string", "description": "Query identifier used in the output."},
                    "sequence": {"type": "string", "description": "Ungapped (or gap-stripped) nucleotide/protein query sequence."},
                },
            },
            "description": "Inline query sequences (alternative to query_fasta).",
        },
        "query_fasta": {
            "type": "string",
            "description": "Path to a FASTA file of query sequences (alternative to queries).",
        },
        "feature_spans": {
            "type": "object",
            "additionalProperties": True,
            "description": (
                "Feature definitions as alignment-column spans (0-based inclusive), e.g. "
                "{'feature_a': [[10, 60]], 'feature_b': [[200, 260]]}. A flat [start,end] pair "
                "or a list of column indices is also accepted."
            ),
        },
        "feature_coords": {
            "type": "object",
            "additionalProperties": True,
            "description": (
                "Feature definitions as 1-based ungapped reference-row coordinates, e.g. "
                "{'feature_a': [[11, 61]], 'feature_b': [[201, 261]]}; converted to column "
                "spans through the reference row."
            ),
        },
        "coords_reference_row": {
            "type": "string",
            "description": (
                "Alignment row whose ungapped sequence defines feature_coords/reference "
                "coordinates (default: the reference row). Use the row the coordinates were "
                "annotated on when it differs from the mapping reference row."
            ),
        },
        "features_file": {
            "type": "string",
            "description": (
                "JSON file with feature definitions. Accepts a plain {feature: span(s)} map "
                "(treated as column spans by default), or keys "
                "feature_spans/feature_coords/coordinate_system."
            ),
        },
        "min_identity": {
            "type": "number",
            "minimum": 0.0,
            "maximum": 1.0,
            "default": 0.6,
            "description": "Identity threshold below which LOW_IDENTITY_MAPPING and FEATURE_SPAN_UNSTABLE are raised.",
        },
        "out": {
            "type": "string",
            "description": "Optional path to write the mapping payload JSON.",
        },
    },
    "required": ["alignment"],
}

_FEATURE_KEY_RE = re.compile(r"^(?:feature_)?(?:spans|columns)$", re.IGNORECASE)
_COORD_KEY_RE = re.compile(r"^(?:feature_)?(?:coords|coordinates|reference_coords)$", re.IGNORECASE)


def _norm_row_name(name: str) -> str:
    """Normalise an alignment row name (drop coordinates suffix, keep first token)."""
    token = str(name).strip().split()[0] if str(name).strip() else ""
    return re.sub(r"/\d+-\d+$", "", token)


def _normalise_spans(value: Any) -> list[list[int]]:
    """Normalise one feature's column definition into ``[[start, end], ...]`` (inclusive)."""
    if value is None:
        return []
    if isinstance(value, int):
        return [[value, value]]
    if isinstance(value, dict):
        for key in ("columns", "spans", "cols"):
            if key in value:
                return _normalise_spans(value[key])
        raise InvalidInputError(
            "unsupported feature span object; expected {'columns': [...]} or a span list",
            details={"value": util.truncate(util.canonical_json(value), 200)},
        )
    if isinstance(value, (list, tuple)):
        if not value:
            return []
        if all(isinstance(item, int) for item in value):
            if len(value) == 2:
                return [[int(value[0]), int(value[1])]]
            return [[int(item), int(item)] for item in value]
        spans: list[list[int]] = []
        for item in value:
            spans.extend(_normalise_spans(item))
        return spans
    raise InvalidInputError(
        f"unsupported feature span value of type {type(value).__name__}",
        details={"value": util.truncate(str(value), 120)},
    )


def _normalise_coords(value: Any) -> list[list[int]]:
    """Normalise one feature's reference-coordinate definition into pairs of 1-based positions."""
    if value is None:
        return []
    if isinstance(value, dict):
        for key in ("coords", "coordinates", "positions"):
            if key in value:
                return _normalise_coords(value[key])
        raise InvalidInputError("unsupported feature coordinate object")
    if isinstance(value, (list, tuple)):
        if not value:
            return []
        if all(isinstance(item, (int, float)) for item in value) and len(value) == 2:
            return [[int(value[0]), int(value[1])]]
        pairs: list[list[int]] = []
        for item in value:
            if isinstance(item, (list, tuple)) and len(item) == 2:
                pairs.append([int(item[0]), int(item[1])])
            else:
                pairs.extend(_normalise_coords(item))
        return pairs
    raise InvalidInputError(
        f"unsupported feature coordinate value of type {type(value).__name__}"
    )


def _load_feature_file(path: Path) -> tuple[dict[str, list[list[int]]], dict[str, list[list[int]]]]:
    """Split a feature JSON file into (column spans, reference coords)."""
    payload = util.read_json(path)
    if not isinstance(payload, dict):
        raise DataError(
            f"feature file must contain a JSON object: {path}",
            details={"path": str(path)},
        )
    coordinate_system = str(payload.get("coordinate_system", "columns")).lower()
    spans: dict[str, list[list[int]]] = {}
    coords: dict[str, list[list[int]]] = {}
    source = payload.get("features") if isinstance(payload.get("features"), dict) else payload

    def _looks_like_feature(value: Any) -> bool:
        """Distinguish feature definitions from metadata values in the same JSON object."""
        if isinstance(value, (int, list, tuple)):
            return True
        if isinstance(value, Mapping):
            return any(key in value for key in ("columns", "spans", "cols", "coords", "coordinates", "start", "end"))
        return False

    _metadata_keys = {
        "coordinate_system", "description", "source", "version", "reference_row",
        "alignment_length", "reference_ungapped_length", "coordinate_note",
    }
    for key, value in source.items():
        key_text = str(key)
        if key_text in _metadata_keys:
            continue
        if not (_FEATURE_KEY_RE.match(key_text) or _COORD_KEY_RE.match(key_text) or _looks_like_feature(value)):
            continue
        if _FEATURE_KEY_RE.match(key_text):
            if isinstance(value, dict):
                spans.update({str(name): _normalise_spans(definition) for name, definition in value.items()})
            continue
        if _COORD_KEY_RE.match(key_text):
            if isinstance(value, dict):
                coords.update({str(name): _normalise_coords(definition) for name, definition in value.items()})
            continue
        if coordinate_system in {"reference", "coords", "coordinates"}:
            coords[key_text] = _normalise_coords(value)
        else:
            spans[key_text] = _normalise_spans(value)
    return {name: value for name, value in spans.items() if value}, {
        name: value for name, value in coords.items() if value
    }


def _record_local(ctx: Any, path: Path, *, role: str) -> None:
    from .. import provenance as provenance_mod

    if not path.is_file():
        return
    log = getattr(ctx, "_provenance_log", None)
    if log is None:
        log = provenance_mod.ProvenanceLog(ctx.log_path)
        ctx._provenance_log = log
    entry = log.local_file(
        path=path, sha256=util.sha256_file(path), bytes_=path.stat().st_size, notes={"role": role}
    )
    ctx.provenance.append(entry)


def _load_queries(args: dict[str, Any], ctx: Any) -> tuple[list[dict[str, str]], dict[str, Any] | None]:
    """Load inline or FASTA queries; returns (queries, fasta_metadata)."""
    inline = args.get("queries")
    fasta_path = args.get("query_fasta")
    if inline is not None and fasta_path is not None:
        raise UsageError("provide only one of 'queries' and 'query_fasta'")
    if inline is not None:
        queries = []
        for index, record in enumerate(inline):
            name = str(record.get("name") or f"query_{index + 1}")
            sequence = re.sub(r"[-.\s]", "", str(record.get("sequence") or "")).upper()
            if not sequence:
                raise InvalidInputError(f"query {name!r} has an empty sequence")
            queries.append({"name": name, "sequence": sequence})
        return queries, None
    if not fasta_path:
        raise UsageError("align.map requires 'queries' or 'query_fasta'")
    path = Path(str(fasta_path)).expanduser()
    if not path.is_absolute():
        path = (Path(ctx.cwd) / path).resolve()
    if not path.is_file():
        raise NotFoundError(
            f"query FASTA does not exist: {path}",
            code="QUERY_FILE_NOT_FOUND",
            details={"path": str(path)},
        )
    records = util.read_fasta(path)
    queries = []
    for header, sequence in records:
        name = _norm_row_name(header) or header.strip()
        cleaned = re.sub(r"[-.\s]", "", sequence).upper()
        if cleaned:
            queries.append({"name": name, "sequence": cleaned})
    if not queries:
        raise DataError(f"no usable query sequences in {path}", details={"path": str(path)})
    _record_local(ctx, path, role="align_map_queries")
    return queries, {
        "path": str(path),
        "sha256": util.sha256_file(path),
        "bytes": path.stat().st_size,
        "n_queries": len(queries),
    }


@command(
    name="align.map",
    title="Map sequences onto a reference MSA row and measure feature spans",
    summary="Project query sequences onto a reference alignment row; report identity, coverage, feature lengths.",
    description=(
        "Aligns each query to a reference row of an MSA using tuned PairwiseAligner settings "
        "(match +2, mismatch -1, gap open -8, extend -0.5, cheap end gaps), records the "
        "residue→column map, and counts query residues inside feature column spans or "
        "reference-row coordinate ranges. Returns per query: length, n_aligned, identity, "
        "coverage, per-feature lengths + residues, and flags. Warns LOW_IDENTITY_MAPPING below "
        "min_identity (default 0.60) and FEATURE_SPAN_UNSTABLE because feature-span column "
        "counts are unreliable at low identity — always report identity with the lengths. "
        "reference_row='auto' tries every candidate row per query and keeps the "
        "highest-identity one (restrict with reference_rows); the chosen row is reported per "
        "query. Use feature_spans (0-based inclusive alignment columns) or feature_coords "
        "(1-based ungapped reference-row positions)."
    ),
    input_schema=INPUT_SCHEMA,
    output_schema="align.map.schema.json",
    category="alignment",
    timeout_ms=300_000,
    examples=(
        {
            "alignment": "alignments/reference.fa",
            "reference_row": "reference_1",
            "queries": [
                {"name": "sample_1", "sequence": "ACGTACGTACGTACGTACGTACGTACGTACGTACGTACGTACGT"},
            ],
            "feature_spans": {"feature_a": [[2, 6]], "feature_b": [[10, 14]]},
        },
    ),
    danger="read",
)
def run(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
    ctx.validate()
    from ..lib import alignment as alignment_lib

    alignment_path = Path(str(args.get("alignment") or "")).expanduser()
    if not alignment_path.is_absolute():
        alignment_path = (Path(ctx.cwd) / alignment_path).resolve()
    if not alignment_path.is_file():
        raise NotFoundError(
            f"alignment file does not exist: {alignment_path}",
            code="ALIGNMENT_NOT_FOUND",
            details={"path": str(alignment_path)},
        )
    _record_local(ctx, alignment_path, role="align_map_alignment")
    alignment = alignment_lib.read_alignment(alignment_path)
    records = alignment.get("records") or []
    if not records:
        raise DataError(
            f"alignment contains no sequences: {alignment_path}",
            details={"path": str(alignment_path)},
        )

    # -- resolve the reference row(s) -------------------------------------
    # Fixed mode: one row defines the column coordinate system.  ``auto`` mode
    # tries every candidate row per query and keeps the row with the highest
    # alignment identity (ties keep the first row in file order).  Feature spans
    # are MSA-global columns, so they are unaffected by which row was chosen.
    requested = args.get("reference_row")
    requested_text = str(requested) if requested is not None else ""
    auto_reference = requested_text.strip().lower() == "auto"
    available_rows: list[str] = [str(record.get("name", "")) for record in records]
    records_by_norm: dict[str, dict[str, Any]] = {}
    for record in records:
        records_by_norm.setdefault(_norm_row_name(str(record.get("name", ""))), record)
    auto_candidates: list[dict[str, Any]] = []
    reference_aligned = ""
    if auto_reference:
        candidate_names = args.get("reference_rows")
        if candidate_names:
            missing_rows: list[str] = []
            for candidate_name in candidate_names:
                record = records_by_norm.get(_norm_row_name(str(candidate_name)))
                if record is None:
                    missing_rows.append(str(candidate_name))
                else:
                    auto_candidates.append(record)
            if missing_rows:
                ctx.warn(
                    "REFERENCE_ROW_NOT_FOUND",
                    f"{len(missing_rows)} requested reference candidate(s) were not found and "
                    "were skipped.",
                    severity="info",
                    context={"missing": missing_rows, "available_rows": available_rows[:50]},
                )
            if not auto_candidates:
                raise NotFoundError(
                    "none of the requested reference_rows candidates exist in the alignment",
                    code="REFERENCE_ROW_MISSING",
                    details={
                        "reference_rows": [str(name) for name in candidate_names],
                        "available_rows": available_rows[:50],
                    },
                )
        else:
            auto_candidates = list(records)
        if len(auto_candidates) > 50:
            ctx.warn(
                "MANY_REFERENCE_CANDIDATES",
                f"reference_row='auto' will align every query against {len(auto_candidates)} "
                "candidate rows, which can be slow on large alignments; restrict the search with "
                "the reference_rows input to the relevant rows.",
                context={"n_candidates": len(auto_candidates)},
            )
        reference_name = "auto"
    else:
        reference_text = requested_text or available_rows[0]
        reference_record = records_by_norm.get(_norm_row_name(reference_text))
        if reference_record is None:
            raise NotFoundError(
                f"reference row {reference_text!r} not found in {alignment_path.name}",
                code="REFERENCE_ROW_MISSING",
                details={
                    "reference_row": reference_text,
                    "normalised": _norm_row_name(reference_text),
                    "available_rows": available_rows[:50],
                    "n_sequences": len(records),
                },
                fallbacks=["List the alignment row names and pass one as reference_row."],
            )
        reference_aligned = str(reference_record.get("seq") or "")
        reference_name = str(reference_record.get("name", reference_text))

    # -- feature definitions ----------------------------------------------
    spans: dict[str, list[list[int]]] = {}
    coords: dict[str, list[list[int]]] = {}
    features_file = args.get("features_file")
    if features_file:
        feature_path = Path(str(features_file)).expanduser()
        if not feature_path.is_absolute():
            feature_path = (Path(ctx.cwd) / feature_path).resolve()
        if not feature_path.is_file():
            raise NotFoundError(
                f"feature definitions file does not exist: {feature_path}",
                code="FEATURES_FILE_NOT_FOUND",
                details={"path": str(feature_path)},
            )
        file_spans, file_coords = _load_feature_file(feature_path)
        spans.update(file_spans)
        coords.update(file_coords)
        _record_local(ctx, feature_path, role="align_map_features")
    for name, definition in (args.get("feature_spans") or {}).items():
        spans[str(name)] = _normalise_spans(definition)
    for name, definition in (args.get("feature_coords") or {}).items():
        coords[str(name)] = _normalise_coords(definition)

    feature_definitions: dict[str, dict[str, Any]] = {}
    if coords:
        # Feature coordinates are ungapped positions on the coordinates reference row
        # (default: the mapping reference row; use the annotation row when it differs).
        coords_row_text = args.get("coords_reference_row")
        if not coords_row_text:
            if auto_reference:
                raise UsageError(
                    "feature_coords requires an explicit coords_reference_row when "
                    "reference_row='auto' (the coordinate system row cannot be inferred)",
                    code="COORDS_REFERENCE_ROW_REQUIRED",
                    details={"feature_coords": sorted(coords)},
                )
            coords_row_text = reference_name
        coords_row_text = str(coords_row_text)
        coords_target = _norm_row_name(coords_row_text)
        coords_record = records_by_norm.get(coords_target)
        coords_row_aligned = str((coords_record or {}).get("seq") or "") if coords_record else None
        if coords_row_aligned is None:
            raise NotFoundError(
                f"feature-coordinate reference row {coords_row_text!r} not found in {alignment_path.name}",
                code="REFERENCE_ROW_MISSING",
                details={
                    "coords_reference_row": coords_row_text,
                    "normalised": coords_target,
                    "available_rows": available_rows[:50],
                },
            )
        # The library helper converts one 1-based [start, end] reference range per
        # feature; multi-range features call it per range and merge the spans.
        for name, definition in coords.items():
            merged: list[list[int]] = []
            for pair in definition:
                converted = alignment_lib.column_span_from_reference(
                    coords_row_aligned, {name: pair}
                )
                merged.extend(_normalise_spans(converted.get(name)))
            if merged:
                spans.setdefault(name, [])
                spans[name] = list(spans[name]) + merged
                feature_definitions[name] = {
                    "source": "reference_coords",
                    "coords": definition,
                    "coords_reference_row": coords_row_text,
                    "spans": spans[name],
                }
    for name in spans:
        feature_definitions.setdefault(
            name, {"source": "columns", "spans": spans[name]}
        )
        feature_definitions[name].setdefault("spans", spans[name])
    spans = {name: value for name, value in spans.items() if value}
    if not spans:
        ctx.warn(
            "FEATURE_SPANS_MISSING",
            "No feature spans/coords were supplied: only identity, coverage, and alignment "
            "statistics are reported (no feature lengths).",
            severity="info",
        )

    # -- queries -----------------------------------------------------------
    queries, query_file = _load_queries(args, ctx)
    min_identity = float(args.get("min_identity", 0.6))
    query_payload: list[dict[str, Any]] = []
    low_identity: list[dict[str, Any]] = []
    for query in queries:
        if auto_reference:
            mapping = None
            best_identity = -1.0
            query_reference = ""
            for candidate in auto_candidates:
                candidate_aligned = str(candidate.get("seq") or "")
                if not candidate_aligned:
                    continue
                candidate_mapping = alignment_lib.map_sequence(
                    query["sequence"], candidate_aligned, min_identity=min_identity
                )
                candidate_identity = float(candidate_mapping.get("identity") or 0.0)
                # Strict '>' keeps the first row in file order on ties.
                if candidate_identity > best_identity:
                    best_identity = candidate_identity
                    mapping = candidate_mapping
                    query_reference = str(candidate.get("name", ""))
            if mapping is None:
                raise DataError(
                    f"no candidate reference row aligned query {query['name']!r}",
                    details={"query": query["name"], "n_candidates": len(auto_candidates)},
                )
        else:
            mapping = alignment_lib.map_sequence(
                query["sequence"], reference_aligned, min_identity=min_identity
            )
            query_reference = reference_name
        measured = alignment_lib.feature_spans(mapping, spans) if spans else {}
        lengths: dict[str, int] = {}
        residues: dict[str, list[int]] = {}
        for feature in sorted(measured):
            info = measured[feature] or {}
            residue_list = [int(value) for value in info.get("residues") or []]
            lengths[feature] = int(info.get("n_residues", len(residue_list)))
            residues[feature] = sorted({position + 1 for position in residue_list})
        identity = float(mapping.get("identity") or 0.0)
        is_low = bool(mapping.get("low_identity")) or identity < min_identity
        flags = [str(flag) for flag in (mapping.get("flags") or [])]
        if is_low:
            flags = util.unique_preserving_order(flags + ["LOW_IDENTITY_MAPPING"])
            low_identity.append(
                {
                    "query": query["name"],
                    "identity": round(identity, 4),
                    "min_identity": min_identity,
                    "n_aligned": int(mapping.get("n_aligned") or 0),
                }
            )
            ctx.warn(
                "LOW_IDENTITY_MAPPING",
                f"Query {query['name']!r} maps at identity {identity:.2f}, below min_identity "
                f"{min_identity:.2f}; residue assignments and feature lengths may be unreliable.",
                context={
                    "query": query["name"],
                    "identity": round(identity, 4),
                    "min_identity": min_identity,
                },
            )
        query_payload.append(
            {
                "name": query["name"],
                "reference_row": query_reference,
                "query_length": int(mapping.get("query_length") or len(query["sequence"])),
                "n_aligned": int(mapping.get("n_aligned") or 0),
                "identity": round(identity, 4),
                "coverage": round(float(mapping.get("coverage") or 0.0), 4),
                "lengths": lengths,
                "mapped_residues": residues,
                "flags": flags,
                "low_identity": is_low,
            }
        )

    if spans and low_identity:
        ctx.warn(
            "FEATURE_SPAN_UNSTABLE",
            "Feature column spans are alignment-unstable: at low sequence identity the query "
            "residues counted inside a span can be shifted or absorbed into neighbouring "
            "regions, so feature lengths from low-identity mappings are not trustworthy. "
            "Report identity/coverage next to every length.",
            context={"low_identity_queries": [item["query"] for item in low_identity]},
        )

    payload: dict[str, Any] = {
        "alignment": {
            "path": str(alignment_path),
            "format": alignment.get("format"),
            "n_sequences": int(alignment.get("n_sequences") or len(records)),
            "length": int(
                alignment.get("length")
                or (len(reference_aligned) if reference_aligned else max(
                    (len(str(record.get("seq") or "")) for record in records), default=0
                ))
            ),
            "reference_row": reference_name,
            "reference_ungapped_length": (
                len(reference_aligned.replace("-", "").replace(".", ""))
                if reference_aligned
                else None
            ),
            "reference_candidates": (
                [str(record.get("name", "")) for record in auto_candidates]
                if auto_reference
                else None
            ),
            "n_reference_candidates": (len(auto_candidates) if auto_reference else 1),
            "sha256": util.sha256_file(alignment_path),
            "bytes": alignment_path.stat().st_size,
        },
        "features": feature_definitions,
        "queries": query_payload,
        "low_identity": low_identity,
        "n_queries": len(query_payload),
        "min_identity": min_identity,
    }
    if query_file:
        payload["query_file"] = query_file
    out_path = args.get("out")
    if out_path and not bool(ctx.dry_run):
        target = Path(str(out_path)).expanduser()
        if not target.is_absolute():
            target = (Path(ctx.cwd) / target).resolve()
        util.atomic_write_json(target, payload)
        payload["out"] = str(target)
    return payload
