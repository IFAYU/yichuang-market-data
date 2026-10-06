"""Publication gate, atomic releases, last-known-good, run records, the scheduled runner, and low-request ingestion."""
import copy
import json
import tempfile
import unittest
from dataclasses import replace
from datetime import date, datetime
from pathlib import Path
from unittest import mock

from mitw.config import MAX_ATTEMPTS_PER_DAY, OUT_DIR, RAW_DIR
from mitw.ingest import ingest_daily, list_partitions, next_partition
from mitw.pipeline import build, load_inputs
from mitw.providers.endpoints import ENDPOINTS
from mitw.providers.http import RunContext, SourceStopped, new_run
from mitw.raw.store import RawStore
from mitw.runlog import MAX_RUN_RECORDS, RunRecord, load_runs, record, summarize, write_health
from mitw.runner import publish_existing, publish_partition, run_once
from mitw.snapshot import build as snapshot_build
from mitw.snapshot.build import PublishRefused, publish, read_manifest, rollback
from mitw.snapshot.gate import evaluate_gate
from tests.helpers import RUN_DATE, build_store

NOW = datetime(2026, 10, 6, 22, 30)
PUB = "2026-10-06T22:30:00+08:00"
HAVE_OUT = (OUT_DIR / "manifest.json").exists()
HAVE_RAW = RawStore(RAW_DIR).get("2026-10-06", "TPEX", "form_ins") is not None


def real_docs():
    m = json.loads((OUT_DIR / "manifest.json").read_text(encoding="utf-8"))
    d = OUT_DIR / m["files"]["market-snapshot.json"]["path"]
    return json.loads(d.read_text(encoding="utf-8")), json.loads((d.parent / "industry-snapshots.json").read_text(encoding="utf-8")), m


def fresh_dir() -> Path:
    return Path(tempfile.mkdtemp(prefix="mitw_pub_"))


@unittest.skipUnless(HAVE_OUT, "needs a published snapshot in out/")
class GateTests(unittest.TestCase):
    def setUp(self):
        self.market, self.industry, self.manifest = real_docs()  # a real snapshot whose exchanges are on different dates

    def names(self, g):
        return {c["name"]: c for c in g.checks}

    def test_mixed_exchange_dates_are_refused_and_both_dates_are_named(self):
        g = evaluate_gate(self.market, self.industry, None, NOW)
        self.assertFalse(g.passed)
        self.assertFalse(self.names(g)["market-date-same"]["ok"])
        self.assertIn("2026-10-05", self.names(g)["market-date-same"]["detail"])
        self.assertIn("2026-10-06", self.names(g)["market-date-same"]["detail"])

    def test_the_mixed_date_exception_must_be_explicit_and_is_recorded(self):
        g = evaluate_gate(self.market, self.industry, None, NOW, allow_mixed_dates=True)
        self.assertTrue(g.passed)
        self.assertEqual(g.exceptions[0]["code"], "MIXED_MARKET_DATES")
        self.assertIn("EXCEPTION", self.names(g)["market-date-same"]["detail"])

    def test_same_date_on_both_exchanges_passes_without_any_exception(self):
        m = copy.deepcopy(self.market)
        m["marketAsOf"] = {"TWSE": "2026-10-06", "TPEX": "2026-10-06"}
        g = evaluate_gate(m, self.industry, None, NOW)
        self.assertTrue(g.passed, g.failures)
        self.assertEqual(g.exceptions, [])

    def test_a_publication_can_never_move_backwards_in_time(self):
        m = copy.deepcopy(self.market)
        m["marketAsOf"] = {"TWSE": "2026-10-02", "TPEX": "2026-10-02"}
        prev = {"marketAsOf": {"TWSE": "2026-10-05", "TPEX": "2026-10-05"}}
        g = evaluate_gate(m, self.industry, prev, NOW)
        self.assertFalse(self.names(g)["not-older-than-latest"]["ok"])
        self.assertIn("backwards", self.names(g)["not-older-than-latest"]["detail"])

    def test_missing_or_unparseable_market_date_is_refused(self):
        for bad in ({"TWSE": None, "TPEX": "2026-10-06"}, {"TWSE": "abc", "TPEX": "2026-10-06"}, {}):
            m = copy.deepcopy(self.market)
            m["marketAsOf"] = bad
            g = evaluate_gate(m, self.industry, None, NOW, allow_mixed_dates=True)
            self.assertFalse(g.passed, bad)
            self.assertFalse(self.names(g)["market-date-present"]["ok"])

    def test_a_future_date_means_freshness_cannot_be_determined(self):
        m = copy.deepcopy(self.market)
        m["marketAsOf"] = {"TWSE": "2026-12-01", "TPEX": "2026-12-01"}
        g = evaluate_gate(m, self.industry, None, NOW)
        self.assertFalse(self.names(g)["freshness-determinable"]["ok"])

    def test_missing_outlier_methodology_is_refused(self):
        ind = copy.deepcopy(self.industry)
        for s in ind["snapshots"]:
            s["methodology"].pop("outlierPolicy", None)
        g = evaluate_gate(self.market, ind, None, NOW, allow_mixed_dates=True)
        self.assertFalse(self.names(g)["methodology"]["ok"])

    def test_the_official_pe_basis_must_stay_the_exchanges_definition(self):
        m = copy.deepcopy(self.market)
        m["basisFinding"]["officialPEBasis"] = "INFERRED_TTM"
        self.assertFalse(self.names(evaluate_gate(m, self.industry, None, NOW, allow_mixed_dates=True))["methodology"]["ok"])

    def test_wrong_schema_major_is_refused(self):
        m = copy.deepcopy(self.market)
        m["schemaVersion"] = "2.0.0"
        self.assertFalse(self.names(evaluate_gate(m, self.industry, None, NOW, allow_mixed_dates=True))["schema"]["ok"])

    def test_thin_coverage_is_refused(self):
        m = copy.deepcopy(self.market)
        m["companies"] = m["companies"][:200]
        self.assertFalse(self.names(evaluate_gate(m, self.industry, None, NOW, allow_mixed_dates=True))["coverage"]["ok"])


@unittest.skipUnless(HAVE_RAW, "needs the 2026-10-06 raw partition")
class AtomicPublicationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.base = build(load_inputs(RawStore(RAW_DIR), "2026-10-06"), "t0")

    def same_day(self, d="2026-10-06", tweak=None):
        r = replace(self.base, market_as_of={"TWSE": d, "TPEX": d}, warnings=self.base.warnings + ([tweak] if tweak else []))
        return r

    def test_release_is_written_and_verified_before_the_manifest_points_at_it(self):
        out = fresh_dir()
        manifest, outcome = publish(self.same_day(), out, "g", PUB, NOW)
        self.assertEqual(outcome, "PUBLISHED")
        self.assertEqual(manifest["release"], manifest["sourceHash"][:12])
        for n, rec in manifest["files"].items():
            self.assertEqual(rec["path"], f"releases/{manifest['release']}/{n}")
            self.assertTrue((out / rec["path"]).exists())
        self.assertTrue((out / "releases" / manifest["release"] / "release.json").exists())
        self.assertEqual(read_manifest(out)["sourceHash"], manifest["sourceHash"])
        for k in ("sourceFetchedAt", "publishedAt", "generatedAt", "marketAsOf", "financialAsOf", "schemaVersion", "sourceHash", "freshnessAtPublish", "gate"):
            self.assertIn(k, manifest)
        self.assertEqual(manifest["freshnessAtPublish"]["status"], "FRESH")

    def test_the_same_data_published_twice_is_unchanged(self):
        out = fresh_dir()
        first, _ = publish(self.same_day(), out, "g1", "p1", NOW)
        before = (out / "manifest.json").read_bytes()
        second, outcome = publish(self.same_day(), out, "g2", "p2", NOW)
        self.assertEqual(outcome, "UNCHANGED")
        self.assertEqual((out / "manifest.json").read_bytes(), before)

    def test_a_failure_while_replacing_the_manifest_leaves_the_last_known_good_untouched(self):
        out = fresh_dir()
        good, _ = publish(self.same_day("2026-10-06"), out, "g", PUB, NOW)
        before = (out / "manifest.json").read_bytes()
        real = snapshot_build.write_atomic

        def boom(path, data):
            if Path(path).name == "manifest.json":
                raise OSError("disk full while replacing the manifest")
            return real(path, data)

        newer = datetime(2026, 10, 7, 22, 30)
        with mock.patch.object(snapshot_build, "write_atomic", boom):
            with self.assertRaises(OSError):
                publish(self.same_day("2026-10-07", tweak="new"), out, "g", PUB, newer)
        self.assertEqual((out / "manifest.json").read_bytes(), before)  # the pointer never moved
        for n, rec in good["files"].items():  # and the release it points at is intact
            self.assertTrue((out / rec["path"]).exists())

    def test_a_gate_refusal_leaves_the_last_known_good_untouched(self):
        out = fresh_dir()
        publish(self.same_day("2026-10-06"), out, "g", PUB, NOW)
        before = (out / "manifest.json").read_bytes()
        with self.assertRaises(PublishRefused) as cm:
            publish(self.same_day("2026-10-02", tweak="older"), out, "g", PUB, NOW)  # older than what is published
        self.assertTrue(any("backwards" in p for p in cm.exception.problems))
        self.assertEqual((out / "manifest.json").read_bytes(), before)

    def test_mixed_dates_are_never_published_as_latest_without_the_explicit_exception(self):
        out = fresh_dir()
        with self.assertRaises(PublishRefused):
            publish(self.base, out, "g", PUB, NOW)  # real data: TWSE 10-05 vs TPEx 10-06
        self.assertFalse((out / "manifest.json").exists())

    def test_rollback_repoints_the_manifest_after_verifying_the_old_release(self):
        out = fresh_dir()
        old, _ = publish(self.same_day("2026-10-06"), out, "g", PUB, NOW)
        new, _ = publish(self.same_day("2026-10-07", tweak="newer"), out, "g", PUB, datetime(2026, 10, 7, 22, 30))
        self.assertEqual(read_manifest(out)["release"], new["release"])
        rb = rollback(out, old["release"], "2026-10-08T09:00:00")
        self.assertEqual(read_manifest(out)["release"], old["release"])
        self.assertEqual(rb["marketAsOf"], old["marketAsOf"])
        # a damaged release cannot be rolled back to
        victim = out / "releases" / new["release"] / "market-snapshot.json"
        victim.write_bytes(victim.read_bytes() + b" ")
        with self.assertRaises(PublishRefused):
            rollback(out, new["release"], "2026-10-08T09:05:00")
        self.assertEqual(read_manifest(out)["release"], old["release"])


class RunRecordTests(unittest.TestCase):
    def rec(self, i, status, **kw):
        return RunRecord(runId=f"r{i:03d}", startedAt=f"2026-10-{(i % 28) + 1:02d}T22:00:00", finishedAt="x", runDate="2026-10-06", status=status, **kw)

    def test_summary_answers_last_success_last_failure_and_consecutive_failures(self):
        runs = [self.rec(1, "SUCCESS").to_json(), self.rec(2, "FAILED", failureReason="HTTP 429").to_json(), self.rec(3, "REFUSED").to_json(),
                self.rec(4, "WAITING").to_json(), self.rec(5, "FAILED", failureReason="timeout").to_json()]
        s = summarize(runs)
        self.assertEqual(s["lastSuccess"]["runId"], "r001")
        self.assertEqual(s["lastFailure"]["runId"], "r005")
        self.assertEqual(s["lastFailure"]["failureReason"], "timeout")
        self.assertEqual(s["consecutiveFailures"], 3)  # WAITING neither resets nor extends the streak
        self.assertEqual(summarize(runs + [self.rec(6, "SUCCESS").to_json()])["consecutiveFailures"], 0)
        self.assertEqual(summarize(runs + [self.rec(6, "NOOP").to_json()])["consecutiveFailures"], 3)

    def test_only_the_last_30_records_are_kept(self):
        d = fresh_dir()
        for i in range(40):
            record(self.rec(i, "SUCCESS"), d)
        names = sorted(p.name for p in d.glob("*.json"))
        self.assertEqual(len(names), MAX_RUN_RECORDS)
        self.assertEqual(names[0], "r010.json")
        self.assertEqual(names[-1], "r039.json")

    def test_a_corrupt_record_does_not_break_reporting(self):
        d = fresh_dir()
        record(self.rec(1, "SUCCESS"), d)
        (d / "garbage.json").write_text("{nope", encoding="utf-8")
        self.assertEqual(len(load_runs(d)), 1)

    def test_health_is_rebuilt_after_every_run(self):
        runs, out = fresh_dir(), fresh_dir()
        record(self.rec(1, "FAILED", failureReason="HTTP 403"), runs)
        h = write_health("2026-10-06T22:30:00", runs, out)
        self.assertEqual(json.loads((out / "health.json").read_text(encoding="utf-8")), h)
        self.assertEqual(h["consecutiveFailures"], 1)
        self.assertIsNone(h["lastSuccess"])


class RunnerTests(unittest.TestCase):
    def dirs(self):
        return fresh_dir(), fresh_dir(), fresh_dir()  # out, runs, raw

    def manifest(self, out, twse, tpex, published="2026-10-11T10:12:00"):
        (out / "manifest.json").write_text(json.dumps({"release": "x", "marketAsOf": {"TWSE": twse, "TPEX": tpex}, "updatePolicy": "WEEKLY",
                                                       "lastSuccessfulPublication": published, "publishedAt": published}), encoding="utf-8")

    def never(self, *a, **k):
        raise AssertionError("the network must not be touched")

    def test_this_weeks_publication_already_done_is_a_noop_with_zero_requests(self):
        out, runs, raw = self.dirs()
        self.manifest(out, "2026-10-09", "2026-10-09")
        r = run_once(now_fn=lambda: datetime(2026, 10, 11, 14, 0), raw_dir=raw, out_dir=out, runs_dir=runs, fetch=self.never, db_path=None)
        self.assertEqual((r.status, r.requests), ("NOOP", 0))  # the 14:00 retry after a 10:12 success
        self.assertEqual(r.publication["result"], "WEEK_ALREADY_PUBLISHED")
        self.assertEqual(json.loads((out / "health.json").read_text(encoding="utf-8"))["runs"][-1]["status"], "NOOP")

    def test_midweek_runs_need_no_new_data_either(self):
        out, runs, raw = self.dirs()
        self.manifest(out, "2026-10-09", "2026-10-09")
        for now in (datetime(2026, 10, 12, 9, 0), datetime(2026, 10, 16, 22, 30), datetime(2026, 10, 17, 22, 30), datetime(2026, 10, 18, 9, 59)):
            r = run_once(now_fn=lambda n=now: n, raw_dir=raw, out_dir=out, runs_dir=runs, fetch=self.never, db_path=None)
            self.assertEqual(r.status, "NOOP", now)

    def test_a_source_failure_is_recorded_and_leaves_latest_untouched(self):
        out, runs, raw = self.dirs()
        self.manifest(out, "2026-10-02", "2026-10-02", published="2026-10-04T10:12:00")
        before = (out / "manifest.json").read_bytes()

        def blocked(ep, store, ctx, **k):
            raise SourceStopped(ep.id, "HTTP 429")

        for i in range(2):
            r = run_once(now_fn=lambda i=i: datetime(2026, 10, 11, 10, 0, i), raw_dir=raw, out_dir=out, runs_dir=runs, fetch=blocked, db_path=None)
            self.assertEqual(r.status, "FAILED")
        self.assertEqual((out / "manifest.json").read_bytes(), before)
        h = json.loads((out / "health.json").read_text(encoding="utf-8"))
        self.assertEqual(h["consecutiveFailures"], 2)
        self.assertIn("429", h["lastFailure"]["failureReason"])
        self.assertIsNone(h["lastSuccess"])

    def test_the_retry_limit_of_the_weekly_slot_stops_polling(self):
        out, runs, raw = self.dirs()
        self.manifest(out, "2026-10-02", "2026-10-02", published="2026-10-04T10:12:00")
        for i, hhmm in enumerate(("10:00", "14:00", "20:00")):  # the three Sunday attempts of 2026-10-11
            record(RunRecord(runId=f"a{i}", startedAt=f"2026-10-11T{hhmm}:00+08:00", runDate="2026-10-11", status="FAILED"), runs)
        r = run_once(trigger="schedule", now_fn=lambda: datetime(2026, 10, 11, 21, 0), raw_dir=raw, out_dir=out, runs_dir=runs, fetch=self.never, db_path=None)
        self.assertEqual((r.status, r.requests), ("SKIPPED", 0))
        # a person pressing "run" (workflow_dispatch) is not the retry loop: it is never refused by the weekly cap
        m = run_once(trigger="manual", now_fn=lambda: datetime(2026, 10, 11, 21, 5), raw_dir=raw, out_dir=out, runs_dir=runs, fetch=self.never, db_path=None)
        self.assertNotEqual(m.status, "SKIPPED")

    def test_attempts_of_an_earlier_week_do_not_count(self):
        out, runs, raw = self.dirs()
        self.manifest(out, "2026-10-02", "2026-10-02", published="2026-10-04T10:12:00")
        for i in range(3):
            record(RunRecord(runId=f"old{i}", startedAt=f"2026-10-04T1{i}:00:00+08:00", runDate="2026-10-04", status="FAILED"), runs)
        calls = []

        def blocked(ep, store, ctx, **k):
            calls.append(ep.id)
            raise SourceStopped(ep.id, "HTTP 429")

        r = run_once(now_fn=lambda: datetime(2026, 10, 11, 10, 0), raw_dir=raw, out_dir=out, runs_dir=runs, fetch=blocked, db_path=None)
        self.assertEqual(r.status, "FAILED")
        self.assertTrue(calls)

    def test_partitions_do_not_overwrite_earlier_attempts(self):
        raw = fresh_dir()
        self.assertEqual(next_partition(raw, "2026-10-06"), "2026-10-06")
        (raw / "2026-10-06").mkdir()
        self.assertEqual(next_partition(raw, "2026-10-06"), "2026-10-06.2")
        (raw / "2026-10-06.2").mkdir()
        self.assertEqual(next_partition(raw, "2026-10-06"), "2026-10-06.3")
        self.assertEqual(list_partitions(raw, "2026-10-06"), ["2026-10-06", "2026-10-06.2"])

    @unittest.skipUnless(HAVE_RAW, "needs the 2026-10-06 raw partition")
    def test_one_exchange_ahead_of_the_other_means_waiting_not_publishing(self):
        out, runs, _ = self.dirs()
        r = publish_existing("2026-10-06", now_fn=lambda: NOW, raw_dir=RAW_DIR, out_dir=out, runs_dir=runs, db_path=None)
        self.assertEqual(r.status, "WAITING")
        self.assertIn("different trade dates", r.failureReason)
        self.assertFalse((out / "manifest.json").exists())

    @unittest.skipUnless(HAVE_RAW, "needs the 2026-10-06 raw partition")
    def test_a_sunday_run_that_fetched_old_data_is_not_a_publication(self):
        """The schedule ran, the pipeline worked, but the market date did not move: that is NO_NEW_MARKET_DATA, never FRESH."""
        out, runs, _ = self.dirs()
        self.manifest(out, "2026-10-06", "2026-10-06", published="2026-10-04T10:12:00")
        before = (out / "manifest.json").read_bytes()
        early = publish_partition("2026-10-06", RunRecord(runId="e", startedAt="x"), datetime(2026, 10, 11, 10, 5), RAW_DIR, out, db_path=None,
                                  allow_mixed_dates=True, final_attempt=False)
        self.assertEqual(early.status, "WAITING")  # first attempts: wait for the next retry
        self.assertIn("NO_NEW_MARKET_DATA", early.failureReason)
        last = publish_partition("2026-10-06", RunRecord(runId="l", startedAt="x"), datetime(2026, 10, 11, 20, 5), RAW_DIR, out, db_path=None,
                                 allow_mixed_dates=True, final_attempt=True)
        self.assertEqual(last.status, "FAILED")  # the last attempt of the week: this week's publication has failed
        self.assertEqual(last.publication["result"], "NO_NEW_MARKET_DATA")
        self.assertEqual((out / "manifest.json").read_bytes(), before)  # Last Known Good untouched

    @unittest.skipUnless(HAVE_RAW, "needs the 2026-10-06 raw partition")
    def test_the_explicit_exception_publishes_and_a_repeat_changes_nothing(self):
        out, runs, _ = self.dirs()
        a = publish_existing("2026-10-06", now_fn=lambda: NOW, raw_dir=RAW_DIR, out_dir=out, runs_dir=runs, allow_mixed_dates=True, db_path=None)
        self.assertEqual(a.status, "SUCCESS")
        m = read_manifest(out)
        self.assertEqual(m["gate"]["exceptions"][0]["code"], "MIXED_MARKET_DATES")
        again = publish_existing("2026-10-06", now_fn=lambda: datetime(2026, 10, 6, 22, 31), raw_dir=RAW_DIR, out_dir=out, runs_dir=runs, allow_mixed_dates=True, db_path=None)
        self.assertEqual(again.status, "WAITING")  # same market date as the release it would replace: NO_NEW_MARKET_DATA
        b = publish_existing("2026-10-06", now_fn=lambda: datetime(2026, 10, 6, 22, 32), raw_dir=RAW_DIR, out_dir=out, runs_dir=runs, allow_mixed_dates=True,
                             allow_republish=True, db_path=None)
        self.assertEqual(b.status, "NOOP")  # even when re-publication is explicitly allowed, identical data creates no new release
        self.assertEqual(read_manifest(out)["release"], m["release"])
        h = json.loads((out / "health.json").read_text(encoding="utf-8"))
        self.assertEqual(h["lastSuccess"]["release"], m["release"])


class LowRequestIngestionTests(unittest.TestCase):
    def fake_fetch(self, calls):
        def fetch(ep, store, ctx, **k):
            calls.append(ep.id)
            prior = store.latest_before(ctx.run_date, ep.market, ep.dataset)
            if prior is None:
                raise SourceStopped(ep.id, "no fixture")
            ctx.request_count += 1
            meta = replace(prior.meta, runDate=ctx.run_date, fetchedAt=f"{ctx.run_date[:10]}T19:00:00+08:00", reusedFrom=None)
            return store.write(meta, prior.path.read_bytes(), ep.market, ep.dataset)
        return fetch

    def test_only_prices_and_official_ratios_are_fetched_for_a_new_trading_day(self):
        store, root = build_store()  # partition 2026-10-06 with every dataset except swagger
        calls: list[str] = []
        ctx = new_run("2026-10-07")
        res = ingest_daily("2026-10-07", root / "raw", ctx, self.fake_fetch(calls))
        fetched_datasets = {ENDPOINTS[c].dataset for c in calls}
        self.assertEqual(fetched_datasets, {"prices", "ratios", "swagger"})  # swagger has no earlier copy in this fixture
        self.assertEqual(sorted(c for c in calls if ENDPOINTS[c].dataset != "swagger"), ["tpex.prices", "tpex.ratios", "twse.prices", "twse.ratios"])
        self.assertEqual(ctx.reused_recent, 14)  # companies, EPS table, monthly revenue and 4 statement-form lists, per exchange
        self.assertEqual(ctx.request_count, 4)  # the two swagger attempts failed before any request in this fixture

    def test_reuse_keeps_the_original_fetch_time_so_provenance_stays_honest(self):
        store, root = build_store()
        ingest_daily("2026-10-07", root / "raw", new_run("2026-10-07"), self.fake_fetch([]))
        ref = RawStore(root / "raw").get("2026-10-07", "TWSE", "companies")
        self.assertEqual(ref.meta.reusedFrom, "2026-10-06")
        self.assertTrue(ref.meta.fetchedAt.startswith("2026-10-06"))

    def test_datasets_older_than_their_window_are_fetched_again(self):
        store, root = build_store()
        calls: list[str] = []
        ctx = new_run("2026-12-01")  # companies window 7d, statement forms 30d: everything is too old
        ingest_daily("2026-12-01", root / "raw", ctx, self.fake_fetch(calls))
        self.assertEqual(ctx.reused_recent, 0)
        self.assertGreaterEqual(len(calls), 18)


if __name__ == "__main__":
    unittest.main()
