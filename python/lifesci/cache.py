"""Content-addressed HTTP cache with TTL, integrity verification, and offline replay.

Layout::

    <cache>/cache.json                     format marker
    <cache>/v1/objects/<aa>/<key>.body     raw payload bytes (verbatim)
    <cache>/v1/objects/<aa>/<key>.meta.json metadata + SHA-256 + release info
    <cache>/.lock                          advisory lock for maintenance operations

The cache key is ``sha256(canonical_json({format, method, url, params,
request_headers_subset, request_body_sha256}))`` — i.e. content-addressed on the
request, so re-deriving a result only needs the recorded request plus the raw
payload.  Bodies are stored verbatim (including gzip bytes); adapters decide how
to decode them.

Writes are atomic (temporary file + ``os.replace``); metadata is written last and
acts as the commit marker, so a crashed write can never yield a half-valid entry.
``--offline`` must work exclusively from this store.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

from . import __version__, util
from .errors import CacheCorruptError, ConfigError, DataError

CACHE_FORMAT = 1
BODY_SUFFIX = ".body"
META_SUFFIX = ".meta.json"
LOCK_NAME = ".lock"

#: Request headers that must never be persisted in cache metadata.
_FORBIDDEN_HEADERS = frozenset(
    {"authorization", "proxy-authorization", "cookie", "set-cookie", "x-api-key", "api-key", "x-auth-token"}
)


def _safe_request_headers(headers: Mapping[str, str] | None) -> dict[str, str] | None:
    """Drop credential-bearing request headers before writing them to disk."""
    if not headers:
        return None
    safe = {
        str(k): str(v)
        for k, v in headers.items()
        if str(k).lower() not in _FORBIDDEN_HEADERS
    }
    return safe or None


def cache_key(
    method: str,
    url: str,
    *,
    params: Mapping[str, Any] | Sequence[tuple[str, Any]] | None = None,
    request_headers: Mapping[str, str] | None = None,
    request_body: bytes | None = None,
) -> str:
    """Deterministic key for one HTTP request.

    Only semantically relevant request headers are included (``accept`` and
    ``content-type``); volatile headers (User-Agent, date, auth) are excluded so
    a cached public payload can be replayed later from any client.
    """
    relevant: dict[str, str] = {}
    for name, value in (request_headers or {}).items():
        if name.lower() in {"accept", "content-type"} and value:
            relevant[name.lower()] = value
    if isinstance(params, Mapping):
        normalised = {str(k): str(v) for k, v in sorted(params.items())}
        params_repr: Any = normalised or None
    elif params:
        params_repr = sorted((str(k), str(v)) for k, v in params)
    else:
        params_repr = None
    payload = {
        "lifesci_cache": CACHE_FORMAT,
        "method": method.upper(),
        "url": url,
        "params": params_repr,
        "headers": relevant or None,
        "body_sha256": util.sha256_bytes(request_body) if request_body else None,
    }
    return util.sha256_text(util.canonical_json(payload))


@dataclass
class CacheEntry:
    """One cached payload: metadata plus lazy access to the body bytes."""

    key: str
    meta: dict[str, Any]
    body_path: Path

    @property
    def url(self) -> str:
        return str(self.meta.get("url", ""))

    @property
    def status(self) -> int | None:
        value = self.meta.get("status")
        return int(value) if value is not None else None

    @property
    def bytes(self) -> int:
        return int(self.meta.get("bytes", 0))

    @property
    def sha256(self) -> str:
        return str(self.meta.get("body_sha256", ""))

    @property
    def release(self) -> str | None:
        value = self.meta.get("release")
        return str(value) if value else None

    @property
    def database(self) -> str | None:
        value = self.meta.get("database")
        return str(value) if value else None

    @property
    def stored_at(self) -> str:
        return str(self.meta.get("stored_at", ""))

    @property
    def age_seconds(self) -> float:
        try:
            stored = datetime.fromisoformat(self.stored_at.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            return float("inf")
        return (datetime.now(timezone.utc) - stored).total_seconds()

    @property
    def ttl_seconds(self) -> float:
        return float(self.meta.get("ttl_seconds") or 0.0)

    @property
    def expired(self) -> bool:
        if self.ttl_seconds <= 0:
            return False  # explicitly immutable
        return self.age_seconds > self.ttl_seconds

    @property
    def content_type(self) -> str:
        return str(self.meta.get("content_type") or "")

    @property
    def encoding(self) -> str:
        return str(self.meta.get("encoding") or "")

    def read_bytes(self, *, verify: bool = True) -> bytes:
        try:
            data = self.body_path.read_bytes()
        except FileNotFoundError as err:
            raise CacheCorruptError(
                f"cache entry {self.key} is missing its body file",
                details={"key": self.key, "body_path": str(self.body_path)},
            ) from err
        if verify:
            actual = util.sha256_bytes(data)
            if actual != self.sha256:
                raise CacheCorruptError(
                    f"cache entry {self.key} body hash mismatch (expected {self.sha256}, got {actual})",
                    details={
                        "key": self.key,
                        "url": self.url,
                        "expected_sha256": self.sha256,
                        "actual_sha256": actual,
                    },
                )
        return data

    def read_text(self, *, verify: bool = True) -> str:
        return self.read_bytes(verify=verify).decode(self.encoding or "utf-8", errors="replace")

    def read_json(self, *, verify: bool = True) -> Any:
        try:
            return json.loads(self.read_text(verify=verify))
        except json.JSONDecodeError as err:
            raise DataError(
                f"cached payload for {self.url} is not valid JSON: {err}",
                details={"key": self.key, "url": self.url},
            ) from err

    def summary(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "url": self.url,
            "database": self.database,
            "release": self.release,
            "status": self.status,
            "bytes": self.bytes,
            "sha256": self.sha256,
            "stored_at": self.stored_at,
            "age_seconds": round(self.age_seconds, 1),
            "ttl_seconds": self.ttl_seconds,
            "expired": self.expired,
            "cache_hit": True,
        }


class Cache:
    """Filesystem cache rooted at ``root``."""

    def __init__(self, root: str | os.PathLike[str], *, ttl_default: float = 30 * 24 * 3600.0):
        self.root = Path(root).expanduser().resolve()
        self.ttl_default = float(ttl_default)
        self._objects = self.root / "v1" / "objects"
        self._ensure_layout()

    # ------------------------------------------------------------------
    # layout
    # ------------------------------------------------------------------
    def _ensure_layout(self) -> None:
        self._objects.mkdir(parents=True, exist_ok=True)
        marker = self.root / "cache.json"
        if not marker.exists():
            util.atomic_write_json(
                marker,
                {
                    "format": CACHE_FORMAT,
                    "created_at": util.now_iso(),
                    "lifesci_version": __version__,
                    "layout": "v1/objects/<aa>/<key>.body|.meta.json",
                },
            )

    def _bucket(self, key: str) -> Path:
        bucket = self._objects / key[:2]
        bucket.mkdir(parents=True, exist_ok=True)
        return bucket

    def body_path_for(self, key: str) -> Path:
        return self._objects / key[:2] / f"{key}{BODY_SUFFIX}"

    def meta_path_for(self, key: str) -> Path:
        return self._objects / key[:2] / f"{key}{META_SUFFIX}"

    # ------------------------------------------------------------------
    # read
    # ------------------------------------------------------------------
    def lookup(self, key: str, *, verify: bool | None = None) -> CacheEntry | None:
        """Return a cache entry or ``None`` when absent.

        Raises:
            CacheCorruptError: metadata is unreadable or the body hash does not
                match while verification is enabled.
        """
        meta_path = self.meta_path_for(key)
        body_path = self.body_path_for(key)
        try:
            raw = meta_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        try:
            meta = json.loads(raw)
        except json.JSONDecodeError as err:
            raise CacheCorruptError(
                f"corrupt cache metadata for key {key}",
                details={"key": key, "meta_path": str(meta_path), "error": str(err)},
            ) from err
        if meta.get("format") != CACHE_FORMAT:
            raise CacheCorruptError(
                f"cache entry {key} has unsupported format {meta.get('format')!r}",
                details={"key": key, "meta_path": str(meta_path)},
            )
        entry = CacheEntry(key=key, meta=meta, body_path=body_path)
        if not body_path.exists():
            raise CacheCorruptError(
                f"cache entry {key} is missing its body file",
                details={"key": key, "body_path": str(body_path)},
            )
        if verify:
            entry.read_bytes(verify=True)  # raises on mismatch
        return entry

    def has(self, key: str) -> bool:
        return self.meta_path_for(key).is_file() and self.body_path_for(key).is_file()

    # ------------------------------------------------------------------
    # write
    # ------------------------------------------------------------------
    def store(
        self,
        key: str,
        body: bytes,
        *,
        status: int,
        method: str,
        url: str,
        params: Mapping[str, Any] | Sequence[tuple[str, Any]] | None = None,
        request_headers: Mapping[str, str] | None = None,
        response_headers: Mapping[str, str] | None = None,
        content_type: str = "",
        encoding: str = "",
        release: str | None = None,
        database: str | None = None,
        final_url: str | None = None,
        ttl_seconds: float | None = None,
        immutable: bool = False,
        retrieved_at: str | None = None,
        provenance_ref: str | None = None,
        notes: Mapping[str, Any] | None = None,
        request_body_sha256: str | None = None,
    ) -> CacheEntry:
        """Store one payload and its metadata (metadata written last)."""
        body_path = self.body_path_for(key)
        meta_path = self.meta_path_for(key)
        self._bucket(key)
        now = util.now_iso()
        ttl = 0.0 if immutable else float(self.ttl_default if ttl_seconds is None else ttl_seconds)
        meta: dict[str, Any] = {
            "format": CACHE_FORMAT,
            "key": key,
            "method": method.upper(),
            "url": url,
            "final_url": final_url or url,
            "params": util.json_safe(dict(params) if isinstance(params, Mapping) else params),
            "request_headers": _safe_request_headers(
                {str(k).lower(): str(v) for k, v in (request_headers or {}).items()}
            ),
            "request_body_sha256": request_body_sha256,
            "status": int(status),
            "response_headers": {
                str(k).lower(): str(v) for k, v in (response_headers or {}).items()
            },
            "content_type": content_type,
            "encoding": encoding or "utf-8",
            "body_sha256": util.sha256_bytes(body),
            "bytes": len(body),
            "stored_at": now,
            "retrieved_at": retrieved_at or now,
            "ttl_seconds": ttl,
            "release": release,
            "database": database,
            "provenance_ref": provenance_ref,
            "lifesci_version": __version__,
        }
        if notes:
            meta["notes"] = util.json_safe(dict(notes))
        # Body first (never visible without metadata), metadata last (commit).
        # Verify after commit so a concurrent writer cannot leave a mismatched
        # body/metadata pair; retry a few times before failing loudly.
        import time as _time

        for attempt in range(3):
            util.atomic_write_bytes(body_path, body)
            util.atomic_write_json(meta_path, meta)
            try:
                if util.sha256_file(body_path) == meta["body_sha256"]:
                    break
            except FileNotFoundError:  # pragma: no cover - concurrent clear
                pass
            _time.sleep(0.01)
        else:  # pragma: no cover - requires pathological concurrent writes
            raise ConfigError(
                f"could not publish cache entry {key} consistently (concurrent writers?)",
                details={"key": key, "url": url},
            )
        return CacheEntry(key=key, meta=meta, body_path=body_path)

    # ------------------------------------------------------------------
    # maintenance
    # ------------------------------------------------------------------
    @contextlib.contextmanager
    def _maintenance_lock(self, timeout: float = 30.0) -> Iterator[None]:
        """Advisory lock so ``clear``/``prune`` do not race with writers."""
        import time

        lock_path = self.root / LOCK_NAME
        deadline = time.monotonic() + timeout
        while True:
            try:
                fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            except FileExistsError:
                if time.monotonic() > deadline:
                    raise ConfigError(
                        f"timed out waiting for cache maintenance lock {lock_path}"
                    )
                # Stale lock detection (> 10 min old).
                try:
                    if time.time() - lock_path.stat().st_mtime > 600:
                        lock_path.unlink(missing_ok=True)
                        continue
                except FileNotFoundError:  # pragma: no cover - race
                    continue
                time.sleep(0.1)
                continue
            try:
                os.write(fd, f"{os.getpid()}".encode())
                os.close(fd)
                yield
            finally:
                with contextlib.suppress(FileNotFoundError):
                    lock_path.unlink()
            return

    def iter_meta(self) -> Iterator[dict[str, Any]]:
        if not self._objects.exists():
            return
        for bucket in sorted(self._objects.iterdir()):
            if not bucket.is_dir():
                continue
            for meta_file in sorted(bucket.glob(f"*{META_SUFFIX}")):
                try:
                    yield json.loads(meta_file.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, OSError):
                    yield {"key": meta_file.name[: -len(META_SUFFIX)], "corrupt": True}

    def entries(self) -> list[CacheEntry]:
        out: list[CacheEntry] = []
        for meta in self.iter_meta():
            key = str(meta.get("key", ""))
            if not key or meta.get("corrupt"):
                continue
            out.append(CacheEntry(key=key, meta=meta, body_path=self.body_path_for(key)))
        return out

    def stats(self) -> dict[str, Any]:
        entries = self.entries()
        databases: dict[str, int] = {}
        releases: dict[str, str] = {}
        expired = 0
        total_bytes = 0
        for entry in entries:
            total_bytes += entry.bytes
            db = entry.database or "unknown"
            databases[db] = databases.get(db, 0) + 1
            if entry.release:
                releases[db] = entry.release
            if entry.expired:
                expired += 1
        return {
            "root": str(self.root),
            "n_entries": len(entries),
            "n_expired": expired,
            "bytes": total_bytes,
            "databases": databases,
            "releases": releases,
            "missing_bodies": sum(1 for e in entries if not e.body_path.exists()),
        }

    def verify_all(self, *, limit: int | None = None) -> list[dict[str, Any]]:
        report: list[dict[str, Any]] = []
        for index, entry in enumerate(self.entries()):
            if limit is not None and index >= limit:
                break
            try:
                entry.read_bytes(verify=True)
                report.append({"key": entry.key, "ok": True, "url": entry.url})
            except CacheCorruptError as err:
                report.append(
                    {
                        "key": entry.key,
                        "ok": False,
                        "url": entry.url,
                        "error": err.message,
                    }
                )
        return report

    def delete(self, key: str) -> bool:
        removed = False
        for path in (self.body_path_for(key), self.meta_path_for(key)):
            with contextlib.suppress(FileNotFoundError):
                path.unlink()
                removed = True
        return removed

    def prune_expired(self, *, dry_run: bool = False) -> dict[str, Any]:
        with self._maintenance_lock():
            expired = [e for e in self.entries() if e.expired]
            removed_bytes = sum(e.bytes for e in expired)
            if not dry_run:
                for entry in expired:
                    self.delete(entry.key)
            return {
                "n_pruned": 0 if dry_run else len(expired),
                "n_expired": len(expired),
                "bytes_freed": 0 if dry_run else removed_bytes,
                "dry_run": dry_run,
            }

    def clear(self, *, confirm: bool = True) -> dict[str, Any]:
        if not confirm:
            raise ConfigError("cache clear requires confirm=True")
        with self._maintenance_lock():
            entries = self.entries()
            total = sum(e.bytes for e in entries)
            shutil.rmtree(self._objects, ignore_errors=True)
            self._objects.mkdir(parents=True, exist_ok=True)
            return {"n_cleared": len(entries), "bytes_freed": total, "root": str(self.root)}

    # ------------------------------------------------------------------
    # named blobs (for fixture seeding and derived local data)
    # ------------------------------------------------------------------
    def blob_path(self, namespace: str, name: str) -> Path:
        safe_ns = util.slugify(namespace, max_length=40)
        safe_name = util.slugify(name, max_length=120)
        return self.root / "blobs" / safe_ns / safe_name

    def store_blob(self, namespace: str, name: str, data: bytes) -> dict[str, Any]:
        path = self.blob_path(namespace, name)
        util.atomic_write_bytes(path, data)
        return {
            "namespace": namespace,
            "name": name,
            "path": str(path),
            "sha256": util.sha256_bytes(data),
            "bytes": len(data),
        }

    def load_blob(self, namespace: str, name: str) -> bytes | None:
        path = self.blob_path(namespace, name)
        try:
            return path.read_bytes()
        except FileNotFoundError:
            return None


def cache_from_context(ctx: Any) -> Cache:
    """Build a :class:`Cache` consistent with a :class:`lifesci.context.Context`."""
    return Cache(ctx.cache_dir, ttl_default=ctx.cache_ttl)


def store_fixture_payload(
    cache: Cache,
    *,
    fixture_path: str | os.PathLike[str],
    method: str,
    url: str,
    params: Mapping[str, Any] | None = None,
    request_headers: Mapping[str, str] | None = None,
    response_headers: Mapping[str, str] | None = None,
    status: int = 200,
    content_type: str = "application/json",
    encoding: str = "utf-8",
    release: str | None = None,
    database: str | None = None,
    ttl_seconds: float | None = None,
    immutable: bool = False,
) -> CacheEntry:
    """Seed the cache from a recorded payload (used by tests and cache-seeding utilities)."""
    body = Path(fixture_path).read_bytes()
    key = cache_key(method, url, params=params, request_headers=request_headers)  # noqa: F841
    return cache.store(
        key,
        body,
        status=status,
        method=method,
        url=url,
        params=params,
        request_headers=request_headers,
        response_headers=response_headers,
        content_type=content_type,
        encoding=encoding,
        release=release,
        database=database,
        ttl_seconds=ttl_seconds,
        immutable=immutable,
        notes={"seeded_from_fixture": str(fixture_path)},
    )
