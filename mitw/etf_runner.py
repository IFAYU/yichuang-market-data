"""Entry points of the ETF resource (Phase 8P): publish today's quotes + history, and the ONE-TIME manual history seeding.

Network discipline (same spirit as providers/http.py): official read-only GETs only, a fixed User-Agent, a gap between requests, no retry after 403 / 429 / HTML, and no
rotation of anything. The seeding command is run by a person on purpose; nothing schedules it.
"""
from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from . import etf as E
from . import etf_history as H

from .daily_runner import MANUAL_CLOSURES, fetch_holiday_rows, load_calendar
from .providers.http import now_taipei

UA = "yichuang-market-data (read-only, official open data)"
MONTH_URL = "https://www.twse.com.tw/rwd/zh/afterTrading/STOCK_DAY?date={ym}01&stockNo={sym}&response=json"


class SourceRefused(Exception):
    """403 / 429 / HTML: the source says stop; the run ends here."""


def _get_json(url: str):
    import requests
    r = requests.get(url, headers={"User-Agent": UA, "Accept": "application/json"}, timeout=30)
    if r.status_code in (403, 429):
        raise SourceRefused(f"HTTP {r.status_code} from {url}")
    if r.status_code != 200 or r.text.lstrip().startswith("<"):
        return None
    try:
        return r.json()
    except ValueError:
        return None


def fetch_month_json(symbol: str, ym: str) -> Optional[dict]:
    return _get_json(MONTH_URL.format(ym=ym, sym=symbol))


def run_etf_publish(out_root: Path, now: Optional[datetime] = None, fetch: Callable[[str], object] = _get_json, calendar_rows: Optional[list] = None,
                    allow_republish: bool = False) -> dict:
    """fetch the two official files, build the quotes, append the day's closes to the published history, publish both (the manifest last)"""
    out_root = Path(out_root)
    now = now or now_taipei()
    stamp = now.isoformat(timespec="seconds")
    try:
        prices, master = fetch(E.PRICE_URL), fetch(E.MASTER_URL)
        doc = E.build_quotes(prices, master, stamp, stamp, {}) if isinstance(prices, list) and isinstance(master, list) else None
        err = None if doc else "an official file was not a JSON list"
    except (E.EtfSourceError, SourceRefused) as e:
        doc, err = None, str(e)
    rows = calendar_rows if calendar_rows is not None else fetch_holiday_rows()
    cal, cal_source = load_calendar(rows, None, MANUAL_CLOSURES, stamp)
    hist_doc = None
    if doc is not None:
        hist = H.load_history(out_root)
        H.append_quotes(hist, doc)
        hist_doc = H.build_history_doc(hist, [i["symbol"] for i in doc["instruments"] if i["supportStatus"] == "SUPPORTED"], stamp, doc["priceDate"])
    pub = E.publish_etf(doc, out_root, now, cal, source_error=err, allow_republish=allow_republish, history_doc=hist_doc)
    return {"status": pub.outcome.status, "reason": getattr(pub.outcome, "reason", None), "calendar": cal_source, "release": (pub.manifest or {}).get("release")}


def supported_symbols(out_root: Path) -> list:
    m = E.read_etf_manifest(Path(out_root))
    f = ((m or {}).get("files") or {}).get(E.FILE)
    if not f:
        return []
    doc = json.loads((Path(out_root) / f["path"]).read_text(encoding="utf-8"))
    return sorted(i["symbol"] for i in doc["instruments"] if i["supportStatus"] == "SUPPORTED")


def run_history_seed(out_root: Path, months: int = 14, symbols: Optional[list] = None, today_iso: Optional[str] = None,
                     fetch_month: Callable[[str, str], Optional[dict]] = fetch_month_json, sleep: Callable[[float], None] = time.sleep, gap_s: float = 3.0) -> dict:
    """ONE-TIME seeding of the past closes of the supported ETFs into the published release (re-publishes the same quotes with a fuller history). Manual only."""
    out_root = Path(out_root)
    syms = symbols or supported_symbols(out_root)
    if not syms:
        raise SystemExit("no published ETF quotes yet: run `etf-publish` first (or pass --symbols)")
    hist = H.load_history(out_root)
    today = today_iso or now_taipei().date().isoformat()
    cur = today[:7].replace('-', '')
    # a past month that already has data for this ETF is not requested again (only the current month is refreshed)
    skip = lambda s, ym: ym != cur and any(d.startswith(f'{ym[:4]}-{ym[4:]}') for d in hist.get(s, {}))  # noqa: E731
    r = H.seed_from_months(hist, syms, H.month_list(today, months), fetch_month, sleep=sleep, gap_s=gap_s, skip=skip)
    m = E.read_etf_manifest(out_root)
    qpath = Path(out_root) / m["files"][E.FILE]["path"]
    doc = json.loads(qpath.read_text(encoding="utf-8"))
    stamp = (today_iso and f"{today_iso}T00:00:00+08:00") or now_taipei().isoformat(timespec="seconds")
    hd = H.build_history_doc(hist, syms, stamp, doc["priceDate"])
    from .snapshot.build import dumps, sha, write_atomic
    hblob = dumps(hd)
    hh = sha(hblob)
    rel = sha((m["files"][E.FILE]["sha256"] + hh).encode("ascii"))[:12]
    rel_dir = Path(out_root) / "etf" / "releases" / rel
    write_atomic(rel_dir / E.FILE, qpath.read_bytes())
    write_atomic(rel_dir / E.HISTORY_FILE, hblob)
    m2 = {**m, "release": rel, "files": {E.FILE: {**m["files"][E.FILE], "path": f"etf/releases/{rel}/{E.FILE}"},
                                          E.HISTORY_FILE: {"path": f"etf/releases/{rel}/{E.HISTORY_FILE}", "sha256": hh, "bytes": len(hblob)}}}
    write_atomic(Path(out_root) / "etf" / "manifest.json", dumps(m2))  # LAST
    return {**r, "symbols": len(syms), "release": rel, "failed": [f"{s}:{ym}" for s, ym in r["failed"]]}
