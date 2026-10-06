import json
import unittest
from datetime import date, datetime, time, timedelta
from pathlib import Path

from mitw.freshness import (FRESH, SEVERELY_STALE, STALE, UNKNOWN, FreshnessPolicy, classify, classify_financial, classify_market,
                            expected_financial_period, expected_latest_trade_date, is_trading_day, next_slot_after, previous_trading_day,
                            slot_at_or_before, trading_days_between, weekly_metadata)

HERE = Path(__file__).resolve().parent
VECTORS = json.loads((HERE / "weekly_vectors.json").read_text(encoding="utf-8"))["cases"]
TS_COPY = HERE.parent.parent / "risk-profiler" / "src" / "valuation-lab" / "__tests__" / "weeklyVectors.json"


def manifest_like(v: dict) -> dict:
    m = {"marketAsOf": v["marketAsOf"], "financialAsOf": {"epsPeriod": v.get("eps", "2026Q2")}, "lastSuccessfulPublication": v["published"]}
    if v.get("updatePolicy", "WEEKLY") is not None:
        m["updatePolicy"] = v.get("updatePolicy", "WEEKLY")
    return m


class WeeklyTimeTravelTests(unittest.TestCase):
    def test_every_vector(self):
        for v in VECTORS:
            with self.subTest(v["id"]):
                r = classify(manifest_like(v), datetime.fromisoformat(v["now"]))
                self.assertEqual(r["status"], v["status"], r.get("reason"))
                if "missed" in v:
                    self.assertEqual(r["missedWeeklyUpdates"], v["missed"])
                if "pending" in v:
                    self.assertEqual(r["updatePending"], v["pending"])
                if "marketAge" in v:
                    self.assertEqual(r["marketAgeTradingDays"], v["marketAge"])
                if "expectedLatest" in v:
                    self.assertEqual(r["expectedLatestTradeDate"], v["expectedLatest"])
                if "next" in v:
                    self.assertEqual(r["nextScheduledPublication"], v["next"])
                if "datesDiffer" in v:
                    self.assertEqual(r["datesDiffer"], v["datesDiffer"])
                if "financialStatus" in v:
                    self.assertEqual(r["financial"]["status"], v["financialStatus"])
                if "financialExpected" in v:
                    self.assertEqual(r["financial"]["expectedPeriod"], v["financialExpected"])

    def test_the_two_copies_of_the_vectors_are_identical(self):
        if not TS_COPY.exists():
            self.skipTest("risk-profiler is not next to this project")
        self.assertEqual(json.loads(TS_COPY.read_text(encoding="utf-8")), json.loads((HERE / "weekly_vectors.json").read_text(encoding="utf-8")))

    def test_the_key_regression_friday_with_sundays_data_is_fresh(self):
        r = classify(manifest_like({"published": "2026-10-11T10:12:00", "marketAsOf": {"TWSE": "2026-10-09", "TPEX": "2026-10-09"}}), datetime(2026, 10, 16, 21, 0))
        self.assertEqual(r["status"], FRESH)
        self.assertGreaterEqual(r["marketAgeTradingDays"], 3)  # the diagnostic number exists, and does not decide

    def test_generatedAt_is_never_consulted(self):
        m = manifest_like({"published": "2026-10-11T10:12:00", "marketAsOf": {"TWSE": "2026-10-09", "TPEX": "2026-10-09"}})
        m["generatedAt"] = "2026-11-10T11:59:00+08:00"  # "built just now" must not make a month-old publication fresh
        self.assertEqual(classify(m, datetime(2026, 11, 10, 12, 0))["status"], SEVERELY_STALE)

    def test_slot_arithmetic_is_in_taipei_time_whatever_the_machine_zone(self):
        utc = datetime.fromisoformat("2026-10-11T02:30:00+00:00")  # = Sunday 10:30 in Taipei
        self.assertEqual(slot_at_or_before(utc).isoformat(), "2026-10-11T10:00:00+08:00")
        self.assertEqual(next_slot_after(utc).isoformat(), "2026-10-18T10:00:00+08:00")

    def test_weekly_metadata(self):
        m = weekly_metadata("2026-10-11T10:12:00+08:00")
        self.assertEqual((m["updatePolicy"], m["timezone"], m["scheduledWeekday"], m["scheduledTime"]), ("WEEKLY", "Asia/Taipei", "SUNDAY", "10:00"))
        self.assertEqual(m["nextScheduledPublication"], "2026-10-18T10:00:00+08:00")
        self.assertEqual(m["retryTimes"], ["10:00", "14:00", "20:00"])


class ThirtyDaySimulation(unittest.TestCase):
    """Walk a clock forward in 3-hour steps for 31 days and watch what a person opening the app would see."""

    FRIDAY = {"TWSE": "2026-10-09", "TPEX": "2026-10-09"}

    def walk(self, publishes):
        """publishes: datetimes at which a publication succeeded. Yields (now, status) every 3 hours from 2026-10-11 to day 31."""
        out, t, end = [], datetime(2026, 10, 11, 9, 0), datetime(2026, 11, 11, 12, 0)
        pubs = sorted(publishes)
        while t <= end:
            done = [p for p in pubs if p <= t]
            if done:
                last = done[-1]
                friday = last.date() - timedelta(days=(last.weekday() - 4) % 7)  # the Friday before that publication
                m = {"marketAsOf": {"TWSE": friday.isoformat(), "TPEX": friday.isoformat()}, "updatePolicy": "WEEKLY",
                     "lastSuccessfulPublication": last.isoformat(), "financialAsOf": {"epsPeriod": "2026Q2"}}
                out.append((t, classify(m, t)["status"]))
            t += timedelta(hours=3)
        return out

    def test_a_working_scheduler_means_the_app_is_never_stale_in_31_days(self):
        sundays = [datetime(2026, 10, 11, 10, 12) + timedelta(days=7 * i) for i in range(5)]  # 10-11 ... 11-08
        statuses = {st for _, st in self.walk(sundays)}
        self.assertEqual(statuses, {FRESH})

    def test_a_broken_scheduler_goes_stale_then_severe_at_the_exact_boundaries_and_never_silently(self):
        timeline = self.walk([datetime(2026, 10, 11, 10, 12)])  # one good publication, then nothing for a month
        first = lambda want: next(t for t, st in timeline if st == want)
        self.assertGreaterEqual(first(STALE), datetime(2026, 10, 19, 0, 0))  # not before the first Sunday window closed
        self.assertLess(first(STALE), datetime(2026, 10, 19, 3, 1))
        self.assertGreaterEqual(first(SEVERELY_STALE), datetime(2026, 10, 26, 0, 0))
        self.assertLess(first(SEVERELY_STALE), datetime(2026, 10, 26, 3, 1))
        seq = [st for _, st in timeline]
        self.assertEqual(seq, sorted(seq, key=[FRESH, STALE, SEVERELY_STALE].index))  # it only ever gets worse, never flickers
        self.assertEqual(timeline[-1][1], SEVERELY_STALE)  # day 31


class MarketAgeTests(unittest.TestCase):
    def test_built_today_with_ten_day_old_market_data_has_a_large_market_age(self):
        r = classify_market({"TWSE": "2026-10-06", "TPEX": "2026-10-06"}, datetime(2026, 10, 16, 12, 0))
        self.assertGreater(r["lagTradingDays"], 5)  # diagnostic: the weekly verdict above is what decides FRESH / STALE


class CalendarTests(unittest.TestCase):
    def test_weekends_are_not_trading_days(self):
        self.assertTrue(is_trading_day(date(2026, 10, 9)))
        self.assertFalse(is_trading_day(date(2026, 10, 10)))
        self.assertFalse(is_trading_day(date(2026, 10, 11)))
        self.assertFalse(is_trading_day(date(2026, 10, 9), frozenset({date(2026, 10, 9)})))

    def test_previous_trading_day_skips_the_weekend_and_closures(self):
        self.assertEqual(previous_trading_day(date(2026, 10, 12)), date(2026, 10, 9))
        self.assertEqual(previous_trading_day(date(2026, 10, 12), frozenset({date(2026, 10, 9)})), date(2026, 10, 8))

    def test_trading_days_between_is_half_open(self):
        self.assertEqual(trading_days_between(date(2026, 10, 5), date(2026, 10, 5)), 0)
        self.assertEqual(trading_days_between(date(2026, 10, 5), date(2026, 10, 6)), 1)
        self.assertEqual(trading_days_between(date(2026, 10, 9), date(2026, 10, 12)), 1)  # Fri -> Mon is one session
        self.assertEqual(trading_days_between(date(2026, 10, 12), date(2026, 10, 5)), 0)

    def test_cutoff_separates_not_yet_published_from_missing(self):
        d = date(2026, 10, 6)
        self.assertEqual(expected_latest_trade_date(datetime(2026, 10, 6, 21, 59)), date(2026, 10, 5))
        self.assertEqual(expected_latest_trade_date(datetime(2026, 10, 6, 22, 0)), d)
        # a longer policy cut-off moves the boundary (thresholds are configuration, not scattered constants)
        late = FreshnessPolicy(publish_cutoff=time(23, 30))
        self.assertEqual(expected_latest_trade_date(datetime(2026, 10, 6, 22, 30), policy=late), date(2026, 10, 5))

    def test_thresholds_are_configurable(self):
        strict = FreshnessPolicy(fresh_max_lag=0, stale_max_lag=2)
        r = classify_market({"TWSE": "2026-10-05", "TPEX": "2026-10-05"}, datetime(2026, 10, 6, 22, 30), policy=strict)
        self.assertEqual(r["status"], STALE)


class FinancialTests(unittest.TestCase):
    def test_expected_period_follows_the_filing_calendar(self):
        for today, want in [(date(2026, 8, 20), "2026Q1"), (date(2026, 8, 22), "2026Q2"), (date(2026, 10, 6), "2026Q2"), (date(2026, 11, 20), "2026Q2"),
                            (date(2026, 11, 21), "2026Q3"), (date(2027, 4, 6), "2026Q3"), (date(2027, 4, 7), "2026Q4")]:
            self.assertEqual(expected_financial_period(today), want, today)

    def test_financial_uses_its_own_thresholds_not_the_price_ones(self):
        self.assertEqual(classify_financial("2026Q2", datetime(2026, 11, 5))["status"], FRESH)
        self.assertEqual(classify_financial("2026Q2", datetime(2026, 11, 25))["status"], STALE)
        self.assertEqual(classify_financial("2026Q2", datetime(2027, 4, 12))["status"], SEVERELY_STALE)
        self.assertEqual(classify_financial(None, datetime(2026, 11, 5))["status"], UNKNOWN)


if __name__ == "__main__":
    unittest.main()
