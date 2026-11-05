"""``lifesci.contacts`` — inter-element contact distances between structural features.

Computes minimum atom-atom distances between features of one or more structures:
RNA features can be selected by **explicit residue numbers** (``"101-120"``) or
by **alignment-column spans** resolved through a residue→column colmap JSON
(``{"chains": {"AA": {"0": 348, ...}}, "feature_a_columns": [...],
"feature_b_columns": [...]}``), while protein features are selected by
chain/residues and use all atoms by default.

For every feature pair the command reports the minimum distance, the closest
atom/residue pair, counts below thresholds, and the **percentile position** of
the contact along each element.  It also reports missing features explicitly and
detects bridges through a third feature
(``CONTACT_BRIDGE_THROUGH_THIRD_FEATURE``: a third element lies within the
bridge threshold of both members of a non-contacting pair).

Distance caveats always emitted: ``DISTANCE_ATOM_SELECTION`` (the number depends
on the atom selection — P atoms ignore side chains/heteroatoms),
``CONTACT_ABSENT_NOT_PROOF`` (no contact within the thresholds is not evidence
of no interaction), ``FEATURE_MISSING``, and the bridge warning. All pairs are
sorted deterministically and coordinates are rounded to 3 decimals at output.
"""

from __future__ import annotations

from itertools import combinations
from pathlib import Path
from typing import Any, Mapping, Sequence

from .. import util
from ..commands.base import command
from ..errors import DataError, InvalidInputError, NotFoundError

INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "structures": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["id", "path"],
                "properties": {
                    "id": {
                        "type": "string",
                        "description": "Short structure identifier used in feature/pairs references (e.g. 'structure_a').",
                    },
                    "path": {
                        "type": "string",
                        "description": "Path to an mmCIF/PDB structure file (gzip allowed; a trimmed structure works).",
                    },
                    "colmap": {
                        "oneOf": [
                            {"type": "string"},
                            {"type": "object", "additionalProperties": True},
                        ],
                        "description": (
                            "Residue-index→alignment-column map for this structure: path to a "
                            "colmap JSON or an inline object, e.g. {\"chains\": {\"AA\": {\"0\": "
                            "348}}, \"feature_a_columns\": [...], \"feature_b_columns\": [...]}."
                        ),
                    },
                },
            },
            "description": "Structures to analyse; every feature belongs to exactly one of them.",
        },
        "features": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["id", "structure"],
                "properties": {
                    "id": {
                        "type": "string",
                        "description": (
                            "Feature id used in pairs (unique per structure; the same id may "
                            "appear in several structures, e.g. feature_a in two structures)."
                        ),
                    },
                    "structure": {
                        "type": "string",
                        "description": "Structure id this feature belongs to.",
                    },
                    "chain": {
                        "type": "string",
                        "description": (
                            "Auth chain id containing the feature (required for residues mode; "
                            "optional in columns mode where chains come from the colmap)."
                        ),
                    },
                    "residues": {
                        "oneOf": [
                            {"type": "string"},
                            {"type": "array"},
                            {"type": "object", "additionalProperties": True},
                        ],
                        "description": (
                            "Explicit residue selection: 'all', '101-120', '101,105,120', "
                            "{'seqids': [...]}, or a resolved list of {'seqid','icode'} objects. "
                            "Mutually exclusive with 'columns'."
                        ),
                    },
                    "columns": {
                        "oneOf": [
                            {"type": "string"},
                            {"type": "array"},
                            {"type": "integer"},
                        ],
                        "description": (
                            "Alignment-column selection resolved through the colmap: a colmap "
                            "key such as 'feature_a_columns', an inclusive [start,end] span, a "
                            "list of columns, or nested spans. Mutually exclusive with 'residues'."
                        ),
                    },
                    "colmap": {
                        "oneOf": [
                            {"type": "string"},
                            {"type": "object", "additionalProperties": True},
                        ],
                        "description": "Optional per-feature colmap override (path or inline object).",
                    },
                    "atoms": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Atom-name priority list ('*' means all atoms). Default for rRNA "
                            "chains: ['P'] with fallback ['C1\\'', 'C1*']; default for protein "
                            "chains: ['*'] (all atoms, needed for realistic side-chain contacts)."
                        ),
                    },
                    "fallback_atoms": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Fallback atom names used only when no primary atom exists (default ['C1\\'', 'C1*'] for rRNA).",
                    },
                },
                "oneOf": [{"required": ["residues"]}, {"required": ["columns"]}],
            },
            "description": "Named features (rRNA elements or protein chains) to measure distances between.",
        },
        "pairs": {
            "type": "array",
            "items": {
                "oneOf": [
                    {
                        "type": "array",
                        "items": {"type": "string"},
                        "minItems": 2,
                        "maxItems": 2,
                    },
                    {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["a", "b"],
                        "properties": {
                            "a": {"type": "string", "description": "First feature id of the pair."},
                            "b": {"type": "string", "description": "Second feature id of the pair."},
                        },
                    },
                ]
            },
            "description": "Feature pairs to measure. Default: every pair of features within each structure.",
        },
        "thresholds": {
            "type": "array",
            "items": {"type": "number", "minimum": 0},
            "default": [4.0, 5.0, 10.0],
            "description": "Distance thresholds (Å) for the n_under counts.",
        },
        "top_pairs": {
            "type": "integer",
            "minimum": 1,
            "maximum": 50,
            "default": 10,
            "description": "Number of closest atom pairs retained per feature pair (the minimum distance is always first).",
        },
        "bridge_threshold": {
            "type": "number",
            "minimum": 0,
            "maximum": 20,
            "default": 4.5,
            "description": (
                "Å threshold for bridge detection: warn when a third feature lies within this "
                "distance of both members of a non-contacting pair."
            ),
        },
        "out": {
            "type": "string",
            "description": "Optional path to write the contact payload JSON.",
        },
    },
    "required": ["structures", "features"],
}

_DEFAULT_RRNA_ATOMS = ["P"]
_DEFAULT_RRNA_FALLBACK = ["C1'", "C1*"]
_DEFAULT_PROTEIN_ATOMS = ["*"]


# ---------------------------------------------------------------------------
# helpers: colmap / columns / features
# ---------------------------------------------------------------------------


def _normalise_spans(value: Any) -> list[list[int]]:
    """Normalise a column definition into inclusive ``[[start, end], ...]`` spans."""
    if value is None:
        return []
    if isinstance(value, int):
        return [[value, value]]
    if isinstance(value, dict):
        if "start" in value and "end" in value:
            return [[int(value["start"]), int(value["end"])]]
        for key in ("columns", "spans", "cols"):
            if key in value:
                return _normalise_spans(value[key])
        raise InvalidInputError("unsupported column definition object", details={"keys": sorted(value)})
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
    raise InvalidInputError(f"unsupported column definition type {type(value).__name__}")


def _spans_to_columns(spans: Sequence[Sequence[int]]) -> set[int]:
    columns: set[int] = set()
    for start, end in spans:
        low, high = int(start), int(end)
        if high < low:
            low, high = high, low
        columns.update(range(low, high + 1))
    return columns


def _colmap_chains(colmap: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """Extract ``{chain: {polymer_index_str: column}}`` from supported colmap layouts."""
    if not isinstance(colmap, Mapping):
        raise InvalidInputError("colmap must be a JSON object")
    chains: Any = colmap.get("chains")
    if chains is None and "q2col" in colmap:
        chains = {}
        for key, column in (colmap.get("q2col") or {}).items():
            chain_name, _, index = str(key).partition("|")
            if not chain_name:
                continue
            chains.setdefault(chain_name, {})[str(index)] = int(column)
        return {name: dict(mapping) for name, mapping in chains.items()}
    if chains is None:
        if all(isinstance(value, Mapping) for value in colmap.values()) and colmap:
            chains = colmap
        else:
            raise InvalidInputError(
                "colmap has no 'chains' mapping; expected {'chains': {chain: {index: column}}}",
                details={"keys": sorted(str(key) for key in colmap)[:20]},
            )
    if isinstance(chains, list):
        converted: dict[str, dict[str, Any]] = {}
        for entry in chains:
            if isinstance(entry, Mapping):
                name = str(entry.get("chain") or entry.get("name") or "")
                mapping = entry.get("residues") or entry.get("map") or {}
                if name:
                    converted[name] = dict(mapping)
        return converted
    if not isinstance(chains, Mapping):
        raise InvalidInputError("colmap 'chains' must be an object")
    return {str(name): dict(mapping) for name, mapping in chains.items() if isinstance(mapping, Mapping)}


def _columns_from_definition(columns_value: Any, colmap: Mapping[str, Any]) -> tuple[set[int], Any]:
    """Resolve a feature's ``columns`` definition into a column set."""
    source = columns_value
    if isinstance(columns_value, str):
        if columns_value not in colmap:
            raise InvalidInputError(
                f"column key {columns_value!r} not found in the structure colmap",
                details={"available_keys": sorted(str(key) for key in colmap)[:30]},
            )
        source = colmap[columns_value]
    spans = _normalise_spans(source)
    if not spans:
        raise InvalidInputError(f"column definition {columns_value!r} contains no columns")
    return _spans_to_columns(spans), spans


def _feature_kind(summary: Mapping[str, Any], chain: str) -> str:
    for info in summary.get("chains") or []:
        if info.get("chain") == chain:
            return str(info.get("kind") or "other")
    return "other"


def _default_atoms(kind: str) -> tuple[list[str] | None, list[str] | None]:
    if kind == "rrna":
        return list(_DEFAULT_RRNA_ATOMS), list(_DEFAULT_RRNA_FALLBACK)
    if kind == "protein":
        return list(_DEFAULT_PROTEIN_ATOMS), []
    return None, None


def _format_threshold(key: Any) -> str:
    """Format a threshold key deterministically ("4" for 4.0, "4.5" for 4.5)."""
    try:
        return util.fmt_float(float(key))
    except (TypeError, ValueError):
        return str(key)


def _residue_record(record: Mapping[str, Any]) -> dict[str, Any]:
    """Residue-level summary (chain/seqid/icode/resname) for residue-pair minima."""
    return {
        "chain": record.get("chain"),
        "seqid": record.get("seqid"),
        "icode": record.get("icode") or "",
        "resname": record.get("resname"),
    }


def _round_atom(atom: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "chain": atom.get("chain"),
        "seqid": atom.get("seqid"),
        "icode": atom.get("icode") or "",
        "resname": atom.get("resname"),
        "atom": atom.get("name"),
        "xyz": [
            round(float(atom.get("x", 0.0)), 3),
            round(float(atom.get("y", 0.0)), 3),
            round(float(atom.get("z", 0.0)), 3),
        ],
    }


def _residue_index(order: Sequence[tuple[str, str]], chain: str, seqid: str, icode: str) -> int | None:
    try:
        return order.index((str(seqid), str(icode or "")))
    except ValueError:
        return None


def _percentile(index: int | None, total: int) -> float | None:
    if index is None or total <= 0:
        return None
    if total == 1:
        return 0.0
    return round(100.0 * index / (total - 1), 1)


def _load_colmap(ctx: Any, value: Any, *, label: str) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Load a colmap from a path or inline object; returns (colmap, provenance_info)."""
    if value is None:
        return None, None
    if isinstance(value, Mapping):
        return dict(value), None
    path = Path(str(value)).expanduser()
    if not path.is_absolute():
        path = (Path(ctx.cwd) / path).resolve()
    if not path.is_file():
        raise NotFoundError(
            f"colmap file does not exist: {path}",
            code="COLMAP_NOT_FOUND",
            details={"path": str(path), "label": label},
        )
    payload = util.read_json(path)
    if not isinstance(payload, Mapping):
        raise DataError(f"colmap file is not a JSON object: {path}", details={"path": str(path)})
    info = {"path": str(path), "sha256": util.sha256_file(path), "bytes": path.stat().st_size}
    return dict(payload), info


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


# ---------------------------------------------------------------------------
# feature resolution
# ---------------------------------------------------------------------------


def _resolve_columns_feature(
    feature: Mapping[str, Any],
    structure: Mapping[str, Any],
    colmap: Mapping[str, Any],
    feature_id: str,
) -> dict[str, Any]:
    """Resolve a column-based feature into per-chain residue selections."""
    summary = structure["summary"]
    columns_value = feature.get("columns")
    column_set, spans = _columns_from_definition(columns_value, colmap)
    chains_map = _colmap_chains(colmap)
    selections: list[dict[str, Any]] = []
    ordered_residues: list[tuple[str, str]] = []
    columns_used: set[int] = set()
    missing_chains: list[str] = []
    missing_indices: int = 0
    for chain_name in sorted(chains_map):
        chaininfo = None
        for info in summary.get("chains") or []:
            if info.get("chain") == chain_name:
                chaininfo = info
                break
        if chaininfo is None:
            missing_chains.append(chain_name)
            continue
        residues = chaininfo.get("residues") or []
        index_map = chains_map[chain_name]
        # Sort by alignment column so atom order == coordinate order along the element.
        mapped: list[tuple[int, int, str, str]] = []
        for index_key, column in index_map.items():
            try:
                index = int(index_key)
                column_number = int(column)
            except (TypeError, ValueError):
                continue
            if column_number not in column_set:
                continue
            if index < 0 or index >= len(residues):
                missing_indices += 1
                continue
            record = residues[index]
            mapped.append((column_number, index, str(record.get("seqid")), str(record.get("icode") or "")))
        mapped.sort(key=lambda item: (item[0], item[1]))
        if not mapped:
            continue
        residues_selection = [{"seqid": seqid, "icode": icode} for _, _, seqid, icode in mapped]
        ordered_residues.extend((seqid, icode) for _, _, seqid, icode in mapped)
        columns_used.update(column for column, _, _, _ in mapped)
        kind = _feature_kind(summary, chain_name)
        atoms = feature.get("atoms")
        fallback = feature.get("fallback_atoms")
        if atoms is None:
            atoms, fallback = _default_atoms(kind)
        selections.append(
            {
                "name": feature_id,
                "chain": chain_name,
                "residues": residues_selection,
                "atoms": atoms,
                "fallback_atoms": fallback if fallback is not None else [],
            }
        )
    detail = {
        "mode": "columns",
        "columns_source": columns_value if isinstance(columns_value, str) else "inline",
        "spans": spans,
        "n_columns_requested": len(column_set),
        "n_columns_mapped": len(columns_used),
        "chains": [selection["chain"] for selection in selections],
        "missing_chains": sorted(missing_chains),
        "missing_indices": missing_indices,
    }
    if not selections:
        status = "missing_chain" if missing_chains else "missing_residues"
        return {
            "id": feature_id,
            "structure": structure["id"],
            "status": status,
            "selections": [],
            "ordered_residues": [],
            "atoms": [],
            "detail": detail,
        }
    return {
        "id": feature_id,
        "structure": structure["id"],
        "status": "pending",
        "selections": selections,
        "ordered_residues": ordered_residues,
        "detail": detail,
    }


def _resolve_residues_feature(
    feature: Mapping[str, Any],
    structure: Mapping[str, Any],
    feature_id: str,
) -> dict[str, Any]:
    """Resolve a residue-selected feature into one selection."""
    from ..lib import structures as structures_lib

    summary = structure["summary"]
    chain_name = str(feature.get("chain") or "")
    if not chain_name:
        raise InvalidInputError(
            f"feature {feature_id!r}: 'chain' is required when 'residues' is used",
            details={"feature": feature_id, "structure": structure["id"]},
        )
    chaininfo = structures_lib.find_chain(summary, chain_name)
    detail: dict[str, Any] = {"mode": "residues", "chain": chain_name, "selector": feature.get("residues")}
    if chaininfo is None:
        detail["missing_chain"] = chain_name
        return {
            "id": feature_id,
            "structure": structure["id"],
            "status": "missing_chain",
            "selections": [],
            "ordered_residues": [],
            "atoms": [],
            "detail": detail,
        }
    selector = feature.get("residues")
    selected = structures_lib.select_residues(chaininfo, selector)
    if not selected:
        return {
            "id": feature_id,
            "structure": structure["id"],
            "status": "missing_residues",
            "selections": [],
            "ordered_residues": [],
            "atoms": [],
            "detail": detail,
        }
    kind = _feature_kind(summary, chain_name)
    atoms = feature.get("atoms")
    fallback = feature.get("fallback_atoms")
    if atoms is None:
        atoms, fallback = _default_atoms(kind)
    selection = {
        "name": feature_id,
        "chain": chain_name,
        "residues": [{"seqid": seqid, "icode": icode} for seqid, icode in selected],
        "atoms": atoms,
        "fallback_atoms": fallback if fallback is not None else [],
    }
    detail["n_residues_requested"] = len(selected)
    detail["kind"] = kind
    return {
        "id": feature_id,
        "structure": structure["id"],
        "status": "pending",
        "selections": [selection],
        "ordered_residues": [(seqid, icode) for seqid, icode in selected],
        "detail": detail,
    }


def _collect_feature_atoms(ctx: Any, structure: Mapping[str, Any], resolved: dict[str, Any]) -> None:
    """Fill ``resolved['atoms']`` and finalise its status via lib.structures.collect_atoms."""
    from ..lib import structures as structures_lib

    if not resolved.get("selections"):
        return
    collected = structures_lib.collect_atoms(structure["path"], selections=resolved["selections"])
    atoms = [dict(atom) for atom in collected.get("atoms") or []]
    resolved["atoms"] = atoms
    resolved["missing"] = list(collected.get("missing") or [])
    library_status = (collected.get("feature_status") or {}).get(resolved["id"])
    if atoms and library_status in {None, "ok"}:
        resolved["status"] = "ok"
    elif not atoms and library_status in {"missing_chain", "missing_residues"}:
        resolved["status"] = library_status
    elif not atoms:
        resolved["status"] = "no_atoms"
    else:
        resolved["status"] = "ok"


# ---------------------------------------------------------------------------
# command
# ---------------------------------------------------------------------------


@command(
    name="contacts",
    title="Inter-element contact distances",
    summary="Minimum atom-atom distances, closest atom pairs, percentiles, missing features, and bridges.",
    description=(
        "Measures minimum atom-atom distances between named structural features across one or "
        "more structures. RNA features may be selected by explicit residues ('101-120') or by "
        "alignment-column spans resolved through a residue→column colmap JSON; protein features "
        "use all atoms by default. Returns per pair: min_distance, closest atom/residue pair, "
        "n_under counts for thresholds (default 4/5/10 Å), and the percentile position of the "
        "contact along each element; missing features are reported explicitly, and bridges "
        "through a third feature trigger CONTACT_BRIDGE_THROUGH_THIRD_FEATURE. Always warns "
        "DISTANCE_ATOM_SELECTION (the value depends on the atom selection; P atoms ignore side "
        "chains) and CONTACT_ABSENT_NOT_PROOF (no contact in these structures/thresholds is not "
        "evidence of no interaction)."
    ),
    input_schema=INPUT_SCHEMA,
    output_schema="contacts.schema.json",
    category="structure",
    timeout_ms=600_000,
    examples=(
        {
            "structures": [
                {"id": "structure_a", "path": "structures/structure_a.cif", "colmap": "structures/structure_a_colmap.json"}
            ],
            "features": [
                {"id": "feature_a", "structure": "structure_a", "columns": "feature_a_columns", "atoms": ["P"]},
                {"id": "feature_b", "structure": "structure_a", "columns": "feature_b_columns", "atoms": ["P"]},
                {"id": "chain_x", "structure": "structure_a", "chain": "A", "residues": "all"},
            ],
        },
    ),
    danger="read",
)
def run(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
    ctx.validate()
    from ..lib import contacts as contacts_lib
    from ..lib import structures as structures_lib

    # -- structures ---------------------------------------------------------
    structure_records = args.get("structures") or []
    if not structure_records:
        raise InvalidInputError("contacts requires at least one structure")
    structures: dict[str, dict[str, Any]] = {}
    structure_output: list[dict[str, Any]] = []
    colmap_info: dict[str, Any] = {}
    for record in structure_records:
        structure_id = str(record.get("id") or "")
        if not structure_id:
            raise InvalidInputError("every structure needs a non-empty 'id'")
        if structure_id in structures:
            raise InvalidInputError(f"duplicate structure id {structure_id!r}", details={"id": structure_id})
        path = Path(str(record.get("path") or "")).expanduser()
        if not path.is_absolute():
            path = (Path(ctx.cwd) / path).resolve()
        if not path.is_file():
            raise NotFoundError(
                f"structure file does not exist: {path}",
                code="STRUCTURE_NOT_FOUND",
                details={"structure": structure_id, "path": str(path)},
            )
        _record_local(ctx, path, role="contacts_structure")
        summary = structures_lib.read_structure(path)
        colmap, colmap_record = _load_colmap(ctx, record.get("colmap"), label=structure_id)
        if colmap_record:
            _record_local(ctx, colmap_record["path"], role="contacts_colmap")
            colmap_info[structure_id] = colmap_record
        structures[structure_id] = {
            "id": structure_id,
            "path": path,
            "summary": summary,
            "colmap": colmap,
            "sha256": util.sha256_file(path),
            "bytes": path.stat().st_size,
        }
        structure_output.append(
            {
                "id": structure_id,
                "path": str(path),
                "sha256": structures[structure_id]["sha256"],
                "bytes": structures[structure_id]["bytes"],
                "format": summary.get("format"),
                "n_chains": len(summary.get("chains") or []),
            }
        )

    # -- features -----------------------------------------------------------
    feature_records = args.get("features") or []
    if not feature_records:
        raise InvalidInputError("contacts requires at least one feature")
    features: dict[tuple[str, str], dict[str, Any]] = {}
    for record in feature_records:
        feature_id = str(record.get("id") or "")
        structure_id = str(record.get("structure") or "")
        if not feature_id:
            raise InvalidInputError("every feature needs a non-empty 'id'")
        if structure_id not in structures:
            raise InvalidInputError(
                f"feature {feature_id!r} references unknown structure {structure_id!r}",
                details={"known_structures": sorted(structures)},
            )
        key = (structure_id, feature_id)
        if key in features:
            raise InvalidInputError(
                f"duplicate feature {feature_id!r} in structure {structure_id!r}",
                details={"structure": structure_id, "feature": feature_id},
            )
        structure = structures[structure_id]
        has_columns = "columns" in record and record.get("columns") is not None
        has_residues = "residues" in record and record.get("residues") is not None
        if has_columns and has_residues:
            raise InvalidInputError(
                f"feature {feature_id!r}: use either 'residues' or 'columns', not both",
                details={"structure": structure_id, "feature": feature_id},
            )
        if has_columns:
            colmap, colmap_record = _load_colmap(
                ctx,
                record.get("colmap") if record.get("colmap") is not None else structure.get("colmap"),
                label=f"{structure_id}:{feature_id}",
            )
            if colmap is None:
                raise InvalidInputError(
                    f"feature {feature_id!r} uses 'columns' but no colmap is available for "
                    f"structure {structure_id!r}",
                    details={"structure": structure_id, "feature": feature_id},
                )
            if colmap_record:
                _record_local(ctx, colmap_record["path"], role="contacts_colmap")
            resolved = _resolve_columns_feature(record, structure, colmap, feature_id)
        elif has_residues:
            resolved = _resolve_residues_feature(record, structure, feature_id)
        else:
            raise InvalidInputError(
                f"feature {feature_id!r} needs 'residues' or 'columns'",
                details={"structure": structure_id, "feature": feature_id},
            )
        _collect_feature_atoms(ctx, structure, resolved)
        features[key] = resolved

    # -- feature payloads and missing features ------------------------------
    feature_output: list[dict[str, Any]] = []
    feature_status: dict[str, dict[str, str]] = {}
    missing: list[dict[str, Any]] = []
    atom_selection_note: dict[str, Any] = {}
    for (structure_id, feature_id) in sorted(features):
        resolved = features[(structure_id, feature_id)]
        status = str(resolved.get("status"))
        feature_status.setdefault(structure_id, {})[feature_id] = status
        detail = dict(resolved.get("detail") or {})
        kind = detail.get("kind")
        atoms_label = None
        if resolved.get("selections"):
            atoms_label = resolved["selections"][0].get("atoms")
        elif kind:
            atoms_label, _ = _default_atoms(str(kind))
        feature_output.append(
            {
                "id": feature_id,
                "structure": structure_id,
                "status": status,
                **{key: value for key, value in detail.items() if key != "kind"},
                "atoms": atoms_label,
                "n_atoms": len(resolved.get("atoms") or []),
                "n_residues": len(resolved.get("ordered_residues") or []),
            }
        )
        atom_selection_note[f"{structure_id}:{feature_id}"] = {
            "mode": detail.get("mode"),
            "atoms": atoms_label,
            "n_atoms": len(resolved.get("atoms") or []),
        }
        if status != "ok":
            missing.append(
                {
                    "structure": structure_id,
                    "feature": feature_id,
                    "status": status,
                    "detail": detail,
                }
            )
            ctx.warn(
                "FEATURE_MISSING",
                f"Feature {feature_id!r} in structure {structure_id!r} is {status}: distances "
                "involving it cannot be computed. This is an explicit missing-feature result, "
                "not a zero distance.",
                context={"structure": structure_id, "feature": feature_id, "status": status},
            )

    # -- pairs: default = all pairs within each structure -------------------
    requested_pairs = args.get("pairs")
    pair_specs: list[tuple[str, str]] = []
    if requested_pairs:
        for entry in requested_pairs:
            if isinstance(entry, Mapping):
                a, b = str(entry.get("a") or ""), str(entry.get("b") or "")
            else:
                a, b = str(entry[0]), str(entry[1])
            if not a or not b or a == b:
                raise InvalidInputError(
                    "each pair must name two distinct features",
                    details={"pair": entry},
                )
            pair_specs.append((a, b))
    else:
        for structure_id in sorted(structures):
            ids_in_structure = sorted(
                feature_id for (sid, feature_id) in features if sid == structure_id
            )
            pair_specs.extend(combinations(ids_in_structure, 2))
    # De-duplicate while keeping deterministic order.
    pair_specs = sorted(set(pair_specs))

    thresholds = [float(value) for value in (args.get("thresholds") or [4.0, 5.0, 10.0])]
    thresholds = sorted({round(value, 3) for value in thresholds if value >= 0})
    top_pairs = int(args.get("top_pairs", 10))
    bridge_threshold = float(args.get("bridge_threshold", 4.5))

    # Pre-compute pairwise minima per structure for present features (bridging).
    minima: dict[tuple[str, str, str], float] = {}
    pair_payload: list[dict[str, Any]] = []
    skipped_pairs: list[dict[str, Any]] = []
    absent_not_proof: list[dict[str, Any]] = []
    for a, b in pair_specs:
        computed_any = False
        for structure_id in sorted(structures):
            feature_a = features.get((structure_id, a))
            feature_b = features.get((structure_id, b))
            if feature_a is None or feature_b is None:
                continue
            atoms_a = feature_a.get("atoms") or []
            atoms_b = feature_b.get("atoms") or []
            if not atoms_a or not atoms_b:
                continue
            result = contacts_lib.min_distances(
                atoms_a,
                atoms_b,
                kdtree=True,
                thresholds=tuple(thresholds),
                limit=top_pairs,
            )
            min_distance = float(result.get("min_distance") or 0.0)
            minima[(structure_id, a, b)] = min_distance
            minima[(structure_id, b, a)] = min_distance
            closest_raw = (result.get("pairs") or [None])[0]
            closest = None
            if closest_raw:
                closest = {
                    "a": _round_atom(closest_raw.get("a") or {}),
                    "b": _round_atom(closest_raw.get("b") or {}),
                    "distance": round(float(closest_raw.get("distance") or min_distance), 3),
                }
            positions: dict[str, Any] = {}
            if closest:
                resid_a = closest_raw.get("a") or {}
                resid_b = closest_raw.get("b") or {}
                order_a = feature_a.get("ordered_residues") or []
                order_b = feature_b.get("ordered_residues") or []
                index_a = _residue_index(order_a, str(resid_a.get("chain")), str(resid_a.get("seqid")), str(resid_a.get("icode") or ""))
                index_b = _residue_index(order_b, str(resid_b.get("chain")), str(resid_b.get("seqid")), str(resid_b.get("icode") or ""))
                positions = {
                    "a": {
                        "index": index_a,
                        "n": len(order_a),
                        "percentile": _percentile(index_a, len(order_a)),
                    },
                    "b": {
                        "index": index_b,
                        "n": len(order_b),
                        "percentile": _percentile(index_b, len(order_b)),
                    },
                }
            pair_payload.append(
                {
                    "structure": structure_id,
                    "a": a,
                    "b": b,
                    "min_distance": round(min_distance, 3),
                    "closest": closest,
                    "residue_pairs": [
                        {
                            "a": _residue_record(residue_pair.get("a") or {}),
                            "b": _residue_record(residue_pair.get("b") or {}),
                            "distance": round(float(residue_pair.get("distance") or 0.0), 3),
                        }
                        for residue_pair in (result.get("residue_pairs") or [])[:5]
                    ],
                    "n_under": {
                        _format_threshold(key): int(value)
                        for key, value in (result.get("n_under") or {}).items()
                    },
                    "n_atoms_a": int(result.get("n_atoms_a") or len(atoms_a)),
                    "n_atoms_b": int(result.get("n_atoms_b") or len(atoms_b)),
                    "contact_positions": positions,
                }
            )
            computed_any = True
            if thresholds and min_distance > max(thresholds):
                absent_not_proof.append(
                    {
                        "structure": structure_id,
                        "a": a,
                        "b": b,
                        "min_distance": round(min_distance, 3),
                        "max_threshold": max(thresholds),
                    }
                )
        if not computed_any:
            skipped_pairs.append(
                {
                    "a": a,
                    "b": b,
                    "reason": "no structure has both features with atoms (see feature_status/missing)",
                }
            )
    pair_payload.sort(key=lambda row: (row["structure"], row["a"], row["b"]))
    skipped_pairs.sort(key=lambda row: (row["a"], row["b"]))
    if absent_not_proof:
        ctx.warn(
            "CONTACT_ABSENT_NOT_PROOF",
            "One or more feature pairs have minimum distances above every threshold; that is "
            "not evidence of no interaction. Contacts are conformation-, atom-selection- and "
            "structure-dependent (crystal/cryo-EM packing, missing atoms, other states).",
            context={
                "pairs": absent_not_proof[:10],
                "max_threshold": max(thresholds) if thresholds else None,
            },
        )

    # -- bridges through a third feature ------------------------------------
    bridges: list[dict[str, Any]] = []
    for structure_id in sorted(structures):
        ids_in_structure = sorted(
            feature_id for (sid, feature_id) in features if sid == structure_id
        )
        for a, b in combinations(ids_in_structure, 2):
            distance_ab = minima.get((structure_id, a, b))
            if distance_ab is None or distance_ab <= bridge_threshold:
                continue
            for via in ids_in_structure:
                if via in {a, b}:
                    continue
                distance_a = minima.get((structure_id, a, via))
                distance_b = minima.get((structure_id, b, via))
                if distance_a is None or distance_b is None:
                    # Features may not be pairwise-compared yet; compute on demand.
                    feature_a = features.get((structure_id, a))
                    feature_via = features.get((structure_id, via))
                    if distance_a is None and feature_a and feature_via and feature_a.get("atoms") and feature_via.get("atoms"):
                        computed = contacts_lib.min_distances(feature_a["atoms"], feature_via["atoms"], kdtree=True, thresholds=(), limit=1)
                        distance_a = float(computed.get("min_distance") or 0.0)
                        minima[(structure_id, a, via)] = distance_a
                    feature_b = features.get((structure_id, b))
                    if distance_b is None and feature_b and feature_via and feature_b.get("atoms") and feature_via.get("atoms"):
                        computed = contacts_lib.min_distances(feature_b["atoms"], feature_via["atoms"], kdtree=True, thresholds=(), limit=1)
                        distance_b = float(computed.get("min_distance") or 0.0)
                        minima[(structure_id, b, via)] = distance_b
                if distance_a is None or distance_b is None:
                    continue
                if distance_a <= bridge_threshold and distance_b <= bridge_threshold:
                    bridges.append(
                        {
                            "structure": structure_id,
                            "a": a,
                            "b": b,
                            "via": via,
                            "distance_ab": round(distance_ab, 3),
                            "distance_via_a": round(distance_a, 3),
                            "distance_via_b": round(distance_b, 3),
                            "bridge_threshold": bridge_threshold,
                        }
                    )
    bridges.sort(key=lambda row: (row["structure"], row["a"], row["b"], row["via"]))
    if bridges:
        ctx.warn(
            "CONTACT_BRIDGE_THROUGH_THIRD_FEATURE",
            "A third feature lies within the bridge threshold of both members of a pair whose "
            "minimum distance is larger: the two elements may be linked through that bridging "
            "element rather than touching directly.",
            context={"bridges": bridges[:10], "bridge_threshold": bridge_threshold},
        )

    atom_selection_summary = {
        "rrna_default": {"atoms": _DEFAULT_RRNA_ATOMS, "fallback_atoms": _DEFAULT_RRNA_FALLBACK},
        "protein_default": {"atoms": _DEFAULT_PROTEIN_ATOMS},
        "features": atom_selection_note,
        "note": (
            "Distances depend on the atom selection: P-atom rRNA contact minima ignore "
            "side-chain/heteroatom contact, all-atom protein features include side chains."
        ),
    }
    ctx.warn(
        "DISTANCE_ATOM_SELECTION",
        "Contact distances are minimum atom-atom distances for the selected atom sets: rRNA "
        "features default to P atoms with C1' fallback (no side chains/base edges), protein "
        "features default to all atoms. Report the atom selection with every distance.",
        context=atom_selection_summary,
    )

    payload: dict[str, Any] = {
        "structures": structure_output,
        "features": feature_output,
        "feature_status": feature_status,
        "missing": missing,
        "pairs": pair_payload,
        "skipped_pairs": skipped_pairs,
        "bridges": bridges,
        "thresholds": thresholds,
        "bridge_threshold": bridge_threshold,
        "top_pairs": top_pairs,
        "atom_selection": atom_selection_summary,
    }
    if colmap_info:
        payload["colmaps"] = colmap_info
    out_path = args.get("out")
    if out_path and not bool(ctx.dry_run):
        target = Path(str(out_path)).expanduser()
        if not target.is_absolute():
            target = (Path(ctx.cwd) / target).resolve()
        util.atomic_write_json(target, payload)
        payload["out"] = str(target)
    return payload
