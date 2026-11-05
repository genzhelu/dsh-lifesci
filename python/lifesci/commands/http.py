"""``lifesci.http`` — generic retrying/caching HTTP primitive exposed as a command.

This is the escape hatch for endpoints that do not (yet) have a dedicated
adapter.  It routes through :func:`lifesci.http.request`, so the caller gets the
same content-addressed cache, per-host rate limiting, exponential backoff,
``--offline`` cache replay, ``--dry-run`` planning, and provenance logging as
every first-class adapter.  Prefer a dedicated command when one exists: this
primitive does not parse portal-specific payloads or know portal quirks.

The returned payload contains the request plan, response metadata (status,
headers, SHA-256, byte count, cache hit, attempts, elapsed ms) and, when
requested, the decoded body.  Large bodies are not echoed into the envelope
unless the caller writes them with ``out``.
"""

from __future__ import annotations

import base64
import binascii
from pathlib import Path
from typing import Any

from .. import http, util
from ..commands.base import command
from ..errors import InvalidInputError

#: Actions that are safe to retry on transient failures; writes are not.
_IDEMPOTENT_METHODS = {"GET", "HEAD", "OPTIONS"}
_ALLOWED_METHODS = ("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE")

INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["url"],
    "properties": {
        "url": {
            "type": "string",
            "minLength": 1,
            "description": (
                "Absolute http(s) URL without query string. Query parameters go in "
                "'params' so they are sorted deterministically, recorded in provenance, "
                "and form part of the cache key."
            ),
        },
        "method": {
            "type": "string",
            "enum": list(_ALLOWED_METHODS),
            "default": "GET",
            "description": (
                "HTTP method. GET/HEAD/OPTIONS are retried on transient failures; "
                "POST/PUT/PATCH/DELETE are only retried when 'idempotent' is true."
            ),
        },
        "params": {
            "type": "object",
            "description": "Query parameters; values are converted to strings and sorted before use.",
            "additionalProperties": {"type": ["string", "number", "boolean", "null"]},
        },
        "headers": {
            "type": "object",
            "description": (
                "Extra request headers. The lifesci User-Agent is added automatically. "
                "Authorization/token headers are never cached or written to the provenance log."
            ),
            "additionalProperties": {"type": "string"},
        },
        "body": {
            "type": "string",
            "description": "Raw request body as text (UTF-8 encoded). Mutually exclusive with other body fields.",
        },
        "body_base64": {
            "type": "string",
            "description": "Raw request body as base64 (for binary payloads). Mutually exclusive with other body fields.",
        },
        "json_body": {
            "description": "Request body serialised as canonical JSON (sets Content-Type: application/json).",
            "type": ["object", "array"],
        },
        "form_fields": {
            "type": "object",
            "description": (
                "urlencoded form fields. When 'files' is also given, these become the ordinary "
                "multipart/form-data fields of the upload."
            ),
            "additionalProperties": {"type": ["string", "number", "boolean"]},
        },
        "files": {
            "type": "array",
            "description": (
                "Multipart file uploads. Only multipart file upload works for some APIs "
                "(e.g. Foldseek web ticket); JSON bodies are rejected by those servers."
            ),
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["field", "path"],
                "properties": {
                    "field": {"type": "string", "description": "Form field name, e.g. 'q'."},
                    "path": {
                        "type": "string",
                        "description": "Local file to upload; relative paths resolve against the working directory.",
                    },
                    "filename": {
                        "type": "string",
                        "description": "Filename sent to the server (defaults to the basename of 'path').",
                    },
                    "content_type": {
                        "type": "string",
                        "default": "application/octet-stream",
                        "description": "MIME type sent for this file part.",
                    },
                },
            },
        },
        "cache": {
            "type": "boolean",
            "default": True,
            "description": (
                "Read/write the content-addressed cache. Set false for polling endpoints "
                "whose URL changes or that must be re-requested."
            ),
        },
        "cache_ttl": {
            "type": "number",
            "minimum": 0,
            "description": "Override the cache TTL in seconds for this response.",
        },
        "retries": {
            "type": "integer",
            "minimum": 0,
            "maximum": 20,
            "description": "Transient retry budget for this request (overrides the context default).",
        },
        "timeout": {
            "type": "number",
            "minimum": 0.1,
            "description": "Per-attempt timeout in seconds (overrides the context default).",
        },
        "allow_status": {
            "type": "array",
            "items": {"type": "integer", "minimum": 100, "maximum": 599},
            "default": [200],
            "description": (
                "HTTP statuses treated as success. Non-listed statuses raise a PortalError "
                "with the status appended to the error code (e.g. HTTP_ERROR_404)."
            ),
        },
        "database": {
            "type": "string",
            "description": "Optional database label recorded in the provenance cache entry (e.g. 'uniprot').",
        },
        "release_header": {
            "type": "string",
            "description": (
                "Response header carrying the database release (e.g. 'x-uniprot-release'); "
                "its value is recorded in the release registry and provenance."
            ),
        },
        "idempotent": {
            "type": "boolean",
            "description": (
                "Force or forbid retries for a write request. Defaults to true for "
                "GET/HEAD/OPTIONS and false for body-bearing methods."
            ),
        },
        "parse": {
            "type": "string",
            "enum": ["auto", "none", "json", "text"],
            "default": "auto",
            "description": (
                "Decode the response body into the payload. 'auto' parses JSON when the "
                "content type looks like JSON, otherwise returns a truncated text preview. "
                "'none' returns metadata only (useful with 'out')."
            ),
        },
        "max_parse_bytes": {
            "type": "integer",
            "minimum": 0,
            "default": 2000000,
            "description": (
                "Do not echo parsed bodies larger than this many bytes into the envelope; "
                "write them with 'out' instead. Prevents multi-megabyte tool results."
            ),
        },
        "text_preview_bytes": {
            "type": "integer",
            "minimum": 0,
            "default": 4000,
            "description": "Maximum characters of a text body echoed into the payload.",
        },
        "out": {
            "type": "string",
            "description": (
                "Optional path: write the raw response body here and record its SHA-256. "
                "Relative paths resolve against the working directory."
            ),
        },
    },
}


def _resolve_path(ctx: Any, value: str) -> Path:
    target = Path(str(value)).expanduser()
    if not target.is_absolute():
        target = Path(ctx.cwd) / target
    return target.resolve()


def _build_body(args: dict[str, Any], ctx: Any, *, read_files: bool) -> dict[str, Any]:
    """Translate the input body fields into ``http.request`` keyword arguments."""
    present = [
        name
        for name in ("body", "body_base64", "json_body", "form_fields", "files")
        if args.get(name) is not None
    ]
    # 'form_fields' is the ordinary-fields companion of a multipart 'files' upload.
    exclusive = [name for name in present if not (name == "form_fields" and "files" in present)]
    if len(exclusive) > 1 or ("form_fields" in present and "files" not in present and len(present) > 1):
        raise InvalidInputError(
            "provide only one request body form",
            details={"provided": present, "allowed": ["body", "body_base64", "json_body", "form_fields", "files"]},
        )
    kwargs: dict[str, Any] = {}
    if "files" in present:
        files: list[tuple[str, str, str, bytes]] = []
        for index, item in enumerate(args["files"] or []):
            field = str(item.get("field") or "")
            path = item.get("path")
            if not field or not path:
                raise InvalidInputError(f"files[{index}] requires both 'field' and 'path'")
            filename = str(item.get("filename") or Path(str(path)).name)
            content_type = str(item.get("content_type") or "application/octet-stream")
            content = b"" if not read_files else _resolve_path(ctx, str(path)).read_bytes()
            files.append((field, filename, content_type, content))
        kwargs["files"] = files
        if args.get("form_fields") is not None:
            kwargs["form_fields"] = dict(args["form_fields"])
    elif "body" in present:
        kwargs["body"] = str(args["body"]).encode("utf-8")
    elif "body_base64" in present:
        try:
            kwargs["body"] = base64.b64decode(str(args["body_base64"]), validate=True)
        except (binascii.Error, ValueError) as err:
            raise InvalidInputError(f"'body_base64' is not valid base64: {err}") from err
    elif "json_body" in present:
        kwargs["json_body"] = args["json_body"]
    elif "form_fields" in present:
        kwargs["form_fields"] = dict(args["form_fields"])
    return kwargs


def _plan(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
    method = str(args.get("method", "GET")).upper()
    url = str(args["url"])
    final_url = http.build_url(url, args.get("params"))
    body_kwargs = _build_body(args, ctx, read_files=False)
    has_body = any(key in body_kwargs for key in ("body", "json_body", "form_fields", "files"))
    return {
        "command": "http",
        "dry_run": True,
        "request": {
            "method": method,
            "url": url,
            "final_url": final_url,
            "params": util.json_safe(dict(args.get("params") or {})),
            "cache": bool(args.get("cache", True)),
            "has_body": has_body,
            "body_forms": sorted(name for name in ("body", "body_base64", "json_body", "form_fields", "files") if args.get(name) is not None),
            "files": [
                {"field": item.get("field"), "path": str(item.get("path")), "filename": item.get("filename")}
                for item in (args.get("files") or [])
            ],
            "allow_status": list(args.get("allow_status") or [200]),
            "database": args.get("database"),
        },
        "response": {
            "status": None,
            "headers": {},
            "sha256": None,
            "bytes": None,
            "from_cache": False,
            "attempts": 0,
            "elapsed_ms": 0.0,
            "dry_run": True,
        },
        "parsed": None,
        "out": None,
    }


def _decode_payload(
    args: dict[str, Any],
    response: Any,
    ctx: Any,
) -> tuple[Any, str | None]:
    """Return ``(parsed_value, skipped_reason)`` for the response body."""
    mode = str(args.get("parse") or "auto").lower()
    if mode == "none" or response.dry_run:
        return None, None
    body = response.body or b""
    max_parse = int(args.get("max_parse_bytes", 2_000_000))
    content_type = (response.content_type or "").lower()
    if mode == "auto":
        mode = "json" if "json" in content_type or body.lstrip().startswith((b"{", b"[")) else "text"
    if mode == "json":
        if len(body) > max_parse:
            return None, f"body is {len(body)} bytes (> max_parse_bytes={max_parse}); write it with 'out'"
        return response.json(), None
    text = response.text()
    if len(text) > max_parse:
        return None, f"body is {len(text)} characters (> max_parse_bytes={max_parse}); write it with 'out'"
    limit = int(args.get("text_preview_bytes", 4000))
    truncated = len(text) > limit
    return {"text": text[:limit], "truncated": truncated, "total_characters": len(text)}, None


@command(
    name="http",
    title="Raw cached HTTP request",
    summary="Perform one retrying/caching/rate-limited HTTP request with full provenance.",
    description=(
        "Generic HTTP primitive for endpoints without a dedicated lifesci adapter. Goes through the "
        "same content-addressed cache, per-host rate limiting, exponential backoff, offline replay "
        "and provenance logging as every adapter. Returns request metadata, response status/headers/"
        "SHA-256/bytes/cache-hit/attempts and, when 'parse' allows it, the decoded body. In --dry-run "
        "it returns the planned request without touching the network; in --offline it replays a cached "
        "payload only. Caveats: E-values/TM-scores in arbitrary returned payloads carry the semantics "
        "of the source database (not comparable across databases, often query-normalised and top-hit "
        "capped) - this command only guarantees provenance, not scientific interpretation."
    ),
    input_schema=INPUT_SCHEMA,
    output_schema="http.schema.json",
    category="foundation",
    timeout_ms=180_000,
    danger="network",
    examples=(
        {"url": "https://rest.uniprot.org/uniprotkb/P69905.fasta", "parse": "text"},
        {"url": "https://www.ebi.ac.uk/interpro/api/entry/pfam/PF00042/", "parse": "json"},
    ),
)
def run(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
    """Execute one HTTP request and return response metadata plus optional body."""
    ctx.validate()
    method = str(args.get("method", "GET")).upper()
    if method not in _ALLOWED_METHODS:
        raise InvalidInputError(f"unsupported HTTP method {method!r}", details={"allowed": list(_ALLOWED_METHODS)})
    if not str(args.get("url") or "").strip():
        raise InvalidInputError("'url' must be a non-empty string")

    if ctx.dry_run:
        return _plan(args, ctx)

    body_kwargs = _build_body(args, ctx, read_files=True)
    idempotent = args.get("idempotent")
    if idempotent is None:
        idempotent = method in _IDEMPOTENT_METHODS

    response = http.request(
        ctx,
        method,
        str(args["url"]),
        params=args.get("params"),
        headers=args.get("headers"),
        cache=bool(args.get("cache", True)),
        cache_ttl=args.get("cache_ttl"),
        retries=args.get("retries"),
        timeout=args.get("timeout"),
        allow_status=tuple(args.get("allow_status") or (200,)),
        database=args.get("database"),
        release_header=args.get("release_header"),
        idempotent=bool(idempotent),
        **body_kwargs,
    )

    parsed, parse_skipped = _decode_payload(args, response, ctx)
    if parse_skipped:
        ctx.warn(
            "HTTP_RESPONSE_NOT_ECHOED",
            "Response body was not echoed into the payload; " + parse_skipped + ".",
            context={"url": response.final_url, "bytes": len(response.body or b"")},
        )

    body = response.body or b""
    out_info: dict[str, Any] | None = None
    out_value = args.get("out")
    if out_value:
        out_path = _resolve_path(ctx, str(out_value))
        util.atomic_write_bytes(out_path, body)
        out_info = {
            "path": str(out_path),
            "sha256": util.sha256_bytes(body),
            "bytes": len(body),
        }
        from .. import provenance

        log = getattr(ctx, "_provenance_log", None)
        if log is None:
            log = provenance.ProvenanceLog(ctx.log_path)
            ctx._provenance_log = log
        ctx.provenance.append(
            log.local_file(
                path=str(out_path),
                sha256=out_info["sha256"],
                bytes_=len(body),
                kind="output_file",
                notes={"command": "http", "url": response.final_url},
            )
        )

    body_sha = util.sha256_bytes(body)
    request_info = {
        "method": method,
        "url": str(args["url"]),
        "final_url": response.final_url,
        "params": util.json_safe(dict(args.get("params") or {})),
        "headers_sent": sorted(str(k).lower() for k in (args.get("headers") or {})),
        "cache": bool(args.get("cache", True)),
        "has_body": bool(body_kwargs),
        "body_sha256": body_sha if body_kwargs else None,
        "idempotent": bool(idempotent),
        "database": args.get("database"),
    }
    response_info = {
        "status": response.status,
        "headers": util.json_safe(dict(response.headers or {})),
        "sha256": body_sha,
        "bytes": len(body),
        "from_cache": bool(response.from_cache),
        "attempts": response.attempts,
        "elapsed_ms": round(float(response.elapsed_ms), 1),
        "request_id": response.request_id,
        "content_type": response.content_type,
        "encoding": response.encoding,
        "release": response.release,
        "final_url": response.final_url,
        "cache_key": response.cache_key,
        "dry_run": bool(response.dry_run),
    }
    payload: dict[str, Any] = {
        "command": "http",
        "dry_run": False,
        "request": request_info,
        "response": response_info,
        "parsed": parsed,
        "out": out_info,
    }
    if response.release:
        payload["resolved"] = {"release": response.release, "database": args.get("database")}
    return payload
