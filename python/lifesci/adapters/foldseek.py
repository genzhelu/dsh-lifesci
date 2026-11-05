"""Foldseek adapter: local static binary and the search.foldseek.com web API.

Upstream quirks implemented here:

* **Local binary** — `foldseek easy-search` on a controlled panel, with an
  explicit ``--format-output`` column list; the binary path is resolved from
  ``$LIFESCI_FOLDSEEK`` → ``tools/bin/foldseek`` → ``PATH`` and its version and
  SHA-256 are recorded. There is **no silent auto-update**; installation is a
  separate checksum-pinned script (``scripts/install_foldseek.sh``).
* **Web API** — only **multipart PDB upload** works: JSON-body submission returns
  HTTP 400 (``JobRequest.Job.Database/Mode required``) and FASTA/sequence
  submissions create a ticket but the job ends ``ERROR``. Tickets are polled until
  ``COMPLETE``/``ERROR``; the result JSON is large (15–23 MB) and cached.
  Server ``score`` is query-length-normalised TM × 100 in ``tmalign`` mode,
  ``prob`` is Foldseek's homology probability, and ``eval`` depends on the
  database size — never compare E-values across databases.

Interpretation limits (also emitted as warnings by the command layer): a server
result list is capped at ~1000 alignments per database, so absence from the
returned hits is not proof of absence; structural similarity is evidence for the
closest structural neighbour, not automatic orthology.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from .. import http, paths, util
from ..errors import DataError, InvalidInputError, LifesciError, NotFoundError, PortalError, ToolMissingError
from . import _common

WEB_BASE = "https://search.foldseek.com/api"
DATABASE = "foldseek"

#: Columns requested from ``foldseek easy-search``.
DEFAULT_FMT = "query,target,alntmscore,qtmscore,ttmscore,lddt,evalue,qcov,tcov,alnlen"

#: Numeric fields in a Foldseek m8-style record.
_NUMERIC_FIELDS = frozenset(
    {"alntmscore", "qtmscore", "ttmscore", "lddt", "evalue", "qcov", "tcov", "alnlen", "fident", "prob", "score"}
)

#: Documented dead ends for the web API (kept as constants so tests/callers see them).
WEB_JSON_BODY_REJECTED = (
    "the Foldseek web API rejects JSON-body submissions (HTTP 400: "
    "'JobRequest.Job.Database/Mode required'); submit a multipart PDB upload instead"
)
WEB_SEQUENCE_QUERY_BROKEN = (
    "sequence/FASTA submissions to the Foldseek web API create a ticket but the job ends ERROR; "
    "upload a structure (PDB model) instead"
)


# ---------------------------------------------------------------------------
# binary resolution / version
# ---------------------------------------------------------------------------


def resolve_binary(binary: str | os.PathLike[str] | None = None) -> Path:
    """Resolve the Foldseek executable; raise a precise ``ToolMissingError`` if absent."""
    candidates: list[Path] = []
    if binary:
        candidates.append(Path(str(binary)).expanduser())
    env_value = os.environ.get("LIFESCI_FOLDSEEK")
    if env_value:
        candidates.append(Path(env_value).expanduser())
    candidates.append(paths.TOOLS_DIR / "foldseek")
    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
    on_path = shutil.which("foldseek")
    if on_path:
        return Path(on_path)
    raise ToolMissingError(
        "Foldseek binary not found (checked $LIFESCI_FOLDSEEK, tools/bin/foldseek, PATH)",
        code="FOLDSEEK_MISSING",
        details={"checked": [str(c) for c in candidates], "path_lookup": "foldseek"},
        fallbacks=[
            "run scripts/install_foldseek.sh (pinned, SHA-256-verified static build)",
            "set LIFESCI_FOLDSEEK=/path/to/foldseek",
        ],
    )


def _run(ctx: Any, argv: Sequence[str], *, timeout: float = 600.0, cwd: Path | None = None) -> subprocess.CompletedProcess:
    started = time.monotonic()
    try:
        proc = subprocess.run(
            [str(arg) for arg in argv],
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(cwd) if cwd else None,
            check=False,
        )
    except subprocess.TimeoutExpired as err:
        raise LifesciError(
            f"Foldseek command timed out after {timeout:.0f}s: {' '.join(str(a) for a in argv)}",
            code="FOLDSEEK_TIMEOUT",
            retriable=True,
            details={"argv": [str(a) for a in argv]},
        ) from err
    except (FileNotFoundError, PermissionError) as err:
        raise ToolMissingError(
            f"cannot execute Foldseek at {argv[0]}: {err}",
            code="FOLDSEEK_MISSING",
            details={"binary": str(argv[0])},
            fallbacks=["run scripts/install_foldseek.sh", "set LIFESCI_FOLDSEEK"],
        ) from err
    elapsed_ms = (time.monotonic() - started) * 1000.0
    _common.log_tool(
        ctx,
        "foldseek",
        version=None,
        argv=list(str(a) for a in argv),
        exit_code=proc.returncode,
        elapsed_ms=elapsed_ms,
    )
    return proc


def version(ctx: Any, binary: str | os.PathLike[str] | None = None) -> dict[str, Any]:
    """Return ``{available, path, version, sha256, error}`` for the Foldseek binary."""
    try:
        path = resolve_binary(binary)
    except ToolMissingError as err:
        return {"available": False, "path": None, "version": None, "sha256": None, "error": err.message}
    try:
        proc = _run(ctx, [str(path), "version"], timeout=30.0)
    except LifesciError as err:
        return {"available": False, "path": str(path), "version": None, "sha256": util.sha256_file(path), "error": err.message}
    version_text = ""
    for line in ((proc.stdout or "").strip() or (proc.stderr or "").strip()).splitlines():
        if line.strip():
            version_text = line.strip()
            break
    ok = proc.returncode == 0 and bool(version_text)
    if not ok:
        ctx.warn(
            "FOLDSEEK_PROBE_FAILED",
            "Foldseek binary exists but the version probe failed.",
            context={"path": str(path), "exit_code": proc.returncode},
        )
    return {
        "available": ok,
        "path": str(path),
        "version": version_text or None,
        "sha256": util.sha256_file(path),
        "error": None if ok else f"exit code {proc.returncode}",
    }


def require_version(ctx: Any, binary: str | os.PathLike[str] | None = None) -> dict[str, Any]:
    """Like :func:`version`, but raise ``ToolMissingError`` when unusable."""
    info = version(ctx, binary)
    if not info["available"]:
        raise ToolMissingError(
            f"Foldseek is not usable: {info.get('error') or 'not found'}",
            code="FOLDSEEK_MISSING",
            details=info,
            fallbacks=["run scripts/install_foldseek.sh", "set LIFESCI_FOLDSEEK"],
        )
    return info


# ---------------------------------------------------------------------------
# local search
# ---------------------------------------------------------------------------


def parse_m8(text: str, *, columns: Sequence[str] | None = None) -> list[dict[str, Any]]:
    """Parse Foldseek m8/TSV output into deterministic typed hit records."""
    fields = list(columns or DEFAULT_FMT.split(","))
    hits: list[dict[str, Any]] = []
    for lineno, line in enumerate(text.splitlines(), 1):
        if not line.strip() or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 2:
            raise DataError(
                f"Foldseek output line {lineno} has fewer than 2 columns",
                details={"line": util.truncate(line, 200), "columns": fields},
            )
        record: dict[str, Any] = {}
        for index, name in enumerate(fields):
            raw = parts[index] if index < len(parts) else ""
            if name in _NUMERIC_FIELDS:
                record[name] = util.parse_float(raw)
            else:
                record[name] = raw
        hits.append(record)
    return hits


def local_search(
    ctx: Any,
    *,
    query_path: str | os.PathLike[str],
    target_path: str | os.PathLike[str],
    out_path: str | os.PathLike[str],
    tmp_dir: str | os.PathLike[str],
    fmt: str = DEFAULT_FMT,
    e: float = 100.0,
    threads: int = 4,
    binary: str | os.PathLike[str] | None = None,
    timeout: float = 900.0,
) -> dict[str, Any]:
    """Run ``foldseek easy-search`` for one query against one target/panel.

    The output table is parsed and returned; the raw m8 file stays at ``out_path``
    for auditing.  Foldseek runs inside ``tmp_dir`` (no writes elsewhere).
    """
    query = Path(query_path)
    target = Path(target_path)
    if not query.exists():
        raise InvalidInputError(f"Foldseek query does not exist: {query}")
    if not target.exists():
        raise InvalidInputError(f"Foldseek target does not exist: {target}")
    out = Path(out_path)
    tmp = Path(tmp_dir)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp.mkdir(parents=True, exist_ok=True)
    info = require_version(ctx, binary)
    argv = [
        info["path"],
        "easy-search",
        str(query),
        str(target),
        str(out),
        str(tmp),
        "--format-output",
        str(fmt),
        "-e",
        f"{float(e):g}",
        "--threads",
        str(max(1, int(threads))),
    ]
    if bool(getattr(ctx, "dry_run", False)):
        return {
            "dry_run": True,
            "hits": [],
            "n_hits": 0,
            "out_path": str(out),
            "tmp_dir": str(tmp),
            "version": info.get("version"),
            "binary": info,
            "command": argv,
        }
    proc = _run(ctx, argv, timeout=timeout, cwd=tmp)
    if proc.returncode != 0:
        stderr = (proc.stderr or "").strip()
        raise LifesciError(
            f"Foldseek easy-search failed (exit {proc.returncode}): {util.truncate(stderr, 600)}",
            code="FOLDSEEK_FAILED",
            retriable=False,
            details={
                "argv": argv,
                "exit_code": proc.returncode,
                "stderr_tail": util.truncate(stderr, 2000),
            },
        )
    text = out.read_text(encoding="utf-8", errors="replace") if out.is_file() else ""
    columns = [name.strip() for name in str(fmt).split(",") if name.strip()]
    hits = parse_m8(text, columns=columns)
    return {
        "dry_run": False,
        "hits": hits,
        "n_hits": len(hits),
        "out_path": str(out),
        "tmp_dir": str(tmp),
        "version": info.get("version"),
        "binary": info,
        "command": argv,
        "columns": columns,
        "parameters": {"e": float(e), "threads": int(threads)},
    }


def create_db(
    ctx: Any,
    structures_dir: str | os.PathLike[str],
    db_path: str | os.PathLike[str],
    *,
    binary: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    """Create a Foldseek database from a directory of structures."""
    directory = Path(structures_dir)
    if not directory.is_dir():
        raise InvalidInputError(f"structures directory does not exist: {directory}")
    info = require_version(ctx, binary)
    db = Path(db_path)
    db.parent.mkdir(parents=True, exist_ok=True)
    argv = [info["path"], "createdb", str(directory), str(db)]
    if bool(getattr(ctx, "dry_run", False)):
        return {"dry_run": True, "db_path": str(db), "command": argv}
    proc = _run(ctx, argv, timeout=900.0)
    if proc.returncode != 0:
        raise LifesciError(
            f"Foldseek createdb failed (exit {proc.returncode}): {util.truncate((proc.stderr or '').strip(), 600)}",
            code="FOLDSEEK_CREATEDB_FAILED",
            retriable=False,
            details={"argv": argv, "exit_code": proc.returncode},
        )
    return {"dry_run": False, "db_path": str(db), "command": argv, "version": info.get("version")}


# ---------------------------------------------------------------------------
# web API
# ---------------------------------------------------------------------------


def _web_database_fields(databases: Iterable[str]) -> list[tuple[str, str]]:
    values = [str(db) for db in databases]
    if not values:
        raise InvalidInputError("at least one Foldseek database is required")
    return [("database[]", db) for db in values]


def web_submit(
    ctx: Any,
    *,
    pdb_bytes: bytes,
    filename: str,
    mode: str = "tmalign",
    databases: Sequence[str] = ("afdb50",),
    taxfilter: str | None = None,
    email: str | None = None,
) -> dict[str, Any]:
    """Submit a PDB upload to the Foldseek web API and return the ticket id.

    Only multipart PDB uploads work (see module docstring); the deterministic
    multipart boundary makes the request body reproducible.
    """
    if not pdb_bytes:
        raise InvalidInputError("Foldseek web submission needs non-empty PDB content")
    fields: list[tuple[str, str]] = [("mode", str(mode)), *_web_database_fields(databases)]
    if taxfilter:
        fields.append(("taxfilter", str(taxfilter)))
    if email:
        fields.append(("email", str(email)))
    files = [("q", str(filename or "query.pdb"), "chemical/x-pdb", pdb_bytes)]
    url = f"{WEB_BASE}/ticket"
    try:
        # POST must go through http.request directly (multipart upload).
        response = http.request(
            ctx,
            "POST",
            url,
            files=files,
            form_fields=fields,
            headers={"accept": "application/json"},
            cache=False,
            idempotent=False,
            retries=0,
            error_code="FOLDSEEK_WEB",
            database=DATABASE,
        )
        payload = response.json()
    except PortalError as err:
        if _common.is_http_status(err, 400):
            raise PortalError(
                "Foldseek web API rejected the submission (HTTP 400). " + WEB_JSON_BODY_REJECTED,
                code="FOLDSEEK_WEB_BAD_REQUEST",
                retriable=False,
                details=err.details,
                fallbacks=["submit a multipart PDB upload (this adapter does)", "use the local Foldseek panel"],
            ) from err
        raise
    if not isinstance(payload, Mapping):
        raise PortalError(
            "Foldseek web API returned an unexpected ticket payload",
            code="FOLDSEEK_WEB_PAYLOAD",
            retriable=False,
            details={"url": url, "body_preview": util.truncate(response.text(), 300)},
        )
    ticket = _common.first_text(_common.pick(payload, "id", "ticket", "job_id"))
    if not ticket:
        raise PortalError(
            "Foldseek web API did not return a ticket id",
            code="FOLDSEEK_WEB_PAYLOAD",
            retriable=False,
            details={"url": url, "body_preview": util.truncate(response.text(), 300)},
        )
    return {
        "ticket": ticket,
        "status": _common.first_text(_common.pick(payload, "status")) or "PENDING",
        "url": url,
        "databases": [str(db) for db in databases],
        "mode": str(mode),
    }


def web_status(ctx: Any, ticket: str) -> dict[str, Any]:
    """Poll one Foldseek ticket once (never cached)."""
    ticket = _common.require_identifier(str(ticket or "").strip(), "ticket")
    url = f"{WEB_BASE}/ticket/{ticket}"
    try:
        # Polling must not be cached; retries use the context policy (get_json has
        # no per-call retries kwarg - only http.request does).
        payload, _response = http.get_json(
            ctx,
            url,
            cache=False,
            error_code="FOLDSEEK_WEB",
            database=DATABASE,
            headers={"accept": "application/json"},
        )
    except PortalError as err:
        if _common.is_http_status(err, 404):
            raise NotFoundError(
                f"Foldseek ticket {ticket} not found (expired?)",
                details={"ticket": ticket, "url": url},
                fallbacks=["resubmit the search"],
            ) from err
        raise
    status = _common.first_text(_common.pick(payload, "status", "state")) or "UNKNOWN"
    return {"ticket": ticket, "status": status, "url": url}


def _normalise_web_hit(hit: Mapping[str, Any], *, database: str, rank: int) -> dict[str, Any]:
    target_raw = _common.first_text(_common.pick(hit, "target", "target_id", "db_id")) or ""
    score = util.parse_float(_common.pick(hit, "score", "tmScore", "qtmscore"))
    tm_score = util.parse_float(_common.pick(hit, "tm_score", "tmScore"))
    if tm_score is None and score is not None:
        # tmalign mode: server score = query-normalised TM x 100
        tm_score = score / 100.0 if score > 1.0 else score
    accession = None
    import re

    match = re.match(r"^[A-Z]{3}-([A-Z0-9]+)-F\\d+-model", target_raw)
    if match:
        accession = match.group(1)
    elif re.fullmatch(r"[0-9][A-Za-z0-9]{3}", target_raw.strip()):
        accession = target_raw.strip().upper()
    description = re.sub(r"^[A-Z]{3}-[A-Z0-9]+-F\\d+-model_v\\d+\\s*", "", target_raw).strip()
    return {
        "rank": int(rank),
        "db": database,
        "target_accession": accession,
        "target_raw": target_raw,
        "organism": _common.first_text(_common.pick(hit, "taxName", "organism")),
        "taxid": _common.to_int(_common.pick(hit, "taxId", "taxid")),
        "tm_score": round(tm_score, 4) if tm_score is not None else None,
        "prob": util.parse_float(_common.pick(hit, "prob")),
        "evalue": util.parse_float(_common.pick(hit, "eval", "evalue", "eValue")),
        "seqid": util.parse_float(_common.pick(hit, "seqId", "seqid", "fident")),
        "alnlen": _common.to_int(_common.pick(hit, "alnLength", "alnlen")),
        "qlen": _common.to_int(_common.pick(hit, "qLen", "qlen")),
        "tlen": _common.to_int(_common.pick(hit, "dbLen", "tlen")),
        "qstart": _common.to_int(_common.pick(hit, "qStartPos", "qstart")),
        "qend": _common.to_int(_common.pick(hit, "qEndPos", "qend")),
        "tstart": _common.to_int(_common.pick(hit, "dbStartPos", "tstart")),
        "tend": _common.to_int(_common.pick(hit, "dbEndPos", "tend")),
        "description": description or None,
    }


def web_result(
    ctx: Any,
    ticket: str,
    *,
    db_index: int = 0,
    database: str | None = None,
) -> dict[str, Any]:
    """Fetch and normalise one database's result JSON for a completed ticket."""
    ticket = _common.require_identifier(str(ticket or "").strip(), "ticket")
    db_index = max(0, int(db_index))
    url = f"{WEB_BASE}/result/{ticket}/{db_index}"
    try:
        payload, response = http.get_json(
            ctx,
            url,
            cache_ttl=365 * 24 * 3600.0,
            error_code="FOLDSEEK_WEB",
            database=DATABASE,
            headers={"accept": "application/json"},
        )
    except PortalError as err:
        if _common.is_http_status(err, 404):
            raise NotFoundError(
                f"Foldseek result for ticket {ticket} database index {db_index} is not available",
                details={"ticket": ticket, "db_index": db_index, "url": url},
            ) from err
        raise
    hits: list[dict[str, Any]] = []
    db_label = database or f"db{db_index}"
    try:
        results = _common.as_list(_common.pick(payload, "results"))
        if results:
            first = results[db_index] if db_index < len(results) else results[0]
            if isinstance(first, Mapping):
                db_label = _common.first_text(_common.pick(first, "db", "database", "key")) or db_label
                alignments = _common.pick(first, "alignments")
                raw_hits: list[Any] = []
                for block in _common.as_list(alignments):
                    # alignments is normally [[hit, hit, ...]] (one list per query)
                    for item in _common.as_list(block):
                        if isinstance(item, Mapping):
                            raw_hits.append(item)
                if not raw_hits and isinstance(alignments, list):
                    raw_hits = [item for item in alignments if isinstance(item, Mapping)]
                for rank, raw_hit in enumerate(raw_hits, 1):
                    hits.append(_normalise_web_hit(raw_hit, database=db_label, rank=rank))
    except (TypeError, ValueError, KeyError) as err:
        raise DataError(
            f"could not parse Foldseek result JSON for ticket {ticket}",
            details={"ticket": ticket, "db_index": db_index, "error": str(err)},
        ) from err
    return {
        "ticket": ticket,
        "db": db_label,
        "db_index": db_index,
        "n_hits": len(hits),
        "hits": hits,
        "sha256": util.sha256_bytes(response.body),
        "bytes": len(response.body),
        "url": url,
    }


def _default_sleep(seconds: float) -> None:  # pragma: no cover - trivial
    time.sleep(seconds)


def web_search(
    ctx: Any,
    *,
    pdb_bytes: bytes,
    filename: str,
    databases: Sequence[str] = ("afdb50",),
    poll_interval: float = 5.0,
    max_wait: float = 600.0,
    mode: str = "tmalign",
    taxfilter: str | None = None,
    _sleep: Callable[[float], None] | None = None,
    _monotonic: Callable[[], float] | None = None,
) -> dict[str, Any]:
    """Submit, poll, and fetch all requested databases for one PDB query.

    Returns combined hits labelled by database plus per-database sections.  Never
    compares E-values across databases; warns about the query-normalised TM and
    the server-side hit cap.
    """
    databases = [str(db) for db in databases] or ["afdb50"]
    sleeper = _sleep or getattr(ctx, "_sleeper", None) or _default_sleep
    clock = _monotonic or time.monotonic
    submitted = web_submit(
        ctx,
        pdb_bytes=pdb_bytes,
        filename=filename,
        mode=mode,
        databases=databases,
        taxfilter=taxfilter,
        email=getattr(ctx, "contact_email", None),
    )
    ticket = submitted["ticket"]
    status_payload = {"ticket": ticket, "status": submitted.get("status") or "PENDING"}
    started = clock()
    deadline = started + max(0.0, float(max_wait))
    interval = max(0.5, float(poll_interval))
    status = str(status_payload.get("status") or "").upper()
    while status not in {"COMPLETE", "ERROR"}:
        if clock() >= deadline:
            raise LifesciError(
                f"Foldseek web job {ticket} did not finish within {max_wait:.0f}s (last status {status or 'UNKNOWN'})",
                code="FOLDSEEK_WEB_TIMEOUT",
                retriable=True,
                details={"ticket": ticket, "last_status": status, "max_wait": max_wait},
                fallbacks=["retry with a larger max_wait", "use the local Foldseek panel"],
            )
        sleeper(interval)
        status_payload = web_status(ctx, ticket)
        status = str(status_payload.get("status") or "").upper()
    if status == "ERROR":
        raise PortalError(
            f"Foldseek web job {ticket} ended ERROR. Sequence/FASTA queries are known to fail; "
            "this search uploaded a PDB structure, so the database or server may be busy.",
            code="FOLDSEEK_WEB_JOB_ERROR",
            retriable=True,
            details={"ticket": ticket, "hint": WEB_SEQUENCE_QUERY_BROKEN},
            fallbacks=["resubmit later", "use the local Foldseek panel"],
        )

    results: list[dict[str, Any]] = []
    combined: list[dict[str, Any]] = []
    for index, database in enumerate(databases):
        db_result = web_result(ctx, ticket, db_index=index, database=database)
        results.append(db_result)
        combined.extend(db_result["hits"])
    ctx.warn_tm_query_normalised()
    ctx.warn_top_hit_cap("search.foldseek.com (" + ", ".join(databases) + ")", 1000)
    ctx.warn_evalue_db_dependence(
        "search.foldseek.com (" + ", ".join(databases) + ")",
        "Server E-values depend on the searched database size and the same pair can score very "
        "differently in a small local panel and in a global database; use prob + TM + control-relative "
        "margins instead.",
    )
    return {
        "ticket": ticket,
        "status": status,
        "mode": mode,
        "databases": databases,
        "results": results,
        "hits": combined,
        "n_hits": len(combined),
        "parameters": {"poll_interval": interval, "max_wait": float(max_wait), "taxfilter": taxfilter},
        "server": WEB_BASE,
    }
