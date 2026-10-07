"""Phase 3H.2A (PREFLIGHT): build the isolated 興櫃 (emerging board) resource from OFFICIAL TPEx OpenAPI payloads.

    python scripts/build_emerging_snapshot.py --fetch --api <dir> --out <dir>     # download the five official datasets, then build
    python scripts/build_emerging_snapshot.py --api <dir with the official JSON> --out <dir>

Writes   <out>/emerging/manifest.json   and   <out>/emerging/releases/<release>/emerging-snapshot.json
Official facts only (no price is chosen, no P/B is computed, no P/E exists): the app derives those with the audited Phase 3H.1 rules, so the
price / P/B logic has ONE implementation. Official JSON only: no HTML, no third-party site, no browser automation.

It is NOT part of the weekly pipeline: in the default mode it does not import mitw, does not touch out/, manifest.json, health.json or releases/ of
the weekly snapshot, and publishes nothing. A future publisher would write only under emerging/.

--daily (Phase 3I.1, STAGED, not activated by any workflow) adds the trading-day-aware decision of mitw/daily.py for emerging ALONE:
    python scripts/build_emerging_snapshot.py --daily --fetch --api <dir> --out <dir> --calendar <holidaySchedule.json> --current-manifest <published emerging/manifest.json>
  precheck (zero requests when nothing is owed) -> fetch -> snapshot-level market date from the MARKET aggregate (tpex_esb_highlight.Date), never from one
  company's fallback price -> evaluate -> write the release + manifest LAST only when the decision is to publish. It prints one JSON decision line.
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
    "highlight": "tpex_esb_highlight",        # market-level aggregate (Date, RegisteredStocksNumber): the authority for the SNAPSHOT's market date
}
SOURCES = {
    "quotes": "https://www.tpex.org.tw/openapi/v1/tpex_esb_latest_statistics",
    "master": "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap03_R",
    "balance": "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap07_U_ci",
    "income": "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap06_U_ci",
    "revenue": "https://www.tpex.org.tw/openapi/v1/t187ap05_R",
    "highlight": "https://www.tpex.org.tw/openapi/v1/tpex_esb_highlight",
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


def snapshot_market_date(highlight_rows, quote_rows) -> tuple:
    """The SNAPSHOT's market date comes from the market-level aggregate (tpex_esb_highlight.Date), cross-checked against every quote row's Date and the
    registered-stock count. One company having no trade today (its quote falls back to the previous average) says nothing about the market date.
    Returns (YYYY-MM-DD | None, problems)."""
    problems = []
    hl = highlight_rows[0] if isinstance(highlight_rows, list) and len(highlight_rows) == 1 and isinstance(highlight_rows[0], dict) else None
    hd = roc_date(hl.get("Date")) if hl else None
    if hd is None:
        problems.append("HIGHLIGHT_DATE_MISSING_OR_MALFORMED")
    qd = {roc_date(r.get("Date")) for r in quote_rows}
    if None in qd:
        problems.append("QUOTE_DATE_MISSING_OR_MALFORMED")
    qd.discard(None)
    if len(qd) > 1:
        problems.append(f"QUOTE_DATES_DIFFER: {sorted(qd)}")
    if hd is not None and qd and qd != {hd}:
        problems.append(f"HIGHLIGHT_DATE_{hd}_DIFFERS_FROM_QUOTE_DATES_{sorted(qd)}")
    if hl and num(hl.get("RegisteredStocksNumber")) is not None and int(num(hl["RegisteredStocksNumber"])) != len(quote_rows):
        problems.append(f"REGISTERED_COUNT_{hl['RegisteredStocksNumber']}_DIFFERS_FROM_QUOTE_ROWS_{len(quote_rows)}")
    return hd, problems


def build(api: Path, now: datetime, daily: bool = False) -> tuple[dict, bytes]:
    d = {k: load(api / f"{v}.json") for k, v in INPUTS.items() if k != "highlight"}
    highlight = load(api / f"{INPUTS['highlight']}.json") if (api / f"{INPUTS['highlight']}.json").exists() else None
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
    market_date = dates[-1] if dates else None
    if daily:
        market_date, problems = snapshot_market_date(highlight, d["quotes"])
        if problems:
            raise SystemExit("MARKET_DATE_VALIDATION_FAILED: " + "; ".join(problems))
    doc = {
        "schemaVersion": SCHEMA, "kind": "emerging-snapshot", **({} if daily else {"generatedAt": now.isoformat(timespec="seconds")}),
        "marketAsOf": {"EMERGING": market_date},
        "financialAsOf": {"epsPeriod": ev[-1] if ev else None, "bvpsPeriod": bv[-1] if bv else None},
        "sources": SOURCES, "companies": companies,
        # informational coverage numbers (the app derives price / P/B itself from the raw facts)
        "counts": {
            "quotes": len(companies), "masterRecords": len(master), "balanceSheets": len(bal), "incomeStatements": len(inc), "monthlyRevenue": len(rev),
            "tradedToday": sum(1 for c in companies if (c["quote"]["avg"] or 0) > 0 and (c["quote"]["volume"] or 0) > 0),
            "bvpsPresent": sum(1 for c in companies if c["bvps"]["value"] is not None),
            "previousDayFallback": sum(1 for c in companies if not ((c["quote"]["avg"] or 0) > 0 and (c["quote"]["volume"] or 0) > 0) and (c["quote"]["prevAvg"] or 0) > 0),
            "pbComputable": sum(1 for c in companies if (c["bvps"]["value"] or 0) > 0 and (((c["quote"]["avg"] or 0) > 0 and (c["quote"]["volume"] or 0) > 0) or (c["quote"]["prevAvg"] or 0) > 0)),
        },
    }
    return doc, json.dumps(doc, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def write_release(out: Path, doc: dict, blob: bytes, now: datetime, policy: str, extra: dict | None = None, release_json: bool = False) -> dict:
    sha = hashlib.sha256(blob).hexdigest()
    release = sha[:12]
    base = Path(out) / "emerging"
    rel = base / "releases" / release
    rel.mkdir(parents=True, exist_ok=True)
    (rel / "emerging-snapshot.json").write_bytes(blob)  # the release first ...
    manifest = {
        "schemaVersion": SCHEMA, "kind": "emerging-manifest", "release": release, "sourceHash": sha,
        "generatedAt": doc.get("generatedAt") or now.isoformat(timespec="seconds"), "publishedAt": now.isoformat(timespec="seconds"), "updatePolicy": policy,
        "lastSuccessfulPublication": now.isoformat(timespec="seconds"), "marketAsOf": doc["marketAsOf"], "financialAsOf": doc["financialAsOf"],
        "companyCount": len(doc["companies"]),
        "files": {"emerging-snapshot.json": {"sha256": sha, "bytes": len(blob), "path": f"releases/{release}/emerging-snapshot.json"}},
        **(extra or {}),
    }
    if release_json:  # daily mode: the release carries its own publication record (retention reads its publishedAt); still written BEFORE the manifest
        (rel / "release.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")
    (base / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")  # ... the manifest LAST
    return manifest


def run_daily(a) -> int:
    """STAGED daily decision for emerging alone. Needs mitw/daily.py (pure functions); never touches the weekly files."""
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from mitw import daily as D
    from mitw.trading_calendar import from_twse_holiday_schedule

    now = D.to_taipei(datetime.fromisoformat(a.now) if a.now else datetime.now(TAIPEI))
    cal = from_twse_holiday_schedule(json.loads(Path(a.calendar).read_text(encoding="utf-8-sig")), now.isoformat(timespec="seconds"))
    cur = json.loads(Path(a.current_manifest).read_text(encoding="utf-8")) if a.current_manifest and Path(a.current_manifest).exists() else None
    published = {"EMERGING": D.parse_market_date(((cur or {}).get("marketAsOf") or {}).get("EMERGING"))}
    final = D.is_final_attempt(now, a.trigger)
    av = D.AVAILABILITY["EMERGING"]
    base = {"market": "EMERGING", "businessTimeTaipei": now.isoformat(timespec="seconds"), "attemptType": D.attempt_label(now), "final": final, "trigger": a.trigger,
            "previousPublishedMarketDate": cur and (cur.get("marketAsOf") or {}).get("EMERGING"), "lastKnownGood": cur and cur.get("release"),
            "availabilityBasis": av.basis, "evidenceLevel": D.EVIDENCE_LEVEL, "manifestUpdated": False, "lastKnownGoodPreserved": True, "publicVerification": "NOT_APPLICABLE"}
    pre = D.precheck(now, published, cal, D.EMERGING_MARKETS)
    if not pre.fetch:
        print(json.dumps({**base, "decision": pre.state, "state": pre.state, "requests": 0, "reason": pre.reason, "expectedMarketDate": pre.targets.get("EMERGING"),
                          "actualSourceMarketDate": None, "releaseId": base["lastKnownGood"]}, ensure_ascii=False))
        return 0

    def failed(kind: str, msg: str) -> int:
        dec = D.evaluate(now, published, {"EMERGING": D.Obs(kind, None, msg[:300])}, cal, D.EMERGING_MARKETS, final)
        print(json.dumps({**base, "decision": dec.state, "state": dec.state, "publish": False, "reason": dec.reason, "expectedMarketDate": dec.targets.get("EMERGING"),
                          "actualSourceMarketDate": None, "releaseId": base["lastKnownGood"]}, ensure_ascii=False))
        return 1

    try:
        if a.fetch:
            fetch_all(Path(a.api))
        doc, blob = build(Path(a.api), now, daily=True)
    except SystemExit as e:
        msg = str(e)
        return failed("MALFORMED" if msg.startswith("MARKET_DATE_VALIDATION_FAILED") else "ERROR", msg)
    except Exception as e:  # network / JSON problems: the source failed, the Last Known Good stays
        return failed("ERROR", f"{type(e).__name__}: {e}")
    obs = {"EMERGING": D.Obs("OK", D.parse_market_date(doc["marketAsOf"]["EMERGING"]))}
    dec = D.evaluate(now, published, obs, cal, D.EMERGING_MARKETS, final)
    c = doc["counts"]
    out = {**base, "decision": dec.state, "state": dec.state, "publish": dec.publish, "reason": dec.reason, "expectedMarketDate": dec.targets.get("EMERGING"),
           "actualSourceMarketDate": doc["marketAsOf"]["EMERGING"], "marketDate": doc["marketAsOf"]["EMERGING"], "releaseId": base["lastKnownGood"],
           "quoteCount": c["quotes"], "companyCount": len(doc["companies"]), "currentDayPriceCount": c["tradedToday"], "previousDayFallbackCount": c["previousDayFallback"],
           "bvpsCoverage": c["bvpsPresent"], "pbCoverage": c["pbComputable"], "sourceCounts": {"quotes": c["quotes"], "masterRecords": c["masterRecords"], "balanceSheets": c["balanceSheets"],
                                                                                              "incomeStatements": c["incomeStatements"], "monthlyRevenue": c["monthlyRevenue"]}}
    if dec.publish:
        sha = hashlib.sha256(blob).hexdigest()
        if cur and cur.get("sourceHash") == sha:  # same content, same market date: nothing new to say
            out.update(state=D.NOOP_ALREADY_PUBLISHED, publish=False, reason="identical content to the Last Known Good")
        else:
            m = write_release(Path(a.out), doc, blob, now, D.POLICY, {"publicationPolicy": D.publication_policy(cal, markets=D.EMERGING_MARKETS)}, release_json=True)
            from mitw.retention import prune_daily
            pruned = prune_daily(Path(a.out) / "emerging" / "releases", now, protect={m["release"], (cur or {}).get("release") or m["release"]})
            out.update(release=m["release"], releaseId=m["release"], companyCount=m["companyCount"], sourceHash=sha, manifestUpdated=True,
                       publicVerification="PENDING_PUBLIC_READBACK", pruned=pruned)
    print(json.dumps(out, ensure_ascii=False))
    return 1 if out["state"].startswith("FAILED") else 0  # a failed attempt must show as a failed job; the Last Known Good is untouched either way


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--api", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--fetch", action="store_true", help="download the official datasets into --api first")
    ap.add_argument("--daily", action="store_true", help="STAGED: trading-day-aware decision (needs --calendar); default mode is the 3H.2B manual build")
    ap.add_argument("--calendar", help="official TWSE holidaySchedule JSON (daily mode)")
    ap.add_argument("--current-manifest", help="the published emerging/manifest.json = the Last Known Good (daily mode)")
    ap.add_argument("--now", help="override the clock (tests)")
    ap.add_argument("--trigger", default="schedule", choices=["schedule", "manual"])
    a = ap.parse_args()
    if a.daily:
        if not a.calendar:
            raise SystemExit("--daily needs --calendar (the official holiday list): the decision never assumes every weekday trades")
        raise SystemExit(run_daily(a))
    if a.fetch:
        fetch_all(Path(a.api))
    now = datetime.now(TAIPEI)
    doc, blob = build(Path(a.api), now)
    m = write_release(Path(a.out), doc, blob, now, "WEEKLY")
    print(f"release {m['release']}: {len(doc['companies'])} companies, marketAsOf {doc['marketAsOf']['EMERGING']}, bvps {doc['financialAsOf']['bvpsPeriod']}, counts {json.dumps(doc['counts'])}")


if __name__ == "__main__":
    main()
