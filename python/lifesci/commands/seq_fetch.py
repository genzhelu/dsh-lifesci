"""``lifesci.seq.fetch`` — UniProt sequence, entry metadata, and proteome fetches.

Wraps :mod:`lifesci.adapters.uniprot` so the caller gets the same cache, retry
policy, database-release pinning and fallback behaviour (REST proteome stream ->
FTP reference proteome) as every other lifesci fetch.  Sequences and entry
metadata are returned inline **and** written to ``out`` (large artefacts such as
reference-proteome FASTAs are only written to disk, never echoed into the
envelope).  Partial failures are reported in ``errors[]`` and never silently
dropped; the command only raises when *every* requested item failed.

The agent-facing description stresses the two publication-relevant caveats: the
UniProt release is recorded (E-values from different releases are not strictly
comparable) and reference-proteome FTP downloads are release-pinned only by the
``current_release`` path component, so the recorded release must travel with any
downstream result.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from .. import paths, provenance, util
from ..cache import Cache
from ..commands.base import command
from ..errors import InvalidInputError, LifesciError, ToolMissingError

INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "accessions": {
            "type": "array",
            "items": {"type": "string", "minLength": 1},
            "description": (
                "UniProt accessions to fetch. Each yields a FASTA sequence record; the same "
                "accession is also looked up as an entry when 'fetch_entries' is true. "
                "Unknown accessions are reported in errors[] without failing the other fetches."
            ),
        },
        "fetch_entries": {
            "type": "boolean",
            "default": False,
            "description": (
                "Also fetch and normalise the full UniProtKB entry (protein name, organism, "
                "length, cross-references, features, comments). Needed by downstream orthology "
                "and provenance reporting; costs one request per accession."
            ),
        },
        "proteomes": {
            "type": "array",
            "description": (
                "Reference proteomes to download. Provide either 'proteome_id' (UP...) or 'taxid' "
                "(resolved through the UniProt proteomes API). The REST stream is tried first; on "
                "HTTP 429/5xx/empty it falls back to the release-pinned EBI/FTP reference-proteome "
                "FASTA and warns (some non-reference proteomes are only available via REST)."
            ),
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "proteome_id": {
                        "type": "string",
                        "description": "UniProt proteome identifier, e.g. UP000005640.",
                    },
                    "taxid": {
                        "type": "integer",
                        "minimum": 1,
                        "description": "NCBI species taxid; resolved to a proteome id when 'proteome_id' is absent.",
                    },
                    "out_dir": {
                        "type": "string",
                        "description": "Directory for this proteome FASTA (defaults to the command 'out' directory).",
                    },
                },
            },
        },
        "out": {
            "type": "string",
            "description": (
                "Output directory for sequence FASTA files and downloaded proteomes "
                "(default: <cache-dir>/out/seq.fetch). Relative paths resolve against the working directory."
            ),
        },
        "write_sequences": {
            "type": "boolean",
            "default": True,
            "description": (
                "Write one <accession>.fasta file per fetched sequence into 'out'. Disable for a "
                "pure metadata query; the sequence is still returned inline in sequences[]."
            ),
        },
        "strict": {
            "type": "boolean",
            "default": False,
            "description": (
                "Raise on the first accession/proteome failure instead of collecting failures in "
                "errors[] and continuing. The command always raises when every requested item fails."
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
    base = _resolve_path(ctx, out) if out else Path(ctx.cache_dir) / "out" / "seq.fetch"
    util.ensure_dir(base)
    return base


def _record_local_file(ctx: Any, path: Path, *, kind: str, notes: Mapping[str, Any] | None = None) -> dict[str, Any]:
    log = getattr(ctx, "_provenance_log", None)
    if log is None:
        log = provenance.ProvenanceLog(getattr(ctx, "log_path", None))
        ctx._provenance_log = log
    entry = log.local_file(
        path=str(path),
        sha256=util.sha256_file(path),
        bytes_=path.stat().st_size,
        kind=kind,
        notes=dict(notes or {}),
    )
    ctx.provenance.append(entry)
    return entry


def _plan(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
    accessions = [str(a) for a in (args.get("accessions") or [])]
    proteomes = [dict(p) for p in (args.get("proteomes") or [])]
    out_dir = _out_dir(ctx, args.get("out")) if (args.get("out") or args.get("write_sequences", True)) else None
    planned: list[dict[str, Any]] = []
    for accession in accessions:
        planned.append(
            {
                "method": "GET",
                "url": f"https://rest.uniprot.org/uniprotkb/{accession}.fasta",
                "purpose": "uniprot_sequence",
                "accession": accession,
            }
        )
        if args.get("fetch_entries"):
            planned.append(
                {
                    "method": "GET",
                    "url": f"https://rest.uniprot.org/uniprotkb/{accession}.json",
                    "purpose": "uniprot_entry",
                    "accession": accession,
                }
            )
    for proteome in proteomes:
        if proteome.get("proteome_id"):
            planned.append(
                {
                    "method": "GET",
                    "url": "https://rest.uniprot.org/uniprotkb/stream",
                    "params": {"query": f"proteome:{proteome['proteome_id']}", "format": "fasta", "compressed": "true"},
                    "purpose": "proteome_rest_stream",
                    "proteome_id": proteome["proteome_id"],
                }
            )
        else:
            planned.append(
                {
                    "method": "GET",
                    "url": "https://rest.uniprot.org/proteomes/search",
                    "params": {"query": f"organism_id:{proteome.get('taxid')}", "format": "json"},
                    "purpose": "proteome_id_resolution",
                    "taxid": proteome.get("taxid"),
                }
            )
    return {
        "command": "seq.fetch",
        "dry_run": True,
        "requested": {"accessions": accessions, "proteomes": proteomes, "fetch_entries": bool(args.get("fetch_entries"))},
        "planned_requests": planned,
        "entries": [],
        "sequences": [],
        "proteomes": [],
        "resolved": {
            "dry_run": True,
            "out_dir": str(out_dir) if out_dir else None,
            "cache_dir": str(ctx.cache_dir),
            "n_accessions_requested": len(accessions),
            "n_proteomes_requested": len(proteomes),
        },
        "errors": [],
    }


@command(
    name="seq.fetch",
    title="Fetch UniProt sequences, entries and proteomes",
    summary="Fetch UniProtKB FASTA sequences, normalised entry metadata, and reference proteomes with release pinning.",
    description=(
        "Fetch UniProt data reproducibly: per-accession FASTA sequences (returned inline and written "
        "to 'out'), optional normalised entry metadata (protein name, organism/taxid, cross-references "
        "to PANTHER/eggNOG/OrthoDB/OMA/AlphaFold, features, comments, references), and reference-proteome "
        "FASTAs. The UniProt release is recorded for every payload; REST proteome streaming falls back to "
        "the release-pinned FTP/EBI reference-proteome FASTA when rate-limited or empty (a warning tells "
        "you which path was used; non-reference proteomes may not exist on FTP). Failures for individual "
        "accessions are returned in errors[] while the remaining fetches continue. Caveats: protein "
        "E-values and BLAST/HMMER counts computed against different UniProt releases or proteome sets are "
        "not strictly comparable - always carry the recorded release forward."
    ),
    input_schema=INPUT_SCHEMA,
    output_schema="seq.fetch.schema.json",
    category="sequence",
    timeout_ms=600_000,
    danger="network",
    examples=(
        {"accessions": ["P69905"], "fetch_entries": True},
        {"proteomes": [{"proteome_id": "UP000005640", "taxid": 9606}]},
    ),
)
def run(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
    """Fetch UniProt sequences/entries/proteomes and return a provenance-rich payload."""
    ctx.validate()
    accessions = [str(item).strip() for item in (args.get("accessions") or []) if str(item).strip()]
    proteome_requests = [dict(item) for item in (args.get("proteomes") or [])]
    if not accessions and not proteome_requests:
        raise InvalidInputError("provide at least one of 'accessions' or 'proteomes'")
    for index, request in enumerate(proteome_requests):
        if not request.get("proteome_id") and not request.get("taxid"):
            raise InvalidInputError(f"proteomes[{index}] requires 'proteome_id' or 'taxid'")
    if ctx.dry_run:
        return _plan(args, ctx)

    try:
        from ..adapters import uniprot
    except ImportError as err:  # pragma: no cover - parallel workstream ordering
        raise ToolMissingError(
            "lifesci.adapters.uniprot is not available; seq.fetch cannot run",
            details={"error": str(err)},
            fallbacks=["retry after the adapters workstream lands", "use lifesci.http for the raw request"],
        ) from err

    out_dir = _out_dir(ctx, args.get("out"))
    write_sequences = bool(args.get("write_sequences", True))
    strict = bool(args.get("strict", False))
    entries: list[dict[str, Any]] = []
    sequences: list[dict[str, Any]] = []
    proteomes: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    first_error: LifesciError | None = None

    for accession in accessions:
        try:
            fasta = uniprot.fetch_fasta(ctx, accession)
        except LifesciError as err:
            errors.append({"accession": accession, "code": err.code, "message": err.message})
            first_error = first_error or err
            if strict:
                raise
            continue
        sequence = str(fasta.get("sequence") or "")
        record = {
            "accession": fasta.get("accession") or accession,
            "header": fasta.get("header"),
            "sequence": sequence,
            "length": int(fasta.get("length") or len(sequence)),
            "release": fasta.get("release"),
            "url": fasta.get("url"),
            "sha256": fasta.get("sha256"),
            "path": None,
            "file_sha256": None,
        }
        if write_sequences:
            fasta_path = out_dir / f"{util.slugify(str(record['accession']), max_length=80, fallback='sequence')}.fasta"
            util.write_fasta(fasta_path, [(str(record["header"] or record["accession"]), sequence)])
            record["path"] = str(fasta_path)
            record["file_sha256"] = util.sha256_file(fasta_path)
            _record_local_file(
                ctx,
                fasta_path,
                kind="sequence_fasta",
                notes={"accession": record["accession"], "url": record["url"]},
            )
        sequences.append(record)
        if not record["release"]:
            ctx.warn(
                "DATABASE_RELEASE_UNKNOWN",
                "UniProt did not expose a release header for this sequence; downstream E-values cannot be release-pinned.",
                context={"accession": record["accession"], "url": record["url"]},
            )

        if args.get("fetch_entries"):
            try:
                entry = uniprot.fetch_entry(ctx, accession)
            except LifesciError as err:
                errors.append({"accession": accession, "scope": "entry", "code": err.code, "message": err.message})
                if strict:
                    raise
                continue
            entries.append(
                {
                    "accession": entry.get("accession") or accession,
                    "parsed": entry.get("parsed"),
                    "release": entry.get("release"),
                    "url": entry.get("url"),
                    "sha256": entry.get("sha256"),
                }
            )

    if strict and errors:
        raise LifesciError(
            "seq.fetch failed for at least one requested item",
            code="SEQ_FETCH_FAILED",
            details={"errors": errors},
        )

    for request in proteome_requests:
        try:
            proteome_id = request.get("proteome_id")
            taxid = request.get("taxid")
            resolved = None
            if not proteome_id:
                resolved = uniprot.proteome_id(ctx, int(taxid))
                proteome_id = resolved.get("proteome_id")
                if not proteome_id:
                    raise LifesciError(
                        f"UniProt did not return a proteome identifier for taxid {taxid}",
                        code="PROTEOME_NOT_FOUND",
                        details={"taxid": taxid},
                    )
            target_dir = _resolve_path(ctx, request["out_dir"]) if request.get("out_dir") else out_dir
            util.ensure_dir(target_dir)
            proteome = uniprot.fetch_proteome(
                ctx,
                proteome_id=str(proteome_id),
                taxid=int(taxid) if taxid else None,
                out_dir=target_dir,
            )
            record = {
                "proteome_id": proteome_id,
                "taxid": taxid,
                "name": (resolved or {}).get("name") or proteome.get("name"),
                "path": proteome.get("path"),
                "sha256": proteome.get("sha256"),
                "bytes": proteome.get("bytes"),
                "n_sequences": proteome.get("n_sequences"),
                "release": proteome.get("release"),
                "source": proteome.get("source"),
                "reference_proteome": proteome.get("reference_proteome"),
                "url": proteome.get("url"),
            }
            proteomes.append(record)
            if record.get("path"):
                _record_local_file(
                    ctx,
                    Path(str(record["path"])),
                    kind="proteome_fasta",
                    notes={"proteome_id": proteome_id, "taxid": taxid, "source": record.get("source")},
                )
        except LifesciError as err:
            errors.append(
                {
                    "proteome_id": request.get("proteome_id"),
                    "taxid": request.get("taxid"),
                    "code": err.code,
                    "message": err.message,
                }
            )
            first_error = first_error or err
            if strict:
                raise

    requested = len(accessions) + len(proteome_requests)
    succeeded = len(sequences) + len(proteomes)
    if requested and succeeded == 0:
        if first_error is not None:
            raise first_error
        raise LifesciError(
            "seq.fetch produced no results for any requested item",
            code="SEQ_FETCH_FAILED",
            details={"errors": errors},
        )
    if errors:
        ctx.warn(
            "SEQ_FETCH_PARTIAL",
            f"{len(errors)} of {requested} requested item(s) failed but the remaining fetches succeeded; "
            "inspect errors[] before concluding anything from this payload.",
            context={"errors": errors[:10], "n_errors": len(errors)},
        )

    cache = Cache(ctx.cache_dir, ttl_default=ctx.cache_ttl)
    releases = provenance.load_releases(ctx.cache_dir)
    return {
        "dry_run": False,
        "requested": {
            "accessions": accessions,
            "proteomes": proteome_requests,
            "fetch_entries": bool(args.get("fetch_entries")),
        },
        "entries": entries,
        "sequences": sequences,
        "proteomes": proteomes,
        "resolved": {
            "cache_dir": str(ctx.cache_dir),
            "out_dir": str(out_dir),
            "n_accessions_requested": len(accessions),
            "n_sequences": len(sequences),
            "n_entries": len(entries),
            "n_proteomes": len(proteomes),
            "n_errors": len(errors),
            "releases": releases,
            "cache": cache.stats(),
            "schemas_dir": [str(p) for p in paths.SCHEMA_DIRS],
        },
        "errors": errors,
    }
