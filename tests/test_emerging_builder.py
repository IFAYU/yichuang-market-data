"""Phase 3I.1 (STAGED): the emerging builder's daily mode. Synthetic official-shaped payloads written to a temp dir; no network."""
from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).parent
spec = importlib.util.spec_from_file_location("build_emerging_snapshot", HERE.parent / "scripts" / "build_emerging_snapshot.py")
B = importlib.util.module_from_spec(spec)
spec.loader.exec_module(B)
TAIPEI = timezone(timedelta(hours=8))
CAL = HERE / "fixtures" / "twse_holiday_schedule_2026.json"


def q(code, date="1151007", avg="10", prev="9.5", vol="1000"):
    return {"Date": date, "Time": "163005", "SecuritiesCompanyCode": code, "CompanyName": code, "PreviousAveragePrice": prev, "BuyingPrice": "9.9", "BuyingQuantity": "1",
            "SellingPrice": "10.1", "SellingQuantity": "1", "Highest": "10", "Lowest": "9", "Average": avg, "LatestPrice": "10", "SuspendTime": "000000", "TransactionVolume": vol}


def write_api(d: Path, quotes, highlight_date="1151007", registered=None, extra_missing=()):
    codes = [r["SecuritiesCompanyCode"] for r in quotes]
    files = {
        "tpex_esb_latest_statistics": quotes,
        "tpex_esb_highlight": [{"Date": highlight_date, "RegisteredStocksNumber": str(len(quotes) if registered is None else registered)}],
        "esb_basic_R": [{"SecuritiesCompanyCode": c, "CompanyName": f"{c}股份有限公司", "CompanyAbbreviation": c, "SecuritiesIndustryCode": "02", "DateOfListing": "20200101",
                         "Paidin.Capital.NTDollars": "100000000", "ParValueOfCommonStock": "新台幣 10.0000元"} for c in codes],
        "esb_bs_ci_U": [{"公司代號": c, "年度": "115", "季別": "2", "每股參考淨值": "12.5"} for c in codes],
        "esb_is_ci_U": [{"SecuritiesCompanyCode": c, "年度": "115", "季別": "2", "營業收入": "1000", "營業利益（損失）": "100", "本期淨利（淨損）": "80", "基本每股盈餘（元）": "0.8"} for c in codes],
        "esb_rev_R": [{"公司代號": c, "資料年月": "11508", "產業別": "食品工業", "累計營業收入-當月累計營收": "900", "累計營業收入-去年累計營收": "800"} for c in codes],
    }
    for k, v in files.items():
        (d / f"{k}.json").write_text(json.dumps(v, ensure_ascii=False), encoding="utf-8")


class MarketDate(unittest.TestCase):
    def test_snapshot_market_date_comes_from_the_market_aggregate(self):
        rows = [q("1000"), q("1001")]
        self.assertEqual(B.snapshot_market_date([{"Date": "1151007", "RegisteredStocksNumber": "2"}], rows), ("2026-10-07", []))

    def test_S_a_company_without_a_trade_today_does_not_make_the_snapshot_old(self):
        rows = [q("1000"), q("1001", avg="", vol="0", prev="9.5")]  # 1001: no trade, falls back to the previous average, but its row still carries today's date
        date, problems = B.snapshot_market_date([{"Date": "1151007", "RegisteredStocksNumber": "2"}], rows)
        self.assertEqual((date, problems), ("2026-10-07", []))

    def test_disagreement_between_aggregate_and_rows_is_a_validation_failure(self):
        date, problems = B.snapshot_market_date([{"Date": "1151008", "RegisteredStocksNumber": "2"}], [q("1000"), q("1001")])
        self.assertTrue(any(p.startswith("HIGHLIGHT_DATE_") for p in problems), problems)
        _, problems = B.snapshot_market_date([{"Date": "1151007"}], [q("1000"), q("1001", date="1151006")])
        self.assertTrue(any(p.startswith("QUOTE_DATES_DIFFER") for p in problems), problems)
        _, problems = B.snapshot_market_date([{"Date": "abc"}], [q("1000")])
        self.assertIn("HIGHLIGHT_DATE_MISSING_OR_MALFORMED", problems)
        _, problems = B.snapshot_market_date([{"Date": "1151007", "RegisteredStocksNumber": "5"}], [q("1000")])
        self.assertTrue(any(p.startswith("REGISTERED_COUNT_") for p in problems), problems)
        _, problems = B.snapshot_market_date([], [q("1000")])
        self.assertIn("HIGHLIGHT_DATE_MISSING_OR_MALFORMED", problems)


class DailyBuild(unittest.TestCase):
    def build(self, quotes, **kw):
        with tempfile.TemporaryDirectory() as t:
            write_api(Path(t), quotes, **kw)
            return B.build(Path(t), datetime(2026, 10, 7, 22, 5, tzinfo=TAIPEI), daily=True)

    def test_daily_snapshot_has_no_clock_inside_so_the_same_market_gives_the_same_release(self):
        a = self.build([q("1000"), q("1001")])
        with tempfile.TemporaryDirectory() as t:
            write_api(Path(t), [q("1000"), q("1001")])
            b = B.build(Path(t), datetime(2026, 10, 7, 23, 40, tzinfo=TAIPEI), daily=True)  # 95 minutes later
        self.assertEqual(a[1], b[1])
        self.assertNotIn("generatedAt", a[0])
        self.assertEqual(a[0]["marketAsOf"], {"EMERGING": "2026-10-07"})

    def test_daily_mode_refuses_a_snapshot_whose_market_date_cannot_be_proven(self):
        with self.assertRaises(SystemExit) as e:
            self.build([q("1000"), q("1001")], highlight_date="1151008")
        self.assertIn("MARKET_DATE_VALIDATION_FAILED", str(e.exception))

    def test_default_mode_is_unchanged_by_the_extra_highlight_input(self):
        with tempfile.TemporaryDirectory() as t:
            write_api(Path(t), [q("1000")])
            (Path(t) / "tpex_esb_highlight.json").unlink()  # an old api dir without the aggregate still builds in the 3H.2B mode
            doc, _ = B.build(Path(t), datetime(2026, 10, 7, 22, 5, tzinfo=TAIPEI))
        self.assertIn("generatedAt", doc)
        self.assertEqual(doc["marketAsOf"]["EMERGING"], "2026-10-07")


class DailyDecision(unittest.TestCase):
    def run_daily(self, now, quotes, current=None, trigger="schedule", **kw):
        import io
        from contextlib import redirect_stdout
        with tempfile.TemporaryDirectory() as t:
            t = Path(t)
            write_api(t / "api", quotes, **kw) if (t / "api").mkdir() is None else None
            cur = None
            if current:
                cur = t / "cur.json"
                cur.write_text(json.dumps(current), encoding="utf-8")
            args = type("A", (), dict(api=str(t / "api"), out=str(t / "out"), fetch=False, calendar=str(CAL), current_manifest=str(cur) if cur else None, now=now, trigger=trigger))
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = B.run_daily(args)
            published = sorted(p.name for p in (t / "out" / "emerging" / "releases").glob("*")) if (t / "out" / "emerging" / "releases").exists() else []
            return code, json.loads(buf.getvalue().strip().splitlines()[-1]), published

    LKG = {"release": "aaaaaaaaaaaa", "sourceHash": "a" * 64, "marketAsOf": {"EMERGING": "2026-10-06"}}

    def test_Q_emerging_current_publishes_and_writes_the_manifest_last(self):
        code, out, published = self.run_daily("2026-10-07T22:10:00+08:00", [q("1000"), q("1001")], self.LKG)
        self.assertEqual((code, out["state"], out["publish"], len(published)), (0, "SUCCESS", True, 1))

    def test_K_nothing_owed_means_zero_requests_and_no_release(self):
        lkg = {**self.LKG, "marketAsOf": {"EMERGING": "2026-10-07"}}
        code, out, published = self.run_daily("2026-10-07T22:10:00+08:00", [q("1000")], lkg)
        self.assertEqual((code, out["state"], out["requests"], published), (0, "NOOP_ALREADY_PUBLISHED", 0, []))

    def test_endpoint_failure_keeps_the_last_known_good(self):
        with tempfile.TemporaryDirectory() as t:
            t = Path(t)
            (t / "api").mkdir()  # nothing readable: a source failure
            (t / "cur.json").write_text(json.dumps(self.LKG), encoding="utf-8")
            args = type("A", (), dict(api=str(t / "api"), out=str(t / "out"), fetch=False, calendar=str(CAL), current_manifest=str(t / "cur.json"), now="2026-10-07T22:10:00+08:00", trigger="schedule"))
            import io
            from contextlib import redirect_stdout
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = B.run_daily(args)
            out = json.loads(buf.getvalue().strip().splitlines()[-1])
            self.assertEqual((code, out["state"]), (1, "FAILED_SOURCE"))
            self.assertFalse((t / "out").exists())

    def test_final_attempt_without_new_data_fails_honestly_and_writes_nothing(self):
        code, out, published = self.run_daily("2026-10-08T23:10:00+08:00", [q("1000")], {**self.LKG, "marketAsOf": {"EMERGING": "2026-10-07"}})
        self.assertEqual((code, out["state"], published), (1, "FAILED_NO_NEW_MARKET_DATA", []))

    def test_a_manual_run_is_never_the_final_attempt(self):
        code, out, _ = self.run_daily("2026-10-08T23:10:00+08:00", [q("1000")], {**self.LKG, "marketAsOf": {"EMERGING": "2026-10-07"}}, trigger="manual")
        self.assertEqual(out["state"], "WAITING_FOR_NEW_MARKET_DATA")

    def test_inconsistent_market_date_is_a_validation_failure_not_a_publication(self):
        code, out, published = self.run_daily("2026-10-07T22:10:00+08:00", [q("1000"), q("1001")], self.LKG, highlight_date="1151008")
        self.assertEqual((code, out["state"], published), (1, "FAILED_VALIDATION", []))


class DailyObservabilityAndRetention(unittest.TestCase):
    def run_daily_out(self, now, quotes, current=None, out_pre=None, highlight_date="1151008"):
        import io
        from contextlib import redirect_stdout
        t = Path(tempfile.mkdtemp(prefix="mitw_eo_"))
        write_api(t / "api", quotes, highlight_date=highlight_date) if (t / "api").mkdir() is None else None
        cur = None
        if current:
            cur = t / "cur.json"
            cur.write_text(json.dumps(current), encoding="utf-8")
        if out_pre:
            out_pre(t / "out")
        args = type("A", (), dict(api=str(t / "api"), out=str(t / "out"), fetch=False, calendar=str(CAL), current_manifest=str(cur) if cur else None, now=now, trigger="schedule"))
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = B.run_daily(args)
        return code, json.loads(buf.getvalue().strip().splitlines()[-1]), t

    LKG = {"release": "aaaaaaaaaaaa", "sourceHash": "a" * 64, "marketAsOf": {"EMERGING": "2026-10-07"}}

    def test_every_observability_field_of_the_brief_is_reported(self):
        quotes = [q("1000", "1151008"), q("1001", "1151008", avg="", vol="0", prev="9.5"), q("1002", "1151008")]
        code, out, t = self.run_daily_out("2026-10-08T22:10:00+08:00", quotes, self.LKG)
        for k in ("businessTimeTaipei", "attemptType", "expectedMarketDate", "actualSourceMarketDate", "previousPublishedMarketDate", "availabilityBasis", "evidenceLevel", "decision", "releaseId",
                  "companyCount", "sourceCounts", "sourceHash", "publicVerification", "manifestUpdated", "lastKnownGoodPreserved", "quoteCount", "currentDayPriceCount",
                  "previousDayFallbackCount", "bvpsCoverage", "pbCoverage"):
            self.assertIn(k, out, k)
        self.assertEqual((out["decision"], out["attemptType"], out["expectedMarketDate"], out["actualSourceMarketDate"], out["previousPublishedMarketDate"]),
                         ("SUCCESS", "RETRY_1", "2026-10-08", "2026-10-08", "2026-10-07"))
        self.assertEqual((out["quoteCount"], out["currentDayPriceCount"], out["previousDayFallbackCount"], out["bvpsCoverage"], out["pbCoverage"]), (3, 2, 1, 3, 3))
        self.assertEqual((out["manifestUpdated"], out["evidenceLevel"], out["publicVerification"]), (True, "EMPIRICALLY_OBSERVED", "PENDING_PUBLIC_READBACK"))

    def test_the_release_carries_its_own_record_and_the_manifest_points_at_it(self):
        code, out, t = self.run_daily_out("2026-10-08T22:10:00+08:00", [q("1000", "1151008"), q("1001", "1151008")], self.LKG)
        m = json.loads((t / "out" / "emerging" / "manifest.json").read_text(encoding="utf-8"))
        rj = json.loads((t / "out" / "emerging" / "releases" / m["release"] / "release.json").read_text(encoding="utf-8"))
        self.assertEqual((m["release"], m["updatePolicy"], rj["publishedAt"]), (out["release"], "DAILY_TRADING_DAY", m["publishedAt"]))
        self.assertEqual(m["publicationPolicy"]["markets"]["EMERGING"]["expectedBy"], "21:00")

    def test_retention_prunes_old_releases_but_never_the_current_or_the_previous_one(self):
        def seed(out):
            for name, when in (("oldoldoldold", "2025-01-01T22:00:00+08:00"), ("aaaaaaaaaaaa", "2024-01-01T22:00:00+08:00"), ("recentrecent", "2026-10-06T22:00:00+08:00")):
                d = out / "emerging" / "releases" / name
                d.mkdir(parents=True)
                (d / "emerging-snapshot.json").write_text("{}", encoding="utf-8")
                (d / "release.json").write_text(json.dumps({"publishedAt": when}), encoding="utf-8")
        code, out, t = self.run_daily_out("2026-10-08T22:10:00+08:00", [q("1000", "1151008"), q("1001", "1151008")], self.LKG, out_pre=seed)
        left = sorted(p.name for p in (t / "out" / "emerging" / "releases").iterdir())
        self.assertIn("aaaaaaaaaaaa", left)          # the Last Known Good (rollback target) is protected although it is ancient
        self.assertIn("recentrecent", left)
        self.assertNotIn("oldoldoldold", left)
        self.assertIn(out["release"], left)
        self.assertEqual(out["pruned"], ["oldoldoldold"])

    def test_R_an_unchanged_emerging_market_writes_nothing_at_all(self):
        code, out, t = self.run_daily_out("2026-10-08T22:10:00+08:00", [q("1000")], {**self.LKG, "marketAsOf": {"EMERGING": "2026-10-08"}})
        self.assertEqual((out["decision"], out["requests"], out["manifestUpdated"]), ("NOOP_ALREADY_PUBLISHED", 0, False))
        self.assertFalse((t / "out").exists())


if __name__ == "__main__":
    unittest.main()
