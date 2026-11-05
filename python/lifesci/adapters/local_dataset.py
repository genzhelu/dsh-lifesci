"""Local read-only dataset registry adapter.

Reference data that is too large or too specific to fetch through a portal
(alignments, curated tables, structure/model collections) can be *registered* as
a read-only, checksum-pinned dataset.  The registry is one JSON file
(``ctx.datasets_file``, default ``<cache>/datasets.json``)::

    {"version": 1, "datasets": {"<name>": {
        "name": "...", "kind": "fasta", "path": "/abs/path",
        "sha256": "...", "bytes": 1234, "description": "...",
        "registered_at": "2026-11-05T...Z", "read_only": true,
        "format_verified": true, "n_files": 1, "files": [...]}}}

Design rules implemented here:

* **Read-only** — the adapter never writes into a dataset path; it only reads
  files, verifies checksums and writes the registry itself (under a small
  advisory lock so parallel agents cannot clobber each other).
* **Refuse missing paths / checksum mismatches** — registering a missing path
  raises ``DATASET_PATH_MISSING``; reading a dataset whose bytes no longer match
  the recorded SHA-256 raises ``DATASET_CHECKSUM_MISMATCH`` instead of silently
  returning tampered data.
* **Directory kinds** hash every file into a sorted manifest
  (``{"path": relative_posix, "sha256", "bytes"}``); ``verify_dataset`` re-hashes
  and reports mismatches, missing files and (drift) extra files.
* **``verify=True`` at registration** also sanity-checks the payload format
  (FASTA/TSV/JSON/structure/HMM) so a corrupt file is rejected at registration
  time instead of failing later inside a scientific calculation.
"""

from __future__ import annotations

import contextlib
import io
import os
import re
import time
from pathlib import Path
from typing import Any, Iterator, Mapping

from .. import util
from ..errors import ConfigError, DataError, InvalidInputError, LifesciError, NotFoundError
from . import _common

#: Registry format version written into the JSON file.
REGISTRY_VERSION = 1

#: Dataset kinds stored as a single file.
FILE_KINDS: frozenset[str] = frozenset(
    {"fasta", "tsv", "json", "msa", "structure", "table", "hmm"}
)

#: Dataset kinds stored as a directory (hashed file-by-file into a manifest).
DIR_KINDS: frozenset[str] = frozenset(
    {"structure_dir", "processed_dir", "generic_dir", "hmm_dir"}
)

#: Every supported kind.
KINDS: frozenset[str] = FILE_KINDS | DIR_KINDS

#: Registry names: filesystem-safe, no path separators, command schema-compatible.
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$")

#: Advisory lock timeout (seconds) and stale-lock threshold.
LOCK_TIMEOUT_S = 20.0
LOCK_STALE_S = 120.0


# ---------------------------------------------------------------------------
# registry IO
# ---------------------------------------------------------------------------


def _registry_path(ctx: Any) -> Path:
    """Resolve the registry file path (``ctx.datasets_file`` or cache default)."""
    configured = getattr(ctx, "datasets_file", None)
    if configured:
        return Path(configured).expanduser()
    from .. import paths

    return paths.default_datasets_file(Path(ctx.cache_dir))


def _load_registry(ctx: Any) -> dict[str, Any]:
    """Read the registry, returning an empty version-1 structure when absent."""
    path = _registry_path(ctx)
    payload = util.read_json(path, None)
    if payload is None:
        return {"version": REGISTRY_VERSION, "datasets": {}}
    if not isinstance(payload, Mapping):
        raise DataError(
            f"dataset registry {path} is not a JSON object",
            code="DATASET_REGISTRY_INVALID",
            details={"path": str(path), "type": type(payload).__name__},
        )
    version = payload.get("version")
    if version not in (None, REGISTRY_VERSION):
        raise DataError(
            f"dataset registry {path} has unsupported version {version!r}",
            code="DATASET_REGISTRY_INVALID",
            details={"path": str(path), "version": version},
        )
    datasets = payload.get("datasets")
    if datasets is None:
        datasets = {}
    if not isinstance(datasets, Mapping):
        raise DataError(
            f"dataset registry {path} has a non-object 'datasets' entry",
            code="DATASET_REGISTRY_INVALID",
            details={"path": str(path)},
        )
    return {"version": REGISTRY_VERSION, "datasets": dict(datasets)}


@contextlib.contextmanager
def _registry_lock(ctx: Any) -> Iterator[None]:
    """Advisory inter-process lock around registry read-modify-write cycles."""
    registry = _registry_path(ctx)
    util.ensure_dir(registry.parent)
    lock_path = registry.with_name(registry.name + ".lock")
    deadline = time.monotonic() + LOCK_TIMEOUT_S
    while True:
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            try:
                if time.time() - lock_path.stat().st_mtime > LOCK_STALE_S:
                    lock_path.unlink(missing_ok=True)
                    continue
            except FileNotFoundError:  # pragma: no cover - race with holder
                continue
            if time.monotonic() > deadline:
                raise ConfigError(
                    f"timed out waiting for the dataset registry lock {lock_path}; another "
                    "lifesci process may be registering datasets",
                    code="DATASET_REGISTRY_LOCKED",
                    details={"registry": str(registry), "lock": str(lock_path)},
                    fallbacks=["Retry in a few seconds.", "Remove a stale lock file if no process is running."],
                )
            time.sleep(0.05)
            continue
        try:
            os.write(fd, f"{os.getpid()}".encode())
        finally:
            os.close(fd)
        break
    try:
        yield
    finally:
        with contextlib.suppress(FileNotFoundError):
            lock_path.unlink()


def _save_registry(ctx: Any, registry: Mapping[str, Any]) -> Path:
    """Atomically write the registry and return its path."""
    path = _registry_path(ctx)
    util.atomic_write_json(path, {"version": REGISTRY_VERSION, "datasets": dict(registry["datasets"])})
    return path


# ---------------------------------------------------------------------------
# path / name helpers
# ---------------------------------------------------------------------------


def _resolve_path(ctx: Any, value: str | Path) -> Path:
    """Resolve a user-supplied path against the context working directory."""
    path = Path(str(value)).expanduser()
    if not path.is_absolute():
        path = (Path(getattr(ctx, "cwd", Path.cwd())) / path).resolve()
    return path


def _require_name(name: Any) -> str:
    text = str(name or "").strip()
    if not NAME_RE.match(text):
        raise InvalidInputError(
            f"dataset name {name!r} is invalid: use 1-80 characters of letters, digits, "
            "'.', '_' or '-', starting with a letter or digit",
            details={"name": name},
        )
    return text


def _safe_child(root: Path, filename: str) -> Path:
    """Resolve one file inside a directory dataset, rejecting path escapes."""
    candidate_name = str(filename or "").strip()
    if not candidate_name:
        raise InvalidInputError("filename must be a non-empty relative path inside the dataset")
    rel = Path(candidate_name)
    if rel.is_absolute() or any(part in {"..", ""} for part in rel.parts):
        raise InvalidInputError(
            f"filename {filename!r} must be a relative path inside the dataset directory",
            code="DATASET_PATH_ESCAPE",
            details={"filename": filename},
        )
    root_resolved = root.resolve()
    candidate = (root_resolved / rel).resolve()
    if not candidate.is_relative_to(root_resolved):
        raise InvalidInputError(
            f"filename {filename!r} escapes the dataset directory",
            code="DATASET_PATH_ESCAPE",
            details={"filename": filename, "dataset_root": str(root_resolved)},
        )
    return candidate


def _relative_posix(root: Path, path: Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()


# ---------------------------------------------------------------------------
# hashing
# ---------------------------------------------------------------------------


def _hash_file(path: Path) -> dict[str, Any]:
    return {"path": path.name, "sha256": util.sha256_file(path), "bytes": path.stat().st_size}


def _manifest_for_dir(root: Path) -> list[dict[str, Any]]:
    """Hash every regular file below ``root`` into a deterministic manifest."""
    files: list[Path] = []
    for path in root.rglob("*"):
        if path.is_file() and not path.is_symlink():
            files.append(path)
    manifest: list[dict[str, Any]] = []
    for path in sorted(files, key=lambda item: _relative_posix(root, item)):
        manifest.append(
            {
                "path": _relative_posix(root, path),
                "sha256": util.sha256_file(path),
                "bytes": path.stat().st_size,
            }
        )
    return manifest


def _manifest_digest(manifest: list[dict[str, Any]]) -> str:
    """Stable digest of a directory manifest (the dataset's aggregate checksum)."""
    return util.sha256_text(util.canonical_json(manifest))


# ---------------------------------------------------------------------------
# format verification
# ---------------------------------------------------------------------------


def _verify_format(path: Path, kind: str) -> None:
    """Sanity-check that the payload matches the declared dataset kind.

    Raises:
        DataError: the file is not a plausible instance of the declared kind
            (``DATASET_FORMAT_INVALID``).
    """
    if kind in DIR_KINDS:
        try:
            has_files = any(item.is_file() for item in path.rglob("*"))
        except OSError as err:
            raise DataError(
                f"dataset directory {path} could not be listed: {err}",
                code="DATASET_FORMAT_INVALID",
                details={"path": str(path)},
            ) from err
        if not has_files:
            raise DataError(
                f"dataset directory {path} contains no files",
                code="DATASET_FORMAT_INVALID",
                details={"path": str(path), "kind": kind},
            )
        return
    if not path.is_file():  # pragma: no cover - checked before
        raise DataError(
            f"dataset file {path} does not exist",
            code="DATASET_FORMAT_INVALID",
            details={"path": str(path)},
        )
    try:
        if kind in {"fasta", "msa"}:
            _verify_fasta_or_msa(path, kind)
        elif kind in {"tsv", "table"}:
            rows = util.read_tsv(path)
            if path.stat().st_size > 0 and not rows:
                raise DataError(
                    f"dataset table {path} has no data rows",
                    code="DATASET_FORMAT_INVALID",
                    details={"path": str(path), "kind": kind},
                )
        elif kind == "json":
            text = path.read_text(encoding="utf-8")
            if not text.strip():
                raise DataError(
                    f"dataset JSON file {path} is empty",
                    code="DATASET_FORMAT_INVALID",
                    details={"path": str(path)},
                )
            import json

            json.loads(text)
        elif kind == "structure":
            _verify_structure_format(path)
        elif kind == "hmm":
            _verify_hmm_format(path)
    except DataError:
        raise
    except Exception as err:  # noqa: BLE001 - any parser failure invalidates the dataset
        raise DataError(
            f"dataset {path} does not look like a valid {kind!r} file: {type(err).__name__}: {err}",
            code="DATASET_FORMAT_INVALID",
            details={"path": str(path), "kind": kind, "error": f"{type(err).__name__}: {err}"},
        ) from err


def _verify_fasta_or_msa(path: Path, kind: str) -> None:
    """Validate a FASTA (or MSA text) dataset without loading huge files twice."""
    if kind == "fasta":
        records = util.read_fasta(path)
        if not records:
            raise DataError(
                f"dataset FASTA {path} contains no records",
                code="DATASET_FORMAT_INVALID",
                details={"path": str(path)},
            )
        return
    try:
        util.read_fasta(path)
        return
    except DataError:
        pass
    # MSA may be Stockholm/Clustal; accept those recognised formats.
    text = util.read_maybe_gzip(path).decode("utf-8", errors="replace")
    head = text.lstrip()[:200]
    if head.startswith("# STOCKHOLM") or head.upper().startswith("CLUSTAL"):
        return
    raise DataError(
        f"dataset alignment {path} is not FASTA, Stockholm or Clustal",
        code="DATASET_FORMAT_INVALID",
        details={"path": str(path), "preview": util.truncate(head, 120)},
    )


def _verify_structure_format(path: Path) -> None:
    """Validate a structure file with gemmi (lazy import; gemmi is a hard dep)."""
    import gemmi

    try:
        structure = gemmi.read_structure(str(path))
    except Exception as err:  # noqa: BLE001 - gemmi raises RuntimeError on bad input
        raise DataError(
            f"dataset structure {path} could not be parsed by gemmi: {err}",
            code="DATASET_FORMAT_INVALID",
            details={"path": str(path), "error": str(err)},
        ) from err
    if len(structure) == 0:
        raise DataError(
            f"dataset structure {path} contains no models",
            code="DATASET_FORMAT_INVALID",
            details={"path": str(path)},
        )


def _verify_hmm_format(path: Path) -> None:
    """Validate a HMMER3 model file (accept gzip-compressed models too)."""
    raw = path.read_bytes()
    if util.is_gzip_bytes(raw):
        raw = util.gzip_decompress(raw)
    if not raw.lstrip().startswith(b"HMMER3/"):
        raise DataError(
            f"dataset HMM {path} is not a HMMER3 model",
            code="DATASET_FORMAT_INVALID",
            details={"path": str(path), "preview": util.truncate(raw.decode("utf-8", errors="replace"), 120)},
        )


# ---------------------------------------------------------------------------
# records
# ---------------------------------------------------------------------------


def _record_for_file(name: str, path: Path, kind: str, description: str | None, verify: bool) -> dict[str, Any]:
    info = _hash_file(path)
    return {
        "name": name,
        "kind": kind,
        "path": str(path),
        "sha256": info["sha256"],
        "bytes": info["bytes"],
        "description": description,
        "registered_at": util.now_iso(),
        "read_only": True,
        "format_verified": bool(verify),
        "n_files": 1,
        "files": [],
    }


def _record_for_dir(name: str, path: Path, kind: str, description: str | None, verify: bool) -> dict[str, Any]:
    manifest = _manifest_for_dir(path)
    if not manifest:
        raise DataError(
            f"dataset directory {path} contains no regular files",
            code="DATASET_FORMAT_INVALID",
            details={"path": str(path), "kind": kind},
        )
    return {
        "name": name,
        "kind": kind,
        "path": str(path),
        "sha256": _manifest_digest(manifest),
        "bytes": sum(item["bytes"] for item in manifest),
        "description": description,
        "registered_at": util.now_iso(),
        "read_only": True,
        "format_verified": bool(verify),
        "n_files": len(manifest),
        "files": manifest,
    }


# ---------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------


def register_dataset(
    ctx: Any,
    *,
    name: str,
    path: str | Path,
    kind: str,
    description: str | None = None,
    verify: bool = True,
    force: bool = False,
) -> dict[str, Any]:
    """Register a local file or directory as a read-only, checksum-pinned dataset.

    Args:
        ctx: command context (``ctx.datasets_file`` selects the registry).
        name: registry name (1-80 chars: letters, digits, ``.``, ``_``, ``-``).
        path: existing file/directory (relative paths resolve against ``ctx.cwd``).
        kind: one of :data:`KINDS` (``fasta``, ``tsv``, ``json``, ``msa``,
            ``structure``, ``structure_dir``, ``processed_dir``, ``table``,
            ``hmm``, ``hmm_dir``, ``generic_dir``).
        description: free-text origin/version note.
        verify: also validate the payload format (recommended, default True).
        force: replace an existing registration with the same name.

    Returns:
        The stored record: ``{"name", "kind", "path", "sha256", "bytes",
        "description", "registered_at", "read_only": True, "format_verified",
        "n_files", "files"}`` (``files`` is the per-file manifest for directory
        kinds, empty for file kinds).  In ``--dry-run`` the record is returned with
        ``"registered": False, "dry_run": True`` and nothing is written.

    Raises:
        InvalidInputError: invalid name/kind or file/dir kind mismatch.
        NotFoundError: the path does not exist (``DATASET_PATH_MISSING``).
        DataError: the payload is not a plausible instance of the declared kind.
    """
    name = _require_name(name)
    kind_text = str(kind or "").strip().lower()
    if kind_text not in KINDS:
        raise InvalidInputError(
            f"unsupported dataset kind {kind!r}",
            details={"kind": kind, "known": sorted(KINDS)},
        )
    resolved = _resolve_path(ctx, path)
    if not resolved.exists():
        raise NotFoundError(
            f"dataset path does not exist: {resolved}",
            code="DATASET_PATH_MISSING",
            details={"name": name, "path": str(resolved)},
            fallbacks=[
                "Check the path (relative paths resolve against ctx.cwd).",
                "Copy the reference data into the workspace before registering it.",
            ],
        )
    if kind_text in FILE_KINDS and not resolved.is_file():
        raise InvalidInputError(
            f"dataset kind {kind_text!r} requires a file but {resolved} is not a file",
            code="DATASET_KIND_MISMATCH",
            details={"name": name, "path": str(resolved), "kind": kind_text},
        )
    if kind_text in DIR_KINDS and not resolved.is_dir():
        raise InvalidInputError(
            f"dataset kind {kind_text!r} requires a directory but {resolved} is not a directory",
            code="DATASET_KIND_MISMATCH",
            details={"name": name, "path": str(resolved), "kind": kind_text},
        )
    if verify:
        _verify_format(resolved, kind_text)
    if kind_text in FILE_KINDS:
        record = _record_for_file(name, resolved, kind_text, description, verify)
    else:
        record = _record_for_dir(name, resolved, kind_text, description, verify)

    registry = _load_registry(ctx)
    if name in registry["datasets"] and not force:
        raise InvalidInputError(
            f"a dataset named {name!r} is already registered; remove it first or pass force=True",
            code="DATASET_ALREADY_REGISTERED",
            details={"name": name, "path": str(resolved), "registry": str(_registry_path(ctx))},
            fallbacks=[
                f"Run lifesci.datasets action='remove' name='{name}' first.",
                "Register under a different name to keep both versions.",
            ],
        )
    if bool(getattr(ctx, "dry_run", False)):
        record["registered"] = False
        record["dry_run"] = True
        record["would_register"] = True
        return record
    with _registry_lock(ctx):
        registry = _load_registry(ctx)
        registry["datasets"][name] = record
        registry_path = _save_registry(ctx, registry)
    _common.log_local_file(
        ctx, registry_path, kind="dataset_registry", notes={"action": "register", "dataset": name}
    )
    record["registered"] = True
    return record


def list_datasets(ctx: Any) -> list[dict[str, Any]]:
    """Return every registered dataset record, sorted by name (read-only view)."""
    registry = _load_registry(ctx)
    records = [dict(record) for record in registry["datasets"].values() if isinstance(record, Mapping)]
    records.sort(key=lambda item: str(item.get("name", "")))
    return records


def get_dataset(ctx: Any, name: str) -> dict[str, Any]:
    """Return one registered dataset record.

    Raises:
        NotFoundError: no dataset with that name (``DATASET_NOT_FOUND``).
    """
    name = str(name or "").strip()
    registry = _load_registry(ctx)
    record = registry["datasets"].get(name)
    if not isinstance(record, Mapping):
        known = sorted(str(key) for key in registry["datasets"])
        raise NotFoundError(
            f"no registered dataset named {name!r}",
            code="DATASET_NOT_FOUND",
            details={"name": name, "registry": str(_registry_path(ctx)), "known": known},
            fallbacks=[
                "List registered datasets with lifesci.datasets action='list'.",
                "Register the path first with action='register' (it must exist).",
            ],
        )
    output = dict(record)
    output.setdefault("read_only", True)
    output["path_exists"] = Path(str(output.get("path", ""))).exists()
    return output


def _verify_record(record: Mapping[str, Any]) -> dict[str, Any]:
    """Re-hash one record (file or directory) and build a verification report."""
    name = str(record.get("name") or "")
    kind = str(record.get("kind") or "")
    root = Path(str(record.get("path") or ""))
    if not root.exists():
        raise NotFoundError(
            f"dataset {name!r} path is missing: {root}",
            code="DATASET_PATH_MISSING",
            details={"name": name, "path": str(root)},
            fallbacks=["Re-register the dataset at its new location, or remove the stale record."],
        )
    entries: list[dict[str, Any]] = []
    if kind in FILE_KINDS:
        actual = util.sha256_file(root) if root.is_file() else None
        expected = str(record.get("sha256") or "")
        ok = actual is not None and actual == expected
        entries.append(
            {
                "path": str(root),
                "expected_sha256": expected,
                "actual_sha256": actual,
                "bytes": root.stat().st_size if root.is_file() else None,
                "ok": ok,
            }
        )
    else:
        expected_manifest = {
            str(item.get("path")): item
            for item in _common.as_list(record.get("files"))
            if isinstance(item, Mapping)
        }
        actual_manifest = {
            item["path"]: item for item in _manifest_for_dir(root)
        } if root.is_dir() else {}
        for rel in sorted(expected_manifest):
            expected = expected_manifest[rel]
            actual = actual_manifest.get(rel)
            if actual is None:
                entries.append(
                    {
                        "path": rel,
                        "expected_sha256": expected.get("sha256"),
                        "actual_sha256": None,
                        "bytes": None,
                        "missing": True,
                        "ok": False,
                    }
                )
            else:
                ok = actual.get("sha256") == expected.get("sha256")
                entries.append(
                    {
                        "path": rel,
                        "expected_sha256": expected.get("sha256"),
                        "actual_sha256": actual.get("sha256"),
                        "bytes": actual.get("bytes"),
                        "ok": ok,
                    }
                )
        for rel in sorted(set(actual_manifest) - set(expected_manifest)):
            actual = actual_manifest[rel]
            entries.append(
                {
                    "path": rel,
                    "expected_sha256": None,
                    "actual_sha256": actual.get("sha256"),
                    "bytes": actual.get("bytes"),
                    "extra": True,
                    "ok": False,
                }
            )
    n_mismatched = sum(1 for entry in entries if not entry["ok"])
    n_missing = sum(1 for entry in entries if entry.get("missing"))
    n_extra = sum(1 for entry in entries if entry.get("extra"))
    actual_digest: str | None
    if kind in FILE_KINDS:
        actual_digest = entries[0]["actual_sha256"] if entries else None
    else:
        actual_digest = _manifest_digest(
            [
                {"path": entry["path"], "sha256": entry["actual_sha256"], "bytes": entry["bytes"]}
                for entry in entries
                if not entry.get("missing")
            ]
        )
    return {
        "ok": n_mismatched == 0,
        "name": name,
        "kind": kind,
        "path": str(root),
        "sha256_recorded": record.get("sha256"),
        "sha256_actual": actual_digest,
        "bytes": sum(int(entry.get("bytes") or 0) for entry in entries),
        "n_files": len(entries),
        "n_mismatched": n_mismatched,
        "n_missing": n_missing,
        "n_extra": n_extra,
        "entries": entries,
    }


def verify_dataset(ctx: Any, name: str) -> dict[str, Any]:
    """Re-hash one registered dataset and report every mismatch.

    Args:
        ctx: command context.
        name: registered dataset name.

    Returns:
        ``{"ok", "name", "kind", "path", "sha256_recorded", "sha256_actual",
        "bytes", "n_files", "n_mismatched", "n_missing", "n_extra", "entries"}``.
        ``ok`` is only true when every recorded file matches and no file has been
        added, removed or modified.

    Raises:
        NotFoundError: unknown dataset name or its path has gone missing.
    """
    record = get_dataset(ctx, name)
    return _verify_record(record)


def resolve_dataset_file(
    ctx: Any,
    name: str,
    filename: str | None = None,
) -> Path:
    """Resolve (and checksum-verify) one file of a registered dataset for reading.

    This is the read path every consumer must use: it refuses paths outside a
    directory dataset, refuses missing files and raises
    ``DATASET_CHECKSUM_MISMATCH`` when the bytes changed after registration, so a
    scientific result can never silently use tampered reference data.

    Args:
        ctx: command context.
        name: registered dataset name.
        filename: for directory datasets, the relative path of one file inside it;
            for file datasets it may be omitted (or must match the file name).

    Returns:
        The verified absolute :class:`pathlib.Path` (read provenance is logged).

    Raises:
        NotFoundError: unknown dataset, missing dataset path or missing file.
        InvalidInputError: filename required / escapes the dataset / wrong dataset.
        DataError: the file no longer matches the recorded SHA-256.
    """
    record = get_dataset(ctx, name)
    kind = str(record.get("kind") or "")
    root = Path(str(record.get("path") or ""))
    if not root.exists():
        raise NotFoundError(
            f"dataset {name!r} path is missing: {root}",
            code="DATASET_PATH_MISSING",
            details={"name": name, "path": str(root)},
            fallbacks=["Re-register the dataset at its new location, or remove the stale record."],
        )
    if kind in DIR_KINDS:
        if not filename:
            raise InvalidInputError(
                f"dataset {name!r} is a directory dataset; pass filename=<relative path>",
                code="DATASET_FILENAME_REQUIRED",
                details={"name": name, "kind": kind, "path": str(root)},
            )
        target = _safe_child(root, filename)
        if not target.is_file():
            manifest_paths = sorted(
                str(item.get("path")) for item in _common.as_list(record.get("files"))
            )
            raise NotFoundError(
                f"file {filename!r} is not part of dataset {name!r}",
                code="DATASET_FILE_NOT_FOUND",
                details={"name": name, "filename": filename, "known_files": manifest_paths[:20]},
                fallbacks=[
                    "List the dataset files from the registry manifest.",
                    "Re-register the directory if its contents changed.",
                ],
            )
        expected = {
            str(item.get("path")): str(item.get("sha256") or "")
            for item in _common.as_list(record.get("files"))
            if isinstance(item, Mapping)
        }.get(_relative_posix(root, target))
        actual = util.sha256_file(target)
        if expected and actual != expected:
            raise DataError(
                f"dataset {name!r} file {filename!r} no longer matches its recorded SHA-256 "
                f"(expected {expected}, got {actual}); refusing to read modified reference data",
                code="DATASET_CHECKSUM_MISMATCH",
                details={
                    "name": name,
                    "filename": filename,
                    "path": str(target),
                    "expected_sha256": expected,
                    "actual_sha256": actual,
                },
                fallbacks=[
                    "Restore the file from its original source, then run action='verify'.",
                    "Re-register the dataset if the change is intentional and documented.",
                ],
            )
    else:
        target = root
        if not target.is_file():
            raise NotFoundError(
                f"dataset {name!r} resolves to a missing file: {target}",
                code="DATASET_PATH_MISSING",
                details={"name": name, "path": str(target)},
            )
        if filename and str(filename) not in {target.name, str(target)}:
            raise InvalidInputError(
                f"dataset {name!r} stores a single file ({target.name}); filename={filename!r} "
                "does not match it",
                code="DATASET_FILENAME_NOT_APPLICABLE",
                details={"name": name, "filename": filename, "path": str(target)},
            )
        expected = str(record.get("sha256") or "")
        actual = util.sha256_file(target)
        if expected and actual != expected:
            raise DataError(
                f"dataset {name!r} no longer matches its recorded SHA-256 "
                f"(expected {expected}, got {actual}); refusing to read modified reference data",
                code="DATASET_CHECKSUM_MISMATCH",
                details={
                    "name": name,
                    "path": str(target),
                    "expected_sha256": expected,
                    "actual_sha256": actual,
                },
                fallbacks=[
                    "Restore the file from its original source, then run action='verify'.",
                    "Re-register the dataset if the change is intentional and documented.",
                ],
            )
    _common.log_local_file(ctx, target, dataset=name, kind="dataset_read", notes={"kind": kind})
    return target


__all__ = [
    "DIR_KINDS",
    "FILE_KINDS",
    "KINDS",
    "NAME_RE",
    "REGISTRY_VERSION",
    "get_dataset",
    "list_datasets",
    "register_dataset",
    "resolve_dataset_file",
    "verify_dataset",
]
