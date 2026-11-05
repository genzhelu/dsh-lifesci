"""Command envelope construction and self-validation.

Every lifesci command returns the same JSON envelope so the agent, the CLI, and
the provenance log can consume results uniformly::

    {"ok": true, "command": "seq.fetch", "version": "0.1.0",
     "schema_version": 1, "started_at": "...", "finished_at": "...",
     "duration_s": 1.2, "offline": false, "dry_run": false,
     "cache_dir": "...", "data": {...},
     "warnings": [...], "human_review": [...], "provenance": [...]}

On failure the envelope carries ``ok: false``, ``data: null`` and a structured
``error`` object; warnings/provenance are still returned so a partial run remains
auditable.
"""

from __future__ import annotations

import json
from functools import lru_cache
from typing import Any, Mapping

from . import __version__, paths, util
from .errors import LifesciError

ENVELOPE_SCHEMA_VERSION = 1


def make_envelope(
    *,
    command: str,
    data: Any,
    ctx: Any,
    started_at: str,
    finished_at: str,
    duration_s: float,
    error: LifesciError | None = None,
) -> dict[str, Any]:
    """Build the canonical JSON envelope for one command run."""
    envelope: dict[str, Any] = {
        "ok": error is None,
        "command": command,
        "version": __version__,
        "schema_version": ENVELOPE_SCHEMA_VERSION,
        "started_at": started_at,
        "finished_at": finished_at,
        "duration_s": round(duration_s, 3),
        "offline": bool(getattr(ctx, "offline", False)),
        "dry_run": bool(getattr(ctx, "dry_run", False)),
        "cache_dir": str(getattr(ctx, "cache_dir", "")),
        "data": util.json_safe(data) if error is None else None,
        "warnings": util.json_safe(list(getattr(ctx, "warnings", []))),
        "human_review": util.json_safe(list(getattr(ctx, "human_review", []))),
        "provenance": util.json_safe(list(getattr(ctx, "provenance", []))),
    }
    if error is not None:
        envelope["error"] = error.to_dict()
    return envelope


@lru_cache(maxsize=1)
def _validator() -> Any:
    """Lazily build the envelope JSON-schema validator."""
    import jsonschema
    from jsonschema import Draft202012Validator

    schema = util.read_json(paths.schema_path("envelope.schema.json"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def validate_envelope(envelope: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Return schema-validation errors for an envelope (empty means valid).

    The validator is intentionally tolerant about payload contents (``data`` is
    free-form) but strict about the envelope contract itself.
    """
    try:
        validator = _validator()
    except (FileNotFoundError, ImportError) as err:  # pragma: no cover - env issue
        return [{"path": "$", "message": f"cannot load envelope schema: {err}"}]
    errors: list[dict[str, Any]] = []
    for error in sorted(validator.iter_errors(dict(envelope)), key=lambda e: list(e.absolute_path)):
        errors.append(
            {
                "path": "/".join(str(part) for part in error.absolute_path) or "$",
                "message": error.message,
            }
        )
    return errors


def validate_payload(data: Any, schema_name: str) -> list[dict[str, Any]]:
    """Validate a command payload against ``schemas/<schema_name>``."""
    import jsonschema
    from jsonschema import Draft202012Validator

    try:
        schema = util.read_json(paths.schema_path(schema_name))
    except FileNotFoundError:
        return [{"path": "$", "message": f"missing payload schema {schema_name}"}]
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema)
    errors: list[dict[str, Any]] = []
    for error in sorted(validator.iter_errors(util.json_safe(data)), key=lambda e: list(e.absolute_path)):
        errors.append(
            {
                "path": "/".join(str(part) for part in error.absolute_path) or "$",
                "message": error.message,
            }
        )
    return errors


def dumps_envelope(envelope: Mapping[str, Any]) -> str:
    """Serialise an envelope to deterministic, human-readable JSON."""
    return json.dumps(util.json_safe(envelope), sort_keys=True, indent=2, ensure_ascii=False) + "\n"
