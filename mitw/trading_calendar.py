"""Trading calendar for the daily publication policy (Phase 3I.1 / 3I.2).

Three answers only, never a guess:  KNOWN_TRADING_DAY (TRADING) / KNOWN_NON_TRADING_DAY (CLOSED) / UNKNOWN.
Source hierarchy:
  1. the exchange's own closure list: TWSE OpenAPI  /holidaySchedule/holidaySchedule  ("有價證券集中交易市場開（休）市日期", official).
     TPEx and 興櫃 follow the same trading days; TPEx's OpenAPI has no separate calendar endpoint (stated assumption, not a documented fact).
  2. EXPLICIT manual closures a maintainer confirmed (typhoon, special closure) from config/manual_closures.json: each one has a date, a market
     scope, a reason, a source/reference note and the time it was added. Nothing is hard-coded and nothing is inferred from a missing file.
  3. otherwise UNKNOWN. A weekday outside the covered years is UNKNOWN, never silently TRADING (no 2027 entries are invented).

Weekends are always closed (the exchanges do not open on make-up working Saturdays). The calendar is evaluated by the PUBLICATION pipeline and
published as plain data inside the manifest's publicationPolicy, so the browser never calls an exchange or a holiday API.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Iterable, Optional

TRADING = "TRADING"      # KNOWN_TRADING_DAY
CLOSED = "CLOSED"        # KNOWN_NON_TRADING_DAY
UNKNOWN = "UNKNOWN"
MARKETS = ("TWSE", "TPEX", "EMERGING")

# "國曆新年開始交易日" / "農曆春節前最後交易日" / "農曆春節後開始交易日" are TRADING days that the official list mentions for information.
_TRADING_NAME = re.compile(r"(開始交易日|最後交易日)")


def roc_to_date(s) -> Optional[date]:
    s = str(s or "").strip()
    if not s.isdigit() or len(s) < 7:
        return None
    try:
        return date(int(s[:-4]) + 1911, int(s[-4:-2]), int(s[-2:]))
    except ValueError:
        return None


@dataclass(frozen=True)
class ManualClosure:
    date: date
    markets: tuple          # a subset of MARKETS, or ("ALL",)
    reason: str
    source: str             # reference note: where the closure was announced / confirmed
    added_at: str

    def applies_to(self, market: Optional[str]) -> bool:
        return "ALL" in self.markets or (market is not None and market in self.markets)

    def to_json(self) -> dict:
        return {"date": self.date.isoformat(), "markets": list(self.markets), "reason": self.reason, "source": self.source, "addedAt": self.added_at}


class ManualClosureError(ValueError):
    pass


def parse_manual_closures(doc: dict) -> tuple:
    """Validate config/manual_closures.json. A malformed entry is an ERROR (never silently skipped): a closure that is ignored would turn a
    non-trading day into a 'failed update'."""
    if not isinstance(doc, dict) or not isinstance(doc.get("closures", []), list):
        raise ManualClosureError("manual closures: expected {\"closures\": [...]}")
    out = []
    for i, c in enumerate(doc.get("closures", [])):
        try:
            d = date.fromisoformat(c["date"])
            markets = tuple(c.get("markets") or ["ALL"])
            if not markets or any(m not in MARKETS + ("ALL",) for m in markets):
                raise ValueError(f"markets {markets}")
            reason, source, added = str(c.get("reason") or "").strip(), str(c.get("source") or "").strip(), str(c.get("addedAt") or "").strip()
            if not reason or not source or not added:
                raise ValueError("reason, source and addedAt are required")
        except (KeyError, TypeError, ValueError) as e:
            raise ManualClosureError(f"manual closure #{i + 1} is invalid: {e}") from e
        out.append(ManualClosure(d, markets, reason, source, added))
    return tuple(out)


def load_manual_closures(path) -> tuple:
    p = Path(path)
    if not p.exists():
        return ()  # no file = no confirmed closures (NOT "everything trades": the calendar still says UNKNOWN where it cannot know)
    return parse_manual_closures(json.loads(p.read_text(encoding="utf-8")))


@dataclass(frozen=True)
class TradingCalendar:
    closed: frozenset = frozenset()          # official closure dates (weekday or weekend; weekends are closed regardless)
    covered_from: Optional[date] = None      # first day the list is authoritative for
    covered_to: Optional[date] = None
    manual: tuple = ()                       # ManualClosure entries
    source: str = ""
    fetched_at: Optional[str] = None

    @property
    def extra_closures(self) -> frozenset:   # kept for the 3I.1 callers: closures that apply to every market
        return frozenset(c.date for c in self.manual if "ALL" in c.markets)

    def status(self, d: date, market: Optional[str] = None) -> str:
        if d.weekday() >= 5:
            return CLOSED
        if any(c.date == d and c.applies_to(market) for c in self.manual):
            return CLOSED
        if self.covered_from is None or self.covered_to is None or not (self.covered_from <= d <= self.covered_to):
            return UNKNOWN
        return CLOSED if d in self.closed else TRADING

    def to_json(self) -> dict:
        return {"source": self.source, "fetchedAt": self.fetched_at,
                "coveredFrom": self.covered_from.isoformat() if self.covered_from else None,
                "coveredTo": self.covered_to.isoformat() if self.covered_to else None,
                "closedDates": sorted(d.isoformat() for d in self.closed if d.weekday() < 5),
                "extraClosures": sorted(d.isoformat() for d in self.extra_closures),
                "manualClosures": [c.to_json() for c in sorted(self.manual, key=lambda c: c.date)]}

    @staticmethod
    def from_json(j: dict) -> "TradingCalendar":
        p = lambda v: date.fromisoformat(v) if isinstance(v, str) and v else None  # noqa: E731
        manual = tuple(ManualClosure(date.fromisoformat(c["date"]), tuple(c["markets"]), c["reason"], c["source"], c["addedAt"]) for c in j.get("manualClosures", []))
        if not manual:  # a calendar published before 3I.2 carried only dates that applied to every market
            manual = tuple(ManualClosure(date.fromisoformat(x), ("ALL",), "legacy extraClosures", "legacy", "") for x in j.get("extraClosures", []))
        return TradingCalendar(frozenset(date.fromisoformat(x) for x in j.get("closedDates", [])), p(j.get("coveredFrom")), p(j.get("coveredTo")), manual, j.get("source", ""), j.get("fetchedAt"))


def from_twse_holiday_schedule(rows: Iterable[dict], fetched_at: Optional[str] = None, extra_closures: Iterable = (), manual: Iterable = ()) -> TradingCalendar:
    """Parse the official holidaySchedule rows. Coverage is the whole calendar year(s) the list contains: a year the list does not mention is UNKNOWN.
    `extra_closures` (plain dates, every market) is the 3I.1 form; `manual` takes ManualClosure entries."""
    closed, years = set(), set()
    for r in rows:
        d = roc_to_date(r.get("Date"))
        if d is None:
            continue
        years.add(d.year)
        if _TRADING_NAME.search(str(r.get("Name") or "")):
            continue  # a trading day the list only mentions (first / last trading day around a closure)
        closed.add(d)
    man = tuple(manual) + tuple(ManualClosure(d, ("ALL",), "extra closure", "caller", "") for d in extra_closures)
    if not years:
        return TradingCalendar(source="TWSE_OPENAPI_holidaySchedule", fetched_at=fetched_at, manual=man)
    return TradingCalendar(frozenset(closed), date(min(years), 1, 1), date(max(years), 12, 31), man, "TWSE_OPENAPI_holidaySchedule", fetched_at)


def trading_days_between(cal: TradingCalendar, after: date, upto: date, market: Optional[str] = None) -> tuple[Optional[int], bool]:
    """Completed trading days in (after, upto]. Returns (count, unknown): unknown=True when any day in the range has an UNKNOWN status."""
    n, d = 0, after
    while d < upto:
        d += timedelta(days=1)
        s = cal.status(d, market)
        if s == UNKNOWN:
            return None, True
        if s == TRADING:
            n += 1
    return n, False
