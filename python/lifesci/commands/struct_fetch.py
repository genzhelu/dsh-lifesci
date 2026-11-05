"""``lifesci.struct.fetch`` — structure/model retrieval with explicit-failure semantics.

Downloads protein structure models and experimental structures through the
adapters, records a SHA-256 for every written file, and — crucially — never
papers over an unavailable model:

* AlphaFold DB prediction metadata + model file (``https://alphafold.ebi.ac.uk``).
  A 404/``available: false`` response is expected for many TrEMBL entries; the
  command then attempts the explicit ESMFold fallback (only when it has the
  sequence), and if that also fails it returns an ``unavailable[]`` entry with the
  reasons and warnings.  An unavailable model is **not** evidence of absence.
* RCSB PDB experimental structures (mmCIF by default).
* EMDB density maps (large; a warning is emitted and the file is only written to
  ``out``).

Partial failures are collected in ``unavailable[]`` and never silently become
successes; the command only raises for invalid input.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from .. import provenance, util
from ..commands.base import command
from ..errors import InvalidInputError, LifesciError, ToolMissingError

AFDB_API_URL = "https://alphafold.ebi.ac.uk/api/prediction/{accession}"
AFDB_FILE_URL = "https://alphafold.ebi.ac.uk/files/AF-{accession}-F1-model_v6.pdb"
ESMFOLD_URL = "https://api.esmatlas.com/foldSequence/v1/pdb/"
PDB_FILE_URL = "https://files.rcsb.org/download/{pdb_id}.cif"
EMDB_MAP_URL = "https://ftp.ebi.ac.uk/pub/databases/emdb/structures/EMD-{emdb_id}/map/emd_{emdb_id}.map.gz"

INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "accessions": {
            "type": "array",
            "items": {"type": "string", "minLength": 1},
            "description": (
                "UniProt accessions whose AlphaFold DB models to fetch. AFDB has no model for many "
                "TrEMBL entries; those entries go through the ESMFold fallback (when enabled) and, if "
                "that fails too, are reported in unavailable[] with an explicit 'no model' verdict."
            ),
        },
        "pdb_ids": {
            "type": "array",
            "items": {"type": "string", "minLength": 4},
            "description": "RCSB PDB IDs (e.g. 1UBQ) to download as experimental structures.",
        },
        "emdb_ids": {
            "type": "array",
            "items": {"type": "string"},
            "description": "EMDB map IDs (e.g. EMD-1234). Maps are large; they are written to 'out' only.",
        },
        "out": {
            "type": "string",
            "description": (
                "Output directory for downloaded model/structure files (default <cache-dir>/out/struct.fetch). "
                "Relative paths resolve against the working directory. Every file gets a recorded SHA-256."
            ),
        },
        "afdb_format": {
            "type": "string",
            "enum": ["pdb", "cif"],
            "default": "pdb",
            "description": "File format for AlphaFold DB model downloads (pdb or cif).",
        },
        "pdb_format": {
            "type": "string",
            "enum": ["cif", "pdb"],
            "default": "cif",
            "description": "File format for RCSB PDB downloads (mmCIF is the canonical format).",
        },
        "esmfold_fallback": {
            "type": "boolean",
            "default": True,
            "description": (
                "When an AlphaFold DB model is unavailable, fetch the UniProt sequence and attempt an "
                "ESMFold prediction (single-sequence, not an experimentally validated structure). The "
                "fallback attempt and its outcome are always reported; ESMFold returns HTTP 413 for "
                "very long proteins and can time out (504)."
            ),
        },
    },
}


def _resolve_path(ctx: Any, value: str) -> Path:
    target = Path(str(value)).expanduser()
    if not target.is_absolute():
        target = Path(ctx.cwd) / target
    return target.resolve()


def _out_dir(ctx: Any, out: str | None) -> Path:
    base = _resolve_path(ctx, out) if out else Path(ctx.cache_dir) / "out" / "struct.fetch"
    util.ensure_dir(base)
    return base


def _record_file(ctx: Any, path: Path, *, source: str, notes: Mapping[str, Any] | None = None) -> dict[str, Any]:
    log = getattr(ctx, "_provenance_log", None)
    if log is None:
        log = provenance.ProvenanceLog(getattr(ctx, "log_path", None))
        ctx._provenance_log = log
    sha256 = util.sha256_file(path)
    size = path.stat().st_size
    ctx.provenance.append(
        log.local_file(
            path=str(path),
            sha256=sha256,
            bytes_=size,
            kind="structure_file",
            notes={"source": source, **dict(notes or {})},
        )
    )
    return {"sha256": sha256, "bytes": size}


def _plan(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
    accessions = [str(a) for a in (args.get("accessions") or [])]
    pdb_ids = [str(p) for p in (args.get("pdb_ids") or [])]
    emdb_ids = [str(e) for e in (args.get("emdb_ids") or [])]
    out_dir = _out_dir(ctx, args.get("out"))
    planned: list[dict[str, Any]] = []
    for accession in accessions:
        planned.append(
            {"method": "GET", "url": AFDB_API_URL.format(accession=accession), "purpose": "alphafold_prediction"}
        )
        planned.append(
            {
                "method": "GET",
                "url": AFDB_FILE_URL.format(accession=accession),
                "purpose": "alphafold_model",
                "note": "actual URL comes from the prediction payload (pdbUrl/cifUrl)",
            }
        )
        if args.get("esmfold_fallback", True):
            planned.append(
                {
                    "method": "POST",
                    "url": ESMFOLD_URL,
                    "purpose": "esmfold_fallback_if_afdb_unavailable",
                    "note": "body = raw UniProt sequence (text/plain); only attempted after an AFDB 404",
                }
            )
            planned.append(
                {
                    "method": "GET",
                    "url": "https://rest.uniprot.org/uniprotkb/{accession}.fasta".format(accession=accession),
                    "purpose": "sequence_for_esmfold_fallback",
                }
            )
    for pdb_id in pdb_ids:
        planned.append({"method": "GET", "url": PDB_FILE_URL.format(pdb_id=pdb_id.upper()), "purpose": "pdb_structure"})
    for emdb_id in emdb_ids:
        planned.append({"method": "GET", "url": EMDB_MAP_URL.format(emdb_id=emdb_id), "purpose": "emdb_map"})
    return {
        "command": "struct.fetch",
        "dry_run": True,
        "requested": {"accessions": accessions, "pdb_ids": pdb_ids, "emdb_ids": emdb_ids},
        "planned_requests": planned,
        "structures": [],
        "unavailable": [],
        "resolved": {
            "dry_run": True,
            "cache_dir": str(ctx.cache_dir),
            "out_dir": str(out_dir),
            "n_requested": len(accessions) + len(pdb_ids) + len(emdb_ids),
            "n_available": 0,
            "n_unavailable": 0,
        },
    }


def _write_esmfold_pdb(ctx: Any, pdb_text: str, out_dir: Path, accession: str) -> tuple[Path, dict[str, Any]]:
    path = out_dir / f"AF-{util.slugify(accession, max_length=80, fallback='model')}-F1-esmfold.pdb"
    util.atomic_write_text(path, pdb_text if pdb_text.endswith("\n") else pdb_text + "\n")
    info = _record_file(ctx, path, source="esmfold", notes={"accession": accession})
    return path, info


def _unavailable_record(
    identifier: str,
    requested_source: str,
    *,
    reason: str,
    attempted: list[dict[str, Any]],
    detail: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "id": identifier,
        "requested_source": requested_source,
        "reason": reason,
        "attempted": util.json_safe(list(attempted)),
        "explicit_no_model": True,
        "detail": util.json_safe(dict(detail or {})),
    }


@command(
    name="struct.fetch",
    title="Fetch structure models",
    summary="Download AlphaFold DB models (with ESMFold fallback), PDB structures and EMDB maps; record SHA-256.",
    description=(
        "Fetch protein structure models/structures reproducibly. AlphaFold DB entries come with their "
        "model version and file URLs; when AFDB has no model (common for TrEMBL entries) the command "
        "attempts an ESMFold single-sequence prediction and then, if that also fails, reports an explicit "
        "'no model' verdict in unavailable[] with warnings - an unavailable model is NOT evidence of "
        "absence. RCSB PDB mmCIF files and EMDB density maps (large; warning) are also supported. Every "
        "written file carries a SHA-256 recorded in provenance, and the model version/release is returned "
        "for each available structure. Caveats: AlphaFold/ESMFold models are predictions, not experimental "
        "structures; ESMFold is limited to short sequences (HTTP 413 for very long proteins) and can time "
        "out; per-residue confidence (pLDDT) is not evaluated here."
    ),
    input_schema=INPUT_SCHEMA,
    output_schema="struct.fetch.schema.json",
    category="structure",
    timeout_ms=600_000,
    danger="network",
    examples=(
        {"accessions": ["P69905"]},
        {"pdb_ids": ["1UBQ"], "out": "structures"},
    ),
)
def run(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
    """Download requested structures/models and report availability explicitly."""
    ctx.validate()
    accessions = [str(item).strip() for item in (args.get("accessions") or []) if str(item).strip()]
    pdb_ids = [str(item).strip().upper() for item in (args.get("pdb_ids") or []) if str(item).strip()]
    emdb_ids = [str(item).strip() for item in (args.get("emdb_ids") or []) if str(item).strip()]
    if not accessions and not pdb_ids and not emdb_ids:
        raise InvalidInputError("provide at least one of 'accessions', 'pdb_ids' or 'emdb_ids'")
    if ctx.dry_run:
        return _plan(args, ctx)

    try:
        from ..adapters import alphafold, pdb
    except ImportError as err:  # pragma: no cover - parallel workstream ordering
        raise ToolMissingError(
            "lifesci.adapters.alphafold/pdb are not available; struct.fetch cannot run",
            details={"error": str(err)},
            fallbacks=["retry after the adapters workstream lands"],
        ) from err
    try:
        from ..adapters import esmfold, uniprot
    except ImportError:  # ESMFold fallback is optional; report it as unavailable
        esmfold = None  # type: ignore[assignment]
        uniprot = None  # type: ignore[assignment]

    out_dir = _out_dir(ctx, args.get("out"))
    afdb_format = str(args.get("afdb_format") or "pdb").lower()
    pdb_format = str(args.get("pdb_format") or "cif").lower()
    esmfold_fallback = bool(args.get("esmfold_fallback", True))
    structures: list[dict[str, Any]] = []
    unavailable: list[dict[str, Any]] = []
    attempted_by_id: dict[str, list[dict[str, Any]]] = {}

    def attempt(identifier: str, source: str, status: str, detail: Mapping[str, Any] | None = None) -> None:
        attempted_by_id.setdefault(identifier, []).append(
            {"source": source, "status": status, "detail": util.json_safe(dict(detail or {}))}
        )

    for accession in accessions:
        attempt(accession, "alphafold", "requested")
        try:
            prediction = alphafold.prediction(ctx, accession)
        except LifesciError as err:
            attempt(accession, "alphafold", "error", {"code": err.code, "message": err.message})
            unavailable.append(
                _unavailable_record(
                    accession,
                    "alphafold",
                    reason=err.code,
                    attempted=attempted_by_id[accession],
                    detail={"message": err.message},
                )
            )
            ctx.warn(
                "MODEL_FETCH_FAILED",
                f"AlphaFold DB lookup for {accession} failed ({err.code}); no model retrieved.",
                context={"accession": accession, "error": err.message},
            )
            continue

        if prediction.get("available") and (prediction.get("pdb_url") or prediction.get("cif_url")):
            attempt(accession, "alphafold", "available", {"model_version": prediction.get("model_version")})
            try:
                download = alphafold.download_model(ctx, accession, out_dir=out_dir, fmt=afdb_format)
            except LifesciError as err:
                attempt(accession, "alphafold", "download_failed", {"code": err.code, "message": err.message})
                unavailable.append(
                    _unavailable_record(
                        accession,
                        "alphafold",
                        reason=err.code,
                        attempted=attempted_by_id[accession],
                        detail={"message": err.message},
                    )
                )
                ctx.warn(
                    "MODEL_DOWNLOAD_FAILED",
                    f"AlphaFold DB reported a model for {accession} but the download failed ({err.code}).",
                    context={"accession": accession, "error": err.message},
                )
                continue
            if download.get("available") is False or not download.get("path"):
                reason = str(download.get("reason") or download.get("error") or "download_unavailable")
                attempt(accession, "alphafold", "download_unavailable", {"reason": reason})
                unavailable.append(
                    _unavailable_record(
                        accession,
                        "alphafold",
                        reason="download_unavailable",
                        attempted=attempted_by_id[accession],
                        detail={"adapter_reason": reason, "note": "AFDB metadata existed but the model file was unavailable"},
                    )
                )
                ctx.warn(
                    "MODEL_UNAVAILABLE_EXPLICIT",
                    f"AlphaFold DB metadata for {accession} was found but no model file could be retrieved "
                    f"({reason}); report 'no model available', not evidence of absence.",
                    context={"accession": accession, "reason": reason},
                )
                continue
            record: dict[str, Any] = {
                "id": accession,
                "source": "alphafold",
                "kind": "afdb_model",
                "path": download.get("path"),
                "sha256": download.get("sha256"),
                "bytes": download.get("bytes"),
                "model_version": prediction.get("model_version"),
                "release": prediction.get("release") or download.get("release"),
                "entry_id": prediction.get("entry_id"),
                "available": bool(download.get("available", True)),
                "url": prediction.get("pdb_url") if afdb_format == "pdb" else prediction.get("cif_url"),
                "uniprot_start": prediction.get("uniprot_start"),
                "uniprot_end": prediction.get("uniprot_end"),
                "format": afdb_format,
            }
            if not record["available"]:
                ctx.warn(
                    "MODEL_DOWNLOAD_UNAVAILABLE",
                    f"AlphaFold DB reported a model for {accession} but no usable model file was "
                    f"produced ({download.get('reason') or 'unknown reason'}); this is a missing model, "
                    "not evidence of absence.",
                    context={"accession": accession, "reason": download.get("reason")},
                )
            if record["path"] and Path(str(record["path"])).is_file():
                file_info = _record_file(ctx, Path(str(record["path"])), source="alphafold", notes={"accession": accession})
                record["file_sha256"] = file_info["sha256"]
                record["bytes"] = file_info.get("bytes") or record.get("bytes")
            if not record.get("release"):
                ctx.warn(
                    "DATABASE_RELEASE_UNKNOWN",
                    f"AlphaFold DB did not expose a release/model version for {accession}.",
                    context={"accession": accession, "model_version": record.get("model_version")},
                )
            structures.append(record)
            continue

        # AFDB has no model: explicit fallback chain, never a silent failure.
        attempt(accession, "alphafold", "unavailable", {"reason": "no_prediction"})
        if not esmfold_fallback:
            unavailable.append(
                _unavailable_record(
                    accession,
                    "alphafold",
                    reason="afdb_unavailable",
                    attempted=attempted_by_id[accession],
                    detail={"note": "esmfold_fallback disabled by input"},
                )
            )
            ctx.warn(
                "MODEL_UNAVAILABLE",
                f"AlphaFold DB has no model for {accession} and the ESMFold fallback was disabled; "
                "report as 'no model available', not as evidence of absence.",
                context={"accession": accession},
            )
            continue

        ctx.warn(
            "AFDB_UNAVAILABLE_ESMFOLD_FALLBACK",
            f"AlphaFold DB has no model for {accession}; attempting the ESMFold fallback.",
            context={"accession": accession},
        )
        sequence: str | None = None
        try:
            if uniprot is None:
                raise LifesciError(
                    "the UniProt adapter needed for the sequence is unavailable",
                    code="ADAPTER_MISSING",
                    retriable=False,
                )
            fasta = uniprot.fetch_fasta(ctx, accession)
            sequence = str(fasta.get("sequence") or "")
            attempt(accession, "uniprot_sequence", "ok", {"length": len(sequence)})
        except LifesciError as err:
            attempt(accession, "uniprot_sequence", "error", {"code": err.code, "message": err.message})
            ctx.warn(
                "ESMFOLD_FALLBACK_UNAVAILABLE",
                f"ESMFold fallback for {accession} needs its sequence, but the UniProt fetch failed ({err.code}).",
                context={"accession": accession, "error": err.message},
            )
        if sequence and esmfold is None:
            attempt(accession, "esmfold", "unavailable", {"reason": "adapter missing"})
            ctx.warn(
                "ESMFOLD_FALLBACK_UNAVAILABLE",
                "The ESMFold adapter is not available in this build; no fallback model can be produced.",
                context={"accession": accession},
            )
        elif sequence:
            try:
                fold = esmfold.fold(ctx, sequence)
            except LifesciError as err:
                fold = {"available": False, "reason": err.code, "status": None}
                attempt(accession, "esmfold", "error", {"code": err.code, "message": err.message})
            else:
                attempt(
                    accession,
                    "esmfold",
                    "available" if fold.get("available") else "unavailable",
                    {"status": fold.get("status"), "reason": fold.get("reason")},
                )
            if fold.get("available") and fold.get("pdb"):
                path, file_info = _write_esmfold_pdb(ctx, str(fold["pdb"]), out_dir, accession)
                structures.append(
                    {
                        "id": accession,
                        "source": "esmfold",
                        "kind": "esmfold_model",
                        "path": str(path),
                        "sha256": fold.get("sha256"),
                        "file_sha256": file_info["sha256"],
                        "bytes": file_info["bytes"],
                        "model_version": "esmfold",
                        "release": fold.get("release"),
                        "available": True,
                        "url": fold.get("url") or ESMFOLD_URL,
                        "sequence_length": len(sequence),
                        "fallback_for": "alphafold",
                        "format": "pdb",
                    }
                )
                ctx.warn(
                    "ESMFOLD_MODEL_USED",
                    f"No AlphaFold DB model for {accession}; an ESMFold single-sequence prediction was used "
                    "instead (lower confidence, not an experimental structure).",
                    context={"accession": accession, "length": len(sequence)},
                )
                continue
        unavailable.append(
            _unavailable_record(
                accession,
                "alphafold",
                reason="no_model_available",
                attempted=attempted_by_id[accession],
                detail={"esmfold": "unavailable", "note": "AFDB 404 and ESMFold fallback did not produce a model"},
            )
        )
        ctx.warn(
            "MODEL_UNAVAILABLE_EXPLICIT",
            f"No structure model could be obtained for {accession}: AlphaFold DB has no entry and the "
            "ESMFold fallback did not produce a model. Report 'no model available', never 'protein absent'.",
            context={"accession": accession, "attempted": attempted_by_id[accession]},
        )

    for pdb_id in pdb_ids:
        attempt(pdb_id, "pdb", "requested")
        try:
            download = pdb.download_structure(ctx, pdb_id, out_dir=out_dir, fmt=pdb_format)
        except LifesciError as err:
            attempt(pdb_id, "pdb", "error", {"code": err.code, "message": err.message})
            unavailable.append(
                _unavailable_record(
                    pdb_id, "pdb", reason=err.code, attempted=attempted_by_id[pdb_id], detail={"message": err.message}
                )
            )
            ctx.warn(
                "STRUCTURE_FETCH_FAILED",
                f"RCSB PDB download for {pdb_id} failed ({err.code}).",
                context={"pdb_id": pdb_id, "error": err.message},
            )
            continue
        path_value = download.get("path")
        file_sha = None
        if path_value and Path(str(path_value)).is_file():
            file_info = _record_file(ctx, Path(str(path_value)), source="pdb", notes={"pdb_id": pdb_id})
            file_sha = file_info["sha256"]
        structures.append(
            {
                "id": pdb_id,
                "source": "pdb",
                "kind": "pdb_experimental",
                "path": path_value,
                "sha256": download.get("sha256"),
                "file_sha256": file_sha,
                "bytes": download.get("bytes"),
                "model_version": None,
                "release": download.get("release"),
                "available": True,
                "url": download.get("url") or PDB_FILE_URL.format(pdb_id=pdb_id),
                "format": download.get("format") or pdb_format,
            }
        )

    for emdb_id in emdb_ids:
        attempt(emdb_id, "emdb", "requested")
        try:
            download = pdb.download_emdb_map(ctx, emdb_id, out_dir=out_dir)
        except LifesciError as err:
            attempt(emdb_id, "emdb", "error", {"code": err.code, "message": err.message})
            unavailable.append(
                _unavailable_record(
                    emdb_id, "emdb", reason=err.code, attempted=attempted_by_id[emdb_id], detail={"message": err.message}
                )
            )
            ctx.warn(
                "STRUCTURE_FETCH_FAILED",
                f"EMDB map download for {emdb_id} failed ({err.code}).",
                context={"emdb_id": emdb_id, "error": err.message},
            )
            continue
        path_value = download.get("path")
        file_sha = None
        if path_value and Path(str(path_value)).is_file():
            file_info = _record_file(ctx, Path(str(path_value)), source="emdb", notes={"emdb_id": emdb_id})
            file_sha = file_info["sha256"]
        structures.append(
            {
                "id": emdb_id,
                "source": "emdb",
                "kind": "emdb_map",
                "path": path_value,
                "sha256": download.get("sha256"),
                "file_sha256": file_sha,
                "bytes": download.get("bytes"),
                "model_version": None,
                "release": download.get("release"),
                "available": True,
                "url": download.get("url") or EMDB_MAP_URL.format(emdb_id=emdb_id),
                "format": "map.gz",
            }
        )

    requested = len(accessions) + len(pdb_ids) + len(emdb_ids)
    if requested and not structures and not unavailable:  # pragma: no cover - defensive
        raise LifesciError(
            "struct.fetch produced no structures and no explicit unavailable verdicts",
            code="STRUCT_FETCH_FAILED",
            details={"requested": requested},
        )
    releases = provenance.load_releases(ctx.cache_dir)
    return {
        "dry_run": False,
        "requested": {"accessions": accessions, "pdb_ids": pdb_ids, "emdb_ids": emdb_ids},
        "structures": structures,
        "unavailable": unavailable,
        "resolved": {
            "cache_dir": str(ctx.cache_dir),
            "out_dir": str(out_dir),
            "n_requested": requested,
            "n_available": len(structures),
            "n_unavailable": len(unavailable),
            "providers": {
                "alphafold": {"format": afdb_format, "n_models": sum(1 for s in structures if s["source"] == "alphafold")},
                "esmfold": {"n_models": sum(1 for s in structures if s["source"] == "esmfold")},
                "pdb": {"format": pdb_format, "n_structures": sum(1 for s in structures if s["source"] == "pdb")},
                "emdb": {"n_maps": sum(1 for s in structures if s["source"] == "emdb")},
            },
            "releases": releases,
        },
    }
