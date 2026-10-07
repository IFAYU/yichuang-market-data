"""Phase 3I.2: safe data-branch publication (concurrent remote movement) and daily retention. Local bare repositories only; no network."""
import json
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from mitw.gitpublish import PublishRace, publish_paths
from mitw.retention import plan, prune_daily

TAIPEI = timezone(timedelta(hours=8))


def git(repo, *a):
    r = subprocess.run(["git", "-C", str(repo), *a], capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0, (a, r.stderr)
    return r.stdout.strip()


class DataBranch(unittest.TestCase):
    def setUp(self):
        self.t = Path(tempfile.mkdtemp(prefix="mitw_gp_"))
        self.bare = self.t / "remote.git"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "data", str(self.bare)], check=True)
        self.a, self.b = self.t / "a", self.t / "b"
        subprocess.run(["git", "clone", "-q", str(self.bare), str(self.a)], check=True, capture_output=True)
        for k, v in (("user.name", "t"), ("user.email", "t@example.test")):
            git(self.a, "config", k, v)
        git(self.a, "checkout", "-q", "-B", "data")
        (self.a / "manifest.json").write_text('{"release":"old"}', encoding="utf-8")
        (self.a / "releases" / "old").mkdir(parents=True)
        (self.a / "releases" / "old" / "m.json").write_text("{}", encoding="utf-8")
        git(self.a, "add", "--", "manifest.json", "releases/old/m.json")
        git(self.a, "commit", "-q", "-m", "seed")
        git(self.a, "push", "-q", "origin", "data")
        subprocess.run(["git", "clone", "-q", "-b", "data", str(self.bare), str(self.b)], check=True, capture_output=True)
        for k, v in (("user.name", "t"), ("user.email", "t@example.test")):
            git(self.b, "config", k, v)

    def remote_head(self):
        return git(self.bare, "rev-parse", "data")

    def test_T_a_disjoint_remote_move_is_reconciled_with_a_rebase_and_a_fast_forward_push(self):
        (self.b / "emerging" / "releases" / "e1").mkdir(parents=True)                  # the other workflow pushes its own namespace first
        (self.b / "emerging" / "releases" / "e1" / "s.json").write_text("{}", encoding="utf-8")
        git(self.b, "add", "--", "emerging/releases/e1/s.json")
        git(self.b, "commit", "-q", "-m", "emerging")
        git(self.b, "push", "-q", "origin", "data")
        (self.a / "releases" / "new").mkdir()                                          # ours was prepared before we saw that
        (self.a / "releases" / "new" / "m.json").write_text('{"x":1}', encoding="utf-8")
        out = publish_paths(self.a, "data", ["releases/new"], "release new", guard=["manifest.json", "health.json", "releases/"])
        self.assertEqual((out["pushed"], out["rebased"] or out["attempts"] >= 1), (True, True))
        self.assertEqual(self.remote_head(), git(self.a, "rev-parse", "HEAD"))
        names = git(self.bare, "ls-tree", "-r", "--name-only", "data").splitlines()
        self.assertIn("emerging/releases/e1/s.json", names)                            # the other workflow's work is preserved
        self.assertIn("releases/new/m.json", names)
        self.assertEqual(git(self.bare, "log", "--format=%s", "-3").splitlines()[0], "release new")   # a normal history, no merge, no force

    def test_T_unstaged_pointer_files_survive_a_reconciling_rebase(self):
        (self.b / "emerging" / "releases" / "e2").mkdir(parents=True)
        (self.b / "emerging" / "releases" / "e2" / "s.json").write_text("{}", encoding="utf-8")
        git(self.b, "add", "--", "emerging/releases/e2/s.json")
        git(self.b, "commit", "-q", "-m", "emerging")
        git(self.b, "push", "-q", "origin", "data")
        (self.a / "releases" / "n").mkdir()
        (self.a / "releases" / "n" / "m.json").write_text("{}", encoding="utf-8")
        (self.a / "manifest.json").write_text('{"release":"n"}', encoding="utf-8")   # the manifest is modified but NOT part of this push (release first, manifest last)
        out = publish_paths(self.a, "data", ["releases"], "release n", guard=["manifest.json", "health.json", "releases/"])
        self.assertTrue(out["pushed"])
        self.assertEqual((self.a / "manifest.json").read_text(encoding="utf-8"), '{"release":"n"}')           # still there, still unpublished
        self.assertNotIn('"n"', git(self.bare, "show", "data:manifest.json"))                                   # the live manifest did not move

    def test_T_a_remote_move_inside_our_namespace_fails_closed_and_pushes_nothing(self):
        (self.b / "manifest.json").write_text('{"release":"someone else"}', encoding="utf-8")
        git(self.b, "add", "--", "manifest.json")
        git(self.b, "commit", "-q", "-m", "other main publication")
        git(self.b, "push", "-q", "origin", "data")
        remote_before = self.remote_head()
        (self.a / "manifest.json").write_text('{"release":"mine"}', encoding="utf-8")
        with self.assertRaises(PublishRace) as e:
            publish_paths(self.a, "data", ["manifest.json"], "publish mine", guard=["manifest.json", "health.json", "releases/"])
        self.assertIn("this publisher owns", str(e.exception))
        self.assertEqual(self.remote_head(), remote_before)

    def test_only_explicit_paths_are_staged_and_other_changes_stay_untouched(self):
        (self.a / "releases" / "new").mkdir()
        (self.a / "releases" / "new" / "m.json").write_text("{}", encoding="utf-8")
        (self.a / "stray.txt").write_text("not mine", encoding="utf-8")
        publish_paths(self.a, "data", ["releases/new"], "release new", guard=["releases/"])
        self.assertNotIn("stray.txt", git(self.bare, "ls-tree", "-r", "--name-only", "data"))
        self.assertTrue((self.a / "stray.txt").exists())

    def test_nothing_to_commit_is_not_a_push_and_wildcards_are_refused(self):
        self.assertEqual(publish_paths(self.a, "data", ["manifest.json"], "x", guard=["manifest.json"])["pushed"], False)
        for bad in (".", "*", "-A", ""):
            with self.assertRaises(ValueError):
                publish_paths(self.a, "data", [bad], "x", guard=["x/"])

    def test_a_behind_local_branch_is_fast_forwarded_and_a_diverged_one_is_a_hard_stop(self):
        (self.b / "health.json").write_text("{}", encoding="utf-8")
        git(self.b, "add", "--", "health.json")
        git(self.b, "commit", "-q", "-m", "health")
        git(self.b, "push", "-q", "origin", "data")
        (self.a / "releases" / "n1").mkdir()
        (self.a / "releases" / "n1" / "m.json").write_text("{}", encoding="utf-8")
        self.assertTrue(publish_paths(self.a, "data", ["releases/n1"], "n1", guard=["releases/"])["pushed"])      # local was BEHIND: fast-forwarded first
        (self.a / "local.txt").write_text("x", encoding="utf-8")                                                  # now make the local branch diverge
        git(self.a, "add", "--", "local.txt")
        git(self.a, "commit", "-q", "-m", "local only")
        (self.b / "other.txt").write_text("y", encoding="utf-8")
        git(self.b, "pull", "-q", "--rebase", "origin", "data")
        git(self.b, "add", "--", "other.txt")
        git(self.b, "commit", "-q", "-m", "remote only")
        git(self.b, "push", "-q", "origin", "data")
        (self.a / "releases" / "n2").mkdir()
        (self.a / "releases" / "n2" / "m.json").write_text("{}", encoding="utf-8")
        with self.assertRaises(PublishRace):
            publish_paths(self.a, "data", ["releases/n2"], "n2", guard=["releases/"])


NOW = datetime(2026, 10, 8, 22, 0, tzinfo=TAIPEI)


def rel(days_ago, extra_hours=0):
    return NOW - timedelta(days=days_ago, hours=extra_hours)


class Retention(unittest.TestCase):
    def test_U_everything_in_30_days_plus_one_per_earlier_week_for_12_weeks(self):
        releases = {f"d{n}": rel(n) for n in range(0, 31)}                          # 31 daily releases: all kept
        releases.update({f"w{n}a": rel(n) for n in range(31, 31 + 7 * 14)})        # 14 weeks of older daily releases
        keep, delete = plan(releases, NOW)
        for n in range(0, 31):
            self.assertIn(f"d{n}", keep)
        older = [k for k in keep if k.startswith("w")]
        self.assertLessEqual(len(older), 13)                                         # about one per week, never every day
        self.assertGreaterEqual(len(older), 11)
        self.assertTrue(delete)
        self.assertFalse({k for k in delete if k.startswith("d")})
        # nothing older than 30 days + 12 weeks survives
        self.assertTrue(all((NOW - releases[k]).days <= 30 + 84 + 7 for k in keep))

    def test_U_the_current_release_and_the_rollback_target_are_never_deleted_even_when_ancient(self):
        releases = {"current": rel(400), "previous": rel(500), "recent": rel(1), "old": rel(300)}
        keep, delete = plan(releases, NOW, protect={"current", "previous"})
        self.assertEqual((keep, delete), ({"current", "previous", "recent"}, {"old"}))

    def test_a_release_whose_time_cannot_be_read_is_kept(self):
        keep, delete = plan({"mystery": None, "old": rel(400)}, NOW)
        self.assertEqual((keep, delete), ({"mystery"}, {"old"}))

    def test_prune_daily_deletes_directories_and_spares_protected_and_unreadable_ones(self):
        d = Path(tempfile.mkdtemp(prefix="mitw_rt_")) / "releases"
        def mk(name, when):
            (d / name).mkdir(parents=True)
            (d / name / "market-snapshot.json").write_text("{}", encoding="utf-8")
            if when is not None:
                (d / name / "release.json").write_text(json.dumps({"publishedAt": when.isoformat(timespec="seconds")}), encoding="utf-8")
        mk("cur", rel(1)); mk("prev", rel(2)); mk("anc", rel(400)); mk("ancient-current", rel(500)); mk("broken", None)
        gone = prune_daily(d, NOW, protect={"cur", "prev", "ancient-current"})
        self.assertEqual(gone, ["anc"])
        self.assertEqual(sorted(p.name for p in d.iterdir()), ["ancient-current", "broken", "cur", "prev"])

    def test_the_manifest_target_cannot_be_selected_by_the_daily_publisher(self):
        # publish_daily protects {new release, previous release}: both are in `protect`, so a prune right after publication can never delete either
        import inspect
        from mitw.snapshot import daily_publish
        self.assertIn("protect={release,", inspect.getsource(daily_publish.publish_daily).replace(" ", ""))


if __name__ == "__main__":
    unittest.main()
