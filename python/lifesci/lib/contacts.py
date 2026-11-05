"""Inter-element contact analysis with atom-level KD-trees.

The library measures the minimum atom-atom distance between structural
features (RNA elements, protein chains) and reports the closest atom/residue
pairs, the percentile position of the contact along each feature, and bridges
through third features.

Conventions
-----------
* Atom inputs are the dicts produced by :func:`lifesci.lib.structures.collect_atoms`
  (``chain, seqid, icode, resname, name, x, y, z``); plain ``(x, y, z)`` points
  are also accepted.
* ``n_under`` counts, **per atom of feature A**, how many A atoms have at least
  one B atom closer than the threshold ("number of feature-A atoms within X Å of
  the feature-B atom set").
* Distances are rounded to 3 decimals at output time only; internal maths uses
  full precision.
* Output ordering is deterministic: pairs by distance then chain/seqid/atom
  name; bridges sorted by pair then third feature.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

import numpy as np

from ..errors import DataError, InvalidInputError
from ..util import json_safe
from . import structures as structures_lib

#: Default distance thresholds (Å) for contact counts.
DEFAULT_THRESHOLDS: tuple[float, ...] = (4.0, 5.0, 10.0)
#: A third feature within this distance (Å) of both partners is a bridge.
DEFAULT_BRIDGE_CUTOFF: float = 4.5

_ATOM_FIELDS = ("chain", "seqid", "icode", "resname", "name", "x", "y", "z")


# ---------------------------------------------------------------------------
# atom helpers
# ---------------------------------------------------------------------------


def _atom_xyz(atom: Any) -> np.ndarray:
    if isinstance(atom, Mapping):
        try:
            return np.array(
                [float(atom["x"]), float(atom["y"]), float(atom["z"])],
                dtype=float,
            )
        except (KeyError, TypeError, ValueError) as err:
            raise InvalidInputError(
                "atom dictionaries need numeric x, y, z",
                details={"atom": json_safe(dict(atom))},
            ) from err
    return np.asarray(atom, dtype=float)


def _coordinates(atoms: Sequence[Any]) -> np.ndarray:
    if not atoms:
        return np.zeros((0, 3), dtype=float)
    return np.vstack([_atom_xyz(atom) for atom in atoms])


def _clean_atom(atom: Any) -> dict[str, Any]:
    """Return a canonical atom record (extra keys are dropped, floats kept)."""
    if not isinstance(atom, Mapping):
        xyz = _atom_xyz(atom)
        return {
            "chain": "",
            "seqid": "",
            "icode": "",
            "resname": "",
            "name": "",
            "x": float(xyz[0]),
            "y": float(xyz[1]),
            "z": float(xyz[2]),
        }
    return {
        "chain": str(atom.get("chain", "")),
        "seqid": str(atom.get("seqid", "")),
        "icode": str(atom.get("icode") or ""),
        "resname": str(atom.get("resname", "")),
        "name": str(atom.get("name", "")),
        "x": float(atom.get("x", 0.0)),
        "y": float(atom.get("y", 0.0)),
        "z": float(atom.get("z", 0.0)),
    }


def _atom_sort_key(atom: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        str(atom.get("chain", "")),
        str(atom.get("seqid", "")),
        str(atom.get("icode", "")),
        str(atom.get("name", "")),
    )


def _pair_sort_key(pair: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        float(pair.get("distance", 0.0)),
        _atom_sort_key(pair["a"]),
        _atom_sort_key(pair["b"]),
    )


def percentile_position(
    feature_residues: Sequence[Any],
    residue_key: Any,
    *,
    digits: int = 3,
) -> float | None:
    """Fractional position (0..1) of a residue along an ordered feature.

    ``feature_residues`` is the feature's ordered residue list (as returned by
    :func:`lifesci.lib.structures.collect_atoms` under ``feature_residues``);
    each entry may be ``(seqid, icode)`` or a mapping with those keys.  Returns
    ``None`` for features with fewer than two residues or when the residue is
    not part of the feature.
    """
    normalised = []
    for entry in feature_residues:
        if isinstance(entry, Mapping):
            normalised.append((str(entry.get("seqid", "")), str(entry.get("icode") or "")))
        elif isinstance(entry, (tuple, list)) and entry:
            normalised.append((str(entry[0]), str(entry[1]) if len(entry) > 1 else ""))
        else:
            normalised.append((str(entry), ""))
    if len(normalised) < 2:
        return None
    if isinstance(residue_key, Mapping):
        key = (str(residue_key.get("seqid", "")), str(residue_key.get("icode") or ""))
    elif isinstance(residue_key, (tuple, list)) and residue_key:
        key = (str(residue_key[0]), str(residue_key[1]) if len(residue_key) > 1 else "")
    else:
        key = (str(residue_key), "")
    try:
        index = normalised.index(key)
    except ValueError:
        return None
    return round(index / (len(normalised) - 1), digits)


# ---------------------------------------------------------------------------
# pairwise minima
# ---------------------------------------------------------------------------


def _nearest_distances(
    points_a: np.ndarray, points_b: np.ndarray, *, kdtree: bool
) -> tuple[np.ndarray, np.ndarray]:
    """For every point in A: (distance, index) of the nearest point in B."""
    if points_a.shape[0] == 0 or points_b.shape[0] == 0:
        return np.zeros(0, dtype=float), np.zeros(0, dtype=int)
    if kdtree:
        from scipy.spatial import cKDTree

        tree = cKDTree(points_b)
        distances, indices = tree.query(points_a, k=1)
        return np.asarray(distances, dtype=float), np.asarray(indices, dtype=int)
    # brute-force fallback
    distances = np.empty(points_a.shape[0], dtype=float)
    indices = np.empty(points_a.shape[0], dtype=int)
    for index in range(points_a.shape[0]):
        delta = points_b - points_a[index]
        squared = (delta * delta).sum(axis=1)
        best = int(np.argmin(squared))
        indices[index] = best
        distances[index] = float(np.sqrt(squared[best]))
    return distances, indices


def min_distances(
    atoms_a: Sequence[Any],
    atoms_b: Sequence[Any],
    *,
    kdtree: bool = True,
    thresholds: Sequence[float] = DEFAULT_THRESHOLDS,
    limit: int = 10,
    residue_limit: int = 20,
) -> dict[str, Any]:
    """Minimum atom-atom distances between two atom sets.

    Parameters
    ----------
    atoms_a, atoms_b:
        Atom dicts from :func:`lifesci.lib.structures.collect_atoms` (or raw
        ``(x, y, z)`` points).
    kdtree:
        Use :class:`scipy.spatial.cKDTree` (default); ``False`` runs a
        brute-force reference implementation.
    thresholds:
        Distance thresholds (Å) for the per-atom contact counts.
    limit:
        Number of closest atom pairs to report (deterministic order).
    residue_limit:
        Number of closest distinct residue pairs to report.

    Returns
    -------
    dict
        ``{"min_distance", "pairs": [{"a", "b", "distance"}][:limit],
        "residue_pairs": [{"a", "b", "distance"}][:residue_limit],
        "n_under": {str(threshold): count}, "n_atoms_a", "n_atoms_b"}``.
        ``min_distance`` is ``None`` when either atom set is empty.
    """
    list_a = list(atoms_a)
    list_b = list(atoms_b)
    threshold_values = [float(threshold) for threshold in thresholds]
    n_under = {str(threshold): 0 for threshold in threshold_values}
    result: dict[str, Any] = {
        "min_distance": None,
        "pairs": [],
        "residue_pairs": [],
        "n_under": n_under,
        "n_atoms_a": len(list_a),
        "n_atoms_b": len(list_b),
    }
    if not list_a or not list_b:
        return result
    points_a = _coordinates(list_a)
    points_b = _coordinates(list_b)
    distances, indices = _nearest_distances(points_a, points_b, kdtree=kdtree)
    for threshold in threshold_values:
        n_under[str(threshold)] = int((distances < threshold).sum())
    order = sorted(
        range(len(list_a)),
        key=lambda index: (
            float(distances[index]),
            _atom_sort_key(_clean_atom(list_a[index])),
            _atom_sort_key(_clean_atom(list_b[int(indices[index])])),
        ),
    )
    pairs: list[dict[str, Any]] = []
    for index in order[: max(0, int(limit))]:
        atom_a = _clean_atom(list_a[index])
        atom_b = _clean_atom(list_b[int(indices[index])])
        pairs.append(
            {
                "a": atom_a,
                "b": atom_b,
                "distance": round(float(distances[index]), 3),
            }
        )
    residue_pairs: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    for index in order:
        atom_a = _clean_atom(list_a[index])
        atom_b = _clean_atom(list_b[int(indices[index])])
        residue_key = (
            atom_a["chain"],
            atom_a["seqid"],
            atom_a["icode"],
            atom_b["chain"],
            atom_b["seqid"],
            atom_b["icode"],
        )
        if residue_key in seen:
            continue
        seen.add(residue_key)
        residue_pairs.append(
            {
                "a": {
                    "chain": atom_a["chain"],
                    "seqid": atom_a["seqid"],
                    "icode": atom_a["icode"],
                    "resname": atom_a["resname"],
                },
                "b": {
                    "chain": atom_b["chain"],
                    "seqid": atom_b["seqid"],
                    "icode": atom_b["icode"],
                    "resname": atom_b["resname"],
                },
                "distance": round(float(distances[index]), 3),
            }
        )
        if len(residue_pairs) >= max(0, int(residue_limit)):
            break
    result["min_distance"] = round(float(distances.min()), 3)
    result["pairs"] = pairs
    result["residue_pairs"] = residue_pairs
    result["closest"] = pairs[0] if pairs else None
    return result


# ---------------------------------------------------------------------------
# feature resolution (explicit selectors or alignment columns)
# ---------------------------------------------------------------------------


def _colmap_records(colmap: Any) -> dict[str, Any]:
    """Normalise a colmap to the :mod:`superposition` record format."""
    if isinstance(colmap, (str, bytes)):
        from ..util import read_json

        loaded = read_json(colmap)
        if loaded is None:
            raise DataError(f"column map file not found or unreadable: {colmap}")
        return loaded
    return colmap


def residues_for_columns(
    colmap: Any,
    columns: Sequence[int],
    *,
    chain: str | None = None,
    colmap_chain: str | None = None,
) -> list[tuple[str, str]]:
    """Residues whose alignment column lies inside an inclusive column span.

    Parameters
    ----------
    colmap:
        Residue -> column map (path/JSON or any format accepted by
        :func:`lifesci.lib.superposition.normalise_colmap`).
    columns:
        ``[start_column, end_column]`` inclusive (or a list of such spans).
    chain:
        Optional auth chain id filter; the colmap format stores chains
        inside the map, so this restricts which chain's residues are returned.
    colmap_chain:
        Alias for filtering by the chain key stored *inside* the colmap.

    Returns
    -------
    list[(seqid, icode)]
        Ordered by alignment column (5'->3'), deduplicated.
    """
    from .superposition import normalise_colmap

    normalised = normalise_colmap(_colmap_records(colmap))
    if (
        columns
        and not isinstance(columns[0], (list, tuple, Mapping))
        and len(columns) >= 2
        and all(isinstance(value, (int, float)) for value in columns[:2])
    ):
        spans: list[tuple[int, int]] = [(int(columns[0]), int(columns[1]))]
    else:
        spans = []
        for span in columns:
            if isinstance(span, Mapping):
                spans.append((int(span["start"]), int(span["end"])))
            else:
                spans.append((int(span[0]), int(span[1])))
    wanted_chain = chain or colmap_chain
    entries: list[tuple[int, str, str]] = []
    for record in normalised["residues"]:
        if wanted_chain is not None and str(record["chain"]) != wanted_chain:
            continue
        column = int(record["column"])
        if any(start <= column <= stop for start, stop in spans):
            entries.append((column, str(record["seqid"]), str(record.get("icode") or "")))
    entries.sort()
    return list(dict.fromkeys((seqid, icode) for _column, seqid, icode in entries))


def _normalise_pair(pair: Any) -> tuple[str, str]:
    if isinstance(pair, str):
        for separator in ("-", ":", "|", ","):
            if separator in pair:
                first, second = pair.split(separator, 1)
                return first.strip(), second.strip()
        raise InvalidInputError(
            f"pair string {pair!r} must look like 'feature_a-feature_b'"
        )
    if isinstance(pair, Mapping):
        return str(pair.get("a")), str(pair.get("b"))
    if isinstance(pair, (tuple, list)) and len(pair) >= 2:
        return str(pair[0]), str(pair[1])
    raise InvalidInputError(f"unsupported feature pair: {pair!r}")


def _structure_features(
    structure_entry: Mapping[str, Any], global_features: Mapping[str, Any]
) -> dict[str, Any]:
    """Merge shared feature definitions with per-structure overrides."""
    features: dict[str, Any] = {
        str(name): dict(spec) if isinstance(spec, Mapping) else spec
        for name, spec in (global_features or {}).items()
    }
    overrides = structure_entry.get("features") or {}
    for name, spec in overrides.items():
        features[str(name)] = dict(spec) if isinstance(spec, Mapping) else spec
    return features


def _selection_for_feature(
    structure_entry: Mapping[str, Any],
    feature: str,
    spec: Mapping[str, Any],
) -> dict[str, Any]:
    """Turn a feature spec into a ``structures.collect_atoms`` selection."""
    selection: dict[str, Any] = {
        "name": feature,
        "chain": str(spec.get("chain", "")),
        "residues": spec.get("residues", "all"),
        "atoms": spec.get("atoms", ("P",)),
        "fallback_atoms": spec.get("fallback_atoms", ("C1'", "C1*")),
    }
    colmap = spec.get("colmap")
    if colmap is None:
        colmap = (structure_entry.get("colmaps") or {}).get(feature)
    if colmap is not None and spec.get("columns") is not None:
        chain_filter = str(spec.get("chain") or "") or None
        selection["residues"] = residues_for_columns(
            colmap,
            spec["columns"],
            chain=chain_filter,
            colmap_chain=spec.get("colmap_chain"),
        )
        if not selection["residues"]:
            raise DataError(
                f"no residues map into the requested columns for feature {feature!r}",
                details={"feature": feature, "columns": json_safe(spec["columns"])},
            )
    return selection


# ---------------------------------------------------------------------------
# full analysis
# ---------------------------------------------------------------------------


def contact_analysis(
    ctx: Any,
    structures: Sequence[Any],
    features: Mapping[str, Any],
    pairs: Sequence[Any],
    *,
    thresholds: Sequence[float] = DEFAULT_THRESHOLDS,
    top_pairs: int = 10,
    percentiles: bool = True,
    bridge_cutoff: float = DEFAULT_BRIDGE_CUTOFF,
    detect_bridges: bool = True,
) -> dict[str, Any]:
    """Contact analysis across one or more structures.

    Parameters
    ----------
    ctx:
        :class:`lifesci.context.Context` (warnings) or ``None``.
    structures:
        ``[{"id", "path", "features"?, "colmaps"?}, ...]`` (or plain paths).
        A structure's ``features`` override the shared ``features`` definitions
        for that structure (e.g. different chain ids per PDB entry); its
        ``colmaps`` map feature -> residue->column map for column-based
        features.
    features:
        Shared ``{feature: spec}`` where spec holds ``chain``, ``residues``
        (explicit selector) *or* ``columns`` [+ ``colmap``], ``atoms`` (default
        ``["P"]``) and ``fallback_atoms`` (default ``["C1'", "C1*"]``).
    pairs:
        Feature pairs to measure: ``[("feature_a","feature_b"), ...]`` /
        ``[{"a","b"}, ...]`` / ``["feature_a-feature_b", ...]``.
    thresholds, top_pairs, percentiles:
        Contact thresholds, closest-pair count, and whether to report the
        percentile position of the contacting residues along each feature.
    bridge_cutoff, detect_bridges:
        Third-feature bridge detection (``CONTACT_BRIDGE_THROUGH_THIRD_FEATURE``).

    Returns
    -------
    dict
        ``{"structures": [...], "pairs": [...], "bridges": [...],
        "thresholds": [...]}``; each pair record carries ``structure``,
        ``a``, ``b``, ``min_distance``, ``closest``, ``pairs``, ``n_under``,
        ``percentiles``, ``status`` and any per-feature missing status.
    """
    from pathlib import Path

    structure_entries: list[dict[str, Any]] = []
    for item in structures or []:
        if isinstance(item, Mapping):
            path = item.get("path")
            if not path:
                raise InvalidInputError(
                    "structure entries need a 'path'",
                    details={"structure": json_safe(dict(item))},
                )
            stem = Path(str(path)).name.split(".")[0]
            entry = dict(item)
            entry.setdefault("id", stem)
            structure_entries.append(entry)
        else:
            stem = Path(str(item)).name.split(".")[0]
            structure_entries.append({"id": stem, "path": str(item)})
    if not structure_entries:
        raise InvalidInputError("contact_analysis needs at least one structure")
    normalised_pairs = [_normalise_pair(pair) for pair in pairs or []]
    if not normalised_pairs:
        raise InvalidInputError("contact_analysis needs at least one feature pair")

    structure_results: list[dict[str, Any]] = []
    pair_results: list[dict[str, Any]] = []
    bridges: list[dict[str, Any]] = []
    for entry in structure_entries:
        structure_id = str(entry["id"])
        feature_specs = _structure_features(entry, features)
        selections: list[dict[str, Any]] = []
        spec_by_name: dict[str, dict[str, Any]] = {}
        for name, spec in feature_specs.items():
            if not isinstance(spec, Mapping):
                raise InvalidInputError(
                    f"feature {name!r} specification must be a mapping",
                    details={"feature": name},
                )
            selection = _selection_for_feature(entry, name, spec)
            selections.append(selection)
            spec_by_name[name] = dict(spec)
        atoms_by_feature: dict[str, list[dict[str, Any]]] = {
            name: [] for name in feature_specs
        }
        feature_status: dict[str, str] = {}
        feature_residues: dict[str, list[tuple[str, str]]] = {}
        all_missing: list[dict[str, Any]] = []
        n_atoms_total = 0
        for name in feature_specs:
            try:
                single = structures_lib.collect_atoms(
                    entry["path"],
                    selections=[_selection_for_feature(entry, name, spec_by_name[name])],
                )
            except DataError as err:
                feature_status[name] = "missing_chain"
                all_missing.append(
                    {
                        "feature": name,
                        "chain": str(spec_by_name[name].get("chain", "")),
                        "reason": "unreadable_structure",
                        "detail": str(err),
                    }
                )
                continue
            atoms_by_feature[name] = single["atoms"]
            n_atoms_total += len(single["atoms"])
            feature_status[name] = single["feature_status"].get(name, "missing_chain")
            feature_residues[name] = list(single.get("feature_residues", {}).get(name, []))
            all_missing.extend(single.get("missing") or [])
        for name, status in feature_status.items():
            if status != "ok" and ctx is not None and hasattr(ctx, "warn"):
                ctx.warn(
                    "FEATURE_MISSING",
                    (
                        f"Feature {name!r} is not modelled/representable in "
                        f"structure {structure_id} (status: {status}); contacts "
                        "involving it are not measurable and absence of contact is "
                        "not evidence of absence."
                    ),
                    context={"structure": structure_id, "feature": name, "status": status},
                )
        structure_results.append(
            {
                "id": structure_id,
                "path": str(entry["path"]),
                "feature_status": feature_status,
                "n_atoms": n_atoms_total,
                "missing": all_missing,
            }
        )

        for feature_a, feature_b in normalised_pairs:
            status_a = feature_status.get(feature_a)
            status_b = feature_status.get(feature_b)
            if feature_a not in feature_specs or feature_b not in feature_specs:
                raise InvalidInputError(
                    f"pair refers to unknown feature(s): {feature_a!r}, {feature_b!r}",
                    details={"features": sorted(feature_specs)},
                )
            record: dict[str, Any] = {
                "structure": structure_id,
                "a": feature_a,
                "b": feature_b,
                "status": "ok",
                "feature_status": {"a": status_a, "b": status_b},
                "min_distance": None,
                "closest": None,
                "pairs": [],
                "n_under": {str(float(t)): 0 for t in thresholds},
                "percentiles": None,
            }
            if status_a != "ok" or status_b != "ok":
                record["status"] = "missing_feature"
                pair_results.append(record)
                continue
            contact = min_distances(
                atoms_by_feature[feature_a],
                atoms_by_feature[feature_b],
                thresholds=thresholds,
                limit=top_pairs,
            )
            record.update(contact)
            if percentiles and contact.get("closest"):
                closest = contact["closest"]
                residue_a = closest["a"]
                residue_b = closest["b"]
                percentiles_a = percentile_position(
                    feature_residues.get(feature_a, []), residue_a
                )
                percentiles_b = percentile_position(
                    feature_residues.get(feature_b, []), residue_b
                )
                record["percentiles"] = {
                    "a": {
                        "residue": f"{residue_a['chain']}:{residue_a['seqid']}"
                        f"{residue_a['icode']}",
                        "percentile": percentiles_a,
                        "n_residues": len(feature_residues.get(feature_a, [])),
                    },
                    "b": {
                        "residue": f"{residue_b['chain']}:{residue_b['seqid']}"
                        f"{residue_b['icode']}",
                        "percentile": percentiles_b,
                        "n_residues": len(feature_residues.get(feature_b, [])),
                    },
                }
            if (
                contact["min_distance"] is not None
                and contact["min_distance"] > max(float(t) for t in thresholds)
                and ctx is not None
                and hasattr(ctx, "warn")
            ):
                ctx.warn(
                    "CONTACT_ABSENT_NOT_PROOF",
                    (
                        f"No {feature_a}-{feature_b} atom contact within "
                        f"{max(float(t) for t in thresholds):g} Å in {structure_id}; "
                        "report 'no contact detected at this atom selection' rather "
                        "than proven absence (missing atoms/features are common)."
                    ),
                    context={
                        "structure": structure_id,
                        "a": feature_a,
                        "b": feature_b,
                    },
                )
            pair_results.append(record)

            if detect_bridges:
                for third in feature_specs:
                    if third in (feature_a, feature_b):
                        continue
                    if feature_status.get(third) != "ok":
                        continue
                    contact_a = min_distances(
                        atoms_by_feature[third],
                        atoms_by_feature[feature_a],
                        thresholds=(),
                        limit=1,
                    )
                    contact_b = min_distances(
                        atoms_by_feature[third],
                        atoms_by_feature[feature_b],
                        thresholds=(),
                        limit=1,
                    )
                    distance_a = contact_a["min_distance"]
                    distance_b = contact_b["min_distance"]
                    if distance_a is None or distance_b is None:
                        continue
                    if distance_a <= bridge_cutoff and distance_b <= bridge_cutoff:
                        bridge = {
                            "structure": structure_id,
                            "pair": [feature_a, feature_b],
                            "via": third,
                            "distance_to_a": distance_a,
                            "distance_to_b": distance_b,
                            "cutoff": float(bridge_cutoff),
                        }
                        bridges.append(bridge)
                        if ctx is not None and hasattr(ctx, "warn"):
                            ctx.warn(
                                "CONTACT_BRIDGE_THROUGH_THIRD_FEATURE",
                                (
                                    f"{third} sits within {bridge_cutoff:g} Å of both "
                                    f"{feature_a} ({distance_a:g} Å) and {feature_b} "
                                    f"({distance_b:g} Å) in {structure_id}; the "
                                    "pairwise minimum does not prove a direct contact."
                                ),
                                context={
                                    "structure": structure_id,
                                    "pair": [feature_a, feature_b],
                                    "via": third,
                                },
                            )
    if ctx is not None and hasattr(ctx, "warn"):
        used_atoms = sorted(
            {
                ",".join(
                    [str(name) for name in atoms]
                    if isinstance(atoms, (list, tuple))
                    else [str(atoms)]
                )
                for spec in (features or {}).values()
                if isinstance(spec, Mapping)
                for atoms in [spec.get("atoms") or ["P"]]
            }
        )
        if used_atoms:
            ctx.warn(
                "DISTANCE_ATOM_SELECTION",
                (
                    "Contact distances use the configured atom selection "
                    f"({', '.join(used_atoms) or 'P'}, with fallback atoms); "
                    "distances between different atom selections are not "
                    "comparable."
                ),
                severity="info",
                context={"atom_selections": used_atoms},
            )
    bridges.sort(
        key=lambda bridge: (
            bridge["structure"],
            bridge["pair"][0],
            bridge["pair"][1],
            bridge["via"],
        )
    )
    return {
        "structures": structure_results,
        "pairs": pair_results,
        "bridges": bridges,
        "thresholds": [float(threshold) for threshold in thresholds],
        "parameters": {
            "bridge_cutoff": float(bridge_cutoff),
            "top_pairs": int(top_pairs),
            "percentiles": bool(percentiles),
            "detect_bridges": bool(detect_bridges),
        },
    }
