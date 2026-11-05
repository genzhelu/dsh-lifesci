"""``lifesci.stats`` — correlation, partial correlation, log-log regression, groups, outliers.

The command loads a small table (JSON / TSV / CSV, or inline rows) and reports
the relationship between two numeric variables:

* Pearson ``r`` and Spearman ``rho`` with n and p-values;
* a first-order **partial correlation** controlling for a nuisance variable
  (``control``); ``control='auto'`` uses an existing ``core`` column when one is
  present and otherwise reports no partial correlation, while ``control='core'``
  derives the control from ``core_from`` (``total_column`` minus
  ``segment_columns``) or an existing ``core`` column;
* a **log-log fit** ``log(y) = intercept + slope * log(x)``;
* per-group correlations when a grouping is available (auto-inferred from the
  identifier prefix, or from an explicit column);
* standardised-residual **outliers** from the log-log fit.

Mandatory caveats are always attached and emitted as warnings:
``CORRELATION_NOT_CAUSATION``, ``PHYLOGENETIC_NON_INDEPENDENCE`` (without a
control), and ``SMALL_N`` below the configured threshold.
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
    "oneOf": [{"required": ["table"]}, {"required": ["rows"]}],
    "properties": {
        "table": {
            "type": "string",
            "description": (
                "Path to a table: JSON (object-of-records keyed by name, list of records, or "
                "{'rows': [...]}), TSV, or CSV. Relative paths resolve against ctx.cwd."
            ),
        },
        "rows": {
            "type": "array",
            "items": {"type": "object", "additionalProperties": True},
            "description": "Inline records (alternative to table).",
        },
        "id_column": {
            "type": "string",
            "description": (
                "Column used as the point identifier/label. Default: JSON object keys, else "
                "the first of name/id/key/label/organism present, else row_NNNN."
            ),
        },
        "x": {
            "type": "string",
            "description": "Numeric column for the x variable. Log-log fit uses log(x).",
        },
        "y": {
            "type": "string",
            "description": "Numeric column for the y variable. Log-log fit uses log(y).",
        },
        "control": {
            "type": ["string", "null"],
            "description": (
                "Column for the partial correlation controlling variable, or the special value "
                "'core'. Default 'auto': use an existing 'core' column if present, else no partial "
                "correlation. 'core' without an existing column requires 'core_from' "
                "(total_column + segment_columns). Null disables the partial correlation."
            ),
        },
        "core_from": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "total_column": {
                    "type": "string",
                    "description": "Column holding the full (core + segment) length.",
                },
                "segment_columns": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Segment columns subtracted from the total column to give the core value.",
                },
            },
            "description": "Definition used when control='core': core = total - sum(segment columns).",
        },
        "filter": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "name_prefix": { "type": "string", "description": "Keep only ids starting with this prefix." },
                "name_suffix": { "type": "string", "description": "Keep only ids ending with this suffix." },
                "name_contains": { "type": "string", "description": "Keep only ids containing this substring." },
                "name_regex": {
                    "type": "string",
                    "description": (
                        "Keep only ids matching this Python regular expression."
                    ),
                },
                "ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Keep only these exact ids.",
                },
                "columns": {
                    "type": "object",
                    "additionalProperties": True,
                    "description": (
                        "Column filters: keys are column names, values are a literal or list of "
                        "allowed literal values (compared as strings)."
                    ),
                },
            },
            "description": "Row selection applied before correlation (all listed conditions must match).",
        },
        "group_by": {
            "type": "string",
            "description": (
                "Column used to split per-group correlations. Default 'auto' groups by the "
                "identifier prefix before the first '_' or '-' (lowercased); 'none' disables "
                "groups; any other value must be an existing column name."
            ),
        },
        "small_n_threshold": {
            "type": "integer",
            "minimum": 3,
            "maximum": 10000,
            "default": 100,
            "description": (
                "Emit the SMALL_N caveat when the number of valid points is below this "
                "(default 100: cross-species correlations with dozens of species are small)."
            ),
        },
        "outlier_z": {
            "type": "number",
            "minimum": 0.5,
            "maximum": 10,
            "default": 2.0,
            "description": "Absolute standardised-residual cutoff used to label outliers.",
        },
        "include_points": {
            "type": "boolean",
            "default": True,
            "description": "Include the per-point (label, x, y, group, control) table in the payload.",
        },
        "out": {
            "type": "string",
            "description": "Optional path to write the statistics payload JSON.",
        },
    },
    "required": ["x", "y"],
}



def _normalise_row_id(row: dict[str, Any], index: int, id_column: str | None) -> tuple[str, dict[str, Any]]:
    record = dict(row)
    if id_column and record.get(id_column) not in (None, ""):
        return str(record[id_column]), record
    for candidate in ("name", "id", "key", "label", "organism"):
        value = record.get(candidate)
        if value not in (None, ""):
            return str(value), record
    return f"row_{index:04d}", record


def _load_table(path: Path) -> tuple[list[dict[str, Any]], str, str | None]:
    """Load JSON/TSV/CSV into records; returns (rows, format, json_key_column)."""
    suffix = path.suffix.lower()
    if suffix == ".json":
        payload = util.read_json(path)
        if isinstance(payload, dict) and isinstance(payload.get("rows"), list):
            return [dict(row) for row in payload["rows"] if isinstance(row, dict)], "json", None
        if isinstance(payload, dict):
            rows = []
            for key in sorted(payload):
                record = payload[key]
                if not isinstance(record, dict):
                    continue
                record = dict(record)
                record.setdefault("name", key)
                rows.append(record)
            return rows, "json", "name"
        if isinstance(payload, list):
            return [dict(row) for row in payload if isinstance(row, dict)], "json", None
        raise DataError(
            f"unsupported JSON table layout in {path}: expected object-of-records, list, or {{'rows': [...]}}",
            details={"path": str(path)},
        )
    delimiter = "\t" if suffix in {".tsv", ".tab", ".txt"} else ","
    text = util.read_text(path)
    rows: list[dict[str, Any]] = []
    reader = csv.DictReader(text.splitlines(), delimiter=delimiter)
    for raw in reader:
        rows.append({str(key): value for key, value in raw.items() if key is not None})
    if not rows:
        raise DataError(f"table contains no rows: {path}", details={"path": str(path)})
    return rows, "tsv" if delimiter == "\t" else "csv", None


def _to_float(value: Any) -> float | None:
    number = util.parse_float(value)
    if number is None or not math.isfinite(number):
        return None
    return float(number)


def _column_values(
    rows: list[dict[str, Any]], column: str
) -> list[float | None]:
    return [_to_float(row.get(column)) for row in rows]


def _missing_column(column: str, available: list[str]) -> InvalidInputError:
    return InvalidInputError(
        f"column {column!r} does not exist in the table",
        code="COLUMN_MISSING",
        details={"column": column, "available_columns": available},
    )


def _infer_group(label: str) -> str:
    """Infer a group from the identifier prefix before the first ``_`` or ``-``.

    Used only by ``group_by='auto'``: ``sample_A_01`` -> ``sample``,
    ``species-2`` -> ``species``; a label with neither separator is its own
    lowercased group name.
    """
    text = str(label)
    for separator in ("_", "-"):
        index = text.find(separator)
        if index > 0:
            return text[:index].lower()
    return text.lower()


def _match_filter(identifier: str, row: dict[str, Any], filt: dict[str, Any]) -> bool:
    prefix = filt.get("name_prefix")
    if prefix and not identifier.startswith(str(prefix)):
        return False
    suffix = filt.get("name_suffix")
    if suffix and not identifier.endswith(str(suffix)):
        return False
    contains = filt.get("name_contains")
    if contains and str(contains) not in identifier:
        return False
    name_regex = filt.get("name_regex")
    if name_regex:
        import re

        if re.search(str(name_regex), identifier) is None:
            return False
    ids = filt.get("ids")
    if ids and identifier not in {str(value) for value in ids}:
        return False
    columns = filt.get("columns") or {}
    for column, expected in columns.items():
        actual = row.get(column)
        if isinstance(expected, list):
            if str(actual) not in {str(value) for value in expected}:
                return False
        elif str(actual) != str(expected):
            return False
    return True


def _record_local(ctx: Any, path: Path, *, role: str = "table") -> None:
    from .. import provenance as provenance_mod

    if not path.is_file():
        return
    log = getattr(ctx, "_provenance_log", None)
    if log is None:
        log = provenance_mod.ProvenanceLog(ctx.log_path)
        ctx._provenance_log = log
    entry = log.local_file(
        path=path, sha256=util.sha256_file(path), bytes_=path.stat().st_size, notes={"role": role}
    )
    ctx.provenance.append(entry)


def _resolve_control(
    args: dict[str, Any],
    rows: list[dict[str, Any]],
    columns: list[str],
    ctx: Any,
) -> tuple[str | None, list[float] | None, dict[str, Any] | None]:
    """Resolve the partial-correlation control column (explicit, 'core', or auto).

    ``control='auto'`` uses an existing ``core`` column when available and
    otherwise reports no partial correlation (it never guesses a definition).
    ``control='core'`` uses an existing ``core`` column, or derives it from
    ``core_from`` (``total_column`` and ``segment_columns``) and raises a typed
    error when neither is available.
    """
    raw_control = args.get("control", "auto")
    if raw_control is None:
        return None, None, None
    control_name = str(raw_control)
    core_from = args.get("core_from") or {}

    def existing_core() -> tuple[str, list[float], dict[str, Any]] | None:
        if "core" not in columns:
            return None
        values = _column_values(rows, "core")
        if not any(value is not None for value in values):
            return None
        cleaned = [float(value) if value is not None else float("nan") for value in values]
        return "core", cleaned, {"name": "core", "definition": "column 'core'"}

    def derive_core() -> tuple[str, list[float], dict[str, Any]]:
        total_column = str(core_from.get("total_column") or "").strip()
        segment_columns = [str(column) for column in (core_from.get("segment_columns") or [])]
        if not total_column or not segment_columns:
            raise InvalidInputError(
                "control='core' needs either an existing 'core' column or 'core_from' with "
                "'total_column' and 'segment_columns'",
                code="CORE_DEFINITION_MISSING",
                details={
                    "control": control_name,
                    "available_columns": columns,
                    "core_from": dict(core_from),
                },
                fallbacks=[
                    "add a numeric 'core' column to the table",
                    "pass core_from={'total_column': ..., 'segment_columns': [...]}",
                ],
            )
        if total_column not in columns:
            raise InvalidInputError(
                f"cannot derive 'core': total column {total_column!r} is missing",
                code="COLUMN_MISSING",
                details={"column": total_column, "available_columns": columns},
            )
        missing = [column for column in segment_columns if column not in columns]
        if missing:
            raise InvalidInputError(
                "cannot derive 'core': segment columns are missing",
                code="COLUMN_MISSING",
                details={"missing": missing, "available_columns": columns},
            )
        values: list[float] = []
        for row in rows:
            total_value = _to_float(row.get(total_column))
            segment_values = [_to_float(row.get(column)) for column in segment_columns]
            if total_value is None or any(value is None for value in segment_values):
                values.append(float("nan"))
                continue
            values.append(float(total_value - sum(value for value in segment_values if value is not None)))
        return "core", values, {
            "name": "core",
            "definition": f"{total_column} - (" + " + ".join(segment_columns) + ")",
            "total_column": total_column,
            "segment_columns": segment_columns,
        }

    if control_name == "auto":
        resolved = existing_core()
        return resolved if resolved is not None else (None, None, None)
    if control_name == "core":
        resolved = existing_core()
        return resolved if resolved is not None else derive_core()

    if control_name not in columns:
        raise _missing_column(control_name, columns)
    values = _column_values(rows, control_name)
    if not any(value is not None for value in values):
        raise InvalidInputError(
            f"control column {control_name!r} contains no numeric values",
            code="COLUMN_NOT_NUMERIC",
            details={"column": control_name},
        )
    cleaned = [float(value) if value is not None else float("nan") for value in values]
    return control_name, cleaned, {"name": control_name, "definition": "column"}


def _summary(pearson: Any, spearman: Any, partial: Any, loglog: Any, n: int) -> dict[str, Any]:
    def _number(container: Any, key: str) -> float | None:
        if isinstance(container, dict):
            return container.get(key)
        return None

    return {
        "n": n,
        "pearson_r": _number(pearson, "r"),
        "spearman_rho": _number(spearman, "rho"),
        "partial_r": _number(partial, "r"),
        "loglog_slope": _number(loglog, "slope"),
        "loglog_r2": _number(loglog, "r2"),
    }


@command(
    name="stats",
    title="Correlation, partial correlation, and log-log regression",
    summary="Compute Pearson/Spearman/partial correlations, a log-log fit, group trends, and outliers.",
    description=(
        "Statistical association between two numeric columns of a small table. Reports n, "
        "Pearson r (with p), Spearman rho (with p), an optional partial correlation controlling "
        "for a nuisance variable (control='auto' uses an existing 'core' column; control='core' "
        "derives it from core_from, i.e. total_column minus segment_columns), a log-log fit "
        "log(y)=a+slope*log(x), per-group correlations, and standardised-residual outliers. "
        "group_by='auto' groups identifiers by the prefix before the first '_' or '-'. Always "
        "emits CORRELATION_NOT_CAUSATION and PHYLOGENETIC_NON_INDEPENDENCE caveats, plus SMALL_N "
        "below the threshold. Row filters: name_prefix, name_suffix, name_contains, name_regex, "
        "ids, and per-column value filters."
    ),
    input_schema=INPUT_SCHEMA,
    output_schema="stats.schema.json",
    category="statistics",
    timeout_ms=120_000,
    examples=(
        {
            "rows": [
                {"name": "sample_a", "x": 4.0, "y": 9.1},
                {"name": "sample_b", "x": 6.4, "y": 15.8},
                {"name": "sample_c", "x": 8.1, "y": 22.5},
                {"name": "sample_d", "x": 10.2, "y": 30.4},
            ],
            "x": "x",
            "y": "y",
        },
        {
            "table": "tables/measurements.tsv",
            "x": "length",
            "y": "score",
            "group_by": "none",
        },
    ),
    danger="read",
)
def run(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
    ctx.validate()
    from ..lib import stats as stats_lib

    x_name = str(args.get("x") or "")
    y_name = str(args.get("y") or "")
    if not x_name or not y_name:
        raise InvalidInputError("stats requires non-empty 'x' and 'y' column names")

    table_path: Path | None = None
    if args.get("table") is not None:
        table_path = Path(str(args["table"])).expanduser()
        if not table_path.is_absolute():
            table_path = (Path(ctx.cwd) / table_path).resolve()
        if not table_path.is_file():
            raise DataError(
                f"table file does not exist: {table_path}",
                code="TABLE_NOT_FOUND",
                details={"path": str(table_path)},
            )
        rows, table_format, json_key_column = _load_table(table_path)
        _record_local(ctx, table_path, role="stats_table")
        table_sha = util.sha256_file(table_path)
        table_bytes = table_path.stat().st_size
    else:
        inline = args.get("rows")
        if not isinstance(inline, list):
            raise InvalidInputError("stats requires either 'table' or 'rows'")
        rows = [dict(row) for row in inline if isinstance(row, dict)]
        table_format = "inline"
        json_key_column = None
        table_sha = None
        table_bytes = None
    if not rows:
        raise DataError("stats table contains no records", code="INSUFFICIENT_DATA")

    # Attach ids ------------------------------------------------------------
    id_column = args.get("id_column") or json_key_column
    labelled: list[dict[str, Any]] = []
    columns: list[str] = sorted({str(key) for row in rows for key in row})
    for index, row in enumerate(rows):
        identifier, record = _normalise_row_id(row, index, id_column)
        record = dict(record)
        record["__id__"] = identifier
        labelled.append(record)
    labels = [str(record["__id__"]) for record in labelled]
    if id_column is None:
        for candidate in ("name", "id", "key", "label", "organism"):
            if candidate in columns:
                id_column = candidate
                break
    id_column = id_column or "__id__"

    # Filter ----------------------------------------------------------------
    filt = dict(args.get("filter") or {})
    selected = [
        (label, record)
        for label, record in zip(labels, labelled)
        if _match_filter(label, record, filt)
    ]
    if not selected:
        raise DataError(
            "no rows remain after applying the filter",
            code="NO_ROWS_AFTER_FILTER",
            details={"filter": filt, "n_input_rows": len(rows)},
        )
    labels = [label for label, _ in selected]
    selected_rows = [record for _, record in selected]
    if len(selected_rows) != len(rows) and filt:
        ctx.warn(
            "DATA_FILTERED",
            f"Filter selected {len(selected_rows)} of {len(rows)} rows; the reported statistics "
            "apply only to the selected subset.",
            severity="info",
            context={"filter": filt, "n_selected": len(selected_rows), "n_input_rows": len(rows)},
        )

    available = sorted({str(key) for row in selected_rows for key in row} - {"__id__"})
    if x_name not in available:
        raise _missing_column(x_name, available)
    if y_name not in available:
        raise _missing_column(y_name, available)
    x_values = _column_values(selected_rows, x_name)
    y_values = _column_values(selected_rows, y_name)

    numeric_rows = [
        (label, record, xv, yv)
        for label, record, xv, yv in zip(labels, selected_rows, x_values, y_values)
        if xv is not None and yv is not None
    ]
    if len(numeric_rows) < 3:
        raise DataError(
            f"need at least 3 rows with numeric {x_name!r} and {y_name!r}; got {len(numeric_rows)}",
            code="INSUFFICIENT_DATA",
            details={"n": len(numeric_rows), "x": x_name, "y": y_name},
        )
    dropped = len(selected_rows) - len(numeric_rows)
    if dropped:
        ctx.warn(
            "ROWS_DROPPED_NON_NUMERIC",
            f"{dropped} row(s) were dropped because {x_name!r}/{y_name!r} were not numeric.",
            context={"n_dropped": dropped, "x": x_name, "y": y_name},
        )
    labels = [label for label, _, _, _ in numeric_rows]
    selected_rows = [record for _, record, _, _ in numeric_rows]
    x_values = [xv for _, _, xv, _ in numeric_rows]
    y_values = [yv for _, _, _, yv in numeric_rows]

    # Control (partial correlation) ----------------------------------------
    control_name, control_values, control_definition = _resolve_control(
        args, selected_rows, available, ctx
    )

    # Groups ----------------------------------------------------------------
    group_by = str(args.get("group_by", "auto"))
    group_values: list[str] | None = None
    if group_by == "auto":
        group_values = [_infer_group(label) for label in labels]
    elif group_by in {"none", "null", ""}:
        group_values = None
    else:
        if group_by not in available:
            raise _missing_column(group_by, available)
        group_values = [
            "" if record.get(group_by) is None else str(record.get(group_by))
            for record in selected_rows
        ]

    # Report ----------------------------------------------------------------
    small_n_threshold = int(args.get("small_n_threshold", 100))
    outlier_z = float(args.get("outlier_z", 2.0))
    try:
        report = stats_lib.correlation_report(
            x_values,
            y_values,
            labels=labels,
            groups=group_values,
            control=control_values,
            x_name=x_name,
            y_name=y_name,
            control_name=(control_definition or {}).get("name", "control"),
            ctx=ctx,
            small_n_threshold=small_n_threshold,
            outlier_z=outlier_z,
        )
    except DataError as err:
        # The log-log fit needs >=3 strictly positive pairs; a generic table may not
        # provide them. Fall back to the non-loglog statistics so the command still
        # reports a useful correlation (with an explicit warning).
        if err.code != "DATA_INVALID" or "log-log" not in err.message:
            raise
        ctx.warn(
            "LOGLOG_FIT_SKIPPED",
            "Log-log regression was skipped: fewer than 3 strictly positive (x, y) pairs.",
            context={"x": x_name, "y": y_name},
        )
        pearson_r = stats_lib.pearson(x_values, y_values)
        spearman_rho = stats_lib.spearman(x_values, y_values)
        partial_r = (
            stats_lib.partial_correlation(x_values, y_values, control_values)
            if control_values is not None
            else None
        )
        report = {
            "variables": {"x": x_name, "y": y_name, "control": (control_definition or {}).get("name")},
            "n": len(x_values),
            "pearson": {"r": None if math.isnan(pearson_r) else round(pearson_r, 4), "p": None, "n": len(x_values)},
            "spearman": {"rho": None if math.isnan(spearman_rho) else round(spearman_rho, 4), "p": None, "n": len(x_values)},
            "partial": None
            if partial_r is None or math.isnan(partial_r)
            else {"r": round(partial_r, 4), "n": len(x_values), "controlled_for": (control_definition or {}).get("name")},
            "loglog": None,
            "groups": [],
            "outliers": [],
            "caveats": [
                {
                    "code": "CORRELATION_NOT_CAUSATION",
                    "message": "A correlation is not evidence of causation.",
                }
            ],
            "data": [
                {"label": label, "x": xv, "y": yv, "group": None, "control": None}
                for label, xv, yv in zip(labels, x_values, y_values)
            ],
        }
        ctx.warn(
            "CORRELATION_NOT_CAUSATION",
            "A correlation is not evidence of causation; report n and the fitted model.",
        )
        ctx.warn(
            "PHYLOGENETIC_NON_INDEPENDENCE",
            "Cross-species points are not statistically independent; p-values are anti-conservative.",
        )

    # Ensure the mandatory caveats always appear.  The library emits
    # PHYLOGENETIC_NON_INDEPENDENCE only when no control variable is supplied,
    # but even a core-controlled partial correlation leaves shared ancestry.
    caveats = list(report.get("caveats") or [])
    present_codes = {str(caveat.get("code")) for caveat in caveats}
    if "CORRELATION_NOT_CAUSATION" not in present_codes:
        note = {
            "code": "CORRELATION_NOT_CAUSATION",
            "message": "A correlation is not evidence of causation; report the coefficient and n.",
        }
        caveats.append(note)
        ctx.warn(note["code"], note["message"])
    if "PHYLOGENETIC_NON_INDEPENDENCE" not in present_codes:
        note = {
            "code": "PHYLOGENETIC_NON_INDEPENDENCE",
            "message": (
                "Cross-species observations are not statistically independent; even a "
                "core-controlled partial correlation does not remove shared ancestry, so "
                "p-values are anti-conservative. Use phylogenetically independent contrasts "
                "for a formal test."
            ),
        }
        caveats.append(note)
        ctx.warn(note["code"], note["message"])
    report["caveats"] = caveats

    variables = dict(report.get("variables") or {})
    variables["control_definition"] = control_definition
    payload: dict[str, Any] = {
        "table": {
            "path": str(table_path) if table_path else None,
            "format": table_format,
            "sha256": table_sha,
            "bytes": table_bytes,
            "n_input_rows": len(rows),
            "n_selected": len(selected_rows),
            "n_used": report.get("n"),
            "id_column": id_column,
            "filter": filt or None,
        },
        "variables": variables,
        "n": report.get("n"),
        "pearson": report.get("pearson"),
        "spearman": report.get("spearman"),
        "partial": report.get("partial"),
        "loglog": report.get("loglog"),
        "groups": report.get("groups") or [],
        "outliers": report.get("outliers") or [],
        "caveats": [caveat.get("code") for caveat in report.get("caveats") or []],
        "caveat_notes": report.get("caveats") or [],
        "summary": _summary(
            report.get("pearson"),
            report.get("spearman"),
            report.get("partial"),
            report.get("loglog"),
            int(report.get("n") or 0),
        ),
    }
    if args.get("include_points", True):
        payload["points"] = report.get("data") or []

    out_path = args.get("out")
    if out_path and not bool(ctx.dry_run):
        target = Path(str(out_path)).expanduser()
        if not target.is_absolute():
            target = (Path(ctx.cwd) / target).resolve()
        util.atomic_write_json(target, payload)
        payload["out"] = str(target)
    return payload
