"""The single retrying, caching, rate-limited, provenance-logged HTTP primitive.

Every portal adapter goes through :func:`request`.  Behaviour:

* Bearer/token headers are never cached and never logged.
* Successful payloads are stored verbatim in the content-addressed cache; the
  SHA-256, byte count, status, headers, database release, and retrieval time are
  recorded in the provenance log.
* Transient failures (connection errors, timeouts, 429/500/502/503/504) are
  retried with exponential backoff + jitter, honouring ``Retry-After`` and
  applying a host-wide cooldown after a 429/503 so we never hammer a portal.
* Body-level error payloads (NCBI's ``200 {"error": ...}``) can be detected via a
  ``detect_error`` callback and are retried like transport failures.
* ``--dry-run`` returns the planned request without touching the network.
* ``--offline`` replays cached payloads only and fails with a precise
  ``OFFLINE_CACHE_MISS`` otherwise.

The transport is injectable, so every failure mode above is unit-testable offline.
"""

from __future__ import annotations

import gzip
import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import urlencode, urlsplit, urlunsplit

from . import __version__, provenance, util
from .cache import Cache, CacheEntry, cache_from_context, cache_key
from .errors import DataError, LifesciError, OfflineCacheMiss, PortalError
from .ratelimit import HostLimiter, backoff_seconds
from .transport import (
    FakeTransport,
    Transport,
    TransportError,
    TransportResponse,
    UrllibTransport,
    multipart_encode,
)

#: HTTP statuses considered transient and therefore retried.
RETRY_STATUSES: frozenset[int] = frozenset({429, 500, 502, 503, 504})

#: Statuses that make us apply a host-wide cooldown in addition to local backoff.
COOLDOWN_STATUSES: frozenset[int] = frozenset({429, 503})


# ---------------------------------------------------------------------------
# response object
# ---------------------------------------------------------------------------


@dataclass
class HttpResponse:
    """Decoded response plus provenance metadata for one request."""

    method: str
    url: str
    final_url: str
    status: int | None
    headers: dict[str, str]
    body: bytes
    from_cache: bool
    cache_key: str | None
    attempts: int
    elapsed_ms: float
    request_id: str
    provenance_entry: dict[str, Any] | None = None
    release: str | None = None
    dry_run: bool = False
    content_type: str = ""
    encoding: str = "utf-8"

    @property
    def ok(self) -> bool:
        return self.dry_run or (self.status is not None and 200 <= self.status < 300)

    def text(self) -> str:
        return self.body.decode(self.encoding or "utf-8", errors="replace")

    def json(self) -> Any:
        try:
            return json.loads(self.text())
        except json.JSONDecodeError as err:
            preview = util.truncate(self.text(), 300)
            raise DataError(
                f"expected JSON from {self.url} but parsing failed: {err}",
                details={
                    "url": self.url,
                    "status": self.status,
                    "content_type": self.content_type,
                    "body_preview": preview,
                    "request_id": self.request_id,
                },
            ) from err

    def describe(self) -> dict[str, Any]:
        return {
            "method": self.method,
            "url": self.url,
            "final_url": self.final_url,
            "status": self.status,
            "from_cache": self.from_cache,
            "cache_key": self.cache_key,
            "attempts": self.attempts,
            "elapsed_ms": round(self.elapsed_ms, 1),
            "bytes": len(self.body),
            "request_id": self.request_id,
            "release": self.release,
            "dry_run": self.dry_run,
        }


# ---------------------------------------------------------------------------
# helpers attached lazily to the context
# ---------------------------------------------------------------------------


def get_transport(ctx: Any) -> Transport:
    """Return the context's transport, defaulting to the urllib implementation."""
    transport = getattr(ctx, "transport", None)
    if transport is not None:
        return transport
    cached = getattr(ctx, "_default_transport", None)
    if cached is None:
        cached = UrllibTransport()
        ctx._default_transport = cached
    return cached


def get_limiter(ctx: Any) -> HostLimiter:
    limiter = getattr(ctx, "_limiter", None)
    if limiter is None:
        limiter = HostLimiter(limits=ctx.rate_limits)
        ctx._limiter = limiter
    return limiter


def _sleep(ctx: Any, seconds: float) -> None:
    if seconds <= 0:
        return
    sleeper = getattr(ctx, "_sleeper", None)
    if sleeper is not None:
        sleeper(seconds)
    else:
        time.sleep(seconds)


def _preview(body: bytes, limit: int = 400) -> str:
    try:
        text = body.decode("utf-8", errors="replace")
    except Exception:  # pragma: no cover - defensive
        return f"<{len(body)} bytes>"
    return util.truncate(" ".join(text.split()), limit)


# ---------------------------------------------------------------------------
# URL / body construction
# ---------------------------------------------------------------------------


def build_url(url: str, params: Mapping[str, Any] | Sequence[tuple[str, Any]] | None) -> str:
    """Append query parameters deterministically (sorted) to ``url``."""
    if not params:
        return url
    if isinstance(params, Mapping):
        items: list[tuple[str, str]] = [
            (str(k), str(v)) for k, v in sorted(params.items()) if v is not None
        ]
    else:
        items = [(str(k), str(v)) for k, v in params if v is not None]
    if not items:
        return url
    parts = urlsplit(url)
    query = urlencode(items, doseq=True)
    merged = f"{parts.query}&{query}" if parts.query else query
    return urlunsplit((parts.scheme, parts.netloc, parts.path, merged, parts.fragment))


def _encode_body(
    *,
    json_body: Any | None = None,
    form_fields: Mapping[str, Any] | Sequence[tuple[str, Any]] | None = None,
    files: Sequence[tuple[str, str, str, bytes]] | None = None,
    body: bytes | None = None,
) -> tuple[bytes | None, dict[str, str], str | None]:
    """Return ``(body_bytes, headers, content_type)`` for a request."""
    if body is not None:
        return body, {}, None
    if files is not None:
        encoded, content_type = multipart_encode(form_fields or {}, files)
        return encoded, {"content-type": content_type}, content_type
    if json_body is not None:
        encoded = util.canonical_json(json_body).encode("utf-8")
        return encoded, {"content-type": "application/json"}, "application/json"
    if form_fields is not None:
        if isinstance(form_fields, Mapping):
            pairs = [(str(k), str(v)) for k, v in form_fields.items()]
        else:
            pairs = [(str(k), str(v)) for k, v in form_fields]
        encoded = urlencode(pairs).encode()
        return encoded, {"content-type": "application/x-www-form-urlencoded"}, "application/x-www-form-urlencoded"
    return None, {}, None


# ---------------------------------------------------------------------------
# the primitive
# ---------------------------------------------------------------------------


def request(
    ctx: Any,
    method: str,
    url: str,
    *,
    params: Mapping[str, Any] | Sequence[tuple[str, Any]] | None = None,
    headers: Mapping[str, str] | None = None,
    body: bytes | None = None,
    json_body: Any | None = None,
    form_fields: Mapping[str, Any] | Sequence[tuple[str, Any]] | None = None,
    files: Sequence[tuple[str, str, str, bytes]] | None = None,
    cache: bool = True,
    cache_ttl: float | None = None,
    retries: int | None = None,
    timeout: float | None = None,
    allow_status: Sequence[int] = (200,),
    detect_error: Callable[[TransportResponse], Any] | None = None,
    error_code: str | None = None,
    database: str | None = None,
    release_header: str | None = None,
    idempotent: bool | None = None,
    verify_cache: bool = True,
) -> HttpResponse:
    """Perform one logical HTTP request with cache, retries, and provenance.

    Args:
        ctx: command context (cache path, retry policy, limiter, transport).
        method: HTTP method.
        url: URL without query string (``params`` are appended).
        params: query parameters, sorted before use.
        headers: extra request headers (``User-Agent`` is added automatically).
        body: raw request body.
        json_body: JSON body (alternative to ``body``).
        form_fields: urlencoded form body, or text fields for ``files``.
        files: multipart uploads ``(field, filename, content_type, content)``.
        cache: cache reads/writes for this request (``False`` for polling).
        cache_ttl: override the default TTL for a successful payload.
        retries: override ``ctx.max_retries`` transient retry budget.
        timeout: per-attempt timeout seconds.
        allow_status: statuses treated as success (default ``(200,)``).
        detect_error: callable returning an error message when a nominally
            successful body is actually a portal error; may return
            ``(message, retriable_bool)``.
        error_code: stable prefix for raised ``PortalError`` codes.
        database: database label recorded in provenance/release registry.
        release_header: response header carrying the database release.
        idempotent: whether the request may be retried on transient failures
            (defaults to ``True`` for GET/HEAD, ``False`` for writes).

    Returns:
        :class:`HttpResponse` with raw body and provenance metadata.
    """
    method = method.upper()
    final_url = build_url(url, params)
    request_headers = dict(headers or {})
    request_headers.setdefault("user-agent", ctx.user_agent)
    if ctx.contact_email and "from" not in {k.lower() for k in request_headers}:
        request_headers["from"] = ctx.contact_email
    encoded_body, body_headers, content_type = _encode_body(
        json_body=json_body, form_fields=form_fields, files=files, body=body
    )
    for name, value in body_headers.items():
        request_headers.setdefault(name, value)

    if idempotent is None:
        idempotent = method in {"GET", "HEAD", "OPTIONS"}
    max_attempts = int(retries if retries is not None else ctx.max_retries) + 1
    if not idempotent:
        max_attempts = 1
    max_attempts = max(1, max_attempts)
    per_attempt_timeout = float(timeout if timeout is not None else ctx.timeout)

    # Key on the *logical* request (url + params), never on the rendered URL:
    # deterministic serialisation, so a recorded replay produces the same key.
    key = (
        cache_key(
            method,
            url,
            params=params,
            request_headers=request_headers,
            request_body=encoded_body,
        )
        if cache
        else None
    )
    cache_store: Cache | None = cache_from_context(ctx) if cache else None
    request_id = provenance.new_request_id()

    # ---- dry run ------------------------------------------------------
    if ctx.dry_run:
        entry = _record(
            ctx,
            method=method,
            url=url,
            params=params,
            status=None,
            sha256=None,
            bytes_=None,
            cache="dry_run",
            database=database,
            release=None,
            retries=0,
            elapsed_ms=0.0,
            request_id=request_id,
            final_url=final_url,
        )
        return HttpResponse(
            method=method,
            url=url,
            final_url=final_url,
            status=None,
            headers={},
            body=b"",
            from_cache=False,
            cache_key=key,
            attempts=0,
            elapsed_ms=0.0,
            request_id=request_id,
            provenance_entry=entry,
            dry_run=True,
            encoding="utf-8",
        )

    # ---- offline / cache read ----------------------------------------
    cached_entry: CacheEntry | None = None
    if cache_store is not None and key is not None:
        try:
            cached_entry = cache_store.lookup(key, verify=False)
        except LifesciError as err:
            if ctx.offline:
                raise
            ctx.warn(
                "CACHE_CORRUPT",
                "A cached payload failed integrity verification and will be discarded.",
                context={"key": key, "url": final_url, "error": err.code},
            )
            cached_entry = None
            cache_store.delete(key)

    cached_body: bytes | None = None
    if cached_entry is not None:
        if cached_entry.expired and not ctx.offline:
            ctx.warn(
                "CACHE_ENTRY_EXPIRED",
                "Cached payload is older than its TTL and will be re-fetched.",
                context={"url": final_url, "age_hours": round(cached_entry.age_seconds / 3600, 1)},
            )
            cached_entry = None
        else:
            if ctx.offline and cached_entry.expired:
                ctx.warn(
                    "CACHE_EXPIRED_OFFLINE_REPLAY",
                    "Offline mode replayed an expired cached payload; treat the release as historical.",
                    context={"url": final_url, "stored_at": cached_entry.stored_at},
                )
            try:
                cached_body = cached_entry.read_bytes(verify=verify_cache)
            except LifesciError as err:
                if ctx.offline:
                    raise
                ctx.warn(
                    "CACHE_CORRUPT",
                    "A cached payload failed integrity verification and will be discarded.",
                    context={"key": key, "url": final_url, "error": err.code},
                )
                if cache_store is not None:
                    cache_store.delete(key)
                cached_entry = None
                cached_body = None
            if cached_entry is not None and cached_body is not None:
                # Cached responses obey the same validation rules as live ones:
                # status allow-list plus portal-level error detection.
                cached_problem: str | None = None
                cached_problem_code = "CACHE_ENTRY_INVALID"
                cached_headers = dict(cached_entry.meta.get("response_headers") or {})
                if cached_entry.status not in allow_status:
                    cached_problem = (
                        f"cached status {cached_entry.status} is not in allow_status "
                        f"{list(allow_status)}"
                    )
                    cached_problem_code = "CACHE_STATUS_NOT_ALLOWED"
                elif detect_error is not None:
                    try:
                        detected = detect_error(
                            TransportResponse(
                                status=cached_entry.status,
                                headers=cached_headers,
                                body=cached_body,
                                url=final_url,
                            )
                        )
                    except Exception:  # pragma: no cover - defensive
                        detected = None
                    if detected:
                        cached_problem = _normalise_body_error(detected)[0]
                        cached_problem_code = "CACHE_ENTRY_REJECTED"
                if cached_problem is not None:
                    if ctx.offline:
                        raise PortalError(
                            f"offline mode: cached payload for {url} is not usable: {cached_problem}",
                            code=cached_problem_code,
                            retriable=False,
                            details={
                                "cache_key": key,
                                "url": final_url,
                                "cache_dir": str(getattr(ctx, "cache_dir", "")),
                            },
                        )
                    ctx.warn(
                        cached_problem_code,
                        "Cached payload rejected by current validation rules; re-fetching.",
                        context={"url": final_url, "reason": cached_problem, "cache_key": key},
                    )
                    if cache_store is not None:
                        cache_store.delete(key)
                    cached_entry = None
                    cached_body = None

    if cached_entry is not None and cached_body is not None:
        entry = _record(
            ctx,
            method=method,
            url=url,
            params=params,
            status=cached_entry.status,
            sha256=cached_entry.sha256,
            bytes_=cached_entry.bytes,
            cache="hit",
            database=cached_entry.database or database,
            release=cached_entry.release,
            retries=0,
            elapsed_ms=0.0,
            request_id=request_id,
            final_url=cached_entry.meta.get("final_url") or final_url,
            content_type=cached_entry.content_type,
        )
        return HttpResponse(
            method=method,
            url=url,
            final_url=str(cached_entry.meta.get("final_url") or final_url),
            status=cached_entry.status,
            headers=dict(cached_entry.meta.get("response_headers") or {}),
            body=cached_body,
            from_cache=True,
            cache_key=key,
            attempts=0,
            elapsed_ms=0.0,
            request_id=request_id,
            provenance_entry=entry,
            release=cached_entry.release,
            content_type=cached_entry.content_type,
            encoding=cached_entry.encoding,
        )

    if ctx.offline:
        detail = "cache disabled for this request" if cache_store is None else "no cached payload"
        raise OfflineCacheMiss(
            f"offline mode: {detail} for {method} {final_url}",
            details={
                "method": method,
                "url": final_url,
                "cache_key": key,
                "cache_dir": str(getattr(ctx, "cache_dir", "")),
                "hint": "Run the same command once online, then retry offline with the same "
                "--cache-dir so the cached payload is reused.",
            },
        )

    # ---- network with retries ----------------------------------------
    transport = get_transport(ctx)
    limiter = get_limiter(ctx)
    diagnostics: list[dict[str, Any]] = []
    started = time.monotonic()
    last_error: str | None = None
    last_status: int | None = None
    response: TransportResponse | None = None
    attempt = 0
    body_error: Any = None
    body_error_retriable: bool = True
    last_transport_retriable: bool | None = None

    while attempt < max_attempts:
        attempt += 1
        limiter.wait(final_url)
        attempt_started = time.monotonic()
        try:
            response = transport.request(
                method,
                final_url,
                headers=request_headers,
                body=encoded_body,
                timeout=per_attempt_timeout,
            )
        except TransportError as err:
            elapsed = (time.monotonic() - attempt_started) * 1000.0
            diagnostics.append(
                {"attempt": attempt, "error": "transport", "message": err.message, "elapsed_ms": round(elapsed, 1)}
            )
            last_error = err.message
            last_transport_retriable = err.retriable
            if not err.retriable or attempt >= max_attempts:
                break
            wait = backoff_seconds(attempt, base=ctx.retry_backoff, maximum=ctx.retry_backoff_max)
            ctx.warn(
                "HTTP_RETRY",
                f"Transient transport failure; retrying in {wait:.1f}s "
                f"(attempt {attempt}/{max_attempts}).",
                context={"url": final_url, "error": err.message},
            )
            _sleep(ctx, wait)
            continue

        # Decode Content-Encoding before any error detection, preview, caching,
        # or returning: everything downstream sees the decoded representation.
        response = _decode_content_encoding(response)
        last_status = response.status
        # Portal errors delivered with an HTTP 200 body (e.g. NCBI eutils).
        body_error = None
        if detect_error is not None:
            try:
                body_error = detect_error(response)
            except Exception as err:  # pragma: no cover - defensive
                body_error = f"error detector crashed: {err}"
        if body_error and (response.status in allow_status or response.status in RETRY_STATUSES):
            message, err_retriable = _normalise_body_error(body_error)
            body_error_retriable = err_retriable
            diagnostics.append(
                {"attempt": attempt, "error": "body", "message": message, "status": response.status}
            )
            last_error = message
            if not err_retriable or attempt >= max_attempts:
                break
            retry_after = util.parse_retry_after(response.header("retry-after"), default=0.0)
            wait = max(
                retry_after,
                backoff_seconds(attempt, base=ctx.retry_backoff, maximum=ctx.retry_backoff_max),
            )
            ctx.warn(
                "HTTP_BODY_ERROR_RETRY",
                f"Portal returned an error object inside a successful response; retrying in "
                f"{wait:.1f}s (attempt {attempt}/{max_attempts}).",
                context={"url": final_url, "error": message, "status": response.status},
            )
            _sleep(ctx, wait)
            continue

        if response.status in allow_status:
            break  # success

        if response.status in RETRY_STATUSES and attempt < max_attempts:
            retry_after = util.parse_retry_after(response.header("retry-after"), default=0.0)
            wait = max(
                retry_after,
                backoff_seconds(attempt, base=ctx.retry_backoff, maximum=ctx.retry_backoff_max),
            )
            if response.status in COOLDOWN_STATUSES:
                limiter.penalise(final_url, wait)
            diagnostics.append(
                {
                    "attempt": attempt,
                    "error": "status",
                    "status": response.status,
                    "retry_after_s": retry_after,
                    "wait_s": round(wait, 2),
                }
            )
            ctx.warn(
                "HTTP_RATE_LIMITED" if response.status in COOLDOWN_STATUSES else "HTTP_TRANSIENT",
                f"HTTP {response.status} from {final_url.rsplit('/', 1)[0]}; backing off "
                f"{wait:.1f}s (attempt {attempt}/{max_attempts}).",
                context={
                    "url": final_url,
                    "status": response.status,
                    "retry_after_s": retry_after,
                    "body_preview": _preview(response.body),
                },
            )
            _sleep(ctx, wait)
            continue

        # Non-retryable status (or retry budget exhausted).
        break

    elapsed_ms = (time.monotonic() - started) * 1000.0

    if response is None or response.status not in allow_status or body_error:
        status = response.status if response is not None else None
        preview = _preview(response.body) if response is not None else ""
        error_kind = "body" if body_error else "http"
        message = last_error or (
            f"{method} {url} returned HTTP {status}"
            if status is not None
            else f"{method} {url} failed"
        )
        if body_error is not None:
            retriable = bool(body_error_retriable)
        elif status is None:
            # No HTTP response at all: report the last transport error honestly.
            retriable = True if last_transport_retriable is None else bool(last_transport_retriable)
        else:
            retriable = status in RETRY_STATUSES
        if status in COOLDOWN_STATUSES:
            # Even the final 429/503 must cool the host down for subsequent calls.
            retry_after = util.parse_retry_after(
                response.header("retry-after") if response is not None else None,
                default=ctx.retry_backoff_max,
            )
            limiter.penalise(final_url, retry_after)
        suffix = "transient" if retriable else "non-retryable"
        raise PortalError(
            f"{method} {url} failed after {attempt} attempt(s): {message} "
            f"[{error_kind}, {suffix}]",
            code=(error_code or "HTTP_ERROR") + (f"_{status}" if status is not None else ""),
            retriable=retriable,
            details={
                "method": method,
                "url": url,
                "final_url": final_url,
                "status": status,
                "attempts": attempt,
                "diagnostics": diagnostics,
                "body_preview": preview,
                "request_id": request_id,
                "database": database,
            },
            fallbacks=[],
        )

    # ---- success: record, cache (body was decoded after transport) ----
    body_bytes = response.body
    encoding = _encoding_from_headers(response.headers)
    content_type = response.header("content-type", "") or ""
    if "charset=" in content_type:
        charset = content_type.split("charset=")[-1].split(";")[0].strip()
        encoding = charset or encoding
    sha = util.sha256_bytes(body_bytes)
    release = None
    if release_header:
        release = response.header(release_header)
        if release:
            provenance.record_release(
                ctx.cache_dir,
                database=database or "unknown",
                release=release,
                source_url=final_url,
            )

    cache_state = "disabled"
    if cache_store is not None and key is not None and response.status is not None and 200 <= response.status < 300:
        cache_store.store(
            key,
            body_bytes,
            status=response.status,
            method=method,
            url=url,
            params=params,
            request_headers=request_headers,
            response_headers=_cache_response_headers(response.headers),
            content_type=content_type,
            encoding=encoding,
            release=release,
            database=database,
            final_url=response.url or final_url,
            ttl_seconds=cache_ttl,
            request_body_sha256=util.sha256_bytes(encoded_body) if encoded_body else None,
        )
        cache_state = "store"

    entry = _record(
        ctx,
        method=method,
        url=url,
        params=params,
        status=response.status,
        sha256=sha,
        bytes_=len(body_bytes),
        cache=cache_state,
        database=database,
        release=release,
        retries=attempt - 1,
        elapsed_ms=elapsed_ms,
        request_id=request_id,
        final_url=response.url or final_url,
        content_type=content_type,
    )
    return HttpResponse(
        method=method,
        url=url,
        final_url=response.url or final_url,
        status=response.status,
        headers=dict(response.headers),
        body=body_bytes,
        from_cache=False,
        cache_key=key,
        attempts=attempt,
        elapsed_ms=elapsed_ms,
        request_id=request_id,
        provenance_entry=entry,
        release=release,
        content_type=content_type,
        encoding=encoding,
    )


# ---------------------------------------------------------------------------
# convenience wrappers
# ---------------------------------------------------------------------------


def get_json(
    ctx: Any,
    url: str,
    *,
    params: Mapping[str, Any] | None = None,
    headers: Mapping[str, str] | None = None,
    cache: bool = True,
    cache_ttl: float | None = None,
    allow_status: Sequence[int] = (200,),
    detect_error: Callable[[TransportResponse], Any] | None = None,
    error_code: str | None = None,
    database: str | None = None,
    release_header: str | None = None,
) -> tuple[Any, HttpResponse]:
    """GET + parse JSON, returning ``(value, response)``."""
    response = request(
        ctx,
        "GET",
        url,
        params=params,
        headers={"accept": "application/json", **(headers or {})},
        cache=cache,
        cache_ttl=cache_ttl,
        allow_status=allow_status,
        detect_error=detect_error,
        error_code=error_code,
        database=database,
        release_header=release_header,
    )
    if response.dry_run:
        return None, response
    return response.json(), response


def get_text(
    ctx: Any,
    url: str,
    *,
    params: Mapping[str, Any] | None = None,
    headers: Mapping[str, str] | None = None,
    cache: bool = True,
    cache_ttl: float | None = None,
    allow_status: Sequence[int] = (200,),
    error_code: str | None = None,
    database: str | None = None,
    release_header: str | None = None,
) -> tuple[str, HttpResponse]:
    """GET + decode text (FASTA, XML, TSV payloads)."""
    response = request(
        ctx,
        "GET",
        url,
        params=params,
        headers=headers,
        cache=cache,
        cache_ttl=cache_ttl,
        allow_status=allow_status,
        error_code=error_code,
        database=database,
        release_header=release_header,
    )
    if response.dry_run:
        return "", response
    return response.text(), response


def get_bytes(
    ctx: Any,
    url: str,
    *,
    params: Mapping[str, Any] | None = None,
    headers: Mapping[str, str] | None = None,
    cache: bool = True,
    cache_ttl: float | None = None,
    allow_status: Sequence[int] = (200,),
    error_code: str | None = None,
    database: str | None = None,
    release_header: str | None = None,
) -> tuple[bytes, HttpResponse]:
    response = request(
        ctx,
        "GET",
        url,
        params=params,
        headers=headers,
        cache=cache,
        cache_ttl=cache_ttl,
        allow_status=allow_status,
        error_code=error_code,
        database=database,
        release_header=release_header,
    )
    if response.dry_run:
        return b"", response
    return response.body, response


# ---------------------------------------------------------------------------
# internals
# ---------------------------------------------------------------------------


def _record(
    ctx: Any,
    **kwargs: Any,
) -> dict[str, Any]:
    """Append a provenance entry to the context (and log when configured)."""
    log = getattr(ctx, "_provenance_log", None)
    if log is None:
        log = provenance.ProvenanceLog(getattr(ctx, "log_path", None))
        ctx._provenance_log = log
    entry = log.http(**kwargs)
    ctx.provenance.append(entry)
    return entry


def _cache_response_headers(headers: Mapping[str, str]) -> dict[str, str]:
    """Response headers safe to persist next to a *decoded* cached body."""
    out: dict[str, str] = {}
    for key, value in headers.items():
        lowered = key.lower()
        if lowered == "content-encoding" or lowered.startswith("x-lifesci-"):
            continue
        out[key] = value
    return out


def _decode_content_encoding(response: TransportResponse) -> TransportResponse:
    """Decode a gzip Content-Encoding body once, normalising the headers.

    The cache stores the *decoded* representation and its metadata no longer
    advertises ``content-encoding: gzip`` (it records the decoding instead), so
    cached and live responses have identical semantics.
    """
    encoding = (response.header("content-encoding") or "").strip().lower()
    if encoding != "gzip" or not response.body:
        return response
    try:
        body = gzip.decompress(response.body)
    except OSError:
        # A lying header on an error page is just an error page; do not fail
        # here, let the caller see the raw body and report it.
        return response
    headers = dict(response.headers)
    headers["x-lifesci-content-encoding"] = "gzip-decoded"
    return TransportResponse(
        status=response.status,
        headers=headers,
        body=body,
        url=response.url,
        reason=response.reason,
        elapsed_ms=response.elapsed_ms,
    )


def _encoding_from_headers(headers: Mapping[str, str]) -> str:
    content_type = headers.get("content-type", "") or ""
    if "charset=" in content_type:
        return content_type.split("charset=")[-1].split(";")[0].strip() or "utf-8"
    return "utf-8"


def _normalise_body_error(value: Any) -> tuple[str, bool]:
    """Normalise a ``detect_error`` result into ``(message, retriable)``."""
    if isinstance(value, str):
        return value, True
    if isinstance(value, tuple) and len(value) >= 2:
        return str(value[0]), bool(value[1])
    if isinstance(value, Mapping):
        return str(value.get("message") or value), bool(value.get("retriable", True))
    return str(value), True


def error_detector_from_keys(*keys: str) -> Callable[[TransportResponse], Any]:
    """Build a ``detect_error`` callback matching JSON top-level error keys."""

    def detect(response: TransportResponse) -> Any:
        if response.status is None or response.status >= 400 or not response.body:
            return None
        content_type = (response.headers.get("content-type") or "").lower()
        if "json" not in content_type and not response.body.lstrip().startswith(b"{"):
            return None
        try:
            payload = json.loads(response.body.decode("utf-8", errors="replace"))
        except json.JSONDecodeError:
            return None
        if not isinstance(payload, Mapping):
            return None
        for key in keys:
            if key in payload:
                value = payload[key]
                if isinstance(value, Mapping):
                    message = value.get("message") or value.get("error") or util.canonical_json(value)
                else:
                    message = str(value)
                return f"{key}: {message}"
        return None

    return detect


__all__ = [
    "HttpResponse",
    "RETRY_STATUSES",
    "build_url",
    "error_detector_from_keys",
    "get_bytes",
    "get_json",
    "get_text",
    "get_limiter",
    "get_transport",
    "request",
]


# Re-export the fake transport so tests can import it from here.
__all__ += ["FakeTransport", "TransportResponse", "TransportError"]
