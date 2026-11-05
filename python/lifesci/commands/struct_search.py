"""``lifesci.struct.search`` — controlled Foldseek panel, Foldseek web API, or recorded-table replay.

Three modes:

``panel``
    Controlled local Foldseek search (static binary through
    :mod:`lifesci.adapters.foldseek`) of query structures against a target
    panel, plus **negative-control calibration**: unrelated proteins are searched
    against the same panel and the highest control TM defines the background.
    Hits are annotated as interpretable only above ``control_max_tm + margin``;
    domain/family evidence remains the arbiter even then.

``web``
    Database-scale search through the Foldseek web API (AFDB50 / AFDB-SwissProt /
    PDB100). Server TM is query-length-normalised and each database returns a
    capped number of alignments (~1000), so absence from the returned list is not
    proof of absence. Reciprocal searches are recommended before excluding a
    homologue.

``from_table``
    Offline replay of recorded Foldseek hit tables (web-style ``hits_*.tsv`` or
    the header-less local-panel format), with optional **sanity-anchor checks**:
    anchors supplied by the caller via ``expected_anchors`` must be recovered,
    and an explicit ``SANITY_ANCHOR_MISSING`` warning is raised otherwise.

Every mode always warns that TM-scores are query-normalised
(``TM_SCORE_QUERY_NORMALISED``), that result lists are capped
(``TOP_HIT_LIST_CAPPED``), and that E-values are database-size dependent and not
comparable across databases (``EVALUE_NOT_COMPARABLE_ACROSS_DATABASES``).
Structural similarity is evidence for "closest structural neighbour", not
automatic orthology.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import Any, Iterable, Mapping

from .. import provenance, util
from ..commands.base import command
from ..errors import InvalidInputError, LifesciError, ToolMissingError

_UNIPROT_ACC_TOKEN = re.compile(
    r"(?<![A-Za-z0-9])([OPQ][0-9][A-Z0-9]{3}[0-9]|[A-NR-Z][0-9](?:[A-Z][A-Z0-9]{2}[0-9]){1,2})(?![A-Za-z0-9])"
)
_AFDB_TARGET = re.compile(r"^AF-([A-Z0-9]+)-F\d+-model_v\d+")
_WEB_COLUMNS = [
    "rank",
    "db",
    "target_accession",
    "target_raw",
    "organism",
    "taxid",
    "tm_score",
    "prob",
    "evalue",
    "seqid",
    "alnlen",
    "qlen",
    "tlen",
    "qstart",
    "qend",
    "tstart",
    "tend",
    "description",
]
_PANEL_COLUMNS = [
    "query",
    "target",
    "fident",
    "alntmscore",
    "qtmscore",
    "ttmscore",
    "evalue",
    "qcov",
    "tcov",
    "alnlen",
]

#: There are no built-in sanity anchors: anchors are supplied explicitly via
#: ``expected_anchors`` so the library carries no project-specific expectations.
DEFAULT_ANCHORS: dict[str, list[dict[str, Any]]] = {}

INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["mode"],
    "properties": {
        "mode": {
            "type": "string",
            "enum": ["panel", "web", "from_table"],
            "description": (
                "Search backend: 'panel' = local Foldseek on a controlled panel with negative controls; "
                "'web' = Foldseek web API against AFDB50/SwissProt/PDB100; 'from_table' = offline replay "
                "of recorded hit tables with sanity-anchor checks."
            ),
        },
        "queries": {
            "type": "array",
            "description": "panel mode: query structures as {\"id\", \"path\"} objects or path strings.",
            "items": {
                "oneOf": [
                    {"type": "string"},
                    {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["path"],
                        "properties": {
                            "id": {"type": "string", "description": "Query label (defaults to the file stem)."},
                            "path": {"type": "string", "description": "Local structure file (PDB/mmCIF)."},
                        },
                    },
                ]
            },
        },
        "targets": {
            "description": (
                "panel mode: the target panel — a directory path, {\"dir\": path}, or a list of "
                "structure paths. Candidate homologues and negative controls should be in the panel."
            ),
            "oneOf": [
                {"type": "string"},
                {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {"dir": {"type": "string"}},
                },
                {"type": "array", "items": {"type": "string"}},
            ],
        },
        "controls": {
            "type": "array",
            "description": (
                "panel mode: negative-control structures of the same fold class (unrelated proteins). "
                "They are searched against the same panel; the maximum control TM is the background and "
                "hits must exceed control_max_tm + margin to be interpretable."
            ),
            "items": {
                "oneOf": [
                    {"type": "string"},
                    {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["path"],
                        "properties": {
                            "id": {"type": "string"},
                            "path": {"type": "string"},
                        },
                    },
                ]
            },
        },
        "margin": {
            "type": "number",
            "minimum": 0,
            "maximum": 1,
            "default": 0.05,
            "description": "panel mode: margin above the control maximum before a TM score is interpretable.",
        },
        "min_control_coverage": {
            "type": "number",
            "minimum": 0,
            "maximum": 1,
            "default": 0.3,
            "description": (
                "panel mode: minimum query coverage (qcov) for a negative-control alignment to count towards "
                "the background. Foldseek can give a high alignment TM for a 10-residue fragment; such "
                "fragments are not a meaningful background."
            ),
        },
        "min_hit_coverage": {
            "type": "number",
            "minimum": 0,
            "maximum": 1,
            "default": 0.3,
            "description": (
                "panel mode: minimum query coverage (qcov) for a query hit to be interpretable. Hits below "
                "this are reported but flagged as low-coverage, not called homologous."
            ),
        },
        "fmt": {
            "type": "string",
            "default": "query,target,alntmscore,qtmscore,ttmscore,lddt,evalue,qcov,tcov,alnlen",
            "description": "panel mode: Foldseek --format-output string.",
        },
        "e": {
            "type": "number",
            "minimum": 0,
            "default": 100.0,
            "description": "panel mode: Foldseek E-value reporting threshold.",
        },
        "threads": {
            "type": "integer",
            "minimum": 1,
            "maximum": 64,
            "default": 4,
            "description": "panel mode: Foldseek threads.",
        },
        "binary": {
            "type": "string",
            "description": "Explicit Foldseek binary path (default: $LIFESCI_FOLDSEEK, tools/bin, then PATH).",
        },
        "pdb_paths": {
            "type": "array",
            "items": {"type": "string"},
            "description": "web mode: local PDB model files uploaded as queries (only PDB upload works; FASTA/JSON fail).",
        },
        "pdb_accessions": {
            "type": "array",
            "items": {"type": "string"},
            "description": "web mode: UniProt accessions whose AlphaFold DB PDB models are used as queries.",
        },
        "databases": {
            "type": "array",
            "items": {"type": "string", "enum": ["afdb50", "afdb-swissprot", "pdb100"]},
            "default": ["afdb50"],
            "description": "web mode: Foldseek databases to search. AFDB50 is the global sensitive AFDB search.",
        },
        "taxfilter": {
            "type": "string",
            "description": "web mode: optional NCBI taxonomy filter (applied only when set).",
        },
        "poll_interval": {
            "type": "number",
            "minimum": 1,
            "default": 5,
            "description": "web mode: seconds between ticket polls.",
        },
        "max_wait": {
            "type": "number",
            "minimum": 10,
            "default": 600,
            "description": "web mode: maximum seconds to wait for a Foldseek web job.",
        },
        "tables": {
            "description": "from_table mode: recorded hit table(s) — path strings or {\"path\", \"query\", \"database\", \"format\"}.",
            "oneOf": [
                {"type": "string"},
                {"type": "array", "items": {}},
            ],
        },
        "hits": {
            "type": "array",
            "items": {"type": "object"},
            "description": (
                "from_table mode: inline recorded hits instead of files. Each row may carry query/db/rank/"
                "target_accession/tm_score/prob/evalue/... (web format) or query/target/alntmscore/... (panel format)."
            ),
        },
        "expected_anchors": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "query": {"type": "string", "description": "Query label/accession this anchor applies to (default: any query)."},
                    "target_accession": {"type": "string", "description": "Accession that must be recovered."},
                    "family_hint": {
                        "type": "string",
                        "description": "Alternative to target_accession: '|'-separated substrings that must appear in a top hit's description/organism.",
                    },
                    "min_tm": {"type": "number", "description": "Minimum TM-score the hit must reach."},
                    "min_prob": {"type": "number", "description": "Minimum Foldseek homology probability the hit must reach."},
                    "max_rank": {"type": "integer", "minimum": 1, "description": "The hit must be found at or above this rank."},
                    "required": {"type": "boolean", "default": True, "description": "Anchor failure is reported as SANITY_ANCHOR_MISSING when required."},
                },
            },
            "description": (
                "Anchors that must be recovered for the search to be considered sane. There are no "
                "built-in anchors: supply the expected target(s) explicitly."
            ),
        },
        "out": {
            "type": "string",
            "description": "Optional directory: combined hits TSV + search summary JSON + anchor report are written here.",
        },
        "db_metadata": {
            "type": "object",
            "description": "Optional extra database/release metadata to record with the result.",
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


def _query_accession(label: str) -> str | None:
    match = _UNIPROT_ACC_TOKEN.search(str(label or ""))
    return match.group(1) if match else None


def _plan(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
    mode = str(args.get("mode"))
    planned: list[dict[str, Any]] = []
    if mode == "panel":
        planned.append(
            {
                "tool": "foldseek.easy-search",
                "purpose": "query_panel_search",
                "params": {"e": args.get("e", 100.0), "threads": args.get("threads", 4), "fmt": args.get("fmt")},
            }
        )
        if args.get("controls"):
            planned.append({"tool": "foldseek.easy-search", "purpose": "negative_control_calibration"})
        planned.append({"purpose": "read_target_panel", "targets": args.get("targets")})
    elif mode == "web":
        for database in args.get("databases") or ["afdb50"]:
            planned.append(
                {
                    "method": "POST",
                    "url": "https://search.foldseek.com/api/ticket",
                    "purpose": "foldseek_web_submit",
                    "database": database,
                    "note": "multipart PDB upload only; JSON body returns HTTP 400 and FASTA queries end ERROR",
                }
            )
        planned.append({"method": "GET", "url": "https://search.foldseek.com/api/ticket/<ticket>", "purpose": "poll"})
        planned.append({"method": "GET", "url": "https://search.foldseek.com/api/result/<ticket>/0", "purpose": "fetch_result"})
    else:
        for table in _normalise_table_specs(args.get("tables")):
            planned.append({"purpose": "read_recorded_foldseek_table", "path": str(table.get("path"))})
    return {
        "command": "struct.search",
        "dry_run": True,
        "mode": mode,
        "planned_commands": planned,
        "queries": [],
        "hits": {},
        "n_hits": 0,
        "panel_controls": None,
        "background": {"available": False, "dry_run": True},
        "anchors": {"checked": 0, "passed": 0, "failed": 0, "rows": []},
        "db_metadata": {"databases": list(args.get("databases") or []), "dry_run": True},
        "cross_database_evalue_checks": [],
        "interpretation": {"n_above_background": 0, "n_below_background": 0},
        "resolved": {"dry_run": True, "cache_dir": str(ctx.cache_dir)},
    }


def _normalise_structure_specs(value: Any, *, default_id_prefix: str = "item") -> list[dict[str, str]]:
    specs: list[dict[str, str]] = []
    for index, item in enumerate(value or []):
        if isinstance(item, Mapping):
            path = item.get("path")
            if not path:
                raise InvalidInputError(f"structure entry {index} is missing 'path'")
            specs.append({"id": str(item.get("id") or Path(str(path)).stem), "path": str(path)})
        elif isinstance(item, str):
            specs.append({"id": Path(item).stem or f"{default_id_prefix}{index}", "path": item})
        else:
            raise InvalidInputError(f"unsupported structure entry {index}: {type(item).__name__}")
    return specs


def _prepare_structure_dir(ctx: Any, paths: Iterable[Path], *, name: str) -> Path:
    """Collect explicit structure paths into one directory for Foldseek search."""
    target_dir = util.ensure_dir(Path(ctx.cache_dir) / "tmp" / name)
    for path in paths:
        if not path.is_file():
            raise InvalidInputError(f"structure file not found: {path}")
        destination = target_dir / path.name
        if destination.exists() and destination.stat().st_size == path.stat().st_size:
            continue
        shutil.copyfile(path, destination)
    return target_dir


# ---------------------------------------------------------------------------
# recorded table parsing
# ---------------------------------------------------------------------------


def _normalise_table_specs(value: Any) -> list[dict[str, Any]]:
    specs: list[dict[str, Any]] = []
    if value is None:
        return specs
    items = value if isinstance(value, list) else [value]
    for index, item in enumerate(items):
        if isinstance(item, str):
            specs.append({"path": item})
        elif isinstance(item, Mapping):
            if not item.get("path"):
                raise InvalidInputError(f"tables[{index}] is missing 'path'")
            specs.append(dict(item))
        else:
            raise InvalidInputError(f"unsupported tables[{index}]: {type(item).__name__}")
    return specs


def _read_table(path: Path) -> list[dict[str, str]]:
    text = util.read_text(path)
    lines = [line for line in text.splitlines() if line.strip() and not line.startswith("#")]
    if not lines:
        return []
    header = lines[0].split("\t")
    if "target_accession" in header or "rank" in header or "target_raw" in header:
        return util.read_tsv(path)
    # Header-less local-panel format: query, target, fident, alntmscore, qtmscore,
    # ttmscore, evalue, qcov, tcov, alnlen (the standard foldseek cmp.tsv layout).
    rows: list[dict[str, str]] = []
    for lineno, line in enumerate(lines, 2):
        fields = line.split("\t")
        if len(fields) < 2:
            raise InvalidInputError(f"{path}:{lineno}: expected at least 2 columns in a panel table")
        if len(fields) == len(_PANEL_COLUMNS):
            rows.append(dict(zip(_PANEL_COLUMNS, fields)))
        elif len(fields) == 9:  # query,target + 7 numeric columns (older m8 layout)
            rows.append(
                dict(
                    zip(
                        ["query", "target", "fident", "alntmscore", "qtmscore", "ttmscore", "evalue", "qcov", "tcov"],
                        fields,
                    )
                )
            )
        else:
            raise InvalidInputError(
                f"{path}:{lineno}: unsupported panel table layout ({len(fields)} columns); expected header or "
                f"{len(_PANEL_COLUMNS)} columns"
            )
    return rows


def _target_accession_from_label(label: str) -> str | None:
    match = _AFDB_TARGET.match(str(label))
    if match:
        return match.group(1)
    match = _UNIPROT_ACC_TOKEN.search(str(label))
    if match:
        return match.group(1)
    token = str(label).split()[0]
    if "_" in token:
        candidate = token.rsplit("_", 1)[-1]
        if re.fullmatch(r"[A-Z0-9]{6,10}", candidate):
            return candidate
    return None


def _normalise_web_hit(row: Mapping[str, Any], *, database: str, query: str) -> dict[str, Any]:
    target_raw = str(row.get("target_raw") or row.get("target") or "")
    target_accession = row.get("target_accession") or _target_accession_from_label(target_raw)
    return {
        "query": query,
        "db": str(row.get("db") or database),
        "rank": util.parse_int(row.get("rank")),
        "target_accession": str(target_accession) if target_accession else None,
        "target_raw": target_raw,
        "organism": row.get("organism"),
        "taxid": util.parse_int(row.get("taxid")),
        "tm_score": util.parse_float(row.get("tm_score")),
        "prob": util.parse_float(row.get("prob")),
        "evalue": util.parse_float(row.get("evalue")),
        "seqid": util.parse_float(row.get("seqid")),
        "alnlen": util.parse_int(row.get("alnlen")),
        "qlen": util.parse_int(row.get("qlen")),
        "tlen": util.parse_int(row.get("tlen")),
        "qstart": util.parse_int(row.get("qstart")),
        "qend": util.parse_int(row.get("qend")),
        "tstart": util.parse_int(row.get("tstart")),
        "tend": util.parse_int(row.get("tend")),
        "description": row.get("description"),
        "source": "recorded_table",
    }


def _normalise_panel_hit(row: Mapping[str, Any], *, query: str, rank: int, database: str) -> dict[str, Any]:
    target_raw = str(row.get("target") or "")
    return {
        "query": query,
        "db": str(row.get("db") or database),
        "rank": rank,
        "target_accession": _target_accession_from_label(target_raw),
        "target_raw": target_raw,
        "organism": row.get("organism"),
        "taxid": util.parse_int(row.get("taxid")),
        "tm_score": util.parse_float(row.get("alntmscore")),
        "alntmscore": util.parse_float(row.get("alntmscore")),
        "qtmscore": util.parse_float(row.get("qtmscore")),
        "ttmscore": util.parse_float(row.get("ttmscore")),
        "fident": util.parse_float(row.get("fident")),
        "lddt": util.parse_float(row.get("lddt")),
        "prob": util.parse_float(row.get("prob")),
        "evalue": util.parse_float(row.get("evalue")),
        "seqid": util.parse_float(row.get("seqid") or row.get("fident")),
        "alnlen": util.parse_int(row.get("alnlen")),
        "qlen": util.parse_int(row.get("qlen")),
        "tlen": util.parse_int(row.get("tlen")),
        "qcov": util.parse_float(row.get("qcov")),
        "tcov": util.parse_float(row.get("tcov")),
        "description": row.get("description"),
        "source": "recorded_table",
    }


def _replay_tables(ctx: Any, args: Mapping[str, Any]) -> dict[str, Any]:
    """Offline replay of recorded Foldseek hit tables (web TSV and/or panel TSV)."""
    hits_by_query: dict[str, list[dict[str, Any]]] = {}
    tables_meta: list[dict[str, Any]] = []
    databases: list[str] = []
    inline_hits = [dict(row) for row in (args.get("hits") or [])]
    table_specs = _normalise_table_specs(args.get("tables"))
    if not table_specs and not inline_hits:
        raise InvalidInputError("from_table mode needs 'tables' (recorded TSV paths) or inline 'hits'")

    for spec in table_specs:
        path = _resolve_path(ctx, str(spec["path"]))
        if not path.is_file():
            raise InvalidInputError(f"recorded table not found: {path}")
        _record_local_file(ctx, path, kind="foldseek_table", notes={"command": "struct.search"})
        rows = _read_table(path)
        if not rows:
            continue
        first = rows[0]
        is_web = "target_accession" in first or "target_raw" in first or "rank" in first
        declared_db = spec.get("database")
        if is_web:
            default_db = str(declared_db or first.get("db") or "afdb50")
            for row in rows:
                database = str(declared_db or row.get("db") or default_db)
                query = str(spec.get("query") or row.get("query") or "")
                if not query:
                    query = _query_accession(path.stem) or path.stem
                hit = _normalise_web_hit(row, database=database, query=query)
                hits_by_query.setdefault(query, []).append(hit)
                if database not in databases:
                    databases.append(database)
            tables_meta.append({"path": str(path), "format": "web_tsv", "database": default_db, "n_rows": len(rows)})
        else:
            default_db = str(declared_db or "local_panel")
            panel_rank: dict[str, int] = {}
            for row in rows:
                query = str(spec.get("query") or row.get("query") or "")
                if not query:
                    continue
                if spec.get("query"):
                    query = str(spec["query"])
                panel_rank[query] = panel_rank.get(query, 0) + 1
                hit = _normalise_panel_hit(row, query=query, rank=panel_rank[query], database=default_db)
                hits_by_query.setdefault(query, []).append(hit)
                if default_db not in databases:
                    databases.append(default_db)
            tables_meta.append({"path": str(path), "format": "panel_tsv", "database": default_db, "n_rows": len(rows)})

    if inline_hits:
        for index, row in enumerate(inline_hits):
            query = str(row.get("query") or _query_accession(str(row.get("target") or "")) or "inline")
            database = str(row.get("db") or "inline")
            if "alntmscore" in row or ("tm_score" not in row and "rank" not in row):
                hit = _normalise_panel_hit(row, query=query, rank=index + 1, database=database)
            else:
                hit = _normalise_web_hit(row, database=database, query=query)
            hits_by_query.setdefault(query, []).append(hit)
            if database not in databases:
                databases.append(database)
        tables_meta.append({"path": None, "format": "inline", "database": databases[0] if databases else "inline", "n_rows": len(inline_hits)})

    for query, hits in hits_by_query.items():
        web_hits = [hit for hit in hits if hit.get("rank") is not None and hit.get("tm_score") == hit.get("tm_score")]
        if any(hit.get("db") not in {"local_panel", "inline"} for hit in hits):
            hits.sort(key=lambda hit: (hit.get("rank") is None, hit.get("rank") or 0))
        else:
            hits.sort(key=lambda hit: (hit.get("evalue") is None, hit.get("evalue") or 1e300, hit.get("target_raw") or ""))
            for rank, hit in enumerate(hits, 1):
                hit["rank"] = rank
    queries = [
        {"id": query, "accession": _query_accession(query), "source": "recorded_table"}
        for query in sorted(hits_by_query)
    ]
    return {
        "queries": queries,
        "hits": {query: hits_by_query[query] for query in sorted(hits_by_query)},
        "tables": tables_meta,
        "databases": databases or ["unknown"],
    }


# ---------------------------------------------------------------------------
# anchor checking
# ---------------------------------------------------------------------------


def _verify_anchors(
    queries: list[dict[str, Any]],
    hits_by_query: Mapping[str, list[Mapping[str, Any]]],
    expected_anchors: list[Mapping[str, Any]] | None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Check caller-supplied sanity anchors and return ``(anchor_payload, failures)``.

    Anchors are **explicit only**: nothing is checked unless the caller passed
    ``expected_anchors``, so no project-specific expectation is baked in.
    """
    rows: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    any_query_has_anchor = False
    for query in queries:
        query_id = str(query.get("id") or "")
        accession = str(query.get("accession") or _query_accession(query_id) or "")
        hits = list(hits_by_query.get(query_id) or [])
        anchor_defs = [dict(anchor) for anchor in (expected_anchors or [])]
        if not anchor_defs:
            continue
        any_query_has_anchor = True
        for anchor in anchor_defs:
            anchor_query = anchor.get("query")
            if anchor_query and str(anchor_query) not in {query_id, accession}:
                continue
            target_accession = anchor.get("target_accession")
            family_hint = anchor.get("family_hint")
            min_tm = util.parse_float(anchor.get("min_tm"))
            min_prob = util.parse_float(anchor.get("min_prob"))
            max_rank = util.parse_int(anchor.get("max_rank"))
            required = bool(anchor.get("required", True))
            matched: Mapping[str, Any] | None = None
            reasons: list[str] = []
            candidates = []
            for hit in hits:
                if target_accession and str(hit.get("target_accession") or "").upper() != str(target_accession).upper():
                    continue
                if family_hint and not target_accession:
                    haystack = " ".join(
                        str(hit.get(key) or "") for key in ("description", "target_raw", "organism")
                    ).lower()
                    hints = [part.strip().lower() for part in str(family_hint).split("|") if part.strip()]
                    if not any(hint in haystack for hint in hints):
                        continue
                if max_rank is not None and (hit.get("rank") is not None and int(hit["rank"]) > max_rank):
                    continue
                candidates.append(hit)
            if candidates:
                matched = min(candidates, key=lambda hit: hit.get("rank") or 10**9)
                rank = matched.get("rank")
                tm = util.parse_float(matched.get("tm_score"))
                prob = util.parse_float(matched.get("prob"))
                if min_tm is not None and (tm is None or tm + 1e-9 < min_tm):
                    reasons.append(f"tm_score {tm} < required {min_tm}")
                if min_prob is not None and (prob is None or prob + 1e-9 < min_prob):
                    reasons.append(f"prob {prob} < required {min_prob}")
                if max_rank is not None and rank is not None and int(rank) > max_rank:
                    reasons.append(f"rank {rank} > max {max_rank}")
            else:
                if target_accession:
                    reasons.append(f"target {target_accession} not recovered")
                else:
                    reasons.append(f"no hit matching family_hint {family_hint!r} within max_rank {max_rank}")
            passed = bool(matched) and not reasons
            row = {
                "query": query_id,
                "query_accession": accession or None,
                "target_accession": target_accession,
                "family_hint": family_hint,
                "min_tm": min_tm,
                "min_prob": min_prob,
                "max_rank": max_rank,
                "required": required,
                "found": bool(matched),
                "rank": (matched or {}).get("rank") if matched else None,
                "tm_score": util.parse_float((matched or {}).get("tm_score")) if matched else None,
                "prob": util.parse_float((matched or {}).get("prob")) if matched else None,
                "evalue": util.parse_float((matched or {}).get("evalue")) if matched else None,
                "matched_target": (matched or {}).get("target_raw") if matched else None,
                "db": (matched or {}).get("db") if matched else None,
                "passed": passed,
                "reasons": reasons,
            }
            rows.append(row)
            if not passed and required:
                failures.append(row)
    return (
        {
            "checked": len(rows),
            "passed": sum(1 for row in rows if row["passed"]),
            "failed": sum(1 for row in rows if not row["passed"]),
            "have_anchors": any_query_has_anchor,
            "rows": rows,
        },
        failures,
    )


def _cross_database_checks(hits_by_query: Mapping[str, list[Mapping[str, Any]]]) -> list[dict[str, Any]]:
    """Find the same (query, target) pair scored in multiple databases."""
    pairs: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for query, hits in hits_by_query.items():
        for hit in hits:
            target = hit.get("target_accession")
            if not target:
                continue
            key = (_query_accession(str(query)) or str(query), str(target).upper())
            pairs.setdefault(key, []).append(
                {
                    "database": hit.get("db"),
                    "evalue": util.parse_float(hit.get("evalue")),
                    "tm_score": util.parse_float(hit.get("tm_score")),
                    "prob": util.parse_float(hit.get("prob")),
                }
            )
    checks: list[dict[str, Any]] = []
    for (query, target), observations in sorted(pairs.items()):
        databases = {str(observation.get("database")) for observation in observations}
        if len(databases) < 2:
            continue
        evalues = [
            float(observation["evalue"])
            for observation in observations
            if observation.get("evalue") is not None and float(observation["evalue"]) > 0
        ]
        ratio = None
        if len(evalues) >= 2:
            ratio = max(evalues) / min(evalues)
        checks.append(
            {
                "query_accession": query,
                "target_accession": target,
                "databases": sorted(databases),
                "observations": observations,
                "evalue_ratio": round(ratio, 3) if ratio is not None else None,
                "not_comparable": True,
            }
        )
    return checks


def _warn_once(ctx: Any, code: str, message: str, *, context: Mapping[str, Any] | None = None) -> None:
    """Record a standard guardrail warning only once per envelope (adapters may also emit it)."""
    if any(warning.get("code") == code for warning in ctx.warnings):
        return
    ctx.warn(code, message, context=context)


def _emit_standard_warnings(
    ctx: Any,
    *,
    mode: str,
    hits_by_query: Mapping[str, list[Mapping[str, Any]]],
    databases: list[str],
    cap: int | None,
    query_length: int | None = None,
) -> None:
    if not any(warning.get("code") == "TM_SCORE_QUERY_NORMALISED" for warning in ctx.warnings):
        ctx.warn_tm_query_normalised(query_length)
    if cap is not None and not any(warning.get("code") == "TOP_HIT_LIST_CAPPED" for warning in ctx.warnings):
        ctx.warn_top_hit_cap(", ".join(databases) or mode, cap)
    examples = _cross_database_checks(hits_by_query)
    hint = (
        "A structural pair can score very differently in a small local panel and in a global database: "
        "compare prob/TM and control-relative margins, never raw E-values across databases."
    )
    if examples:
        hint += f" Detected {len(examples)} pair(s) observed in more than one database with different E-values."
    if not any(warning.get("code") == "EVALUE_NOT_COMPARABLE_ACROSS_DATABASES" for warning in ctx.warnings):
        ctx.warn_evalue_db_dependence(", ".join(databases) or mode, hint)



#: Maximum hits per query echoed into a model-facing payload (full tables stay on disk).
OUTPUT_HIT_LIMIT = 60


def _output_hits(
    hits_by_query: Mapping[str, list[dict[str, Any]]],
    limit: int = OUTPUT_HIT_LIMIT,
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, dict[str, int]]]:
    """Truncate hits echoed to the model while preserving totals and anchors.

    A web/AFDB50 search can return hundreds of hits per query; echoing every row
    crowds out interpretation. The full recorded table remains available
    (``tables``/``out``) and totals are reported, so nothing is lost.
    """
    truncated: dict[str, dict[str, int]] = {}
    out: dict[str, list[dict[str, Any]]] = {}
    for query, hits in hits_by_query.items():
        out[query] = hits[:limit]
        if len(hits) > limit:
            truncated[query] = {"returned": limit, "total": len(hits)}
    return out, truncated


def _warn_truncated(ctx: Any, hits_truncated: Mapping[str, Any]) -> None:
    if not hits_truncated:
        return
    total = sum(int(item.get("total") or 0) for item in hits_truncated.values())
    ctx.warn(
        "HITS_TRUNCATED_FOR_OUTPUT",
        f"{total} hits were returned by the search but only the top {OUTPUT_HIT_LIMIT} per query are "
        "echoed here to keep the result usable; the complete table is in the recorded table / out dir. "
        "Do not interpret the truncated list as the complete result set (top-hit lists are capped anyway).",
        context={"queries": util.json_safe(dict(hits_truncated)), "limit": OUTPUT_HIT_LIMIT},
    )


def _write_outputs(
    ctx: Any,
    args: Mapping[str, Any],
    *,
    mode: str,
    hits_by_query: Mapping[str, list[Mapping[str, Any]]],
    payload_extra: Mapping[str, Any],
) -> dict[str, Any] | None:
    if not args.get("out"):
        return None
    out_dir = util.ensure_dir(_resolve_path(ctx, str(args["out"])))
    info: dict[str, Any] = {"dir": str(out_dir)}
    rows: list[dict[str, Any]] = []
    for query, hits in hits_by_query.items():
        for hit in hits:
            record = dict(hit)
            record["query"] = query
            rows.append(record)
    if rows:
        all_columns = sorted({key for row in rows for key in row})
        leading = [name for name in ("query", "db", "rank", "target_accession", "tm_score", "prob", "evalue") if name in all_columns]
        columns = leading + [name for name in all_columns if name not in leading]
        hits_path = out_dir / f"struct.search.{mode}.hits.tsv"
        util.write_tsv(hits_path, rows, columns=columns)
        _record_local_file(ctx, hits_path, kind="output_table", notes={"command": "struct.search"})
        info["hits"] = {"path": str(hits_path), "sha256": util.sha256_file(hits_path), "bytes": hits_path.stat().st_size}
    summary_path = out_dir / f"struct.search.{mode}.summary.json"
    util.atomic_write_json(summary_path, util.json_safe(payload_extra))
    _record_local_file(ctx, summary_path, kind="output_summary", notes={"command": "struct.search"})
    info["summary"] = {"path": str(summary_path), "sha256": util.sha256_file(summary_path), "bytes": summary_path.stat().st_size}
    return info


def _foldseek_hit_tm(hit: Mapping[str, Any]) -> float | None:
    for key in ("tm_score", "alntmscore", "qtmscore"):
        value = util.parse_float(hit.get(key))
        if value is not None:
            return value
    return None


def _normalise_foldseek_hit(hit: Mapping[str, Any], *, query_id: str, database: str) -> dict[str, Any]:
    normalised = dict(hit)
    normalised["query"] = query_id
    normalised["db"] = database
    normalised["tm_score"] = _foldseek_hit_tm(hit)
    target_label = str(hit.get("target") or hit.get("target_raw") or "")
    normalised["target_raw"] = target_label
    normalised.setdefault("target_accession", _target_accession_from_label(target_label))
    return normalised


def _run_panel(ctx: Any, args: Mapping[str, Any]) -> dict[str, Any]:
    try:
        from ..adapters import foldseek
    except ImportError as err:  # pragma: no cover - parallel workstream ordering
        raise ToolMissingError(
            "lifesci.adapters.foldseek is not available; panel mode cannot run",
            details={"error": str(err)},
            fallbacks=["use struct.search --mode from_table with recorded hit tables"],
        ) from err

    queries = _normalise_structure_specs(args.get("queries"))
    if not queries:
        raise InvalidInputError("panel mode needs 'queries'")
    targets_value = args.get("targets")
    target_dir: Path
    target_count: int | None = None
    if isinstance(targets_value, Mapping) and targets_value.get("dir"):
        target_dir = _resolve_path(ctx, str(targets_value["dir"]))
        target_count = len([p for p in target_dir.iterdir() if p.is_file()])
    elif isinstance(targets_value, str):
        target_dir = _resolve_path(ctx, targets_value)
        target_count = len([p for p in target_dir.iterdir() if p.is_file()])
    elif isinstance(targets_value, list):
        target_paths = [_resolve_path(ctx, str(item)) for item in targets_value]
        target_dir = _prepare_structure_dir(ctx, target_paths, name="struct_search_panel")
        target_count = len(target_paths)
    else:
        raise InvalidInputError("panel mode needs 'targets' (a directory or a list of structure paths)")
    if not target_dir.is_dir():
        raise InvalidInputError(f"target panel directory not found: {target_dir}")

    out_root = util.ensure_dir(_resolve_path(ctx, str(args["out"]))) if args.get("out") else util.ensure_dir(Path(ctx.cache_dir) / "out" / "struct.search")
    tmp_root = util.ensure_dir(out_root / "tmp")
    hits_by_query: dict[str, list[dict[str, Any]]] = {}
    versions: list[str] = []
    for query in queries:
        query_path = _resolve_path(ctx, query["path"])
        if not query_path.is_file():
            raise InvalidInputError(f"query structure not found: {query_path}")
        result = foldseek.local_search(
            ctx,
            query_path=query_path,
            target_path=target_dir,
            out_path=out_root / f"{util.slugify(query['id'], max_length=60, fallback='query')}.panel.m8",
            tmp_dir=tmp_root,
            fmt=str(args.get("fmt") or "query,target,alntmscore,qtmscore,ttmscore,lddt,evalue,qcov,tcov,alnlen"),
            e=float(args.get("e", 100.0)),
            threads=int(args.get("threads", 4)),
            binary=args.get("binary"),
        )
        hits = [
            _normalise_foldseek_hit(hit, query_id=query["id"], database="local_panel")
            for hit in result.get("hits") or []
        ]
        hits.sort(key=lambda hit: (hit.get("evalue") is None, hit.get("evalue") or 1e300))
        for rank, hit in enumerate(hits, 1):
            hit["rank"] = rank
        hits_by_query[query["id"]] = hits
        if result.get("version") and result["version"] not in versions:
            versions.append(str(result["version"]))

    # ---- negative-control calibration ---------------------------------
    controls = _normalise_structure_specs(args.get("controls"))
    margin = float(args.get("margin", 0.05))
    min_control_coverage = float(args.get("min_control_coverage", 0.3))
    min_hit_coverage = float(args.get("min_hit_coverage", 0.3))
    control_max_tm: float | None = None
    per_control: list[dict[str, Any]] = []
    if controls:
        all_control_tm: list[float] = []
        for control in controls:
            control_path = _resolve_path(ctx, control["path"])
            if not control_path.is_file():
                raise InvalidInputError(f"control structure not found: {control_path}")
            result = foldseek.local_search(
                ctx,
                query_path=control_path,
                target_path=target_dir,
                out_path=out_root / f"{util.slugify(control['id'], max_length=60, fallback='control')}.control.m8",
                tmp_dir=tmp_root,
                fmt=str(args.get("fmt") or "query,target,alntmscore,qtmscore,ttmscore,lddt,evalue,qcov,tcov,alnlen"),
                e=float(args.get("e", 100.0)),
                threads=int(args.get("threads", 4)),
                binary=args.get("binary"),
            )
            control_hits = [
                _normalise_foldseek_hit(hit, query_id=control["id"], database="local_panel_control")
                for hit in result.get("hits") or []
            ]
            own_tm = None
            credible: list[float] = []
            all_tm: list[float] = []
            control_accession = _query_accession(control["id"])
            for hit in control_hits:
                tm = _foldseek_hit_tm(hit)
                if tm is None:
                    continue
                target_accession = str(hit.get("target_accession") or "").upper()
                target_label = Path(str(hit.get("target_raw") or "")).stem
                is_self = bool(
                    (control_accession and target_accession and target_accession == control_accession.upper())
                    or (target_label and target_label == str(control["id"]))
                )
                if is_self:
                    own_tm = tm
                    continue
                all_tm.append(tm)
                qcov = util.parse_float(hit.get("qcov"))
                if qcov is not None and qcov >= min_control_coverage:
                    credible.append(tm)
                    all_control_tm.append(tm)
            per_control.append(
                {
                    "id": control["id"],
                    "n_hits": len(control_hits),
                    "n_credible_hits": len(credible),
                    "max_tm_against_panel": round(max(credible), 4) if credible else None,
                    "max_tm_against_panel_unfiltered": round(max(all_tm), 4) if all_tm else None,
                    "min_control_coverage": min_control_coverage,
                    "self_tm": round(own_tm, 4) if own_tm is not None else None,
                }
            )
        control_max_tm = max(all_control_tm) if all_control_tm else 0.0
    else:
        ctx.warn(
            "PANEL_CONTROLS_MISSING",
            "No negative controls were supplied for the controlled panel: the background TM is unknown and hits "
            "cannot be declared interpretable above background. Supply >=5 unrelated proteins of the same fold class.",
        )
    min_interpretable = (control_max_tm + margin) if controls else None
    for query, hits in hits_by_query.items():
        for hit in hits:
            tm = _foldseek_hit_tm(hit)
            qcov = util.parse_float(hit.get("qcov"))
            coverage_pass = qcov is not None and qcov >= min_hit_coverage
            hit["coverage_pass"] = coverage_pass
            hit["min_hit_coverage"] = min_hit_coverage
            if min_interpretable is None:
                hit["above_background"] = None
                hit["interpretation"] = "background_uncalibrated"
            elif not coverage_pass:
                hit["above_background"] = False
                hit["interpretation"] = "low_coverage"
            elif tm is not None and tm >= min_interpretable:
                hit["above_background"] = True
                hit["interpretation"] = "interpretable_above_background"
            else:
                hit["above_background"] = False
                hit["interpretation"] = "within_control_background"
            hit["background_margin"] = (
                round(tm - min_interpretable, 4) if (tm is not None and min_interpretable is not None) else None
            )
    n_above = sum(
        1
        for hits in hits_by_query.values()
        for hit in hits
        if hit.get("above_background") is True and hit.get("coverage_pass")
    )
    n_below = sum(
        1
        for hits in hits_by_query.values()
        for hit in hits
        if hit.get("above_background") is False and hit.get("coverage_pass")
    )
    n_low_coverage = sum(
        1 for hits in hits_by_query.values() for hit in hits if not hit.get("coverage_pass")
    )
    background = {
        "available": bool(controls),
        "n_controls": len(controls),
        "control_max_tm": round(control_max_tm, 4) if control_max_tm is not None else None,
        "margin": margin,
        "min_control_coverage": min_control_coverage,
        "min_interpretable_tm": round(min_interpretable, 4) if min_interpretable is not None else None,
        "per_control": per_control,
        "note": (
            "Control alignments with query coverage >= min_control_coverage set the background; hits at or "
            "below min_interpretable_tm are within that background and are not evidence of homology."
            if controls
            else "no negative controls: background uncalibrated"
        ),
    }
    databases = ["local_panel"]
    cap = None
    if n_low_coverage:
        ctx.warn(
            "PANEL_LOW_COVERAGE_HITS",
            f"{n_low_coverage} panel hit(s) fall below the minimum query coverage ({min_hit_coverage}); short "
            "fragment alignments can have a high alignment TM and are not interpretable as homology.",
            context={"n_low_coverage": n_low_coverage, "min_hit_coverage": min_hit_coverage},
        )
    _emit_standard_warnings(ctx, mode="panel", hits_by_query=hits_by_query, databases=databases, cap=cap)
    queries_payload = [
        {"id": query["id"], "path": query["path"], "accession": _query_accession(query["id"]), "source": "local_panel"}
        for query in queries
    ]
    anchors, failures = _verify_anchors(
        queries_payload,
        hits_by_query,
        args.get("expected_anchors"),
    )
    if failures:
        ctx.warn(
            "SANITY_ANCHOR_MISSING",
            f"{len(failures)} structural sanity anchor(s) were not recovered in the panel search; the panel may miss "
            "known orthologues - do not interpret a missing candidate as absence.",
            context={"failures": failures[:10]},
        )
    db_metadata = {
        "databases": databases,
        "foldseek_version": versions[0] if versions else None,
        "n_targets": target_count,
        "evalue_note": "panel E-values are database-size dependent and not comparable across databases",
        "extra": util.json_safe(dict(args.get("db_metadata") or {})),
    }
    output_hits, hits_truncated = _output_hits(hits_by_query)
    _warn_truncated(ctx, hits_truncated)
    payload = {
        "dry_run": False,
        "mode": "panel",
        "queries": queries_payload,
        "hits": output_hits,
        "hits_truncated": hits_truncated,
        "n_hits": sum(len(hits) for hits in hits_by_query.values()),
        "n_queries": len(queries),
        "n_targets": target_count,
        "panel_controls": {
            "n_controls": len(controls),
            "controls": per_control,
            "control_max_tm": round(control_max_tm, 4) if control_max_tm is not None else None,
        },
        "background": background,
        "anchors": anchors,
        "db_metadata": db_metadata,
        "cross_database_evalue_checks": _cross_database_checks(hits_by_query),
        "interpretation": {
            "n_above_background": n_above,
            "n_below_background": n_below,
            "n_low_coverage": n_low_coverage,
            "min_hit_coverage": min_hit_coverage,
            "interpretable_rule": (
                f"TM >= control_max_tm + margin = {round(min_interpretable, 4)} AND qcov >= {min_hit_coverage}"
                if min_interpretable is not None
                else None
            ),
            "note": "Structural similarity is evidence for the closest structural neighbour, not automatic orthology.",
        },
        "resolved": {
            "cache_dir": str(ctx.cache_dir),
            "target_panel": str(target_dir),
            "foldseek_version": versions[0] if versions else None,
            "e": float(args.get("e", 100.0)),
            "threads": int(args.get("threads", 4)),
        },
    }
    out_info = _write_outputs(ctx, args, mode="panel", hits_by_query=hits_by_query, payload_extra=payload)
    payload["resolved"]["out"] = out_info
    return payload


def _run_web(ctx: Any, args: Mapping[str, Any]) -> dict[str, Any]:
    try:
        from ..adapters import alphafold, foldseek
    except ImportError as err:  # pragma: no cover - parallel workstream ordering
        raise ToolMissingError(
            "lifesci.adapters.alphafold/foldseek are not available; web mode cannot run",
            details={"error": str(err)},
            fallbacks=["use struct.search --mode from_table with recorded hit tables"],
        ) from err

    pdb_paths = [str(item) for item in (args.get("pdb_paths") or [])]
    accessions = [str(item) for item in (args.get("pdb_accessions") or [])]
    if not pdb_paths and not accessions:
        raise InvalidInputError("web mode needs 'pdb_paths' or 'pdb_accessions' (only PDB uploads work)")
    databases = [str(item) for item in (args.get("databases") or ["afdb50"])]
    out_root = (
        util.ensure_dir(_resolve_path(ctx, str(args["out"])))
        if args.get("out")
        else util.ensure_dir(Path(ctx.cache_dir) / "out" / "struct.search")
    )
    model_dir = util.ensure_dir(out_root / "query_models")
    queries: list[dict[str, Any]] = []
    models: list[tuple[str, bytes, str]] = []
    for path_value in pdb_paths:
        path = _resolve_path(ctx, path_value)
        if not path.is_file():
            raise InvalidInputError(f"PDB query file not found: {path}")
        _record_local_file(ctx, path, kind="query_structure", notes={"command": "struct.search"})
        queries.append({"id": path.stem, "path": str(path), "accession": _query_accession(path.stem), "source": "local_pdb"})
        models.append((path.stem, path.read_bytes(), path.name))
    for accession in accessions:
        downloaded = alphafold.download_model(ctx, accession, out_dir=model_dir, fmt="pdb")
        path_value = downloaded.get("path")
        if not path_value or not Path(str(path_value)).is_file():
            raise LifesciError(
                f"could not obtain an AlphaFold DB PDB model for web search query {accession}",
                code="WEB_QUERY_MODEL_UNAVAILABLE",
                details={"accession": accession, "download": util.json_safe(downloaded)},
            )
        path = Path(str(path_value))
        _record_local_file(ctx, path, kind="query_structure", notes={"command": "struct.search", "accession": accession})
        queries.append({"id": accession, "path": str(path), "accession": accession, "source": "alphafold"})
        models.append((accession, path.read_bytes(), path.name))

    hits_by_query: dict[str, list[dict[str, Any]]] = {}
    raw_results: list[dict[str, Any]] = []
    for query, pdb_bytes, filename in models:
        result = foldseek.web_search(
            ctx,
            pdb_bytes=pdb_bytes,
            filename=filename,
            databases=databases,
            poll_interval=float(args.get("poll_interval", 5)),
            max_wait=float(args.get("max_wait", 600)),
        )
        raw_results.append({"query": query, "ticket": result.get("ticket"), "status": result.get("status")})
        raw_hits: list[Mapping[str, Any]] = []
        if isinstance(result.get("hits"), list):
            raw_hits = [hit for hit in result["hits"] if isinstance(hit, Mapping)]
        else:
            for db_result in result.get("results") or []:
                for hit in (db_result or {}).get("hits") or []:
                    if isinstance(hit, Mapping):
                        raw_hits.append({**hit, "db": hit.get("db") or (db_result or {}).get("db")})
        normalised = []
        for raw_hit in raw_hits:
            hit = dict(raw_hit)
            hit.setdefault("query", query)
            tm = util.parse_float(hit.get("tm_score"))
            if tm is None:
                score = util.parse_float(hit.get("score"))
                if score is not None:
                    tm = score / 100.0 if score > 1 else score
                    hit["tm_score"] = round(tm, 4)
            if hit.get("target_accession") is None:
                hit["target_accession"] = _target_accession_from_label(str(hit.get("target_raw") or hit.get("target") or ""))
            normalised.append(hit)
        normalised.sort(key=lambda hit: (hit.get("rank") is None, hit.get("rank") or 0))
        hits_by_query[query] = normalised
    databases_present = sorted({str(hit.get("db")) for hits in hits_by_query.values() for hit in hits if hit.get("db")})
    _emit_standard_warnings(
        ctx,
        mode="web",
        hits_by_query=hits_by_query,
        databases=databases_present or databases,
        cap=1000,
        query_length=None,
    )
    queries_payload = [dict(query) for query in queries]
    anchors, failures = _verify_anchors(
        queries_payload,
        hits_by_query,
        args.get("expected_anchors"),
    )
    if failures:
        ctx.warn(
            "SANITY_ANCHOR_MISSING",
            f"{len(failures)} structural sanity anchor(s) were not recovered in the web search; check the database "
            "selection and the query model.",
            context={"failures": failures[:10]},
        )
    output_hits, hits_truncated = _output_hits(hits_by_query)
    _warn_truncated(ctx, hits_truncated)
    payload = {
        "dry_run": False,
        "mode": "web",
        "queries": queries_payload,
        "hits": output_hits,
        "hits_truncated": hits_truncated,
        "n_hits": sum(len(hits) for hits in hits_by_query.values()),
        "n_queries": len(queries),
        "n_targets": None,
        "panel_controls": None,
        "background": {"available": False, "note": "web database search has no negative-control background"},
        "anchors": anchors,
        "db_metadata": {
            "databases": databases_present or databases,
            "requested_databases": databases,
            "top_hit_cap_per_database": 1000,
            "evalue_note": "server E-values depend on database size; never compare across databases",
            "server": "https://search.foldseek.com/api",
            "extra": util.json_safe(dict(args.get("db_metadata") or {})),
        },
        "cross_database_evalue_checks": _cross_database_checks(hits_by_query),
        "interpretation": {
            "note": (
                "Server score is query-normalised TM x100; prob is Foldseek's homology probability. A divergent "
                "ortholog can be missed in the forward direction - run the reciprocal search before excluding it."
            )
        },
        "resolved": {
            "cache_dir": str(ctx.cache_dir),
            "queries": raw_results,
        },
    }
    out_info = _write_outputs(ctx, args, mode="web", hits_by_query=hits_by_query, payload_extra=payload)
    payload["resolved"]["out"] = out_info
    return payload


def _run_from_table(ctx: Any, args: Mapping[str, Any]) -> dict[str, Any]:
    replay = _replay_tables(ctx, args)
    hits_by_query = replay["hits"]
    databases = replay["databases"]
    ranks_known = all(
        hit.get("rank") is not None for hits in hits_by_query.values() for hit in hits
    )
    cap = None
    if ranks_known:
        max_rows = max((len(hits) for hits in hits_by_query.values()), default=0)
        cap = max_rows
    _emit_standard_warnings(
        ctx,
        mode="from_table",
        hits_by_query=hits_by_query,
        databases=databases,
        cap=cap,
    )
    queries = replay["queries"]
    anchors, failures = _verify_anchors(queries, hits_by_query, args.get("expected_anchors"))
    if failures:
        ctx.warn(
            "SANITY_ANCHOR_MISSING",
            f"{len(failures)} structural sanity anchor(s) were not recovered from the recorded table(s); the search "
            "or table may be truncated/wrong - do not interpret the missing candidate as absence.",
            context={"failures": failures[:10]},
        )
        for failure in failures:
            ctx.review(
                "SANITY_ANCHOR_REVIEW",
                f"Anchor {failure.get('target_accession') or failure.get('family_hint')} not recovered for query "
                f"{failure.get('query')}; inspect the recorded table.",
                context={"anchor": failure},
            )
    elif anchors.get("checked"):
        for row in anchors["rows"]:
            if not row["passed"] and row.get("required"):
                ctx.review(
                    "SANITY_ANCHOR_REVIEW",
                    f"Anchor {row.get('target_accession') or row.get('family_hint')} failed for {row.get('query')}.",
                )
    cross_checks = _cross_database_checks(hits_by_query)
    panel_databases = [database for database in databases if "panel" in database.lower()]
    db_metadata = {
        "databases": databases,
        "top_hit_cap_per_query": cap,
        "n_tables": len(replay["tables"]),
        "tables": replay["tables"],
        "evalue_note": (
            "E-values depend on database size: the same pair scores very differently against a small local "
            "panel and against a global database. Never compare E-values across databases."
        ),
        "panel_databases": panel_databases,
        "extra": util.json_safe(dict(args.get("db_metadata") or {})),
    }
    interpretation = {
        "n_queries": len(queries),
        "n_hits": sum(len(hits) for hits in hits_by_query.values()),
        "n_cross_database_pairs": len(cross_checks),
        "note": (
            "Recorded-table replay: structural similarity is evidence for the closest structural neighbour, not "
            "automatic orthology; TM is query-length-normalised and result lists are capped."
        ),
    }
    output_hits, hits_truncated = _output_hits(hits_by_query)
    _warn_truncated(ctx, hits_truncated)
    payload = {
        "dry_run": False,
        "mode": "from_table",
        "queries": queries,
        "hits": output_hits,
        "hits_truncated": hits_truncated,
        "n_hits": interpretation["n_hits"],
        "n_queries": len(queries),
        "n_targets": None,
        "panel_controls": None,
        "background": {"available": False, "note": "no panel controls in a recorded-table replay"},
        "anchors": anchors,
        "db_metadata": db_metadata,
        "cross_database_evalue_checks": cross_checks,
        "interpretation": interpretation,
        "resolved": {
            "cache_dir": str(ctx.cache_dir),
            "tables": replay["tables"],
        },
    }
    out_info = _write_outputs(ctx, args, mode="from_table", hits_by_query=hits_by_query, payload_extra=payload)
    payload["resolved"]["out"] = out_info
    return payload


@command(
    name="struct.search",
    title="Foldseek structural search",
    summary="Controlled local Foldseek panel with negative-control calibration, Foldseek web API, or recorded-table replay with sanity anchors.",
    description=(
        "Structural-similarity search with explicit interpretation limits. Modes: 'panel' runs the local Foldseek "
        "binary on a controlled panel and calibrates the background with negative controls (unrelated proteins of "
        "the same fold class); only scores above control_max_tm + margin are interpretable. 'web' runs the Foldseek "
        "web API (AFDB50/AFDB-SwissProt/PDB100; only multipart PDB upload works). 'from_table' replays recorded "
        "hit tables offline and optionally checks caller-supplied sanity anchors (expected_anchors), warning with "
        "SANITY_ANCHOR_MISSING when one is not recovered. Always reported: TM-scores are query-normalised "
        "(fragment/short queries score higher), result lists are capped (AFDB50 top ~1000 per database), and "
        "E-values are database-size dependent, so E-values must never be compared across databases. Structural "
        "similarity supports 'closest structural neighbour' but is not automatic orthology; use reciprocal "
        "searches and domain/family evidence as the arbiter."
    ),
    input_schema=INPUT_SCHEMA,
    output_schema="struct.search.schema.json",
    category="structure",
    timeout_ms=600_000,
    danger="network",
    examples=(
        {
            "mode": "from_table",
            "tables": ["hits/structural_search.tsv"],
            "expected_anchors": [{"target_accession": "P00698", "min_tm": 0.5, "required": True}],
        },
        {
            "mode": "panel",
            "queries": ["models/query_model.pdb"],
            "targets": {"dir": "panel/"},
            "controls": ["controls/control_a.pdb", "controls/control_b.pdb"],
        },
        {"mode": "web", "pdb_accessions": ["P69905"], "databases": ["afdb50"]},
    ),
)
def run(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
    """Run one struct.search mode and return hits, background, anchors and caveat warnings."""
    ctx.validate()
    mode = str(args.get("mode") or "").strip().lower()
    if mode not in {"panel", "web", "from_table"}:
        raise InvalidInputError(f"unknown struct.search mode {mode!r}; expected panel, web or from_table")
    if ctx.dry_run:
        return _plan(args, ctx)
    if mode == "panel":
        return _run_panel(ctx, args)
    if mode == "web":
        return _run_web(ctx, args)
    return _run_from_table(ctx, args)
