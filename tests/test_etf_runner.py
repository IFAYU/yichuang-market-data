"""Phase 8P: the ETF publish run and the one-time history seeding, with injected fetchers (no network)."""
import json
import shutil
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from mitw import etf as E
from mitw.etf_history import load_history
from mitw.etf_runner import SourceRefused, run_etf_publish, run_history_seed, supported_symbols
from tests.test_etf_history import MONTH
from tests.test_etf_quotes import rows

HOLIDAYS = [{"Date": "1151009", "Name": "x", "Description": "x"}]


class Runner(unittest.TestCase):
    def setUp(self):
        self.out = Path(tempfile.mkdtemp(prefix="mitw_etfr_"))
        self.addCleanup(shutil.rmtree, self.out, True)

    def fetcher(self, day):
        p, m = rows(day)
        return lambda url: p if url == E.PRICE_URL else m

    def test_publish_writes_quotes_and_history_then_a_second_day_appends(self):
        r1 = run_etf_publish(self.out, datetime.fromisoformat("2026-10-13T18:00:00+08:00"), self.fetcher("2026-10-13"), calendar_rows=HOLIDAYS)
        self.assertEqual(r1["status"], "PUBLISHED")
        h1 = load_history(self.out)
        self.assertTrue(h1 and all(list(v) == ["2026-10-13"] for v in h1.values()))
        r2 = run_etf_publish(self.out, datetime.fromisoformat("2026-10-14T18:00:00+08:00"), self.fetcher("2026-10-14"), calendar_rows=HOLIDAYS)
        self.assertEqual(r2["status"], "PUBLISHED")
        h2 = load_history(self.out)
        self.assertTrue(all(sorted(v) == ["2026-10-13", "2026-10-14"] for v in h2.values()))
        self.assertIn("0050", h2)

    def test_a_refusing_or_broken_source_publishes_nothing_and_leaves_the_last_known_good(self):
        run_etf_publish(self.out, datetime.fromisoformat("2026-10-13T18:00:00+08:00"), self.fetcher("2026-10-13"), calendar_rows=HOLIDAYS)
        before = (self.out / "etf" / "manifest.json").read_bytes()

        def refuse(url):
            raise SourceRefused("HTTP 429")
        r = run_etf_publish(self.out, datetime.fromisoformat("2026-10-14T18:00:00+08:00"), refuse, calendar_rows=HOLIDAYS)
        self.assertNotEqual(r["status"], "PUBLISHED")
        self.assertEqual((self.out / "etf" / "manifest.json").read_bytes(), before)

    def test_seeding_republishes_the_same_quotes_with_a_fuller_history_and_reports_failures(self):
        run_etf_publish(self.out, datetime.fromisoformat("2026-10-13T18:00:00+08:00"), self.fetcher("2026-10-13"), calendar_rows=HOLIDAYS)
        syms = supported_symbols(self.out)
        self.assertIn("0050", syms)
        calls = []

        def month(sym, ym):
            calls.append((sym, ym))
            return None if (sym, ym) == ("0056", "202609") else MONTH
        r = run_history_seed(self.out, months=2, symbols=["0050", "0056"], today_iso="2026-10-14", fetch_month=month, sleep=lambda s: None)
        self.assertEqual(calls, [("0050", "202609"), ("0050", "202610"), ("0056", "202609"), ("0056", "202610")])
        self.assertEqual(r["failed"], ["0056:202609"])
        m = json.loads((self.out / "etf" / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(sorted(m["files"]), ["etf-history.json", "etf-quotes.json"])
        self.assertTrue((self.out / m["files"]["etf-history.json"]["path"]).exists() and (self.out / m["files"]["etf-quotes.json"]["path"]).exists())
        h = load_history(self.out)
        self.assertIn("2026-10-13", h["0050"]); self.assertIn("2026-10-01", h["0050"])  # the published close and the seeded month side by side
        self.assertEqual(m["priceDate"], "2026-10-13")

    def test_a_second_seeding_skips_past_months_already_on_file(self):
        run_etf_publish(self.out, datetime.fromisoformat("2026-10-13T18:00:00+08:00"), self.fetcher("2026-10-13"), calendar_rows=HOLIDAYS)
        calls = []
        def fm(sym, ym):
            calls.append((sym, ym))
            roc = f"{int(ym[:4]) - 1911}/{ym[4:]}/"  # the month's own dates, like the real file
            return {**MONTH, "data": [[roc + r[0][-2:], *r[1:]] for r in MONTH["data"]]}
        run_history_seed(self.out, months=2, symbols=["0050"], today_iso="2026-10-14", fetch_month=fm, sleep=lambda s: None)
        self.assertEqual(calls, [("0050", "202609"), ("0050", "202610")])
        calls.clear()
        run_history_seed(self.out, months=3, symbols=["0050"], today_iso="2026-10-14", fetch_month=fm, sleep=lambda s: None)
        self.assertEqual(calls, [("0050", "202608"), ("0050", "202610")])  # 202609 is on file; the current month is refreshed; 202608 is new

    def test_seeding_needs_published_quotes_or_explicit_symbols(self):
        with self.assertRaises(SystemExit):
            run_history_seed(self.out, months=1, fetch_month=lambda s, y: None, sleep=lambda s: None)


if __name__ == "__main__":
    unittest.main()
