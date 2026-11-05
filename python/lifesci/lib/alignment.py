"""Sequence-to-reference-alignment mapping and feature measurement.

The workflow maps a novel sequence onto a reference alignment row with a global
:class:`Bio.Align.PairwiseAligner`, builds residue -> alignment-column maps, and
counts residues inside predefined column spans (features).  The aligner scores
below are tuned defaults — **do not change them casually**: mismatch penalties
are tuned so that feature insertions are aligned with cheap but not free end
gaps.

Coordinate conventions
----------------------
* Alignment columns are **0-based inclusive** indices into the aligned
  reference row string (gaps count as columns).
* Reference ungapped positions are **1-based** indices into the reference
  sequence with gap columns removed (annotation coordinates).
* Query positions are **0-based** indices into the ungapped query sequence.

At low identity the alignment of feature spans is unreliable; mappings carry
``LOW_IDENTITY`` flags and callers must surface the ``FEATURE_SPAN_UNSTABLE``
caveat when measuring such spans.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from ..errors import DataError, InvalidInputError
from ..util import json_safe, read_fasta

#: Tuned PairwiseAligner scores (do not change casually).
ALIGNER_SETTINGS: dict[str, Any] = {
    "mode": "global",
    "match_score": 2.0,
    "mismatch_score": -1.0,
    "open_gap_score": -8.0,
    "extend_gap_score": -0.5,
    "end_gap_score": -0.4,
    "end_open_gap_score": -0.4,
    "end_extend_gap_score": -0.1,
}

_GAP_CHARACTERS = frozenset("-.")


# ---------------------------------------------------------------------------
# alignment IO
# ---------------------------------------------------------------------------


def make_aligner() -> Any:
    """Return the tuned global :class:`Bio.Align.PairwiseAligner`.

    Settings: match 2.0, mismatch -1.0, open gap -8.0, extend gap -0.5,
    end gap/open/extend -0.4/-0.4/-0.1 (see :data:`ALIGNER_SETTINGS`).
    """
    from Bio.Align import PairwiseAligner

    aligner = PairwiseAligner()
    aligner.mode = ALIGNER_SETTINGS["mode"]
    aligner.match_score = ALIGNER_SETTINGS["match_score"]
    aligner.mismatch_score = ALIGNER_SETTINGS["mismatch_score"]
    aligner.open_gap_score = ALIGNER_SETTINGS["open_gap_score"]
    aligner.extend_gap_score = ALIGNER_SETTINGS["extend_gap_score"]
    aligner.end_gap_score = ALIGNER_SETTINGS["end_gap_score"]
    # Biopython >= 1.86 renamed end_* gap scores to *_end_gap_score; set both
    # names when available so the documented -0.4/-0.4/-0.1 end-gap model holds.
    if hasattr(aligner, "open_end_gap_score"):
        aligner.open_end_gap_score = ALIGNER_SETTINGS["end_open_gap_score"]
        aligner.extend_end_gap_score = ALIGNER_SETTINGS["end_extend_gap_score"]
    else:  # pragma: no cover - older Biopython
        aligner.end_open_gap_score = ALIGNER_SETTINGS["end_open_gap_score"]
        aligner.end_extend_gap_score = ALIGNER_SETTINGS["end_extend_gap_score"]
    return aligner


def read_alignment(path: str | Path) -> dict[str, Any]:
    """Read FASTA / Stockholm / Clustal alignment rows.

    Returns
    -------
    dict
        ``{"format", "records": [{"name", "seq"}], "n_sequences", "length"}``.
        Record order is file order; ``length`` is the aligned row length.  Rows
        must be equal length (an MSA); a clear :class:`DataError` is raised
        otherwise.
    """
    target = Path(path)
    if not target.is_file():
        raise DataError(
            f"alignment file not found: {target}", details={"path": str(target)}
        )
    text = _read_text(target)
    stripped = text.lstrip()
    format_name: str
    if stripped.startswith(">"):
        format_name = "fasta"
        records = [
            {"name": header.split()[0] if header.split() else header, "seq": seq}
            for header, seq in read_fasta(target)
        ]
    elif stripped.startswith("# STOCKHOLM"):
        format_name = "stockholm"
        records = _read_with_alignio(target, "stockholm")
    elif stripped.upper().startswith("CLUSTAL"):
        format_name = "clustal"
        records = _read_with_alignio(target, "clustal")
    else:
        format_name, records = _sniff_with_alignio(target)
    if not records:
        raise DataError(f"no alignment rows found in {target}", details={"path": str(target)})
    lengths = [len(record["seq"]) for record in records]
    counts: dict[int, int] = {}
    for length in lengths:
        counts[length] = counts.get(length, 0) + 1
    # Some source alignments carry rows that are a few characters longer than the
    # rest (source-table quirks); rows are still individually aligned.
    # Take the modal aligned length and report whether the MSA is regular.
    modal_length = max(counts, key=lambda length: (counts[length], length))
    return {
        "format": format_name,
        "records": records,
        "n_sequences": len(records),
        "length": modal_length,
        "aligned": len(counts) == 1,
        "length_min": min(lengths),
        "length_max": max(lengths),
    }


def _read_text(path: Path) -> str:
    from ..util import read_maybe_gzip

    return read_maybe_gzip(path).decode("utf-8", errors="replace")


def _read_with_alignio(path: Path, format_name: str) -> list[dict[str, str]]:
    from Bio import AlignIO

    try:
        alignment = AlignIO.read(str(path), format_name)
    except Exception as err:
        raise DataError(
            f"failed to parse {format_name} alignment {path}: {err}",
            details={"path": str(path), "format": format_name},
        ) from err
    return [
        {"name": record.id or record.description.split()[0], "seq": str(record.seq)}
        for record in alignment
    ]


def _sniff_with_alignio(path: Path) -> tuple[str, list[dict[str, str]]]:
    from Bio import AlignIO

    errors: list[str] = []
    for format_name in ("fasta", "stockholm", "clustal", "phylip", "emboss"):
        try:
            alignment = AlignIO.read(str(path), format_name)
        except Exception as err:  # try the next format
            errors.append(f"{format_name}: {err}")
            continue
        records = [
            {"name": record.id or record.description.split()[0], "seq": str(record.seq)}
            for record in alignment
        ]
        return format_name, records
    raise DataError(
        f"could not read alignment {path} in any supported format",
        details={"path": str(path), "errors": errors},
    )


def alignment_reference_row(
    alignment: Mapping[str, Any],
    reference: str | None = None,
) -> tuple[str, str]:
    """Pick the reference row from a :func:`read_alignment` payload.

    Returns ``(reference_name, aligned_row)``.  ``reference`` matches a record
    name exactly, by the part before ``/`` (rows may carry ``/start-end``
    coordinate suffixes) or by prefix; the first record is the default.
    """
    records = alignment.get("records") or []
    if not records:
        raise DataError("alignment has no records")
    if reference is None:
        first = records[0]
        return str(first["name"]), str(first["seq"])
    wanted = str(reference)
    for record in records:
        name = str(record["name"])
        if name == wanted or name.split("/")[0] == wanted:
            return name, str(record["seq"])
    for record in records:
        name = str(record["name"])
        if name.split("/")[0].startswith(wanted):
            return name, str(record["seq"])
    raise InvalidInputError(
        f"reference row {wanted!r} not found in the alignment",
        details={"reference": wanted, "available": [r["name"] for r in records]},
    )


# ---------------------------------------------------------------------------
# mapping
# ---------------------------------------------------------------------------


def _aligned_columns(reference_aligned: str) -> list[int]:
    """Alignment column index (0-based) for each ungapped reference position."""
    return [
        column
        for column, char in enumerate(reference_aligned)
        if char not in _GAP_CHARACTERS
    ]


def _normalise_rna_dna(query: str, reference: str) -> str:
    """Make a DNA query comparable to an RNA reference (or vice versa).

    Genome annotations are DNA (``T``) while some reference rows are RNA
    (``U``); aligning them naively counts T/U differences as mismatches.  Only
    convert when the alphabets clearly disagree (reference has ``U`` and no
    ``T``, query has ``T`` and no ``U``).

    **Off by default** (``normalise_rna_dna=False``): reference mapping compares
    the raw alphabets and reports T/U positions as mismatches.  Turn it on when
    you want alphabet-normalised identity values.
    """
    query = query.upper()
    reference = reference.upper()
    if "U" in reference and "T" not in reference and "T" in query and "U" not in query:
        return query.replace("T", "U")
    if "T" in reference and "U" not in reference and "U" in query and "T" not in query:
        return query.replace("U", "T")
    return query


def map_sequence(
    query: str,
    reference_aligned: str,
    *,
    min_identity: float = 0.6,
    normalise_rna_dna: bool = False,
) -> dict[str, Any]:
    """Global-align an ungapped query to one aligned reference row.

    The alignment is computed against the **ungapped** reference row (gap
    columns carry no information and would triple the runtime) and the result is
    translated back to alignment columns.  Lowercase input is upper-cased.  DNA/RNA
    alphabets are **not** silently reconciled: set ``normalise_rna_dna=True`` to
    count T/U positions as matches (off by default; see
    :func:`_normalise_rna_dna`).

    Parameters
    ----------
    query:
        Ungapped query sequence (gaps in the query are ignored/rejected).
    reference_aligned:
        The reference row, **including gap characters** as it appears in the
        MSA (e.g. ``"ACG--UUAG..."``).
    min_identity:
        Identity below which the mapping carries a ``LOW_IDENTITY`` flag.
    normalise_rna_dna:
        Treat T/U as equivalent before aligning (default ``False`` compares the
        raw alphabets).

    Returns
    -------
    dict
        ``{"query_length", "reference_length", "n_aligned", "identity",
        "coverage", "score", "q2row", "q2col", "row2q", "col2q", "flags",
        "low_identity", "min_identity"}`` where

        * ``q2row``: query idx -> ungapped reference row idx (0-based)
        * ``q2col``: query idx -> alignment column idx (0-based, gaps count)
        * ``row2q``: ungapped reference row idx -> query idx
        * ``col2q``: alignment column idx -> query idx

        JSON serialisation turns the integer keys into strings (``json.dumps``).
    """
    if not isinstance(query, str) or not query:
        raise InvalidInputError("query must be a non-empty sequence string")
    if not isinstance(reference_aligned, str) or not reference_aligned:
        raise InvalidInputError("reference_aligned must be a non-empty aligned row")
    if any(char in _GAP_CHARACTERS for char in query):
        raise InvalidInputError(
            "query must be ungapped (strip alignment gap characters first)"
        )
    ungapped_to_column = _aligned_columns(reference_aligned)
    reference = "".join(
        reference_aligned[column] for column in ungapped_to_column
    ).upper()
    if normalise_rna_dna:
        query = _normalise_rna_dna(query, reference)
    else:
        query = query.upper()
    aligner = make_aligner()
    alignments = aligner.align(query, reference)
    try:
        alignment = alignments[0]
    except (IndexError, TypeError) as err:  # pragma: no cover - always at least one
        raise DataError(f"pairwise alignment returned no result: {err}") from err
    query_to_row: dict[int, int] = {}
    query_to_column: dict[int, int] = {}
    row_to_query: dict[int, int] = {}
    column_to_query: dict[int, int] = {}
    identical = 0
    # ``alignment.aligned`` returns a pair of arrays (query blocks, target blocks).
    aligned_pairs = alignment.aligned
    query_blocks, target_blocks = aligned_pairs[0], aligned_pairs[1]
    for (query_start, query_stop), (target_start, target_stop) in zip(
        query_blocks, target_blocks
    ):
        for offset in range(int(query_stop - query_start)):
            query_index = int(query_start) + offset
            row_index = int(target_start) + offset
            query_to_row[query_index] = row_index
            column = ungapped_to_column[row_index]
            query_to_column[query_index] = column
            row_to_query[row_index] = query_index
            column_to_query[column] = query_index
            if query[query_index] == reference[row_index]:
                identical += 1
    n_aligned = len(query_to_row)
    identity = identical / n_aligned if n_aligned else 0.0
    coverage = n_aligned / len(query) if query else 0.0
    flags: list[str] = []
    low_identity = identity < float(min_identity)
    if low_identity:
        flags.append("LOW_IDENTITY")
    if n_aligned < 30:
        flags.append("SHORT_ALIGNMENT")
    if coverage < 0.5:
        flags.append("LOW_QUERY_COVERAGE")
    reference_length = len(ungapped_to_column)
    if n_aligned and (
        query_to_row[min(query_to_row)] > 5
        or query_to_row[max(query_to_row)] < reference_length - 6
    ):
        flags.append("TERMINAL_OFFSET")
    return {
        "query_length": len(query),
        "reference_length": reference_length,
        "alignment_length": len(reference_aligned),
        "n_aligned": n_aligned,
        "identity": round(float(identity), 4),
        "coverage": round(float(coverage), 4),
        "score": float(alignment.score),
        "q2row": query_to_row,
        "q2col": query_to_column,
        "row2q": row_to_query,
        "col2q": column_to_query,
        "flags": flags,
        "low_identity": bool(low_identity),
        "min_identity": float(min_identity),
    }


def extract_sequence(pieces: Iterable[str] | str) -> str:
    """Join alignment pieces into one ungapped sequence (``-``/``.`` removed).

    Biopython alignment extraction sometimes returns a list of pieces; this
    helper concatenates them and strips gap columns.
    """
    if isinstance(pieces, str):
        text = pieces
    else:
        text = "".join(str(piece) for piece in pieces)
    return re.sub(r"[-.]", "", text)


# ---------------------------------------------------------------------------
# feature spans
# ---------------------------------------------------------------------------


def _normalise_spans(spans: Any) -> list[tuple[int, int]]:
    """Normalise a span specification to inclusive 0-based column ranges."""
    if spans is None:
        return []
    if isinstance(spans, Mapping):
        spans = spans.get("columns") or spans.get("spans") or []
    if isinstance(spans, (tuple, list)) and len(spans) == 2 and all(
        isinstance(value, (int, float)) for value in spans
    ):
        spans = [spans]
    normalised: list[tuple[int, int]] = []
    for span in spans:
        if isinstance(span, Mapping):
            start = span.get("start", span.get("from"))
            end = span.get("end", span.get("to"))
        else:
            try:
                start, end = span[0], span[1]
            except (TypeError, IndexError) as err:
                raise InvalidInputError(f"invalid feature span {span!r}") from err
        first, last = int(start), int(end)
        if last < first:
            first, last = last, first
        normalised.append((first, last))
    return normalised


def feature_spans(
    mapping: Mapping[str, Any],
    spans: Mapping[str, Any],
) -> dict[str, dict[str, Any]]:
    """Count query residues falling inside alignment-column spans.

    Parameters
    ----------
    mapping:
        Output of :func:`map_sequence` (uses ``q2col``).
    spans:
        ``{feature: [[start_col, end_col], ...]}`` column ranges, 0-based and
        inclusive.  A single 2-int list is accepted as one span.

    Returns
    -------
    dict
        ``{feature: {"n_residues": n, "residues": [query_idx, ...],
        "columns": [alignment_column, ...]}}``.  Residues are ordered 5'->3'
        (query order).  Features outside all spans report ``n_residues: 0``.
    """
    query_to_column: Mapping[int, int] = mapping.get("q2col") or {}
    ordered = sorted(int(query) for query in query_to_column)
    result: dict[str, dict[str, Any]] = {}
    for feature, span_spec in spans.items():
        ranges = _normalise_spans(span_spec)
        residues = [
            query
            for query in ordered
            if any(start <= int(query_to_column[query]) <= stop for start, stop in ranges)
        ]
        result[str(feature)] = {
            "n_residues": len(residues),
            "residues": residues,
            "columns": [int(query_to_column[query]) for query in residues],
            "column_spans": [[start, stop] for start, stop in ranges],
        }
    return result


def column_span_from_reference(
    reference_aligned: str,
    coords: Mapping[str, Any],
) -> dict[str, list[int]]:
    """Map 1-based ungapped reference coordinates to alignment column spans.

    Parameters
    ----------
    reference_aligned:
        The reference row including gap characters.
    coords:
        ``{feature: [start, end]}`` where start/end are **1-based inclusive
        positions in the ungapped reference sequence** (e.g. residues 101-200
        of the annotation row).  A mapping ``{"start","end"}`` per feature is
        also accepted.

    Returns
    -------
    dict
        ``{feature: [start_column, end_column]}`` inclusive 0-based alignment
        column indices.
    """
    ungapped_to_column = _aligned_columns(reference_aligned)
    reference_length = len(ungapped_to_column)
    result: dict[str, list[int]] = {}
    for feature, span in coords.items():
        if isinstance(span, Mapping):
            start, end = span.get("start", span.get("from")), span.get("end", span.get("to"))
        else:
            try:
                start, end = span[0], span[1]
            except (TypeError, IndexError) as err:
                raise InvalidInputError(
                    f"invalid reference coordinates for feature {feature!r}: {span!r}"
                ) from err
        first, last = int(start), int(end)
        if last < first:
            first, last = last, first
        if first < 1 or last > reference_length:
            raise InvalidInputError(
                f"reference coordinates {first}-{last} for {feature!r} are outside "
                f"the ungapped reference length {reference_length}",
                details={
                    "feature": str(feature),
                    "start": first,
                    "end": last,
                    "reference_length": reference_length,
                },
            )
        result[str(feature)] = [
            int(ungapped_to_column[first - 1]),
            int(ungapped_to_column[last - 1]),
        ]
    return result


# ---------------------------------------------------------------------------
# high-level query measurement
# ---------------------------------------------------------------------------


def _normalise_queries(queries: Any) -> list[dict[str, str]]:
    """Normalise the supported query inputs to ``[{"name","sequence"}]``."""
    if isinstance(queries, (str, Path)):
        path = Path(queries)
        if not path.is_file():
            raise DataError(
                f"query FASTA not found: {path}", details={"path": str(path)}
            )
        return [
            {"name": header.split()[0] if header.split() else header, "sequence": seq}
            for header, seq in read_fasta(path)
        ]
    if isinstance(queries, Mapping):
        return [
            {"name": str(name), "sequence": str(sequence)}
            for name, sequence in queries.items()
        ]
    records: list[dict[str, str]] = []
    for index, item in enumerate(queries or []):
        if isinstance(item, Mapping):
            name = str(item.get("name") or item.get("id") or f"query_{index + 1}")
            sequence = item.get("sequence") or item.get("seq") or ""
            if not sequence:
                raise InvalidInputError(f"query {name!r} has no sequence")
            records.append({"name": name, "sequence": str(sequence)})
        else:
            records.append({"name": f"query_{index + 1}", "sequence": str(item)})
    return records


def _normalise_features(
    features: Mapping[str, Any],
    reference_aligned: str,
) -> dict[str, list[tuple[int, int]]]:
    """Normalise feature specs to inclusive alignment-column ranges."""
    normalised: dict[str, list[tuple[int, int]]] = {}
    for feature, spec in (features or {}).items():
        if isinstance(spec, Mapping):
            if "ref_coords" in spec or "reference_coords" in spec:
                coords = spec.get("ref_coords") or spec.get("reference_coords")
                columns = column_span_from_reference(
                    reference_aligned, {str(feature): coords}
                )[str(feature)]
                normalised[str(feature)] = [(columns[0], columns[1])]
                continue
            if "coords" in spec or "reference" in spec:
                coords = spec.get("coords") or spec.get("reference")
                columns = column_span_from_reference(
                    reference_aligned, {str(feature): coords}
                )[str(feature)]
                normalised[str(feature)] = [(columns[0], columns[1])]
                continue
            ranges = _normalise_spans(spec)
        else:
            ranges = _normalise_spans(spec)
        normalised[str(feature)] = ranges
    return normalised


def measure_queries(
    ctx: Any,
    *,
    alignment_path: str | Path,
    queries: Any,
    features: Mapping[str, Any],
    min_identity: float = 0.6,
    reference: str | None = None,
    include_mappings: bool = False,
) -> dict[str, Any]:
    """Map queries onto a reference MSA row and measure feature lengths.

    This is the library implementation behind the ``align.map`` command.

    Parameters
    ----------
    ctx:
        :class:`lifesci.context.Context` (warnings) or ``None``.
    alignment_path:
        FASTA/Stockholm/Clustal file containing the reference alignment.
    queries:
        Mapping ``name -> sequence``, a list of sequences/records, or a FASTA
        path.
    features:
        ``{feature: spec}`` where a spec is one of

        * ``[start_col, end_col]`` or ``[[start_col, end_col], ...]`` —
          alignment columns (0-based inclusive);
        * ``{"columns": [...]}`` — same;
        * ``{"ref_coords": [start, end]}`` / ``{"reference_coords": ...}`` —
          1-based ungapped reference positions (converted through the reference
          row).
    min_identity:
        Low-identity threshold; below it mappings are flagged.
    reference:
        Name (or ``name/start-end`` prefix) of the reference alignment row;
        defaults to the first row.
    include_mappings:
        Include the full per-query ``q2col``/``q2row``/``row2q`` maps in the
        payload (larger output).

    Returns
    -------
    dict
        ``{"alignment", "features", "queries", "low_identity", "warnings",
        "parameters"}`` where each query record carries
        ``{"name", "query_length", "n_aligned", "identity", "coverage",
        "flags", "low_identity", "lengths", "mapped_residues"}``.
    """
    alignment = read_alignment(alignment_path)
    reference_name, reference_aligned = alignment_reference_row(alignment, reference)
    normalised_features = _normalise_features(features, reference_aligned)
    query_records = _normalise_queries(queries)

    feature_payload = {
        feature: {
            "columns": [[start, stop] for start, stop in ranges],
            "n_columns": sum(stop - start + 1 for start, stop in ranges),
        }
        for feature, ranges in normalised_features.items()
    }
    results: list[dict[str, Any]] = []
    low_identity: list[str] = []
    warnings: list[dict[str, Any]] = []
    for record in query_records:
        mapping = map_sequence(
            record["sequence"],
            reference_aligned,
            min_identity=min_identity,
        )
        spans = feature_spans(mapping, normalised_features)
        lengths = {feature: data["n_residues"] for feature, data in spans.items()}
        mapped_residues = {
            feature: data["residues"] for feature, data in spans.items()
        }
        query_result: dict[str, Any] = {
            "name": record["name"],
            "query_length": mapping["query_length"],
            "n_aligned": mapping["n_aligned"],
            "identity": mapping["identity"],
            "coverage": mapping["coverage"],
            "flags": list(mapping["flags"]),
            "low_identity": mapping["low_identity"],
            "lengths": lengths,
            "mapped_residues": mapped_residues,
            "feature_columns": {feature: data["columns"] for feature, data in spans.items()},
        }
        if include_mappings:
            query_result["mapping"] = {
                "q2row": mapping["q2row"],
                "q2col": mapping["q2col"],
                "row2q": mapping["row2q"],
            }
        if mapping["low_identity"]:
            low_identity.append(record["name"])
            message = (
                f"{record['name']} maps to {reference_name} with identity "
                f"{mapping['identity']:.3f} (< {min_identity}); feature-span "
                "lengths from this mapping are unreliable — do not interpret "
                "column counts quantitatively."
            )
            warnings.append(
                {
                    "code": "LOW_IDENTITY_MAPPING",
                    "severity": "warning",
                    "message": message,
                    "context": {
                        "query": record["name"],
                        "identity": mapping["identity"],
                        "min_identity": min_identity,
                    },
                }
            )
            if ctx is not None and hasattr(ctx, "warn"):
                ctx.warn(
                    "LOW_IDENTITY_MAPPING",
                    message,
                    context={
                        "query": record["name"],
                        "identity": mapping["identity"],
                        "min_identity": min_identity,
                    },
                )
            if ctx is not None and hasattr(ctx, "warn"):
                ctx.warn(
                    "FEATURE_SPAN_UNSTABLE",
                    (
                        "Feature spans are alignment-unstable; span counts measured "
                        f"for {record['name']} at identity {mapping['identity']:.3f} "
                        "are low-confidence."
                    ),
                    context={"query": record["name"]},
                )
        results.append(query_result)
    return {
        "alignment": {
            "path": str(alignment_path),
            "format": alignment["format"],
            "n_sequences": alignment["n_sequences"],
            "length": alignment["length"],
            "reference": reference_name,
        },
        "features": feature_payload,
        "queries": results,
        "low_identity": low_identity,
        "warnings": warnings,
        "parameters": {
            "min_identity": float(min_identity),
            "aligner": dict(ALIGNER_SETTINGS),
            "include_mappings": bool(include_mappings),
        },
    }
