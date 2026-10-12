"""Phase 8B: listed-ETF closing quotes, the three-word publication outcome, and the holiday-aware weekly NO_UPDATE.

Everything runs on fixtures and a fake clock: no network, nothing is pushed, nothing is published anywhere but a temp directory.
The five-symbol sample is REAL data (TWSE OpenAPI, read 2026-10-12): the raw rows are in the fixture, the expected closes are written down by hand.
"""
import copy
import json
import shutil
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path

from mitw.contracts.version import SNAPSHOT_SCHEMA_VERSION
from mitw.etf import EtfSourceError, build_quotes, classify_fund, content_hash, parse_price, publish_etf, read_etf_manifest
from mitw.publish_state import FAILED, NO_UPDATE, PENDING, PUBLISHED, classify_outcome, outcome_of_status
from mitw.trading_calendar import TradingCalendar

FIX = json.loads((Path(__file__).parent / "fixtures" / "etf_official_sample_2026-10-08.json").read_text(encoding="utf-8"))
K_CODE, K_TYPE, K_NAME = "基金代號", "基金類型", "基金簡稱"
DOMESTIC = "國內成分證券指數股票型基金"
# the official 2026 closure list as the company manifest published it (2026-10-09 is the holiday that matters here)
CLOSED = ["2026-01-01", "2026-02-12", "2026-02-13", "2026-02-16", "2026-02-17", "2026-02-18", "2026-02-19", "2026-02-20", "2026-02-27", "2026-04-03", "2026-04-06",
          "2026-05-01", "2026-06-19", "2026-09-25", "2026-09-28", "2026-10-09", "2026-10-26", "2026-12-25"]
CAL = TradingCalendar.from_json({"source": "TWSE_OPENAPI_holidaySchedule", "fetchedAt": "2026-10-09T06:35:23+08:00", "coveredFrom": "2026-01-01", "coveredTo": "2026-12-31",
                                 "closedDates": CLOSED, "extraClosures": [], "manualClosures": []})


def roc(d: str) -> str:
    y, m, dd = d.split("-")
    return f"{int(y) - 1911}{m}{dd}"


def rows(trade_date: str):
    """The real sample + enough synthetic filler to pass the source-size sanity floors. Filler is clearly fake (codes 9xxx / 008xx)."""
    price = [dict(r, Date=roc(trade_date)) for r in FIX["priceRows"]]
    master = copy.deepcopy(FIX["masterRows"])
    for i in range(900):
        price.append({"Date": roc(trade_date), "Code": f"{9000 + i}", "Name": "x", "ClosingPrice": "10.00"})
    for i in range(60):
        code = f"008{i:02d}"
        price.append({"Date": roc(trade_date), "Code": code, "Name": "etf", "ClosingPrice": f"{20 + i}.50"})
        master.append({K_CODE: code, K_NAME: f"fake{i}", K_TYPE: DOMESTIC})
    for i in range(100):  # unsupported filler so the master list is long enough
        master.append({K_CODE: f"0099{i:02d}"[:6], K_NAME: f"over{i}", K_TYPE: "國外成分證券指數股票型基金"})
    return price, master


def doc_for(trade_date: str, retrieved="2026-10-13T07:00:00+08:00"):
    p, m = rows(trade_date)
    return build_quotes(p, m, retrieved, retrieved, {})


class FiveSymbols(unittest.TestCase):
    """Raw source -> converted record, for the five symbols the brief names."""

    def test_raw_rows_and_converted_prices_match(self):
        raw = {r["Code"]: r for r in FIX["priceRows"]}
        doc = doc_for("2026-10-08")
        by = {i["symbol"]: i for i in doc["instruments"]}
        for sym, expected in FIX["expected"].items():
            raw_close = raw[sym]["ClosingPrice"]
            self.assertEqual(float(raw_close), float(expected), sym)  # the hand-written expectation equals the raw source
            rec = by[sym]
            self.assertEqual((rec["price"], rec["priceStatus"], rec["priceDate"], rec["currency"], rec["market"], rec["instrumentType"]),
                             (expected, "OK", "2026-10-08", "TWD", "TWSE", "ETF"), sym)
            self.assertEqual(rec["supportStatus"], "SUPPORTED")
            self.assertIsInstance(rec["price"], str)  # decimal string, never a float
        self.assertEqual(raw["0056"]["ClosingPrice"], "58.30")
        self.assertEqual(by["0056"]["price"], "58.3")  # trailing zero removed, value identical

    def test_price_unit_and_roc_date(self):
        self.assertEqual(roc("2026-10-08"), "1151008")
        self.assertEqual(doc_for("2026-10-08")["priceDate"], "2026-10-08")

    def test_etf_identity_comes_from_the_master_list_not_from_the_code(self):
        doc = doc_for("2026-10-08")
        symbols = {i["symbol"] for i in doc["instruments"]}
        self.assertNotIn("2330", symbols)  # a company is in the price file, not in the fund master
        self.assertNotIn("9000", symbols)
        p, m = rows("2026-10-08")
        m = [x for x in m if x[K_CODE] != "0050"]  # 0050 looks like an ETF but the master list does not know it here
        self.assertNotIn("0050", {i["symbol"] for i in build_quotes(p, m, "t", "t", {})["instruments"]})

    def test_unsupported_types_are_listed_with_a_reason_and_no_price(self):
        by = {i["symbol"]: i for i in doc_for("2026-10-08")["instruments"]}
        for sym, cat in (("00631L", "LEVERAGED_INVERSE"), ("006205", "OVERSEAS_COMPONENTS"), ("00625K", "OVERSEAS_COMPONENTS"), ("00682U", "FUTURES_TRUST")):
            self.assertEqual((by[sym]["supportStatus"], by[sym]["price"], by[sym]["etfCategory"]), ("NOT_SUPPORTED", None, cat), sym)
            self.assertTrue(by[sym]["notSupportedReason"])
        self.assertEqual(classify_fund("國外成分證券主動式交易所交易基金(債券)")[0], "BOND")
        self.assertEqual(classify_fund("something new")[1:], ("NOT_SUPPORTED", "UNKNOWN_FUND_TYPE: not in the supported map"))
        self.assertEqual(classify_fund(DOMESTIC, "某債券")[0], "BOND")  # a bond name in a domestic type is not trusted

    def test_no_close_is_null_never_zero_and_a_listed_code_without_a_row_is_unknown(self):
        by = {i["symbol"]: i for i in doc_for("2026-10-08")["instruments"]}
        self.assertEqual((by["0054"]["price"], by["0054"]["priceStatus"]), (None, "NOT_QUOTED_BY_SOURCE"))
        for raw, status in (("", "NO_CLOSE"), ("0.00", "INVALID"), ("-1", "INVALID"), ("abc", "INVALID"), ("1e3", "INVALID"), ("12.34567", "INVALID"), (None, "NO_CLOSE")):
            self.assertEqual(parse_price(raw), (None, status), repr(raw))
        self.assertEqual(parse_price("1,234.50"), ("1234.5", "OK"))

    def test_the_artifact_carries_the_contract_fields_and_no_client_data(self):
        doc = doc_for("2026-10-08")
        for k in ("schemaVersion", "priceDate", "retrievedAt", "generatedAt", "source", "integrity", "instruments"):
            self.assertIn(k, doc)
        rec = next(i for i in doc["instruments"] if i["symbol"] == "0050")
        for k in ("symbol", "market", "instrumentType", "currency", "price", "priceDate", "retrievedAt", "source"):
            self.assertIn(k, rec)
        self.assertIn("CONTENT_CONSISTENCY_ONLY", doc["integrity"]["sha256Semantics"])
        self.assertEqual(doc["source"]["terms"]["redistributionStatus"], "UNCONFIRMED_FOR_THESE_ENDPOINTS")
        text = json.dumps(doc).lower()
        for word in ("holding", "client", "quantity", "shares", "workspace", "email", "bid", "ask"):
            self.assertNotIn(f'"{word}', text)

    def test_a_broken_source_is_refused_not_published_as_a_quiet_day(self):
        p, m = rows("2026-10-08")
        mixed = [dict(r) for r in p]
        mixed[0]["Date"] = roc("2026-10-07")
        with self.assertRaises(EtfSourceError):
            build_quotes(mixed, m, "t", "t", {})
        with self.assertRaises(EtfSourceError):
            build_quotes(p[:50], m, "t", "t", {})
        with self.assertRaises(EtfSourceError):
            build_quotes([{k: v for k, v in r.items() if k != "ClosingPrice"} for r in p], m, "t", "t", {})
        dup = p + [dict(p[0])]
        with self.assertRaises(EtfSourceError):
            build_quotes(dup, m, "t", "t", {})
        no_close = [dict(r, ClosingPrice="") if r["Code"].startswith("008") else r for r in p]  # nearly every supported ETF without a close
        for r in no_close:
            if r["Code"] in FIX["expected"]:
                r["ClosingPrice"] = ""
        with self.assertRaises(EtfSourceError):
            build_quotes(no_close, m, "t", "t", {})

    def test_a_rerun_of_the_same_source_has_the_same_content_hash(self):
        a, b = doc_for("2026-10-08", "2026-10-12T18:00:00+08:00"), doc_for("2026-10-08", "2026-10-12T19:30:00+08:00")
        self.assertNotEqual(json.dumps(a), json.dumps(b))
        self.assertEqual(content_hash(a), content_hash(b))
        self.assertNotEqual(content_hash(a), content_hash(doc_for("2026-10-12")))


class Outcome(unittest.TestCase):
    """PUBLISHED / NO_UPDATE / FAILED / PENDING: the reason the date did not move decides the word."""

    def kw(self, **o):
        base = dict(source_date=date(2026, 10, 8), published_date=date(2026, 10, 8), expected_date=date(2026, 10, 8), today=date(2026, 10, 12), final=True, calendar_known=True)
        return {**base, **o}

    def test_holiday_is_no_update_and_normal(self):
        o = classify_outcome(**self.kw())
        self.assertEqual((o.status, o.reason_code, o.normal), (NO_UPDATE, "NO_NEW_TRADING_DAY", True))

    def test_owed_trading_day_missing_is_failed_on_the_last_attempt_and_pending_before(self):
        o = classify_outcome(**self.kw(expected_date=date(2026, 10, 12)))
        self.assertEqual((o.status, o.reason_code, o.normal), (FAILED, "EXPECTED_TRADING_DAY_MISSING", False))
        o = classify_outcome(**self.kw(expected_date=date(2026, 10, 12), final=False))
        self.assertEqual((o.status, o.normal), (PENDING, False))

    def test_a_source_that_goes_backwards_is_never_no_update(self):
        o = classify_outcome(**self.kw(source_date=date(2026, 10, 7), expected_date=date(2026, 10, 7)))
        self.assertEqual((o.status, o.reason_code), (FAILED, "SOURCE_DATE_REGRESSED"))
        o = classify_outcome(**self.kw(source_date=date(2026, 10, 7), final=False))
        self.assertEqual(o.status, FAILED)  # not even a retry makes a regression "pending"

    def test_unknown_calendar_is_never_silently_normal(self):
        o = classify_outcome(**self.kw(expected_date=None, calendar_known=False))
        self.assertNotEqual(o.status, NO_UPDATE)
        self.assertEqual(o.reason_code, "CALENDAR_UNKNOWN")

    def test_other_failures_and_publication(self):
        self.assertEqual(classify_outcome(**self.kw(source_error="HTTP 503")).reason_code, "SOURCE_ERROR")
        self.assertEqual(classify_outcome(**self.kw(source_date=None)).reason_code, "SOURCE_DATE_MALFORMED")
        self.assertEqual(classify_outcome(**self.kw(source_date=date(2026, 10, 13))).reason_code, "SOURCE_DATE_IN_FUTURE")
        self.assertEqual(classify_outcome(**self.kw(gate_refused="coverage")).status, FAILED)
        self.assertEqual(classify_outcome(**self.kw(source_date=date(2026, 10, 12), expected_date=date(2026, 10, 12), published_ok=True)).status, PUBLISHED)
        self.assertEqual(classify_outcome(**self.kw(source_date=date(2026, 10, 12), expected_date=date(2026, 10, 12))).reason_code, "ADVANCED_BUT_NOT_PUBLISHED")

    def test_recorded_status_vocabularies_map_and_unknown_is_failed(self):
        self.assertEqual(outcome_of_status("NOOP_ALREADY_PUBLISHED").status, NO_UPDATE)
        self.assertEqual(outcome_of_status("NO_TRADING_DAY_EXPECTED").status, NO_UPDATE)
        self.assertEqual(outcome_of_status("SUCCESS").status, PUBLISHED)
        self.assertEqual(outcome_of_status("WAITING_FOR_NEW_MARKET_DATA").status, PENDING)
        self.assertEqual(outcome_of_status("FAILED_NO_NEW_MARKET_DATA").status, FAILED)
        self.assertEqual(outcome_of_status("REFUSED").status, FAILED)
        self.assertEqual(outcome_of_status("???").status, FAILED)


class Publish(unittest.TestCase):
    def setUp(self):
        self.out = Path(tempfile.mkdtemp(prefix="mitw_etf_"))
        self.addCleanup(shutil.rmtree, self.out, True)

    def at(self, when: str, trade_date: str = None, doc=None, **kw):
        d = doc if doc is not None else (doc_for(trade_date, when) if trade_date else None)
        return publish_etf(d, self.out, datetime.fromisoformat(when), CAL, source_error=kw.pop("source_error", None), **kw)

    def manifest_bytes(self):
        p = self.out / "etf" / "manifest.json"
        return p.read_bytes() if p.exists() else b""

    def files(self):
        return sorted(str(p.relative_to(self.out)) for p in self.out.rglob("*") if p.is_file())

    def test_normal_trading_day_publishes_then_a_second_read_is_no_update(self):
        r = self.at("2026-10-13T07:00:00+08:00", "2026-10-12", final=False)  # Tuesday after 06:30: Monday's close is owed
        self.assertEqual((r.outcome.status, r.outcome.reason_code), (PUBLISHED, "NEW_RELEASE"))
        self.assertEqual(read_etf_manifest(self.out)["priceDate"], "2026-10-12")
        before, files = self.manifest_bytes(), self.files()
        r2 = self.at("2026-10-13T07:30:00+08:00", "2026-10-12", final=False)
        self.assertEqual((r2.outcome.status, r2.outcome.normal), (NO_UPDATE, True))
        self.assertEqual((self.manifest_bytes(), self.files()), (before, files))  # no new release, manifest untouched

    def test_weekend_and_long_holiday_are_normal_no_update(self):
        self.at("2026-10-09T07:00:00+08:00", "2026-10-08", final=False)  # holiday Friday: Thursday's close is the latest
        before = self.manifest_bytes()
        for when in ("2026-10-09T08:00:00+08:00", "2026-10-10T07:30:00+08:00", "2026-10-11T12:00:00+08:00", "2026-10-12T09:00:00+08:00"):
            r = self.at(when, "2026-10-08")
            self.assertEqual((r.outcome.status, r.outcome.reason_code, r.outcome.normal), (NO_UPDATE, "NO_NEW_TRADING_DAY", True), when)
        self.assertEqual(self.manifest_bytes(), before)

    def test_before_the_source_publishes_is_not_a_failure(self):
        self.at("2026-10-12T07:00:00+08:00", "2026-10-08", final=False)
        # Tuesday 05:50: Monday's close is not yet owed (06:30) -> the old close is simply the latest
        self.assertEqual(self.at("2026-10-13T05:50:00+08:00", "2026-10-08").outcome.status, NO_UPDATE)
        # Tuesday 08:00 and the source is still on Thursday: now Monday IS owed
        late = self.at("2026-10-13T08:00:00+08:00", "2026-10-08", final=False)
        self.assertEqual((late.outcome.status, late.outcome.reason_code), (PENDING, "EXPECTED_TRADING_DAY_MISSING"))
        last = self.at("2026-10-13T08:30:00+08:00", "2026-10-08", final=True)
        self.assertEqual((last.outcome.status, last.outcome.normal), (FAILED, False))

    def test_source_date_regression_and_future_and_closed_day_fail_and_keep_the_last_good(self):
        self.at("2026-10-13T07:00:00+08:00", "2026-10-12", final=False)
        before, files = self.manifest_bytes(), self.files()
        for when, td, code in (("2026-10-13T09:00:00+08:00", "2026-10-08", "SOURCE_DATE_REGRESSED"), ("2026-10-13T09:00:00+08:00", "2026-10-14", "SOURCE_DATE_IN_FUTURE"),
                               ("2026-10-13T09:00:00+08:00", "2026-10-09", "SOURCE_ERROR")):
            r = self.at(when, td)
            self.assertEqual((r.outcome.status, r.outcome.reason_code), (FAILED, code), td)
            self.assertFalse(r.outcome.normal)
        self.assertEqual((self.manifest_bytes(), self.files()), (before, files))

    def test_download_failure_and_broken_payload_are_failed_not_no_update(self):
        self.at("2026-10-13T07:00:00+08:00", "2026-10-12", final=False)
        before = self.manifest_bytes()
        r = self.at("2026-10-14T07:00:00+08:00", source_error="URLError: timed out")
        self.assertEqual((r.outcome.status, r.outcome.reason_code), (FAILED, "SOURCE_ERROR"))
        self.assertEqual(self.manifest_bytes(), before)

    def test_unknown_calendar_year_does_not_become_a_normal_no_update(self):
        self.at("2026-12-30T07:00:00+08:00", "2026-12-29", final=False)
        r = publish_etf(doc_for("2026-12-29"), self.out, datetime.fromisoformat("2027-01-05T10:00:00+08:00"), CAL, final=True)
        self.assertEqual((r.outcome.status, r.outcome.reason_code), (FAILED, "CALENDAR_UNKNOWN"))

    def test_same_date_with_different_content_is_a_failure_a_person_must_look_at(self):
        self.at("2026-10-13T07:00:00+08:00", "2026-10-12", final=False)
        p, m = rows("2026-10-12")
        p[0] = dict(p[0], ClosingPrice="1.00")
        corrected = build_quotes(p, m, "2026-10-13T09:00:00+08:00", "2026-10-13T09:00:00+08:00", {})
        r = self.at("2026-10-13T09:00:00+08:00", doc=corrected)
        self.assertEqual((r.outcome.status, r.outcome.reason_code), (FAILED, "SAME_DATE_DIFFERENT_CONTENT"))

    def test_company_snapshot_files_and_contract_are_untouched(self):
        (self.out / "manifest.json").write_text('{"release":"company"}', encoding="utf-8")
        self.at("2026-10-13T07:00:00+08:00", "2026-10-12", final=False)
        self.assertEqual((self.out / "manifest.json").read_text(encoding="utf-8"), '{"release":"company"}')
        self.assertTrue(all(f.startswith("etf") or f == "manifest.json" for f in self.files()))
        self.assertEqual(SNAPSHOT_SCHEMA_VERSION.split(".")[0], "1")  # the company contract's major version did not move

    def test_published_manifest_names_the_file_and_the_hash_matches(self):
        import hashlib
        self.at("2026-10-13T07:00:00+08:00", "2026-10-12", final=False)
        m = read_etf_manifest(self.out)
        f = m["files"]["etf-quotes.json"]
        self.assertEqual(hashlib.sha256((self.out / f["path"]).read_bytes()).hexdigest(), f["sha256"])
        self.assertIn("CONTENT_CONSISTENCY_ONLY", m["hashSemantics"])


if __name__ == "__main__":
    unittest.main()
