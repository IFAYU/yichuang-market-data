import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

import requests

from mitw.config import RequestPolicy
from mitw.providers.endpoints import Endpoint
from mitw.providers.http import RunContext, SourceStopped, fetch_endpoint
from mitw.raw.store import ImmutableError, RawStore
from mitw.validate.schema import SchemaDriftError

POLICY = RequestPolicy(min_delay_s=0.0, backoff_s=(0.0, 0.0), max_attempts=2)
EP = Endpoint("twse.companies", "TWSE", "companies", "https://example.test/v1/a", ("code", "name"), 2, True)
EP2 = Endpoint("twse.prices", "TWSE", "prices", "https://example.test/v1/b", ("code",), 1, True)
GOOD = [{"code": "1", "name": "a"}, {"code": "2", "name": "b"}]


class Resp:
    def __init__(self, status=200, body=b"[]", headers=None):
        self.status_code, self.content, self.headers = status, body, headers or {"Content-Type": "application/json"}


class FakeSession:
    def __init__(self, *responses):
        self.responses, self.calls = list(responses), []

    def get(self, url, headers=None, timeout=None):
        self.calls.append({"url": url, "headers": dict(headers or {})})
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def j(x):
    return json.dumps(x, ensure_ascii=False).encode()


class RequestPolicyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="mitw_http_"))
        self.store = RawStore(self.tmp)
        self.ctx = RunContext("r1", "2026-10-06")

    def fetch(self, ep, session):
        return fetch_endpoint(ep, self.store, self.ctx, POLICY, session=session, sleep=lambda s: None)

    def test_success_writes_immutable_raw_with_hash_and_headers(self):
        s = FakeSession(Resp(200, j(GOOD), {"Content-Type": "application/json", "ETag": '"abc"', "Last-Modified": "Fri, 04 Sep 2026 06:29:54 GMT"}))
        ref = self.fetch(EP, s)
        self.assertEqual(ref.meta.httpStatus, 200)
        self.assertEqual(ref.meta.etag, '"abc"')
        self.assertEqual(ref.meta.rows, 2)
        self.assertEqual(len(ref.meta.sha256), 64)
        self.assertEqual(self.ctx.request_count, 1)
        with self.assertRaises(ImmutableError):
            self.store.write(ref.meta, b"[]", "TWSE", "companies")

    def test_same_day_success_is_never_refetched(self):
        s = FakeSession(Resp(200, j(GOOD)))
        self.fetch(EP, s)
        ref = self.fetch(EP, FakeSession())  # a second session with NO responses: any request would blow up
        self.assertTrue(ref.reusedToday)
        self.assertEqual(self.ctx.request_count, 1)

    def test_403_and_429_stop_the_source_with_no_retry(self):
        for code in (403, 429):
            self.setUp()
            s = FakeSession(Resp(code, b"nope"))
            with self.assertRaises(SourceStopped):
                self.fetch(EP, s)
            self.assertEqual(len(s.calls), 1, f"HTTP {code} must not be retried")
            # the whole source is now stopped: the next endpoint of the same market makes no request at all
            s2 = FakeSession()
            with self.assertRaises(SourceStopped):
                self.fetch(EP2, s2)
            self.assertEqual(s2.calls, [])

    def test_html_instead_of_json_stops_source_and_keeps_evidence(self):
        s = FakeSession(Resp(200, b"<html>verify you are human</html>", {"Content-Type": "text/html"}))
        with self.assertRaises(SourceStopped):
            self.fetch(EP, s)
        self.assertEqual(len(s.calls), 1)
        self.assertIsNone(self.store.get("2026-10-06", "TWSE", "companies"))
        self.assertTrue((self.tmp / "2026-10-06" / "twse" / "companies.rejected.json").exists())

    def test_schema_drift_fails_and_produces_no_raw_snapshot(self):
        renamed = [{"code": "1", "company": "a"}, {"code": "2", "company": "b"}]  # 'name' renamed
        s = FakeSession(Resp(200, j(renamed)))
        with self.assertRaises(SchemaDriftError):
            self.fetch(EP, s)
        self.assertIsNone(self.store.get("2026-10-06", "TWSE", "companies"))
        self.assertIn("TWSE", self.ctx.halted)

    def test_empty_market_response_is_a_failure(self):
        s = FakeSession(Resp(200, b"[]"))
        with self.assertRaises(SchemaDriftError):
            self.fetch(EP, s)

    def test_server_error_retries_a_limited_number_of_times_then_stops(self):
        s = FakeSession(Resp(503, b""), Resp(503, b""), Resp(200, j(GOOD)))
        with self.assertRaises(SourceStopped):
            self.fetch(EP, s)
        self.assertEqual(len(s.calls), 2)  # max_attempts, never a third

    def test_transient_network_error_recovers_within_limit(self):
        s = FakeSession(requests.ConnectionError("boom"), Resp(200, j(GOOD)))
        ref = self.fetch(EP, s)
        self.assertEqual(ref.meta.httpStatus, 200)
        self.assertEqual(len(s.calls), 2)

    def test_conditional_request_uses_etag_and_reuses_bytes_on_304(self):
        old = RunContext("r0", "2026-10-05")
        fetch_endpoint(EP, self.store, old, POLICY, session=FakeSession(Resp(200, j(GOOD), {"Content-Type": "application/json", "ETag": '"v1"'})), sleep=lambda s: None)
        s = FakeSession(Resp(304, b""))
        ref = self.fetch(EP, s)
        self.assertEqual(s.calls[0]["headers"].get("If-None-Match"), '"v1"')
        self.assertEqual(ref.meta.httpStatus, 304)
        self.assertEqual(ref.meta.reusedFrom, "2026-10-05")
        self.assertEqual(ref.load(), GOOD)
        self.assertEqual(self.ctx.not_modified, 1)

    def test_no_conditional_headers_for_sources_that_do_not_support_them(self):
        old = RunContext("r0", "2026-10-05")
        ep = replace(EP, conditional=False)
        fetch_endpoint(ep, self.store, old, POLICY, session=FakeSession(Resp(200, j(GOOD), {"Content-Type": "application/json", "ETag": '"v1"'})), sleep=lambda s: None)
        s = FakeSession(Resp(200, j(GOOD)))
        self.fetch(ep, s)
        self.assertNotIn("If-None-Match", s.calls[0]["headers"])

    def test_identity_is_constant_and_honest(self):
        """No user-agent rotation, no proxy settings anywhere in the request path."""
        s = FakeSession(Resp(200, j(GOOD)), Resp(200, j(GOOD)))
        self.fetch(EP, s)
        self.fetch(replace(EP2, min_rows=1, required_fields=("code",)), s)
        uas = {c["headers"]["User-Agent"] for c in s.calls}
        self.assertEqual(len(uas), 1)
        self.assertIn("market-intelligence-tw", uas.pop())
        src = (Path(__file__).resolve().parent.parent / "mitw" / "providers" / "http.py").read_text(encoding="utf-8")
        for banned in ("proxies", "playwright", "selenium", "random.choice"):
            self.assertNotIn(banned, src)


if __name__ == "__main__":
    unittest.main()
