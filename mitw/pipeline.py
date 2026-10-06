"""normalize -> derive -> industry statistics. A PURE function of the raw snapshots: same raw data in, same result out
(runtime metadata such as generatedAt is injected by the caller and never influences a number)."""
from __future__ import annotations

import collections
from dataclasses import dataclass, field
from typing import Optional

from .config import OUTLIER_POLICY, RECONCILIATION_POLICY, OutlierPolicy, ReconciliationPolicy
from .contracts.models import (Company, DerivedMetric, ExcludedCompany, FinancialMetric, IndustrySnapshot, MarketPrice, OfficialRatio, OK, UNKNOWN_BASIS)
from .contracts.version import PIPELINE_VERSION
from .derive import reconcile as rc
from .derive.eps_basis import BasisRow, CUMULATIVE_YTD, find_basis
from .derive.valuation import implied_ttm_eps, market_cap, pe_annualized_ytd, ps_annualized_ytd
from .industry.codes import attach_industry_names, build_industry_names
from .industry.financial import FINANCIAL, FINANCIAL_WARNING, FORM_LABEL, attach_financial_forms, special_industry
from .industry.stats import ALGORITHM, describe, robust_filter
from .industry.taxonomy import apply_l2, load_poc
from .providers.official import provider_for
from .raw.store import RawStore

SERIES_OFFICIAL = "OFFICIAL_PE"
SERIES_CALC = "CALCULATED_PE_ANNUALIZED_YTD"
FORM_DATASETS = ("basi", "bd", "fh", "ins")


@dataclass
class Inputs:
    run_date: str
    companies: list = field(default_factory=list)
    prices: list = field(default_factory=list)
    ratios: list = field(default_factory=list)
    eps: list = field(default_factory=list)
    monthly: list = field(default_factory=list)
    industry_name_by_ticker: dict = field(default_factory=dict)
    forms: dict = field(default_factory=dict)
    forms_complete: bool = False
    raw_meta: dict = field(default_factory=dict)  # endpoint id -> {sha256, fetchedAt, rows, url}


def load_inputs(store: RawStore, run_date: str) -> Inputs:
    inp = Inputs(run_date=run_date)
    forms_ok = True
    for market in ("TWSE", "TPEX"):
        p = provider_for(market)
        get = lambda ds: store.get(run_date, market, ds)
        need = {"companies": get("companies"), "prices": get("prices"), "ratios": get("ratios"), "industry_eps": get("industry_eps"),
                "monthly_revenue": get("monthly_revenue")}
        for k, v in need.items():
            if v is None:
                raise FileNotFoundError(f"raw snapshot missing: {market}.{k} for {run_date} (run `fetch` first)")
            inp.raw_meta[v.meta.endpointId] = {"sha256": v.meta.sha256, "fetchedAt": v.meta.fetchedAt, "rows": v.meta.rows, "url": v.meta.url}
        inp.companies += p.companies(need["companies"])
        inp.prices += p.prices(need["prices"])
        inp.ratios += p.official_ratios(need["ratios"])
        inp.eps += p.industry_eps(need["industry_eps"])
        inp.monthly += p.monthly_revenue(need["monthly_revenue"])
        inp.industry_name_by_ticker.update(p.industry_names(need["industry_eps"]))
        form_raws = {f: get(f"form_{f}") for f in FORM_DATASETS}
        if all(form_raws.values()):
            for f, r in form_raws.items():
                inp.raw_meta[r.meta.endpointId] = {"sha256": r.meta.sha256, "fetchedAt": r.meta.fetchedAt, "rows": r.meta.rows, "url": r.meta.url}
            inp.forms.update(p.financial_forms(form_raws))
        else:
            forms_ok = False
    inp.forms_complete = forms_ok
    return inp


@dataclass
class BuildResult:
    run_date: str
    companies: list  # snapshot company records (dicts, sorted by ticker)
    derived: list  # DerivedMetric
    industry_snapshots: list  # IndustrySnapshot
    findings: dict
    warnings: list
    coverage: dict
    source_versions: dict
    market_as_of: dict
    financial_as_of: dict


def _first(d: dict, k):
    return d.get(k)


def _r(x: Optional[float], n: int = 4) -> Optional[float]:
    return None if x is None else round(x, n)


def build(inp: Inputs, generated_at: str, outlier: OutlierPolicy = OUTLIER_POLICY,
          recon_policy: ReconciliationPolicy = RECONCILIATION_POLICY) -> BuildResult:
    warnings: list[str] = []

    # ---- universe -------------------------------------------------------------------------------------------
    seen: dict[str, Company] = {}
    for c in inp.companies:
        if c.ticker in seen:
            warnings.append(f"ticker {c.ticker} appears in both markets; keeping {seen[c.ticker].market}")
            continue
        seen[c.ticker] = c
    companies = list(seen.values())
    names, w = build_industry_names(companies, inp.industry_name_by_ticker)
    warnings += w
    companies = attach_industry_names(companies, names)
    if inp.forms_complete:
        companies = attach_financial_forms(companies, inp.forms)
    else:
        warnings.append("financial statement-form feeds were not fetched: financial companies are NOT identified in this build")
    by_ticker = {c.ticker: c for c in companies}

    def latest(items, keyf):
        out: dict = {}
        for it in items:
            if it.ticker in by_ticker:
                prev = out.get(it.ticker)
                if prev is not None:
                    warnings.append(f"duplicate row for {it.ticker} in {it.source}")
                out[it.ticker] = it
        return out

    price = latest(inp.prices, None)
    ratio = latest(inp.ratios, None)
    eps_m: dict[str, dict[str, FinancialMetric]] = collections.defaultdict(dict)
    for m in inp.eps:
        if m.ticker in by_ticker:
            eps_m[m.ticker][m.metric] = m
    mon: dict[str, dict[str, FinancialMetric]] = collections.defaultdict(dict)
    for m in inp.monthly:
        if m.ticker in by_ticker:
            mon[m.ticker][m.metric] = m

    quarters = collections.Counter(m["eps_basic"].fiscalQuarter for m in eps_m.values() if "eps_basic" in m)
    periods = collections.Counter(m["eps_basic"].period for m in eps_m.values() if "eps_basic" in m)
    months = collections.Counter(m["revenue_cum_ytd"].period for m in mon.values() if "revenue_cum_ytd" in m)
    quarter = quarters.most_common(1)[0][0] if quarters else None
    eps_period = periods.most_common(1)[0][0] if periods else None
    cum_period = months.most_common(1)[0][0] if months else None
    if len(periods) > 1:
        warnings.append(f"EPS feed mixes fiscal periods {dict(periods)}; the most common one is used and the rest are treated as missing")
    if len(months) > 1:
        warnings.append(f"monthly revenue feed mixes months {dict(months)}")

    # ---- EPS basis finding (evidence, not assumption) -------------------------------------------------------
    basis_rows = [
        BasisRow(t, price[t].close if t in price else None, ratio[t].officialPE if t in ratio else None,
                 eps_m[t]["eps_basic"].value if t in eps_m and "eps_basic" in eps_m[t] else None,
                 eps_m[t]["revenue"].value if t in eps_m and "revenue" in eps_m[t] else None,
                 mon[t]["revenue_cum_ytd"].value if t in mon and "revenue_cum_ytd" in mon[t] else None)
        for t in by_ticker
    ]
    cum_month = int(cum_period[5:]) if cum_period else None
    finding = find_basis(basis_rows, quarter or 0, cum_month) if quarter else {"epsFeedBasis": UNKNOWN_BASIS, "officialPEBasis": UNKNOWN_BASIS, "confirmed": False, "reasoning": ["no EPS period"]}
    eps_basis = finding["epsFeedBasis"]
    if eps_basis != CUMULATIVE_YTD:
        warnings.append("EPS basis not confirmed: calculated P/E and P/S are UNKNOWN_BASIS")

    market_as_of = {m: max((p.tradeDate for p in inp.prices if p.ticker in by_ticker and by_ticker[p.ticker].market == m), default=None) for m in ("TWSE", "TPEX")}

    # ---- L2 proof of concept --------------------------------------------------------------------------------
    l2, w = apply_l2(companies, load_poc())
    warnings += w

    # ---- per company ----------------------------------------------------------------------------------------
    derived: list[DerivedMetric] = []
    records: list[dict] = []
    elig: dict[str, dict] = {}  # ticker -> {"OFFICIAL_PE": (value|None, reason|None), "CALC": ...}
    for t in sorted(by_ticker):
        c = by_ticker[t]
        px = price.get(t)
        close, pdate = (px.close, px.tradeDate) if px else (None, market_as_of.get(c.market))
        asof = pdate or market_as_of.get(c.market) or inp.run_date
        e = eps_m.get(t, {})
        eps_ytd = e["eps_basic"].value if "eps_basic" in e and e["eps_basic"].period == eps_period else None
        rev = e["revenue"].value if "revenue" in e and e["revenue"].period == eps_period else None
        ni = e["net_income"].value if "net_income" in e and e["net_income"].period == eps_period else None
        oi = e["operating_income"].value if "operating_income" in e and e["operating_income"].period == eps_period else None
        sp = special_industry(c)
        mc = market_cap(t, asof, close, pdate, c.sharesOutstanding, c.asOfDate, PIPELINE_VERSION)
        pe_c = pe_annualized_ytd(t, asof, close, pdate, eps_ytd, eps_period, quarter, eps_basis, PIPELINE_VERSION)
        ps_c = ps_annualized_ytd(t, asof, mc.value, rev, eps_period, quarter, eps_basis, sp, PIPELINE_VERSION)
        rt = ratio.get(t)
        off_pe = rt.officialPE if rt else None
        imp = implied_ttm_eps(t, asof, close, off_pe, PIPELINE_VERSION)
        derived += [mc, pe_c, ps_c, imp]

        if finding["confirmed"]:
            recon_result = rc.classify_run_rate_vs_official(pe_c.value, off_pe, recon_policy)
        else:
            recon_result = rc.reconcile(pe_c.value, off_pe, basis_comparable=False, policy=recon_policy)

        # eligibility for industry statistics
        if rt is None:
            off_reason = "NO_OFFICIAL_RATIO_ROW"
        elif off_pe is not None and off_pe > 0:
            off_reason = None
        elif eps_ytd is None:
            off_reason = "MISSING_EPS"
        elif eps_ytd < 0:
            off_reason = "NEGATIVE_EPS"
        elif eps_ytd == 0:
            off_reason = "ZERO_EPS"
        else:
            off_reason = "INVALID_PE"  # blank / non-positive official P/E although YTD EPS is positive (trailing EPS probably <= 0)
        calc_reason = None if pe_c.status == OK else (pe_c.reason or "INVALID_PE")
        elig[t] = {SERIES_OFFICIAL: (off_pe if off_reason is None else None, off_reason), SERIES_CALC: (pe_c.value if calc_reason is None else None, calc_reason)}

        cum_yoy = mon.get(t, {}).get("revenue_cum_yoy_pct")
        records.append({
            "ticker": t, "name": c.companyName, "shortName": c.shortName, "market": c.market,
            "industry": {"code": c.officialIndustryCode, "name": c.officialIndustryName},
            "subIndustry": ({"code": l2[t].code, "name": l2[t].name, "assignedBy": l2[t].assignedBy} if t in l2 else None),
            "specialIndustry": sp, "financialForm": c.financialForm, "listedDate": c.listedDate,
            "price": {"close": close, "tradeDate": pdate, "source": px.source if px else None},
            "sharesOutstanding": c.sharesOutstanding,
            "revenue": {"value": rev, "unit": "TWD_THOUSAND", "period": eps_period, "basis": eps_basis},
            "marketCap": {"value": mc.value, "status": mc.status, "reason": mc.reason},
            "eps": {"value": eps_ytd, "period": eps_period, "basis": eps_basis, "unit": "TWD_PER_SHARE", "asOfDate": e["eps_basic"].asOfDate if "eps_basic" in e else None},
            "officialPE": {"value": _r(off_pe, 2), "tradeDate": rt.tradeDate if rt else None, "basis": finding["officialPEBasis"], "source": rt.source if rt else None},
            "calculatedPE": {"value": pe_c.value, "status": pe_c.status, "reason": pe_c.reason, "basis": pe_c.basis, "formula": pe_c.formula},
            "impliedEpsFromOfficialPE": imp.value,
            "peReconciliation": recon_result,
            "ps": {"value": ps_c.value, "status": ps_c.status, "reason": ps_c.reason, "basis": ps_c.basis, "formula": ps_c.formula},
            "officialPB": _r(rt.officialPB, 2) if rt else None,
            "revenueGrowthYtdYoyPct": {"value": cum_yoy.value if cum_yoy else None, "period": cum_yoy.period if cum_yoy else None, "basis": "CUMULATIVE_YTD_TO_MONTH_YOY"},
            "netMarginYtdPct": _r(ni / rev * 100, 2) if (ni is not None and rev and rev > 0 and sp is None) else None,
            "operatingMarginYtdPct": _r(oi / rev * 100, 2) if (oi is not None and rev and rev > 0 and sp is None) else None,
            "profitabilityPeriod": eps_period, "profitabilityBasis": eps_basis,
        })

    # ---- industry snapshots ---------------------------------------------------------------------------------
    members_by_l1: dict[str, list[str]] = collections.defaultdict(list)
    for t, c in by_ticker.items():
        members_by_l1[c.officialIndustryCode].append(t)

    snapshots: list[IndustrySnapshot] = []
    mkt_asof = max((d for d in market_as_of.values() if d), default=inp.run_date)

    def make(series: str, code: str, name: str, sub: Optional[str], members: list[str], extra_warn: list[str]) -> IndustrySnapshot:
        valid, excluded = [], []
        for t in sorted(members):
            v, reason = elig[t][series]
            (valid if reason is None else excluded).append((t, v) if reason is None else ExcludedCompany(t, reason))
        raw_stats = describe([v for _, v in valid], outlier.min_stat_sample)
        kept, removed, fences = robust_filter(valid, outlier)
        robust_stats = describe([v for _, v in kept], outlier.min_stat_sample)
        fin_share = sum(1 for t in members if special_industry(by_ticker[t])) / len(members) if members else 0
        special = FINANCIAL if fin_share >= 0.5 else None
        warn = list(extra_warn)
        if special:
            warn.append(FINANCIAL_WARNING)
        if raw_stats is None:
            warn.append(f"only {len(valid)} valid P/E (< {outlier.min_stat_sample}): no statistics published")
        if series == SERIES_CALC:
            warn.append("run-rate P/E (annualized YTD EPS), not trailing-twelve-month; use for comparison with the official series only")
        return IndustrySnapshot(
            asOfDate=mkt_asof, industryCode=code, industryName=name, subIndustryCode=sub, specialIndustry=special,
            companyCount=len(members), validCount=len(valid), excludedCount=len(excluded), metricSeries=series,
            raw=raw_stats, robust=robust_stats,
            methodology={"series": series, "percentile": ALGORITHM, "outlierPolicy": {"name": outlier.name, "iqrK": outlier.iqr_k, "minSample": outlier.min_sample},
                         "robustFences": fences, "minStatSample": outlier.min_stat_sample, "marketAsOf": market_as_of,
                         "epsBasis": eps_basis, "officialPEBasis": finding["officialPEBasis"], "eligibility": "P/E strictly positive and finite; see excludedCompanies for reasons"},
            includedCompanies=[{"ticker": t, "pe": _r(v, 4), "market": by_ticker[t].market} for t, v in valid],
            excludedCompanies=[{"ticker": x.ticker, "reason": x.reason} for x in excluded],
            robustExcluded=[{"ticker": x.ticker, "reason": x.reason, "detail": x.detail} for x in removed],
            warnings=warn, generatedAt=generated_at, sourceVersion=PIPELINE_VERSION,
        )

    for series in (SERIES_OFFICIAL, SERIES_CALC):
        for code in sorted(members_by_l1):
            mem = members_by_l1[code]
            snapshots.append(make(series, code, names.get(code, code), None, mem, []))
            # financial sub-types (identified by the exchange's own statement-form feeds)
            by_form: dict[str, list[str]] = collections.defaultdict(list)
            for t in mem:
                f = by_ticker[t].financialForm
                if f in FORM_LABEL:
                    by_form[f].append(t)
            if len(by_form) > 0:
                for f, tl in sorted(by_form.items()):
                    snapshots.append(make(series, code, names.get(code, code), f"FIN_{f}", tl, [f"財報型態子分類：{FORM_LABEL[f]}（依交易所財報格式資料識別）"]))
        # semiconductor L2 proof of concept
        poc = load_poc()
        l1 = poc["l1Code"]
        groups: dict[str, list[str]] = collections.defaultdict(list)
        for t, a in l2.items():
            groups[a.code].append(t)
        for code2, tl in sorted(groups.items()):
            nm = next(a.name for a in l2.values() if a.code == code2)
            snapshots.append(make(series, l1, names.get(l1, l1), code2, tl,
                                  [f"L2 概念驗證（{nm}）：僅涵蓋人工挑選的 {len(l2)} 家，未經人工複核，不代表該子產業整體"]))

    coverage = _coverage(by_ticker, price, ratio, eps_m, eps_period, names)
    fin_asof = {"epsPeriod": eps_period, "monthlyRevenuePeriod": cum_period, "eps_basis": eps_basis}
    return BuildResult(inp.run_date, records, derived, snapshots, finding, sorted(set(warnings)), coverage,
                       {"pipeline": PIPELINE_VERSION, "raw": inp.raw_meta}, market_as_of, fin_asof)


def _coverage(by_ticker, price, ratio, eps_m, eps_period, names) -> dict:
    out = {}
    for m in ("TWSE", "TPEX", "ALL"):
        ts = [t for t, c in by_ticker.items() if m == "ALL" or c.market == m]
        n = len(ts)
        def cnt(f):
            return sum(1 for t in ts if f(t))
        c_ok = {
            "companies": n,
            "withPrice": cnt(lambda t: t in price and price[t].close is not None),
            "withOfficialPE": cnt(lambda t: t in ratio and ratio[t].officialPE is not None and ratio[t].officialPE > 0),
            "withOfficialRatioRow": cnt(lambda t: t in ratio),
            "withEPS": cnt(lambda t: "eps_basic" in eps_m.get(t, {}) and eps_m[t]["eps_basic"].value is not None and eps_m[t]["eps_basic"].period == eps_period),
            "withIndustryCode": cnt(lambda t: bool(by_ticker[t].officialIndustryCode)),
            "withIndustryName": cnt(lambda t: bool(by_ticker[t].officialIndustryName) and not by_ticker[t].officialIndustryName.startswith("產業代碼")),
            "withShares": cnt(lambda t: (by_ticker[t].sharesOutstanding or 0) > 0),
        }
        c_ok["missing"] = {k: n - v for k, v in c_ok.items() if k.startswith("with") and k != "withOfficialPE"}
        c_ok["invalid"] = {
            "nonPositiveOrMissingPriceRow": cnt(lambda t: t in price and price[t].close is None),
            "nonPositiveShares": cnt(lambda t: by_ticker[t].sharesOutstanding is not None and by_ticker[t].sharesOutstanding <= 0),
            "officialPEnonPositive": cnt(lambda t: t in ratio and ratio[t].officialPE is not None and ratio[t].officialPE <= 0),
        }
        out[m] = c_ok
    return out
