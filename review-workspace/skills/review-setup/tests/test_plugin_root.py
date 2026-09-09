"""Plugin-packaging contract: the review root is never inferred outside the in-repo layout."""
from pathlib import Path
import sys
import unittest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import review_common  # noqa: E402


class PluginRootDiscoveryTests(unittest.TestCase):
    def test_missing_root_is_refused_when_installed_from_a_plugin(self):
        # This test tree lives at <plugin>/skills/review-setup/scripts, not
        # <review-root>/.claude/skills/review-setup/scripts, so inference must refuse.
        self.assertNotEqual(SCRIPTS.parents[2].name, ".claude")
        with self.assertRaises(review_common.ReviewError) as raised:
            review_common.discover_root(None)
        self.assertIn("--root is required", str(raised.exception))

    def test_explicit_root_still_validates_the_directory(self):
        with self.assertRaises(review_common.ReviewError) as raised:
            review_common.discover_root(str(Path(__file__).resolve().parent / "does-not-exist"))
        self.assertNotIn("--root is required", str(raised.exception))

    def test_documentation_states_root_is_required(self):
        text = (SCRIPTS.parent / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("--root <review-repo-root>", text)
        self.assertNotIn("/workspace/.claude/skills/review-setup", text)


if __name__ == "__main__":
    unittest.main()
