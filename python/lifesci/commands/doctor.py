"""``lifesci.doctor`` — environment, dependency, cache, and portal health checks.

Reports versions of every science dependency, the Foldseek/PyMOL/SSH tool
availability, cache writability and contents, registered local datasets, and
(unless ``--offline``) reachability of each public portal.  Doctor never fails
the command because a portal is down: it reports ``status: degraded`` with
machine-readable per-check results so the agent can decide what is usable.
"""

from __future__ import annotations

import importlib
import importlib.metadata
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from .. import __version__, http, paths, provenance, util
from ..commands.base import command

#: (import name, pip/distribution name, required)
PACKAGES: tuple[tuple[str, str, bool], ...] = (
    ("pyhmmer", "pyhmmer", True),
    ("Bio", "biopython", True),
    ("gemmi", "gemmi", True),
    ("numpy", "numpy", True),
    ("scipy", "scipy", True),
    ("matplotlib", "matplotlib", True),
    ("jsonschema", "jsonschema", True),
    ("pytest", "pytest", False),
)

#: Lightweight reachability probes (tiny payloads; not cached).
PORTAL_PROBES: tuple[tuple[str, str], ...] = (
    ("uniprot_rest", "https://rest.uniprot.org/uniprotkb/P69905.fasta"),
    (
        "uniprot_ftp",
        "https://ftp.uniprot.org/pub/databases/uniprot/current_release/relnotes.txt",
    ),
    ("interpro", "https://www.ebi.ac.uk/interpro/api/entry/pfam/PF00042/"),
    (
        "europe_pmc",
        "https://www.ebi.ac.uk/europepmc/webservices/rest/search?query=globin&format=json&pageSize=1",
    ),
    (
        "ncbi_eutils",
        "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi?db=pubmed&term=globin&retmode=json&retmax=1",
    ),
    ("alphafold_db", "https://alphafold.ebi.ac.uk/api/prediction/P69905"),
    ("rcsb_data", "https://data.rcsb.org/rest/v1/core/entry/1UBQ"),
    ("esm_atlas", "https://api.esmatlas.com/foldSequence/v1/pdb/"),
    ("foldseek_web", "https://search.foldseek.com/api/ticket"),
)

INPUT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "network": {
            "type": "boolean",
            "default": True,
            "description": "Probe public portals (automatically skipped in offline mode).",
        },
        "probe_timeout_s": {
            "type": "number",
            "minimum": 1,
            "maximum": 30,
            "default": 10,
            "description": "Per-portal reachability timeout.",
        },
        "portals": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Optional subset of portal probe names to check.",
        },
        "foldseek_path": {"type": "string", "description": "Explicit Foldseek binary path."},
        "pymol_path": {"type": "string", "description": "Explicit PyMOL executable path."},
    },
}


def _package_version(import_name: str, dist_name: str) -> tuple[bool, str | None, str | None]:
    try:
        module = importlib.import_module(import_name)
    except Exception as err:  # noqa: BLE001 - report any import failure
        return False, None, f"{type(err).__name__}: {err}"
    # Prefer distribution metadata: ``module.__version__`` is deprecated for some
    # packages (jsonschema) and absent for others (Bio uses a string, not module).
    version: str | None = None
    try:
        version = importlib.metadata.version(dist_name)
    except importlib.metadata.PackageNotFoundError:
        raw_version = getattr(module, "__version__", None)
        if raw_version is not None and not isinstance(raw_version, str):
            raw_version = getattr(raw_version, "strip", lambda: None)()
        version = str(raw_version) if raw_version else None
    return True, str(version) if version else None, None


def _tool_version(argv: list[str], timeout: float = 10.0) -> tuple[bool, str | None, str | None]:
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (FileNotFoundError, PermissionError):
        return False, None, "not found"
    except subprocess.TimeoutExpired:
        return False, None, f"timed out after {timeout}s"
    output = (proc.stdout or "").strip() or (proc.stderr or "").strip()
    if proc.returncode != 0 and not output:
        return False, None, f"exit code {proc.returncode}"
    first_line = output.splitlines()[0] if output else None
    return proc.returncode == 0, first_line, None if proc.returncode == 0 else f"exit code {proc.returncode}"


def _resolve_tool(name: str, explicit: str | None, env_var: str) -> str | None:
    if explicit:
        return explicit
    env_value = os.environ.get(env_var)
    if env_value:
        return env_value
    candidates = [paths.TOOLS_DIR / name]
    for suffix in ("", ".exe"):
        candidates.append(paths.TOOLS_DIR / (name + suffix))
    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return shutil.which(name)


def _cache_check(ctx: Any) -> dict[str, Any]:
    from ..cache import Cache

    probe_path = ctx.cache_dir / ".doctor-probe"
    writable = True
    writable_error: str | None = None
    try:
        probe_path.parent.mkdir(parents=True, exist_ok=True)
        probe_path.write_text(util.now_iso(), encoding="utf-8")
        probe_path.unlink()
    except OSError as err:
        writable = False
        writable_error = str(err)
    report: dict[str, Any] = {
        "root": str(ctx.cache_dir),
        "exists": ctx.cache_dir.exists(),
        "writable": writable,
        "writable_error": writable_error,
        "n_entries": 0,
        "n_expired": 0,
        "bytes": 0,
        "databases": {},
        "missing_bodies": 0,
    }
    try:
        stats = Cache(ctx.cache_dir, ttl_default=ctx.cache_ttl).stats()
    except Exception as err:  # noqa: BLE001 - doctor must report, never crash
        report["error"] = f"{type(err).__name__}: {err}"
        report["writable"] = False
        return report
    report.update(
        {
            "n_entries": stats["n_entries"],
            "n_expired": stats["n_expired"],
            "bytes": stats["bytes"],
            "databases": stats["databases"],
            "missing_bodies": stats["missing_bodies"],
        }
    )
    return report


def _datasets_check(ctx: Any) -> dict[str, Any]:
    registry = util.read_json(ctx.datasets_file, {"datasets": {}}) if ctx.datasets_file else {}
    datasets = (registry or {}).get("datasets") or {}
    entries = []
    for name, record in sorted(datasets.items()):
        path = Path(str(record.get("path", "")))
        entries.append(
            {
                "name": name,
                "kind": record.get("kind"),
                "path": str(path),
                "exists": path.exists(),
                "sha256": record.get("sha256"),
            }
        )
    return {"file": str(ctx.datasets_file), "n_datasets": len(entries), "datasets": entries}


def _probe(ctx: Any, url: str, timeout: float) -> dict[str, Any]:
    try:
        response = http.request(
            ctx,
            "GET",
            url,
            cache=False,
            retries=1,
            timeout=timeout,
            allow_status=(200, 204, 301, 302, 303, 307, 308, 400, 401, 403, 404, 405, 406, 429),
        )
    except Exception as err:  # noqa: BLE001 - unreachable is a reportable outcome
        return {
            "reachable": False,
            "error": f"{type(err).__name__}: {getattr(err, 'message', err)}",
            "url": url,
        }
    return {
        "reachable": True,
        "http_status": response.status,
        "elapsed_ms": round(response.elapsed_ms, 1),
        "url": url,
    }


def _schema_check() -> dict[str, Any]:
    names = ["envelope.schema.json"]
    present: dict[str, bool] = {}
    missing: list[str] = []
    for name in names:
        try:
            present[name] = paths.schema_path(name).is_file()
        except FileNotFoundError:
            present[name] = False
            missing.append(name)
    return {"dirs": [str(p) for p in paths.SCHEMA_DIRS], "present": present, "missing": missing}


@command(
    name="doctor",
    title="Check lifesci environment",
    summary="Report dependency versions, cache health, tool availability, and portal reachability.",
    description=(
        "Environment preflight for the lifesci tool layer. Reports Python/package versions, "
        "Foldseek/PyMOL/SSH availability, cache writability and contents, registered read-only "
        "datasets, pinned database releases, and (unless offline) reachability of every public "
        "portal. Run this first when a lifesci call fails strangely. It never fails the command "
        "because a portal is down: read data.status ('ok'|'degraded'|'offline') and data.checks."
    ),
    input_schema=INPUT_SCHEMA,
    output_schema="doctor.schema.json",
    category="foundation",
    timeout_ms=120_000,
    examples=({"network": False},),
)
def run(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
    ctx.validate()
    checks: list[dict[str, Any]] = []

    def add(check: str, status: str, detail: Any) -> None:
        checks.append({"check": check, "status": status, "detail": util.json_safe(detail)})

    # -- runtime ------------------------------------------------------
    python_info = {
        "executable": sys.executable,
        "version": platform.python_version(),
        "implementation": platform.python_implementation(),
        "platform": platform.platform(),
        "ok": sys.version_info >= (3, 11),
    }
    add("python", "ok" if python_info["ok"] else "error", python_info)
    if not python_info["ok"]:
        ctx.warn("PYTHON_TOO_OLD", "lifesci requires Python >= 3.11.", context={"version": python_info["version"]})

    # -- packages -----------------------------------------------------
    packages: dict[str, Any] = {}
    for import_name, dist_name, required in PACKAGES:
        installed, version, error = _package_version(import_name, dist_name)
        packages[dist_name] = {
            "installed": installed,
            "version": version,
            "required": required,
            "error": error,
        }
        if required and not installed:
            add("package:" + dist_name, "error", {"error": error})
            ctx.warn(
                "DEPENDENCY_MISSING",
                f"Required Python package {dist_name} is not importable: {error}",
                context={"package": dist_name},
            )
        elif not installed:
            add("package:" + dist_name, "warn", {"error": error})
    if all(p["installed"] for p in packages.values() if p["required"]):
        add("packages", "ok", {"required": len([p for p in packages.values() if p["required"]])})

    # -- external tools ----------------------------------------------
    external: dict[str, Any] = {}
    foldseek_path = _resolve_tool("foldseek", args.get("foldseek_path"), "LIFESCI_FOLDSEEK")
    if foldseek_path:
        ok, version, error = _tool_version([foldseek_path, "version"])
        external["foldseek"] = {
            "available": ok,
            "path": foldseek_path,
            "version": version,
            "error": error,
        }
        if not ok:
            ctx.warn("FOLDSEEK_BROKEN", "Foldseek binary exists but did not run.", context={"path": foldseek_path})
            add("tool:foldseek", "error", external["foldseek"])
        else:
            add("tool:foldseek", "ok", external["foldseek"])
    else:
        external["foldseek"] = {"available": False, "path": None, "version": None, "error": "not found"}
        add("tool:foldseek", "warn", {"error": "not found; local Foldseek search unavailable"})
        ctx.warn(
            "FOLDSEEK_MISSING",
            "Foldseek is not installed; local structural search/superposition is unavailable. "
            "Install it with scripts/install_foldseek.sh (checksum-pinned) or set LIFESCI_FOLDSEEK.",
        )

    pymol_path = _resolve_tool("pymol", args.get("pymol_path"), "LIFESCI_PYMOL")
    if pymol_path:
        ok, version, error = _tool_version([pymol_path, "-cq", "-d", "print('pymol-probe')"])
        external["pymol"] = {"available": ok, "path": pymol_path, "version": version, "error": error}
        add("tool:pymol", "ok" if ok else "warn", external["pymol"])
        if not ok:
            ctx.warn("PYMOL_PROBE_FAILED", "PyMOL executable found but the probe failed.", context={"path": pymol_path})
    else:
        external["pymol"] = {"available": False, "path": None, "version": None, "error": "not found"}
        add("tool:pymol", "warn", {"error": "not found; PyMOL session generation will be skipped"})
        ctx.warn(
            "PYMOL_MISSING",
            "PyMOL is not installed; session generation falls back to emitting a .pml script "
            "without rendering (headless full-session PNG rendering is unreliable anyway).",
        )

    ssh_path = shutil.which("ssh")
    external["ssh"] = {"available": bool(ssh_path), "path": ssh_path}
    add("tool:ssh", "ok" if ssh_path else "warn", external["ssh"])

    # -- cache / datasets / schemas ----------------------------------
    cache_info = _cache_check(ctx)
    add("cache", "ok" if cache_info["writable"] else "error", cache_info)
    if not cache_info["writable"]:
        ctx.warn("CACHE_NOT_WRITABLE", "Cache directory is not writable.", context=cache_info)

    datasets_info = _datasets_check(ctx)
    add("datasets", "ok", datasets_info)
    for entry in datasets_info["datasets"]:
        if not entry["exists"]:
            ctx.warn(
                "DATASET_PATH_MISSING",
                f"Registered dataset {entry['name']!r} points to a missing path.",
                context=entry,
            )

    schema_info = _schema_check()
    add("schemas", "ok" if not schema_info["missing"] else "error", schema_info)
    if schema_info["missing"]:
        ctx.warn("SCHEMA_MISSING", "Some JSON schemas are missing.", context=schema_info)

    releases = provenance.load_releases(ctx.cache_dir)
    add("releases", "ok" if releases else "warn", releases)
    if not releases:
        ctx.warn(
            "NO_DATABASE_RELEASES_RECORDED",
            "No database releases have been recorded yet; run a fetch command to pin releases.",
        )

    # -- network ------------------------------------------------------
    network: dict[str, Any] = {}
    want_network = args.get("network", True) is not False
    if ctx.offline:
        add("network", "warn", {"skipped": "offline mode"})
        ctx.warn("NETWORK_CHECK_SKIPPED", "Network probes skipped because --offline was requested.")
    elif not want_network:
        add("network", "warn", {"skipped": "network=false"})
    else:
        probe_timeout = float(args.get("probe_timeout_s", 10))
        only = set(args.get("portals") or [])
        unreachable: list[str] = []
        for name, url in PORTAL_PROBES:
            if only and name not in only:
                continue
            result = _probe(ctx, url, probe_timeout)
            network[name] = result
            if not result["reachable"]:
                unreachable.append(name)
        if unreachable:
            add("network", "warn", {"unreachable": unreachable, "probes": network})
            ctx.warn(
                "PORTAL_UNREACHABLE",
                "Some portals are unreachable right now; commands depending on them will fail "
                "or fall back to cached payloads.",
                context={"unreachable": unreachable},
            )
        else:
            add("network", "ok", {"n_probes": len(network)})

    # -- overall status ----------------------------------------------
    statuses = {check["status"] for check in checks}
    if ctx.offline:
        status = "offline"
    elif "error" in statuses:
        status = "degraded"
    elif "warn" in statuses:
        status = "ok-with-warnings"
    else:
        status = "ok"

    return {
        "status": status,
        "lifesci_version": __version__,
        "python": python_info,
        "packages": packages,
        "external_tools": external,
        "cache": cache_info,
        "datasets": datasets_info,
        "schemas": schema_info,
        "releases": releases,
        "network": network,
        "checks": checks,
        "exit_meaning": {
            "0": "command executed",
            "2": "usage/input error",
            "6": "required tool missing",
        },
    }
