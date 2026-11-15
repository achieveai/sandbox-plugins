"""Disposable real-Git integration tests for local merge-review."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import merge_review
import review_common as common
import review_setup


def git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        text=True,
        capture_output=True,
        check=check,
    )


def output(repo: Path, *args: str) -> str:
    return git(repo, *args).stdout.strip()


class MergeFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        self.remote = base / "source-remote.git"
        self.seed = base / "source-seed"
        self.root = base / "reviews"
        git(base, "init", "--bare", str(self.remote))
        git(base, "init", "-b", "trunk", str(self.seed))
        self._identity(self.seed)
        (self.seed / "source.txt").write_text("baseline\n", encoding="utf-8")
        git(self.seed, "add", "source.txt")
        git(self.seed, "commit", "-m", "source baseline")
        self.baseline = output(self.seed, "rev-parse", "HEAD")
        git(self.seed, "remote", "add", "origin", str(self.remote))
        git(self.seed, "push", "-u", "origin", "trunk")
        git(self.remote, "symbolic-ref", "HEAD", "refs/heads/trunk")

        git(base, "init", "-b", "main", str(self.root))
        self._identity(self.root)
        (self.root / "PRs").mkdir()
        (self.root / "PRs" / ".gitkeep").touch()
        self.configured_url = "https://dev.azure.com/org/project/_git/Nova"
        (self.root / ".gitmodules").write_text(
            '[submodule "repos/Nova"]\n'
            ' path = repos/Nova\n'
            f' url = {self.configured_url}\n',
            encoding="utf-8",
        )
        git(self.root, "add", ".gitmodules", "PRs")
        git(
            self.root,
            "update-index",
            "--add",
            "--cacheinfo",
            "160000",
            self.baseline,
            "repos/Nova",
        )
        git(self.root, "commit", "-m", "reviews baseline")
        self.root_start = output(self.root, "rev-parse", "HEAD")
        git(
            self.root,
            "config",
            f"url.{self.remote.as_uri()}.insteadOf",
            self.configured_url,
        )
        self.state = common.ReviewState(self.root)
        self.module = common.read_submodules(self.root)[0]
        self.pool = common.SourcePool(self.state, self.module)
        with common.lock_session(20), self.state.lock(shared=True), self.state.slot_lock(
            self.pool.repo_key, 0
        ), common.slot_writer_scope(self.state, self.pool.repo_key, 0, phase="fixture"):
            self.pool._ensure_store()
            common.run_git(
                self.pool.store,
                ["fetch", "--", self.remote.as_uri(), self.baseline],
            )
            self.pool.publish_baseline(self.baseline)
            self.pool.warm([0])
        self.outer = self.root / self.pool.slot_relative_path(0)
        self.source = self.outer / self.module["path"]
        git(self.outer, "checkout", "-b", "review/nova-42", self.root_start)
        pr_dir = self.outer / "PRs/nova-42"
        pr_dir.mkdir(parents=True)
        (pr_dir / "review.md").write_text("review output\n", encoding="utf-8")
        self.manifest = {
            "schema_version": 2,
            "identity": self.state.identity,
            "repo_key": self.pool.repo_key,
            "repo_name": self.pool.repo_name,
            "slot_number": 0,
            "slot": self.pool.slot_name(0),
            "slot_id": self.pool.slot_name(0),
            "slot_path": self.pool.slot_relative_path(0),
            "key": "nova-42",
            "branch": "review/nova-42",
            "branch_existed": False,
            "branch_start": self.root_start,
            "root_head": self.root_start,
            "baseline_sha": self.baseline,
            "baseline_ref": f"refs/review-setup/baselines/{self.pool.repo_key}",
            "submodule": self.module,
            "snapshot": {
                "head_sha": self.baseline,
                "target_sha": self.baseline,
                "base_sha": self.baseline,
            },
            "base_sha": self.baseline,
            "pr_dir": "PRs/nova-42",
            "status": "ready",
            "phase": "ready",
            "run_id": "20260909T000000Z-fixture",
            "created_at": common.utc_now(),
            "artifacts": {
                "PRs/nova-42/review.md": common.file_hash(pr_dir / "review.md")
            },
            "baseline_artifacts": {},
            "source_prepared": True,
            "source_baseline": None,
            "retention_refs": {},
            "required_shas": {
                "head": self.baseline,
                "base": self.baseline,
                "target": self.baseline,
            },
            "complete": True,
            "stale": False,
        }
        with common.lock_session(20), self.state.lock(shared=True), self.state.slot_lock(
            self.pool.repo_key, 0
        ), common.slot_writer_scope(self.state, self.pool.repo_key, 0, phase="fixture-retain"):
            retained = self.pool.retain(
                self.manifest["run_id"],
                {
                    "head": self.baseline,
                    "base": self.baseline,
                    "target": self.baseline,
                    "baseline": self.baseline,
                },
            )
        self.manifest["retained_commits"] = retained
        self.manifest["retention_refs"] = {
            kind: self.pool.retention_ref(self.manifest["run_id"], kind)
            for kind in retained
        }
        self.state.save_pool_run(self.manifest)

    @staticmethod
    def _identity(repo: Path) -> None:
        git(repo, "config", "user.name", "Merge Test")
        git(repo, "config", "user.email", "merge@example.invalid")

    def run_merge(self, **kwargs):
        with patch.object(
            merge_review,
            "resolve_tracking_baseline",
            return_value=("trunk", self.baseline),
        ):
            return merge_review.merge_review(
                self.root,
                "Nova",
                0,
                history_timeout=20,
                lock_timeout=20,
                **kwargs,
            )

    def use_canonical_pr_dir(self):
        legacy = self.outer / self.manifest["pr_dir"]
        canonical_relative = "PRs/Nova-42"
        canonical = self.outer / canonical_relative
        legacy.rename(canonical)
        self.manifest["pr_dir"] = canonical_relative
        self.manifest["artifacts"] = {
            f"{canonical_relative}/review.md": common.file_hash(canonical / "review.md")
        }
        self.state.save_pool_run(self.manifest)
        return canonical

    def assert_merged(self, result):
        self.assertEqual(result["status"], "merged")
        merged = result["merge_result_sha"]
        parents = output(self.root, "rev-list", "--parents", "-n", "1", merged).split()
        self.assertEqual(len(parents), 3)
        self.assertEqual(output(self.root, "rev-parse", f"{merged}:repos/Nova"), self.baseline)
        self.assertNotEqual(git(self.root, "show-ref", "--verify", "refs/heads/review/nova-42", check=False).returncode, 0)
        self.assertEqual(output(self.outer, "branch", "--show-current"), "")

    def test_clean_merge_checkpoints_scoped_paths_and_is_idempotent(self):
        canonical = self.use_canonical_pr_dir()
        unrelated = self.root / "notes.local"
        unrelated.write_text("preserve\n", encoding="utf-8")
        result = self.run_merge()
        self.assert_merged(result)
        self.assertEqual(
            output(
                self.root,
                "show",
                f"{result['merge_result_sha']}:PRs/Nova-42/review.md",
            ),
            "review output",
        )
        self.assertTrue(canonical.is_dir())
        self.assertFalse((self.outer / "PRs/nova-42").exists())
        self.assertEqual(unrelated.read_text(encoding="utf-8"), "preserve\n")
        retry = self.run_merge()
        self.assertEqual(retry["merge_result_sha"], result["merge_result_sha"])

    def test_existing_lowercase_manifest_remains_authoritative_for_merge(self):
        canonical_collision = self.root / "PRs/Nova-42"
        canonical_collision.mkdir(parents=True)
        (canonical_collision / "review.md").write_text(
            "unowned canonical collision\n", encoding="utf-8"
        )

        result = self.run_merge()

        self.assert_merged(result)
        self.assertEqual(
            output(
                self.root,
                "show",
                f"{result['merge_result_sha']}:PRs/nova-42/review.md",
            ),
            "review output",
        )
        self.assertEqual(
            (canonical_collision / "review.md").read_text(encoding="utf-8"),
            "unowned canonical collision\n",
        )
        self.assertNotIn(
            "PRs/Nova-42/review.md",
            output(self.root, "ls-tree", "-r", "--name-only", result["merge_result_sha"]).splitlines(),
        )

    def test_configured_branch_name_wins_over_snapshot_target(self):
        git(self.root, "config", "submodule.repos/Nova.branch", "trunk")
        self.manifest["snapshot"]["target_ref"] = "refs/heads/not-trunk"
        self.state.save_pool_run(self.manifest)
        self.assertEqual(merge_review._configured_branch(self.root, self.module), "trunk")

    def test_gitmodules_branch_and_special_dot_refusal(self):
        git(self.root, "config", "--file", ".gitmodules", "submodule.repos/Nova.branch", "trunk")
        self.assertEqual(merge_review._configured_branch(self.root, self.module), "trunk")
        git(self.root, "config", "--file", ".gitmodules", "submodule.repos/Nova.branch", ".")
        with self.assertRaises(common.ReviewError):
            merge_review._configured_branch(self.root, self.module)

    def test_remote_symbolic_head_fallback(self):
        with patch.dict(
            os.environ,
            {
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": f"url.{self.remote.as_uri()}.insteadOf",
                "GIT_CONFIG_VALUE_0": self.configured_url,
            },
        ):
            self.assertEqual(
                merge_review._remote_default_branch(self.root, self.pool), "trunk"
            )

    def test_freshness_failure_does_not_record_baseline(self):
        with patch.object(common, "bounded_fetch", return_value=subprocess.CompletedProcess([], 1, "", "")):
            with self.assertRaises(common.ReviewError):
                merge_review.resolve_tracking_baseline(
                    self.state, self.pool, self.manifest, max_depth=1000000
                )
        current = self.state.load_pool_run(self.pool.repo_key, 0)
        self.assertNotIn("merge_baseline_sha", current)

    def test_allowed_precommitted_review_delta_is_merged(self):
        git(self.outer, "add", "PRs/nova-42/review.md")
        git(self.outer, "commit", "-m", "legitimate review work")
        committed_tip = output(self.outer, "rev-parse", "HEAD")

        result = self.run_merge()

        self.assert_merged(result)
        self.assertEqual(
            output(self.root, "show", f"{result['merge_result_sha']}:PRs/nova-42/review.md"),
            "review output",
        )
        self.assertEqual(result["review_tip"], committed_tip)

    def test_checkpoint_refuses_committed_foreign_delta(self):
        (self.outer / "foreign-committed.txt").write_text("foreign\n", encoding="utf-8")
        git(self.outer, "add", "foreign-committed.txt")
        git(self.outer, "commit", "-m", "foreign committed delta")
        with self.assertRaisesRegex(common.ReviewError, "committed changes outside"):
            self.run_merge()

    def test_checkpoint_refuses_foreign_path_renamed_into_allowed_scope(self):
        git(
            self.outer,
            "mv",
            "PRs/.gitkeep",
            "PRs/nova-42/moved-from-foreign.txt",
        )
        git(self.outer, "commit", "-m", "rename foreign path into allowed scope")

        with self.assertRaisesRegex(common.ReviewError, "committed changes outside"):
            self.run_merge()

    def test_checkpoint_refuses_rewritten_non_descendant_tip(self):
        tree = output(self.outer, "write-tree")
        rewritten = git(
            self.outer,
            "commit-tree",
            tree,
            "-m",
            "rewritten review history",
        ).stdout.strip()
        git(self.outer, "reset", "--hard", rewritten)

        with self.assertRaisesRegex(common.ReviewError, "not a descendant"):
            self.run_merge()

    def test_root_preflight_precedes_fetch_and_all_preparation_mutation(self):
        cases = ("wrong-branch", "dirty-root")
        for case in cases:
            with self.subTest(case=case):
                if case == "wrong-branch":
                    git(self.root, "checkout", "-b", "not-main")
                else:
                    (self.root / ".gitmodules").write_text(
                        (self.root / ".gitmodules").read_text(encoding="utf-8")
                        + "# dirty\n",
                        encoding="utf-8",
                    )
                manifest_path = self.state.pool_run_path(self.pool.repo_key, 0)
                manifest_bytes = manifest_path.read_bytes()
                source_head = output(self.source, "rev-parse", "HEAD")
                review_tip = output(self.outer, "rev-parse", "HEAD")
                with patch.object(
                    merge_review,
                    "resolve_tracking_baseline",
                    side_effect=AssertionError("network resolution must not start"),
                ) as resolver:
                    with self.assertRaises(common.ReviewError):
                        merge_review.merge_review(
                            self.root,
                            "Nova",
                            0,
                            history_timeout=20,
                            lock_timeout=20,
                        )
                resolver.assert_not_called()
                self.assertEqual(manifest_path.read_bytes(), manifest_bytes)
                self.assertEqual(output(self.source, "rev-parse", "HEAD"), source_head)
                self.assertEqual(output(self.outer, "rev-parse", "HEAD"), review_tip)
                self.assertIsNone(common.load_outer_transaction(self.state))
                if case == "wrong-branch":
                    git(self.root, "checkout", "main")
                else:
                    git(self.root, "restore", ".gitmodules")

    def test_checkpoint_refuses_unrelated_slot_change(self):
        (self.outer / "foreign.txt").write_text("foreign\n", encoding="utf-8")
        with self.assertRaisesRegex(common.ReviewError, "Unrelated slot changes"):
            self.run_merge()

    def test_root_untracked_collision_refused_without_removal(self):
        collision = self.root / "PRs/nova-42/review.md"
        collision.parent.mkdir(parents=True, exist_ok=True)
        collision.write_text("local\n", encoding="utf-8")
        with self.assertRaisesRegex(common.ReviewError, "collides"):
            self.run_merge()
        self.assertEqual(collision.read_text(encoding="utf-8"), "local\n")

    def test_gitlink_heuristic_is_overridden_by_exact_baseline(self):
        (self.seed / "source.txt").write_text("review tip\n", encoding="utf-8")
        git(self.seed, "commit", "-am", "source tip")
        tip = output(self.seed, "rev-parse", "HEAD")
        git(self.outer, "update-index", "--cacheinfo", "160000", tip, "repos/Nova")
        self.manifest["artifacts"]["PRs/nova-42/review.md"] = common.file_hash(
            self.outer / "PRs/nova-42/review.md"
        )
        self.state.save_pool_run(self.manifest)
        result = self.run_merge()
        self.assertEqual(
            output(self.root, "rev-parse", f"{result['merge_result_sha']}:repos/Nova"),
            self.baseline,
        )

    def test_conflict_unresolved_then_staged_resume(self):
        conflict = self.outer / "PRs/nova-42/conflict.txt"
        conflict.write_text("review\n", encoding="utf-8")
        self.manifest["artifacts"]["PRs/nova-42/conflict.txt"] = common.file_hash(conflict)
        self.state.save_pool_run(self.manifest)
        root_conflict = self.root / "PRs/nova-42/conflict.txt"
        root_conflict.parent.mkdir(parents=True, exist_ok=True)
        root_conflict.write_text("main\n", encoding="utf-8")
        git(self.root, "add", "PRs/nova-42/conflict.txt")
        git(self.root, "commit", "-m", "main conflict")
        with self.assertRaisesRegex(common.ReviewError, "manual resolution") as first:
            self.run_merge()
        self.assertEqual(first.exception.code, merge_review.CONFLICT_EXIT)
        with self.assertRaisesRegex(common.ReviewError, "remain unresolved"):
            self.run_merge()
        root_conflict.write_text("resolved\n", encoding="utf-8")
        git(self.root, "add", "PRs/nova-42/conflict.txt")
        root_conflict.write_text("resolved but not staged\n", encoding="utf-8")
        with self.assertRaisesRegex(common.ReviewError, "unstaged"):
            self.run_merge()
        git(self.root, "add", "PRs/nova-42/conflict.txt")
        result = self.run_merge()
        self.assert_merged(result)

    def test_manual_merge_commit_is_recognized(self):
        manifest, _, _ = self._prepare_only()
        transaction = common.load_outer_transaction(self.state)
        self.assertIsNotNone(transaction)
        with common.lock_session(20), self.state.lock(shared=True), common.outer_merge_owner(transaction["id"]), self.state.outer_lock():
            with common.outer_writer_scope(self.state, transaction["id"], phase="manual-fixture"):
                git(self.root, "merge", "--no-ff", "--no-commit", transaction["review_tip"])
                git(self.root, "update-index", "--cacheinfo", "160000", self.baseline, "repos/Nova")
                git(self.root, "commit", "-m", "manual exact merge")
        result = self.run_merge()
        self.assert_merged(result)

    def _prepare_only(self):
        with patch.object(
            merge_review,
            "resolve_tracking_baseline",
            return_value=("trunk", self.baseline),
        ):
            with common.lock_session(20), self.state.lock(shared=True), self.state.slot_lock(
                self.pool.repo_key, 0
            ), common.slot_writer_scope(self.state, self.pool.repo_key, 0, phase="merge-review"), common.fetch_session(20):
                manifest, outer, source, transaction = merge_review._prepare_review(
                    self.state,
                    self.pool,
                    self.state.load_pool_run(self.pool.repo_key, 0),
                    max_depth=1000000,
                )
        return manifest, outer, source

    def _retire_pending_outer_writer(self):
        path = self.state.directory / "outer-writer.json"
        record = json.loads(path.read_text(encoding="utf-8"))
        operation = record.get("operation")
        self.assertIsNotNone(operation)
        process = operation.get("process")
        if process:
            deadline = time.monotonic() + 3
            while common._recorded_identity_is_active(process, group=True):
                if time.monotonic() >= deadline:
                    self.fail("recorded outer writer did not retire")
                time.sleep(0.01)
        record["operation"] = None
        record["last_settled_at"] = common.utc_now()
        record["last_settled_operation"] = operation
        record["last_settlement_proof"] = "fixture-observed-retired"
        common.atomic_json(path, record)

    def _fail_transaction_save_after(self, phase: str):
        original = merge_review.save_outer_transaction
        fired = False

        def fail_after(state, transaction):
            nonlocal fired
            original(state, transaction)
            if transaction.get("phase") == phase and not fired:
                fired = True
                raise RuntimeError(f"fixture crash after {phase}")

        return patch.object(merge_review, "save_outer_transaction", side_effect=fail_after)

    def test_moved_branch_refuses_deletion(self):
        manifest, _, _ = self._prepare_only()
        transaction = common.load_outer_transaction(self.state)
        with common.lock_session(20), self.state.lock(shared=True), common.outer_merge_owner(transaction["id"]), self.state.outer_lock():
            result = merge_review._merge_or_resume(self.state, transaction)
            merge_review._detach_slot(
                self.state, self.pool, manifest, transaction, result
            )
            git(self.root, "update-ref", "refs/heads/review/nova-42", self.root_start)
            with self.assertRaisesRegex(common.ReviewError, "moved"):
                merge_review._delete_review_ref(
                    self.state,
                    manifest,
                    transaction,
                    result,
                )

    def test_merge_command_completed_before_journal_update_resumes(self):
        self._prepare_only()
        original = merge_review.run_git
        fired = False

        def fail_after_merge(repo, args, **kwargs):
            nonlocal fired
            result = original(repo, args, **kwargs)
            if "merge" in args and "--no-commit" in args and not fired:
                fired = True
                raise RuntimeError("fixture crash after merge command")
            return result

        with patch.object(merge_review, "run_git", side_effect=fail_after_merge):
            with self.assertRaisesRegex(RuntimeError, "fixture crash"):
                self.run_merge()
        transaction = common.load_outer_transaction(self.state)
        self.assertEqual(transaction["phase"], "merge-intent")
        self.assertEqual(merge_review._merge_head(self.root), transaction["review_tip"])
        result = self.run_merge()
        self.assert_merged(result)

    def test_commit_completed_before_journal_update_resumes(self):
        self._prepare_only()
        with self._fail_transaction_save_after("committed"):
            with self.assertRaisesRegex(RuntimeError, "fixture crash"):
                self.run_merge()
        transaction = common.load_outer_transaction(self.state)
        self.assertEqual(transaction["phase"], "committed")
        self.assertEqual(output(self.root, "rev-parse", "HEAD"), transaction["result_sha"])
        result = self.run_merge()
        self.assert_merged(result)

    def test_slot_detach_completed_before_journal_update_resumes(self):
        original = merge_review.save_outer_transaction
        fired = False

        def fail_before_detached_save(state, transaction):
            nonlocal fired
            if transaction.get("phase") == "slot-detached" and not fired:
                fired = True
                raise RuntimeError("fixture crash after slot detach")
            original(state, transaction)

        with patch.object(
            merge_review, "save_outer_transaction", side_effect=fail_before_detached_save
        ):
            with self.assertRaisesRegex(RuntimeError, "fixture crash"):
                self.run_merge()
        transaction = common.load_outer_transaction(self.state)
        self.assertEqual(transaction["phase"], "slot-detach-intent")
        self.assertEqual(output(self.outer, "branch", "--show-current"), "")
        result = self.run_merge()
        self.assert_merged(result)

    def test_missing_review_ref_at_slot_detached_is_not_claimed(self):
        manifest, _, _ = self._prepare_only()
        transaction = common.load_outer_transaction(self.state)
        with common.lock_session(20), self.state.lock(shared=True), common.outer_merge_owner(
            transaction["id"]
        ), self.state.outer_lock():
            result = merge_review._merge_or_resume(self.state, transaction)
            merge_review._detach_slot(
                self.state, self.pool, manifest, transaction, result
            )
            self.assertEqual(transaction["phase"], "slot-detached")
            git(
                self.root,
                "update-ref",
                "-d",
                "refs/heads/review/nova-42",
                transaction["review_tip"],
            )
            with self.assertRaisesRegex(common.ReviewError, "before recorded deletion intent"):
                merge_review._delete_review_ref(
                    self.state,
                    manifest,
                    transaction,
                    result,
                )
        self.assertEqual(transaction["phase"], "slot-detached")

    def test_branch_deletion_completed_before_journal_update_resumes(self):
        original = merge_review.save_outer_transaction
        fired = False

        def fail_before_deleted_save(state, transaction):
            nonlocal fired
            if transaction.get("phase") == "branch-deleted" and not fired:
                fired = True
                raise RuntimeError("fixture crash after branch deletion")
            original(state, transaction)

        with patch.object(
            merge_review, "save_outer_transaction", side_effect=fail_before_deleted_save
        ):
            with self.assertRaisesRegex(RuntimeError, "fixture crash"):
                self.run_merge()
        transaction = common.load_outer_transaction(self.state)
        self.assertEqual(transaction["phase"], "branch-delete-intent")
        self.assertNotEqual(
            git(
                self.root,
                "show-ref",
                "--verify",
                "refs/heads/review/nova-42",
                check=False,
            ).returncode,
            0,
        )
        result = self.run_merge()
        self.assert_merged(result)

    def test_failed_branch_deletion_retries_without_claiming_success(self):
        original = merge_review.run_git
        failed = False

        def fail_delete(repo, args, **kwargs):
            nonlocal failed
            if args[:2] == ["update-ref", "-d"] and not failed:
                failed = True
                raise common.ReviewError("fixture delete failed")
            return original(repo, args, **kwargs)

        with patch.object(merge_review, "run_git", side_effect=fail_delete):
            with self.assertRaisesRegex(common.ReviewError, "fixture delete failed"):
                self.run_merge()
        transaction = common.load_outer_transaction(self.state)
        self.assertEqual(transaction["phase"], "branch-delete-intent")
        self.assertEqual(
            output(self.root, "rev-parse", "refs/heads/review/nova-42"),
            transaction["review_tip"],
        )
        result = self.run_merge()
        self.assert_merged(result)

    def test_terminal_history_write_interruption_resumes(self):
        original = merge_review.write_new_json
        fired = False

        def fail_history(path, value):
            nonlocal fired
            if path.parent.name == "history" and not fired:
                fired = True
                raise OSError("fixture history interruption")
            return original(path, value)

        with patch.object(merge_review, "write_new_json", side_effect=fail_history):
            with self.assertRaisesRegex(OSError, "fixture history interruption"):
                self.run_merge()
        transaction = common.load_outer_transaction(self.state)
        self.assertEqual(transaction["phase"], "terminal-intent")
        recorded = self.state.load_pool_run(self.pool.repo_key, 0)
        self.assertEqual(recorded["status"], "merged")
        result = self.run_merge()
        self.assert_merged(result)
        self.assertTrue(merge_review._history_path(self.state, result["run_id"]).is_file())

    def test_terminal_transaction_clear_interruption_resumes(self):
        original = merge_review.clear_outer_transaction
        fired = False

        def fail_clear(state, transaction_id):
            nonlocal fired
            if not fired:
                fired = True
                raise OSError("fixture clear interruption")
            return original(state, transaction_id)

        with patch.object(
            merge_review, "clear_outer_transaction", side_effect=fail_clear
        ):
            with self.assertRaisesRegex(OSError, "fixture clear interruption"):
                self.run_merge()
        transaction = common.load_outer_transaction(self.state)
        self.assertEqual(transaction["phase"], "terminal-recorded")
        result = self.run_merge()
        self.assert_merged(result)
        self.assertIsNone(common.load_outer_transaction(self.state))

    def test_conflict_refuses_staged_path_outside_transaction_scope(self):
        conflict = self.outer / "PRs/nova-42/conflict.txt"
        conflict.write_text("review\n", encoding="utf-8")
        self.manifest["artifacts"]["PRs/nova-42/conflict.txt"] = common.file_hash(
            conflict
        )
        self.state.save_pool_run(self.manifest)
        root_conflict = self.root / "PRs/nova-42/conflict.txt"
        root_conflict.parent.mkdir(parents=True, exist_ok=True)
        root_conflict.write_text("main\n", encoding="utf-8")
        git(self.root, "add", "PRs/nova-42/conflict.txt")
        git(self.root, "commit", "-m", "main conflict")
        with self.assertRaisesRegex(common.ReviewError, "manual resolution"):
            self.run_merge()
        root_conflict.write_text("resolved\n", encoding="utf-8")
        foreign = self.root / "foreign.txt"
        foreign.write_text("foreign\n", encoding="utf-8")
        git(self.root, "add", "PRs/nova-42/conflict.txt", "foreign.txt")
        with self.assertRaisesRegex(common.ReviewError, "outside the recorded"):
            self.run_merge()
        self.assertTrue(foreign.exists())

    def test_merged_slot_cleanup_normalizes_for_reuse(self):
        merged = self.run_merge()
        with common.lock_session(20), self.state.lock(shared=True):
            self.pool.publish_baseline(self.baseline)
        snapshot = {
            "repository": {"name": "Nova"},
            "pr_number": 43,
            "snapshot_fingerprint": "next-snapshot",
            "head_sha": self.baseline,
            "target_sha": self.baseline,
            "base_sha": self.baseline,
        }
        with common.lock_session(20), self.state.lock(shared=True), self.state.slot_lock(
            self.pool.repo_key, 0
        ), common.slot_writer_scope(self.state, self.pool.repo_key, 0, phase="setup-slot"):
            review_setup._cleanup_setup_slot(self.state, snapshot, self.module, 0)
        old = self.state.load_pool_run(self.pool.repo_key, 0)
        self.assertEqual(old["status"], "superseded")
        self.assertEqual(output(self.outer, "branch", "--show-current"), "")
        self.assertEqual(output(self.outer, "rev-parse", "HEAD"), output(self.root, "rev-parse", "HEAD"))
        self.assertEqual(output(self.source, "rev-parse", "HEAD"), self.pool.cached_baseline_sha())
        self.assertEqual(merged["merge_result_sha"], output(self.root, "rev-parse", "HEAD"))

    def test_foreign_root_merge_state_refused(self):
        (self.root / "other.txt").write_text("main\n", encoding="utf-8")
        git(self.root, "add", "other.txt")
        git(self.root, "commit", "-m", "main other")
        other = Path(self.temp.name) / "other"
        git(Path(self.temp.name), "clone", str(self.root), str(other))
        self._identity(other)
        git(other, "checkout", "-b", "foreign")
        (other / "other.txt").write_text("foreign\n", encoding="utf-8")
        git(other, "commit", "-am", "foreign")
        foreign = output(other, "rev-parse", "HEAD")
        git(self.root, "fetch", str(other), f"{foreign}:refs/heads/foreign")
        git(self.root, "merge", "--no-ff", "--no-commit", "foreign", check=False)
        with self.assertRaisesRegex(common.ReviewError, "existing Git operation"):
            self.run_merge()
        git(self.root, "merge", "--abort")

    def test_explicit_abort_preserves_checkpoint_and_retention(self):
        manifest, _, _ = self._prepare_only()
        transaction = common.load_outer_transaction(self.state)
        with common.lock_session(20), self.state.lock(shared=True), common.outer_merge_owner(transaction["id"]), self.state.outer_lock():
            with common.outer_writer_scope(self.state, transaction["id"], phase="abort-fixture"):
                git(self.root, "merge", "--no-ff", "--no-commit", transaction["review_tip"])
        aborted = merge_review.abort_merge(
            self.root,
            "Nova",
            0,
            confirm=manifest["key"],
            lock_timeout=20,
        )
        self.assertEqual(aborted["phase"], "merge-aborted")
        self.assertEqual(output(self.root, "rev-parse", "HEAD"), transaction["root_start"])
        self.assertEqual(output(self.root, "rev-parse", "refs/heads/review/nova-42"), transaction["review_tip"])
        self.assertIsNone(common.load_outer_transaction(self.state))

    def test_outer_transaction_blocks_foreign_mutator(self):
        self._prepare_only()
        with self.assertRaisesRegex(common.ReviewError, "reserved by merge run"):
            common.run_git(self.root, ["checkout", "--detach", self.root_start])

    def test_pending_transaction_blocks_other_slot_mutation(self):
        self._prepare_only()
        with common.lock_session(1), self.state.lock(shared=True), self.state.slot_lock(
            self.pool.repo_key, 1
        ):
            with self.assertRaisesRegex(common.ReviewError, "Canonical outer mutation"):
                with common.slot_writer_scope(
                    self.state, self.pool.repo_key, 1, phase="warm-slot"
                ):
                    pass

    def test_multiprocess_outer_lock_serializes_root_writers(self):
        ready = Path(self.temp.name) / "outer-ready"
        code = (
            "import pathlib,sys,time; "
            f"sys.path.insert(0,{str(SCRIPTS)!r}); import review_common as c; "
            f"s=c.ReviewState(pathlib.Path({str(self.root)!r})); "
            "locks=c.lock_session(5); locks.__enter__(); "
            "compat=s.lock(shared=True); compat.__enter__(); "
            "outer=s.outer_lock(phase='fixture-holder'); outer.__enter__(); "
            f"pathlib.Path({str(ready)!r}).write_text('ready'); time.sleep(.5); "
            "outer.__exit__(None,None,None); compat.__exit__(None,None,None); "
            "locks.__exit__(None,None,None)"
        )
        holder = subprocess.Popen([sys.executable, "-c", code])
        self.addCleanup(lambda: holder.poll() is None and holder.kill())
        deadline = time.monotonic() + 3
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(ready.exists())
        started = time.monotonic()
        with common.lock_session(5), self.state.lock(shared=True), self.state.outer_lock(
            phase="fixture-contender"
        ):
            waited = time.monotonic() - started
        self.assertGreater(waited, 0.2)
        self.assertEqual(holder.wait(timeout=3), 0)

    def test_initialized_mismatched_canonical_source_refused(self):
        canonical_source = self.root / "repos/Nova"
        canonical_source.parent.mkdir(parents=True, exist_ok=True)
        git(
            self.root,
            "-c",
            "protocol.file.allow=always",
            "-c",
            f"url.{self.remote.as_uri()}.insteadOf={self.configured_url}",
            "submodule",
            "update",
            "--init",
            "--",
            "repos/Nova",
        )
        git(self.seed, "checkout", "-b", "later")
        (self.seed / "source.txt").write_text("later\n", encoding="utf-8")
        git(self.seed, "commit", "-am", "later")
        later = output(self.seed, "rev-parse", "HEAD")
        git(canonical_source, "fetch", self.seed.as_uri(), later)
        git(canonical_source, "checkout", "--detach", later)
        with self.assertRaisesRegex(common.ReviewError, "not at the pinned"):
            self.run_merge()

    def test_other_uninitialized_gitlink_is_not_modified_recursively(self):
        other_url = "https://dev.azure.com/org/project/_git/Other"
        other_path = "repos/Other"
        git(
            self.root,
            "update-index",
            "--add",
            "--cacheinfo",
            "160000",
            self.baseline,
            other_path,
        )
        modules = self.root / ".gitmodules"
        modules.write_text(
            modules.read_text(encoding="utf-8")
            + f'[submodule "repos/Other"]\n path = {other_path}\n url = {other_url}\n',
            encoding="utf-8",
        )
        git(self.root, "add", ".gitmodules")
        git(self.root, "commit", "-m", "add other source")
        new_root = output(self.root, "rev-parse", "HEAD")
        # Rebase the still-uncheckpointed review branch to this canonical baseline.
        git(self.outer, "reset", "--hard", new_root)
        pr = self.outer / "PRs/nova-42/review.md"
        pr.parent.mkdir(parents=True, exist_ok=True)
        pr.write_text("review output\n", encoding="utf-8")
        self.manifest["root_head"] = new_root
        self.manifest["branch_start"] = new_root
        self.manifest["artifacts"] = {"PRs/nova-42/review.md": common.file_hash(pr)}
        self.state.save_pool_run(self.manifest)
        result = self.run_merge()
        self.assert_merged(result)
        self.assertFalse((self.root / other_path / ".git").exists())
        self.assertEqual(output(self.root, "rev-parse", f"HEAD:{other_path}"), self.baseline)

    def test_outer_writer_recovery_requires_exact_assertion(self):
        self._prepare_only()
        transaction = common.load_outer_transaction(self.state)
        operation = {
            "id": "a" * 32,
            "transaction_id": transaction["id"],
            "phase": "merge-start",
            "spawn_pending": False,
            "process": {
                **common.process_identity(os.getpid()),
                "pgid": 99999999,
                "session": 99999999,
            },
            "coordinator": {
                **common.process_identity(os.getpid()),
                "pid": 99999998,
                "start_ticks": 1,
            },
            "started_at": common.utc_now(),
        }
        common.atomic_json(
            self.state.directory / "outer-writer.json",
            {
                "schema_version": 1,
                "workspace_identity": self.state.identity,
                "operation": operation,
            },
        )
        with common.lock_session(20), self.state.lock(shared=True), common.outer_merge_owner(transaction["id"]), self.state.outer_lock():
            details = common.recover_outer_writer(self.state, inspect=True)
            with self.assertRaisesRegex(common.ReviewError, "assertion"):
                common.recover_outer_writer(self.state, assertion={})
            assertion = {
                key: details[key]
                for key in (
                    "workspace_identity",
                    "transaction_id",
                    "transaction_sha256",
                    "writer_sha256",
                    "operation_id",
                    "operation_phase",
                    "topology_sha256",
                    "writer_visibility",
                )
            }
            assertion.update(
                prior_writer_retired=True,
                operator="fixture",
                basis="fixture process identities cannot exist",
            )
            settled = common.recover_outer_writer(self.state, assertion=assertion)
        self.assertFalse(settled["pending"])


if __name__ == "__main__":
    unittest.main()
