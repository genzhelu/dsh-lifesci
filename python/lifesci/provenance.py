"""Provenance logging: JSONL request log, checksums, and database release pinning.

Every HTTP fetch, local-file read, command run, and external-tool invocation is
recorded with enough context to re-derive the result later:
URL, parameters, status, payload SHA-256, byte count, retrieval time, database
release, retry count, cache hit/miss, and the command that consumed it.

Secrets are redacted before anything is written; request bodies are recorded by
SHA-256 only (never verbatim) so tokens cannot leak into the log.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import Any, Iterable, Mapping

from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from . import __version__, util

#: Keys whose values are never written to the provenance log.
_SECRET_HINTS = ("token", "secret", "password", "api_key", "apikey", "authorization", "cookie")


def new_request_id() -> str:
    """Short unique id linking a log line to an envelope provenance entry."""
    return uuid.uuid4().hex[:16]


def _is_secret_key(key: Any) -> bool:
    lowered = str(key).lower()
    return any(hint in lowered for hint in _SECRET_HINTS)


def _redact_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return redact(value)
    if isinstance(value, (list, tuple)):
        return [_redact_value(item) for item in value]
    return util.json_safe(value)


def redact(mapping: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """Return a copy of ``mapping`` with credential-like values masked (recursive)."""
    if not mapping:
        return None
    out: dict[str, Any] = {}
    for key, value in mapping.items():
        if _is_secret_key(key):
            out[str(key)] = "***redacted***"
        else:
            out[str(key)] = _redact_value(value)
    return out


def redact_url(url: str | None) -> str | None:
    """Mask userinfo and credential-like query parameter values in a URL."""
    if not url:
        return url
    try:
        parts = urlsplit(str(url))
    except ValueError:
        return str(url)
    netloc = parts.netloc
    if "@" in netloc:
        # Keep the host, drop the userinfo.
        netloc = "***@" + netloc.rsplit("@", 1)[1]
    query = urlencode(
        [
            (key, "***redacted***" if _is_secret_key(key) else value)
            for key, value in parse_qsl(parts.query, keep_blank_values=True)
        ]
    )
    return urlunsplit((parts.scheme, netloc, parts.path, query, parts.fragment))


_URL_KEYS = {"url", "final_url", "source_url", "referer", "href"}


def _sanitise_record(record: Mapping[str, Any]) -> dict[str, Any]:
    """Recursively redact secrets and URL credentials from a provenance record."""
    out: dict[str, Any] = {}
    for key, value in record.items():
        if _is_secret_key(key):
            out[str(key)] = "***redacted***"
        elif isinstance(value, Mapping):
            out[str(key)] = _sanitise_record(value)
        elif isinstance(value, (list, tuple)):
            out[str(key)] = [
                _sanitise_record(item) if isinstance(item, Mapping) else _redact_value(item)
                for item in value
            ]
        elif str(key).lower() in _URL_KEYS and isinstance(value, str):
            out[str(key)] = redact_url(value)
        else:
            out[str(key)] = util.json_safe(value)
    return out


class ProvenanceLog:
    """Append-only JSONL provenance log plus in-memory entries for the envelope."""

    def __init__(self, path: str | os.PathLike[str] | None):
        self.path = Path(path).expanduser().resolve() if path else None
        self.entries: list[dict[str, Any]] = []
        self._command_started = False

    # ------------------------------------------------------------------
    # low-level
    # ------------------------------------------------------------------
    def _write(self, record: Mapping[str, Any]) -> dict[str, Any]:
        entry = {
            "time": util.now_iso(),
            "pid": os.getpid(),
            "lifesci_version": __version__,
            **_sanitise_record(dict(record)),
        }
        self.entries.append(entry)
        if self.path is not None:
            try:
                util.append_jsonl(self.path, entry)
            except OSError:
                # A read-only log location must never fail the scientific work;
                # the in-memory provenance is still returned in the envelope.
                pass
        return entry

    # ------------------------------------------------------------------
    # record types
    # ------------------------------------------------------------------
    def http(
        self,
        *,
        method: str,
        url: str,
        params: Mapping[str, Any] | None = None,
        status: int | None,
        sha256: str | None,
        bytes_: int | None,
        cache: str,
        database: str | None = None,
        release: str | None = None,
        retries: int = 0,
        elapsed_ms: float | None = None,
        request_id: str | None = None,
        content_type: str | None = None,
        final_url: str | None = None,
        warnings: Iterable[str] | None = None,
        error: str | None = None,
    ) -> dict[str, Any]:
        return self._write(
            {
                "kind": "http",
                "request_id": request_id or new_request_id(),
                "method": method.upper(),
                "url": url,
                "final_url": final_url,
                "params": redact(params),
                "status": status,
                "sha256": sha256,
                "bytes": bytes_,
                "cache": cache,  # hit | miss | store | disabled | dry_run
                "database": database,
                "release": release,
                "retries": retries,
                "elapsed_ms": round(elapsed_ms, 1) if elapsed_ms is not None else None,
                "content_type": content_type,
                "warnings": list(warnings or []) or None,
                "error": error,
            }
        )

    def local_file(
        self,
        *,
        path: str | os.PathLike[str],
        sha256: str,
        bytes_: int,
        dataset: str | None = None,
        kind: str = "local_dataset",
        notes: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self._write(
            {
                "kind": kind,
                "path": str(path),
                "sha256": sha256,
                "bytes": bytes_,
                "dataset": dataset,
                "notes": util.json_safe(dict(notes)) if notes else None,
            }
        )

    def tool(
        self,
        *,
        name: str,
        version: str | None,
        argv: list[str] | None = None,
        exit_code: int | None = None,
        elapsed_ms: float | None = None,
        notes: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self._write(
            {
                "kind": "tool",
                "name": name,
                "version": version,
                "argv": argv,
                "exit_code": exit_code,
                "elapsed_ms": round(elapsed_ms, 1) if elapsed_ms is not None else None,
                "notes": util.json_safe(dict(notes)) if notes else None,
            }
        )

    def command(
        self,
        *,
        command: str,
        args: Mapping[str, Any] | None,
        ok: bool,
        started_at: str,
        finished_at: str,
        duration_s: float,
        cache_dir: str = "",
        offline: bool = False,
        dry_run: bool = False,
        error_code: str | None = None,
        warnings: int = 0,
    ) -> dict[str, Any]:
        return self._write(
            {
                "kind": "command",
                "command": command,
                "ok": ok,
                "started_at": started_at,
                "finished_at": finished_at,
                "duration_s": round(duration_s, 3),
                "cache_dir": cache_dir,
                "offline": offline,
                "dry_run": dry_run,
                "args": redact(args),
                "error_code": error_code,
                "n_warnings": warnings,
            }
        )


# ---------------------------------------------------------------------------
# release pinning
# ---------------------------------------------------------------------------


def release_registry_path(cache_dir: str | os.PathLike[str]) -> Path:
    return Path(cache_dir).expanduser().resolve() / "releases.json"


def record_release(
    cache_dir: str | os.PathLike[str],
    *,
    database: str,
    release: str | None,
    source_url: str | None = None,
    observed_at: str | None = None,
) -> dict[str, Any] | None:
    """Record/update the observed release of a database (``2026_04`` etc.)."""
    if not release:
        return None
    path = release_registry_path(cache_dir)
    registry: dict[str, Any] = util.read_json(path, {}) or {}
    observed = observed_at or util.now_iso()
    previous = registry.get(database) or {}
    registry[database] = {
        "release": str(release),
        "first_seen": previous.get("first_seen") or observed,
        "last_seen": observed,
        "source_url": source_url or previous.get("source_url"),
        "observations": int(previous.get("observations") or 0) + 1,
    }
    try:
        util.atomic_write_json(path, registry)
    except OSError:
        return registry[database]
    return registry[database]


def load_releases(cache_dir: str | os.PathLike[str]) -> dict[str, Any]:
    return util.read_json(release_registry_path(cache_dir), {}) or {}


# ---------------------------------------------------------------------------
# checksums / file manifests
# ---------------------------------------------------------------------------


def file_manifest(
    paths: Iterable[str | os.PathLike[str]],
    *,
    labels: Mapping[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Build ``{path, sha256, bytes}`` entries for a list of existing files."""
    manifest: list[dict[str, Any]] = []
    for path in paths:
        target = Path(path)
        if not target.is_file():
            manifest.append({"path": str(target), "missing": True})
            continue
        manifest.append(
            {
                "path": str(target),
                "sha256": util.sha256_file(target),
                "bytes": target.stat().st_size,
                "label": (labels or {}).get(str(target)),
            }
        )
    return manifest
