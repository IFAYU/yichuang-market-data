import json
import shutil
import tempfile
import unittest
from pathlib import Path

from mitw.config import OUT_DIR
from mitw.sync import FILES, SyncRefused, sync, verify_source

HAVE = (OUT_DIR / "manifest.json").exists()


def release_dir(root: Path) -> Path:
    m = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    return root / "releases" / m["release"]


@unittest.skipUnless(HAVE, "no published snapshot in out/")
class SyncTests(unittest.TestCase):
    def setUp(self):
        tmp = Path(tempfile.mkdtemp(prefix="mitw_sync_"))
        self.src, self.dst = tmp / "src", tmp / "app" / "market-data"
        self.src.mkdir()
        shutil.copy(OUT_DIR / "manifest.json", self.src / "manifest.json")
        if (OUT_DIR / "health.json").exists():
            shutil.copy(OUT_DIR / "health.json", self.src / "health.json")
        m = json.loads((self.src / "manifest.json").read_text(encoding="utf-8"))
        shutil.copytree(OUT_DIR / "releases" / m["release"], self.src / "releases" / m["release"])
        self.release = m["release"]

    def test_good_snapshot_is_published_with_the_freshness_contract_in_the_manifest(self):
        m = sync(self.src, self.dst)
        for k in ("schemaVersion", "release", "generatedAt", "sourceFetchedAt", "publishedAt", "syncedAt", "marketAsOf", "financialAsOf", "sourceHash",
                  "files", "companyCount", "freshnessAtPublish", "gate"):
            self.assertIn(k, m)
        self.assertEqual(json.loads((self.dst / "manifest.json").read_text(encoding="utf-8")), m)
        for n in FILES:
            self.assertEqual(m["files"][n]["path"], f"releases/{self.release}/{n}")
            self.assertTrue((self.dst / m["files"][n]["path"]).exists())
        self.assertEqual(len(m["sourceHash"]), 64)
        self.assertEqual(m["release"], m["sourceHash"][:12])
        self.assertEqual(list(self.dst.rglob(".*.tmp")), [])

    def test_health_is_copied_every_time_but_is_not_part_of_the_pinned_release(self):
        sync(self.src, self.dst)
        self.assertTrue((self.dst / "health.json").exists())
        (self.src / "health.json").write_text(json.dumps({"schemaVersion": 1, "marker": "newer"}), encoding="utf-8")
        before = (self.dst / "manifest.json").read_bytes()
        sync(self.src, self.dst)
        self.assertEqual((self.dst / "manifest.json").read_bytes(), before)  # same release: manifest untouched
        self.assertEqual(json.loads((self.dst / "health.json").read_text(encoding="utf-8"))["marker"], "newer")

    def test_syncing_the_same_data_twice_changes_nothing(self):
        first = sync(self.src, self.dst)
        before = {p.relative_to(self.dst).as_posix(): p.read_bytes() for p in self.dst.rglob("*") if p.is_file() and p.name != "health.json"}
        second = sync(self.src, self.dst)
        self.assertEqual(first, second)
        self.assertEqual({p.relative_to(self.dst).as_posix(): p.read_bytes() for p in self.dst.rglob("*") if p.is_file() and p.name != "health.json"}, before)

    def test_hash_mismatch_is_refused_and_the_previous_snapshot_survives(self):
        sync(self.src, self.dst)
        before = {p.relative_to(self.dst).as_posix(): p.read_bytes() for p in self.dst.rglob("*") if p.is_file()}
        p = release_dir(self.src) / "market-snapshot.json"
        p.write_bytes(p.read_bytes() + b" ")  # tamper after publication
        with self.assertRaises(SyncRefused) as cm:
            sync(self.src, self.dst)
        self.assertTrue(any("sha256" in x for x in cm.exception.problems))
        self.assertEqual({p.relative_to(self.dst).as_posix(): p.read_bytes() for p in self.dst.rglob("*") if p.is_file()}, before)

    def test_a_manifest_that_names_a_path_outside_the_release_is_refused(self):
        m = json.loads((self.src / "manifest.json").read_text(encoding="utf-8"))
        m["files"]["market-snapshot.json"]["path"] = "../../etc/passwd"
        (self.src / "manifest.json").write_text(json.dumps(m), encoding="utf-8")
        with self.assertRaises(SyncRefused) as cm:
            verify_source(self.src)
        self.assertTrue(any("unsafe path" in x for x in cm.exception.problems))

    def test_inconsistent_manifest_is_refused(self):
        m = json.loads((self.src / "manifest.json").read_text(encoding="utf-8"))
        m["release"] = "000000000000"
        (self.src / "manifest.json").write_text(json.dumps(m), encoding="utf-8")
        with self.assertRaises(SyncRefused) as cm:
            verify_source(self.src)
        self.assertTrue(any("release" in x for x in cm.exception.problems))

    def test_unsupported_schema_version_is_refused(self):
        m = json.loads((self.src / "manifest.json").read_text(encoding="utf-8"))
        m["schemaVersion"] = "2.0.0"
        (self.src / "manifest.json").write_text(json.dumps(m), encoding="utf-8")
        with self.assertRaises(SyncRefused) as cm:
            verify_source(self.src)
        self.assertTrue(any("unsupported schemaVersion" in x for x in cm.exception.problems))

    def test_missing_file_or_manifest_is_refused_and_nothing_is_written(self):
        (release_dir(self.src) / "industry-snapshots.json").unlink()
        with self.assertRaises(SyncRefused):
            sync(self.src, self.dst)
        self.assertFalse((self.dst / "manifest.json").exists())
        (self.src / "manifest.json").unlink()
        with self.assertRaises(SyncRefused):
            sync(self.src, self.dst)

    def test_corrupt_json_is_refused(self):
        (release_dir(self.src) / "industry-snapshots.json").write_text("{not json", encoding="utf-8")
        with self.assertRaises(SyncRefused):
            sync(self.src, self.dst)

    def test_manifest_is_replaced_last_so_a_failed_copy_never_changes_what_the_app_sees(self):
        first = sync(self.src, self.dst)
        # a different release arrives, but the copy fails half-way: the app must keep serving the previous release
        other = json.loads((self.src / "manifest.json").read_text(encoding="utf-8"))
        new_rel = self.src / "releases" / "aaaaaaaaaaaa"
        shutil.copytree(release_dir(self.src), new_rel)
        for n in FILES:
            (new_rel / n).write_bytes((new_rel / n).read_bytes().replace(b'"companyCount"', b'"companyCount"'))
        other["release"] = "aaaaaaaaaaaa"  # inconsistent with its sourceHash -> refused before anything is copied
        (self.src / "manifest.json").write_text(json.dumps(other), encoding="utf-8")
        with self.assertRaises(SyncRefused):
            sync(self.src, self.dst)
        self.assertEqual(json.loads((self.dst / "manifest.json").read_text(encoding="utf-8")), first)

    def test_old_releases_in_the_target_are_pruned_but_the_current_one_never(self):
        sync(self.src, self.dst)
        for i in range(6):
            d = self.dst / "releases" / f"old{i}"
            d.mkdir(parents=True)
            (d / "x.json").write_text("{}")
        m = json.loads((self.src / "manifest.json").read_text(encoding="utf-8"))
        (self.dst / "manifest.json").unlink()  # force a real (non-idempotent) sync
        sync(self.src, self.dst)
        names = sorted(p.name for p in (self.dst / "releases").iterdir())
        self.assertIn(m["release"], names)
        self.assertLessEqual(len(names), 3)


if __name__ == "__main__":
    unittest.main()
