"""Live ``doctor`` preflight: every public portal probe actually works.

Opt-in only (``LIFESCI_NETWORK=1``); see ``conftest.py``.
"""

from __future__ import annotations

import pytest

from lifesci import cli
from lifesci import envelope as envelope_mod

from tests.conftest import require_command_registry

pytestmark = pytest.mark.network

#: Portal probes doctor must run (doctor reports reachability).
EXPECTED_PROBES = (
    "uniprot_rest",
    "uniprot_ftp",
    "interpro",
    "europe_pmc",
    "ncbi_eutils",
    "alphafold_db",
    "rcsb_data",
    "esm_atlas",
    "foldseek_web",
)

#: The probes an offline/blocked-network machine is most likely to fail.
REQUIRED_REACHABLE = ("uniprot_rest", "interpro", "rcsb_data")


def test_doctor_network_reports_reachable_portals(net_ctx):
    require_command_registry()
    env = cli.run_command(
        "doctor",
        {"network": True, "probe_timeout_s": 5},
        ctx=net_ctx,
    )
    assert env["ok"] is True, env.get("error")
    assert env["offline"] is False
    assert envelope_mod.validate_envelope(env) == []
    data = env["data"]
    assert envelope_mod.validate_payload(data, "doctor.schema.json") == []
    assert data["status"] in {"ok", "ok-with-warnings", "degraded"}
    assert data["lifesci_version"] == env["version"]

    network = data["network"]
    assert len(network) >= 8, f"expected >=8 portal probes, got {sorted(network)}"
    assert set(EXPECTED_PROBES) <= set(network), f"missing probes: {set(EXPECTED_PROBES) - set(network)}"

    reachable = [name for name, result in network.items() if result.get("reachable")]
    if not reachable:
        pytest.skip(
            "this machine appears to be offline: every portal probe failed "
            f"({ {name: result.get('error') for name, result in network.items()} })"
        )
    for portal in REQUIRED_REACHABLE:
        result = network[portal]
        assert result.get("reachable") is True, f"{portal} probe failed: {result}"
        assert isinstance(result.get("http_status"), int), result

    # A real network run must not have taken the offline/network=false path.
    warning_codes = [warning.get("code") for warning in env["warnings"]]
    assert "NETWORK_CHECK_SKIPPED" not in warning_codes
    # doctor probes are deliberately uncached, so they must not populate the cache.
    assert data["cache"]["n_entries"] == 0
