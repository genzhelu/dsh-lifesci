"""Matplotlib (Agg) figure generation for lifesci results.

All figures use the non-interactive ``Agg`` backend, a fixed figure size and
DPI, and deterministic colours/annotations, so re-running on the same data
produces a byte-stable PNG.  ``MPLCONFIGDIR`` is pointed at the context cache
directory (matplotlib otherwise fails in sandboxed runs where ``$HOME/.config``
is not writable).

The returned payload always carries ``path``, ``sha256`` and ``bytes`` of the
written image; when a ``ctx`` is supplied the file is also appended to
``ctx.provenance`` as a ``local_file`` record.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any, Mapping, Sequence

from ..errors import DataError, InvalidInputError
from ..util import ensure_dir, json_safe, now_iso, sha256_file

#: Deterministic group colour palette (colour-blind friendly-ish).
GROUP_COLORS: tuple[str, ...] = (
    "#4c72b0",
    "#c44e52",
    "#55a868",
    "#8172b2",
    "#b8860b",
    "#da8bc3",
    "#937860",
    "#8c8c8c",
)


def _prepare_matplotlib(ctx: Any = None) -> Any:
    """Configure and import matplotlib; returns ``matplotlib.pyplot``."""
    config_dir: Path | None = None
    if ctx is not None and getattr(ctx, "cache_dir", None):
        config_dir = Path(ctx.cache_dir) / "matplotlib"
    if config_dir is not None:
        try:
            ensure_dir(config_dir)
            os.environ.setdefault("MPLCONFIGDIR", str(config_dir))
        except OSError:  # pragma: no cover - non-fatal
            pass
    if not os.environ.get("MPLCONFIGDIR"):
        os.environ["MPLCONFIGDIR"] = os.path.join(
            tempfile.gettempdir(), "lifesci-matplotlib"
        )
        try:
            os.makedirs(os.environ["MPLCONFIGDIR"], exist_ok=True)
        except OSError:  # pragma: no cover
            pass
    import matplotlib

    matplotlib.use("Agg", force=True)
    import matplotlib.pyplot as plt

    return plt


def _record_file(ctx: Any, path: Path, *, tool: str = "matplotlib") -> None:
    if ctx is None or not hasattr(ctx, "provenance"):
        return
    try:
        ctx.provenance.append(
            {
                "kind": "local_file",
                "path": str(path),
                "sha256": sha256_file(path),
                "bytes": path.stat().st_size,
                "tool": tool,
                "time": now_iso(),
            }
        )
    except OSError:  # pragma: no cover - provenance must never fail the figure
        pass


def _figure_result(path: Path, **extra: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "path": str(path),
        "sha256": sha256_file(path),
        "bytes": path.stat().st_size,
    }
    payload.update(extra)
    return payload


def _row_label(row: Mapping[str, Any], index: int) -> str:
    for key in ("label", "key", "name", "id", "organism", "species"):
        value = row.get(key)
        if value:
            return str(value)
    return f"row_{index}"


def _numeric_rows(
    data: Sequence[Mapping[str, Any]], x: str, y: str, *, positive: bool
) -> list[tuple[int, Mapping[str, Any], float, float]]:
    if not data:
        raise InvalidInputError("figures need at least one data row")
    rows: list[tuple[int, Mapping[str, Any], float, float]] = []
    for index, row in enumerate(data):
        if not isinstance(row, Mapping):
            raise InvalidInputError(f"data row {index} is not a mapping")
        try:
            x_value = float(row[x])
            y_value = float(row[y])
        except (KeyError, TypeError, ValueError) as err:
            raise InvalidInputError(
                f"data row {index} has no numeric {x!r}/{y!r} values",
                details={"row": json_safe(dict(row))},
            ) from err
        if positive and (x_value <= 0 or y_value <= 0):
            continue
        rows.append((index, row, x_value, y_value))
    if not rows:
        raise DataError(
            f"no data rows with positive {x!r}/{y!r} values to plot"
        )
    return rows


# ---------------------------------------------------------------------------
# log-log scatter
# ---------------------------------------------------------------------------


def scatter_loglog(
    ctx: Any,
    *,
    data: Sequence[Mapping[str, Any]],
    x: str,
    y: str,
    group: str | None = None,
    regression: bool = True,
    annotate_outliers: bool = True,
    title: str = "",
    xlabel: str | None = None,
    ylabel: str | None = None,
    out_path: str | Path,
    figsize: tuple[float, float] = (7.6, 6.2),
    dpi: int = 220,
    annotate_threshold: float = 2.0,
) -> dict[str, Any]:
    """Scatter ``y`` vs ``x`` on log-log axes with an optional regression.

    Parameters
    ----------
    ctx:
        Optional :class:`lifesci.context.Context` (matplotlib config dir and
        provenance recording) or ``None``.
    data:
        List of row dicts (e.g. the per-species ES length table).
    x, y:
        Column names of the two variables; must be strictly positive to plot.
    group:
        Optional column name used to colour points by group (groups sorted
        alphabetically for deterministic output).
    regression:
        Fit ``log(y) ~ log(x)`` with :func:`lifesci.lib.stats.loglog_fit` and
        draw the fit line; the fit is returned in ``regression``.
    annotate_outliers:
        Annotate points with ``|standardised residual| >= annotate_threshold``.
    title, xlabel, ylabel:
        Plot labels (``xlabel``/``ylabel`` default to the column names).
    out_path:
        Output PNG path.
    figsize, dpi:
        Fixed for deterministic output.

    Returns
    -------
    dict
        ``{"path", "sha256", "bytes", "n_points", "n_rows", "regression",
        "groups"}`` where ``regression`` is the log-log fit payload.
    """
    from . import stats as stats_lib

    plt = _prepare_matplotlib(ctx)
    rows = _numeric_rows(data, x, y, positive=True)
    x_values = [row[2] for row in rows]
    y_values = [row[3] for row in rows]
    fit: dict[str, Any] | None = None
    if regression:
        fit = stats_lib.loglog_fit(x_values, y_values)

    figure, axes = plt.subplots(figsize=figsize)
    groups: list[str] = []
    if group:
        group_names = sorted({str(row[1].get(group)) for row in rows})
        groups = [name for name in group_names if name not in {"None", ""}]
        colour_map = {
            name: GROUP_COLORS[index % len(GROUP_COLORS)]
            for index, name in enumerate(groups)
        }
        for name in groups:
            xs = [row[2] for row in rows if str(row[1].get(group)) == name]
            ys = [row[3] for row in rows if str(row[1].get(group)) == name]
            axes.scatter(
                xs,
                ys,
                s=46,
                color=colour_map[name],
                edgecolor="k",
                linewidth=0.4,
                label=name,
                zorder=3,
            )
        other_x = [row[2] for row in rows if str(row[1].get(group)) not in colour_map]
        other_y = [row[3] for row in rows if str(row[1].get(group)) not in colour_map]
        if other_x:
            axes.scatter(
                other_x,
                other_y,
                s=46,
                color="#8c8c8c",
                edgecolor="k",
                linewidth=0.4,
                label="other",
                zorder=3,
            )
            groups.append("other")
        axes.legend(fontsize=7.5, frameon=False, loc="upper left")
    else:
        axes.scatter(
            x_values,
            y_values,
            s=46,
            color=GROUP_COLORS[0],
            edgecolor="k",
            linewidth=0.4,
            zorder=3,
        )
    axes.set_xscale("log")
    axes.set_yscale("log")
    axes.set_xlabel(xlabel or x)
    axes.set_ylabel(ylabel or y)
    if title:
        axes.set_title(title, fontsize=11)
    axes.grid(ls=":", lw=0.5, alpha=0.6, which="both")
    if fit is not None:
        import numpy as np

        minimum = min(x_values)
        maximum = max(x_values)
        line_x = np.linspace(minimum * 0.8, maximum * 1.15, 100)
        line_y = np.exp(fit["intercept"]) * line_x ** fit["slope"]
        axes.plot(
            line_x,
            line_y,
            "k--",
            lw=1.2,
            label=(
                f"log-log fit: {y} ∝ {x}^{fit['slope']:.2f} "
                f"(r²={fit['r2']:.2f}, n={fit['n']})"
            ),
        )
        axes.legend(fontsize=7.5, frameon=False, loc="upper left")
    if annotate_outliers and fit is not None:
        import math

        residual_rows = stats_lib.standardised_residuals(fit, x_values, y_values)
        for z_value, (index, row, x_value, y_value) in zip(residual_rows, rows):
            if abs(z_value) < annotate_threshold:
                continue
            if math.isfinite(x_value) and math.isfinite(y_value):
                axes.annotate(
                    _row_label(row, index),
                    (x_value, y_value),
                    fontsize=6.5,
                    xytext=(3, 3),
                    textcoords="offset points",
                )
    figure.tight_layout()
    target = Path(out_path)
    ensure_dir(target.parent)
    figure.savefig(str(target), dpi=int(dpi))
    plt.close(figure)
    _record_file(ctx, target)
    return _figure_result(
        target,
        n_points=len(rows),
        n_rows=len(data),
        groups=groups,
        regression=fit,
        kind="loglog_scatter",
        parameters={
            "x": x,
            "y": y,
            "group": group,
            "regression": bool(regression),
            "annotate_outliers": bool(annotate_outliers),
            "figsize": list(figsize),
            "dpi": int(dpi),
        },
    )


# ---------------------------------------------------------------------------
# bar chart
# ---------------------------------------------------------------------------


def bar_contacts(
    ctx: Any,
    *,
    data: Sequence[Mapping[str, Any]],
    label_key: str,
    value_key: str,
    out_path: str | Path,
    title: str = "",
    xlabel: str | None = None,
    color: str = "#4c72b0",
    threshold: float | None = None,
    figsize: tuple[float, float] = (7.2, 4.6),
    dpi: int = 220,
) -> dict[str, Any]:
    """Horizontal bar chart (e.g. minimum contact distance per structure).

    ``data`` rows are sorted ascending by ``value_key`` for determinism.
    Returns the same artifact payload shape as :func:`scatter_loglog`.
    """
    plt = _prepare_matplotlib(ctx)
    rows: list[tuple[str, float]] = []
    for index, row in enumerate(data or []):
        try:
            rows.append((str(row[label_key]), float(row[value_key])))
        except (KeyError, TypeError, ValueError) as err:
            raise InvalidInputError(
                f"data row {index} has no numeric {value_key!r} value",
                details={"row": json_safe(dict(row))},
            ) from err
    if not rows:
        raise InvalidInputError("bar_contacts needs at least one data row")
    rows.sort(key=lambda item: (item[1], item[0]))
    labels = [label for label, _value in rows]
    values = [value for _label, value in rows]
    height = max(2.4, 0.32 * len(rows) + 1.6)
    figure, axes = plt.subplots(figsize=(figsize[0], max(figsize[1], height)))
    positions = list(range(len(rows)))
    axes.barh(positions, values, color=color, alpha=0.85)
    axes.set_yticks(positions)
    axes.set_yticklabels(labels, fontsize=8)
    if xlabel:
        axes.set_xlabel(xlabel)
    if title:
        axes.set_title(title, fontsize=10)
    if threshold is not None:
        axes.axvline(float(threshold), color="k", ls="--", lw=1)
    figure.tight_layout()
    target = Path(out_path)
    ensure_dir(target.parent)
    figure.savefig(str(target), dpi=int(dpi))
    plt.close(figure)
    _record_file(ctx, target)
    return _figure_result(
        target,
        n_points=len(rows),
        kind="bar_contacts",
        parameters={
            "label": label_key,
            "value": value_key,
            "dpi": int(dpi),
            "threshold": None if threshold is None else float(threshold),
        },
    )


# ---------------------------------------------------------------------------
# contact map
# ---------------------------------------------------------------------------


def contact_map(
    ctx: Any,
    *,
    data: Sequence[Mapping[str, Any]],
    row_key: str,
    column_key: str,
    value_key: str,
    out_path: str | Path,
    title: str = "",
    vmin: float = 2.0,
    vmax: float = 10.0,
    cmap: str = "viridis_r",
    figsize: tuple[float, float] | None = None,
    dpi: int = 200,
) -> dict[str, Any]:
    """Heatmap of long-form contact distances (row x column).

    Missing combinations are masked (not zero); values above ``vmax`` are
    clipped for colouring only (the matrix is returned unclipped in ``matrix``).
    """
    plt = _prepare_matplotlib(ctx)
    try:
        import numpy as np
    except ImportError as err:  # pragma: no cover - numpy is a dependency
        raise DataError(f"numpy is required for contact maps: {err}") from err
    rows = sorted({str(row[row_key]) for row in data})
    columns = sorted({str(row[column_key]) for row in data})
    if not rows or not columns:
        raise InvalidInputError("contact_map needs at least one row and column")
    row_index = {name: index for index, name in enumerate(rows)}
    column_index = {name: index for index, name in enumerate(columns)}
    matrix = np.full((len(rows), len(columns)), np.nan)
    for row in data:
        matrix[row_index[str(row[row_key])], column_index[str(row[column_key])]] = (
            float(row[value_key])
        )
    if not np.isfinite(matrix).any():
        raise DataError("contact_map data contains no finite values")
    shown = np.ma.masked_invalid(matrix)
    height = max(2.6, 0.42 * len(rows) + 2.2)
    width = max(4.0, 0.6 * len(columns) + 3.0)
    figure, axes = plt.subplots(
        figsize=figsize if figsize else (width, height)
    )
    colormap = plt.get_cmap(cmap).copy()
    colormap.set_bad("white")
    image = axes.imshow(
        shown,
        aspect="auto",
        cmap=colormap,
        vmin=float(vmin),
        vmax=float(vmax),
    )
    axes.set_xticks(range(len(columns)))
    axes.set_xticklabels(columns, rotation=60, ha="right", fontsize=7.5)
    axes.set_yticks(range(len(rows)))
    axes.set_yticklabels(rows, fontsize=8.5)
    if title:
        axes.set_title(title, fontsize=10)
    figure.colorbar(image, ax=axes, label="Å (dark = contact)", shrink=0.8)
    figure.tight_layout()
    target = Path(out_path)
    ensure_dir(target.parent)
    figure.savefig(str(target), dpi=int(dpi))
    plt.close(figure)
    _record_file(ctx, target)
    return _figure_result(
        target,
        n_points=int(np.isfinite(matrix).sum()),
        kind="contact_map",
        matrix={
            "rows": rows,
            "columns": columns,
            "values": [
                [None if not np.isfinite(value) else round(float(value), 3) for value in line]
                for line in matrix
            ],
        },
        parameters={"vmin": float(vmin), "vmax": float(vmax), "dpi": int(dpi)},
    )
