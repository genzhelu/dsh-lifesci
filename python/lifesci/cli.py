"""Command-line interface: JSON in, JSON envelope out.

Usage examples::

    python -m lifesci --list
    python -m lifesci doctor --input '{"network": false}'
    python -m lifesci seq.fetch --input '{"accessions": ["P69905"]}' --cache-dir .cache
    echo '{"accessions": ["P00698"]}' | python -m lifesci seq.fetch --input-file -
    python -m lifesci struct.fetch --input-file job.json --out struct.fetch.json

The Python API is the same code path::

    from lifesci.cli import run_command
    envelope = run_command("seq.fetch", {"accessions": ["P69905"]})

Exit codes: 0 ok, 2 usage/invalid input, 3 not found, 4 offline cache
miss/corrupt cache, 5 portal/data error, 6 missing external tool, 7 internal,
8 remote execution error.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
from pathlib import Path
from typing import Any, Mapping

from . import __version__, commands, envelope as envelope_mod, paths, util
from .context import Context
from .errors import (
    EXIT_CODE_MEANINGS,
    ConfigError,
    InvalidInputError,
    LifesciError,
    UsageError,
)
from .provenance import ProvenanceLog


# ---------------------------------------------------------------------------
# argument parsing
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lifesci",
        description=(
            "Reproducible life-science research tools. Every command takes a JSON "
            "input object and prints one JSON envelope (data, provenance, warnings)."
        ),
        epilog=(
            "Exit codes: "
            + "; ".join(f"{code}={meaning}" for code, meaning in sorted(EXIT_CODE_MEANINGS.items()))
        ),
    )
    parser.add_argument("command", nargs="?", help="dotted command name, e.g. seq.fetch")
    parser.add_argument("--list", action="store_true", help="list available commands as JSON")
    parser.add_argument("--input", dest="input_json", help="JSON input object as a string")
    parser.add_argument(
        "--input-file",
        dest="input_file",
        help="read JSON input from a file ('-' for stdin)",
    )
    parser.add_argument("--out", help="also write the JSON envelope to this path")
    parser.add_argument("--cache-dir", help="content-addressed cache directory")
    parser.add_argument("--log", help="provenance JSONL log path")
    parser.add_argument("--offline", action="store_true", help="cache-only; no network requests")
    parser.add_argument("--dry-run", action="store_true", help="return the planned request without executing")
    parser.add_argument("--timeout", type=float, help="per-request timeout in seconds (default 60)")
    parser.add_argument("--retries", type=int, help="transient retry budget (default 4)")
    parser.add_argument("--cache-ttl", type=float, help="cache TTL in seconds")
    parser.add_argument("--cwd", help="working directory used for relative paths and default cache")
    parser.add_argument("--config", help="JSON config file with default options")
    parser.add_argument("--no-validate", action="store_true", help="skip input JSON-schema validation")
    parser.add_argument("--compact", action="store_true", help="emit compact single-line JSON")
    parser.add_argument("--quiet", action="store_true", help="suppress non-JSON diagnostics on stderr")
    parser.add_argument("--verbose", action="store_true", help="extra diagnostics on stderr")
    parser.add_argument("--version", action="version", version=f"lifesci {__version__}")
    return parser


def _load_config(path: str | None) -> dict[str, Any]:
    if not path:
        return {}
    config_path = Path(path).expanduser()
    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError as err:
        raise ConfigError(f"config file not found: {config_path}") from err
    except json.JSONDecodeError as err:
        raise ConfigError(f"config file is not valid JSON: {config_path}: {err}") from err
    if not isinstance(payload, dict):
        raise ConfigError("config file must contain a JSON object")
    return payload


def _read_input(args: argparse.Namespace) -> dict[str, Any]:
    raw: str | None = None
    source = "stdin" if args.input_file == "-" else str(args.input_file)
    if args.input_json is not None and args.input_file is not None:
        raise UsageError("provide only one of --input and --input-file")
    if args.input_json is not None:
        raw = args.input_json
        source = "--input"
    elif args.input_file is not None:
        if args.input_file == "-":
            raw = sys.stdin.read()
        else:
            try:
                raw = Path(args.input_file).expanduser().read_text(encoding="utf-8")
            except FileNotFoundError as err:
                raise UsageError(f"input file not found: {args.input_file}") from err
            except OSError as err:
                raise UsageError(f"cannot read input file {args.input_file}: {err}") from err
    if raw is None or not raw.strip():
        return {}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as err:
        raise InvalidInputError(
            f"input JSON is invalid ({source}): {err}",
            details={"source": source, "line": err.lineno, "column": err.colno},
        ) from err
    if not isinstance(payload, dict):
        raise InvalidInputError(
            f"input must be a JSON object, got {type(payload).__name__}",
            details={"source": source},
        )
    return payload


def validate_input(command_obj: commands.Command, payload: Mapping[str, Any]) -> None:
    """Validate the command input against its JSON schema."""
    import jsonschema

    try:
        if not command_obj.input_schema:
            return
        jsonschema.validate(instance=util.json_safe(dict(payload)), schema=dict(command_obj.input_schema))
    except jsonschema.ValidationError as err:
        raise InvalidInputError(
            f"invalid input for {command_obj.name}: {err.message}",
            details={
                "path": "/".join(str(part) for part in err.absolute_path) or "$",
                "validator": err.validator,
                "schema_path": "/".join(str(part) for part in err.absolute_schema_path) or "$",
                "command": command_obj.name,
            },
        ) from err


# ---------------------------------------------------------------------------
# context / execution
# ---------------------------------------------------------------------------


def _context_from_args(args: argparse.Namespace, config: Mapping[str, Any]) -> Context:
    def pick(cli_value: Any, key: str, default: Any = None) -> Any:
        if cli_value is not None:
            return cli_value
        if key in config:
            return config[key]
        return default

    return Context.create(
        cache_dir=pick(args.cache_dir, "cache_dir"),
        log_path=pick(args.log, "log"),
        offline=bool(args.offline or config.get("offline", False)),
        dry_run=bool(args.dry_run or config.get("dry_run", False)),
        timeout=pick(args.timeout, "timeout"),
        max_retries=pick(args.retries, "retries"),
        cache_ttl=pick(args.cache_ttl, "cache_ttl"),
        contact_email=config.get("contact_email"),
        cwd=pick(args.cwd, "cwd"),
        verbose=bool(args.verbose),
    )


def _validate_payload_or_warn(command_obj: commands.Command, payload: Any, ctx: Context) -> None:
    if not command_obj.output_schema:
        return
    errors = envelope_mod.validate_payload(payload, command_obj.output_schema)
    if not errors:
        return
    strict = str(os.environ.get("LIFESCI_STRICT_SCHEMAS", "")).lower() in {"1", "true", "yes"}
    if strict:
        raise LifesciError(
            f"output of {command_obj.name} does not match {command_obj.output_schema}",
            details={"errors": errors[:20]},
        )
    ctx.warn(
        "OUTPUT_SCHEMA_VALIDATION_FAILED",
        f"output of {command_obj.name} does not match {command_obj.output_schema}; "
        "this is a plugin bug, the data is still returned.",
        context={"errors": errors[:10]},
    )


# ---------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------


def run_command(
    name: str,
    args: Mapping[str, Any] | None = None,
    *,
    ctx: Context | None = None,
    validate: bool = True,
) -> dict[str, Any]:
    """Run one command and return its envelope (library API parity with the CLI)."""
    import time

    registry = commands.discover()
    try:
        command_obj = registry[name]
    except KeyError as err:
        raise UsageError(str(err).strip("'\"")) from err

    payload = dict(args or {})
    if validate:
        validate_input(command_obj, payload)
    run_ctx = ctx or Context.create()
    started_at = util.now_iso()
    started = time.monotonic()
    error: LifesciError | None = None
    data: Any = None
    try:
        data = command_obj.run(payload, run_ctx)
        _validate_payload_or_warn(command_obj, data, run_ctx)
    except LifesciError as err:
        error = err
    except Exception as err:  # noqa: BLE001 - convert to a structured internal error
        error = LifesciError(
            f"internal error in {name}: {type(err).__name__}: {err}",
            code="INTERNAL",
            details={
                "exception": type(err).__name__,
                "traceback": traceback.format_exc() if os.environ.get("LIFESCI_DEBUG") else None,
            },
        )
    duration_s = time.monotonic() - started
    finished_at = util.now_iso()
    # Inject duration accurately (the helper recomputes from the monotonic start).
    entry = None
    try:
        log = getattr(run_ctx, "_provenance_log", None) or ProvenanceLog(run_ctx.log_path)
        run_ctx._provenance_log = log
        entry = log.command(
            command=name,
            args=payload,
            ok=error is None,
            started_at=started_at,
            finished_at=finished_at,
            duration_s=duration_s,
            cache_dir=str(run_ctx.cache_dir),
            offline=run_ctx.offline,
            dry_run=run_ctx.dry_run,
            error_code=error.code if error else None,
            warnings=len(run_ctx.warnings),
        )
        run_ctx.provenance.append(entry)
    except Exception:  # pragma: no cover
        pass
    env = envelope_mod.make_envelope(
        command=name,
        data=data,
        ctx=run_ctx,
        started_at=started_at,
        finished_at=finished_at,
        duration_s=duration_s,
        error=error,
    )
    schema_errors = envelope_mod.validate_envelope(env)
    if schema_errors:
        run_ctx.warn(
            "ENVELOPE_SCHEMA_VALIDATION_FAILED",
            "command envelope failed self-validation (plugin bug)",
            context={"errors": schema_errors[:10]},
        )
        env = envelope_mod.make_envelope(
            command=name,
            data=data,
            ctx=run_ctx,
            started_at=started_at,
            finished_at=finished_at,
            duration_s=duration_s,
            error=error,
        )
    return env


def list_commands_envelope() -> dict[str, Any]:
    """Envelope for ``--list`` describing every command and its input schema."""
    registry = commands.discover()
    started_at = util.now_iso()
    data = {
        "package": "dsh-lifesci",
        "version": __version__,
        "commands": [
            {
                "name": cmd.name,
                "tool_name": cmd.tool_name,
                "title": cmd.title,
                "summary": cmd.summary,
                "category": cmd.category,
                "danger": cmd.danger,
                "output_schema": cmd.output_schema,
                "input_schema": util.json_safe(dict(cmd.input_schema)),
            }
            for cmd in sorted(registry.values(), key=lambda c: c.name)
        ],
    }
    ctx = Context.create()
    return envelope_mod.make_envelope(
        command="list",
        data=data,
        ctx=ctx,
        started_at=started_at,
        finished_at=util.now_iso(),
        duration_s=0.0,
    )


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------


def _error_envelope(
    command: str,
    error: LifesciError,
    *,
    offline: bool = False,
    dry_run: bool = False,
    schema_version: int | None = None,
) -> dict[str, Any]:
    """Build a schema-valid error envelope for pre-dispatch failures."""
    from . import __version__

    payload: dict[str, Any] = {
        "ok": False,
        "command": command,
        "version": __version__,
        "schema_version": schema_version or envelope_mod.ENVELOPE_SCHEMA_VERSION,
        "started_at": util.now_iso(),
        "finished_at": util.now_iso(),
        "duration_s": 0.0,
        "offline": bool(offline),
        "dry_run": bool(dry_run),
        "cache_dir": "",
        "data": None,
        "warnings": [],
        "human_review": [],
        "provenance": [],
        "error": error.to_dict(),
    }
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 2
        if code == 0:  # --help / --version already printed by argparse
            return 0
        error = UsageError(
            "invalid command-line arguments (see stderr); pass --help for usage"
        )
        sys.stdout.write(envelope_mod.dumps_envelope(_error_envelope("cli", error)))
        return code

    if args.list:
        try:
            env = list_commands_envelope()
        except LifesciError as err:
            sys.stdout.write(envelope_mod.dumps_envelope(_error_envelope("list", err)))
            if not args.quiet:
                print(f"lifesci: {err}", file=sys.stderr)
            return err.exit_code
        sys.stdout.write(
            json.dumps(env, sort_keys=True, separators=(",", ":")) + "\n"
            if args.compact
            else envelope_mod.dumps_envelope(env)
        )
        return 0

    if not args.command:
        parser.print_help(sys.stderr)
        return 2

    try:
        config = _load_config(args.config or os.environ.get("LIFESCI_CONFIG"))
        registry = commands.discover()
        try:
            command_obj = registry[args.command]
        except KeyError as err:
            known = ", ".join(sorted(registry))
            raise UsageError(f"unknown command {args.command!r}; known commands: {known}") from err
        payload = _read_input(args)
        if not args.no_validate:
            validate_input(command_obj, payload)
        ctx = _context_from_args(args, config)
        env = run_command(args.command, payload, ctx=ctx, validate=False)
    except LifesciError as err:
        env = _error_envelope(
            args.command or "cli", err, offline=bool(args.offline), dry_run=bool(args.dry_run)
        )
        sys.stdout.write(envelope_mod.dumps_envelope(env))
        if not args.quiet:
            print(f"lifesci: {err}", file=sys.stderr)
        return err.exit_code
    except Exception as err:  # noqa: BLE001 - last-resort structured error
        internal = LifesciError(
            f"{type(err).__name__}: {err}",
            code="INTERNAL",
            retriable=False,
            details={"traceback": traceback.format_exc()} if args.verbose else {},
        )
        env = _error_envelope(
            args.command or "cli", internal, offline=False, dry_run=False
        )
        sys.stdout.write(envelope_mod.dumps_envelope(env))
        if not args.quiet:
            print(f"lifesci: internal error: {err}", file=sys.stderr)
        return 7

    text = (
        json.dumps(env, sort_keys=True, separators=(",", ":")) + "\n"
        if args.compact
        else envelope_mod.dumps_envelope(env)
    )
    sys.stdout.write(text)
    if args.out:
        out_path = Path(args.out).expanduser()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        util.atomic_write_text(out_path, text)
        if not args.quiet:
            print(f"lifesci: envelope written to {out_path}", file=sys.stderr)
    if not env.get("ok", False):
        error = env.get("error") or {}
        if not args.quiet:
            print(f"lifesci: {error.get('code')}: {error.get('message')}", file=sys.stderr)
        code_map = {
            "USAGE": 2,
            "INVALID_INPUT": 2,
            "NOT_FOUND": 3,
            "OFFLINE_CACHE_MISS": 4,
            "CACHE_CORRUPT": 4,
            "HTTP_ERROR": 5,
            "RATE_LIMITED": 5,
            "DATA_INVALID": 5,
            "TOOL_MISSING": 6,
            "CONFIG_ERROR": 2,
            "REMOTE_DISABLED": 8,
            "REMOTE_ERROR": 8,
        }
        return code_map.get(str(error.get("code", "INTERNAL")), 7)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
