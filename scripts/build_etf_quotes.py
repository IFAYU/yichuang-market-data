"""Phase 8B: build (and, only into a LOCAL directory, publish) the isolated listed-ETF closing-quote resource from the OFFICIAL TWSE OpenAPI.

    python scripts/build_etf_quotes.py --fetch --api <dir> --out <dir> --calendar-manifest <company manifest.json>
    python scripts/build_etf_quotes.py --api <dir with STOCK_DAY_ALL.json + t187ap47_L.json> --out <dir> --calendar-manifest <file> [--now 2026-10-12T09:00:00+08:00]

Two read-only GETs at most (--fetch). Writes <out>/etf/manifest.json and <out>/etf/releases/<id>/etf-quotes.json ONLY when the outcome is PUBLISHED.
It never touches the company snapshot, never pushes, never uploads anything. Prints one JSON line: {publishStatus, reasonCode, normal, detail, ...}.
Exit code: 0 PUBLISHED / NO_UPDATE, 3 PENDING (a retry is due), 1 FAILED.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import urllib.request
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mitw import daily as D  # noqa: E402
from mitw.etf import MASTER_URL, PRICE_URL, EtfSourceError, build_quotes, publish_etf  # noqa: E402
from mitw.trading_calendar import TradingCalendar  # noqa: E402

UA = {"User-Agent": "yichuang-market-data etf quotes (read-only, official OpenAPI)", "Accept": "application/json"}
FILES = {"prices": ("STOCK_DAY_ALL.json", PRICE_URL), "master": ("t187ap47_L.json", MASTER_URL)}


def fetch_all(api: Path) -> dict:
    api.mkdir(parents=True, exist_ok=True)
    meta = {}
    for key, (name, url) in FILES.items():
        with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=60) as r:
            body = r.read()
            meta[key] = {"url": url, "lastModified": r.headers.get("Last-Modified"), "etag": r.headers.get("ETag")}
        (api / name).write_bytes(body)
    return meta


def evidence_of(api: Path, fetched: dict) -> dict:
    out = {}
    for key, (name, url) in FILES.items():
        raw = (api / name).read_bytes()
        out[key] = {"url": url, "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw), "rows": len(json.loads(raw.decode("utf-8-sig"))),
                    "lastModified": (fetched.get(key) or {}).get("lastModified"), "etag": (fetched.get(key) or {}).get("etag")}
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--api", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--fetch", action="store_true")
    ap.add_argument("--calendar-manifest", required=True, type=Path, help="a company manifest.json whose publicationPolicy.calendar is the trading calendar")
    ap.add_argument("--now", default=None, help="ISO instant (tests / replays); default: the real clock in Asia/Taipei")
    ap.add_argument("--final", action="store_true", help="this is the last scheduled attempt of the day: a missing owed trading day is FAILED instead of PENDING")
    ap.add_argument("--republish", action="store_true")
    a = ap.parse_args(argv)
    now = D.to_taipei(datetime.fromisoformat(a.now)) if a.now else D.to_taipei(datetime.now(D.TAIPEI))
    cal_json = ((json.loads(a.calendar_manifest.read_text(encoding="utf-8")).get("publicationPolicy") or {}).get("calendar"))
    cal = TradingCalendar.from_json(cal_json) if cal_json else TradingCalendar()
    fetched, doc, err = {}, None, None
    try:
        if a.fetch:
            fetched = fetch_all(a.api)
        load = lambda n: json.loads((a.api / n).read_text(encoding="utf-8-sig"))  # noqa: E731
        retrieved = now.isoformat(timespec="seconds")
        doc = build_quotes(load(FILES["prices"][0]), load(FILES["master"][0]), retrieved, retrieved, evidence_of(a.api, fetched))
    except (EtfSourceError, OSError, ValueError) as e:
        err = f"{type(e).__name__}: {e}"[:300]
    res = publish_etf(doc, a.out, now, cal, source_error=err, final=a.final, allow_republish=a.republish)
    print(json.dumps({**res.outcome.to_json(), "priceDate": (doc or {}).get("priceDate"), "release": (res.manifest or {}).get("release"), "evaluatedAt": now.isoformat(timespec="seconds")},
                     ensure_ascii=False))
    return {"PUBLISHED": 0, "NO_UPDATE": 0, "PENDING": 3}.get(res.outcome.status, 1)


if __name__ == "__main__":
    raise SystemExit(main())
