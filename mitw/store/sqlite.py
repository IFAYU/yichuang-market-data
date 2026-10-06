"""SQLite: raw metadata, normalized records, derived metrics, historical industry snapshots, ingestion runs.

Internal storage only. Consumers (risk-profiler) never read this file: they read the versioned static JSON (snapshot/).
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict
from pathlib import Path

from ..pipeline import BuildResult, Inputs
from ..providers.endpoints import ENDPOINTS

SCHEMA = """
CREATE TABLE IF NOT EXISTS data_sources (id TEXT PRIMARY KEY, market TEXT, dataset TEXT, url TEXT, tier INTEGER);
CREATE TABLE IF NOT EXISTS ingestion_runs (run_id TEXT PRIMARY KEY, run_date TEXT, started_at TEXT, status TEXT, requests INTEGER, reused INTEGER, notes TEXT);
CREATE TABLE IF NOT EXISTS raw_files (run_date TEXT, endpoint_id TEXT, sha256 TEXT, fetched_at TEXT, rows INTEGER, url TEXT, PRIMARY KEY (run_date, endpoint_id));
CREATE TABLE IF NOT EXISTS companies (run_date TEXT, ticker TEXT, market TEXT, name TEXT, industry_code TEXT, industry_name TEXT, listed_date TEXT,
  shares INTEGER, financial_form TEXT, source TEXT, as_of_date TEXT, PRIMARY KEY (run_date, ticker));
CREATE TABLE IF NOT EXISTS company_industries (run_date TEXT, ticker TEXT, level TEXT, code TEXT, name TEXT, assigned_by TEXT, PRIMARY KEY (run_date, ticker, level));
CREATE TABLE IF NOT EXISTS market_prices (ticker TEXT, trade_date TEXT, close REAL, source TEXT, fetched_at TEXT, PRIMARY KEY (ticker, trade_date));
CREATE TABLE IF NOT EXISTS official_ratios (ticker TEXT, trade_date TEXT, official_pe REAL, official_pb REAL, dividend_yield REAL, basis TEXT, source TEXT, fetched_at TEXT, PRIMARY KEY (ticker, trade_date));
CREATE TABLE IF NOT EXISTS financial_periods (ticker TEXT, period TEXT, fiscal_year INTEGER, fiscal_quarter INTEGER, PRIMARY KEY (ticker, period));
CREATE TABLE IF NOT EXISTS financial_metrics (ticker TEXT, period TEXT, metric TEXT, value REAL, unit TEXT, source TEXT, status TEXT, basis TEXT, as_of_date TEXT, fetched_at TEXT,
  PRIMARY KEY (ticker, period, metric, as_of_date));
CREATE TABLE IF NOT EXISTS valuation_metrics (ticker TEXT, as_of_date TEXT, metric TEXT, value REAL, status TEXT, reason TEXT, basis TEXT, formula TEXT, dependencies TEXT, source_version TEXT,
  PRIMARY KEY (ticker, as_of_date, metric));
CREATE TABLE IF NOT EXISTS industry_snapshots (run_date TEXT, industry_code TEXT, sub_industry_code TEXT, series TEXT, special_industry TEXT, payload TEXT,
  PRIMARY KEY (run_date, industry_code, sub_industry_code, series));
CREATE TABLE IF NOT EXISTS comparable_sets (id TEXT PRIMARY KEY, created_at TEXT, purpose TEXT, note TEXT);
CREATE TABLE IF NOT EXISTS comparable_set_members (set_id TEXT, ticker TEXT, state TEXT, reason TEXT, PRIMARY KEY (set_id, ticker));
CREATE TABLE IF NOT EXISTS forward_estimates (ticker TEXT, period TEXT, value REAL, status TEXT, source TEXT, source_note TEXT, as_of_date TEXT, PRIMARY KEY (ticker, period, status, as_of_date));
CREATE TABLE IF NOT EXISTS snapshot_publications (run_date TEXT, published_at TEXT, status TEXT, manifest TEXT, PRIMARY KEY (run_date, published_at));
"""


def connect(path: Path) -> sqlite3.Connection:
    path = Path(path)
    if str(path) != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.executescript(SCHEMA)
    for e in ENDPOINTS.values():
        conn.execute("INSERT OR IGNORE INTO data_sources VALUES (?,?,?,?,1)", (e.id, e.market, e.dataset, e.url))
    conn.commit()
    return conn


def record_run(conn, run_id: str, run_date: str, started_at: str, status: str, requests: int, reused: int, notes: str = "") -> None:
    conn.execute("INSERT OR REPLACE INTO ingestion_runs VALUES (?,?,?,?,?,?,?)", (run_id, run_date, started_at, status, requests, reused, notes))
    conn.commit()


def persist_build(conn, inp: Inputs, res: BuildResult) -> dict:
    d = res.run_date
    for eid, m in inp.raw_meta.items():
        conn.execute("INSERT OR REPLACE INTO raw_files VALUES (?,?,?,?,?,?)", (d, eid, m["sha256"], m["fetchedAt"], m["rows"], m["url"]))
    seen = {c.ticker for c in inp.companies}
    by = {r["ticker"]: r for r in res.companies}
    for c in inp.companies:
        r = by.get(c.ticker)
        if r is None:
            continue
        conn.execute("INSERT OR REPLACE INTO companies VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                     (d, c.ticker, c.market, c.companyName, c.officialIndustryCode, r["industry"]["name"], c.listedDate, c.sharesOutstanding,
                      r["financialForm"], c.source, c.asOfDate))
        conn.execute("INSERT OR REPLACE INTO company_industries VALUES (?,?,?,?,?,?)", (d, c.ticker, "L1", c.officialIndustryCode, r["industry"]["name"], "OFFICIAL"))
        if r["subIndustry"]:
            conn.execute("INSERT OR REPLACE INTO company_industries VALUES (?,?,?,?,?,?)",
                         (d, c.ticker, "L2", r["subIndustry"]["code"], r["subIndustry"]["name"], r["subIndustry"]["assignedBy"]))
    conn.executemany("INSERT OR REPLACE INTO market_prices VALUES (?,?,?,?,?)", [(p.ticker, p.tradeDate, p.close, p.source, p.fetchedAt) for p in inp.prices if p.ticker in seen])
    conn.executemany("INSERT OR REPLACE INTO official_ratios VALUES (?,?,?,?,?,?,?,?)",
                     [(x.ticker, x.tradeDate, x.officialPE, x.officialPB, x.dividendYield, x.basis, x.source, x.fetchedAt) for x in inp.ratios if x.ticker in seen])
    conn.executemany("INSERT OR IGNORE INTO financial_periods VALUES (?,?,?,?)", {(m.ticker, m.period, m.fiscalYear, m.fiscalQuarter) for m in inp.eps + inp.monthly if m.ticker in seen})
    conn.executemany("INSERT OR REPLACE INTO financial_metrics VALUES (?,?,?,?,?,?,?,?,?,?)",
                     [(m.ticker, m.period, m.metric, m.value, m.unit, m.source, m.status, m.basis, m.asOfDate or "", m.fetchedAt) for m in inp.eps + inp.monthly if m.ticker in seen])
    conn.executemany("INSERT OR REPLACE INTO valuation_metrics VALUES (?,?,?,?,?,?,?,?,?,?)",
                     [(m.ticker, m.asOfDate, m.metric, m.value, m.status, m.reason, m.basis, m.formula, json.dumps(m.dependencies, ensure_ascii=False, sort_keys=True), m.sourceVersion)
                      for m in res.derived])
    conn.executemany("INSERT OR REPLACE INTO industry_snapshots VALUES (?,?,?,?,?,?)",
                     [(d, s.industryCode, s.subIndustryCode or "", s.metricSeries, s.specialIndustry, json.dumps(asdict(s), ensure_ascii=False, sort_keys=True)) for s in res.industry_snapshots])
    conn.commit()
    return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in
            ("companies", "market_prices", "official_ratios", "financial_metrics", "valuation_metrics", "industry_snapshots", "raw_files")}
