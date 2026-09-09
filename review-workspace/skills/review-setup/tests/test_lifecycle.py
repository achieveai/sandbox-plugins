"""Local-only lifecycle tests; never operate on the real review checkout."""
import contextlib
import hashlib
import importlib.util
import io
import json
import os
import stat
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
SPEC = importlib.util.find_spec("review_common")
if SPEC:
    import review_common as common


def git(root, *args):
    result = subprocess.run(["git", "-C", str(root), *args], capture_output=True,
                            text=True, check=True)
    return result.stdout.strip()


class CommonContractTests(unittest.TestCase):
    def test_shared_runtime_exists(self):
        self.assertIsNotNone(importlib.util.find_spec("review_common"))

    @unittest.skipUnless(SPEC, "runtime not implemented yet")
    def test_legacy_and_modern_urls_match(self):
        modern = common.parse_ado_url("https://dev.azure.com/O365Exchange/O365%20Core/_git/Nova")
        legacy = common.parse_ado_url("https://o365exchange.visualstudio.com/DefaultCollection/O365%20Core/_git/Nova")
        self.assertEqual(modern, legacy)
        self.assertEqual(modern["project"], "O365 Core")

    @unittest.skipUnless(SPEC, "runtime not implemented yet")
    def test_rejects_credentials_and_path_traversal(self):
        for url in ["https://token@dev.azure.com/org/p/_git/r",
                    "https://dev.azure.com/org/../_git/r", "http://dev.azure.com/org/p/_git/r"]:
            with self.subTest(url=url), self.assertRaises(common.ReviewError):
                common.parse_ado_url(url)

    @unittest.skipUnless(SPEC, "runtime not implemented yet")
    def test_metadata_url_strips_only_matching_organization_username(self):
        self.assertTrue(hasattr(common, "canonical_ado_metadata_url"))
        canonical = common.canonical_ado_metadata_url(
            "https://ExampleOrg@dev.azure.com/ExampleOrg/Project/_git/Nova", "https://dev.azure.com/exampleorg")
        self.assertEqual(canonical, "https://dev.azure.com/exampleorg/Project/_git/Nova")
        self.assertNotIn("@", canonical)
        with self.assertRaises(common.ReviewError):
            common.parse_ado_url("https://ExampleOrg@dev.azure.com/ExampleOrg/Project/_git/Nova")

    @unittest.skipUnless(SPEC, "runtime not implemented yet")
    def test_metadata_canonicalization_rejects_credentials_and_redirects(self):
        self.assertTrue(hasattr(common, "canonical_ado_metadata_url"))
        for url in ["https://token@dev.azure.com/org/p/_git/r", "https://org:secret@dev.azure.com/org/p/_git/r",
                    "https://org:@dev.azure.com/org/p/_git/r", "https://org@evil.example/org/p/_git/r",
                    "http://org@dev.azure.com/org/p/_git/r", "https://org@dev.azure.com:444/org/p/_git/r",
                    "https://org@dev.azure.com/org/p/_git/r?token=secret", "https://org@dev.azure.com/org/%2e%2e/_git/r",
                    "https://other@dev.azure.com/other/p/_git/r"]:
            with self.subTest(url=url), self.assertRaises(common.ReviewError):
                common.canonical_ado_metadata_url(url, "https://dev.azure.com/org")

    @unittest.skipUnless(SPEC, "runtime not implemented yet")
    def test_slug_matches_daemon(self):
        self.assertEqual(common.repo_slug("My Repo_v2.Name"), "my-repo_v2.name")


@unittest.skipUnless(SPEC, "runtime not implemented yet")
class GitLifecycleTests(unittest.TestCase):
    def setUp(self):
        job_dir = os.environ.get("CLAUDE_JOB_DIR")
        job_tmp = Path(job_dir) / "tmp" if job_dir else None
        temp_parent = job_tmp if job_tmp and job_tmp.is_dir() else None
        self.temp = tempfile.TemporaryDirectory(dir=temp_parent)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "store"
        self.source = Path(self.temp.name) / "source"
        for root in [self.root, self.source]:
            root.mkdir()
            git(root, "init", "-b", "main")
            git(root, "config", "user.name", "Lifecycle Test")
            git(root, "config", "user.email", "lifecycle@example.invalid")
        (self.source / "file.txt").write_text("base\n", encoding="utf-8")
        git(self.source, "add", "file.txt")
        git(self.source, "commit", "-m", "base")
        self.base = git(self.source, "rev-parse", "HEAD")
        (self.source / "file.txt").write_text("head\n", encoding="utf-8")
        git(self.source, "commit", "-am", "head")
        self.head = git(self.source, "rev-parse", "HEAD")
        (self.root / ".gitmodules").write_text(
            '[submodule "repos/Nova"]\n path = repos/Nova\n'
            ' url = https://dev.azure.com/org/project/_git/Nova\n', encoding="utf-8")
        (self.root / "PRs").mkdir()
        (self.root / "PRs" / ".gitkeep").touch()
        git(self.root, "add", ".gitmodules", "PRs")
        git(self.root, "update-index", "--add", "--cacheinfo", "160000", self.base, "repos/Nova")
        git(self.root, "commit", "-m", "store")
        self.store_base = git(self.root, "rev-parse", "HEAD")
        (self.root / "unrelated.json").write_text("keep", encoding="utf-8")
        self.snapshot = {
            "pr_number": 42, "repository": {"id": "repo-id", "name": "Nova", "project": {"name": "project"}},
            "head_sha": self.head, "target_sha": self.base, "base_sha": self.base,
            "source_ref": "refs/heads/main", "target_ref": "refs/heads/main",
            "iteration_id": 1, "snapshot_fingerprint": "snapshot-1", "title": "Test review"
        }
        self.module = {"name": "repos/Nova", "path": "repos/Nova", "url": "https://dev.azure.com/org/project/_git/Nova"}
        self.state = common.ReviewState(self.root)

    def reserve(self, slot=None):
        with self.state.lock():
            return self.state.reserve(self.snapshot, self.module, slot=slot)

    def populate_source(self, manifest):
        slot = self.state.slot_path(manifest)
        module = slot / "repos/Nova"
        git(slot, "-c", "protocol.file.allow=always", "-c", f"url.{self.source.as_uri()}.insteadOf={self.module['url']}",
            "submodule", "update", "--init", "--", "repos/Nova")
        git(module, "checkout", "--detach", self.head)
        manifest["source_prepared"] = True
        manifest["base_sha"] = self.base
        manifest["status"] = "ready"
        self.state.save(manifest)
        return module

    def add_report(self, manifest):
        slot = self.state.slot_path(manifest)
        rel = "PRs/nova-42/PR_Setup_01.json"
        common.write_new_json(slot / rel, {"run_id": manifest["run_id"]})
        manifest["artifacts"] = {rel: common.file_hash(slot / rel)}
        manifest["status"] = "ready"
        self.state.save(manifest)
        return rel

    def test_reserves_isolated_worktree_without_switching_root(self):
        manifest = self.reserve()
        self.assertEqual(manifest["slot"], 0)
        slot = self.state.slot_path(manifest)
        self.assertEqual(git(slot, "branch", "--show-current"), "review/nova-42")
        self.assertEqual(git(self.root, "branch", "--show-current"), "main")
        self.assertEqual((self.root / "unrelated.json").read_text(), "keep")
        self.assertEqual(git(self.root, "rev-parse", "HEAD"), self.store_base)

    def test_resume_reuses_slot_and_refuses_changed_snapshot(self):
        first = self.reserve(2)
        again = self.reserve()
        self.assertEqual(first["run_id"], again["run_id"])
        changed = dict(self.snapshot, snapshot_fingerprint="changed")
        with self.state.lock(), self.assertRaises(common.ReviewError):
            self.state.reserve(changed, self.module)

    def test_rejects_unmanaged_slot_directory(self):
        (self.root / ".worktrees/review-slot-3").mkdir(parents=True)
        with self.assertRaises(common.ReviewError):
            self.reserve(3)

    def test_rejects_symlink_worktree_root(self):
        (self.root / ".worktrees").symlink_to(self.source, target_is_directory=True)
        with self.assertRaises(common.ReviewError):
            self.reserve()

    def test_rejects_wrong_repository_when_submodule_empty(self):
        manifest = self.reserve()
        empty = self.state.slot_path(manifest) / "repos/Nova"
        empty.mkdir(exist_ok=True, parents=True)
        with self.assertRaises(common.ReviewError):
            common.ensure_repository(empty)

    def test_archive_commits_only_report_and_gitlink_then_reuses_slot(self):
        import review_reset
        manifest = self.reserve(1)
        self.populate_source(manifest)
        rel = self.add_report(manifest)
        archive = review_reset.reset_run(self.state, 1, mode="archive")
        archive_ref = archive["archive_branch"]
        self.assertTrue(archive_ref.startswith("archive/review/nova-42/"))
        self.assertEqual(git(self.root, "show", f"{archive_ref}:{rel}"), json.dumps({"run_id": manifest["run_id"]}, indent=2, sort_keys=True))
        self.assertEqual(git(self.root, "rev-parse", f"{archive_ref}:repos/Nova"), self.head)
        self.assertNotEqual(git(self.root, "rev-parse", archive_ref), self.store_base)
        self.assertEqual(git(self.root, "branch", "--show-current"), "main")
        self.assertEqual(git(self.state.slot_path(manifest), "status", "--porcelain"), "")
        fresh = self.reserve(1)
        self.assertNotEqual(fresh["run_id"], manifest["run_id"])
        self.assertEqual(git(self.state.slot_path(fresh), "rev-parse", "HEAD"), self.store_base)

    def test_discard_rejects_modified_artifact_and_unrelated_file(self):
        import review_reset
        manifest = self.reserve()
        rel = self.add_report(manifest)
        (self.state.slot_path(manifest) / rel).write_text("user changed this", encoding="utf-8")
        with self.assertRaises(common.ReviewError):
            review_reset.reset_run(self.state, 0, mode="discard", confirm="nova-42")
        self.assertEqual(git(self.state.slot_path(manifest), "branch", "--show-current"), "review/nova-42")

    def test_discard_deletes_only_run_local_ref_and_owned_report(self):
        import review_reset
        manifest = self.reserve()
        self.populate_source(manifest)
        rel = self.add_report(manifest)
        result = review_reset.reset_run(self.state, 0, mode="discard", confirm="nova-42")
        self.assertEqual(result["status"], "discarded")
        self.assertFalse((self.state.slot_path(manifest) / rel).exists())
        self.assertNotIn("review/nova-42", git(self.root, "branch", "--list"))
        self.assertEqual((self.root / "unrelated.json").read_text(), "keep")

    def test_dry_run_is_read_only_and_refuses_unknown_slot(self):
        import review_reset
        manifest = self.reserve()
        rel = self.add_report(manifest)
        before = self.state.manifest_path(0).read_bytes()
        review_reset.reset_run(self.state, 0, mode="discard", dry_run=True)
        self.assertEqual(before, self.state.manifest_path(0).read_bytes())
        self.assertTrue((self.state.slot_path(manifest) / rel).exists())
        with self.assertRaises(common.ReviewError):
            review_reset.reset_run(self.state, 99, dry_run=True)

    def test_prepare_source_pins_head_and_validates_base(self):
        self.assertIsNotNone(importlib.util.find_spec("review_setup"))
        import review_setup
        manifest = self.reserve()
        self.populate_source(manifest)
        module = self.state.slot_path(manifest) / "repos/Nova"
        # Objects already exist; preparation must not move the canonical checkout.
        base = review_setup.resolve_base(module, self.snapshot, max_depth=100)
        self.assertEqual(base, self.base)
        self.assertEqual(git(module, "rev-parse", "HEAD"), self.head)

    def test_base_validation_rejects_unrelated_provider_base(self):
        self.assertIsNotNone(importlib.util.find_spec("review_setup"))
        import review_setup
        manifest = self.reserve()
        module = self.populate_source(manifest)
        bad = dict(self.snapshot, base_sha=self.head)
        with self.assertRaises(common.ReviewError):
            review_setup.resolve_base(module, bad, max_depth=100)

    def test_full_setup_publishes_and_resumes_without_duplicate_artifacts(self):
        from unittest.mock import patch
        import review_setup

        class Client:
            org_url = "https://dev.azure.com/org"

        def prepared(state, manifest, **kwargs):
            source = self.populate_source(manifest)
            return source, self.base

        report = {"complete": True, "warnings": [], "work_items": {"roots": [], "nodes": [], "complete": True},
                  "files": [], "provenance": {"commits": {}, "pull_requests": []}}
        manifest = self.reserve()
        with patch("review_context.resolve_pr", return_value=self.snapshot), \
             patch("review_setup.prepare_source", side_effect=prepared), \
             patch("review_setup._collect", return_value=report), \
             patch("review_context.render_markdown", return_value="# Setup\n"):
            first = review_setup.setup_run(self.root, 42, client=Client())
            second = review_setup.setup_run(self.root, 42, client=Client())
        self.assertEqual(first["run_id"], second["run_id"])
        self.assertTrue(first["complete"])
        self.assertEqual(len(first["artifacts"]), 2)
        self.assertEqual(git(self.root, "rev-parse", "HEAD"), self.store_base)

    def test_report_publish_recovers_after_json_before_manifest_update(self):
        from unittest.mock import patch
        import review_setup
        manifest = self.reserve()
        manifest["base_sha"] = self.base
        report = {"complete": True, "warnings": []}
        original = self.state.save
        hit = False

        def interrupted(value):
            nonlocal hit
            if value.get("artifacts") and not hit:
                hit = True
                raise common.ReviewError("simulated crash")
            return original(value)

        with patch.object(self.state, "save", side_effect=interrupted), \
             patch("review_context.render_markdown", return_value="# Setup\n"):
            with self.assertRaises(common.ReviewError):
                review_setup.publish_report(self.state, manifest, report)
        manifest = self.state.load(0)
        with patch("review_context.render_markdown", return_value="# Setup\n"):
            review_setup.publish_report(self.state, manifest, report)
        self.assertEqual(len(manifest["artifacts"]), 2)
        self.assertEqual(manifest["status"], "ready")

    def test_setup_resumes_partially_published_report(self):
        from unittest.mock import patch
        import review_setup
        manifest = self.reserve()
        manifest["base_sha"] = self.base
        report = {"complete": True, "warnings": []}
        original = self.state.save
        crashed = False

        def interrupted(value):
            nonlocal crashed
            if value.get("artifacts") and not crashed:
                crashed = True
                raise common.ReviewError("simulated crash")
            return original(value)

        with patch.object(self.state, "save", side_effect=interrupted), patch("review_context.render_markdown", return_value="# Setup\n"):
            with self.assertRaises(common.ReviewError):
                review_setup.publish_report(self.state, manifest, report)
        client = type("Client", (), {"org_url": "https://dev.azure.com/org"})()
        with patch("review_context.resolve_pr", return_value=self.snapshot), patch("review_context.render_markdown", return_value="# Setup\n"):
            restored = review_setup.setup_run(self.root, 42, client=client)
        self.assertEqual(restored["status"], "ready")
        self.assertEqual(len(restored["artifacts"]), 2)

    def test_archive_missing_identity_preserves_index_and_state(self):
        import review_reset
        manifest = self.reserve()
        self.add_report(manifest)
        slot = self.state.slot_path(manifest)
        git(slot, "config", "user.email", "")
        git(slot, "config", "author.email", "")
        git(slot, "config", "committer.email", "")
        git(slot, "config", "user.useConfigOnly", "true")
        before = self.state.manifest_path(0).read_bytes()
        from unittest.mock import patch
        environment = {key: value for key, value in os.environ.items()
                       if not key.startswith(("GIT_AUTHOR_", "GIT_COMMITTER_", "GIT_CONFIG_")) and key != "EMAIL"}
        environment.update({"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull,
                            "GIT_CONFIG_NOSYSTEM": "1"})
        with patch.dict(os.environ, environment, clear=True), self.assertRaises(common.ReviewError):
            review_reset.reset_run(self.state, 0, mode="archive")
        self.assertEqual(git(slot, "diff", "--cached", "--name-only"), "")
        self.assertEqual(self.state.manifest_path(0).read_bytes(), before)
        self.assertEqual(git(slot, "branch", "--show-current"), manifest["branch"])

    def test_archive_refuses_unrelated_staged_changes(self):
        import review_reset
        manifest = self.reserve()
        self.add_report(manifest)
        slot = self.state.slot_path(manifest)
        (slot / "secret.txt").write_text("must not commit", encoding="utf-8")
        git(slot, "add", "secret.txt")
        with self.assertRaises(common.ReviewError):
            review_reset.reset_run(self.state, 0, mode="archive")
        self.assertEqual(git(slot, "diff", "--cached", "--name-only"), "secret.txt")


class WarmPoolTests(unittest.TestCase):
    def setUp(self):
        GitLifecycleTests.setUp(self)
        # A second, independent source repo/module -- needed to prove slot
        # numbering and store identity never collide across repos.
        self.source2 = Path(self.temp.name) / "source2"
        self.source2.mkdir()
        git(self.source2, "init", "-b", "main")
        git(self.source2, "config", "user.name", "Lifecycle Test")
        git(self.source2, "config", "user.email", "lifecycle@example.invalid")
        (self.source2 / "other.txt").write_text("other-base\n", encoding="utf-8")
        git(self.source2, "add", "other.txt")
        git(self.source2, "commit", "-m", "other-base")
        self.other_base = git(self.source2, "rev-parse", "HEAD")
        (self.source2 / "other.txt").write_text("other-head\n", encoding="utf-8")
        git(self.source2, "commit", "-am", "other-head")
        self.other_head = git(self.source2, "rev-parse", "HEAD")
        self.other_module = {"name": "repos/Other", "path": "repos/Other",
                              "url": "https://dev.azure.com/org/project/_git/Other"}
        gitmodules = self.root / ".gitmodules"
        gitmodules.write_text(
            gitmodules.read_text(encoding="utf-8")
            + '[submodule "repos/Other"]\n path = repos/Other\n'
              ' url = https://dev.azure.com/org/project/_git/Other\n',
            encoding="utf-8",
        )
        git(self.root, "add", ".gitmodules")
        git(self.root, "update-index", "--add", "--cacheinfo", "160000", self.other_base, "repos/Other")
        git(self.root, "commit", "-m", "add other submodule")
        self.store_base = git(self.root, "rev-parse", "HEAD")

    def reserve(self, slot=None):
        # WarmPoolTests does not subclass GitLifecycleTests (only borrows its
        # setUp), so this legacy schema-1 helper is duplicated here rather
        # than inherited -- needed to prove pool state and legacy per-PR
        # manifests never interfere with each other (brief req #8).
        with self.state.lock():
            return self.state.reserve(self.snapshot, self.module, slot=slot)

    def fixture_remote(self):
        from unittest.mock import patch
        return patch.dict(os.environ, {
            "GIT_CONFIG_COUNT": "2",
            "GIT_CONFIG_KEY_0": f"url.{self.source.as_uri()}.insteadOf",
            "GIT_CONFIG_VALUE_0": self.module["url"],
            "GIT_CONFIG_KEY_1": "protocol.file.allow",
            "GIT_CONFIG_VALUE_1": "always",
        })

    def fixture_remote_both(self):
        """Redirects both fixture modules' HTTPS URLs to local file:// repos."""
        from unittest.mock import patch
        return patch.dict(os.environ, {
            "GIT_CONFIG_COUNT": "3",
            "GIT_CONFIG_KEY_0": f"url.{self.source.as_uri()}.insteadOf",
            "GIT_CONFIG_VALUE_0": self.module["url"],
            "GIT_CONFIG_KEY_1": f"url.{self.source2.as_uri()}.insteadOf",
            "GIT_CONFIG_VALUE_1": self.other_module["url"],
            "GIT_CONFIG_KEY_2": "protocol.file.allow",
            "GIT_CONFIG_VALUE_2": "always",
        })

    def _recorded_argv(self):
        """Wraps the real subprocess.Popen (SourcePool's only OS-process entry
        point, via run_owned/run_git) to record every argv actually executed,
        while still performing the real call -- per the brief's explicit
        instruction that "no submodule update/deinit" and "no redundant
        fetch" must be proven by observing real Git invocations, not by
        mocking the public refresh() method."""
        from unittest.mock import patch
        real_popen = common.subprocess.Popen
        calls: list[list[str]] = []

        def wrapper(args, *a, **kw):
            calls.append(list(args))
            return real_popen(args, *a, **kw)

        return patch.object(common.subprocess, "Popen", side_effect=wrapper), calls

    def test_six_nested_checkouts_share_objects_not_heads_or_indexes(self):
        expected_store = self.state.common / "review-setup" / "source-stores" / "repos-nova.git"
        with self.fixture_remote(), self.state.lock():
            pool = common.SourcePool(self.state, self.module, reserve_bytes=0)
            self.assertEqual(pool.store.resolve(), expected_store.resolve())
            rows = pool.warm(range(6))
            self.assertEqual(len(rows), 6)
            self.assertEqual([r["slot"] for r in rows], [f"Nova-{i}" for i in range(6)])
            self.assertEqual([r["slot_path"] for r in rows], [f".worktrees/Nova-{i}" for i in range(6)])
            stores, indexes, sources = set(), set(), []
            for row in rows:
                outer = self.root / row["slot_path"]
                source = outer / self.module["path"]
                sources.append(source)
                common_dir = git(source, "rev-parse", "--path-format=absolute", "--git-common-dir")
                stores.add(common_dir)
                self.assertEqual(Path(common_dir).resolve(), expected_store.resolve())
                indexes.add(git(source, "rev-parse", "--path-format=absolute", "--git-path", "index"))
                self.assertEqual(git(source, "rev-parse", "HEAD"), self.head)
                self.assertEqual(git(source, "status", "--porcelain"), "")
                self.assertIsNone(common.current_ref(source), "nested source worktree must be detached")
                self.assertEqual(common.ensure_repository(source), source.resolve())
                git(outer, "add", "--", self.module["path"])
                self.assertEqual(git(outer, "rev-parse", ":repos/Nova"), self.head)
                git(outer, "restore", "--staged", "--", self.module["path"])
                self.assertEqual(git(outer, "diff", "--cached", "--name-only"), "")
            self.assertEqual(len(stores), 1, msg=f"expected one common-dir, saw: {stores}")
            self.assertEqual(len(indexes), 6, msg=f"expected six distinct indexes, saw: {indexes}")

            # Two distinct worktree registries: the bare store's own list of
            # the six nested source worktrees, and the NOVA root's own list
            # of the six outer worktrees -- assert both, not just one.
            # `git worktree list` against a bare repo always adds a leading
            # pseudo-entry for the bare repo itself; exclude it explicitly
            # rather than asserting a raw count that would be wrong.
            store_rows = common.worktree_rows(pool.store)
            self.assertEqual(len(store_rows), 7, msg=f"expected 6 nested + 1 bare pseudo-entry: {store_rows}")
            nested_rows = [row for row in store_rows if row["worktree"] != pool.store.resolve()]
            self.assertEqual({row["worktree"] for row in nested_rows}, {s.resolve() for s in sources})
            for row in nested_rows:
                self.assertTrue(row.get("detached"), msg=f"expected detached nested worktree, saw: {row}")
            root_registered = self.state.registered()
            outer_paths = {(self.root / row["slot_path"]).resolve() for row in rows}
            self.assertTrue(outer_paths.issubset(set(root_registered)))

            first = self.root / rows[0]["slot_path"] / self.module["path"]
            git(first, "checkout", "--detach", self.base)
            for row in rows[1:]:
                other_source = self.root / row["slot_path"] / self.module["path"]
                self.assertEqual(git(other_source, "rev-parse", "HEAD"), self.head,
                                  msg=f"slot {row['slot']} HEAD must be independent of slot 0's mutation")
            self.assertEqual(git(first, "rev-parse", "HEAD"), self.base)

            # Compatibility gate: outer gitlink staging still works once the
            # nested HEAD has diverged from whatever was previously staged.
            outer0 = self.root / rows[0]["slot_path"]
            git(outer0, "add", "--", self.module["path"])
            self.assertEqual(git(outer0, "rev-parse", ":repos/Nova"), self.base)
            git(outer0, "restore", "--staged", "--", self.module["path"])
            self.assertEqual(git(outer0, "diff", "--cached", "--name-only"), "")
        self.assertEqual(git(self.root, "rev-parse", "HEAD"), self.store_base)

    def test_setup_status_and_dry_run_are_read_only_and_cover_every_slot(self):
        import review_setup

        def snapshot_tree(root):
            rows = {}
            for path in sorted(root.rglob("*")):
                relative = path.relative_to(root).as_posix()
                if path.is_symlink():
                    rows[relative] = ("link", os.readlink(path))
                elif path.is_file():
                    rows[relative] = ("file", path.read_bytes())
                else:
                    rows[relative] = ("dir", None)
            return rows

        before = snapshot_tree(self.root)
        status = review_setup.status_run(self.root)
        dry = review_setup.warm_all_run(self.root, dry_run=True)
        self.assertEqual(len(status), 12)
        self.assertEqual(len(dry), 12)
        self.assertEqual(
            [row["slot"] for row in status],
            [f"{repo}-{slot}" for repo in ("Nova", "Other") for slot in range(6)],
        )
        self.assertTrue(all(row["status"] == "absent" for row in status))
        self.assertTrue(all(row["action"] == "would-warm" for row in dry))
        self.assertEqual(snapshot_tree(self.root), before)
        self.assertFalse((self.state.common / "review-setup").exists())

    def test_warm_status_and_dry_run_report_reuse_without_mutation(self):
        import review_setup

        def snapshot_tree(root):
            rows = {}
            for path in sorted(root.rglob("*")):
                relative = path.relative_to(root).as_posix()
                value = path.lstat()
                rows[relative] = (
                    stat.S_IFMT(value.st_mode),
                    stat.S_IMODE(value.st_mode),
                    value.st_size,
                    value.st_mtime_ns,
                    os.readlink(path) if path.is_symlink() else hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None,
                )
            return rows

        with self.fixture_remote():
            warmed = review_setup.warm_run(self.root, "Nova", 0)
        self.assertEqual(warmed["status"], "warm")
        before = snapshot_tree(self.root)
        status = next(row for row in review_setup.status_run(self.root) if row["slot"] == "Nova-0")
        dry = review_setup.warm_run(self.root, "Nova", 0, dry_run=True)
        self.assertEqual(status["status"], "warm")
        self.assertEqual(dry["status"], "warm")
        self.assertEqual(dry["action"], "reuse")
        self.assertEqual(snapshot_tree(self.root), before)

    def explicit_setup(self, snapshot=None, slot=0):
        from unittest.mock import patch
        import review_setup
        snapshot = snapshot or self.snapshot
        report = {"complete": True, "warnings": [], "files": [],
                  "work_items": {"roots": [], "nodes": []}, "provenance": {}}
        client = type("Client", (), {"org_url": "https://dev.azure.com/org"})()
        with self.fixture_remote(), patch("review_context.resolve_pr", return_value=snapshot), \
             patch("review_setup._collect", side_effect=lambda *a, **k: dict(report)), \
             patch("review_context.render_markdown", return_value="# regenerated\n"):
            return review_setup.setup_run(self.root, snapshot["pr_number"], slot=slot, client=client)

    def test_explicit_setup_cleans_all_content_and_recollects(self):
        first = self.explicit_setup()
        outer = self.root / first["slot_path"]
        source = outer / self.module["path"]
        for repo, tracked in ((outer, ".gitmodules"), (source, "file.txt")):
            (repo / tracked).write_text("changed\n")
            git(repo, "add", tracked)
            (repo / "notes.txt").write_text("discard")
            (repo / ".gitignore").write_text("build/\n")
            (repo / "build").mkdir()
            (repo / "build/local.config").write_text("discard ignored")
        second = self.explicit_setup()
        self.assertEqual(second["status"], "ready")
        self.assertNotEqual(first["report_paths"], second["report_paths"])
        self.assertTrue(second["report_paths"][0].endswith("02.json"))
        for repo in (outer, source):
            self.assertFalse((repo / "notes.txt").exists())
            self.assertFalse((repo / "build").exists())
            self.assertEqual(git(repo, "diff", "--cached", "--name-only"), "")
        self.assertEqual((source / "file.txt").read_text(), "head\n")
        self.assertEqual((self.root / "unrelated.json").read_text(), "keep")

    def test_explicit_switch_preserves_committed_branch_and_retention(self):
        first = self.explicit_setup()
        outer = self.root / first["slot_path"]
        git(outer, "add", "PRs")
        git(outer, "commit", "-m", "saved review")
        tip = git(outer, "rev-parse", "HEAD")
        refs = dict(first["retention_refs"])
        second = self.explicit_setup(dict(self.snapshot, pr_number=43, snapshot_fingerprint="pr43"))
        self.assertEqual(second["key"], "nova-43")
        self.assertEqual(git(self.root, "rev-parse", first["branch"]), tip)
        self.assertIn("PR_Setup_01.md", git(self.root, "ls-tree", "-r", "--name-only", first["branch"]))
        source = outer / self.module["path"]
        for ref in refs.values():
            self.assertTrue(git(source, "rev-parse", ref))
        history = list((self.state.directory / "setup-history").glob("*.json"))
        self.assertTrue(any(json.loads(p.read_text())["run_id"] == first["run_id"] for p in history))

    def test_explicit_cleanup_refuses_nested_repo_and_archive_before_deletion(self):
        first = self.explicit_setup()
        outer = self.root / first["slot_path"]
        note = outer / "notes.txt"
        note.write_text("preserve on refusal")
        nested = outer / "unmanaged"
        nested.mkdir()
        git(nested, "init")
        with self.assertRaises(common.ReviewError):
            self.explicit_setup()
        self.assertEqual(note.read_text(), "preserve on refusal")
        import shutil
        shutil.rmtree(nested)
        first["reset_mode"] = "archive"
        first["reset_phase"] = "validated"
        self.state.save(first)
        before = self.state.pool_run_path(first["repo_key"], 0).read_bytes()
        with self.assertRaises(common.ReviewError):
            self.explicit_setup()
        self.assertEqual(before, self.state.pool_run_path(first["repo_key"], 0).read_bytes())
        self.assertTrue(note.exists())

    def test_explicit_cleanup_resumes_after_source_clean_before_outer_clean(self):
        from unittest.mock import patch
        import review_setup
        first = self.explicit_setup()
        outer = self.root / first["slot_path"]
        source = outer / self.module["path"]
        (outer / "note").write_text("remove")
        (source / "file.txt").write_text("remove")
        real = review_setup.cleanup_worktree
        def interrupted(repo, *args, **kwargs):
            if repo == outer:
                raise common.ReviewError("simulated interrupted cleanup")
            return real(repo, *args, **kwargs)
        with patch.object(review_setup, "cleanup_worktree", side_effect=interrupted), self.assertRaises(common.ReviewError):
            self.explicit_setup()
        self.assertTrue((outer / "note").exists())
        again = self.explicit_setup()
        self.assertEqual(again["status"], "ready")
        self.assertFalse((outer / "note").exists())
        self.assertTrue(again["report_paths"][0].endswith("02.json"))

    def test_explicit_cleanup_preserves_other_slot_and_symlink_target(self):
        import review_setup
        first = self.explicit_setup()
        with self.fixture_remote():
            review_setup.warm_run(self.root, "Nova", 1)
        outer = self.root / first["slot_path"]
        other = self.root / ".worktrees/Nova-1"
        (other / "notes").write_text("preserve")
        outside = Path(self.temp.name) / "outside"
        outside.mkdir()
        (outside / "config").write_text("preserve")
        (outer / "link").symlink_to(outside, target_is_directory=True)
        self.explicit_setup()
        self.assertFalse((outer / "link").exists())
        self.assertEqual((outside / "config").read_text(), "preserve")
        self.assertEqual((other / "notes").read_text(), "preserve")

    def test_explicit_setup_auto_allocation_never_cleans_occupied_review(self):
        first = self.explicit_setup()
        outer = self.root / first["slot_path"]
        (outer / "notes").write_text("preserve")
        second = self.explicit_setup(dict(self.snapshot, pr_number=43, snapshot_fingerprint="pr43"), slot=None)
        self.assertEqual(second["slot_number"], 1)
        self.assertEqual((outer / "notes").read_text(), "preserve")

    def test_explicit_setup_switch_back_preserves_artifact_sequence(self):
        first = self.explicit_setup()
        self.explicit_setup(dict(self.snapshot, pr_number=43, snapshot_fingerprint="pr43"))
        again = self.explicit_setup()
        self.assertNotEqual(first["run_id"], again["run_id"])
        self.assertTrue(again["report_paths"][0].endswith("02.json"))

    def test_explicit_cleanup_refuses_active_lock_and_git_operation(self):
        first = self.explicit_setup()
        outer = self.root / first["slot_path"]
        note = outer / "note"
        note.write_text("keep")
        with self.state.lock(), self.assertRaises(common.ReviewError):
            self.explicit_setup()
        marker = Path(git(outer, "rev-parse", "--path-format=absolute", "--git-path", "MERGE_HEAD"))
        marker.write_text(self.head)
        with self.assertRaises(common.ReviewError):
            self.explicit_setup()
        self.assertEqual(note.read_text(), "keep")

    def test_explicit_setup_cleans_unreserved_warm_slot_but_warm_does_not(self):
        import review_setup
        with self.fixture_remote():
            warmed = review_setup.warm_run(self.root, "Nova", 0)
        outer = self.root / warmed["slot_path"]
        source = outer / self.module["path"]
        (source / "file.txt").write_text("discard edit")
        (outer / "notes").write_text("discard note")
        before = (outer / "notes").read_bytes()
        with self.fixture_remote():
            self.assertEqual(review_setup.warm_run(self.root, "Nova", 0)["status"], "occupied")
            review_setup.warm_run(self.root, "Nova", 0, dry_run=True)
        self.assertEqual(before, (outer / "notes").read_bytes())
        result = self.explicit_setup()
        self.assertEqual(result["status"], "ready")
        self.assertFalse((outer / "notes").exists())
        self.assertEqual((source / "file.txt").read_text(), "head\n")

    def test_explicit_switch_resumes_after_detach_without_losing_branch(self):
        from unittest.mock import patch
        import review_setup
        first = self.explicit_setup()
        old_tip = git(self.root, "rev-parse", first["branch"])
        new = dict(self.snapshot, pr_number=43, snapshot_fingerprint="pr43")
        real = review_setup.run_git
        failed = False
        def interrupted(repo, args, **kwargs):
            nonlocal failed
            if not failed and args[:4] == ["-c", "submodule.recurse=false", "checkout", "--detach"]:
                failed = True
                real(repo, args, **kwargs)
                raise common.ReviewError("crash after detach")
            return real(repo, args, **kwargs)
        with patch.object(review_setup, "run_git", side_effect=interrupted), self.assertRaises(common.ReviewError):
            self.explicit_setup(new)
        result = self.explicit_setup(new)
        self.assertEqual(result["key"], "nova-43")
        self.assertEqual(git(self.root, "rev-parse", first["branch"]), old_tip)

    def test_pool_setup_derives_nullable_provider_base(self):
        from unittest.mock import patch
        import review_setup

        class Client:
            org_url = "https://dev.azure.com/org"

        snapshot = dict(self.snapshot, base_sha=None, snapshot_fingerprint="nullable-base")
        report = {
            "complete": True,
            "warnings": [],
            "work_items": {"roots": [], "nodes": [], "complete": True},
            "files": [],
            "provenance": {"commits": {}, "pull_requests": []},
        }
        with self.fixture_remote(), \
             patch("review_context.resolve_pr", return_value=snapshot), \
             patch("review_setup._collect", return_value=report), \
             patch("review_context.render_markdown", return_value="# Setup\n"):
            manifest = review_setup.setup_run(self.root, 42, slot=0, client=Client())
        self.assertEqual(manifest["base_sha"], self.base)
        self.assertEqual(
            git(self.root / manifest["slot_path"] / self.module["path"], "rev-parse", manifest["retention_refs"]["base"]),
            self.base,
        )

    def test_warm_orchestration_is_repo_then_slot_sequential_and_idempotent(self):
        from unittest.mock import patch
        import review_setup

        calls = []
        active = False
        original = common.SourcePool.warm

        def recording(pool, slots):
            nonlocal active
            self.assertFalse(active, "warm calls must never overlap")
            active = True
            try:
                calls.append((pool.repo_name, list(slots)))
                return original(pool, slots)
            finally:
                active = False

        with self.fixture_remote_both(), patch.object(common.SourcePool, "warm", recording):
            rows = review_setup.warm_all_run(self.root)
        self.assertEqual(len(rows), 12)
        self.assertEqual(
            calls,
            [("Nova", [slot]) for slot in range(6)]
            + [("Other", [slot]) for slot in range(6)],
        )
        self.assertTrue(all(row["status"] == "warm" for row in rows))

    def test_cli_new_modes_and_invalid_combinations(self):
        from unittest.mock import patch
        import review_setup

        def invoke(argv):
            stdout, stderr = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                try:
                    code = review_setup.main(["--root", str(self.root), *argv])
                except SystemExit as exc:
                    code = exc.code
            return code, stdout.getvalue(), stderr.getvalue()

        before = {
            path.relative_to(self.root).as_posix(): path.read_bytes()
            for path in self.root.rglob("*")
            if path.is_file() and not path.is_symlink()
        }
        code, output, _ = invoke(["--status"])
        self.assertEqual(code, 0)
        self.assertIn("Nova-0", output)
        self.assertIn("Other-5", output)
        code, output, _ = invoke(["--warm-all", "--dry-run"])
        self.assertEqual(code, 0)
        self.assertIn("would-warm", output)
        after = {
            path.relative_to(self.root).as_posix(): path.read_bytes()
            for path in self.root.rglob("*")
            if path.is_file() and not path.is_symlink()
        }
        self.assertEqual(after, before)

        for argv in (
            ["--warm-all", "--slot", "0"],
            ["--warm", "--repo", "Nova"],
            ["--status", "42"],
            ["--preflight", "--warm-all"],
        ):
            with self.subTest(argv=argv):
                code, _, _ = invoke(argv)
                self.assertNotEqual(code, 0)

        with self.fixture_remote(), patch.object(
            review_setup, "preflight", return_value={"python": "x", "git": "x", "curl": "x"}
        ):
            code, output, _ = invoke(["--warm", "--repo", "Nova", "--slot", "0"])
        self.assertEqual(code, 0)
        self.assertIn("Nova-0", output)

    def test_pool_setup_uses_repo_local_slot_and_exact_shared_objects(self):
        from unittest.mock import patch
        import review_setup

        class Client:
            org_url = "https://dev.azure.com/org"

        report = {
            "complete": True,
            "warnings": [],
            "work_items": {"roots": [], "nodes": [], "complete": True},
            "files": [],
            "provenance": {"commits": {}, "pull_requests": []},
        }
        patcher, calls = self._recorded_argv()
        with self.fixture_remote(), patcher, \
             patch("review_context.resolve_pr", return_value=self.snapshot), \
             patch("review_setup._collect", return_value=report), \
             patch("review_context.render_markdown", return_value="# Setup\n"):
            manifest = review_setup.setup_run(self.root, 42, slot=0, client=Client())
            again = review_setup.setup_run(self.root, 42, slot=0, client=Client())
        self.assertEqual(manifest["schema_version"], 2)
        self.assertEqual(manifest["slot_id"], "Nova-0")
        self.assertEqual(manifest["run_id"], again["run_id"])
        self.assertEqual(manifest["status"], "ready")
        source = self.root / manifest["slot_path"] / self.module["path"]
        self.assertEqual(git(source, "rev-parse", "HEAD"), self.head)
        self.assertEqual(git(source, "rev-parse", "HEAD"), manifest["snapshot"]["head_sha"])
        self.assertEqual(git(self.root, "branch", "--show-current"), "main")
        self.assertEqual(
            git(self.root / manifest["slot_path"], "branch", "--show-current"),
            "review/nova-42",
        )
        self.assertEqual(
            git(source, "rev-parse", "--path-format=absolute", "--git-common-dir"),
            str((self.state.common / "review-setup/source-stores/repos-nova.git").resolve()),
        )
        self.assertFalse(self.state.manifest_path(0).exists())
        fetches = [call for call in calls if "fetch" in call]
        self.assertEqual(len(fetches), 1, msg=f"warm cache should satisfy exact PR objects: {fetches}")
        self.assertTrue(all(any(a.startswith("--depth=") for a in call) for call in fetches))
        for role in ("head", "base", "target", "baseline"):
            ref = manifest["retention_refs"][role]
            expected = (
                manifest["baseline_sha"]
                if role == "baseline"
                else manifest["base_sha"]
                if role == "base"
                else manifest["snapshot"][f"{role}_sha"]
            )
            self.assertEqual(git(source, "rev-parse", ref), expected)

    def test_combined_warm_setup_archive_and_warm_reuse(self):
        from unittest.mock import patch
        import review_setup
        import review_reset

        class Client:
            org_url = "https://dev.azure.com/org"

        report = {"complete": True, "warnings": [], "files": [],
                  "work_items": {"roots": [], "nodes": [], "complete": True},
                  "provenance": {"commits": {}, "pull_requests": []}}
        with self.fixture_remote(), patch("review_context.resolve_pr", return_value=self.snapshot), \
             patch("review_setup._collect", return_value=report), \
             patch("review_context.render_markdown", return_value="# Setup\n"):
            warm = review_setup.warm_run(self.root, "Nova", 0)
            repeated = review_setup.warm_run(self.root, "Nova", 0)
            self.assertEqual(warm["baseline_sha"], repeated["baseline_sha"])
            manifest = review_setup.setup_run(self.root, 42, slot=0, client=Client())
            source = self.root / manifest["slot_path"] / self.module["path"]
            self.assertEqual(git(source, "rev-parse", "HEAD"), self.head)
            reset = review_reset.reset_pool_run(self.state, "Nova", 0, run_maintenance=False)
            self.assertEqual(reset["status"], "archived")
            final = review_setup.warm_run(self.root, "Nova", 0)
            self.assertEqual(final["status"], "warm")
            self.assertEqual(git(source, "rev-parse", "HEAD"), final["baseline_sha"])
            for relative in manifest["artifacts"]:
                self.assertTrue(git(self.root, "show", f"{reset['archive_branch']}:{relative}"))

    def test_warm_resolves_canonical_repo_before_slot_validation(self):
        import review_setup

        with self.assertRaisesRegex(common.ReviewError, "canonical"):
            review_setup.warm_run(self.root, "missing", 99)
        self.assertFalse((self.state.common / "review-setup").exists())

    def test_pool_rejects_out_of_range_slots_before_mutation(self):
        pool = common.SourcePool(self.state, self.module, reserve_bytes=0)
        for number in (-1, 6, 90):
            with self.subTest(number=number), self.assertRaises(common.ReviewError):
                pool.warm([number])
        self.assertFalse(pool.store.exists())

    def test_pool_rejects_non_integer_and_bool_slots_before_mutation(self):
        pool = common.SourcePool(self.state, self.module, reserve_bytes=0)
        for value in (True, False, "3", 3.0, None):
            with self.subTest(value=value), self.assertRaises(common.ReviewError):
                pool.warm([value])
        self.assertFalse(pool.store.exists())

    def test_pool_rejects_mixed_valid_and_invalid_batch_without_partial_mutation(self):
        # The brief's own named example: one valid slot mixed with one
        # invalid slot in a single request must reject the whole batch
        # before any mutation -- not warm slot 0 and then fail on slot 6.
        pool = common.SourcePool(self.state, self.module, reserve_bytes=0)
        with self.assertRaises(common.ReviewError):
            pool.warm([0, 6])
        self.assertFalse(pool.store.exists())
        self.assertFalse((self.root / pool.slot_relative_path(0)).exists())

    def test_pool_warm_dedups_duplicate_slot_requests(self):
        with self.fixture_remote(), self.state.lock():
            pool = common.SourcePool(self.state, self.module, reserve_bytes=0)
            rows = pool.warm([0, 0])
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["slot"], "Nova-0")

    def test_pool_construction_requires_validated_gitmodules_entry(self):
        mismatched_url = dict(self.module, url="https://dev.azure.com/org/project/_git/Fake")
        with self.assertRaises(common.ReviewError):
            common.SourcePool(self.state, mismatched_url, reserve_bytes=0)
        unknown_path = {"name": "repos/Ghost", "path": "repos/Ghost",
                         "url": "https://dev.azure.com/org/project/_git/Ghost"}
        with self.assertRaises(common.ReviewError):
            common.SourcePool(self.state, unknown_path, reserve_bytes=0)
        # A validated entry (even hand-built, matching the real .gitmodules
        # row exactly) still constructs successfully.
        common.SourcePool(self.state, self.module, reserve_bytes=0)

    def test_pool_repeat_reuses_checkout_and_cached_baseline(self):
        from unittest.mock import patch
        with self.fixture_remote(), self.state.lock():
            pool = common.SourcePool(self.state, self.module, reserve_bytes=0)
            first = pool.warm([0])[0]
            with patch.object(pool, "refresh", side_effect=AssertionError("redundant fetch")):
                second = pool.warm([0])[0]
            self.assertEqual(first["baseline_sha"], second["baseline_sha"])
            self.assertEqual(first["slot_path"], second["slot_path"])

    def test_pool_repeat_and_multi_repo_warm_never_calls_submodule_or_refetches(self):
        # Per the brief: prove "no submodule update/deinit" and "no
        # redundant fetch" by observing real argv across the entire warm
        # window (fresh, repeat, and multi-repo), not by mocking refresh().
        patcher, calls = self._recorded_argv()
        with self.fixture_remote_both(), self.state.lock(), patcher:
            pool = common.SourcePool(self.state, self.module, reserve_bytes=0)
            other = common.SourcePool(self.state, self.other_module, reserve_bytes=0)
            first = pool.warm([0])[0]
            second = pool.warm([1])[0]  # same repo/store -> cached baseline, no new fetch
            other_first = other.warm([0])[0]
            other_second = other.warm([2])[0]

        self.assertEqual(first["baseline_sha"], second["baseline_sha"])
        self.assertEqual(other_first["baseline_sha"], other_second["baseline_sha"])

        submodule_calls = [c for c in calls if "submodule" in c]
        self.assertEqual(submodule_calls, [], msg=f"submodule invocations must never occur: {submodule_calls}")
        move_calls = [c for c in calls if "worktree" in c and "move" in c]
        self.assertEqual(move_calls, [], msg=f"worktrees must never be moved: {move_calls}")
        forced_calls = [c for c in calls if "--force" in c or "-f" in c]
        self.assertEqual(forced_calls, [], msg=f"unexpected forced argv: {forced_calls}")

        fetch_calls = [c for c in calls if "fetch" in c]
        # Exactly one fetch per distinct store (this repo, then the other
        # repo) -- the two repeat warms above must add zero further fetches.
        self.assertEqual(len(fetch_calls), 2, msg=f"expected exactly two fetches total, saw: {fetch_calls}")
        for call in fetch_calls:
            self.assertTrue(
                any(arg.startswith("--depth=") or arg.startswith("--deepen=") for arg in call),
                msg=f"every fetch must carry an explicit history bound: {call}",
            )

        self.assertFalse((pool.store / "objects" / "info" / "alternates").exists())
        self.assertFalse((other.store / "objects" / "info" / "alternates").exists())

    def test_pool_two_repos_have_independent_slot_numbering_and_stores(self):
        with self.fixture_remote_both(), self.state.lock():
            pool = common.SourcePool(self.state, self.module, reserve_bytes=0)
            other = common.SourcePool(self.state, self.other_module, reserve_bytes=0)
            self.assertNotEqual(pool.store.resolve(), other.store.resolve())
            self.assertNotEqual(pool.state_path.resolve(), other.state_path.resolve())
            nova_rows = pool.warm([0, 1])
            other_rows = other.warm([0, 2])
            self.assertEqual([r["slot"] for r in nova_rows], ["Nova-0", "Nova-1"])
            self.assertEqual([r["slot"] for r in other_rows], ["Other-0", "Other-2"])
            self.assertEqual({r["slot_path"] for r in nova_rows} & {r["slot_path"] for r in other_rows}, set())
            other_source = self.root / other_rows[0]["slot_path"] / "repos/Other"
            self.assertEqual(git(other_source, "rev-parse", "HEAD"), self.other_head)
            nova_source = self.root / nova_rows[0]["slot_path"] / self.module["path"]
            self.assertEqual(git(nova_source, "rev-parse", "HEAD"), self.head)
            self.assertNotEqual(
                git(other_source, "rev-parse", "--path-format=absolute", "--git-common-dir"),
                git(nova_source, "rev-parse", "--path-format=absolute", "--git-common-dir"),
            )

    def test_pool_rejects_occupied_foreign_slot_directory(self):
        target = self.root / ".worktrees" / "Nova-2"
        target.mkdir(parents=True)
        (target / "marker.txt").write_text("not ours", encoding="utf-8")
        with self.fixture_remote(), self.state.lock():
            pool = common.SourcePool(self.state, self.module, reserve_bytes=0)
            with self.assertRaises(common.ReviewError):
                pool.warm([2])
        self.assertTrue((target / "marker.txt").exists())
        self.assertEqual((target / "marker.txt").read_text(encoding="utf-8"), "not ours")
        self.assertFalse((target / ".git").exists())

    def test_pool_repeat_warm_refuses_and_preserves_dirty_staged_untracked_and_ignored_content(self):
        with self.fixture_remote(), self.state.lock():
            pool = common.SourcePool(self.state, self.module, reserve_bytes=0)
            row = pool.warm([3])[0]
            outer = self.root / row["slot_path"]
            source = outer / self.module["path"]

            (outer / "untracked.txt").write_text("outer-untracked", encoding="utf-8")
            (outer / "staged.txt").write_text("outer-staged", encoding="utf-8")
            git(outer, "add", "staged.txt")
            (outer / ".gitignore").write_text("ignored.txt\n", encoding="utf-8")
            (outer / "ignored.txt").write_text("outer-ignored", encoding="utf-8")

            (source / "untracked.txt").write_text("source-untracked", encoding="utf-8")
            (source / "staged.txt").write_text("source-staged", encoding="utf-8")
            git(source, "add", "staged.txt")
            (source / ".gitignore").write_text("ignored.txt\n", encoding="utf-8")
            (source / "ignored.txt").write_text("source-ignored", encoding="utf-8")

            before_outer_status = git(outer, "status", "--porcelain=v1", "--ignored")
            before_source_status = git(source, "status", "--porcelain=v1", "--ignored")
            before_head = git(source, "rev-parse", "HEAD")

            with self.assertRaises(common.ReviewError):
                pool.warm([3])

            self.assertEqual(git(outer, "status", "--porcelain=v1", "--ignored"), before_outer_status)
            self.assertEqual(git(source, "status", "--porcelain=v1", "--ignored"), before_source_status)
            self.assertEqual(git(source, "rev-parse", "HEAD"), before_head)
            for relative, expected in ((outer, "outer"), (source, "source")):
                self.assertEqual((relative / "untracked.txt").read_text(encoding="utf-8"), f"{expected}-untracked")
                self.assertEqual((relative / "staged.txt").read_text(encoding="utf-8"), f"{expected}-staged")
                self.assertEqual((relative / "ignored.txt").read_text(encoding="utf-8"), f"{expected}-ignored")

    def test_pool_state_is_separate_from_legacy_review_run_state(self):
        legacy = self.reserve(4)
        legacy_bytes = self.state.manifest_path(4).read_bytes()
        with self.fixture_remote(), self.state.lock():
            pool = common.SourcePool(self.state, self.module, reserve_bytes=0)
            pool.warm([0])
        self.assertEqual(self.state.manifest_path(4).read_bytes(), legacy_bytes)
        self.assertEqual(self.state.load(4)["slot"], 4)
        self.assertTrue(pool.state_path.exists())
        self.assertTrue(pool.state_path.is_relative_to(self.state.common / "review-setup" / "pool"))
        self.assertFalse(pool.state_path.is_relative_to(self.state.directory / "slots"))
        legacy_slot_path = self.state.slot_path(legacy)
        self.assertNotEqual(legacy_slot_path.name, "Nova-4")

    def test_pool_warm_resumes_idempotently_after_interruption_between_outer_and_nested_worktree(self):
        # Rubric's highest-risk issue: simulate a crash after the shared
        # store was created/fetched and the outer worktree was added, but
        # before the nested source worktree was created. The nested gitlink
        # path is an empty placeholder auto-created by `git worktree add`
        # itself (confirmed empirically against this Git version) -- never
        # populated. A resumed warm() must complete it via the *same*
        # validated identity checks as a fresh build, not a distinct,
        # less-checked "resume" path, and must not re-fetch or re-create
        # the outer worktree.
        with self.fixture_remote(), self.state.lock():
            pool = common.SourcePool(self.state, self.module, reserve_bytes=0)
            baseline = pool.refresh()
            root_head = git(self.root, "rev-parse", "HEAD")
            outer = self.root / pool.slot_relative_path(1)
            outer.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            git(self.root, "worktree", "add", "--detach", str(outer), root_head)
            nested = outer / self.module["path"]
            self.assertTrue(nested.exists(), "gitlink entry must auto-create an empty placeholder")
            self.assertEqual(list(nested.iterdir()), [])

            rows = pool.warm([1])
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["slot"], "Nova-1")
            source = outer / self.module["path"]
            self.assertEqual(git(source, "rev-parse", "HEAD"), baseline)
            self.assertEqual(
                git(source, "rev-parse", "--path-format=absolute", "--git-common-dir"),
                str(pool.store.resolve()),
            )
            self.assertIsNone(common.current_ref(source))
            self.assertEqual(git(outer, "rev-parse", "HEAD"), root_head)

    def test_pool_rejects_incompatible_partial_nested_placeholder_as_foreign(self):
        # The other half of the same crash-recovery window: content in the
        # nested gitlink placeholder that this pool did not create must
        # never be silently adopted or deleted -- fail safely instead.
        with self.fixture_remote(), self.state.lock():
            pool = common.SourcePool(self.state, self.module, reserve_bytes=0)
            pool.refresh()
            root_head = git(self.root, "rev-parse", "HEAD")
            outer = self.root / pool.slot_relative_path(1)
            outer.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            git(self.root, "worktree", "add", "--detach", str(outer), root_head)
            nested = outer / self.module["path"]
            (nested / "stray.txt").write_text("not ours", encoding="utf-8")

            with self.assertRaises(common.ReviewError):
                pool.warm([1])
            self.assertTrue((nested / "stray.txt").exists())
            self.assertEqual((nested / "stray.txt").read_text(encoding="utf-8"), "not ours")
            self.assertFalse((nested / ".git").exists())

    def test_pool_rejects_symlinked_store_path(self):
        with self.fixture_remote(), self.state.lock():
            pool = common.SourcePool(self.state, self.module, reserve_bytes=0)
            pool.store.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            elsewhere = Path(self.temp.name) / "elsewhere-store"
            elsewhere.mkdir()
            pool.store.symlink_to(elsewhere)

            with self.assertRaises(common.ReviewError):
                pool.warm([0])
            self.assertTrue(pool.store.is_symlink())

    def test_pool_rejects_symlinked_outer_slot_directory(self):
        with self.fixture_remote(), self.state.lock():
            pool = common.SourcePool(self.state, self.module, reserve_bytes=0)
            outer = self.root / pool.slot_relative_path(2)
            outer.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            elsewhere = Path(self.temp.name) / "elsewhere-outer"
            elsewhere.mkdir()
            outer.symlink_to(elsewhere)

            with self.assertRaises(common.ReviewError):
                pool.warm([2])
            self.assertTrue(outer.is_symlink())

    def test_pool_rejects_adopted_outer_branch(self):
        with self.fixture_remote(), self.state.lock():
            pool = common.SourcePool(self.state, self.module, reserve_bytes=0)
            row = pool.warm([0])[0]
            outer = self.root / row["slot_path"]
            git(outer, "switch", "-c", "foreign")
            with self.assertRaises(common.ReviewError):
                pool.warm([0])
            self.assertEqual(git(outer, "branch", "--show-current"), "foreign")

    def test_pool_rejects_symlinked_nested_source_placeholder(self):
        with self.fixture_remote(), self.state.lock():
            pool = common.SourcePool(self.state, self.module, reserve_bytes=0)
            pool.refresh()
            root_head = git(self.root, "rev-parse", "HEAD")
            outer = self.root / pool.slot_relative_path(3)
            outer.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            git(self.root, "worktree", "add", "--detach", str(outer), root_head)
            nested = outer / self.module["path"]
            nested.rmdir()
            elsewhere = Path(self.temp.name) / "elsewhere-nested"
            elsewhere.mkdir()
            nested.symlink_to(elsewhere)

            with self.assertRaises(common.ReviewError):
                pool.warm([3])
            self.assertTrue(nested.is_symlink())

    def test_pool_warm_rejects_when_reserved_disk_space_unavailable(self):
        # req #10/rubric section 10: the reserve check must be a real
        # shutil.disk_usage-style measurement against the actual target
        # (state.common), performed before any mutation -- not a hardcoded
        # constant, and not checked against the wrong path.
        import shutil
        from unittest.mock import patch
        with self.fixture_remote(), self.state.lock():
            pool = common.SourcePool(self.state, self.module, reserve_bytes=10**15)
            real_usage = shutil.disk_usage(self.state.common)
            starved = real_usage._replace(free=0)
            with patch.object(common.shutil, "disk_usage", return_value=starved) as mock_disk:
                with self.assertRaises(common.ReviewError):
                    pool.warm([0])
                mock_disk.assert_called_once_with(self.state.common)
            self.assertFalse(pool.store.exists())
            self.assertFalse((self.root / pool.slot_relative_path(0)).exists())

    def test_pool_warm_beyond_full_range_raises_and_preserves_existing_slots(self):
        with self.fixture_remote(), self.state.lock():
            pool = common.SourcePool(self.state, self.module, reserve_bytes=0)
            rows = pool.warm(range(6))
            with self.assertRaises(common.ReviewError):
                pool.warm([6])
            for row in rows:
                source = self.root / row["slot_path"] / self.module["path"]
                self.assertEqual(git(source, "rev-parse", "HEAD"), self.head)
                self.assertEqual(git(source, "status", "--porcelain"), "")

    def _wait_until_gone(self, pid, deadline_seconds=2.0):
        """Poll for a bounded window instead of a single immediate check, so a
        slow-to-be-reaped process doesn't cause a flaky false failure."""
        status = Path(f"/proc/{pid}/stat")

        def _is_gone_or_zombie():
            if not status.exists():
                return True
            return status.read_text().split(") ", 1)[1].split()[0] == "Z"

        deadline = time.monotonic() + deadline_seconds
        while time.monotonic() < deadline:
            if _is_gone_or_zombie():
                return True
            time.sleep(0.02)
        return _is_gone_or_zombie()

    def test_owned_timeout_stops_child_and_grandchild(self):
        marker = Path(self.temp.name) / "writer.pid"
        heartbeat = Path(self.temp.name) / "writer.heartbeat"
        # Grandchild: ignores SIGTERM (must require the group KILL escalation,
        # not the parent's own death/reparenting) and keeps writing so we can
        # later prove the writes actually stopped, not merely that the call
        # returned. Writing to inherited stdout also exercises the
        # highest-risk case: a descendant holding the pipe's write end open.
        grandchild = (
            "import os, signal, sys, time\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            f"open({str(marker)!r}, 'w').write(str(os.getpid()))\n"
            f"fd = os.open({str(heartbeat)!r}, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)\n"
            "while True:\n"
            "    os.write(fd, b'x')\n"
            "    sys.stdout.write('x')\n"
            "    sys.stdout.flush()\n"
            "    time.sleep(0.02)\n"
        )
        parent = (
            "import subprocess, sys, time\n"
            f"subprocess.Popen([sys.executable, '-c', {grandchild!r}])\n"
            "time.sleep(60)\n"
        )
        # An independent process outside run_owned's process group entirely;
        # it must survive completely untouched.
        unrelated_heartbeat = Path(self.temp.name) / "unrelated.heartbeat"
        unrelated_code = (
            "import os, time\n"
            f"fd = os.open({str(unrelated_heartbeat)!r}, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)\n"
            "while True:\n"
            "    os.write(fd, b'y')\n"
            "    time.sleep(0.02)\n"
        )
        unrelated = subprocess.Popen([sys.executable, "-c", unrelated_code])
        try:
            started = time.monotonic()
            with self.assertRaises(common.ReviewError):
                common.run_owned([sys.executable, "-c", parent], timeout=0.5,
                                 term_grace=0.3, kill_grace=0.4)
            elapsed = time.monotonic() - started
            # Bounded completion: this must never approach the descendants'
            # own 60s sleeps, even with a TERM-resistant descendant.
            self.assertLess(elapsed, 5.0)

            self.assertTrue(marker.exists())
            pid = int(marker.read_text())
            self.assertTrue(self._wait_until_gone(pid))

            # Continuing-write detection: confirm the writes have actually
            # stopped once the timeout was handled, not just that run_owned
            # returned while the descendant kept running unseen.
            before = heartbeat.stat().st_size
            time.sleep(0.3)
            after = heartbeat.stat().st_size
            self.assertEqual(before, after)

            # Unrelated-process survival: run_owned must never signal outside
            # the process group it created for this call.
            self.assertIsNone(unrelated.poll())
            before_unrelated = unrelated_heartbeat.stat().st_size
            time.sleep(0.1)
            after_unrelated = unrelated_heartbeat.stat().st_size
            self.assertGreater(after_unrelated, before_unrelated)
        finally:
            # Reliable cleanup of this test's own fixture process even if an
            # assertion above fails partway through.
            unrelated.terminate()
            try:
                unrelated.wait(timeout=5)
            except subprocess.TimeoutExpired:
                unrelated.kill()
                unrelated.wait(timeout=5)

    def test_owned_timeout_proves_group_empty_when_descendant_detaches_from_pipe(self):
        # Regression for a confirmed false-positive: a descendant that (a)
        # ignores SIGTERM and (b) closes its inherited stdout/stderr fds is
        # invisible to pipe-EOF-based detection -- the direct child's own
        # exit can still produce pipe EOF while this grandchild keeps
        # running untouched. run_owned must independently prove the whole
        # process group is empty (not infer it from pipe EOF plus the direct
        # child being reaped) before ever reporting "terminated"; if it
        # can't prove that, it must raise a blocked ReviewError instead.
        marker = Path(self.temp.name) / "detached.pid"
        heartbeat = Path(self.temp.name) / "detached.heartbeat"
        grandchild = (
            "import os, signal, time\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            # Detach from the inherited pipe immediately, before writing the
            # marker, so this process is never observable via the parent's
            # stdout/stderr pipe at any point.
            "os.close(1)\n"
            "os.close(2)\n"
            f"open({str(marker)!r}, 'w').write(str(os.getpid()))\n"
            f"fd = os.open({str(heartbeat)!r}, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)\n"
            "while True:\n"
            "    os.write(fd, b'x')\n"
            "    time.sleep(0.02)\n"
        )
        parent = (
            "import subprocess, sys, time\n"
            f"subprocess.Popen([sys.executable, '-c', {grandchild!r}])\n"
            "time.sleep(60)\n"
        )
        started = time.monotonic()
        with self.assertRaises(common.ReviewError):
            common.run_owned([sys.executable, "-c", parent], timeout=0.3,
                             term_grace=0.2, kill_grace=0.2)
        elapsed = time.monotonic() - started
        # Bounded completion even though the descendant never becomes
        # observable through the pipe and never responds to TERM.
        self.assertLess(elapsed, 5.0)

        self.assertTrue(marker.exists())
        pid = int(marker.read_text())
        # The core assertion: the pipe-invisible descendant must actually be
        # gone (KILL must have reached it and that must be independently
        # confirmed), not merely presumed gone because pipe EOF was seen.
        self.assertTrue(self._wait_until_gone(pid))
        before = heartbeat.stat().st_size
        time.sleep(0.3)
        after = heartbeat.stat().st_size
        self.assertEqual(before, after)


    def test_pool_repeat_warm_preserves_dirty_staged_untracked_and_ignored_content(self):
        with self.fixture_remote(), self.state.lock():
            pool = common.SourcePool(self.state, self.module, reserve_bytes=0)
            row = pool.warm([3])[0]
            outer = self.root / row["slot_path"]
            source = outer / self.module["path"]

            (outer / "untracked.txt").write_text("outer-untracked", encoding="utf-8")
            (outer / "staged.txt").write_text("outer-staged", encoding="utf-8")
            git(outer, "add", "staged.txt")
            (outer / ".gitignore").write_text("ignored.txt\n", encoding="utf-8")
            (outer / "ignored.txt").write_text("outer-ignored", encoding="utf-8")

            (source / "untracked.txt").write_text("source-untracked", encoding="utf-8")
            (source / "staged.txt").write_text("source-staged", encoding="utf-8")
            git(source, "add", "staged.txt")
            (source / ".gitignore").write_text("ignored.txt\n", encoding="utf-8")
            (source / "ignored.txt").write_text("source-ignored", encoding="utf-8")

            before_outer_status = git(outer, "status", "--porcelain=v1", "--ignored")
            before_source_status = git(source, "status", "--porcelain=v1", "--ignored")
            before_head = git(source, "rev-parse", "HEAD")

            with self.assertRaises(common.ReviewError):
                pool.warm([3])

            self.assertEqual(git(outer, "status", "--porcelain=v1", "--ignored"), before_outer_status)
            self.assertEqual(git(source, "status", "--porcelain=v1", "--ignored"), before_source_status)
            self.assertEqual(git(source, "rev-parse", "HEAD"), before_head)
            for relative, expected in ((outer, "outer"), (source, "source")):
                self.assertEqual((relative / "untracked.txt").read_text(encoding="utf-8"), f"{expected}-untracked")
                self.assertEqual((relative / "staged.txt").read_text(encoding="utf-8"), f"{expected}-staged")
                self.assertEqual((relative / "ignored.txt").read_text(encoding="utf-8"), f"{expected}-ignored")


    def test_pool_retention_refs_are_exact_and_discard_is_run_scoped(self):
        with self.fixture_remote(), self.state.lock():
            pool = common.SourcePool(self.state, self.module, reserve_bytes=0)
            row = pool.warm([0])[0]
            first = pool.retain(
                "run-one",
                {"head": self.head, "base": self.base, "target": self.base,
                 "baseline": row["baseline_sha"]},
            )
            second = pool.retain(
                "run-two",
                {"head": self.head, "base": self.base, "target": self.base,
                 "baseline": row["baseline_sha"]},
            )
            pool.drop_retention("run-one", expected=first)
            self.assertEqual(
                git(pool.store, "for-each-ref", "--format=%(refname)",
                    pool.retention_namespace("run-one")),
                "",
            )
            for kind, sha in second.items():
                self.assertEqual(
                    git(pool.store, "rev-parse", pool.retention_ref("run-two", kind)), sha
                )


    def test_pool_reset_uses_verified_cache_after_bounded_refresh_failure(self):
        from unittest.mock import patch
        with self.fixture_remote(), self.state.lock():
            pool = common.SourcePool(self.state, self.module, reserve_bytes=0)
            row = pool.warm([0])[0]
            source = self.root / row["slot_path"] / self.module["path"]
            git(source, "checkout", "--detach", self.base)
            retained = {"head": self.head, "base": self.base, "target": self.base,
                        "baseline": row["baseline_sha"]}
            with patch.object(pool, "refresh", side_effect=common.ReviewError("timed out")):
                result = pool.reset_source(0, "run-one", retained, refresh=True)
            self.assertEqual(result["baseline_sha"], row["baseline_sha"])
            self.assertTrue(result["warning"])
            self.assertEqual(git(source, "rev-parse", "HEAD"), row["baseline_sha"])
            self.assertIsNone(common.current_ref(source))


    def test_pool_reset_without_valid_cache_blocks_before_moving_head(self):
        from unittest.mock import patch
        with self.fixture_remote(), self.state.lock():
            pool = common.SourcePool(self.state, self.module, reserve_bytes=0)
            row = pool.warm([0])[0]
            source = self.root / row["slot_path"] / self.module["path"]
            original = git(source, "rev-parse", "HEAD")
            pool.state_path.unlink()
            pool._baseline_cache = None
            with patch.object(pool, "refresh", side_effect=common.ReviewError("timed out")):
                with self.assertRaises(common.ReviewError):
                    pool.reset_source(0, "run-one", {
                        "head": self.head, "base": self.base, "target": self.base,
                        "baseline": self.head,
                    }, refresh=True)
            self.assertEqual(git(source, "rev-parse", "HEAD"), original)


    def test_pool_reset_refuses_ignored_source_data(self):
        with self.fixture_remote(), self.state.lock():
            pool = common.SourcePool(self.state, self.module, reserve_bytes=0)
            row = pool.warm([0])[0]
            source = self.root / row["slot_path"] / self.module["path"]
            ignored = source / "private.cache"
            ignored.write_text("preserve", encoding="utf-8")
            gitdir = Path(git(source, "rev-parse", "--absolute-git-dir"))
            (gitdir / "info").mkdir(exist_ok=True)
            (gitdir / "info/exclude").write_text("private.cache\n", encoding="utf-8")
            with self.assertRaises(common.ReviewError):
                pool.reset_source(0, "run-one", {
                    "head": self.head, "base": self.base, "target": self.base,
                    "baseline": row["baseline_sha"],
                }, refresh=False)
            self.assertEqual(ignored.read_text(encoding="utf-8"), "preserve")


    def test_pool_maintenance_obeys_cadence_headroom_and_safe_gc_argv(self):
        from unittest.mock import patch
        patcher, calls = self._recorded_argv()
        with self.fixture_remote(), self.state.lock(), patcher:
            pool = common.SourcePool(self.state, self.module, reserve_bytes=0)
            row = pool.warm([0])[0]
            pool.retain("run-one", {"head": self.head, "base": self.base,
                        "target": self.base, "baseline": row["baseline_sha"]})
            ample = common.shutil.disk_usage(pool.store)._replace(free=10**12)
            with patch.object(common.shutil, "disk_usage", return_value=ample):
                first = pool.maintain(now="2026-09-07T12:00:00+00:00", reserve_bytes=1)
                second = pool.maintain(now="2026-09-07T12:00:01+00:00", reserve_bytes=1)
            self.assertEqual(first["status"], "completed")
            self.assertEqual(second["status"], "skipped-cadence")
        maintenance_calls = [c for c in calls if "repack" in c or "prune" in c]
        self.assertEqual(len(maintenance_calls), 2, maintenance_calls)
        flattened = [item for call in maintenance_calls for item in call]
        self.assertIn("repack", flattened)
        self.assertIn("--expire=2.weeks.ago", flattened)
        self.assertNotIn("gc", flattened)
        self.assertNotIn("--aggressive", flattened)
        self.assertNotIn("--prune=now", flattened)
        self.assertNotIn("--force", flattened)


    def test_pool_maintenance_low_headroom_skips_without_timestamp(self):
        from unittest.mock import patch
        with self.fixture_remote(), self.state.lock():
            pool = common.SourcePool(self.state, self.module, reserve_bytes=0)
            pool.warm([0])
            low = common.shutil.disk_usage(pool.store)._replace(free=0)
            with patch.object(common.shutil, "disk_usage", return_value=low):
                result = pool.maintain(now="2026-09-07T12:00:00+00:00", reserve_bytes=1)
            self.assertEqual(result["status"], "skipped-headroom")
            state = json.loads(pool.state_path.read_text(encoding="utf-8"))
            self.assertNotIn("maintenance_at", state)


    def test_pool_maintenance_failure_does_not_publish_timestamp(self):
        from unittest.mock import patch
        with self.fixture_remote(), self.state.lock():
            pool = common.SourcePool(self.state, self.module, reserve_bytes=0)
            pool.warm([0])
            original = common.run_git

            def fail_gc(root, args, **kwargs):
                if root == pool.store and "repack" in args:
                    raise common.ReviewError("fixture maintenance failed")
                return original(root, args, **kwargs)

            with patch.object(common, "run_git", side_effect=fail_gc):
                with self.assertRaises(common.ReviewError):
                    pool.maintain(now="2026-09-07T12:00:00+00:00")
            state = json.loads(pool.state_path.read_text(encoding="utf-8"))
            self.assertNotIn("maintenance_at", state)


    def _pool_manifest(self, pool, row, *, status):
        return {
            "schema_version": 2,
            "identity": self.state.identity,
            "repo_key": pool.repo_key,
            "repo_name": pool.repo_name,
            "slot_number": 1,
            "slot": pool.slot_name(1),
            "slot_id": pool.slot_name(1),
            "slot_path": row["slot_path"],
            "key": "nova-42",
            "branch": "review/nova-42",
            "branch_start": row["root_head"],
            "branch_existed": False,
            "run_id": "pool-run-one",
            "snapshot": self.snapshot,
            "submodule": self.module,
            "pr_dir": "PRs/nova-42",
            "baseline_sha": row["baseline_sha"],
            "baseline_ref": f"refs/review-setup/baselines/{pool.repo_key}",
            "root_head": row["root_head"],
            "source_prepared": True,
            "source_baseline": {"head": row["baseline_sha"], "ref": None},
            "retention_refs": {},
            "required_shas": {"head": self.head, "base": self.base, "target": self.base},
            "artifacts": {},
            "baseline_artifacts": {},
            "status": status,
            "created_at": "2026-09-07T00:00:00+00:00",
        }


    def _activate_pool_run(self, manifest):
        outer = self.root / manifest["slot_path"]
        git(outer, "switch", "-c", manifest["branch"])
        artifact = "PRs/nova-42/PR_Setup_01.json"
        common.write_new_json(outer / artifact, {"run_id": manifest["run_id"]})
        manifest["artifacts"] = {artifact: common.file_hash(outer / artifact)}
        return artifact


    def test_pool_reset_orchestration_archives_refs_and_keeps_nested_source(self):
        import review_reset
        with self.fixture_remote(), self.state.lock():
            pool = common.SourcePool(self.state, self.module, reserve_bytes=0)
            row = pool.warm([1])[0]
            manifest = self._pool_manifest(pool, row, status="ready")
            artifact = self._activate_pool_run(manifest)
            self.state.save_pool_run(manifest)
        with self.fixture_remote():
            result = review_reset.reset_pool_run(
                self.state, "Nova", 1, mode="archive", run_maintenance=False
            )
        self.assertEqual(result["status"], "archived")
        outer = self.root / row["slot_path"]
        nested = outer / self.module["path"]
        self.assertTrue((nested / ".git").exists())
        self.assertEqual(git(nested, "rev-parse", "HEAD"), row["baseline_sha"])
        self.assertEqual(git(outer, "branch", "--show-current"), "")
        self.assertEqual(git(outer, "rev-parse", "HEAD"), row["root_head"])
        self.assertEqual(
            git(self.root, "show", f"{result['archive_branch']}:{artifact}"),
            json.dumps({"run_id": manifest["run_id"]}, indent=2, sort_keys=True),
        )
        for kind, sha in result["retained_commits"].items():
            self.assertEqual(
                git(pool.store, "rev-parse", pool.retention_ref(result["run_id"], kind)), sha
            )


    def test_pool_reset_orchestration_discard_drops_only_its_refs(self):
        import review_reset
        with self.fixture_remote(), self.state.lock():
            pool = common.SourcePool(self.state, self.module, reserve_bytes=0)
            row = pool.warm([1])[0]
            other = pool.retain(
                "other-run",
                {"head": self.head, "base": self.base, "target": self.base,
                 "baseline": row["baseline_sha"]},
            )
            manifest = self._pool_manifest(pool, row, status="ready")
            self._activate_pool_run(manifest)
            self.state.save_pool_run(manifest)
        with self.fixture_remote():
            result = review_reset.reset_pool_run(
                self.state, "Nova", 1, mode="discard", confirm="nova-42",
                run_maintenance=False,
            )
        self.assertEqual(result["status"], "discarded")
        self.assertEqual(
            git(pool.store, "for-each-ref", "--format=%(refname)",
                pool.retention_namespace(result["run_id"])),
            "",
        )
        for kind, sha in other.items():
            self.assertEqual(
                git(pool.store, "rev-parse", pool.retention_ref("other-run", kind)), sha
            )


    def test_pool_reset_orchestration_blocks_recoverably_without_cache(self):
        import review_reset
        from unittest.mock import patch
        with self.fixture_remote(), self.state.lock():
            pool = common.SourcePool(self.state, self.module, reserve_bytes=0)
            row = pool.warm([1])[0]
            manifest = self._pool_manifest(pool, row, status="ready")
            self._activate_pool_run(manifest)
            self.state.save_pool_run(manifest)
            pool.state_path.unlink()
            with patch.object(common.SourcePool, "retain", return_value={
                "head": self.head, "base": self.base, "target": self.base,
                "baseline": row["baseline_sha"],
            }), patch.object(common.SourcePool, "refresh",
                              side_effect=common.ReviewError("timed out")):
                with self.assertRaises(common.ReviewError):
                    review_reset.reset_pool_run_locked(
                        self.state, "Nova", 1, mode="archive", run_maintenance=False
                    )
            blocked = self.state.load_pool_run(pool.repo_key, 1)
        self.assertEqual(blocked["status"], "blocked")
        self.assertEqual(blocked["reset_phase"], "source-reset")
        self.assertEqual(blocked["run_id"], manifest["run_id"])


    def test_pool_archive_retries_after_checkpoint_commit_failure(self):
        import review_reset
        from unittest.mock import patch

        with self.fixture_remote(), self.state.lock():
            pool = common.SourcePool(self.state, self.module, reserve_bytes=0)
            row = pool.warm([1])[0]
            manifest = self._pool_manifest(pool, row, status="ready")
            artifact = self._activate_pool_run(manifest)
            self.state.save_pool_run(manifest)
        original = review_reset.run_git
        failed = False

        def fail_once(repo, args, check=True):
            nonlocal failed
            if args and args[0] == "commit" and not failed:
                failed = True
                raise common.ReviewError("fixture commit failure")
            return original(repo, args, check=check)

        with self.fixture_remote(), patch.object(
            review_reset, "run_git", side_effect=fail_once
        ):
            with self.assertRaises(common.ReviewError):
                review_reset.reset_pool_run(
                    self.state, "Nova", 1, mode="archive", run_maintenance=False
                )
        interrupted = self.state.load_pool_run(pool.repo_key, 1)
        self.assertEqual(interrupted["status"], "checkpointing")
        self.assertNotIn("checkpoint_sha", interrupted)

        with self.fixture_remote():
            result = review_reset.reset_pool_run(
                self.state, "Nova", 1, mode="archive", run_maintenance=False
            )
        self.assertEqual(result["status"], "archived")
        self.assertEqual(
            git(self.root, "show", f"{result['archive_branch']}:{artifact}"),
            json.dumps({"run_id": manifest["run_id"]}, indent=2, sort_keys=True),
        )


    def test_pool_archive_retries_after_branch_rename(self):
        import review_reset
        from unittest.mock import patch

        with self.fixture_remote(), self.state.lock():
            pool = common.SourcePool(self.state, self.module, reserve_bytes=0)
            row = pool.warm([1])[0]
            manifest = self._pool_manifest(pool, row, status="ready")
            self._activate_pool_run(manifest)
            self.state.save_pool_run(manifest)
        original = review_reset.run_git
        failed = False

        def interrupt_after_rename(repo, args, check=True):
            nonlocal failed
            result = original(repo, args, check=check)
            if args[:2] == ["branch", "-m"] and not failed:
                failed = True
                raise common.ReviewError("fixture interruption after rename")
            return result

        with self.fixture_remote(), patch.object(
            review_reset, "run_git", side_effect=interrupt_after_rename
        ):
            with self.assertRaises(common.ReviewError):
                review_reset.reset_pool_run(
                    self.state, "Nova", 1, mode="archive", run_maintenance=False
                )
        interrupted = self.state.load_pool_run(pool.repo_key, 1)
        self.assertEqual(interrupted["reset_phase"], "branch-archive-intent")
        outer = self.root / row["slot_path"]
        self.assertEqual(common.current_ref(outer), interrupted["archive_branch"])

        with self.fixture_remote():
            result = review_reset.reset_pool_run(
                self.state, "Nova", 1, mode="archive", run_maintenance=False
            )
        self.assertEqual(result["status"], "archived")


    def test_pool_discard_retries_after_outer_detach(self):
        import review_reset
        from unittest.mock import patch

        with self.fixture_remote(), self.state.lock():
            pool = common.SourcePool(self.state, self.module, reserve_bytes=0)
            row = pool.warm([1])[0]
            manifest = self._pool_manifest(pool, row, status="ready")
            self._activate_pool_run(manifest)
            self.state.save_pool_run(manifest)
        original = review_reset.run_git
        failed = False

        def interrupt_after_detach(repo, args, check=True):
            nonlocal failed
            result = original(repo, args, check=check)
            if args[:2] == ["checkout", "--detach"] and Path(repo) == self.root / row["slot_path"] and not failed:
                failed = True
                raise common.ReviewError("fixture interruption after detach")
            return result

        with self.fixture_remote(), patch.object(
            review_reset, "run_git", side_effect=interrupt_after_detach
        ):
            with self.assertRaises(common.ReviewError):
                review_reset.reset_pool_run(
                    self.state,
                    "Nova",
                    1,
                    mode="discard",
                    confirm="nova-42",
                    run_maintenance=False,
                )
        interrupted = self.state.load_pool_run(pool.repo_key, 1)
        self.assertEqual(interrupted["reset_phase"], "outer-detach-intent")
        self.assertIsNone(common.current_ref(self.root / row["slot_path"]))

        with self.fixture_remote():
            result = review_reset.reset_pool_run(
                self.state,
                "Nova",
                1,
                mode="discard",
                confirm="nova-42",
                run_maintenance=False,
            )
        self.assertEqual(result["status"], "discarded")


    def test_pool_maintenance_rejects_naive_persisted_timestamp(self):
        with self.fixture_remote(), self.state.lock():
            pool = common.SourcePool(self.state, self.module, reserve_bytes=0)
            pool.warm([0])
            pool._write_state({"maintenance_at": "2026-09-07T12:00:00"})
            with self.assertRaisesRegex(common.ReviewError, "timezone"):
                pool.maintain(now="2026-09-08T12:00:00+00:00", reserve_bytes=1)


    def test_pool_reset_maintenance_failure_is_separate_from_success(self):
        import review_reset
        from unittest.mock import patch
        with self.fixture_remote(), self.state.lock():
            pool = common.SourcePool(self.state, self.module, reserve_bytes=0)
            row = pool.warm([1])[0]
            manifest = self._pool_manifest(pool, row, status="ready")
            self._activate_pool_run(manifest)
            self.state.save_pool_run(manifest)
        with self.fixture_remote(), patch.object(
            common.SourcePool, "maintain", side_effect=common.ReviewError("fixture gc failed")
        ):
            result = review_reset.reset_pool_run(
                self.state, "Nova", 1, mode="archive", run_maintenance=True
            )
        self.assertEqual(result["status"], "archived")
        self.assertEqual(result["reset_phase"], "released")
        self.assertEqual(result["maintenance"]["status"], "failed")


    def test_reset_cli_requires_unambiguous_pool_or_legacy_selector(self):
        import review_reset
        from unittest.mock import patch
        state = common.ReviewState(self.root)
        with patch.object(review_reset, "preflight", return_value={}), \
             patch.object(review_reset, "discover_root", return_value=self.root), \
             patch.object(review_reset, "reset_pool_run", return_value={
                 "status": "archived", "slot_id": "Nova-1"
             }) as pooled, \
             patch.object(review_reset, "reset_run", return_value={
                 "status": "archived", "slot": 1
             }) as legacy, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(review_reset.main(["--slot", "Nova-1"]), 0)
            pooled.assert_called_once_with(
                unittest.mock.ANY, "Nova", 1, mode="archive", confirm=None, dry_run=False
            )
            self.assertEqual(review_reset.main(["--legacy-slot", "1"]), 0)
            legacy.assert_called_once_with(
                unittest.mock.ANY, 1, mode="archive", confirm=None, dry_run=False
            )
        self.assertEqual(state.identity, common.ReviewState(self.root).identity)



if __name__ == "__main__":
    unittest.main()
