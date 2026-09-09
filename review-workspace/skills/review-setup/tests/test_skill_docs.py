"""Contract tests for review-setup operator documentation."""
from pathlib import Path
import re
import unittest


SKILL = Path(__file__).resolve().parents[1] / "SKILL.md"


class SkillDocumentationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = SKILL.read_text(encoding="utf-8")

    def test_explicit_cleanup_and_million_default_contract(self):
        import inspect
        import sys
        from unittest.mock import patch
        import contextlib
        import io
        sys.path.insert(0, str(SKILL.parent / "scripts"))
        import review_common
        import review_setup
        for function in (review_common.bounded_fetch, review_setup.setup_run,
                         review_setup.prepare_source, review_setup.resolve_base):
            self.assertEqual(inspect.signature(function).parameters["max_depth"].default, 1000000)
        fake = {"key": "nova-42", "slot": "Nova-0", "slot_path": ".worktrees/Nova-0",
                "branch": "review/nova-42", "submodule": {"path": "repos/Nova"},
                "snapshot": {"head_sha": "a" * 40}, "complete": True, "stale": False}
        with patch.object(review_setup, "discover_root", return_value=Path("/fixture")), \
             patch.object(review_setup, "preflight", return_value={}), \
             patch.object(review_setup, "setup_run", return_value=fake) as setup, \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(review_setup.main(["42"]), 0)
            self.assertEqual(setup.call_args.kwargs["max_depth"], 1000000)
            self.assertEqual(review_setup.main(["42", "--max-depth", "200"]), 0)
            self.assertEqual(setup.call_args.kwargs["max_depth"], 200)
            self.assertEqual(review_setup.main(["--preflight"]), 0)
        self.assertIn("`1000000`", self.text)
        self.assertIn("Destructive on every explicit pooled PR setup", self.text)
        self.assertIn("ignored files", self.text)
        self.assertIn("same-PR rerun", self.text)
        self.assertIn("Warm-only commands, status and dry-run remain non-destructive", self.text)

    def test_long_running_setup_warning_is_preserved(self):
        warning = (
            "**Allow substantial time: review setup can take as much as 400 seconds (6 minutes\n"
            "40 seconds), especially when initializing or fetching source repositories.** This is\n"
            "not a guaranteed upper bound. Use an execution timeout longer than 400 seconds where\n"
            "supported. A tool/client timeout does not prove the underlying setup or Git processes\n"
            "have stopped: check process and slot recovery state before retrying. Do not launch a\n"
            "duplicate setup while the original run is still active."
        )
        self.assertIn(warning, self.text)

    def test_capacity_is_desired_not_claimed_ready(self):
        # Plugin packaging: the repository list comes from the review repo's .gitmodules,
        # not from a literal list in this document.
        self.assertIn("configured in the review repo's `.gitmodules`", self.text)
        for absent in ("Astra", "WeveNova", "MODISService"):
            self.assertNotIn(absent, self.text)
        for example in ("`Nova-0`", "`NovaClient-1`"):
            self.assertIn(example, self.text)
        self.assertIn("six slots (`0`–`5`)", self.text)
        self.assertIn("desired capacity of 30 slots", self.text)
        self.assertIn("not a\nclaim that all 30 are currently created, warm, or ready", self.text)

        normalized = " ".join(self.text.lower().split())
        contradictory_claims = (
            r"\ball 30 (?:slots )?(?:are|have been) (?:ready|warm|warmed|validated|verified|available|created)\b",
            r"\b30 (?:slots )?(?:are|have been) (?:ready|warm|warmed|validated|verified|available|created)\b",
            r"\b(?:ready|warm|warmed|validated|verified|available|created) all 30 (?:slots)?\b",
            r"\bfull 30-slot (?:pool|capacity) (?:is|has been) (?:ready|warm|warmed|validated|verified|available|created)\b",
        )
        for claim in contradictory_claims:
            self.assertIsNone(
                re.search(claim, normalized),
                f"SKILL.md makes an unverified 30-slot readiness claim: {claim}",
            )

    def test_task_3_warm_and_status_commands_are_documented(self):
        commands = (
            "review_setup.py --status",
            "review_setup.py --warm --repo Nova --slot 1 --dry-run",
            "review_setup.py --warm --repo Nova --slot 1",
            "review_setup.py --warm-all --dry-run",
            "review_setup.py --warm-all",
        )
        for command in commands:
            self.assertIn(command, self.text)
        self.assertIn("does not start 30 concurrent downloads", self.text)
        for state in ("`absent`", "`warm`", "`occupied`", "`blocked`"):
            self.assertIn(state, self.text)

    def test_partial_and_warmup_disk_limits_are_documented(self):
        self.assertIn("pool `partial` state", self.text)
        self.assertIn("2 GiB", self.text)
        self.assertIn("review_reset.py --slot Nova-2", self.text)
        self.assertIn("review_reset.py --legacy-slot 2", self.text)
        self.assertIn("setup exit code `2`", self.text)
        self.assertIn("source-object and working-tree filesystems", self.text)
        self.assertIn("when those are distinct", self.text)
        self.assertIn("stop safely before disk exhaustion", self.text)

    def test_shared_store_and_gitlink_delta_are_explained(self):
        self.assertIn("`review-setup/source-stores/{repo-key}.git`", self.text)
        self.assertIn("independent working files, indexes, and detached HEADs", self.text)
        self.assertIn("warm source HEAD may intentionally differ", self.text)
        self.assertIn("outer NOVA store's recorded\ngitlink", self.text)

    def test_legacy_slots_are_explicitly_separate(self):
        self.assertIn("Legacy schema-1 numeric slots remain separate and unchanged", self.text)
        self.assertIn("does not implicitly map", self.text)
        self.assertIn("explicit legacy selector", self.text)

    def test_reset_fallback_and_non_ready_states_are_explained(self):
        self.assertIn("bounded refresh", self.text)
        self.assertIn("verified\ncached baseline", self.text)
        self.assertIn("recoverably blocked", self.text)
        for state in ("occupied", "absent"):
            self.assertIn(state, self.text)

    def test_maintenance_safety_contract_is_documented(self):
        required = (
            "at most once per 24 hours",
            "object-store filesystem headroom",
            "normal two-week pruning grace",
            "must not use aggressive GC",
            "immediate pruning",
            "reflog expiry",
            "worktree\nmetadata pruning",
        )
        for phrase in required:
            self.assertIn(phrase, self.text)


if __name__ == "__main__":
    unittest.main()
