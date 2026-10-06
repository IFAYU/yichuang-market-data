"""TwseProvider / TpexProvider: official OpenAPI feeds -> domain objects. Pure parsing; fetching goes through providers/http.py."""
from __future__ import annotations

from typing import Optional

from ..contracts.models import Company, FinancialMetric, MarketPrice, OfficialRatio, UNKNOWN_BASIS
from ..normalize.parse import parse_int, parse_num, parse_price, parse_roc_year, parse_tw_date, parse_year_month
from ..raw.store import RawRef
from .base import OfficialProviderBase

REPORTED = "REPORTED"


class _Fields:
    """Field names of one market's feeds (names are exactly those in the exchange's own swagger)."""

    def __init__(self, **kw):
        self.__dict__.update(kw)


_TWSE = _Fields(
    c_ticker="公司代號", c_name="公司名稱", c_short="公司簡稱", c_industry="產業別", c_listed="上市日期", c_shares="已發行普通股數或TDR原股發行股數", c_asof="出表日期",
    p_ticker="Code", p_date="Date", p_close="ClosingPrice",
    r_ticker="Code", r_date="Date", r_pe="PEratio", r_pb="PBratio", r_yield="DividendYield",
    e_ticker="公司代號", e_year="年度", e_q="季別", e_eps="基本每股盈餘(元)", e_asof="出表日期", e_industry="產業別",
    f_ticker="公司代號",
)
_TPEX = _Fields(
    c_ticker="SecuritiesCompanyCode", c_name="CompanyName", c_short="CompanyAbbreviation", c_industry="SecuritiesIndustryCode", c_listed="DateOfListing", c_shares="IssueShares", c_asof="Date",
    p_ticker="SecuritiesCompanyCode", p_date="Date", p_close="Close",
    r_ticker="SecuritiesCompanyCode", r_date="Date", r_pe="PriceEarningRatio", r_pb="PriceBookRatio", r_yield="YieldRatio",
    e_ticker="SecuritiesCompanyCode", e_year="Year", e_q="季別", e_eps="基本每股盈餘", e_asof="Date", e_industry="產業別",
    f_ticker="SecuritiesCompanyCode",
)


class _OfficialProvider(OfficialProviderBase):
    F: _Fields

    @staticmethod
    def _t(rec: dict, key: str) -> str:
        return str(rec.get(key, "")).strip()

    def companies(self, raw: RawRef) -> list[Company]:
        F, out = self.F, []
        for r in raw.load():
            t = self._t(r, F.c_ticker)
            if not t:
                continue
            out.append(Company(
                ticker=t, companyName=self._t(r, F.c_name), market=self.market,
                officialIndustryCode=self._t(r, F.c_industry), officialIndustryName="",
                listedDate=parse_tw_date(r.get(F.c_listed)), sharesOutstanding=parse_int(r.get(F.c_shares)),
                source=raw.meta.endpointId, asOfDate=parse_tw_date(r.get(F.c_asof)),
                shortName=self._t(r, F.c_short) or None,
            ))
        return out

    def prices(self, raw: RawRef) -> list[MarketPrice]:
        F, out = self.F, []
        for r in raw.load():
            t, d = self._t(r, F.p_ticker), parse_tw_date(r.get(F.p_date))
            if t and d:
                out.append(MarketPrice(t, d, parse_price(r.get(F.p_close)), raw.meta.endpointId, raw.meta.fetchedAt))
        return out

    def official_ratios(self, raw: RawRef) -> list[OfficialRatio]:
        F, out = self.F, []
        for r in raw.load():
            t, d = self._t(r, F.r_ticker), parse_tw_date(r.get(F.r_date))
            if t and d:
                out.append(OfficialRatio(t, d, parse_num(r.get(F.r_pe)), parse_num(r.get(F.r_pb)), parse_num(r.get(F.r_yield)),
                                         raw.meta.endpointId, raw.meta.fetchedAt))
        return out

    def industry_eps(self, raw: RawRef) -> list[FinancialMetric]:
        """EPS / revenue / operating income / net income of the latest reported period.

        Documentation says nothing about whether these are single-quarter or cumulative, so basis = UNKNOWN_BASIS here;
        analysis/eps_basis.py decides from evidence.  Money fields are in NT$ thousands (MOPS convention, verified against
        EPS x shares in the dry run).
        """
        F, out = self.F, []
        for r in raw.load():
            t = self._t(r, F.e_ticker)
            fy, q = parse_roc_year(r.get(F.e_year)), parse_int(r.get(F.e_q))
            if not (t and fy and q):
                continue
            asof, common = parse_tw_date(r.get(F.e_asof)), (t, f"{fy}Q{q}", fy, q)
            for metric, key, unit in (
                ("eps_basic", F.e_eps, "TWD_PER_SHARE"), ("revenue", "營業收入", "TWD_THOUSAND"),
                ("operating_income", "營業利益", "TWD_THOUSAND"), ("net_income", "稅後淨利", "TWD_THOUSAND"),
            ):
                out.append(FinancialMetric(*common, metric, parse_num(r.get(key)), unit, raw.meta.endpointId, REPORTED, asof,
                                           raw.meta.fetchedAt, basis=UNKNOWN_BASIS))
        return out

    def industry_names(self, raw: RawRef) -> dict[str, str]:
        """ticker -> official industry NAME, as printed in the EPS feed (the company feed only carries the numeric code)."""
        F = self.F
        return {self._t(r, F.e_ticker): self._t(r, F.e_industry) for r in raw.load() if self._t(r, F.e_ticker)}

    def monthly_revenue(self, raw: RawRef) -> list[FinancialMetric]:
        out = []
        for r in raw.load():
            t, ym = self._t(r, "公司代號"), parse_year_month(r.get("資料年月"))
            if not (t and ym):
                continue
            y, m = int(ym[:4]), int(ym[5:])
            asof, q = parse_tw_date(r.get("出表日期")), (m - 1) // 3 + 1
            for metric, key, unit in (
                ("revenue_month", "營業收入-當月營收", "TWD_THOUSAND"),
                ("revenue_cum_ytd", "累計營業收入-當月累計營收", "TWD_THOUSAND"),
                ("revenue_cum_ytd_prior_year", "累計營業收入-去年累計營收", "TWD_THOUSAND"),
                ("revenue_cum_yoy_pct", "累計營業收入-前期比較增減(%)", "PERCENT"),
            ):
                out.append(FinancialMetric(t, ym, y, q, metric, parse_num(r.get(key)), unit, raw.meta.endpointId, REPORTED, asof,
                                           raw.meta.fetchedAt, basis="CUMULATIVE_YTD_TO_MONTH" if "cum" in metric else "SINGLE_MONTH"))
        return out

    def financial_forms(self, raws: dict) -> dict:
        """ticker -> statement form ('BASI' bank, 'BD' securities/futures, 'FH' financial holding, 'INS' insurer).

        Membership of the form-specific income-statement feeds is the exchange's own signal of which statement a company files.
        """
        out: dict[str, str] = {}
        for form, raw in raws.items():
            for r in raw.load():
                t = self._t(r, "公司代號") or self._t(r, "SecuritiesCompanyCode")  # alias differs across feeds
                if t:
                    out[t] = form.upper()
        return out


class TwseProvider(_OfficialProvider):
    market = "TWSE"
    F = _TWSE


class TpexProvider(_OfficialProvider):
    market = "TPEX"
    F = _TPEX


def provider_for(market: str) -> _OfficialProvider:
    return TwseProvider() if market == "TWSE" else TpexProvider()
