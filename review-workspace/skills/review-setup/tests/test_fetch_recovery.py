"""Regression tests for failed immutable-fetch recovery; local Git only."""
import subprocess
import unittest
from unittest.mock import patch

import test_lifecycle
from test_lifecycle import git
import review_common as common
import review_setup as setup


class FetchRecoveryTests(unittest.TestCase):
    setUp = test_lifecycle.WarmPoolTests.setUp
    fixture_remote = test_lifecycle.WarmPoolTests.fixture_remote

    def _failed_fetch_reservation(self):
        manifest = setup._reserve_pool_run(self.state, self.snapshot, self.module, 2)
        with patch.object(setup, '_fetch_exact_objects', side_effect=common.ReviewError('transport failure')):
            with self.assertRaises(common.ReviewError):
                setup.prepare_source(self.state, manifest)
        return self.state.load_pool_run('repos-nova', 2)

    def test_retry_fetch_phase_keeps_owned_branch(self):
        with self.fixture_remote(), self.state.lock():
            old = self._failed_fetch_reservation()
            self.assertEqual(old['phase'], 'fetch-intent')
            retry = setup._reserve_pool_run(self.state, self.snapshot, self.module, 2)
            self.assertEqual(retry['run_id'], old['run_id'])
            source, base = setup.prepare_source(self.state, retry)
            self.assertEqual(git(source, 'rev-parse', 'HEAD'), self.head)
            self.assertEqual(base, self.base)

    def test_prior_bug_warm_phase_recovers_exact_owned_branch(self):
        with self.fixture_remote(), self.state.lock():
            old = self._failed_fetch_reservation()
            old['phase'] = 'warm-intent'
            self.state.save(old)
            retry = setup._reserve_pool_run(self.state, self.snapshot, self.module, 2)
            self.assertEqual(retry['run_id'], old['run_id'])
            self.assertEqual(retry['phase'], 'reserved')
            self.assertEqual(git(self.root / retry['slot_path'], 'branch', '--show-current'), old['branch'])

    def test_warm_phase_refuses_foreign_branch_without_state_change(self):
        with self.fixture_remote(), self.state.lock():
            old = self._failed_fetch_reservation()
            old['phase'] = 'warm-intent'
            self.state.save(old)
            outer = self.root / old['slot_path']
            git(outer, 'checkout', '-b', 'foreign-review')
            before = self.state.pool_run_path('repos-nova', 2).read_bytes()
            with self.assertRaises(common.ReviewError):
                setup._reserve_pool_run(self.state, self.snapshot, self.module, 2)
            self.assertEqual(self.state.pool_run_path('repos-nova', 2).read_bytes(), before)
            self.assertEqual(git(outer, 'branch', '--show-current'), 'foreign-review')

    def test_fetch_retry_refuses_changed_owned_branch(self):
        with self.fixture_remote(), self.state.lock():
            old = self._failed_fetch_reservation()
            outer = self.root / old['slot_path']
            git(outer, 'commit', '--allow-empty', '-m', 'unrelated user commit')
            before = self.state.pool_run_path('repos-nova', 2).read_bytes()
            with self.assertRaisesRegex(common.ReviewError, 'Review ref changed'):
                setup._reserve_pool_run(self.state, self.snapshot, self.module, 2)
            self.assertEqual(self.state.pool_run_path('repos-nova', 2).read_bytes(), before)

    def test_archive_in_progress_is_not_restarted_by_setup(self):
        with self.fixture_remote(), self.state.lock():
            old = self._failed_fetch_reservation()
            old.update(phase='warm-intent', reset_mode='archive', reset_phase='validated')
            self.state.save(old)
            before = self.state.pool_run_path('repos-nova', 2).read_bytes()
            with self.assertRaisesRegex(common.ReviewError, 'archive.*validated'):
                setup._reserve_pool_run(self.state, self.snapshot, self.module, 2)
            self.assertEqual(self.state.pool_run_path('repos-nova', 2).read_bytes(), before)

    def test_fetch_error_names_missing_role_and_safe_failure_category(self):
        snapshot = dict(self.snapshot, head_sha='f' * 40)
        original = common.run_git
        def failed_fetch(source, args, **kwargs):
            if 'fetch' in args and not any(value.startswith('file://') for value in args):
                return subprocess.CompletedProcess(args, 128, '', "fatal: Authentication failed for 'https://user:SECRET@dev.azure.com/org/repo?token=PRIVATE' Authorization: Bearer CREDENTIAL")
            return original(source, args, **kwargs)
        with self.fixture_remote(), self.state.lock():
            pool = common.SourcePool(self.state, self.module)
            pool.warm([2])
            source = self.root / '.worktrees/Nova-2/repos/Nova'
            with patch.object(common, 'run_git', side_effect=failed_fetch):
                with self.assertRaises(common.ReviewError) as raised:
                    setup._fetch_exact_objects(source, snapshot, depth=100)
            message = str(raised.exception)
            self.assertIn('head=' + 'f' * 40, message)
            self.assertIn('authentication', message)
            self.assertIn('128', message)
            for sensitive in ['SECRET','PRIVATE','CREDENTIAL','https://','Authorization']:
                self.assertNotIn(sensitive, message)
