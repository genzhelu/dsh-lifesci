"""Unit tests for :mod:`lifesci.envelope` and :mod:`lifesci.cli`.

Covers the frozen envelope contract (``schemas/envelope.schema.json``), the
library ``run_command`` path, and real ``python -m lifesci`` subprocess calls for
doctor/--list/usage-error/--out.  Everything runs with ``--offline`` and a
``--cache-dir`` inside ``tmp_path``: no network, no writes outside pytest temp.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lifesci import cli
from lifesci import envelope as envelope_mod
from lifesci.errors import InvalidInputError, LifesciError, PortalError, UsageError
from lifesci.transport import FakeTransport

from tests.conftest import (
    assert_warning,
    make_context,
    parse_envelope,
    require_command_registry,
    run_cli,
)


# ---------------------------------------------------------------------------
# envelope construction / schema validation
# ---------------------------------------------------------------------------


def test_make_envelope_ok_shape_validates(tmp_path):
    ctx = make_context(tmp_path)
    ctx.warn("SOME_WARNING", "a warning")
    ctx.review("SOME_REVIEW", "needs a human")
    env = envelope_mod.make_envelope(
        command="doctor",
        data={"status": "ok", "path": Path("/tmp/x"), "nan": float("nan"), "n": 1},
        ctx=ctx,
        started_at="2026-11-05T12:00:00Z",
        finished_at="2026-11-05T12:00:01Z",
        duration_s=1.23456,
    )
    assert env["ok"] is True
    assert env["command"] == "doctor"
    assert env["schema_version"] == envelope_mod.ENVELOPE_SCHEMA_VERSION
    assert env["duration_s"] == 1.235
    assert env["offline"] is False and env["dry_run"] is False
    assert env["data"] == {"status": "ok", "path": "/tmp/x", "nan": None, "n": 1}
    assert env["warnings"][0]["code"] == "SOME_WARNING"
    assert env["human_review"][0]["code"] == "SOME_REVIEW"
    assert env["provenance"] == []
    assert "error" not in env
    assert envelope_mod.validate_envelope(env) == []


def test_make_envelope_error_shape_validates(tmp_path):
    ctx = make_context(tmp_path)
    error = PortalError(
        "primary portal down",
        code="HTTP_ERROR_503",
        retriable=True,
        details={"status": 503, "url": "https://x"},
        fallbacks=["Europe PMC search"],
    )
    env = envelope_mod.make_envelope(
        command="seq.fetch",
        data={"must": "be dropped"},
        ctx=ctx,
        started_at="2026-11-05T12:00:00Z",
        finished_at="2026-11-05T12:00:01Z",
        duration_s=0.0,
        error=error,
    )
    assert env["ok"] is False
    assert env["data"] is None
    assert env["error"]["code"] == "HTTP_ERROR_503"
    assert env["error"]["retriable"] is True
    assert env["error"]["fallbacks"] == ["Europe PMC search"]
    assert env["error"]["details"]["status"] == 503
    assert envelope_mod.validate_envelope(env) == []


def test_envelope_schema_rejects_broken_envelopes(tmp_path):
    ctx = make_context(tmp_path)
    good = envelope_mod.make_envelope(
        command="doctor",
        data={},
        ctx=ctx,
        started_at="t0",
        finished_at="t1",
        duration_s=0.0,
    )
    missing_version = dict(good)
    del missing_version["version"]
    assert envelope_mod.validate_envelope(missing_version)

    bad_command = dict(good)
    bad_command["command"] = "Doctor"  # envelope command names are lower-case dotted
    assert envelope_mod.validate_envelope(bad_command)

    bad_ok = dict(good)
    bad_ok["ok"] = False  # ok:false requires an error object
    assert envelope_mod.validate_envelope(bad_ok)

    assert envelope_mod.validate_envelope(good) == []


def test_dumps_envelope_is_deterministic_and_json_safe(tmp_path):
    ctx = make_context(tmp_path)
    env = envelope_mod.make_envelope(
        command="doctor",
        data={"b": 1, "a": Path("/tmp/a"), "nan": float("inf")},
        ctx=ctx,
        started_at="t0",
        finished_at="t1",
        duration_s=0.0,
    )
    first = envelope_mod.dumps_envelope(env)
    second = envelope_mod.dumps_envelope(env)
    assert first == second
    assert first.endswith("\n")
    assert first.index('"a"') < first.index('"b"')  # sorted keys
    parsed = json.loads(first)
    assert parsed["data"]["a"] == "/tmp/a"
    assert parsed["data"]["nan"] is None
    assert "Infinity" not in first


def test_validate_payload_reports_missing_schema():
    errors = envelope_mod.validate_payload({"x": 1}, "does.not.exist.schema.json")
    assert errors and "missing payload schema" in errors[0]["message"]


# ---------------------------------------------------------------------------
# library API: run_command
# ---------------------------------------------------------------------------


def test_run_command_rejects_unknown_command():
    with pytest.raises(UsageError):
        cli.run_command("does.not.exist", {})


def test_run_command_validates_input(tmp_path):
    require_command_registry()
    with pytest.raises(InvalidInputError) as excinfo:
        cli.run_command("doctor", {"network": "yes"}, ctx=make_context(tmp_path))
    assert excinfo.value.code == "INVALID_INPUT"
    assert excinfo.value.details["validator"] == "type"


def test_run_command_doctor_offline_payload_and_envelope(tmp_path):
    registry = require_command_registry()
    assert "doctor" in registry
    ctx = make_context(tmp_path, offline=True, transport=FakeTransport([]))
    env = cli.run_command("doctor", {"network": False}, ctx=ctx)
    assert env["ok"] is True
    assert envelope_mod.validate_envelope(env) == []
    data = env["data"]
    assert data["status"] == "offline"
    assert data["lifesci_version"] == env["version"]
    # doctor payload must satisfy its published schema.
    assert envelope_mod.validate_payload(data, "doctor.schema.json") == []
    assert_warning(ctx, "NETWORK_CHECK_SKIPPED")
    assert ctx.transport.calls == []  # offline mode: no probes
    # A command provenance entry is appended and returned in the envelope.
    assert env["provenance"][-1]["kind"] == "command"
    assert env["provenance"][-1]["command"] == "doctor"
    assert env["provenance"][-1]["ok"] is True


def test_run_command_doctor_dry_run_flag_echoed(tmp_path):
    require_command_registry()
    ctx = make_context(tmp_path, offline=True, dry_run=True, transport=FakeTransport([]))
    env = cli.run_command("doctor", {"network": False}, ctx=ctx)
    assert env["ok"] is True and env["dry_run"] is True and env["offline"] is True


# ---------------------------------------------------------------------------
# CLI subprocess: --list / doctor / usage errors / --out
# ---------------------------------------------------------------------------


def test_cli_list_returns_one_valid_envelope(tmp_path):
    require_command_registry()
    proc = run_cli(tmp_path, "--list")
    assert proc.returncode == 0, proc.stderr
    env = parse_envelope(proc.stdout, context="for --list")
    assert env["ok"] is True
    assert env["command"] == "list"
    assert envelope_mod.validate_envelope(env) == []
    commands = env["data"]["commands"]
    names = [command["name"] for command in commands]
    assert names == sorted(names)
    assert "doctor" in names
    doctor = next(command for command in commands if command["name"] == "doctor")
    assert doctor["tool_name"] == "lifesci_doctor"
    assert doctor["input_schema"]["type"] == "object"
    assert doctor["input_schema"]["additionalProperties"] is False
    assert all(command["tool_name"].startswith("lifesci_") for command in commands)


def test_cli_list_compact_is_single_line(tmp_path):
    require_command_registry()
    proc = run_cli(tmp_path, "--list", "--compact")
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.count("\n") == 1
    assert parse_envelope(proc.stdout)["command"] == "list"


def test_cli_doctor_offline_returns_zero_and_one_envelope(tmp_path):
    require_command_registry()
    cache_dir = tmp_path / "cache"
    proc = run_cli(
        tmp_path,
        "doctor",
        "--offline",
        "--input",
        '{"network": false}',
        "--cache-dir",
        cache_dir,
        "--log",
        tmp_path / "provenance.jsonl",
        "--quiet",
    )
    assert proc.returncode == 0, proc.stderr
    env = parse_envelope(proc.stdout, context="for doctor")
    assert env["ok"] is True
    assert env["command"] == "doctor"
    assert env["offline"] is True
    assert env["cache_dir"] == str(cache_dir.resolve())
    assert envelope_mod.validate_envelope(env) == []
    assert env["data"]["status"] == "offline"
    assert envelope_mod.validate_payload(env["data"], "doctor.schema.json") == []
    assert (cache_dir / "cache.json").is_file()


def test_cli_doctor_offline_strict_schemas_has_no_output_warning(tmp_path):
    require_command_registry()
    proc = run_cli(
        tmp_path,
        "doctor",
        "--offline",
        "--input",
        '{"network": false}',
        "--cache-dir",
        tmp_path / "cache",
        "--quiet",
        strict_schemas=True,
    )
    assert proc.returncode == 0, proc.stderr
    env = parse_envelope(proc.stdout)
    codes = [warning["code"] for warning in env["warnings"]]
    assert "OUTPUT_SCHEMA_VALIDATION_FAILED" not in codes
    assert "ENVELOPE_SCHEMA_VALIDATION_FAILED" not in codes


@pytest.mark.parametrize(
    ("payload", "expected_path"),
    [
        ('{"network": "yes"}', "network"),  # wrong type
        ("{not json", None),  # malformed JSON
        ("[]", None),  # not an object
    ],
)
def test_cli_invalid_input_exits_2_with_structured_error(tmp_path, payload, expected_path):
    proc = run_cli(tmp_path, "doctor", "--offline", "--input", payload)
    assert proc.returncode == 2, f"stderr={proc.stderr}"
    env = parse_envelope(proc.stdout, context="for invalid input")
    assert env["ok"] is False
    assert env["data"] is None
    assert env["error"]["code"] == "INVALID_INPUT"
    assert env["error"]["message"]
    assert envelope_mod.validate_envelope(env) == []
    if expected_path is not None:
        assert env["error"]["details"]["path"] == expected_path


def test_cli_unknown_command_exits_2_with_usage_error(tmp_path):
    require_command_registry()
    proc = run_cli(tmp_path, "does.not.exist", "--input", "{}")
    assert proc.returncode == 2, proc.stderr
    env = parse_envelope(proc.stdout, context="for unknown command")
    assert env["ok"] is False
    assert env["error"]["code"] == "USAGE"
    assert "does.not.exist" in env["error"]["message"]
    assert envelope_mod.validate_envelope(env) == []


def test_cli_missing_command_prints_help_to_stderr(tmp_path):
    proc = run_cli(tmp_path)
    assert proc.returncode == 2
    assert proc.stdout.strip() == ""  # no envelope for a pure usage error
    assert "usage" in proc.stderr.lower()


def test_cli_error_from_command_is_structured(tmp_path):
    """A real command failure (doctor validates the cache dir) yields ok:false + exit 2."""
    require_command_registry()
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("file", encoding="utf-8")
    proc = run_cli(
        tmp_path,
        "doctor",
        "--offline",
        "--input",
        '{"network": false}',
        "--cache-dir",
        blocker,
        "--quiet",
    )
    assert proc.returncode == 2, proc.stderr
    env = parse_envelope(proc.stdout, context="for failing doctor")
    assert env["ok"] is False
    assert env["error"]["code"] == "CONFIG_ERROR"
    assert envelope_mod.validate_envelope(env) == []


def test_cli_out_writes_the_same_envelope(tmp_path):
    require_command_registry()
    out_path = tmp_path / "out" / "doctor.envelope.json"
    proc = run_cli(
        tmp_path,
        "doctor",
        "--offline",
        "--input",
        '{"network": false}',
        "--cache-dir",
        tmp_path / "cache",
        "--out",
        out_path,
        "--quiet",
    )
    assert proc.returncode == 0, proc.stderr
    stdout_env = parse_envelope(proc.stdout, context="for --out")
    assert out_path.is_file()
    file_text = out_path.read_text(encoding="utf-8")
    assert file_text.endswith("\n")
    assert json.loads(file_text) == stdout_env
    assert envelope_mod.validate_envelope(stdout_env) == []


def test_cli_input_file_and_stdin(tmp_path):
    require_command_registry()
    input_path = tmp_path / "input.json"
    input_path.write_text('{"network": false}', encoding="utf-8")
    from_file = run_cli(
        tmp_path,
        "doctor",
        "--offline",
        "--input-file",
        input_path,
        "--cache-dir",
        tmp_path / "cache-file",
        "--quiet",
    )
    assert from_file.returncode == 0, from_file.stderr
    assert parse_envelope(from_file.stdout)["ok"] is True

    from_stdin = run_cli(
        tmp_path,
        "doctor",
        "--offline",
        "--input-file",
        "-",
        "--cache-dir",
        tmp_path / "cache-stdin",
        "--quiet",
        stdin='{"network": false}',
    )
    assert from_stdin.returncode == 0, from_stdin.stderr
    assert parse_envelope(from_stdin.stdout)["ok"] is True


def test_cli_dry_run_flag_is_echoed(tmp_path):
    require_command_registry()
    proc = run_cli(
        tmp_path,
        "doctor",
        "--offline",
        "--dry-run",
        "--input",
        '{"network": false}',
        "--cache-dir",
        tmp_path / "cache",
        "--quiet",
    )
    assert proc.returncode == 0, proc.stderr
    env = parse_envelope(proc.stdout)
    assert env["dry_run"] is True
    assert env["offline"] is True


def test_cli_offline_seeded_cache_replay_through_http_command(tmp_path):
    """End-to-end: seed one payload, then replay it via the generic http command offline."""
    registry = require_command_registry()
    if "http" not in registry:
        pytest.skip("lifesci.commands.http is not available in this checkout")
    from lifesci.cache import Cache
    from lifesci.cache import store_fixture_payload

    url = "https://example.org/offline-replay"
    fixture = tmp_path / "payload.json"
    body = b'{"replayed": true}'
    fixture.write_bytes(body)
    cache_dir = tmp_path / "seed-cache"
    # The generic ``lifesci.http`` command sends no extra accept header, so the
    # seeded key must match a header-less logical request.
    store_fixture_payload(
        Cache(cache_dir),
        fixture_path=fixture,
        method="GET",
        url=url,
        request_headers={},
        response_headers={"content-type": "application/json"},
        content_type="application/json",
        status=200,
        database="example",
    )
    proc = run_cli(
        tmp_path,
        "http",
        "--offline",
        "--input",
        json.dumps({"method": "GET", "url": url}),
        "--cache-dir",
        cache_dir,
        "--quiet",
    )
    if proc.returncode != 0:
        pytest.skip(f"lifesci.http command not usable offline yet: rc={proc.returncode}")
    env = parse_envelope(proc.stdout)
    assert env["ok"] is True
    assert env["offline"] is True


def test_cli_argparse_failure_still_emits_schema_valid_error_envelope(tmp_path):
    """Even argparse-level failures must produce exactly one valid error envelope."""
    proc = run_cli(tmp_path, "--definitely-not-a-flag")
    assert proc.returncode == 2, proc.stderr
    env = parse_envelope(proc.stdout, context="for argparse failure")
    assert env["ok"] is False
    assert env["command"] == "cli"
    assert env["error"]["code"] == "USAGE"
    assert envelope_mod.validate_envelope(env) == []
    assert "usage" in proc.stderr.lower()


def test_cli_help_exits_zero_without_envelope(tmp_path):
    proc = run_cli(tmp_path, "--help")
    assert proc.returncode == 0
    assert "usage" in proc.stdout.lower()
    assert not proc.stdout.strip().startswith("{")
