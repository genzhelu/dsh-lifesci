"""``lifesci.struct.superpose`` — Kabsch column-based superposition or Foldseek pairwise TM.

Two modes are supported:

``kabsch``
    Superpose structures/models on **alignment columns** (a shared coordinate
    system).  Each structure supplies a residue→alignment-column map
    (colmap JSON: ``{"chains": {"AA": {"0": 348, ...}}, "chain_residue_ids":
    {"AA": [{"seqid": "3", "icode": ""}, ...]}}`` or the processed ``q2col``
    format).  The core is every column modelled in all structures (minus
    explicit ``exclude_spans`` such as a variable region); coordinates are P atoms
    with C1' fallback (CA for protein chains).  Reports per-structure transforms
    (R, t into the reference frame), core RMSD, and local RMSD for the core
    columns within ``local_window`` columns of the ``focus_spans``.  Warns
    ``FEW_CORE_COLUMNS`` (also when the library core is under 50 columns) and
    raises ``NO_SHARED_COLUMNS`` when the structures share no core.

``foldseek``
    Pairwise structural alignment through the local Foldseek binary
    (:mod:`lifesci.adapters.foldseek`), reporting TM/LDDT/e-value/Q-T
    coverage for every structure pair.  Warns ``TM_SCORE_QUERY_NORMALISED`` and
    ``EVALUE_NOT_COMPARABLE_ACROSS_DATABASES``: TM is query-length normalised
    and E-values depend on the database size, so only compare within one run
    and always report the reciprocal direction.
"""

from __future__ import annotations

import math
from itertools import combinations
from pathlib import Path
from typing import Any, Mapping, Sequence

from .. import util
from ..commands.base import command
from ..errors import ConfigError, DataError, InvalidInputError, NotFoundError, UsageError

INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "mode": {
            "type": "string",
            "enum": ["kabsch", "foldseek"],
            "default": "kabsch",
            "description": (
                "'kabsch': column-based superposition (requires a colmap per structure; reports "
                "transforms and core/local RMSD). 'foldseek': pairwise TM/LDDT alignment using "
                "the local Foldseek binary."
            ),
        },
        "structures": {
            "type": "array",
            "minItems": 2,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["id", "path"],
                "properties": {
                    "id": {"type": "string", "description": "Unique structure/model identifier used in the output."},
                    "path": {"type": "string", "description": "mmCIF/PDB file (gzip allowed; trimmed structures work)."},
                    "colmap": {
                        "oneOf": [
                            {"type": "string"},
                            {"type": "object", "additionalProperties": True},
                        ],
                        "description": (
                            "Residue→alignment-column map: colmap JSON path or inline object. "
                            "Required in kabsch mode, ignored in foldseek mode."
                        ),
                    },
                },
            },
            "description": "Structures to compare; ids must be unique.",
        },
        "reference": {
            "type": "string",
            "description": "kabsch: id of the reference structure (default: first structure). Transforms map others into its frame.",
        },
        "exclude_spans": {
            "type": "array",
            "items": {
                "type": "array",
                "items": {"type": "integer"},
                "minItems": 2,
                "maxItems": 2,
            },
            "description": (
                "kabsch: alignment-column spans [[start,end], ...] excluded from the core "
                "(e.g. a variable region, so the fit is not driven by it)."
            ),
        },
        "core_spans": {
            "type": "array",
            "items": {
                "type": "array",
                "items": {"type": "integer"},
                "minItems": 2,
                "maxItems": 2,
            },
            "description": "kabsch: restrict the core to these alignment-column spans (inclusive).",
        },
        "core_columns": {
            "type": "array",
            "items": {"type": "integer"},
            "description": "kabsch: explicit alignment columns to use as core (intersected with the shared/modelled set).",
        },
        "focus_spans": {
            "type": "array",
            "items": {
                "type": "array",
                "items": {"type": "integer"},
                "minItems": 2,
                "maxItems": 2,
            },
            "description": (
                "kabsch: spans defining the insertion-site neighbourhood for local RMSD (local "
                "core columns are those within local_window columns of any focus span). "
                "Default: exclude_spans."
            ),
        },
        "local_window": {
            "type": "integer",
            "minimum": 0,
            "maximum": 100000,
            "default": 600,
            "description": "kabsch: half-window in alignment columns around focus_spans used for the local RMSD.",
        },
        "binary": {
            "type": "string",
            "description": "foldseek: explicit Foldseek binary path (default: LIFESCI_FOLDSEEK / tools/bin / $PATH).",
        },
        "threads": {
            "type": "integer",
            "minimum": 1,
            "maximum": 64,
            "default": 4,
            "description": "foldseek: CPU threads for the local search.",
        },
        "e": {
            "type": "number",
            "minimum": 0,
            "maximum": 10000,
            "default": 100,
            "description": "foldseek: E-value cutoff for the local search (E-values are database-size dependent).",
        },
        "out": {
            "type": "string",
            "description": "Optional directory for Foldseek intermediates and/or the payload JSON (a .json suffix writes JSON).",
        },
    },
    "required": ["structures"],
}

_RRNA_ATOMS = ["P"]
_RRNA_FALLBACK = ["C1'", "C1*"]


def _resolve_path(ctx: Any, value: str) -> Path:
    path = Path(str(value)).expanduser()
    if not path.is_absolute():
        path = (Path(ctx.cwd) / path).resolve()
    return path


def _record_local(ctx: Any, path: str | Path, *, role: str) -> None:
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


def _load_colmap(ctx: Any, value: Any, *, label: str) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    path = _resolve_path(ctx, str(value))
    if not path.is_file():
        raise NotFoundError(
            f"colmap file does not exist: {path}",
            code="COLMAP_NOT_FOUND",
            details={"structure": label, "path": str(path)},
        )
    payload = util.read_json(path)
    if not isinstance(payload, Mapping):
        raise DataError(f"colmap file is not a JSON object: {path}", details={"path": str(path)})
    _record_local(ctx, path, role="superpose_colmap")
    return dict(payload)


def _span_contains(spans: Sequence[Sequence[int]], column: int) -> bool:
    return any(int(start) <= column <= int(end) for start, end in spans)


def _normalise_spans(value: Any) -> list[list[int]]:
    spans: list[list[int]] = []
    for entry in value or []:
        if isinstance(entry, (list, tuple)) and len(entry) == 2:
            low, high = int(entry[0]), int(entry[1])
            spans.append([min(low, high), max(low, high)])
        else:
            raise InvalidInputError(
                "spans must be [start, end] pairs",
                details={"value": util.truncate(str(entry), 80)},
            )
    return spans


# ---------------------------------------------------------------------------
# local RMSD helpers (coordinate extraction through the pinned structures API)
# ---------------------------------------------------------------------------


def _colmap_index_map(colmap: Mapping[str, Any]) -> dict[str, dict[int, int]]:
    """Extract ``{chain: {polymer_index: column}}`` from supported colmap layouts."""
    if "chains" in colmap and isinstance(colmap.get("chains"), Mapping):
        chains = colmap["chains"]
        out: dict[str, dict[int, int]] = {}
        for chain, mapping in chains.items():
            if not isinstance(mapping, Mapping):
                continue
            converted: dict[int, int] = {}
            for index, column in mapping.items():
                try:
                    converted[int(index)] = int(column)
                except (TypeError, ValueError):
                    continue
            out[str(chain)] = converted
        return out
    if "q2col" in colmap and isinstance(colmap.get("q2col"), Mapping):
        out = {}
        for key, column in colmap["q2col"].items():
            chain, _, index = str(key).partition("|")
            try:
                out.setdefault(chain, {})[int(index)] = int(column)
            except ValueError:
                continue
        return out
    out = {}
    for chain, mapping in colmap.items():
        if not isinstance(mapping, Mapping):
            continue
        converted = {}
        for key, column in mapping.items():
            try:
                converted[int(key)] = int(column)
            except (TypeError, ValueError):
                continue
        if converted:
            out[str(chain)] = converted
    return out


def _column_coordinates(
    path: Path,
    colmap: Mapping[str, Any],
    wanted_columns: set[int],
) -> dict[int, list[float]]:
    """Return ``{column: [x, y, z]}`` for the wanted columns of one structure."""
    from ..lib import structures as structures_lib

    summary = structures_lib.read_structure(path)
    coordinates: dict[int, list[float]] = {}
    for chain, index_map in sorted(_colmap_index_map(colmap).items()):
        chaininfo = structures_lib.find_chain(summary, chain)
        if chaininfo is None:
            continue
        residues = chaininfo.get("residues") or []
        wanted: list[tuple[int, int]] = []
        for index, column in sorted(index_map.items()):
            if column not in wanted_columns or index < 0 or index >= len(residues):
                continue
            wanted.append((index, column))
        if not wanted:
            continue
        # Use the same coordinate convention as the library superposition
        # (P -> C1' -> C1*), so core and local RMSDs are directly comparable.
        atoms, fallback = _RRNA_ATOMS, _RRNA_FALLBACK
        selection = {
            "name": chain,
            "chain": chain,
            "residues": [
                {"seqid": str(residues[index].get("seqid")), "icode": str(residues[index].get("icode") or "")}
                for index, _ in wanted
            ],
            "atoms": atoms,
            "fallback_atoms": fallback,
        }
        collected = structures_lib.collect_atoms(path, selections=[selection])
        by_residue: dict[tuple[str, str], list[float]] = {}
        for atom in collected.get("atoms") or []:
            key = (str(atom.get("seqid")), str(atom.get("icode") or ""))
            by_residue.setdefault(key, [float(atom["x"]), float(atom["y"]), float(atom["z"])])
        for index, column in wanted:
            record = residues[index]
            key = (str(record.get("seqid")), str(record.get("icode") or ""))
            point = by_residue.get(key)
            if point is not None:
                coordinates[column] = point
    return coordinates


def _local_rmsd(
    structures: Sequence[Mapping[str, Any]],
    colmaps: Mapping[str, Mapping[str, Any]],
    *,
    reference: str,
    transforms: Mapping[str, Any],
    core_columns: Sequence[int],
    focus_spans: Sequence[Sequence[int]],
    local_window: int,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """RMSD over local core columns after applying the core-derived transforms."""
    ids = [str(item["id"]) for item in structures]
    if not focus_spans or local_window <= 0:
        local_columns = list(core_columns)
    else:
        local_columns = [
            column
            for column in core_columns
            if any(start - local_window <= column <= end + local_window for start, end in focus_spans)
        ]
    by_id = {str(item["id"]): item for item in structures}
    reference_coords = _column_coordinates(
        Path(by_id[reference]["path"]), colmaps[reference], set(local_columns)
    )
    rows: list[dict[str, Any]] = []
    for identifier in ids:
        transform = transforms.get(identifier) or {}
        rotation = transform.get("R")
        translation = transform.get("t")
        if rotation is None or translation is None:
            continue
        coordinates = (
            reference_coords
            if identifier == reference
            else _column_coordinates(Path(by_id[identifier]["path"]), colmaps[identifier], set(local_columns))
        )
        distances: list[float] = []
        for column in local_columns:
            reference_point = reference_coords.get(column)
            point = coordinates.get(column)
            if reference_point is None or point is None:
                continue
            moved = [
                rotation[row][0] * point[0] + rotation[row][1] * point[1] + rotation[row][2] * point[2]
                + translation[row]
                for row in range(3)
            ]
            distances.append(math.dist(moved, reference_point))
        if len(distances) < 3:
            rows.append(
                {
                    "id": identifier,
                    "rmsd": None,
                    "n_columns": len(distances),
                    "note": "fewer than 3 local columns; RMSD undefined",
                }
            )
            continue
        rmsd = math.sqrt(sum(value * value for value in distances) / len(distances))
        rows.append({"id": identifier, "rmsd": round(rmsd, 4), "n_columns": len(distances)})
    rows.sort(key=lambda row: row["id"])
    return rows, {"n_local_columns": len(local_columns)}


# ---------------------------------------------------------------------------
# foldseek mode
# ---------------------------------------------------------------------------


def _foldseek_adapter() -> Any:
    import importlib

    try:
        return importlib.import_module("lifesci.adapters.foldseek")
    except ImportError as err:  # pragma: no cover - parallel-workstream guard
        raise ConfigError(
            "Foldseek superposition requires python/lifesci/adapters/foldseek.py, which is "
            "not available in this checkout",
            details={"import_error": f"{type(err).__name__}: {err}"},
        ) from err


@command(
    name="struct.superpose",
    title="Superpose structures (Kabsch on alignment columns, or Foldseek TM)",
    summary="Column-based superposition with transforms/core/local RMSD, or pairwise Foldseek TM alignment.",
    description=(
        "Structural comparison with two modes. kabsch: superpose structures on shared alignment "
        "columns (from colmap residue→column maps), optionally excluding spans such as a variable "
        "region; returns per-structure rotation/translation into the reference frame, core RMSD and "
        "local RMSD near the focus spans, plus absent columns. Warns FEW_CORE_COLUMNS and raises "
        "NO_SHARED_COLUMNS when too few columns are shared. foldseek: pairwise structural alignment "
        "via the local Foldseek binary, reporting TM/LDDT/e-value/coverage per pair. Always treats "
        "TM as query-length normalised and E-values as database-size dependent: run reciprocal "
        "searches and never compare E-values across databases. Trimmed structures are valid "
        "inputs; use the preserved residue numbering and colmap."
    ),
    input_schema=INPUT_SCHEMA,
    output_schema="struct.superpose.schema.json",
    category="structure",
    timeout_ms=600_000,
    examples=(
        {
            "mode": "kabsch",
            "structures": [
                {"id": "structure_a", "path": "structures/structure_a.cif", "colmap": "structures/structure_a_colmap.json"},
                {"id": "structure_b", "path": "structures/structure_b.cif", "colmap": "structures/structure_b_colmap.json"},
            ],
            "reference": "structure_a",
            "exclude_spans": [[10, 20]],
        },
    ),
    danger="compute",
)
def run(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
    ctx.validate()
    mode = str(args.get("mode", "kabsch") or "kabsch")
    if mode not in {"kabsch", "foldseek"}:
        raise UsageError(
            f"unknown struct.superpose mode {mode!r}",
            details={"mode": mode, "known": ["kabsch", "foldseek"]},
        )
    structure_records = args.get("structures") or []
    if len(structure_records) < 2:
        raise InvalidInputError("struct.superpose needs at least two structures")
    structures: list[dict[str, Any]] = []
    seen: set[str] = set()
    for record in structure_records:
        identifier = str(record.get("id") or "")
        if not identifier:
            raise InvalidInputError("every structure needs a non-empty 'id'")
        if identifier in seen:
            raise InvalidInputError(f"duplicate structure id {identifier!r}", details={"id": identifier})
        seen.add(identifier)
        path = _resolve_path(ctx, str(record.get("path") or ""))
        if not path.is_file():
            raise NotFoundError(
                f"structure file does not exist: {path}",
                code="STRUCTURE_NOT_FOUND",
                details={"structure": identifier, "path": str(path)},
            )
        _record_local(ctx, path, role="superpose_structure")
        structures.append({"id": identifier, "path": str(path)})

    if mode == "foldseek":
        return _run_foldseek(args, ctx, structures)

    from ..lib import superposition as superposition_lib

    colmaps: dict[str, dict[str, Any]] = {}
    colmap_output: dict[str, Any] = {}
    for record in structure_records:
        identifier = str(record["id"])
        value = record.get("colmap")
        if value is None:
            raise InvalidInputError(
                f"kabsch mode requires a colmap for structure {identifier!r} "
                "(residue→alignment-column map from align.map or a processed JSON)",
                details={"structure": identifier},
            )
        colmap = _load_colmap(ctx, value, label=identifier)
        colmaps[identifier] = colmap
        normalised = superposition_lib.normalise_colmap(colmap)
        colmap_output[identifier] = {
            "n_columns": len(normalised.get("columns") or []),
            "chains": sorted(
                {str(record.get("chain", "")) for record in normalised.get("residues") or []}
                | {str(record.get("chain", "")) for record in normalised.get("indices") or []}
            ),
            "columns_span": (
                [int(normalised["columns"][0]), int(normalised["columns"][-1])]
                if normalised.get("columns")
                else None
            ),
        }
    normalised_colmaps = {
        identifier: superposition_lib.normalise_colmap(colmap) for identifier, colmap in colmaps.items()
    }

    exclude_spans = _normalise_spans(args.get("exclude_spans"))
    core_spans = _normalise_spans(args.get("core_spans"))
    focus_spans = _normalise_spans(args.get("focus_spans")) or exclude_spans
    local_window = int(args.get("local_window", 600))

    core_columns_input: list[int] | None = None
    explicit_core = args.get("core_columns")
    if explicit_core or core_spans or exclude_spans:
        allowed: set[int] = set()
        for data in normalised_colmaps.values():
            allowed.update(int(column) for column in data.get("columns") or [])
        if core_spans:
            allowed = {column for column in allowed if _span_contains(core_spans, column)}
        if exclude_spans:
            allowed = {column for column in allowed if not _span_contains(exclude_spans, column)}
        if explicit_core:
            explicit = {int(column) for column in explicit_core}
            allowed &= explicit
        core_columns_input = sorted(allowed)
        if not core_columns_input:
            raise DataError(
                "NO_SHARED_COLUMNS: the requested core_columns/core_spans/exclude_spans leave "
                "no candidate columns",
                code="NO_SHARED_COLUMNS",
                details={"core_spans": core_spans, "exclude_spans": exclude_spans},
            )

    result = superposition_lib.superpose_structures(
        ctx,
        structures,
        reference=args.get("reference"),
        colmaps=colmaps,
        core_columns=core_columns_input,
        min_core_columns=3,
    )
    reference_id = str(result.get("reference"))
    core_columns = [int(column) for column in result.get("core_columns") or []]
    local_rows, local_info = _local_rmsd(
        structures,
        colmaps,
        reference=reference_id,
        transforms=result.get("transforms") or {},
        core_columns=core_columns,
        focus_spans=focus_spans,
        local_window=local_window,
    )
    n_local = int(local_info.get("n_local_columns") or 0)
    if n_local < 10:
        ctx.warn(
            "FEW_LOCAL_COLUMNS",
            f"Only {n_local} local core columns are available near the focus spans; the local "
            "RMSD is under-constrained and indicative only.",
            context={"n_local_columns": n_local},
        )

    rmsd_core = [
        {
            "id": row.get("id"),
            "rmsd": row.get("core_rmsd"),
            "n_columns": row.get("n_core_used"),
            "n_core_columns": row.get("n_core"),
        }
        for row in result.get("per_structure") or []
    ]
    payload: dict[str, Any] = {
        "mode": "kabsch",
        "reference": reference_id,
        "core": {
            "n_columns": int(result.get("n_core_columns") or 0),
            "column_span": (
                [core_columns[0], core_columns[-1]] if core_columns else None
            ),
            "exclude_spans": exclude_spans,
            "core_spans": core_spans,
            "focus_spans": focus_spans,
            "local_window": local_window,
            "n_local_columns": n_local,
        },
        "transforms": util.json_safe(result.get("transforms") or {}),
        "rmsd_core": util.json_safe(rmsd_core),
        "rmsd_local": util.json_safe(local_rows),
        "per_structure": util.json_safe(result.get("per_structure") or []),
        "absent_columns": util.json_safe(result.get("absent_columns") or {}),
        "column_maps": colmap_output,
        "parameters": util.json_safe(result.get("parameters") or {}),
    }
    out_path = args.get("out")
    if out_path and not bool(ctx.dry_run):
        target = Path(str(out_path)).expanduser()
        if not target.is_absolute():
            target = (Path(ctx.cwd) / target).resolve()
        if target.suffix.lower() != ".json":
            target = target / "struct.superpose.json"
        util.atomic_write_json(target, payload)
        payload["artifacts"] = {"json_path": str(target)}
    return payload


def _run_foldseek(args: dict[str, Any], ctx: Any, structures: list[dict[str, Any]]) -> dict[str, Any]:
    """Pairwise Foldseek local search for one or more structure pairs."""
    adapter = _foldseek_adapter()
    out_arg = args.get("out")
    if out_arg:
        out_dir = Path(str(out_arg)).expanduser()
        if not out_dir.is_absolute():
            out_dir = (Path(ctx.cwd) / out_dir).resolve()
    else:
        out_dir = (Path(ctx.cwd) / "lifesci-struct-superpose").resolve()
    tmp_dir = out_dir / "tmp"
    binary = args.get("binary")
    threads = int(args.get("threads", 4))
    evalue = float(args.get("e", 100))
    binary_info = adapter.version(ctx, binary)
    pairs: list[dict[str, Any]] = []
    if bool(ctx.dry_run):
        for left, right in combinations(structures, 2):
            pairs.append(
                {
                    "a": left["id"],
                    "b": right["id"],
                    "dry_run": True,
                    "query_path": left["path"],
                    "target_path": right["path"],
                }
            )
        ctx.warn(
            "REMOTE_DRY_RUN",
            "struct.superpose foldseek mode was planned but not executed because --dry-run was set.",
            severity="info",
        )
        return {
            "mode": "foldseek",
            "pairs": pairs,
            "binary": util.json_safe(binary_info),
            "dry_run": True,
            "out_dir": str(out_dir),
        }
    out_dir.mkdir(parents=True, exist_ok=True)
    tmp_dir.mkdir(parents=True, exist_ok=True)
    for left, right in combinations(structures, 2):
        pair_out = tmp_dir / f"{left['id']}_vs_{right['id']}.m8"
        result = adapter.local_search(
            ctx,
            query_path=left["path"],
            target_path=right["path"],
            out_path=str(pair_out),
            tmp_dir=str(tmp_dir),
            e=evalue,
            threads=threads,
            binary=binary,
        )
        hits = [dict(hit) for hit in result.get("hits") or []]
        # Foldseek orders by its own score; rank by alignment TM (then e-value) so the
        # "top" hit is the best structural correspondence, not an arbitrary tie.
        hits.sort(
            key=lambda hit: (
                -float(hit.get("alntmscore") or -1.0),
                float(hit.get("evalue") if hit.get("evalue") is not None else 1e300),
                str(hit.get("query")),
                str(hit.get("target")),
            )
        )
        top = hits[0] if hits else None
        pairs.append(
            {
                "a": left["id"],
                "b": right["id"],
                "hits": hits[:5],
                "top": top,
                "top_tm_score": (top or {}).get("alntmscore"),
                "n_hits": len(hits),
                "version": result.get("version"),
                "command": result.get("command"),
            }
        )
    pairs.sort(key=lambda row: (row["a"], row["b"]))
    if pairs and all(int(row.get("n_hits") or 0) == 0 for row in pairs):
        ctx.warn(
            "FOLDSEEK_NO_HITS",
            "Foldseek returned no alignments for any structure pair. Foldseek aligns individual "
            "protein chains (CA traces); nucleic-acid-only structures have no protein chain to "
            "align, and short/partial fragments may fall below the E-value cutoff. Use kabsch mode "
            "on alignment columns for nucleic-acid feature comparisons.",
            context={"n_pairs": len(pairs), "e": evalue},
        )
    ctx.warn(
        "FOLDSEEK_CHAIN_LEVEL",
        "Foldseek easy-search aligns individual protein chains and reports the best chain pair per "
        "structure pair; it does not superpose RNA chains or multi-chain complexes as a whole. For "
        "nucleic-acid features use kabsch mode with alignment-column colmaps.",
        context={"n_pairs": len(pairs)},
    )
    ctx.warn_tm_query_normalised(None)
    ctx.warn_evalue_db_dependence(
        "foldseek-local",
        "Local Foldseek E-values depend on the target database size; do not compare them with "
        "panel/web values of the same pair.",
    )
    payload: dict[str, Any] = {
        "mode": "foldseek",
        "pairs": pairs,
        "binary": util.json_safe(binary_info),
        "out_dir": str(out_dir),
        "parameters": {"threads": threads, "e": evalue},
    }
    if out_arg:
        target = Path(str(out_arg)).expanduser()
        if not target.is_absolute():
            target = (Path(ctx.cwd) / target).resolve()
        if target.suffix.lower() == ".json":
            util.atomic_write_json(target, payload)
            payload["artifacts"] = {"json_path": str(target)}
    return payload
