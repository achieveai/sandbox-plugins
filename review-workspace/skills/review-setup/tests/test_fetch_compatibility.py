"""Actual Git with a remote-capability shim; all repositories disposable."""
import json
import os
from pathlib import Path
import shutil
import stat
import unittest
from unittest.mock import patch
import test_lifecycle
from test_lifecycle import git
import review_common as common
import review_setup as setup


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
            for a in remote:self.assertTrue(any(x.startswith(('--depth=','--deepen=')) for x in a))
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
                common.bounded_fetch(self.nested,'origin',[self.pr],depth=100,max_depth=100)
            self.assertEqual((self.pool.store/'shallow').read_bytes(),before)
            self.assertFalse(list(self.pool.store.parent.glob('.bounded-fetch-*')))

    def test_unproven_cleanup_preserves_only_owned_temporary_store(self):
        from subprocess import CompletedProcess
        with self.fixture_remote(),self.state.lock():
            self.history();original=common.run_git
            def fail(repo,args,**kwargs):
                if 'fetch' in args and any(x.startswith('--deepen=') for x in args):
                    return CompletedProcess(args,128,'','fatal: Server does not support --deepen')
                if 'fetch' in args and any(x.startswith('--depth=') for x in args):
                    raise common.CleanupNotProvenError('cleanup not proven')
                return original(repo,args,**kwargs)
            with patch.object(common,'run_git',side_effect=fail),self.assertRaises(common.CleanupNotProvenError):
                common.bounded_fetch(self.nested,'origin',[self.pr],depth=100,max_depth=1000)
            self.assertEqual(len(list(self.pool.store.parent.glob('.bounded-fetch-*'))),1)
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
            acquisitions=[json.loads(line) for line in log.read_text().splitlines() if '--depth=' in line]
            self.assertTrue(any(any(x.startswith(self.main+':refs/heads/acquire-') for x in args) for args in acquisitions))
            self.assertEqual(git(self.nested,'merge-base','--is-ancestor',self.sentinel,self.main),'')

    def test_unrelated_error_does_not_start_temporary_fetch(self):
        from subprocess import CompletedProcess
        with self.fixture_remote(),self.state.lock():
            self.history()
            original=common.run_git; calls=[]
            def reject(repo,args,**kwargs):
                if 'fetch' in args:
                    calls.append(args)
                    return CompletedProcess(args,128,'','fatal: Authentication failed')
                return original(repo,args,**kwargs)
            with patch.object(common,'run_git',side_effect=reject):
                result=common.bounded_fetch(self.nested,'origin',[self.pr],depth=100,max_depth=1000)
            self.assertEqual(result.returncode,128);self.assertEqual(len(calls),1)
            self.assertFalse(list(self.pool.store.parent.glob('.bounded-fetch-*')))


class CollectionDeepeningBudgetTests(unittest.TestCase):
    """Provenance deepening is best effort: a history-budget refusal ends it as partial, not failed."""

    def report(self):
        return {"files": [{"path": "a.txt", "provenance_status": "partial-shallow-history"}],
                "warnings": [], "complete": True}

    def test_history_budget_refusal_ends_deepening_with_partial_provenance(self):
        calls = []

        def deepen(*args, **kwargs):
            calls.append(kwargs.get("depth"))
            raise common.HistoryBudgetError("Server lacks relative-depth fetch and absolute acquisition exceeds the history budget.")

        with patch("review_context.collect_context", side_effect=lambda *a, **k: self.report()), \
             patch("review_setup.run_git") as run_git, \
             patch("review_setup._deepen_history", side_effect=deepen):
            run_git.return_value.stdout = "true\n"
            report = setup._collect(None, {}, Path("."), "base", max_depth=1000000, max_work_items=1, max_commits=1)
        self.assertEqual(calls, [100])
        self.assertFalse(report["complete"])
        self.assertEqual(report["files"][0]["provenance_status"], "partial-shallow-history")
        self.assertTrue(any("history budget" in warning for warning in report["warnings"]), report["warnings"])

    def test_other_deepening_refusals_still_fail_collection(self):
        with patch("review_context.collect_context", side_effect=lambda *a, **k: self.report()), \
             patch("review_setup.run_git") as run_git, \
             patch("review_setup._deepen_history", side_effect=common.ReviewError("History deepening was requested for a complete repository.")):
            run_git.return_value.stdout = "true\n"
            with self.assertRaisesRegex(common.ReviewError, "complete repository"):
                setup._collect(None, {}, Path("."), "base", max_depth=1000000, max_work_items=1, max_commits=1)

    def test_budget_refusals_are_history_budget_errors(self):
        self.assertTrue(issubclass(common.HistoryBudgetError, common.ReviewError))
