"""Deterministic process-level locking regressions for pooled review setup."""
import contextlib
from datetime import datetime
import io
import json
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import review_common as common
import review_reset
import review_setup


def git(root, *args):
    return subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


class ScopedLockTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "root"
        self.root.mkdir()
        git(self.root, "init", "-b", "main")
        git(self.root, "config", "user.name", "Concurrency Test")
        git(self.root, "config", "user.email", "concurrency@example.invalid")
        (self.root / "tracked").write_text("x\n")
        git(self.root, "add", "tracked")
        git(self.root, "commit", "-m", "initial")
        self.state = common.ReviewState(self.root)

    def _holder(self, body, ready):
        code = (
            "import pathlib,sys,time; "
            f"sys.path.insert(0,{str(SCRIPTS)!r}); "
            "import review_common as c; "
            f"s=c.ReviewState(pathlib.Path({str(self.root)!r})); "
            + body.replace("READY", repr(str(ready)))
        )
        return subprocess.Popen([sys.executable, "-c", code])

    def _wait_ready(self, path):
        deadline = time.monotonic() + 5
        while not path.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(path.exists(), "holder never reached its lock barrier")

    def test_compatibility_shared_invocations_overlap_and_legacy_exclusive_waits(self):
        ready = Path(self.temp.name) / "ready"
        holder = self._holder(
            "ctx=c.lock_session(5); ctx.__enter__(); "
            "lock=s.lock(shared=True, phase='test-holder'); lock.__enter__(); "
            "pathlib.Path(READY).write_text('ready'); time.sleep(.5); "
            "lock.__exit__(None,None,None); ctx.__exit__(None,None,None)",
            ready,
        )
        self.addCleanup(lambda: holder.poll() is None and holder.kill())
        self._wait_ready(ready)
        started = time.monotonic()
        with common.lock_session(1), self.state.lock(shared=True, phase="test-peer"):
            shared_elapsed = time.monotonic() - started
        self.assertLess(shared_elapsed, 0.25)
        started = time.monotonic()
        with common.lock_session(2), self.state.lock(phase="legacy-exclusive"):
            exclusive_elapsed = time.monotonic() - started
        self.assertGreater(exclusive_elapsed, 0.20)
        self.assertEqual(holder.wait(timeout=3), 0)

    def test_same_slot_excludes_but_different_slots_overlap(self):
        ready = Path(self.temp.name) / "slot-ready"
        holder = self._holder(
            "ctx=c.lock_session(5); ctx.__enter__(); "
            "compat=s.lock(shared=True); compat.__enter__(); "
            "slot=s.slot_lock('nova',0,phase='holder'); slot.__enter__(); "
            "pathlib.Path(READY).write_text('ready'); time.sleep(.5); "
            "slot.__exit__(None,None,None); compat.__exit__(None,None,None); "
            "ctx.__exit__(None,None,None)",
            ready,
        )
        self.addCleanup(lambda: holder.poll() is None and holder.kill())
        self._wait_ready(ready)
        with common.lock_session(2), self.state.lock(shared=True):
            started = time.monotonic()
            with self.state.slot_lock("nova", 1, phase="different"):
                different_elapsed = time.monotonic() - started
            self.assertLess(different_elapsed, 0.25)
            started = time.monotonic()
            with self.state.slot_lock("nova", 0, phase="same"):
                same_elapsed = time.monotonic() - started
            self.assertGreater(same_elapsed, 0.20)
        self.assertEqual(holder.wait(timeout=3), 0)

    def test_unrelated_work_does_not_consume_actual_lock_wait_budget(self):
        ready = Path(self.temp.name) / "actual-wait-ready"
        holder = self._holder(
            "ctx=c.lock_session(5); ctx.__enter__(); "
            "compat=s.lock(shared=True); compat.__enter__(); "
            "slot=s.slot_lock('nova',4,phase='actual-wait-holder'); slot.__enter__(); "
            "pathlib.Path(READY).write_text('ready'); time.sleep(.45); "
            "slot.__exit__(None,None,None); compat.__exit__(None,None,None); "
            "ctx.__exit__(None,None,None)",
            ready,
        )
        self.addCleanup(lambda: holder.poll() is None and holder.kill())
        self._wait_ready(ready)
        with common.lock_session(0.35) as session, self.state.lock(shared=True):
            time.sleep(0.25)
            started = time.monotonic()
            with self.state.slot_lock("nova", 4, phase="actual-wait-contender"):
                blocked = time.monotonic() - started
            self.assertGreater(blocked, 0.10)
            self.assertLess(blocked, 0.35)
            self.assertGreater(session.waited, 0.10)
            self.assertLess(session.waited, 0.35)
        self.assertEqual(holder.wait(timeout=3), 0)

    def test_source_lock_wait_is_excluded_from_fetch_budget(self):
        fake_common = self.state.directory / "source-stores" / "nova.git"
        ready = Path(self.temp.name) / "fetch-wait-ready"
        holder = self._holder(
            "ctx=c.lock_session(5); ctx.__enter__(); "
            "compat=s.lock(shared=True); compat.__enter__(); "
            f"source=c.source_lock(pathlib.Path({str(fake_common)!r}),phase='fetch-wait-holder'); source.__enter__(); "
            "pathlib.Path(READY).write_text('ready'); time.sleep(.30); "
            "source.__exit__(None,None,None); compat.__exit__(None,None,None); "
            "ctx.__exit__(None,None,None)",
            ready,
        )
        self.addCleanup(lambda: holder.poll() is None and holder.kill())
        self._wait_ready(ready)
        with common.lock_session(1), self.state.lock(shared=True), common.fetch_session(0.15) as fetch:
            fetch.remaining()
            with common.source_lock(fake_common, phase="fetch-wait-contender"):
                self.assertGreater(fetch.remaining(), 0.08)
            self.assertGreater(fetch.excluded_lock_wait, 0.20)
        self.assertEqual(holder.wait(timeout=3), 0)

    def test_lock_timeout_is_bounded_and_reports_scope_phase_and_owner(self):
        ready = Path(self.temp.name) / "timeout-ready"
        holder = self._holder(
            "ctx=c.lock_session(5); ctx.__enter__(); "
            "compat=s.lock(shared=True); compat.__enter__(); "
            "slot=s.slot_lock('nova',2,phase='network-transfer'); slot.__enter__(); "
            "pathlib.Path(READY).write_text('ready'); time.sleep(1); "
            "slot.__exit__(None,None,None); compat.__exit__(None,None,None); "
            "ctx.__exit__(None,None,None)",
            ready,
        )
        self.addCleanup(lambda: holder.poll() is None and holder.kill())
        self._wait_ready(ready)
        started = time.monotonic()
        with common.lock_session(0.15), self.state.lock(shared=True):
            with self.assertRaisesRegex(
                common.ReviewError,
                r"nova.*slot 2.*waited.*0\.15.*network-transfer",
            ):
                with self.state.slot_lock("nova", 2, phase="contender"):
                    pass
        self.assertLess(time.monotonic() - started, 0.75)
        self.assertEqual(holder.wait(timeout=3), 0)

    def test_killed_slot_coordinator_refuses_retry_while_child_group_lives(self):
        ready = Path(self.temp.name) / "slot-writer-ready"
        heartbeat = Path(self.temp.name) / "slot-writer-heartbeat"
        child_code = (
            "import os,signal,time\n"
            "signal.signal(signal.SIGTERM,signal.SIG_IGN)\n"
            f"open({str(ready)!r},'w').write(str(os.getpid()))\n"
            f"fd=os.open({str(heartbeat)!r},os.O_WRONLY|os.O_CREAT|os.O_APPEND,0o600)\n"
            "while True:\n"
            " os.write(fd,b'x')\n"
            " time.sleep(.02)\n"
        )
        code = (
            "import pathlib,sys; "
            f"sys.path.insert(0,{str(SCRIPTS)!r}); import review_common as c; "
            f"s=c.ReviewState(pathlib.Path({str(self.root)!r})); "
            "locks=c.lock_session(10); locks.__enter__(); "
            "compat=s.lock(shared=True); compat.__enter__(); "
            "slot=s.slot_lock('nova',0); slot.__enter__(); "
            "scope=c.slot_writer_scope(s,'nova',0,phase='fixture-checkout'); scope.__enter__(); "
            f"c.run_owned([sys.executable,'-c',{child_code!r}],timeout=60,mutates=True)"
        )
        owner = subprocess.Popen([sys.executable, "-c", code])
        self.addCleanup(lambda: owner.poll() is None and owner.kill())
        self._wait_ready(ready)
        child = int(ready.read_text())
        os.kill(owner.pid, signal.SIGKILL)
        owner.wait(timeout=3)
        with common.lock_session(1), self.state.lock(shared=True), self.state.slot_lock("nova", 0):
            with self.assertRaisesRegex(common.ReviewError, "still executable"):
                with common.slot_writer_scope(self.state, "nova", 0, phase="retry"):
                    pass
        os.killpg(child, signal.SIGKILL)
        deadline = time.monotonic() + 3
        while Path(f"/proc/{child}").exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        with common.lock_session(1), self.state.lock(shared=True), self.state.slot_lock("nova", 0):
            with common.slot_writer_scope(self.state, "nova", 0, phase="retry"):
                pass

    def test_known_prelaunch_failure_settles_slot_journal(self):
        real_popen = common.subprocess.Popen

        def fail_git_only(args, *popen_args, **popen_kwargs):
            if "git" in args:
                raise OSError("fixture prelaunch failure")
            return real_popen(args, *popen_args, **popen_kwargs)

        with (
            common.lock_session(1),
            self.state.lock(shared=True),
            self.state.slot_lock("nova", 0),
            common.slot_writer_scope(
                self.state, "nova", 0, phase="fixture-checkout"
            ),
            patch.object(
                common.subprocess,
                "Popen",
                side_effect=fail_git_only,
            ),
        ):
            with self.assertRaises(common.CommandNotStartedError):
                common.run_git(self.root, ["checkout", "--detach", "HEAD"])
        journal = json.loads(
            common._slot_writer_path(self.state, "nova", 0).read_text()
        )
        self.assertIsNone(journal["operation"])
        self.assertEqual(
            journal["last_settlement_proof"], "typed-command-not-started"
        )
        with (
            common.lock_session(1),
            self.state.lock(shared=True),
            self.state.slot_lock("nova", 0),
            common.slot_writer_scope(self.state, "nova", 0, phase="retry"),
        ):
            pass

    def test_lock_order_rejects_registry_reacquire_and_acquisition_under_reader(self):
        fake_common = self.state.directory / "source-stores" / "nova.git"
        with common.lock_session(1), self.state.lock(shared=True):
            with self.state.slot_lock("nova", 0):
                with self.assertRaisesRegex(common.ReviewError, "Registry lock"):
                    with self.state.registry_lock():
                        pass
            with common.source_lock(fake_common, shared=True):
                with self.assertRaisesRegex(
                    common.ReviewError, "Acquisition cannot begin under"
                ):
                    with common.acquisition_lock(fake_common):
                        pass

    def test_outer_administration_cannot_nest_with_source_or_acquisition(self):
        fake_common = self.state.directory / "source-stores" / "nova.git"
        with common.lock_session(1), self.state.lock(shared=True):
            with common.source_lock(fake_common, shared=True):
                with self.assertRaisesRegex(
                    common.ReviewError, "Outer administration cannot nest"
                ):
                    with self.state.outer_lock():
                        pass
            with self.state.outer_lock():
                with self.assertRaisesRegex(
                    common.ReviewError, "cannot nest with outer administration"
                ):
                    with common.source_lock(fake_common):
                        pass

    def test_recovery_requires_real_acquisition_and_source_write_locks(self):
        fake_common = self.state.directory / "source-stores" / "nova.git"
        with self.assertRaisesRegex(common.ReviewError, "acquisition.*source-write"):
            common.recover_acquisition_cache(fake_common, inspect=True)

    def test_unlocked_observation_of_legitimate_cache_fetch_never_recovers(self):
        common_dir = self.state.directory / "source-stores" / "nova.git"
        cache = common.acquisition_cache_path(common_dir)
        cache.mkdir(parents=True)
        common.atomic_json(
            cache / "review-acquisition.json",
            {
                "schema_version": 1,
                "source_common": str(common_dir),
                "remote_hash": "0" * 64,
                "blocked": False,
                "operation": {
                    "phase": "network-acquisition",
                    "source_untouched": True,
                },
            },
        )
        with patch.object(
            common,
            "recover_acquisition_cache",
            side_effect=AssertionError("unlocked observation attempted recovery"),
        ) as recovery:
            common.check_acquisition_ready(common_dir)
        recovery.assert_not_called()

    def test_unlocked_observation_still_blocks_import_and_unknown_writer_state(self):
        common_dir = self.state.directory / "source-stores" / "nova.git"
        cache = common.acquisition_cache_path(common_dir)
        cache.mkdir(parents=True)
        for operation in (
            {"phase": "source-import", "source_untouched": False},
            {"phase": "unknown", "source_untouched": True},
        ):
            with self.subTest(phase=operation["phase"]):
                common.atomic_json(
                    cache / "review-acquisition.json",
                    {
                        "schema_version": 1,
                        "source_common": str(common_dir),
                        "remote_hash": "0" * 64,
                        "blocked": False,
                        "operation": operation,
                    },
                )
                with self.assertRaisesRegex(
                    common.ReviewError, "source import|unknown acquisition"
                ):
                    common.check_acquisition_ready(common_dir)

    def test_successful_import_is_settled_before_source_lock_handoff(self):
        source = Path(self.temp.name) / "source"
        source.mkdir()
        git(source, "init", "-b", "main")
        git(source, "config", "user.name", "Concurrency Test")
        git(source, "config", "user.email", "concurrency@example.invalid")
        (source / "file.txt").write_text("main\n", encoding="utf-8")
        git(source, "add", "file.txt")
        git(source, "commit", "-m", "main")

        remote = Path(self.temp.name) / "remote.git"
        subprocess.run(
            ["git", "clone", "--bare", str(source), str(remote)],
            check=True,
            capture_output=True,
            text=True,
        )
        common_dir = self.state.directory / "source-stores" / "nova.git"
        common_dir.parent.mkdir(parents=True)
        subprocess.run(
            [
                "git",
                "clone",
                "--bare",
                "--depth=1",
                remote.resolve().as_uri(),
                str(common_dir),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        git(source, "checkout", "-b", "feature")
        (source / "feature.txt").write_text("feature\n", encoding="utf-8")
        git(source, "add", "feature.txt")
        git(source, "commit", "-m", "feature")
        tip = git(source, "rev-parse", "HEAD")
        git(source, "push", str(remote), "feature")

        handoff = Path(self.temp.name) / "source-lock-released"
        release = Path(self.temp.name) / "release-importer"
        worker_code = f"""
import contextlib
import pathlib
import sys
import time
sys.path.insert(0, {str(SCRIPTS)!r})
import review_common as common
original_source_lock = common.source_lock
@contextlib.contextmanager
def gated_source_lock(path, *, shared=False, phase='source-store', wait=True):
    with original_source_lock(path, shared=shared, phase=phase, wait=wait):
        yield
    if phase == 'source-import':
        pathlib.Path({str(handoff)!r}).write_text('released', encoding='utf-8')
        deadline = time.monotonic() + 10
        while not pathlib.Path({str(release)!r}).exists():
            if time.monotonic() >= deadline:
                raise TimeoutError('fixture source-lock handoff was not released')
            time.sleep(0.01)
common.source_lock = gated_source_lock
state = common.ReviewState(pathlib.Path({str(self.root)!r}))
with common.lock_session(20), state.lock(shared=True), common.fetch_session(20):
    result = common.bounded_fetch(
        pathlib.Path({str(common_dir)!r}),
        {remote.resolve().as_uri()!r},
        [{tip!r}],
        depth=10,
        max_depth=1000,
    )
sys.exit(result.returncode)
"""
        worker = subprocess.Popen(
            [sys.executable, "-c", worker_code],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.addCleanup(lambda: worker.poll() is None and worker.kill())
        follower = None
        try:
            self._wait_ready(handoff)
            follower_code = f"""
import pathlib
import sys
sys.path.insert(0, {str(SCRIPTS)!r})
import review_common as common
state = common.ReviewState(pathlib.Path({str(self.root)!r}))
source = pathlib.Path({str(common_dir)!r})
with common.lock_session(5), state.lock(shared=True):
    with common.source_lock(source, shared=True, phase='handoff-follower'):
        common.check_acquisition_ready(source, source_reader=True)
        assert common.run_git_read(source, ['cat-file', '-e', {tip!r} + '^{{commit}}'], check=False).returncode == 0
"""
            follower = subprocess.run(
                [sys.executable, "-c", follower_code],
                capture_output=True,
                text=True,
                timeout=10,
            )
        finally:
            release.write_text("release", encoding="utf-8")
            worker_stdout, worker_stderr = worker.communicate(timeout=10)
        self.assertEqual(worker.returncode, 0, worker_stdout + worker_stderr)
        self.assertIsNotNone(follower)
        self.assertEqual(follower.returncode, 0, follower.stdout + follower.stderr)
        state = json.loads(
            (
                common.acquisition_cache_path(common_dir)
                / "review-acquisition.json"
            ).read_text(encoding="utf-8")
        )
        self.assertNotIn("operation", state)
        self.assertFalse(state.get("blocked"))


class OperationLoggingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "root"
        self.root.mkdir()
        git(self.root, "init", "-b", "main")
        git(self.root, "config", "user.name", "Fixture")
        git(self.root, "config", "user.email", "fixture@example.invalid")
        (self.root / "tracked").write_text("x\n", encoding="utf-8")
        git(self.root, "add", "tracked")
        git(self.root, "commit", "-m", "initial")
        self.state = common.ReviewState(self.root)

    def _events(self, log_path):
        return [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]

    def test_operation_log_is_private_parseable_correlated_and_outside_slots(self):
        with common.operation_logging(
            self.root,
            command="setup",
            verbose=False,
            pr_number=42,
            slot_id="Nova-2",
            invocation_id="a" * 32,
        ) as operation:
            operation.bind(run_id="run-safe", operation_id="operation-safe")
            operation.event(
                "source.snapshot",
                "Source snapshot validated",
                phase="prepared",
                source_sha="1" * 40,
                outcome="valid",
            )
            log_path = operation.path
        events = self._events(log_path)
        self.assertGreaterEqual(len(events), 3)
        self.assertEqual(events[0]["event"], "operation.begin")
        self.assertEqual(events[-1]["event"], "operation.end")
        self.assertTrue(all(row["invocation_id"] == "a" * 32 for row in events))
        self.assertTrue(all(row["attempt_id"] for row in events))
        self.assertEqual(events[-1]["actual_exit"], 0)
        self.assertEqual(events[-1]["run_id"], "run-safe")
        for row in events:
            self.assertTrue({"@t", "@l", "@m", "@mt", "@logger", "application", "event"}.issubset(row))
            datetime.fromisoformat(row["@t"])
        self.assertEqual(stat.S_IMODE(log_path.stat().st_mode), 0o600)
        self.assertTrue(log_path.is_relative_to(self.state.directory / "logs"))
        self.assertFalse(log_path.is_relative_to(self.root / ".worktrees"))

    def test_numeric_fetch_progress_is_bounded_redacted_and_survives_failure(self):
        with self.assertRaisesRegex(common.ReviewError, "fixture transfer failed"):
            with common.operation_logging(
                self.root,
                command="setup",
                verbose=False,
                invocation_id="f" * 32,
            ) as operation:
                progress = common.FetchProgress()
                self.addCleanup(progress.path.unlink, missing_ok=True)
                for current in range(300):
                    progress.git_line(
                        f"Receiving objects: 50% ({current}/600), 1.00 MiB | 2.00 MiB/s"
                    )
                progress.git_line(
                    "remote: https://user:SECRET@example.invalid/repo Authorization: Bearer PRIVATE"
                )
                progress.close()
                log_path = operation.path
                raise common.ReviewError("fixture transfer failed")

        events = self._events(log_path)
        progress_events = [
            row for row in events if row["event"] == "git.fetch.progress"
        ]
        self.assertLessEqual(len(progress_events), 256)
        self.assertTrue(progress_events)
        for row in progress_events:
            self.assertEqual(row["phase"], "network-acquisition")
            self.assertEqual(row["progress_kind"], "Receiving objects")
            self.assertGreaterEqual(row["elapsed_ms"], 0)
            self.assertEqual(row["percent"], 50)
            self.assertIsInstance(row["current_count"], int)
            self.assertEqual(row["total_count"], 600)
            self.assertEqual(row["received_bytes"], 1024 ** 2)
        rendered = log_path.read_text(encoding="utf-8")
        self.assertNotIn("SECRET", rendered)
        self.assertNotIn("PRIVATE", rendered)
        self.assertNotIn("https://", rendered)
        self.assertEqual(events[-1]["event"], "operation.end")
        self.assertEqual(events[-1]["outcome"], "failed")

    def test_lock_wait_heartbeat_acquire_release_and_hold_are_recorded(self):
        clock = iter([0.0, 0.0, 0.0, 10.1, 10.1, 10.2, 10.3, 10.4, 10.5, 10.6])
        with common.operation_logging(
            self.root,
            command="setup",
            verbose=False,
            invocation_id="b" * 32,
        ) as operation, common.lock_session(20):
            with patch.object(common.time, "monotonic", side_effect=lambda: next(clock)):
                with patch.object(
                    common.fcntl,
                    "flock",
                    side_effect=[BlockingIOError(), BlockingIOError(), None, None],
                ):
                    with self.state.lock(shared=True, phase="fixture-lock"):
                        pass
            log_path = operation.path
        events = self._events(log_path)
        names = [row["event"] for row in events]
        self.assertIn("lock.wait.begin", names)
        self.assertIn("lock.wait.heartbeat", names)
        self.assertIn("lock.acquired", names)
        self.assertIn("lock.released", names)
        acquired = next(row for row in events if row["event"] == "lock.acquired")
        released = next(row for row in events if row["event"] == "lock.released")
        self.assertGreaterEqual(acquired["wait_ms"], 0)
        self.assertGreaterEqual(released["hold_ms"], 0)
        self.assertEqual(acquired["lock_class"], "compatibility")

    def test_owned_child_lifecycle_records_safe_class_identity_and_exit(self):
        with common.operation_logging(
            self.root,
            command="setup",
            verbose=False,
            invocation_id="e" * 32,
        ) as operation:
            result = common.run_owned(
                [sys.executable, "-c", "pass"], timeout=5, mutates=False
            )
            self.assertEqual(result.returncode, 0)
            log_path = operation.path
        events = self._events(log_path)
        intent = next(row for row in events if row["event"] == "owned-child.spawn-intent")
        started = next(row for row in events if row["event"] == "owned-child.started")
        ended = next(row for row in events if row["event"] == "owned-child.ended")
        self.assertEqual(intent["command_class"], "python")
        self.assertEqual(started["child_pid"], ended["child_pid"])
        self.assertGreater(started["child_start_ticks"], 0)
        self.assertEqual(ended["actual_exit"], 0)
        rendered = log_path.read_text(encoding="utf-8")
        self.assertNotIn('"argv"', rendered)
        self.assertNotIn('"command"', rendered)
        self.assertNotIn('"pass"', rendered)

    def test_redaction_is_recursive_and_exception_text_never_leaks(self):
        canaries = [
            "PAT-CANARY-123",
            "Bearer SECRET-CANARY",
            "https://user:password@example.invalid/repo",
            "proxy-user:proxy-password",
            "private PR title and body",
        ]
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            with common.operation_logging(
                self.root,
                command="setup",
                verbose=True,
                invocation_id="c" * 32,
            ) as operation:
                operation.event(
                    "redaction.fixture",
                    "Potentially unsafe input was classified",
                    level="Trace",
                    headers={"Authorization": canaries[1], "safe": "ok"},
                    command=["git", canaries[2]],
                    environment={"TOKEN": canaries[0]},
                    error=RuntimeError(" ".join(canaries)),
                    pr_content=canaries[4],
                    proxy=canaries[3],
                )
                log_path = operation.path
        combined = log_path.read_text(encoding="utf-8") + stdout.getvalue() + stderr.getvalue()
        for canary in canaries:
            self.assertNotIn(canary, combined)
        row = next(item for item in self._events(log_path) if item["event"] == "redaction.fixture")
        self.assertEqual(row["headers"], "[REDACTED]")
        self.assertEqual(row["command"], "[REDACTED]")
        self.assertEqual(row["environment"], "[REDACTED]")
        self.assertEqual(row["error"], {"category": "RuntimeError"})

    def test_logging_failure_warns_once_and_does_not_change_operation_authority(self):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            with common.operation_logging(
                self.root,
                command="setup",
                verbose=False,
                invocation_id="d" * 32,
            ) as operation:
                with patch.object(operation, "_write", side_effect=OSError("disk full secret")):
                    operation.event("fixture.one", "First")
                    operation.event("fixture.two", "Second")
                self.assertFalse(operation.available)
        self.assertEqual(stderr.getvalue().count("Structured diagnostics unavailable"), 1)

    def test_diagnostic_identity_failure_does_not_change_setup_or_reset_results(self):
        setup_out, setup_err = io.StringIO(), io.StringIO()
        with (
            patch.object(common, "process_identity", side_effect=OSError("diagnostic proc failure")),
            patch.object(review_setup, "preflight", return_value={"python": "fixture"}),
            contextlib.redirect_stdout(setup_out),
            contextlib.redirect_stderr(setup_err),
        ):
            setup_code = review_setup.main(
                ["--root", str(self.root), "--preflight"]
            )
        self.assertEqual(setup_code, 0)
        self.assertEqual(json.loads(setup_out.getvalue()), {"python": "fixture"})
        self.assertNotIn("diagnostic proc failure", setup_err.getvalue())

        reset_out, reset_err = io.StringIO(), io.StringIO()
        with (
            patch.object(common, "process_identity", side_effect=OSError("diagnostic proc failure")),
            patch.object(
                review_reset,
                "preflight",
                side_effect=common.ReviewError("fixture lifecycle stop"),
            ),
            contextlib.redirect_stdout(reset_out),
            contextlib.redirect_stderr(reset_err),
        ):
            reset_code = review_reset.main(
                ["--root", str(self.root), "--slot", "Nova-0"]
            )
        self.assertEqual(reset_code, 1)
        self.assertEqual(reset_out.getvalue(), "")
        self.assertIn("fixture lifecycle stop", reset_err.getvalue())
        self.assertNotIn("diagnostic proc failure", reset_err.getvalue())

    def test_setup_and_reset_accept_verbose_without_changing_stdout_contract(self):
        setup_out, setup_err = io.StringIO(), io.StringIO()
        with (
            patch.object(review_setup, "preflight", return_value={"python": "fixture"}),
            contextlib.redirect_stdout(setup_out),
            contextlib.redirect_stderr(setup_err),
        ):
            setup_code = review_setup.main(["--preflight", "--verbose"])
        self.assertEqual(setup_code, 0)
        self.assertEqual(json.loads(setup_out.getvalue()), {"python": "fixture"})
        self.assertIn("operation.begin", setup_err.getvalue())
        self.assertIn("operation.end", setup_err.getvalue())

        reset_out, reset_err = io.StringIO(), io.StringIO()
        with (
            patch.object(review_reset, "preflight", side_effect=common.ReviewError("fixture stop")),
            contextlib.redirect_stdout(reset_out),
            contextlib.redirect_stderr(reset_err),
        ):
            reset_code = review_reset.main(
                ["--root", str(self.root), "--slot", "Nova-0", "--verbose"]
            )
        self.assertEqual(reset_code, 1)
        self.assertEqual(reset_out.getvalue(), "")
        self.assertIn("operation.begin", reset_err.getvalue())
        self.assertIn("failure_category=ReviewError", reset_err.getvalue())
        self.assertIn("actual_exit=1", reset_err.getvalue())

    def test_nonverbose_cli_stdout_and_stderr_remain_compatible(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        with (
            patch.object(review_setup, "preflight", return_value={"python": "fixture"}),
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            code = review_setup.main(["--preflight"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(stdout.getvalue()), {"python": "fixture"})
        self.assertEqual(stderr.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
