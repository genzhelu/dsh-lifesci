"""``lifesci.remote`` — opt-in SSH execution on configured lab compute hosts.

Heavy jobs (ColabFold/AlphaFold prediction, HH-suite/HMMER/MMseqs2 searches) can
be offloaded to lab machines over SSH, but remote execution is
**off by default** and never required for offline work:

* hosts are configured only through ``LIFESCI_REMOTE_HOSTS`` /
  ``LIFESCI_REMOTE_CONFIG`` (read by :mod:`lifesci.adapters.remote`); the plugin
  ships no hostnames and never stores credentials;
* ``list`` reports the configured aliases and connection metadata **without
  secrets** (key paths are never returned);
* ``plan`` and ``--dry-run`` only build and return the ``ssh`` plan;
* ``run`` requires ``allow: true`` *and* a configured host, and records a
  provenance entry with host alias, argv, start/end time, exit code and the
  remote tool version.

Use absolute tool paths in ``argv`` (there is usually no usable ``$PATH`` on
the remote machines); results are written back through stdout or a job script.
"""

from __future__ import annotations

import inspect
from pathlib import Path
from typing import Any

from .. import provenance as provenance_mod
from .. import util
from ..commands.base import command
from ..errors import ConfigError, InvalidInputError, RemoteDisabledError, UsageError

INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "action": {
            "type": "string",
            "enum": ["list", "plan", "run"],
            "description": (
                "'list' configured host aliases (never leaks credentials); 'plan' build the "
                "ssh job plan without executing it; 'run' execute the job over ssh (requires "
                "allow:true, and remote execution must be configured)."
            ),
        },
        "host": {
            "type": "string",
            "description": "Configured host alias (e.g. 'gpu01'), required for plan/run.",
        },
        "argv": {
            "type": "array",
            "items": {"type": "string"},
            "minItems": 1,
            "description": (
                "Remote command argv (no shell string), e.g. ['/opt/hhsuite/bin/hhsearch', "
                "'-i', 'query.a3m', '-d', '/data/hhsuite/db']. Prefer absolute tool paths "
                "because remote PATHs are minimal."
            ),
        },
        "cwd": {
            "type": "string",
            "description": "Remote working directory for the job (optional; host work_dir otherwise).",
        },
        "timeout": {
            "type": "number",
            "minimum": 1,
            "maximum": 86400,
            "default": 600,
            "description": "Maximum wall-clock seconds for the remote job (default 600).",
        },
        "allow": {
            "type": "boolean",
            "default": False,
            "description": (
                "Explicit opt-in required for action='run'. Without it, run refuses; use "
                "action='plan' or --dry-run to inspect the job first."
            ),
        },
        "out": {
            "type": "string",
            "description": "Optional path to write the full job result JSON (stdout/stderr are truncated in data).",
        },
    },
    "required": ["action"],
}

_HOST_FIELDS = ("host", "user", "port", "work_dir", "n_allowed_commands", "tool_paths", "key_configured")
_MAX_STREAM_CHARS = 20000


def _adapter() -> Any:
    """Import the opt-in remote adapter lazily."""
    import importlib

    try:
        return importlib.import_module("lifesci.adapters.remote")
    except ImportError as err:  # pragma: no cover - parallel-workstream guard
        raise ConfigError(
            "the lifesci remote adapter is not available in this checkout; SSH execution "
            "requires python/lifesci/adapters/remote.py",
            details={"import_error": f"{type(err).__name__}: {err}"},
        ) from err


def _sanitised_hosts(hosts: Any) -> list[dict[str, Any]]:
    """Return credential-free host summaries sorted by alias."""
    if isinstance(hosts, dict):
        items = list(hosts.items())
    else:
        items = [(str(entry.get("alias", entry.get("name", ""))), entry) for entry in (hosts or [])]
    out: list[dict[str, Any]] = []
    for alias, config in items:
        if not isinstance(config, dict):
            continue
        config = dict(config or {})
        record: dict[str, Any] = {"alias": str(alias)}
        for field in _HOST_FIELDS:
            if field in config:
                value = config[field]
                if field == "tool_paths" and isinstance(value, dict):
                    # Report available tool keys only; paths are not secrets but are verbose.
                    record[field] = sorted(str(key) for key in value)
                elif isinstance(value, dict):
                    continue
                else:
                    record[field] = util.json_safe(value)
        out.append(record)
    out.sort(key=lambda item: item["alias"])
    return out


def _call_run(adapter: Any, ctx: Any, **kwargs: Any) -> dict[str, Any]:
    """Call adapter.run with ``allow=True`` only when the adapter advertises it."""
    try:
        parameters = inspect.signature(adapter.run).parameters
    except (TypeError, ValueError):  # pragma: no cover - exotic callables
        parameters = {}
    if "allow" in parameters:
        kwargs["allow"] = True
    return adapter.run(ctx, **kwargs)


def _record_provenance(ctx: Any, job: dict[str, Any], *, timeout: float) -> None:
    """Append the remote tool-run provenance entry to the envelope."""
    log = getattr(ctx, "_provenance_log", None)
    if log is None:
        log = provenance_mod.ProvenanceLog(ctx.log_path)
        ctx._provenance_log = log
    started = job.get("started_at")
    finished = job.get("finished_at")
    elapsed_ms = None
    try:
        if started and finished:
            from datetime import datetime

            start_dt = datetime.fromisoformat(str(started).replace("Z", "+00:00"))
            end_dt = datetime.fromisoformat(str(finished).replace("Z", "+00:00"))
            elapsed_ms = (end_dt - start_dt).total_seconds() * 1000.0
    except (TypeError, ValueError):  # pragma: no cover - malformed adapter timestamps
        elapsed_ms = None
    entry = log.tool(
        name="ssh",
        version=job.get("tool_version"),
        argv=job.get("argv"),
        exit_code=job.get("exit_code"),
        elapsed_ms=elapsed_ms,
        notes={
            "host": job.get("host"),
            "cwd": job.get("cwd"),
            "started_at": started,
            "finished_at": finished,
            "timeout_s": timeout,
        },
    )
    ctx.provenance.append(entry)


@command(
    name="remote",
    title="Opt-in remote execution over SSH",
    summary="List configured SSH hosts, plan a remote job, or run it with explicit opt-in.",
    description=(
        "Offload heavy compute to configured lab machines over SSH. Remote execution is OFF "
        "unless LIFESCI_REMOTE_HOSTS / LIFESCI_REMOTE_CONFIG are configured, and is never "
        "required for offline work. Actions: 'list' returns host aliases and connection "
        "metadata with no secrets (key paths never appear); 'plan' returns the ssh plan "
        "without executing; 'run' executes the job and records provenance (host, argv, "
        "start/end, exit code, remote tool version). run requires allow:true AND host+argv; "
        "use absolute tool paths because remote PATHs are minimal. stdout/stderr in "
        "the payload are truncated (use out to save the full job result). The plugin never "
        "writes credentials and never auto-executes downloaded code."
    ),
    input_schema=INPUT_SCHEMA,
    output_schema="remote.schema.json",
    category="foundation",
    timeout_ms=90_000,
    examples=(
        {"action": "list"},
        {"action": "plan", "host": "gpu01", "argv": ["/opt/hhsuite/bin/hhsearch", "-h"]},
    ),
    danger="network",
)
def run(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
    ctx.validate()
    action = str(args.get("action") or "")
    if action not in {"list", "plan", "run"}:
        raise InvalidInputError(
            f"unknown remote action {action!r}",
            details={"action": action, "known": ["list", "plan", "run"]},
        )
    adapter = _adapter()
    hosts = adapter.configured_hosts(ctx)
    configured = _sanitised_hosts(hosts)
    if not configured:
        raise RemoteDisabledError(
            "remote execution is not configured; set LIFESCI_REMOTE_HOSTS (JSON) or "
            "LIFESCI_REMOTE_CONFIG (path) to enable it. Remote execution is opt-in and the "
            "plugin ships no default hosts.",
            details={"action": action, "env_vars": ["LIFESCI_REMOTE_HOSTS", "LIFESCI_REMOTE_CONFIG"]},
        )

    payload: dict[str, Any] = {"action": action, "n_hosts": len(configured)}

    if action == "list":
        payload["hosts"] = configured
        return payload

    host = args.get("host")
    argv = args.get("argv")
    if not host:
        raise InvalidInputError(f"remote {action} requires a host alias", details={"known_hosts": [h["alias"] for h in configured]})
    if host not in {item["alias"] for item in configured}:
        raise InvalidInputError(
            f"unknown remote host {host!r}",
            details={"host": host, "known_hosts": [item["alias"] for item in configured]},
        )
    if not argv or not isinstance(argv, list) or not all(isinstance(part, str) and part for part in argv):
        raise InvalidInputError(
            "remote plan/run requires argv: a non-empty array of strings",
            details={"host": host},
        )
    timeout = float(args.get("timeout", 600))
    cwd = args.get("cwd")

    if action == "plan" or bool(ctx.dry_run):
        plan = adapter.run(
            ctx,
            host=str(host),
            argv=[str(part) for part in argv],
            cwd=cwd,
            timeout=timeout,
            dry_run=True,
        )
        payload.update({"host": str(host), "plan": util.json_safe(plan), "dry_run": True})
        ctx.warn(
            "REMOTE_DRY_RUN",
            "Remote job was planned but not executed; pass action='run', allow=true and drop "
            "--dry-run to run it.",
            severity="info",
            context={"host": host, "argv": argv},
        )
        return payload

    if not args.get("allow", False):
        raise UsageError(
            "remote run requires allow:true in the input; nothing was executed. Plan the job "
            "first with action='plan' (or --dry-run), review argv/host, then re-run with allow:true.",
            details={"host": host, "argv": argv, "requires": "allow:true"},
        )

    job = _call_run(
        adapter,
        ctx,
        host=str(host),
        argv=[str(part) for part in argv],
        cwd=cwd,
        timeout=timeout,
        dry_run=False,
    )
    job = util.json_safe(job) or {}
    _record_provenance(ctx, job, timeout=timeout)
    truncated = False
    for stream in ("stdout", "stderr"):
        value = job.get(stream)
        if isinstance(value, str) and len(value) > _MAX_STREAM_CHARS:
            job[stream] = util.truncate(value, _MAX_STREAM_CHARS)
            truncated = True
    if truncated:
        ctx.warn(
            "REMOTE_OUTPUT_TRUNCATED",
            f"Remote stdout/stderr were truncated to {_MAX_STREAM_CHARS} characters in the "
            "payload; write the full result with the 'out' input parameter.",
            context={"host": host, "max_chars": _MAX_STREAM_CHARS},
        )
    if job.get("exit_code") not in (0, None):
        ctx.warn(
            "REMOTE_JOB_FAILED",
            f"Remote job exited with code {job.get('exit_code')} on host {host!r}; see stderr.",
            context={"host": host, "exit_code": job.get("exit_code")},
        )
    payload.update({"host": str(host), "job": job, "dry_run": False})
    out_path = args.get("out")
    if out_path and not bool(ctx.dry_run):
        target = Path(out_path).expanduser()
        if not target.is_absolute():
            target = (Path(ctx.cwd) / target).resolve()
        util.atomic_write_json(target, job)
        payload["out"] = str(target)
    return payload
