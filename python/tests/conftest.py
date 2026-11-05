"""Shared helpers and fixtures for the offline ``lifesci`` pytest suite.

Suite conventions:

* **No network.**  Every network-touching code path receives an injected
  :class:`lifesci.transport.FakeTransport` or runs with ``offline=True``.
* **No writes outside ``tmp_path``.**  Cache directories and provenance logs are
  always created under pytest's ``tmp_path``.
* **No real sleeping.**  Contexts get a :class:`FakeClock`; the HTTP retry
  sleeper and the per-host rate limiter both use it, so retry/backoff logic is
  tested deterministically without slowing the suite down.
* **Deterministic.**  No wall-clock assertions, no random ordering, no reliance
  on the developer's environment: an autouse fixture scrubs ``LIFESCI_*`` env
  vars and pins ``MPLCONFIGDIR`` into a session-scoped temp directory.

Helpers defined here are imported by the test modules via
``from tests.conftest import ...`` (``python/tests`` is a package and
``PYTHONPATH=python`` puts the repo's ``python/`` directory on ``sys.path``).
"""

from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Mapping

import pytest

#: Repository root (the directory holding ``python/`` and ``schemas/``).
REPO_ROOT = Path(__file__).resolve().parents[2]
PYTHON_DIR = REPO_ROOT / "python"
SCHEMAS_DIR = REPO_ROOT / "schemas"


# ---------------------------------------------------------------------------
# deterministic time
# ---------------------------------------------------------------------------


class FakeClock:
    """Monotonic fake clock whose ``sleep`` advances virtual time.

    Usable both as the rate limiter ``clock``/``sleeper`` and as the context
    ``_sleeper``; every requested sleep is recorded in :attr:`sleeps`.
    """

    def __init__(self, start: float = 1000.0) -> None:
        self.now = float(start)
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        value = float(seconds)
        self.sleeps.append(value)
        self.now += value

    @property
    def total_slept(self) -> float:
        return sum(self.sleeps)


@pytest.fixture(scope="session", autouse=True)
def _isolated_session_env(tmp_path_factory: pytest.TempPathFactory):
    """Pin matplotlib's config dir inside pytest's temp area for the whole run.

    ``Context.create`` exports ``MPLCONFIGDIR`` when it is unset; without this
    fixture that would leak a (deleted) tmp path into later subprocess tests and
    let matplotlib write into the developer's home directory.
    """
    mpl_dir = tmp_path_factory.mktemp("mpl-config")
    previous = os.environ.get("MPLCONFIGDIR")
    os.environ["MPLCONFIGDIR"] = str(mpl_dir)
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("MPLCONFIGDIR", None)
        else:
            os.environ["MPLCONFIGDIR"] = previous


@pytest.fixture(autouse=True)
def _isolated_lifesci_env(monkeypatch: pytest.MonkeyPatch):
    """Scrub ambient lifesci configuration so tests are environment-independent."""
    for name in list(os.environ):
        if name.startswith("LIFESCI_") or name in {"DSH_HOME", "XDG_CACHE_HOME"}:
            monkeypatch.delenv(name, raising=False)
    yield


# ---------------------------------------------------------------------------
# contexts
# ---------------------------------------------------------------------------


def make_context(
    tmp_path: Path,
    *,
    transport: Any = None,
    offline: bool = False,
    dry_run: bool = False,
    max_retries: int = 2,
    cache_dir: str | os.PathLike[str] | None = None,
    log_path: str | os.PathLike[str] | None = None,
    clock: FakeClock | None = None,
    rate_limits: Mapping[str, Mapping[str, float]] | None = None,
    env: Mapping[str, str] | None = None,
    **create_kwargs: Any,
):
    """Create a :class:`lifesci.context.Context` wired for offline testing.

    The returned context has:

    * ``ctx.transport`` — the injected transport (``None`` means "never called");
    * ``ctx._sleeper`` — the shared :class:`FakeClock`;
    * ``ctx._limiter`` — a permissive :class:`lifesci.ratelimit.HostLimiter` that
      also runs on the fake clock (so a legitimate wait is recorded, never slept).
    """
    from lifesci.context import Context
    from lifesci.ratelimit import HostLimiter

    clock = clock or FakeClock()
    resolved_cache = Path(cache_dir) if cache_dir is not None else tmp_path / "cache"
    resolved_log = Path(log_path) if log_path is not None else tmp_path / "provenance.jsonl"
    # Keep Context.create from exporting a (soon-to-be-deleted) MPLCONFIGDIR into
    # the real process environment: pin it into tmp_path through the env mapping.
    environment = dict(env or {})
    environment.setdefault("MPLCONFIGDIR", str(tmp_path / "mpl-config"))
    ctx = Context.create(
        cache_dir=resolved_cache,
        log_path=resolved_log,
        offline=offline,
        dry_run=dry_run,
        max_retries=max_retries,
        transport=transport,
        env=environment,
        **create_kwargs,
    )
    limits = dict(rate_limits or _permissive_limits())
    ctx._sleeper = clock.sleep
    ctx._limiter = HostLimiter(limits=limits, clock=clock, sleeper=clock.sleep)
    ctx._test_clock = clock  # convenience handle for assertions
    return ctx


def _permissive_limits() -> dict[str, dict[str, float]]:
    """Rate limits that never throttle in unit tests (fake clock records waits anyway)."""
    return {
        "default": {"rate": 1000.0, "burst": 1000.0},
        "eutils.ncbi.nlm.nih.gov": {"rate": 1000.0, "burst": 1000.0},
        "rest.uniprot.org": {"rate": 1000.0, "burst": 1000.0},
        "ftp.uniprot.org": {"rate": 1000.0, "burst": 1000.0},
        "alphafold.ebi.ac.uk": {"rate": 1000.0, "burst": 1000.0},
        "api.esmatlas.com": {"rate": 1000.0, "burst": 1000.0},
    }


# ---------------------------------------------------------------------------
# assertion helpers
# ---------------------------------------------------------------------------


def warning_codes(ctx: Any) -> list[str]:
    """Machine-readable warning codes recorded on a context, in insertion order."""
    return [str(entry.get("code")) for entry in getattr(ctx, "warnings", [])]


def assert_warning(ctx: Any, code: str) -> dict[str, Any]:
    """Return the warning entry ``code`` or fail with the codes that *were* recorded."""
    for entry in getattr(ctx, "warnings", []):
        if entry.get("code") == code:
            return entry
    raise AssertionError(f"expected warning {code!r}; recorded codes: {warning_codes(ctx)}")


# ---------------------------------------------------------------------------
# CLI / subprocess helpers
# ---------------------------------------------------------------------------


def cli_env(
    tmp_path: Path,
    *,
    extra: Mapping[str, str] | None = None,
    strict_schemas: bool = False,
) -> dict[str, str]:
    """A hermetic environment for invoking ``python -m lifesci`` in a subprocess."""
    env = {key: value for key, value in os.environ.items() if not key.startswith("LIFESCI_")}
    env["PYTHONPATH"] = str(PYTHON_DIR)
    env["MPLCONFIGDIR"] = str(tmp_path / "mpl-config")
    if strict_schemas:
        env["LIFESCI_STRICT_SCHEMAS"] = "1"
    if extra:
        env.update({str(key): str(value) for key, value in extra.items()})
    return env


def run_cli(
    tmp_path: Path,
    *args: Any,
    stdin: str | None = None,
    extra_env: Mapping[str, str] | None = None,
    timeout: float = 240.0,
    strict_schemas: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Run ``python -m lifesci <args>`` with a hermetic environment, repo cwd, no network."""
    command = [sys.executable, "-m", "lifesci", *[str(arg) for arg in args]]
    return subprocess.run(
        command,
        cwd=str(REPO_ROOT),
        env=cli_env(tmp_path, extra=extra_env, strict_schemas=strict_schemas),
        input=stdin,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def parse_envelope(stdout: str, *, context: str = "") -> dict[str, Any]:
    """Parse stdout as *exactly one* JSON envelope document (trailing junk fails)."""
    text = stdout.strip()
    assert text, f"expected one JSON envelope on stdout{context}, got empty stdout"
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as err:
        raise AssertionError(
            f"stdout is not exactly one JSON document{context}: {err}; "
            f"stdout[:2000]={text[:2000]!r}"
        ) from err
    assert isinstance(payload, dict), f"envelope must be a JSON object, got {type(payload).__name__}"
    return payload


# ---------------------------------------------------------------------------
# optional module imports
# ---------------------------------------------------------------------------


def import_optional(module_name: str, *, feature: str = "") -> Any:
    """Import an optional module, skipping the test when it is unavailable.

    Skips the test **only** when a ``lifesci`` module is missing.  Any other
    import failure — or a broken implementation — fails loudly, because that is
    a real regression.
    """
    try:
        return importlib.import_module(module_name)
    except ModuleNotFoundError as err:
        missing = str(err.name or "")
        if missing.startswith("lifesci"):
            suffix = f" ({feature})" if feature else ""
            pytest.skip(f"module {module_name} not yet implemented{suffix}: {err}")
        raise
    except ImportError as err:  # transitive import problem inside a lifesci module
        if "lifesci" in str(err):
            suffix = f" ({feature})" if feature else ""
            pytest.skip(f"module {module_name} not yet importable{suffix}: {err}")
        raise


def require_command_registry() -> dict[str, Any]:
    """Discover the full command registry or skip while command modules are unavailable.

    A missing ``lifesci`` module is an explicit, reported skip; any other
    discovery failure (syntax error, broken import, duplicate command) is
    surfaced as a hard failure.
    """
    from lifesci import commands
    from lifesci.errors import ConfigError

    try:
        return commands.discover(force=True)
    except ConfigError as err:
        failures = list((err.details or {}).get("failures") or [])
        missing = [
            failure
            for failure in failures
            if "ModuleNotFoundError" in failure or "ImportError" in failure
        ]
        if failures and len(missing) == len(failures):
            pytest.skip(
                "command modules not available in this checkout: "
                + "; ".join(failures)
            )
        raise


# ---------------------------------------------------------------------------
# pytest fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def clock() -> FakeClock:
    """A fresh deterministic clock for tests that assert on sleeps."""
    return FakeClock()


@pytest.fixture()
def fake_transport_factory():
    """Factory building a :class:`lifesci.transport.FakeTransport` from scripted responses."""
    from lifesci.transport import FakeTransport

    def build(*responses: Any, repeat_last: bool = False) -> FakeTransport:
        return FakeTransport(list(responses), repeat_last=repeat_last)

    return build
