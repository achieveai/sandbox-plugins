"""Focused regression coverage for the Task 1 provider-neutral contract
shapes in review_common.py. Full contract-shape coverage is authored later
in Task 8; this file currently covers only the GitTransportAuth fail-closed
binding behavior fixed in review round 1."""
from __future__ import annotations

from pathlib import Path
import sys
import unittest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import review_common as common


class GitTransportAuthBindingTests(unittest.TestCase):
    def test_unbound_apply_fails_closed_without_leaking_the_token(self):
        auth = common.GitTransportAuth.bearer("super-secret-token")
        with self.assertRaises(common.ReviewError) as ctx:
            auth.apply({"PATH": "/usr/bin"})
        message = str(ctx.exception)
        self.assertNotIn("super-secret-token", message)
        self.assertNotIn("super-secret-token", repr(auth))
        self.assertNotIn("super-secret-token", str(auth))
        # The error must be actionable, not just "no".
        self.assertIn("_bound_to", message)

    def test_bound_apply_is_url_scoped_and_preserves_inherited_entries(self):
        auth = common.GitTransportAuth.bearer("super-secret-token")
        bound = auth._bound_to("https://dev.azure.com/org/proj/_git/Nova/")

        env = bound.apply({
            "PATH": "/usr/bin",
            "GIT_CONFIG_COUNT": "2",
            "GIT_CONFIG_KEY_0": "existing.key.0",
            "GIT_CONFIG_VALUE_0": "existing-value-0",
            "GIT_CONFIG_KEY_1": "existing.key.1",
            "GIT_CONFIG_VALUE_1": "existing-value-1",
        })

        # Inherited entries are preserved verbatim, never overwritten.
        self.assertEqual(env["GIT_CONFIG_KEY_0"], "existing.key.0")
        self.assertEqual(env["GIT_CONFIG_VALUE_0"], "existing-value-0")
        self.assertEqual(env["GIT_CONFIG_KEY_1"], "existing.key.1")
        self.assertEqual(env["GIT_CONFIG_VALUE_1"], "existing-value-1")

        # New entry is appended at the next index and is URL-scoped.
        self.assertEqual(env["GIT_CONFIG_COUNT"], "3")
        self.assertEqual(
            env["GIT_CONFIG_KEY_2"], "http.https://dev.azure.com/org/proj/_git/Nova.extraHeader"
        )
        self.assertEqual(env["GIT_CONFIG_VALUE_2"], "AUTHORIZATION: bearer super-secret-token")

        # Original mapping passed in is not mutated.
        self.assertNotIn("GIT_CONFIG_KEY_2", {"GIT_CONFIG_KEY_0", "GIT_CONFIG_KEY_1"})

        # No secret-shaped material anywhere except the one intended header value.
        self.assertNotIn("super-secret-token", repr(bound))
        self.assertNotIn("super-secret-token", str(bound))
        self.assertEqual(bound.git_arguments(), [])

    def test_apply_does_not_mutate_the_input_mapping(self):
        auth = common.GitTransportAuth.bearer("token")._bound_to("https://example.com/repo")
        original = {"GIT_CONFIG_COUNT": "1"}
        auth.apply(original)
        self.assertEqual(original, {"GIT_CONFIG_COUNT": "1"})


if __name__ == "__main__":
    unittest.main()
