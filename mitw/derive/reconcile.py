"""Official P/E vs calculated P/E. Never forces agreement: it records the difference and classifies it.

  MATCH / MINOR_DIFFERENCE / MATERIAL_DIFFERENCE  by the configurable policy (relative difference vs the official value)
  UNKNOWN_BASIS   one side has no usable value, or the two sides are not on a comparable EPS basis
"""
from __future__ import annotations

from typing import Optional

from ..config import RECONCILIATION_POLICY, ReconciliationPolicy

MATCH = "MATCH"
MINOR = "MINOR_DIFFERENCE"
MATERIAL = "MATERIAL_DIFFERENCE"
UNKNOWN = "UNKNOWN_BASIS"


def reconcile(calculated: Optional[float], official: Optional[float], basis_comparable: bool,
              policy: ReconciliationPolicy = RECONCILIATION_POLICY) -> dict:
    if calculated is None or official is None or official <= 0:
        return {"class": UNKNOWN, "diffPct": None, "reason": "one side has no usable value"}
    if not basis_comparable:
        diff = (calculated - official) / official
        return {"class": UNKNOWN, "diffPct": round(diff * 100, 2), "reason": "EPS bases are not comparable (annualized YTD run-rate vs official trailing)"}
    diff = (calculated - official) / official
    a = abs(diff)
    cls = MATCH if a <= policy.match_max else MINOR if a <= policy.minor_max else MATERIAL
    return {"class": cls, "diffPct": round(diff * 100, 2), "reason": None}


def classify_run_rate_vs_official(calculated: Optional[float], official: Optional[float],
                                  policy: ReconciliationPolicy = RECONCILIATION_POLICY) -> dict:
    """The run-rate P/E and the official (trailing) P/E use different EPS windows by construction, so a MATERIAL difference is
    expected for growing / seasonal companies. The class is still computed on the numbers; `basisNote` says why it is not an error."""
    r = reconcile(calculated, official, basis_comparable=True, policy=policy)
    r["basisNote"] = "calculated = annualized YTD run-rate, official = trailing basis (inferred); different windows by design"
    return r
