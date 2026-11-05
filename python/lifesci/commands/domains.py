"""``lifesci.domains`` — domain/family annotation and the domain-arbiter verdict.

The command supports three complementary evidence sources and merges them into one
explicit per-protein, per-domain verdict:

1. **InterPro remote scan** (``accessions``) — the cached InterPro entry API for
   each protein (Pfam/PANTHER/PRINTS/… entries with residue locations).
2. **Recorded evidence** (``evidence_rows`` / ``rows_file``) — previously recorded
   per-protein best-domain records (e.g. a Pfam proteome scan exported as
   ``P69905 PF00042 E=1.2e-30``). This replays recorded evidence offline.
3. **Local HMM scan** (``hmms`` or ``hmm_ids`` + ``sequences``/``targets_fasta``) —
   pyhmmer hmmsearch of profile HMMs against the supplied sequences. The actual
   HMMER E-value is reported together with the number of target sequences scanned,
   because E-values scale with the target database size.

Every requested domain gets ``present`` / ``weak`` / ``absent`` / ``unknown`` and a
human-readable evidence string. **Absent is an annotation statement, not proof of
functional absence**: the payload reports the E-value cutoff and reporting
threshold used, and emits ``DOMAIN_ABSENT``. When sequence-level evidence is
supplied, the domain arbiter compares it with the domain call: a protein with no
sequence hit but a significant domain is reported as domain-arbiter-present
(domain evidence outranks a single weak sequence line).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Iterable, Mapping

from .. import provenance, util
from ..commands.base import command
from ..errors import DataError, InvalidInputError, LifesciError, ToolMissingError

INTERPRO_URL = "https://www.ebi.ac.uk/interpro/api/entry/all/protein/uniprot/{accession}/"
HMM_URL = "https://www.ebi.ac.uk/interpro/wwwapi/entry/{database}/{entry_id}/?annotation=hmm"
_UNIPROT_ACC = re.compile(r"(?:sp|tr)\|([^|]+)\|")
_ACC_TOKEN = re.compile(r"\b([OPQ][0-9][A-Z0-9]{3}[0-9]|[A-NR-Z][0-9](?:[A-Z][A-Z0-9]{2}[0-9]){1,2})\b")
_HMM_ACC = re.compile(r"^ACC\s+(\S+)")
_HMM_NAME = re.compile(r"^NAME\s+(\S+)")

INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "accessions": {
            "type": "array",
            "items": {"type": "string", "minLength": 1},
            "description": (
                "UniProt accessions for a remote InterPro scan (cached; replays offline when "
                "the InterPro payloads are already in the cache)."
            ),
        },
        "domains": {
            "type": "array",
            "items": {"type": "string", "minLength": 1},
            "description": (
                "Domain/family accessions to arbitrate explicitly (e.g. ['PF00042']). Each gets a "
                "present/weak/absent/unknown verdict even when no annotation is returned. When "
                "omitted, every model found in the merged evidence is reported."
            ),
        },
        "evidence_rows": {
            "type": "array",
            "items": {"type": "object"},
            "description": (
                "Recorded per-protein domain-evidence rows (offline replay). Accepts the standard "
                "presence formats: keys accession/protein/best_hit, model/pfam_model, evalue, score, "
                "model_cov, protein_length, description, source, release, target_db_size."
            ),
        },
        "rows_file": {
            "type": "string",
            "description": (
                "TSV/JSON file of recorded domain-evidence rows (or a curated table). Relative "
                "paths resolve against the working directory."
            ),
        },
        "hmms": {
            "type": "array",
            "description": "Local HMMER profile files to scan with (no remote model fetch).",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["path"],
                "properties": {
                    "id": {"type": "string", "description": "Model accession/name, e.g. PF00042."},
                    "path": {"type": "string", "description": "Local .hmm/.hmm.gz file path."},
                },
            },
        },
        "hmm_ids": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Pfam model accessions to download from InterPro (wwwapi?annotation=hmm, cached) and "
                "scan locally, e.g. ['PF00042']."
            ),
        },
        "sequences": {
            "type": "object",
            "description": "Inline target sequences for the local HMM scan, keyed by accession.",
            "additionalProperties": {"type": "string"},
        },
        "targets_fasta": {
            "type": "string",
            "description": "FASTA file of target sequences for the local HMM scan.",
        },
        "report_evalue": {
            "type": "number",
            "minimum": 0,
            "default": 10.0,
            "description": (
                "HMMER reporting threshold for the local scan. Use a permissive value (e.g. 1000) to "
                "state explicitly 'no hit at E<=N' for an absent domain; the cutoff is reported with "
                "the absent verdict."
            ),
        },
        "present_evalue": {
            "type": "number",
            "minimum": 0,
            "default": 0.001,
            "description": "E-value cutoff for a 'present' domain call (default 1e-3).",
        },
        "weak_evalue": {
            "type": "number",
            "minimum": 0,
            "default": 1.0,
            "description": "E-value cutoff below which a domain call is 'weak' rather than absent.",
        },
        "sequence_evidence": {
            "type": "object",
            "description": (
                "Optional sequence-level (phmmer) evidence to compare with the domain calls, keyed by "
                "accession: {'detectable': bool|'weak'|'absent', 'best_evalue': number|null, 'source': str}. "
                "A significant domain with no sequence hit is reported as domain-arbiter-present."
            ),
            "additionalProperties": {"type": "object"},
        },
        "cpus": {
            "type": "integer",
            "minimum": 1,
            "maximum": 64,
            "default": 4,
            "description": "CPU threads for the local pyhmmer scan.",
        },
        "max_hits": {
            "type": "integer",
            "minimum": 1,
            "maximum": 5000,
            "default": 200,
            "description": "Maximum recorded local HMM hits.",
        },
        "out": {
            "type": "string",
            "description": "Optional directory for merged domain records written as JSON.",
        },
    },
}


def _resolve_path(ctx: Any, value: str) -> Path:
    target = Path(str(value)).expanduser()
    if not target.is_absolute():
        target = Path(ctx.cwd) / target
    return target.resolve()


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
    accessions = [str(a) for a in (args.get("accessions") or [])]
    planned: list[dict[str, Any]] = []
    for accession in accessions:
        planned.append(
            {
                "method": "GET",
                "url": INTERPRO_URL.format(accession=accession),
                "params": {"page_size": 50},
                "purpose": "interpro_entries",
            }
        )
    for two_part in [str(item) for item in (args.get("hmm_ids") or [])]:
        database = "pfam" if two_part.upper().startswith("PF") else "all"
        planned.append(
            {
                "method": "GET",
                "url": HMM_URL.format(database=database, entry_id=two_part),
                "purpose": "hmm_download",
            }
        )
    local_scan = bool(args.get("hmms") or args.get("hmm_ids") or args.get("targets_fasta") or args.get("sequences"))
    rows_file = args.get("rows_file")
    if rows_file:
        planned.append({"purpose": "read_recorded_domain_evidence", "path": str(rows_file)})
    return {
        "command": "domains",
        "dry_run": True,
        "requested": {
            "accessions": accessions,
            "domains": list(args.get("domains") or []),
            "local_hmm_scan": local_scan,
            "recorded_evidence": bool(rows_file or args.get("evidence_rows")),
        },
        "planned_requests": planned,
        "proteins": [],
        "domain_counts": {"present": 0, "weak": 0, "absent": 0, "unknown": 0},
        "hmm_scan": {"ran": False, "planned": local_scan, "hits": [], "models": []},
        "arbiter": {"rows": [], "n_domain_present": 0, "n_domain_absent": 0, "n_conflicts": 0},
        "resolved": {
            "dry_run": True,
            "cache_dir": str(ctx.cache_dir),
            "n_proteins": len(accessions),
            "present_evalue_cutoff": float(args.get("present_evalue", 1e-3)),
            "weak_evalue_cutoff": float(args.get("weak_evalue", 1.0)),
            "report_evalue": float(args.get("report_evalue", 10.0)),
        },
        "errors": [],
    }


# ---------------------------------------------------------------------------
# row normalisation (recorded evidence)
# ---------------------------------------------------------------------------


def _first(row: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in row and row[key] not in (None, ""):
            return row[key]
    return None


def _normalise_protein(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.upper() in {"NA", "N/A", "NONE", "-"}:
        return None
    match = _UNIPROT_ACC.search(text)
    if match:
        return match.group(1)
    token = text.split()[0]
    return token or None


def _normalise_rows(rows: Iterable[Mapping[str, Any]], *, default_source: str = "recorded") -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, Mapping):
            raise DataError("domain evidence rows must be JSON objects")
        accession = _normalise_protein(
            _first(row, "accession", "protein", "best_hit", "hit", "target", "accession_id")
        )
        model = _first(row, "model", "pfam_model", "entry_accession", "domain", "hmm", "model_accession")
        model = str(model).strip() if model is not None else None
        if model is not None and model.upper() in {"NA", "N/A", "NONE", "-"}:
            model = None
        evalue = util.parse_float(_first(row, "evalue", "e_value", "E", "eval"))
        score = util.parse_float(_first(row, "score", "bitscore", "bit_score"))
        model_cov = util.parse_float(_first(row, "model_cov", "model_coverage", "mcov", "cov"))
        target_db_size = util.parse_int(_first(row, "target_db_size", "n_targets", "db_size", "n_sequences"))
        records.append(
            {
                "accession": accession,
                "model": model,
                "evalue": evalue,
                "score": score,
                "model_cov": model_cov,
                "protein_length": util.parse_int(_first(row, "protein_length", "target_length", "length")),
                "description": (str(_first(row, "description", "desc") or "")).strip() or None,
                "source": str(_first(row, "source") or default_source),
                "database": str(_first(row, "database", "db") or default_source),
                "release": _first(row, "release"),
                "target_db_size": target_db_size,
                "organism": _first(row, "organism"),
            }
        )
    return records


# ---------------------------------------------------------------------------
# InterPro records
# ---------------------------------------------------------------------------


def _looks_like_evalue(value: float) -> bool:
    return value >= 0 and (value < 1e-2 or value == 0)


def _location_record(location: Mapping[str, Any]) -> dict[str, Any]:
    """Normalise one InterPro location, disambiguating score vs E-value."""
    raw_score = util.parse_float(location.get("score"))
    raw_evalue = util.parse_float(location.get("evalue"))
    evalue = raw_evalue
    score_source = "evalue"
    if evalue is None and raw_score is not None and _looks_like_evalue(raw_score):
        evalue = raw_score
        score_source = "interpro_score_field"
    return {
        "start": util.parse_int(location.get("start")),
        "end": util.parse_int(location.get("end")),
        "evalue": evalue,
        "score": raw_score,
        "score_source": score_source,
        "fragments": util.json_safe(location.get("fragments")),
    }


def _interpro_models(ctx: Any, accession: str) -> dict[str, Any]:
    try:
        from ..adapters import interpro
    except ImportError as err:  # pragma: no cover - parallel workstream ordering
        raise ToolMissingError(
            "lifesci.adapters.interpro is not available; remote InterPro scans cannot run",
            details={"error": str(err)},
            fallbacks=["replay recorded evidence rows ('rows_file'/'evidence_rows')"],
        ) from err

    scan = interpro.entry_all_for_protein(ctx, accession)
    models: dict[str, dict[str, Any]] = {}
    entries: list[dict[str, Any]] = []
    for entry in scan.get("entries") or []:
        entry_accession = str(entry.get("entry_accession") or "").strip()
        locations = [_location_record(location) for location in (entry.get("locations") or [])]
        record = {
            "model": entry_accession,
            "source": "interpro",
            "database": str(entry.get("source_database") or "interpro"),
            "entry_name": entry.get("entry_name"),
            "entry_type": entry.get("entry_type"),
            "locations": locations,
            "release": scan.get("release"),
        }
        entries.append(record)
        if not entry_accession:
            continue
        current = models.get(entry_accession)
        if current is None:
            models[entry_accession] = record
        else:
            current["locations"].extend(locations)
    return {
        "available": True,
        "models": models,
        "entries": entries,
        "release": scan.get("release"),
        "url": scan.get("url"),
        "sha256": scan.get("sha256"),
        "n_entries": int(scan.get("n_entries") or len(entries)),
    }


# ---------------------------------------------------------------------------
# local HMM scan
# ---------------------------------------------------------------------------


def _hmm_identity(path: Path) -> dict[str, str | None]:
    """Read NAME/ACC from a (possibly gzipped) HMMER model file."""
    try:
        raw = util.read_maybe_gzip(path)
    except OSError:  # pragma: no cover - unreadable file is reported elsewhere
        return {"accession": None, "name": None}
    text = raw.decode("utf-8", errors="replace")
    name = accession = None
    for line in text.splitlines()[:80]:
        match = _HMM_NAME.match(line)
        if match and name is None:
            name = match.group(1)
        match = _HMM_ACC.match(line)
        if match and accession is None:
            accession = match.group(1).split(".")[0]
        if name and accession:
            break
    return {"accession": accession, "name": name}


def _target_sequences(
    ctx: Any,
    args: dict[str, Any],
) -> tuple[dict[str, str], Path | None, list[dict[str, Any]]]:
    """Resolve target sequences and, if needed, write a temp FASTA for pyhmmer."""
    sequences: dict[str, str] = {}
    for name, sequence in (args.get("sequences") or {}).items():
        accession = _normalise_protein(name) or str(name)
        sequences[accession] = str(sequence).strip()
    fasta_path: Path | None = None
    if args.get("targets_fasta"):
        fasta_path = _resolve_path(ctx, str(args["targets_fasta"]))
        if not fasta_path.is_file():
            raise InvalidInputError(f"targets_fasta not found: {fasta_path}")
        _record_local_file(ctx, fasta_path, kind="target_sequences", notes={"command": "domains"})
        for header, sequence in util.read_fasta(fasta_path):
            accession = _normalise_protein(header) or header.split()[0]
            sequences.setdefault(accession, sequence)
    if not sequences:
        raise InvalidInputError("local HMM scan needs 'sequences' or 'targets_fasta'")
    if not fasta_path:
        tmp_dir = util.ensure_dir(Path(ctx.cache_dir) / "tmp")
        fasta_path = tmp_dir / "domains-local-scan.fasta"
        records = [(accession, sequence) for accession, sequence in sorted(sequences.items())]
        util.write_fasta(fasta_path, records)
    else:
        records = util.read_fasta(fasta_path)
    targets = [
        {"accession": _normalise_protein(header) or header.split()[0], "header": header, "length": len(sequence)}
        for header, sequence in records
    ]
    return sequences, fasta_path, targets


def _resolve_hmms(ctx: Any, args: dict[str, Any]) -> list[dict[str, Any]]:
    models: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in args.get("hmms") or []:
        path = _resolve_path(ctx, str(item.get("path")))
        if not path.is_file():
            raise InvalidInputError(f"HMM file not found: {path}")
        identity = _hmm_identity(path)
        model_id = str(item.get("id") or identity.get("accession") or identity.get("name") or path.stem)
        model_id = model_id.split(".")[0].upper()
        if model_id in seen:
            continue
        seen.add(model_id)
        _record_local_file(ctx, path, kind="hmm_model", notes={"command": "domains", "model": model_id})
        models.append(
            {
                "id": model_id,
                "name": identity.get("name"),
                "path": str(path),
                "sha256": util.sha256_file(path),
                "bytes": path.stat().st_size,
                "source": "local_file",
                "release": None,
            }
        )
    hmm_ids = [str(item) for item in (args.get("hmm_ids") or [])]
    if hmm_ids:
        try:
            from ..adapters import interpro
        except ImportError as err:  # pragma: no cover - parallel workstream ordering
            raise ToolMissingError(
                "lifesci.adapters.interpro is not available; cannot download HMM models for the local scan",
                details={"error": str(err)},
                fallbacks=["pass local HMM files via 'hmms' instead"],
            ) from err

        for model_id in hmm_ids:
            clean = model_id.split(".")[0]
            if clean in seen:
                continue
            database = "pfam" if clean.upper().startswith("PF") else "all"
            fetched = interpro.fetch_hmm(ctx, database=database, entry_id=clean)
            path_value = fetched.get("path")
            record = {
                "id": clean,
                "name": _hmm_identity(Path(str(path_value))).get("name") if path_value else None,
                "path": str(path_value) if path_value else None,
                "sha256": fetched.get("sha256"),
                "bytes": fetched.get("bytes"),
                "source": "interpro",
                "release": fetched.get("release"),
                "url": fetched.get("url"),
            }
            if path_value:
                _record_local_file(ctx, Path(str(path_value)), kind="hmm_model", notes={"command": "domains", "model": clean})
            seen.add(clean)
            models.append(record)
    return models


def _local_hmm_scan(
    ctx: Any,
    *,
    args: dict[str, Any],
    targets_fasta: Path,
    targets: list[dict[str, Any]],
) -> dict[str, Any]:
    try:
        from ..lib import hmmer
    except ImportError as err:  # pragma: no cover - dependency ordering
        raise ToolMissingError(
            "lifesci.lib.hmmer is not available; cannot run the local HMM scan",
            details={"error": str(err)},
            fallbacks=["use recorded evidence (evidence_rows/rows_file) or the remote InterPro scan"],
        ) from err
    if not hasattr(hmmer, "hmmsearch"):
        raise ToolMissingError(
            "lifesci.lib.hmmer.hmmsearch is not implemented yet; cannot run the local HMM scan",
            fallbacks=["use recorded evidence (evidence_rows/rows_file) or the remote InterPro scan"],
        )
    models = _resolve_hmms(ctx, args)
    if not models:
        return {"ran": False, "reason": "no HMM models resolved", "hits": [], "models": [], "target_db_size": len(targets)}
    hmm_paths = [Path(str(model["path"])) for model in models if model.get("path")]
    report_evalue = float(args.get("report_evalue", 10.0))
    result = hmmer.hmmsearch(
        ctx,
        hmm_paths=hmm_paths,
        targets_fasta=targets_fasta,
        E=report_evalue,
        cpus=int(args.get("cpus", 4)),
        max_hits=int(args.get("max_hits", 200)),
    )
    by_model_id = {str(model["id"]): model for model in models}
    by_alias: dict[str, str] = {}
    for model in models:
        for alias in (model.get("id"), model.get("name")):
            if alias:
                by_alias[str(alias).upper()] = str(model["id"])
    hits: list[dict[str, Any]] = []
    accession_by_target: dict[str, str] = {}
    for target in targets:
        accession_by_target[str(target["header"])] = str(target["accession"])
        accession_by_target[str(target["accession"])] = str(target["accession"])
    for raw_hit in result.get("hits") or []:
        hit = dict(raw_hit)
        model_raw = str(hit.get("model") or "").upper()
        model_id = by_alias.get(model_raw) or by_alias.get(model_raw.split(".")[0])
        target_raw = str(hit.get("target") or "")
        accession = accession_by_target.get(target_raw)
        if accession is None:
            model_match = _ACC_TOKEN.search(target_raw)
            accession = model_match.group(1) if model_match else target_raw.split()[0]
        normalised = {
            "model": model_id or model_raw,
            "target": accession,
            "target_raw": target_raw,
            "evalue": util.parse_float(hit.get("evalue")),
            "score": util.parse_float(hit.get("score")),
            "bias": util.parse_float(hit.get("bias")),
            "model_cov": util.parse_float(hit.get("model_cov")),
            "target_length": util.parse_int(hit.get("target_length")),
            "description": hit.get("description"),
            "target_db_size": len(targets),
            "report_evalue": report_evalue,
        }
        hits.append(normalised)
    version = result.get("version")
    return {
        "ran": True,
        "models": models,
        "n_models": len(models),
        "hits": hits,
        "n_hits": len(hits),
        "target_db_size": len(targets),
        "targets": targets,
        "report_evalue": report_evalue,
        "version": version,
        "params": util.json_safe(result.get("params") or {"E": report_evalue}),
    }


# ---------------------------------------------------------------------------
# verdicts
# ---------------------------------------------------------------------------


def _best_location_evalue(records: Iterable[Mapping[str, Any]]) -> float | None:
    values = [float(record["evalue"]) for record in records if record.get("evalue") is not None]
    return min(values) if values else None


def _evidence_string(
    model: str,
    *,
    verdict: str,
    source: str,
    evalue: float | None,
    protein_length: int | None,
    model_cov: float | None,
    accession: str | None,
    report_evalue: float | None,
    locations: list[Mapping[str, Any]] | None = None,
) -> str:
    if verdict == "present" and evalue is not None:
        if source == "recorded":
            return f"{model} E={util.fmt_evalue(evalue)} (recorded domain evidence; {accession})"
        if source == "local_hmm":
            return f"{model} E={util.fmt_evalue(evalue)} (local hmmsearch; {accession})"
        if source == "interpro":
            span = ""
            if locations:
                start = locations[0].get("start")
                end = locations[0].get("end")
                if start is not None and end is not None:
                    span = f"; residues {start}-{end}"
            return f"{model} E={util.fmt_evalue(evalue)} (InterPro annotation{span}; {accession})"
        return f"{model} E={util.fmt_evalue(evalue)} ({source}; {accession})"
    if verdict == "present":
        return f"{model} entry/annotation present without a numeric E-value ({source}; {accession})"
    if verdict == "weak" and evalue is not None:
        return f"{model} E={util.fmt_evalue(evalue)} (weak, above present cutoff; {accession})"
    if verdict == "absent":
        if source == "local_hmm":
            if report_evalue is None:
                cutoff = "the reporting threshold"
            elif float(report_evalue) == int(report_evalue):
                cutoff = str(int(report_evalue))
            else:
                cutoff = util.fmt_evalue(report_evalue)
            return f"no {model} hit at report E<={cutoff} (local hmmsearch)"
        if source == "recorded":
            return f"no {model} annotation in the recorded domain evidence"
        if source == "interpro":
            return f"no {model} annotation returned by InterPro"
        return f"no {model} annotation ({source})"
    return f"{model} evidence not assessed ({source})"


def _verdict_from_evalue(evalue: float | None, present_evalue: float, weak_evalue: float) -> str:
    if evalue is None:
        return "unknown"
    if evalue < present_evalue:
        return "present"
    if evalue < weak_evalue:
        return "weak"
    return "absent"


def _merge_model_verdict(
    sources: Mapping[str, dict[str, Any]],
    *,
    present_evalue: float,
    weak_evalue: float,
) -> tuple[str, str | None]:
    """Combine per-source calls; a present call from any source wins."""
    available = {name: record for name, record in sources.items() if record.get("scanned")}
    verdicts = {name: record.get("verdict") for name, record in available.items()}
    for preferred in ("present", "weak"):
        winners = [name for name, verdict in verdicts.items() if verdict == preferred]
        if winners:
            return preferred, winners[0]
    if any(verdict == "absent" for verdict in verdicts.values()):
        absent = [name for name, verdict in verdicts.items() if verdict == "absent"]
        return "absent", absent[0]
    return "unknown", None


def _arbiter(
    accession: str,
    *,
    sequence_evidence: Mapping[str, Any] | None,
    domain_verdicts: Mapping[str, str],
    requested_domains: list[str],
) -> dict[str, Any]:
    present = [model for model, verdict in domain_verdicts.items() if verdict == "present"]
    absent = [model for model, verdict in domain_verdicts.items() if verdict == "absent"]
    if sequence_evidence is None:
        return {
            "accession": accession,
            "sequence_evidence_available": False,
            "domain_present": bool(present),
            "domain_absent": bool(absent) and not present,
            "conclusion": "domain_only",
            "note": "no sequence-level evidence supplied; verdict rests on domain/recorded evidence only",
        }
    detectable = sequence_evidence.get("detectable")
    if isinstance(detectable, str):
        sequence_present = detectable.lower() in {"present", "detected", "true", "yes", "weak"}
        if detectable.lower() == "weak":
            sequence_present = "weak"
    elif detectable is None:
        best = util.parse_float(sequence_evidence.get("best_evalue"))
        sequence_present = bool(best is not None and best < 1.0)
    else:
        sequence_present = bool(detectable)
    if present and not sequence_present:
        conclusion = "domain_arbiter_present_without_sequence_hit"
        note = (
            "a significant domain was found while the sequence search gave no detectable hit; "
            "domain/family evidence outranks a single weak sequence line"
        )
    elif present and sequence_present:
        conclusion = "domain_supported"
        note = "significant domain and sequence-level evidence agree"
    elif absent and sequence_present:
        conclusion = "sequence_present_without_domain"
        note = "sequence hit without a supporting domain; inspect for domain false positives or partial architecture"
    elif absent and not sequence_present:
        conclusion = "domain_absent"
        note = "neither sequence nor domain evidence supports the family; this is absence of annotation, not proof of absence"
    else:
        conclusion = "unresolved"
        note = "no domain call could be compared with the sequence evidence"
    return {
        "accession": accession,
        "sequence_evidence_available": True,
        "sequence_present": sequence_present,
        "sequence_evidence": util.json_safe(dict(sequence_evidence)),
        "domain_present": bool(present),
        "domain_absent": bool(absent) and not present,
        "present_domains": present,
        "absent_domains": absent,
        "requested_domains": requested_domains,
        "conclusion": conclusion,
        "note": note,
    }


# ---------------------------------------------------------------------------
# command
# ---------------------------------------------------------------------------


@command(
    name="domains",
    title="Domain/family annotation and domain arbiter",
    summary="InterPro/Pfam domain scan, recorded-evidence replay, and/or local HMM scan with explicit domain-absent verdicts.",
    description=(
        "Annotate proteins with domains/families and deliver an explicit per-domain verdict. Three "
        "evidence sources can be combined: (1) a cached InterPro entry-API scan for UniProt accessions; "
        "(2) recorded per-protein domain-evidence rows ('evidence_rows'/'rows_file', e.g. a recorded "
        "Pfam proteome scan) for fully offline replay; (3) a local pyhmmer hmmsearch against supplied "
        "sequences/HMMs. Each requested domain gets present/weak/absent/unknown with its E-value, the "
        "database/release and the cutoff used, so an absent call is an explicit annotation statement with "
        "its reporting threshold (e.g. 'no PF00042 hit at report E<=1000'), never proof of functional "
        "absence. The domain arbiter can compare optional sequence-level (phmmer) evidence: a significant "
        "domain with no sequence hit is reported as domain-arbiter-present because domain/family evidence "
        "outranks a single weak sequence hit. Caveats: local hmmsearch E-values scale with the number of "
        "target sequences scanned and are not directly comparable to proteome-scan or InterPro-reported "
        "values (a warning is emitted); InterPro's per-location 'score' field has portal-specific "
        "semantics and for Pfam matches is an E-value-like score."
    ),
    input_schema=INPUT_SCHEMA,
    output_schema="domains.schema.json",
    category="sequence",
    timeout_ms=600_000,
    danger="network",
    examples=(
        {"accessions": ["P69905", "P00698"], "domains": ["PF00042"]},
        {"rows_file": "evidence/pfam_hits.tsv", "domains": ["PF00042"]},
        {
            "targets_fasta": "sequences/targets.fasta",
            "hmm_ids": ["PF00042"],
            "domains": ["PF00042"],
            "report_evalue": 1000.0,
        },
    ),
)
def run(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
    """Scan/merge domain evidence and return explicit per-domain verdicts."""
    ctx.validate()
    accessions = [str(item).strip() for item in (args.get("accessions") or []) if str(item).strip()]
    recorded_rows: list[dict[str, Any]] = []
    if args.get("evidence_rows"):
        if isinstance(args["evidence_rows"], Mapping):
            raise InvalidInputError("'evidence_rows' must be an array of row objects")
        for index, row in enumerate(args["evidence_rows"]):
            if not isinstance(row, Mapping):
                raise InvalidInputError(f"evidence_rows[{index}] must be an object")
            recorded_rows.append(dict(row))
    if args.get("rows_file"):
        rows_path = _resolve_path(ctx, str(args["rows_file"]))
        if not rows_path.is_file():
            raise InvalidInputError(f"rows_file not found: {rows_path}")
        raw_rows = _json_rows(rows_path) if rows_path.suffix.lower() == ".json" else util.read_tsv(rows_path)
        recorded_rows.extend(raw_rows)
        _record_local_file(ctx, rows_path, kind="evidence_rows", notes={"command": "domains"})
    recorded = _normalise_rows(recorded_rows) if recorded_rows else []
    local_inputs = bool(
        args.get("hmms") or args.get("hmm_ids") or args.get("targets_fasta") or args.get("sequences")
    )
    if not accessions and not recorded and not local_inputs:
        raise InvalidInputError(
            "provide at least one evidence source: 'accessions' (InterPro), "
            "'evidence_rows'/'rows_file' (recorded), or HMM/sequence inputs (local scan)"
        )
    requested_domains = [str(item).strip().upper() for item in (args.get("domains") or []) if str(item).strip()]
    if ctx.dry_run:
        return _plan(args, ctx)
    present_evalue = float(args.get("present_evalue", 1e-3))
    weak_evalue = float(args.get("weak_evalue", 1.0))
    report_evalue = float(args.get("report_evalue", 10.0))

    # ---- local HMM scan -------------------------------------------------
    hmm_scan: dict[str, Any] = {"ran": False, "hits": [], "models": [], "target_db_size": None}
    if local_inputs:
        _, targets_fasta, targets = _target_sequences(ctx, args)
        hmm_scan = _local_hmm_scan(ctx, args=args, targets_fasta=targets_fasta, targets=targets)
        if hmm_scan.get("ran"):
            ctx.warn_evalue_db_dependence(
                "local_hmmsearch",
                f"The local scan searched {hmm_scan.get('target_db_size')} sequence(s); E-values scale with "
                "target-database size and are not comparable with proteome-scan or InterPro E-values.",
            )
        else:
            ctx.warn(
                "HMM_SCAN_SKIPPED",
                "The local HMM scan did not run (" + str(hmm_scan.get("reason")) + ").",
                context={"reason": hmm_scan.get("reason")},
            )

    # ---- InterPro remote scan ------------------------------------------
    interpro_by_accession: dict[str, dict[str, Any]] = {}
    interpro_errors: list[dict[str, Any]] = []
    first_interpro_error: LifesciError | None = None
    for accession in accessions:
        try:
            interpro_by_accession[accession.upper()] = _interpro_models(ctx, accession)
        except (LifesciError, ImportError) as err:
            code = getattr(err, "code", "ADAPTER_MISSING")
            message = getattr(err, "message", str(err))
            first_interpro_error = first_interpro_error or (
                err if isinstance(err, LifesciError) else LifesciError(message, code=code, retriable=False)
            )
            interpro_errors.append({"accession": accession, "code": code, "message": message})
            ctx.warn(
                "DOMAIN_SCAN_FAILED",
                f"InterPro scan failed for {accession} ({code}); its domain verdict will rely on other evidence sources.",
                context={"accession": accession, "error": message},
            )

    # ---- merge per protein ---------------------------------------------
    protein_keys: list[str] = []
    for accession in accessions:
        protein_keys.append(accession.upper())
    if not protein_keys:
        # No explicit accessions: evaluate every protein present in the evidence.
        for row in recorded:
            if row.get("accession"):
                key = str(row["accession"]).upper()
                if key not in protein_keys:
                    protein_keys.append(key)
        for accession in interpro_by_accession:
            if accession not in protein_keys:
                protein_keys.append(accession)
        for target in hmm_scan.get("targets") or []:
            key = str(target.get("accession") or "").upper()
            if key and key not in protein_keys:
                protein_keys.append(key)
    if not protein_keys:  # pragma: no cover - guarded by input validation
        raise InvalidInputError("no protein keys could be assembled from the provided evidence")

    sequence_evidence_input = {
        str(key).upper(): dict(value) for key, value in (args.get("sequence_evidence") or {}).items() if isinstance(value, Mapping)
    }
    if any(accession not in sequence_evidence_input for accession in accessions):
        # Sequence evidence may use the original case-sensitive accession.
        for key, value in (args.get("sequence_evidence") or {}).items():
            if isinstance(value, Mapping) and str(key).upper() not in sequence_evidence_input:
                sequence_evidence_input[str(key).upper()] = dict(value)

    proteins: list[dict[str, Any]] = []
    domain_counts = {"present": 0, "weak": 0, "absent": 0, "unknown": 0}
    arbiter_rows: list[dict[str, Any]] = []
    saw_interpro_score_field = False

    recorded_by_accession: dict[str, list[dict[str, Any]]] = {}
    for row in recorded:
        if row.get("accession"):
            recorded_by_accession.setdefault(str(row["accession"]).upper(), []).append(row)
    local_hits_by_accession: dict[str, list[dict[str, Any]]] = {}
    for hit in hmm_scan.get("hits") or []:
        local_hits_by_accession.setdefault(str(hit.get("target") or "").upper(), []).append(hit)

    for key in protein_keys:
        interpro_record = interpro_by_accession.get(key)
        recorded_model_sources: dict[str, dict[str, Any]] = {}
        for row in recorded_by_accession.get(key, []):
            model = str(row.get("model") or "").strip().upper()
            if not model:
                continue
            record = recorded_model_sources.setdefault(
                model,
                {"scanned": True, "source": "recorded", "hits": [], "verdict": "unknown"},
            )
            record["hits"].append(row)
        for model, record in recorded_model_sources.items():
            evalues = [row["evalue"] for row in record["hits"] if row.get("evalue") is not None]
            best = min(evalues) if evalues else None
            record["evalue"] = best
            record["verdict"] = _verdict_from_evalue(best, present_evalue, weak_evalue) if best is not None else "unknown"
            record["locations"] = []

        interpro_model_sources: dict[str, dict[str, Any]] = {}
        for model, model_record in (interpro_record or {}).get("models", {}).items():
            locations = model_record.get("locations") or []
            if any(location.get("score_source") == "interpro_score_field" for location in locations):
                saw_interpro_score_field = True
            best = _best_location_evalue(locations)
            interpro_model_sources[model.upper()] = {
                "scanned": True,
                "source": "interpro",
                "hits": [model_record],
                "locations": locations,
                "evalue": best,
                "verdict": _verdict_from_evalue(best, present_evalue, weak_evalue),
                "entry_name": model_record.get("entry_name"),
                "database": model_record.get("database"),
                "release": model_record.get("release"),
            }

        local_model_sources: dict[str, dict[str, Any]] = {}
        for hit in local_hits_by_accession.get(key, []):
            model = str(hit.get("model") or "").strip().upper()
            if not model:
                continue
            record = local_model_sources.setdefault(
                model,
                {"scanned": True, "source": "local_hmm", "hits": [], "verdict": "unknown"},
            )
            record["hits"].append(hit)
        for model, record in local_model_sources.items():
            evalues = [hit["evalue"] for hit in record["hits"] if hit.get("evalue") is not None]
            best = min(evalues) if evalues else None
            record["evalue"] = best
            record["verdict"] = _verdict_from_evalue(best, present_evalue, weak_evalue) if best is not None else "absent"
            record["locations"] = []
            record["target_db_size"] = (record["hits"][0].get("target_db_size") if record["hits"] else hmm_scan.get("target_db_size"))

        # Which models to report: requested domains (always) plus every observed model.
        observed_models: list[str] = []
        for collection in (recorded_model_sources, interpro_model_sources, local_model_sources):
            for model in collection:
                if model not in observed_models:
                    observed_models.append(model)
        if requested_domains:
            models_to_report = list(requested_domains)
        else:
            models_to_report = list(observed_models)
            if hmm_scan.get("ran"):
                for model in hmm_scan.get("models") or []:
                    model_id = str(model.get("id") or "").strip().upper()
                    if model_id and model_id not in models_to_report:
                        models_to_report.append(model_id)

        domains_payload: dict[str, dict[str, Any]] = {}
        for model in models_to_report:
            sources: dict[str, dict[str, Any]] = {}
            if model in recorded_model_sources:
                sources["recorded"] = recorded_model_sources[model]
            if model in interpro_model_sources:
                sources["interpro"] = interpro_model_sources[model]
            if model in local_model_sources:
                sources["local_hmm"] = local_model_sources[model]
            if not sources:
                # Requested but not found by any source that actually ran.
                scanned_sources = []
                if recorded_by_accession.get(key):
                    scanned_sources.append("recorded")
                if interpro_record is not None:
                    scanned_sources.append("interpro")
                if hmm_scan.get("ran"):
                    scanned_sources.append("local_hmm")
                sources = {
                    name: {"scanned": True, "source": name, "hits": [], "locations": [], "evalue": None, "verdict": "absent"}
                    for name in scanned_sources
                }
            verdict, best_source = _merge_model_verdict(
                sources, present_evalue=present_evalue, weak_evalue=weak_evalue
            )
            primary = sources.get(best_source) if best_source else None
            evalue = None
            locations: list[Mapping[str, Any]] = []
            model_cov = None
            protein_length = None
            accession_display = next(
                (str(row.get("accession")) for row in recorded if str((row.get("accession") or "")).upper() == key),
                key,
            )
            if primary is not None:
                evalue = primary.get("evalue")
                if verdict == "absent":
                    evalue = _best_source_evalue(sources)
                locations = list(primary.get("locations") or [])
                first_hit = (primary.get("hits") or [None])[0]
                if isinstance(first_hit, Mapping):
                    model_cov = first_hit.get("model_cov")
                    protein_length = first_hit.get("protein_length")
                if verdict == "present" and isinstance(first_hit, Mapping):
                    model_cov = first_hit.get("model_cov") or model_cov
                    protein_length = first_hit.get("protein_length") or protein_length
            else:
                evalue = _best_source_evalue(sources)
            source_name = best_source or (sorted(sources)[0] if sources else "none")
            evidence = _evidence_string(
                model,
                verdict=verdict,
                source=source_name,
                evalue=evalue,
                protein_length=protein_length,
                model_cov=model_cov,
                accession=accession_display,
                report_evalue=report_evalue if source_name == "local_hmm" else None,
                locations=locations,
            )
            domain_absent = verdict == "absent"
            sources_summary = {
                name: {
                    "source": value.get("source"),
                    "scanned": value.get("scanned"),
                    "verdict": value.get("verdict"),
                    "evalue": value.get("evalue"),
                    "target_db_size": value.get("target_db_size"),
                    "database": value.get("database"),
                }
                for name, value in sources.items()
            }
            domains_payload[model] = {
                "model": model,
                "verdict": verdict,
                "evidence": evidence,
                "evalue": evalue,
                "score": (primary or {}).get("hits", [{}])[0].get("score") if (primary and primary.get("hits")) else None,
                "model_cov": model_cov,
                "protein_length": protein_length,
                "present_evalue_cutoff": present_evalue,
                "weak_evalue_cutoff": weak_evalue,
                "report_evalue": report_evalue if (source_name == "local_hmm" or "local_hmm" in sources) else None,
                "locations": util.json_safe(locations),
                "sources": sources_summary,
                "domain_absent": domain_absent,
                "source_used": source_name,
            }
            domain_counts[verdict] += 1
            explicitly_requested = model in requested_domains or not requested_domains
            if domain_absent and explicitly_requested:
                cutoff_parts = [f"present cutoff E<{util.fmt_evalue(present_evalue)}"]
                if "local_hmm" in sources:
                    cutoff_parts.append(f"local hmmsearch report threshold E<={util.fmt_evalue(report_evalue)}")
                if "interpro" in sources:
                    cutoff_parts.append("no annotation returned by InterPro")
                ctx.warn(
                    "DOMAIN_ABSENT",
                    f"{accession_display}: {model} domain absent ({'; '.join(cutoff_parts)}). "
                    "This is absence of annotation at the stated cutoffs, not proof of functional absence.",
                    context={
                        "accession": accession_display,
                        "model": model,
                        "present_evalue_cutoff": present_evalue,
                        "report_evalue": report_evalue,
                        "sources": sorted(sources),
                    },
                )

        domain_verdicts = {model: record["verdict"] for model, record in domains_payload.items()}
        arbiter = _arbiter(
            next(
                (str(row.get("accession")) for row in recorded if str((row.get("accession") or "")).upper() == key),
                key,
            ),
            sequence_evidence=sequence_evidence_input.get(key),
            domain_verdicts=domain_verdicts,
            requested_domains=requested_domains,
        )
        arbiter_rows.append(arbiter)
        if arbiter["conclusion"] == "domain_arbiter_present_without_sequence_hit":
            ctx.warn(
                "DOMAIN_EVIDENCE_OUTRANKS_SEQUENCE",
                f"{arbiter['accession']}: significant domain evidence exists without a detectable sequence hit; "
                "domain/family evidence outranks a single weak sequence line - report the protein as present but flag "
                "the divergence.",
                context={"accession": arbiter["accession"], "present_domains": arbiter.get("present_domains")},
            )
        elif arbiter["conclusion"] == "sequence_present_without_domain":
            ctx.warn(
                "DOMAIN_MISSING_WITH_SEQUENCE_HIT",
                f"{arbiter['accession']}: sequence hit without supporting domain; inspect for a domain false positive "
                "or a partial/divergent protein.",
                context={"accession": arbiter["accession"], "absent_domains": arbiter.get("absent_domains")},
            )
            ctx.review(
                "SINGLE_WEAK_LINE_EVIDENCE",
                f"{arbiter['accession']}: sequence-only evidence without the expected domain; manual review required.",
                context={"accession": arbiter["accession"]},
            )

        present_models = [model for model, record in domains_payload.items() if record["verdict"] == "present"]
        weak_models = [model for model, record in domains_payload.items() if record["verdict"] == "weak"]
        absent_models = [model for model, record in domains_payload.items() if record["verdict"] == "absent"]
        unknown_models = [model for model, record in domains_payload.items() if record["verdict"] == "unknown"]
        protein_payload = {
            "accession": arbiter["accession"],
            "available": interpro_record is not None or bool(recorded_by_accession.get(key)) or bool(local_hits_by_accession.get(key)),
            "entries": util.json_safe((interpro_record or {}).get("entries") or []),
            "n_entries": int((interpro_record or {}).get("n_entries") or 0),
            "release": (interpro_record or {}).get("release") or next(
                (row.get("release") for row in recorded if row.get("release")), None
            ),
            "url": (interpro_record or {}).get("url"),
            "sha256": (interpro_record or {}).get("sha256"),
            "domains": domains_payload,
            "domain_summary": {
                "verdicts": domain_verdicts,
                "present": present_models,
                "weak": weak_models,
                "absent": absent_models,
                "unknown": unknown_models,
                "domain_absent": absent_models,
                "present_evalue_cutoff": present_evalue,
                "weak_evalue_cutoff": weak_evalue,
                "report_evalue": report_evalue,
            },
            "arbiter": arbiter,
        }
        proteins.append(protein_payload)
        if args.get("out"):
            out_dir = util.ensure_dir(_resolve_path(ctx, str(args["out"])))
            output_path = out_dir / f"{util.slugify(arbiter['accession'], max_length=80, fallback='protein')}.domains.json"
            util.atomic_write_json(output_path, protein_payload)
            _record_local_file(ctx, output_path, kind="domain_record", notes={"accession": arbiter["accession"]})

    if saw_interpro_score_field:
        ctx.warn(
            "INTERPRO_SCORE_SEMANTICS",
            "Some InterPro locations expose only a 'score' field (for Pfam matches it behaves like an E-value); "
            "it was treated as an E-value for the verdict. Its magnitude is not directly comparable with a local "
            "hmmsearch E-value computed against a different target database.",
            context={"source": "interpro"},
        )
    if hmm_scan.get("ran") and (recorded or interpro_by_accession):
        ctx.warn_evalue_db_dependence(
            "mixed_domain_evidence",
            "Recorded/InterPro and local-hmmsearch E-values come from different target databases; compare verdicts, "
            "not raw E-values.",
        )

    if accessions and not proteins and interpro_errors:
        raise LifesciError(
            "domains could not evaluate any requested accession",
            code="DOMAIN_SCAN_FAILED",
            retriable=bool(first_interpro_error.retriable) if first_interpro_error else True,
            details={"errors": interpro_errors},
        )
    if accessions and interpro_errors and not interpro_by_accession and not recorded and not hmm_scan.get("ran"):
        raise LifesciError(
            "domains could not obtain evidence for any requested accession",
            code=first_interpro_error.code if first_interpro_error else "DOMAIN_SCAN_FAILED",
            retriable=bool(first_interpro_error.retriable) if first_interpro_error else True,
            details={"errors": interpro_errors},
        )
    releases = provenance.load_releases(ctx.cache_dir)
    # ---- mandatory E-value comparability warnings ----------------------
    # Recorded evidence rows may come from databases of very different sizes,
    # and the local HMM scan uses yet another target set. E-values are only
    # comparable within one fixed database+size, so say so explicitly.
    recorded_sizes = {
        size
        for size in (util.parse_int(row.get("target_db_size")) for row in recorded)
        if size is not None
    }
    recorded_databases = {
        str(row.get("database") or row.get("source"))
        for row in recorded
        if row.get("database") or row.get("source")
    }
    local_size = hmm_scan.get("target_db_size") if hmm_scan.get("ran") else None
    if len(recorded_sizes) > 1:
        ctx.warn_evalue_db_dependence(
            "recorded_domain_evidence",
            f"Recorded evidence rows were produced against different target-database sizes "
            f"({sorted(recorded_sizes)}); their E-values are not mutually comparable.",
        )
    elif recorded_sizes and local_size is not None and sorted(recorded_sizes)[0] != local_size:
        ctx.warn_evalue_db_dependence(
            "recorded+local_hmm",
            f"Recorded domain E-values come from a database of size {sorted(recorded_sizes)[0]} while the "
            f"local HMM scan searched {local_size} target sequence(s); the two E-value scales are not "
            "comparable.",
        )
    elif len(recorded_databases) > 1:
        ctx.warn_evalue_db_dependence(
            "recorded_domain_evidence",
            f"Recorded domain evidence mixes databases/sources {sorted(recorded_databases)}; E-values are "
            "not comparable across databases.",
        )
    if interpro_by_accession and (recorded or hmm_scan.get("ran")):
        ctx.warn(
            "EVALUE_SOURCES_MIXED",
            "Domain evidence mixes InterPro portal annotations (portal score field, model-level semantics) "
            "with local/recorded HMMER E-values; these numbers are produced by different methods and are not "
            "directly comparable. Prefer the HMMER E-value for significance statements and cite the source.",
            context={"interpro_accessions": sorted(interpro_by_accession)},
        )
    return {
        "dry_run": False,
        "requested": {
            "accessions": accessions,
            "domains": requested_domains,
            "rows_file": args.get("rows_file"),
            "local_hmm_scan": bool(hmm_scan.get("ran")),
        },
        "proteins": proteins,
        "domain_counts": domain_counts,
        "hmm_scan": util.json_safe(hmm_scan),
        "arbiter": {
            "rows": arbiter_rows,
            "n_domain_present": sum(1 for row in arbiter_rows if row.get("domain_present")),
            "n_domain_absent": sum(1 for row in arbiter_rows if row.get("domain_absent")),
            "n_conflicts": sum(
                1
                for row in arbiter_rows
                if row.get("conclusion") in {"sequence_present_without_domain", "domain_arbiter_present_without_sequence_hit"}
            ),
            "n_sequence_evidence": sum(1 for row in arbiter_rows if row.get("sequence_evidence_available")),
        },
        "resolved": {
            "cache_dir": str(ctx.cache_dir),
            "n_proteins": len(proteins),
            "present_evalue_cutoff": present_evalue,
            "weak_evalue_cutoff": weak_evalue,
            "report_evalue": report_evalue,
            "releases": releases,
            "interpro_errors": interpro_errors,
        },
        "errors": interpro_errors,
    }


def _best_source_evalue(sources: Mapping[str, Mapping[str, Any]]) -> float | None:
    values = [src.get("evalue") for src in sources.values() if src.get("evalue") is not None]
    return min(float(value) for value in values) if values else None


def _json_rows(path: Path) -> list[dict[str, Any]]:
    payload = util.read_json(path)
    if isinstance(payload, list):
        return [dict(item) for item in payload if isinstance(item, Mapping)]
    if isinstance(payload, Mapping) and isinstance(payload.get("hits"), list):
        return [dict(item) for item in payload["hits"] if isinstance(item, Mapping)]
    raise DataError(f"unsupported JSON rows format in {path}")
