"""Generic offline golden tests for ``figures``.

Figures are rendered from inline synthetic rows with the headless Agg backend.
The tests assert that a real PNG is written, that its recorded SHA-256 matches
the file on disk, that the data provenance warning is raised, and that
covariance plots require explicit ``x``/``y`` columns (no implicit defaults).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from lifesci import util
from lifesci.cli import run_command

from tests.golden.golden_helpers import run_golden, warning_codes


def _rows(count: int = 12) -> list[dict[str, float]]:
    """Exact power law (plus a tiny deterministic wiggle) for a log-log plot."""
    return [
        {
            "name": f"sample_{index}",
            "x": float(index),
            "y": 3.0 * (float(index) ** 1.5) + (0.1 if index % 2 else -0.1),
        }
        for index in range(1, count + 1)
    ]


def test_covariance_figure_png_and_sha256(golden_ctx, tmp_path):
    out_dir = tmp_path / "figures"
    envelope = run_golden(
        golden_ctx,
        "figures",
        {
            "figures": [
                {
                    "kind": "covariance_loglog",
                    "data": {"rows": _rows()},
                    "x": "x",
                    "y": "y",
                    "title": "Synthetic covariance",
                    "filename": "synthetic_covariance.png",
                }
            ],
            "out_dir": str(out_dir),
        },
        schema="figures.schema.json",
    )
    data = envelope["data"]

    assert data["out_dir"] == str(out_dir)
    assert len(data["figures"]) == 1
    figure = data["figures"][0]
    assert figure["kind"] == "covariance_loglog"
    assert figure["title"] == "Synthetic covariance"
    assert figure["n_points"] == 12

    path = Path(figure["path"])
    assert path == out_dir / "synthetic_covariance.png"
    assert path.is_file(), f"figure PNG was not written: {path}"
    assert path.stat().st_size == figure["bytes"]
    # The recorded hash is the hash of the emitted file (reproducibility hook).
    assert figure["sha256"] == util.sha256_file(path)
    assert len(figure["sha256"]) == 64

    regression = figure["regression"]
    assert regression["n"] == 12
    assert regression["slope"] == pytest.approx(1.5, abs=0.05)
    assert regression["r2"] > 0.99
    assert "FIGURE_DATA_PROVENANCE" in set(warning_codes(envelope))


def test_length_histogram_figure(golden_ctx, tmp_path):
    envelope = run_golden(
        golden_ctx,
        "figures",
        {
            "figures": [
                {
                    "kind": "length_histogram",
                    "data": {"rows": _rows()},
                    "column": "y",
                    "bins": 8,
                    "filename": "synthetic_histogram.png",
                }
            ],
            "out_dir": str(tmp_path / "figures"),
        },
        schema="figures.schema.json",
    )
    figure = envelope["data"]["figures"][0]
    assert figure["kind"] == "length_histogram"
    assert figure["n_points"] == 12
    path = Path(figure["path"])
    assert path.is_file()
    assert figure["sha256"] == util.sha256_file(path)


def test_covariance_requires_explicit_xy_columns(golden_ctx, tmp_path):
    envelope = run_command(
        "figures",
        {
            "figures": [
                {
                    "kind": "covariance_loglog",
                    "data": {"rows": _rows()},
                    "filename": "no_columns.png",
                }
            ],
            "out_dir": str(tmp_path / "figures"),
        },
        ctx=golden_ctx,
        validate=True,
    )
    assert envelope["ok"] is False
    assert envelope["error"]["code"] == "COLUMN_SPEC_MISSING"
    assert not (tmp_path / "figures" / "no_columns.png").exists()
