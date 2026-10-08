"""Phase 3I.2: the daily TWSE / TPEx pipeline, end to end through run_daily, on a fake clock and fake exchanges.

Same technique as test_weekly_scenarios: the raw fetch and the build are replaced by fakes (no network); the daily precheck / evaluate, the daily gate, the atomic
release writer, the run log and the health file are the real ones. Every step starts on a brand-new runner (data/runs wiped; the history and the carried raw
partition names come back from the published manifest / health.json, exactly as on GitHub Actions).
"""
import json
import shutil
import tempfile
import unittest
from dataclasses import replace
from datetime import date, datetime
from pathlib import Path
from unittest import mock

from mitw import daily as D
from mitw.daily_ingest import ingest_markets
from mitw.ingest import IngestResult
from mitw.pipeline import build, load_inputs
from mitw.providers.endpoints import ENDPOINTS
from mitw.providers.http import new_run
from mitw.daily_runner import run_daily
from mitw.raw.store import RawMeta, RawStore, sha256_hex
from mitw.snapshot.build import read_manifest
from tests.helpers import build_store, write_raw

HERE = Path(__file__).parent
ROWS = json.loads((HERE / "fixtures" / "twse_holiday_schedule_2026.json").read_text(encoding="utf-8-sig"))
NO_CLOSURES = HERE / "does-not-exist.json"


class Daily(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        store, _ = build_store()
        cls.base = build(load_inputs(store, "2026-10-06"), "t")

    def setUp(self):
        self.out, self.runs, self.raw = (Path(tempfile.mkdtemp(prefix="mitw_dl_")) for _ in range(3))
        self.calls = []  # (partition, fetch_markets) of every ingest that really happened
        self.gate = mock.patch("mitw.snapshot.daily_publish.validate_documents", lambda *a, **k: [])  # synthetic data is far below the coverage floor
        self.gate.start()
        self.addCleanup(self.gate.stop)

    def run_at(self, when: str, dates: dict, fail: dict | None = None, trigger="schedule", rows=ROWS):
        """One run on a brand-new runner. `dates` = what the exchanges would show; `fail` = {market: reason} makes that market's fetch fail."""
        shutil.rmtree(self.runs, ignore_errors=True)
        self.runs.mkdir()

        def fake_ingest(partition, raw_dir, ctx, fetch_markets, carry, fetch):
            self.calls.append((partition, tuple(fetch_markets), dict(carry)))
            ctx.request_count = 2 * len(fetch_markets)
            res = IngestResult(ctx=ctx)
            for m, why in (fail or {}).items():
                if m in fetch_markets:
                    res.failures[f"{m.lower()}.prices"] = why
            return res, ()

        def fake_dates(raw_dir, partition):
            return dict(dates)

        with mock.patch("mitw.daily_runner.ingest_markets", fake_ingest), mock.patch("mitw.daily_runner.partition_market_dates", fake_dates), \
                mock.patch("mitw.daily_runner.load_inputs", lambda *a: None), \
                mock.patch("mitw.daily_runner.build", lambda inp, gen: replace(self.base, market_as_of=dict(dates))):
            return run_daily(trigger=trigger, now_fn=lambda: datetime.fromisoformat(when), calendar_rows=rows, manual_closures_path=NO_CLOSURES,
                             raw_dir=self.raw, out_dir=self.out, runs_dir=self.runs, fetch=lambda *a, **k: self.fail("the fake ingest must not call the real fetch"))

    def manifest(self):
        return read_manifest(self.out)

    # ---- A / the whole approved day ---------------------------------------------------------------------------------------------------------------
    def test_a_normal_day_end_to_end_with_different_market_dates(self):
        # 06:30 Thursday: TWSE Wednesday arrives (first publication, nothing carried yet: both markets are read)
        r = self.run_at("2026-10-08T06:30:00", {"TWSE": "2026-10-07", "TPEX": "2026-10-07"})
        self.assertEqual((r.status, r.publication["result"]), (D.SUCCESS, "PUBLISHED"))
        self.assertEqual(self.calls[-1][1], ("TWSE", "TPEX"))      # no Last Known Good to carry from: both owe
        # 21:00 Thursday: TPEx owes Thursday and it is not out yet -> WAITING, the manifest does not move
        before = (self.out / "manifest.json").read_bytes()
        r = self.run_at("2026-10-08T21:00:00", {"TWSE": "2026-10-07", "TPEX": "2026-10-07"})
        self.assertEqual(r.status, D.WAITING)
        self.assertEqual(self.calls[-1][1], ("TPEX",))              # TWSE is NOT asked in the evening
        self.assertEqual((self.out / "manifest.json").read_bytes(), before)
        # 22:00: TPEx Thursday arrives; TWSE stays at Wednesday = its own target: a plain SUCCESS, dates differ
        r = self.run_at("2026-10-08T22:00:00", {"TWSE": "2026-10-07", "TPEX": "2026-10-08"})
        self.assertEqual((r.status, r.publication["result"]), (D.SUCCESS, "PUBLISHED"))
        m = self.manifest()
        self.assertEqual(m["marketAsOf"], {"TWSE": "2026-10-07", "TPEX": "2026-10-08"})
        self.assertEqual((m["updatePolicy"], m["decision"]["state"], m["decision"]["carried"]), ("DAILY_TRADING_DAY", "SUCCESS", ["TWSE"]))
        self.assertTrue(all(m["markets"][k]["targetReached"] for k in ("TWSE", "TPEX")))
        self.assertEqual((m["markets"]["TWSE"]["expectedMarketDate"], m["markets"]["TPEX"]["expectedMarketDate"]), ("2026-10-07", "2026-10-08"))
        self.assertEqual({m["markets"][k]["availabilityBasis"] for k in m["markets"]}, {"EMPIRICALLY_OBSERVED"})
        self.assertEqual(m["freshnessAtPublish"]["TWSE"]["status"], "FRESH")
        self.assertEqual(m["freshnessAtPublish"]["TPEX"]["status"], "FRESH")
        self.assertIn("calendar", m["publicationPolicy"])
        # 23:00: nothing owed -> NOOP, zero requests, manifest byte-identical
        n_calls, before = len(self.calls), (self.out / "manifest.json").read_bytes()
        r = self.run_at("2026-10-08T23:00:00", {"TWSE": "2026-10-07", "TPEX": "2026-10-08"})
        self.assertEqual((r.status, r.requests, len(self.calls)), (D.NOOP_ALREADY_PUBLISHED, 0, n_calls))
        self.assertEqual((self.out / "manifest.json").read_bytes(), before)
        # Friday 06:30 (a market holiday): TWSE still owes Thursday -> a new atomic release, TPEx unchanged
        r = self.run_at("2026-10-09T06:30:00", {"TWSE": "2026-10-08", "TPEX": "2026-10-08"})
        self.assertEqual((r.status, self.calls[-1][1]), (D.SUCCESS, ("TWSE",)))
        self.assertEqual(self.manifest()["marketAsOf"], {"TWSE": "2026-10-08", "TPEX": "2026-10-08"})
        # Friday 21:00 / 22:00 / 23:00: debt paid, closed day -> NO_TRADING_DAY_EXPECTED with zero requests
        n_calls = len(self.calls)
        for t in ("21:00", "22:00", "23:00"):
            r = self.run_at(f"2026-10-09T{t}:00", {"TWSE": "2026-10-08", "TPEX": "2026-10-08"})
            self.assertEqual((r.status, r.requests), (D.NO_TRADING_DAY, 0), t)
        self.assertEqual(len(self.calls), n_calls)

    def test_the_final_attempt_without_new_data_fails_honestly_and_keeps_the_last_known_good(self):
        self.run_at("2026-10-08T06:30:00", {"TWSE": "2026-10-07", "TPEX": "2026-10-07"})
        before = (self.out / "manifest.json").read_bytes()
        for when, want in (("2026-10-08T21:00:00", D.WAITING), ("2026-10-08T22:00:00", D.WAITING), ("2026-10-08T23:00:00", D.FAILED_NO_NEW)):
            r = self.run_at(when, {"TWSE": "2026-10-07", "TPEX": "2026-10-07"})
            self.assertEqual(r.status, want, when)
        self.assertEqual((self.out / "manifest.json").read_bytes(), before)
        h = json.loads((self.out / "health.json").read_text(encoding="utf-8"))
        self.assertEqual((h["runs"][-1]["status"], h["consecutiveFailures"]), (D.FAILED_NO_NEW, 1))

    def test_a_source_failure_and_a_backward_date_never_touch_the_manifest(self):
        self.run_at("2026-10-08T06:30:00", {"TWSE": "2026-10-07", "TPEX": "2026-10-07"})
        before = (self.out / "manifest.json").read_bytes()
        r = self.run_at("2026-10-08T22:00:00", {"TWSE": "2026-10-07", "TPEX": "2026-10-08"}, fail={"TPEX": "HTTP 503 from the exchange"})
        self.assertEqual(r.status, D.FAILED_SOURCE)
        r = self.run_at("2026-10-08T22:00:00", {"TWSE": "2026-10-07", "TPEX": "2026-10-06"})
        self.assertEqual(r.status, D.FAILED_VALIDATION)
        self.assertIn("MOVED_BACKWARD", r.failureReason)
        self.assertEqual((self.out / "manifest.json").read_bytes(), before)

    def test_zero_request_outcomes_do_not_rewrite_health_json(self):
        self.run_at("2026-10-08T06:30:00", {"TWSE": "2026-10-07", "TPEX": "2026-10-07"})
        self.run_at("2026-10-08T22:00:00", {"TWSE": "2026-10-07", "TPEX": "2026-10-08"})
        health = (self.out / "health.json").read_bytes()
        self.run_at("2026-10-08T23:00:00", {"TWSE": "2026-10-07", "TPEX": "2026-10-08"})
        self.assertEqual((self.out / "health.json").read_bytes(), health)

    def test_saturday_morning_pays_fridays_twse_data_and_never_repeats(self):
        # Friday 22:00: TWSE has Thursday, TPEx has Friday (first publication)
        self.run_at("2026-10-16T22:00:00", {"TWSE": "2026-10-15", "TPEX": "2026-10-16"})
        before = self.manifest()["release"]
        # Saturday 06:30 is not a trading day, but TWSE still owes Friday: a TWSE-only atomic release, TPEx carried
        r = self.run_at("2026-10-17T06:30:00", {"TWSE": "2026-10-16", "TPEX": "2026-10-16"})
        self.assertEqual((r.status, self.calls[-1][1]), (D.SUCCESS, ("TWSE",)))
        m = self.manifest()
        self.assertNotEqual(m["release"], before)
        self.assertEqual(m["marketAsOf"], {"TWSE": "2026-10-16", "TPEX": "2026-10-16"})
        # a second Saturday attempt, and the following Monday 06:30: nothing owed -> no request, no new release
        n_calls, rel = len(self.calls), m["release"]
        for when, state in (("2026-10-17T06:40:00", D.NO_TRADING_DAY), ("2026-10-19T06:30:00", D.NOOP_ALREADY_PUBLISHED)):
            r = self.run_at(when, {"TWSE": "2026-10-16", "TPEX": "2026-10-16"})
            self.assertEqual((r.status, r.requests), (state, 0), when)
        self.assertEqual((len(self.calls), self.manifest()["release"]), (n_calls, rel))

    def test_a_closed_day_morning_is_not_reported_as_a_fault_when_the_debt_is_paid(self):
        self.run_at("2026-10-08T22:00:00", {"TWSE": "2026-10-07", "TPEX": "2026-10-08"})
        self.run_at("2026-10-09T06:30:00", {"TWSE": "2026-10-08", "TPEX": "2026-10-08"})   # holiday: pays Thursday
        n = len(self.calls)
        r = self.run_at("2026-10-09T06:45:00", {"TWSE": "2026-10-08", "TPEX": "2026-10-08"})
        self.assertIn(r.status, (D.NO_TRADING_DAY, D.NOOP_ALREADY_PUBLISHED))
        self.assertFalse(str(r.status).startswith("FAILED"))
        self.assertEqual(len(self.calls), n)

    def test_saturday_retries_wait_without_failing_then_pay_once_and_ask_nothing_when_paid(self):
        self.run_at("2026-10-16T22:00:00", {"TWSE": "2026-10-15", "TPEX": "2026-10-16"})
        old = self.manifest()["release"]
        # 06:30 and 07:30: Friday's TWSE file is not out yet -> WAITING (only TWSE asked, never a failure, manifest untouched)
        for when in ("2026-10-17T06:30:00", "2026-10-17T07:30:00"):
            before = (self.out / "manifest.json").read_bytes()
            r = self.run_at(when, {"TWSE": "2026-10-15", "TPEX": "2026-10-16"})
            self.assertEqual((r.status, self.calls[-1][1]), (D.WAITING, ("TWSE",)), when)
            self.assertEqual((self.out / "manifest.json").read_bytes(), before)
        # 08:30: it arrived -> exactly one TWSE-only release
        r = self.run_at("2026-10-17T08:30:00", {"TWSE": "2026-10-16", "TPEX": "2026-10-16"})
        self.assertEqual((r.status, self.calls[-1][1]), (D.SUCCESS, ("TWSE",)))
        rel = self.manifest()["release"]
        self.assertNotEqual(rel, old)
        # a late duplicate of the 08:30 slot: zero requests, no new release
        n = len(self.calls)
        r = self.run_at("2026-10-17T08:45:00", {"TWSE": "2026-10-16", "TPEX": "2026-10-16"})
        self.assertEqual((r.status, r.requests, len(self.calls), self.manifest()["release"]), (D.NO_TRADING_DAY, 0, n, rel))

    def test_when_0630_already_paid_the_saturday_retries_make_no_official_request(self):
        self.run_at("2026-10-16T22:00:00", {"TWSE": "2026-10-15", "TPEX": "2026-10-16"})
        self.run_at("2026-10-17T06:30:00", {"TWSE": "2026-10-16", "TPEX": "2026-10-16"})
        n = len(self.calls)
        for when in ("2026-10-17T07:30:00", "2026-10-17T08:30:00"):
            r = self.run_at(when, {"TWSE": "2026-10-16", "TPEX": "2026-10-16"})
            self.assertEqual(r.requests, 0, when)
        self.assertEqual(len(self.calls), n)

    def test_observability_fields_are_recorded(self):
        self.run_at("2026-10-08T06:30:00", {"TWSE": "2026-10-07", "TPEX": "2026-10-07"})
        r = self.run_at("2026-10-08T22:00:00", {"TWSE": "2026-10-07", "TPEX": "2026-10-08"})
        o = r.publication["daily"]
        for k in ("businessTimeTaipei", "attemptType", "markets", "decision", "releaseId", "companyCount", "sourceCounts", "sourceHash", "publicVerification", "manifestUpdated",
                  "lastKnownGoodPreserved"):
            self.assertIn(k, o)
        self.assertEqual((o["attemptType"], o["decision"], o["manifestUpdated"], o["publicVerification"]), ("RETRY_1", "SUCCESS", True, "PENDING_PUBLIC_READBACK"))
        tw, tp = o["markets"]["TWSE"], o["markets"]["TPEX"]
        self.assertEqual((tw["queried"], tw["carried"], tw["expectedMarketDate"], tw["actualSourceMarketDate"], tw["previousPublishedMarketDate"]), (False, True, "2026-10-07", "2026-10-07", "2026-10-07"))
        self.assertEqual((tp["queried"], tp["expectedMarketDate"], tp["actualSourceMarketDate"], tp["previousPublishedMarketDate"], tp["evidenceLevel"]), (True, "2026-10-08", "2026-10-08", "2026-10-07", "EMPIRICALLY_OBSERVED"))
        self.assertTrue(o["lastKnownGoodPreserved"])

    def test_public_verification_is_recorded_after_the_readback(self):
        from mitw.runlog import mark_last_run_verified
        self.run_at("2026-10-08T06:30:00", {"TWSE": "2026-10-07", "TPEX": "2026-10-07"})
        rel = self.manifest()["release"]
        last = mark_last_run_verified(rel, "2026-10-08T06:35:00+08:00", self.runs, self.out)
        self.assertEqual(last["publication"]["daily"]["publicVerification"], "VERIFIED_PUBLIC_READBACK")
        # ... and the published health.json says the same (the block is built AFTER the record was updated, not before)
        from mitw.daily_runner import daily_health_block
        mark_last_run_verified(rel, "2026-10-08T06:36:00+08:00", self.runs, self.out, daily=lambda: daily_health_block(__import__("mitw.runlog", fromlist=["load_runs"]).load_runs(self.runs), self.manifest(), datetime.fromisoformat("2026-10-08T06:36:00+08:00")))
        health = json.loads((self.out / "health.json").read_text(encoding="utf-8"))
        self.assertEqual(health["daily"]["lastDecision"]["publicVerification"], "VERIFIED_PUBLIC_READBACK")

    # ---- migration: a WEEKLY Last Known Good --------------------------------------------------------------------------------------------------------
    def test_a_weekly_last_known_good_is_accepted_and_both_markets_are_read_once(self):
        weekly = {"schemaVersion": "1.4.0", "release": "w" * 12, "updatePolicy": "WEEKLY", "marketAsOf": {"TWSE": "2026-10-02", "TPEX": "2026-10-02"}, "sourceHash": "0" * 64,
                  "companyCount": 3, "files": {}}
        (self.out / "manifest.json").write_text(json.dumps(weekly), encoding="utf-8")
        r = self.run_at("2026-10-08T22:30:00", {"TWSE": "2026-10-07", "TPEX": "2026-10-08"})
        self.assertEqual(r.status, D.SUCCESS)
        self.assertEqual(self.calls[-1][1], ("TWSE", "TPEX"))
        self.assertEqual(self.manifest()["updatePolicy"], "DAILY_TRADING_DAY")

    def test_an_unreadable_official_calendar_falls_back_to_the_published_one_then_to_unknown(self):
        self.run_at("2026-10-08T06:30:00", {"TWSE": "2026-10-07", "TPEX": "2026-10-07"})
        r = self.run_at("2026-10-09T21:00:00", {"TWSE": "2026-10-08", "TPEX": "2026-10-08"}, rows=None)  # no official list: the LKG's calendar still knows Friday is closed
        self.assertIn(r.status, (D.SUCCESS, D.NO_TRADING_DAY))
        shutil.rmtree(self.out, ignore_errors=True)
        self.out.mkdir()
        r = self.run_at("2026-10-09T22:00:00", {"TWSE": "2026-10-08", "TPEX": "2026-10-08"}, rows=None)  # nothing to fall back to: UNKNOWN, never "everything trades"
        self.assertEqual(r.status, D.FAILED_VALIDATION)  # freshness cannot be determined without a calendar: fail safe, nothing is published
        self.assertIn("freshness-determinable", json.dumps(r.publication["gate"]))
        self.assertIsNone(self.manifest())


class CarryOver(unittest.TestCase):
    """The REAL ingest_markets: a market that owes nothing is copied from the Last Known Good's raw partition (no request); the other is fetched."""

    def test_only_the_owed_market_is_requested_and_the_carried_files_keep_their_original_provenance(self):
        store, root = build_store()                      # partition 2026-10-06 holds both markets, as the Last Known Good was built from it
        raw = root / "raw"
        requests = []

        def fake_fetch(ep, st, ctx):
            requests.append(ep.id)
            ctx.request_count += 1
            body = json.dumps([{"Date": "1151007"}]).encode()
            meta = RawMeta(ep.id, ep.url, ctx.run_date, "2026-10-08T22:00:00+08:00", 200, None, None, sha256_hex(body), len(body), 1, ctx.run_id)
            return st.write(meta, body, ep.market, ep.dataset)

        part = "2026-10-08"
        res, fallbacks = ingest_markets(part, raw, new_run(part), ("TPEX",), {"TWSE": "2026-10-06"}, fake_fetch)
        self.assertEqual(fallbacks, ())
        self.assertTrue(requests and all(r.startswith("tpex.") for r in requests), requests)   # TWSE: zero requests
        s = RawStore(raw)
        carried = s.get(part, "TWSE", "prices")
        self.assertEqual((carried.meta.reusedFrom, carried.meta.fetchedAt[:10]), ("2026-10-06", "2026-10-06"))   # original fetch time, honest lineage
        self.assertEqual(carried.path.read_bytes(), s.get("2026-10-06", "TWSE", "prices").path.read_bytes())
        self.assertEqual(s.get(part, "TPEX", "prices").meta.fetchedAt[:10], "2026-10-08")

    def test_isolated_dry_run_tpex_update_with_twse_carry_builds_identical_twse_companies(self):
        """PRODUCTION_CARRY_FORWARD_NOT_YET_OBSERVED for this direction (TPEx new, TWSE carried): real ingest + real load/build on an isolated raw store."""
        store, root = build_store()
        raw = root / "raw"
        before = build(load_inputs(store, "2026-10-06"), "t")

        def fake_fetch(ep, st, ctx):                       # the "new" TPEx files: same bytes, later fetch time (isolates the carry, not the exchange)
            old = st.get("2026-10-06", ep.market, ep.dataset)
            body = old.path.read_bytes() if old else b"[]"      # a dataset the fixture never had stays empty, as before
            ctx.request_count += 1
            meta = RawMeta(ep.id, ep.url, ctx.run_date, "2026-10-08T22:00:00+08:00", 200, None, None, sha256_hex(body), len(body), 1, ctx.run_id)
            return st.write(meta, body, ep.market, ep.dataset)

        res, fallbacks = ingest_markets("2026-10-08", raw, new_run("2026-10-08"), ("TPEX",), {"TWSE": "2026-10-06"}, fake_fetch)
        self.assertEqual(fallbacks, ())
        after = build(load_inputs(RawStore(raw), "2026-10-08"), "t")
        def twse(snap):
            return sorted(json.dumps(c.to_dict() if hasattr(c, "to_dict") else c, sort_keys=True, default=str) for c in snap.companies
                          if (c["market"] if isinstance(c, dict) else c.market) == "TWSE")
        self.assertEqual(twse(before), twse(after))                       # carried market: same companies, same numbers, same count
        self.assertEqual(before.market_as_of["TWSE"], after.market_as_of["TWSE"])

    def test_a_missing_carry_partition_is_a_fallback_fetch_never_a_guess(self):
        store, root = build_store()
        requests = []

        def fake_fetch(ep, st, ctx):
            requests.append(ep.market)
            ctx.request_count += 1
            body = b"[]"
            meta = RawMeta(ep.id, ep.url, ctx.run_date, "2026-10-08T22:00:00+08:00", 200, None, None, sha256_hex(body), 2, 0, ctx.run_id)
            return st.write(meta, body, ep.market, ep.dataset)

        res, fallbacks = ingest_markets("2026-10-08", root / "raw", new_run("2026-10-08"), ("TPEX",), {"TWSE": "2026-09-01"}, fake_fetch)  # that partition does not exist
        self.assertEqual(fallbacks, ("TWSE",))
        self.assertIn("TWSE", requests)

    def test_a_carried_market_must_be_complete_never_a_mixture(self):
        store, root = build_store()
        (root / "raw" / "2026-10-06" / "twse" / "ratios.json").unlink()   # one dataset of the carried partition is gone
        res, fallbacks = ingest_markets("2026-10-08", root / "raw", new_run("2026-10-08"), ("TPEX",), {"TWSE": "2026-10-06"},
                                        _write)
        self.assertEqual(fallbacks, ("TWSE",))


def _write(ep, st, ctx):
    body = b"[]"
    meta = RawMeta(ep.id, ep.url, ctx.run_date, "2026-10-08T22:00:00+08:00", 200, None, None, sha256_hex(body), 2, 0, ctx.run_id)
    return st.write(meta, body, ep.market, ep.dataset)


if __name__ == "__main__":
    unittest.main()
