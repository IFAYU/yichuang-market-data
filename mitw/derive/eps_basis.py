"""Empirical EPS-basis finding. The exchanges' documentation does not say whether the EPS feed is single-quarter, cumulative
or trailing, so the basis is decided from the data, with the evidence kept next to the conclusion.

Evidence A  (EPS feed vs official P/E)
    implied_eps = close / officialPE        (what EPS the exchange must have used)
    r = implied_eps / eps_feed
    If the feed is cumulative year-to-date through quarter Q and the official P/E uses a trailing year, r ~ 4/Q  (Q2 -> 2).
    If the feed is a single quarter, r ~ 4.   If the feed is already trailing, r ~ 1.

Evidence B  (revenue in the EPS feed vs the monthly revenue feed's year-to-date revenue)
    c = cumulative_revenue_through_month_M / revenue_in_eps_feed
    Feed is YTD through quarter-end month 3Q  -> c ~ M / (3Q).    Feed is the single quarter -> c ~ M / 3.

Both must point to the same hypothesis; otherwise the basis stays UNKNOWN_BASIS and nothing downstream is calculated from it.
"""
from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from typing import Optional

from ..contracts.models import UNKNOWN_BASIS

CUMULATIVE_YTD = "CUMULATIVE_YTD"
SINGLE_QUARTER = "SINGLE_QUARTER"
TRAILING = "TTM"
OFFICIAL_TTM = "OFFICIAL_TTM"

# The exchanges' own definition of the published P/E (looked up 2026-10-06). The data feed documents no EPS window, but the
# exchanges' pages do, and our own evidence A (below) is consistent with it. Quoted, not paraphrased.
OFFICIAL_PE_DEFINITION = {
    "TWSE": {
        "text": "本益比 = 收盤價／每股參考稅後純益；純益資料採用各上市公司於公開資訊觀測站過去已申報格式化之近滿4季財務報告為計算基礎；每股參考稅後純益為0或負數時，則不計算本益比。",
        "url": "https://www.twse.com.tw/zh/trading/historical/bwibbu-day.html",
    },
    "TPEX": {
        "text": "本益比 = 收盤價／每股稅後純益，其中每股稅後純益 = 該公司最近4季稅後純益／發行股數，當每股稅後純益為0或負數時，則不計算本益比。",
        "url": "https://www.tpex.org.tw/web/stock/aftertrading/peratio_analysis/pera.php?l=zh-tw",
        "note": "text retrieved through a search-engine excerpt of that page; a direct fetch of the page returned HTTP 403",
    },
    "checkedAt": "2026-10-06",
}

# how close (in log distance, i.e. relative) the median must be to a hypothesis' expected value to accept it
TOLERANCE = 0.30
MIN_SAMPLE = 30


@dataclass
class BasisRow:
    ticker: str
    close: Optional[float]
    official_pe: Optional[float]
    eps_feed: Optional[float]
    revenue_feed: Optional[float]
    cum_revenue_to_month: Optional[float]


def _quantiles(xs: list[float]) -> dict:
    xs = sorted(xs)
    n = len(xs)
    if n == 0:
        return {"n": 0}
    def q(p):
        k = (n - 1) * p
        lo, hi = math.floor(k), math.ceil(k)
        return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)
    return {"n": n, "p25": round(q(0.25), 3), "median": round(q(0.5), 3), "p75": round(q(0.75), 3)}


def _closest(median: float, candidates: dict) -> tuple[Optional[str], dict]:
    dist = {h: abs(math.log(median / e)) for h, e in candidates.items()}
    best = min(dist, key=dist.get)
    return (best if dist[best] <= math.log(1 + TOLERANCE) else None), {h: round(d, 3) for h, d in dist.items()}


def find_basis(rows: list[BasisRow], quarter: int, cum_month: Optional[int]) -> dict:
    """quarter = fiscal quarter of the EPS feed (1-4); cum_month = month number of the monthly feed's data (1-12)."""
    a = [
        (r.close / r.official_pe) / r.eps_feed
        for r in rows
        if r.close and r.official_pe and r.official_pe > 0 and r.eps_feed and r.eps_feed > 0
    ]
    b = [
        r.cum_revenue_to_month / r.revenue_feed
        for r in rows
        if r.cum_revenue_to_month and r.revenue_feed and r.revenue_feed > 0 and r.cum_revenue_to_month > 0
    ]
    a_expect = {CUMULATIVE_YTD: 4 / quarter, SINGLE_QUARTER: 4.0, TRAILING: 1.0}
    out: dict = {
        "quarter": quarter,
        "evidenceA": {"description": "median of (close / officialPE) / eps_feed", "expected": a_expect, **_quantiles(a)},
        "evidenceB": None,
        "epsFeedBasis": UNKNOWN_BASIS,
        "officialPEBasis": OFFICIAL_TTM,  # by the exchanges' own definition, not by inference
        "officialPEDefinition": OFFICIAL_PE_DEFINITION,
        "evidenceConsistentWithDefinition": False,
        "confirmed": False,
        "reasoning": [],
    }
    hyp_a: Optional[str] = None
    if len(a) >= MIN_SAMPLE:
        hyp_a, dist = _closest(statistics.median(a), a_expect)
        out["evidenceA"]["distance"] = dist
        out["evidenceA"]["supports"] = hyp_a
    else:
        out["reasoning"].append(f"evidence A has only {len(a)} usable companies (< {MIN_SAMPLE})")

    hyp_b: Optional[str] = None
    if cum_month and cum_month > 3 * quarter and len(b) >= MIN_SAMPLE:
        b_expect = {CUMULATIVE_YTD: cum_month / (3 * quarter), SINGLE_QUARTER: cum_month / 3}
        hyp_b, dist = _closest(statistics.median(b), b_expect)
        out["evidenceB"] = {"description": "median of cumulative revenue to month M / revenue in EPS feed", "month": cum_month,
                            "expected": b_expect, **_quantiles(b), "distance": dist, "supports": hyp_b}
    else:
        out["reasoning"].append("evidence B unavailable (monthly feed month is not beyond the EPS feed's quarter, or too few rows)")

    if hyp_a == CUMULATIVE_YTD and hyp_b == CUMULATIVE_YTD:
        out.update(epsFeedBasis=CUMULATIVE_YTD, evidenceConsistentWithDefinition=True, confirmed=True)
        out["reasoning"].append("A and B both support: the EPS feed is cumulative year-to-date, and the data is consistent with the exchanges' definition of the official P/E as trailing four quarters")
    elif hyp_a == CUMULATIVE_YTD and hyp_b is None:
        out.update(epsFeedBasis=UNKNOWN_BASIS)
        out["reasoning"].append("only A supports cumulative YTD; B did not confirm, so the basis is NOT accepted")
    else:
        out["reasoning"].append(f"A supports {hyp_a}, B supports {hyp_b}: no agreement, basis stays UNKNOWN_BASIS")
    return out
