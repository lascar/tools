"""Suppression sur dépôts temporaires uniquement ; Docker et sudo simulés."""
import json
import os
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch
import test_worktrees
import _wt_remove as removal
import _wt_common as c


class RemoveTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_worktrees.WorktreeTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.main, self.root = self.fixture.main, self.fixture.root
        self.task = '1234_réglage'
        self.branch = 'feature/' + self.task
        self.scope = self.fixture.scope
        original_iterdir = Path.iterdir
        def test_processes(path):
            if path == Path('/proc'):
                children = Path('/proc/%s/task/%s/children' % (os.getpid(), os.getpid()))
                return iter(Path('/proc') / pid for pid in children.read_text().split())
            return original_iterdir(path)
        self.scope.enter_context(patch.object(Path, 'iterdir', test_processes))
        self.scope.enter_context(patch.object(removal, 'MAIN', self.main))
        self.scope.enter_context(patch.object(removal, 'STATE', self.main / 'doc/docker-worktrees'))
        # Le contexte appelant est master dans le dépôt temporaire.
        original = removal.context
        self.scope.enter_context(patch.object(removal, 'context', side_effect=lambda path=None, **kw: original(path or self.main, **kw)))
        self.scope.enter_context(patch.object(removal.runtime, 'app_mounts', return_value=[]))

    def test_removes_clean_worktree_and_ignored_content_without_archive(self):
        doc = self.root / 'doc'; doc.mkdir()
        (doc / 'notes.txt').write_text('notes privées')
        (doc / 'shared').symlink_to(self.main / 'tracked.txt')
        removal.remove(self.task)
        self.assertFalse(self.root.exists())
        self.assertEqual(self.fixture.git(self.main, 'rev-parse', self.branch), self.fixture.git(self.main, 'rev-parse', 'master'))
        self.assertFalse((self.main / 'doc/worktree-backups').exists())
        self.assertTrue((self.main / 'tracked.txt').exists())

    def test_dirty_worktree_is_preserved(self):
        (self.root / 'tracked.txt').write_text('travail')
        with self.assertRaisesRegex(c.ToolError, 'Modifications Git'):
            removal.remove(self.task)
        self.assertTrue(self.root.exists())

    def test_untracked_work_is_preserved(self):
        (self.root / 'unfinished.txt').write_text('local work')
        with self.assertRaisesRegex(c.ToolError, 'fichiers non suivis'):
            removal.remove(self.task)
        self.assertTrue((self.root / 'unfinished.txt').exists())
        self.assertFalse((self.main / 'doc/worktree-backups').exists())

    def test_docker_activity_blocks(self):
        with patch.object(removal.runtime, 'app_mounts', return_value=[('web', self.root, 'id', True)]):
            with self.assertRaisesRegex(c.ToolError, 'conteneur'):
                removal.remove(self.task)
        self.assertTrue(self.root.exists())

    def test_protected_process_uses_targeted_privileged_read(self):
        proc = self.main / '2052'; proc.mkdir()
        for directory in (self.main, self.root):
            with patch.object(Path, 'resolve', side_effect=PermissionError()), \
                    patch.object(removal, 'run', return_value=subprocess.CompletedProcess([], 0, str(directory) + '\n', '')) as read:
                self.assertEqual(removal.process_directory(proc), directory)
                read.assert_called_once_with(['sudo', 'readlink', '-e', '--', proc / 'cwd'], capture=True, check=False)

    def test_protected_process_failed_privileged_read_still_blocks(self):
        proc = self.main / '2052'; proc.mkdir()
        with patch.object(Path, 'resolve', side_effect=PermissionError()), \
                patch.object(removal, 'run', return_value=subprocess.CompletedProcess([], 1, '', 'sudo refusé')):
            with self.assertRaisesRegex(c.ToolError, 'sudo refusé'):
                removal.process_directory(proc)

    def test_protected_process_exiting_during_read_is_ignored(self):
        proc = self.main / '2052'
        with patch.object(Path, 'resolve', side_effect=PermissionError()), \
                patch.object(removal, 'run', return_value=subprocess.CompletedProcess([], 1, '', '')):
            self.assertIsNone(removal.process_directory(proc))

    def test_open_terminal_blocks(self):
        child = subprocess.Popen(['sleep', '30'], cwd=self.root)
        try:
            with self.assertRaisesRegex(c.ToolError, 'processus'):
                removal.remove(self.task)
        finally:
            child.terminate(); child.wait(timeout=3)

    def test_unsafe_name_is_refused(self):
        for task in ('../project', 'master', 'feature/1234_réglage'):
            with self.assertRaises(c.ToolError): removal.remove(task)

    def test_locked_worktree_is_preserved(self):
        self.fixture.git(self.main, 'worktree', 'lock', str(self.root))
        with self.assertRaisesRegex(c.ToolError, 'verrouillé'):
            removal.remove(self.task)

    def test_symlink_target_is_refused(self):
        moved = self.root.with_name('preserved')
        self.root.rename(moved); self.root.symlink_to(moved, target_is_directory=True)
        with self.assertRaisesRegex(c.ToolError, 'lien symbolique'):
            removal.remove(self.task)

    def test_residual_worktree_is_removed_without_archive(self):
        marker = (self.root / '.git').read_text()
        self.fixture.git(self.main, 'worktree', 'remove', str(self.root))
        self.root.mkdir(); (self.root / '.git').write_text(marker)
        (self.root / 'notes.txt').write_text('reste')
        removal.remove(self.task)
        self.assertFalse(self.root.exists())
        self.assertFalse((self.main / 'doc/worktree-backups').exists())

    def test_unidentified_residue_is_preserved(self):
        self.fixture.git(self.main, 'worktree', 'remove', str(self.root))
        self.root.mkdir()
        with self.assertRaisesRegex(c.ToolError, 'marqueur Git'):
            removal.remove(self.task)

    def test_inventory_failure_prevents_git_remove(self):
        with patch.object(removal, 'inventory', side_effect=OSError('lecture impossible')):
            with self.assertRaises(OSError): removal.remove(self.task)
        self.assertTrue(self.root.exists())
        self.assertIn(str(self.root), self.fixture.git(self.main, 'worktree', 'list'))

    def test_permission_error_uses_only_targeted_sudo_without_backup(self):
        marker = (self.root / '.git').read_text()
        self.fixture.git(self.main, 'worktree', 'remove', str(self.root))
        self.root.mkdir(); (self.root / '.git').write_text(marker)
        original = removal.shutil.rmtree
        def sudo(args, **kw):
            self.assertEqual(args, ['sudo', 'rm', '-r', '--', self.root])
            self.assertFalse((self.main / 'doc/worktree-backups').exists())
            original(self.root)
        with patch.object(removal.shutil, 'rmtree', side_effect=PermissionError()), patch.object(removal, 'run', side_effect=sudo) as call:
            removal.remove(self.task)
            call.assert_called_once()

    def test_source_changed_during_checks_is_preserved(self):
        original = removal.inventory
        def changing(*args):
            result = original(*args)
            (self.root / 'tracked.txt').write_text('modifié après inventaire')
            return result
        with patch.object(removal, 'inventory', side_effect=changing):
            with self.assertRaisesRegex(c.ToolError, 'changé'):
                removal.remove(self.task)
        self.assertTrue(self.root.exists())

    def test_git_refusal_keeps_registered_worktree(self):
        original = removal.git
        def refusing(root, *args, **kw):
            if args[:2] == ('worktree', 'remove'):
                return subprocess.CompletedProcess(args, 1, '', 'suppression refusée')
            return original(root, *args, **kw)
        with patch.object(removal, 'git', side_effect=refusing):
            with self.assertRaisesRegex(c.ToolError, 'Git refuse'):
                removal.remove(self.task)
        self.assertTrue(self.root.exists())
        self.assertIn(str(self.root), self.fixture.git(self.main, 'worktree', 'list'))

    def test_git_partial_removal_continues_with_verified_residue(self):
        original = removal.git
        def partial(root, *args, **kw):
            if args[:2] == ('worktree', 'remove'):
                metadata = Path((self.root / '.git').read_text().strip()[8:])
                removal.shutil.rmtree(metadata)
                return subprocess.CompletedProcess(args, 1, '', 'Permission denied')
            return original(root, *args, **kw)
        with patch.object(removal, 'git', side_effect=partial):
            removal.remove(self.task)
        self.assertFalse(self.root.exists())
        self.assertTrue(self.fixture.git(self.main, 'rev-parse', self.branch))
