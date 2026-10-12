"""Publication outcome, in the three words a person needs (Phase 8B): PUBLISHED / NO_UPDATE / FAILED  (+ PENDING while a retry is still due).

  PUBLISHED  a new release was written, verified and is now latest.
  NO_UPDATE  NORMAL: the source's trade date did not advance AND no newer trading day is owed (a holiday, a weekend, an already published day).
             The previous release stays; no new release is made.
  FAILED     a real problem: the source could not be read / is malformed / moved BACKWARDS / lacks a trading day that IS owed / the gate refused.
             The previous release stays.
  PENDING    an owed trading day is not at the source yet and a scheduled retry is still ahead (never a final outcome).

"The date did not move" alone says nothing: it is NO_UPDATE only when the calendar proves that nothing newer is owed. If the calendar cannot say, it is
never silently normal. Pure functions, no I/O. Every result carries a stable `reasonCode` and `normal` (False means somebody should look).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Optional

PUBLISHED, NO_UPDATE, FAILED, PENDING = "PUBLISHED", "NO_UPDATE", "FAILED", "PENDING"
STATUSES = (PUBLISHED, NO_UPDATE, FAILED, PENDING)


@dataclass(frozen=True)
class Outcome:
    status: str
    reason_code: str
    normal: bool
    detail: str = ""

    def to_json(self) -> dict:
        return {"publishStatus": self.status, "reasonCode": self.reason_code, "normal": self.normal, "detail": self.detail}


def classify_outcome(*, source_date: Optional[date], published_date: Optional[date], expected_date: Optional[date], today: Optional[date],
                     published_ok: bool = False, source_error: Optional[str] = None, gate_refused: Optional[str] = None, final: bool = False,
                     calendar_known: bool = True) -> Outcome:
    """source_date = the trade date the official source carries now; published_date = the date of the Last Known Good; expected_date = the latest trading day
    the source owes by now (from the trading calendar and the source's availability time), None when the calendar cannot say."""
    def not_final(code: str, detail: str) -> Outcome:
        return Outcome(FAILED, code, False, detail) if final else Outcome(PENDING, code, False, detail + " (a scheduled retry is still due)")

    if source_error:
        return Outcome(FAILED, "SOURCE_ERROR", False, source_error[:300])
    if source_date is None:
        return Outcome(FAILED, "SOURCE_DATE_MALFORMED", False, "the source carries no readable trade date")
    if today is not None and source_date > today:
        return Outcome(FAILED, "SOURCE_DATE_IN_FUTURE", False, f"source date {source_date} is after today {today}")
    if published_date is not None and source_date < published_date:
        return Outcome(FAILED, "SOURCE_DATE_REGRESSED", False, f"source date {source_date} is OLDER than the published {published_date}")
    if gate_refused:
        return Outcome(FAILED, "GATE_REFUSED", False, gate_refused[:300])
    if published_ok:
        return Outcome(PUBLISHED, "NEW_RELEASE", True, f"published trade date {source_date}")
    advanced = published_date is None or source_date > published_date
    if advanced:  # newer than the Last Known Good yet not published: something stopped it
        return Outcome(FAILED, "ADVANCED_BUT_NOT_PUBLISHED", False, f"source {source_date} is newer than {published_date} but nothing was published")
    # the source did not advance
    if not calendar_known or expected_date is None:
        return not_final("CALENDAR_UNKNOWN", "the source did not advance and the trading calendar cannot prove that nothing newer is owed")
    if expected_date <= source_date:
        return Outcome(NO_UPDATE, "NO_NEW_TRADING_DAY", True, f"the latest owed trading day is {expected_date}; the source is at {source_date}")
    return not_final("EXPECTED_TRADING_DAY_MISSING", f"the source is at {source_date} but trading day {expected_date} is owed")


_DAILY = {"SUCCESS": (PUBLISHED, "NEW_RELEASE", True), "SUCCESS_PARTIAL": (PUBLISHED, "PARTIAL_RELEASE", False),
          "NOOP_ALREADY_PUBLISHED": (NO_UPDATE, "ALREADY_PUBLISHED", True), "NO_TRADING_DAY_EXPECTED": (NO_UPDATE, "NO_TRADING_DAY", True),
          "WAITING_FOR_NEW_MARKET_DATA": (PENDING, "EXPECTED_TRADING_DAY_MISSING", False),
          "FAILED_NO_NEW_MARKET_DATA": (FAILED, "EXPECTED_TRADING_DAY_MISSING", False), "FAILED_SOURCE": (FAILED, "SOURCE_ERROR", False),
          "FAILED_VALIDATION": (FAILED, "VALIDATION_FAILED", False), "FAILED_PUBLICATION": (FAILED, "PUBLICATION_FAILED", False)}
_WEEKLY = {"SUCCESS": (PUBLISHED, "NEW_RELEASE", True), "NOOP": (NO_UPDATE, "ALREADY_PUBLISHED", True), "WAITING": (PENDING, "EXPECTED_TRADING_DAY_MISSING", False),
           "REFUSED": (FAILED, "GATE_REFUSED", False), "FAILED": (FAILED, "RUN_FAILED", False), "SKIPPED": (NO_UPDATE, "RETRY_LIMIT_REACHED", False)}


def outcome_of_status(status: Optional[str]) -> Outcome:
    """Map a recorded run status (daily or weekly vocabulary) to the three words. An unknown status is FAILED, never silently normal."""
    hit = _DAILY.get(status or "") or _WEEKLY.get(status or "")
    if hit is None:
        return Outcome(FAILED, "UNKNOWN_STATUS", False, f"unrecognised run status {status!r}")
    return Outcome(*hit)
