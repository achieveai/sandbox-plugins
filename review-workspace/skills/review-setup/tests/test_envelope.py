"""Result-envelope, run-id and provider-seam contract tests for the review-setup CLIs.

These pin the machine-readable surface the review daemon consumes: exactly one envelope
object on stdout under ``--json``, a faithful exit-code -> status mapping, the daemon's
run identity echoed verbatim, and a GitHub provider that refuses before it can mutate
anything.
"""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import unittest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
SPEC = importlib.util.find_spec("review_common")
if SPEC:
    import review_common as common

# Imported as a module, not by name: importing the TestCase class into this namespace
# would make unittest discovery collect and re-run test_lifecycle's whole suite here.
import test_lifecycle  # noqa: E402

git = test_lifecycle.git

RUN_ID = "crd-run1-42-0"


def invoke(module, argv):
    """Run a script's main() in-process, returning (code, stdout, stderr)."""
    stdout, stderr = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        try:
            code = module.main(argv)
        except SystemExit as exc:
            code = exc.code
    return code, stdout.getvalue(), stderr.getvalue()


def parse_envelope(test, stdout):
    """The whole of stdout must be exactly one envelope object on one line."""
    lines = [line for line in stdout.splitlines() if line.strip()]
    test.assertEqual(len(lines), 1, msg=f"expected exactly one stdout line, saw: {stdout!r}")
    envelope = json.loads(lines[0])
    test.assertEqual(envelope["schemaVersion"], 1)
    test.assertEqual(
        set(envelope),
        {"schemaVersion", "status", "runId", "operation", "artifacts", "effects"},
    )
    return envelope


def effect_types(envelope):
    return [effect["type"] for effect in envelope["effects"]]


@unittest.skipUnless(SPEC, "runtime not implemented yet")
class EnvelopeArgumentTests(unittest.TestCase):
    """Grammar and pairing rules that must be rejected before anything runs."""

    def setUp(self):
        import review_reset
        import review_setup

        self.modules = {"review_setup": review_setup, "review_reset": review_reset}

    def base_argv(self, name):
        return ["--status"] if name == "review_setup" else ["--slot", "Nova-0"]

    def test_json_without_run_id_is_an_argparse_error(self):
        for name, module in self.modules.items():
            with self.subTest(script=name):
                code, out, err = invoke(module, [*self.base_argv(name), "--json"])
                self.assertEqual(code, 2)
                self.assertEqual(out, "")
                self.assertIn("--run-id", err)

    def test_run_id_grammar_is_enforced(self):
        bad = ["", "-leading-dash", "has space", "has/slash", "x" * 129, "unicodeé"]
        for name, module in self.modules.items():
            for value in bad:
                with self.subTest(script=name, run_id=value):
                    code, out, _ = invoke(
                        module, [*self.base_argv(name), "--json", "--run-id", value]
                    )
                    self.assertEqual(code, 2)
                    self.assertEqual(out, "")

    def test_longest_legal_run_id_is_accepted(self):
        import review_envelope

        self.assertIsNotNone(review_envelope.RUN_ID_PATTERN.fullmatch("a" + "b" * 127))
        self.assertIsNone(review_envelope.RUN_ID_PATTERN.fullmatch("a" + "b" * 128))


@unittest.skipUnless(SPEC, "runtime not implemented yet")
class EnvelopeModeTests(unittest.TestCase):
    """One envelope per mode and per exit code, over a real fixture review repo."""

    def setUp(self):
        test_lifecycle.GitLifecycleTests.setUp(self)
        import review_reset
        import review_setup

        self.review_setup = review_setup
        self.review_reset = review_reset

    def fixture_remote(self):
        from unittest.mock import patch

        return patch.dict(
            os.environ,
            {
                "GIT_CONFIG_COUNT": "2",
                "GIT_CONFIG_KEY_0": f"url.{self.source.as_uri()}.insteadOf",
                "GIT_CONFIG_VALUE_0": self.module["url"],
                "GIT_CONFIG_KEY_1": "protocol.file.allow",
                "GIT_CONFIG_VALUE_1": "always",
            },
        )

    def setup_env(self, argv, *, complete=True, stale=False, snapshot=None):
        """Drive a full PR setup through the CLI with a faked provider."""
        from unittest.mock import patch

        class Client:
            org_url = "https://dev.azure.com/org"

        report = {
            "complete": complete,
            "warnings": [],
            "work_items": {"roots": [], "nodes": [], "complete": complete},
            "files": [],
            "provenance": {"commits": {}, "pull_requests": []},
        }
        resolved = [snapshot or self.snapshot, snapshot or self.snapshot]
        if stale:
            moved = dict(self.snapshot, snapshot_fingerprint="snapshot-2")
            resolved = [self.snapshot, moved]
        import review_provider

        with self.fixture_remote(), patch.object(
            review_provider.AdoProvider, "make_client", return_value=Client()
        ), patch("review_context.resolve_pr", side_effect=resolved), patch(
            "review_setup._collect", return_value=report
        ), patch(
            "review_context.render_markdown", return_value="# Setup\n"
        ):
            return invoke(self.review_setup, ["--root", str(self.root), *argv])

    def test_status_envelope_is_a_no_op_with_one_slot_status_per_row(self):
        code, out, _ = invoke(
            self.review_setup, ["--root", str(self.root), "--status", "--json", "--run-id", RUN_ID]
        )
        envelope = parse_envelope(self, out)
        self.assertEqual(code, 0)
        self.assertEqual(envelope["status"], "no_op")
        self.assertEqual(envelope["operation"], "status")
        self.assertEqual(envelope["runId"], RUN_ID)
        self.assertEqual(set(effect_types(envelope)), {"slot_status"})
        self.assertEqual(len(envelope["effects"]), 6)
        self.assertEqual(
            [effect["slot"] for effect in envelope["effects"]],
            [f"Nova-{index}" for index in range(6)],
        )

    def test_preflight_envelope_is_a_no_op(self):
        code, out, _ = invoke(
            self.review_setup, ["--preflight", "--json", "--run-id", RUN_ID]
        )
        envelope = parse_envelope(self, out)
        self.assertEqual(code, 0)
        self.assertEqual(envelope["status"], "no_op")
        self.assertEqual(envelope["operation"], "preflight")
        self.assertEqual(effect_types(envelope), ["preflight"])

    def test_warm_dry_run_envelope_is_a_no_op_and_warm_completes(self):
        from unittest.mock import patch

        with self.fixture_remote(), patch.object(
            self.review_setup, "preflight", return_value={"python": "x", "git": "x", "curl": "x"}
        ):
            code, out, _ = invoke(
                self.review_setup,
                ["--root", str(self.root), "--warm", "--repo", "Nova", "--slot", "0",
                 "--dry-run", "--json", "--run-id", RUN_ID],
            )
            envelope = parse_envelope(self, out)
            self.assertEqual((code, envelope["status"], envelope["operation"]), (0, "no_op", "warm"))
            self.assertEqual(effect_types(envelope), ["slot_status"])

            code, out, _ = invoke(
                self.review_setup,
                ["--root", str(self.root), "--warm", "--repo", "Nova", "--slot", "0",
                 "--json", "--run-id", RUN_ID],
            )
            envelope = parse_envelope(self, out)
            self.assertEqual((code, envelope["status"]), (0, "completed"))

    def test_warm_all_dry_run_envelope_is_a_no_op(self):
        code, out, _ = invoke(
            self.review_setup,
            ["--root", str(self.root), "--warm-all", "--dry-run", "--json", "--run-id", RUN_ID],
        )
        envelope = parse_envelope(self, out)
        self.assertEqual((code, envelope["status"], envelope["operation"]), (0, "no_op", "warm_all"))
        self.assertEqual(set(effect_types(envelope)), {"slot_status"})

    def test_setup_exit_zero_is_completed_with_setup_complete_details(self):
        code, out, _ = self.setup_env(["42", "--slot", "0", "--json", "--run-id", RUN_ID])
        envelope = parse_envelope(self, out)
        self.assertEqual((code, envelope["status"], envelope["operation"]), (0, "completed", "setup"))
        self.assertEqual(envelope["runId"], RUN_ID)
        complete = next(e for e in envelope["effects"] if e["type"] == "setup_complete")
        self.assertEqual(complete["head_sha"], self.head)
        self.assertEqual(complete["base_sha"], self.base)
        self.assertEqual(complete["slot_path"], ".worktrees/Nova-0")
        self.assertEqual(complete["source_path"], ".worktrees/Nova-0/repos/Nova")
        self.assertEqual(complete["branch"], "review/nova-42")
        self.assertEqual(complete["pr_dir"], "PRs/nova-42")
        script_run_id = next(e for e in envelope["effects"] if e["type"] == "script_run_id")
        self.assertNotEqual(script_run_id["detail"], RUN_ID)
        reports = [a for a in envelope["artifacts"] if a["kind"] == "report"]
        self.assertTrue(reports)
        for artifact in reports:
            self.assertTrue(artifact["path"].startswith(".worktrees/Nova-0/PRs/nova-42/"))
            self.assertRegex(artifact["sha256"], r"^[0-9a-f]{64}$")
            self.assertTrue((self.root / artifact["path"]).is_file())

    def test_setup_exit_two_is_completed_with_partial_context(self):
        code, out, _ = self.setup_env(
            ["42", "--slot", "0", "--json", "--run-id", RUN_ID], complete=False
        )
        envelope = parse_envelope(self, out)
        self.assertEqual((code, envelope["status"]), (2, "completed"))
        partial = next(e for e in envelope["effects"] if e["type"] == "partial_context")
        self.assertEqual(partial["slot_path"], ".worktrees/Nova-0")
        self.assertEqual(partial["head_sha"], self.head)

    def test_setup_exit_three_is_blocked_with_stale_snapshot(self):
        code, out, _ = self.setup_env(
            ["42", "--slot", "0", "--json", "--run-id", RUN_ID], stale=True
        )
        envelope = parse_envelope(self, out)
        self.assertEqual((code, envelope["status"]), (3, "blocked"))
        self.assertIn("stale_snapshot", effect_types(envelope))

    def test_lock_contention_is_retryable(self):
        from unittest.mock import patch

        import review_provider

        class Client:
            org_url = "https://dev.azure.com/org"

        state = common.ReviewState(self.root)
        # The provider work happens before the allocation lock, so it must succeed for the
        # contention itself to be the refusal under test.
        with self.fixture_remote(), patch.object(
            review_provider.AdoProvider, "make_client", return_value=Client()
        ), patch("review_context.resolve_pr", return_value=self.snapshot), state.lock():
            code, out, _ = invoke(
                self.review_setup,
                ["--root", str(self.root), "42", "--slot", "0", "--json", "--run-id", RUN_ID],
            )
        envelope = parse_envelope(self, out)
        self.assertEqual((code, envelope["status"]), (1, "retryable"))
        self.assertIn("lock_contention", effect_types(envelope))

    def test_reset_dry_run_envelope_is_a_no_op(self):
        self.setup_env(["42", "--slot", "0"])
        code, out, _ = invoke(
            self.review_reset,
            ["--root", str(self.root), "--slot", "Nova-0", "--dry-run",
             "--json", "--run-id", RUN_ID],
        )
        envelope = parse_envelope(self, out)
        self.assertEqual((code, envelope["status"], envelope["operation"]), (0, "no_op", "reset"))
        self.assertEqual(envelope["runId"], RUN_ID)

    def test_reset_envelope_completes(self):
        self.setup_env(["42", "--slot", "0"])
        code, out, _ = invoke(
            self.review_reset,
            ["--root", str(self.root), "--slot", "Nova-0", "--json", "--run-id", RUN_ID],
        )
        envelope = parse_envelope(self, out)
        self.assertEqual((code, envelope["status"]), (0, "completed"))
        self.assertIn("reset_complete", effect_types(envelope))

    def test_reset_error_is_failed(self):
        code, out, _ = invoke(
            self.review_reset,
            ["--root", str(self.root), "--slot", "Nova-9", "--json", "--run-id", RUN_ID],
        )
        envelope = parse_envelope(self, out)
        self.assertEqual((code, envelope["status"]), (1, "failed"))
        self.assertIn("error", effect_types(envelope))

    def test_human_output_is_unchanged_without_json(self):
        code, out, _ = invoke(self.review_setup, ["--root", str(self.root), "--status"])
        self.assertEqual(code, 0)
        self.assertIn("Nova-0", out)
        rows = json.loads(out)
        self.assertEqual([row["slot"] for row in rows], [f"Nova-{index}" for index in range(6)])
        self.assertNotIn("schemaVersion", out)


@unittest.skipUnless(SPEC, "runtime not implemented yet")
class GitHubProviderStubTests(unittest.TestCase):
    """The GitHub selection is a visible refusal, never a partial setup."""

    def setUp(self):
        test_lifecycle.GitLifecycleTests.setUp(self)
        import review_setup

        self.review_setup = review_setup

    def snapshot_tree(self):
        return sorted(
            path.relative_to(self.root).as_posix()
            for path in self.root.rglob("*")
            if not path.is_symlink()
        )

    def test_github_provider_fails_before_any_mutation(self):
        state = common.ReviewState(self.root, read_only=True)
        before = self.snapshot_tree()
        code, out, _ = invoke(
            self.review_setup,
            ["--root", str(self.root), "42", "--slot", "0", "--provider", "github",
             "--json", "--run-id", RUN_ID],
        )
        envelope = parse_envelope(self, out)
        self.assertEqual((code, envelope["status"]), (1, "failed"))
        unsupported = next(e for e in envelope["effects"] if e["type"] == "unsupported_provider")
        self.assertEqual(unsupported["detail"], "github adapter lands in Slice 3")
        self.assertEqual(self.snapshot_tree(), before)
        self.assertFalse((self.state.common / "review-setup").exists())
        self.assertFalse((self.root / ".worktrees").exists())
        self.assertEqual(git(self.root, "branch", "--format=%(refname)"), "refs/heads/main")
        self.assertEqual(set(state.registered()), {self.root.resolve()})

    def test_every_github_provider_method_refuses(self):
        import review_provider

        provider = review_provider.get_provider("github")
        calls = (
            lambda: provider.ensure_available(),
            lambda: provider.parse_repo_url("https://github.com/org/repo"),
            lambda: provider.canonical_metadata_url("https://github.com/org/repo", "x"),
            lambda: provider.make_client("https://github.com"),
            lambda: provider.resolve_pr(None, 1),
            lambda: provider.select_module(None, {}, []),
        )
        for index, call in enumerate(calls):
            with self.subTest(method=index), self.assertRaises(common.ReviewError) as caught:
                call()
            self.assertIn("GitHub provider is not implemented", str(caught.exception))
            self.assertEqual(caught.exception.code, 1)

    def test_ado_provider_delegates_to_the_unchanged_functions(self):
        import review_provider

        provider = review_provider.get_provider("ado")
        self.assertEqual(
            provider.parse_repo_url(self.module["url"]),
            common.parse_ado_url(self.module["url"]),
        )
        self.assertEqual(
            provider.canonical_metadata_url(
                "https://org@dev.azure.com/org/project/_git/Nova", "https://dev.azure.com/org"
            ),
            common.canonical_ado_metadata_url(
                "https://org@dev.azure.com/org/project/_git/Nova", "https://dev.azure.com/org"
            ),
        )

    def test_provider_flag_is_rejected_outside_pr_setup(self):
        for argv in (["--status", "--provider", "github"], ["--warm-all", "--provider", "ado"]):
            with self.subTest(argv=argv):
                code, _, _ = invoke(self.review_setup, ["--root", str(self.root), *argv])
                self.assertNotEqual(code, 0)


if __name__ == "__main__":
    unittest.main()
