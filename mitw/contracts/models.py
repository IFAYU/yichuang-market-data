"""Phase 3A data contract.

Every record answers: where from (source), which day (asOfDate / tradeDate), which period, raw or computed
(kind / status), by which formula, depending on which raw values. Nothing is stored as a bare "PE = 20".

Status vocabulary for computed values (never a magic number):
  OK                 value is usable
  NOT_APPLICABLE     the metric does not apply (e.g. P/E when EPS <= 0)
  INSUFFICIENT_DATA  an input is missing
  UNKNOWN_BASIS      inputs exist but their period basis is not reliable enough to compute the metric
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Optional

# ---- vocabularies -----------------------------------------------------------------------------------------
MARKETS = ("TWSE", "TPEX")

OK = "OK"
NOT_APPLICABLE = "NOT_APPLICABLE"
INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
UNKNOWN_BASIS = "UNKNOWN_BASIS"

# Source tier: 1 = official primary source. Only Tier 1 exists in this phase.
TIER_OFFICIAL = 1

# Forward estimate statuses (interface only in this phase; nothing generates them automatically).
FORWARD_STATUSES = ("COMPANY_GUIDANCE", "ANALYST_ESTIMATE", "USER_ASSUMPTION")


def to_dict(obj: Any) -> Any:
    return asdict(obj)


@dataclass(frozen=True)
class Company:
    ticker: str
    companyName: str
    market: str  # TWSE | TPEX
    officialIndustryCode: str
    officialIndustryName: str
    listedDate: Optional[str]  # ISO date
    sharesOutstanding: Optional[int]
    source: str  # endpoint identifier, e.g. "twse.companies"
    asOfDate: Optional[str]  # ISO date the source says the row is as of
    shortName: Optional[str] = None  # the exchange's own short name (公司簡稱), e.g. 台積電
    financialForm: Optional[str] = None  # GENERAL | BASI | BD | FH | INS (None = forms feeds not fetched)  (statement form membership; see industry/financial.py)


@dataclass(frozen=True)
class MarketPrice:
    ticker: str
    tradeDate: str  # ISO
    close: Optional[float]
    source: str
    fetchedAt: str


@dataclass(frozen=True)
class OfficialRatio:
    """The exchange's own published ratios. EPS basis is NOT disclosed in the feed, so basis = UNKNOWN_BASIS until proven."""

    ticker: str
    tradeDate: str
    officialPE: Optional[float]
    officialPB: Optional[float]
    dividendYield: Optional[float]
    source: str
    fetchedAt: str
    basis: str = UNKNOWN_BASIS


@dataclass(frozen=True)
class FinancialMetric:
    ticker: str
    period: str  # e.g. "2026Q2"
    fiscalYear: int  # Gregorian
    fiscalQuarter: int
    metric: str  # eps_basic | revenue | operating_income | net_income | revenue_cum_yoy_pct ...
    value: Optional[float]
    unit: str  # TWD | TWD_PER_SHARE | PERCENT
    source: str
    status: str  # REPORTED
    asOfDate: Optional[str]
    fetchedAt: str
    basis: str = UNKNOWN_BASIS  # CUMULATIVE_YTD | SINGLE_QUARTER | TTM | UNKNOWN_BASIS


@dataclass(frozen=True)
class DerivedMetric:
    ticker: str
    asOfDate: str
    metric: str  # market_cap | pe_annualized_ytd | ps_annualized_ytd ...
    value: Optional[float]
    status: str  # OK | NOT_APPLICABLE | INSUFFICIENT_DATA | UNKNOWN_BASIS
    reason: Optional[str]
    basis: str
    formula: str
    dependencies: dict  # name -> {"value":..., "source":..., "asOf":..., "period":...}
    sourceVersion: str


@dataclass(frozen=True)
class ForwardEstimate:
    """Interface only. Nothing in this pipeline creates these automatically; the user supplies them."""

    ticker: Optional[str]
    value: float
    period: str  # e.g. "FY2027"
    status: str  # COMPANY_GUIDANCE | ANALYST_ESTIMATE | USER_ASSUMPTION
    source: str
    sourceNote: Optional[str]
    asOfDate: str

    def __post_init__(self) -> None:
        if self.status not in FORWARD_STATUSES:
            raise ValueError(f"ForwardEstimate.status must be one of {FORWARD_STATUSES}, got {self.status!r}")


@dataclass
class IndustryStats:
    count: int
    mean: Optional[float]
    median: Optional[float]
    p25: Optional[float]
    p75: Optional[float]
    min: Optional[float] = None
    max: Optional[float] = None


@dataclass
class ExcludedCompany:
    ticker: str
    reason: str
    detail: Optional[str] = None


@dataclass
class IndustrySnapshot:
    asOfDate: str
    industryCode: str
    industryName: str
    subIndustryCode: Optional[str]
    specialIndustry: Optional[str]  # FINANCIAL | None
    companyCount: int
    validCount: int
    excludedCount: int
    metricSeries: str  # OFFICIAL_PE | CALCULATED_PE_ANNUALIZED_YTD
    raw: Optional[IndustryStats]
    robust: Optional[IndustryStats]
    methodology: dict
    includedCompanies: list = field(default_factory=list)
    excludedCompanies: list = field(default_factory=list)
    robustExcluded: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    generatedAt: str = ""
    sourceVersion: str = ""
