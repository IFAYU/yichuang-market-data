import json
import tempfile
import unittest
from pathlib import Path

from mitw.reconcile import PENDING, VERIFIED, reconcile_files, reconcile_health

NOW = "2026-10-08T09:00:00+08:00"


def health(release="119a6f91b6fe", state=PENDING):
    d = {"releaseId": release, "publicVerification": state, "manifestUpdated": True}
    return {"updatedAt": "x", "daily": {"lastDecision": d}, "runs": [{"runId": "r1", "daily": dict(d)}], "lastSuccess": {"release": release}}


class ReconcilePure(unittest.TestCase):
    def test_live_and_readable_becomes_verified(self):
        h, o = reconcile_health(health(), {"release": "119a6f91b6fe"}, lambda r: (True, "ok"), NOW)
        self.assertEqual(o, "VERIFIED")
        self.assertEqual(h["daily"]["lastDecision"]["publicVerification"], VERIFIED)
        self.assertEqual(h["runs"][0]["daily"]["publicVerification"], VERIFIED)

    def test_unreadable_stays_pending_and_records_the_attempt(self):
        h, o = reconcile_health(health(), {"release": "119a6f91b6fe"}, lambda r: (False, "HTTP 404"), NOW)
        self.assertEqual(o, "STILL_PENDING")
        self.assertEqual(h["daily"]["lastDecision"]["publicVerification"], PENDING)
        self.assertEqual(h["daily"]["lastDecision"]["publicVerificationCheckedAt"], NOW)

    def test_release_that_is_not_live_is_never_verified(self):
        called = []
        h, o = reconcile_health(health(), {"release": "other"}, lambda r: called.append(r) or (True, ""), NOW)
        self.assertEqual((o, called), ("NOT_LIVE_RELEASE", []))
        self.assertEqual(h["daily"]["lastDecision"]["publicVerification"], PENDING)

    def test_nothing_pending_does_not_call_the_network(self):
        called = []
        h, o = reconcile_health(health(state=VERIFIED), {"release": "119a6f91b6fe"}, lambda r: called.append(r) or (True, ""), NOW)
        self.assertEqual((o, called), ("NOTHING_PENDING", []))

    def test_repeat_is_idempotent(self):
        h, _ = reconcile_health(health(), {"release": "119a6f91b6fe"}, lambda r: (True, ""), NOW)
        h2, o2 = reconcile_health(h, {"release": "119a6f91b6fe"}, lambda r: (True, ""), "later")
        self.assertEqual((o2, h2["daily"]["lastDecision"]["publicVerifiedAt"]), ("NOTHING_PENDING", NOW))

    def test_weekly_health_without_daily_block_is_untouched(self):
        h = {"weekly": {}, "daily": None}
        self.assertEqual(reconcile_health(h, {"release": "x"}, lambda r: (True, ""), NOW)[1], "NOTHING_PENDING")


class ReconcileFiles(unittest.TestCase):
    def _dir(self, h, m):
        d = Path(tempfile.mkdtemp())
        (d / "health.json").write_text(json.dumps(h), encoding="utf-8")
        (d / "manifest.json").write_text(json.dumps(m), encoding="utf-8")
        (d / "releases").mkdir()
        (d / "releases" / "keep.txt").write_text("last known good", encoding="utf-8")
        return d

    def test_only_health_is_written_and_lkg_is_untouched(self):
        m = {"release": "119a6f91b6fe", "marker": 1}
        d = self._dir(health(), m)
        before = (d / "manifest.json").read_bytes()
        self.assertEqual(reconcile_files(d, lambda r: (True, ""), NOW), "VERIFIED")
        self.assertEqual((d / "manifest.json").read_bytes(), before)
        self.assertEqual((d / "releases" / "keep.txt").read_text(encoding="utf-8"), "last known good")
        self.assertEqual(json.loads((d / "health.json").read_text(encoding="utf-8"))["daily"]["lastDecision"]["publicVerification"], VERIFIED)
        self.assertFalse((d / "health.json.tmp").exists())

    def test_failed_check_leaves_health_bytes_unchanged_apart_from_the_attempt(self):
        d = self._dir(health(), {"release": "119a6f91b6fe"})
        self.assertEqual(reconcile_files(d, lambda r: (False, "x"), NOW), "STILL_PENDING")
        self.assertEqual((d / "releases" / "keep.txt").read_text(encoding="utf-8"), "last known good")

    def test_unreadable_or_corrupt_input_is_reported_not_raised(self):
        d = Path(tempfile.mkdtemp())
        self.assertEqual(reconcile_files(d, lambda r: (True, ""), NOW), "UNREADABLE")
        (d / "health.json").write_text("{not json", encoding="utf-8")
        (d / "manifest.json").write_text("{}", encoding="utf-8")
        self.assertEqual(reconcile_files(d, lambda r: (True, ""), NOW), "UNREADABLE")

    def test_verify_exception_does_not_destroy_health(self):
        d = self._dir(health(), {"release": "119a6f91b6fe"})
        def boom(r): raise RuntimeError("net down")
        with self.assertRaises(RuntimeError):
            reconcile_files(d, boom, NOW)
        self.assertEqual(json.loads((d / "health.json").read_text(encoding="utf-8"))["daily"]["lastDecision"]["publicVerification"], PENDING)


if __name__ == "__main__":
    unittest.main()
