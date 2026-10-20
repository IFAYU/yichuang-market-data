"""Listed-ETF closing quotes (Phase 8B): an ISOLATED resource next to the company snapshot, never a fake "company".

    etf/manifest.json                        the only "latest" pointer of THIS resource (the company manifest.json is not touched)
    etf/releases/<sha[:12]>/etf-quotes.json  one immutable release

Sources (official TWSE OpenAPI, read-only GET, official JSON only):
    exchangeReport/STOCK_DAY_ALL   Date (ROC yyyMMdd), Code, ClosingPrice ...   every listed security of the latest trade day, ETFs included
    opendata/t187ap47_L            fund master list: fund code, fund type ...  the OFFICIAL list that says which code is a fund and of which kind

What is an ETF is decided by the master list, never by the shape of the code. A code that is in the price file but not in the master list is not an ETF here.
Only domestic-component equity ETFs (index or active-equity) are SUPPORTED in this version; every other fund type is listed with an explicit NOT_SUPPORTED reason
and NO price. An empty / zero / malformed ClosingPrice is `null` (no close), never 0, and never replaced by a bid/ask. Prices are decimal STRINGS (no float).

`sha256` in the manifest only proves that the downloaded file equals what was published (content consistency). The same publisher computes it, so it is NOT an
independent authentication of the source.

Public artifact content: exchange facts only. It never contains a holding, a client or a person.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

from . import daily as D
from .publish_state import Outcome, classify_outcome
from .snapshot.build import dumps, sha, write_atomic
from .trading_calendar import CLOSED, TradingCalendar, roc_to_date

ETF_SCHEMA_VERSION = "1.0.0"
KIND = "TW_LISTED_ETF_CLOSING_QUOTES"
SOURCE_ID = "TWSE_OPENAPI_STOCK_DAY_ALL"
PRICE_URL = "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"
MASTER_URL = "https://openapi.twse.com.tw/v1/opendata/t187ap47_L"
FILE = "etf-quotes.json"
MIN_PRICE_ROWS, MIN_MASTER_ROWS, MIN_SUPPORTED = 800, 150, 40  # a source that shrank like this is broken, not "a quiet day"
_PRICE = re.compile(r"^\d{1,7}(\.\d{1,4})?$")
_CODE = re.compile(r"^[0-9A-Z]{4,6}$")

K_CODE, K_TYPE, K_NAME = "基金代號", "基金類型", "基金簡稱"  # 基金代號 基金類型 基金簡稱

# official fund type wording -> (category, supportStatus, reason)
FUND_TYPES = {
    "國內成分證券指數股票型基金": ("DOMESTIC_EQUITY_INDEX", "SUPPORTED", None),
    "國內成分證券主動式交易所交易基金(股票)": ("DOMESTIC_EQUITY_ACTIVE", "SUPPORTED", None),
    "國外成分證券指數股票型基金": ("OVERSEAS_COMPONENTS", "NOT_SUPPORTED", "OVERSEAS_COMPONENTS: foreign constituents (bond ETFs are included in this type); not verified in this version"),
    "國外成份/加掛外幣證券指數股票型基金": ("OVERSEAS_COMPONENTS", "NOT_SUPPORTED", "OVERSEAS_COMPONENTS / FOREIGN_CURRENCY_LINE: the price may not be in TWD"),
    "國外成分證券主動式交易所交易基金(股票)": ("OVERSEAS_COMPONENTS", "NOT_SUPPORTED", "OVERSEAS_COMPONENTS: active ETF with foreign constituents"),
    "國外成分證券主動式交易所交易基金(債券)": ("BOND", "NOT_SUPPORTED", "BOND: bond ETF"),
    "國外成分證券平衡型指數股票型基金": ("BALANCED", "NOT_SUPPORTED", "BALANCED: stock + bond mix"),
    "境外指數股票型基金": ("OVERSEAS_COMPONENTS", "NOT_SUPPORTED", "OFFSHORE_ETF"),
    "連結式證券指數股票型基金": ("LINKED", "NOT_SUPPORTED", "LINKED_ETF"),
    "槓桿/反向指數股票型基金": ("LEVERAGED_INVERSE", "NOT_SUPPORTED", "LEVERAGED_OR_INVERSE: path-dependent product"),
    "指數股票型期貨信託基金": ("FUTURES_TRUST", "NOT_SUPPORTED", "FUTURES_TRUST: commodity / futures ETF"),
}
BOND_CHAR = "債"  # 債


class EtfSourceError(ValueError):
    """The official payload is not usable. Nothing is built or published."""


def _need(rows, keys, what):
    if not isinstance(rows, list) or not rows or not all(isinstance(r, dict) for r in rows):
        raise EtfSourceError(f"{what}: not a non-empty JSON array of objects")
    missing = [k for k in keys if k not in rows[0]]
    if missing:
        raise EtfSourceError(f"{what}: schema drift, missing fields {missing}")


def parse_price(raw) -> tuple:
    """-> (decimal string | None, status). '' is NO_CLOSE (no trade); 0 / negative / junk is INVALID. Never 0, never a guess."""
    s = str(raw if raw is not None else "").strip().replace(",", "")
    if s == "" or s in ("--", "-"):
        return None, "NO_CLOSE"
    if not _PRICE.match(s) or float(s) <= 0:
        return None, "INVALID"
    return (s.rstrip("0").rstrip(".") if "." in s else s), "OK"


def classify_fund(fund_type: str, name: str = "") -> tuple:
    cat, status, reason = FUND_TYPES.get(str(fund_type or "").strip(), ("UNKNOWN", "NOT_SUPPORTED", "UNKNOWN_FUND_TYPE: not in the supported map"))
    if status == "SUPPORTED" and BOND_CHAR in str(name):
        return "BOND", "NOT_SUPPORTED", "BOND_SUSPECTED: the official short name mentions bonds"
    return cat, status, reason


def build_quotes(price_rows: list, master_rows: list, retrieved_at: str, generated_at: str, evidence: Optional[dict] = None) -> dict:
    _need(price_rows, ("Date", "Code", "ClosingPrice"), "STOCK_DAY_ALL")
    _need(master_rows, (K_CODE, K_TYPE, K_NAME), "t187ap47_L")
    if len(price_rows) < MIN_PRICE_ROWS:
        raise EtfSourceError(f"STOCK_DAY_ALL: only {len(price_rows)} rows (< {MIN_PRICE_ROWS}); refusing")
    if len(master_rows) < MIN_MASTER_ROWS:
        raise EtfSourceError(f"t187ap47_L: only {len(master_rows)} rows (< {MIN_MASTER_ROWS}); refusing")
    dates = {str(r.get("Date", "")).strip() for r in price_rows}
    if len(dates) != 1:
        raise EtfSourceError(f"STOCK_DAY_ALL: rows carry {len(dates)} different trade dates {sorted(dates)[:3]}; refusing to mix days")
    price_date = roc_to_date(next(iter(dates)))
    if price_date is None:
        raise EtfSourceError(f"STOCK_DAY_ALL: unreadable trade date {sorted(dates)}")
    by_code = {}
    for r in price_rows:
        c = str(r.get("Code", "")).strip()
        if c in by_code:
            raise EtfSourceError(f"STOCK_DAY_ALL: duplicate code {c}")
        by_code[c] = r
    seen, instruments = set(), []
    for m in master_rows:
        sym = str(m.get(K_CODE, "")).strip()
        if not _CODE.match(sym) or sym in seen:
            raise EtfSourceError(f"t187ap47_L: invalid or duplicate fund code {sym!r}")
        seen.add(sym)
        category, support, reason = classify_fund(m.get(K_TYPE), m.get(K_NAME))
        row = by_code.get(sym)
        price, pstatus = (None, "NOT_QUOTED_BY_SOURCE") if row is None else parse_price(row.get("ClosingPrice"))
        rec = {"symbol": sym, "market": "TWSE", "instrumentType": "ETF", "etfCategory": category, "supportStatus": support,
               "shortName": str(m.get(K_NAME, "")).strip(), "fundType": str(m.get(K_TYPE, "")).strip()}
        if support == "SUPPORTED":
            rec.update({"currency": "TWD", "price": price, "priceStatus": pstatus, "priceDate": price_date.isoformat() if price else None,
                        "retrievedAt": retrieved_at, "source": SOURCE_ID})
        else:  # no price is published for what this version does not support
            rec.update({"currency": None, "price": None, "priceStatus": "NOT_SUPPORTED", "priceDate": None, "retrievedAt": retrieved_at, "source": SOURCE_ID,
                        "notSupportedReason": reason})
        instruments.append(rec)
    instruments.sort(key=lambda x: x["symbol"])
    supported = [i for i in instruments if i["supportStatus"] == "SUPPORTED"]
    if len(supported) < MIN_SUPPORTED:
        raise EtfSourceError(f"only {len(supported)} supported ETFs (< {MIN_SUPPORTED}); refusing")
    priced = [i for i in supported if i["price"] is not None]
    if len(priced) < len(supported) * 0.8:
        raise EtfSourceError(f"only {len(priced)}/{len(supported)} supported ETFs carry a close; the source looks broken")
    return {
        "schemaVersion": ETF_SCHEMA_VERSION, "kind": KIND, "market": "TWSE", "currency": "TWD", "timezone": "Asia/Taipei",
        "priceDate": price_date.isoformat(), "retrievedAt": retrieved_at, "generatedAt": generated_at,
        "priceDefinition": "TWSE official closing price (ClosingPrice) of the trade day; empty = no close (null), never 0; no bid/ask is ever used",
        "source": {"id": SOURCE_ID, "priceEndpoint": PRICE_URL, "masterEndpoint": MASTER_URL, "evidence": evidence or {},
                   "terms": {"license": "Government Open Data License v1 (the TWSE OpenAPI page points to it)", "attribution": "Source: Taiwan Stock Exchange (TWSE OpenAPI)",
                             "redistributionStatus": "UNCONFIRMED_FOR_THESE_ENDPOINTS",
                             "note": "Whether re-publication on a public static site is covered for these endpoints is NOT confirmed (legal review pending); do not widen the audience of this artifact."}},
        "integrity": {"sha256Semantics": "CONTENT_CONSISTENCY_ONLY: proves the bytes equal what the publisher wrote; the publisher computes it, so it is not an independent source authentication"},
        "counts": {"funds": len(instruments), "supported": len(supported), "supportedWithClose": len(priced), "supportedWithoutClose": len(supported) - len(priced),
                   "notSupported": len(instruments) - len(supported)},
        "instruments": instruments,
    }


def content_hash(doc: dict) -> str:
    """sha256 of the exchange facts only: the run-time stamps (retrievedAt / generatedAt) are left out, so re-reading the same source is recognised as the same data."""
    def strip(o):
        if isinstance(o, dict):
            return {k: strip(v) for k, v in o.items() if k not in ("retrievedAt", "generatedAt")}
        return [strip(v) for v in o] if isinstance(o, list) else o
    return sha(dumps(strip(doc)))


# ---- publication --------------------------------------------------------------------------------------------------------------------
def read_etf_manifest(root: Path) -> Optional[dict]:
    p = Path(root) / "etf" / "manifest.json"
    try:
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None
    except json.JSONDecodeError:
        return None


@dataclass
class EtfPublication:
    outcome: Outcome
    manifest: Optional[dict] = None


HISTORY_FILE = "etf-history.json"  # Phase 8P: optional second file of the same release (see etf_history.py)


def publish_etf(doc: Optional[dict], out_root: Path, now: datetime, cal: TradingCalendar, published_at: Optional[str] = None, source_error: Optional[str] = None,
                final: bool = True, allow_republish: bool = False, history_doc: Optional[dict] = None) -> EtfPublication:
    """Decide and (only for PUBLISHED) write: release first, re-read and verify, manifest LAST. Every other outcome leaves the Last Known Good byte-identical.
    With `history_doc` (Phase 8P) the release also carries etf-history.json; without it the release is exactly what it was before (release id = quotes hash)."""
    out_root = Path(out_root)
    now = D.to_taipei(now)
    previous = read_etf_manifest(out_root)
    prev_date = D.parse_market_date((previous or {}).get("priceDate"))
    expected, unknown = D.target_date("TWSE", now, cal)
    src_date = D.parse_market_date((doc or {}).get("priceDate")) if doc else None
    common = dict(source_date=src_date, published_date=prev_date, expected_date=expected, today=now.date(), final=final, calendar_known=not unknown)
    if source_error or doc is None:
        return EtfPublication(classify_outcome(**common, source_error=source_error or "no document"))
    if src_date is not None and cal.status(src_date, "TWSE") == CLOSED:
        return EtfPublication(classify_outcome(**{**common, "source_date": None}, source_error=f"MARKET_DATE_ON_A_CLOSED_DAY: {src_date}"))
    blob = dumps(doc)
    h = sha(blob)
    advanced = prev_date is None or (src_date is not None and src_date > prev_date)
    if previous and src_date == prev_date and not allow_republish and previous.get("contentSha256") != content_hash(doc):
        # same trade date, other content: a silent correction must be looked at by a person
        return EtfPublication(Outcome("FAILED", "SAME_DATE_DIFFERENT_CONTENT", False, f"trade date {src_date} is already published with other content"))
    if not advanced and not allow_republish:
        return EtfPublication(classify_outcome(**common))  # NO_UPDATE / PENDING / FAILED(regressed, expected day missing)
    pre = classify_outcome(**common, published_ok=True)  # the hard failures (malformed / future / regressed) still win over "publish"
    if pre.status != "PUBLISHED":
        return EtfPublication(pre)
    hblob = dumps(history_doc) if history_doc is not None else None
    hh = sha(hblob) if hblob is not None else None
    release = h[:12] if hh is None else sha((h + hh).encode("ascii"))[:12]
    rel_dir = out_root / "etf" / "releases" / release
    write_atomic(rel_dir / FILE, blob)
    if hblob is not None:
        write_atomic(rel_dir / HISTORY_FILE, hblob)
    if sha((rel_dir / FILE).read_bytes()) != h or (hblob is not None and sha((rel_dir / HISTORY_FILE).read_bytes()) != hh):
        return EtfPublication(Outcome("FAILED", "GATE_REFUSED", False, "the release on disk does not match what was written"))
    manifest = {"schemaVersion": ETF_SCHEMA_VERSION, "kind": KIND, "release": release, "priceDate": doc["priceDate"], "retrievedAt": doc["retrievedAt"],
                "publishedAt": published_at or now.isoformat(timespec="seconds"), "generatedAt": doc["generatedAt"], "sourceId": SOURCE_ID,
                "files": {FILE: {"path": f"etf/releases/{release}/{FILE}", "sha256": h, "bytes": len(blob)},
                          **({HISTORY_FILE: {"path": f"etf/releases/{release}/{HISTORY_FILE}", "sha256": hh, "bytes": len(hblob)}} if hblob is not None else {})},
                "contentSha256": content_hash(doc),
                "hashSemantics": doc["integrity"]["sha256Semantics"], "counts": doc["counts"]}
    write_atomic(out_root / "etf" / "manifest.json", dumps(manifest))  # LAST
    return EtfPublication(classify_outcome(**common, published_ok=True), manifest)


KEEP_RELEASES = 7  # the published ETF history is about 2 MB, so a daily release must not pile up forever


def prune_releases(out_root: Path, keep: int = KEEP_RELEASES) -> list:
    """Delete old ETF releases from the working tree: always keep the one the manifest points at, then the newest by `generatedAt`; what cannot be read is kept."""
    import shutil
    out_root = Path(out_root)
    root = out_root / "etf" / "releases"
    if not root.is_dir():
        return []
    live = (read_etf_manifest(out_root) or {}).get("release")
    dated, kept = [], set([live] if live else [])
    for d in root.iterdir():
        if not d.is_dir():
            continue
        try:
            dated.append((json.loads((d / FILE).read_text(encoding="utf-8"))["generatedAt"], d.name))
        except (OSError, ValueError, KeyError):
            kept.add(d.name)
    for _, name in sorted(dated, reverse=True)[:max(keep, 1)]:
        kept.add(name)
    gone = sorted(n for _, n in dated if n not in kept)
    for n in gone:
        shutil.rmtree(root / n)
    return gone
