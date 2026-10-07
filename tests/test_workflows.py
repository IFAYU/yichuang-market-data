"""Phase 3I.2: the new daily workflows are MANUAL ONLY, and the legacy weekly workflow is byte-identical to its pre-phase state (still the active scheduled fallback)."""
import hashlib
import re
import unittest
from pathlib import Path

W = Path(__file__).resolve().parent.parent / ".github" / "workflows"
WEEKLY_SHA256 = "dcb1041151617b1fd7f4eae2bef77d4bc1af89ce657e811fff6f3adf18ef30a3"  # weekly-publish.yml with LF line endings (the form git stores and GitHub checks out; a Windows working copy has CRLF)
NEW = ("weekday-market-publish.yml", "weekday-emerging-publish.yml")


def code_lines(name):
    return [ln for ln in (W / name).read_text(encoding="utf-8").splitlines() if not ln.lstrip().startswith("#")]


class Workflows(unittest.TestCase):
    def test_the_new_workflows_have_no_schedule_and_no_cron(self):
        for n in NEW:
            text = "\n".join(code_lines(n))
            self.assertNotRegex(text, r"(?m)^\s*schedule\s*:", n)
            self.assertNotRegex(text, r"cron\s*:", n)
            self.assertNotIn("* * *", text, n)
            self.assertRegex(text, r"(?m)^on:\s*\n\s+workflow_dispatch:", n)

    def test_only_manual_triggers_exist(self):
        for n in NEW:
            on_block = re.search(r"(?ms)^on:\n(.*?)^\S", "\n".join(code_lines(n)) + "\nX")
            keys = re.findall(r"(?m)^  ([a-z_]+):", on_block.group(1))
            self.assertEqual(keys, ["workflow_dispatch"], n)

    def test_the_legacy_weekly_workflow_is_unchanged_and_still_scheduled(self):
        raw = (W / "weekly-publish.yml").read_bytes().replace(b"
", b"
")   # line endings are not content
        self.assertEqual(hashlib.sha256(raw).hexdigest(), WEEKLY_SHA256)
        self.assertIn(b'cron: "0 2 * * 0"', raw)

    def test_the_two_publishers_use_different_concurrency_groups_and_never_force_push(self):
        groups = []
        for n in NEW:
            text = (W / n).read_text(encoding="utf-8")
            groups.append(re.search(r"group:\s*(\S+)", text).group(1))
            self.assertNotRegex("\n".join(code_lines(n)), r"--force(?!-market)|push -f|git add -A|git add \.", n)
            self.assertIn("push-paths", text, n)
            self.assertIn("cancel-in-progress: false", text, n)
        self.assertEqual(len(set(groups)), 2)

    def test_manifest_last_ordering_in_both_publishers(self):
        m = (W / "weekday-market-publish.yml").read_text(encoding="utf-8")
        self.assertLess(m.index("--path releases"), m.index("verify-remote"))
        self.assertLess(m.index("verify-remote"), m.index("--path manifest.json"))
        e = (W / "weekday-emerging-publish.yml").read_text(encoding="utf-8")
        self.assertLess(e.index("--path emerging/releases"), e.index("verify_emerging_public.py"))
        self.assertLess(e.index("verify_emerging_public.py"), e.index("--path emerging/manifest.json"))

    def test_syntax_when_a_yaml_parser_is_available(self):
        try:
            import yaml
        except ImportError:
            self.skipTest("PyYAML is not part of requirements.txt; the text checks above still run")
        for n in NEW + ("weekly-publish.yml",):
            doc = yaml.safe_load((W / n).read_text(encoding="utf-8"))
            self.assertIn("jobs", doc, n)
            on = doc.get("on", doc.get(True))
            if n in NEW:
                self.assertEqual(list(on), ["workflow_dispatch"], n)


if __name__ == "__main__":
    unittest.main()
