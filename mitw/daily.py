"""Daily, trading-day-aware publication policy (Phase 3I.1 design, Phase 3I.2 integration). Pure functions: no network, no files.

Replaces "did we miss a weekly slot" with three ideas that must never be confused:
  SCHEDULE DAY         Mon-Fri. TWSE is worked at 06:30; TPEx and 興櫃 at 21:00 / 22:00 / 23:00 (Asia/Taipei). A day's attempts close at 24:00.
  EXPECTED MARKET DAY  the latest trading day a market is expected to carry by an instant, from ITS OWN availability rule
  ACTUAL MARKET DAY    the trade date the official source really carries (never inferred from the clock)

Operational availability rules. They are OPERATIONAL POLICY based on OBSERVATION (EMPIRICALLY_OBSERVED), NOT exchange publication guarantees:
  TWSE      trade date D appears in the OpenAPI files around 05:20 on D+1 (2 observations) -> target for D from D+1 06:30
  TPEX/興櫃 trade date D appears on the evening of D (18:00 and 22:00 seen)                  -> target for D from D 21:00 (first attempt)

Each market is judged against ITS OWN target. TWSE at T-1 and TPEx at T is the NORMAL state at 22:30 and is a plain SUCCESS: different dates are not
a failure. SUCCESS_PARTIAL is reserved for "at least one market that owes a newer date has not reached its own target".
Only markets that OWE something are queried: TWSE is not asked at 21/22/23 when its T-1 data is already published, TPEx is not asked at 06:30 when
its T-1 data is already published. The release stays ONE atomic release: a market that owes nothing is carried from the Last Known Good.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from typing import Optional

from .trading_calendar import CLOSED, TRADING, UNKNOWN as CAL_UNKNOWN, TradingCalendar, trading_days_between

TAIPEI = timezone(timedelta(hours=8))
POLICY = "DAILY_TRADING_DAY"
TIMEZONE_NAME = "Asia/Taipei"
MORNING_ATTEMPT = time(6, 30)                                 # TWSE
ATTEMPT_TIMES = (time(21, 0), time(22, 0), time(23, 0))       # TPEX / EMERGING: PRIMARY, RETRY_1, RETRY_2_FINAL
ATTEMPT_NAMES = ("PRIMARY", "RETRY_1", "RETRY_2_FINAL")
SCHEDULE_WEEKDAYS = (0, 1, 2, 3, 4)  # Python weekday(): Monday-Friday
MAIN_MARKETS = ("TWSE", "TPEX")
EMERGING_MARKETS = ("EMERGING",)

# GitHub Actions cron is UTC; Taiwan has no DST (UTC+8). NOT ACTIVATED by anything in this phase (the workflows are workflow_dispatch only).
CRON_UTC = ("0 13 * * 1-5", "0 14 * * 1-5", "0 15 * * 1-5")   # 21:00 / 22:00 / 23:00 Taipei, the same weekday
MORNING_CRON_UTC = "30 22 * * 0-4"                            # 06:30 Taipei Mon-Fri = 22:30 UTC the evening before (Sun-Thu)

FRESH, STALE, SEVERELY_STALE, UNKNOWN = "FRESH", "STALE", "SEVERELY_STALE", "UNKNOWN"

SUCCESS = "SUCCESS"
SUCCESS_PARTIAL = "SUCCESS_PARTIAL"
NOOP_ALREADY_PUBLISHED = "NOOP_ALREADY_PUBLISHED"
WAITING = "WAITING_FOR_NEW_MARKET_DATA"
NO_TRADING_DAY = "NO_TRADING_DAY_EXPECTED"
FAILED_NO_NEW = "FAILED_NO_NEW_MARKET_DATA"
FAILED_SOURCE = "FAILED_SOURCE"
FAILED_VALIDATION = "FAILED_VALIDATION"
FAILED_PUBLICATION = "FAILED_PUBLICATION"                # decided by the publisher after writing / verifying
STATES = (SUCCESS, SUCCESS_PARTIAL, NOOP_ALREADY_PUBLISHED, WAITING, NO_TRADING_DAY, FAILED_NO_NEW, FAILED_SOURCE, FAILED_VALIDATION, FAILED_PUBLICATION)


@dataclass(frozen=True)
class Availability:
    offset_days: int
    earliest: time        # the earliest we have seen the file
    expected_by: time     # the operational target: from this moment on the market OWES date D
    first_attempt: time   # when the pipeline starts working this market
    basis: str            # DOCUMENTED | EMPIRICALLY_OBSERVED | UNKNOWN
    evidence: str


AVAILABILITY = {
    "TWSE": Availability(1, time(5, 0), time(6, 30), time(6, 30), "EMPIRICALLY_OBSERVED",
                         "Last-Modified 05:20:4x Taipei on D+1 for D=2026-10-05 and D=2026-10-06 (2 observations; weekend D+1 not yet observed). Operational target 06:30 (not a guarantee)"),
    "TPEX": Availability(0, time(18, 0), time(21, 0), time(21, 0), "EMPIRICALLY_OBSERVED",
                         "Last-Modified 18:00 on D=2026-10-06 and 22:00 on D=2026-10-07 (2 observations; cadence between them unknown). Operational target: first attempt 21:00 (not a guarantee)"),
    "EMERGING": Availability(0, time(18, 0), time(21, 0), time(21, 0), "EMPIRICALLY_OBSERVED",
                             "tpex_esb_latest_statistics Last-Modified 22:00:18 on D=2026-10-07 (1 observation); same TPEx platform as the main board. Operational target: first attempt 21:00 (not a guarantee)"),
}
EVIDENCE_LEVEL = "EMPIRICALLY_OBSERVED"


def to_taipei(dt: datetime) -> datetime:
    return dt.replace(tzinfo=TAIPEI) if dt.tzinfo is None else dt.astimezone(TAIPEI)


def _at(d: date, t: time) -> datetime:
    return datetime.combine(d, t, TAIPEI)


def parse_market_date(v) -> Optional[date]:
    """A trade date as the source printed it: ISO (YYYY-MM-DD) or ROC (1151007). Anything else is None (= malformed)."""
    if isinstance(v, date) and not isinstance(v, datetime):
        return v
    s = str(v or "").strip()
    try:
        if len(s) == 10 and s[4] == "-" and s[7] == "-":
            return date.fromisoformat(s)
        if s.isdigit() and len(s) == 7:
            return date(int(s[:3]) + 1911, int(s[3:5]), int(s[5:7]))
    except ValueError:
        return None
    return None


# ---- windows and targets -------------------------------------------------------------------------------------------------------------------
def window_end_of(day: date) -> datetime:
    """A schedule day's attempts close at 24:00 (= the next day 00:00)."""
    return _at(day + timedelta(days=1), time(0, 0))


def freshness_instant(now: datetime) -> datetime:
    """The end of the latest schedule day that has CLOSED at `now`. Freshness is judged against what that day could have published, so a window that
    is still open (tonight's run still ahead) never turns the previous publication STALE."""
    now = to_taipei(now)
    d = now.date()
    for _ in range(10):
        if d.weekday() in SCHEDULE_WEEKDAYS and window_end_of(d) <= now:
            return window_end_of(d)
        d -= timedelta(days=1)
    raise AssertionError("unreachable")


def window_open(market: str, now: datetime, avail: dict = AVAILABILITY) -> bool:
    now = to_taipei(now)
    return now.date().weekday() in SCHEDULE_WEEKDAYS and now.time() >= avail[market].first_attempt


def attempt_label(now: datetime) -> str:
    """MORNING_TWSE (before 12:00) or PRIMARY / RETRY_1 / RETRY_2_FINAL (21:00 / 22:00 / 23:00). GitHub starts runs late: the latest slot already due."""
    t = to_taipei(now).time()
    if t < time(12, 0):
        return "MORNING_TWSE"
    return ATTEMPT_NAMES[max(0, sum(1 for a in ATTEMPT_TIMES if t >= a) - 1)]


def attempt_index(now: datetime) -> int:
    t = to_taipei(now).time()
    return max(0, sum(1 for a in ATTEMPT_TIMES if t >= a) - 1)


def is_final_attempt(now: datetime, trigger: str = "schedule") -> bool:
    """Only the 23:00 scheduled attempt closes the day. The 06:30 TWSE attempt never declares a failure (the evening attempts still owe the catch-up)."""
    t = to_taipei(now).time()
    return trigger != "manual" and t >= ATTEMPT_TIMES[-1]


def target_date(market: str, as_of: datetime, cal: TradingCalendar, avail: dict = AVAILABILITY) -> tuple[Optional[date], bool]:
    """expectedLatestMarketDate(market, as_of): the latest TRADING day D whose data the market owes by `as_of`.
    Returns (date, unknown). unknown=True when the calendar cannot say (then there is no date: the caller must not guess one)."""
    av = avail[market]
    as_of = to_taipei(as_of)
    d = as_of.date()
    for _ in range(60):
        s = cal.status(d, market)
        if s == CAL_UNKNOWN:
            return None, True
        if s == TRADING and _at(d + timedelta(days=av.offset_days), av.expected_by) <= as_of:
            return d, False
        d -= timedelta(days=1)
    return None, True


# ---- freshness (per market) ----------------------------------------------------------------------------------------------------------------
def classify_market(market: str, published: Optional[date], now: datetime, cal: TradingCalendar, avail: dict = AVAILABILITY) -> dict:
    """published = the trade date the market's current Last Known Good carries. FRESH / STALE / SEVERELY_STALE = 0 / 1 / 2+ completed trading days behind
    the date the last CLOSED schedule day could have published. Calendar days alone never create STALE: a long holiday has no completed trading day."""
    now = to_taipei(now)
    base = {"market": market, "publishedMarketDate": published.isoformat() if published else None, "evaluatedAt": now.isoformat(timespec="seconds"),
            "expectedMarketDate": None, "lagTradingDays": None, "updatePending": False}

    def unk(why: str) -> dict:
        return {**base, "status": UNKNOWN, "reason": why}

    if published is None:
        return unk("the market date is missing or unparseable")
    if published > now.date():
        return unk("the market date is in the future (clock or data problem)")
    expected, unknown = target_date(market, freshness_instant(now), cal, avail)
    if unknown or expected is None:
        return unk("the trading calendar cannot establish the expected market date")
    lag, unknown = (0, False) if expected <= published else trading_days_between(cal, published, expected, market)
    if unknown or lag is None:
        return unk("the trading calendar cannot count the completed trading days in between")
    today_target = target_date(market, window_end_of(now.date()), cal, avail)[0] if now.date().weekday() in SCHEDULE_WEEKDAYS else None
    pending = window_open(market, now, avail) and today_target is not None and published < today_target
    return {**base, "status": FRESH if lag == 0 else STALE if lag == 1 else SEVERELY_STALE, "expectedMarketDate": expected.isoformat(),
            "lagTradingDays": lag, "updatePending": pending, "reason": None}


def classify_monthly_revenue(period: Optional[str], today: date, due_day: int = 10, grace_days: int = 5) -> dict:
    """Monthly revenue (YYYY-MM) is judged on ITS OWN cadence: month M is due on the 10th of M+1 (plus a grace). It never has to change when prices do."""
    def idx(p):
        try:
            y, m = str(p).split("-")
            return int(y) * 12 + int(m) - 1 if len(y) == 4 and 1 <= int(m) <= 12 else None
        except (ValueError, AttributeError):
            return None
    have = idx(period)
    best = None
    for k in range(today.year * 12 + today.month - 1 - 14, today.year * 12 + today.month - 1 + 1):
        y, m = divmod(k + 1, 12)  # revenue month k is due in month k+1
        due = date(y, m + 1, due_day) if m < 12 else date(y + 1, 1, due_day)
        if due + timedelta(days=grace_days) <= today:
            best = k
    base = {"period": period, "expectedPeriod": None if best is None else f"{best // 12}-{best % 12 + 1:02d}"}
    if have is None or best is None:
        return {**base, "status": UNKNOWN, "monthsBehind": None}
    behind = max(0, best - have)
    return {**base, "status": FRESH if behind == 0 else STALE if behind == 1 else SEVERELY_STALE, "monthsBehind": behind}


# ---- publication decisions -----------------------------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Obs:
    """What one market's official source showed in this attempt."""

    kind: str  # OK | ERROR (unreachable / HTTP / schema) | MALFORMED (date missing or unparseable)
    date: Optional[date] = None
    detail: str = ""


@dataclass(frozen=True)
class Precheck:
    fetch: bool
    state: Optional[str]  # NO_TRADING_DAY_EXPECTED / NOOP_ALREADY_PUBLISHED when no fetch is needed, else None
    targets: dict = field(default_factory=dict)
    owed: tuple = ()      # the markets that must be queried: their Last Known Good is behind the date they owe NOW
    reason: str = ""


@dataclass(frozen=True)
class Decision:
    state: str
    publish: bool
    advanced: tuple = ()   # markets whose date moved past the Last Known Good (and are published)
    behind: tuple = ()     # owed markets that did NOT reach their own target
    carried: tuple = ()    # markets that owed nothing: carried from the Last Known Good unchanged
    targets: dict = field(default_factory=dict)
    reason: str = ""

    @property
    def failed(self) -> bool:
        return self.state.startswith("FAILED")


def _targets(markets, now, cal, avail):
    return {m: target_date(m, now, cal, avail) for m in markets}


def precheck(now: datetime, published: dict, cal: TradingCalendar, markets=MAIN_MARKETS, avail: dict = AVAILABILITY) -> Precheck:
    """Before any request: what does each market owe AT THIS MOMENT? `published` = {market: trade date of the Last Known Good, or None}.
    A market whose Last Known Good already reaches its target is not queried (no TWSE request at 21/22/23 once T-1 is published)."""
    now = to_taipei(now)
    t = _targets(markets, now, cal, avail)
    targets = {m: (d.isoformat() if d else None) for m, (d, _) in t.items()}
    owed = tuple(m for m in markets if published.get(m) is None or t[m][0] is None or published[m] < t[m][0])
    unknown = [m for m in markets if t[m][0] is None]
    if not owed:
        closed = all(cal.status(now.date(), m) == CLOSED for m in markets)  # closed for EVERY market this run is about (a scoped manual closure counts)
        return Precheck(False, NO_TRADING_DAY if closed else NOOP_ALREADY_PUBLISHED, targets, (),
                        "every market already carries the date it owes; today is closed" if closed else "every market already carries the date it owes")
    why = "behind the date it owes now: " + ", ".join(owed)
    if unknown:
        why = "CALENDAR_UNKNOWN for " + ", ".join(unknown) + ": the calendar cannot say what is owed, so the source is read; " + why
    return Precheck(True, None, targets, owed, why)


def evaluate(now: datetime, published: dict, observed: dict, cal: TradingCalendar, markets=MAIN_MARKETS, final: bool = False,
             avail: dict = AVAILABILITY) -> Decision:
    """After reading the sources of the OWED markets (`observed` has an entry for each of them; markets that owe nothing are carried).
    Never publishes old data as new: a market that did not move past its Last Known Good is not 'advanced'.
    SUCCESS = every owed market reached its OWN target (so TWSE T-1 + TPEx T is SUCCESS). If some owed market did not: WAITING (nothing is published
    while retries remain); on the final attempt the markets that did advance are published as SUCCESS_PARTIAL and the others are named.
    The Last Known Good is never touched by a failed attempt."""
    now = to_taipei(now)
    t = _targets(markets, now, cal, avail)
    targets = {m: (d.isoformat() if d else None) for m, (d, _) in t.items()}
    owed = tuple(m for m in markets if m in observed)
    carried = tuple(m for m in markets if m not in observed)
    errors = [m for m in owed if observed[m].kind == "ERROR"]
    if errors:
        return Decision(FAILED_SOURCE, False, (), (), carried, targets, "; ".join(f"{m}: {observed[m].detail}" for m in errors))
    bad = [m for m in owed if observed[m].kind == "MALFORMED" or observed[m].date is None]
    if bad:
        return Decision(FAILED_VALIDATION, False, (), (), carried, targets, "MARKET_DATE_MALFORMED: " + ", ".join(bad))
    for m in owed:
        d = observed[m].date
        if d > now.date():
            return Decision(FAILED_VALIDATION, False, (), (), carried, targets, f"MARKET_DATE_IN_THE_FUTURE: {m} {d}")
        if published.get(m) is not None and d < published[m]:
            return Decision(FAILED_VALIDATION, False, (), (), carried, targets, f"MARKET_DATE_MOVED_BACKWARD: {m} {d} < Last Known Good {published[m]}")
        if cal.status(d, m) == CLOSED:
            return Decision(FAILED_VALIDATION, False, (), (), carried, targets, f"MARKET_DATE_ON_A_CLOSED_DAY: {m} {d}")
    advanced = tuple(m for m in owed if published.get(m) is None or observed[m].date > published[m])
    cal_unknown = [m for m in owed if t[m][0] is None]
    behind = tuple(m for m in owed if t[m][0] is not None and observed[m].date < t[m][0])
    if not advanced:
        if not owed:
            return Decision(NOOP_ALREADY_PUBLISHED, False, (), (), carried, targets, "no market owes anything")
        if cal_unknown and not behind:
            return Decision(FAILED_NO_NEW if final else WAITING, False, (), (), carried, targets, "CALENDAR_UNKNOWN: no market moved and a holiday cannot be proven")
        if not behind:  # read although it owed nothing (a manual force, a carry fallback) and nothing moved: there is simply nothing new
            return Decision(NOOP_ALREADY_PUBLISHED, False, (), (), carried, targets, "no market moved and none owes a newer date")
        why = "NO_NEW_MARKET_DATA: " + ", ".join(f"{m} still {observed[m].date} (owes {targets[m]})" for m in behind)
        return Decision(FAILED_NO_NEW if final else WAITING, False, (), behind, carried, targets, why)
    if not behind:
        return Decision(SUCCESS, True, advanced, (), carried, targets, "every owed market reached its own target date")
    if final:
        return Decision(SUCCESS_PARTIAL, True, advanced, behind, carried, targets, "final attempt: " + ", ".join(behind) + " did not reach its own target; the advanced markets are published")
    return Decision(WAITING, False, advanced, behind, carried, targets, "held for a retry: " + ", ".join(behind) + " has not reached its own target yet")


# ---- what a manifest / health file carries so the app needs no exchange call ---------------------------------------------------------------
def publication_policy(cal: TradingCalendar, avail: dict = AVAILABILITY, markets=MAIN_MARKETS) -> dict:
    return {
        "policy": POLICY, "timezone": TIMEZONE_NAME, "scheduleWeekdays": [d + 1 for d in SCHEDULE_WEEKDAYS],
        "attempts": [t.strftime("%H:%M") for t in ATTEMPT_TIMES], "windowClosesAt": "24:00",
        "markets": {m: {"offsetDays": avail[m].offset_days, "expectedBy": avail[m].expected_by.strftime("%H:%M"), "firstAttempt": avail[m].first_attempt.strftime("%H:%M"),
                        "earliestSeen": avail[m].earliest.strftime("%H:%M"), "basis": avail[m].basis, "evidence": avail[m].evidence} for m in markets},
        "calendar": cal.to_json(),
    }
