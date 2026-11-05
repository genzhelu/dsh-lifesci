"""RCSB PDB / PDBe structure and EMDB map adapter.

Endpoints::

    https://files.rcsb.org/download/<PDB>.cif|.pdb    experimental structures
    https://ftp.ebi.ac.uk/pub/databases/emdb/structures/EMD-<id>/map/emd_<id>.map.gz

Parsing is delegated to :mod:`lifesci.lib.structures` (gemmi).  Documented
quirks surfaced by :func:`structure_entities`:

* some protein/rRNA chains are unmodelled in a deposited structure — the chain
  list is what the model actually contains, not what the entity table lists;
* gemmi's ``Model.__contains__`` tests chains, not chain *names*, so callers must
  iterate ``[chain.name for chain in model]``;
* gemmi has no ``Structure.write`` — use ``st.make_mmcif_document().write_file``
  (handled inside :mod:`lifesci.lib.structures`).

EMDB maps are large; they are written to the caller's ``out_dir`` and recorded in
provenance with their SHA-256.  Nothing is written outside ``out_dir``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .. import http, util
from ..errors import InvalidInputError, LifesciError, NotFoundError, PortalError
from . import _common

RCSB_FILES = "https://files.rcsb.org/download"
PDBE_FILES = "https://www.ebi.ac.uk/pdbe/entry-files/download"
EMDB_FTP = "https://ftp.ebi.ac.uk/pub/databases/emdb/structures"
DATABASE = "rcsb_pdb"

_STRUCTURE_FORMATS = {"cif": "cif", "mmcif": "cif", "pdb": "pdb"}


def _normalise_pdb_id(value: Any) -> str:
    pdb_id = str(value or "").strip().upper()
    if not pdb_id:
        raise InvalidInputError("PDB id is required")
    if len(pdb_id) != 4 or not pdb_id.isalnum():
        raise InvalidInputError(f"invalid PDB id {pdb_id!r}: expected 4 alphanumeric characters")
    return pdb_id


def _normalise_emdb_id(value: Any) -> tuple[str, str]:
    text = str(value or "").strip().upper()
    digits = text.replace("EMD-", "").replace("EMD_", "").replace("EMDB-", "").strip()
    if not digits.isdigit():
        raise InvalidInputError(f"invalid EMDB id {text!r}: expected EMD-<digits>")
    # Live-verified: EBI EMDB FTP uses the accession digits unpadded, e.g.
    # EMD-1000 -> .../EMD-1000/map/emd_1000.map.gz (padded emd_01000 is a 404).
    number = int(digits)
    return f"EMD-{number}", f"emd_{number}"


def _write_output(ctx: Any, data: bytes, out_dir: Path, filename: str, *, database: str, release: str | None) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / filename
    util.atomic_write_bytes(target, data)
    _common.log_local_file(ctx, target, dataset=database, notes={"release": release})
    return target


def download_structure(
    ctx: Any,
    pdb_id: str,
    *,
    out_dir: str | Path,
    fmt: str = "cif",
) -> dict[str, Any]:
    """Download one experimental structure file from RCSB (mmCIF or PDB)."""
    pdb_id = _normalise_pdb_id(pdb_id)
    fmt = str(fmt).lower()
    if fmt not in _STRUCTURE_FORMATS:
        raise InvalidInputError(f"unsupported structure format {fmt!r}; use 'cif' or 'pdb'")
    extension = _STRUCTURE_FORMATS[fmt]
    url = f"{RCSB_FILES}/{pdb_id}.{extension}"
    try:
        body, response = http.get_bytes(
            ctx,
            url,
            error_code="RCSB",
            database=DATABASE,
            headers={"accept": "chemical/x-mmcif, chemical/x-pdb, */*"},
        )
    except PortalError as err:
        if _common.is_http_status(err, *_common.NOT_FOUND_STATUSES):
            raise NotFoundError(
                f"RCSB PDB has no file for {pdb_id} ({extension})",
                details={"pdb_id": pdb_id, "url": url},
                fallbacks=["PDBe entry files (https://www.ebi.ac.uk/pdbe/entry-files/)", "check the PDB id"],
            ) from err
        raise
    if not body:
        raise NotFoundError(f"RCSB returned an empty file for {pdb_id}", details={"url": url})
    out_root = Path(out_dir)
    target = _write_output(ctx, body, out_root, f"{pdb_id}.{extension}", database=DATABASE, release=None)
    return {
        "pdb_id": pdb_id,
        "format": extension,
        "path": str(target),
        "url": url,
        "sha256": util.sha256_bytes(body),
        "bytes": len(body),
        "final_url": response.final_url,
        "database": DATABASE,
    }


def download_emdb_map(ctx: Any, emdb_id: str, *, out_dir: str | Path) -> dict[str, Any]:
    """Download an EMDB map (gzipped MRC), written verbatim to ``out_dir``."""
    canonical, stem = _normalise_emdb_id(emdb_id)
    url = f"{EMDB_FTP}/{canonical}/map/{stem}.map.gz"
    try:
        body, response = http.get_bytes(
            ctx,
            url,
            error_code="EMDB",
            database="emdb",
            headers={"accept": "application/gzip, */*"},
        )
    except PortalError as err:
        if _common.is_http_status(err, *_common.NOT_FOUND_STATUSES):
            raise NotFoundError(
                f"EMDB has no map for {canonical}",
                details={"emdb_id": canonical, "url": url},
                fallbacks=["check the EMDB accession", "https://www.ebi.ac.uk/emdb/"],
            ) from err
        raise
    out_root = Path(out_dir)
    target = _write_output(ctx, body, out_root, f"{stem}.map.gz", database="emdb", release=None)
    size_mb = len(body) / (1024 * 1024)
    if size_mb > 200:
        ctx.warn(
            "EMDB_MAP_LARGE",
            f"EMDB map is {size_mb:.0f} MB; downstream processing may need substantial memory.",
            context={"emdb_id": canonical, "bytes": len(body)},
        )
    return {
        "emdb_id": canonical,
        "path": str(target),
        "url": url,
        "sha256": util.sha256_bytes(body),
        "bytes": len(body),
        "gzipped": util.is_gzip_bytes(body),
        "database": "emdb",
        "note": "MRC map is stored gzipped; decompress before parsing with gemmi.",
    }


def structure_entities(ctx: Any, path: str | Path) -> dict[str, Any]:
    """Parse a local mmCIF/PDB file and report chains, entities and residue counts.

    Uses :mod:`lifesci.lib.structures` so the entity-description and gemmi quirks
    are handled in one place.
    """
    from ..lib import structures

    target = Path(path)
    if not target.is_file():
        raise NotFoundError(f"structure file not found: {target}", details={"path": str(target)})
    _common.log_local_file(ctx, target, dataset="structure")
    payload = structures.read_structure(target)
    chains = payload.get("chains") or []
    return {
        "path": str(target),
        "format": payload.get("format"),
        "n_models": payload.get("n_models", 1),
        "n_chains": len(chains),
        "chains": [
            {
                "chain": chain.get("chain"),
                "kind": chain.get("kind"),
                "entity_name": chain.get("entity_name"),
                "n_residues": chain.get("n_residues"),
            }
            for chain in chains
        ],
        "source": "RCSB PDB / PDBe mmCIF",
    }
