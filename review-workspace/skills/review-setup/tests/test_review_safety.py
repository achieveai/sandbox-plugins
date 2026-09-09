"""Adversarial lifecycle tests using disposable local Git repositories only."""
from __future__ import annotations

import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import review_common as common


def git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args], check=True, capture_output=True, text=True
    )
    return result.stdout.strip()


class MetadataUrlSafetyTests(unittest.TestCase):
    def test_provider_org_decoration_is_removed_only_from_metadata(self):
        decorated = "https://Org@dev.azure.com/Org/Project/_git/Nova"
        clean = common.canonical_ado_metadata_url(decorated, "https://dev.azure.com/org")
        self.assertEqual(clean, "https://dev.azure.com/org/Project/_git/Nova")
        with self.assertRaises(common.ReviewError):
            common.parse_ado_url(decorated)

    def test_metadata_rejects_credentials_cross_org_and_address_confusion(self):
        unsafe = [
            "https://org:password@dev.azure.com/org/Project/_git/Nova",
            "https://org:@dev.azure.com/org/Project/_git/Nova",
            "https://someone@dev.azure.com/org/Project/_git/Nova",
            "https://org%40other@dev.azure.com/org/Project/_git/Nova",
            "https://org@dev.azure.com@evil.example/org/Project/_git/Nova",
            "https://org@dev.azure.com.evil.example/org/Project/_git/Nova",
            "https://org@other.visualstudio.com/Project/_git/Nova",
            "https://org@dev.azure.com/other/Project/_git/Nova",
            "https://org@dev.azure.com:444/org/Project/_git/Nova",
            "https://org@dev.azure.com/org/Project/_git/Nova?token=fake",
            "https://org@dev.azure.com/org/Project/_git/Nova#fragment",
            "https://org@dev.azure.com/org/%2e%2e/_git/Nova",
            "https://org@dev.azure.com/org/Project%2fother/_git/Nova",
            "https://org@dev.azure.com/org/Project/_git/Nova%5cother",
            "https://org@dev.azure.com/org/Project/_git/Nova%00other",
            "http://org@dev.azure.com/org/Project/_git/Nova",
        ]
        for url in unsafe:
            with self.subTest(url=url), self.assertRaises(common.ReviewError):
                common.canonical_ado_metadata_url(url, "https://dev.azure.com/org")

    def test_legacy_metadata_uses_the_same_strict_canonical_host_boundary(self):
        clean = common.canonical_ado_metadata_url(
            "https://org@org.visualstudio.com/DefaultCollection/Project%20Name/_git/Nova",
            "https://dev.azure.com/org",
        )
        self.assertEqual(clean, "https://dev.azure.com/org/Project%20Name/_git/Nova")
        self.assertNotIn("@", clean)


class ReviewSafetyTests(unittest.TestCase):
    def setUp(self):
        job = os.environ.get("CLAUDE_JOB_DIR")
        job_tmp = Path(job) / "tmp" if job else None
        temp_parent = job_tmp if job_tmp is not None and job_tmp.is_dir() else tempfile.gettempdir()
        self.temp = tempfile.TemporaryDirectory(prefix="review-safety-", dir=temp_parent)
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.root = self.directory / "store"
        self.source = self.directory / "source"
        for repository in (self.root, self.source):
            repository.mkdir()
            git(repository, "init", "-b", "main")
            git(repository, "config", "user.name", "Safety Fixture")
            git(repository, "config", "user.email", "safety@example.invalid")
        (self.source / "source.txt").write_text("base\n", encoding="utf-8")
        git(self.source, "add", "source.txt")
        git(self.source, "commit", "-m", "fixture base")
        self.base = git(self.source, "rev-parse", "HEAD")
        (self.source / "source.txt").write_text("head\n", encoding="utf-8")
        git(self.source, "commit", "-am", "fixture head")
        self.head = git(self.source, "rev-parse", "HEAD")
        self.url = "https://dev.azure.com/org/project/_git/Nova"
        (self.root / ".gitmodules").write_text(
            '[submodule "repos/Nova"]\n path = repos/Nova\n url = ' + self.url + "\n",
            encoding="utf-8",
        )
        (self.root / "PRs").mkdir()
        (self.root / "PRs/.gitkeep").touch()
        git(self.root, "add", ".gitmodules", "PRs")
        git(self.root, "update-index", "--add", "--cacheinfo", "160000", self.base, "repos/Nova")
        git(self.root, "commit", "-m", "fixture store")
        self.store_head = git(self.root, "rev-parse", "HEAD")
        self.root_files = {
            "nova-active-prs-probe.json": '{"probe":true}',
            "nova-active-prs.json": "[]",
            "nova-active-prs.md": "Existing PR list\n",
            "nova-review-agent-candidates.json": "[]",
            "PRs/nova-5625433/user-notes.md": "Existing user review\n",
        }
        for relative, content in self.root_files.items():
            target = self.root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        self.snapshot = {
            "pr_number": 42,
            "repository": {"id": "repo-id", "name": "Nova", "project": {"id": "project-id", "name": "project"}},
            "head_sha": self.head,
            "target_sha": self.base,
            "base_sha": self.base,
            "source_ref": "refs/heads/main",
            "target_ref": "refs/heads/main",
            "iteration_id": 1,
            "snapshot_fingerprint": "fixture-snapshot",
            "title": "Fixture PR",
        }
        self.module = {"name": "repos/Nova", "path": "repos/Nova", "url": self.url}
        self.state = common.ReviewState(self.root)

    def reserve(self, slot=0):
        with self.state.lock():
            return self.state.reserve(self.snapshot, self.module, slot=slot)

    def prepare(self, slot=0):
        manifest = self.reserve(slot)
        checkout = self.state.slot_path(manifest)
        git(checkout, "-c", "protocol.file.allow=always", "-c",
            f"url.{self.source.as_uri()}.insteadOf={self.url}",
            "submodule", "update", "--init", "--", "repos/Nova")
        git(checkout / "repos/Nova", "checkout", "--detach", self.head)
        artifact = "PRs/nova-42/PR_Setup_01.json"
        common.write_new_json(checkout / artifact, {"run_id": manifest["run_id"]})
        manifest.update(source_prepared=True, base_sha=self.base, status="ready")
        manifest["artifacts"] = {artifact: common.file_hash(checkout / artifact)}
        self.state.save(manifest)
        return manifest, checkout, artifact

    def reset(self, slot=0, **kwargs):
        return importlib.import_module("review_reset").reset_run(self.state, slot, **kwargs)

    def assert_root_preserved(self):
        self.assertEqual(git(self.root, "symbolic-ref", "--short", "HEAD"), "main")
        self.assertEqual(git(self.root, "rev-parse", "HEAD"), self.store_head)
        self.assertFalse((self.root / ".gitignore").exists())
        for relative, content in self.root_files.items():
            self.assertEqual((self.root / relative).read_text(encoding="utf-8"), content)

    def test_refuses_review_branch_owned_by_other_worktree_without_touching_root(self):
        other = self.directory / "unmanaged-review"
        git(self.root, "worktree", "add", "-b", "review/nova-42", str(other), "HEAD")
        before = git(other, "rev-parse", "HEAD")
        with self.assertRaises(common.ReviewError):
            self.reserve(2)
        self.assertEqual(git(other, "rev-parse", "HEAD"), before)
        self.assertEqual(git(other, "symbolic-ref", "--short", "HEAD"), "review/nova-42")
        self.assertFalse((self.root / ".worktrees/review-slot-2").exists())
        self.assert_root_preserved()

    def test_active_allocation_lock_refuses_second_operation(self):
        with self.state.lock():
            with self.assertRaises(common.ReviewError):
                with common.ReviewState(self.root).lock():
                    self.fail("A second independent lock acquired the protected registry")
        self.assertFalse((self.root / ".worktrees").exists())
        self.assert_root_preserved()

    def test_manifest_from_other_namespace_is_rejected_without_changing_ref(self):
        manifest = self.reserve()
        path = self.state.manifest_path(0)
        value = json.loads(path.read_text(encoding="utf-8"))
        value["identity"] = "different-host-or-container-git-common-dir"
        path.write_text(json.dumps(value), encoding="utf-8")
        before = path.read_bytes()
        with self.assertRaises(common.ReviewError):
            self.state.load(0)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(git(self.state.slot_path(manifest), "symbolic-ref", "--short", "HEAD"), "review/nova-42")
        self.assert_root_preserved()

    def test_archive_refuses_source_edit_without_staging_or_committing(self):
        _, slot, artifact = self.prepare()
        source_file = slot / "repos/Nova/source.txt"
        source_file.write_text("user source modification\n", encoding="utf-8")
        before = git(slot, "rev-parse", "HEAD")
        with self.assertRaises(common.ReviewError):
            self.reset(mode="archive")
        self.assertEqual(source_file.read_text(encoding="utf-8"), "user source modification\n")
        self.assertEqual(git(slot, "rev-parse", "HEAD"), before)
        self.assertEqual(git(slot, "diff", "--cached", "--name-only"), "")
        self.assertTrue((slot / artifact).exists())
        self.assert_root_preserved()

    def test_discard_refuses_unowned_new_review_output_and_preserves_owned_output(self):
        _, slot, artifact = self.prepare()
        unknown = slot / "PRs/nova-42/PR_Findings_01_user.md"
        unknown.write_text("User wrote this after setup\n", encoding="utf-8")
        with self.assertRaises(common.ReviewError):
            self.reset(mode="discard", confirm="nova-42")
        self.assertEqual(unknown.read_text(encoding="utf-8"), "User wrote this after setup\n")
        self.assertTrue((slot / artifact).exists())
        self.assertEqual(git(slot, "symbolic-ref", "--short", "HEAD"), "review/nova-42")
        self.assert_root_preserved()

    def test_archive_preserves_ignored_slot_files_and_never_commits_them(self):
        _, slot, artifact = self.prepare()
        exclude = self.root / ".git/info/exclude"
        with exclude.open("a", encoding="utf-8") as stream:
            stream.write("\n/.claude/\n/.conversations/\n/.mcp-gateway/\n/Memory/\n/scripts/\n")
        ignored = {}
        for name in (".claude", ".conversations", ".mcp-gateway", "Memory", "scripts"):
            target = slot / name / "preserve.txt"
            target.parent.mkdir(exist_ok=True)
            target.write_text("fixture-only private state", encoding="utf-8")
            ignored[target] = target.read_bytes()
        result = self.reset(mode="archive")
        tree = git(self.root, "ls-tree", "-r", "--name-only", result["archive_branch"])
        self.assertIn(artifact, tree.splitlines())
        for target, contents in ignored.items():
            self.assertEqual(target.read_bytes(), contents)
            self.assertNotIn(target.relative_to(slot).as_posix(), tree.splitlines())
        self.assert_root_preserved()

    def test_checkpoint_failure_preserves_branch_outputs_and_allows_retry(self):
        _, slot, artifact = self.prepare()
        reset_module = importlib.import_module("review_reset")
        original = reset_module.run_git

        def fail_commit(repo, args, check=True):
            if args and args[0] == "commit":
                raise common.ReviewError("Injected checkpoint failure")
            return original(repo, args, check=check)

        with mock.patch.object(reset_module, "run_git", side_effect=fail_commit):
            with self.assertRaises(common.ReviewError):
                self.reset(mode="archive")
        self.assertEqual(git(slot, "symbolic-ref", "--short", "HEAD"), "review/nova-42")
        self.assertTrue((slot / artifact).exists())
        result = self.reset(mode="archive")
        self.assertTrue(result["archive_branch"].startswith("archive/review/nova-42/"))
        self.assertEqual(git(self.root, "show", f"{result['archive_branch']}:{artifact}"),
                         json.dumps({"run_id": result["run_id"]}, indent=2, sort_keys=True))
        self.assert_root_preserved()

    def test_archive_recovers_when_process_stops_after_branch_rename(self):
        _, slot, artifact = self.prepare()
        reset_module = importlib.import_module("review_reset")
        original = reset_module.run_git

        def interrupt_after_rename(repo, args, check=True):
            result = original(repo, args, check=check)
            if args[:2] == ["branch", "-m"]:
                raise common.ReviewError("Injected interruption after successful rename")
            return result

        with mock.patch.object(reset_module, "run_git", side_effect=interrupt_after_rename):
            with self.assertRaises(common.ReviewError):
                self.reset(mode="archive")
        archive_ref = git(slot, "symbolic-ref", "--short", "HEAD")
        self.assertTrue(archive_ref.startswith("archive/review/nova-42/"))
        archived_content = git(self.root, "show", f"{archive_ref}:{artifact}")
        result = self.reset(mode="archive")
        self.assertEqual(result["status"], "archived")
        self.assertEqual(result["archive_branch"], archive_ref)
        self.assertEqual(git(self.root, "show", f"{archive_ref}:{artifact}"), archived_content)
        self.assertEqual(git(slot, "status", "--porcelain"), "")
        self.assert_root_preserved()

    def test_discard_recovers_when_process_stops_after_detach(self):
        manifest, slot, _ = self.prepare()
        reset_module = importlib.import_module("review_reset")
        original = reset_module.run_git

        def interrupt_after_detach(repo, args, check=True):
            result = original(repo, args, check=check)
            if Path(repo) == slot and args == ["checkout", "--detach", manifest["baseline_sha"]]:
                raise common.ReviewError("Injected interruption after successful detach")
            return result

        with mock.patch.object(reset_module, "run_git", side_effect=interrupt_after_detach):
            with self.assertRaises(common.ReviewError):
                self.reset(mode="discard", confirm="nova-42")
        self.assertIsNone(common.current_ref(slot))
        result = self.reset(mode="discard", confirm="nova-42")
        self.assertEqual(result["status"], "discarded")
        self.assertEqual(git(slot, "rev-parse", "HEAD"), self.store_head)
        self.assertEqual(git(slot, "status", "--porcelain"), "")
        self.assert_root_preserved()

    def test_interrupted_reset_refuses_changed_slot_branch_on_retry(self):
        manifest, slot, _ = self.prepare()
        reset_module = importlib.import_module("review_reset")
        original = reset_module.run_git

        def interrupt_before_detach(repo, args, check=True):
            if Path(repo) == slot and args == ["checkout", "--detach", manifest["baseline_sha"]]:
                raise common.ReviewError("Injected interruption before detach")
            return original(repo, args, check=check)

        with mock.patch.object(reset_module, "run_git", side_effect=interrupt_before_detach):
            with self.assertRaises(common.ReviewError):
                self.reset(mode="archive")
        git(slot, "checkout", "-b", "user-unrelated-branch")
        before = git(slot, "rev-parse", "HEAD")
        with self.assertRaises(common.ReviewError):
            self.reset(mode="archive")
        self.assertEqual(common.current_ref(slot), "user-unrelated-branch")
        self.assertEqual(git(slot, "rev-parse", "HEAD"), before)
        self.assert_root_preserved()

    def test_state_slots_symlink_cannot_write_external_manifest(self):
        self.state.directory.mkdir(parents=True)
        outside = self.directory / "outside-state"
        outside.mkdir()
        (self.state.directory / "slots").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(common.ReviewError):
            self.reserve()
        self.assertEqual(list(outside.iterdir()), [])
        self.assert_root_preserved()

    def test_archive_refuses_submodule_symlink_without_touching_other_repository(self):
        manifest = self.reserve()
        slot = self.state.slot_path(manifest)
        source_path = slot / "repos/Nova"
        if source_path.exists():
            source_path.rmdir()
        source_path.symlink_to(self.source, target_is_directory=True)
        before = git(self.source, "rev-parse", "HEAD")
        with self.assertRaises(common.ReviewError):
            self.reset(mode="archive")
        self.assertTrue(source_path.is_symlink())
        self.assertEqual(git(self.source, "rev-parse", "HEAD"), before)
        self.assertEqual(git(self.source, "status", "--porcelain"), "")
        self.assert_root_preserved()

    def test_archive_preserves_ignored_unowned_source_data(self):
        _, slot, _ = self.prepare()
        source = slot / "repos/Nova"
        excluded = source / "local-state/private.txt"
        excluded.parent.mkdir()
        excluded.write_text("fixture-only user state", encoding="utf-8")
        gitdir = Path(git(source, "rev-parse", "--absolute-git-dir"))
        with (gitdir / "info/exclude").open("a", encoding="utf-8") as stream:
            stream.write("\n/local-state/\n")
        self.assertEqual(git(source, "status", "--porcelain"), "")
        # Refusal is safe; silently deleting an ignored file is not.
        try:
            self.reset(mode="archive")
        except common.ReviewError:
            pass
        self.assertTrue(excluded.exists(), "Reset erased unowned ignored submodule data")
        self.assertEqual(excluded.read_text(encoding="utf-8"), "fixture-only user state")
        self.assert_root_preserved()

    def test_discard_refuses_artifact_symlink_and_never_changes_destination(self):
        _, slot, artifact = self.prepare()
        destination = self.directory / "outside.txt"
        destination.write_text("must survive", encoding="utf-8")
        path = slot / artifact
        path.unlink()
        path.symlink_to(destination)
        with self.assertRaises(common.ReviewError):
            self.reset(mode="discard", confirm="nova-42")
        self.assertEqual(destination.read_text(encoding="utf-8"), "must survive")
        self.assertTrue(path.is_symlink())
        self.assertEqual(git(slot, "symbolic-ref", "--short", "HEAD"), "review/nova-42")
        self.assert_root_preserved()


if __name__ == "__main__":
    unittest.main()
