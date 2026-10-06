"""Phase 3D.4: the weekly publication cycle, end to end through run_once, on a fake clock and fake exchanges.

The CI runner is EPHEMERAL: every scheduled run starts with an empty data/runs, so each step below wipes it and relies on the history
restored from the published health.json (exactly what happens on GitHub Actions). The raw fetch and the build are replaced by fakes
(no network); the publication gate, the atomic release writer, the slot / attempt logic and the freshness verdict are the real ones.
"""
import json
import shutil
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from unittest import mock

from mitw.freshness import classify
from mitw.ingest import IngestResult
from mitw.pipeline import build, load_inputs
from mitw.providers.http import new_run
from mitw.runner import run_once
from mitw.snapshot.build import read_manifest
from tests.helpers import build_store


class Week:
    """Fake exchanges: `dates` is what a fetch returns; `fail` makes the fetch fail."""

    dates = {"TWSE": "2026-10-09", "TPEX": "2026-10-09"}
    fail = False


class WeeklyCycle(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        store, _ = build_store()
        cls.base = build(load_inputs(store, "2026-10-06"), "t")

    def setUp(self):
        self.out, self.runs, self.raw = (Path(tempfile.mkdtemp(prefix="mitw_wk_")) for _ in range(3))
        self.requests = 0
        Week.dates, Week.fail = {"TWSE": "2026-10-09", "TPEX": "2026-10-09"}, False

    def run_at(self, when: str, dates=None, fail=False, trigger="schedule"):
        """One scheduled run on a brand-new runner (data/runs wiped; history comes back from out/health.json)."""
        shutil.rmtree(self.runs, ignore_errors=True)
        self.runs.mkdir()
        market = dates or {"TWSE": "2026-10-09", "TPEX": "2026-10-09"}

        def fake_ingest(partition, raw_dir, ctx, fetch):
            ctx.request_count = 4
            self.requests += 4
            res = IngestResult(ctx=ctx)
            if fail:
                res.failures = {"twse.prices": "HTTP 503 from the exchange"}
            return res

        gate_floor = mock.patch("mitw.snapshot.gate.validate_documents", lambda *a, **k: [])  # synthetic data is far below the coverage floor
        with mock.patch("mitw.runner.ingest_daily", fake_ingest), mock.patch("mitw.runner.partition_market_dates", lambda *a: dict(market)), \
                mock.patch("mitw.runner.load_inputs", lambda *a: None), gate_floor, \
                mock.patch("mitw.runner.build", lambda inp, gen: replace(self.base, market_as_of=dict(market))):
            return run_once(trigger=trigger, now_fn=lambda: datetime.fromisoformat(when), raw_dir=self.raw, out_dir=self.out, runs_dir=self.runs,
                            fetch=lambda *a, **k: self.fail_if_called(), db_path=None)

    def fail_if_called(self):
        raise AssertionError("the fake ingest must not call the real fetch")

    def freshness(self, when: str) -> dict:
        return classify(read_manifest(self.out), datetime.fromisoformat(when))

    # ---- 1. normal weekly update, 2. already updated this week -> NOOP --------------------------------------------------------------
    def test_normal_week_then_the_14_and_20_oclock_runs_are_noops_with_zero_requests(self):
        r = self.run_at("2026-10-11T10:05:00")
        self.assertEqual((r.status, r.publication["result"]), ("SUCCESS", "PUBLISHED"))
        m = read_manifest(self.out)
        self.assertEqual(m["marketAsOf"], {"TWSE": "2026-10-09", "TPEX": "2026-10-09"})  # Friday's data, not "Sunday"
        self.assertEqual(m["lastSuccessfulPublication"], "2026-10-11T10:05:00")
        before, requests = (self.out / "manifest.json").read_bytes(), self.requests
        for when in ("2026-10-11T14:02:00", "2026-10-11T20:01:00"):
            n = self.run_at(when)
            self.assertEqual((n.status, n.requests, n.publication["result"]), ("NOOP", 0, "WEEK_ALREADY_PUBLISHED"))
        self.assertEqual(self.requests, requests)  # no external request at all
        self.assertEqual((self.out / "manifest.json").read_bytes(), before)
        self.assertEqual(self.freshness("2026-10-16T21:00:00")["status"], "FRESH")  # Friday, 4 trading days old: still fresh

    # ---- 3. the market date did not advance ------------------------------------------------------------------------------------------
    def test_new_data_date_does_not_advance_waiting_waiting_failed_and_the_manifest_never_changes(self):
        self.run_at("2026-10-11T10:05:00")
        before = (self.out / "manifest.json").read_bytes()
        stale_fetch = {"TWSE": "2026-10-09", "TPEX": "2026-10-09"}  # the exchanges still serve the same Friday
        a = self.run_at("2026-10-18T10:00:00", stale_fetch)
        b = self.run_at("2026-10-18T14:00:00", stale_fetch)
        c = self.run_at("2026-10-18T20:00:00", stale_fetch)
        self.assertEqual([x.status for x in (a, b, c)], ["WAITING", "WAITING", "FAILED"])
        self.assertTrue(all(x.publication["result"] == "NO_NEW_MARKET_DATA" for x in (a, b, c)))
        self.assertIn("NO_NEW_MARKET_DATA", c.failureReason)
        d = self.run_at("2026-10-18T20:30:00", stale_fetch)
        self.assertEqual(d.status, "SKIPPED")  # three attempts used, nothing more this week
        self.assertEqual((self.out / "manifest.json").read_bytes(), before)  # same old data is never re-labelled as this week's
        self.assertEqual(self.freshness("2026-10-18T22:00:00")["status"], "FRESH")  # the window is still open at 22:00 ...
        monday = self.freshness("2026-10-19T09:00:00")
        self.assertEqual((monday["status"], monday["missedWeeklyUpdates"]), ("STALE", 1))  # ... and Monday says so

    # ---- 4. 10:00 failure, 14:00 recovery --------------------------------------------------------------------------------------------
    def test_a_failure_at_10_recovers_at_14(self):
        self.run_at("2026-10-11T10:05:00")
        a = self.run_at("2026-10-18T10:00:00", {"TWSE": "2026-10-16", "TPEX": "2026-10-16"}, fail=True)
        self.assertEqual(a.status, "FAILED")
        self.assertEqual(read_manifest(self.out)["marketAsOf"]["TWSE"], "2026-10-09")  # Last Known Good still serving
        b = self.run_at("2026-10-18T14:00:00", {"TWSE": "2026-10-16", "TPEX": "2026-10-16"})
        self.assertEqual((b.status, b.publication["result"]), ("SUCCESS", "PUBLISHED"))
        self.assertEqual(read_manifest(self.out)["marketAsOf"], {"TWSE": "2026-10-16", "TPEX": "2026-10-16"})
        self.assertEqual(self.run_at("2026-10-18T20:00:00").status, "NOOP")
        self.assertEqual(self.freshness("2026-10-19T09:00:00")["status"], "FRESH")
        health = json.loads((self.out / "health.json").read_text(encoding="utf-8"))
        self.assertEqual(health["consecutiveFailures"], 0)  # the success reset the streak (history survived the wiped runner)

    # ---- 5. three failures ------------------------------------------------------------------------------------------------------------
    def test_three_failures_keep_the_previous_release_and_show_as_stale_on_monday(self):
        self.run_at("2026-10-11T10:05:00")
        before = (self.out / "manifest.json").read_bytes()
        for when in ("2026-10-18T10:00:00", "2026-10-18T14:00:00", "2026-10-18T20:00:00"):
            self.assertEqual(self.run_at(when, fail=True).status, "FAILED")
        self.assertEqual((self.out / "manifest.json").read_bytes(), before)
        health = json.loads((self.out / "health.json").read_text(encoding="utf-8"))
        self.assertEqual(health["consecutiveFailures"], 3)  # counted across three different ephemeral runners
        self.assertIn("503", health["lastFailure"]["failureReason"])
        self.assertEqual(self.freshness("2026-10-19T09:00:00")["status"], "STALE")

    # ---- 6. one week not updated, 7. two weeks -----------------------------------------------------------------------------------------
    def test_one_and_two_missed_weeks(self):
        self.run_at("2026-10-11T10:05:00")
        self.assertEqual(self.freshness("2026-10-26T09:00:00")["status"], "SEVERELY_STALE")  # 10-18 and 10-25 both missed
        self.assertEqual(self.freshness("2026-10-19T09:00:00")["status"], "STALE")
        self.assertEqual(self.freshness("2026-10-19T09:00:00")["missedWeeklyUpdates"], 1)

    def test_a_catch_up_run_in_midweek_publishes_when_a_slot_was_missed(self):
        self.run_at("2026-10-11T10:05:00")
        r = self.run_at("2026-10-21T09:00:00", {"TWSE": "2026-10-20", "TPEX": "2026-10-20"}, trigger="manual")  # workflow_dispatch on a Wednesday
        self.assertEqual(r.status, "SUCCESS")
        self.assertEqual(self.freshness("2026-10-22T09:00:00")["status"], "FRESH")

    def test_exchanges_on_different_dates_are_not_published_as_one_snapshot(self):
        r = self.run_at("2026-10-11T10:05:00", {"TWSE": "2026-10-09", "TPEX": "2026-10-08"})
        self.assertEqual(r.status, "WAITING")
        self.assertFalse((self.out / "manifest.json").exists())


if __name__ == "__main__":
    unittest.main()
