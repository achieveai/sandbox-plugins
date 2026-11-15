"""Actual Git with a remote-capability shim; all repositories disposable."""
import json
import multiprocessing
import os
from pathlib import Path
import queue
import shutil
import stat
import unittest
from unittest.mock import patch
import test_lifecycle
from test_lifecycle import git
import review_common as common
import review_setup as setup


def _setup_while_remote_is_gated(root, snapshot, slot, required_sha, remote_entered, release_remote, results):
    import review_context
    import review_setup

    original = common.run_git
    real_fetch = common.bounded_fetch

    def operation():
        remote_entered.set()
        if not release_remote.wait(timeout=10):
            raise TimeoutError("fixture remote acquisition gate was not released")

        def gate_remote(repo, args, **kwargs):
            is_remote = (
                "fetch" in args
                and not any(value.startswith("file://") for value in args)
            )
            if is_remote:
                remote_entered.set()
                if not release_remote.wait(timeout=10):
                    raise TimeoutError("fixture remote acquisition gate was not released")
            return original(repo, args, **kwargs)

        def fetch_missing(source, ignored_snapshot, **kwargs):
            return real_fetch(source, "origin", [required_sha], depth=100, max_depth=1000)

        with patch.object(review_context, "resolve_pr", return_value=snapshot), \
             patch.object(review_setup, "_collect", return_value=test_lifecycle._empty_report()), \
             patch.object(review_setup, "_fetch_exact_objects", side_effect=fetch_missing), \
             patch.object(review_context, "render_markdown", return_value="# fixture\n"), \
             patch.object(common, "run_git", side_effect=gate_remote), \
             patch.object(review_setup, "run_git", side_effect=gate_remote):
            return review_setup.setup_run(
                Path(root), snapshot["pr_number"], slot=slot,
                client=test_lifecycle._OfflineClient(), history_timeout=20,
            )

    test_lifecycle._result(results, "acquisition", operation)


def _setup_cached_reader(root, snapshot, slot, entered, results):
    def operation():
        import review_context
        import review_setup

        def collect(*args, **kwargs):
            entered.set()
            return test_lifecycle._empty_report()

        with patch.object(review_context, "resolve_pr", return_value=snapshot), \
             patch.object(review_setup, "_collect", side_effect=collect), \
             patch.object(review_context, "render_markdown", return_value="# fixture\n"):
            return review_setup.setup_run(
                Path(root), snapshot["pr_number"], slot=slot,
                client=test_lifecycle._OfflineClient(), history_timeout=20,
            )

    test_lifecycle._result(results, "reader", operation)


def _hold_shared_source_reader(pool_root, module, nested, entered, release, results):
    def operation():
        state = common.ReviewState(Path(pool_root))
        pool = common.SourcePool(state, module, reserve_bytes=0)
        with state.lock(shared=True), pool.store_lock(shared=True):
            entered.set()
            value = git(Path(nested), "rev-parse", "HEAD")
            if not release.wait(timeout=10):
                raise TimeoutError("fixture source-reader gate was not released")
            return value

    test_lifecycle._result(results, "reader", operation)


def _run_exclusive_source_import(pool_root, module, nested, entered, results):
    def operation():
        state = common.ReviewState(Path(pool_root))
        pool = common.SourcePool(state, module, reserve_bytes=0)
        with state.lock(shared=True), pool.store_lock():
            entered.set()
            return git(Path(nested), "cat-file", "-t", "HEAD")

    test_lifecycle._result(results, "import", operation)


class FetchCompatibilityTests(unittest.TestCase):
    setUp = test_lifecycle.WarmPoolTests.setUp
    fixture_remote = test_lifecycle.WarmPoolTests.fixture_remote

    def history(self):
        commits=[self.head]
        for i in range(30):
            (self.source/'file.txt').write_text(f'main{i}\n')
            git(self.source,'commit','-am',f'main{i}')
            commits.append(git(self.source,'rev-parse','HEAD'))
        self.main=commits[-1]
        self.pool=common.SourcePool(self.state,self.module)
        self.pool.refresh(depth=12)
        self.pool.warm([0,1])
        self.sentinel=commits[-10]
        self.old_edges=self.edges()
        self.merge=commits[-13]
        git(self.source,'checkout','-b','feature',self.merge)
        for i in range(7):
            (self.source/'feature.txt').write_text(f'feature{i}\n')
            git(self.source,'add','feature.txt')
            git(self.source,'commit','-m',f'feature{i}')
        self.pr=git(self.source,'rev-parse','HEAD')
        self.snap=dict(self.snapshot,head_sha=self.pr,target_sha=self.main,base_sha=self.merge,source_ref='refs/heads/feature')
        self.nested=self.root/'.worktrees/Nova-0/repos/Nova'

    def edges(self):
        return {parts[0]:set(parts[1:]) for parts in (line.split() for line in git(self.pool.store,'rev-list','--all','--parents').splitlines())}

    def shim(self):
        real=shutil.which('git'); bindir=Path(self.temp.name)/'bin';bindir.mkdir(exist_ok=True)
        log=Path(self.temp.name)/'calls.jsonl'
        shim=bindir/'git'
        shim.write_text('#!/usr/bin/env python3\nimport os,sys,json\na=sys.argv[1:]\n'
            f'with open({str(log)!r},"a") as f: f.write(json.dumps(a)+"\\n")\n'
            'if "fetch" in a and any(x.startswith("--deepen=") for x in a) and not any(x.startswith("file://") for x in a):\n'
            ' sys.stderr.write("fatal: Server does not support --deepen\\nfatal: the remote end hung up unexpectedly\\n");sys.exit(128)\n'
            'if "remote" in a and "get-url" in a:\n'
            ' i=a.index("remote");a=a[:i]+["config","--get","remote.origin.url"]\n'
            f'os.execv({real!r},[{real!r}]+a)\n')
        shim.chmod(shim.stat().st_mode|stat.S_IXUSR)
        return patch.dict(os.environ,{'PATH':str(bindir)+os.pathsep+os.environ['PATH']}),log

    def test_exact_pin_then_ancestor_recovery_preserves_old_dag(self):
        with self.fixture_remote(),self.state.lock():
            self.history(); heads=[git(self.root/f'.worktrees/Nova-{i}/repos/Nova','rev-parse','HEAD') for i in (0,1)]
            context,log=self.shim()
            with context:
                manifest=setup._reserve_pool_run(self.state,self.snap,self.module,0)
                source,base=setup.prepare_source(self.state,manifest,max_depth=1000)
            self.assertEqual(base,self.merge)
            self.assertEqual(git(source,'rev-parse','HEAD'),self.pr)
            self.assertEqual(git(self.root/'.worktrees/Nova-1/repos/Nova','rev-parse','HEAD'),heads[1])
            self.assertEqual(git(source,'merge-base',self.pr,self.main),self.merge)
            after=self.edges()
            for commit,parents in self.old_edges.items():
                self.assertIn(commit,after);self.assertLessEqual(parents,after[commit])
            self.assertEqual(git(source,'merge-base','--is-ancestor',self.sentinel,self.main),'')
            calls=[json.loads(line) for line in log.read_text().splitlines()]
            remote=[a for a in calls if 'fetch' in a and not any(x.startswith('file://') for x in a)]
            self.assertTrue(any(any(x.startswith('--depth=') for x in a) for a in remote))
            self.assertTrue(any(any(x.startswith('--deepen=') for x in a) for a in remote))
            self.assertTrue(
                any(
                    not any(x.startswith(('--depth=', '--deepen=')) for x in a)
                    for a in remote
                )
            )
            self.assertFalse(list(self.pool.store.parent.glob('.bounded-fetch-*')))

    def test_baseline_refresh_uses_capability_fallback_without_shrinking(self):
        with self.fixture_remote(),self.state.lock():
            self.history();git(self.source,'checkout','main')
            (self.source/'file.txt').write_text('updated main\n');git(self.source,'commit','-am','updated main')
            new=git(self.source,'rev-parse','HEAD')
            context,_=self.shim()
            with context:actual=self.pool.refresh(depth=100)
            self.assertEqual(actual,new);self.assertEqual(self.pool.cached_baseline_sha(),new)
            after=self.edges()
            for commit,parents in self.old_edges.items():self.assertLessEqual(parents,after[commit])

    def test_history_budget_blocks_before_temporary_acquisition(self):
        with self.fixture_remote(),self.state.lock():
            self.history();context,log=self.shim()
            before=(self.pool.store/'shallow').read_bytes()
            with context,self.assertRaisesRegex(common.ReviewError,'history budget'):
                common.bounded_fetch(
                    self.nested,
                    'origin',
                    [self.pr],
                    depth=100,
                    max_depth=100,
                    deepen=True,
                )
            self.assertEqual((self.pool.store/'shallow').read_bytes(),before)
            self.assertFalse(list(self.pool.store.parent.glob('.bounded-fetch-*')))

    def test_unproven_cleanup_preserves_only_owned_temporary_store(self):
        from subprocess import CompletedProcess
        with self.fixture_remote(),self.state.lock():
            self.history();original=common.run_git
            def fail(repo,args,**kwargs):
                if 'fetch' in args and not any(x.startswith('file://') for x in args):
                    raise common.CleanupNotProvenError('cleanup not proven')
                return original(repo,args,**kwargs)
            with patch.object(common,'run_git',side_effect=fail),self.assertRaises(common.CleanupNotProvenError):
                common.bounded_fetch(self.nested,'origin',[self.pr],depth=100,max_depth=1000)
            cache=common.acquisition_cache_path(self.pool.store)
            self.assertTrue(cache.is_dir())
            self.assertTrue(json.loads((cache/'review-acquisition.json').read_text())['blocked'])
            with self.assertRaisesRegex(
                common.ReviewError,
                'Pending source import|cleanup',
            ):
                common.bounded_fetch(self.nested,'origin',[self.pr],depth=100,max_depth=1000)
            self.assertFalse(list(self.pool.store.parent.glob('.bounded-fetch-*')))
            self.assertEqual(git(self.nested,'rev-parse','HEAD'),self.main)

    def test_registered_detached_tip_is_acquired_without_retention_ref(self):
        with self.fixture_remote(),self.state.lock():
            self.history()
            # Remove the baseline ref in this disposable fixture: registered
            # detached HEADs must still be included in temporary acquisition.
            git(self.pool.store,'update-ref','-d','refs/review-setup/baselines/repos-nova')
            context,log=self.shim()
            with context:result=common.bounded_fetch(self.nested,'origin',[self.pr],depth=100,max_depth=1000)
            self.assertEqual(result.returncode,0)
            acquisitions=[
                json.loads(line)
                for line in log.read_text().splitlines()
                if '"fetch"' in line
                and not any(
                    value.startswith('file://')
                    for value in json.loads(line)
                )
            ]
            # A warm cache already retains registered detached tips. The remote
            # request stays incremental, while local import and edge checks prove
            # the detached history remains available without a named source ref.
            self.assertFalse(
                any(
                    any(
                        value.lstrip('+').startswith(
                            self.main + ':refs/heads/acquire-'
                        )
                        for value in args
                    )
                    for args in acquisitions
                )
            )
            self.assertEqual(git(self.nested,'merge-base','--is-ancestor',self.sentinel,self.main),'')

    def test_persistent_cache_reuses_completed_acquisition_and_old_history(self):
        with self.fixture_remote(),self.state.lock():
            self.history(); context,log=self.shim()
            with context:
                first=common.bounded_fetch(self.nested,'origin',[self.pr],depth=100,max_depth=1000)
                self.assertEqual(first.returncode,0)
                cache=common.acquisition_cache_path(self.pool.store)
                packs={p.name:p.stat().st_size for p in (cache/'objects/pack').glob('*.pack')}
                before_calls=len(log.read_text().splitlines())
                second=common.bounded_fetch(self.nested,'origin',[self.pr],depth=100,max_depth=1000)
                self.assertEqual(second.returncode,0)
                later=[json.loads(s) for s in log.read_text().splitlines()[before_calls:]]
                self.assertFalse([a for a in later if 'fetch' in a and not any(x.startswith('file://') for x in a)])
                self.assertTrue(all((cache/'objects/pack'/name).stat().st_size==size for name,size in packs.items()))
                (self.source/'feature.txt').write_text('incremental\n');git(self.source,'commit','-am','incremental')
                new=git(self.source,'rev-parse','HEAD')
                third=common.bounded_fetch(self.nested,'origin',[new],depth=20,max_depth=1000)
                self.assertEqual(third.returncode,0)
                self.assertEqual(git(self.nested,'cat-file','-t',new),'commit')
                self.assertTrue(all((cache/'objects/pack'/name).exists() for name in packs))
            after=self.edges()
            for commit,parents in self.old_edges.items(): self.assertLessEqual(parents,after[commit])
            self.assertEqual(git(self.nested,'rev-parse','HEAD'),self.main)

    def test_prime_existing_warm_store_verifies_coverage_and_repeat_without_fetch(self):
        with self.fixture_remote():
            with self.state.lock():
                self.history(); git(self.source,'checkout','main')
                heads=[git(self.root/f'.worktrees/Nova-{i}/repos/Nova','rev-parse','HEAD') for i in (0,1)]
                context,log=self.shim()
                with context:
                    row=self.pool.prime_history(depth=25,timeout=30)
                    self.assertEqual(row['status'],'primed')
                    self.assertGreaterEqual(row['covered_depth'],25)
                    before=len(log.read_text().splitlines())
                    repeated=self.pool.prime_history(depth=25,timeout=30)
                    calls=[json.loads(s) for s in log.read_text().splitlines()[before:]]
                    self.assertFalse([a for a in calls if 'fetch' in a])
                    self.assertTrue(repeated['cache_hit'])
                self.assertEqual([git(self.root/f'.worktrees/Nova-{i}/repos/Nova','rev-parse','HEAD') for i in (0,1)],heads)

    def test_prime_dry_run_leaves_all_files_and_dirty_active_slot_unchanged(self):
        with self.fixture_remote(), self.state.lock():
            self.history()
            dirty=self.nested/'uncommitted.txt'; dirty.write_text('preserve me')
            before={str(p.relative_to(self.root)):p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
            with patch.object(common, 'bounded_fetch', side_effect=AssertionError('no network')):
                row=self.pool.prime_history(depth=25,dry_run=True)
            self.assertTrue(row['network_required'])
            after={str(p.relative_to(self.root)):p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
            self.assertEqual(before,after)
            context,_=self.shim()
            with context: self.pool.prime_history(depth=25,timeout=30)
            self.assertEqual(dirty.read_text(),'preserve me')
            self.assertEqual(git(self.nested,'rev-parse','HEAD'),self.main)

    def test_prime_cli_cold_store_and_repeat_dry_run(self):
        with self.fixture_remote():
            args=['--root',str(self.root),'--prime-history','--repo','Nova','--baseline-depth','25','--history-timeout','30']
            self.assertEqual(setup.main(args+['--dry-run']),0)
            self.assertFalse((self.state.common/'review-setup/source-stores').exists())
            self.assertEqual(setup.main(args),0)
            self.assertEqual(setup.main(args+['--dry-run']),0)
            self.assertFalse((self.root/'.worktrees').exists())

    def test_private_progress_is_live_and_filters_secrets(self):
        import sys, threading, time
        progress=common.FetchProgress(); self.addCleanup(progress.path.unlink,missing_ok=True)
        errors=[]
        def execute():
            try:
                common.run_owned([sys.executable,'-c',
                    'import sys,time;sys.stderr.write("Receiving objects: 50% (1/2), 1.00 MiB | 2.00 MiB/s\\rhttps://secret-token@example.test\\n");sys.stderr.flush();time.sleep(0.8)'],
                    timeout=5,progress=progress)
            except BaseException as exc: errors.append(exc)
        worker=threading.Thread(target=execute); worker.start()
        try:
            until=time.monotonic()+2
            while time.monotonic()<until and 'Receiving objects' not in progress.path.read_text(): time.sleep(0.02)
            self.assertTrue(worker.is_alive())
            text=progress.path.read_text()
            self.assertIn('Receiving objects',text);self.assertNotIn('secret',text)
            self.assertEqual(progress.path.stat().st_mode & 0o777,0o600)
        finally: worker.join(6);progress.close()
        self.assertFalse(errors)

    def test_progress_failure_and_timeout_preserve_owned_runner(self):
        import sys
        progress=common.FetchProgress();self.addCleanup(progress.path.unlink,missing_ok=True)
        with patch.object(progress.stream,'write',side_effect=OSError('full')):
            result=common.run_owned([sys.executable,'-c','print("Counting objects: 1",file=__import__("sys").stderr)'],timeout=5,progress=progress)
        self.assertEqual(result.returncode,0);self.assertTrue(progress.failed)
        with self.assertRaisesRegex(common.ReviewError,'process group was terminated'):
            common.run_owned([sys.executable,'-c','import time;time.sleep(10)'],timeout=0.1,progress=progress)
        progress.close()

    def test_fetch_session_deadline_is_shared(self):
        with common.fetch_session(timeout=1) as session:
            session.remaining()
            session.deadline=0
            with self.assertRaisesRegex(common.ReviewError,'shared operation timeout'):
                common.run_git(self.root,['rev-parse','HEAD'])
        self.assertIsNone(common._FETCH_SESSION.get())

    def test_exact_fallback_stops_when_required_objects_exist(self):
        from subprocess import CompletedProcess
        calls=[]; available=set()
        def fetch(source,remote,refs,**kwargs):
            calls.append(refs)
            if len(calls)==1:return CompletedProcess([],128,'','not our ref')
            available.update([self.head,self.base])
            return CompletedProcess([],0,'','')
        with patch.object(setup,'bounded_fetch',side_effect=fetch), patch.object(setup,'_has_commit',side_effect=lambda source,sha:sha in available):
            setup._fetch_exact_objects(self.root,self.snapshot,depth=100)
        self.assertEqual(len(calls),2)

    def test_zombie_only_group_is_terminated_not_permanently_blocked(self):
        import subprocess, sys
        # Separate subreaper owns/reaps the fixture orphan; no leaked zombies.
        program = '''
import ctypes, os, signal, sys, time
import review_common as common
assert ctypes.CDLL(None).prctl(36, 1, 0, 0, 0) == 0
child = "import os,signal,time; p=os.fork(); signal.signal(signal.SIGTERM,signal.SIG_IGN) if p==0 else None; time.sleep(20)"
try:
    common.run_owned([sys.executable, '-c', child], timeout=.3, term_grace=.1, kill_grace=.1)
except common.CleanupNotProvenError:
    code = 9
except common.ReviewError:
    code = 0
finally:
    while True:
        try: os.waitpid(-1, 0)
        except ChildProcessError: break
sys.exit(code)
'''
        result = subprocess.run([sys.executable, '-c', program], timeout=5,
                                env=dict(os.environ, PYTHONPATH=str(Path(common.__file__).parent)))
        self.assertEqual(result.returncode, 0, 'Zombie-only groups must not block acquisition forever')

    def test_owned_identity_is_durable_before_command_runs(self):
        import sys
        marker = Path(self.temp.name) / 'identity.json'
        with common.owned_journal(lambda identity: marker.write_text(json.dumps(identity))):
            result = common.run_owned([sys.executable, '-c',
                f'import json; from pathlib import Path; assert json.loads(Path({str(marker)!r}).read_text())["start_ticks"] > 0'], timeout=5)
        self.assertEqual(result.returncode, 0)
        identity = json.loads(marker.read_text())
        self.assertEqual(identity['pid'], identity['pgid'])
        self.assertIn('pid_namespace', identity)
        self.assertIn('boot_id', identity)

    def test_recovery_refuses_legacy_without_assertion_and_preserves_bytes(self):
        with self.fixture_remote(), self.state.lock():
            self.history(); context, _ = self.shim()
            with context: common.bounded_fetch(self.nested, 'origin', [self.pr], depth=100, max_depth=1000)
            path = common.acquisition_cache_path(self.pool.store) / 'review-acquisition.json'
            state = json.loads(path.read_text()); state['blocked'] = True
            path.write_text(json.dumps(state)); before = path.read_bytes()
            with self.pool.acquisition_lock(phase="test-recovery"), self.pool.store_lock(phase="test-recovery"):
                with self.assertRaisesRegex(common.ReviewError, 'historical process identity'):
                    common.recover_acquisition_cache(self.pool.store)
            self.assertEqual(path.read_bytes(), before)

    def test_recovery_checks_checkpoint_edges_before_clearing_block(self):
        with self.fixture_remote(), self.state.lock():
            self.history(); context, _ = self.shim()
            with context: common.bounded_fetch(self.nested, 'origin', [self.pr], depth=100, max_depth=1000)
            cache = common.acquisition_cache_path(self.pool.store)
            path = cache / 'review-acquisition.json'; state = json.loads(path.read_text())
            state.update(blocked=True, operation={'id':'fixture', 'processes':[], 'phase':'fetch',
                         'owner':common.process_identity(os.getpid()), 'spawn_pending':False,
                         'depth':100, 'checkpoint':'review-checkpoint-fixture.json'})
            checkpoint = {'source':{self.main:['0'*40]}, 'cache':{}}
            common.atomic_json(cache / state['operation']['checkpoint'], checkpoint)
            common.atomic_json(path, state); before = path.read_bytes()
            with self.pool.acquisition_lock(phase="test-recovery"), self.pool.store_lock(phase="test-recovery"):
                with patch.object(common, '_assert_cache_quiescent'), self.assertRaisesRegex(common.ReviewError, 'preservation'):
                    common.recover_acquisition_cache(self.pool.store)
            self.assertEqual(path.read_bytes(), before)

    def test_journal_write_failure_never_executes_command(self):
        import sys
        marker = Path(self.temp.name) / 'must-not-exist'
        for logged in (False, True):
            progress = common.FetchProgress() if logged else None
            try:
                with common.owned_journal(lambda _: (_ for _ in ()).throw(OSError('full'))):
                    with self.assertRaises(OSError):
                        common.run_owned([sys.executable, '-c', f'open({str(marker)!r},"w").write("bad")'], timeout=5, progress=progress)
                self.assertFalse(marker.exists())
            finally:
                if progress: progress.close(); progress.path.unlink()

    def test_recovery_rejects_other_namespace_and_active_group(self):
        current = common.process_identity(os.getpid())
        operation = {'owner':current, 'processes':[dict(current, pid_namespace='pid:[other]')], 'spawn_pending':False}
        with self.assertRaisesRegex(common.ReviewError, 'another boot/PID namespace'):
            common._assert_cache_quiescent(self.root, self.root, operation)
        operation['processes'] = [current]
        with self.assertRaisesRegex(common.ReviewError, 'still active'):
            common._assert_cache_quiescent(self.root, self.root, operation)

    def test_explicit_recovery_preserves_refs_packs_and_audits_assertion(self):
        import hashlib
        with self.fixture_remote(), self.state.lock():
            self.history(); context, _ = self.shim()
            with context: common.bounded_fetch(self.nested, 'origin', [self.pr], depth=100, max_depth=1000)
            cache = common.acquisition_cache_path(self.pool.store); path = cache/'review-acquisition.json'
            state = json.loads(path.read_text()); state['blocked'] = True; common.atomic_json(path,state)
            before = path.read_bytes(); refs = git(cache,'show-ref')
            packs = {p.name:p.read_bytes() for p in (cache/'objects/pack').iterdir()}
            assertion = {'source_common':str(self.pool.store), 'cache':str(cache),
                         'state_sha256':hashlib.sha256(before).hexdigest(), 'prior_writers_retired':True,
                         'operator':'fixture operator', 'basis':'Disposable repository owned exclusively by this test; no external writers.',
                         'current_pid_namespace':common.process_identity(os.getpid())['pid_namespace']}
            # Fixture owns its repositories; simulate an otherwise complete process
            # handle census without requiring access to root-owned host services.
            with self.pool.acquisition_lock(phase="test-recovery"), self.pool.store_lock(phase="test-recovery"):
                with patch.object(common, '_process_census', return_value=[]):
                    result = common.recover_acquisition_cache(self.pool.store, assertion=assertion)
            self.assertFalse(result['blocked']); self.assertIn('operator-asserted',result['proof'])
            self.assertEqual(git(cache,'show-ref'),refs)
            self.assertEqual({p.name:p.read_bytes() for p in (cache/'objects/pack').iterdir()},packs)
            records = [json.loads(p.read_text()) for p in cache.glob('review-recovery-*.json')]
            self.assertTrue(any(r['operator_assertion']==assertion and r['previous_state']==state for r in records))

    def test_recovery_rejects_changed_assertion_and_git_lock(self):
        with self.fixture_remote(), self.state.lock():
            self.history(); context, _ = self.shim()
            with context: common.bounded_fetch(self.nested,'origin',[self.pr],depth=100,max_depth=1000)
            cache = common.acquisition_cache_path(self.pool.store); path=cache/'review-acquisition.json'
            state=json.loads(path.read_text()); state['blocked']=True; common.atomic_json(path,state)
            before=path.read_bytes()
            with self.pool.acquisition_lock(phase="test-recovery"), self.pool.store_lock(phase="test-recovery"):
                with self.assertRaisesRegex(common.ReviewError,'Operator assertion'):
                    common.recover_acquisition_cache(self.pool.store,assertion={'state_sha256':'wrong'})
                (cache/'shallow.lock').write_text('fixture')
                with self.assertRaisesRegex(common.ReviewError,'Git lock'):
                    common.recover_acquisition_cache(self.pool.store)
            self.assertEqual(path.read_bytes(),before)
            self.assertEqual((cache/'shallow.lock').read_text(),'fixture')

    def test_handled_timeout_settles_operation_before_next_namespace(self):
        with self.fixture_remote(), self.state.lock():
            self.history(); context, log = self.shim()
            # Delay only real network-side fetch in this disposable Git shim.
            shim = Path(self.temp.name)/'bin/git'
            text=shim.read_text(); text=text.replace('a=sys.argv[1:]', 'a=sys.argv[1:]\nif "fetch" in a and not any(x.startswith("file://") for x in a): __import__("time").sleep(10)')
            shim.write_text(text)
            with context, common.fetch_session(timeout=2), self.assertRaises(common.OwnedTimeoutError):
                common.bounded_fetch(self.nested,'origin',[self.pr],depth=100,max_depth=1000)
            cache=common.acquisition_cache_path(self.pool.store)
            state=json.loads((cache/'review-acquisition.json').read_text())
            self.assertFalse(state.get('blocked')); self.assertNotIn('operation',state)
            changed=dict(common.process_identity(os.getpid()),pid_namespace='pid:[next-exec]')
            with self.pool.acquisition_lock(phase="test-inspect"), self.pool.store_lock(phase="test-inspect"):
                with patch.object(common,'process_identity',return_value=changed):
                    self.assertFalse(common.recover_acquisition_cache(self.pool.store)['pending'])

    def test_cache_recovery_cli_inspects_without_clearing_legacy(self):
        with self.fixture_remote(), self.state.lock():
            self.history(); context,_=self.shim()
            with context: common.bounded_fetch(self.nested,'origin',[self.pr],depth=100,max_depth=1000)
            cache=common.acquisition_cache_path(self.pool.store); path=cache/'review-acquisition.json'
            state=json.loads(path.read_text());state['blocked']=True;common.atomic_json(path,state); before=path.read_bytes()
        self.assertEqual(setup.main(['--root',str(self.root),'--recover-cache','--repo','Nova','--dry-run']),0)
        self.assertEqual(path.read_bytes(),before)
        self.assertEqual(setup.main(['--root',str(self.root),'--recover-cache','--repo','Nova']),1)
        self.assertEqual(path.read_bytes(),before)

    def test_process_census_includes_nonleader_threads(self):
        import threading
        stop=threading.Event(); tid=[]
        def worker():
            tid.append(threading.get_native_id()); stop.wait(5)
        thread=threading.Thread(target=worker);thread.start()
        try:
            rows=common._process_census()
            self.assertTrue(any(row['pid']==tid[0] for row in rows), 'A zombie leader can still have executable threads')
        finally: stop.set();thread.join()

    def test_recovery_requires_lifecycle_lock(self):
        with self.state.lock(shared=True):
            with self.assertRaisesRegex(common.ReviewError,'acquisition.*source-write'):
                common.recover_acquisition_cache(self.root/'not-a-cache')

    def test_shared_deadline_between_commands_settles_verified_operation(self):
        with self.fixture_remote(), self.state.lock():
            self.history(); context,_=self.shim(); original=common.run_git
            def expire_after_fetch(repo,args,**kwargs):
                result=original(repo,args,**kwargs)
                if 'fetch' in args and not any(x.startswith('file://') for x in args):
                    common._FETCH_SESSION.get().deadline=0
                return result
            with context, common.fetch_session(timeout=30), patch.object(common,'run_git',side_effect=expire_after_fetch):
                with self.assertRaisesRegex(common.ReviewError,'shared operation timeout'):
                    common.bounded_fetch(self.nested,'origin',[self.pr],depth=100,max_depth=1000)
            state=json.loads((common.acquisition_cache_path(self.pool.store)/'review-acquisition.json').read_text())
            self.assertNotIn('operation',state)
            self.assertFalse(state.get('blocked'))

    def test_deadline_after_spawn_intent_settles_proven_unlaunched_command(self):
        with self.fixture_remote(), self.state.lock():
            self.history(); context,_=self.shim(); original=common.atomic_json
            def expire_at_intent(path,value):
                original(path,value)
                if path.name=='review-acquisition.json' and value.get('operation',{}).get('spawn_pending'):
                    common._FETCH_SESSION.get().deadline=0
            with context, common.fetch_session(timeout=30), patch.object(common,'atomic_json',side_effect=expire_at_intent):
                with self.assertRaisesRegex(common.ReviewError,'shared operation timeout'):
                    common.bounded_fetch(self.nested,'origin',[self.pr],depth=100,max_depth=1000)
            state=json.loads((common.acquisition_cache_path(self.pool.store)/'review-acquisition.json').read_text())
            self.assertNotIn('operation',state)
            self.assertFalse(state.get('blocked'))

    def test_cached_objects_cannot_bypass_pending_source_import(self):
        with self.fixture_remote(), self.state.lock():
            self.history(); context,_=self.shim()
            with context: common.bounded_fetch(self.nested,'origin',[self.pr],depth=100,max_depth=1000)
            manifest=setup._reserve_pool_run(self.state,self.snap,self.module,0)
            cache=common.acquisition_cache_path(self.pool.store); path=cache/'review-acquisition.json'
            state=json.loads(path.read_text());state['blocked']=True;common.atomic_json(path,state)
            before=git(self.nested,'rev-parse','HEAD')
            with self.assertRaisesRegex(
                common.ReviewError,
                'Pending source import|historical process identity|acquisition.*source-write',
            ):
                setup.prepare_source(self.state,manifest,max_depth=1000)
            self.assertEqual(git(self.nested,'rev-parse','HEAD'),before)

    def _finish_fixture_processes(self, processes, release_events=()):
        for event in release_events:
            event.set()
        for process in processes:
            if process.pid is not None:
                process.join(timeout=10)
        for process in processes:
            if process.pid is not None and process.is_alive():
                process.terminate()
                process.join(timeout=5)
            if process.pid is not None and process.is_alive():
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

    def test_cached_source_reader_progresses_while_other_slot_waits_on_remote_acquisition(self):
        with self.fixture_remote(), self.state.lock():
            self.history()
            warm_head = git(self.root / ".worktrees/Nova-1/repos/Nova", "rev-parse", "HEAD")
            (self.source / "feature.txt").write_text("network candidate\n")
            git(self.source, "commit", "-am", "network candidate")
            required_sha = git(self.source, "rev-parse", "HEAD")
            common.acquisition_cache_path(self.pool.store).mkdir(parents=True, exist_ok=True)
        acquire_snapshot = dict(
            self.snap,
            pr_number=42,
            snapshot_fingerprint="acquisition-42",
        )
        cached_snapshot = dict(
            self.snapshot,
            pr_number=43,
            snapshot_fingerprint="cached-43",
            head_sha=warm_head,
            target_sha=self.base,
            base_sha=self.base,
        )
        process_context = multiprocessing.get_context("fork")
        remote_entered = process_context.Event()
        release_remote = process_context.Event()
        reader_entered = process_context.Event()
        results = process_context.Queue()
        acquisition = process_context.Process(
            target=_setup_while_remote_is_gated,
            args=(self.root, acquire_snapshot, 0, required_sha, remote_entered, release_remote, results),
        )
        reader = process_context.Process(
            target=_setup_cached_reader,
            args=(self.root, cached_snapshot, 1, reader_entered, results),
        )
        processes = [acquisition, reader]
        with self.fixture_remote():
            acquisition.start()
            try:
                self.assertTrue(remote_entered.wait(timeout=10), "acquisition never reached the remote shim")
                reader.start()
                self.assertTrue(
                    reader_entered.wait(timeout=10),
                    "cached source reader did not progress while remote acquisition was blocked",
                )
                release_remote.set()
                rows = self._collect_fixture_results(results, 2)
            finally:
                self._finish_fixture_processes(processes, (release_remote,))
        self.assertEqual(
            {label: status for label, (status, _) in rows.items()},
            {"acquisition": "ok", "reader": "ok"},
            rows,
        )
        self.assertEqual(rows["reader"][1]["status"], "ready")
        reader_source = self.root / rows["reader"][1]["slot_path"] / self.module["path"]
        self.assertEqual(git(reader_source, "rev-parse", "HEAD"), warm_head)

    def test_source_import_waits_until_shared_source_reader_releases(self):
        with self.fixture_remote(), self.state.lock():
            self.history()
        process_context = multiprocessing.get_context("fork")
        reader_entered = process_context.Event()
        release_reader = process_context.Event()
        import_entered = process_context.Event()
        results = process_context.Queue()
        reader = process_context.Process(
            target=_hold_shared_source_reader,
            args=(self.root, self.module, self.nested, reader_entered, release_reader, results),
        )
        importer = process_context.Process(
            target=_run_exclusive_source_import,
            args=(self.root, self.module, self.nested, import_entered, results),
        )
        processes = [reader, importer]
        reader.start()
        try:
            self.assertTrue(reader_entered.wait(timeout=10), "reader never reached its Git critical stage")
            importer.start()
            self.assertFalse(
                import_entered.wait(timeout=1),
                "shared source import overlapped an active Git reader critical stage",
            )
            release_reader.set()
            self.assertTrue(import_entered.wait(timeout=10), "source import never progressed after reader release")
            rows = self._collect_fixture_results(results, 2)
        finally:
            self._finish_fixture_processes(processes, (release_reader,))
        self.assertEqual(
            {label: status for label, (status, _) in rows.items()},
            {"reader": "ok", "import": "ok"},
            rows,
        )
        self.assertEqual(rows["import"][1], "commit")

    def test_warm_highwater_new_tip_uses_incremental_fetch_without_losing_retained_history(self):
        with self.fixture_remote(), self.state.lock():
            self.history()
            context, log = self.shim()
            with context:
                first = common.bounded_fetch(
                    self.nested, "origin", [self.pr], depth=20, max_depth=1000
                )
                self.assertEqual(first.returncode, 0)
                cache = common.acquisition_cache_path(self.pool.store)
                state = json.loads((cache / "review-acquisition.json").read_text())
                highwater = state["depth_highwater"]
                self.assertGreater(highwater, 5)
                retained_source = self.edges()
                retained_cache = {
                    parts[0]: set(parts[1:])
                    for parts in (
                        line.split()
                        for line in git(cache, "rev-list", "--all", "--parents").splitlines()
                    )
                }
                shallow = cache / "shallow"
                boundaries = shallow.read_bytes() if shallow.exists() else b""
                call_count = len(log.read_text().splitlines())

                (self.source / "feature.txt").write_text("new incremental tip\n")
                git(self.source, "commit", "-am", "new incremental tip")
                new_tip = git(self.source, "rev-parse", "HEAD")
                second = common.bounded_fetch(
                    self.nested, "origin", [new_tip], depth=5, max_depth=1000
                )

            self.assertEqual(second.returncode, 0)
            self.assertEqual(git(self.nested, "cat-file", "-t", new_tip), "commit")
            later = [json.loads(line) for line in log.read_text().splitlines()[call_count:]]
            remote_fetches = [
                args
                for args in later
                if "fetch" in args
                and not any(value.startswith("file://") for value in args)
            ]
            self.assertEqual(len(remote_fetches), 1)
            self.assertFalse(
                any(value == f"--depth={highwater}" for value in remote_fetches[0]),
                "a warm cache must not reassert its old absolute high-water depth",
            )
            self.assertFalse(
                any(value.startswith("--depth=") for value in remote_fetches[0]),
                "the first warm-cache probe must preserve the existing shallow boundaries",
            )
            self.assertEqual(shallow.read_bytes() if shallow.exists() else b"", boundaries)
            source_after = self.edges()
            cache_after = {
                parts[0]: set(parts[1:])
                for parts in (
                    line.split()
                    for line in git(cache, "rev-list", "--all", "--parents").splitlines()
                )
            }
            for commit, parents in retained_source.items():
                self.assertLessEqual(parents, source_after[commit])
            for commit, parents in retained_cache.items():
                self.assertLessEqual(parents, cache_after[commit])

    def test_transient_warm_fetch_retries_once_after_verified_settlement(self):
        with self.fixture_remote(), self.state.lock():
            self.history()
            common.bounded_fetch(self.nested, "origin", [self.pr], depth=20, max_depth=1000)
            (self.source / "feature.txt").write_text("retry tip\n")
            git(self.source, "commit", "-am", "retry tip")
            new_tip = git(self.source, "rev-parse", "HEAD")
            context, log = self.shim()
            shim = Path(self.temp.name) / "bin" / "git"
            marker = Path(self.temp.name) / "transient-once"
            text = shim.read_text()
            text = text.replace(
                "if \"fetch\" in a and any(x.startswith(\"--deepen=\") for x in a) and not any(x.startswith(\"file://\") for x in a):",
                f'if "fetch" in a and not any(x.startswith("file://") for x in a) and not os.path.exists({str(marker)!r}):\n open({str(marker)!r},"w").write("1");sys.stderr.write("fatal: connection reset by peer\\n");sys.exit(128)\nif "fetch" in a and any(x.startswith("--deepen=") for x in a) and not any(x.startswith("file://") for x in a):',
            )
            shim.write_text(text)
            with context, common.fetch_session(timeout=20) as session:
                deadline = session.deadline
                result = common.bounded_fetch(
                    self.nested, "origin", [new_tip], depth=5, max_depth=1000
                )
                self.assertIsNotNone(session.deadline)
                if deadline is not None:
                    self.assertEqual(session.deadline, deadline)
            self.assertEqual(result.returncode, 0)
            calls = [json.loads(line) for line in log.read_text().splitlines()]
            remote = [
                args
                for args in calls
                if "fetch" in args
                and not any(value.startswith("file://") for value in args)
            ]
            self.assertEqual(len(remote), 2)
            state = json.loads(
                (common.acquisition_cache_path(self.pool.store) / "review-acquisition.json").read_text()
            )
            self.assertFalse(state.get("blocked"))
            self.assertNotIn("operation", state)

    def test_transient_retry_gets_no_fresh_budget_after_timeout_exhaustion(self):
        from subprocess import CompletedProcess

        with self.fixture_remote(), self.state.lock():
            self.history()
            original = common.run_git
            attempts = []

            def exhaust_after_first_transfer(repo, args, **kwargs):
                result = original(repo, args, **kwargs)
                if "fetch" in args and not any(
                    value.startswith("file://") for value in args
                ):
                    attempts.append(tuple(args))
                    common._FETCH_SESSION.get().deadline = 0
                    return CompletedProcess(
                        args, 128, result.stdout, "fatal: connection reset by peer"
                    )
                return result

            with common.fetch_session(timeout=20), patch.object(
                common, "run_git", side_effect=exhaust_after_first_transfer
            ), self.assertRaisesRegex(common.ReviewError, "shared operation timeout"):
                common.bounded_fetch(
                    self.nested, "origin", [self.pr], depth=5, max_depth=1000
                )

            self.assertEqual(len(attempts), 1)
            state = json.loads(
                (
                    common.acquisition_cache_path(self.pool.store)
                    / "review-acquisition.json"
                ).read_text()
            )
            self.assertFalse(state.get("blocked"))
            self.assertNotIn("operation", state)

    def test_unrelated_error_does_not_start_temporary_fetch(self):
        from subprocess import CompletedProcess
        with self.fixture_remote(),self.state.lock():
            self.history()
            original=common.run_git; calls=[]
            def reject(repo,args,**kwargs):
                if 'fetch' in args and not any(x.startswith('file://') for x in args):
                    calls.append(args)
                    return CompletedProcess(args,128,'','fatal: Authentication failed')
                return original(repo,args,**kwargs)
            with patch.object(common,'run_git',side_effect=reject):
                result=common.bounded_fetch(self.nested,'origin',[self.pr],depth=100,max_depth=1000)
            self.assertEqual(result.returncode,128);self.assertEqual(len(calls),1)
            self.assertFalse(list(self.pool.store.parent.glob('.bounded-fetch-*')))
