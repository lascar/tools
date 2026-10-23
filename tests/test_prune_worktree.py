"""Tests du prune sur dépôts temporaires ; aucune suppression du projet réel."""
import contextlib
import io
import os
from pathlib import Path
import time
import unittest
from unittest.mock import patch

import test_remove_worktree
import _wt_prune as prune


class PruneTests(unittest.TestCase):
    def setUp(self):
        fixture = self.fixture = test_remove_worktree.RemoveTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.root, self.main = fixture.root, fixture.main
        scope = fixture.scope
        scope.enter_context(patch.object(prune, 'MAIN', self.main))
        scope.enter_context(patch.object(prune, 'context', return_value=(self.main, 'master', 'master')))
        scope.enter_context(patch.dict(os.environ, {'GIT_OPTIONAL_LOCKS': '0'}))
        self.output = io.StringIO()
        scope.enter_context(contextlib.redirect_stdout(self.output))

    def age(self):
        old = time.time() - 30 * 86400
        admin = Path(prune.value(self.root, 'rev-parse', '--path-format=absolute', '--git-dir'))
        for base in (self.root, admin):
            for directory, dirs, files in os.walk(base):
                for name in files + dirs:
                    os.utime(Path(directory) / name, (old, old), follow_symlinks=False)
            os.utime(base, (old, old))

    def test_default_removes_old_clean_worktree_and_keeps_branch(self):
        self.age()
        self.assertEqual(prune.prune(), 0)
        self.assertFalse(self.root.exists())
        self.fixture.fixture.git(self.main, 'rev-parse', self.fixture.branch)
        self.assertFalse((self.main / 'doc/worktree-backups').exists())

    def test_ignored_files_removed_without_backup_and_shared_target_kept(self):
        doc = self.root / 'doc'
        doc.mkdir()
        (doc / 'ignored.txt').write_text('local only')
        (doc / 'shared').symlink_to(self.main / 'tracked.txt')
        self.age()
        with patch.object(prune.removal, 'backup', side_effect=AssertionError('archive interdite')):
            self.assertEqual(prune.prune(), 0)
        self.assertFalse(self.root.exists())
        self.assertTrue((self.main / 'tracked.txt').exists())
        self.assertNotIn(str(self.root), self.fixture.fixture.git(self.main, 'worktree', 'list', '--porcelain'))
        self.assertFalse((self.main / 'doc/worktree-backups').exists())

    def test_untracked_files_are_preserved_without_backup(self):
        (self.root / 'unfinished.txt').write_text('local work')
        self.age()
        self.assertEqual(prune.prune(), 0)
        self.assertTrue((self.root / 'unfinished.txt').exists())
        self.assertIn('CONSERVÉ', self.output.getvalue())

    def test_preview_does_not_archive_or_remove(self):
        self.age()
        self.assertEqual(prune.prune(dry_run=True), 0)
        self.assertIn('CANDIDAT', self.output.getvalue())
        self.assertTrue(self.root.exists())
        self.assertFalse((self.main / 'doc/worktree-backups').exists())

    def test_recent_checkout_is_kept(self):
        prune.prune()
        self.assertTrue(self.root.exists())
        self.assertNotIn('SUPPRESSION', self.output.getvalue())

    def test_custom_threshold(self):
        self.age()
        prune.prune(40)
        self.assertTrue(self.root.exists())

    def test_old_dirty_worktree_is_kept(self):
        (self.root / 'tracked.txt').write_text('travail non enregistré')
        self.age()
        prune.prune()
        self.assertTrue(self.root.exists())
        self.assertIn('Modifications Git', self.output.getvalue())

    def test_recent_central_journal_is_kept(self):
        self.age()
        journal = self.main / prune.setting('JOURNAL_DIR', 'sessions_codex') / (self.fixture.task + '.jsont')
        journal.parent.mkdir(parents=True, exist_ok=True)
        journal.write_text('{}')
        prune.prune()
        self.assertTrue(self.root.exists())

    def test_shared_doc_target_does_not_count_as_activity(self):
        doc = self.main / 'doc'; doc.mkdir(exist_ok=True)
        (self.root / 'doc').symlink_to(doc, target_is_directory=True)
        with (self.main / '.git/info/exclude').open('a') as stream:
            stream.write('\n/doc\n')
        self.age()
        (doc / 'recent.txt').write_text('activité dans une autre tâche')
        prune.prune(dry_run=True)
        self.assertIn('CANDIDAT', self.output.getvalue())

    def test_current_worktree_is_kept(self):
        self.age()
        with patch.object(prune, 'context', return_value=(self.root, self.fixture.branch, self.fixture.task)):
            prune.prune()
        self.assertTrue(self.root.exists())

    def test_locked_worktree_is_kept(self):
        self.age()
        self.fixture.fixture.git(self.main, 'worktree', 'lock', str(self.root))
        prune.prune()
        self.assertTrue(self.root.exists())
        self.assertIn('verrouillé', self.output.getvalue())

    def test_active_runtime_is_kept(self):
        self.age()
        with patch.object(prune.removal.runtime, 'app_mounts', return_value=[('web', self.root, 'id', True)]):
            prune.prune()
        self.assertTrue(self.root.exists())
        self.assertIn('conteneur', self.output.getvalue())

    def test_activity_during_checks_prevents_removal(self):
        self.age()
        original = prune.removal.inventory
        def activity(*args):
            saved = original(*args)
            admin = Path(prune.value(self.root, 'rev-parse', '--path-format=absolute', '--git-dir'))
            os.utime(admin / 'HEAD', None)
            return saved
        with patch.object(prune.removal, 'inventory', side_effect=activity), contextlib.redirect_stderr(self.output):
            self.assertEqual(prune.prune(), 1)
        self.assertTrue(self.root.exists())

    def test_invalid_days_are_refused(self):
        for days in (0, -2):
            with self.assertRaises(prune.ToolError): prune.prune(days)


if __name__ == '__main__':
    unittest.main()
