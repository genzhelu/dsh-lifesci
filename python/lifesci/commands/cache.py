"""``lifesci.cache`` — inspect, verify, prune, and clear the content-addressed cache.

The cache is the reproducibility backbone of the plugin: every
fetched payload is keyed by a SHA-256 of its request and can be replayed
offline.  This command exposes the cache for maintenance **without ever touching
the network**, so it behaves identically in ``--offline`` runs:

* ``stats`` — entry/byte/database counts, expired entries, missing bodies;
* ``list`` — deterministic per-entry metadata (no wall-clock-dependent fields);
* ``verify`` — re-hash every checked body and report corrupt entries;
* ``prune`` — delete expired entries (``dry_run`` reports only);
* ``clear`` — delete **all** entries; requires ``confirm: true`` in the input.

Destructive actions are explicit: ``prune`` honours ``dry_run``/``--dry-run``
and ``clear`` refuses to run unless the caller passes ``confirm: true``.
"""

from __future__ import annotations

from typing import Any

from .. import util
from ..cache import Cache, cache_from_context
from ..commands.base import command
from ..errors import CacheCorruptError, UsageError

INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "action": {
            "type": "string",
            "enum": ["stats", "list", "verify", "prune", "clear"],
            "description": (
                "Cache operation: 'stats' summary counts; 'list' per-entry metadata; "
                "'verify' re-hash cached bodies; 'prune' delete expired entries; "
                "'clear' delete the whole cache (requires confirm:true)."
            ),
        },
        "limit": {
            "type": "integer",
            "minimum": 1,
            "maximum": 100000,
            "description": (
                "Maximum number of entries to list or verify (deterministic "
                "key order). Omit for all entries."
            ),
        },
        "database": {
            "type": "string",
            "description": (
                "Only include entries recorded for this database name "
                "(e.g. 'uniprot', 'interpro'); omitted means every database."
            ),
        },
        "expired_only": {
            "type": "boolean",
            "default": False,
            "description": "For 'list'/'verify': only include entries whose TTL has expired.",
        },
        "dry_run": {
            "type": "boolean",
            "default": False,
            "description": "For 'prune': report what would be removed without deleting anything.",
        },
        "confirm": {
            "type": "boolean",
            "default": False,
            "description": (
                "For 'clear': must be true. This is a deliberate second confirmation "
                "because clearing destroys the offline replay cache."
            ),
        },
    },
    "required": ["action"],
}

_OUTPUT_SCHEMA = "cache.schema.json"


def _entry_summary(entry: Any) -> dict[str, Any]:
    """Deterministic metadata for one cache entry (no wall-clock age field)."""
    return {
        "key": entry.key,
        "url": entry.url,
        "database": entry.database,
        "release": entry.release,
        "status": entry.status,
        "bytes": entry.bytes,
        "sha256": entry.sha256,
        "stored_at": entry.stored_at,
        "ttl_seconds": entry.ttl_seconds,
        "expired": entry.expired,
        "has_body": entry.body_path.is_file(),
    }


def _selected_entries(
    cache: Cache,
    *,
    database: str | None,
    expired_only: bool,
    limit: int | None,
) -> list[Any]:
    entries = [entry for entry in cache.entries() if not database or entry.database == database]
    if expired_only:
        entries = [entry for entry in entries if entry.expired]
    entries.sort(key=lambda item: item.key)
    if limit is not None:
        entries = entries[: int(limit)]
    return entries


def _verify_entries(entries: list[Any]) -> dict[str, Any]:
    report: list[dict[str, Any]] = []
    for entry in entries:
        try:
            entry.read_bytes(verify=True)
        except CacheCorruptError as err:
            report.append(
                {
                    "key": entry.key,
                    "url": entry.url,
                    "ok": False,
                    "error": err.message,
                }
            )
        else:
            report.append({"key": entry.key, "url": entry.url, "ok": True})
    n_failed = sum(1 for row in report if not row["ok"])
    return {
        "ok": n_failed == 0,
        "n_checked": len(report),
        "n_failed": n_failed,
        "entries": report,
    }


@command(
    name="cache",
    title="Inspect and maintain the lifesci cache",
    summary="Report cache stats, list/verify entries, prune expired payloads, or clear the cache.",
    description=(
        "Maintenance for the content-addressed HTTP cache that makes lifesci runs replayable "
        "offline. Actions: 'stats' (counts/bytes/databases/expired), 'list' (deterministic "
        "per-entry metadata), 'verify' (re-hash bodies; reports corruption), 'prune' (delete "
        "expired entries; honour dry_run), 'clear' (delete everything; requires confirm:true). "
        "This command never touches the network, so it is safe under --offline. Clearing the "
        "cache destroys the ability to reproduce prior results offline; verify first and export "
        "anything you need. Entries are sorted by cache key for deterministic output."
    ),
    input_schema=INPUT_SCHEMA,
    output_schema=_OUTPUT_SCHEMA,
    category="foundation",
    timeout_ms=300_000,
    examples=(
        {"action": "stats"},
        {"action": "list", "limit": 20},
        {"action": "prune", "dry_run": True},
        {"action": "clear", "confirm": True},
    ),
    danger="write",
)
def run(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
    ctx.validate()
    action = str(args.get("action") or "")
    if action not in {"stats", "list", "verify", "prune", "clear"}:
        raise UsageError(
            f"unknown cache action {action!r}",
            details={"action": action, "known": ["stats", "list", "verify", "prune", "clear"]},
        )
    cache = cache_from_context(ctx)
    database = args.get("database")
    expired_only = bool(args.get("expired_only", False))
    limit = args.get("limit")
    limit_int = int(limit) if limit is not None else None

    payload: dict[str, Any] = {
        "action": action,
        "root": str(cache.root),
        "filters": {
            "database": database,
            "expired_only": expired_only,
            "limit": limit_int,
        },
        "offline": bool(ctx.offline),
        "dry_run": bool(ctx.dry_run),
    }

    if action == "stats":
        payload["stats"] = util.json_safe(cache.stats())
        return payload

    if action == "list":
        entries = _selected_entries(
            cache,
            database=database,
            expired_only=expired_only,
            limit=limit_int,
        )
        payload["entries"] = [_entry_summary(entry) for entry in entries]
        payload["n_entries"] = len(entries)
        return payload

    if action == "verify":
        entries = _selected_entries(
            cache,
            database=database,
            expired_only=expired_only,
            limit=limit_int,
        )
        verification = _verify_entries(entries)
        payload["verification"] = verification
        payload["n_entries"] = len(entries)
        if verification["n_failed"]:
            ctx.warn(
                "CACHE_CORRUPT",
                f"{verification['n_failed']} of {verification['n_checked']} cache entries failed "
                "SHA-256 verification; delete them or re-fetch with the same parameters.",
                context={"failed": [row["key"] for row in verification["entries"] if not row["ok"]]},
            )
        return payload

    if action == "prune":
        effective_dry_run = bool(args.get("dry_run", False)) or bool(ctx.dry_run)
        result = cache.prune_expired(dry_run=effective_dry_run)
        payload["prune"] = util.json_safe(result)
        return payload

    # clear ----------------------------------------------------------------
    if not args.get("confirm", False):
        raise UsageError(
            "cache clear requires confirm:true in the input; nothing was deleted. "
            "Run action='verify' and/or action='prune', or export the cache first.",
            code="CONFIRM_REQUIRED",
            details={"action": "clear", "confirm": False, "root": str(cache.root)},
        )
    if ctx.dry_run:
        entries = cache.entries()
        payload["clear"] = {
            "dry_run": True,
            "n_entries": len(entries),
            "bytes": sum(entry.bytes for entry in entries),
            "root": str(cache.root),
            "would_clear": True,
        }
        return payload
    payload["clear"] = util.json_safe(cache.clear())
    return payload
