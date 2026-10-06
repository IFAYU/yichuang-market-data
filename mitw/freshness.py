"""Data freshness: "the system may be old, but it must never not know it is old."

Freshness is judged on the MARKET date inside the data (marketAsOf), never on when the file was built. A snapshot generated
today that still holds 10-day-old prices is SEVERELY_STALE.

Trading-day policy (MVP, conservative, with a stated limitation):
  * A trading day is Monday-Friday that is not in `closures` (extra closure dates a maintainer can add).
  * Taiwan exchange holidays are NOT modelled. Counting a holiday as a trading day makes the data look older than it is, so the
    error direction is "warn earlier", never "claim fresh when it is not". This is recorded in `calendarNote` on every result.
  * "Expected latest trade date": after the daily publication cut-off (22:00 Asia/Taipei, an assumption: TWSE's open-data feed was
    still on the previous day at 19:13 on 2026-10-06) the expected latest date is today (if a trading day); before it, or on a
    weekend, it is the previous trading day. That separates "today has not closed / not published yet" (no penalty) from "a newer
    date should exist and we do not have it" (lag grows).

Policy (market prices, official P/E, industry statistics), lag = trading days between the OLDER market's date and the expected date:
  0-1 FRESH   2-5 STALE   >5 SEVERELY_STALE   unparseable / missing / in the future -> UNKNOWN
Financial statements are judged separately against the legal filing calendar, not with the price thresholds.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Iterable, Optional

FRESH = "FRESH"
STALE = "STALE"
SEVERELY_STALE = "SEVERELY_STALE"
UNKNOWN = "UNKNOWN"

CALENDAR_NOTE = "weekends only; exchange holidays are not modelled (conservative: a holiday counts as a missed trading day)"


@dataclass(frozen=True)
class FreshnessPolicy:
    """All thresholds live here (and in src/valuation-lab/market-data/freshness.ts), never in the UI."""

    fresh_max_lag: int = 1  # trading days
    stale_max_lag: int = 5
    publish_cutoff: time = time(22, 0)  # Asia/Taipei: after this, today's close is expected to be published by both exchanges
    # financial statements: filing deadlines (month, day) of each quarter's report, and a grace period
    financial_deadlines: tuple = ((5, 15), (8, 14), (11, 14), (3, 31))  # Q1, Q2, Q3, Q4 (Q4 is due the following year)
    financial_grace_days: int = 7


POLICY = FreshnessPolicy()


def parse_date(v) -> Optional[date]:
    if isinstance(v, date) and not isinstance(v, datetime):
        return v
    if not isinstance(v, str):
        return None
    try:
        return date.fromisoformat(v[:10])
    except ValueError:
        return None


def is_trading_day(d: date, closures: frozenset = frozenset()) -> bool:
    return d.weekday() < 5 and d not in closures


def previous_trading_day(d: date, closures: frozenset = frozenset()) -> date:
    x = d - timedelta(days=1)
    while not is_trading_day(x, closures):
        x -= timedelta(days=1)
    return x


def trading_days_between(a: date, b: date, closures: frozenset = frozenset()) -> int:
    """Trading days in the half-open range (a, b]: how many sessions happened AFTER a up to and including b."""
    if b <= a:
        return 0
    n, x = 0, a
    while x < b:
        x += timedelta(days=1)
        if is_trading_day(x, closures):
            n += 1
    return n


def expected_latest_trade_date(now: datetime, closures: frozenset = frozenset(), policy: FreshnessPolicy = POLICY) -> date:
    today = now.date()
    if is_trading_day(today, closures) and now.time() >= policy.publish_cutoff:
        return today
    return previous_trading_day(today, closures)


def classify_market(market_as_of: dict, now: datetime, closures: frozenset = frozenset(), policy: FreshnessPolicy = POLICY) -> dict:
    """market_as_of: {"TWSE": "YYYY-MM-DD", "TPEX": "YYYY-MM-DD"}. Returns a JSON-serialisable verdict."""
    expected = expected_latest_trade_date(now, closures, policy)
    parsed = {m: parse_date(v) for m, v in market_as_of.items()}
    base = {"expectedLatestTradeDate": expected.isoformat(), "calendarNote": CALENDAR_NOTE,
            "marketAsOf": {m: (d.isoformat() if d else None) for m, d in parsed.items()}}
    if not parsed or any(d is None for d in parsed.values()):
        return {**base, "status": UNKNOWN, "lagTradingDays": None, "reason": "a market date is missing or unparseable"}
    if any(d > now.date() for d in parsed.values()):
        return {**base, "status": UNKNOWN, "lagTradingDays": None, "reason": "a market date is in the future (clock or data problem)"}
    oldest = min(parsed.values())
    lag = trading_days_between(oldest, expected, closures)
    per_market = {m: trading_days_between(d, expected, closures) for m, d in parsed.items()}
    status = FRESH if lag <= policy.fresh_max_lag else STALE if lag <= policy.stale_max_lag else SEVERELY_STALE
    return {**base, "status": status, "lagTradingDays": lag, "perMarketLag": per_market, "oldestMarketDate": oldest.isoformat(),
            "datesDiffer": len({d for d in parsed.values()}) > 1, "reason": None}


def _quarter_index(period: str) -> Optional[int]:
    if not isinstance(period, str) or len(period) != 6 or period[4] != "Q" or not period[:4].isdigit() or period[5] not in "1234":
        return None
    return int(period[:4]) * 4 + int(period[5]) - 1


def expected_financial_period(today: date, policy: FreshnessPolicy = POLICY) -> str:
    """The latest quarter whose statutory filing deadline (plus grace) has passed."""
    best = None
    for year in (today.year - 2, today.year - 1, today.year):
        for q, (mo, dy) in enumerate(policy.financial_deadlines, start=1):
            due_year = year + 1 if q == 4 else year
            due = date(due_year, mo, dy) + timedelta(days=policy.financial_grace_days)
            if due <= today:
                idx = year * 4 + q - 1
                best = idx if best is None or idx > best else best
    assert best is not None
    return f"{best // 4}Q{best % 4 + 1}"


def classify_financial(eps_period: Optional[str], now: datetime, policy: FreshnessPolicy = POLICY) -> dict:
    expected = expected_financial_period(now.date(), policy)
    have, want = _quarter_index(eps_period or ""), _quarter_index(expected)
    base = {"epsPeriod": eps_period, "expectedPeriod": expected}
    if have is None:
        return {**base, "status": UNKNOWN, "quartersBehind": None, "reason": "financial period missing or unparseable"}
    behind = max(0, want - have)
    return {**base, "status": FRESH if behind == 0 else STALE if behind == 1 else SEVERELY_STALE, "quartersBehind": behind, "reason": None}


def classify_daily(manifest_like: dict, now: datetime, closures: frozenset = frozenset(), policy: FreshnessPolicy = POLICY) -> dict:
    """The Phase 3D.3 per-trading-day verdict. Since Phase 3D.3A it is DIAGNOSTIC ONLY (it feeds `marketAgeTradingDays`):
    the product updates weekly, so a weekly snapshot that is 4 trading days old is normal and must never be STALE for that reason."""
    market = classify_market(manifest_like.get("marketAsOf") or {}, now, closures, policy)
    fin = classify_financial((manifest_like.get("financialAsOf") or {}).get("epsPeriod"), now, policy)
    return {**market, "financial": fin, "evaluatedAt": now.isoformat(timespec="seconds")}


# ---------------------------------------------------------------------------------------------------------------------
# Phase 3D.3A — WEEKLY, schedule-based freshness.
#
#   Policy: MARKET_DATA_UPDATE_POLICY = WEEKLY. The pipeline publishes every Sunday 10:00 Asia/Taipei (retries 14:00 and 20:00,
#   nothing after midnight). The snapshot holds the latest complete trading day (normally the Friday) - never "Sunday".
#
#   The question is NOT "how many trading days old are the prices" but "did we miss a weekly publication".
#   A scheduled slot S (a Sunday 10:00) is MISSED when its retry window has closed (Monday 00:00) and the last successful
#   publication happened before S.   0 missed FRESH   1 missed STALE   2+ missed SEVERELY_STALE
#   UNKNOWN: policy / publication time / market dates missing, unparseable, or in the future.
#   Publication date != market data date: the market dates are kept and shown as they are.
# ---------------------------------------------------------------------------------------------------------------------
from datetime import timezone

TAIPEI = timezone(timedelta(hours=8))
UPDATE_POLICY = "WEEKLY"
TIMEZONE_NAME = "Asia/Taipei"
SCHEDULED_WEEKDAY = 6  # Python weekday(): Sunday
SCHEDULED_WEEKDAY_NAME = "SUNDAY"
SCHEDULED_TIME = time(10, 0)
RETRY_TIMES = (time(10, 0), time(14, 0), time(20, 0))
WEEK = timedelta(days=7)


def to_taipei(dt: datetime) -> datetime:
    """Naive datetimes are taken to be Taipei wall-clock time."""
    return dt.replace(tzinfo=TAIPEI) if dt.tzinfo is None else dt.astimezone(TAIPEI)


def parse_instant(v) -> Optional[datetime]:
    if isinstance(v, datetime):
        return to_taipei(v)
    if not isinstance(v, str):
        return None
    try:
        return to_taipei(datetime.fromisoformat(v))
    except ValueError:
        return None


def slot_at_or_before(dt: datetime) -> datetime:
    dt = to_taipei(dt)
    d = dt.date() - timedelta(days=(dt.weekday() - SCHEDULED_WEEKDAY) % 7)
    slot = datetime.combine(d, SCHEDULED_TIME, TAIPEI)
    return slot - WEEK if slot > dt else slot


def next_slot_after(dt: datetime) -> datetime:
    return slot_at_or_before(dt) + WEEK


def window_close(slot: datetime) -> datetime:
    """The retry window of a slot ends at midnight (Monday 00:00): after that, a missing publication counts as a miss."""
    return datetime.combine(slot.date() + timedelta(days=1), time(0, 0), TAIPEI)


def missed_slots(published_at: datetime, now: datetime) -> int:
    n, s, now = 0, next_slot_after(published_at), to_taipei(now)
    while window_close(s) <= now:
        n += 1
        s += WEEK
    return n


def weekly_metadata(published_at: str) -> dict:
    """The update-policy metadata every manifest carries (freshness is computed from these, never from generatedAt)."""
    p = parse_instant(published_at)
    return {"updatePolicy": UPDATE_POLICY, "timezone": TIMEZONE_NAME, "scheduledWeekday": SCHEDULED_WEEKDAY_NAME,
            "scheduledTime": SCHEDULED_TIME.strftime("%H:%M"), "retryTimes": [t.strftime("%H:%M") for t in RETRY_TIMES],
            "lastSuccessfulPublication": published_at,
            "nextScheduledPublication": next_slot_after(p).isoformat(timespec="seconds") if p else None}


def classify(manifest_like: dict, now: datetime, closures: frozenset = frozenset(), policy: FreshnessPolicy = POLICY) -> dict:
    """Weekly, schedule-based verdict. manifest_like needs updatePolicy, lastSuccessfulPublication, marketAsOf (and financialAsOf)."""
    now = to_taipei(now)
    daily = classify_daily(manifest_like, now.replace(tzinfo=None), closures, policy)  # diagnostic only
    published = parse_instant(manifest_like.get("lastSuccessfulPublication"))
    parsed = {m: parse_date(v) for m, v in (manifest_like.get("marketAsOf") or {}).items()}
    out = {"updatePolicy": manifest_like.get("updatePolicy"), "lastSuccessfulPublication": manifest_like.get("lastSuccessfulPublication"),
           "lastScheduledSlot": slot_at_or_before(now).isoformat(timespec="seconds"),
           "nextScheduledPublication": next_slot_after(now).isoformat(timespec="seconds"),
           "marketAgeTradingDays": daily.get("lagTradingDays"), "expectedLatestTradeDate": daily["expectedLatestTradeDate"],
           "marketAsOf": daily["marketAsOf"], "oldestMarketDate": daily.get("oldestMarketDate"), "datesDiffer": daily.get("datesDiffer", False),
           "calendarNote": CALENDAR_NOTE, "financial": daily["financial"], "evaluatedAt": now.isoformat(timespec="seconds")}

    def unknown(why: str) -> dict:
        return {**out, "status": UNKNOWN, "missedWeeklyUpdates": None, "updatePending": False, "reason": why}

    if manifest_like.get("updatePolicy") != UPDATE_POLICY:
        return unknown("update policy is missing or is not WEEKLY")
    if published is None:
        return unknown("the last successful publication time is missing or unparseable")
    if published > now + timedelta(minutes=5):
        return unknown("the last successful publication is in the future (clock or data problem)")
    if not parsed or any(d is None for d in parsed.values()):
        return unknown("a market date is missing or unparseable")
    if any(d > now.date() for d in parsed.values()) or any(d > published.date() for d in parsed.values()):
        return unknown("a market date is later than the publication or than today (clock or data problem)")
    missed = missed_slots(published, now)
    slot = slot_at_or_before(now)
    pending = published < slot and now < window_close(slot)  # this week's run is due (or retrying) and has not succeeded yet
    status = FRESH if missed == 0 else STALE if missed == 1 else SEVERELY_STALE
    return {**out, "status": status, "missedWeeklyUpdates": missed, "updatePending": pending, "reason": None}
