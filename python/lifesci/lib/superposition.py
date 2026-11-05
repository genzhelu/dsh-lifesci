"""Superposition: Kabsch, iterative refinement and column-based multi-structure fits.

Coordinate conventions
----------------------
All coordinates are row vectors (``n x 3``); :func:`kabsch` returns ``R, t``
such that ``P @ R.T + t ≈ Q`` (which is identical to ``R @ p + t`` for a column
vector ``p``).

The column-based workflow (:func:`superpose_structures`) uses a *universal*
coordinate system: every modelled residue carries the alignment column it maps
to (from :mod:`lifesci.lib.alignment` or a precomputed processed JSON).  Core
columns are those modelled in **every** structure (optionally excluding
caller-supplied spans); each non-reference structure is superposed onto the
reference using only core-column ``P`` (fallback ``C1'``) atoms.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import numpy as np

from ..errors import DataError, InvalidInputError
from ..util import json_safe

#: Distance cutoff (Å) used by the iterative superposition refinement.
DEFAULT_REFINE_CUTOFF: float = 4.0
#: Number of refinement iterations used by the iterative superposition.
DEFAULT_REFINE_ITERATIONS: int = 5


# ---------------------------------------------------------------------------
# primitives
# ---------------------------------------------------------------------------


def kabsch(P: Any, Q: Any) -> tuple[np.ndarray, np.ndarray]:
    """Least-squares rigid transform aligning ``P`` onto ``Q``.

    Parameters
    ----------
    P, Q:
        ``(n, 3)`` arrays of corresponding points.

    Returns
    -------
    (R, t):
        Rotation matrix ``(3, 3)`` and translation vector ``(3,)`` such that
        ``P @ R.T + t ≈ Q`` (proper rotation, never a reflection).
    """
    P = np.asarray(P, dtype=float)
    Q = np.asarray(Q, dtype=float)
    if P.shape != Q.shape:
        raise InvalidInputError(
            f"kabsch needs matching point arrays, got {P.shape} and {Q.shape}",
            details={"shape_p": list(P.shape), "shape_q": list(Q.shape)},
        )
    if P.ndim != 2 or P.shape[1] != 3 or P.shape[0] < 3:
        raise InvalidInputError(
            "kabsch needs an (n>=3, 3) point array",
            details={"shape": list(P.shape)},
        )
    P_centered = P - P.mean(axis=0)
    Q_centered = Q - Q.mean(axis=0)
    covariance = P_centered.T @ Q_centered
    U, _singular_values, Vt = np.linalg.svd(covariance)
    sign = np.sign(np.linalg.det(Vt.T @ U.T))
    correction = np.diag([1.0, 1.0, float(sign)])
    rotation = Vt.T @ correction @ U.T
    translation = Q.mean(axis=0) - rotation @ P.mean(axis=0)
    return rotation, translation


def transform(xyz: Any, R: Any, t: Any) -> np.ndarray:
    """Apply a rigid transform: ``xyz @ R.T + t`` (row-vector convention)."""
    points = np.asarray(xyz, dtype=float)
    rotation = np.asarray(R, dtype=float)
    translation = np.asarray(t, dtype=float)
    return points @ rotation.T + translation


def rmsd(P: Any, Q: Any) -> float:
    """Root-mean-square deviation between corresponding points (same frame)."""
    P = np.asarray(P, dtype=float)
    Q = np.asarray(Q, dtype=float)
    if P.shape != Q.shape:
        raise InvalidInputError(
            f"rmsd needs matching point arrays, got {P.shape} and {Q.shape}",
            details={"shape_p": list(P.shape), "shape_q": list(Q.shape)},
        )
    if P.size == 0:
        return float("nan")
    delta = P - Q
    return float(np.sqrt((delta * delta).sum(axis=1).mean()))


def superpose_refine(
    P: Any,
    Q: Any,
    cutoff: float = 3.0,
    n_iter: int = DEFAULT_REFINE_ITERATIONS,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, float]:
    """Kabsch superposition with iterative outlier rejection.

    Parameters
    ----------
    P, Q:
        ``(n, 3)`` corresponding points (``P`` is moved onto ``Q``).
    cutoff:
        Distance (Å) above which a point pair is rejected as an outlier.
    n_iter:
        Maximum number of refine/re-fit rounds.

    Returns
    -------
    (R, t, mask, rmsd):
        The transform, the boolean inlier mask used for the final fit, and the
        RMSD over inliers.  Matches the reference implementation
        (including keeping the previous mask when fewer than 50 points survive).
    """
    P = np.asarray(P, dtype=float)
    Q = np.asarray(Q, dtype=float)
    if P.shape != Q.shape or P.ndim != 2 or P.shape[1] != 3:
        raise InvalidInputError(
            f"superpose_refine needs two matching (n, 3) arrays, got {P.shape} and {Q.shape}",
            details={"shape_p": list(P.shape), "shape_q": list(Q.shape)},
        )
    if P.shape[0] < 3:
        raise InvalidInputError(
            "superpose_refine needs at least 3 point pairs",
            details={"n": int(P.shape[0])},
        )
    mask = np.ones(P.shape[0], dtype=bool)
    rotation = np.eye(3)
    translation = np.zeros(3)
    for _ in range(max(1, int(n_iter))):
        rotation, translation = kabsch(P[mask], Q[mask])
        deviations = np.linalg.norm(P @ rotation.T + translation - Q, axis=1)
        new_mask = deviations < cutoff
        if new_mask.sum() < 50 or bool((new_mask == mask).all()):
            if new_mask.sum() >= 50:
                mask = new_mask
            break
        mask = new_mask
    rotation, translation = kabsch(P[mask], Q[mask])
    deviations = np.linalg.norm(P @ rotation.T + translation - Q, axis=1)
    return (
        rotation,
        translation,
        mask,
        float(np.sqrt((deviations[mask] ** 2).mean())),
    )


# ---------------------------------------------------------------------------
# column maps
# ---------------------------------------------------------------------------


def normalise_colmap(colmap: Any) -> dict[str, Any]:
    """Normalise the supported residue -> alignment-column map formats.

    Accepted inputs:

    * ``{"AA|616": 1234, ...}`` — the processed ``q2col`` style, where
      the part after ``|`` is a **polymer index** (0-based);
    * ``{"AA|S616": 1234, ...}`` / ``"AA:S616"`` — explicit-seqid keys;
    * ``{"chains": {"AA": {"0": 348, ...}}, "chain_residue_ids": {"AA": [...]}}``
      — the processed colmap format (polymer index -> column, with the
      residue ids listed separately);
    * ``[{"chain","seqid","icode","column"}, ...]`` — explicit residue records;
    * ``{ "AA": {616: 1234, ...} }`` — chain -> seqid -> column.

    Returns
    -------
    dict
        ``{"residues": [{"chain","seqid","icode","column"}], "indices":
        [{"chain","index","column"}], "columns": sorted unique columns}``.
    """
    residues: list[dict[str, Any]] = []
    indices: list[dict[str, Any]] = []
    if isinstance(colmap, Mapping):
        if "chains" in colmap and isinstance(colmap.get("chains"), Mapping):
            chains = colmap["chains"]
            residue_ids = colmap.get("chain_residue_ids") or {}
            for chain, index_map in chains.items():
                if not isinstance(index_map, Mapping):
                    continue
                ids = residue_ids.get(chain) or []
                for raw_index, raw_column in index_map.items():
                    try:
                        index = int(raw_index)
                        column = int(raw_column)
                    except (TypeError, ValueError):
                        continue
                    indices.append(
                        {"chain": str(chain), "index": index, "column": column}
                    )
                    if 0 <= index < len(ids):
                        residue_id = ids[index]
                        if isinstance(residue_id, Mapping):
                            seqid = str(residue_id.get("seqid", ""))
                            icode = str(residue_id.get("icode") or "")
                        else:
                            text = str(residue_id)
                            seqid, icode = _split_residue_id(text)
                        residues.append(
                            {
                                "chain": str(chain),
                                "seqid": seqid,
                                "icode": icode,
                                "column": column,
                            }
                        )
        elif "residues" in colmap and isinstance(colmap.get("residues"), Sequence):
            for record in colmap["residues"]:
                if not isinstance(record, Mapping):
                    continue
                try:
                    column = int(record["column"])
                except (KeyError, TypeError, ValueError):
                    continue
                residues.append(
                    {
                        "chain": str(record.get("chain", "")),
                        "seqid": str(record.get("seqid", "")),
                        "icode": str(record.get("icode") or ""),
                        "column": column,
                    }
                )
        else:
            for chain, mapping in colmap.items():
                if not isinstance(mapping, Mapping):
                    continue
                for key, raw_column in mapping.items():
                    try:
                        column = int(raw_column)
                    except (TypeError, ValueError):
                        continue
                    text = str(key)
                    if "|" in text:
                        _prefix, text = text.split("|", 1)
                    if text.startswith("S"):
                        residues.append(
                            {
                                "chain": str(chain),
                                "seqid": _split_residue_id(text[1:])[0],
                                "icode": _split_residue_id(text[1:])[1],
                                "column": column,
                            }
                        )
                    else:
                        try:
                            indices.append(
                                {"chain": str(chain), "index": int(text), "column": column}
                            )
                        except ValueError:
                            seqid, icode = _split_residue_id(text)
                            residues.append(
                                {
                                    "chain": str(chain),
                                    "seqid": seqid,
                                    "icode": icode,
                                    "column": column,
                                }
                            )
    elif isinstance(colmap, Sequence):
        for record in colmap:
            if not isinstance(record, Mapping):
                continue
            try:
                column = int(record["column"])
            except (KeyError, TypeError, ValueError):
                continue
            residues.append(
                {
                    "chain": str(record.get("chain", "")),
                    "seqid": str(record.get("seqid", "")),
                    "icode": str(record.get("icode") or ""),
                    "column": column,
                }
            )
    else:
        raise InvalidInputError(
            "unsupported column-map format",
            details={"type": type(colmap).__name__},
        )
    columns = sorted(
        {int(record["column"]) for record in residues}
        | {int(record["column"]) for record in indices}
    )
    return {
        "residues": _sort_records(residues),
        "indices": _sort_records(indices),
        "columns": columns,
    }


def _sort_records(records: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        (dict(record) for record in records),
        key=lambda record: (
            str(record.get("chain", "")),
            int(record.get("column", 0)),
            str(record.get("seqid", "")),
            int(record.get("index", 0)),
        ),
    )


def _split_residue_id(text: str) -> tuple[str, str]:
    """Split ``"616"`` / ``"616A"`` / ``"616.A"`` into ``(seqid, icode)``."""
    token = str(text)
    if "." in token:
        seqid, icode = token.split(".", 1)
        return seqid, icode
    digits = ""
    suffix = ""
    for char in token:
        if char.isdigit() and not suffix:
            digits += char
        else:
            suffix += char
    return (digits or token), suffix


# ---------------------------------------------------------------------------
# structure coordinates per column
# ---------------------------------------------------------------------------


def _load_structure_coordinates(
    path: str | Path,
) -> tuple[Any, dict[str, dict[str, Any]]]:
    """Load a structure and index its modelled residues by chain.

    Returns ``(structure, lookup)`` where ``lookup[chain]["by_key"]`` maps
    ``(seqid, icode)`` -> gemmi Residue and ``lookup[chain]["by_index"]`` is the
    polymer-order residue list.  Chain membership is tested by name (never with
    ``gemmi.Model.__contains__``, which tests chain objects).
    """
    import gemmi

    target = Path(path)
    if not target.is_file():
        raise DataError(
            f"structure file not found: {target}", details={"path": str(target)}
        )
    try:
        structure = gemmi.read_structure(str(target))
    except Exception as err:  # pragma: no cover - defensive
        raise DataError(
            f"gemmi failed to parse structure {target}: {err}",
            details={"path": str(target)},
        ) from err
    lookup: dict[str, dict[str, Any]] = {}
    for chain in structure[0]:
        polymer = chain.get_polymer()
        if polymer is None:
            continue
        by_index = list(polymer)
        by_key = {
            (
                str(residue.seqid.num),
                residue.seqid.icode.strip(),
            ): residue
            for residue in by_index
        }
        lookup[chain.name] = {"by_index": by_index, "by_key": by_key}
    return structure, lookup


def _residue_point(residue: Any) -> np.ndarray | None:
    """Coordinate of the P atom (fallback C1'/C1*) of a residue, or ``None``."""
    for atom_name in ("P", "C1'", "C1*"):
        atom = residue.find_atom(atom_name, "*")
        if atom is not None:
            return np.array([atom.pos.x, atom.pos.y, atom.pos.z], dtype=float)
    return None


def _column_points(
    lookup: Mapping[str, Mapping[str, Any]],
    colmap: Mapping[str, Any],
) -> dict[int, np.ndarray]:
    """column -> coordinate for one structure, from a normalised colmap."""
    points: dict[int, np.ndarray] = {}
    for record in colmap.get("residues") or []:
        chain_info = lookup.get(str(record["chain"]))
        if not chain_info:
            continue
        residue = chain_info["by_key"].get(
            (str(record["seqid"]), str(record.get("icode") or ""))
        )
        if residue is None:
            continue
        point = _residue_point(residue)
        if point is not None:
            points.setdefault(int(record["column"]), point)
    for record in colmap.get("indices") or []:
        chain_info = lookup.get(str(record["chain"]))
        if not chain_info:
            continue
        index = int(record["index"])
        by_index = chain_info["by_index"]
        if not 0 <= index < len(by_index):
            continue
        point = _residue_point(by_index[index])
        if point is not None:
            points.setdefault(int(record["column"]), point)
    return points


# ---------------------------------------------------------------------------
# multi-structure superposition
# ---------------------------------------------------------------------------


def _normalise_structures(structures: Any) -> list[dict[str, str]]:
    """Return ``[{"id","path"}]`` from the supported structure-collection forms."""
    normalised: list[dict[str, str]] = []
    if isinstance(structures, Mapping):
        items = list(structures.items())
        for key, value in items:
            if isinstance(value, Mapping):
                path = value.get("path")
                identifier = str(value.get("id") or key)
            else:
                path = value
                identifier = str(key)
            if not path:
                raise InvalidInputError(f"structure {key!r} has no path")
            normalised.append({"id": identifier, "path": str(path)})
        return normalised
    for item in structures or []:
        if isinstance(item, Mapping):
            path = item.get("path")
            if not path:
                raise InvalidInputError(
                    "structure entries need a 'path'",
                    details={"structure": json_safe(dict(item))},
                )
            stem = Path(str(path)).name.split(".")[0]
            normalised.append(
                {"id": str(item.get("id") or stem), "path": str(path)}
            )
        else:
            stem = Path(str(item)).name.split(".")[0]
            normalised.append({"id": stem, "path": str(item)})
    return normalised


def superpose_structures(
    ctx: Any,
    structures: Any,
    *,
    reference: str | None = None,
    colmaps: Mapping[str, Any],
    core_columns: Sequence[int] | None = None,
    cutoff: float = DEFAULT_REFINE_CUTOFF,
    n_iter: int = DEFAULT_REFINE_ITERATIONS,
    min_core_columns: int = 3,
) -> dict[str, Any]:
    """Superpose a set of structures on common alignment columns.

    Parameters
    ----------
    ctx:
        :class:`lifesci.context.Context` (used only for warnings) or ``None``.
    structures:
        Structure collection: ``[{"id","path"}, ...]``, ``{id: path}`` or
        ``[path, ...]`` (the id defaults to the file stem).
    reference:
        Id of the reference structure (defaults to the first structure).  The
        returned transforms map each *other* structure onto this frame.
    colmaps:
        ``{id: residue->column map}`` — any format accepted by
        :func:`normalise_colmap`.
    core_columns:
        Explicit universal columns to use.  When omitted, the core is every
        column whose ``P``/``C1'`` coordinate is available in **all**
        structures (sorted ascending).
    cutoff, n_iter:
        Refinement parameters for :func:`superpose_refine`.
    min_core_columns:
        Raise ``NO_SHARED_COLUMNS`` when fewer than this many columns are shared.

    Returns
    -------
    dict
        ``{"reference", "n_core_columns", "core_columns" (sorted), "transforms":
        {id: {"R","t","core_rmsd","n_core"}}, "rmsds": {id: float},
        "per_structure": [{"id","n_core","n_core_used","core_rmsd",
        "missing_core_columns"}], "absent_columns": {id: [column, ...]}}``.
    """
    structure_list = _normalise_structures(structures)
    if not structure_list:
        raise InvalidInputError("superpose_structures needs at least one structure")
    ids = [item["id"] for item in structure_list]
    if len(set(ids)) != len(ids):
        raise InvalidInputError(
            "structure ids must be unique", details={"ids": ids}
        )
    if not colmaps:
        raise InvalidInputError("superpose_structures needs column maps (colmaps)")
    missing_maps = [identifier for identifier in ids if identifier not in colmaps]
    if missing_maps:
        raise InvalidInputError(
            "column maps are missing for some structures",
            details={"missing": missing_maps},
        )
    reference_id = str(reference or ids[0])
    if reference_id not in ids:
        raise InvalidInputError(
            f"reference structure {reference_id!r} is not in the structure set",
            details={"reference": reference_id, "ids": ids},
        )

    normalised_maps = {identifier: normalise_colmap(colmaps[identifier]) for identifier in ids}
    points_by_id: dict[str, dict[int, np.ndarray]] = {}
    lookup_by_id: dict[str, Any] = {}
    for item in structure_list:
        _structure, lookup = _load_structure_coordinates(item["path"])
        lookup_by_id[item["id"]] = lookup
        points_by_id[item["id"]] = _column_points(lookup, normalised_maps[item["id"]])

    if core_columns is None:
        common = set(points_by_id[ids[0]])
        for identifier in ids[1:]:
            common &= set(points_by_id[identifier])
        core = sorted(common)
    else:
        core = [int(column) for column in core_columns]
        present = set(points_by_id[reference_id])
        for identifier in ids:
            present &= set(points_by_id[identifier])
        core = [column for column in core if column in present]
    if len(core) < max(1, int(min_core_columns)):
        raise DataError(
            f"NO_SHARED_COLUMNS: only {len(core)} shared alignment columns across "
            f"{len(ids)} structures (need at least {max(1, int(min_core_columns))})",
            code="NO_SHARED_COLUMNS",
            details={"n_shared": len(core), "structures": ids},
        )
    if ctx is not None and hasattr(ctx, "warn") and len(core) < 50:
        ctx.warn(
            "FEW_CORE_COLUMNS",
            (
                f"Only {len(core)} shared core columns were available; the "
                "superposition (and downstream RMSDs) is under-constrained. "
                "Interpret core/local RMSDs as indicative only."
            ),
            context={"n_core_columns": len(core)},
        )

    reference_points = points_by_id[reference_id]
    P_reference = np.array([reference_points[column] for column in core], dtype=float)
    transforms: dict[str, dict[str, Any]] = {}
    rmsds: dict[str, float] = {}
    per_structure: list[dict[str, Any]] = []
    absent_columns: dict[str, list[int]] = {}
    for item in structure_list:
        identifier = item["id"]
        structure_points = points_by_id[identifier]
        absent = sorted(
            int(column) for column in (set(reference_points) - set(structure_points))
            if int(column) in set(core)
        )
        absent_columns[identifier] = absent
        if identifier == reference_id:
            rotation = np.eye(3)
            translation = np.zeros(3)
            core_rmsd = 0.0
            n_core_used = len(core)
        else:
            Q = np.array([structure_points[column] for column in core], dtype=float)
            rotation, translation, mask, core_rmsd = superpose_refine(
                Q, P_reference, cutoff=cutoff, n_iter=n_iter
            )
            n_core_used = int(mask.sum())
        transforms[identifier] = {
            "R": rotation.tolist(),
            "t": translation.tolist(),
            "core_rmsd": round(float(core_rmsd), 4),
            "n_core": n_core_used,
        }
        rmsds[identifier] = round(float(core_rmsd), 4)
        per_structure.append(
            {
                "id": identifier,
                "reference": identifier == reference_id,
                "n_core": len(core),
                "n_core_used": n_core_used,
                "core_rmsd": round(float(core_rmsd), 4),
                "n_missing_core_columns": len(absent),
                "mapped_columns": len(structure_points),
            }
        )

    return {
        "reference": reference_id,
        "n_core_columns": len(core),
        "core_columns": core,
        "transforms": transforms,
        "rmsds": rmsds,
        "per_structure": per_structure,
        "absent_columns": absent_columns,
        "parameters": {
            "cutoff": float(cutoff),
            "n_iter": int(n_iter),
            "coordinate_atom": "P (fallback C1')",
        },
    }
