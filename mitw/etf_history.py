"""Close history of the supported listed ETFs (Phase 8P): the series the app needs for 20 / 60 / 120 / 240-day deviation.

It is published INSIDE the ETF resource (same release folder as etf-quotes.json), never a company snapshot:

    etf/releases/<id>/etf-history.json    {"symbols": {"0050": [["2026-10-19", "108.15"], ...], ...}}   closes as decimal STRINGS, ascending dates

How it grows (no extra requests in the regular run):
  - every ETF publication appends that day's official close (the same STOCK_DAY_ALL price already fetched for etf-quotes.json);
  - ONE-TIME seeding of the past is a manual command (`etf-history-seed`, polite 3 s gap, the TWSE site's monthly STOCK_DAY file per ETF) that the Owner runs on purpose; it is never scheduled.
Closes are the exchange's unadjusted closing prices: a split shows as a jump, and the consumer decides how to treat it (the app divides earlier prices by the ratio, like build_etf_backtest.py).
Whether re-publication of these closes is covered by the source's terms is NOT confirmed (see etf.py); the artifact says so.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Callable, Iterable, Optional

from .etf import read_etf_manifest

HISTORY_FILE = "etf-history.json"
HISTORY_KIND = "TW_LISTED_ETF_CLOSE_HISTORY"
HISTORY_SCHEMA_VERSION = "1.0.0"
KEEP_DAYS = 420  # trading days kept per ETF (enough for a 240-day average plus the deviation history)
_ISO = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_CLOSE = re.compile(r"^\d{1,7}(\.\d{1,4})?$")

History = dict  # symbol -> {iso date -> close string}


def roc_slash_to_iso(s: str) -> Optional[str]:
    m = re.match(r"^(\d{2,3})/(\d{2})/(\d{2})$", str(s).strip())
    if not m:
        return None
    iso = f"{int(m.group(1)) + 1911}-{m.group(2)}-{m.group(3)}"
    try:
        from datetime import date
        date.fromisoformat(iso)
    except ValueError:
        return None
    return iso


def parse_month(doc: dict) -> list:
    """(iso date, close string) of one monthly STOCK_DAY JSON; a day without a close is skipped, never filled."""
    if not isinstance(doc, dict) or doc.get("stat") != "OK" or not isinstance(doc.get("fields"), list) or not isinstance(doc.get("data"), list):
        return []
    f = doc["fields"]
    if "日期" not in f or "收盤價" not in f:
        return []
    di, ci = f.index("日期"), f.index("收盤價")
    out = []
    for row in doc["data"]:
        if not isinstance(row, list) or len(row) <= max(di, ci):
            continue
        d = roc_slash_to_iso(row[di])
        c = str(row[ci]).replace(",", "").strip()
        if d and _CLOSE.match(c) and float(c) > 0:
            out.append((d, c))
    return out


def merge(history: History, symbol: str, rows: Iterable) -> int:
    """add rows (later values for the same date replace earlier ones); returns how many dates are new"""
    cur = history.setdefault(symbol, {})
    new = 0
    for d, c in rows:
        if d not in cur:
            new += 1
        cur[d] = c
    return new


def append_quotes(history: History, quotes: dict) -> int:
    """append the day's official close of every SUPPORTED ETF that has one"""
    n = 0
    for i in quotes.get("instruments", []):
        if i.get("supportStatus") == "SUPPORTED" and i.get("price") and i.get("priceDate"):
            n += merge(history, i["symbol"], [(i["priceDate"], i["price"])])
    return n


def load_history(out_root: Path) -> History:
    """the history of the currently published ETF release (empty when there is none or it cannot be read)"""
    m = read_etf_manifest(Path(out_root))
    f = ((m or {}).get("files") or {}).get(HISTORY_FILE)
    if not f:
        return {}
    try:
        doc = json.loads((Path(out_root) / f["path"]).read_text(encoding="utf-8"))
        return {s: {d: c for d, c in rows} for s, rows in doc["symbols"].items()}
    except (OSError, ValueError, KeyError, TypeError):
        return {}


def build_history_doc(history: History, symbols: Iterable, generated_at: str, price_date: str) -> dict:
    syms = {}
    for s in sorted(set(symbols)):
        rows = sorted(history.get(s, {}).items())[-KEEP_DAYS:]
        if rows:
            syms[s] = [[d, c] for d, c in rows]
    return {
        "schemaVersion": HISTORY_SCHEMA_VERSION, "kind": HISTORY_KIND, "market": "TWSE", "currency": "TWD", "timezone": "Asia/Taipei",
        "priceDate": price_date, "generatedAt": generated_at,
        "closeDefinition": "TWSE official closing price per trade day, NOT adjusted for splits or dividends; empty / non-numeric closes are skipped, never filled",
        "terms": {"redistributionStatus": "UNCONFIRMED_FOR_THESE_ENDPOINTS", "note": "Whether re-publication of these closes on a public static site is covered is NOT confirmed (legal review pending)."},
        "symbols": syms,
    }


def seed_from_months(history: History, symbols: Iterable, months: list, fetch_month: Callable[[str, str], Optional[dict]], sleep: Callable[[float], None] = time.sleep, gap_s: float = 3.0) -> dict:
    """ONE-TIME manual seeding. `fetch_month(symbol, 'YYYYMM')` returns the parsed JSON (or None on a refusal); a 403/429 style refusal must raise to stop the run.
    Returns {"requests": n, "added": n, "failed": [(symbol, ym), ...]}. Months already complete in `history` are still requested only for the current month."""
    requests = added = 0
    failed = []
    for s in symbols:
        for ym in months:
            doc = fetch_month(s, ym)
            requests += 1
            if doc is None:
                failed.append((s, ym))
            else:
                added += merge(history, s, parse_month(doc))
            sleep(gap_s)
    return {"requests": requests, "added": added, "failed": failed}


def month_list(today_iso: str, n: int) -> list:
    y, m = int(today_iso[:4]), int(today_iso[5:7])
    out = []
    for _ in range(n):
        out.append(f"{y}{m:02d}")
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    return out[::-1]
