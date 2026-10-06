"""Derived valuation metrics. Every result carries status, reason, basis, formula and the exact inputs it used.

Status rules (spec):
  EPS missing                       -> INSUFFICIENT_DATA
  EPS <= 0                          -> NOT_APPLICABLE (never 0, never a negative P/E)
  price missing                     -> INSUFFICIENT_DATA
  EPS basis not confirmed           -> UNKNOWN_BASIS (the metric is not computed from a period we cannot name)

P/E here is a RUN-RATE: price / (cumulative YTD EPS x 4 / quarters elapsed). It is NOT trailing-twelve-month P/E and is exposed as
a separate series ("pe_annualized_ytd") precisely so it is never mistaken for one.
"""
from __future__ import annotations

from typing import Optional

from ..contracts.models import DerivedMetric, INSUFFICIENT_DATA, NOT_APPLICABLE, OK, UNKNOWN_BASIS
from .eps_basis import CUMULATIVE_YTD

ANNUALIZED = "ANNUALIZED_YTD"


def _m(ticker, asof, metric, value, status, reason, basis, formula, deps, version) -> DerivedMetric:
    return DerivedMetric(ticker, asof, metric, value, status, reason, basis, formula, deps, version)


def market_cap(ticker: str, asof: str, close: Optional[float], price_date: Optional[str], shares: Optional[int], shares_asof: Optional[str],
               version: str) -> DerivedMetric:
    deps = {"close": {"value": close, "asOf": price_date, "source": "prices"}, "sharesOutstanding": {"value": shares, "asOf": shares_asof, "source": "companies"}}
    if close is None:
        return _m(ticker, asof, "market_cap", None, INSUFFICIENT_DATA, "MISSING_PRICE", "SPOT", "close * sharesOutstanding", deps, version)
    if not shares or shares <= 0:
        return _m(ticker, asof, "market_cap", None, INSUFFICIENT_DATA, "MISSING_SHARES", "SPOT", "close * sharesOutstanding", deps, version)
    return _m(ticker, asof, "market_cap", round(close * shares, 2), OK, None, "SPOT", "close * sharesOutstanding", deps, version)


def pe_annualized_ytd(ticker: str, asof: str, close: Optional[float], price_date: Optional[str], eps_ytd: Optional[float], period: Optional[str],
                      quarter: Optional[int], eps_basis: str, version: str) -> DerivedMetric:
    formula = "close / (eps_ytd * 4 / quarter)"
    deps = {"close": {"value": close, "asOf": price_date, "source": "prices"},
            "eps_ytd": {"value": eps_ytd, "period": period, "basis": eps_basis, "source": "industry_eps"}, "quarter": {"value": quarter}}
    if eps_ytd is None or not quarter:
        return _m(ticker, asof, "pe_annualized_ytd", None, INSUFFICIENT_DATA, "MISSING_EPS", ANNUALIZED, formula, deps, version)
    if eps_ytd < 0:
        return _m(ticker, asof, "pe_annualized_ytd", None, NOT_APPLICABLE, "NEGATIVE_EPS", ANNUALIZED, formula, deps, version)
    if eps_ytd == 0:
        return _m(ticker, asof, "pe_annualized_ytd", None, NOT_APPLICABLE, "ZERO_EPS", ANNUALIZED, formula, deps, version)
    if close is None:
        return _m(ticker, asof, "pe_annualized_ytd", None, INSUFFICIENT_DATA, "MISSING_PRICE", ANNUALIZED, formula, deps, version)
    if eps_basis != CUMULATIVE_YTD:
        return _m(ticker, asof, "pe_annualized_ytd", None, UNKNOWN_BASIS, "EPS_BASIS_NOT_CONFIRMED", ANNUALIZED, formula, deps, version)
    return _m(ticker, asof, "pe_annualized_ytd", round(close / (eps_ytd * 4 / quarter), 4), OK, None, ANNUALIZED, formula, deps, version)


def ps_annualized_ytd(ticker: str, asof: str, market_cap_value: Optional[float], revenue_thousand_ytd: Optional[float], period: Optional[str],
                      quarter: Optional[int], revenue_basis: str, special_industry: Optional[str], version: str) -> DerivedMetric:
    formula = "market_cap / (revenue_ytd_twd * 4 / quarter)"
    deps = {"market_cap": {"value": market_cap_value, "source": "derived"},
            "revenue_ytd_twd_thousand": {"value": revenue_thousand_ytd, "period": period, "basis": revenue_basis, "source": "industry_eps"}, "quarter": {"value": quarter}}
    if special_industry == "FINANCIAL":
        return _m(ticker, asof, "ps_annualized_ytd", None, NOT_APPLICABLE, "FINANCIAL_REVENUE_NOT_COMPARABLE", ANNUALIZED, formula, deps, version)
    if revenue_thousand_ytd is None or not quarter:
        return _m(ticker, asof, "ps_annualized_ytd", None, INSUFFICIENT_DATA, "MISSING_REVENUE", ANNUALIZED, formula, deps, version)
    if revenue_thousand_ytd <= 0:
        return _m(ticker, asof, "ps_annualized_ytd", None, NOT_APPLICABLE, "NON_POSITIVE_REVENUE", ANNUALIZED, formula, deps, version)
    if market_cap_value is None:
        return _m(ticker, asof, "ps_annualized_ytd", None, INSUFFICIENT_DATA, "MISSING_MARKET_CAP", ANNUALIZED, formula, deps, version)
    if revenue_basis != CUMULATIVE_YTD:
        return _m(ticker, asof, "ps_annualized_ytd", None, UNKNOWN_BASIS, "REVENUE_BASIS_NOT_CONFIRMED", ANNUALIZED, formula, deps, version)
    return _m(ticker, asof, "ps_annualized_ytd", round(market_cap_value / (revenue_thousand_ytd * 1000 * 4 / quarter), 4), OK, None, ANNUALIZED, formula, deps, version)


def implied_ttm_eps(ticker: str, asof: str, close: Optional[float], official_pe: Optional[float], version: str) -> DerivedMetric:
    """Back-solved from the exchange's own P/E. Not an independent source: it only tells us what EPS the exchange must have used."""
    formula = "close / officialPE"
    deps = {"close": {"value": close, "source": "prices"}, "officialPE": {"value": official_pe, "source": "ratios"}}
    if not close or official_pe is None or official_pe <= 0:
        return _m(ticker, asof, "implied_eps_from_official_pe", None, INSUFFICIENT_DATA, "NO_POSITIVE_OFFICIAL_PE", "OFFICIAL_TTM", formula, deps, version)
    return _m(ticker, asof, "implied_eps_from_official_pe", round(close / official_pe, 4), OK, None, "OFFICIAL_TTM", formula, deps, version)
