import json
import sqlite3
import statistics
import tempfile
import unittest
from pathlib import Path

from mitw.contracts.version import SNAPSHOT_SCHEMA_VERSION
from mitw.pipeline import SERIES_CALC, SERIES_OFFICIAL, build, load_inputs
from mitw.raw.store import RawStore
from mitw.snapshot.build import dumps, make_documents, strip_runtime
from mitw.snapshot.validate import PublishPolicy, validate_documents
from mitw.store.sqlite import connect, persist_build
from tests.helpers import RUN_DATE, build_store, write_raw


class FixtureBuildTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.store, cls.root = build_store()
        cls.inp = load_inputs(cls.store, RUN_DATE)
        cls.res = build(cls.inp, "t0")
        cls.by = {r["ticker"]: r for r in cls.res.companies}
        cls.snaps = {(s.metricSeries, s.industryCode, s.subIndustryCode): s for s in cls.res.industry_snapshots}

    # ---- known fixture numbers ------------------------------------------------------------------------------
    def test_eps_basis_is_confirmed_from_evidence_not_assumed(self):
        # fixture has too few companies for the evidence rule (needs >= 30): the pipeline must NOT silently accept a basis
        self.assertFalse(self.res.findings["confirmed"])
        self.assertEqual(self.by["2330"]["eps"]["basis"], "UNKNOWN_BASIS")
        self.assertEqual(self.by["2330"]["calculatedPE"]["status"], "UNKNOWN_BASIS")
        self.assertIsNone(self.by["2330"]["calculatedPE"]["value"])

    def test_known_values_once_basis_is_confirmed(self):
        from mitw import pipeline
        orig = pipeline.find_basis
        pipeline.find_basis = lambda rows, q, m: {"epsFeedBasis": "CUMULATIVE_YTD", "officialPEBasis": "OFFICIAL_TTM", "confirmed": True, "reasoning": ["test"]}
        try:
            res = build(self.inp, "t0")
        finally:
            pipeline.find_basis = orig
        by = {r["ticker"]: r for r in res.companies}
        c = by["2330"]
        self.assertEqual(c["price"]["close"], 100.0)
        self.assertEqual(c["marketCap"]["value"], 1e11)  # 100 x 1,000,000,000 shares
        self.assertEqual(c["calculatedPE"]["value"], 10.0)  # 100 / (5 x 4/2)
        self.assertEqual(c["officialPE"]["value"], 11.0)
        self.assertEqual(c["peReconciliation"]["diffPct"], -9.09)
        self.assertEqual(c["peReconciliation"]["class"], "MINOR_DIFFERENCE")
        self.assertAlmostEqual(c["impliedEpsFromOfficialPE"], 9.0909, places=3)
        # P/S: market cap 1e11 / (1,000,000 thousand TWD x 1000 x 4/2 = 2e9) = 50
        self.assertEqual(c["ps"]["value"], 50.0)
        self.assertEqual(c["netMarginYtdPct"], 40.0)
        self.assertEqual(c["revenueGrowthYtdYoyPct"]["value"], 33.33)

    # ---- negative / zero / missing --------------------------------------------------------------------------
    def test_negative_eps_never_becomes_a_pe(self):
        for t in ("6770", "2002"):
            self.assertEqual(self.by[t]["calculatedPE"]["reason"], "NEGATIVE_EPS")
            self.assertIsNone(self.by[t]["calculatedPE"]["value"])
        s = self.snaps[(SERIES_OFFICIAL, "24", None)]
        reasons = {e["ticker"]: e["reason"] for e in s.excludedCompanies}
        self.assertEqual(reasons["6770"], "NEGATIVE_EPS")
        self.assertNotIn("6770", [x["ticker"] for x in s.includedCompanies])
        for x in s.includedCompanies:
            self.assertGreater(x["pe"], 0)

    def test_positive_official_pe_is_kept_even_if_ytd_eps_is_negative(self):
        # 8027: YTD EPS -0.46 but the exchange publishes a positive trailing P/E (698.33): it is the exchange's number, kept with its flags
        self.assertEqual(self.by["8027"]["officialPE"]["value"], 698.33)
        self.assertEqual(self.by["8027"]["calculatedPE"]["reason"], "NEGATIVE_EPS")

    def test_blank_official_pe_with_positive_ytd_eps_is_invalid_pe(self):
        s = self.snaps[(SERIES_OFFICIAL, "01", None)]
        self.assertEqual({e["ticker"]: e["reason"] for e in s.excludedCompanies}["1101"], "INVALID_PE")

    def test_missing_data_is_reported_never_dropped_silently(self):
        c = self.by["9999"]  # no price, no EPS, no ratio row
        self.assertIsNone(c["price"]["close"])
        self.assertEqual(c["marketCap"]["status"], "INSUFFICIENT_DATA")
        self.assertEqual(c["calculatedPE"]["status"], "INSUFFICIENT_DATA")
        s = self.snaps[(SERIES_OFFICIAL, "20", None)]
        self.assertEqual(s.companyCount, 1)
        self.assertEqual(s.excludedCompanies[0]["reason"], "NO_OFFICIAL_RATIO_ROW")
        self.assertEqual(len(self.res.companies), 13)  # nobody was dropped: 8 TWSE + 5 TPEx
        self.assertEqual(self.res.coverage["ALL"]["companies"], 13)
        self.assertEqual(self.res.coverage["ALL"]["missing"]["withPrice"], 2)  # 9101 (0.00) and 9999 (---)

    def test_tpex_rows_for_non_companies_are_ignored(self):
        self.assertNotIn("0001W", self.by)

    # ---- financial industry ---------------------------------------------------------------------------------
    def test_financial_companies_are_identified_from_the_exchange_statement_forms(self):
        self.assertEqual(self.by["2882"]["financialForm"], "FH")
        self.assertEqual(self.by["6023"]["financialForm"], "BD")
        self.assertEqual(self.by["2882"]["specialIndustry"], "FINANCIAL")
        self.assertIsNone(self.by["2330"]["specialIndustry"])
        self.assertEqual(self.by["2882"]["ps"]["reason"], "FINANCIAL_REVENUE_NOT_COMPARABLE")
        self.assertIsNone(self.by["2882"]["netMarginYtdPct"])  # revenue/operating income are not comparable for financials

    def test_financial_snapshot_is_flagged_and_warned(self):
        s = self.snaps[(SERIES_OFFICIAL, "17", None)]
        self.assertEqual(s.specialIndustry, "FINANCIAL")
        self.assertTrue(any("不適用" in w for w in s.warnings))
        sub = self.snaps[(SERIES_OFFICIAL, "17", "FIN_FH")]
        self.assertEqual([c["ticker"] for c in sub.includedCompanies], ["2882"])

    # ---- industry distribution ------------------------------------------------------------------------------
    def test_industry_stats_match_an_independent_calculation(self):
        s = self.snaps[(SERIES_OFFICIAL, "24", None)]
        pes = sorted(x["pe"] for x in s.includedCompanies)
        self.assertEqual(pes, [9.5, 11.0, 35.0, 58.5])
        q = statistics.quantiles(pes, n=4, method="inclusive")
        self.assertAlmostEqual(s.raw.p25, q[0], places=3)
        self.assertAlmostEqual(s.raw.median, statistics.median(pes), places=3)
        self.assertAlmostEqual(s.raw.p75, q[2], places=3)
        self.assertAlmostEqual(s.raw.mean, statistics.mean(pes), places=3)
        self.assertEqual((s.companyCount, s.validCount, s.excludedCount), (5, 4, 1))
        self.assertEqual(s.companyCount, s.validCount + s.excludedCount)

    def test_raw_and_robust_are_both_published_and_small_groups_are_not_trimmed(self):
        s = self.snaps[(SERIES_OFFICIAL, "24", None)]
        self.assertIsNotNone(s.raw)
        self.assertIsNotNone(s.robust)
        self.assertFalse(s.methodology["robustFences"]["applied"])
        self.assertEqual(s.robustExcluded, [])
        self.assertEqual(s.methodology["percentile"], "PERCENTILE_INC_LINEAR_TYPE7")

    def test_groups_below_the_minimum_sample_publish_no_statistics(self):
        s = self.snaps[(SERIES_OFFICIAL, "10", None)]  # steel: its only company has no positive P/E
        self.assertIsNone(s.raw)
        self.assertTrue(any("no statistics" in w for w in s.warnings))

    def test_depositary_receipts_without_pe_are_excluded_not_zero(self):
        s = self.snaps[(SERIES_OFFICIAL, "91", None)]
        self.assertEqual(s.validCount, 0)

    # ---- L1 -> L2 -------------------------------------------------------------------------------------------
    def test_semiconductor_l2_poc_assigns_only_companies_really_in_l1_24(self):
        self.assertEqual(self.by["2330"]["subIndustry"]["code"], "SEMI_FOUNDRY")
        self.assertEqual(self.by["2454"]["subIndustry"]["code"], "SEMI_IC_DESIGN")
        self.assertEqual(self.by["6488"]["subIndustry"]["code"], "SEMI_OTHER")
        self.assertIsNone(self.by["1101"]["subIndustry"])  # L1 only: no L2 assigned outside the proof of concept
        self.assertEqual(self.by["2330"]["subIndustry"]["assignedBy"], "POC_ASSISTANT_UNREVIEWED")
        foundry = self.snaps[(SERIES_OFFICIAL, "24", "SEMI_FOUNDRY")]
        self.assertTrue(any("概念驗證" in w for w in foundry.warnings))
        self.assertEqual(sorted(x["ticker"] for x in foundry.includedCompanies + [{"ticker": e["ticker"]} for e in foundry.excludedCompanies]), ["2303", "2330", "6770"])

    def test_poc_ticker_in_a_different_official_industry_is_skipped_not_forced(self):
        def relabel(recs):
            for r in recs:
                if r["公司代號"] == "2330":
                    r["產業別"] = "10"  # now an official steel company
        store, _ = build_store(patch_twse_companies=relabel)
        res = build(load_inputs(store, RUN_DATE), "t0")
        by = {r["ticker"]: r for r in res.companies}
        self.assertIsNone(by["2330"]["subIndustry"])
        self.assertTrue(any("2330" in w and "not forced" in w for w in res.warnings))

    # ---- pre-IPO comparable requirements -------------------------------------------------------------------
    def test_every_company_record_carries_the_comparable_fields(self):
        need = {"ticker", "name", "industry", "subIndustry", "price", "marketCap", "officialPE", "calculatedPE", "ps", "revenueGrowthYtdYoyPct",
                "netMarginYtdPct", "operatingMarginYtdPct", "eps", "specialIndustry"}
        for r in self.res.companies:
            self.assertTrue(need <= set(r), r["ticker"])
        c = self.by["2330"]
        self.assertEqual((c["industry"]["code"], c["industry"]["name"]), ("24", "半導體業"))

    # ---- lineage --------------------------------------------------------------------------------------------
    def test_every_derived_metric_says_where_it_came_from(self):
        for m in self.res.derived:
            self.assertTrue(m.formula, m.metric)
            self.assertTrue(m.dependencies, m.metric)
            self.assertTrue(m.basis, m.metric)
            self.assertTrue(m.asOfDate, m.metric)
            self.assertTrue(m.sourceVersion, m.metric)


class DeterminismAndPublishTests(unittest.TestCase):
    def test_same_raw_data_gives_identical_snapshots(self):
        store, _ = build_store()
        a = make_documents(build(load_inputs(store, RUN_DATE), "2026-10-06T10:00:00"), "2026-10-06T10:00:00")
        b = make_documents(build(load_inputs(store, RUN_DATE), "2026-10-07T23:59:59"), "2026-10-07T23:59:59")
        self.assertEqual(dumps(strip_runtime(a[0])), dumps(strip_runtime(b[0])))
        self.assertEqual(dumps(strip_runtime(a[1])), dumps(strip_runtime(b[1])))
        self.assertNotEqual(dumps(a[0]), dumps(b[0]))  # only generatedAt differs

    def test_contract_header_and_schema_version(self):
        store, _ = build_store()
        m, i = make_documents(build(load_inputs(store, RUN_DATE), "t"), "t")
        for doc in (m, i):
            self.assertEqual(doc["schemaVersion"], SNAPSHOT_SCHEMA_VERSION)
            self.assertTrue({"generatedAt", "marketAsOf", "financialAsOf", "sourceVersions"} <= set(doc))
        self.assertIn("sha256", next(iter(m["sourceVersions"]["raw"].values())))
        self.assertEqual(m["marketAsOf"], {"TWSE": "2026-10-05", "TPEX": "2026-10-06"})

    def test_validation_catches_a_shrunken_market_against_the_previous_snapshot(self):
        store, _ = build_store()
        res = build(load_inputs(store, RUN_DATE), "t")
        m, i = make_documents(res, "t")
        lenient = PublishPolicy(min_companies=1, min_twse=1, min_tpex=1, min_price_coverage=0.1, min_official_pe_coverage=0.1)
        self.assertEqual(validate_documents(m, i, None, lenient), [])
        prev = json.loads(json.dumps(m))
        prev["companies"] = prev["companies"] * 3
        self.assertTrue(any("company count fell" in p for p in validate_documents(m, i, prev, lenient)))


class SqliteTests(unittest.TestCase):
    def test_persist_and_lineage_survive_the_round_trip(self):
        store, _ = build_store()
        inp = load_inputs(store, RUN_DATE)
        res = build(inp, "t")
        conn = connect(Path(":memory:"))
        counts = persist_build(conn, inp, res)
        persist_build(conn, inp, res)  # idempotent
        self.assertEqual(counts["companies"], 13)
        n = conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0]
        self.assertEqual(n, 13)
        row = conn.execute("SELECT formula, dependencies, basis, status FROM valuation_metrics WHERE ticker='2330' AND metric='pe_annualized_ytd'").fetchone()
        self.assertTrue(row[0])
        self.assertIn("eps_ytd", json.loads(row[1]))
        sources = conn.execute("SELECT COUNT(*) FROM data_sources").fetchone()[0]
        self.assertGreaterEqual(sources, 20)
        for table in ("ingestion_runs", "comparable_sets", "comparable_set_members", "forward_estimates", "snapshot_publications", "company_industries", "financial_periods"):
            conn.execute(f"SELECT COUNT(*) FROM {table}")  # exists


if __name__ == "__main__":
    unittest.main()
