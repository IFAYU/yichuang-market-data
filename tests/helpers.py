"""Synthetic but realistic fixtures: small hand-checkable markets written as raw snapshots, exactly as the exchanges' feeds look."""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

from mitw.providers.endpoints import ENDPOINTS
from mitw.raw.store import RawMeta, RawStore, sha256_hex

RUN_DATE = "2026-10-06"


def write_raw(store: RawStore, endpoint_id: str, records, run_date: str = RUN_DATE) -> None:
    ep = ENDPOINTS[endpoint_id]
    body = json.dumps(records, ensure_ascii=False).encode("utf-8")
    meta = RawMeta(endpoint_id, ep.url, run_date, f"{run_date}T19:00:00+08:00", 200, None, None, sha256_hex(body), len(body),
                   len(records) if isinstance(records, list) else 0, "fixture")
    store.write(meta, body, ep.market, ep.dataset)


def twse_company(code, name, industry, shares):
    return {"出表日期": "1151005", "公司代號": code, "公司名稱": name, "公司簡稱": name[:3], "產業別": industry, "上市日期": "20000101", "已發行普通股數或TDR原股發行股數": str(shares)}


def tpex_company(code, name, industry, shares):
    return {"Date": "1151006", "SecuritiesCompanyCode": code, "CompanyName": name, "CompanyAbbreviation": name[:3], "SecuritiesIndustryCode": industry, "DateOfListing": "20100101", "IssueShares": str(shares)}


def build_store(tmp: Path | None = None, patch_twse_companies=None) -> tuple[RawStore, Path]:
    """TWSE: 2330 2303 2454 (semis), 6770 (semis, not in POC set -> stays L1 only), 1101 (cement), 2002 (steel, loss), 2882 (FH), 9101 (DR, no EPS)
       TPEX: 6488 (semis), 3293 (culture), 8027 (machinery: negative YTD EPS but positive official P/E), 6023 (futures broker, BD), 9999 (no price, no EPS)
       Hand-checkable numbers: price 100 and YTD EPS (Q2) 5 -> run-rate EPS 10 -> calculated P/E 10.0."""
    root = Path(tmp or tempfile.mkdtemp(prefix="mitw_fx_"))
    store = RawStore(root / "raw")

    twse_companies = [
        twse_company("2330", "台灣積體電路製造股份有限公司", "24", 1_000_000_000), twse_company("2303", "聯華電子股份有限公司", "24", 2000),
        twse_company("2454", "聯發科技股份有限公司", "24", 500), twse_company("6770", "力積電子股份有限公司", "24", 800),
        twse_company("1101", "臺灣水泥股份有限公司", "01", 3000), twse_company("2002", "中國鋼鐵股份有限公司", "10", 4000),
        twse_company("2882", "國泰金融控股股份有限公司", "17", 1500), twse_company("9101", "某存託憑證", "91", 100),
    ]
    if patch_twse_companies:
        patch_twse_companies(twse_companies)
    twse_prices = [{"Date": "1151005", "Code": c, "Name": c, "ClosingPrice": p} for c, p in
                   [("2330", "100.00"), ("2303", "50.00"), ("2454", "300.00"), ("6770", "20.00"), ("1101", "25.00"), ("2002", "18.90"), ("2882", "111.50"), ("9101", "0.00")]]
    # official P/E: TTM-based, blank when not positive
    twse_ratios = [{"Date": "1151005", "Code": c, "Name": c, "PEratio": pe, "DividendYield": "1.0", "PBratio": "2.0"} for c, pe in
                   [("2330", "11.00"), ("2303", "9.50"), ("2454", "35.00"), ("6770", ""), ("1101", ""), ("2002", ""), ("2882", "13.15")]]

    def eps_row(code, industry, eps, rev, oi, ni):
        return {"出表日期": "1151006", "年度": "115", "季別": "2", "公司代號": code, "公司名稱": code, "產業別": industry, "基本每股盈餘(元)": eps,
                "普通股每股面額": "新台幣 10.0000元", "營業收入": rev, "營業利益": oi, "營業外收入及支出": "0", "稅後淨利": ni}

    twse_eps = [
        eps_row("2330", "半導體業", "5.00", "1000000.00", "500000.00", "400000.00"),
        eps_row("2303", "半導體業", "2.00", "200000.00", "40000.00", "30000.00"),
        eps_row("2454", "半導體業", "15.00", "300000.00", "50000.00", "45000.00"),
        eps_row("6770", "半導體業", "-0.50", "50000.00", "-5000.00", "-4000.00"),
        eps_row("1101", "水泥工業", "0.38", "90000.00", "7000.00", "5800.00"),
        eps_row("2002", "鋼鐵工業", "-0.03", "170000.00", "100.00", "-500.00"),
        eps_row("2882", "金融保險業", "4.95", "160873077.00", "--", "76516146.00"),
    ]
    twse_monthly = [{"出表日期": "1151006", "資料年月": "11508", "公司代號": c, "公司名稱": c, "產業別": "x", "營業收入-當月營收": "1", "累計營業收入-當月累計營收": cum,
                     "累計營業收入-去年累計營收": prior, "累計營業收入-前期比較增減(%)": g} for c, cum, prior, g in
                    [("2330", "1333333.00", "1000000.00", "33.33"), ("2303", "266667.00", "250000.00", "6.67"), ("2454", "400000.00", "320000.00", "25.00"),
                     ("1101", "120000.00", "117000.00", "2.56")]]
    twse_forms = {"basi": [], "bd": [], "fh": [{"公司代號": "2882", "年度": "115", "季別": "2"}], "ins": []}

    tpex_companies = [tpex_company("6488", "環球晶圓", "24", 400), tpex_company("3293", "鈊象電子", "32", 300), tpex_company("8027", "鈦昇科技", "05", 100),
                      tpex_company("6023", "元大期貨", "17", 450), tpex_company("9999", "無資料公司", "20", 50)]
    tpex_prices = [{"Date": "1151006", "SecuritiesCompanyCode": c, "CompanyName": c, "Close": p} for c, p in
                   [("6488", "1205.00"), ("3293", "780.00"), ("8027", "209.50"), ("6023", "84.50"), ("9999", "---"), ("0001W", "1.00")]]
    tpex_ratios = [{"Date": "1151006", "SecuritiesCompanyCode": c, "CompanyName": c, "PriceEarningRatio": pe, "PriceBookRatio": "2.0"} for c, pe in
                   [("6488", "58.50"), ("3293", "18.02"), ("8027", "698.33"), ("6023", "9.34")]]
    tpex_eps = [
        {"Date": "1151006", "Year": "115", "季別": "2", "SecuritiesCompanyCode": c, "CompanyName": c, "產業別": ind, "基本每股盈餘": e, "普通股每股面額": "x",
         "營業收入": r, "營業利益": o, "營業外收入及支出": "0", "稅後淨利": n} for c, ind, e, r, o, n in
        [("6488", "半導體業", "11.87", "29199108.00", "2898184.00", "5674943.00"), ("3293", "文化創意業", "22.77", "12678167.00", "7485965.00", "6417429.00"),
         ("8027", "電機機械", "-0.46", "400000.00", "-10000.00", "-19000.00"), ("6023", "金融保險業", "4.19", "800000.00", "300000.00", "498000.00")]
    ]
    tpex_monthly = [{"出表日期": "1151006", "資料年月": "11508", "公司代號": c, "公司名稱": c, "產業別": "x", "營業收入-當月營收": "1", "累計營業收入-當月累計營收": cum,
                     "累計營業收入-去年累計營收": prior, "累計營業收入-前期比較增減(%)": g} for c, cum, prior, g in
                    [("6488", "38932144.00", "40000000.00", "-2.67"), ("3293", "16904223.00", "14000000.00", "20.74")]]
    tpex_forms = {"basi": [], "bd": [{"公司代號": "6023", "CompanyName": "元大期貨"}], "fh": [], "ins": []}

    for eid, recs in {
        "twse.companies": twse_companies, "twse.prices": twse_prices, "twse.ratios": twse_ratios, "twse.industry_eps": twse_eps,
        "twse.monthly_revenue": twse_monthly, "tpex.companies": tpex_companies, "tpex.prices": tpex_prices, "tpex.ratios": tpex_ratios,
        "tpex.industry_eps": tpex_eps, "tpex.monthly_revenue": tpex_monthly,
    }.items():
        write_raw(store, eid, recs)
    for f, recs in twse_forms.items():
        write_raw(store, f"twse.form_{f}", recs)
    for f, recs in tpex_forms.items():
        write_raw(store, f"tpex.form_{f}", recs)
    return store, root
