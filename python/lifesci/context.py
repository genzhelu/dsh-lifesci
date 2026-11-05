"""Execution context for one lifesci command invocation.

A :class:`Context` bundles every piece of ambient state a command may touch:
cache location, provenance log, offline/dry-run flags, HTTP retry/rate-limit
policy, the injectable transport (used by tests to simulate portal failures),
and the mutable ``warnings`` / ``human_review`` / ``provenance`` lists that are
returned in the command envelope.

There is deliberately **no module-level global context**: every command receives
one explicitly, so tests and library users can run concurrently with different
network policies.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, MutableSequence

from . import __version__, paths, util
from .errors import ConfigError, UsageError

#: Conservative default per-host rate limits (requests/second, burst).
DEFAULT_RATE_LIMITS: dict[str, dict[str, float]] = {
    "rest.uniprot.org": {"rate": 3.0, "burst": 5.0},
    "ftp.uniprot.org": {"rate": 3.0, "burst": 5.0},
    "www.ebi.ac.uk": {"rate": 2.5, "burst": 5.0},
    "www.ebi.ac.uk:443": {"rate": 2.5, "burst": 5.0},
    "eutils.ncbi.nlm.nih.gov": {"rate": 2.5, "burst": 5.0},
    "www.ncbi.nlm.nih.gov": {"rate": 2.5, "burst": 5.0},
    "alphafold.ebi.ac.uk": {"rate": 2.0, "burst": 4.0},
    "api.esmatlas.com": {"rate": 0.5, "burst": 2.0},
    "search.foldseek.com": {"rate": 0.25, "burst": 1.0},
    "files.rcsb.org": {"rate": 5.0, "burst": 8.0},
    "www.ebi.ac.uk:80": {"rate": 2.5, "burst": 5.0},
    "data.orthodb.org": {"rate": 1.5, "burst": 3.0},
    "omabrowser.org": {"rate": 1.5, "burst": 3.0},
    "www.yeastgenome.org": {"rate": 2.0, "burst": 4.0},
    "default": {"rate": 2.0, "burst": 4.0},
}


def _env_number(environment: Mapping[str, str], name: str, default: float, cast: Any = float) -> Any:
    """Parse a numeric environment value from a supplied mapping (not os.environ)."""
    raw = environment.get(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        return cast(str(raw).strip())
    except (TypeError, ValueError):
        return default


@dataclass
class Context:
    """Resolved runtime configuration and per-command accumulators."""

    cache_dir: Path
    log_path: Path | None
    offline: bool = False
    dry_run: bool = False
    timeout: float = 60.0
    max_retries: int = 4
    retry_backoff: float = 1.0
    retry_backoff_max: float = 30.0
    cache_ttl: float = 30 * 24 * 3600.0
    user_agent: str = f"lifesci/{__version__} (+https://github.com/local/dsh-lifesci-plugin)"
    contact_email: str | None = None
    transport: Any = None  # lifesci.transport.Transport | None
    rate_limits: dict[str, dict[str, float]] = field(
        default_factory=lambda: dict(DEFAULT_RATE_LIMITS)
    )
    datasets_file: Path | None = None
    cwd: Path = field(default_factory=Path.cwd)
    env: dict[str, str] = field(default_factory=dict)
    verbose: bool = False

    # Accumulators returned in the envelope (populated by commands/adapters).
    warnings: list[dict[str, Any]] = field(default_factory=list)
    human_review: list[dict[str, Any]] = field(default_factory=list)
    provenance: list[dict[str, Any]] = field(default_factory=list)

    # ------------------------------------------------------------------
    # construction
    # ------------------------------------------------------------------
    @classmethod
    def create(
        cls,
        *,
        cache_dir: str | os.PathLike[str] | None = None,
        log_path: str | os.PathLike[str] | None = None,
        offline: bool = False,
        dry_run: bool = False,
        timeout: float | None = None,
        max_retries: int | None = None,
        cache_ttl: float | None = None,
        contact_email: str | None = None,
        transport: Any = None,
        cwd: str | os.PathLike[str] | None = None,
        env: Mapping[str, str] | None = None,
        verbose: bool = False,
        rate_limits: Mapping[str, Mapping[str, float]] | None = None,
    ) -> "Context":
        """Resolve a context from explicit arguments, environment, and defaults."""
        # Caller-supplied values layer on top of the process environment (like
        # subprocess env overrides); passing {} therefore means "inherit".
        environment = dict(os.environ)
        if env:
            environment.update({str(k): str(v) for k, v in env.items()})
        workdir = Path(cwd or environment.get("LIFESCI_CWD") or os.getcwd()).resolve()

        explicit_cache = cache_dir or environment.get("LIFESCI_CACHE_DIR")
        resolved_cache = (
            Path(explicit_cache).expanduser().resolve()
            if explicit_cache
            else paths.default_cache_dir(workdir)
        )
        explicit_log = log_path or environment.get("LIFESCI_LOG")
        resolved_log = (
            Path(explicit_log).expanduser().resolve()
            if explicit_log
            else paths.default_log_path(resolved_cache)
        )
        if str(environment.get("LIFESCI_OFFLINE", "")).lower() in {"1", "true", "yes", "on"}:
            offline = True
        if str(environment.get("LIFESCI_DRY_RUN", "")).lower() in {"1", "true", "yes", "on"}:
            dry_run = True

        # Explicit arguments (including 0 for cache_ttl = "always refetch") win,
        # then the supplied environment mapping, then the documented defaults.
        resolved_timeout = (
            timeout if timeout is not None else _env_number(environment, "LIFESCI_HTTP_TIMEOUT", 60.0)
        )
        resolved_retries = (
            max_retries
            if max_retries is not None
            else int(_env_number(environment, "LIFESCI_HTTP_RETRIES", 4, int))
        )
        resolved_ttl = (
            cache_ttl
            if cache_ttl is not None
            else _env_number(environment, "LIFESCI_CACHE_TTL", 30 * 24 * 3600.0)
        )

        limits = dict(DEFAULT_RATE_LIMITS)
        if rate_limits:
            for host, cfg in rate_limits.items():
                limits[host] = {"rate": float(cfg.get("rate", 2.0)), "burst": float(cfg.get("burst", 4.0))}

        ua = environment.get("LIFESCI_USER_AGENT") or (
            f"lifesci/{__version__} (+https://github.com/local/dsh-lifesci-plugin)"
        )
        email = contact_email or environment.get("LIFESCI_CONTACT_EMAIL")
        datasets_file = paths.default_datasets_file(resolved_cache)
        datasets_override = environment.get("LIFESCI_DATASETS")
        if datasets_override:
            datasets_file = Path(datasets_override).expanduser().resolve()

        # Matplotlib writing to a read-only ``$HOME/.config`` is common in
        # sandboxed agent runs; point its cache inside our writable cache dir.
        if not environment.get("MPLCONFIGDIR"):
            candidate = resolved_cache / "matplotlib"
            try:
                candidate.mkdir(parents=True, exist_ok=True)
                os.environ["MPLCONFIGDIR"] = str(candidate)
            except OSError:  # pragma: no cover - non-fatal
                pass

        return cls(
            cache_dir=resolved_cache,
            log_path=resolved_log,
            offline=offline,
            dry_run=dry_run,
            timeout=resolved_timeout,
            max_retries=max(0, int(resolved_retries)),
            cache_ttl=resolved_ttl,
            user_agent=ua,
            contact_email=email,
            transport=transport,
            rate_limits=limits,
            datasets_file=datasets_file,
            cwd=workdir,
            env=environment,
            verbose=verbose,
        )

    # ------------------------------------------------------------------
    # accumulators
    # ------------------------------------------------------------------
    def warn(
        self,
        code: str,
        message: str,
        *,
        severity: str = "warning",
        context: Mapping[str, Any] | None = None,
    ) -> None:
        """Record a machine-readable warning in this command's envelope."""
        entry: dict[str, Any] = {"code": code, "message": message, "severity": severity}
        if context:
            entry["context"] = util.json_safe(dict(context))
        # De-duplicate exact repeats but keep first occurrence order.
        for existing in self.warnings:
            if existing == entry:
                return
        self.warnings.append(entry)

    def review(
        self,
        code: str,
        message: str,
        *,
        severity: str = "required",
        context: Mapping[str, Any] | None = None,
    ) -> None:
        """Flag a result that needs human review (single weak line of evidence)."""
        entry: dict[str, Any] = {"code": code, "message": message, "severity": severity}
        if context:
            entry["context"] = util.json_safe(dict(context))
        if entry not in self.human_review:
            self.human_review.append(entry)

    def warn_evalue_db_dependence(self, database: str | None, hint: str | None = None) -> None:
        """Standard guardrail warning."""
        message = (
            "E-values are database-size dependent; the same pair can score differently in "
            "another database. Do not compare E-values across databases."
        )
        if hint:
            message += " " + hint
        self.warn(
            "EVALUE_NOT_COMPARABLE_ACROSS_DATABASES",
            message,
            context={"database": database},
        )

    def warn_tm_query_normalised(self, query_length: int | None = None) -> None:
        self.warn(
            "TM_SCORE_QUERY_NORMALISED",
            "TM-scores are normalised by query length: a short or fragment query scores higher "
            "than the same structural match queried with the full-length protein. Always run "
            "the reciprocal search when excluding a homologue.",
            context={"query_length": query_length},
        )

    def warn_top_hit_cap(self, database: str, cap: int | None = None) -> None:
        self.warn(
            "TOP_HIT_LIST_CAPPED",
            "Search results are capped by the server; absence from the returned list is not "
            "proof of absence. Report 'not recovered in top-N' with the cap.",
            context={"database": database, "cap": cap},
        )

    # ------------------------------------------------------------------
    # validation
    # ------------------------------------------------------------------
    def validate(self) -> None:
        if self.cache_dir.exists() and not self.cache_dir.is_dir():
            raise ConfigError(
                f"cache dir exists but is not a directory: {self.cache_dir}",
                details={"cache_dir": str(self.cache_dir)},
            )
        if self.timeout <= 0:
            raise ConfigError("timeout must be > 0")


def make_context(args: Any, *, transport: Any = None) -> Context:
    """Build a context from a parsed CLI namespace (shared by cli and tests)."""
    return Context.create(
        cache_dir=getattr(args, "cache_dir", None),
        log_path=getattr(args, "log", None),
        offline=bool(getattr(args, "offline", False)),
        dry_run=bool(getattr(args, "dry_run", False)),
        timeout=getattr(args, "timeout", None),
        max_retries=getattr(args, "retries", None),
        cache_ttl=getattr(args, "cache_ttl", None),
        cwd=getattr(args, "cwd", None),
        verbose=bool(getattr(args, "verbose", False)),
        transport=transport,
    )
