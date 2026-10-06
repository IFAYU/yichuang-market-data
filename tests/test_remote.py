import hashlib
import json
import tempfile
import unittest
from pathlib import Path

import requests

from mitw.remote import verify_release
from mitw.runlog import mark_last_run_failed, record, restore_from_health, RunRecord, write_health
from mitw.snapshot.build import prune_releases


class Resp:
    def __init__(self, code, content=b""):
        self.status_code, self.content = code, content


def make_out(files):
    out = Path(tempfile.mkdtemp(prefix="mitw_rem_"))
    manifest = {"release": "abc123abc123", "files": {n: {"sha256": hashlib.sha256(b).hexdigest()} for n, b in files.items()}}
    (out / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return out


FILES = {"market-snapshot.json": b'{"a":1}', "industry-snapshots.json": b'{"b":2}'}


class VerifyRemoteTests(unittest.TestCase):
    def verify(self, getter, timeout=100):
        t = [0.0]
        return verify_release("https://example.invalid/data", make_out(FILES), "abc123abc123", timeout_s=timeout, interval_s=10, get=getter,
                              sleep=lambda s: t.__setitem__(0, t[0] + s), clock=lambda: t[0])

    def test_readable_when_every_file_is_served_with_the_recorded_hash(self):
        ok, detail = self.verify(lambda u: Resp(200, FILES[u.split("/")[-1].split("?")[0]]))
        self.assertTrue(ok, detail)

    def test_site_not_deployed_yet_waits_then_gives_up_so_the_manifest_is_never_published(self):
        calls = []
        ok, detail = self.verify(lambda u: (calls.append(u), Resp(404))[1], timeout=60)
        self.assertFalse(ok)
        self.assertIn("HTTP 404", detail)
        self.assertGreater(len(calls), 2)  # it kept trying until the deadline

    def test_becomes_readable_after_the_deploy_finishes(self):
        n = {"i": 0}

        def getter(u):
            n["i"] += 1
            return Resp(404) if n["i"] <= 4 else Resp(200, FILES[u.split("/")[-1].split("?")[0]])

        self.assertTrue(self.verify(getter)[0])

    def test_corrupted_release_bytes_are_not_accepted(self):
        ok, detail = self.verify(lambda u: Resp(200, b"corrupted"), timeout=20)
        self.assertFalse(ok)
        self.assertIn("do not match", detail)

    def test_network_errors_are_just_not_yet(self):
        def boom(u):
            raise requests.ConnectionError("down")

        ok, detail = self.verify(boom, timeout=20)
        self.assertFalse(ok)
        self.assertIn("ConnectionError", detail)

    def test_a_manifest_for_another_release_is_refused(self):
        ok, detail = verify_release("https://x", make_out(FILES), "other0000000", get=lambda u: Resp(200))
        self.assertFalse(ok)


class CiPersistenceTests(unittest.TestCase):
    def test_history_is_restored_from_the_published_health_and_a_remote_failure_rewrites_it(self):
        out, runs = Path(tempfile.mkdtemp()), Path(tempfile.mkdtemp())
        record(RunRecord(runId="r1", startedAt="2026-10-11T10:00:00+08:00", runDate="2026-10-11", status="FAILED", failureReason="boom", trigger="schedule"), runs)
        write_health("2026-10-11T10:01:00+08:00", runs, out)
        fresh_runs = Path(tempfile.mkdtemp())  # a new CI runner
        self.assertEqual(restore_from_health(out, fresh_runs), 1)
        self.assertEqual(restore_from_health(out, fresh_runs), 0)  # idempotent
        record(RunRecord(runId="r2", startedAt="2026-10-11T14:00:00+08:00", runDate="2026-10-11", status="SUCCESS", trigger="schedule",
                         publication={"result": "PUBLISHED", "release": "x", "gate": None}), fresh_runs)
        last = mark_last_run_failed("release not readable from the public site", "2026-10-11T14:30:00+08:00", fresh_runs, out)
        self.assertEqual(last["runId"], "r2")
        health = json.loads((out / "health.json").read_text(encoding="utf-8"))
        self.assertEqual(health["consecutiveFailures"], 2)
        self.assertEqual(health["lastFailure"]["publication"], "REMOTE_NOT_READABLE")

    def test_prune_orders_by_publication_time_and_never_deletes_the_protected_release(self):
        out = Path(tempfile.mkdtemp())
        for i, name in enumerate(["old1", "old2", "prev", "cur"]):
            d = out / "releases" / name
            d.mkdir(parents=True)
            (d / "release.json").write_text(json.dumps({"publishedAt": f"2026-10-0{i + 1}T10:00:00+08:00"}), encoding="utf-8")
        prune_releases(out, keep=2, current="cur", protect="old1")
        self.assertEqual(sorted(p.name for p in (out / "releases").iterdir()), ["cur", "old1", "prev"])


if __name__ == "__main__":
    unittest.main()
