"""HMMER searches via pyhmmer (phmmer / hmmsearch) and reciprocal best hits.

Critical quirk
--------------
**``bias_filter=False`` is the default for phmmer.**  HMMER's compositional
bias filter can hide genuinely divergent homologues and can also admit
low-complexity false positives; hits should always be reviewed with domain
evidence.  The library therefore defaults to ``bias_filter=False`` and emits a
``PHMMER_BIAS_FILTER_OFF`` warning so results are never mistaken for a
filtered search.

E-values are database-size dependent.  ``hmmsearch`` returns
``n_targets`` so the caller can warn with :func:`warn_evalue_database_size`;
the same Pfam model searched against one sequence vs a full proteome gives
E-values that differ by orders of magnitude for the same protein.

pyhmmer/HMMER writes no stdout during search; results are parsed from
:class:`pyhmmer.plan7.TopHits`.  gzipped ``.hmm.gz`` model files are read
transparently by pyhmmer's ``HMMFile``.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from ..errors import DataError, ToolMissingError

_ACCESSION_RE = re.compile(r"^(?:sp|tr)\|([^|]+)\|")
_MODEL_VERSION_RE = re.compile(r"\.\d+$")
_QUERY_KEYS = ("query", "query_name", "q", "qname")
_TARGET_KEYS = ("target", "target_name", "t", "hit", "best_hit", "target_id")
_EVALUE_KEYS = ("evalue", "e_value", "E")
_SCORE_KEYS = ("score", "bits", "bitscore")


# ---------------------------------------------------------------------------
# tool availability
# ---------------------------------------------------------------------------


def require_pyhmmer() -> Any:
    """Import and return :mod:`pyhmmer`, raising an actionable error if missing."""
    try:
        import pyhmmer
    except ImportError as err:  # pragma: no cover - dependency is installed
        raise ToolMissingError(
            "pyhmmer is required for sequence/HMM searches but is not installed",
            details={"package": "pyhmmer", "required_version": "0.12.3"},
            fallbacks=[
                "pip install -r requirements.txt (pyhmmer==0.12.3)",
                "use a precomputed hits table instead (e.g. --from-table)",
            ],
        ) from err
    return pyhmmer


def hmmer_version() -> str:
    """Return the installed pyhmmer/HMMER version string."""
    pyhmmer = require_pyhmmer()
    return str(getattr(pyhmmer, "__version__", "unknown"))


def _decode(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return "" if value is None else str(value)


def _target_fasta_block(path: str | Path, alphabet: Any) -> tuple[Any, int]:
    """Read a target FASTA into a digital sequence block, returning (db, n)."""
    pyhmmer = require_pyhmmer()
    from pyhmmer import easel

    target = Path(path)
    if not target.is_file():
        raise DataError(
            f"target FASTA not found: {target}",
            details={"path": str(target)},
        )
    try:
        with easel.SequenceFile(str(target), digital=True, alphabet=alphabet) as handle:
            block = easel.DigitalSequenceBlock(alphabet, handle)
    except Exception as err:
        raise DataError(
            f"failed to read target sequences from {target}: {err}",
            details={"path": str(target)},
        ) from err
    if len(block) == 0:
        raise DataError(
            f"target FASTA contains no sequences: {target}",
            details={"path": str(target)},
        )
    return block, len(block)


def _load_hmms(hmm_paths: Sequence[str | Path]) -> list[Any]:
    """Load all HMMs from the given paths (gzipped files supported)."""
    require_pyhmmer()
    from pyhmmer.plan7 import HMMFile

    hmms: list[Any] = []
    for raw_path in hmm_paths:
        path = Path(raw_path)
        if not path.is_file():
            raise DataError(
                f"HMM file not found: {path}", details={"path": str(path)}
            )
        try:
            with HMMFile(str(path)) as handle:
                for hmm in handle:
                    hmms.append(hmm)
        except Exception as err:
            raise DataError(
                f"failed to read HMM file {path}: {err}",
                details={"path": str(path)},
            ) from err
    if not hmms:
        raise DataError("no HMM models loaded from the given paths")
    return hmms


def _model_id(hmm: Any) -> str:
    for value in (hmm.accession, hmm.name):
        text = _decode(value)
        if text:
            return _MODEL_VERSION_RE.sub("", text)
    return "hmm"


def _best_domain_span(hit: Any) -> tuple[int | None, int | None, int | None, int | None]:
    """Return ``(hmm_from, hmm_to, target_from, target_to)`` of the best domain."""
    try:
        alignment = hit.best_domain.alignment
    except Exception:  # pragma: no cover - hits always carry a domain
        return None, None, None, None
    try:
        return (
            int(alignment.hmm_from),
            int(alignment.hmm_to),
            int(alignment.target_from),
            int(alignment.target_to),
        )
    except Exception:  # pragma: no cover
        return None, None, None, None


# ---------------------------------------------------------------------------
# phmmer
# ---------------------------------------------------------------------------


def phmmer(
    ctx: Any,
    *,
    queries: Mapping[str, str] | Sequence[tuple[str, str]],
    targets_fasta: str | Path,
    E: float = 10.0,
    cpus: int = 4,
    bias_filter: bool = False,
    max_hits: int = 50,
) -> dict[str, Any]:
    """Search protein queries against a target FASTA with pyhmmer ``phmmer``.

    Parameters
    ----------
    ctx:
        :class:`lifesci.context.Context` (for the mandatory bias-filter warning)
        or ``None``.
    queries:
        ``{name: sequence}`` (mapping) or ``[(name, sequence), ...]``.
    targets_fasta:
        Protein FASTA searched against (one entry per target protein).
    E:
        Reporting E-value cutoff (default 10.0).
    cpus:
        Worker threads passed to HMMER.
    bias_filter:
        **Default False** (recommended): compositional-bias filtering hides
        divergent homologues.  A warning is recorded when disabled.
    max_hits:
        Maximum hits reported per query.

    Returns
    -------
    dict
        ``{"hits": [{"query","target","target_desc","evalue","score","bias",
        "query_length","target_length","qcov","tcov","alnlen","qstart",
        "qend","tstart","tend"}], "version", "n_targets", "params"}``.
    """
    pyhmmer = require_pyhmmer()
    from pyhmmer import easel

    alphabet = easel.Alphabet.amino()
    if isinstance(queries, Mapping):
        query_items = [(str(name), str(sequence)) for name, sequence in queries.items()]
    else:
        query_items = [(str(name), str(sequence)) for name, sequence in queries]
    if not query_items:
        raise DataError("phmmer needs at least one query sequence")
    digital_queries = []
    for name, sequence in query_items:
        if not sequence:
            raise DataError(f"query {name!r} has an empty sequence")
        try:
            digital = easel.TextSequence(name=name.encode(), sequence=sequence).digitize(
                alphabet
            )
        except Exception as err:
            raise DataError(
                f"query {name!r} is not a valid protein sequence: {err}",
                details={"query": name},
            ) from err
        digital_queries.append(digital)
    target_block, n_targets = _target_fasta_block(targets_fasta, alphabet)
    if not bias_filter and ctx is not None and hasattr(ctx, "warn"):
        ctx.warn(
            "PHMMER_BIAS_FILTER_OFF",
            (
                "phmmer ran with bias_filter=False: HMMER's compositional-bias "
                "filter is DISABLED. This is the recommended setting (the filter can "
                "hide genuinely divergent homologues) but it also admits low-complexity "
                "false positives — always review hits with domain/family evidence and "
                "reciprocal searches."
            ),
            context={"E": float(E), "cpus": int(cpus)},
        )
    try:
        result_stream = pyhmmer.phmmer(
            digital_queries,
            target_block,
            E=E,
            cpus=max(1, int(cpus)),
            bias_filter=bool(bias_filter),
        )
    except Exception as err:  # pragma: no cover - HMMER failures are rare
        raise DataError(
            f"phmmer search failed: {err}",
            details={"n_queries": len(query_items), "n_targets": n_targets},
        ) from err
    query_lengths = {name: len(sequence) for name, sequence in query_items}
    hits: list[dict[str, Any]] = []
    for top_hits in result_stream:
        query_name = _decode(getattr(top_hits.query, "name", b""))
        query_length = query_lengths.get(query_name, getattr(top_hits.query, "length", 0))
        ordered = sorted(top_hits, key=lambda hit: float(hit.evalue))
        for hit in ordered[: max(0, int(max_hits))]:
            hmm_from, hmm_to, target_from, target_to = _best_domain_span(hit)
            target_length = int(getattr(hit, "length", 0) or 0)
            aligned_length = (
                int(target_to - target_from + 1)
                if target_from is not None and target_to is not None
                else 0
            )
            query_span = (
                int(hmm_to - hmm_from + 1)
                if hmm_from is not None and hmm_to is not None
                else 0
            )
            hits.append(
                {
                    "query": query_name,
                    "target": _decode(hit.name),
                    "target_desc": _decode(hit.description),
                    "evalue": float(hit.evalue),
                    "score": float(hit.score),
                    "bias": float(getattr(hit, "bias", 0.0) or 0.0),
                    "query_length": int(query_length),
                    "target_length": target_length,
                    "qcov": round(query_span / query_length, 4)
                    if query_length
                    else 0.0,
                    "tcov": round(aligned_length / target_length, 4)
                    if target_length
                    else 0.0,
                    "alnlen": aligned_length,
                    "qstart": hmm_from,
                    "qend": hmm_to,
                    "tstart": target_from,
                    "tend": target_to,
                }
            )
    hits.sort(
        key=lambda hit: (
            float(hit["evalue"]),
            hit["query"],
            hit["target"],
        )
    )
    return {
        "hits": hits,
        "version": hmmer_version(),
        "n_targets": n_targets,
        "params": {
            "program": "pyhmmer phmmer",
            "E": float(E),
            "bias_filter": bool(bias_filter),
            "cpus": int(cpus),
            "max_hits": int(max_hits),
            "n_queries": len(query_items),
            "n_targets": n_targets,
        },
    }


# ---------------------------------------------------------------------------
# hmmsearch
# ---------------------------------------------------------------------------


def hmmsearch(
    ctx: Any,
    *,
    hmm_paths: Sequence[str | Path],
    targets_fasta: str | Path,
    E: float = 10.0,
    cpus: int = 4,
    bias_filter: bool = False,
    max_hits: int = 100,
) -> dict[str, Any]:
    """Search HMM models against a target FASTA with pyhmmer ``hmmsearch``.

    Parameters
    ----------
    ctx:
        :class:`lifesci.context.Context` (may be ``None``); used for warnings.
    hmm_paths:
        HMM files (``.hmm`` or gzipped ``.hmm.gz``); each may contain one or
        more models.
    targets_fasta:
        Protein FASTA searched against.  ``n_targets`` is returned so callers
        can explain that E-values scale with database size.
    E:
        Reporting E-value cutoff (default 10.0).
    cpus:
        Worker threads passed to HMMER.
    bias_filter:
        Default False (recommended; same rationale as :func:`phmmer`).
    max_hits:
        Maximum hits reported per model.

    Returns
    -------
    dict
        ``{"hits": [{"model","model_name","model_accession","target",
        "target_desc","evalue","score","bias","model_cov","target_length"}],
        "version", "n_targets", "E_cutoff", "bias_filter", "params"}``.
    """
    pyhmmer = require_pyhmmer()
    from pyhmmer import easel

    alphabet = easel.Alphabet.amino()
    hmms = _load_hmms(hmm_paths)
    target_block, n_targets = _target_fasta_block(targets_fasta, alphabet)
    hits: list[dict[str, Any]] = []
    for hmm in hmms:
        try:
            result_stream = pyhmmer.hmmsearch(
                [hmm],
                target_block,
                E=E,
                cpus=max(1, int(cpus)),
                bias_filter=bool(bias_filter),
            )
        except Exception as err:  # pragma: no cover
            raise DataError(
                f"hmmsearch failed for model {_model_id(hmm)!r}: {err}",
                details={"model": _model_id(hmm), "n_targets": n_targets},
            ) from err
        model_length = int(getattr(hmm, "M", 0) or 0)
        model_id = _model_id(hmm)
        model_name = _decode(getattr(hmm, "name", b""))
        for top_hits in result_stream:
            ordered = sorted(top_hits, key=lambda hit: float(hit.evalue))
            for hit in ordered[: max(0, int(max_hits))]:
                hmm_from, hmm_to, target_from, target_to = _best_domain_span(hit)
                target_length = int(getattr(hit, "length", 0) or 0)
                model_cov = 0.0
                if (
                    model_length
                    and hmm_from is not None
                    and hmm_to is not None
                ):
                    model_cov = round((hmm_to - hmm_from + 1) / model_length, 3)
                hits.append(
                    {
                        "model": model_id,
                        "model_name": model_name,
                        "model_accession": model_id,
                        "target": _decode(hit.name),
                        "target_desc": _decode(hit.description),
                        "evalue": float(hit.evalue),
                        "score": float(hit.score),
                        "bias": float(getattr(hit, "bias", 0.0) or 0.0),
                        "model_cov": model_cov,
                        "target_length": target_length,
                        "tstart": target_from,
                        "tend": target_to,
                    }
                )
    hits.sort(
        key=lambda hit: (
            float(hit["evalue"]),
            hit["model"],
            hit["target"],
        )
    )
    return {
        "hits": hits,
        "version": hmmer_version(),
        "n_targets": n_targets,
        "E_cutoff": float(E),
        "bias_filter": bool(bias_filter),
        "params": {
            "program": "pyhmmer hmmsearch",
            "E": float(E),
            "bias_filter": bool(bias_filter),
            "cpus": int(cpus),
            "max_hits": int(max_hits),
            "n_models": len(hmms),
            "n_targets": n_targets,
        },
    }


# ---------------------------------------------------------------------------
# reciprocal best hits
# ---------------------------------------------------------------------------


def _norm_id(value: Any) -> str:
    """Normalise a sequence identifier for cross-direction comparison.

    Handles ``sp|P69905|HBA_HUMAN``, ``HBA=P69905`` and plain accessions.
    """
    text = _decode(value).strip()
    if not text:
        return ""
    match = _ACCESSION_RE.match(text)
    if match:
        return match.group(1)
    if "=" in text:
        return text.rsplit("=", 1)[1].strip()
    if "|" in text:
        parts = [part for part in text.split("|") if part]
        if len(parts) >= 2:
            return parts[1]
    return text.split()[0]


def _hit_value(hit: Mapping[str, Any], keys: Sequence[str]) -> Any:
    for key in keys:
        if key in hit and hit[key] not in (None, ""):
            return hit[key]
    return None


def _hit_evalue(hit: Mapping[str, Any]) -> float | None:
    value = _hit_value(hit, _EVALUE_KEYS)
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number:  # NaN
        return None
    return number


def canonicalise_hit(hit: Mapping[str, Any]) -> dict[str, Any] | None:
    """Normalise one hit row (toleration of extra/reordered columns)."""
    query = _hit_value(hit, _QUERY_KEYS)
    target = _hit_value(hit, _TARGET_KEYS)
    if query is None or target is None:
        return None
    query_id = _norm_id(query)
    target_id = _norm_id(target)
    if not query_id or not target_id or target_id in {"-", "NA"}:
        return None
    canonical = dict(hit)
    canonical["query"] = query_id
    canonical["target"] = target_id
    canonical["query_raw"] = _decode(query)
    canonical["target_raw"] = _decode(target)
    canonical["evalue"] = _hit_evalue(hit)
    score = _hit_value(hit, _SCORE_KEYS)
    if score is not None:
        try:
            canonical["score"] = float(score)
        except (TypeError, ValueError):
            pass
    return canonical


def _hit_rows(hits: Any) -> list[dict[str, Any]]:
    """Flatten the supported hit-collection forms into a list of hit dicts."""
    if hits is None:
        return []
    if isinstance(hits, Mapping):
        rows: list[dict[str, Any]] = []
        for key, value in hits.items():
            if isinstance(value, Mapping):
                row = dict(value)
                row.setdefault("query", key)
                rows.append(row)
            elif isinstance(value, (list, tuple)):
                for item in value:
                    if isinstance(item, Mapping):
                        row = dict(item)
                        row.setdefault("query", key)
                        rows.append(row)
        return rows
    rows = []
    for item in hits:
        if isinstance(item, Mapping):
            rows.append(dict(item))
    return rows


def best_hit_per_query(
    hits: Any,
    *,
    query_keys: Sequence[str] = _QUERY_KEYS,
    target_keys: Sequence[str] = _TARGET_KEYS,
) -> dict[str, dict[str, Any]]:
    """Return the lowest-E-value hit per query.

    Tolerant of extra columns and of the recorded-table variants (e.g.
    ``query`` / ``best_hit``, ``HBA=P69905`` identifiers).  Rows without
    a parseable E-value are ignored; ties break deterministically by target id.
    """
    best: dict[str, dict[str, Any]] = {}
    best_key: dict[str, tuple[float, str]] = {}
    for row in _hit_rows(hits):
        canonical = canonicalise_hit(row)
        if canonical is None:
            continue
        query = canonical["query"]
        evalue = canonical["evalue"]
        if evalue is None:
            evalue = float("inf")
        key = (float(evalue), str(canonical["target"]))
        if query not in best_key or key < best_key[query]:
            best_key[query] = key
            best[query] = canonical
    return best


def reciprocal_best(
    ctx: Any,
    forward_hits: Any,
    reverse_hits: Any,
) -> dict[str, Any]:
    """Compare forward and reverse searches and report reciprocal best hits.

    Parameters
    ----------
    ctx:
        :class:`lifesci.context.Context` or ``None`` (reserved for warnings).
    forward_hits:
        Forward search hits (list of hit dicts, or mapping query -> hit(s)).
    reverse_hits:
        Reverse search hits in the same forms, **or** a callable that receives
        the forward-hit list and returns the reverse hits (e.g. a function that
        runs the reverse phmmer search).

    Returns
    -------
    dict
        ``{"pairs": [{"query","target","forward_evalue","reverse_evalue",
        "reciprocal","direction"}], "n_reciprocal", "n_forward_only",
        "n_reverse_only", "n_queries", "n_targets"}``.  Pairs are sorted by
        query then target.  A pair is reciprocal when the forward best target's
        reverse best hit is the original query (identifiers normalised:
        ``sp|ACC|NAME`` and ``NAME=ACC`` are compared by accession).
    """
    forward_rows = _hit_rows(forward_hits)
    if callable(reverse_hits):
        reverse_rows = _hit_rows(reverse_hits(forward_rows))
    else:
        reverse_rows = _hit_rows(reverse_hits)
    forward_best = best_hit_per_query(forward_rows)
    reverse_best = best_hit_per_query(reverse_rows)
    reverse_targets = {canonical["target"] for canonical in reverse_best.values()}
    pairs: list[dict[str, Any]] = []
    reciprocal_queries: set[str] = set()
    for query in sorted(forward_best):
        forward = forward_best[query]
        target = forward["target"]
        reverse = reverse_best.get(target)
        is_reciprocal = reverse is not None and reverse["target"] == query
        if is_reciprocal:
            reciprocal_queries.add(query)
        pairs.append(
            {
                "query": query,
                "target": target,
                "query_raw": forward.get("query_raw", query),
                "target_raw": forward.get("target_raw", target),
                "forward_evalue": forward.get("evalue"),
                "forward_score": forward.get("score"),
                "reverse_evalue": None if reverse is None else reverse.get("evalue"),
                "reverse_score": None if reverse is None else reverse.get("score"),
                "reciprocal": bool(is_reciprocal),
                "direction": "forward",
            }
        )
    for target in sorted(reverse_best):
        reverse = reverse_best[target]
        query = reverse["target"]
        if forward_best.get(query, {}).get("target") == target:
            continue  # already represented as a reciprocal forward pair
        pairs.append(
            {
                "query": query,
                "target": target,
                "query_raw": reverse.get("query_raw", query),
                "target_raw": reverse.get("target_raw", target),
                "forward_evalue": None,
                "forward_score": None,
                "reverse_evalue": reverse.get("evalue"),
                "reverse_score": reverse.get("score"),
                "reciprocal": False,
                "direction": "reverse",
            }
        )
    pairs.sort(key=lambda pair: (pair["query"], pair["target"], pair["direction"]))
    n_reciprocal = sum(1 for pair in pairs if pair["reciprocal"])
    n_forward_only = sum(
        1 for pair in pairs if pair["direction"] == "forward" and not pair["reciprocal"]
    )
    n_reverse_only = sum(1 for pair in pairs if pair["direction"] == "reverse")
    return {
        "pairs": pairs,
        "n_reciprocal": n_reciprocal,
        "n_forward_only": n_forward_only,
        "n_reverse_only": n_reverse_only,
        "n_queries": len(forward_best),
        "n_targets": len(reverse_targets),
        "forward_best": forward_best,
        "reverse_best": reverse_best,
    }


# ---------------------------------------------------------------------------
# E-value database-size warning helper
# ---------------------------------------------------------------------------


def warn_evalue_database_size(
    ctx: Any,
    *,
    database: str,
    n_targets: int,
    compared_to: int | None = None,
    example: str | None = None,
) -> None:
    """Record the mandatory E-value/database-size warning.

    A domain search against one protein and against a full proteome produce
    E-values that differ by orders of magnitude for the same match.  Never
    compare E-values across differently-sized databases.
    """
    if ctx is None or not hasattr(ctx, "warn"):
        return
    message = (
        f"E-values scale with the searched database size: {database} contained "
        f"n_targets={int(n_targets)}"
        + (
            f" (compared with n_targets={int(compared_to)})"
            if compared_to is not None
            else ""
        )
        + ". The same match scores differently against a single sequence and a "
        "full proteome — never compare E-values across database sizes; use "
        "coverage, domain architecture and reciprocal searches instead."
    )
    if example:
        message += " " + example
    ctx.warn(
        "EVALUE_DATABASE_SIZE_DEPENDENT",
        message,
        context={
            "database": database,
            "n_targets": int(n_targets),
            "compared_to": None if compared_to is None else int(compared_to),
        },
    )
