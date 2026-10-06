"""Distribution statistics. Percentiles use the same definition as the Valuation Lab engine (inclusive linear, 'type 7'),
so a P25/median/P75 produced here is read identically downstream."""
from __future__ import annotations

import math
from typing import Optional

from ..config import OUTLIER_POLICY, OutlierPolicy
from ..contracts.models import ExcludedCompany, IndustryStats

ALGORITHM = "PERCENTILE_INC_LINEAR_TYPE7"


def percentile_inc(sorted_asc: list[float], p: float) -> float:
    n = len(sorted_asc)
    if n == 0:
        raise ValueError("empty sample")
    k = (n - 1) * p
    lo, hi = math.floor(k), math.ceil(k)
    return sorted_asc[lo] + (sorted_asc[hi] - sorted_asc[lo]) * (k - lo)


def _r(x: float) -> float:
    return round(x, 4)


def describe(values: list[float], min_n: int) -> Optional[IndustryStats]:
    """None when the group is too small to publish statistics for (count is still reported by the caller)."""
    if len(values) < min_n:
        return None
    s = sorted(values)
    return IndustryStats(
        count=len(s), mean=_r(sum(s) / len(s)), median=_r(percentile_inc(s, 0.5)),
        p25=_r(percentile_inc(s, 0.25)), p75=_r(percentile_inc(s, 0.75)), min=_r(s[0]), max=_r(s[-1]),
    )


def robust_filter(items: list[tuple[str, float]], policy: OutlierPolicy = OUTLIER_POLICY) -> tuple[list[tuple[str, float]], list[ExcludedCompany], dict]:
    """Tukey fences on the positive P/E sample. Below `min_sample` nothing is trimmed (too few points to call an outlier).
    Returns (kept, removed, fences)."""
    vals = sorted(v for _, v in items)
    if len(vals) < policy.min_sample:
        return list(items), [], {"applied": False, "reason": f"sample {len(vals)} < min_sample {policy.min_sample}"}
    q1, q3 = percentile_inc(vals, 0.25), percentile_inc(vals, 0.75)
    iqr = q3 - q1
    lo, hi = q1 - policy.iqr_k * iqr, q3 + policy.iqr_k * iqr
    kept, removed = [], []
    for t, v in items:
        if v > hi:
            removed.append(ExcludedCompany(t, "EXTREME_PE_HIGH", f"{v} > upper fence {_r(hi)}"))
        elif v < lo:
            removed.append(ExcludedCompany(t, "EXTREME_PE_LOW", f"{v} < lower fence {_r(lo)}"))
        else:
            kept.append((t, v))
    return kept, removed, {"applied": True, "q1": _r(q1), "q3": _r(q3), "lower": _r(lo), "upper": _r(hi), "k": policy.iqr_k}
