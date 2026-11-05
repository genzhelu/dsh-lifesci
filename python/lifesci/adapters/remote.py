"""Opt-in remote execution adapter.

Heavy jobs (ColabFold/AlphaFold prediction, HH-suite/HMMER/MMseqs2 searches) may
be offloaded to configured lab machines over SSH.  **Remote execution is OFF by
default**, never required for offline work, and configured exclusively through the
environment:

* ``LIFESCI_REMOTE_HOSTS`` — JSON, e.g.::

      {"hosts": {"gpu01": {"host": "gpu01.example.edu", "user": "someone",
                           "port": 22, "key_path": "~/.ssh/id_ed25519",
                           "work_dir": "/scratch/user/jobs",
                           "tool_paths": {"hhsuite": "/opt/hhsuite"},
                           "allowed_commands": [["/opt/hhsuite/bin/hhsearch"],
                                                ["/opt/hhsuite/bin/hmmsearch"]]}}}

* ``LIFESCI_REMOTE_CONFIG`` — path to a JSON file with the same shape.

Security rules enforced here:

* no hosts are hard-coded and no credentials are ever written to the repository;
* key **paths and contents are never logged or returned** — the ssh plan/result
  reports a redacted ``-i <key_path>`` placeholder and only the host *alias*;
* a real execution requires **both** an explicit opt-in (``allow=True`` or
  ``--dry-run`` plan mode) **and** an ``allowed_commands`` prefix allow-list on the
  host (deny-by-default); argv is passed as an argv list through ``shlex.quote``,
  never through a shell string supplied by the caller;
* ``BatchMode=yes`` avoids hanging on interactive password prompts;
* JSONL provenance records host alias, redacted argv, start/end times and exit
  code, so a remote job is auditable like any local tool run.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Mapping, Sequence

from .. import provenance, util
from ..errors import (
    ConfigError,
    InvalidInputError,
    LifesciError,
    RemoteDisabledError,
    RemoteError,
    ToolMissingError,
)

#: Environment variable holding the JSON host configuration.
ENV_HOSTS = "LIFESCI_REMOTE_HOSTS"

#: Environment variable holding a path to a JSON host-configuration file.
ENV_CONFIG = "LIFESCI_REMOTE_CONFIG"

#: SSH executable name (resolved through PATH; never a hard-coded absolute path).
SSH_BINARY = "ssh"

#: Host config keys accepted from the user (others are ignored, never echoed).
_HOST_KEYS = frozenset(
    {"host", "user", "port", "key_path", "work_dir", "tool_paths", "allowed_commands", "label"}
)

#: Maximum remote argv size accepted (defensive; argv is not a shell string).
MAX_ARGV_TOKENS = 512
MAX_ARGV_LENGTH = 65536


# ---------------------------------------------------------------------------
# configuration
# ---------------------------------------------------------------------------


def _env_value(ctx: Any, name: str) -> str | None:
    """Read ``name`` from the context environment snapshot, then ``os.environ``."""
    env = getattr(ctx, "env", None) or {}
    if name in env:
        return env.get(name)
    return os.environ.get(name)


def _parse_hosts_payload(payload: Any, *, source: str) -> dict[str, dict[str, Any]]:
    """Normalise any accepted config shape into ``{alias: host_config}``."""
    if not isinstance(payload, Mapping):
        raise ConfigError(
            f"remote host configuration from {source} must be a JSON object",
            code="REMOTE_CONFIG_INVALID",
            details={"source": source, "type": type(payload).__name__},
        )
    hosts_value = payload.get("hosts")
    if hosts_value is None:
        hosts_value = {key: value for key, value in payload.items() if key != "version"}
    if not isinstance(hosts_value, Mapping):
        raise ConfigError(
            f"remote host configuration from {source} must contain a 'hosts' object",
            code="REMOTE_CONFIG_INVALID",
            details={"source": source},
        )
    hosts: dict[str, dict[str, Any]] = {}
    for alias, config in hosts_value.items():
        alias_text = str(alias).strip()
        if not alias_text:
            continue
        if not isinstance(config, Mapping):
            raise ConfigError(
                f"remote host {alias_text!r} in {source} must be a JSON object",
                code="REMOTE_CONFIG_INVALID",
                details={"source": source, "alias": alias_text},
            )
        hosts[alias_text] = _validate_host(alias_text, config, source=source)
    return hosts


def _validate_host(alias: str, config: Mapping[str, Any], *, source: str) -> dict[str, Any]:
    """Validate one host configuration and strip any keys we do not support."""
    host = config.get("host")
    user = config.get("user")
    if not isinstance(host, str) or not host.strip():
        raise ConfigError(
            f"remote host {alias!r} in {source} needs a non-empty 'host'",
            code="REMOTE_CONFIG_INVALID",
            details={"source": source, "alias": alias},
        )
    if not isinstance(user, str) or not user.strip():
        raise ConfigError(
            f"remote host {alias!r} in {source} needs a non-empty 'user'",
            code="REMOTE_CONFIG_INVALID",
            details={"source": source, "alias": alias},
        )
    port_raw = config.get("port", 22)
    port = util.parse_int(port_raw)
    if port is None or not 1 <= port <= 65535:
        raise ConfigError(
            f"remote host {alias!r} has an invalid port {port_raw!r} (1-65535)",
            code="REMOTE_CONFIG_INVALID",
            details={"source": source, "alias": alias, "port": port_raw},
        )
    work_dir = config.get("work_dir")
    key_path = config.get("key_path")
    tool_paths = config.get("tool_paths")
    if tool_paths is not None and not isinstance(tool_paths, Mapping):
        raise ConfigError(
            f"remote host {alias!r} 'tool_paths' must be a JSON object",
            code="REMOTE_CONFIG_INVALID",
            details={"source": source, "alias": alias},
        )
    allowed = _normalise_allowed(config.get("allowed_commands"), alias=alias, source=source)
    return {
        "host": host.strip(),
        "user": user.strip(),
        "port": port,
        "key_path": str(key_path).strip() if isinstance(key_path, str) and key_path.strip() else None,
        "work_dir": str(work_dir).strip() if isinstance(work_dir, str) and work_dir.strip() else None,
        "tool_paths": {
            str(key): str(value) for key, value in (tool_paths or {}).items()
        }
        if isinstance(tool_paths, Mapping)
        else {},
        "allowed_commands": allowed,
    }


def _normalise_allowed(value: Any, *, alias: str, source: str) -> list[list[str]]:
    """Normalise the ``allowed_commands`` allow-list to argv prefixes."""
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        raise ConfigError(
            f"remote host {alias!r} 'allowed_commands' must be a list of argv prefixes",
            code="REMOTE_CONFIG_INVALID",
            details={"source": source, "alias": alias},
        )
    prefixes: list[list[str]] = []
    for entry in value:
        if isinstance(entry, str):
            parts = [entry]
        elif isinstance(entry, (list, tuple)) and all(isinstance(part, str) for part in entry):
            parts = [str(part) for part in entry]
        else:
            raise ConfigError(
                f"remote host {alias!r} has an invalid allowed_commands entry {entry!r}",
                code="REMOTE_CONFIG_INVALID",
                details={"source": source, "alias": alias},
            )
        parts = [part for part in parts if part]
        if parts:
            prefixes.append(parts)
    return prefixes


def _load_config(ctx: Any) -> tuple[dict[str, dict[str, Any]], str | None]:
    """Load the configured hosts (JSON env var and/or JSON config file)."""
    hosts: dict[str, dict[str, Any]] = {}
    sources: list[str] = []
    file_value = _env_value(ctx, ENV_CONFIG)
    if file_value:
        path = Path(str(file_value)).expanduser()
        if not path.is_file():
            raise ConfigError(
                f"{ENV_CONFIG} points to a missing file: {path}",
                code="REMOTE_CONFIG_INVALID",
                details={"env": ENV_CONFIG, "path": str(path)},
            )
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as err:
            raise ConfigError(
                f"remote config file {path} is not readable JSON: {err}",
                code="REMOTE_CONFIG_INVALID",
                details={"env": ENV_CONFIG, "path": str(path), "error": str(err)},
            ) from err
        hosts.update(_parse_hosts_payload(payload, source=str(path)))
        sources.append(ENV_CONFIG)
    raw_hosts = _env_value(ctx, ENV_HOSTS)
    if raw_hosts:
        try:
            payload = json.loads(raw_hosts)
        except json.JSONDecodeError as err:
            raise ConfigError(
                f"{ENV_HOSTS} is not valid JSON: {err}",
                code="REMOTE_CONFIG_INVALID",
                details={"env": ENV_HOSTS, "error": str(err)},
            ) from err
        hosts.update(_parse_hosts_payload(payload, source=f"${ENV_HOSTS}"))
        sources.append(ENV_HOSTS)
    source = "+".join(sources) if sources else None
    return hosts, source


def _public_host(alias: str, config: Mapping[str, Any]) -> dict[str, Any]:
    """Return the credential-free public view of one host configuration."""
    return {
        "host": config.get("host"),
        "user": config.get("user"),
        "port": config.get("port", 22),
        "work_dir": config.get("work_dir"),
        "tool_paths": dict(config.get("tool_paths") or {}),
        "n_allowed_commands": len(config.get("allowed_commands") or []),
        "key_configured": bool(config.get("key_path")),
    }


def configured_hosts(ctx: Any) -> dict[str, dict[str, Any]]:
    """Return the configured SSH hosts as ``{alias: public_config}`` (no secrets).

    ``key_path`` values are **never** returned (only ``key_configured: true/false``
    and the number of allowed command prefixes).  An empty mapping means remote
    execution is disabled, which is the default: no configuration environment
    variable is set in a clean environment.

    Raises:
        ConfigError: configuration is present but invalid (``REMOTE_CONFIG_INVALID``).
    """
    hosts, _source = _load_config(ctx)
    return {alias: _public_host(alias, config) for alias, config in sorted(hosts.items())}


def is_enabled(ctx: Any) -> bool:
    """True when at least one remote host is configured (remote execution is opt-in)."""
    return bool(configured_hosts(ctx))


# ---------------------------------------------------------------------------
# ssh plan / execution
# ---------------------------------------------------------------------------


def _command_allowed(argv: Sequence[str], allowed: Sequence[Sequence[str]]) -> bool:
    """True when ``argv`` starts with one of the ``allowed`` prefixes."""
    for prefix in allowed:
        if len(prefix) <= len(argv) and list(argv[: len(prefix)]) == list(prefix):
            return True
    return False


def _redacted_ssh_argv(ssh_argv: Sequence[str], key_path: str | None) -> list[str]:
    """Copy the ssh argv with the private-key path replaced by a placeholder."""
    if not key_path:
        return list(ssh_argv)
    redacted: list[str] = []
    for token in ssh_argv:
        if token == key_path:
            redacted.append("<key_path>")
        elif token.startswith(key_path + os.sep):  # pragma: no cover - defensive
            redacted.append("<key_path>" + token[len(key_path):])
        else:
            redacted.append(token)
    return redacted


def _build_ssh_argv(
    config: Mapping[str, Any],
    *,
    remote_command: str,
) -> tuple[list[str], str | None]:
    """Build the ssh argv (and the key path to redact) for one remote command."""
    ssh_binary = shutil.which(SSH_BINARY)
    if not ssh_binary:
        raise ToolMissingError(
            "the 'ssh' executable is not installed; remote execution is unavailable",
            fallbacks=[
                "Install an OpenSSH client (e.g. 'apt install openssh-client').",
                "Use the local tools instead; remote execution is optional and off by default.",
            ],
        )
    key_path = str(config["key_path"]) if config.get("key_path") else None
    argv: list[str] = [
        ssh_binary,
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-o",
        "ConnectTimeout=20",
        "-p",
        str(config.get("port", 22)),
    ]
    if key_path:
        argv.extend(["-i", str(Path(key_path).expanduser())])
    argv.append(f"{config['user']}@{config['host']}")
    argv.append(remote_command)
    return argv, key_path


def _remote_command(argv: Sequence[str], cwd: str | None) -> str:
    """Quote argv into a remote shell command, optionally preceded by ``cd``."""
    command = " ".join(shlex.quote(str(part)) for part in argv)
    if cwd:
        command = f"cd {shlex.quote(str(cwd))} && {command}"
    return command


def _validate_argv(argv: Any) -> list[str]:
    """Validate the remote argv list before any plan/execution."""
    if not isinstance(argv, (list, tuple)) or not argv:
        raise InvalidInputError("remote argv must be a non-empty array of strings")
    parts = [str(part) for part in argv]
    if not all(part.strip() for part in parts):
        raise InvalidInputError("remote argv entries must be non-empty strings")
    if len(parts) > MAX_ARGV_TOKENS:
        raise InvalidInputError(
            f"remote argv has {len(parts)} tokens (limit {MAX_ARGV_TOKENS})",
            details={"tokens": len(parts)},
        )
    if sum(len(part) for part in parts) > MAX_ARGV_LENGTH:
        raise InvalidInputError("remote argv is too long")
    if parts[0].startswith("-"):
        raise InvalidInputError(
            "remote argv[0] must be an executable path, not an option string",
            details={"argv0": parts[0]},
        )
    return parts


def _check_allowed(
    alias: str,
    config: Mapping[str, Any],
    argv: Sequence[str],
    *,
    dry_run: bool,
) -> bool:
    """Enforce the per-host command allow-list (deny-by-default for real runs)."""
    allowed = list(config.get("allowed_commands") or [])
    if not allowed:
        if dry_run:
            return False
        raise ConfigError(
            f"remote host {alias!r} has no 'allowed_commands' allow-list; refusing to execute "
            "arbitrary commands. Add the argv prefixes you permit to the host configuration.",
            code="REMOTE_COMMAND_NOT_ALLOWED",
            details={"alias": alias},
            fallbacks=[
                "Add e.g. \"allowed_commands\": [[\"/opt/hhsuite/bin/hhsearch\"]].",
                "Run the job locally if remote access is not configured for it.",
            ],
        )
    if not _command_allowed(argv, allowed):
        raise LifesciError(
            f"remote command {argv[0]!r} is not in the allow-list for host {alias!r}",
            code="REMOTE_COMMAND_NOT_ALLOWED",
            details={
                "alias": alias,
                "argv0": argv[0],
                "allowed_commands": [" ".join(prefix) for prefix in allowed],
            },
            fallbacks=[
                "Add the exact argv prefix to the host 'allowed_commands' configuration.",
                "Inspect the planned ssh argv with action='plan' first.",
            ],
        )
    return True


#: argv flags whose following value (or =value) is a credential and must be masked.
_CREDENTIAL_FLAGS = (
    "api-key",
    "apikey",
    "api_key",
    "token",
    "access-token",
    "password",
    "passwd",
    "secret",
    "client-secret",
    "private-key",
    "credential",
    "auth",
)


def _is_credential_flag(token: str) -> bool:
    name = str(token).lstrip("-").lower().split("=", 1)[0]
    return any(hint == name or name.endswith("-" + hint) or hint in name for hint in _CREDENTIAL_FLAGS)


def _redact_argv(argv: Sequence[Any]) -> list[str]:
    """Mask credential-bearing argv values so plans/results/provenance never leak them."""
    cleaned: list[str] = []
    hide_next = False
    for raw in argv:
        token = str(raw)
        if hide_next:
            cleaned.append("***redacted***")
            hide_next = False
            continue
        if "=" in token and _is_credential_flag(token.split("=", 1)[0]):
            cleaned.append(token.split("=", 1)[0] + "=***redacted***")
            continue
        if _is_credential_flag(token):
            cleaned.append(token)
            hide_next = True
            continue
        cleaned.append(token)
    return cleaned


def _plan_payload(
    *,
    alias: str,
    argv: Sequence[str],
    config: Mapping[str, Any],
    cwd: str | None,
    timeout: float,
    dry_run: bool,
    allowed: bool,
    ssh_argv: Sequence[str],
    key_path: str | None,
) -> dict[str, Any]:
    """Build a credential-free ssh plan / result skeleton."""
    safe_argv = _redact_argv(argv)
    return {
        "host": alias,
        "argv": safe_argv,
        "command": _remote_command(safe_argv, cwd),
        "cwd": cwd,
        "timeout": timeout,
        "dry_run": dry_run,
        "allowed": allowed,
        "ssh_argv": _redacted_ssh_argv(ssh_argv, key_path),
        "exit_code": None,
        "stdout": "",
        "stderr": "",
        "started_at": None,
        "finished_at": None,
        "tool_version": None,
        "ok": None,
        "provenance": None,
    }


def run(
    ctx: Any,
    *,
    host: str,
    argv: Sequence[str],
    cwd: str | None = None,
    timeout: float = 600,
    dry_run: bool | None = None,
    allow: bool = False,
) -> dict[str, Any]:
    """Plan (dry-run) or execute one remote argv over SSH.

    Args:
        ctx: command context.
        host: configured host **alias** (never a hostname).
        argv: remote command argv list (absolute tool paths recommended).
        cwd: remote working directory; falls back to the host ``work_dir``.
        timeout: wall-clock timeout in seconds for the remote job.
        dry_run: build the ssh plan only.  Defaults to ``ctx.dry_run``; planning
            is also how ``action='plan'`` inspects a job without executing it.
        allow: explicit opt-in required for a real execution (the remote command
            also passes ``allow=True`` only for ``action='run'``).

    Returns:
        A credential-free job record: ``{"host", "argv", "command", "cwd",
        "timeout", "dry_run", "allowed", "ssh_argv" (redacted), "exit_code",
        "stdout", "stderr", "started_at", "finished_at", "tool_version", "ok",
        "provenance"}``.  ``exit_code`` is returned even when non-zero (SSH itself
        succeeded); only connection/auth failures raise.

    Raises:
        RemoteDisabledError: no hosts configured, or a real run without opt-in.
        InvalidInputError: unknown host alias / invalid argv.
        ConfigError: invalid configuration, or no allow-list for a real run.
        LifesciError: command not permitted by the host allow-list.
        ToolMissingError: the local ssh client is missing.
        RemoteError: SSH connection/auth failure or timeout.
    """
    hosts, _source = _load_config(ctx)
    if not hosts:
        raise RemoteDisabledError(
            "remote execution is not configured; set "
            f"{ENV_HOSTS} (JSON) or {ENV_CONFIG} (path) to enable it. Remote execution is "
            "opt-in, the plugin ships no default hosts, and no credentials are stored.",
            details={"env_vars": [ENV_HOSTS, ENV_CONFIG]},
            fallbacks=[
                "Run the job locally instead (this is the default, fully functional path).",
                f"Configure a host in ${ENV_HOSTS} as documented in docs/remote.md.",
            ],
        )
    alias = str(host or "").strip()
    config = hosts.get(alias)
    if config is None:
        raise InvalidInputError(
            f"unknown remote host alias {host!r}",
            details={"host": host, "known_hosts": sorted(hosts)},
        )
    parts = _validate_argv(argv)
    effective_dry_run = bool(dry_run) if dry_run is not None else bool(getattr(ctx, "dry_run", False))
    if not effective_dry_run and not allow:
        raise RemoteDisabledError(
            "remote execution requires an explicit opt-in: pass allow=True (the remote "
            "command only does this for action='run'). Nothing was executed.",
            code="REMOTE_OPT_IN_REQUIRED",
            details={"host": alias, "argv": parts},
            fallbacks=[
                "Inspect the job first with action='plan' (or --dry-run).",
                "Re-run with allow=true after reviewing host and argv.",
            ],
        )
    if not effective_dry_run and bool(getattr(ctx, "offline", False)):
        raise RemoteDisabledError(
            f"remote execution is disabled in offline mode; nothing was sent to {alias!r}. "
            "Offline mode must be reproducible from the local cache only.",
            code="REMOTE_OFFLINE_FORBIDDEN",
            details={"host": alias},
            fallbacks=[
                "Inspect the job with action='plan' (or run with --dry-run), which stays available offline.",
                "Re-run without --offline to execute the remote job.",
            ],
        )
    allowed = _check_allowed(alias, config, parts, dry_run=effective_dry_run)
    timeout_value = max(1.0, float(timeout or 600))
    remote_cwd = str(cwd).strip() if cwd else config.get("work_dir")
    ssh_argv, key_path = _build_ssh_argv(config, remote_command=_remote_command(parts, remote_cwd))
    if effective_dry_run:
        plan = _plan_payload(
            alias=alias,
            argv=parts,
            config=config,
            cwd=remote_cwd,
            timeout=timeout_value,
            dry_run=True,
            allowed=allowed,
            ssh_argv=ssh_argv,
            key_path=key_path,
        )
        plan["plan_only"] = True
        return plan

    started_at = util.now_iso()
    started_monotonic = time.monotonic()
    try:
        completed = subprocess.run(
            ssh_argv,
            capture_output=True,
            text=True,
            timeout=timeout_value,
            check=False,
        )
    except FileNotFoundError as err:
        raise ToolMissingError(
            "the 'ssh' executable disappeared before the remote job could start",
            details={"host": alias},
        ) from err
    except subprocess.TimeoutExpired as err:
        raise RemoteError(
            f"remote job on host {alias!r} exceeded the {timeout_value:.0f}s timeout and was killed",
            code="REMOTE_TIMEOUT",
            retriable=True,
            details={"host": alias, "timeout_s": timeout_value, "argv": parts},
            fallbacks=["Increase the timeout for a legitimately long job.", "Run the step locally if possible."],
        ) from err
    except subprocess.SubprocessError as err:  # pragma: no cover - defensive
        raise RemoteError(
            f"failed to launch ssh for host {alias!r}: {err}",
            code="REMOTE_SSH_FAILED",
            retriable=True,
            details={"host": alias},
        ) from err
    elapsed_ms = (time.monotonic() - started_monotonic) * 1000.0
    finished_at = util.now_iso()
    stdout = completed.stdout or ""
    stderr = completed.stderr or ""
    exit_code = int(completed.returncode)
    if exit_code == 255:
        raise RemoteError(
            f"ssh connection to host {alias!r} failed (exit 255); check the host name, user, "
            "port, key and known_hosts configuration",
            code="REMOTE_SSH_FAILED",
            retriable=True,
            details={"host": alias, "stderr_tail": util.truncate(stderr, 1500)},
            fallbacks=[
                "Verify the alias configuration (host/user/port/key) locally with 'ssh -v'.",
                "Use action='plan' to inspect the exact ssh argv (key path redacted).",
            ],
        )
    safe_parts = _redact_argv(parts)
    safe_ssh_argv = list(_redacted_ssh_argv(ssh_argv, key_path))
    # The final ssh argv element is the remote command string; rebuild it from the
    # redacted argv so credential values never appear in outputs or provenance.
    if safe_ssh_argv:
        safe_ssh_argv[-1] = _remote_command(safe_parts, remote_cwd)
    entry = provenance_log(ctx).tool(
        name="ssh",
        version=None,
        argv=safe_ssh_argv,
        exit_code=exit_code,
        elapsed_ms=elapsed_ms,
        notes={
            "host_alias": alias,
            "remote_argv": safe_parts,
            "cwd": remote_cwd,
            "started_at": started_at,
            "finished_at": finished_at,
            "timeout_s": timeout_value,
        },
    )
    safe_ssh_argv = list(_redacted_ssh_argv(ssh_argv, key_path))
    if safe_ssh_argv:
        safe_ssh_argv[-1] = _remote_command(safe_parts, remote_cwd)
    return {
        "host": alias,
        "argv": safe_parts,
        "command": _remote_command(safe_parts, remote_cwd),
        "cwd": remote_cwd,
        "timeout": timeout_value,
        "dry_run": False,
        "allowed": allowed,
        "ssh_argv": safe_ssh_argv,
        "exit_code": exit_code,
        "stdout": stdout,
        "stderr": stderr,
        "started_at": started_at,
        "finished_at": finished_at,
        "tool_version": None,
        "ok": exit_code == 0,
        "provenance": entry,
    }


def provenance_log(ctx: Any) -> provenance.ProvenanceLog:
    """Return (creating lazily) the context's provenance log object."""
    log = getattr(ctx, "_provenance_log", None)
    if log is None:
        log = provenance.ProvenanceLog(getattr(ctx, "log_path", None))
        ctx._provenance_log = log
    return log


__all__ = [
    "ENV_CONFIG",
    "ENV_HOSTS",
    "MAX_ARGV_LENGTH",
    "MAX_ARGV_TOKENS",
    "SSH_BINARY",
    "configured_hosts",
    "is_enabled",
    "run",
]
