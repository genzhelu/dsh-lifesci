"""``lifesci.figures`` — quantitative figures (matplotlib Agg) and PyMOL session builds.

Two deliverables are supported:

* **Quantitative figures** rendered with matplotlib's Agg backend: a log-log
  covariance scatter with regression line and annotated outliers, a length
  histogram, and a contact-distance bar chart.
  Data come from a table (JSON/TSV/CSV), inline rows, or a saved ``lifesci.stats``
  payload; the source file hash and regression parameters are recorded so a
  figure can be regenerated and audited (``FIGURE_DATA_PROVENANCE``).
* **PyMOL sessions** through :mod:`lifesci.lib.pymol_session`: the (opaque)
  session spec is passed through; the library writes a ``.pml`` script, runs
  ``pymol -cq`` when available to validate/export a session, and reports an
  honest ``PASS``/``FAIL``/``SKIPPED`` validation.  When PyMOL is absent the
  command emits ``PYMOL_MISSING`` and the ``.pml`` fallback.  Full-session headless
  PNG rendering is known to fail (183 MB / 403k atoms rendered nothing), so
  ``PNG_RENDER_LIMITATION`` is raised when rendering was requested but did not
  produce an image — render small extracted structures instead.

Deterministic rendering: fixed figsize/dpi, no annotations of wall-clock time.
Every written figure is returned with its SHA-256 and byte count.
"""

from __future__ import annotations

import csv
import math
from pathlib import Path
from typing import Any

from .. import util
from ..commands.base import command
from ..errors import DataError, InvalidInputError

INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "figures": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["kind", "data"],
                "properties": {
                    "kind": {
                        "type": "string",
                        "enum": ["covariance_loglog", "length_histogram", "contact_bars"],
                        "description": (
                            "'covariance_loglog': log-log scatter with regression + annotated "
                            "outliers; 'length_histogram': distribution of one numeric column; "
                            "'contact_bars': bar chart of one value column by label (e.g. "
                            "contact minimum distances)."
                        ),
                    },
                    "data": {
                        "type": "object",
                        "additionalProperties": False,
                        "description": "Figure data source: one of table/rows/stats plus an optional row filter.",
                        "properties": {
                            "table": {
                                "type": "string",
                                "description": "Path to a JSON/TSV/CSV table (relative paths resolve against ctx.cwd).",
                            },
                            "rows": {
                                "type": "array",
                                "items": {"type": "object", "additionalProperties": True},
                                "description": "Inline records (alternative to table).",
                            },
                            "stats": {
                                "type": "string",
                                "description": (
                                    "Path to a saved lifesci.stats payload/envelope; its 'points' "
                                    "are used as the figure rows."
                                ),
                            },
                            "filter": {
                                "type": "object",
                                "additionalProperties": True,
                                "description": "Optional name_prefix/name_regex/ids row filter (same shape as lifesci.stats).",
                            },
                        },
                    },
                    "x": {"type": "string", "description": "covariance_loglog: x column (required)."},
                    "y": {"type": "string", "description": "covariance_loglog: y column (required)."},
                    "column": {"type": "string", "description": "length_histogram: numeric column to plot."},
                    "value_column": {"type": "string", "description": "contact_bars: numeric value column (e.g. 'min_distance')."},
                    "label_column": {"type": "string", "description": "contact_bars: category label column (e.g. 'pair'); default: row id."},
                    "group_by": {
                        "type": "string",
                        "description": (
                            "covariance_loglog: column used for colouring. 'auto' groups by the "
                            "identifier prefix before the first '_' or '-'; 'none' disables grouping."
                        ),
                    },
                    "title": {"type": "string", "description": "Figure title."},
                    "xlabel": {"type": "string", "description": "X-axis label."},
                    "ylabel": {"type": "string", "description": "Y-axis label."},
                    "filename": {"type": "string", "description": "Output PNG filename (default derived from kind + index)."},
                    "regression": {"type": "boolean", "default": True, "description": "covariance_loglog: draw the log-log regression line."},
                    "annotate_outliers": {"type": "boolean", "default": True, "description": "covariance_loglog: annotate standardised-residual outliers."},
                    "bins": {"type": "integer", "minimum": 2, "maximum": 200, "default": 30, "description": "length_histogram: number of bins."},
                },
            },
            "description": "Figures to render.",
        },
        "out_dir": {
            "type": "string",
            "description": "Directory for generated figures (default: <ctx.cwd>/lifesci-figures).",
        },
        "pymol_spec": {
            "type": "object",
            "additionalProperties": True,
            "description": (
                "Opaque PyMOL session spec passed verbatim to lifesci.lib.pymol_session: "
                "{name, objects:[{path, name?, load_type?, color?, selections?}], commands?, "
                "scenes?, save_pse?, render_png?:{path,width,height,dpi}}."
            ),
        },
        "pymol_out_dir": {
            "type": "string",
            "description": "Directory for the generated .pml/.pse session (default: <out_dir>/pymol).",
        },
    },
    "anyOf": [{"required": ["figures"]}, {"required": ["pymol_spec"]}],
}


# ---------------------------------------------------------------------------
# data loading
# ---------------------------------------------------------------------------


def _infer_id(record: dict[str, Any], index: int) -> str:
    for candidate in ("name", "id", "key", "label", "organism"):
        value = record.get(candidate)
        if value not in (None, ""):
            return str(value)
    return f"row_{index:04d}"


def _load_rows_from_path(path: Path) -> list[dict[str, Any]]:
    suffix = path.suffix.lower()
    if suffix == ".json":
        payload = util.read_json(path)
        if isinstance(payload, dict) and isinstance(payload.get("points"), list):
            rows = payload["points"]
        elif isinstance(payload, dict) and isinstance(payload.get("rows"), list):
            rows = payload["rows"]
        elif isinstance(payload, dict) and isinstance(payload.get("data"), dict) and isinstance(payload["data"].get("points"), list):
            rows = payload["data"]["points"]
        elif isinstance(payload, dict):
            rows = []
            for key in sorted(payload):
                value = payload[key]
                if isinstance(value, dict):
                    record = dict(value)
                    record.setdefault("name", key)
                    rows.append(record)
        elif isinstance(payload, list):
            rows = payload
        else:
            raise DataError(f"unsupported JSON table layout: {path}", details={"path": str(path)})
    else:
        delimiter = "\t" if suffix in {".tsv", ".tab", ".txt"} else ","
        reader = csv.DictReader(util.read_text(path).splitlines(), delimiter=delimiter)
        rows = [{str(key): value for key, value in raw.items() if key is not None} for raw in reader]
    return [dict(row) for row in rows if isinstance(row, dict)]


def _match_filter(identifier: str, record: dict[str, Any], filt: dict[str, Any]) -> bool:
    prefix = filt.get("name_prefix")
    if prefix and not identifier.startswith(str(prefix)):
        return False
    contains = filt.get("name_contains")
    if contains and str(contains) not in identifier:
        return False
    regex = filt.get("name_regex")
    if regex:
        import re

        if re.search(str(regex), identifier) is None:
            return False
    ids = filt.get("ids")
    if ids and identifier not in {str(value) for value in ids}:
        return False
    return True


def _load_figure_data(ctx: Any, spec: dict[str, Any], sources: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], str]:
    """Load rows for one figure spec; returns (rows, source description)."""
    data = dict(spec.get("data") or {})
    consumed = [key for key in ("table", "rows", "stats") if data.get(key) is not None]
    if len([key for key in consumed if key != "filter"]) == 0:
        raise InvalidInputError(
            "figure 'data' needs one of 'table', 'rows', or 'stats'",
            details={"kind": spec.get("kind")},
        )
    if data.get("rows") is not None:
        rows = [dict(row) for row in data["rows"] if isinstance(row, dict)]
        source = "inline rows"
    elif data.get("stats") is not None:
        path = Path(str(data["stats"])).expanduser()
        if not path.is_absolute():
            path = (Path(ctx.cwd) / path).resolve()
        if not path.is_file():
            raise DataError(f"stats file does not exist: {path}", code="TABLE_NOT_FOUND", details={"path": str(path)})
        rows = _load_rows_from_path(path)
        source = str(path)
        sources.append(
            {
                "path": str(path),
                "sha256": util.sha256_file(path),
                "bytes": path.stat().st_size,
                "role": "stats",
            }
        )
        _record_local(ctx, path, role="figures_stats")
    else:
        path = Path(str(data["table"])).expanduser()
        if not path.is_absolute():
            path = (Path(ctx.cwd) / path).resolve()
        if not path.is_file():
            raise DataError(f"table file does not exist: {path}", code="TABLE_NOT_FOUND", details={"path": str(path)})
        rows = _load_rows_from_path(path)
        source = str(path)
        sources.append(
            {
                "path": str(path),
                "sha256": util.sha256_file(path),
                "bytes": path.stat().st_size,
                "role": "table",
            }
        )
        _record_local(ctx, path, role="figures_table")
    records: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        record = dict(row)
        record.setdefault("__id__", _infer_id(record, index))
        records.append(record)
    filt = dict(data.get("filter") or {})
    if filt:
        records = [record for record in records if _match_filter(str(record.get("__id__")), record, filt)]
    if not records:
        raise DataError("figure data selection is empty after filtering", code="NO_ROWS_AFTER_FILTER")
    return records, source


def _numeric(value: Any) -> float | None:
    number = util.parse_float(value)
    if number is None or not math.isfinite(number):
        return None
    return float(number)


def _record_local(ctx: Any, path: str | Path, *, role: str) -> None:
    from .. import provenance as provenance_mod

    target = Path(path)
    if not target.is_file():
        return
    log = getattr(ctx, "_provenance_log", None)
    if log is None:
        log = provenance_mod.ProvenanceLog(ctx.log_path)
        ctx._provenance_log = log
    entry = log.local_file(
        path=target, sha256=util.sha256_file(target), bytes_=target.stat().st_size, notes={"role": role}
    )
    ctx.provenance.append(entry)


def _figure_record(kind: str, path: Path, *, n_points: int, regression: Any = None, title: str | None = None) -> dict[str, Any]:
    return {
        "kind": kind,
        "path": str(path),
        "sha256": util.sha256_file(path),
        "bytes": path.stat().st_size,
        "n_points": int(n_points),
        "regression": util.json_safe(regression) if regression is not None else None,
        "title": title,
    }


def _ensure_matplotlib() -> Any:
    """Import pyplot with the headless Agg backend (MPLCONFIGDIR set by Context)."""
    import matplotlib

    matplotlib.use("Agg", force=True)
    import matplotlib.pyplot as plt

    return plt


# ---------------------------------------------------------------------------
# figure kinds
# ---------------------------------------------------------------------------


def _render_covariance_loglog(
    ctx: Any,
    spec: dict[str, Any],
    rows: list[dict[str, Any]],
    out_path: Path,
) -> dict[str, Any]:
    from ..lib import figures as figures_lib
    from .stats import _infer_group

    x_name = str(spec.get("x") or "").strip()
    y_name = str(spec.get("y") or "").strip()
    if not x_name or not y_name:
        raise InvalidInputError(
            "covariance_loglog figures require explicit 'x' and 'y' column names",
            code="COLUMN_SPEC_MISSING",
            details={"x": x_name or None, "y": y_name or None},
        )
    available_columns = {str(key) for record in rows for key in record}
    missing_columns = [name for name in (x_name, y_name) if name not in available_columns]
    if missing_columns:
        raise InvalidInputError(
            f"column(s) {missing_columns} do not exist in the figure data",
            code="COLUMN_MISSING",
            details={"missing": missing_columns, "available_columns": sorted(available_columns)},
        )
    points: list[dict[str, Any]] = []
    point_records: list[dict[str, Any]] = []
    for record in rows:
        xv = _numeric(record.get(x_name))
        yv = _numeric(record.get(y_name))
        if xv is None or yv is None or xv <= 0.0 or yv <= 0.0:
            continue
        points.append(
            {
                "label": str(record.get("__id__")),
                x_name: xv,
                y_name: yv,
                "group": None,
            }
        )
        point_records.append(record)
    dropped = len(rows) - len(points)
    if dropped:
        ctx.warn(
            "FIGURE_POINTS_DROPPED",
            f"{dropped} row(s) without strictly positive {x_name}/{y_name} were dropped from the "
            "log-log figure (log is undefined for non-positive values).",
            severity="info",
            context={"n_dropped": dropped, "x": x_name, "y": y_name},
        )
    if len(points) < 3:
        raise DataError(
            f"need at least 3 strictly positive {x_name}/{y_name} points for a log-log figure; "
            f"got {len(points)}",
            code="INSUFFICIENT_DATA",
        )
    group_by = str(spec.get("group_by", "auto"))
    group_column = None
    if group_by not in {"none", "null", ""}:
        if group_by == "auto":
            group_column = "group"
            for point in points:
                point["group"] = _infer_group(point["label"])
        else:
            group_column = group_by
            for point, record in zip(points, point_records):
                record_value = record.get(group_by)
                point["group"] = None if record_value is None else str(record_value)
    title = str(spec.get("title") or f"{y_name} vs {x_name} (log-log)")
    xlabel = str(spec.get("xlabel") or x_name)
    ylabel = str(spec.get("ylabel") or y_name)
    result = figures_lib.scatter_loglog(
        ctx,
        data=points,
        x=x_name,
        y=y_name,
        group=group_column,
        regression=bool(spec.get("regression", True)),
        annotate_outliers=bool(spec.get("annotate_outliers", True)),
        title=title,
        xlabel=xlabel,
        ylabel=ylabel,
        out_path=out_path,
    )
    record = _figure_record(
        "covariance_loglog",
        Path(str(result.get("path") or out_path)),
        n_points=int(result.get("n_points") or len(points)),
        regression=result.get("regression"),
        title=title,
    )
    record["path"] = str(result.get("path") or out_path)
    return record


def _render_length_histogram(
    ctx: Any,
    spec: dict[str, Any],
    rows: list[dict[str, Any]],
    out_path: Path,
) -> dict[str, Any]:
    plt = _ensure_matplotlib()
    column = str(spec.get("column") or "length")
    values = [_numeric(record.get(column)) for record in rows]
    values = [value for value in values if value is not None]
    if len(values) < 2:
        raise DataError(
            f"length_histogram needs at least 2 numeric values in column {column!r}",
            code="INSUFFICIENT_DATA",
            details={"column": column, "n": len(values)},
        )
    bins = int(spec.get("bins", 30))
    title = str(spec.get("title") or f"{column} distribution (n={len(values)})")
    xlabel = str(spec.get("xlabel") or column)
    ylabel = str(spec.get("ylabel") or "count")
    fig, ax = plt.subplots(figsize=(7.2, 4.6))
    try:
        ax.hist(values, bins=bins, color="#4c72b0", edgecolor="white", linewidth=0.6)
        ax.set_xlabel(xlabel)
        ax.set_ylabel(ylabel)
        ax.set_title(title, fontsize=11)
        ax.grid(axis="y", linestyle=":", linewidth=0.5, alpha=0.6)
        fig.tight_layout()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        fig.savefig(out_path, dpi=200)
    finally:
        plt.close(fig)
    return _figure_record("length_histogram", out_path, n_points=len(values), title=title)


def _render_contact_bars(
    ctx: Any,
    spec: dict[str, Any],
    rows: list[dict[str, Any]],
    out_path: Path,
) -> dict[str, Any]:
    plt = _ensure_matplotlib()
    value_column = str(spec.get("value_column") or "min_distance")
    label_column = spec.get("label_column")
    entries: list[tuple[str, float]] = []
    for record in rows:
        value = _numeric(record.get(value_column))
        if value is None:
            continue
        if label_column:
            label = str(record.get(label_column))
        else:
            label = str(record.get("__id__"))
        entries.append((label, value))
    if not entries:
        raise DataError(
            f"contact_bars needs numeric {value_column!r} values",
            code="INSUFFICIENT_DATA",
            details={"column": value_column},
        )
    entries.sort(key=lambda item: (item[1], item[0]))
    labels = [label for label, _ in entries]
    values = [value for _, value in entries]
    title = str(spec.get("title") or f"{value_column} by {label_column or 'row'}")
    fig, ax = plt.subplots(figsize=(max(6.0, 0.5 * len(labels) + 3.0), 4.6))
    try:
        positions = range(len(labels))
        bars = ax.bar(positions, values, color="#55a868", edgecolor="black", linewidth=0.4)
        for bar, value in zip(bars, values):
            ax.annotate(
                util.fmt_float(value),
                (bar.get_x() + bar.get_width() / 2.0, bar.get_height()),
                xytext=(0, 2),
                textcoords="offset points",
                ha="center",
                fontsize=7,
            )
        ax.set_xticks(list(positions))
        ax.set_xticklabels(labels, rotation=30, ha="right", fontsize=8)
        ax.set_xlabel(str(spec.get("xlabel") or label_column or "feature pair"))
        ax.set_ylabel(str(spec.get("ylabel") or value_column))
        ax.set_title(title, fontsize=11)
        ax.grid(axis="y", linestyle=":", linewidth=0.5, alpha=0.6)
        fig.tight_layout()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        fig.savefig(out_path, dpi=200)
    finally:
        plt.close(fig)
    return _figure_record("contact_bars", out_path, n_points=len(entries), title=title)


# ---------------------------------------------------------------------------
# PyMOL
# ---------------------------------------------------------------------------


def _ensure_provenance_timestamps(ctx: Any) -> None:
    """Fill ``time`` on provenance records appended directly by library helpers.

    The envelope schema requires every provenance record to carry ``kind`` and
    ``time``; library figure/report/PyMOL helpers append plain ``local_file``
    records without a timestamp, so normalise them before the envelope is built.
    """
    for entry in getattr(ctx, "provenance", []) or []:
        if isinstance(entry, dict) and "time" not in entry:
            entry["time"] = util.now_iso()


def _build_pymol(ctx: Any, spec: dict[str, Any], out_dir: Path) -> dict[str, Any]:
    import importlib

    try:
        pymol_lib = importlib.import_module("lifesci.lib.pymol_session")
    except ImportError as err:  # pragma: no cover - parallel-workstream guard
        from ..errors import ConfigError

        raise ConfigError(
            "PyMOL session generation requires python/lifesci/lib/pymol_session.py, which is "
            "not available in this checkout",
            details={"import_error": f"{type(err).__name__}: {err}"},
        ) from err
    result = pymol_lib.build_session(ctx, spec=spec, out_dir=out_dir)
    validation = result.get("validation") or {}
    warnings: list[Any] = list(result.get("warnings") or [])
    status = str(validation.get("status") or "SKIPPED")
    session = {
        "pml_path": result.get("pml_path"),
        "pse_path": result.get("pse_path"),
        "rendered": bool(result.get("rendered")),
        "validation": util.json_safe(validation),
    }
    existing_warning_codes = {str(warning.get("code")) for warning in ctx.warnings}
    if status == "SKIPPED" and "PYMOL_MISSING" not in existing_warning_codes:
        ctx.warn(
            "PYMOL_MISSING",
            "PyMOL was unavailable: the .pml script was written but the session could not be "
            "built or validated (validation status SKIPPED). Install PyMOL or use the .pml "
            "fallback in a PyMOL session.",
            context={"pml_path": result.get("pml_path")},
        )
    render_png = spec.get("render_png") or {}
    existing_warning_codes = {str(warning.get("code")) for warning in ctx.warnings}
    if render_png and not result.get("rendered") and "PNG_RENDER_LIMITATION" not in existing_warning_codes:
        ctx.warn(
            "PNG_RENDER_LIMITATION",
            "Headless full-session PNG rendering did not produce an image. This is a known "
            "limitation for large sessions (hundreds of thousands of atoms); render small "
            "extracted structures instead.",
            context={"spec_render_png": render_png, "validation": util.json_safe(validation)},
        )
    return {
        "session": session,
        "validation": util.json_safe(validation),
        "warnings": util.json_safe(warnings),
        "spec": util.json_safe(spec),
    }


@command(
    name="figures",
    title="Generate quantitative figures and PyMOL sessions",
    summary="Render log-log covariance / histogram / contact-bar figures with matplotlib Agg, and build PyMOL sessions.",
    description=(
        "Renders publication-style figures with the headless matplotlib Agg backend: a log-log "
        "covariance scatter with regression line and annotated outliers, a distribution "
        "histogram, or a contact-distance bar chart. Data may come from a "
        "table (JSON/TSV/CSV), inline rows, or a saved lifesci.stats payload; the source SHA-256 "
        "and regression parameters are recorded (FIGURE_DATA_PROVENANCE) so figures are "
        "reproducible. Optionally builds a PyMOL session from an opaque spec via "
        "lib.pymol_session: emits a .pml always, validates/renders with `pymol -cq` only when "
        "PyMOL is installed, and reports honest PASS/FAIL/SKIPPED. Warns PYMOL_MISSING when PyMOL "
        "is absent and PNG_RENDER_LIMITATION when headless full-session rendering fails (render "
        "small extracted structures instead)."
    ),
    input_schema=INPUT_SCHEMA,
    output_schema="figures.schema.json",
    category="visualisation",
    timeout_ms=600_000,
    examples=(
        {
            "figures": [
                {
                    "kind": "covariance_loglog",
                    "data": {
                        "rows": [
                            {"name": "sample_a", "length": 4.0, "score": 9.1},
                            {"name": "sample_b", "length": 6.4, "score": 15.8},
                            {"name": "sample_c", "length": 8.1, "score": 22.5},
                            {"name": "sample_d", "length": 10.2, "score": 30.4},
                        ]
                    },
                    "x": "length",
                    "y": "score",
                    "group_by": "none",
                    "filename": "covariance.png",
                }
            ]
        },
    ),
    danger="write",
)
def run(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
    ctx.validate()
    out_arg = args.get("out_dir")
    if out_arg:
        out_dir = Path(str(out_arg)).expanduser()
        if not out_dir.is_absolute():
            out_dir = (Path(ctx.cwd) / out_dir).resolve()
    else:
        out_dir = (Path(ctx.cwd) / "lifesci-figures").resolve()
    figure_specs = list(args.get("figures") or [])
    pymol_spec = args.get("pymol_spec")
    if not figure_specs and not pymol_spec:
        raise InvalidInputError("figures requires at least one figure spec or pymol_spec")

    payload: dict[str, Any] = {
        "out_dir": str(out_dir),
        "figures": [],
        "pymol": None,
        "sources": [],
        "dry_run": bool(ctx.dry_run),
    }
    if bool(ctx.dry_run):
        payload["planned"] = [
            {"kind": spec.get("kind"), "filename": spec.get("filename")} for spec in figure_specs
        ] + ([{"kind": "pymol_session"}] if pymol_spec else [])
        ctx.warn(
            "FIGURE_DRY_RUN",
            "Figures/sessions were planned but not generated because --dry-run was set.",
            severity="info",
        )
        return payload

    out_dir.mkdir(parents=True, exist_ok=True)
    sources: list[dict[str, Any]] = []
    for index, spec in enumerate(figure_specs):
        kind = str(spec.get("kind") or "")
        if kind not in {"covariance_loglog", "length_histogram", "contact_bars"}:
            raise InvalidInputError(
                f"unsupported figure kind {kind!r}",
                details={"kind": kind, "known": ["covariance_loglog", "length_histogram", "contact_bars"]},
            )
        rows, source = _load_figure_data(ctx, spec, sources)
        filename = str(spec.get("filename") or f"fig_{index + 1:02d}_{kind}.png")
        if not filename.lower().endswith(".png"):
            filename += ".png"
        out_path = out_dir / filename
        if kind == "covariance_loglog":
            record = _render_covariance_loglog(ctx, spec, rows, out_path)
        elif kind == "length_histogram":
            record = _render_length_histogram(ctx, spec, rows, out_path)
        else:
            record = _render_contact_bars(ctx, spec, rows, out_path)
        record["source"] = source
        payload["figures"].append(record)
        _record_local(ctx, record["path"], role="figure_output")
        ctx.warn(
            "FIGURE_DATA_PROVENANCE",
            f"Figure {record['path']!r} was rendered from {source!r} "
            f"({record.get('n_points')} points); regenerate it from the recorded source SHA-256.",
            context={
                "kind": kind,
                "path": record["path"],
                "sha256": record["sha256"],
                "source": source,
            },
        )
    payload["sources"] = sources

    if pymol_spec:
        pymol_dir_arg = args.get("pymol_out_dir")
        if pymol_dir_arg:
            pymol_out_dir = Path(str(pymol_dir_arg)).expanduser()
            if not pymol_out_dir.is_absolute():
                pymol_out_dir = (Path(ctx.cwd) / pymol_out_dir).resolve()
        else:
            pymol_out_dir = out_dir / "pymol"
        payload["pymol"] = _build_pymol(ctx, dict(pymol_spec), pymol_out_dir)
    _ensure_provenance_timestamps(ctx)
    return payload
