"""Local-only lifecycle tests; never operate on the real review checkout."""
import contextlib
import hashlib
import importlib.util
import io
import json
import multiprocessing
import os
import queue
import shutil
import signal
import stat
from contextlib import contextmanager
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
SPEC = importlib.util.find_spec("review_common")
if SPEC:
    import review_common as common


def git(root, *args):
    result = subprocess.run(["git", "-C", str(root), *args], capture_output=True,
                            text=True, check=True)
    return result.stdout.strip()


class _OfflineClient:
    org_url = "https://dev.azure.com/org"


def _empty_report():
    return {
        "complete": True,
        "warnings": [],
        "files": [],
        "work_items": {"roots": [], "nodes": [], "complete": True},
        "provenance": {"commits": {}, "pull_requests": {}, "complete": True},
    }


def _result(queue_, label, operation):
    try:
        queue_.put((label, "ok", operation()))
    except BaseException as exc:
        queue_.put((label, "error", f"{type(exc).__name__}: {exc}"))


def _setup_at_context_barrier(root, snapshot, slot, barrier, entered, results):
    def operation():
        import review_context
        import review_setup

        def collect(*args, **kwargs):
            entered.set()
            barrier.wait(timeout=10)
            return _empty_report()

        with patch.object(review_context, "resolve_pr", return_value=snapshot), \
             patch.object(review_setup, "_collect", side_effect=collect), \
             patch.object(review_context, "render_markdown", return_value="# fixture\n"):
            return review_setup.setup_run(Path(root), snapshot["pr_number"], slot=slot,
                                          client=_OfflineClient(), history_timeout=20)

    _result(results, f"slot-{slot}", operation)


def _setup_blocked_in_context(root, snapshot, slot, entered, release, results):
    def operation():
        import review_context
        import review_setup

        def collect(*args, **kwargs):
            entered.set()
            if not release.wait(timeout=10):
                raise TimeoutError("fixture context gate was not released")
            return _empty_report()

        with patch.object(review_context, "resolve_pr", return_value=snapshot), \
             patch.object(review_setup, "_collect", side_effect=collect), \
             patch.object(review_context, "render_markdown", return_value="# fixture\n"):
            return review_setup.setup_run(Path(root), snapshot["pr_number"], slot=slot,
                                          client=_OfflineClient(), history_timeout=20)

    _result(results, "owner", operation)


def _setup_observing_cleanup(root, snapshot, slot, started, cleanup_entered, results):
    def operation():
        import review_context
        import review_setup

        original_cleanup = review_setup.cleanup_worktree

        def observed_cleanup(*args, **kwargs):
            cleanup_entered.set()
            return original_cleanup(*args, **kwargs)

        started.set()
        with patch.object(review_context, "resolve_pr", return_value=snapshot), \
             patch.object(review_setup, "cleanup_worktree", side_effect=observed_cleanup), \
             patch.object(review_setup, "_collect", return_value=_empty_report()), \
             patch.object(review_context, "render_markdown", return_value="# fixture\n"):
            return review_setup.setup_run(Path(root), snapshot["pr_number"], slot=slot,
                                          client=_OfflineClient(), history_timeout=20)

    _result(results, "contender", operation)


def _setup_with_reservation_gate(root, snapshot, slot, gate, entered, results, label):
    def operation():
        import review_context
        import review_setup

        original_all = common.ReviewState.all_pool_runs
        calls = 0

        def synchronized_all(state):
            nonlocal calls
            calls += 1
            rows = original_all(state)
            if calls == 3:
                entered.set()
                try:
                    gate.wait(timeout=2)
                except threading.BrokenBarrierError:
                    pass
            return rows

        with patch.object(review_context, "resolve_pr", return_value=snapshot), \
             patch.object(common.ReviewState, "all_pool_runs", synchronized_all), \
             patch.object(review_setup, "_collect", return_value=_empty_report()), \
             patch.object(review_context, "render_markdown", return_value="# fixture\n"):
            return review_setup.setup_run(
                Path(root), snapshot["pr_number"], slot=slot,
                client=_OfflineClient(), history_timeout=20,
            )

    _result(results, label, operation)


def _refresh_with_post_fetch_gate(root, module, expected, fetched, release, results):
    def operation():
        original_fetch = common.bounded_fetch

        def gated_fetch(*args, **kwargs):
            result = original_fetch(*args, **kwargs)
            fetched.set()
            if not release.wait(timeout=10):
                raise TimeoutError("refresh result gate was not released")
            return result

        state = common.ReviewState(Path(root))
        pool = common.SourcePool(state, module, reserve_bytes=0)
        with patch.object(common, "bounded_fetch", side_effect=gated_fetch), \
             common.lock_session(20), state.lock(shared=True), common.fetch_session(20):
            actual = pool.refresh()
        return {"actual": actual, "expected": expected, "cached": pool.cached_baseline_sha()}

    _result(results, "refresh", operation)


def _cold_warm_slot(root, module, slot, gate, results):
    def operation():
        state = common.ReviewState(Path(root))
        pool = common.SourcePool(state, module, reserve_bytes=0)
        gate.wait(timeout=10)
        with common.lock_session(20), state.lock(shared=True):
            return pool.warm([slot])[0]

    _result(results, f"cold-{slot}", operation)


def _five_slot_setup_worker(root, snapshot, slot, gate, entered, exited, results):
    def operation():
        import review_context
        import review_setup

        gate.wait(timeout=20)
        entered[slot] = time.monotonic_ns()
        with (
            patch.object(review_context, "resolve_pr", return_value=snapshot),
            patch.object(review_setup, "_collect", return_value=_empty_report()),
            patch.object(review_context, "render_markdown", return_value="# fixture\n"),
        ):
            manifest = review_setup.setup_run(
                Path(root),
                snapshot["pr_number"],
                slot=slot,
                client=_OfflineClient(),
                history_timeout=30,
                lock_timeout=30,
            )
        exited[slot] = time.monotonic_ns()
        source = Path(root) / manifest["slot_path"] / manifest["submodule"]["path"]
        return {
            "status": manifest["status"],
            "head": git(source, "rev-parse", "HEAD"),
            "git_dir": git(source, "rev-parse", "--path-format=absolute", "--git-dir"),
            "common_dir": git(source, "rev-parse", "--path-format=absolute", "--git-common-dir"),
            "object_dir": git(source, "rev-parse", "--path-format=absolute", "--git-path", "objects"),
            "index": git(source, "rev-parse", "--path-format=absolute", "--git-path", "index"),
            "head_file": git(source, "rev-parse", "--path-format=absolute", "--git-path", "HEAD"),
        }

    _result(results, f"slot-{slot}", operation)


def _five_slot_archive_worker(root, slot, gate, entered, exited, results):
    def operation():
        import review_reset

        original_outer_lock = common.ReviewState.outer_lock

        @contextmanager
        def observed_outer_lock(state, *args, **kwargs):
            gate.wait(timeout=20)
            with original_outer_lock(state, *args, **kwargs) as lock:
                entered[slot] = time.monotonic_ns()
                time.sleep(0.1)
                try:
                    yield lock
                finally:
                    exited[slot] = time.monotonic_ns()

        state = common.ReviewState(Path(root))
        with patch.object(common.ReviewState, "outer_lock", new=observed_outer_lock):
            return review_reset.reset_pool_run(
                state,
                "Nova",
                slot,
                mode="archive",
                run_maintenance=False,
                lock_timeout=30,
            )

    _result(results, f"slot-{slot}", operation)


def _killable_cold_store_runner(
    root,
    module,
    slot,
    ready_write,
    release_read,
    result_queue,
    identity_attempt_write=None,
    identity_release_read=None,
    identity_published_write=None,
):
    """Warm one cold slot and gate the real source-store init child."""
    real_popen = common.subprocess.Popen
    real_atomic_json = common.atomic_json
    gated = False
    identity_intercepted = False
    identity_published = False

    def popen(args, *popen_args, **popen_kwargs):
        nonlocal gated
        command = list(args)
        target = command[4:] if command[:2] == [sys.executable, "-c"] and len(command) > 4 else command
        if not gated and "init" in target and "--bare" in target:
            gated = True
            command = [
                sys.executable,
                "-c",
                "import os,sys; os.write(int(sys.argv[1]),str(os.getpid()).encode()+b'\\n'); "
                "go=os.read(int(sys.argv[2]),1); "
                "os.execvpe(sys.argv[3],sys.argv[3:],os.environ) if go==b'1' else sys.exit(124)",
                str(ready_write),
                str(release_read),
                *target,
            ]
            inherited = tuple(popen_kwargs.get("pass_fds", ()))
            popen_kwargs["pass_fds"] = tuple(
                dict.fromkeys((*inherited, ready_write, release_read))
            )
        return real_popen(command, *popen_args, **popen_kwargs)

    def atomic_json(path, value):
        nonlocal identity_intercepted, identity_published
        operation = value.get("operation") if isinstance(value, dict) else None
        if (
            (identity_attempt_write is not None or identity_published_write is not None)
            and not identity_intercepted
            and Path(path).parent.name == "shared-writers"
            and isinstance(operation, dict)
            and operation.get("process") is not None
            and operation.get("spawn_pending") is False
        ):
            identity_intercepted = True
            if identity_attempt_write is not None:
                os.write(identity_attempt_write, b"1")
                if os.read(identity_release_read, 1) != b"1":
                    raise RuntimeError("fixture identity publication was not released")
        real_atomic_json(path, value)
        if (
            identity_published_write is not None
            and identity_intercepted
            and not identity_published
        ):
            identity_published = True
            os.write(identity_published_write, b"1")

    try:
        state = common.ReviewState(Path(root))
        pool = common.SourcePool(state, module, reserve_bytes=0)
        patches = [patch.object(common.subprocess, "Popen", side_effect=popen)]
        if identity_attempt_write is not None or identity_published_write is not None:
            patches.append(patch.object(common, "atomic_json", side_effect=atomic_json))
        with contextlib.ExitStack() as stack:
            for fixture_patch in patches:
                stack.enter_context(fixture_patch)
            stack.enter_context(common.lock_session(20))
            stack.enter_context(state.lock(shared=True))
            result = pool.warm([slot])[0]
        result_queue.put(("ok", result))
    except BaseException as exc:
        result_queue.put(("error", f"{type(exc).__name__}: {exc}"))


def _import_exact_after_refresh(root, module, sha, fetched, release, results):
    def operation():
        if not fetched.wait(timeout=10):
            raise TimeoutError("refresh never completed its fetch")
        state = common.ReviewState(Path(root))
        pool = common.SourcePool(state, module, reserve_bytes=0)
        with common.lock_session(20), state.lock(shared=True), common.fetch_session(20):
            result = common.bounded_fetch(pool.store, module["url"], [sha], depth=1000)
        if result.returncode:
            raise RuntimeError(f"exact import failed: {result.returncode}")
        release.set()
        return sha

    _result(results, "import", operation)


def _killable_fixture_runner(
    root,
    module,
    mode,
    slot,
    ready_write,
    release_read,
    result_queue,
):
    """Run an actual setup/reset entrypoint and gate one owning Git child."""
    import review_reset
    import review_setup

    real_popen = common.subprocess.Popen
    gated = False

    def popen(args, *popen_args, **popen_kwargs):
        nonlocal gated
        command = list(args)
        matches = (
            mode == "setup"
            and not gated
            and "restore" in command
            and "--worktree" in command
        ) or (mode == "reset" and not gated and "commit" in command)
        if matches:
            gated = True
            original = command
            target = original
            if original[:2] == [sys.executable, "-c"] and len(original) > 4:
                target = original[4:]
            command = [
                sys.executable,
                "-c",
                "import os,sys; os.write(int(sys.argv[1]),str(os.getpid()).encode()+b'\\n'); "
                "go=os.read(int(sys.argv[2]),1); "
                "os.execvpe(sys.argv[3],sys.argv[3:],os.environ) if go==b'1' else sys.exit(124)",
                str(ready_write),
                str(release_read),
                *target,
            ]
            inherited = tuple(popen_kwargs.get("pass_fds", ()))
            popen_kwargs["pass_fds"] = tuple(
                dict.fromkeys((*inherited, ready_write, release_read))
            )
        return real_popen(command, *popen_args, **popen_kwargs)

    try:
        state = common.ReviewState(Path(root))
        with patch.object(common.subprocess, "Popen", side_effect=popen):
            if mode == "setup":
                snapshot = {
                    "pr_number": 42,
                    "repository": {
                        "id": "repo-id",
                        "name": "Nova",
                        "project": {"name": "project"},
                    },
                    "head_sha": git(Path(root).parent / "source", "rev-parse", "HEAD"),
                    "target_sha": git(
                        Path(root).parent / "source", "rev-parse", "HEAD^"
                    ),
                    "base_sha": git(
                        Path(root).parent / "source", "rev-parse", "HEAD^"
                    ),
                    "source_ref": "refs/heads/main",
                    "target_ref": "refs/heads/main",
                    "iteration_id": 1,
                    "snapshot_fingerprint": "snapshot-1",
                    "title": "Test review",
                }
                client = type(
                    "Client", (), {"org_url": "https://dev.azure.com/org"}
                )()
                with (
                    patch("review_context.resolve_pr", return_value=snapshot),
                    patch("review_setup._collect", return_value=_empty_report()),
                    patch(
                        "review_context.render_markdown", return_value="# fixture\n"
                    ),
                ):
                    result = review_setup.setup_run(
                        Path(root),
                        42,
                        slot=slot,
                        client=client,
                        history_timeout=20,
                    )
            else:
                result = review_reset.reset_pool_run(
                    state,
                    "Nova",
                    slot,
                    mode="archive",
                    run_maintenance=False,
                    lock_timeout=20,
                )
        result_queue.put(("ok", result))
    except BaseException as exc:
        result_queue.put(("error", f"{type(exc).__name__}: {exc}"))


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
        rel = f'{manifest["pr_dir"]}/PR_Setup_01.json'
        common.write_new_json(slot / rel, {"run_id": manifest["run_id"]})
        manifest["artifacts"] = {rel: common.file_hash(slot / rel)}
        manifest["status"] = "ready"
        self.state.save(manifest)
        return rel

    def test_reserves_isolated_worktree_without_switching_root(self):
        manifest = self.reserve()
        self.assertEqual(manifest["slot"], 0)
        self.assertEqual(manifest["key"], "nova-42")
        self.assertEqual(manifest["branch"], "review/nova-42")
        self.assertEqual(manifest["pr_dir"], "PRs/Nova-42")
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

    def test_existing_lowercase_manifest_archives_recorded_path_then_reuses_slot(self):
        import review_reset
        manifest = self.reserve(1)
        manifest["pr_dir"] = "PRs/nova-42"
        self.state.save(manifest)
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

    def _foreign_outer_transaction(self):
        return {
            "id": "a" * 32,
            "run_id": "foreign-run",
            "repo_key": "nova",
            "repo_name": "Nova",
            "slot_number": 0,
            "slot_id": "Nova-0",
            "branch": "review/foreign",
            "review_tip": self.store_base,
            "root_start": self.store_base,
            "main_branch": "main",
            "baseline_branch": "main",
            "baseline_sha": self.base,
            "module_path": "repos/Nova",
            "pr_dir": "PRs/foreign",
            "allowed_paths": ["PRs/foreign/review.md", "repos/Nova"],
            "expected_merge_head": self.store_base,
            "phase": "reserved",
            "created_at": common.utc_now(),
        }

    def _assert_legacy_reset_blocked_without_mutation(self, blocker):
        import review_reset

        manifest = self.reserve()
        rel = self.add_report(manifest)
        slot = self.state.slot_path(manifest)
        artifact = slot / rel
        manifest_bytes = self.state.manifest_path(0).read_bytes()
        artifact_bytes = artifact.read_bytes()
        ref_before = git(self.root, "rev-parse", f"refs/heads/{manifest['branch']}")
        head_before = git(slot, "rev-parse", "HEAD")
        index_before = git(slot, "write-tree")
        status_before = git(slot, "status", "--porcelain=v1", "--untracked-files=all")
        blocker()

        dry = review_reset.reset_run(
            self.state, 0, mode="discard", confirm="nova-42", dry_run=True
        )
        self.assertTrue(dry["dry_run"])
        with self.assertRaises(common.ReviewError):
            review_reset.reset_run(
                self.state, 0, mode="discard", confirm="nova-42"
            )

        self.assertEqual(self.state.manifest_path(0).read_bytes(), manifest_bytes)
        self.assertEqual(artifact.read_bytes(), artifact_bytes)
        self.assertEqual(
            git(self.root, "rev-parse", f"refs/heads/{manifest['branch']}"),
            ref_before,
        )
        self.assertEqual(git(slot, "rev-parse", "HEAD"), head_before)
        self.assertEqual(git(slot, "write-tree"), index_before)
        self.assertEqual(
            git(slot, "status", "--porcelain=v1", "--untracked-files=all"),
            status_before,
        )

    def test_legacy_reset_transaction_gate_precedes_all_mutation(self):
        self._assert_legacy_reset_blocked_without_mutation(
            lambda: common.save_outer_transaction(
                self.state, self._foreign_outer_transaction()
            )
        )

    def test_legacy_reset_pending_writer_gate_precedes_all_mutation(self):
        def write_pending():
            common.atomic_json(
                self.state.directory / "outer-writer.json",
                {
                    "schema_version": 1,
                    "workspace_identity": self.state.identity,
                    "operation": {
                        "id": "b" * 32,
                        "transaction_id": "c" * 32,
                        "phase": "foreign-write",
                        "spawn_pending": True,
                        "coordinator": common.process_identity(os.getpid()),
                        "process": None,
                    },
                },
            )

        self._assert_legacy_reset_blocked_without_mutation(write_pending)

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

    def test_ready_legacy_setup_remains_non_destructive_for_all_content_classes(self):
        import review_setup

        manifest = self.reserve()
        source = self.populate_source(manifest)
        outer = self.state.slot_path(manifest)
        for repo, tracked, prefix in (
            (outer, ".gitmodules", "legacy-outer"),
            (source, "file.txt", "legacy-source"),
        ):
            (repo / tracked).write_text("staged\n", encoding="utf-8")
            git(repo, "add", tracked)
            (repo / tracked).write_text("unstaged\n", encoding="utf-8")
            (repo / f"{prefix}-untracked").write_text("preserve", encoding="utf-8")
            exclude = Path(git(repo, "rev-parse", "--git-path", "info/exclude"))
            with exclude.open("a", encoding="utf-8") as stream:
                stream.write(f"\n/{prefix}-ignored\n")
            (repo / f"{prefix}-ignored").write_text("preserve", encoding="utf-8")
        before = {
            repo: git(repo, "status", "--porcelain", "--ignored")
            for repo in (outer, source)
        }
        client = type("Client", (), {"org_url": "https://dev.azure.com/org"})()

        with patch("review_context.resolve_pr", return_value=self.snapshot):
            result = review_setup.setup_run(self.root, 42, client=client)

        self.assertEqual(result["schema_version"], 1)
        self.assertEqual(result["status"], "ready")
        for repo in (outer, source):
            self.assertEqual(git(repo, "status", "--porcelain", "--ignored"), before[repo])

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

    def _advance_baseline_through_reset(self, *, stale_slot=1):
        """Create real A->B baseline skew through the supported reset path."""
        with self.fixture_remote(), self.state.lock(shared=True):
            pool = common.SourcePool(self.state, self.module, reserve_bytes=0)
            rows = pool.warm([0, stale_slot])
            baseline_a = rows[0]["baseline_sha"]
            (self.source / "baseline-b.txt").write_text("baseline b\n", encoding="utf-8")
            git(self.source, "add", "baseline-b.txt")
            git(self.source, "commit", "-m", "baseline b")
            baseline_b = git(self.source, "rev-parse", "HEAD")
            retained = {
                "head": self.head,
                "base": self.base,
                "target": self.base,
                "baseline": baseline_a,
            }
            with self.state.slot_lock(pool.repo_key, 0, phase="fixture-reset"), common.slot_writer_scope(
                self.state, pool.repo_key, 0, phase="fixture-reset"
            ):
                result = pool.reset_source(0, "baseline-advance", retained, refresh=True)
        self.assertEqual(result["baseline_sha"], baseline_b)
        stale_source = self.root / pool.slot_relative_path(stale_slot) / self.module["path"]
        self.assertEqual(git(stale_source, "rev-parse", "HEAD"), baseline_a)
        return pool, baseline_a, baseline_b, stale_source

    def test_reset_advance_marks_idle_prior_generation_stale_and_warm_normalizes(self):
        import review_setup

        pool, baseline_a, baseline_b, stale_source = self._advance_baseline_through_reset()
        status = review_setup._pool_status(self.root, self.module, 1)
        dry = review_setup.warm_run(self.root, "Nova", 1, dry_run=True)
        self.assertEqual(status["status"], "stale")
        self.assertEqual(status["head_sha"], baseline_a)
        self.assertEqual(status["baseline_sha"], baseline_b)
        self.assertEqual(dry["status"], "stale")
        self.assertEqual(dry["action"], "would-normalize")

        reservation = {
            "id": "fixture-reservation",
            "repo_key": pool.repo_key,
            "key": "nova-42",
            "slot_number": 1,
            "snapshot_fingerprint": self.snapshot["snapshot_fingerprint"],
            "created_at": "2026-09-11T00:00:00+00:00",
        }
        self.state.save_setup_reservation(reservation)
        reservation_path = self.state.setup_reservation_path(pool.repo_key, "nova-42")
        reservation_before = reservation_path.read_bytes()
        with self.fixture_remote():
            warmed = review_setup.warm_run(self.root, "Nova", 1)
        self.assertEqual(warmed["status"], "warm")
        self.assertEqual(git(stale_source, "rev-parse", "HEAD"), baseline_b)
        self.assertEqual(reservation_path.read_bytes(), reservation_before)

    def test_status_blocks_clean_outer_with_active_git_operation(self):
        import review_setup

        with self.fixture_remote(), self.state.lock(shared=True):
            pool = common.SourcePool(self.state, self.module, reserve_bytes=0)
            row = pool.warm([1])[0]
        outer = self.root / row["slot_path"]
        merge_head = Path(git(outer, "rev-parse", "--git-path", "MERGE_HEAD"))
        merge_head.write_text(row["root_head"] + "\n", encoding="ascii")
        status = review_setup._pool_status(self.root, self.module, 1)
        dry = review_setup.warm_run(self.root, "Nova", 1, dry_run=True)
        self.assertEqual(status["status"], "blocked")
        self.assertEqual(dry["status"], "blocked")

    def test_warm_refuses_ignored_outer_dirt_without_mutation(self):
        import review_setup

        pool, baseline_a, _, source = self._advance_baseline_through_reset()
        outer = self.root / pool.slot_relative_path(1)
        self._ignore_in(outer, "only-ignored.tmp")
        ignored = outer / "only-ignored.tmp"
        ignored.write_text("preserve\n", encoding="utf-8")
        before = git(outer, "status", "--porcelain=v1", "--ignored")
        with self.fixture_remote():
            refused = review_setup.warm_run(self.root, "Nova", 1)
        self.assertEqual(refused["status"], "occupied")
        self.assertEqual(git(outer, "status", "--porcelain=v1", "--ignored"), before)
        self.assertEqual(git(source, "rev-parse", "HEAD"), baseline_a)
        self.assertEqual(ignored.read_text(encoding="utf-8"), "preserve\n")

    def test_dirty_stale_warm_refuses_unchanged_but_explicit_setup_cleans_and_continues(self):
        import review_setup

        pool, baseline_a, baseline_b, source = self._advance_baseline_through_reset()
        outer = self.root / pool.slot_relative_path(1)
        self._dirty_all_classes(outer, source, "stale")
        before_outer = git(outer, "status", "--porcelain=v1", "--ignored")
        before_source = git(source, "status", "--porcelain=v1", "--ignored")
        before_head = git(source, "rev-parse", "HEAD")
        with self.fixture_remote():
            refused = review_setup.warm_run(self.root, "Nova", 1)
        self.assertEqual(refused["status"], "occupied")
        self.assertEqual(git(outer, "status", "--porcelain=v1", "--ignored"), before_outer)
        self.assertEqual(git(source, "status", "--porcelain=v1", "--ignored"), before_source)
        self.assertEqual(git(source, "rev-parse", "HEAD"), before_head)

        result = self.explicit_setup(slot=1)
        self.assertEqual(result["status"], "ready")
        self._assert_dirt_removed(outer, source, "stale")
        self.assertEqual(result["baseline_sha"], baseline_b)
        self.assertEqual(git(source, "rev-parse", "HEAD"), self.snapshot["head_sha"])

    def test_setup_uses_journal_pinned_target_when_publisher_advances_after_cleanup(self):
        import review_setup

        pool, _, baseline_b, source = self._advance_baseline_through_reset()
        (self.source / "baseline-c.txt").write_text("baseline c\n", encoding="utf-8")
        git(self.source, "add", "baseline-c.txt")
        git(self.source, "commit", "-m", "baseline c")
        with self.fixture_remote(), self.state.lock(shared=True):
            baseline_c = pool.refresh(persist=False)
        advanced = False
        original_atomic = review_setup.atomic_json

        def advance_after_normalization_intent(path, value):
            nonlocal advanced
            original_atomic(path, value)
            if value.get("phase") == "normalizing" and not advanced:
                advanced = True
                current = git(pool.store, "rev-parse", pool.baseline_ref())
                commands = ["start\n"]
                for sha in (current, baseline_c):
                    ref = pool.generation_ref(sha)
                    old = common.run_git(
                        pool.store, ["rev-parse", "--verify", ref], check=False
                    )
                    commands.append(
                        f"update {ref} {sha} {old.stdout.strip() if old.returncode == 0 else '0' * 40}\n"
                    )
                commands.append(f"update {pool.baseline_ref()} {baseline_c} {current}\n")
                commands.extend(("prepare\n", "commit\n"))
                common.run_git(
                    pool.store,
                    ["update-ref", "--stdin"],
                    input_data="".join(commands).encode("ascii"),
                )
                state = json.loads(pool.state_path.read_text(encoding="utf-8"))
                state.update(baseline_sha=baseline_c, verified_at=common.utc_now())
                original_atomic(pool.state_path, state)

        with patch.object(
            review_setup, "atomic_json", side_effect=advance_after_normalization_intent
        ):
            result = self.explicit_setup(slot=1)
        self.assertTrue(advanced)
        self.assertNotEqual(baseline_c, baseline_b)
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["baseline_sha"], baseline_c)
        self.assertEqual(git(source, "rev-parse", "HEAD"), self.snapshot["head_sha"])

    def test_setup_retry_recleans_dirt_after_interrupted_stale_normalization(self):
        import review_setup

        pool, _, baseline_b, source = self._advance_baseline_through_reset()
        outer = self.root / pool.slot_relative_path(1)
        original = common.run_git
        interrupted = False

        def checkout_then_interrupt(repo, args, **kwargs):
            nonlocal interrupted
            result = original(repo, args, **kwargs)
            if (
                repo == source
                and args[:2] == ["checkout", "--detach"]
                and args[2] == baseline_b
                and not interrupted
            ):
                interrupted = True
                raise common.ReviewError("fixture setup normalization interruption")
            return result

        with patch.object(common, "run_git", side_effect=checkout_then_interrupt):
            with self.assertRaisesRegex(common.ReviewError, "normalization interruption"):
                self.explicit_setup(slot=1)
        self.assertEqual(self._cleanup_journal(slot=1)[1]["phase"], "normalizing")
        self._dirty_all_classes(outer, source, "normalization-retry")

        result = self.explicit_setup(slot=1)
        self.assertEqual(result["status"], "ready")
        self._assert_dirt_removed(outer, source, "normalization-retry")
        self.assertEqual(git(source, "rev-parse", "HEAD"), self.snapshot["head_sha"])

    def test_repeat_same_pr_setup_does_not_normalize_review_head_as_baseline(self):
        with self.fixture_remote(), self.state.lock(shared=True):
            pool = common.SourcePool(self.state, self.module, reserve_bytes=0)
            pool.warm([1])
            (self.source / "new-default.txt").write_text("new default\n", encoding="utf-8")
            git(self.source, "add", "new-default.txt")
            git(self.source, "commit", "-m", "new default")
            baseline = pool.refresh(persist=False)
            pool.publish_baseline(baseline)
        result = self.explicit_setup(slot=1)
        source = self.root / pool.slot_relative_path(1) / self.module["path"]
        self.assertEqual(git(source, "rev-parse", "HEAD"), self.snapshot["head_sha"])
        self.assertNotEqual(self.snapshot["head_sha"], baseline)
        repeated = self.explicit_setup(slot=1)
        self.assertEqual(repeated["status"], "ready")
        self.assertEqual(git(source, "rev-parse", "HEAD"), self.snapshot["head_sha"])

    def test_forged_nested_legacy_baseline_ref_does_not_grant_provenance(self):
        import review_setup

        pool, _, _, source = self._advance_baseline_through_reset()
        git(source, "checkout", "--detach", self.base)
        git(
            pool.store,
            "update-ref",
            f"refs/review-setup/retain/{pool.repo_key}/forged/deeper/baseline",
            self.base,
        )
        row = review_setup._pool_status(self.root, self.module, 1)
        self.assertEqual(row["status"], "blocked")
        self.assertEqual(git(source, "rev-parse", "HEAD"), self.base)

    def test_publication_recovery_can_advance_to_newer_fetched_target(self):
        with self.fixture_remote(), self.state.lock(shared=True):
            pool = common.SourcePool(self.state, self.module, reserve_bytes=0)
            baseline_a = pool.warm([0])[0]["baseline_sha"]
            (self.source / "publication-b2.txt").write_text("b\n", encoding="utf-8")
            git(self.source, "add", "publication-b2.txt")
            git(self.source, "commit", "-m", "publication b2")
            baseline_b = pool.refresh(persist=False)
            original = pool._write_state
            failed = False

            def fail_b_state(updates):
                nonlocal failed
                if updates.get("baseline_sha") == baseline_b and not failed:
                    failed = True
                    raise common.ReviewError("fixture b-state failure")
                return original(updates)

            with patch.object(pool, "_write_state", side_effect=fail_b_state):
                with self.assertRaisesRegex(common.ReviewError, "b-state failure"):
                    pool.publish_baseline(baseline_b)
            (self.source / "publication-c2.txt").write_text("c\n", encoding="utf-8")
            git(self.source, "add", "publication-c2.txt")
            git(self.source, "commit", "-m", "publication c2")
            baseline_c = pool.refresh(persist=False)
            pool.publish_baseline(baseline_c)
        self.assertEqual(pool.cached_baseline_sha(), baseline_c)
        for sha in (baseline_a, baseline_b, baseline_c):
            self.assertEqual(git(pool.store, "rev-parse", pool.generation_ref(sha)), sha)
        self.assertFalse(pool.publication_intent_path().exists())

    def test_unchanged_publication_verifies_existing_generation_in_ref_transaction(self):
        """Bypassing the expected-old transaction on an unchanged target must fail."""
        with self.fixture_remote(), self.state.lock(shared=True):
            pool = common.SourcePool(self.state, self.module, reserve_bytes=0)
            baseline = pool.warm([0])[0]["baseline_sha"]
            real_run_git = common.run_git
            transactions = []

            def record_update_ref(repo, args, **kwargs):
                if repo == pool.store and args == ["update-ref", "--stdin"]:
                    transactions.append(kwargs.get("input_data", b"").decode("ascii"))
                return real_run_git(repo, args, **kwargs)

            with patch.object(common, "run_git", side_effect=record_update_ref):
                pool.publish_baseline(baseline)
        expected = (
            f"update {pool.generation_ref(baseline)} {baseline} {baseline}\n"
            f"update {pool.baseline_ref()} {baseline} {baseline}\n"
        )
        self.assertEqual(len(transactions), 1)
        self.assertIn(expected, transactions[0])
        self.assertEqual(pool.cached_baseline_sha(), baseline)
        self.assertFalse(pool.publication_intent_path().exists())

    def test_unchanged_reset_bootstraps_pre_generation_installation(self):
        """Removing same-SHA generation creation must break this reset migration."""
        with self.fixture_remote(), self.state.lock(shared=True):
            pool = common.SourcePool(self.state, self.module, reserve_bytes=0)
            row = pool.warm([0])[0]
            baseline = row["baseline_sha"]
            git(pool.store, "update-ref", "-d", pool.generation_ref(baseline), baseline)
            self.assertFalse(pool._verify_generation_ref(baseline))
            retained = {
                "head": self.head,
                "base": self.base,
                "target": self.base,
                "baseline": baseline,
            }
            with self.state.slot_lock(
                pool.repo_key, 0, phase="fixture-unchanged-reset"
            ), common.slot_writer_scope(
                self.state, pool.repo_key, 0, phase="fixture-unchanged-reset"
            ):
                result = pool.reset_source(
                    0,
                    "unchanged-reset-migration",
                    retained,
                    refresh=True,
                )
        self.assertEqual(result["baseline_sha"], baseline)
        self.assertEqual(pool.cached_baseline_sha(), baseline)
        self.assertEqual(git(pool.store, "rev-parse", pool.generation_ref(baseline)), baseline)
        self.assertFalse(pool.publication_intent_path().exists())

    def test_unjournaled_generation_and_baseline_ref_mutation_is_never_reconciled(self):
        """Generation existence alone must not prove an owned interrupted publish."""
        import review_setup

        with self.fixture_remote(), self.state.lock(shared=True):
            pool = common.SourcePool(self.state, self.module, reserve_bytes=0)
            baseline_a = pool.warm([0])[0]["baseline_sha"]
            unknown = self.base
            self.assertNotEqual(unknown, baseline_a)
            state_before = pool.state_path.read_bytes()
            commands = [
                "start\n",
                f"update {pool.generation_ref(unknown)} {unknown} {'0' * 40}\n",
                f"update {pool.baseline_ref()} {unknown} {baseline_a}\n",
                "prepare\n",
                "commit\n",
            ]
            common.run_git(
                pool.store,
                ["update-ref", "--stdin"],
                input_data="".join(commands).encode("ascii"),
            )
            refs_before = git(
                pool.store,
                "for-each-ref",
                "--format=%(refname) %(objectname)",
                "refs/review-setup/baseline-generations",
                pool.baseline_ref(),
            )
            status = review_setup._pool_status(self.root, self.module, 0)
            warm = review_setup.warm_run(self.root, "Nova", 0)
            self.assertEqual(status["status"], "blocked")
            self.assertEqual(warm["status"], "blocked")
            with self.assertRaisesRegex(common.ReviewError, "intent|disagree"):
                pool.publish_baseline(baseline_a)
        self.assertEqual(pool.state_path.read_bytes(), state_before)
        self.assertEqual(
            git(
                pool.store,
                "for-each-ref",
                "--format=%(refname) %(objectname)",
                "refs/review-setup/baseline-generations",
                pool.baseline_ref(),
            ),
            refs_before,
        )
        self.assertEqual(git(pool.store, "rev-parse", pool.baseline_ref()), unknown)
        self.assertFalse(pool.publication_intent_path().exists())

    def test_unknown_detached_ancestor_is_never_managed_baseline_provenance(self):
        import review_setup

        pool, _, _, source = self._advance_baseline_through_reset()
        git(source, "checkout", "--detach", self.base)
        before = git(source, "rev-parse", "HEAD")
        status = review_setup._pool_status(self.root, self.module, 1)
        self.assertEqual(status["status"], "blocked")
        with self.fixture_remote():
            refused = review_setup.warm_run(self.root, "Nova", 1)
        self.assertEqual(refused["status"], "blocked")
        self.assertEqual(git(source, "rev-parse", "HEAD"), before)
        with self.assertRaises(common.ReviewError):
            self.explicit_setup(slot=1)
        self.assertEqual(git(source, "rev-parse", "HEAD"), before)

    def test_prior_generation_survives_run_discard_and_maintenance(self):
        import review_setup

        pool, baseline_a, baseline_b, _ = self._advance_baseline_through_reset()
        with self.state.lock(shared=True):
            pool.drop_retention(
                "baseline-advance",
                expected={
                    "head": self.head,
                    "base": self.base,
                    "target": self.base,
                    "baseline": baseline_a,
                },
            )
        self.assertEqual(
            git(
                pool.store,
                "for-each-ref",
                "--format=%(refname)",
                pool.retention_namespace("baseline-advance"),
            ),
            "",
        )
        with self.state.lock(shared=True):
            maintained = pool.maintain(now="2026-09-11T12:00:00+00:00", reserve_bytes=0)
        self.assertEqual(maintained["status"], "completed")
        self.assertEqual(git(pool.store, "rev-parse", pool.generation_ref(baseline_a)), baseline_a)
        self.assertEqual(git(pool.store, "rev-parse", pool.generation_ref(baseline_b)), baseline_b)
        self.assertEqual(review_setup._pool_status(self.root, self.module, 1)["status"], "stale")

    def test_terminal_merge_baseline_is_contextual_not_global_provenance(self):
        import review_setup

        with self.fixture_remote(), self.state.lock(shared=True):
            pool = common.SourcePool(self.state, self.module, reserve_bytes=0)
            rows = pool.warm([1, 2])
            for slot in (1, 2):
                source = self.root / pool.slot_relative_path(slot) / self.module["path"]
                git(source, "checkout", "--detach", self.base)
            manifest = self._pool_manifest(pool, rows[0], status="merged")
            manifest["run_id"] = "terminal-merge"
            manifest["slot_number"] = 1
            manifest["slot"] = pool.slot_name(1)
            manifest["slot_id"] = pool.slot_name(1)
            manifest["slot_path"] = pool.slot_relative_path(1)
            manifest["merge_baseline_sha"] = self.base
            manifest["retained_commits"] = pool.retain(
                manifest["run_id"],
                {
                    "head": self.head,
                    "base": self.base,
                    "target": self.base,
                    "baseline": rows[0]["baseline_sha"],
                    "merge-baseline": self.base,
                },
            )
            manifest["retention_refs"] = {
                kind: pool.retention_ref(manifest["run_id"], kind)
                for kind in manifest["retained_commits"]
            }
            self.state.save_pool_run(manifest)

        own = review_setup._pool_status(self.root, self.module, 1)
        other = review_setup._pool_status(self.root, self.module, 2)
        self.assertEqual(own["status"], "stale")
        self.assertEqual(other["status"], "blocked")
        with self.fixture_remote():
            warmed = review_setup.warm_run(self.root, "Nova", 1)
        self.assertEqual(warmed["head_sha"], rows[0]["baseline_sha"])

    def test_malformed_legacy_baseline_ref_cannot_authorize_unknown_head(self):
        import review_setup

        pool, _, _, source = self._advance_baseline_through_reset()
        git(source, "checkout", "--detach", self.base)
        with self.state.lock(shared=True):
            git(
                pool.store,
                "update-ref",
                f"refs/review-setup/retain/{pool.repo_key}/forged/deeper/baseline",
                self.base,
            )
        status = review_setup._pool_status(self.root, self.module, 1)
        self.assertEqual(status["status"], "blocked")
        self.assertEqual(git(source, "rev-parse", "HEAD"), self.base)

    def test_legacy_run_baseline_is_promoted_only_during_stale_normalization(self):
        import review_setup

        pool, baseline_a, baseline_b, source = self._advance_baseline_through_reset()
        with self.state.lock(shared=True):
            git(pool.store, "update-ref", "-d", pool.generation_ref(baseline_a), baseline_a)
        self.assertNotEqual(
            common.run_git(
                pool.store,
                ["show-ref", "--verify", "--quiet", pool.generation_ref(baseline_a)],
                check=False,
            ).returncode,
            0,
        )
        self.assertEqual(review_setup._pool_status(self.root, self.module, 1)["status"], "stale")
        with self.fixture_remote():
            result = review_setup.warm_run(self.root, "Nova", 1)
        self.assertEqual(result["head_sha"], baseline_b)
        self.assertEqual(git(pool.store, "rev-parse", pool.generation_ref(baseline_a)), baseline_a)
        self.assertEqual(git(source, "rev-parse", "HEAD"), baseline_b)

    def test_status_detects_real_baseline_publication_interleaving(self):
        import review_setup

        with self.fixture_remote(), self.state.lock(shared=True):
            pool = common.SourcePool(self.state, self.module, reserve_bytes=0)
            pool.warm([0])
            (self.source / "race-b.txt").write_text("b\n", encoding="utf-8")
            git(self.source, "add", "race-b.txt")
            git(self.source, "commit", "-m", "race b")
            baseline_b = pool.refresh(persist=False)

        release = threading.Event()
        published = threading.Event()
        failures = []

        def publisher():
            try:
                if not release.wait(timeout=10):
                    raise TimeoutError("status never reached publication gate")
                state = common.ReviewState(self.root)
                contender = common.SourcePool(state, self.module, reserve_bytes=0)
                with common.lock_session(20), state.lock(shared=True):
                    contender.publish_baseline(baseline_b)
            except BaseException as exc:
                failures.append(exc)
            finally:
                published.set()

        original = common.SourcePool.cached_baseline_sha_read_only
        main_ident = threading.get_ident()
        reads = 0

        def interleaved_read(instance):
            nonlocal reads
            value = original(instance)
            if threading.get_ident() == main_ident:
                reads += 1
                if reads == 1:
                    release.set()
                    if not published.wait(timeout=10):
                        raise TimeoutError("publisher did not finish")
            return value

        worker = threading.Thread(target=publisher)
        worker.start()
        try:
            with patch.object(
                common.SourcePool,
                "cached_baseline_sha_read_only",
                interleaved_read,
            ):
                row = review_setup._pool_status(self.root, self.module, 0)
        finally:
            release.set()
            worker.join(timeout=10)
        self.assertFalse(worker.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual(row["status"], "blocked")
        self.assertIn("changed during read-only status", row["reason"])

    def test_baseline_publication_recovers_exact_ref_success_state_failure(self):
        with self.fixture_remote(), self.state.lock(shared=True):
            pool = common.SourcePool(self.state, self.module, reserve_bytes=0)
            baseline_a = pool.warm([0])[0]["baseline_sha"]
            (self.source / "publication-b.txt").write_text("b\n", encoding="utf-8")
            git(self.source, "add", "publication-b.txt")
            git(self.source, "commit", "-m", "publication b")
            baseline_b = pool.refresh(persist=False)
            original = pool._write_state
            failed = False

            def fail_final_state(updates):
                nonlocal failed
                if updates.get("baseline_sha") == baseline_b and not failed:
                    failed = True
                    raise common.ReviewError("fixture state publication failure")
                return original(updates)

            with patch.object(pool, "_write_state", side_effect=fail_final_state):
                with self.assertRaisesRegex(common.ReviewError, "state publication"):
                    pool.publish_baseline(baseline_b)
            self.assertEqual(git(pool.store, "rev-parse", pool.baseline_ref()), baseline_b)
            self.assertEqual(json.loads(pool.state_path.read_text())["baseline_sha"], baseline_a)
            self.assertTrue(pool.publication_intent_path().exists())
            pool.publish_baseline(baseline_b)
            self.assertEqual(pool.cached_baseline_sha(), baseline_b)
            self.assertEqual(git(pool.store, "rev-parse", pool.generation_ref(baseline_a)), baseline_a)
            self.assertEqual(git(pool.store, "rev-parse", pool.generation_ref(baseline_b)), baseline_b)
            self.assertFalse(pool.publication_intent_path().exists())

    def test_publication_retry_replays_durable_intent_written_before_refs(self):
        """Applying refs without replaying the prepared intent must fail this retry."""
        with self.fixture_remote(), self.state.lock(shared=True):
            pool = common.SourcePool(self.state, self.module, reserve_bytes=0)
            baseline_a = pool.warm([0])[0]["baseline_sha"]
            (self.source / "publication-prepared.txt").write_text("b\n", encoding="utf-8")
            git(self.source, "add", "publication-prepared.txt")
            git(self.source, "commit", "-m", "publication prepared")
            baseline_b = pool.refresh(persist=False)
            with patch.object(
                pool,
                "_apply_publication_refs",
                side_effect=common.ReviewError("fixture before publication refs"),
            ):
                with self.assertRaisesRegex(common.ReviewError, "before publication refs"):
                    pool.publish_baseline(baseline_b)
            self.assertEqual(git(pool.store, "rev-parse", pool.baseline_ref()), baseline_a)
            self.assertEqual(json.loads(pool.state_path.read_text())["baseline_sha"], baseline_a)
            self.assertEqual(
                json.loads(pool.publication_intent_path().read_text())["phase"],
                "prepared",
            )
            pool.publish_baseline(baseline_b)
        self.assertEqual(pool.cached_baseline_sha(), baseline_b)
        self.assertFalse(pool.publication_intent_path().exists())

    def test_same_sha_bootstrap_recovers_when_refs_commit_before_phase_update(self):
        """Skipping prepared-intent replay must strand this pre-generation migration."""
        with self.fixture_remote(), self.state.lock(shared=True):
            pool = common.SourcePool(self.state, self.module, reserve_bytes=0)
            baseline = pool.warm([0])[0]["baseline_sha"]
            git(pool.store, "update-ref", "-d", pool.generation_ref(baseline), baseline)
            real_atomic = common.atomic_json
            interrupted = False

            def interrupt_refs_applied(path, value):
                nonlocal interrupted
                if (
                    path == pool.publication_intent_path()
                    and value.get("phase") == "refs-applied"
                    and not interrupted
                ):
                    interrupted = True
                    raise common.ReviewError("fixture after same-SHA refs")
                return real_atomic(path, value)

            with patch.object(common, "atomic_json", side_effect=interrupt_refs_applied):
                with self.assertRaisesRegex(common.ReviewError, "same-SHA refs"):
                    pool.publish_baseline(baseline)
            self.assertEqual(git(pool.store, "rev-parse", pool.baseline_ref()), baseline)
            self.assertEqual(git(pool.store, "rev-parse", pool.generation_ref(baseline)), baseline)
            self.assertEqual(
                json.loads(pool.publication_intent_path().read_text())["phase"],
                "prepared",
            )
            pool.publish_baseline(baseline)
        self.assertEqual(pool.cached_baseline_sha(), baseline)
        self.assertFalse(pool.publication_intent_path().exists())

    def test_publication_retry_records_state_applied_after_json_update(self):
        """Retry must recognize target JSON when its phase publication was interrupted."""
        with self.fixture_remote(), self.state.lock(shared=True):
            pool = common.SourcePool(self.state, self.module, reserve_bytes=0)
            pool.warm([0])
            (self.source / "publication-json-boundary.txt").write_text(
                "b\n", encoding="utf-8"
            )
            git(self.source, "add", "publication-json-boundary.txt")
            git(self.source, "commit", "-m", "publication json boundary")
            baseline_b = pool.refresh(persist=False)
            real_atomic = common.atomic_json
            interrupted = False

            def interrupt_state_applied(path, value):
                nonlocal interrupted
                if (
                    path == pool.publication_intent_path()
                    and value.get("phase") == "state-applied"
                    and not interrupted
                ):
                    interrupted = True
                    raise common.ReviewError("fixture after baseline JSON")
                return real_atomic(path, value)

            with patch.object(common, "atomic_json", side_effect=interrupt_state_applied):
                with self.assertRaisesRegex(common.ReviewError, "after baseline JSON"):
                    pool.publish_baseline(baseline_b)
            self.assertEqual(json.loads(pool.state_path.read_text())["baseline_sha"], baseline_b)
            self.assertEqual(git(pool.store, "rev-parse", pool.baseline_ref()), baseline_b)
            self.assertEqual(
                json.loads(pool.publication_intent_path().read_text())["phase"],
                "refs-applied",
            )
            pool.publish_baseline(baseline_b)
        self.assertEqual(pool.cached_baseline_sha(), baseline_b)
        self.assertFalse(pool.publication_intent_path().exists())

    def test_publication_retry_finishes_state_applied_intent_before_retirement(self):
        """Deleting intent before exact final verification must fail this retry."""
        with self.fixture_remote(), self.state.lock(shared=True):
            pool = common.SourcePool(self.state, self.module, reserve_bytes=0)
            pool.warm([0])
            (self.source / "publication-retire.txt").write_text("b\n", encoding="utf-8")
            git(self.source, "add", "publication-retire.txt")
            git(self.source, "commit", "-m", "publication retirement")
            baseline_b = pool.refresh(persist=False)
            retired = False
            original_retire = pool._retire_publication_intent

            def interrupt_retirement(intent):
                nonlocal retired
                if not retired:
                    retired = True
                    raise common.ReviewError("fixture before intent retirement")
                return original_retire(intent)

            with patch.object(
                pool,
                "_retire_publication_intent",
                side_effect=interrupt_retirement,
            ):
                with self.assertRaisesRegex(common.ReviewError, "intent retirement"):
                    pool.publish_baseline(baseline_b)
            self.assertEqual(pool.cached_baseline_sha(), None)
            self.assertEqual(
                json.loads(pool.publication_intent_path().read_text())["phase"],
                "state-applied",
            )
            self.assertEqual(json.loads(pool.state_path.read_text())["baseline_sha"], baseline_b)
            self.assertEqual(git(pool.store, "rev-parse", pool.baseline_ref()), baseline_b)
            pool.publish_baseline(baseline_b)
        self.assertEqual(pool.cached_baseline_sha(), baseline_b)
        self.assertFalse(pool.publication_intent_path().exists())

    def test_foreign_publication_intent_is_preserved_and_refused_without_mutation(self):
        """Weak identity validation or eager cleanup must fail this fail-closed case."""
        with self.fixture_remote(), self.state.lock(shared=True):
            pool = common.SourcePool(self.state, self.module, reserve_bytes=0)
            baseline = pool.warm([0])[0]["baseline_sha"]
            foreign = {
                "schema_version": 1,
                "workspace_identity": "f" * 64,
                "repo_key": pool.repo_key,
                "repo_name": pool.repo_name,
                "module_path": pool.module["path"],
                "store_path": str(pool.store.resolve()),
                "baseline_ref": pool.baseline_ref(),
                "expected_state": pool._read_state(),
                "expected_old_sha": baseline,
                "target_sha": baseline,
                "target_state": pool._read_state(),
                "phase": "prepared",
            }
            common.atomic_json(pool.publication_intent_path(), foreign)
            intent_before = pool.publication_intent_path().read_bytes()
            state_before = pool.state_path.read_bytes()
            refs_before = git(
                pool.store,
                "for-each-ref",
                "--format=%(refname) %(objectname)",
                "refs/review-setup/baseline-generations",
                pool.baseline_ref(),
            )
            with self.assertRaisesRegex(common.ReviewError, "intent"):
                pool.publish_baseline(baseline)
        self.assertEqual(pool.publication_intent_path().read_bytes(), intent_before)
        self.assertEqual(pool.state_path.read_bytes(), state_before)
        self.assertEqual(
            git(
                pool.store,
                "for-each-ref",
                "--format=%(refname) %(objectname)",
                "refs/review-setup/baseline-generations",
                pool.baseline_ref(),
            ),
            refs_before,
        )

    def test_prepared_publication_intent_ref_mismatch_is_preserved_and_refused(self):
        """Broad intent recovery must not accept a third observed baseline ref."""
        with self.fixture_remote(), self.state.lock(shared=True):
            pool = common.SourcePool(self.state, self.module, reserve_bytes=0)
            baseline_a = pool.warm([0])[0]["baseline_sha"]
            (self.source / "publication-owned-b.txt").write_text("b\n", encoding="utf-8")
            git(self.source, "add", "publication-owned-b.txt")
            git(self.source, "commit", "-m", "publication owned b")
            baseline_b = pool.refresh(persist=False)
            with patch.object(
                pool,
                "_apply_publication_refs",
                side_effect=common.ReviewError("fixture leaves prepared intent"),
            ):
                with self.assertRaisesRegex(common.ReviewError, "prepared intent"):
                    pool.publish_baseline(baseline_b)
            intent_before = pool.publication_intent_path().read_bytes()
            state_before = pool.state_path.read_bytes()
            unknown = self.base
            self.assertNotIn(unknown, (baseline_a, baseline_b))
            git(pool.store, "update-ref", pool.baseline_ref(), unknown, baseline_a)
            with self.assertRaisesRegex(common.ReviewError, "intent|ref"):
                pool.publish_baseline(baseline_b)
        self.assertEqual(pool.publication_intent_path().read_bytes(), intent_before)
        self.assertEqual(pool.state_path.read_bytes(), state_before)
        self.assertEqual(git(pool.store, "rev-parse", pool.baseline_ref()), unknown)

    def test_malformed_publication_intent_is_preserved_and_refused(self):
        """Replacing malformed intent would erase evidence and fail this assertion."""
        with self.fixture_remote(), self.state.lock(shared=True):
            pool = common.SourcePool(self.state, self.module, reserve_bytes=0)
            baseline = pool.warm([0])[0]["baseline_sha"]
            pool.publication_intent_path().write_bytes(b"{not-json\n")
            intent_before = pool.publication_intent_path().read_bytes()
            state_before = pool.state_path.read_bytes()
            with self.assertRaisesRegex(common.ReviewError, "intent"):
                pool.publish_baseline(baseline)
        self.assertEqual(pool.publication_intent_path().read_bytes(), intent_before)
        self.assertEqual(pool.state_path.read_bytes(), state_before)
        self.assertEqual(git(pool.store, "rev-parse", pool.baseline_ref()), baseline)

    def test_mismatched_self_named_generation_ref_is_never_accepted(self):
        """Treating a generation ref's name as proof without its target must fail."""
        with self.fixture_remote(), self.state.lock(shared=True):
            pool = common.SourcePool(self.state, self.module, reserve_bytes=0)
            baseline_a = pool.warm([0])[0]["baseline_sha"]
            (self.source / "publication-generation-b.txt").write_text("b\n", encoding="utf-8")
            git(self.source, "add", "publication-generation-b.txt")
            git(self.source, "commit", "-m", "publication generation b")
            baseline_b = pool.refresh(persist=False)
            git(pool.store, "update-ref", pool.generation_ref(baseline_b), baseline_a)
            state_before = pool.state_path.read_bytes()
            with self.assertRaisesRegex(common.ReviewError, "generation"):
                pool.publish_baseline(baseline_b)
        self.assertEqual(pool.state_path.read_bytes(), state_before)
        self.assertEqual(git(pool.store, "rev-parse", pool.baseline_ref()), baseline_a)
        self.assertEqual(git(pool.store, "rev-parse", pool.generation_ref(baseline_b)), baseline_a)
        self.assertFalse(pool.publication_intent_path().exists())

    def test_publication_interruption_can_advance_to_newer_target(self):
        with self.fixture_remote(), self.state.lock(shared=True):
            pool = common.SourcePool(self.state, self.module, reserve_bytes=0)
            baseline_a = pool.warm([0])[0]["baseline_sha"]
            (self.source / "publication-b.txt").write_text("b\n", encoding="utf-8")
            git(self.source, "add", "publication-b.txt")
            git(self.source, "commit", "-m", "publication b")
            baseline_b = pool.refresh(persist=False)
            original = pool._write_state
            failed = False

            def fail_b_state(updates):
                nonlocal failed
                if updates.get("baseline_sha") == baseline_b and not failed:
                    failed = True
                    raise common.ReviewError("fixture state publication failure")
                return original(updates)

            with patch.object(pool, "_write_state", side_effect=fail_b_state):
                with self.assertRaises(common.ReviewError):
                    pool.publish_baseline(baseline_b)
            (self.source / "publication-c.txt").write_text("c\n", encoding="utf-8")
            git(self.source, "add", "publication-c.txt")
            git(self.source, "commit", "-m", "publication c")
            baseline_c = pool.refresh(persist=False)
            pool.publish_baseline(baseline_c)
            self.assertEqual(pool.cached_baseline_sha(), baseline_c)
            for sha in (baseline_a, baseline_b, baseline_c):
                self.assertEqual(git(pool.store, "rev-parse", pool.generation_ref(sha)), sha)

    def test_same_pr_rerun_preserves_authorized_pr_head_until_prepare(self):
        first = self.explicit_setup(slot=0)
        source = self.root / first["slot_path"] / self.module["path"]
        self.assertEqual(git(source, "rev-parse", "HEAD"), self.snapshot["head_sha"])
        second = self.explicit_setup(slot=0)
        self.assertEqual(second["status"], "ready")
        self.assertEqual(git(source, "rev-parse", "HEAD"), self.snapshot["head_sha"])

    def test_normalizing_retry_recleans_dirt_after_interruption(self):
        import review_setup

        pool, _, _, source = self._advance_baseline_through_reset()
        original = review_setup.atomic_json
        interrupted = False

        def interrupt_normalizing(path, value):
            nonlocal interrupted
            original(path, value)
            if value.get("phase") == "normalizing" and not interrupted:
                interrupted = True
                raise common.ReviewError("fixture normalizing interruption")

        with patch.object(review_setup, "atomic_json", side_effect=interrupt_normalizing):
            with self.assertRaisesRegex(common.ReviewError, "normalizing interruption"):
                self.explicit_setup(slot=1)
        dirt = source / "retry-dirt.txt"
        dirt.write_text("remove\n", encoding="utf-8")
        result = self.explicit_setup(slot=1)
        self.assertEqual(result["status"], "ready")
        self.assertFalse(dirt.exists())

    def test_cleanup_retry_uses_pinned_target_after_newer_publication(self):
        import review_setup

        pool, _, baseline_b, source = self._advance_baseline_through_reset()
        original = review_setup.atomic_json
        interrupted = False

        def interrupt_normalizing(path, value):
            nonlocal interrupted
            original(path, value)
            if value.get("phase") == "normalizing" and not interrupted:
                interrupted = True
                raise common.ReviewError("fixture pinned-target interruption")

        with patch.object(review_setup, "atomic_json", side_effect=interrupt_normalizing):
            with self.assertRaisesRegex(common.ReviewError, "pinned-target interruption"):
                self.explicit_setup(slot=1)
        (self.source / "baseline-c.txt").write_text("c\n", encoding="utf-8")
        git(self.source, "add", "baseline-c.txt")
        git(self.source, "commit", "-m", "baseline c")
        with self.fixture_remote(), self.state.lock(shared=True):
            baseline_c = pool.refresh(persist=False)
            pool.publish_baseline(baseline_c)
        self.assertNotEqual(baseline_b, baseline_c)
        result = self.explicit_setup(slot=1)
        self.assertEqual(result["status"], "ready")
        self.assertEqual(git(source, "rev-parse", "HEAD"), self.snapshot["head_sha"])

    def test_interrupted_stale_normalization_is_idempotent_on_exact_retry(self):
        import review_setup

        pool, _, baseline_b, source = self._advance_baseline_through_reset()
        original = common.run_git
        interrupted = False

        def checkout_then_interrupt(repo, args, **kwargs):
            nonlocal interrupted
            result = original(repo, args, **kwargs)
            if repo == source and args[:2] == ["checkout", "--detach"] and not interrupted:
                interrupted = True
                raise common.ReviewError("fixture interruption after checkout")
            return result

        with self.fixture_remote(), patch.object(common, "run_git", side_effect=checkout_then_interrupt):
            with self.assertRaisesRegex(common.ReviewError, "after checkout"):
                review_setup.warm_run(self.root, "Nova", 1)
        self.assertEqual(git(source, "rev-parse", "HEAD"), baseline_b)
        with self.fixture_remote():
            result = review_setup.warm_run(self.root, "Nova", 1)
        self.assertEqual(result["status"], "warm")
        self.assertEqual(result["head_sha"], baseline_b)

    def test_explicit_setup_rechecks_owner_before_stale_cleanup(self):
        import review_setup

        pool, _, _, source = self._advance_baseline_through_reset()
        before = git(source, "rev-parse", "HEAD")
        original_cleanup = review_setup._cleanup_setup_slot
        changed = False

        def replace_owner_then_cleanup(
            state,
            snapshot,
            module,
            requested,
            reservation,
            *,
            reclaim_generation=None,
        ):
            nonlocal changed
            changed = True
            state.save_setup_reservation({**reservation, "id": "f" * 32})
            return original_cleanup(
                state,
                snapshot,
                module,
                requested,
                reservation,
                reclaim_generation=reclaim_generation,
            )

        with patch.object(
            review_setup,
            "_cleanup_setup_slot",
            side_effect=replace_owner_then_cleanup,
        ):
            with self.assertRaisesRegex(common.ReviewError, "ownership changed"):
                self.explicit_setup(slot=1)
        self.assertTrue(changed)
        self.assertEqual(git(source, "rev-parse", "HEAD"), before)

    def explicit_setup(self, snapshot=None, slot=0, collect=None, **options):
        import review_setup
        snapshot = snapshot or self.snapshot
        report = {"complete": True, "warnings": [], "files": [],
                  "work_items": {"roots": [], "nodes": []}, "provenance": {}}
        collect = collect or (lambda *a, **k: dict(report))
        client = type("Client", (), {"org_url": "https://dev.azure.com/org"})()
        with self.fixture_remote(), patch("review_context.resolve_pr", return_value=snapshot), \
             patch("review_setup._collect", side_effect=collect), \
             patch("review_context.render_markdown", return_value="# regenerated\n"):
            return review_setup.setup_run(
                self.root,
                snapshot["pr_number"],
                slot=slot,
                client=client,
                **options,
            )

    def _cleanup_journal(self, slot=0):
        repo_key = common.SourcePool(self.state, self.module, reserve_bytes=0).repo_key
        path = self.state.directory / f"cleanup/{repo_key}-{slot}.json"
        return path, json.loads(path.read_text(encoding="utf-8"))

    def _dirty_all_classes(self, outer, source, prefix):
        for repo, tracked in ((outer, ".gitmodules"), (source, "file.txt")):
            (repo / tracked).write_text(f"{prefix}-staged\n", encoding="utf-8")
            git(repo, "add", tracked)
            (repo / tracked).write_text(f"{prefix}-unstaged\n", encoding="utf-8")
            (repo / f"{prefix}-untracked.txt").write_text("discard", encoding="utf-8")
            with (Path(git(repo, "rev-parse", "--git-path", "info/exclude"))).open(
                "a", encoding="utf-8"
            ) as stream:
                stream.write(f"\n/{prefix}-ignored.tmp\n")
            (repo / f"{prefix}-ignored.tmp").write_text("discard ignored", encoding="utf-8")

    def _assert_dirt_removed(self, outer, source, prefix):
        for repo in (outer, source):
            self.assertFalse((repo / f"{prefix}-untracked.txt").exists())
            self.assertFalse((repo / f"{prefix}-ignored.tmp").exists())
            self.assertEqual(git(repo, "diff", "--cached", "--name-only"), "")
            self.assertNotIn(f"{prefix}-unstaged", (repo / (".gitmodules" if repo == outer else "file.txt")).read_text())

    def _authorize_reclaim(self, manifest, outer, source):
        inspected = common.inspect_preservation_generation(
            self.state,
            manifest["repo_key"],
            manifest["slot_number"],
            manifest["run_id"],
            outer,
            source,
        )
        manifest["reclaim_authorization"] = {
            "schema_version": 1,
            "workspace_identity": self.state.identity,
            "repo_key": manifest["repo_key"],
            "slot_number": manifest["slot_number"],
            "run_id": manifest["run_id"],
            "outer_head": git(outer, "rev-parse", "HEAD"),
            "outer_ref": common.current_ref(outer),
            "source_head": git(source, "rev-parse", "HEAD"),
            "source_ref": common.current_ref(source),
            "reclaim_generation": inspected["reclaim_generation"],
            "basis": "fixture exact generation released for preservation-first reclaim",
            "authorized_at": common.utc_now(),
        }
        self.state.save_pool_run(manifest)
        return manifest

    def _ignore_in(self, repo, relative):
        exclude = Path(git(repo, "rev-parse", "--git-path", "info/exclude"))
        exclude.parent.mkdir(parents=True, exist_ok=True)
        with exclude.open("a", encoding="utf-8") as stream:
            stream.write(f"\n/{relative}\n")

    def _interrupt_at_cleaned(self):
        import review_setup

        repo_key = common.SourcePool(self.state, self.module, reserve_bytes=0).repo_key
        cleanup_path = self.state.directory / f"cleanup/{repo_key}-0.json"
        original = review_setup.atomic_json
        interrupted = False

        def persist_then_interrupt(path, value):
            nonlocal interrupted
            original(path, value)
            if path == cleanup_path and value.get("phase") == "cleaned" and not interrupted:
                interrupted = True
                raise common.ReviewError("fixture interruption after durable cleaned phase")

        with patch.object(review_setup, "atomic_json", side_effect=persist_then_interrupt):
            with self.assertRaisesRegex(common.ReviewError, "durable cleaned"):
                self.explicit_setup()
        self.assertTrue(interrupted)
        self.assertEqual(self._cleanup_journal()[1]["phase"], "cleaned")

    def _wait_pipe_bytes(self, descriptor, *, message, timeout=10):
        import select

        ready, _, _ = select.select([descriptor], [], [], timeout)
        self.assertTrue(ready, message)
        raw = os.read(descriptor, 64)
        self.assertTrue(raw, message)
        return raw

    def _wait_pipe_pid(self, descriptor, timeout=10):
        raw = self._wait_pipe_bytes(
            descriptor,
            message="fixture child never reached its deterministic gate",
            timeout=timeout,
        )
        self.assertTrue(raw.endswith(b"\n"), raw)
        return int(raw.strip())

    def _assert_durable_shared_identity(self, child):
        pool = common.SourcePool(self.state, self.module, reserve_bytes=0)
        journal = json.loads(
            (self.state.directory / f"shared-writers/{pool.repo_key}.json").read_text(
                encoding="utf-8"
            )
        )
        operation = journal["operation"]
        self.assertFalse(operation["spawn_pending"])
        intended = common.process_identity(child)
        identity_keys = (
            "pid",
            "pgid",
            "session",
            "start_ticks",
            "boot_id",
            "pid_namespace",
        )
        self.assertEqual(
            {key: operation["process"][key] for key in identity_keys},
            {key: intended[key] for key in identity_keys},
            "fixture must kill only after the exact intended child identity is durable",
        )
        return operation

    def _kill_actual_entrypoint_at_git(self, mode, slot=1):
        process_context = multiprocessing.get_context("fork")
        ready_read, ready_write = os.pipe()
        release_read, release_write = os.pipe()
        results = process_context.Queue()
        owner = process_context.Process(
            target=_killable_fixture_runner,
            args=(
                self.root,
                self.module,
                mode,
                slot,
                ready_write,
                release_read,
                results,
            ),
        )
        with self.fixture_remote():
            owner.start()
            child = self._wait_pipe_pid(ready_read)
            os.kill(owner.pid, signal.SIGKILL)
            owner.join(timeout=5)
            self.assertFalse(owner.is_alive(), "fixture coordinator survived SIGKILL")
        return child, owner, results, (
            ready_read,
            ready_write,
            release_read,
            release_write,
        )

    def _retire_killed_fixture(self, child, descriptors):
        release_write = descriptors[3]
        os.close(release_write)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                os.kill(child, 0)
            except ProcessLookupError:
                break
            status = Path(f"/proc/{child}/stat")
            if status.exists() and status.read_text().rsplit(") ", 1)[1].split()[0] == "Z":
                break
            time.sleep(0.01)
        else:
            os.kill(child, signal.SIGKILL)
        for descriptor in descriptors:
            if descriptor == release_write:
                continue
            try:
                os.close(descriptor)
            except OSError:
                pass

    def _slot_recovery_inspection(self, slot):
        state = common.ReviewState(self.root)
        pool = common.SourcePool(state, self.module, reserve_bytes=0)
        with (
            common.lock_session(5),
            state.lock(shared=True, phase="test-slot-inspection"),
            state.slot_lock(pool.repo_key, slot, phase="test-slot-inspection"),
        ):
            return common.recover_slot_writer(
                state, pool, slot, inspect=True
            )

    def _recover_slot_from_inspection(self, inspection, slot, **updates):
        state = common.ReviewState(self.root)
        pool = common.SourcePool(state, self.module, reserve_bytes=0)
        keys = (
            "workspace_identity",
            "repo_key",
            "repo_name",
            "slot_number",
            "slot_id",
            "slot_writer_sha256",
            "operation_id",
            "operation_phase",
            "shared_writer_sha256",
            "shared_operation_id",
            "pool_state_sha256",
            "pool_run_sha256",
            "run_id",
            "setup_reservations",
            "topology_sha256",
            "current_pid_namespace",
        )
        assertion = {key: inspection[key] for key in keys}
        assertion.update(
            prior_writers_retired=True,
            operator="fixture-authorized-operator",
            basis="owning fixture Python was reaped and its gated child was proven terminated",
        )
        assertion.update(updates)
        with (
            common.lock_session(5),
            state.lock(shared=True, phase="test-slot-recovery"),
            state.slot_lock(pool.repo_key, slot, phase="test-slot-recovery"),
        ):
            return common.recover_slot_writer(
                state, pool, slot, assertion=assertion
            )

    def _finish_fixture_processes(self, processes, release_events=()):
        for event in release_events:
            event.set()
        for process in processes:
            process.join(timeout=10)
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)
            if process.is_alive():
                process.kill()
                process.join(timeout=5)

    def _collect_fixture_results(self, results, count):
        rows = {}
        for _ in range(count):
            try:
                label, status, value = results.get(timeout=10)
            except queue.Empty:
                self.fail("fixture child did not report a result")
            rows[label] = (status, value)
        return rows

    def test_cold_store_writer_interrupted_before_identity_publication_fails_closed(self):
        process_context = multiprocessing.get_context("fork")
        ready_read, ready_write = os.pipe()
        release_read, release_write = os.pipe()
        identity_attempt_read, identity_attempt_write = os.pipe()
        identity_release_read, identity_release_write = os.pipe()
        results = process_context.Queue()
        owner = process_context.Process(
            target=_killable_cold_store_runner,
            args=(
                self.root,
                self.module,
                0,
                ready_write,
                release_read,
                results,
                identity_attempt_write,
                identity_release_read,
            ),
        )
        descriptors = (
            ready_read,
            ready_write,
            release_read,
            release_write,
            identity_attempt_read,
            identity_attempt_write,
            identity_release_read,
            identity_release_write,
        )
        try:
            with self.fixture_remote():
                owner.start()
                child = self._wait_pipe_pid(ready_read)
                self.assertEqual(
                    self._wait_pipe_bytes(
                        identity_attempt_read,
                        message="fixture never reached pre-identity publication gate",
                    ),
                    b"1",
                )
                os.kill(owner.pid, signal.SIGKILL)
                owner.join(timeout=5)
                self.assertFalse(owner.is_alive(), "fixture coordinator survived SIGKILL")
                with self.assertRaisesRegex(
                    common.ReviewError,
                    "pending or missing launch identity",
                ):
                    state = common.ReviewState(self.root)
                    pool = common.SourcePool(state, self.module, reserve_bytes=0)
                    with common.lock_session(5), state.lock(shared=True):
                        pool.warm([1])
        finally:
            self._retire_killed_fixture(child, descriptors)

    def test_killed_cold_store_writer_in_slot_zero_blocks_slot_one(self):
        process_context = multiprocessing.get_context("fork")
        ready_read, ready_write = os.pipe()
        release_read, release_write = os.pipe()
        identity_published_read, identity_published_write = os.pipe()
        results = process_context.Queue()
        owner = process_context.Process(
            target=_killable_cold_store_runner,
            args=(
                self.root,
                self.module,
                0,
                ready_write,
                release_read,
                results,
                None,
                None,
                identity_published_write,
            ),
        )
        descriptors = (
            ready_read,
            ready_write,
            release_read,
            release_write,
            identity_published_read,
            identity_published_write,
        )
        try:
            with self.fixture_remote():
                owner.start()
                child = self._wait_pipe_pid(ready_read)
                self.assertEqual(
                    self._wait_pipe_bytes(
                        identity_published_read,
                        message="fixture child identity was not durably published",
                    ),
                    b"1",
                )
                self._assert_durable_shared_identity(child)
                os.kill(owner.pid, signal.SIGKILL)
                owner.join(timeout=5)
                self.assertFalse(owner.is_alive(), "fixture coordinator survived SIGKILL")
                with self.assertRaisesRegex(common.ReviewError, "still executable"):
                    state = common.ReviewState(self.root)
                    pool = common.SourcePool(state, self.module, reserve_bytes=0)
                    with common.lock_session(5), state.lock(shared=True):
                        pool.warm([1])
        finally:
            self._retire_killed_fixture(child, descriptors)
        inspection = self._slot_recovery_inspection(0)
        self.assertEqual(inspection["shared_operation_id"], inspection["operation_id"])
        recovered = self._recover_slot_from_inspection(inspection, 0)
        self.assertFalse(recovered["pending"])
        with self.fixture_remote(), self.state.lock(shared=True):
            row = common.SourcePool(
                self.state, self.module, reserve_bytes=0
            ).warm([1])[0]
        self.assertEqual(row["slot"], "Nova-1")

    def test_two_processes_cold_warm_distinct_slots_share_one_exact_store(self):
        process_context = multiprocessing.get_context("fork")
        gate = process_context.Barrier(2)
        results = process_context.Queue()
        processes = [
            process_context.Process(
                target=_cold_warm_slot,
                args=(self.root, self.module, slot, gate, results),
            )
            for slot in (0, 1)
        ]
        with self.fixture_remote():
            for process in processes:
                process.start()
            try:
                rows = self._collect_fixture_results(results, 2)
            finally:
                self._finish_fixture_processes(processes)
        self.assertEqual(
            {label: status for label, (status, _) in rows.items()},
            {"cold-0": "ok", "cold-1": "ok"},
            rows,
        )
        pool = common.SourcePool(self.state, self.module, reserve_bytes=0)
        self.assertEqual(git(pool.store, "rev-parse", "--is-bare-repository"), "true")
        self.assertEqual(
            git(pool.store, "config", "--get", "remote.origin.url"),
            self.module["url"],
        )
        self.assertEqual(
            {
                git(
                    self.root / rows[f"cold-{slot}"][1]["slot_path"] / self.module["path"],
                    "rev-parse",
                    "--path-format=absolute",
                    "--git-common-dir",
                )
                for slot in (0, 1)
            },
            {str(pool.store.resolve())},
        )

    def test_five_way_cold_distinct_pr_setup_reaches_ready_with_shared_objects(self):
        with self.fixture_remote(), self.state.lock(shared=True):
            pool = common.SourcePool(self.state, self.module, reserve_bytes=0)
            pool.warm(range(5))
        snapshots = []
        for slot in range(5):
            branch = f"cold-pr-{slot}"
            git(self.source, "switch", "-c", branch, "main")
            path = self.source / f"cold-{slot}.txt"
            path.write_text(f"cold {slot}\n", encoding="utf-8")
            git(self.source, "add", path.name)
            git(self.source, "commit", "-m", f"cold {slot}")
            head = git(self.source, "rev-parse", "HEAD")
            self.assertNotEqual(head, pool.cached_baseline_sha())
            snapshots.append(
                {
                    **self.snapshot,
                    "pr_number": 1000 + slot,
                    "head_sha": head,
                    "source_ref": f"refs/heads/{branch}",
                    "snapshot_fingerprint": f"cold-{slot}",
                }
            )
        git(self.source, "switch", "main")
        cache = common.acquisition_cache_path(pool.store)
        if cache.exists():
            shutil.rmtree(cache)

        context = multiprocessing.get_context("fork")
        gate = context.Barrier(5)
        manager = context.Manager()
        entered = manager.list([0] * 5)
        exited = manager.list([0] * 5)
        self.addCleanup(manager.shutdown)
        results = context.Queue()
        environment = {
            "GIT_CONFIG_COUNT": "2",
            "GIT_CONFIG_KEY_0": f"url.{self.source.as_uri()}.insteadOf",
            "GIT_CONFIG_VALUE_0": self.module["url"],
            "GIT_CONFIG_KEY_1": "protocol.file.allow",
            "GIT_CONFIG_VALUE_1": "always",
        }
        with patch.dict(os.environ, environment):
            processes = [
                context.Process(
                    target=_five_slot_setup_worker,
                    args=(
                        self.root,
                        snapshots[slot],
                        slot,
                        gate,
                        entered,
                        exited,
                        results,
                    ),
                )
                for slot in range(5)
            ]
            for process in processes:
                process.start()
            rows = [results.get(timeout=60) for _ in processes]
            for process in processes:
                process.join(timeout=60)
                self.assertFalse(process.is_alive())
                self.assertEqual(process.exitcode, 0)
        errors = [row for row in rows if row[1] != "ok"]
        self.assertEqual(errors, [])
        values = [row[2] for row in rows]
        self.assertTrue(all(value["status"] == "ready" for value in values))
        self.assertEqual({value["head"] for value in values}, {s["head_sha"] for s in snapshots})
        self.assertEqual(len({value["common_dir"] for value in values}), 1)
        self.assertEqual(len({value["object_dir"] for value in values}), 1)
        self.assertEqual(len({value["git_dir"] for value in values}), 5)
        self.assertEqual(len({value["index"] for value in values}), 5)
        self.assertEqual(len({value["head_file"] for value in values}), 5)
        intervals = [(entered[slot], exited[slot], slot) for slot in range(5)]
        overlap_pairs = [
            (left[2], right[2])
            for index, left in enumerate(intervals)
            for right in intervals[index + 1 :]
            if max(left[0], right[0]) < min(left[1], right[1])
        ]
        self.assertTrue(overlap_pairs, intervals)

    def test_two_setup_rounds_across_five_slots_clean_same_pr_retries_and_pr_switches(self):
        first_round = []
        for slot in range(5):
            snapshot = dict(
                self.snapshot,
                pr_number=1100 + slot,
                snapshot_fingerprint=f"round-one-{slot}",
            )
            result = self.explicit_setup(snapshot, slot=slot)
            self.assertEqual(result["status"], "ready")
            first_round.append((snapshot, result))

        second_round = []
        for slot, (snapshot, first) in enumerate(first_round):
            outer = self.root / first["slot_path"]
            source = outer / self.module["path"]
            prefix = f"round-two-{slot}"
            self._dirty_all_classes(outer, source, prefix)
            retrying_same_pr = slot % 2 == 0
            requested = (
                snapshot
                if retrying_same_pr
                else dict(
                    self.snapshot,
                    pr_number=2100 + slot,
                    snapshot_fingerprint=f"round-two-switch-{slot}",
                )
            )
            result = self.explicit_setup(requested, slot=slot)
            second_round.append(result)
            self.assertEqual(result["status"], "ready")
            self.assertEqual(result["slot_number"], slot)
            self.assertEqual(result["key"], f"nova-{requested['pr_number']}")
            self.assertEqual(git(source, "rev-parse", "HEAD"), requested["head_sha"])
            self._assert_dirt_removed(outer, source, prefix)
            expected_sequence = "02.json" if retrying_same_pr else "01.json"
            self.assertTrue(result["report_paths"][0].endswith(expected_sequence))

        self.assertEqual(len({row["slot_path"] for row in second_round}), 5)
        self.assertEqual(
            {row["key"] for row in second_round},
            {"nova-1100", "nova-2101", "nova-1102", "nova-2103", "nova-1104"},
        )

    def test_same_repository_distinct_cached_slots_overlap_context_and_succeed(self):
        with self.fixture_remote(), self.state.lock():
            common.SourcePool(self.state, self.module, reserve_bytes=0).warm([0, 1])
        second = dict(
            self.snapshot,
            pr_number=43,
            snapshot_fingerprint="snapshot-43",
            title="Second fixture review",
        )
        process_context = multiprocessing.get_context("fork")
        barrier = process_context.Barrier(2)
        entered = [process_context.Event(), process_context.Event()]
        results = process_context.Queue()
        processes = [
            process_context.Process(
                target=_setup_at_context_barrier,
                args=(self.root, snapshot, slot, barrier, entered[slot], results),
            )
            for slot, snapshot in enumerate((self.snapshot, second))
        ]
        with self.fixture_remote():
            processes[0].start()
            try:
                self.assertTrue(entered[0].wait(timeout=10), "slot 0 never reached context collection")
                processes[1].start()
                self.assertTrue(entered[1].wait(timeout=10), "slot 1 never overlapped context collection")
                rows = self._collect_fixture_results(results, 2)
            finally:
                self._finish_fixture_processes(processes)
        self.assertEqual(
            {label: status for label, (status, _) in rows.items()},
            {"slot-0": "ok", "slot-1": "ok"},
            rows,
        )
        manifests = [rows[f"slot-{slot}"][1] for slot in (0, 1)]
        sources = [self.root / row["slot_path"] / self.module["path"] for row in manifests]
        self.assertEqual([git(source, "rev-parse", "HEAD") for source in sources], [self.head, self.head])
        indexes = [git(source, "rev-parse", "--path-format=absolute", "--git-path", "index") for source in sources]
        self.assertEqual(len(set(indexes)), 2)
        self.assertTrue(all(row["status"] == "ready" and len(row["artifacts"]) == 2 for row in manifests))
        self.assertTrue(set(manifests[0]["artifacts"]).isdisjoint(manifests[1]["artifacts"]))
        for row, source in zip(manifests, sources):
            for role, ref in row["retention_refs"].items():
                expected = row["baseline_sha"] if role == "baseline" else row["base_sha"] if role == "base" else row["snapshot"][f"{role}_sha"]
                self.assertEqual(git(source, "rev-parse", ref), expected)

    def test_same_pr_different_explicit_slots_have_one_durable_owner(self):
        with self.fixture_remote(), self.state.lock():
            common.SourcePool(self.state, self.module, reserve_bytes=0).warm([0, 1])
        process_context = multiprocessing.get_context("fork")
        gate = process_context.Barrier(2)
        entered = [process_context.Event(), process_context.Event()]
        results = process_context.Queue()
        processes = [
            process_context.Process(
                target=_setup_with_reservation_gate,
                args=(
                    self.root,
                    self.snapshot,
                    slot,
                    gate,
                    entered[slot],
                    results,
                    f"slot-{slot}",
                ),
            )
            for slot in (0, 1)
        ]
        with self.fixture_remote():
            for process in processes:
                process.start()
            try:
                self.assertTrue(
                    any(event.wait(timeout=10) for event in entered),
                    "the owning call never reached its post-registry reservation read",
                )
                rows = self._collect_fixture_results(results, 2)
            finally:
                self._finish_fixture_processes(processes)
        self.assertEqual(
            sum(status == "ok" for status, _ in rows.values()),
            1,
            rows,
        )
        active = [
            row
            for row in self.state.all_pool_runs()
            if row.get("key") == "nova-42"
            and row.get("status") not in ("archived", "discarded", "superseded")
        ]
        self.assertEqual(len(active), 1, active)
        self.assertEqual(active[0]["status"], "ready")
        self.assertNotIn("setup_reservation", active[0])

    def test_same_pr_automatic_selection_converges_on_one_slot(self):
        with self.fixture_remote(), self.state.lock():
            common.SourcePool(self.state, self.module, reserve_bytes=0).warm([0, 1])
        process_context = multiprocessing.get_context("fork")
        gate = process_context.Barrier(2)
        entered = [process_context.Event(), process_context.Event()]
        results = process_context.Queue()
        processes = [
            process_context.Process(
                target=_setup_with_reservation_gate,
                args=(
                    self.root,
                    self.snapshot,
                    None,
                    gate,
                    entered[index],
                    results,
                    f"auto-{index}",
                ),
            )
            for index in (0, 1)
        ]
        with self.fixture_remote():
            for process in processes:
                process.start()
            try:
                self.assertTrue(
                    any(event.wait(timeout=10) for event in entered),
                    "the automatic owner never reached its post-registry reservation read",
                )
                rows = self._collect_fixture_results(results, 2)
            finally:
                self._finish_fixture_processes(processes)
        self.assertEqual(
            sum(status == "ok" for status, _ in rows.values()),
            1,
            rows,
        )
        successful = [value for status, value in rows.values() if status == "ok"]
        self.assertEqual({value["slot_number"] for value in successful}, {0}, rows)
        active = [
            row
            for row in self.state.all_pool_runs()
            if row.get("key") == "nova-42"
            and row.get("status") not in ("archived", "discarded", "superseded")
        ]
        self.assertEqual(len(active), 1, active)
        self.assertEqual(active[0]["status"], "ready")

    def test_refresh_keeps_its_default_tip_when_exact_import_follows_unlock(self):
        git(self.source, "checkout", "-b", "feature")
        (self.source / "feature.txt").write_text("feature\n", encoding="utf-8")
        git(self.source, "add", "feature.txt")
        git(self.source, "commit", "-m", "feature")
        feature = git(self.source, "rev-parse", "HEAD")
        git(self.source, "checkout", "main")
        expected = git(self.source, "rev-parse", "HEAD")

        process_context = multiprocessing.get_context("fork")
        fetched = process_context.Event()
        release = process_context.Event()
        results = process_context.Queue()
        refresh = process_context.Process(
            target=_refresh_with_post_fetch_gate,
            args=(self.root, self.module, expected, fetched, release, results),
        )
        exact_import = process_context.Process(
            target=_import_exact_after_refresh,
            args=(self.root, self.module, feature, fetched, release, results),
        )
        processes = [refresh, exact_import]
        with self.fixture_remote():
            for process in processes:
                process.start()
            try:
                rows = self._collect_fixture_results(results, 2)
            finally:
                self._finish_fixture_processes(processes, (release,))
        self.assertEqual(rows["import"], ("ok", feature), rows)
        self.assertEqual(rows["refresh"][0], "ok", rows)
        self.assertEqual(rows["refresh"][1]["actual"], expected, rows)
        self.assertEqual(rows["refresh"][1]["cached"], expected, rows)

    def test_same_slot_waits_without_concurrent_cleanup_or_mutation(self):
        self.explicit_setup()
        outer = self.root / ".worktrees/Nova-0"
        marker = outer / "owned-marker.txt"
        marker.write_text("remove only under slot ownership", encoding="utf-8")
        process_context = multiprocessing.get_context("fork")
        owner_entered = process_context.Event()
        release_owner = process_context.Event()
        contender_started = process_context.Event()
        contender_cleanup = process_context.Event()
        results = process_context.Queue()
        owner = process_context.Process(
            target=_setup_blocked_in_context,
            args=(self.root, self.snapshot, 0, owner_entered, release_owner, results),
        )
        contender = process_context.Process(
            target=_setup_observing_cleanup,
            args=(self.root, self.snapshot, 0, contender_started, contender_cleanup, results),
        )
        processes = [owner, contender]
        with self.fixture_remote():
            owner.start()
            try:
                self.assertTrue(owner_entered.wait(timeout=10), "owner never reached context collection")
                self.assertFalse(marker.exists(), "owner must perform its own cleanup before context")
                contender.start()
                self.assertTrue(contender_started.wait(timeout=10), "contender never started")
                self.assertFalse(
                    contender_cleanup.wait(timeout=1),
                    "same-slot contender entered cleanup while the owner was still active",
                )
                release_owner.set()
                rows = self._collect_fixture_results(results, 2)
            finally:
                self._finish_fixture_processes(processes, (release_owner,))
        self.assertEqual(
            {label: status for label, (status, _) in rows.items()},
            {"owner": "ok", "contender": "ok"},
            rows,
        )
        self.assertTrue(contender_cleanup.is_set(), "waiting contender never reached cleanup after release")
        final = rows["contender"][1]
        self.assertEqual(final["status"], "ready")
        self.assertTrue(final["report_paths"][0].endswith("PR_Setup_03.json"))
        self.assertEqual(git(self.root / final["slot_path"] / self.module["path"], "rev-parse", "HEAD"), self.head)

    def test_actual_setup_cleanup_child_survives_owner_kill_then_explicit_recovery(self):
        self.explicit_setup()
        outer = self.root / ".worktrees/Nova-0"
        source = outer / self.module["path"]
        (outer / "cleanup-note").write_text("cleanup fixture", encoding="utf-8")
        (source / "file.txt").write_text("cleanup fixture\n", encoding="utf-8")

        child, _, _, descriptors = self._kill_actual_entrypoint_at_git(
            "setup", slot=0
        )
        with self.assertRaisesRegex(common.ReviewError, "still executable"):
            state = common.ReviewState(self.root)
            pool = common.SourcePool(state, self.module, reserve_bytes=0)
            with (
                common.lock_session(5),
                state.lock(shared=True),
                state.slot_lock(pool.repo_key, 0),
                common.slot_writer_scope(
                    state, pool.repo_key, 0, phase="retry"
                ),
            ):
                pass
        before_outer = (outer / "cleanup-note").read_text(encoding="utf-8")
        before_source = (source / "file.txt").read_text(encoding="utf-8")
        with self.assertRaisesRegex(common.ReviewError, "still executable"):
            self.explicit_setup()
        self.assertEqual(
            (outer / "cleanup-note").read_text(encoding="utf-8"), before_outer
        )
        self.assertEqual(
            (source / "file.txt").read_text(encoding="utf-8"), before_source
        )

        self._retire_killed_fixture(child, descriptors)
        inspection = self._slot_recovery_inspection(0)
        recovered = self._recover_slot_from_inspection(inspection, 0)
        self.assertFalse(recovered["pending"])
        self.assertTrue(Path(recovered["recovery_audit"]).is_file())
        result = self.explicit_setup()
        self.assertEqual(result["status"], "ready")
        self.assertFalse((outer / "cleanup-note").exists())
        self.assertEqual((source / "file.txt").read_text(), "head\n")

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

    def test_cleaned_retry_discards_staged_unstaged_untracked_and_ignored_content(self):
        first = self.explicit_setup()
        outer = self.root / first["slot_path"]
        source = outer / self.module["path"]
        self._interrupt_at_cleaned()
        self._dirty_all_classes(outer, source, "cleaned-retry")

        import review_setup
        observed_phases = []
        real_cleanup = review_setup.cleanup_worktree

        def observe_intent(repo, *args, **kwargs):
            observed_phases.append(self._cleanup_journal()[1]["phase"])
            return real_cleanup(repo, *args, **kwargs)

        with patch.object(review_setup, "cleanup_worktree", side_effect=observe_intent):
            retried = self.explicit_setup()

        self.assertEqual(retried["status"], "ready")
        self.assertEqual(observed_phases, ["cleaning", "cleaning"])
        self._assert_dirt_removed(outer, source, "cleaned-retry")
        self.assertEqual((outer / ".gitmodules").read_text(), (self.root / ".gitmodules").read_text())
        self.assertEqual((source / "file.txt").read_text(), "head\n")
        self.assertEqual(self._cleanup_journal()[1]["phase"], "done")

    def test_cleaned_retry_discards_ignored_only_content(self):
        first = self.explicit_setup()
        outer = self.root / first["slot_path"]
        source = outer / self.module["path"]
        self._interrupt_at_cleaned()
        for repo, name in ((outer, "outer-cleaned.tmp"), (source, "source-cleaned.tmp")):
            self._ignore_in(repo, name)
            (repo / name).write_text("discard", encoding="utf-8")
            self.assertIn(f"!! {name}", git(repo, "status", "--porcelain", "--ignored"))

        retried = self.explicit_setup()

        self.assertEqual(retried["status"], "ready")
        self.assertFalse((outer / "outer-cleaned.tmp").exists())
        self.assertFalse((source / "source-cleaned.tmp").exists())

    def test_ignored_write_immediately_after_cleanup_refuses_then_rerun_cleans(self):
        first = self.explicit_setup()
        outer = self.root / first["slot_path"]
        source = outer / self.module["path"]
        self._ignore_in(outer, "outer-after-cleanup.tmp")
        self._ignore_in(source, "source-after-cleanup.tmp")
        import review_setup

        real_cleanup = review_setup.cleanup_worktree
        cleanups = 0

        def write_after_outer_cleanup(repo, *args, **kwargs):
            nonlocal cleanups
            result = real_cleanup(repo, *args, **kwargs)
            cleanups += 1
            if repo == outer:
                (outer / "outer-after-cleanup.tmp").write_text("late", encoding="utf-8")
                (source / "source-after-cleanup.tmp").write_text("late", encoding="utf-8")
            return result

        with patch.object(review_setup, "cleanup_worktree", side_effect=write_after_outer_cleanup):
            with self.assertRaisesRegex(common.ReviewError, "may have been written after cleanup.*rerun"):
                self.explicit_setup()
        self.assertEqual(cleanups, 2)
        self.assertTrue((outer / "outer-after-cleanup.tmp").exists())
        self.assertTrue((source / "source-after-cleanup.tmp").exists())

        retried = self.explicit_setup()
        self.assertEqual(retried["status"], "ready")
        self.assertFalse((outer / "outer-after-cleanup.tmp").exists())
        self.assertFalse((source / "source-after-cleanup.tmp").exists())

    def test_ignored_write_during_collection_refuses_final_ready_validation(self):
        first = self.explicit_setup()
        outer = self.root / first["slot_path"]
        source = outer / self.module["path"]
        self._ignore_in(outer, "outer-during-collection.tmp")
        self._ignore_in(source, "source-during-collection.tmp")

        def collect(*args, **kwargs):
            (outer / "outer-during-collection.tmp").write_text("late", encoding="utf-8")
            (source / "source-during-collection.tmp").write_text("late", encoding="utf-8")
            return _empty_report()

        with self.assertRaisesRegex(common.ReviewError, "may have been written after cleanup.*rerun"):
            self.explicit_setup(collect=collect)
        persisted = self.state.load_pool_run("repos-nova", 0)
        self.assertNotEqual(persisted["status"], "ready")
        self.assertTrue((outer / "outer-during-collection.tmp").exists())
        self.assertTrue((source / "source-during-collection.tmp").exists())

        retried = self.explicit_setup()
        self.assertEqual(retried["status"], "ready")
        self.assertFalse((outer / "outer-during-collection.tmp").exists())
        self.assertFalse((source / "source-during-collection.tmp").exists())

    def test_prepared_retry_with_done_journal_starts_fresh_cleanup(self):
        first = self.explicit_setup()
        outer = self.root / first["slot_path"]
        source = outer / self.module["path"]
        self.explicit_setup()
        previous_id = self._cleanup_journal()[1]["id"]
        prepared = self.state.load_pool_run("repos-nova", 0)
        prepared["status"] = "prepared"
        prepared["phase"] = "prepared"
        self.state.save_pool_run(prepared)
        self._dirty_all_classes(outer, source, "prepared-retry")

        retried = self.explicit_setup()

        self.assertEqual(retried["status"], "ready")
        self._assert_dirt_removed(outer, source, "prepared-retry")
        self.assertNotEqual(self._cleanup_journal()[1]["id"], previous_id)

    def test_repeated_ready_setups_create_cleanup_generations_and_discard_all_content(self):
        current = self.explicit_setup()
        outer = self.root / current["slot_path"]
        source = outer / self.module["path"]
        generation_ids = []

        for round_number in (2, 3):
            prefix = f"ready-round-{round_number}"
            self._dirty_all_classes(outer, source, prefix)
            current = self.explicit_setup()
            self.assertEqual(current["status"], "ready")
            self.assertTrue(current["report_paths"][0].endswith(f"{round_number:02d}.json"))
            self._assert_dirt_removed(outer, source, prefix)
            generation_ids.append(self._cleanup_journal()[1]["id"])

        self.assertEqual(len(set(generation_ids)), 2)

    def test_ignored_generated_artifacts_stay_allowed_but_ignored_siblings_refuse(self):
        first = self.explicit_setup()
        outer = self.root / first["slot_path"]
        source = outer / self.module["path"]
        generated = first["report_paths"][0]
        self._ignore_in(outer, "PRs/")
        import review_setup

        review_setup.validate_pool_slot(
            self.state, first, require_pinned=True, require_retention=True
        )
        unexpected = f"{first['pr_dir']}/unexpected.tmp"
        (outer / unexpected).write_text("unexpected", encoding="utf-8")
        with self.assertRaisesRegex(common.ReviewError, "may have been written after cleanup.*rerun"):
            review_setup.validate_pool_slot(
                self.state, first, require_pinned=True, require_retention=True
            )
        self.assertTrue((outer / generated).is_file())
        self.assertTrue((outer / unexpected).is_file())

    def test_managed_source_exclusion_does_not_hide_ignored_source_content(self):
        first = self.explicit_setup()
        outer = self.root / first["slot_path"]
        source = outer / self.module["path"]
        self._ignore_in(outer, "repos/")
        self._ignore_in(source, "source-unexpected.tmp")
        (source / "source-unexpected.tmp").write_text("unexpected", encoding="utf-8")
        import review_setup

        with self.assertRaisesRegex(common.ReviewError, "may have been written after cleanup.*rerun"):
            review_setup.validate_pool_slot(
                self.state, first, require_pinned=True, require_retention=True
            )

    def test_ignore_rule_changes_cannot_hide_existing_untracked_content(self):
        first = self.explicit_setup()
        outer = self.root / first["slot_path"]
        import review_setup

        hidden = outer / "becomes-ignored.tmp"
        hidden.write_text("unexpected", encoding="utf-8")
        self._ignore_in(outer, hidden.name)
        self.assertNotIn(hidden.name, git(outer, "status", "--porcelain"))
        with self.assertRaisesRegex(common.ReviewError, "may have been written after cleanup.*rerun"):
            review_setup.validate_pool_slot(
                self.state, first, require_pinned=True, require_retention=True
            )

    def test_attached_source_requires_exact_reclaim_generation_then_preserves_all_state(self):
        import review_setup

        first = self.explicit_setup()
        outer = self.root / first["slot_path"]
        source = outer / self.module["path"]
        git(source, "switch", "-c", "implementation/nova-42")
        git(source, "config", "user.name", "Lifecycle Test")
        git(source, "config", "user.email", "lifecycle@example.invalid")
        (source / "implementation.txt").write_text("committed\n", encoding="utf-8")
        git(source, "add", "implementation.txt")
        git(source, "commit", "-m", "implementation work")
        implementation_tip = git(source, "rev-parse", "HEAD")

        (source / "file.txt").write_text("staged\n", encoding="utf-8")
        git(source, "add", "file.txt")
        staged_tree = git(source, "write-tree")
        (source / "file.txt").write_bytes(b"unstaged\x00binary\n")
        (source / "untracked.bin").write_bytes(bytes(range(256)))
        executable = source / "run-local"
        executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        executable.chmod(0o755)
        self._ignore_in(source, "ignored.local")
        (source / "ignored.local").write_text("ignored\n", encoding="utf-8")
        outside = Path(self.temp.name) / "outside-reclaim"
        outside.write_text("outside remains\n", encoding="utf-8")
        (source / "outside-link").symlink_to(outside)
        before = git(source, "status", "--porcelain=v1", "--ignored")

        inspection = review_setup.inspect_reclaim_generation(
            self.root, self.snapshot, self.module, 0
        )
        self.assertEqual(inspection["source_head"], implementation_tip)
        self.assertEqual(inspection["source_ref"], "implementation/nova-42")
        self.assertEqual(inspection["source_index_tree"], staged_tree)
        with self.assertRaisesRegex(common.ReviewError, "reclaim generation"):
            self.explicit_setup()
        self.assertEqual(git(source, "status", "--porcelain=v1", "--ignored"), before)
        with self.assertRaisesRegex(common.ReviewError, "reclaim generation"):
            self.explicit_setup(reclaim_generation="0" * 64)
        self.assertEqual(git(source, "status", "--porcelain=v1", "--ignored"), before)

        result = self.explicit_setup(
            reclaim_generation=inspection["reclaim_generation"]
        )

        self.assertEqual(result["status"], "ready")
        journal = self._cleanup_journal()[1]
        self.assertEqual(journal["reclaim_generation"], inspection["reclaim_generation"])
        preservation = Path(journal["preservation"]["path"])
        self.assertTrue(preservation.is_dir())
        archive_record = json.loads(
            (preservation / "manifest.json").read_text(encoding="utf-8")
        )
        source_record = archive_record["roots"]["source"]
        self.assertEqual(source_record["head"], implementation_tip)
        self.assertEqual(source_record["ref"], "implementation/nova-42")
        self.assertEqual(source_record["index_tree"], staged_tree)
        entries = {entry["path"]: entry for entry in source_record["entries"]}
        self.assertEqual(entries["file.txt"]["sha256"], hashlib.sha256(b"unstaged\x00binary\n").hexdigest())
        self.assertEqual(entries["untracked.bin"]["sha256"], hashlib.sha256(bytes(range(256))).hexdigest())
        self.assertEqual(entries["run-local"]["mode"], 0o755)
        self.assertEqual(entries["outside-link"]["type"], "symlink")
        self.assertEqual(entries["outside-link"]["target"], str(outside))
        self.assertEqual(outside.read_text(encoding="utf-8"), "outside remains\n")
        self.assertEqual(git(source, "rev-parse", "implementation/nova-42"), implementation_tip)
        self.assertIsNone(common.current_ref(source))

    def test_reclaim_generation_stales_when_preservation_input_changes(self):
        import review_setup

        first = self.explicit_setup()
        source = self.root / first["slot_path"] / self.module["path"]
        git(source, "switch", "-c", "implementation/nova-42")
        (source / "local.txt").write_text("first\n", encoding="utf-8")
        inspection = review_setup.inspect_reclaim_generation(
            self.root, self.snapshot, self.module, 0
        )
        (source / "local.txt").write_text("changed\n", encoding="utf-8")

        with self.assertRaisesRegex(common.ReviewError, "reclaim generation"):
            self.explicit_setup(
                reclaim_generation=inspection["reclaim_generation"]
            )
        self.assertEqual((source / "local.txt").read_text(encoding="utf-8"), "changed\n")
        self.assertEqual(common.current_ref(source), "implementation/nova-42")

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

    def test_explicit_switch_preserves_authorized_attached_implementation_generation(self):
        first = self.explicit_setup()
        outer = self.root / first["slot_path"]
        source = outer / self.module["path"]
        git(source, "switch", "-c", "implementation/nova-42")
        git(source, "config", "user.name", "Lifecycle Test")
        git(source, "config", "user.email", "lifecycle@example.invalid")
        (source / "implementation.bin").write_bytes(b"\x00implementation\xff")
        git(source, "add", "implementation.bin")
        git(source, "commit", "-m", "implementation checkpoint")
        implementation_tip = git(source, "rev-parse", "HEAD")
        (source / "file.txt").write_text("staged implementation\n", encoding="utf-8")
        git(source, "add", "file.txt")
        (source / "file.txt").write_text("unstaged implementation\n", encoding="utf-8")
        (source / "restore-untracked.bin").write_bytes(b"\x00untracked\xfe")
        self._ignore_in(source, "restore-ignored.sh")
        ignored = source / "restore-ignored.sh"
        ignored.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        ignored.chmod(0o751)
        (source / "restore-link").symlink_to("restore-untracked.bin")
        self._authorize_reclaim(first, outer, source)

        requested = dict(self.snapshot, pr_number=43, snapshot_fingerprint="pr43")
        result = self.explicit_setup(requested)

        self.assertEqual(result["key"], "nova-43")
        self.assertEqual(
            git(source, "rev-parse", "refs/heads/implementation/nova-42"),
            implementation_tip,
        )
        history = sorted(
            (self.state.directory / "preservations" / first["repo_key"] / "0").glob(
                "*/manifest.json"
            )
        )
        self.assertEqual(len(history), 1)
        preservation = json.loads(history[0].read_text(encoding="utf-8"))
        source_record = preservation["checkouts"]["source"]
        self.assertEqual(source_record["head"], implementation_tip)
        self.assertEqual(source_record["ref"], "implementation/nova-42")
        entries = {entry["path"]: entry for entry in source_record["entries"]}
        self.assertEqual(entries["file.txt"]["index_sha256"], hashlib.sha256(b"staged implementation\n").hexdigest())
        self.assertEqual(entries["file.txt"]["worktree_sha256"], hashlib.sha256(b"unstaged implementation\n").hexdigest())
        self.assertEqual(entries["restore-untracked.bin"]["worktree_sha256"], hashlib.sha256(b"\x00untracked\xfe").hexdigest())
        self.assertEqual(entries["restore-ignored.sh"]["mode"], 0o751)
        self.assertEqual(entries["restore-link"]["symlink_target"], "restore-untracked.bin")
        for entry in entries.values():
            for payload_key in ("index_payload", "worktree_payload"):
                if entry.get(payload_key):
                    payload = history[0].parent / entry[payload_key]
                    self.assertEqual(common.file_hash(payload), entry[payload_key.replace("payload", "sha256")])

    def _interrupt_setup_after_preserved(self, *, staged=False):
        first = self.explicit_setup()
        outer = self.root / first["slot_path"]
        source = outer / self.module["path"]
        payload = source / ("file.txt" if staged else "resume.bin")
        if staged:
            payload.write_bytes(b"staged-preservation")
            git(source, "add", payload.name)
            payload.write_bytes(b"worktree-preservation")
        else:
            payload.write_bytes(b"resume-preservation")
        requested = dict(self.snapshot, pr_number=43, snapshot_fingerprint="pr43")
        import review_setup

        original = review_setup.atomic_json
        interrupted = False

        def interrupt_after_preserved(path, value):
            nonlocal interrupted
            original(path, value)
            if (
                Path(path).parent.name == "cleanup"
                and not Path(path).name.startswith("reset-")
                and value.get("phase") == "preserved"
                and not interrupted
            ):
                interrupted = True
                raise common.ReviewError("fixture after durable preservation")

        with patch.object(
            review_setup, "atomic_json", side_effect=interrupt_after_preserved
        ), self.assertRaisesRegex(common.ReviewError, "durable preservation"):
            self.explicit_setup(requested)
        journal_path, journal = self._cleanup_journal()
        self.assertEqual(journal["phase"], "preserved")
        archive = Path(journal["preservation_path"])
        self.assertTrue(payload.exists())
        return first, outer, source, payload, requested, journal_path, journal, archive

    def test_setup_preserved_archive_refuses_missing_index_payload_before_cleanup(self):
        (
            first,
            outer,
            source,
            payload,
            requested,
            _,
            _,
            archive,
        ) = self._interrupt_setup_after_preserved(staged=True)
        record = json.loads((archive / "manifest.json").read_text(encoding="utf-8"))
        entry = next(
            item
            for item in record["checkouts"]["source"]["entries"]
            if item["path"] == payload.name
        )
        index_payload = archive / entry["index_payload"]
        index_payload.unlink()
        before_worktree = payload.read_bytes()
        before_index = git(source, "show", f":{payload.name}")

        with self.assertRaisesRegex(common.ReviewError, "Preservation archive"):
            self.explicit_setup(requested)

        self.assertFalse(index_payload.exists())
        self.assertEqual(payload.read_bytes(), before_worktree)
        self.assertEqual(git(source, "show", f":{payload.name}"), before_index)
        self.assertEqual(common.current_ref(outer), first["branch"])

    def test_setup_preserved_archive_refuses_corrupt_worktree_payload_before_cleanup(self):
        (
            first,
            outer,
            _,
            payload,
            requested,
            _,
            _,
            archive,
        ) = self._interrupt_setup_after_preserved()
        record = json.loads((archive / "manifest.json").read_text(encoding="utf-8"))
        entry = next(
            item
            for item in record["checkouts"]["source"]["entries"]
            if item["path"] == payload.name
        )
        archived_payload = archive / entry["worktree_payload"]
        archived_payload.write_bytes(b"corrupt-preservation")
        before = payload.read_bytes()

        with self.assertRaisesRegex(common.ReviewError, "Preservation archive"):
            self.explicit_setup(requested)

        self.assertEqual(archived_payload.read_bytes(), b"corrupt-preservation")
        self.assertEqual(payload.read_bytes(), before)
        self.assertEqual(common.current_ref(outer), first["branch"])

    def test_setup_preserved_archive_refuses_symlink_payload_before_cleanup(self):
        (
            first,
            outer,
            _,
            payload,
            requested,
            _,
            _,
            archive,
        ) = self._interrupt_setup_after_preserved()
        record = json.loads((archive / "manifest.json").read_text(encoding="utf-8"))
        entry = next(
            item
            for item in record["checkouts"]["source"]["entries"]
            if item["path"] == payload.name
        )
        archived_payload = archive / entry["worktree_payload"]
        outside = Path(self.temp.name) / "outside-archive-payload"
        outside.write_bytes(archived_payload.read_bytes())
        archived_payload.unlink()
        archived_payload.symlink_to(outside)
        before = payload.read_bytes()

        with self.assertRaisesRegex(common.ReviewError, "Preservation archive"):
            self.explicit_setup(requested)

        self.assertTrue(archived_payload.is_symlink())
        self.assertEqual(payload.read_bytes(), before)
        self.assertEqual(common.current_ref(outer), first["branch"])

    def test_setup_preserved_archive_refuses_tampered_manifest_before_cleanup(self):
        (
            first,
            outer,
            _,
            payload,
            requested,
            _,
            journal,
            archive,
        ) = self._interrupt_setup_after_preserved()
        manifest_path = archive / "manifest.json"
        record = json.loads(manifest_path.read_text(encoding="utf-8"))
        record["binding"]["source"]["entries"][0]["path"] = "tampered-path"
        manifest_path.write_text(
            json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        self.assertNotEqual(
            common.file_hash(manifest_path), journal["preservation"]["manifest_sha256"]
        )
        before = payload.read_bytes()

        with self.assertRaisesRegex(common.ReviewError, "Preservation archive"):
            self.explicit_setup(requested)

        self.assertEqual(payload.read_bytes(), before)
        self.assertEqual(common.current_ref(outer), first["branch"])

    def test_preservation_crash_resumes_same_archive_before_cleanup(self):
        first = self.explicit_setup()
        outer = self.root / first["slot_path"]
        source = outer / self.module["path"]
        (source / "resume.bin").write_bytes(b"resume-preservation")
        self._authorize_reclaim(first, outer, source)
        requested = dict(self.snapshot, pr_number=43, snapshot_fingerprint="pr43")
        import review_setup

        original = review_setup.atomic_json
        interrupted = False

        def interrupt_after_preserved(path, value):
            nonlocal interrupted
            original(path, value)
            if (
                Path(path).parent.name == "cleanup"
                and not Path(path).name.startswith("reset-")
                and value.get("phase") == "preserved"
                and not interrupted
            ):
                interrupted = True
                raise common.ReviewError("fixture after durable preservation")

        with patch.object(
            review_setup, "atomic_json", side_effect=interrupt_after_preserved
        ), self.assertRaisesRegex(common.ReviewError, "durable preservation"):
            self.explicit_setup(requested)
        journal = self._cleanup_journal()[1]
        self.assertEqual(journal["phase"], "preserved")
        archive = Path(journal["preservation_path"])
        archive_hash = common.file_hash(archive / "manifest.json")
        self.assertTrue((source / "resume.bin").exists())

        result = self.explicit_setup(requested)

        self.assertEqual(result["status"], "ready")
        self.assertEqual(common.file_hash(archive / "manifest.json"), archive_hash)
        self.assertEqual(
            len(list((self.state.directory / "preservations" / first["repo_key"] / "0").glob("*/manifest.json"))),
            1,
        )

    def test_preservation_refuses_changed_capture_without_cleanup(self):
        first = self.explicit_setup()
        outer = self.root / first["slot_path"]
        source = outer / self.module["path"]
        changing = source / "changing.txt"
        changing.write_text("before\n", encoding="utf-8")
        self._authorize_reclaim(first, outer, source)
        requested = dict(self.snapshot, pr_number=43, snapshot_fingerprint="pr43")
        import review_setup

        original = review_setup.atomic_json
        changed = False

        def mutate_after_intent(path, value):
            nonlocal changed
            original(path, value)
            if (
                Path(path).parent.name == "cleanup"
                and value.get("phase") == "preserving"
                and not changed
            ):
                changed = True
                changing.write_text("after\n", encoding="utf-8")

        with patch.object(
            review_setup, "atomic_json", side_effect=mutate_after_intent
        ), self.assertRaisesRegex(common.ReviewError, "changed before preservation"):
            self.explicit_setup(requested)
        self.assertEqual(changing.read_text(encoding="utf-8"), "after\n")
        self.assertEqual(common.current_ref(source), None)
        self.assertEqual(common.current_ref(outer), first["branch"])

    def test_recorded_reclaim_authorization_stales_when_local_work_is_added(self):
        first = self.explicit_setup()
        outer = self.root / first["slot_path"]
        source = outer / self.module["path"]
        git(source, "switch", "-c", "implementation/nova-42")
        self._authorize_reclaim(first, outer, source)
        late = source / "post-authorization.txt"
        late.write_text("late work\n", encoding="utf-8")
        requested = dict(self.snapshot, pr_number=43, snapshot_fingerprint="pr43")

        with self.assertRaisesRegex(common.ReviewError, "reclaim authorization"):
            self.explicit_setup(requested)

        self.assertEqual(late.read_text(encoding="utf-8"), "late work\n")
        self.assertEqual(common.current_ref(source), "implementation/nova-42")
        self.assertEqual(common.current_ref(outer), first["branch"])

    def test_recorded_reclaim_authorization_stales_when_local_work_is_modified(self):
        first = self.explicit_setup()
        outer = self.root / first["slot_path"]
        source = outer / self.module["path"]
        git(source, "switch", "-c", "implementation/nova-42")
        local = source / "authorized-local.txt"
        local.write_text("authorized bytes\n", encoding="utf-8")
        self._authorize_reclaim(first, outer, source)
        local.write_text("changed after authorization\n", encoding="utf-8")
        requested = dict(self.snapshot, pr_number=43, snapshot_fingerprint="pr43")

        with self.assertRaisesRegex(common.ReviewError, "reclaim authorization"):
            self.explicit_setup(requested)

        self.assertEqual(
            local.read_text(encoding="utf-8"), "changed after authorization\n"
        )
        self.assertEqual(common.current_ref(source), "implementation/nova-42")
        self.assertEqual(common.current_ref(outer), first["branch"])

    def test_attached_source_wrong_generation_refuses_and_retires_new_reservation(self):
        first = self.explicit_setup()
        outer = self.root / first["slot_path"]
        source = outer / self.module["path"]
        git(source, "switch", "-c", "implementation/nova-42")
        first = self._authorize_reclaim(first, outer, source)
        first["reclaim_authorization"]["source_head"] = "0" * 40
        self.state.save_pool_run(first)
        requested = dict(self.snapshot, pr_number=43, snapshot_fingerprint="pr43")

        with self.assertRaisesRegex(common.ReviewError, "reclaim authorization"):
            self.explicit_setup(requested)

        self.assertIsNone(
            self.state.load_setup_reservation(first["repo_key"], "nova-43")
        )
        current = self.state.load_pool_run(first["repo_key"], 0)
        self.assertNotIn("setup_reservation", current)
        self.assertEqual(common.current_ref(source), "implementation/nova-42")

    def test_preservation_refuses_size_and_headroom_limits_without_cleanup(self):
        first = self.explicit_setup()
        outer = self.root / first["slot_path"]
        source = outer / self.module["path"]
        payload = source / "bounded.bin"
        payload.write_bytes(b"bounded")
        self._authorize_reclaim(first, outer, source)
        requested = dict(
            self.snapshot, pr_number=43, snapshot_fingerprint="pr43"
        )

        with patch.object(
            common, "PRESERVATION_MAX_BYTES", 1
        ), self.assertRaisesRegex(common.ReviewError, "bounded archive size"):
            self.explicit_setup(requested)
        self.assertEqual(payload.read_bytes(), b"bounded")
        self.assertEqual(common.current_ref(outer), first["branch"])

        free = common.PRESERVATION_HEADROOM_BYTES - 1
        usage = shutil.disk_usage(self.state.directory)
        constrained = type(usage)(usage.total, usage.used, free)
        with patch.object(
            common.shutil, "disk_usage", return_value=constrained
        ), self.assertRaisesRegex(common.ReviewError, "disk headroom"):
            self.explicit_setup(requested)
        self.assertEqual(payload.read_bytes(), b"bounded")
        self.assertEqual(common.current_ref(outer), first["branch"])

    def test_explicit_cleanup_refuses_special_file_before_preservation(self):
        first = self.explicit_setup()
        outer = self.root / first["slot_path"]
        source = outer / self.module["path"]
        fifo = source / "unsupported.fifo"
        os.mkfifo(fifo)
        self.addCleanup(lambda: fifo.unlink(missing_ok=True))
        requested = dict(self.snapshot, pr_number=43, snapshot_fingerprint="pr43")

        with self.assertRaisesRegex(common.ReviewError, "special file"):
            self.explicit_setup(requested)

        self.assertTrue(fifo.exists())
        self.assertEqual(common.current_ref(outer), first["branch"])

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
        # Reentrant same-process flock acquisition is not a contention proof;
        # process-level exclusion is covered by ScopedLockTests.
        note.unlink()
        marker = Path(git(outer, "rev-parse", "--path-format=absolute", "--git-path", "MERGE_HEAD"))
        marker.write_text(self.head)
        with self.assertRaises(common.ReviewError):
            self.explicit_setup()

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

    def test_switching_retry_before_outer_checkout_recleans_both_checkouts(self):
        from unittest.mock import patch
        import review_setup

        first = self.explicit_setup()
        outer = self.root / first["slot_path"]
        source = outer / self.module["path"]
        git(outer, "add", "PRs")
        git(outer, "commit", "-m", "saved old review")
        old_tip = git(outer, "rev-parse", "HEAD")
        new = dict(self.snapshot, pr_number=43, snapshot_fingerprint="pr43")
        real_run_git = review_setup.run_git
        interrupted = False

        def interrupt_before_outer_checkout(repo, args, **kwargs):
            nonlocal interrupted
            if (
                not interrupted
                and repo == outer
                and args[:4]
                == ["-c", "submodule.recurse=false", "checkout", "--detach"]
            ):
                interrupted = True
                raise common.ReviewError("fixture interruption before outer checkout")
            return real_run_git(repo, args, **kwargs)

        with patch.object(
            review_setup, "run_git", side_effect=interrupt_before_outer_checkout
        ), self.assertRaisesRegex(common.ReviewError, "before outer checkout"):
            self.explicit_setup(new)
        self.assertEqual(self._cleanup_journal()[1]["phase"], "switching")
        self._dirty_all_classes(outer, source, "switching-before-outer")
        observed_phases = []
        real_cleanup = review_setup.cleanup_worktree

        def observe_retry_cleanup(repo, *args, **kwargs):
            observed_phases.append(self._cleanup_journal()[1]["phase"])
            return real_cleanup(repo, *args, **kwargs)

        with patch.object(
            review_setup, "cleanup_worktree", side_effect=observe_retry_cleanup
        ):
            result = self.explicit_setup(new)

        self.assertEqual(result["key"], "nova-43")
        self.assertEqual(observed_phases, ["switching", "switching"])
        self._assert_dirt_removed(outer, source, "switching-before-outer")
        self.assertEqual(git(self.root, "rev-parse", first["branch"]), old_tip)

    def test_switching_retry_after_outer_checkout_keeps_phase_during_recleanup(self):
        from unittest.mock import patch
        import review_setup

        first = self.explicit_setup()
        outer = self.root / first["slot_path"]
        source = outer / self.module["path"]
        git(outer, "add", "PRs")
        git(outer, "commit", "-m", "saved old review")
        old_tip = git(outer, "rev-parse", "HEAD")
        new = dict(self.snapshot, pr_number=43, snapshot_fingerprint="pr43")
        real_run_git = review_setup.run_git
        interrupted = False

        def interrupt_before_source_checkout(repo, args, **kwargs):
            nonlocal interrupted
            if (
                not interrupted
                and repo == source
                and args[:2] == ["checkout", "--detach"]
            ):
                interrupted = True
                raise common.ReviewError("fixture interruption before source checkout")
            return real_run_git(repo, args, **kwargs)

        with patch.object(
            review_setup, "run_git", side_effect=interrupt_before_source_checkout
        ), self.assertRaisesRegex(common.ReviewError, "before source checkout"):
            self.explicit_setup(new)
        journal = self._cleanup_journal()[1]
        self.assertEqual(journal["phase"], "switching")
        self.assertEqual(git(outer, "rev-parse", "HEAD"), journal["root_head"])
        self.assertEqual(git(source, "rev-parse", "HEAD"), journal["source_head"])
        self._dirty_all_classes(outer, source, "switching-after-outer")
        real_cleanup = review_setup.cleanup_worktree
        retry_interrupted = False

        def interrupt_during_retry_cleanup(repo, *args, **kwargs):
            nonlocal retry_interrupted
            if repo == outer and not retry_interrupted:
                retry_interrupted = True
                raise common.ReviewError("fixture interruption during switching cleanup")
            return real_cleanup(repo, *args, **kwargs)

        with patch.object(
            review_setup,
            "cleanup_worktree",
            side_effect=interrupt_during_retry_cleanup,
        ), self.assertRaisesRegex(common.ReviewError, "during switching cleanup"):
            self.explicit_setup(new)
        self.assertEqual(self._cleanup_journal()[1]["phase"], "switching")
        self.assertFalse((source / "switching-after-outer-untracked.txt").exists())
        self.assertTrue((outer / "switching-after-outer-untracked.txt").exists())

        result = self.explicit_setup(new)

        self.assertEqual(result["key"], "nova-43")
        self._assert_dirt_removed(outer, source, "switching-after-outer")
        self.assertEqual(git(self.root, "rev-parse", first["branch"]), old_tip)

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
            if path.is_file()
            and not path.is_symlink()
            and "/review-setup/logs/" not in f"/{path.relative_to(self.root).as_posix()}"
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
            if path.is_file()
            and not path.is_symlink()
            and "/review-setup/logs/" not in f"/{path.relative_to(self.root).as_posix()}"
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

    def test_mixed_case_new_pool_run_uses_canonical_artifact_path_only(self):
        import review_setup

        mixed_module = {
            "name": "repos/WeveNova",
            "path": "repos/WeveNova",
            "url": "https://dev.azure.com/org/project/_git/WeveNova",
        }
        gitmodules = self.root / ".gitmodules"
        gitmodules.write_text(
            gitmodules.read_text(encoding="utf-8")
            + '[submodule "repos/WeveNova"]\n path = repos/WeveNova\n'
            + f' url = {mixed_module["url"]}\n',
            encoding="utf-8",
        )
        lowercase_history = self.root / "PRs/wevenova-5631145/reviewer.md"
        lowercase_history.parent.mkdir(parents=True)
        lowercase_history.write_text("preserve lowercase history\n", encoding="utf-8")
        git(self.root, "add", ".gitmodules", "PRs/wevenova-5631145/reviewer.md")
        git(
            self.root,
            "update-index",
            "--add",
            "--cacheinfo",
            "160000",
            self.base,
            mixed_module["path"],
        )
        git(self.root, "commit", "-m", "add mixed-case submodule")
        self.store_base = git(self.root, "rev-parse", "HEAD")
        snapshot = {
            **self.snapshot,
            "pr_number": 5631145,
            "snapshot_fingerprint": "mixed-case-snapshot",
            "repository": {
                **self.snapshot["repository"],
                "name": "WeveNova",
            },
        }
        client = type("Client", (), {"org_url": "https://dev.azure.com/org"})()
        report = {
            "complete": True,
            "warnings": [],
            "files": [],
            "work_items": {"roots": [], "nodes": []},
            "provenance": {},
        }

        environment = {
            "GIT_CONFIG_COUNT": "2",
            "GIT_CONFIG_KEY_0": f"url.{self.source.as_uri()}.insteadOf",
            "GIT_CONFIG_VALUE_0": mixed_module["url"],
            "GIT_CONFIG_KEY_1": "protocol.file.allow",
            "GIT_CONFIG_VALUE_1": "always",
        }
        with (
            patch.dict(os.environ, environment),
            patch("review_context.resolve_pr", return_value=snapshot),
            patch("review_setup._collect", return_value=report),
            patch("review_context.render_markdown", return_value="# Setup\n"),
            patch.object(review_setup, "POOL_DISK_RESERVE_BYTES", 0),
        ):
            manifest = review_setup.setup_run(
                self.root, snapshot["pr_number"], slot=0, client=client
            )

        self.assertEqual(manifest["repo_key"], "repos-wevenova")
        self.assertEqual(manifest["key"], "wevenova-5631145")
        self.assertEqual(manifest["branch"], "review/wevenova-5631145")
        self.assertEqual(manifest["pr_dir"], "PRs/WeveNova-5631145")
        self.assertTrue(
            all(path.startswith("PRs/WeveNova-5631145/") for path in manifest["report_paths"])
        )
        outer = self.root / manifest["slot_path"]
        self.assertTrue(all((outer / path).is_file() for path in manifest["report_paths"]))
        self.assertEqual(
            (outer / "PRs/wevenova-5631145/reviewer.md").read_text(encoding="utf-8"),
            "preserve lowercase history\n",
        )
        self.assertEqual(
            sorted(path.name for path in (outer / "PRs/wevenova-5631145").iterdir()),
            ["reviewer.md"],
        )

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
        remote_fetches = [
            call
            for call in fetches
            if not any(arg.startswith("file://") for arg in call)
        ]
        self.assertEqual(
            len(remote_fetches),
            1,
            msg=f"warm cache should satisfy exact PR objects: {fetches}",
        )
        self.assertTrue(
            all(
                any(a.startswith(("--depth=", "--deepen=")) for a in call)
                for call in remote_fetches
            )
        )
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
        remote_fetches = [
            c
            for c in fetch_calls
            if not any(arg.startswith("file://") for arg in c)
        ]
        # Exactly one network fetch per distinct store. Local cache-to-source
        # imports are expected, bounded, and do not repeat on later warm slots.
        self.assertEqual(
            len(remote_fetches),
            2,
            msg=f"expected exactly two remote fetches total, saw: {fetch_calls}",
        )
        for call in remote_fetches:
            self.assertTrue(
                any(arg.startswith("--depth=") or arg.startswith("--deepen=") for arg in call),
                msg=f"every remote fetch must carry an explicit history bound: {call}",
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
        artifact = f"{manifest['pr_dir']}/PR_Setup_01.json"
        common.write_new_json(outer / artifact, {"run_id": manifest["run_id"]})
        manifest["artifacts"] = {artifact: common.file_hash(outer / artifact)}
        return artifact


    def test_five_way_archive_serializes_only_outer_administration(self):
        with self.fixture_remote(), self.state.lock():
            pool = common.SourcePool(self.state, self.module, reserve_bytes=0)
            rows = pool.warm(range(5))
            for slot, row in enumerate(rows):
                manifest = self._pool_manifest(pool, row, status="ready")
                manifest.update(
                    slot_number=slot,
                    slot=pool.slot_name(slot),
                    slot_id=pool.slot_name(slot),
                    slot_path=pool.slot_relative_path(slot),
                    key=f"nova-{1000 + slot}",
                    branch=f"review/nova-{1000 + slot}",
                    run_id=f"archive-run-{slot}",
                    pr_dir=f"PRs/Nova-{1000 + slot}",
                )
                self._activate_pool_run(manifest)
                self.state.save_pool_run(manifest)

        context = multiprocessing.get_context("fork")
        manager = context.Manager()
        entered, exited = manager.dict(), manager.dict()
        gate = context.Barrier(5)
        results = context.Queue()
        processes = [
            context.Process(
                target=_five_slot_archive_worker,
                args=(self.root, slot, gate, entered, exited, results),
            )
            for slot in range(5)
        ]
        for process in processes:
            process.start()
        rows = [results.get(timeout=60) for _ in processes]
        for process in processes:
            process.join(timeout=60)
            self.assertFalse(process.is_alive())
            self.assertEqual(process.exitcode, 0)
        errors = [row for row in rows if row[1] != "ok"]
        self.assertEqual(errors, [])
        self.assertEqual(len(entered), 5)
        intervals = sorted((entered[slot], exited[slot], slot) for slot in range(5))
        self.assertTrue(
            all(current[0] >= previous[1] for previous, current in zip(intervals, intervals[1:])),
            intervals,
        )
        for slot in range(5):
            manifest = self.state.load_pool_run(pool.repo_key, slot)
            self.assertEqual(manifest["status"], "archived")
            self.assertEqual(manifest["reset_phase"], "released")
            self.assertEqual(
                git(self.root, "rev-parse", f"refs/heads/{manifest['archive_branch']}"),
                manifest["checkpoint_sha"],
            )

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


    def test_recover_slot_cli_inspects_and_applies_exact_operator_assertion(self):
        import review_setup

        with self.fixture_remote(), self.state.lock():
            pool = common.SourcePool(self.state, self.module, reserve_bytes=0)
            pool.warm([0])
        journal = {
            "schema_version": 1,
            "workspace_identity": self.state.identity,
            "repo_key": pool.repo_key,
            "slot_number": 0,
            "operation": {
                "id": "a" * 32,
                "phase": "warm-slot",
                "spawn_pending": True,
                "process": None,
                "coordinator": common.process_identity(os.getpid()),
                "started_at": common.utc_now(),
            },
        }
        journal["operation"]["coordinator"]["start_ticks"] += 1
        common.atomic_json(
            common._slot_writer_path(self.state, pool.repo_key, 0), journal
        )
        stdout, stderr = io.StringIO(), io.StringIO()
        with (
            patch.object(review_setup, "preflight", return_value={}),
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            code = review_setup.main(
                [
                    "--root",
                    str(self.root),
                    "--recover-slot",
                    "--repo",
                    "Nova",
                    "--slot",
                    "0",
                    "--dry-run",
                ]
            )
        self.assertEqual(code, 0, stderr.getvalue())
        inspection = json.loads(stdout.getvalue())
        assertion = {
            key: inspection[key]
            for key in (
                "workspace_identity",
                "repo_key",
                "repo_name",
                "slot_number",
                "slot_id",
                "slot_writer_sha256",
                "operation_id",
                "operation_phase",
                "shared_writer_sha256",
                "shared_operation_id",
                "pool_state_sha256",
                "pool_run_sha256",
                "run_id",
                "setup_reservations",
                "topology_sha256",
                "current_pid_namespace",
            )
        }
        assertion.update(
            prior_writers_retired=True,
            operator="fixture-authorized-operator",
            basis="fixture coordinator identity is deliberately non-current and no child was launched",
        )
        assertion_path = Path(self.temp.name) / "slot-assertion.json"
        assertion_path.write_text(json.dumps(assertion), encoding="utf-8")
        stdout, stderr = io.StringIO(), io.StringIO()
        with (
            patch.object(review_setup, "preflight", return_value={}),
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            code = review_setup.main(
                [
                    "--root",
                    str(self.root),
                    "--recover-slot",
                    "--repo",
                    "Nova",
                    "--slot",
                    "0",
                    "--operator-assertion",
                    str(assertion_path),
                ]
            )
        self.assertEqual(code, 0, stderr.getvalue())
        self.assertFalse(json.loads(stdout.getvalue())["pending"])

    def test_recover_slot_retries_each_partial_settlement_without_losing_evidence(self):
        with self.fixture_remote(), self.state.lock():
            pool = common.SourcePool(self.state, self.module, reserve_bytes=0)
            pool.warm([0])
        slot_path = common._slot_writer_path(self.state, pool.repo_key, 0)
        shared_path = common._shared_writer_path(self.state, pool.repo_key)
        real_atomic_json = common.atomic_json
        real_write_new_json = common.write_new_json

        def operation(character):
            value = {
                "id": character * 32,
                "phase": "warm-slot",
                "spawn_pending": True,
                "process": None,
                "coordinator": common.process_identity(os.getpid()),
                "started_at": common.utc_now(),
            }
            value["coordinator"]["start_ticks"] += 1
            return value

        def publish_pending(value, *, slot_pending=True, shared_pending=True):
            slot_record = {
                "schema_version": 1,
                "workspace_identity": self.state.identity,
                "repo_key": pool.repo_key,
                "slot_number": 0,
                "operation": value if slot_pending else None,
            }
            shared_operation = {**value, "slot_number": 0}
            shared_record = {
                "schema_version": 1,
                "workspace_identity": self.state.identity,
                "repo_key": pool.repo_key,
                "operation": shared_operation if shared_pending else None,
            }
            real_atomic_json(slot_path, slot_record)
            real_atomic_json(shared_path, shared_record)

        for character, publication in (("a", "audit"), ("b", "slot"), ("c", "shared")):
            with self.subTest(publication=publication):
                value = operation(character)
                publish_pending(value)
                inspection = self._slot_recovery_inspection(0)
                crashed = False

                def crash_after_audit(path, document):
                    nonlocal crashed
                    real_write_new_json(path, document)
                    if not crashed:
                        crashed = True
                        raise RuntimeError("fixture crash after audit publication")

                def crash_after_settlement(path, document):
                    nonlocal crashed
                    real_atomic_json(path, document)
                    target = slot_path if publication == "slot" else shared_path
                    if path == target and not crashed:
                        crashed = True
                        raise RuntimeError(
                            f"fixture crash after {publication} settlement"
                        )

                patcher = (
                    patch.object(
                        common, "write_new_json", side_effect=crash_after_audit
                    )
                    if publication == "audit"
                    else patch.object(
                        common, "atomic_json", side_effect=crash_after_settlement
                    )
                )
                with patcher, self.assertRaisesRegex(RuntimeError, "fixture crash"):
                    self._recover_slot_from_inspection(inspection, 0)

                retry_inspection = self._slot_recovery_inspection(0)
                if retry_inspection["pending"]:
                    recovered = self._recover_slot_from_inspection(
                        retry_inspection, 0
                    )
                    self.assertFalse(recovered["pending"])
                slot_record = json.loads(slot_path.read_text(encoding="utf-8"))
                shared_record = json.loads(shared_path.read_text(encoding="utf-8"))
                self.assertIsNone(slot_record["operation"])
                self.assertIsNone(shared_record["operation"])
                self.assertEqual(
                    slot_record["last_settled_operation"]["id"], value["id"]
                )
                self.assertEqual(
                    shared_record["last_settled_operation"]["id"], value["id"]
                )
                audits = sorted(
                    (self.state.directory / "slot-writer-recoveries").glob(
                        f"{pool.repo_key}-0-*.json"
                    )
                )
                evidence = [json.loads(path.read_text()) for path in audits]
                matching = [
                    audit
                    for audit in evidence
                    if audit["binding"]["operation_id"] == value["id"]
                ]
                self.assertTrue(matching)
                self.assertTrue(
                    any(
                        audit["previous_slot_journal"]["operation"] is not None
                        and audit["previous_shared_journal"]["operation"] is not None
                        for audit in matching
                    )
                )
                self.assertTrue(
                    all(audit["operator_assertion"] for audit in matching)
                )

    def test_recover_slot_handles_shared_only_slot_only_and_refuses_changed_operation(self):
        with self.fixture_remote(), self.state.lock():
            pool = common.SourcePool(self.state, self.module, reserve_bytes=0)
            pool.warm([0])
        slot_path = common._slot_writer_path(self.state, pool.repo_key, 0)
        shared_path = common._shared_writer_path(self.state, pool.repo_key)

        def operation(character):
            value = {
                "id": character * 32,
                "phase": "warm-slot",
                "spawn_pending": True,
                "process": None,
                "coordinator": common.process_identity(os.getpid()),
                "started_at": common.utc_now(),
            }
            value["coordinator"]["start_ticks"] += 1
            return value

        shared_only = operation("d")
        slot_path.unlink(missing_ok=True)
        common.atomic_json(
            shared_path,
            {
                "schema_version": 1,
                "workspace_identity": self.state.identity,
                "repo_key": pool.repo_key,
                "operation": {**shared_only, "slot_number": 0},
            },
        )
        inspection = self._slot_recovery_inspection(0)
        self.assertIsNone(inspection["slot_writer_sha256"])
        recovered = self._recover_slot_from_inspection(inspection, 0)
        self.assertFalse(recovered["pending"])
        settled_slot = json.loads(slot_path.read_text(encoding="utf-8"))
        self.assertEqual(
            settled_slot["last_settled_operation"]["id"], shared_only["id"]
        )

        slot_only = operation("e")
        common.atomic_json(
            slot_path,
            {
                "schema_version": 1,
                "workspace_identity": self.state.identity,
                "repo_key": pool.repo_key,
                "slot_number": 0,
                "operation": slot_only,
            },
        )
        shared_path.unlink()
        inspection = self._slot_recovery_inspection(0)
        self.assertIsNone(inspection["shared_writer_sha256"])
        recovered = self._recover_slot_from_inspection(inspection, 0)
        self.assertFalse(recovered["pending"])

        original = operation("f")
        changed = operation("1")
        common.atomic_json(
            slot_path,
            {
                "schema_version": 1,
                "workspace_identity": self.state.identity,
                "repo_key": pool.repo_key,
                "slot_number": 0,
                "operation": original,
            },
        )
        common.atomic_json(
            shared_path,
            {
                "schema_version": 1,
                "workspace_identity": self.state.identity,
                "repo_key": pool.repo_key,
                "operation": {**original, "slot_number": 0},
            },
        )
        inspection = self._slot_recovery_inspection(0)
        common.atomic_json(
            slot_path,
            {
                "schema_version": 1,
                "workspace_identity": self.state.identity,
                "repo_key": pool.repo_key,
                "slot_number": 0,
                "operation": changed,
            },
        )
        with self.assertRaises(common.ReviewError):
            self._recover_slot_from_inspection(inspection, 0)
        self.assertEqual(
            json.loads(slot_path.read_text())["operation"]["id"], changed["id"]
        )
        self.assertEqual(
            json.loads(shared_path.read_text())["operation"]["id"], original["id"]
        )

        common.atomic_json(
            slot_path,
            {
                "schema_version": 1,
                "workspace_identity": self.state.identity,
                "repo_key": pool.repo_key,
                "slot_number": 0,
                "operation": original,
            },
        )
        common.atomic_json(
            shared_path,
            {
                "schema_version": 1,
                "workspace_identity": self.state.identity,
                "repo_key": pool.repo_key,
                "operation": {**original, "slot_number": 0},
            },
        )
        inspection = self._slot_recovery_inspection(0)
        real_write_new_json = common.write_new_json

        def replace_after_audit(path, document):
            real_write_new_json(path, document)
            common.atomic_json(
                slot_path,
                {
                    "schema_version": 1,
                    "workspace_identity": self.state.identity,
                    "repo_key": pool.repo_key,
                    "slot_number": 0,
                    "operation": changed,
                },
            )

        with (
            patch.object(
                common, "write_new_json", side_effect=replace_after_audit
            ),
            self.assertRaises(common.ReviewError),
        ):
            self._recover_slot_from_inspection(inspection, 0)
        self.assertEqual(
            json.loads(slot_path.read_text())["operation"]["id"], changed["id"]
        )

    def test_actual_reset_commit_child_survives_owner_kill_then_explicit_recovery(self):
        import review_reset

        with self.fixture_remote(), self.state.lock():
            pool = common.SourcePool(self.state, self.module, reserve_bytes=0)
            row = pool.warm([1])[0]
            manifest = self._pool_manifest(pool, row, status="ready")
            self._activate_pool_run(manifest)
            self.state.save_pool_run(manifest)

        child, _, _, descriptors = self._kill_actual_entrypoint_at_git(
            "reset", slot=1
        )
        with self.assertRaisesRegex(common.ReviewError, "still executable"):
            state = common.ReviewState(self.root)
            retry_pool = common.SourcePool(state, self.module, reserve_bytes=0)
            with (
                common.lock_session(5),
                state.lock(shared=True),
                state.slot_lock(retry_pool.repo_key, 1),
                common.slot_writer_scope(
                    state, retry_pool.repo_key, 1, phase="retry"
                ),
            ):
                pass
        interrupted = self.state.load_pool_run(pool.repo_key, 1)
        self.assertEqual(interrupted["status"], "checkpointing")
        self.assertEqual(interrupted["reset_phase"], "retained")
        with self.fixture_remote(), self.assertRaisesRegex(
            common.ReviewError, "still executable"
        ):
            review_reset.reset_pool_run(
                self.state,
                "Nova",
                1,
                mode="archive",
                run_maintenance=False,
                lock_timeout=5,
            )

        self._retire_killed_fixture(child, descriptors)
        inspection = self._slot_recovery_inspection(1)
        with self.assertRaisesRegex(common.ReviewError, "exact workspace"):
            self._recover_slot_from_inspection(
                inspection, 1, slot_writer_sha256="0" * 64
            )
        current = self.state.load_pool_run(pool.repo_key, 1)
        current["status"] = "foreign-state"
        self.state.save_pool_run(current)
        with self.assertRaisesRegex(common.ReviewError, "exact workspace"):
            self._recover_slot_from_inspection(inspection, 1)
        current["status"] = interrupted["status"]
        self.state.save_pool_run(current)
        fresh = self._slot_recovery_inspection(1)
        recovered = self._recover_slot_from_inspection(fresh, 1)
        self.assertFalse(recovered["pending"])
        with self.fixture_remote():
            result = review_reset.reset_pool_run(
                self.state,
                "Nova",
                1,
                mode="archive",
                run_maintenance=False,
                lock_timeout=5,
            )
        self.assertEqual(result["status"], "archived")

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


    def _assert_reset_dry_run_matches_retry_phase(self, expected_phase):
        import review_reset

        with self.fixture_remote(), self.state.lock():
            pool = common.SourcePool(self.state, self.module, reserve_bytes=0)
            row = pool.warm([1])[0]
            manifest = self._pool_manifest(pool, row, status="ready")
            self._activate_pool_run(manifest)
            self.state.save_pool_run(manifest)
        original_save = self.state.save_pool_run
        original_reset_source = common.SourcePool.reset_source
        interrupted = False

        def save_then_interrupt(value):
            nonlocal interrupted
            original_save(value)
            if value.get("reset_phase") == expected_phase and not interrupted:
                interrupted = True
                raise common.ReviewError(f"fixture after {expected_phase}")

        def fail_source_reset(instance, *args, **kwargs):
            if expected_phase == "source-reset":
                raise common.ReviewError("fixture source reset failure")
            return original_reset_source(instance, *args, **kwargs)

        with (
            self.fixture_remote(),
            patch.object(self.state, "save_pool_run", side_effect=save_then_interrupt),
            patch.object(common.SourcePool, "reset_source", new=fail_source_reset),
            self.assertRaises(common.ReviewError),
        ):
            review_reset.reset_pool_run(
                self.state, "Nova", 1, mode="archive", run_maintenance=False
            )
        stuck = self.state.load_pool_run(pool.repo_key, 1)
        self.assertEqual(stuck["reset_phase"], expected_phase)
        before = self.state.pool_run_path(pool.repo_key, 1).read_bytes()
        dry = review_reset.reset_pool_run(
            self.state,
            "Nova",
            1,
            mode="archive",
            dry_run=True,
            run_maintenance=False,
        )
        self.assertEqual(dry["reset_phase"], expected_phase)
        self.assertEqual(dry["next_action"], "resume-reset")
        self.assertEqual(
            self.state.pool_run_path(pool.repo_key, 1).read_bytes(), before
        )

    def test_reset_dry_run_matches_retry_at_outer_detached(self):
        self._assert_reset_dry_run_matches_retry_phase("outer-detached")

    def test_reset_dry_run_matches_retry_at_source_reset_intent(self):
        self._assert_reset_dry_run_matches_retry_phase("source-reset-intent")

    def test_reset_dry_run_matches_retry_at_source_reset(self):
        self._assert_reset_dry_run_matches_retry_phase("source-reset")

    def test_reset_dry_run_matches_retry_at_source_verified(self):
        self._assert_reset_dry_run_matches_retry_phase("source-verified")

    def test_source_verified_reset_preserves_new_files_then_releases_same_mode(self):
        import review_reset

        with self.fixture_remote(), self.state.lock():
            pool = common.SourcePool(self.state, self.module, reserve_bytes=0)
            row = pool.warm([1])[0]
            manifest = self._pool_manifest(pool, row, status="ready")
            self._activate_pool_run(manifest)
            self.state.save_pool_run(manifest)
        original_save = self.state.save_pool_run
        interrupted = False

        def stop_at_source_verified(value):
            nonlocal interrupted
            original_save(value)
            if value.get("reset_phase") == "source-verified" and not interrupted:
                interrupted = True
                raise common.ReviewError("fixture source-verified interruption")

        with (
            self.fixture_remote(),
            patch.object(self.state, "save_pool_run", side_effect=stop_at_source_verified),
            self.assertRaisesRegex(common.ReviewError, "source-verified interruption"),
        ):
            review_reset.reset_pool_run(
                self.state, "Nova", 1, mode="archive", run_maintenance=False
            )
        outer = self.root / row["slot_path"]
        unexpected = outer / "PRs/Nova-5636725/slot-state-investigation.md"
        unexpected.parent.mkdir(parents=True)
        unexpected.write_text("preserve reset investigation\n", encoding="utf-8")

        with self.fixture_remote():
            result = review_reset.reset_pool_run(
                self.state, "Nova", 1, mode="archive", run_maintenance=False
            )

        self.assertEqual(result["status"], "archived")
        self.assertEqual(result["reset_phase"], "released")
        self.assertFalse(unexpected.exists())
        preservation = Path(result["reset_preservation_path"])
        record = json.loads((preservation / "manifest.json").read_text(encoding="utf-8"))
        entry = next(
            entry
            for entry in record["checkouts"]["outer"]["entries"]
            if entry["path"] == "PRs/Nova-5636725/slot-state-investigation.md"
        )
        payload = preservation / entry["worktree_payload"]
        self.assertEqual(payload.read_text(encoding="utf-8"), "preserve reset investigation\n")
        self.assertEqual(common.file_hash(payload), entry["worktree_sha256"])

    def test_source_verified_reset_preservation_crash_resumes_exact_archive(self):
        import review_reset

        with self.fixture_remote(), self.state.lock():
            pool = common.SourcePool(self.state, self.module, reserve_bytes=0)
            row = pool.warm([1])[0]
            manifest = self._pool_manifest(pool, row, status="ready")
            self._activate_pool_run(manifest)
            self.state.save_pool_run(manifest)
        original_save = self.state.save_pool_run
        reached = False

        def stop_at_source_verified(value):
            nonlocal reached
            original_save(value)
            if value.get("reset_phase") == "source-verified" and not reached:
                reached = True
                raise common.ReviewError("fixture source-verified interruption")

        with (
            self.fixture_remote(),
            patch.object(self.state, "save_pool_run", side_effect=stop_at_source_verified),
            self.assertRaisesRegex(common.ReviewError, "source-verified interruption"),
        ):
            review_reset.reset_pool_run(
                self.state, "Nova", 1, mode="archive", run_maintenance=False
            )
        outer = self.root / row["slot_path"]
        (outer / "late.txt").write_text("late\n", encoding="utf-8")
        real_atomic = common.atomic_json
        interrupted = False

        def stop_after_preservation(path, value):
            nonlocal interrupted
            real_atomic(path, value)
            if (
                Path(path).parent.name == "cleanup"
                and Path(path).name.startswith("reset-")
                and value.get("phase") == "preserved"
                and not interrupted
            ):
                interrupted = True
                raise common.ReviewError("fixture reset preservation crash")

        with (
            self.fixture_remote(),
            patch.object(review_reset, "atomic_json", side_effect=stop_after_preservation),
            self.assertRaisesRegex(common.ReviewError, "reset preservation crash"),
        ):
            review_reset.reset_pool_run(
                self.state, "Nova", 1, mode="archive", run_maintenance=False
            )
        interrupted_manifest = self.state.load_pool_run(pool.repo_key, 1)
        journal_path = Path(interrupted_manifest["reset_preservation_journal"])
        journal = json.loads(journal_path.read_text(encoding="utf-8"))
        self.assertEqual(journal["phase"], "preserved")
        archive = Path(journal["preservation_path"])
        digest = common.file_hash(archive / "manifest.json")

        with self.fixture_remote():
            result = review_reset.reset_pool_run(
                self.state, "Nova", 1, mode="archive", run_maintenance=False
            )

        self.assertEqual(result["reset_phase"], "released")
        self.assertEqual(Path(result["reset_preservation_path"]), archive)
        self.assertEqual(common.file_hash(archive / "manifest.json"), digest)

    def test_source_verified_reset_refuses_missing_preserved_payload_before_cleanup(self):
        import review_reset

        with self.fixture_remote(), self.state.lock():
            pool = common.SourcePool(self.state, self.module, reserve_bytes=0)
            row = pool.warm([1])[0]
            manifest = self._pool_manifest(pool, row, status="ready")
            self._activate_pool_run(manifest)
            self.state.save_pool_run(manifest)
        original_save = self.state.save_pool_run
        reached = False

        def stop_at_source_verified(value):
            nonlocal reached
            original_save(value)
            if value.get("reset_phase") == "source-verified" and not reached:
                reached = True
                raise common.ReviewError("fixture source-verified interruption")

        with (
            self.fixture_remote(),
            patch.object(self.state, "save_pool_run", side_effect=stop_at_source_verified),
            self.assertRaisesRegex(common.ReviewError, "source-verified interruption"),
        ):
            review_reset.reset_pool_run(
                self.state, "Nova", 1, mode="archive", run_maintenance=False
            )
        outer = self.root / row["slot_path"]
        late = outer / "late-reset.txt"
        late.write_text("late reset state\n", encoding="utf-8")
        real_atomic = common.atomic_json
        interrupted = False

        def stop_after_preservation(path, value):
            nonlocal interrupted
            real_atomic(path, value)
            if (
                Path(path).parent.name == "cleanup"
                and Path(path).name.startswith("reset-")
                and value.get("phase") == "preserved"
                and not interrupted
            ):
                interrupted = True
                raise common.ReviewError("fixture reset preservation crash")

        with (
            self.fixture_remote(),
            patch.object(review_reset, "atomic_json", side_effect=stop_after_preservation),
            self.assertRaisesRegex(common.ReviewError, "reset preservation crash"),
        ):
            review_reset.reset_pool_run(
                self.state, "Nova", 1, mode="archive", run_maintenance=False
            )
        interrupted_manifest = self.state.load_pool_run(pool.repo_key, 1)
        journal = json.loads(
            Path(interrupted_manifest["reset_preservation_journal"]).read_text(
                encoding="utf-8"
            )
        )
        archive = Path(journal["preservation_path"])
        record = json.loads((archive / "manifest.json").read_text(encoding="utf-8"))
        entry = next(
            item
            for item in record["checkouts"]["outer"]["entries"]
            if item["path"] == late.name
        )
        payload = archive / entry["worktree_payload"]
        payload.unlink()

        with self.fixture_remote(), self.assertRaisesRegex(
            common.ReviewError, "Preservation archive"
        ):
            review_reset.reset_pool_run(
                self.state, "Nova", 1, mode="archive", run_maintenance=False
            )

        self.assertTrue(late.is_file())
        self.assertEqual(late.read_text(encoding="utf-8"), "late reset state\n")
        self.assertEqual(
            self.state.load_pool_run(pool.repo_key, 1)["reset_phase"],
            "source-verified",
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
                unittest.mock.ANY,
                "Nova",
                1,
                mode="archive",
                confirm=None,
                dry_run=False,
                lock_timeout=1200,
            )
            self.assertEqual(review_reset.main(["--legacy-slot", "1"]), 0)
            legacy.assert_called_once_with(
                unittest.mock.ANY, 1, mode="archive", confirm=None, dry_run=False
            )
        self.assertEqual(state.identity, common.ReviewState(self.root).identity)



if __name__ == "__main__":
    unittest.main()
