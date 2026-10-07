"""Phase 3H.2A (PREFLIGHT): build the isolated 興櫃 (emerging board) resource from OFFICIAL TPEx OpenAPI payloads.

    python scripts/build_emerging_snapshot.py --fetch --api <dir> --out <dir>     # download the five official datasets, then build
    python scripts/build_emerging_snapshot.py --api <dir with the official JSON> --out <dir>

Writes   <out>/emerging/manifest.json   and   <out>/emerging/releases/<release>/emerging-snapshot.json
Official facts only (no price is chosen, no P/B is computed, no P/E exists): the app derives those with the audited Phase 3H.1 rules, so the
price / P/B logic has ONE implementation. Official JSON only: no HTML, no third-party site, no browser automation.

It is NOT part of the weekly pipeline: it does not import mitw, does not touch out/, manifest.json, health.json or releases/ of the weekly
snapshot, and publishes nothing. A future publisher would write only under emerging/.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCHEMA = "1.0.0"
INPUTS = {
    "quotes": "tpex_esb_latest_statistics",   # 日均價 / 前日均價 / 報買價 / 報賣價 / 成交 / 成交量
    "master": "esb_basic_R",                  # company master (公司基本資料)
    "balance": "esb_bs_ci_U",                 # balance sheet, includes 每股參考淨值
    "income": "esb_is_ci_U",                  # income statement (year-to-date)
    "revenue": "esb_rev_R",                   # monthly revenue
}
SOURCES = {
    "quotes": "https://www.tpex.org.tw/openapi/v1/tpex_esb_latest_statistics",
    "master": "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap03_R",
    "balance": "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap07_U_ci",
    "income": "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap06_U_ci",
    "revenue": "https://www.tpex.org.tw/openapi/v1/t187ap05_R",
}
UA = {"User-Agent": "yichuang-market-data emerging snapshot builder (read-only)", "Accept": "application/json"}
TAIPEI = timezone(timedelta(hours=8))


def fetch_all(api: Path) -> None:
    """One GET per official TPEx OpenAPI dataset. Official JSON only; nothing else is ever called."""
    api.mkdir(parents=True, exist_ok=True)
    for key, name in INPUTS.items():
        raw = urllib.request.urlopen(urllib.request.Request(SOURCES[key], headers=UA), timeout=60).read()
        rows = json.loads(raw.decode("utf-8-sig"))
        if not isinstance(rows, list) or not rows:
            raise SystemExit(f"{name}: the official endpoint returned no rows; nothing is built")
        (api / f"{name}.json").write_bytes(raw)
        print(f"fetched {name}: {len(rows)} rows")
        time.sleep(0.4)


def load(p: Path):
    return json.loads(p.read_text(encoding="utf-8-sig"))


def num(x):
    try:
        v = float(str(x).replace(",", "").strip())
        return v if math.isfinite(v) else None
    except ValueError:
        return None


def roc_date(d):
    d = str(d or "").strip()
    return f"{int(d[:-4]) + 1911}-{d[-4:-2]}-{d[-2:]}" if len(d) >= 7 and d.isdigit() else None


def yyyymmdd(d):
    d = str(d or "").strip()
    return f"{d[:4]}-{d[4:6]}-{d[6:]}" if len(d) == 8 and d.isdigit() else None


def roc_period(year, season):
    try:
        return f"{int(year) + 1911}Q{int(season)}"
    except (TypeError, ValueError):
        return None


def roc_month(ym):
    ym = str(ym or "").strip()
    return f"{int(ym[:-2]) + 1911}-{ym[-2:]}" if len(ym) >= 4 and ym.isdigit() else None


def par_value(s):
    import re
    m = re.search(r"([0-9]+(?:\.[0-9]+)?)", str(s or ""))
    return float(m.group(1)) if m else None


def build(api: Path, now: datetime) -> tuple[dict, bytes]:
    d = {k: load(api / f"{v}.json") for k, v in INPUTS.items()}
    master = {r["SecuritiesCompanyCode"]: r for r in d["master"]}
    bal = {r["公司代號"]: r for r in d["balance"]}
    inc = {r["SecuritiesCompanyCode"]: r for r in d["income"]}
    rev = {r["公司代號"]: r for r in d["revenue"]}
    ind_names = {}
    for r in d["revenue"]:
        m = master.get(r["公司代號"])
        if m and r.get("產業別"):
            ind_names.setdefault(m["SecuritiesIndustryCode"], r["產業別"])
    companies = []
    for q in sorted(d["quotes"], key=lambda r: r["SecuritiesCompanyCode"]):
        c = q["SecuritiesCompanyCode"]
        m = master.get(c, {})
        b, i, r = bal.get(c), inc.get(c), rev.get(c)
        code = (m.get("SecuritiesIndustryCode") or "").strip()
        companies.append({
            "ticker": c,
            "name": (m.get("CompanyName") or q.get("CompanyName") or c).strip(),
            "shortName": (m.get("CompanyAbbreviation") or q.get("CompanyName") or "").strip() or None,
            "industry": {"code": code, "name": ind_names.get(code, f"產業 {code}")},
            "listedDate": yyyymmdd(m.get("DateOfListing")),
            "quoteDate": roc_date(q.get("Date")),
            "quote": {
                "avg": num(q.get("Average")), "prevAvg": num(q.get("PreviousAveragePrice")), "bid": num(q.get("BuyingPrice")),
                "ask": num(q.get("SellingPrice")), "last": num(q.get("LatestPrice")), "volume": num(q.get("TransactionVolume")),
                "suspendTime": (q.get("SuspendTime") or "").strip() or None,
            },
            "bvps": {"value": num(b.get("每股參考淨值")) if b else None, "period": roc_period(b.get("年度"), b.get("季別")) if b else None},
            "paidInCapital": num(m.get("Paidin.Capital.NTDollars")),
            "parValue": par_value(m.get("ParValueOfCommonStock")),
            "ytd": {
                "period": roc_period(i.get("年度"), i.get("季別")) if i else None,
                "eps": num(i.get("基本每股盈餘（元）")) if i else None,
                "revenueK": num(i.get("營業收入")) if i else None,
                "operatingIncomeK": num(i.get("營業利益（損失）")) if i else None,
                "netIncomeK": num(i.get("本期淨利（淨損）")) if i else None,
            },
            "monthly": {
                "period": roc_month(r.get("資料年月")) if r else None,
                "cumRevenueK": num(r.get("累計營業收入-當月累計營收")) if r else None,
                "prevCumRevenueK": num(r.get("累計營業收入-去年累計營收")) if r else None,
            },
        })
    dates = sorted({c["quoteDate"] for c in companies if c["quoteDate"]})
    bv = sorted({c["bvps"]["period"] for c in companies if c["bvps"]["period"]})
    ev = sorted({c["ytd"]["period"] for c in companies if c["ytd"]["period"]})
    doc = {
        "schemaVersion": SCHEMA, "kind": "emerging-snapshot", "generatedAt": now.isoformat(timespec="seconds"),
        "marketAsOf": {"EMERGING": dates[-1] if dates else None},
        "financialAsOf": {"epsPeriod": ev[-1] if ev else None, "bvpsPeriod": bv[-1] if bv else None},
        "sources": SOURCES, "companies": companies,
        # informational coverage numbers (the app derives price / P/B itself from the raw facts)
        "counts": {
            "quotes": len(companies), "masterRecords": len(master), "balanceSheets": len(bal), "incomeStatements": len(inc), "monthlyRevenue": len(rev),
            "tradedToday": sum(1 for c in companies if (c["quote"]["avg"] or 0) > 0 and (c["quote"]["volume"] or 0) > 0),
            "bvpsPresent": sum(1 for c in companies if c["bvps"]["value"] is not None),
        },
    }
    return doc, json.dumps(doc, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--api", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--fetch", action="store_true", help="download the official datasets into --api first")
    a = ap.parse_args()
    if a.fetch:
        fetch_all(Path(a.api))
    now = datetime.now(TAIPEI)
    doc, blob = build(Path(a.api), now)
    sha = hashlib.sha256(blob).hexdigest()
    release = sha[:12]
    base = Path(a.out) / "emerging"
    rel = base / "releases" / release
    rel.mkdir(parents=True, exist_ok=True)
    (rel / "emerging-snapshot.json").write_bytes(blob)  # the release first ...
    manifest = {
        "schemaVersion": SCHEMA, "kind": "emerging-manifest", "release": release, "sourceHash": sha,
        "generatedAt": doc["generatedAt"], "publishedAt": now.isoformat(timespec="seconds"), "updatePolicy": "WEEKLY",
        "lastSuccessfulPublication": now.isoformat(timespec="seconds"), "marketAsOf": doc["marketAsOf"], "financialAsOf": doc["financialAsOf"],
        "companyCount": len(doc["companies"]),
        "files": {"emerging-snapshot.json": {"sha256": sha, "bytes": len(blob), "path": f"releases/{release}/emerging-snapshot.json"}},
    }
    (base / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")  # ... the manifest LAST
    print(f"release {release}: {len(doc['companies'])} companies, marketAsOf {doc['marketAsOf']['EMERGING']}, bvps {doc['financialAsOf']['bvpsPeriod']}, counts {json.dumps(doc['counts'])}")


if __name__ == "__main__":
    main()
