"""Generic offline golden tests for ``stats``.

The synthetic tables are built from closed-form values, so every asserted
statistic is determined by the test data (not by a recorded research result):

* a partial correlation whose ``core`` control is derived explicitly through
  ``core_from`` (``total - segment columns``);
* a log-log fit of an exact power law ``y = 3 * x**1.5``;
* ``group_by='auto'`` grouping identifiers by the prefix before the first ``_``;
* the mandatory anti-overclaiming caveats;
* typed errors when ``control='core'`` has no definition.
"""

from __future__ import annotations

import pytest

from lifesci.cli import run_command

from tests.golden.golden_helpers import run_golden, warning_codes

#: Residual amplitude used to decorrelate the partial-correlation control.
_A = [1.0, 3.0, 2.0, 5.0, 4.0, 7.0, 6.0, 9.0, 8.0, 11.0]


def _core_rows() -> list[dict[str, float]]:
    """Rows where ``core = total - (seg_a + seg_b)`` and ``x/y`` share that core.

    ``x = core + A`` and ``y = core + 2A``, so after controlling for ``core`` the
    residual correlation is driven entirely by ``A`` and ``2A`` (r = 1). The
    raw Pearson r is also very high, and n = 10 keeps the suite fast.
    """
    rows: list[dict[str, float]] = []
    for index, residual in enumerate(_A):
        core = float(index + 1)
        seg_a = 1.0 + 0.1 * index
        seg_b = 0.5 + 0.05 * index
        rows.append(
            {
                "name": f"sample_{index + 1}",
                "x": core + residual,
                "y": core + 2.0 * residual,
                "total": core + seg_a + seg_b,
                "seg_a": seg_a,
                "seg_b": seg_b,
            }
        )
    return rows


def _power_law_rows(count: int = 19) -> list[dict[str, float]]:
    return [
        {"name": f"point_{index:02d}", "x": float(index), "y": 3.0 * (float(index) ** 1.5)}
        for index in range(1, count + 1)
    ]


def _grouped_rows() -> list[dict[str, float]]:
    rows: list[dict[str, float]] = []
    for group, offset in (("alpha", 0.0), ("beta", 5.0)):
        for index in range(1, 5):
            rows.append(
                {
                    "name": f"{group}_{index}",
                    "x": float(index),
                    "y": 2.0 * float(index) + offset,
                }
            )
    return rows


def test_pearson_and_explicit_core_partial(golden_ctx):
    envelope = run_golden(
        golden_ctx,
        "stats",
        {
            "rows": _core_rows(),
            "x": "x",
            "y": "y",
            "control": "core",
            "core_from": {"total_column": "total", "segment_columns": ["seg_a", "seg_b"]},
            "group_by": "none",
        },
        schema="stats.schema.json",
    )
    data = envelope["data"]

    assert data["n"] == 10
    assert data["pearson"]["n"] == 10
    assert data["pearson"]["r"] == pytest.approx(0.9986, abs=0.01)

    # The partial correlation is computed with the explicitly derived core.
    assert data["partial"] is not None
    assert data["partial"]["controlled_for"] == "core"
    assert data["partial"]["n"] == 10
    assert data["partial"]["r"] == pytest.approx(1.0, abs=1e-6)
    definition = data["variables"]["control_definition"]
    assert definition["definition"] == "total - (seg_a + seg_b)"
    assert definition["total_column"] == "total"
    assert definition["segment_columns"] == ["seg_a", "seg_b"]

    # Mandatory anti-overclaiming caveats travel in the payload and the envelope.
    caveats = set(data["caveats"])
    assert {"CORRELATION_NOT_CAUSATION", "PHYLOGENETIC_NON_INDEPENDENCE", "SMALL_N"} <= caveats
    assert {"CORRELATION_NOT_CAUSATION", "PHYLOGENETIC_NON_INDEPENDENCE", "SMALL_N"} <= set(
        warning_codes(envelope)
    )
    assert all(note["code"] and note["message"] for note in data["caveat_notes"])


def test_loglog_fit_recovers_the_power_law(golden_ctx):
    envelope = run_golden(
        golden_ctx,
        "stats",
        {"rows": _power_law_rows(), "x": "x", "y": "y", "group_by": "none"},
        schema="stats.schema.json",
    )
    data = envelope["data"]
    assert data["n"] == 19
    fit = data["loglog"]
    assert fit["slope"] == pytest.approx(1.5, abs=1e-3)
    assert fit["r2"] == pytest.approx(1.0, abs=1e-3)
    assert fit["model"] == "log(y) = intercept + slope * log(x)"
    # The compact summary mirrors the fit.
    assert data["summary"]["loglog_slope"] == pytest.approx(fit["slope"], abs=1e-6)
    assert data["summary"]["loglog_r2"] == pytest.approx(fit["r2"], abs=1e-6)


def test_group_by_auto_uses_identifier_prefix(golden_ctx):
    envelope = run_golden(
        golden_ctx,
        "stats",
        {"rows": _grouped_rows(), "x": "x", "y": "y"},
        schema="stats.schema.json",
    )
    data = envelope["data"]
    groups = {group["group"]: group for group in data["groups"]}
    assert sorted(groups) == ["alpha", "beta"]
    assert groups["alpha"]["n"] == 4
    assert groups["beta"]["n"] == 4
    assert {point["group"] for point in data["points"]} == {"alpha", "beta"}
    # Deterministic ordering: points keep input order and groups are sorted.
    assert [point["label"] for point in data["points"]] == [
        row["name"] for row in _grouped_rows()
    ]


def test_control_auto_reuses_existing_core_column(golden_ctx):
    rows = [dict(row, core=row["total"] - row["seg_a"] - row["seg_b"]) for row in _core_rows()]
    envelope = run_golden(
        golden_ctx,
        "stats",
        {"rows": rows, "x": "x", "y": "y", "control": "auto", "group_by": "none"},
        schema="stats.schema.json",
    )
    data = envelope["data"]
    assert data["variables"]["control"] == "core"
    assert data["variables"]["control_definition"]["definition"] == "column 'core'"
    assert data["partial"]["r"] == pytest.approx(1.0, abs=1e-6)


def test_control_core_without_definition_is_a_typed_error(golden_ctx):
    envelope = run_command(
        "stats",
        {"rows": _core_rows(), "x": "x", "y": "y", "control": "core"},
        ctx=golden_ctx,
        validate=True,
    )
    assert envelope["ok"] is False
    assert envelope["error"]["code"] == "CORE_DEFINITION_MISSING"
    assert "core_from" in envelope["error"]["message"]
