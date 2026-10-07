"""Phase 3I.1 + 3I.2: the daily, trading-day-aware publication policy. Pure functions only: no network, no filesystem outside tests/."""
from __future__ import annotations

import json
import unittest
from datetime import date, datetime
from pathlib import Path

from mitw import daily as D
from mitw.freshness import classify_financial, expected_financial_period
from mitw.trading_calendar import CLOSED, TRADING, UNKNOWN, ManualClosure, ManualClosureError, from_twse_holiday_schedule, parse_manual_closures, roc_to_date

HERE = Path(__file__).parent
ROWS = json.loads((HERE / "fixtures" / "twse_holiday_schedule_2026.json").read_text(encoding="utf-8-sig"))
CAL = from_twse_holiday_schedule(ROWS, "2026-10-07T23:10:00+08:00")
VECTORS = json.loads((HERE / "daily_vectors.json").read_text(encoding="utf-8"))


def at(s: str) -> datetime:
    return datetime.fromisoformat(s if "+" in s else s + "+08:00")


def d(s: str) -> date:
    return date.fromisoformat(s)


def ok(s: str) -> D.Obs:
    return D.Obs("OK", d(s))


class CalendarTests(unittest.TestCase):
    def test_official_list_is_parsed_not_assumed(self):
        self.assertEqual(roc_to_date("1151009"), date(2026, 10, 9))
        self.assertEqual(CAL.status(d("2026-10-09")), CLOSED)   # 國慶日補假 (a Friday)
        self.assertEqual(CAL.status(d("2026-10-08")), TRADING)
        self.assertEqual(CAL.status(d("2026-10-10")), CLOSED)   # Saturday
        self.assertEqual(CAL.status(d("2026-10-26")), CLOSED)   # Monday after a Sunday holiday
        self.assertEqual(CAL.status(d("2026-02-11")), TRADING)  # 農曆春節前最後交易日 is a TRADING day the list only mentions
        self.assertEqual(CAL.status(d("2026-02-23")), TRADING)  # 農曆春節後開始交易日
        self.assertEqual(CAL.status(d("2026-01-02")), TRADING)  # 國曆新年開始交易日
        for day in ("2026-02-12", "2026-02-13", "2026-02-16", "2026-02-20", "2026-09-28"):
            self.assertEqual(CAL.status(d(day)), CLOSED, day)  # settlement-only days and the Lunar New Year closure

    def test_a_year_the_list_does_not_cover_is_unknown_never_trading(self):
        self.assertEqual(CAL.status(d("2027-01-04")), UNKNOWN)
        self.assertEqual(CAL.status(d("2027-01-02")), CLOSED)  # a Saturday is closed whatever the list says
        self.assertEqual(from_twse_holiday_schedule([]).status(d("2026-10-07")), UNKNOWN)

    def test_round_trip_through_the_published_form(self):
        back = type(CAL).from_json(json.loads(json.dumps(CAL.to_json())))
        for day in ("2026-10-09", "2026-10-08", "2026-02-18", "2027-01-04"):
            self.assertEqual(back.status(d(day)), CAL.status(d(day)))


class ManualClosureTests(unittest.TestCase):
    DOC = {"closures": [{"date": "2026-10-14", "markets": ["TWSE", "TPEX"], "reason": "typhoon: exchanges closed", "source": "exchange announcement (test)", "addedAt": "2026-10-13T20:00:00+08:00"},
                        {"date": "2026-10-15", "markets": ["EMERGING"], "reason": "emerging only (test)", "source": "test", "addedAt": "2026-10-14T08:00:00+08:00"}]}

    def test_scope_reason_source_and_time_are_required_and_honoured(self):
        cal = from_twse_holiday_schedule(ROWS, manual=parse_manual_closures(self.DOC))
        self.assertEqual(cal.status(d("2026-10-14"), "TWSE"), CLOSED)
        self.assertEqual(cal.status(d("2026-10-14"), "TPEX"), CLOSED)
        self.assertEqual(cal.status(d("2026-10-14"), "EMERGING"), TRADING)  # not in this closure's scope
        self.assertEqual(cal.status(d("2026-10-15"), "EMERGING"), CLOSED)
        self.assertEqual(cal.status(d("2026-10-15"), "TWSE"), TRADING)
        self.assertEqual(CAL.status(d("2026-10-14"), "TWSE"), TRADING)      # without the override nothing is assumed closed
        back = type(cal).from_json(json.loads(json.dumps(cal.to_json())))   # and the browser receives it as published data
        self.assertEqual(back.status(d("2026-10-14"), "TWSE"), CLOSED)
        self.assertEqual(cal.to_json()["manualClosures"][0]["reason"], "typhoon: exchanges closed")

    def test_a_malformed_override_is_an_error_not_silently_ignored(self):
        for bad in ({"closures": [{"date": "2026-10-14", "markets": ["TWSE"], "reason": "", "source": "x", "addedAt": "t"}]},
                    {"closures": [{"date": "not-a-date", "markets": ["TWSE"], "reason": "r", "source": "x", "addedAt": "t"}]},
                    {"closures": [{"date": "2026-10-14", "markets": ["NYSE"], "reason": "r", "source": "x", "addedAt": "t"}]},
                    {"closures": [{"date": "2026-10-14", "markets": ["ALL"], "reason": "r", "addedAt": "t"}]}, {"closures": "x"}):
            with self.assertRaises(ManualClosureError):
                parse_manual_closures(bad)

    def test_the_shipped_override_file_is_valid_and_hard_codes_no_closure(self):
        doc = json.loads((HERE.parent / "config" / "manual_closures.json").read_text(encoding="utf-8"))
        self.assertEqual(parse_manual_closures(doc), ())

    def test_a_closure_changes_the_decision_not_the_failure_count(self):
        cal = from_twse_holiday_schedule(ROWS, manual=parse_manual_closures(self.DOC))
        pub = {"TWSE": d("2026-10-13"), "TPEX": d("2026-10-13")}
        pre = D.precheck(at("2026-10-14T22:00"), pub, cal, D.MAIN_MARKETS)
        self.assertEqual((pre.fetch, pre.state), (False, D.NO_TRADING_DAY))   # a confirmed closure: a known non-trading day, never a failure
        pre = D.precheck(at("2026-10-14T22:00"), pub, CAL, D.MAIN_MARKETS)    # unconfirmed: the pipeline cannot know, so it asks the source
        self.assertEqual(pre.owed, ("TPEX",))


class ScheduleTests(unittest.TestCase):
    def test_utc_cron_is_exact_and_not_activated(self):
        self.assertEqual(D.CRON_UTC, ("0 13 * * 1-5", "0 14 * * 1-5", "0 15 * * 1-5"))
        self.assertEqual(D.MORNING_CRON_UTC, "30 22 * * 0-4")  # 06:30 Taipei Mon-Fri = 22:30 UTC the evening before

    def test_attempt_labels_and_final(self):
        self.assertEqual([D.attempt_label(at(f"2026-10-07T{t}")) for t in ("06:30", "07:45", "21:00", "21:20", "22:00", "22:59", "23:00", "23:40")],
                         ["MORNING_TWSE", "MORNING_TWSE", "PRIMARY", "PRIMARY", "RETRY_1", "RETRY_1", "RETRY_2_FINAL", "RETRY_2_FINAL"])
        self.assertFalse(D.is_final_attempt(at("2026-10-07T22:00")))
        self.assertTrue(D.is_final_attempt(at("2026-10-07T23:00")))
        self.assertFalse(D.is_final_attempt(at("2026-10-07T06:30")))             # the morning attempt never declares a failure
        self.assertFalse(D.is_final_attempt(at("2026-10-07T23:00"), "manual"))  # a person pressing 'run' is not the retry loop

    def test_market_dates_parse_both_formats_and_reject_the_rest(self):
        self.assertEqual(D.parse_market_date("1151007"), date(2026, 10, 7))
        self.assertEqual(D.parse_market_date("2026-10-07"), date(2026, 10, 7))
        for bad in ("", None, "abc", "2026-13-45", "11510", "20261007"):
            self.assertIsNone(D.parse_market_date(bad), bad)

    def test_availability_is_labelled_as_observation_never_as_a_guarantee(self):
        for m, a in D.AVAILABILITY.items():
            self.assertEqual((a.basis, D.EVIDENCE_LEVEL), ("EMPIRICALLY_OBSERVED", "EMPIRICALLY_OBSERVED"), m)
        self.assertEqual(D.AVAILABILITY["TWSE"].expected_by.strftime("%H:%M"), "06:30")
        self.assertEqual(D.AVAILABILITY["TWSE"].offset_days, 1)
        self.assertEqual(D.AVAILABILITY["TPEX"].offset_days, 0)


class TargetExamples(unittest.TestCase):
    def test_the_approved_examples(self):
        thu = at("2026-10-08T22:30")
        self.assertEqual(D.target_date("TPEX", thu, CAL)[0], d("2026-10-08"))
        self.assertEqual(D.target_date("TWSE", thu, CAL)[0], d("2026-10-07"))
        self.assertEqual(D.target_date("TWSE", at("2026-10-09T06:30"), CAL)[0], d("2026-10-08"))   # Friday holiday: TWSE still owes Thursday
        self.assertEqual(D.target_date("TWSE", at("2026-10-09T06:29"), CAL)[0], d("2026-10-07"))
        self.assertEqual(D.target_date("TPEX", at("2026-10-08T20:59"), CAL)[0], d("2026-10-07"))
        self.assertEqual(D.target_date("TPEX", at("2026-10-08T21:00"), CAL)[0], d("2026-10-08"))
        self.assertEqual(D.target_date("TPEX", at("2027-01-05T22:00"), CAL), (None, True))        # unknown calendar: no date, never a guess


class FreshnessVectorTests(unittest.TestCase):
    def test_every_hand_calculated_vector(self):
        for c in VECTORS["cases"]:
            r = D.classify_market(c["market"], d(c["published"]) if c["published"] else None, at(c["now"]), CAL)
            got = {"status": r["status"], "expected": r["expectedMarketDate"], "lag": r["lagTradingDays"], "pending": r["updatePending"]}
            self.assertEqual(got, {k: c[k] for k in got}, c["id"])

    def test_J_dates_differ_but_both_markets_are_fresh_by_their_own_targets(self):
        now = at("2026-10-08T22:30")
        tw = D.classify_market("TWSE", d("2026-10-07"), now, CAL)
        tp = D.classify_market("TPEX", d("2026-10-08"), now, CAL)
        self.assertEqual((tw["status"], tp["status"]), (D.FRESH, D.FRESH))

    def test_B_t_minus_2_is_not_fully_current(self):
        self.assertEqual(D.classify_market("TWSE", d("2026-10-06"), at("2026-10-09T12:00"), CAL)["status"], D.STALE)

    def test_the_published_policy_block_in_the_vectors_is_what_the_engine_publishes(self):
        self.assertEqual(VECTORS["publicationPolicy"], D.publication_policy(CAL, markets=("TWSE", "TPEX", "EMERGING")))

    def test_calendar_days_alone_never_make_data_stale(self):
        for now in ("2026-10-09T09:00", "2026-10-10T12:00", "2026-10-11T23:00", "2026-10-12T20:00"):
            self.assertEqual(D.classify_market("TPEX", d("2026-10-08"), at(now), CAL)["status"], "FRESH", now)

    def test_financial_and_monthly_revenue_are_judged_on_their_own_cadence(self):
        self.assertEqual(expected_financial_period(date(2026, 10, 8)), "2026Q2")
        self.assertEqual(classify_financial("2026Q2", datetime(2026, 10, 8, 12))["status"], "FRESH")
        self.assertEqual(D.classify_market("TPEX", d("2026-10-08"), at("2026-10-09T12:00"), CAL)["status"], "FRESH")
        self.assertEqual(D.classify_monthly_revenue("2026-08", date(2026, 10, 8))["status"], "FRESH")
        self.assertEqual(D.classify_monthly_revenue("2026-08", date(2026, 10, 14))["status"], "FRESH")
        self.assertEqual(D.classify_monthly_revenue("2026-08", date(2026, 10, 16))["status"], "STALE")
        self.assertEqual(D.classify_monthly_revenue("garbage", date(2026, 10, 8))["status"], "UNKNOWN")


MAIN = D.MAIN_MARKETS


class DecisionMatrix(unittest.TestCase):
    """The A-U matrix of the brief, as decisions. `pub` = trade dates of the Last Known Good; `obs` = what the OWED markets' sources show."""

    def attempt(self, now, pub, obs, trigger="schedule", markets=MAIN):
        now = at(now)
        published = {m: (d(v) if v else None) for m, v in pub.items()}
        pre = D.precheck(now, published, CAL, markets)
        if not pre.fetch:
            return pre.state, None, pre
        observed = {m: obs[m] for m in pre.owed}   # only the owed markets are ever read
        dec = D.evaluate(now, published, observed, CAL, markets, D.is_final_attempt(now, trigger))
        return dec.state, dec, pre

    def test_A_twse_t_minus_1_and_tpex_t_is_a_plain_success(self):
        # Thursday 22:30: TWSE already carries Wednesday (06:30 run), TPEx owes Thursday and delivers it
        st, dec, pre = self.attempt("2026-10-08T22:30", {"TWSE": "2026-10-07", "TPEX": "2026-10-07"}, {"TPEX": ok("2026-10-08")})
        self.assertEqual((st, dec.publish, dec.advanced, dec.carried, dec.behind), (D.SUCCESS, True, ("TPEX",), ("TWSE",), ()))
        self.assertEqual(pre.owed, ("TPEX",))     # TWSE is NOT queried in the evening: it owes nothing until tomorrow 06:30
        self.assertEqual(dec.targets, {"TWSE": "2026-10-07", "TPEX": "2026-10-08"})

    def test_B_twse_t_minus_2_with_tpex_t_is_not_fully_current(self):
        pub = {"TWSE": "2026-10-06", "TPEX": "2026-10-08"}
        st, dec, pre = self.attempt("2026-10-08T22:30", pub, {"TWSE": ok("2026-10-06")})
        self.assertEqual((pre.owed, st, dec.publish, dec.behind), (("TWSE",), D.WAITING, False, ("TWSE",)))
        st, dec, _ = self.attempt("2026-10-08T23:10", pub, {"TWSE": ok("2026-10-06")})
        self.assertEqual((st, dec.publish), (D.FAILED_NO_NEW, False))

    def test_C_D_E_tpex_delayed_then_advances_then_never(self):
        pub = {"TWSE": "2026-10-07", "TPEX": "2026-10-07"}
        st, dec, _ = self.attempt("2026-10-08T21:00", pub, {"TPEX": ok("2026-10-07")})             # C: owes Thursday, still on Wednesday
        self.assertEqual((st, dec.publish, dec.behind), (D.WAITING, False, ("TPEX",)))
        st, dec, _ = self.attempt("2026-10-08T22:00", pub, {"TPEX": ok("2026-10-08")})             # D
        self.assertEqual((st, dec.publish), (D.SUCCESS, True))
        st, dec, _ = self.attempt("2026-10-08T23:00", pub, {"TPEX": ok("2026-10-07")})             # E: the last attempt, nothing moved
        self.assertEqual((st, dec.publish), (D.FAILED_NO_NEW, False))
        self.assertIn("NO_NEW_MARKET_DATA", dec.reason)

    def test_F_next_morning_twse_advances_and_the_release_is_new_atomic_and_valid_for_both(self):
        st, dec, pre = self.attempt("2026-10-09T06:30", {"TWSE": "2026-10-07", "TPEX": "2026-10-08"}, {"TWSE": ok("2026-10-08")})
        self.assertEqual((st, dec.publish, dec.advanced, dec.carried, pre.owed), (D.SUCCESS, True, ("TWSE",), ("TPEX",), ("TWSE",)))

    def test_G_H_holiday_with_outstanding_twse_debt_then_after_the_debt_is_paid(self):
        # Friday 2026-10-09 is closed, but TWSE still owes Thursday: that is work, not a no-trading-day
        st, dec, pre = self.attempt("2026-10-09T06:30", {"TWSE": "2026-10-07", "TPEX": "2026-10-08"}, {"TWSE": ok("2026-10-08")})
        self.assertNotEqual(st, D.NO_TRADING_DAY)
        self.assertEqual(st, D.SUCCESS)
        for now in ("2026-10-09T21:00", "2026-10-09T22:00", "2026-10-09T23:00"):                    # H: nothing is owed any more
            st, dec, pre = self.attempt(now, {"TWSE": "2026-10-08", "TPEX": "2026-10-08"}, {})
            self.assertEqual((st, pre.fetch), (D.NO_TRADING_DAY, False), now)

    def test_I_both_markets_on_the_same_date(self):
        st, dec, _ = self.attempt("2026-10-06T22:00", {"TWSE": "2026-10-05", "TPEX": "2026-10-05"}, {"TPEX": ok("2026-10-06")})
        self.assertEqual((st, dec.publish), (D.SUCCESS, True))

    def test_monday_morning_twse_catches_up_fridays_data(self):
        st, dec, pre = self.attempt("2026-10-05T06:30", {"TWSE": "2026-10-01", "TPEX": "2026-10-02"}, {"TWSE": ok("2026-10-02")})
        self.assertEqual((st, pre.owed, dec.advanced), (D.SUCCESS, ("TWSE",), ("TWSE",)))

    def test_weekend_and_holiday_runs_with_nothing_owed_make_no_request(self):
        for now in ("2026-10-10T21:00", "2026-10-11T21:00"):
            pre = D.precheck(at(now), {"TWSE": d("2026-10-08"), "TPEX": d("2026-10-08")}, CAL, MAIN)
            self.assertEqual((pre.fetch, pre.state), (False, D.NO_TRADING_DAY), now)

    def test_monday_holiday_after_a_weekend_is_a_catch_up_then_a_no_trading_day(self):
        pub = {"TWSE": "2026-10-22", "TPEX": "2026-10-23"}
        st, dec, pre = self.attempt("2026-10-26T06:30", pub, {"TWSE": ok("2026-10-23")})
        self.assertEqual((st, pre.owed), (D.SUCCESS, ("TWSE",)))
        st, dec, pre = self.attempt("2026-10-26T22:00", {"TWSE": "2026-10-23", "TPEX": "2026-10-23"}, {})
        self.assertEqual((st, dec), (D.NO_TRADING_DAY, None))

    def test_lunar_new_year_multi_day_closure(self):
        pub = {"TWSE": "2026-02-11", "TPEX": "2026-02-11"}
        for now in ("2026-02-13T06:30", "2026-02-16T21:00", "2026-02-18T22:00", "2026-02-20T23:00"):
            self.assertEqual(self.attempt(now, pub, {})[0], D.NO_TRADING_DAY, now)
        st, dec, pre = self.attempt("2026-02-12T06:30", {"TWSE": "2026-02-10", "TPEX": "2026-02-11"}, {"TWSE": ok("2026-02-11")})  # settlement-only day: TWSE still owes the last trading day
        self.assertEqual((st, pre.owed), (D.SUCCESS, ("TWSE",)))
        st, dec, pre = self.attempt("2026-02-23T22:00", pub, {"TPEX": ok("2026-02-23")})
        self.assertEqual((st, dec.advanced), (D.SUCCESS, ("TPEX",)))

    def test_K_L_after_a_success_every_retry_is_a_no_op_with_zero_requests(self):
        pub = {"TWSE": "2026-10-07", "TPEX": "2026-10-08"}
        for now in ("2026-10-08T21:00", "2026-10-08T22:00", "2026-10-08T23:00"):
            if now.endswith("21:00"):
                continue  # 21:00 is when TPEx first owes Thursday: with the Last Known Good already at Thursday nothing is owed
            pre = D.precheck(at(now), {k: d(v) for k, v in pub.items()}, CAL, MAIN)
            self.assertEqual((pre.fetch, pre.state), (False, D.NOOP_ALREADY_PUBLISHED), now)
        pre = D.precheck(at("2026-10-08T21:00"), {k: d(v) for k, v in pub.items()}, CAL, MAIN)
        self.assertEqual((pre.fetch, pre.state), (False, D.NOOP_ALREADY_PUBLISHED))

    def test_M_a_source_date_that_moves_backward_is_refused(self):
        st, dec, _ = self.attempt("2026-10-08T22:00", {"TWSE": "2026-10-07", "TPEX": "2026-10-07"}, {"TPEX": ok("2026-10-06")})
        self.assertEqual((st, dec.publish), (D.FAILED_VALIDATION, False))
        self.assertIn("MOVED_BACKWARD", dec.reason)

    def test_N_malformed_future_and_closed_day_dates_are_refused(self):
        pub = {"TWSE": "2026-10-07", "TPEX": "2026-10-07"}
        for obs, why in (({"TPEX": D.Obs("MALFORMED", None, "abc")}, "MALFORMED"), ({"TPEX": ok("2026-10-09")}, "IN_THE_FUTURE"), ({"TPEX": ok("2026-10-04")}, "MOVED_BACKWARD")):
            st, dec, _ = self.attempt("2026-10-08T22:00", pub, obs)
            self.assertEqual((st, dec.publish), (D.FAILED_VALIDATION, False))
            self.assertIn(why, dec.reason)
        st, dec, _ = self.attempt("2026-10-12T22:00", {"TWSE": "2026-10-08", "TPEX": "2026-10-08"}, {"TPEX": ok("2026-10-10")})
        self.assertIn("CLOSED_DAY", dec.reason)

    def test_O_P_one_exchange_current_the_other_delayed_when_both_owe(self):
        pub = {"TWSE": "2026-10-05", "TPEX": "2026-10-06"}   # e.g. after an outage: both owe something at 22:30 Wednesday
        obs = {"TWSE": ok("2026-10-05"), "TPEX": ok("2026-10-07")}
        st, dec, _ = self.attempt("2026-10-07T22:30", pub, obs)
        self.assertEqual((st, dec.publish, dec.behind, dec.advanced), (D.WAITING, False, ("TWSE",), ("TPEX",)))
        st, dec, _ = self.attempt("2026-10-07T23:10", pub, obs)
        self.assertEqual((st, dec.publish, dec.advanced, dec.behind), (D.SUCCESS_PARTIAL, True, ("TPEX",), ("TWSE",)))   # PARTIAL = a market that owed something did not reach its target

    def test_different_target_dates_are_never_a_partial(self):
        st, dec, _ = self.attempt("2026-10-08T22:30", {"TWSE": "2026-10-07", "TPEX": "2026-10-07"}, {"TPEX": ok("2026-10-08")})
        self.assertEqual(st, D.SUCCESS)
        self.assertNotEqual(st, D.SUCCESS_PARTIAL)

    def test_Q_R_emerging_is_its_own_failure_domain(self):
        pub = {"EMERGING": "2026-10-07"}
        st, dec, _ = self.attempt("2026-10-08T22:00", pub, {"EMERGING": ok("2026-10-08")}, markets=D.EMERGING_MARKETS)
        self.assertEqual((st, dec.publish), (D.SUCCESS, True))
        st, dec, _ = self.attempt("2026-10-08T22:00", pub, {"EMERGING": D.Obs("ERROR", None, "HTTP 503")}, markets=D.EMERGING_MARKETS)
        self.assertEqual((st, dec.publish), (D.FAILED_SOURCE, False))
        st, dec, _ = self.attempt("2026-10-08T22:30", {"TWSE": "2026-10-07", "TPEX": "2026-10-07"}, {"TPEX": ok("2026-10-08")})   # the same instant for main is decided without Emerging
        self.assertEqual((st, dec.publish), (D.SUCCESS, True))

    def test_source_errors_never_publish_and_keep_the_last_known_good(self):
        st, dec, _ = self.attempt("2026-10-07T22:30", {"TWSE": "2026-10-05", "TPEX": "2026-10-06"}, {"TWSE": D.Obs("ERROR", None, "HTTP 403"), "TPEX": ok("2026-10-07")})
        self.assertEqual((st, dec.publish, dec.advanced), (D.FAILED_SOURCE, False, ()))

    def test_unknown_calendar_never_pretends_to_know_a_holiday(self):
        pre = D.precheck(at("2027-01-05T22:00"), {"TWSE": d("2026-12-30"), "TPEX": d("2026-12-30")}, CAL, MAIN)
        self.assertEqual((pre.fetch, pre.state, pre.owed), (True, None, ("TWSE", "TPEX")))
        self.assertIn("CALENDAR_UNKNOWN", pre.reason)
        st, dec, _ = self.attempt("2027-01-05T23:00", {"TWSE": "2026-12-30", "TPEX": "2026-12-30"}, {"TWSE": ok("2026-12-30"), "TPEX": ok("2026-12-30")})
        self.assertEqual(st, D.FAILED_NO_NEW)
        self.assertIn("CALENDAR_UNKNOWN", dec.reason)

    def test_first_publication_has_no_last_known_good(self):
        st, dec, pre = self.attempt("2026-10-08T22:30", {"TWSE": None, "TPEX": None}, {"TWSE": ok("2026-10-07"), "TPEX": ok("2026-10-08")})
        self.assertEqual((st, dec.advanced, pre.owed), (D.SUCCESS, ("TWSE", "TPEX"), ("TWSE", "TPEX")))

    def test_states_are_the_documented_set(self):
        self.assertEqual(set(D.STATES), {"SUCCESS", "SUCCESS_PARTIAL", "NOOP_ALREADY_PUBLISHED", "WAITING_FOR_NEW_MARKET_DATA", "NO_TRADING_DAY_EXPECTED",
                                         "FAILED_NO_NEW_MARKET_DATA", "FAILED_SOURCE", "FAILED_VALIDATION", "FAILED_PUBLICATION"})


class PublishedMetadata(unittest.TestCase):
    def test_policy_block_lets_the_browser_decide_without_an_exchange_call(self):
        p = D.publication_policy(CAL)
        self.assertEqual((p["policy"], p["timezone"], p["attempts"], p["scheduleWeekdays"]), ("DAILY_TRADING_DAY", "Asia/Taipei", ["21:00", "22:00", "23:00"], [1, 2, 3, 4, 5]))
        self.assertEqual((p["markets"]["TWSE"]["offsetDays"], p["markets"]["TWSE"]["expectedBy"], p["markets"]["TWSE"]["firstAttempt"]), (1, "06:30", "06:30"))
        self.assertEqual((p["markets"]["TPEX"]["expectedBy"], p["markets"]["TPEX"]["firstAttempt"]), ("21:00", "21:00"))
        self.assertEqual(p["markets"]["TWSE"]["basis"], "EMPIRICALLY_OBSERVED")
        self.assertIn("2026-10-09", p["calendar"]["closedDates"])
        self.assertEqual(json.loads(json.dumps(p)), p)


if __name__ == "__main__":
    unittest.main()
