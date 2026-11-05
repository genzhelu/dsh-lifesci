"""Correlation, partial-correlation and log-log regression statistics.

Small, dependency-light helpers used by the ``lifesci.stats`` command and by
figure generation.  All functions are pure; numpy/scipy are imported inside the
functions so importing this module stays cheap.

Conventions
-----------
* Correlation coefficients are returned as plain ``float`` (``nan`` when the
  coefficient is undefined, e.g. zero variance); callers serialise ``nan`` as
  ``null`` via :func:`lifesci.util.json_safe`.
* :func:`loglog_fit` fits ``log(y) ~ log(x)`` (a power-law covariance model);
  non-positive or non-finite points are excluded from the fit (log undefined)
  and reported via ``n``.
* Display-bound floats are rounded only in :func:`correlation_report`; the
  primitive functions keep full precision.
"""

from __future__ import annotations

import math
from typing import Any, Mapping, Sequence

from ..errors import DataError
from ..util import json_safe


# ---------------------------------------------------------------------------
# input normalisation
# ---------------------------------------------------------------------------


def _clean_xy(x: Sequence[float], y: Sequence[float]) -> tuple[list[float], list[float]]:
    """Return the finite, pairwise-aligned ``(x, y)`` values."""
    if len(x) != len(y):
        raise DataError(
            f"x and y must have the same length (got {len(x)} and {len(y)})",
            details={"len_x": len(x), "len_y": len(y)},
        )
    xs: list[float] = []
    ys: list[float] = []
    for xv, yv in zip(x, y):
        fx = float(xv)
        fy = float(yv)
        if math.isfinite(fx) and math.isfinite(fy):
            xs.append(fx)
            ys.append(fy)
    return xs, ys


def _clean_xyz(
    x: Sequence[float], y: Sequence[float], z: Sequence[float]
) -> tuple[list[float], list[float], list[float]]:
    if not (len(x) == len(y) == len(z)):
        raise DataError(
            "x, y and z must have the same length",
            details={"len_x": len(x), "len_y": len(y), "len_z": len(z)},
        )
    xs: list[float] = []
    ys: list[float] = []
    zs: list[float] = []
    for xv, yv, zv in zip(x, y, z):
        fx, fy, fz = float(xv), float(yv), float(zv)
        if math.isfinite(fx) and math.isfinite(fy) and math.isfinite(fz):
            xs.append(fx)
            ys.append(fy)
            zs.append(fz)
    return xs, ys, zs


def _require_min(points: Sequence[Any], minimum: int, what: str = "points") -> None:
    if len(points) < minimum:
        raise DataError(
            f"{what}: need at least {minimum}, got {len(points)}",
            details={"n": len(points), "minimum": minimum},
        )


# ---------------------------------------------------------------------------
# correlation coefficients
# ---------------------------------------------------------------------------


def pearson(x: Sequence[float], y: Sequence[float]) -> float:
    """Pearson product-moment correlation coefficient ``r``.

    Returns ``nan`` when the coefficient is undefined (fewer than 2 valid
    points or zero variance in either variable).
    """
    from scipy import stats as _stats

    xs, ys = _clean_xy(x, y)
    if len(xs) < 2:
        return float("nan")
    if _zero_variance(xs) or _zero_variance(ys):
        return float("nan")
    return float(_stats.pearsonr(xs, ys).statistic)


def pearson_with_p(x: Sequence[float], y: Sequence[float]) -> dict[str, Any]:
    """Pearson ``r`` with its two-sided p-value and n (JSON-friendly)."""
    from scipy import stats as _stats

    xs, ys = _clean_xy(x, y)
    if len(xs) < 3 or _zero_variance(xs) or _zero_variance(ys):
        return {"r": None, "p": None, "n": len(xs)}
    result = _stats.pearsonr(xs, ys)
    return {"r": float(result.statistic), "p": float(result.pvalue), "n": len(xs)}


def spearman(x: Sequence[float], y: Sequence[float]) -> float:
    """Spearman rank correlation coefficient ``rho`` (``nan`` if undefined)."""
    from scipy import stats as _stats

    xs, ys = _clean_xy(x, y)
    if len(xs) < 2:
        return float("nan")
    if _zero_variance(xs) or _zero_variance(ys):
        return float("nan")
    return float(_stats.spearmanr(xs, ys).statistic)


def spearman_with_p(x: Sequence[float], y: Sequence[float]) -> dict[str, Any]:
    """Spearman ``rho`` with its two-sided p-value and n."""
    from scipy import stats as _stats

    xs, ys = _clean_xy(x, y)
    if len(xs) < 3 or _zero_variance(xs) or _zero_variance(ys):
        return {"rho": None, "p": None, "n": len(xs)}
    result = _stats.spearmanr(xs, ys)
    return {"rho": float(result.statistic), "p": float(result.pvalue), "n": len(xs)}


def _zero_variance(values: Sequence[float]) -> bool:
    return max(values) == min(values)


def partial_correlation(
    x: Sequence[float],
    y: Sequence[float],
    z: Sequence[float],
) -> float:
    """Partial Pearson correlation of ``x`` and ``y`` controlling for ``z``.

    Implements the textbook first-order formula

    ``r_xy|z = (r_xy - r_xz * r_yz) / sqrt((1 - r_xz^2) * (1 - r_yz^2))``

    which is how a first-order nuisance variable is controlled for.  Returns
    ``nan`` if any of the three pairwise correlations is undefined or if the
    denominator is zero.
    """
    xs, ys, zs = _clean_xyz(x, y, z)
    if len(xs) < 3:
        return float("nan")
    r_xy = pearson(xs, ys)
    r_xz = pearson(xs, zs)
    r_yz = pearson(ys, zs)
    if any(math.isnan(value) for value in (r_xy, r_xz, r_yz)):
        return float("nan")
    denominator = math.sqrt(max(0.0, (1.0 - r_xz**2) * (1.0 - r_yz**2)))
    if denominator == 0.0:
        return float("nan")
    return float((r_xy - r_xz * r_yz) / denominator)


def partial_correlation_with_n(
    x: Sequence[float], y: Sequence[float], z: Sequence[float]
) -> dict[str, Any]:
    """Partial correlation with the number of complete observations."""
    xs, ys, zs = _clean_xyz(x, y, z)
    return {"r": partial_correlation(xs, ys, zs), "n": len(xs), "controlled_for": "z"}


# ---------------------------------------------------------------------------
# log-log regression
# ---------------------------------------------------------------------------


def loglog_fit(x: Sequence[float], y: Sequence[float]) -> dict[str, Any]:
    """Fit ``log(y) = intercept + slope * log(x)`` (natural log).

    Returns ``{"slope", "intercept", "r2", "n", "pearson", "p_value",
    "stderr"}``.  Points with non-positive or non-finite ``x``/``y`` are
    excluded (``n`` reports the number used).
    """
    from scipy import stats as _stats

    xs, ys = _clean_xy(x, y)
    pairs = [(xv, yv) for xv, yv in zip(xs, ys) if xv > 0.0 and yv > 0.0]
    if len(pairs) < 3:
        raise DataError(
            "log-log fit needs at least 3 strictly positive (x, y) points",
            details={"n": len(pairs), "minimum": 3},
        )
    log_x = [math.log(xv) for xv, _ in pairs]
    log_y = [math.log(yv) for _, yv in pairs]
    regression = _stats.linregress(log_x, log_y)
    r_squared = float(regression.rvalue) ** 2
    return {
        "slope": float(regression.slope),
        "intercept": float(regression.intercept),
        "r2": r_squared,
        "n": len(pairs),
        "pearson": float(regression.rvalue),
        "p_value": float(regression.pvalue),
        "stderr": float(regression.stderr),
    }


def standardised_residuals(
    fit: Mapping[str, Any],
    x: Sequence[float],
    y: Sequence[float],
) -> list[float]:
    """Standardised residuals of points from a :func:`loglog_fit`.

    Points with non-positive/non-finite ``x`` or ``y`` are skipped; the result
    therefore aligns with the filtered input order.  Residuals are standardised
    by their sample standard deviation (``ddof=2``); if that is zero (or there
    are too few points) all residuals are returned as ``0.0``.
    """
    slope = float(fit["slope"])
    intercept = float(fit["intercept"])
    xs, ys = _clean_xy(x, y)
    residuals: list[float] = []
    for xv, yv in zip(xs, ys):
        if xv <= 0.0 or yv <= 0.0:
            continue
        residuals.append(math.log(yv) - (intercept + slope * math.log(xv)))
    if len(residuals) < 3:
        return [0.0 for _ in residuals]
    mean = sum(residuals) / len(residuals)
    variance = sum((value - mean) ** 2 for value in residuals) / (len(residuals) - 1)
    scale = math.sqrt(variance * len(residuals) / (len(residuals) - 2))
    if scale == 0.0 or not math.isfinite(scale):
        return [0.0 for _ in residuals]
    return [value / scale for value in residuals]


def outlier_indices(
    fit: Mapping[str, Any], x: Sequence[float], y: Sequence[float], *, z_cutoff: float = 2.0
) -> list[int]:
    """Indices (into ``x``/``y``) of points with ``|standardised residual| >= z``."""
    xs, ys = _clean_xy(x, y)
    valid_indices = [
        index
        for index, (xv, yv) in enumerate(zip(xs, ys))
        if xv > 0.0 and yv > 0.0
    ]
    z_scores = standardised_residuals(fit, xs, ys)
    outliers: list[int] = []
    for z_value, index in zip(z_scores, valid_indices):
        if abs(z_value) >= z_cutoff:
            outliers.append(index)
    return outliers


# ---------------------------------------------------------------------------
# report assembly
# ---------------------------------------------------------------------------


def _round(value: Any, digits: int = 4) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return round(number, digits)


def correlation_report(
    x: Sequence[float],
    y: Sequence[float],
    *,
    labels: Sequence[Any] | None = None,
    groups: Sequence[Any] | None = None,
    control: Sequence[float] | None = None,
    x_name: str = "x",
    y_name: str = "y",
    control_name: str = "control",
    ctx: Any = None,
    small_n_threshold: int = 30,
    outlier_z: float = 2.0,
) -> dict[str, Any]:
    """Assemble the payload for the ``lifesci.stats`` command.

    Parameters
    ----------
    x, y:
        The two variables (raw values — the log-log fit is applied internally).
    labels:
        Optional per-point labels used to name outliers.
    groups:
        Optional per-point group labels; per-group correlations are reported.
    control:
        Optional per-point nuisance variable for the partial correlation
        (e.g. a size or length covariate).
    x_name, y_name, control_name:
        Display names recorded in ``variables``.
    ctx:
        Optional context; when given the caveats are also recorded as envelope
        warnings (``CORRELATION_NOT_CAUSATION``, ``PHYLOGENETIC_NON_INDEPENDENCE``,
        ``SMALL_N``).
    small_n_threshold:
        Emit the ``SMALL_N`` caveat below this n.
    outlier_z:
        Absolute standardised-residual cutoff for outlier reporting.

    Returns
    -------
    dict
        ``{"variables", "n", "pearson", "spearman", "partial", "loglog",
        "groups", "outliers", "caveats", "data"}``.
    """
    if len(x) != len(y):
        raise DataError(
            f"x and y must have the same length (got {len(x)} and {len(y)})",
            details={"len_x": len(x), "len_y": len(y)},
        )
    xs = list(x)
    ys = list(y)
    label_list = list(labels) if labels is not None else [
        f"point_{index}" for index in range(len(xs))
    ]
    if len(label_list) != len(xs):
        raise DataError(
            "labels must have the same length as x/y",
            details={"len_labels": len(label_list), "n": len(xs)},
        )
    group_list = list(groups) if groups is not None else None
    if group_list is not None and len(group_list) != len(xs):
        raise DataError(
            "groups must have the same length as x/y",
            details={"len_groups": len(group_list), "n": len(xs)},
        )
    control_list = list(control) if control is not None else None
    if control_list is not None and len(control_list) != len(xs):
        raise DataError(
            "control must have the same length as x/y",
            details={"len_control": len(control_list), "n": len(xs)},
        )

    pearson_stats = pearson_with_p(xs, ys)
    spearman_stats = spearman_with_p(xs, ys)
    fit = loglog_fit(xs, ys)
    residuals = standardised_residuals(fit, xs, ys)
    valid_indices = [
        index
        for index, (xv, yv) in enumerate(zip(xs, ys))
        if float(xv) > 0.0
        and float(yv) > 0.0
        and math.isfinite(float(xv))
        and math.isfinite(float(yv))
    ]
    outliers: list[dict[str, Any]] = []
    for z_value, index in zip(residuals, valid_indices):
        if abs(z_value) < outlier_z:
            continue
        outliers.append(
            {
                "label": str(label_list[index]),
                "x": _round(xs[index], 3),
                "y": _round(ys[index], 3),
                "standardised_residual": _round(z_value, 2),
                "direction": "above" if z_value > 0 else "below",
            }
        )
    outliers.sort(key=lambda row: (-abs(row["standardised_residual"] or 0.0), row["label"]))

    group_rows: list[dict[str, Any]] = []
    if group_list is not None:
        order: list[str] = []
        buckets: dict[str, list[int]] = {}
        for index, group in enumerate(group_list):
            key = str(group)
            if key not in buckets:
                order.append(key)
                buckets[key] = []
            buckets[key].append(index)
        for key in order:
            indices = buckets[key]
            gx = [xs[index] for index in indices]
            gy = [ys[index] for index in indices]
            row: dict[str, Any] = {
                "group": key,
                "n": len(indices),
                "pearson": _round(pearson(gx, gy)),
                "spearman": _round(spearman(gx, gy)),
                "partial": None,
            }
            if control_list is not None:
                row["partial"] = _round(
                    partial_correlation(gx, gy, [control_list[index] for index in indices])
                )
            try:
                row["loglog"] = loglog_fit(gx, gy)
            except DataError:
                row["loglog"] = None
            group_rows.append(row)

    partial = (
        partial_correlation(xs, ys, control_list) if control_list is not None else None
    )
    caveats: list[dict[str, Any]] = [
        {
            "code": "CORRELATION_NOT_CAUSATION",
            "message": (
                "A correlation (including a partial correlation) is not evidence of "
                "causation; report the coefficient, n and what was controlled for."
            ),
        }
    ]
    if control_list is None:
        caveats.append(
            {
                "code": "PHYLOGENETIC_NON_INDEPENDENCE",
                "message": (
                    "Cross-species points are not statistically independent; without a "
                    "phylogenetic control (e.g. independent contrasts or a nuisance-variable "
                    "partial correlation) the p-values are anti-conservative."
                ),
            }
        )
    if pearson_stats["n"] < small_n_threshold:
        caveats.append(
            {
                "code": "SMALL_N",
                "message": (
                    f"n={pearson_stats['n']} is below {small_n_threshold}; treat the "
                    "correlation as exploratory and report the confidence interval."
                ),
            }
        )
    if ctx is not None and hasattr(ctx, "warn"):
        for caveat in caveats:
            ctx.warn(caveat["code"], caveat["message"], severity="warning")

    return {
        "variables": {
            "x": x_name,
            "y": y_name,
            "control": control_name if control_list is not None else None,
        },
        "n": len(xs),
        "n_valid_loglog": fit["n"],
        "pearson": {
            "r": _round(pearson_stats["r"]),
            "p": _round(pearson_stats["p"], 6),
            "n": pearson_stats["n"],
        },
        "spearman": {
            "rho": _round(spearman_stats["rho"]),
            "p": _round(spearman_stats["p"], 6),
            "n": spearman_stats["n"],
        },
        "partial": (
            None
            if partial is None
            else {
                "r": _round(partial),
                "n": len(xs),
                "controlled_for": control_name,
            }
        ),
        "loglog": {
            "slope": _round(fit["slope"]),
            "intercept": _round(fit["intercept"]),
            "r2": _round(fit["r2"]),
            "n": fit["n"],
            "pearson": _round(fit["pearson"]),
            "p_value": _round(fit["p_value"], 6),
            "stderr": _round(fit["stderr"]),
            "model": f"log({y_name}) = intercept + slope * log({x_name})",
        },
        "groups": group_rows,
        "outliers": outliers,
        "caveats": caveats,
        "data": [
            {
                "label": str(label_list[index]),
                "x": _round(xs[index], 4),
                "y": _round(ys[index], 4),
                "group": None if group_list is None else str(group_list[index]),
                "control": None if control_list is None else _round(control_list[index], 4),
            }
            for index in range(len(xs))
        ],
    }
