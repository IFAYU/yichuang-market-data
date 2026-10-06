import statistics
import unittest

from mitw.contracts.models import ForwardEstimate, INSUFFICIENT_DATA, NOT_APPLICABLE, OK, UNKNOWN_BASIS
from mitw.derive.eps_basis import BasisRow, CUMULATIVE_YTD, SINGLE_QUARTER, find_basis
from mitw.derive.reconcile import MATCH, MATERIAL, MINOR, UNKNOWN, reconcile
from mitw.derive.valuation import implied_ttm_eps, market_cap, pe_annualized_ytd, ps_annualized_ytd
from mitw.industry.stats import describe, percentile_inc, robust_filter
from mitw.normalize.parse import parse_num, parse_price, parse_tw_date, parse_year_month
from mitw.providers.forward import NoForwardEstimates, UserSuppliedForwardEstimates
from mitw.validate.schema import check_payload
from mitw.providers.endpoints import Endpoint

V = "t"


class ParseTests(unittest.TestCase):
    def test_missing_markers_are_none_never_zero(self):
        for s in ("", "--", "---", "-", "N/A", None, "nan", " "):
            self.assertIsNone(parse_num(s), repr(s))
        self.assertEqual(parse_num("1,234.50"), 1234.5)
        self.assertEqual(parse_num("-0.46"), -0.46)
        self.assertEqual(parse_num("0"), 0.0)  # an explicit zero is a value

    def test_price_must_be_positive(self):
        self.assertIsNone(parse_price("0.00"))
        self.assertIsNone(parse_price("---"))
        self.assertEqual(parse_price("2575.00"), 2575.0)

    def test_dates(self):
        self.assertEqual(parse_tw_date("1151005"), "2026-10-05")
        self.assertEqual(parse_tw_date("115/10/05"), "2026-10-05")
        self.assertEqual(parse_tw_date("20260705"), "2026-07-05")
        self.assertIsNone(parse_tw_date("1151345"))
        self.assertIsNone(parse_tw_date(""))
        self.assertEqual(parse_year_month("11508"), "2026-08")
        self.assertEqual(parse_year_month("202609"), "2026-09")


class SchemaTests(unittest.TestCase):
    EP = Endpoint("x.y", "TWSE", "y", "https://e.test", ("a", ("b", "b_alias")), 2, False)

    def test_ok_alias_and_new_field_info(self):
        r = check_payload(self.EP, [{"a": 1, "b_alias": 2, "extra": 3}, {"a": 1, "b": 2}])
        self.assertTrue(r.ok)
        self.assertIn("extra", r.new_fields)

    def test_failures(self):
        self.assertFalse(check_payload(self.EP, []).ok)
        self.assertFalse(check_payload(self.EP, [{"a": 1, "b": 2}]).ok)  # fewer rows than min_rows
        self.assertFalse(check_payload(self.EP, [{"a": 1}, {"a": 1}]).ok)  # b missing everywhere
        self.assertFalse(check_payload(self.EP, [{"a": 1, "b": {"nested": 1}}, {"a": 1, "b": 2}]).ok)  # unexpected type
        self.assertFalse(check_payload(self.EP, {"not": "a list"}).ok)
        self.assertFalse(check_payload(self.EP, ["x", "y"]).ok)


class ValuationFormulaTests(unittest.TestCase):
    def test_market_cap(self):
        m = market_cap("T", "d", 100.0, "d", 1000, "d", V)
        self.assertEqual((m.value, m.status), (100000.0, OK))
        self.assertEqual(market_cap("T", "d", None, None, 1000, None, V).status, INSUFFICIENT_DATA)
        self.assertEqual(market_cap("T", "d", 100.0, "d", 0, None, V).status, INSUFFICIENT_DATA)

    def test_pe_formula_known_values(self):
        # price 100, YTD EPS 5 after 2 quarters -> annual run-rate 10 -> 10.0x
        m = pe_annualized_ytd("T", "d", 100.0, "d", 5.0, "2026Q2", 2, CUMULATIVE_YTD, V)
        self.assertEqual((m.value, m.status), (10.0, OK))
        self.assertEqual(m.formula, "close / (eps_ytd * 4 / quarter)")
        self.assertEqual(m.dependencies["eps_ytd"]["period"], "2026Q2")
        self.assertEqual(m.dependencies["eps_ytd"]["basis"], CUMULATIVE_YTD)
        # Q4 cumulative EPS is already a full year
        self.assertEqual(pe_annualized_ytd("T", "d", 90.0, "d", 6.0, "2026Q4", 4, CUMULATIVE_YTD, V).value, 15.0)

    def test_pe_negative_zero_missing_never_numeric(self):
        neg = pe_annualized_ytd("T", "d", 100.0, "d", -0.5, "p", 2, CUMULATIVE_YTD, V)
        self.assertEqual((neg.value, neg.status, neg.reason), (None, NOT_APPLICABLE, "NEGATIVE_EPS"))
        zero = pe_annualized_ytd("T", "d", 100.0, "d", 0.0, "p", 2, CUMULATIVE_YTD, V)
        self.assertEqual((zero.value, zero.status, zero.reason), (None, NOT_APPLICABLE, "ZERO_EPS"))
        no_eps = pe_annualized_ytd("T", "d", 100.0, "d", None, "p", 2, CUMULATIVE_YTD, V)
        self.assertEqual((no_eps.status, no_eps.reason), (INSUFFICIENT_DATA, "MISSING_EPS"))
        no_px = pe_annualized_ytd("T", "d", None, None, 5.0, "p", 2, CUMULATIVE_YTD, V)
        self.assertEqual((no_px.status, no_px.reason), (INSUFFICIENT_DATA, "MISSING_PRICE"))
        # EPS <= 0 is NOT_APPLICABLE even when the price is missing
        self.assertEqual(pe_annualized_ytd("T", "d", None, None, -1.0, "p", 2, CUMULATIVE_YTD, V).status, NOT_APPLICABLE)

    def test_pe_refuses_an_unconfirmed_basis(self):
        m = pe_annualized_ytd("T", "d", 100.0, "d", 5.0, "p", 2, UNKNOWN_BASIS, V)
        self.assertEqual((m.value, m.status), (None, UNKNOWN_BASIS))

    def test_ps_formula_and_units(self):
        # market cap 1,000,000 TWD; revenue 100,000 thousand TWD YTD after Q2 -> annual 400,000,000 TWD -> P/S 0.0025
        m = ps_annualized_ytd("T", "d", 1_000_000.0, 100_000.0, "p", 2, CUMULATIVE_YTD, None, V)
        self.assertEqual(m.status, OK)
        self.assertAlmostEqual(m.value, 1_000_000 / (100_000 * 1000 * 4 / 2), places=6)
        self.assertEqual(ps_annualized_ytd("T", "d", 1e6, 1e5, "p", 2, CUMULATIVE_YTD, "FINANCIAL", V).reason, "FINANCIAL_REVENUE_NOT_COMPARABLE")
        self.assertEqual(ps_annualized_ytd("T", "d", 1e6, 0.0, "p", 2, CUMULATIVE_YTD, None, V).status, NOT_APPLICABLE)
        self.assertEqual(ps_annualized_ytd("T", "d", None, 1e5, "p", 2, CUMULATIVE_YTD, None, V).status, INSUFFICIENT_DATA)
        self.assertEqual(ps_annualized_ytd("T", "d", 1e6, 1e5, "p", 2, UNKNOWN_BASIS, None, V).status, UNKNOWN_BASIS)

    def test_implied_eps(self):
        self.assertAlmostEqual(implied_ttm_eps("T", "d", 2575.0, 29.85, V).value, 86.2647, places=3)
        self.assertEqual(implied_ttm_eps("T", "d", 2575.0, None, V).status, INSUFFICIENT_DATA)


class ReconcileTests(unittest.TestCase):
    def test_thresholds_are_applied_to_relative_difference(self):
        self.assertEqual(reconcile(10.05, 10.0, True)["class"], MATCH)
        self.assertEqual(reconcile(10.5, 10.0, True)["class"], MINOR)
        self.assertEqual(reconcile(12.0, 10.0, True)["class"], MATERIAL)
        self.assertEqual(reconcile(8.0, 10.0, True)["diffPct"], -20.0)

    def test_unknown_when_a_side_is_missing_or_basis_not_comparable(self):
        self.assertEqual(reconcile(None, 10.0, True)["class"], UNKNOWN)
        self.assertEqual(reconcile(10.0, None, True)["class"], UNKNOWN)
        self.assertEqual(reconcile(10.0, 10.0, False)["class"], UNKNOWN)


class StatsTests(unittest.TestCase):
    def test_percentile_matches_independent_implementation(self):
        data = sorted([3, 8, 8.5, 12, 13, 17, 21, 40, 55, 300.0])
        q = statistics.quantiles(data, n=4, method="inclusive")
        self.assertAlmostEqual(percentile_inc(data, 0.25), q[0])
        self.assertAlmostEqual(percentile_inc(data, 0.50), q[1])
        self.assertAlmostEqual(percentile_inc(data, 0.75), q[2])

    def test_describe_and_min_sample(self):
        s = describe([10, 12, 14, 16, 18], 3)
        self.assertEqual((s.count, s.median, s.mean, s.p25, s.p75), (5, 14, 14, 12, 16))
        self.assertIsNone(describe([10, 12], 3))

    def test_robust_filter_removes_only_clear_outliers_and_reports_why(self):
        items = [(f"T{i}", v) for i, v in enumerate([10, 11, 12, 13, 14, 15, 16, 17, 18, 698.33])]
        kept, removed, fences = robust_filter(items)
        self.assertTrue(fences["applied"])
        self.assertEqual([r.ticker for r in removed], ["T9"])
        self.assertEqual(removed[0].reason, "EXTREME_PE_HIGH")
        self.assertEqual(len(kept), 9)

    def test_robust_filter_leaves_small_samples_alone(self):
        items = [("A", 10), ("B", 12), ("C", 900)]
        kept, removed, fences = robust_filter(items)
        self.assertFalse(fences["applied"])
        self.assertEqual((len(kept), removed), (3, []))

    def test_legacy_rules_are_not_used(self):
        """A P/E of 250 is not dropped merely for being >= 200, and the fence is not IQR x 1.5."""
        items = [(f"T{i}", v) for i, v in enumerate([100, 110, 120, 130, 140, 150, 160, 170, 180, 250])]
        kept, removed, fences = robust_filter(items)
        self.assertEqual(removed, [])
        self.assertEqual(fences["k"], 3.0)


class EpsBasisTests(unittest.TestCase):
    def rows(self, ratio_a, ratio_b, n=40):
        out = []
        for i in range(n):
            eps, close, pe = 2.0, 100.0, 100.0 / (2.0 * ratio_a)
            rev = 1000.0
            out.append(BasisRow(f"T{i}", close, pe, eps, rev, rev * ratio_b))
        return out

    def test_both_evidences_support_cumulative_ytd(self):
        f = find_basis(self.rows(2.0, 8 / 6), quarter=2, cum_month=8)
        self.assertEqual((f["epsFeedBasis"], f["confirmed"]), (CUMULATIVE_YTD, True))

    def test_single_quarter_is_recognised_and_not_accepted_as_ytd(self):
        f = find_basis(self.rows(4.0, 8 / 3), quarter=2, cum_month=8)
        self.assertFalse(f["confirmed"])
        self.assertEqual(f["epsFeedBasis"], UNKNOWN_BASIS)
        self.assertEqual(f["evidenceA"]["supports"], SINGLE_QUARTER)

    def test_disagreement_or_thin_evidence_stays_unknown(self):
        self.assertFalse(find_basis(self.rows(2.0, 8 / 3), 2, 8)["confirmed"])  # A says YTD, B says single quarter
        self.assertFalse(find_basis(self.rows(2.0, 8 / 6, n=5), 2, 8)["confirmed"])  # too few companies
        self.assertFalse(find_basis(self.rows(2.0, 8 / 6), 2, None)["confirmed"])  # no monthly month


class OfficialPeDefinitionTests(unittest.TestCase):
    def test_official_pe_is_trailing_four_quarters_by_the_exchanges_definition_not_by_inference(self):
        rows = [BasisRow(f"T{i}", 100.0, 10.0, 2.0, 1000.0, 1333.0) for i in range(40)]
        for f in (find_basis(rows, 2, 8), find_basis([], 2, None)):
            self.assertEqual(f["officialPEBasis"], "OFFICIAL_TTM")  # holds even when our own evidence is thin
            d = f["officialPEDefinition"]
            self.assertIn("4季", d["TWSE"]["text"])
            self.assertIn("4季", d["TPEX"]["text"])
            self.assertTrue(d["TWSE"]["url"].startswith("https://www.twse.com.tw/"))
            self.assertTrue(d["TPEX"]["url"].startswith("https://www.tpex.org.tw/"))
            self.assertIn("EPS", "EPS")  # definition also says EPS <= 0 is not published
            self.assertIn("0或負數", d["TWSE"]["text"])


class ForwardEstimateTests(unittest.TestCase):
    def test_only_three_statuses_exist(self):
        for st in ("COMPANY_GUIDANCE", "ANALYST_ESTIMATE", "USER_ASSUMPTION"):
            self.assertEqual(ForwardEstimate("2330", 70.0, "FY2027", st, "user", None, "2026-10-06").status, st)
        with self.assertRaises(ValueError):
            ForwardEstimate("2330", 70.0, "FY2027", "CONSENSUS", "scraped", None, "2026-10-06")

    def test_nothing_is_generated_automatically(self):
        self.assertEqual(NoForwardEstimates().estimates("2330", "FY2027"), [])
        store = UserSuppliedForwardEstimates()
        self.assertEqual(store.estimates("2330", "FY2027"), [])
        store.add(ForwardEstimate("2330", 70.0, "FY2027", "USER_ASSUMPTION", "me", "my guess", "2026-10-06"))
        self.assertEqual(store.estimates("2330", "FY2027")[0].value, 70.0)


if __name__ == "__main__":
    unittest.main()
