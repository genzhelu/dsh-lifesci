"""Structure parsing (gemmi), residue selectors, atom collection and mmCIF IO.

The functions wrap the documented gemmi quirks that callers rely on
hit:

* ``gemmi.Model.__contains__`` tests **chain objects**, not names — always test
  membership with ``[c.name for c in model]`` (helpers here do that);
* :class:`gemmi.Structure` has no ``write``/``write_mmcif`` method — writing to
  mmCIF is ``st.make_mmcif_document().write_file(path)``;
* entity descriptions are not carried on :class:`gemmi.Entity`; they are read
  from the raw mmCIF categories ``_entity.pdbx_description``,
  ``_struct_asym.entity_id`` and ``_atom_site.auth_asym_id`` (and re-injected
  when writing a trimmed subset);
* gzipped mmCIF files are read transparently by :func:`gemmi.read_structure`.

All residue identifiers are returned as **strings** (``seqid``) plus the
insertion-code string (``icode``, empty when none).  Atom coordinates are kept
at full precision inside the library and rounded only when a caller renders
them (``contacts`` rounds to 3 decimals at output time).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from ..errors import DataError, InvalidInputError
from ..util import (
    atomic_write_bytes,
    gzip_compress,
    json_safe,
    sha256_bytes,
    sha256_file,
)

_STRUCTURE_SUFFIXES = (".cif", ".mmcif", ".pdb", ".ent")
_RESIDUE_TOKEN_RE = re.compile(r"^(\d+)([A-Za-z]*)$")
_RANGE_TOKEN_RE = re.compile(r"^(\d+)([A-Za-z]*)-(\d+)([A-Za-z]*)$")
_PROTEIN_KINDS = {"AA"}
_RNA_KINDS = {"RNA", "DNA"}


# ---------------------------------------------------------------------------
# low-level gemmi helpers
# ---------------------------------------------------------------------------


def _detect_format(path: str | Path) -> str:
    name = str(path).lower()
    for suffix in (".gz", ".bz2", ".zst"):
        if name.endswith(suffix):
            name = name[: -len(suffix)]
            break
    if name.endswith((".cif", ".mmcif")):
        return "mmcif"
    if name.endswith((".pdb", ".ent")):
        return "pdb"
    return "unknown"


def _read_raw_block(path: str | Path) -> Any:
    """Read the raw mmCIF block (``None`` for non-mmCIF or unreadable files)."""
    try:
        return gemmi_cif_read(path)
    except Exception:  # pragma: no cover - defensive: never fail parsing on this
        return None


def gemmi_cif_read(path: str | Path) -> Any:
    """Read the first mmCIF block of ``path`` (gzip transparently handled)."""
    import gemmi

    return gemmi.cif.read(str(path))[0]


def _entity_descriptions(block: Any) -> dict[str, str]:
    """auth chain -> entity description, from raw mmCIF categories."""
    if block is None:
        return {}
    descriptions: dict[str, str] = {}
    try:
        entity = block.get_mmcif_category("_entity.")
        if "pdbx_description" not in entity:
            return descriptions
        entity_description = {
            entity["id"][index]: entity["pdbx_description"][index]
            for index in range(len(entity["id"]))
        }
        asym = block.get_mmcif_category("_struct_asym.")
        label_to_entity = {
            asym["id"][index]: asym["entity_id"][index]
            for index in range(len(asym["id"]))
        }
        atom_site = block.get_mmcif_category("_atom_site.")
        for auth_chain, label_chain in zip(
            atom_site.get("auth_asym_id", []), atom_site.get("label_asym_id", [])
        ):
            if auth_chain not in descriptions:
                descriptions[auth_chain] = entity_description.get(
                    label_to_entity.get(label_chain, ""), ""
                )
    except Exception:  # pragma: no cover - malformed headers are non-fatal
        return descriptions
    return descriptions


def _load_structure(path: str | Path) -> tuple[Any, dict[str, str], str]:
    """Return ``(gemmi.Structure, entity_descriptions, format)``.

    Raises :class:`DataError` when gemmi cannot parse the file.
    """
    import gemmi

    target = Path(path)
    if not target.is_file():
        raise DataError(
            f"structure file not found: {target}",
            details={"path": str(target)},
        )
    try:
        structure = gemmi.read_structure(str(target))
    except Exception as err:  # gemmi raises RuntimeError/ValueError
        raise DataError(
            f"gemmi failed to parse structure {target}: {err}",
            details={"path": str(target), "format": _detect_format(target)},
        ) from err
    structure.setup_entities()
    fmt = _detect_format(target)
    descriptions = _entity_descriptions(_read_raw_block(target)) if fmt == "mmcif" else {}
    return structure, descriptions, fmt


def _residue_key(residue: Any) -> tuple[str, str]:
    """``(seqid, icode)`` strings for a gemmi residue."""
    icode = residue.seqid.icode.strip()
    return str(residue.seqid.num), icode


def _one_letter(residue: Any) -> str:
    import gemmi

    try:
        info = gemmi.find_tabulated_residue(residue.name)
        letter = (info.one_letter_code or "N").upper()
        return letter if letter else "N"
    except Exception:  # pragma: no cover - unknown residues
        return "N"


def _residue_kind(residue: Any) -> str:
    """Classify one residue: ``AA``, ``RNA``, ``DNA`` or the raw kind name."""
    import gemmi

    try:
        info = gemmi.find_tabulated_residue(residue.name)
        return info.kind.name
    except Exception:  # pragma: no cover
        return "UNKNOWN"


def _chain_kind(kinds: set[str]) -> str:
    if kinds & _PROTEIN_KINDS:
        return "protein"
    if kinds & _RNA_KINDS:
        return "rrna"
    return "other"


def _chain_info(chain: Any, entity_name: str = "") -> dict[str, Any] | None:
    """Build the JSON-friendly chaininfo record (``None`` for non-polymer chains)."""
    polymer = chain.get_polymer()
    if polymer is None or polymer.length() == 0:
        return None
    residues: list[dict[str, Any]] = []
    sequence: list[str] = []
    kinds: set[str] = set()
    for index, residue in enumerate(polymer):
        seqid, icode = _residue_key(residue)
        residues.append(
            {
                "index": index,
                "seqid": seqid,
                "icode": icode,
                "name": residue.name,
            }
        )
        sequence.append(_one_letter(residue))
        kinds.add(_residue_kind(residue))
    return {
        "chain": chain.name,
        "kind": _chain_kind(kinds),
        "entity_name": entity_name or "",
        "n_residues": len(residues),
        "residues": residues,
        "sequence": "".join(sequence),
    }


# ---------------------------------------------------------------------------
# public: structure summary
# ---------------------------------------------------------------------------


def read_structure(path: str | Path) -> dict[str, Any]:
    """Read a structure (mmCIF/PDB, optionally gzipped) into a JSON summary.

    Returns
    -------
    dict
        ``{"path", "format", "n_models", "chains": [chaininfo], "entities",
        "source", "pdb_id"}`` where ``chaininfo`` is
        ``{"chain", "kind": "rrna"|"protein"|"other", "entity_name",
        "n_residues", "residues": [{"index","seqid","icode","name"}],
        "sequence"}``.  ``entities`` maps auth chain id -> entity description;
        only polymer chains are reported (waters/ions/ligands are skipped).
    """
    structure, descriptions, fmt = _load_structure(path)
    model = structure[0]
    chains: list[dict[str, Any]] = []
    for chain in model:
        info = _chain_info(chain, descriptions.get(chain.name, ""))
        if info is not None:
            chains.append(info)
    stem = Path(path).name.split(".")[0]
    pdb_id = stem.upper() if re.fullmatch(r"[0-9][A-Za-z0-9]{3}", stem) else None
    return {
        "path": str(path),
        "format": fmt,
        "n_models": len(structure),
        "chains": chains,
        "entities": dict(descriptions),
        "source": str(Path(path).resolve()),
        "pdb_id": pdb_id,
    }


def find_chain(summary: Mapping[str, Any], chain: str) -> dict[str, Any] | None:
    """Return the chaininfo with name ``chain`` from a :func:`read_structure` dict."""
    for info in summary.get("chains") or []:
        if info.get("chain") == chain:
            return dict(info)
    return None


# ---------------------------------------------------------------------------
# residue selectors
# ---------------------------------------------------------------------------


def _selector_records(obj: Any) -> list[Mapping[str, Any]]:
    """Normalise a chaininfo / residue-list argument to residue records."""
    if obj is None:
        return []
    if isinstance(obj, Mapping):
        records = obj.get("residues") or []
    else:
        records = list(obj)
    normalised: list[Mapping[str, Any]] = []
    for record in records:
        if isinstance(record, Mapping):
            normalised.append(record)
        elif isinstance(record, (tuple, list)) and record:
            seqid = record[0]
            icode = record[1] if len(record) > 1 else ""
            normalised.append({"seqid": seqid, "icode": icode})
        else:
            normalised.append({"seqid": record, "icode": ""})
    return normalised


def _parse_residue_token(token: str) -> tuple[str, str] | None:
    match = _RESIDUE_TOKEN_RE.match(token.strip())
    if not match:
        return None
    return match.group(1), match.group(2)


def _expand_selector(
    selector: Any,
) -> tuple[list[tuple[str, str]] | None, bool]:
    """Expand a selector to an ordered list of requested residue ids.

    Returns ``(requested, is_explicit)``; ``requested`` is ``None`` for the
    ``"all"`` selector (which is defined by the available residues).
    """
    if selector is None:
        return [], True
    if isinstance(selector, str):
        text = selector.strip()
        if text.lower() == "all" or text == "*":
            return None, False
        requested: list[tuple[str, str]] = []
        for token in text.split(","):
            token = token.strip()
            if not token:
                continue
            range_match = _RANGE_TOKEN_RE.match(token)
            if range_match:
                start, start_icode, stop, stop_icode = range_match.groups()
                first, last = int(start), int(stop)
                step = 1 if last >= first else -1
                for number in range(first, last + step, step):
                    icode = start_icode if number == first else (stop_icode if number == last else "")
                    requested.append((str(number), icode))
                continue
            parsed = _parse_residue_token(token)
            if parsed is not None:
                requested.append(parsed)
        return requested, True
    if isinstance(selector, Mapping):
        values = selector.get("seqids")
        if values is None:
            values = selector.get("residues")
        if values is None:
            return [], True
        requested, _ = _expand_selector(values)
        return requested, True
    if isinstance(selector, (list, tuple)):
        requested = []
        for item in selector:
            if isinstance(item, Mapping):
                requested.append((str(item.get("seqid", "")), str(item.get("icode") or "")))
            elif isinstance(item, (tuple, list)) and item:
                seqid = item[0]
                icode = item[1] if len(item) > 1 else ""
                requested.append((str(seqid), str(icode or "")))
            else:
                text = str(item).strip()
                parsed = _parse_residue_token(text)
                requested.append(parsed if parsed is not None else (text, ""))
        return requested, True
    if isinstance(selector, int):
        return [(str(selector), "")], True
    raise InvalidInputError(
        f"unsupported residue selector: {selector!r}",
        details={"selector": json_safe(selector)},
    )


def select_residues(
    chaininfo_or_residues: Mapping[str, Any] | Sequence[Any],
    selector: Any,
) -> list[tuple[str, str]]:
    """Select residues from a chaininfo record (or residue list) in order.

    Supported selector forms (all deterministic; ranges ascending, explicit
    order preserved, duplicates removed)::

        "all"              every residue of the chain, in polymer order
        "3271-3272"        inclusive seqid range
        "616,617"          explicit seqids (comma separated)
        "5-10,20"          mix of ranges and single residues
        {"seqids": [...]}  explicit ids: int, "616", ("616",""), {"seqid","icode"}
        [{"seqid","icode"}, ...]  an already-resolved residue list

    Returns ``[(seqid_string, icode_string), ...]`` containing only residues
    that are actually modelled in the chain.
    """
    records = _selector_records(chaininfo_or_residues)
    available = [
        (str(record.get("seqid", "")), str(record.get("icode") or ""))
        for record in records
    ]
    if not available:
        return []
    requested, _ = _expand_selector(selector)
    if requested is None:
        return list(dict.fromkeys(available))
    allowed = set(available)
    seen: set[tuple[str, str]] = set()
    selected: list[tuple[str, str]] = []
    for key in requested:
        if key in allowed and key not in seen:
            seen.add(key)
            selected.append(key)
    return selected


def _requested_residues(
    chaininfo_or_residues: Mapping[str, Any] | Sequence[Any], selector: Any
) -> list[tuple[str, str]] | None:
    """Explicit requested ids for missing-residue reporting (``None`` for all)."""
    requested, is_explicit = _expand_selector(selector)
    if not is_explicit or requested is None:
        return None
    return requested


# ---------------------------------------------------------------------------
# atom collection
# ---------------------------------------------------------------------------


def _atom_names(selection: Mapping[str, Any]) -> tuple[list[str], list[str]]:
    atoms = selection.get("atoms", ("P",))
    fallback = selection.get("fallback_atoms", ("C1'", "C1*"))
    if isinstance(atoms, str):
        atoms = [atoms]
    if isinstance(fallback, str):
        fallback = [fallback]
    return [str(name) for name in atoms or []], [str(name) for name in (fallback or [])]


def _collect_residue_atoms(
    residue: Any,
    atom_names: Sequence[str],
    fallback_names: Sequence[str],
) -> list[dict[str, Any]]:
    """Return atom records for one residue, applying primary/fallback names."""
    selected = []
    names = list(atom_names)
    if any(name == "*" for name in names):
        selected = [atom for atom in residue]
    else:
        for name in names:
            atom = residue.find_atom(name, "*")
            if atom is not None:
                selected.append(atom)
    if not selected:
        for name in fallback_names:
            atom = residue.find_atom(name, "*")
            if atom is not None:
                selected.append(atom)
    return selected


def _atom_record(
    chain: str, residue: Any, atom: Any
) -> dict[str, Any]:
    seqid, icode = _residue_key(residue)
    return {
        "chain": chain,
        "seqid": seqid,
        "icode": icode,
        "resname": residue.name,
        "name": atom.name,
        "x": float(atom.pos.x),
        "y": float(atom.pos.y),
        "z": float(atom.pos.z),
    }


def collect_atoms(
    path: str | Path,
    *,
    selections: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Collect selected atoms from a structure for contact analysis.

    Each selection is a mapping::

        {"name"?: str,            # stable feature key; default "<chain>:<selector>"
         "chain": str,            # auth chain id
         "residues": selector,    # "all" | "3271-3272" | "616,617" | "5-10,20"
                                  # | {"seqids":[...]} | [(seqid, icode), ...]
         "atoms": ["P"],          # primary atom names ("*" = all atoms)
         "fallback_atoms": ["C1'", "C1*"]}

    Returns
    -------
    dict
        ``{"atoms": [{"chain","seqid","icode","resname","name","x","y","z"}],
        "n_atoms", "missing": [{"feature","chain",...,"reason"}],
        "feature_status": {feature: "ok"|"missing_chain"|"missing_residues"|
        "no_atoms"}, "feature_residues": {feature: [(seqid, icode), ...]}}``.

    ``atoms`` preserves selection order (selector order; for alignment-column
    features pass residues ordered by column, or use ``structures.atom_xyz``
    helpers) so callers can compute positions along a feature.
    """
    structure, _descriptions, _fmt = _load_structure(path)
    model = structure[0]
    chain_by_name = {chain.name: chain for chain in model}
    chaininfo_by_name: dict[str, dict[str, Any]] = {}
    residues_by_chain: dict[str, dict[tuple[str, str], Any]] = {}
    for chain in model:
        info = _chain_info(chain)
        if info is None:
            continue
        chaininfo_by_name[chain.name] = info
        residues_by_chain[chain.name] = {
            (str(record["seqid"]), str(record["icode"])): residue
            for record, residue in zip(
                info["residues"], chain.get_polymer()
            )
        }

    atoms: list[dict[str, Any]] = []
    missing: list[dict[str, Any]] = []
    feature_status: dict[str, str] = {}
    feature_residues: dict[str, list[tuple[str, str]]] = {}
    for selection in selections:
        chain = str(selection.get("chain") or "")
        selector = selection.get("residues", "all")
        feature = str(
            selection.get("name")
            or f"{chain}:{_selector_label(selector)}"
        )
        atom_names, fallback_names = _atom_names(selection)
        if chain not in chain_by_name:
            feature_status[feature] = "missing_chain"
            missing.append(
                {"feature": feature, "chain": chain, "reason": "missing_chain"}
            )
            continue
        chaininfo = chaininfo_by_name.get(chain) or {"residues": []}
        selected = select_residues(chaininfo, selector)
        if not selected:
            feature_status[feature] = "missing_residues"
            missing.append(
                {
                    "feature": feature,
                    "chain": chain,
                    "reason": "missing_residues",
                    "selector": json_safe(selector),
                }
            )
            continue
        feature_residues[feature] = selected
        residue_lookup = residues_by_chain[chain]
        feature_atoms: list[dict[str, Any]] = []
        for seqid, icode in selected:
            residue = residue_lookup.get((seqid, icode))
            if residue is None:  # pragma: no cover - indices are consistent
                missing.append(
                    {
                        "feature": feature,
                        "chain": chain,
                        "seqid": seqid,
                        "icode": icode,
                        "reason": "residue_missing",
                    }
                )
                continue
            selected_atoms = _collect_residue_atoms(residue, atom_names, fallback_names)
            if not selected_atoms:
                missing.append(
                    {
                        "feature": feature,
                        "chain": chain,
                        "seqid": seqid,
                        "icode": icode,
                        "reason": "atom_missing",
                        "atoms": atom_names + fallback_names,
                    }
                )
                continue
            for atom in selected_atoms:
                feature_atoms.append(_atom_record(chain, residue, atom))
        requested = _requested_residues(chaininfo, selector)
        if requested is not None:
            found = set(selected)
            for seqid, icode in requested:
                if (seqid, icode) not in found:
                    missing.append(
                        {
                            "feature": feature,
                            "chain": chain,
                            "seqid": seqid,
                            "icode": icode,
                            "reason": "residue_missing",
                        }
                    )
        atoms.extend(feature_atoms)
        feature_status[feature] = "ok" if feature_atoms else "no_atoms"
    return {
        "atoms": atoms,
        "n_atoms": len(atoms),
        "missing": missing,
        "feature_status": feature_status,
        "feature_residues": feature_residues,
    }


def _selector_label(selector: Any) -> str:
    if selector is None:
        return "all"
    if isinstance(selector, str):
        return selector
    if isinstance(selector, Mapping):
        values = selector.get("seqids") or selector.get("residues") or []
        return _selector_label(values) if isinstance(values, (list, tuple)) else str(values)
    if isinstance(selector, (list, tuple)):
        parts: list[str] = []
        for item in selector:
            if isinstance(item, Mapping):
                parts.append(str(item.get("seqid", "")))
            elif isinstance(item, (tuple, list)) and item:
                parts.append(str(item[0]))
            else:
                parts.append(str(item))
        return ",".join(parts)
    return str(selector)


def atom_coordinates(atoms: Sequence[Mapping[str, Any]]) -> list[list[float]]:
    """Extract ``[[x, y, z], ...]`` from atom dicts (order preserved)."""
    return [
        [float(atom["x"]), float(atom["y"]), float(atom["z"])]
        for atom in atoms
    ]


# ---------------------------------------------------------------------------
# mmCIF writing / subset extraction
# ---------------------------------------------------------------------------


def write_mmcif(structure_or_path: Any, out_path: str | Path) -> dict[str, Any]:
    """Write a structure to mmCIF.

    Accepts an already-parsed :class:`gemmi.Structure` or a path.  gemmi has no
    ``Structure.write``: this uses ``st.make_mmcif_document().write_file(path)``
    after ``setup_entities()``.  When ``out_path`` ends in ``.gz`` the mmCIF
    text is gzipped with ``mtime=0`` for reproducible bytes.

    Returns ``{"path", "sha256", "bytes"}``.
    """
    import gemmi

    if isinstance(structure_or_path, gemmi.Structure):
        structure = structure_or_path
    else:
        structure, _descriptions, _fmt = _load_structure(structure_or_path)
    structure.setup_entities()
    document = structure.make_mmcif_document()
    target = Path(out_path)
    if str(target).lower().endswith(".gz"):
        text = document.as_string()
        data = gzip_compress(text.encode("utf-8"), mtime=0)
        atomic_write_bytes(target, data)
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        document.write_file(str(target))
    return {
        "path": str(target),
        "sha256": sha256_file(target),
        "bytes": target.stat().st_size,
    }


def _residue_in_windows(seqid: str, windows: Sequence[Any] | None) -> bool:
    if not windows:
        return True
    try:
        number = int(seqid)
    except (TypeError, ValueError):
        return True
    for window in windows:
        start, stop = int(window[0]), int(window[1])
        if start <= number <= stop:
            return True
    return False


def _trimmed_structure(
    structure: Any,
    keep_chains: Sequence[str],
    residue_windows: Mapping[str, Sequence[Any]] | None,
) -> Any:
    """Build a new Structure containing only the requested chains/residues."""
    import gemmi

    trimmed = gemmi.Structure()
    trimmed.name = structure.name or "trimmed"
    trimmed.spacegroup_hm = structure.spacegroup_hm or "P 1"
    trimmed.cell = structure.cell
    keep = set(keep_chains)
    for model in structure:
        new_model = gemmi.Model(str(model.num) if model.num else "1")
        for chain in model:
            if chain.name not in keep:
                continue
            polymer = chain.get_polymer()
            if polymer is None or polymer.length() == 0:
                continue
            new_chain = gemmi.Chain(chain.name)
            windows = (residue_windows or {}).get(chain.name)
            for residue in polymer:
                seqid, _icode = _residue_key(residue)
                if _residue_in_windows(seqid, windows):
                    new_chain.add_residue(residue)
            if new_chain.get_polymer().length() > 0:
                new_model.add_chain(new_chain)
        trimmed.add_model(new_model)
    trimmed.setup_entities()
    return trimmed


def extract_substructure(
    path: str | Path,
    out_path: str | Path,
    *,
    keep_chains: Sequence[str],
    residue_windows: Mapping[str, Sequence[Sequence[int]]] | None = None,
) -> dict[str, Any]:
    """Write a trimmed structure (subset of chains/residue ranges) to mmCIF.

    Used by the structure-preparation script to keep small, trimmed structures
    that preserve residue numbering and entity/chain descriptions.  Atom order
    is preserved; entity descriptions (``_entity.pdbx_description``) are copied
    from the source mmCIF headers.

    ``residue_windows`` maps chain -> ``[(start, stop), ...]`` inclusive seqid
    ranges; chains absent from the mapping keep all residues.  ``out_path`` may
    end in ``.gz`` for reproducible gzipped output.

    Returns ``{"path", "sha256", "bytes", "chains", "n_residues", "format"}``.
    """
    structure, descriptions, _fmt = _load_structure(path)
    block = _read_raw_block(path)
    # auth chain -> entity id in the source file
    source_entity: dict[str, str] = {}
    if block is not None:
        try:
            asym = block.get_mmcif_category("_struct_asym.")
            label_to_entity = {
                asym["id"][index]: asym["entity_id"][index]
                for index in range(len(asym["id"]))
            }
            atom_site = block.get_mmcif_category("_atom_site.")
            for auth_chain, label_chain in zip(
                atom_site.get("auth_asym_id", []),
                atom_site.get("label_asym_id", []),
            ):
                source_entity.setdefault(
                    auth_chain, label_to_entity.get(label_chain, "")
                )
        except Exception:  # pragma: no cover
            source_entity = {}
    entity = block.get_mmcif_category("_entity.") if block is not None else {}
    entity_description = {}
    if "pdbx_description" in entity:
        entity_description = {
            entity["id"][index]: entity["pdbx_description"][index]
            for index in range(len(entity["id"]))
        }

    trimmed = _trimmed_structure(structure, keep_chains, residue_windows)
    # new structure's entity id -> source description
    description_by_entity_id: dict[str, str] = {}
    for chain in trimmed[0]:
        entity_obj = trimmed.get_entity_of(chain.get_polymer())
        source_id = source_entity.get(chain.name)
        if entity_obj is not None and source_id in entity_description:
            description_by_entity_id[entity_obj.name] = entity_description[source_id]
    document = trimmed.make_mmcif_document()
    output_block = document[0]
    entity_category = output_block.get_mmcif_category("_entity.")
    updated_category = dict(entity_category)
    updated_category["pdbx_description"] = [
        description_by_entity_id.get(entity_id, "")
        for entity_id in entity_category.get("id", [])
    ]
    output_block.set_mmcif_category("_entity.", updated_category)

    target = Path(out_path)
    if str(target).lower().endswith(".gz"):
        data = gzip_compress(output_block.as_string().encode("utf-8"), mtime=0)
        atomic_write_bytes(target, data)
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        output_block.write_file(str(target))
    counts = {
        chain.name: chain.get_polymer().length() for chain in trimmed[0]
    }
    return {
        "path": str(target),
        "sha256": sha256_file(target),
        "bytes": target.stat().st_size,
        "chains": [chain.name for chain in trimmed[0]],
        "n_residues": counts,
        "format": "mmcif",
    }
