"""Unit tests for :mod:`lifesci.context` — resolution priority, env flags, validation."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

import pytest

from lifesci.context import DEFAULT_RATE_LIMITS, Context, make_context
from lifesci.errors import ConfigError

from tests.conftest import make_context as build_context


# ---------------------------------------------------------------------------
# cache-dir resolution priority
# ---------------------------------------------------------------------------


def test_explicit_cache_dir_wins_over_environment(tmp_path: Path):
    explicit = tmp_path / "explicit-cache"
    env = tmp_path / "env-cache"
    ctx = Context.create(cache_dir=explicit, log_path=tmp_path / "p.jsonl", env={"LIFESCI_CACHE_DIR": str(env)})
    assert ctx.cache_dir == explicit.resolve()
    assert ctx.log_path == (tmp_path / "p.jsonl").resolve()


def test_env_cache_dir_used_when_no_explicit_argument(tmp_path: Path):
    env_cache = tmp_path / "env-cache"
    ctx = Context.create(env={"LIFESCI_CACHE_DIR": str(env_cache)})
    assert ctx.cache_dir == env_cache.resolve()
    # Default provenance log lives beside the cache.
    assert ctx.log_path == env_cache.resolve() / "provenance.jsonl"


def test_default_cache_dir_is_under_cwd(tmp_path: Path):
    ctx = Context.create(cwd=tmp_path, env={})
    assert ctx.cache_dir == (tmp_path / ".lifesci-cache").resolve()
    assert ctx.cwd == tmp_path.resolve()
    assert ctx.log_path == ctx.cache_dir / "provenance.jsonl"
    assert ctx.datasets_file == ctx.cache_dir / "datasets.json"


def test_cwd_resolution_priority_and_env(tmp_path: Path):
    env = {"LIFESCI_CWD": str(tmp_path / "from-env")}
    assert Context.create(cwd=tmp_path / "explicit", env=env).cwd == (tmp_path / "explicit").resolve()
    assert Context.create(env=env).cwd == (tmp_path / "from-env").resolve()
    # Existing directory is cleaned/made when the explicit path is used.
    ctx = Context.create(cwd=tmp_path, env={})
    assert ctx.cwd.is_dir()


def test_env_log_and_datasets_overrides(tmp_path: Path):
    ctx = Context.create(
        cache_dir=tmp_path / "cache",
        env={
            "LIFESCI_LOG": str(tmp_path / "custom.jsonl"),
            "LIFESCI_DATASETS": str(tmp_path / "datasets-registry.json"),
        },
    )
    assert ctx.log_path == (tmp_path / "custom.jsonl").resolve()
    assert ctx.datasets_file == (tmp_path / "datasets-registry.json").resolve()


# ---------------------------------------------------------------------------
# offline / dry-run flags
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value", ["1", "true", "YES", "on", "True"])
def test_offline_and_dry_run_env_flags(tmp_path: Path, value: str):
    ctx = Context.create(
        cache_dir=tmp_path / "cache",
        env={"LIFESCI_OFFLINE": value, "LIFESCI_DRY_RUN": value},
    )
    assert ctx.offline is True
    assert ctx.dry_run is True


@pytest.mark.parametrize("value", ["0", "false", "no", "off", ""])
def test_offline_and_dry_run_env_flags_false(tmp_path: Path, value: str):
    ctx = Context.create(
        cache_dir=tmp_path / "cache",
        env={"LIFESCI_OFFLINE": value, "LIFESCI_DRY_RUN": value},
    )
    assert ctx.offline is False
    assert ctx.dry_run is False


def test_explicit_flags_persist_and_env_can_only_enable(tmp_path: Path):
    ctx = Context.create(cache_dir=tmp_path / "cache", offline=False, dry_run=False, env={"LIFESCI_OFFLINE": "1"})
    assert ctx.offline is True
    ctx2 = Context.create(cache_dir=tmp_path / "cache", offline=True, env={"LIFESCI_OFFLINE": "0"})
    assert ctx2.offline is True


# ---------------------------------------------------------------------------
# retry / timeout / rate-limit policy
# ---------------------------------------------------------------------------


def test_explicit_timeout_retries_ttl_win(tmp_path: Path):
    ctx = Context.create(
        cache_dir=tmp_path / "cache",
        timeout=5.0,
        max_retries=1,
        cache_ttl=60.0,
        env={},
    )
    assert ctx.timeout == 5.0
    assert ctx.max_retries == 1
    assert ctx.cache_ttl == 60.0


def test_env_retry_policy_used_when_not_explicit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("LIFESCI_HTTP_TIMEOUT", "7.5")
    monkeypatch.setenv("LIFESCI_HTTP_RETRIES", "2")
    monkeypatch.setenv("LIFESCI_CACHE_TTL", "120")
    ctx = Context.create(cache_dir=tmp_path / "cache", env={})
    assert ctx.timeout == 7.5
    assert ctx.max_retries == 2
    assert ctx.cache_ttl == 120.0


def test_default_rate_limits_and_overrides(tmp_path: Path):
    ctx = Context.create(cache_dir=tmp_path / "cache", env={})
    assert ctx.rate_limits["default"] == {"rate": 2.0, "burst": 4.0}
    assert ctx.rate_limits["eutils.ncbi.nlm.nih.gov"]["rate"] == 2.5
    overridden = Context.create(
        cache_dir=tmp_path / "cache2",
        rate_limits={"custom.example.org": {"rate": 1.5, "burst": 2.0}},
        env={},
    )
    assert overridden.rate_limits["custom.example.org"] == {"rate": 1.5, "burst": 2.0}
    assert overridden.rate_limits["default"] == dict(DEFAULT_RATE_LIMITS["default"])


def test_user_agent_and_contact_email(tmp_path: Path):
    ctx = Context.create(
        cache_dir=tmp_path / "cache",
        contact_email="dev@example.org",
        env={"LIFESCI_USER_AGENT": "custom-agent/1"},
    )
    assert ctx.user_agent == "custom-agent/1"
    assert ctx.contact_email == "dev@example.org"
    from_env = Context.create(cache_dir=tmp_path / "cache2", env={"LIFESCI_CONTACT_EMAIL": "env@example.org"})
    assert from_env.contact_email == "env@example.org"


def test_mpldir_is_pinned_inside_cache_when_unset(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Context.create points MPLCONFIGDIR inside the writable cache when unset.

    ``Context.create`` sets this process-wide, which is harmless for short-lived
    CLI runs but would leak between pytest tests — the monkeypatch fixture
    restores it afterwards.
    """
    monkeypatch.delenv("MPLCONFIGDIR", raising=False)
    monkeypatch.delenv("LIFESCI_CACHE_DIR", raising=False)
    ctx = Context.create(cache_dir=tmp_path / "cache", env={})
    expected = ctx.cache_dir / "matplotlib"
    assert os.environ["MPLCONFIGDIR"] == str(expected)
    assert expected.is_dir()

    # When MPLCONFIGDIR is already provided, Context.create must leave it alone.
    monkeypatch.setenv("MPLCONFIGDIR", str(tmp_path / "existing-mpl"))
    untouched = Context.create(cache_dir=tmp_path / "cache2", env={})
    assert os.environ["MPLCONFIGDIR"] == str(tmp_path / "existing-mpl")
    assert untouched.cache_dir == (tmp_path / "cache2").resolve()


def test_env_mapping_layers_on_top_of_process_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Context.create(env=...) behaves like subprocess env overrides ({} = inherit)."""
    monkeypatch.setenv("LIFESCI_HTTP_TIMEOUT", "9.5")
    assert Context.create(cache_dir=tmp_path / "c1", env={}).timeout == 9.5
    layered = Context.create(cache_dir=tmp_path / "c2", env={"LIFESCI_HTTP_TIMEOUT": "3.5"})
    assert layered.timeout == 3.5
    # Explicit arguments beat both layers, including falsy-but-valid values.
    explicit = Context.create(cache_dir=tmp_path / "c3", timeout=2.0, cache_ttl=0.0, env={})
    assert explicit.timeout == 2.0
    assert explicit.cache_ttl == 0.0  # 0 means "always refetch", not "unset"
    assert Context.create(cache_dir=tmp_path / "c4", env={"LIFESCI_HTTP_RETRIES": "1"}).max_retries == 1


# ---------------------------------------------------------------------------
# validation
# ---------------------------------------------------------------------------


def test_validate_rejects_cache_dir_that_is_a_file(tmp_path: Path):
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")
    ctx = Context.create(cache_dir=blocker, env={})
    with pytest.raises(ConfigError) as excinfo:
        ctx.validate()
    assert excinfo.value.code == "CONFIG_ERROR"


def test_validate_rejects_non_positive_timeout(tmp_path: Path):
    ctx = Context.create(cache_dir=tmp_path / "cache", timeout=-1.0, env={})
    with pytest.raises(ConfigError):
        ctx.validate()


def test_validate_accepts_valid_context(tmp_path: Path):
    ctx = Context.create(cache_dir=tmp_path / "cache", env={})
    ctx.validate()  # does not raise (cache dir may not exist yet)


# ---------------------------------------------------------------------------
# accumulators (warnings / human review)
# ---------------------------------------------------------------------------


def test_warn_deduplicates_exactly_and_preserves_order(tmp_path: Path):
    ctx = build_context(tmp_path)
    ctx.warn("A_CODE", "message")
    ctx.warn("A_CODE", "message")  # exact duplicate dropped
    ctx.warn("B_CODE", "message")
    ctx.warn("A_CODE", "message", context={"n": 1})  # different context -> kept
    assert [warning["code"] for warning in ctx.warnings] == ["A_CODE", "B_CODE", "A_CODE"]
    assert ctx.warnings[2]["context"] == {"n": 1}
    assert ctx.warnings[0]["severity"] == "warning"


def test_review_uses_required_severity_and_deduplicates(tmp_path: Path):
    ctx = build_context(tmp_path)
    ctx.review("SINGLE_WEAK_LINE_EVIDENCE", "needs human review")
    ctx.review("SINGLE_WEAK_LINE_EVIDENCE", "needs human review")
    assert len(ctx.human_review) == 1
    assert ctx.human_review[0]["severity"] == "required"


def test_standard_guardrail_warnings(tmp_path: Path):
    ctx = build_context(tmp_path)
    ctx.warn_evalue_db_dependence("afdb50", hint="same pair scored 2.3e-8 in a 35-model panel")
    ctx.warn_tm_query_normalised(query_length=512)
    ctx.warn_top_hit_cap("foldseek", cap=1000)
    codes = [warning["code"] for warning in ctx.warnings]
    assert codes == [
        "EVALUE_NOT_COMPARABLE_ACROSS_DATABASES",
        "TM_SCORE_QUERY_NORMALISED",
        "TOP_HIT_LIST_CAPPED",
    ]
    assert ctx.warnings[0]["context"]["database"] == "afdb50"
    assert ctx.warnings[2]["context"]["cap"] == 1000


# ---------------------------------------------------------------------------
# make_context (CLI namespace adapter)
# ---------------------------------------------------------------------------


def test_make_context_from_cli_namespace(tmp_path: Path):
    from lifesci.transport import FakeTransport

    transport = FakeTransport([])
    args = argparse.Namespace(
        cache_dir=str(tmp_path / "cli-cache"),
        log=str(tmp_path / "cli.jsonl"),
        offline=True,
        dry_run=True,
        timeout=None,
        retries=None,
        cache_ttl=None,
        cwd=str(tmp_path),
        verbose=False,
    )
    ctx = make_context(args, transport=transport)
    assert ctx.cache_dir == (tmp_path / "cli-cache").resolve()
    assert ctx.log_path == (tmp_path / "cli.jsonl").resolve()
    assert ctx.offline is True and ctx.dry_run is True
    assert ctx.cwd == tmp_path.resolve()
    assert ctx.transport is transport
