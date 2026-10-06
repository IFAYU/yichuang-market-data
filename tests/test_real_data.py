"""Checks against the REAL raw snapshots fetched on 2026-10-06 (skipped if they are not on disk, e.g. on a fresh clone)."""
import unittest

from mitw.config import RAW_DIR
from mitw.pipeline import SERIES_CALC, SERIES_OFFICIAL, build, load_inputs
from mitw.raw.store import RawStore
from mitw.snapshot.build import dumps, make_documents, strip_runtime
from mitw.snapshot.validate import validate_documents

DATE = "2026-10-06"
HAVE = RawStore(RAW_DIR).get(DATE, "TPEX", "form_ins") is not None


@unittest.skipUnless(HAVE, "real raw snapshots for 2026-10-06 are not present")
class RealDataTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.store = RawStore(RAW_DIR)
        cls.inp = load_inputs(cls.store, DATE)
        cls.res = build(cls.inp, "t")

    def test_raw_files_still_match_their_recorded_hashes(self):
        for market in ("TWSE", "TPEX"):
            for ds in ("companies", "prices", "ratios", "industry_eps", "monthly_revenue"):
                self.assertIsNotNone(self.store.get(DATE, market, ds))  # get() re-hashes and raises on tampering

    def test_deterministic_rebuild_from_the_same_raw_data(self):
        a = make_documents(self.res, "run-A")
        b = make_documents(build(load_inputs(self.store, DATE), "run-B"), "run-B")
        self.assertEqual(dumps(strip_runtime(a[0])), dumps(strip_runtime(b[0])))
        self.assertEqual(dumps(strip_runtime(a[1])), dumps(strip_runtime(b[1])))

    def test_published_documents_pass_the_default_publication_policy(self):
        m, i = make_documents(self.res, "t")
        self.assertEqual(validate_documents(m, i, None), [])

    def test_basis_finding_is_evidence_based_and_confirmed(self):
        f = self.res.findings
        self.assertTrue(f["confirmed"])
        self.assertEqual(f["epsFeedBasis"], "CUMULATIVE_YTD")
        self.assertEqual(f["evidenceA"]["supports"], "CUMULATIVE_YTD")
        self.assertEqual(f["evidenceB"]["supports"], "CUMULATIVE_YTD")

    def test_statistics_only_use_strictly_positive_pe(self):
        for s in self.res.industry_snapshots:
            for x in s.includedCompanies:
                self.assertGreater(x["pe"], 0, (s.industryCode, x))
            if s.raw:
                self.assertLessEqual(s.raw.p25, s.raw.median)
                self.assertLessEqual(s.raw.median, s.raw.p75)
                self.assertLessEqual(s.robust.count, s.raw.count)

    def test_every_official_series_company_is_either_included_or_excluded_with_a_reason(self):
        total = 0
        for s in self.res.industry_snapshots:
            if s.metricSeries == SERIES_OFFICIAL and not s.subIndustryCode:
                total += s.companyCount
                self.assertEqual(len(s.includedCompanies) + len(s.excludedCompanies), s.companyCount)
                self.assertTrue(all(e["reason"] for e in s.excludedCompanies))
        self.assertEqual(total, len(self.res.companies))

    def test_no_company_was_dropped_for_missing_data(self):
        universe = {c.ticker for c in self.inp.companies}
        self.assertEqual({r["ticker"] for r in self.res.companies}, universe)


if __name__ == "__main__":
    unittest.main()
