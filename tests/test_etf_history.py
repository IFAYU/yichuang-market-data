"""Phase 8P: the ETF close history published next to the ETF quotes (synthetic / fixture data, no network)."""
import json
import shutil
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from mitw.etf import HISTORY_FILE, publish_etf
from mitw.etf_history import KEEP_DAYS, append_quotes, build_history_doc, load_history, merge, month_list, parse_month, roc_slash_to_iso, seed_from_months
from mitw.snapshot.build import sha
from tests.test_etf_quotes import CAL, doc_for

MONTH = {"stat": "OK", "fields": ["日期", "成交股數", "成交金額", "開盤價", "最高價", "最低價", "收盤價", "漲跌價差", "成交筆數", "註記"],
         "data": [["115/10/01", "55,443,347", "6,223,565,909", "112.20", "112.90", "111.70", "112.90", "+0.85", "73,100", ""],
                  ["115/10/02", "68,967,747", "7,769,230,752", "112.85", "112.95", "112.30", "112.80", "-0.10", "72,280", ""],
                  ["115/10/05", "0", "0", "--", "--", "--", "--", "0.00", "0", ""],
                  ["115/10/06", "1,000", "1,000", "1,115.50", "1,116.00", "1,115.00", "1,115.50", "+0.55", "10", ""]]}


class Parse(unittest.TestCase):
    def test_roc_dates(self):
        self.assertEqual(roc_slash_to_iso("115/10/01"), "2026-10-01")
        for bad in ("115/02/30", "115-10-01", "", "115/13/01"):
            self.assertIsNone(roc_slash_to_iso(bad))

    def test_month_skips_days_without_a_close_and_strips_thousands_separators(self):
        self.assertEqual(parse_month(MONTH), [("2026-10-01", "112.90"), ("2026-10-02", "112.80"), ("2026-10-06", "1115.50")])
        for bad in (None, "x", {"stat": "很抱歉，沒有符合條件的資料!"}, {"stat": "OK", "fields": ["a"], "data": []}):
            self.assertEqual(parse_month(bad), [])

    def test_merge_counts_new_dates_and_replaces_same_date(self):
        h = {}
        self.assertEqual(merge(h, "0050", [("2026-10-01", "1.00"), ("2026-10-02", "2.00")]), 2)
        self.assertEqual(merge(h, "0050", [("2026-10-02", "2.50"), ("2026-10-05", "3.00")]), 1)
        self.assertEqual(h["0050"]["2026-10-02"], "2.50")


class Build(unittest.TestCase):
    def test_append_quotes_adds_each_supported_close_only(self):
        q = doc_for("2026-10-13")
        h = {}
        n = append_quotes(h, q)
        supported = [i for i in q["instruments"] if i["supportStatus"] == "SUPPORTED" and i["price"]]
        self.assertEqual(n, len(supported)); self.assertGreater(n, 0)
        self.assertTrue(all(h[i["symbol"]]["2026-10-13"] == i["price"] for i in supported))
        notsup = [i["symbol"] for i in q["instruments"] if i["supportStatus"] != "SUPPORTED"]
        self.assertTrue(notsup and not any(s in h for s in notsup))
        self.assertEqual(append_quotes(h, q), 0)  # the same day again adds nothing

    def test_doc_is_sorted_trimmed_string_closes_and_says_redistribution_is_unconfirmed(self):
        h = {"0056": {f"{y}-{m:02d}-{d:02d}": "50.00" for y in (2024, 2025) for m in range(1, 13) for d in range(1, 29)}, "0050": {"2026-10-01": "100.10"}, "ZZZZ": {"2026-10-01": "1.00"}}
        d = build_history_doc(h, ["0056", "0050", "9999"], "2026-10-13T00:00:00+08:00", "2026-10-13")
        self.assertEqual(sorted(d["symbols"]), ["0050", "0056"])
        self.assertEqual(len(d["symbols"]["0056"]), KEEP_DAYS)
        self.assertEqual(d["symbols"]["0056"], sorted(d["symbols"]["0056"]))
        self.assertEqual(d["symbols"]["0050"], [["2026-10-01", "100.10"]])
        self.assertEqual(d["terms"]["redistributionStatus"], "UNCONFIRMED_FOR_THESE_ENDPOINTS")
        self.assertNotIn("holding", json.dumps(d).lower())


class Seed(unittest.TestCase):
    def test_seed_requests_each_symbol_month_with_a_gap_and_reports_failures(self):
        calls, sleeps = [], []

        def fetch(sym, ym):
            calls.append((sym, ym))
            return None if (sym, ym) == ("0056", "202609") else MONTH

        h = {}
        r = seed_from_months(h, ["0050", "0056"], ["202609", "202610"], fetch, sleep=sleeps.append, gap_s=3.0)
        self.assertEqual(calls, [("0050", "202609"), ("0050", "202610"), ("0056", "202609"), ("0056", "202610")])
        self.assertEqual(sleeps, [3.0] * 4)
        self.assertEqual(r["requests"], 4); self.assertEqual(r["failed"], [("0056", "202609")])
        self.assertEqual(len(h["0050"]), 3); self.assertEqual(len(h["0056"]), 3)

    def test_month_list(self):
        self.assertEqual(month_list("2026-02-10", 4), ["202511", "202512", "202601", "202602"])


class Publish(unittest.TestCase):
    def setUp(self):
        self.out = Path(tempfile.mkdtemp(prefix="mitw_etfh_"))
        self.addCleanup(shutil.rmtree, self.out, True)

    def test_release_carries_the_history_file_verified_in_the_manifest_and_it_round_trips(self):
        q = doc_for("2026-10-13", "2026-10-13T18:00:00+08:00")
        h = {}
        append_quotes(h, q); merge(h, "0050", [("2026-10-12", "99.00")])
        hd = build_history_doc(h, [i["symbol"] for i in q["instruments"] if i["supportStatus"] == "SUPPORTED"], "2026-10-13T18:00:00+08:00", "2026-10-13")
        r = publish_etf(q, self.out, datetime.fromisoformat("2026-10-13T18:00:00+08:00"), CAL, history_doc=hd)
        self.assertEqual(r.outcome.status, "PUBLISHED")
        m = json.loads((self.out / "etf" / "manifest.json").read_text(encoding="utf-8"))
        f = m["files"][HISTORY_FILE]
        blob = (self.out / f["path"]).read_bytes()
        self.assertEqual(sha(blob), f["sha256"]); self.assertEqual(len(blob), f["bytes"])
        self.assertIn("etf-quotes.json", m["files"])
        self.assertEqual(m["release"], f["path"].split("/")[2])
        self.assertEqual(load_history(self.out)["0050"]["2026-10-12"], "99.00")

    def test_without_history_the_release_is_what_it_was_before(self):
        q = doc_for("2026-10-13", "2026-10-13T18:00:00+08:00")
        publish_etf(q, self.out, datetime.fromisoformat("2026-10-13T18:00:00+08:00"), CAL)
        m = json.loads((self.out / "etf" / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(list(m["files"]), ["etf-quotes.json"])
        self.assertEqual(m["release"], m["files"]["etf-quotes.json"]["sha256"][:12])
        self.assertEqual(load_history(self.out), {})


if __name__ == "__main__":
    unittest.main()
