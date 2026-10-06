"""The ONLY place that knows official URLs. Domain code refers to endpoint ids ("twse.companies") only.

Field names below come from the exchanges' own OpenAPI (swagger) documents, read 2026-10-06.
Official feeds only: no HTML pages, no third-party sites.
"""
from __future__ import annotations

from dataclasses import dataclass

TWSE_BASE = "https://openapi.twse.com.tw/v1"
TPEX_BASE = "https://www.tpex.org.tw/openapi/v1"


@dataclass(frozen=True)
class Endpoint:
    id: str  # "<market>.<dataset>"
    market: str  # TWSE | TPEX
    dataset: str
    url: str
    required_fields: tuple  # entries are field names, or tuples of aliases
    min_rows: int
    conditional: bool  # source documents ETag / Last-Modified, so conditional requests are used
    kind: str = "records"  # "records" (JSON array of objects) | "swagger" (JSON object)
    stage: str = "core"  # core = needed for the bounded dry run; extra = added by the full batch

    @property
    def host(self) -> str:
        return self.url.split("/")[2]


def _e(*a, **k) -> Endpoint:
    return Endpoint(*a, **k)


ENDPOINTS: dict[str, Endpoint] = {
    e.id: e
    for e in [
        # ---------------- TWSE (listed) ----------------
        _e("twse.companies", "TWSE", "companies", f"{TWSE_BASE}/opendata/t187ap03_L",
           ("出表日期", "公司代號", "公司名稱", "公司簡稱", "產業別", "上市日期", "已發行普通股數或TDR原股發行股數"), 800, True),
        _e("twse.prices", "TWSE", "prices", f"{TWSE_BASE}/exchangeReport/STOCK_DAY_ALL",
           ("Date", "Code", "Name", "ClosingPrice"), 800, True),
        _e("twse.ratios", "TWSE", "ratios", f"{TWSE_BASE}/exchangeReport/BWIBBU_ALL",
           ("Date", "Code", "Name", "PEratio", "PBratio"), 800, True),
        _e("twse.industry_eps", "TWSE", "industry_eps", f"{TWSE_BASE}/opendata/t187ap14_L",
           ("年度", "季別", "公司代號", "產業別", "基本每股盈餘(元)", "營業收入", "營業利益", "稅後淨利"), 800, True),
        _e("twse.swagger", "TWSE", "swagger", f"{TWSE_BASE}/swagger.json", ("paths",), 1, True, kind="swagger"),
        _e("twse.monthly_revenue", "TWSE", "monthly_revenue", f"{TWSE_BASE}/opendata/t187ap05_L",
           ("資料年月", "公司代號", "營業收入-當月營收", "累計營業收入-當月累計營收", "累計營業收入-去年累計營收"), 800, True, stage="extra"),
        _e("twse.form_basi", "TWSE", "form_basi", f"{TWSE_BASE}/opendata/t187ap06_L_basi", (("公司代號", "SecuritiesCompanyCode"),), 1, True, stage="extra"),
        _e("twse.form_bd", "TWSE", "form_bd", f"{TWSE_BASE}/opendata/t187ap06_L_bd", (("公司代號", "SecuritiesCompanyCode"),), 1, True, stage="extra"),
        _e("twse.form_fh", "TWSE", "form_fh", f"{TWSE_BASE}/opendata/t187ap06_L_fh", (("公司代號", "SecuritiesCompanyCode"),), 1, True, stage="extra"),
        _e("twse.form_ins", "TWSE", "form_ins", f"{TWSE_BASE}/opendata/t187ap06_L_ins", (("公司代號", "SecuritiesCompanyCode"),), 1, True, stage="extra"),
        # ---------------- TPEx (OTC) ----------------
        _e("tpex.companies", "TPEX", "companies", f"{TPEX_BASE}/mopsfin_t187ap03_O",
           ("Date", "SecuritiesCompanyCode", "CompanyName", "CompanyAbbreviation", "SecuritiesIndustryCode", "DateOfListing", "IssueShares"), 600, False),
        _e("tpex.prices", "TPEX", "prices", f"{TPEX_BASE}/tpex_mainboard_daily_close_quotes",
           ("Date", "SecuritiesCompanyCode", "CompanyName", "Close"), 600, False),
        _e("tpex.ratios", "TPEX", "ratios", f"{TPEX_BASE}/tpex_mainboard_peratio_analysis",
           ("Date", "SecuritiesCompanyCode", "CompanyName", "PriceEarningRatio", "PriceBookRatio"), 500, False),
        _e("tpex.industry_eps", "TPEX", "industry_eps", f"{TPEX_BASE}/mopsfin_t187ap14_O",
           ("Year", "季別", "SecuritiesCompanyCode", "產業別", "基本每股盈餘", "營業收入", "營業利益", "稅後淨利"), 600, False),
        _e("tpex.swagger", "TPEX", "swagger", "https://www.tpex.org.tw/openapi/swagger.json", ("paths",), 1, False, kind="swagger"),
        _e("tpex.monthly_revenue", "TPEX", "monthly_revenue", f"{TPEX_BASE}/mopsfin_t187ap05_O",
           ("資料年月", "公司代號", "營業收入-當月營收", "累計營業收入-當月累計營收", "累計營業收入-去年累計營收"), 600, False, stage="extra"),
        _e("tpex.form_basi", "TPEX", "form_basi", f"{TPEX_BASE}/mopsfin_t187ap06_O_basi", (("公司代號", "SecuritiesCompanyCode"),), 1, False, stage="extra"),
        _e("tpex.form_bd", "TPEX", "form_bd", f"{TPEX_BASE}/mopsfin_t187ap06_O_bd", (("公司代號", "SecuritiesCompanyCode"),), 1, False, stage="extra"),
        _e("tpex.form_fh", "TPEX", "form_fh", f"{TPEX_BASE}/mopsfin_t187ap06_O_fh", (("公司代號", "SecuritiesCompanyCode"),), 1, False, stage="extra"),
        _e("tpex.form_ins", "TPEX", "form_ins", f"{TPEX_BASE}/mopsfin_t187ap06_O_ins", (("公司代號", "SecuritiesCompanyCode"),), 1, False, stage="extra"),
    ]
}

# How long a successful raw file may be re-used (copied, no request) instead of fetched again. Prices and official ratios change
# every trading day (0 = always fetch for a new trading day); the rest change monthly / quarterly / rarely.
MAX_AGE_DAYS = {"companies": 7, "industry_eps": 7, "monthly_revenue": 7, "swagger": 30,
                "form_basi": 30, "form_bd": 30, "form_fh": 30, "form_ins": 30}


def max_age_days(ep: Endpoint) -> int:
    return MAX_AGE_DAYS.get(ep.dataset, 0)


CORE_IDS = tuple(i for i, e in ENDPOINTS.items() if e.stage == "core")
EXTRA_IDS = tuple(i for i, e in ENDPOINTS.items() if e.stage == "extra")
