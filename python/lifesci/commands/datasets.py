"""``lifesci.datasets`` — register, list, verify, inspect, and remove local read-only datasets.

Reference data that is too large or too specific to fetch through a portal
(alignments, length tables, curated presence matrices, local structure
collections) can be registered as a **read-only dataset**.  The
registry is a small JSON file (``ctx.datasets_file``) recording the name, kind,
path, byte size and SHA-256 of every dataset, so results can be reproduced and
tampering can be detected.

The command refuses to register a missing path or a payload whose checksum does
not match the recorded one; it never writes to the dataset itself (read-only by
contract).  ``remove`` only unregisters a dataset — it does not delete files.

The registry itself is written by the :mod:`lifesci.adapters.local_dataset`
adapter; ``remove`` is the only action implemented here directly (an intentional
registry edit of one known key).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .. import util
from ..commands.base import command
from ..errors import ConfigError, InvalidInputError, NotFoundError

INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "action": {
            "type": "string",
            "enum": ["register", "list", "verify", "get", "remove"],
            "description": (
                "Dataset registry action: 'register' a new read-only dataset with checksum; "
                "'list' all registered datasets with existence/checksum status; 'verify' "
                "re-hash one dataset; 'get' resolve one dataset (optionally one file inside "
                "it); 'remove' unregister a dataset (never deletes data files)."
            ),
        },
        "name": {
            "type": "string",
            "pattern": "^[A-Za-z0-9_.-]{1,80}$",
            "description": "Registered dataset name (required for register/verify/get/remove).",
        },
        "path": {
            "type": "string",
            "description": "Absolute or cwd-relative path to the dataset file or directory (register only).",
        },
        "kind": {
            "type": "string",
            "enum": [
                "fasta", "tsv", "json", "msa", "structure", "structure_dir",
                "processed_dir", "table", "hmm", "hmm_dir", "generic_dir",
            ],
            "description": "Dataset kind; directories are hashed file-by-file into a manifest (register only).",
        },
        "description": {
            "type": "string",
            "description": "Free-text description of origin, version, and what the dataset contains.",
        },
        "verify": {
            "type": "boolean",
            "default": True,
            "description": "For register: hash and verify the payload before recording it (recommended).",
        },
        "filename": {
            "type": "string",
            "description": "For get: resolve this specific file inside a registered directory dataset.",
        },
    },
}

_KINDS = {
    "fasta", "tsv", "json", "msa", "structure", "structure_dir",
    "processed_dir", "table", "hmm", "hmm_dir", "generic_dir",
}


def _adapter() -> Any:
    """Import the local-dataset adapter lazily (parallel workstream ownership)."""
    import importlib

    try:
        return importlib.import_module("lifesci.adapters.local_dataset")
    except ImportError as err:  # pragma: no cover - parallel-workstream guard
        raise ConfigError(
            "the lifesci local_dataset adapter is not available in this checkout; "
            "dataset registration requires python/lifesci/adapters/local_dataset.py",
            details={"import_error": f"{type(err).__name__}: {err}"},
        ) from err


def _registry_path(ctx: Any) -> Path:
    if not getattr(ctx, "datasets_file", None):
        raise ConfigError("no dataset registry path is configured for this context")
    return Path(ctx.datasets_file)


def _normalise_datasets(value: Any) -> list[dict[str, Any]]:
    """Normalise adapter list output (list of records or {'datasets': [...]})."""
    if value is None:
        return []
    if isinstance(value, dict):
        value = value.get("datasets", value)
        if isinstance(value, dict):
            records = [dict(record) for record in value.values()]
        else:
            records = list(value)
    else:
        records = list(value)
    normalised: list[dict[str, Any]] = []
    for record in records:
        if isinstance(record, dict):
            normalised.append(dict(record))
    normalised.sort(key=lambda item: str(item.get("name", "")))
    return normalised


def _record_local(ctx: Any, path: str | Path, *, role: str = "dataset") -> None:
    """Append a local-file provenance entry (read) to the envelope provenance list."""
    from .. import provenance as provenance_mod

    target = Path(path)
    if not target.is_file():
        return
    log = getattr(ctx, "_provenance_log", None)
    if log is None:
        log = provenance_mod.ProvenanceLog(ctx.log_path)
        ctx._provenance_log = log
    entry = log.local_file(
        path=target,
        sha256=util.sha256_file(target),
        bytes_=target.stat().st_size,
        notes={"role": role},
    )
    ctx.provenance.append(entry)


@command(
    name="datasets",
    title="Manage local read-only datasets",
    summary="Register, list, verify, resolve, or unregister checksum-pinned local reference data.",
    description=(
        "Local reference data (alignments, ES-length tables, curated matrices, structure "
        "collections) can be registered as read-only datasets with a recorded SHA-256. "
        "Actions: 'register' hashes a file/directory and records it (fails on a missing path or "
        "checksum mismatch); 'list' shows all datasets plus whether their files still exist; "
        "'verify' re-hashes one dataset and reports every mismatching file; 'get' resolves a "
        "dataset (or one filename inside a directory dataset) and returns its verified path; "
        "'remove' unregisters the record but never deletes data. The command never writes to "
        "dataset contents and refuses to register paths that do not exist."
    ),
    input_schema=INPUT_SCHEMA,
    output_schema="datasets.schema.json",
    category="foundation",
    timeout_ms=600_000,
    examples=(
        {"action": "list"},
        {"action": "register", "name": "reference_alignment", "path": "data/reference_alignment.fa", "kind": "msa"},
        {"action": "verify", "name": "reference_alignment"},
        {"action": "remove", "name": "reference_alignment"},
    ),
    danger="write",
)
def run(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
    ctx.validate()
    action = str(args.get("action") or "")
    if action not in {"register", "list", "verify", "get", "remove"}:
        raise InvalidInputError(
            f"unknown datasets action {action!r}",
            details={"action": action, "known": ["register", "list", "verify", "get", "remove"]},
        )
    registry = _registry_path(ctx)
    payload: dict[str, Any] = {
        "action": action,
        "registry": str(registry),
        "read_only": True,
    }

    if action == "register":
        name = args.get("name")
        path = args.get("path")
        kind = args.get("kind")
        if not name or not path or not kind:
            raise InvalidInputError(
                "datasets register requires name, path, and kind",
                details={"missing": [key for key in ("name", "path", "kind") if not args.get(key)]},
            )
        if kind not in _KINDS:
            raise InvalidInputError(
                f"unsupported dataset kind {kind!r}",
                details={"kind": kind, "known": sorted(_KINDS)},
            )
        resolved = Path(path).expanduser()
        if not resolved.is_absolute():
            resolved = (Path(ctx.cwd) / resolved).resolve()
        if not resolved.exists():
            raise NotFoundError(
                f"dataset path does not exist: {resolved}",
                code="DATASET_PATH_MISSING",
                details={"name": name, "path": str(resolved)},
                fallbacks=[
                    "Check the path (it is resolved against ctx.cwd for relative inputs).",
                    "Copy the reference data into the workspace before registering it.",
                ],
            )
        adapter = _adapter()
        record = adapter.register_dataset(
            ctx,
            name=str(name),
            path=resolved,
            kind=str(kind),
            description=args.get("description"),
            verify=bool(args.get("verify", True)),
        )
        payload["dataset"] = util.json_safe(record)
        payload["datasets"] = _normalise_datasets(adapter.list_datasets(ctx))
        return payload

    if action == "list":
        adapter = _adapter()
        datasets = _normalise_datasets(adapter.list_datasets(ctx))
        for record in datasets:
            record_path = record.get("path")
            exists = bool(record_path) and Path(str(record_path)).exists()
            record["exists"] = exists
            if not exists:
                ctx.warn(
                    "DATASET_PATH_MISSING",
                    f"Registered dataset {record.get('name')!r} points to a missing path; "
                    "re-register it or remove the stale record.",
                    context={"name": record.get("name"), "path": record_path},
                )
        payload["datasets"] = datasets
        payload["n_datasets"] = len(datasets)
        return payload

    name = args.get("name")
    if not name:
        raise InvalidInputError(f"datasets {action} requires a dataset name")
    adapter = _adapter()

    def _get_record() -> dict[str, Any]:
        try:
            return dict(adapter.get_dataset(ctx, str(name)) or {})
        except KeyError as err:
            raise NotFoundError(
                f"no registered dataset named {name!r}",
                code="DATASET_NOT_FOUND",
                details={"name": name, "registry": str(registry)},
                fallbacks=["Run datasets action='list' to see registered dataset names."],
            ) from err

    if action == "verify":
        _get_record()
        result = adapter.verify_dataset(ctx, str(name))
        payload["verification"] = util.json_safe(result)
        dataset_path = (util.json_safe(result) or {}).get("path") if isinstance(result, dict) else None
        if isinstance(result, dict) and result.get("ok") is False:
            ctx.warn(
                "DATASET_CHECKSUM_MISMATCH",
                f"dataset {name!r} failed verification; results derived from it are not reproducible.",
                context={"name": name, "entries": result.get("entries")},
            )
        if dataset_path:
            _record_local(ctx, str(dataset_path), role="dataset_verified")
        elif registry.is_file():
            _record_local(ctx, registry, role="dataset_registry")
        return payload

    if action == "get":
        record = _get_record()
        resolved = adapter.resolve_dataset_file(ctx, str(name), args.get("filename"))
        resolved_path = Path(resolved)
        entry = {
            "path": str(resolved_path),
            "sha256": util.sha256_file(resolved_path) if resolved_path.is_file() else None,
            "bytes": resolved_path.stat().st_size if resolved_path.is_file() else None,
            "exists": resolved_path.is_file(),
        }
        if not entry["exists"]:
            raise NotFoundError(
                f"dataset {name!r} resolves to a missing file: {resolved_path}",
                code="DATASET_PATH_MISSING",
                details={"name": name, "path": str(resolved_path)},
            )
        _record_local(ctx, resolved_path, role="dataset")
        payload["dataset"] = util.json_safe(record)
        payload["resolved"] = entry
        return payload

    # remove --------------------------------------------------------------
    data = util.read_json(registry, {"version": 1, "datasets": {}}) or {}
    datasets = data.get("datasets") or {}
    if str(name) not in datasets:
        raise NotFoundError(
            f"no registered dataset named {name!r}",
            code="DATASET_NOT_FOUND",
            details={"name": name, "registry": str(registry), "known": sorted(datasets)},
        )
    record = dict(datasets[str(name)])
    del datasets[str(name)]
    data["datasets"] = datasets
    if bool(ctx.dry_run):
        payload["remove"] = {
            "name": str(name),
            "dry_run": True,
            "would_remove": True,
            "dataset": util.json_safe(record),
        }
        return payload
    util.atomic_write_json(registry, data)
    _record_local(ctx, registry, role="dataset_registry")
    payload["remove"] = {
        "name": str(name),
        "dry_run": False,
        "removed": True,
        "dataset": util.json_safe(record),
        "files_deleted": False,
    }
    return payload
