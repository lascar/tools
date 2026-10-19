"""Tests isolés : aucun accès à Docker, GNOME, Codex ou aux branches réelles."""
import contextlib
import importlib
import json
import os
import pty
import select
import signal
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch, Mock

TOOLS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS))
import _wt_common as c
import _wt_patch as backup
import _wt_docker as docker
import _wt_sessions as sessions
import _wt_atelier as atelier
import worktree_tools as cli
import session_journal as journal


class WorktreeTests(unittest.TestCase):
    def master_remote(self, filename='upstream.txt'):
        upstream = Path(self.temp.name) / 'upstream'
        subprocess.run(['git', 'clone', '-q', str(self.main), str(upstream)], check=True)
        self.git(upstream, 'config', 'user.name', 'Fixture')
        self.git(upstream, 'config', 'user.email', 'fixture@example.invalid')
        (upstream / filename).parent.mkdir(parents=True, exist_ok=True)
        (upstream / filename).write_text('upstream change\n')
        self.git(upstream, 'add', '-f', filename)
        self.git(upstream, 'commit', '-m', 'upstream change')
        self.git(self.root, 'remote', 'add', 'origin', str(upstream))
        return self.git(upstream, 'rev-parse', 'HEAD')

    def test_open_updates_task_and_local_master_from_remote(self):
        target = self.master_remote()
        args = cli.parser().parse_args(['worktree_open', '1234_réglage', '--prepare-only'])
        with patch.object(cli, 'context', wraps=cli.context) as context_mock, \
                patch.object(sessions, 'prepare') as prepare, patch.object(atelier, 'open_atelier') as opened:
            # L'appel sans chemin vient normalement du shell de l'utilisateur.
            original = context_mock._mock_wraps
            context_mock.side_effect = lambda path=None, **kw: original(path or self.main, **kw)
            cli.open_command(args)
        self.assertEqual(self.git(self.root, 'rev-parse', 'HEAD'), target)
        self.assertEqual(self.git(self.main, 'rev-parse', 'master'), target)
        self.assertEqual((self.main / 'upstream.txt').read_text(), 'upstream change\n')
        self.assertEqual(self.git(self.main, 'branch', '--show-current'), 'master')
        prepare.assert_called_once()
        opened.assert_not_called()

    def test_update_merges_divergent_branch(self):
        target = self.master_remote()
        self.git(self.root, 'commit', '--allow-empty', '-m', 'task change')
        previous = self.git(self.root, 'rev-parse', 'HEAD')
        cli.update_from_master(self.root)
        self.assertEqual(self.git(self.root, 'show', '-s', '--format=%P', 'HEAD').split(), [previous, target])

    def test_update_refuses_local_changes_before_fetch(self):
        (self.root / 'tracked.txt').write_text('unfinished\n')
        with patch.object(cli, 'run') as run, patch.object(cli.os, 'isatty', return_value=False):
            with self.assertRaisesRegex(c.ToolError, 'Modifications locales'):
                cli.update_from_master(self.root)
        run.assert_not_called()
        self.assertEqual((self.root / 'tracked.txt').read_text(), 'unfinished\n')

    def dirty_work(self):
        (self.root / 'tracked.txt').write_text('staged\n')
        self.git(self.root, 'add', 'tracked.txt')
        (self.root / 'tracked.txt').write_text('unstaged\n')
        (self.root / 'delete.txt').unlink()
        (self.root / 'added.txt').write_text('new staged\n')
        self.git(self.root, 'add', 'added.txt')
        (self.root / 'new.txt').write_text('new untracked\n')
        (self.root / 'doc').mkdir(exist_ok=True)
        (self.root / 'doc/notes.txt').write_text('personal\n')
        (self.root / '.env').write_text('private fixture\n')

    def interactive_update(self, answer):
        with patch.object(cli.os, 'isatty', return_value=True), patch('builtins.input', return_value=answer):
            return cli.update_from_master(self.root)

    def test_dirty_merge_preserves_work_and_updates_local_master(self):
        target = self.master_remote()
        self.dirty_work()
        staged = backup.diff_bytes(self.root, True)
        working = backup.diff_bytes(self.root)
        self.interactive_update('1')
        self.assertEqual(self.git(self.root, 'rev-parse', 'HEAD'), target)
        self.assertEqual(self.git(self.main, 'rev-parse', 'master'), target)
        self.assertEqual(backup.diff_bytes(self.root, True), staged)
        self.assertEqual(backup.diff_bytes(self.root), working)
        self.assertEqual((self.root / 'new.txt').read_text(), 'new untracked\n')
        self.assertEqual((self.root / 'doc/notes.txt').read_text(), 'personal\n')
        self.assertEqual((self.root / '.env').read_text(), 'private fixture\n')
        self.assertEqual(self.git(self.root, 'stash', 'list'), '')

    def test_dirty_merge_preserves_existing_stash(self):
        (self.root / 'tracked.txt').write_text('older stash\n')
        self.git(self.root, 'stash', 'push', '-m', 'existing')
        previous = self.git(self.root, 'rev-parse', 'refs/stash')
        self.master_remote()
        (self.root / 'tracked.txt').write_text('current work\n')
        self.interactive_update('1')
        self.assertEqual(self.git(self.root, 'rev-parse', 'refs/stash'), previous)
        self.assertEqual((self.root / 'tracked.txt').read_text(), 'current work\n')

    def test_dirty_merge_already_updated_keeps_changes_without_stash(self):
        self.master_remote()
        cli.update_from_master(self.root)
        self.dirty_work()
        snapshot = cli.changes_snapshot(self.root)
        self.interactive_update('1')
        self.assertEqual(cli.changes_snapshot(self.root), snapshot)
        self.assertEqual(self.git(self.root, 'stash', 'list'), '')

    def test_dirty_merge_fetch_failure_keeps_local_work(self):
        self.dirty_work()
        snapshot = cli.changes_snapshot(self.root)
        with self.assertRaises(c.ToolError):
            self.interactive_update('1')
        self.assertEqual(cli.changes_snapshot(self.root), snapshot)
        self.assertEqual(self.git(self.root, 'stash', 'list'), '')

    def test_dirty_merge_conflict_keeps_stash_and_does_not_launch(self):
        self.master_remote('tracked.txt')
        (self.root / 'tracked.txt').write_text('task commit\n')
        self.git(self.root, 'add', 'tracked.txt')
        self.git(self.root, 'commit', '-m', 'task commit')
        (self.root / 'new.txt').write_text('work not committed\n')
        with self.assertRaisesRegex(c.ToolError, 'Changements locaux conservés dans le stash'):
            self.interactive_update('1')
        self.assertIn('UU tracked.txt', self.git(self.root, 'status', '--porcelain'))
        stash = self.git(self.root, 'rev-parse', 'refs/stash')
        self.assertEqual(self.git(self.root, 'show', stash + '^3:new.txt'), 'work not committed')

    def test_dirty_merge_reapply_conflict_keeps_stash(self):
        target = self.master_remote('tracked.txt')
        self.configure_fixture_mergetool('exit 1')
        (self.root / 'tracked.txt').write_text('local work\n')
        with self.assertRaisesRegex(c.ToolError, 'Réapplication interrompue'):
            self.interactive_update('1')
        self.assertEqual(self.git(self.root, 'rev-parse', 'HEAD'), target)
        self.assertIn('UU tracked.txt', self.git(self.root, 'status', '--porcelain'))
        stash = self.git(self.root, 'rev-parse', 'refs/stash')
        self.assertEqual(self.git(self.root, 'show', stash + ':tracked.txt'), 'local work')

    def configure_fixture_mergetool(self, command='printf "resolved\\n" > "$MERGED"'):
        self.git(self.root, 'config', 'merge.tool', 'fixture')
        self.git(self.root, 'config', 'mergetool.fixture.cmd', command)
        self.git(self.root, 'config', 'mergetool.fixture.trustExitCode', 'true')
        self.git(self.root, 'config', 'mergetool.keepBackup', 'false')

    def existing_stash_conflict(self):
        self.master_remote('tracked.txt')
        (self.root / 'tracked.txt').write_text('local work\n')
        with patch.object(cli.os, 'isatty', return_value=False):
            # Aucun menu possible sans TTY ; preparer la fusion propre d'abord.
            self.git(self.root, 'stash', 'push', '-m', 'fixture saved work')
            cli.update_from_master(self.root)
        stash = self.git(self.root, 'rev-parse', 'refs/stash')
        result = subprocess.run(['git', '-C', str(self.root), 'stash', 'apply', '--index', stash],
                                capture_output=True, text=True, check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('UU tracked.txt', self.git(self.root, 'status', '--porcelain'))
        return stash

    def test_mergetool_resolves_stash_conflict_and_open_continues(self):
        target = self.master_remote('tracked.txt')
        (self.root / 'tracked.txt').write_text('local work\n')
        self.configure_fixture_mergetool()
        args = cli.parser().parse_args(['worktree_open', '1234_réglage'])
        original = cli.context
        with patch.object(cli, 'context', side_effect=lambda path=None, **kw: original(path or self.main, **kw)), \
                patch.object(cli.os, 'isatty', return_value=True), patch('builtins.input', return_value='1'), \
                patch.object(atelier, 'preflight'), patch.object(sessions, 'prepare') as prepare, \
                patch.object(atelier, 'open_atelier') as opened:
            cli.open_command(args)
        self.assertEqual(self.git(self.root, 'rev-parse', 'HEAD'), target)
        self.assertEqual((self.root / 'tracked.txt').read_text(), 'resolved\n')
        self.assertEqual(self.git(self.root, 'diff', '--name-only', '--diff-filter=U'), '')
        stash = self.git(self.root, 'rev-parse', 'refs/stash')
        self.assertEqual(self.git(self.root, 'show', stash + ':tracked.txt'), 'local work')
        prepare.assert_called_once_with(self.root, new=False)
        opened.assert_called_once_with(self.root, 'http')

    def test_existing_stash_conflict_resumes_without_fetch_or_second_apply(self):
        stash = self.existing_stash_conflict()
        self.configure_fixture_mergetool()
        args = cli.parser().parse_args(['worktree_open', '1234_réglage', '--prepare-only'])
        original = cli.context
        with patch.object(cli, 'context', side_effect=lambda path=None, **kw: original(path or self.main, **kw)), \
                patch.object(cli.os, 'isatty', return_value=True), patch('builtins.input') as choice, \
                patch.object(cli, 'refresh_master') as refresh, patch.object(cli, 'run', wraps=cli.run) as run, \
                patch.object(sessions, 'prepare') as prepare:
            cli.open_command(args)
        choice.assert_not_called()
        refresh.assert_not_called()
        calls = [call.args[0] for call in run.call_args_list]
        self.assertEqual(len(calls), 1)
        self.assertIn('mergetool', calls[0])
        self.assertEqual(run.call_args.kwargs['cwd'], self.root)
        self.assertEqual((self.root / 'tracked.txt').read_text(), 'resolved\n')
        self.assertEqual(self.git(self.root, 'rev-parse', 'refs/stash'), stash)
        prepare.assert_called_once_with(self.root, new=False)

    def test_failed_mergetool_keeps_conflict_and_stash(self):
        stash = self.existing_stash_conflict()
        self.configure_fixture_mergetool('exit 1')
        with patch.object(cli.os, 'isatty', return_value=True):
            with self.assertRaisesRegex(c.ToolError, 'Résolution interrompue'):
                cli.update_from_master(self.root)
        self.assertIn('UU tracked.txt', self.git(self.root, 'status', '--porcelain'))
        self.assertEqual(self.git(self.root, 'rev-parse', 'refs/stash'), stash)

    def test_mergetool_success_code_does_not_hide_remaining_conflicts(self):
        stash = self.existing_stash_conflict()
        with patch.object(cli.os, 'isatty', return_value=True), patch.object(cli, 'run', return_value=Mock(returncode=0)):
            with self.assertRaisesRegex(c.ToolError, 'conflits encore présents'):
                cli.update_from_master(self.root)
        self.assertIn('UU tracked.txt', self.git(self.root, 'status', '--porcelain'))
        self.assertEqual(self.git(self.root, 'rev-parse', 'refs/stash'), stash)

    def test_existing_stash_conflict_without_terminal_keeps_work(self):
        stash = self.existing_stash_conflict()
        before = (self.root / 'tracked.txt').read_text()
        with patch.object(cli.os, 'isatty', return_value=False), patch.object(cli, 'run') as run:
            with self.assertRaisesRegex(c.ToolError, 'terminal pour lancer git mergetool'):
                cli.update_from_master(self.root)
        run.assert_not_called()
        self.assertEqual((self.root / 'tracked.txt').read_text(), before)
        self.assertEqual(self.git(self.root, 'rev-parse', 'refs/stash'), stash)

    def test_stash_failure_without_unmerged_paths_does_not_launch_mergetool(self):
        before = self.git(self.root, 'rev-parse', 'HEAD')
        with patch.object(cli, 'run', return_value=Mock(returncode=1)) as run:
            with self.assertRaisesRegex(c.ToolError, 'Aucun conflit de fichier'):
                cli.restore_open_stash(self.root, 'fixture-stash')
        run.assert_called_once()
        self.assertEqual(self.git(self.root, 'rev-parse', 'HEAD'), before)

    def test_dirty_patch_cleans_only_saved_work_and_can_restore(self):
        master = self.git(self.main, 'rev-parse', 'master')
        self.dirty_work()
        snapshot = cli.changes_snapshot(self.root)
        # Aucun remote : le choix patch ne doit pas lancer de fetch ou de fusion.
        self.assertEqual(self.interactive_update('2'), 'patch')
        self.assertEqual(self.git(self.root, 'status', '--porcelain'), '')
        self.assertEqual(self.git(self.main, 'rev-parse', 'master'), master)
        self.assertEqual((self.root / 'doc/notes.txt').read_text(), 'personal\n')
        self.assertEqual((self.root / '.env').read_text(), 'private fixture\n')
        archive, = backup.pending(self.root)
        backup.restore(self.root, archive, yes=True)
        self.assertEqual(cli.changes_snapshot(self.root), snapshot)

    def test_dirty_patch_failure_never_cleans_work(self):
        self.dirty_work()
        snapshot = cli.changes_snapshot(self.root)
        with patch.object(backup, 'make', side_effect=c.ToolError('disk full')):
            with self.assertRaisesRegex(c.ToolError, 'disk full'):
                self.interactive_update('2')
        self.assertEqual(cli.changes_snapshot(self.root), snapshot)

    def test_dirty_discard_cleans_index_and_new_files_preserves_personal(self):
        master = self.git(self.main, 'rev-parse', 'master')
        self.dirty_work()
        self.assertEqual(self.interactive_update('3'), 'discard')
        self.assertEqual(self.git(self.root, 'status', '--porcelain'), '')
        self.assertEqual((self.root / 'tracked.txt').read_text(), 'initial\n')
        self.assertTrue((self.root / 'delete.txt').exists())
        self.assertFalse((self.root / 'added.txt').exists())
        self.assertFalse((self.root / 'new.txt').exists())
        self.assertEqual((self.root / 'doc/notes.txt').read_text(), 'personal\n')
        self.assertEqual((self.root / '.env').read_text(), 'private fixture\n')
        self.assertEqual(self.git(self.main, 'rev-parse', 'master'), master)
        self.assertEqual(backup.pending(self.root), [])

    def test_empty_choice_and_eof_cancel_without_changes(self):
        self.dirty_work()
        snapshot = cli.changes_snapshot(self.root)
        for response in ('', '0', EOFError(), KeyboardInterrupt()):
            with self.subTest(response=repr(response)), \
                    patch.object(cli.os, 'isatty', return_value=True), \
                    patch('builtins.input', **({'side_effect': response} if isinstance(response, BaseException) else {'return_value': response})):
                with self.assertRaisesRegex(c.ToolError, 'Ouverture annulée'):
                    cli.update_from_master(self.root)
            self.assertEqual(cli.changes_snapshot(self.root), snapshot)

    def test_invalid_choice_is_reprompted(self):
        (self.root / 'tracked.txt').write_text('local\n')
        with patch.object(cli.os, 'isatty', return_value=True), patch('builtins.input', side_effect=['wrong', '2']) as prompt:
            self.assertEqual(cli.update_from_master(self.root), 'patch')
        self.assertEqual(prompt.call_count, 2)

    def test_changes_during_choice_cancel_cleanup(self):
        (self.root / 'tracked.txt').write_text('before\n')
        def choose(_):
            (self.root / 'tracked.txt').write_text('new editing\n')
            return '3'
        with patch.object(cli.os, 'isatty', return_value=True), patch('builtins.input', side_effect=choose):
            with self.assertRaisesRegex(c.ToolError, 'changé pendant le choix'):
                cli.update_from_master(self.root)
        self.assertEqual((self.root / 'tracked.txt').read_text(), 'new editing\n')

    def test_changes_during_backup_cancel_cleanup(self):
        (self.root / 'tracked.txt').write_text('before\n')
        original = backup.make
        def save(root):
            archive = original(root)
            (root / 'tracked.txt').write_text('edited during backup\n')
            return archive
        with patch.object(backup, 'make', side_effect=save):
            with self.assertRaisesRegex(c.ToolError, 'changé avant le nettoyage'):
                self.interactive_update('2')
        self.assertEqual((self.root / 'tracked.txt').read_text(), 'edited during backup\n')
        self.assertEqual(len(backup.pending(self.root)), 1)

    def test_dirty_work_collision_blocks_cleanup(self):
        (self.root / 'tracked.txt').write_text('task work\n')
        (self.main / 'tracked.txt').write_text('other work\n')
        with self.assertRaisesRegex(c.ToolError, 'autre worktree'):
            self.interactive_update('3')
        self.assertEqual((self.root / 'tracked.txt').read_text(), 'task work\n')
        self.assertEqual((self.main / 'tracked.txt').read_text(), 'other work\n')

    def test_patch_name_targets_existing_worktree_from_main(self):
        (self.root / 'tracked.txt').write_text('save named task\n')
        original = cli.context
        with patch.object(sys, 'argv', ['tools', 'patch', '1234_réglage']), \
                patch.object(cli, 'context', side_effect=lambda path=None, **kw: original(path or self.main, **kw)):
            cli.main()
        archive, = backup.pending(self.root)
        manifest, _, _ = backup.inspect_archive(archive)
        self.assertEqual(manifest['branch'], 'feature/1234_réglage')
        self.assertEqual((self.root / 'tracked.txt').read_text(), 'save named task\n')
        with self.assertRaises(c.ToolError):
            cli.existing_worktree('9999_missing')

    def test_open_patch_choice_does_not_immediately_offer_restore(self):
        (self.root / 'tracked.txt').write_text('save for later\n')
        args = cli.parser().parse_args(['worktree_open', '1234_réglage'])
        original = cli.context
        with patch.object(cli, 'context', side_effect=lambda path=None, **kw: original(path or self.main, **kw)), \
                patch.object(cli.os, 'isatty', return_value=True), patch('builtins.input', return_value='2'), \
                patch.object(atelier, 'preflight'), patch.object(sessions, 'prepare'), \
                patch.object(atelier, 'open_atelier') as opened:
            cli.open_command(args)
        opened.assert_called_once_with(self.root, 'http', offer_patches=False)
        self.assertEqual(self.git(self.root, 'status', '--porcelain'), '')

    def test_personal_tracked_changes_refuse_cleanup(self):
        (self.root / 'doc').mkdir(exist_ok=True)
        (self.root / 'doc/notes.txt').write_text('personal\n')
        self.git(self.root, 'add', '-f', 'doc/notes.txt')
        with patch('builtins.input') as prompt:
            with self.assertRaisesRegex(c.ToolError, 'Fichiers personnels'):
                cli.update_from_master(self.root)
        prompt.assert_not_called()
        self.assertEqual((self.root / 'doc/notes.txt').read_text(), 'personal\n')

    def test_update_leaves_conflicts_for_resolution(self):
        self.master_remote('tracked.txt')
        (self.root / 'tracked.txt').write_text('task change\n')
        self.git(self.root, 'add', 'tracked.txt')
        self.git(self.root, 'commit', '-m', 'task change')
        with self.assertRaisesRegex(c.ToolError, 'Fusion interrompue'):
            cli.update_from_master(self.root)
        self.assertIn('UU tracked.txt', self.git(self.root, 'status', '--porcelain'))
        with self.assertRaisesRegex(c.ToolError, 'Opération Git en cours'):
            cli.update_from_master(self.root)

    def test_update_refuses_collision_in_other_worktree(self):
        self.master_remote('tracked.txt')
        (self.main / 'tracked.txt').write_text('other work in progress\n')
        before = self.git(self.root, 'rev-parse', 'HEAD')
        with self.assertRaisesRegex(c.ToolError, 'Modifications locales sur master'):
            cli.update_from_master(self.root)
        self.assertEqual(self.git(self.root, 'rev-parse', 'HEAD'), before)

    def test_local_master_dirty_blocks_before_fetch_and_keeps_task_work(self):
        self.master_remote()
        (self.main / 'tracked.txt').write_text('master editing\n')
        (self.root / 'new.txt').write_text('task editing\n')
        before = self.git(self.main, 'rev-parse', 'HEAD')
        with self.assertRaisesRegex(c.ToolError, 'Modifications locales sur master'):
            self.interactive_update('1')
        self.assertEqual(self.git(self.main, 'rev-parse', 'HEAD'), before)
        self.assertEqual(self.git(self.root, 'rev-parse', 'HEAD'), before)
        self.assertEqual((self.main / 'tracked.txt').read_text(), 'master editing\n')
        self.assertEqual((self.root / 'new.txt').read_text(), 'task editing\n')
        self.assertEqual(self.git(self.root, 'stash', 'list'), '')
        self.assertEqual(self.git(self.root, 'for-each-ref', '--format=%(refname)', 'refs/remotes/origin/master'), '')

    def test_local_master_divergence_blocks_without_merge_commit(self):
        self.master_remote()
        self.git(self.main, 'commit', '--allow-empty', '-m', 'local master commit')
        master = self.git(self.main, 'rev-parse', 'HEAD')
        task = self.git(self.root, 'rev-parse', 'HEAD')
        with self.assertRaisesRegex(c.ToolError, 'master local a divergé'):
            cli.update_from_master(self.root)
        self.assertEqual(self.git(self.main, 'rev-parse', 'HEAD'), master)
        self.assertEqual(self.git(self.root, 'rev-parse', 'HEAD'), task)
        self.assertEqual(self.git(self.main, 'show', '-s', '--format=%P', 'HEAD'), task)

    def test_local_master_updates_even_if_task_already_contains_remote(self):
        target = self.master_remote()
        self.git(self.root, 'fetch', 'origin', 'master')
        self.git(self.root, 'merge', '--ff-only', 'FETCH_HEAD')
        cli.update_from_master(self.root)
        self.assertEqual(self.git(self.main, 'rev-parse', 'HEAD'), target)
        self.assertEqual(self.git(self.root, 'rev-parse', 'HEAD'), target)

    def test_main_branch_is_not_switched_to_update_master(self):
        self.master_remote()
        self.git(self.main, 'checkout', '-b', 'local_other')
        before = self.git(self.root, 'rev-parse', 'HEAD')
        with self.assertRaisesRegex(c.ToolError, 'dépôt principal doit être sur master'):
            cli.update_from_master(self.root)
        self.assertEqual(self.git(self.main, 'branch', '--show-current'), 'local_other')
        self.assertEqual(self.git(self.root, 'rev-parse', 'HEAD'), before)

    def test_local_master_refresh_refuses_collisions_with_other_task(self):
        self.master_remote('tracked.txt')
        self.git(self.main, 'branch', 'feature/2345_other')
        other = cli.ensure_worktree('2345_other')
        (other / 'tracked.txt').write_text('other task editing\n')
        before = self.git(self.main, 'rev-parse', 'HEAD')
        with self.assertRaisesRegex(c.ToolError, 'actualisation du master local refusée'):
            cli.update_from_master(self.root)
        self.assertEqual(self.git(self.main, 'rev-parse', 'HEAD'), before)
        self.assertEqual(self.git(self.root, 'rev-parse', 'HEAD'), before)
        self.assertEqual((other / 'tracked.txt').read_text(), 'other task editing\n')

    def test_local_master_ignored_file_is_not_overwritten(self):
        self.master_remote('doc/protected.txt')
        (self.main / 'doc/protected.txt').write_text('local personal\n')
        before = self.git(self.main, 'rev-parse', 'HEAD')
        with self.assertRaisesRegex(c.ToolError, 'Actualisation du master local interrompue'):
            cli.update_from_master(self.root)
        self.assertEqual(self.git(self.main, 'rev-parse', 'HEAD'), before)
        self.assertEqual(self.git(self.root, 'rev-parse', 'HEAD'), before)
        self.assertEqual((self.main / 'doc/protected.txt').read_text(), 'local personal\n')

    def test_open_does_not_launch_after_fetch_failure(self):
        args = cli.parser().parse_args(['worktree_open', '1234_réglage'])
        original = cli.context
        with patch.object(cli, 'context', side_effect=lambda path=None, **kw: original(path or self.main, **kw)), \
                patch.object(atelier, 'preflight'), patch.object(sessions, 'prepare') as prepare, \
                patch.object(atelier, 'open_atelier') as opened:
            with self.assertRaises(c.ToolError): cli.open_command(args)
        prepare.assert_not_called()
        opened.assert_not_called()

    def test_open_no_update_preserves_dirty_worktree(self):
        (self.root / 'tracked.txt').write_text('unfinished\n')
        args = cli.parser().parse_args(['worktree_open', '1234_réglage', '--no-update', '--prepare-only'])
        original = cli.context
        with patch.object(cli, 'context', side_effect=lambda path=None, **kw: original(path or self.main, **kw)), \
                patch.object(cli, 'update_from_master') as update, patch.object(sessions, 'prepare') as prepare:
            cli.open_command(args)
        update.assert_not_called()
        prepare.assert_called_once()
        self.assertEqual((self.root / 'tracked.txt').read_text(), 'unfinished\n')

    def test_audit_ignores_stash_commits_but_keeps_branch_commits(self):
        import io
        self.git(self.root, 'commit', '--allow-empty', '-m', 'real branch change')
        branch_sha = self.git(self.root, 'rev-parse', '--short', 'HEAD')
        (self.root / 'tracked.txt').write_text('staged change\n')
        self.git(self.root, 'add', 'tracked.txt')
        (self.root / 'untracked.txt').write_text('untracked change\n')
        self.git(self.root, 'stash', 'push', '-u', '-m', 'audit fixture')
        stash_before = self.git(self.root, 'rev-parse', 'refs/stash')
        stash_shas = [self.git(self.root, 'rev-parse', '--short', ref)
                      for ref in ('refs/stash', 'refs/stash^2', 'refs/stash^3')]
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            cli.audit(self.root, 'master', 7, None)
        self.assertIn('À INTÉGRER ' + branch_sha, output.getvalue())
        self.assertIn('INTÉGRÉ ', output.getvalue())
        for sha in stash_shas:
            self.assertNotIn(sha, output.getvalue())
        self.assertEqual(self.git(self.root, 'rev-parse', 'refs/stash'), stash_before)

    def test_audit_refreshes_before_selecting_target(self):
        for command in ('merge_to_master', 'merge_to_dev'):
            events = []
            def probe(*args, **kwargs):
                events.append('probe')
                return Mock(returncode=0)
            with patch.object(sys, 'argv', ['tools', command]), \
                    patch.object(cli, 'context', return_value=(self.root, 'master', None)), \
                    patch.object(cli, 'run', side_effect=lambda *a: events.append('fetch')) as run, \
                    patch.object(cli, 'git', side_effect=probe), \
                    patch.object(cli, 'audit') as audit:
                cli.main()
            self.assertEqual(events, ['fetch', 'probe'])
            run.assert_called_once_with(['git', '-C', self.root, 'fetch', '--prune', 'origin'])
            audit.assert_called_once()

    def test_audit_offline_and_explicit_fetch(self):
        for command in ('merge_to_master', 'merge_to_dev'):
            for flag, expected in (('--no-fetch', 0), ('--fetch', 1)):
                with patch.object(sys, 'argv', ['tools', command, flag]), \
                        patch.object(cli, 'context', return_value=(self.root, 'master', None)), \
                        patch.object(cli, 'run') as run, \
                        patch.object(cli, 'git', return_value=Mock(returncode=0)), \
                        patch.object(cli, 'audit'):
                    cli.main()
                self.assertEqual(run.call_count, expected)

    def test_audit_stops_when_fetch_fails(self):
        with patch.object(sys, 'argv', ['tools', 'merge_to_master']), \
                patch.object(cli, 'context', return_value=(self.root, 'master', None)), \
                patch.object(cli, 'run', side_effect=c.ToolError('fetch failed')), \
                patch.object(cli, 'git') as probe, patch.object(cli, 'audit') as audit:
            with self.assertRaises(c.ToolError): cli.main()
        probe.assert_not_called()
        audit.assert_not_called()

    def test_backup_offer_displays_legacy_file_timestamp(self):
        import io
        path = self.main / 'doc/patch_old.txt'; path.write_text('patch')
        os.utime(path, (1700000000, 1700000000))
        output = io.StringIO()
        with patch.object(backup, 'pending', return_value=[path]), \
                patch.object(backup.os, 'isatty', return_value=False), contextlib.redirect_stdout(output):
            backup.offer(self.root)
        expected = c.datetime.datetime.fromtimestamp(1700000000).astimezone().strftime('%d/%m/%Y à %H:%M:%S %Z (%z)')
        self.assertIn('Dernière modification du fichier : ' + expected, output.getvalue())

    def test_backup_offer_uses_archive_creation_timestamp(self):
        import io
        path = self.main / 'doc/patch_new.wtpatch.zip'; path.touch()
        created = '2026-09-25T17:53:10+00:00'
        output = io.StringIO()
        with patch.object(backup, 'pending', return_value=[path]), \
                patch.object(backup, 'inspect_archive', return_value=({'created_at': created}, b'', None)), \
                patch.object(backup, 'already_present', return_value=False), \
                patch.object(backup.os, 'isatty', return_value=False), contextlib.redirect_stdout(output):
            backup.offer(self.root)
        expected = c.datetime.datetime.fromisoformat(created).astimezone().strftime('%d/%m/%Y à %H:%M:%S %Z (%z)')
        self.assertIn('Sauvegarde créée le : ' + expected, output.getvalue())

    def master_terminal_env(self):
        return {'DISPLAY': ':0', 'GNOME_TERMINAL_SERVICE': ':1.89',
                'GNOME_TERMINAL_SCREEN': '/org/gnome/Terminal/screen/fixture'}

    def test_master_launcher_opens_two_tabs_in_calling_terminal(self):
        state = {'mode': 'http', 'port': 3000}
        with patch.dict(os.environ, self.master_terminal_env(), clear=True), \
                patch.object(atelier.shutil, 'which', return_value='/fixture/binary'), \
                patch.object(docker, 'read_state', return_value={}), \
                patch.object(docker, 'activate', return_value=state) as activate, \
                patch.object(atelier, 'run', return_value=Mock(returncode=0)) as run:
            atelier.open_master(self.main)
        activate.assert_called_once_with(self.main, 'http')
        self.assertEqual(run.call_count, 2)
        for call, role in zip(run.call_args_list, ('console', 'server')):
            args = call.args[0]
            self.assertEqual(args[:2], ['gnome-terminal', '--tab'])
            self.assertNotIn('--window', args)
            self.assertEqual(args[args.index('--working-directory')+1], str(self.main))
            self.assertEqual(args[args.index('--')+1:],
                             [sys.executable, str(TOOLS / 'worktree_tools.py'), '_master_tab', role])
            self.assertEqual(call.kwargs, {'cwd': self.main, 'timeout': 15})

    def test_master_launcher_rejects_task_or_nonmaster_before_docker(self):
        with patch.object(docker, 'activate') as activate, patch.object(atelier, 'run') as run:
            with self.assertRaisesRegex(c.ToolError, 'dépôt principal sur master'):
                atelier.open_master(self.root)
            self.git(self.main, 'checkout', '-b', 'feature/9999_other')
            with self.assertRaisesRegex(c.ToolError, 'dépôt principal sur master'):
                atelier.open_master(self.main)
        activate.assert_not_called()
        run.assert_not_called()

    def test_master_launcher_requires_calling_gnome_screen_before_docker(self):
        env = self.master_terminal_env(); env.pop('GNOME_TERMINAL_SCREEN')
        with patch.dict(os.environ, env, clear=True), \
                patch.object(atelier.shutil, 'which', return_value='/fixture/binary'), \
                patch.object(docker, 'activate') as activate, patch.object(atelier, 'run') as run:
            with self.assertRaisesRegex(c.ToolError, 'terminal GNOME'):
                atelier.open_master(self.main)
        activate.assert_not_called()
        run.assert_not_called()

    def test_master_launcher_preserves_mode_and_accepts_explicit_override(self):
        for mode, expected in ((None, 'https'), ('http', 'http')):
            with patch.dict(os.environ, self.master_terminal_env(), clear=True), \
                    patch.object(atelier.shutil, 'which', return_value='/fixture/binary'), \
                    patch.object(docker, 'read_state', return_value={'mode': 'https'}), \
                    patch.object(docker, 'activate', return_value={'mode': expected, 'port': 3000}) as activate, \
                    patch.object(atelier, 'run', return_value=Mock(returncode=0)):
                atelier.open_master(self.main, mode)
            activate.assert_called_once_with(self.main, expected)

    def test_master_launcher_reports_terminal_failure_without_stopping_docker(self):
        with patch.dict(os.environ, self.master_terminal_env(), clear=True), \
                patch.object(atelier.shutil, 'which', return_value='/fixture/binary'), \
                patch.object(docker, 'read_state', return_value={}), \
                patch.object(docker, 'activate', return_value={'mode': 'http', 'port': 3000}), \
                patch.object(docker, 'stop') as stop, \
                patch.object(atelier, 'run', side_effect=[Mock(returncode=0), c.ToolError('GNOME en échec')]):
            with self.assertRaisesRegex(c.ToolError, c.setting_json('MAIN_ROLE_TITLES')['server'] + '.*GNOME en échec'):
                atelier.open_master(self.main)
        stop.assert_not_called()

    def test_master_tabs_revalidate_branch_before_attach(self):
        with patch.object(docker, 'service_command', return_value=['docker', 'attach', 'fixture']) as command, \
                patch.object(atelier, 'run', return_value=Mock(returncode=0)) as run:
            for role, service in (('console', 'console'), ('server', None)):
                self.assertEqual(atelier.master_tab(self.main, role), 0)
                command.assert_called_with(self.main, 'attach', service)
            self.git(self.main, 'checkout', '-b', 'feature/9999_other')
            with self.assertRaisesRegex(c.ToolError, 'dépôt principal sur master'):
                atelier.master_tab(self.main, 'server')
            self.assertEqual(command.call_count, 2)
            self.assertEqual(run.call_count, 2)

    def test_switch_worktree_activates_exact_screen_without_docker(self):
        identifier = 'f0873d17-a8d8-4694-8ecc-9ea2eeb24321'
        with patch.object(atelier, 'terminal_target', return_value=(':1.89', identifier)), \
                patch.object(atelier, 'run', side_effect=[Mock(stdout=repr(([identifier],))), Mock(stdout='()')]) as run, \
                patch.object(docker, 'activate') as activate:
            atelier.worktree_switch(self.root)
        self.assertEqual(run.call_args.args[0][-3:], [identifier, '[]', '0'])
        self.assertIn('org.gnome.Shell.SearchProvider2.ActivateResult', run.call_args.args[0])
        self.assertEqual(run.call_args.kwargs['timeout'], 10)
        activate.assert_not_called()

    def test_switch_worktree_refuses_missing_screen(self):
        with patch.object(atelier, 'terminal_target', return_value=(':1.89', 'missing')), \
                patch.object(atelier, 'run', return_value=Mock(stdout='(@as [],)')) as run:
            with self.assertRaises(c.ToolError): atelier.worktree_switch(self.root)
        self.assertEqual(run.call_count, 1)

    def test_switch_worktree_refuses_closed_atelier(self):
        with patch.object(atelier, 'run') as run:
            with self.assertRaises(c.ToolError): atelier.worktree_switch(self.root)
        run.assert_not_called()

    def test_switch_worktree_without_argument_targets_master(self):
        with patch.object(atelier, 'master_terminal_target', side_effect=c.ToolError('stop')) as target, \
                patch.object(atelier, 'terminal_target') as task_target:
            with self.assertRaises(c.ToolError): atelier.worktree_switch()
        target.assert_called_once_with()
        task_target.assert_not_called()

    def test_master_terminal_uses_oldest_shell_in_main(self):
        proc = Path(self.temp.name) / 'proc'; proc.mkdir()
        for pid, root in ((10, self.root), (20, self.main), (30, self.main)):
            folder = proc / str(pid); folder.mkdir()
            (folder / 'cwd').symlink_to(root)
            (folder / 'comm').write_text('bash\n')
            (folder / 'environ').write_bytes(('GNOME_TERMINAL_SERVICE=:1.89\0'
                'GNOME_TERMINAL_SCREEN=/org/gnome/Terminal/screen/'
                '00000000_0000_0000_0000_%012d\0' % pid).encode())
        with patch.object(atelier, 'Path', side_effect=lambda p: proc if p == '/proc' else Path(p)), \
                patch.object(atelier, 'identity', side_effect=lambda pid: str(pid)):
            self.assertEqual(atelier.master_terminal_target(),
                             (':1.89', '00000000-0000-0000-0000-000000000020'))

    def test_master_terminal_missing_is_reported(self):
        proc = Path(self.temp.name) / 'proc'; proc.mkdir()
        with patch.object(atelier, 'Path', side_effect=lambda p: proc if p == '/proc' else Path(p)):
            with self.assertRaisesRegex(c.ToolError, 'Aucun terminal master'):
                atelier.master_terminal_target()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='worktree-tests-')
        self.addCleanup(self.temp.cleanup)
        self.main = Path(self.temp.name) / 'development' / 'project'
        self.main.mkdir(parents=True)
        self.git(self.main, 'init', '-b', 'master')
        self.git(self.main, 'config', 'user.name', 'Fixture')
        self.git(self.main, 'config', 'user.email', 'fixture@example.invalid')
        (self.main / '.gitignore').write_text('doc/\nsessions_codex/\n.codex/\nAGENTS-local.md\nAGENTS.override.md\n.env\nvimsession_*\ntmp\n')
        (self.main / 'tracked.txt').write_text('initial\n')
        (self.main / 'delete.txt').write_text('delete me\n')
        self.git(self.main, 'add', '.gitignore', 'tracked.txt', 'delete.txt')
        self.git(self.main, 'commit', '-m', 'fixture')
        (self.main / 'doc').mkdir()
        (self.main / '.git/info/exclude').write_text('\n'.join(c.setting_json('PROFILE_FILES', [])) + '\n')
        for profile in c.setting_json('PROFILE_FILES', []):
            (self.main / profile).write_text('Profil fixture')
        self.scope = contextlib.ExitStack(); self.addCleanup(self.scope.close)
        self.scope.enter_context(patch.dict(os.environ, WT_WORKTREE_PREFIX='project-'))
        for module in (c, backup, docker, sessions, atelier, cli):
            self.scope.enter_context(patch.object(module, 'MAIN', self.main))
            self.scope.enter_context(patch.object(module, 'STATE', self.main / 'doc' / 'docker-worktrees'))
        self.git(self.main, 'branch', 'feature/1234_réglage')
        self.root = cli.ensure_worktree('1234_réglage')

    def git(self, root, *args):
        return subprocess.check_output(['git', '-C', str(root), *args], stderr=subprocess.PIPE, text=True).strip()

    def test_close_from_main_without_task_closes_all(self):
        with patch.object(sys, 'argv', ['tools', 'worktree_close']), \
                patch.object(cli, 'context', side_effect=lambda path=None, **kw: c.context(path or self.main, **kw)), \
                patch.object(atelier, 'close_all') as close_all, patch.object(atelier, 'close') as close:
            cli.main()
        close_all.assert_called_once_with()
        close.assert_not_called()

    def test_close_from_task_keeps_single_target(self):
        with patch.object(sys, 'argv', ['tools', 'worktree_close']), \
                patch.object(cli, 'context', return_value=(self.root, 'feature/1234_réglage', '1234_réglage')), \
                patch.object(atelier, 'close_all') as close_all, patch.object(atelier, 'close') as close:
            cli.main()
        close.assert_called_once_with(self.root)
        close_all.assert_not_called()

    def test_close_named_task_from_main_keeps_single_target(self):
        with patch.object(sys, 'argv', ['tools', 'worktree_close', '1234_réglage']), \
                patch.object(cli, 'context', side_effect=lambda path=None, **kw: c.context(path or self.main, **kw)), \
                patch.object(atelier, 'close_all') as close_all, patch.object(atelier, 'close') as close:
            cli.main()
        close.assert_called_once_with(self.root)
        close_all.assert_not_called()

    def test_close_all_selects_only_open_worktrees_and_preserves_main(self):
        self.status_record()
        inactive = self.main.parent / 'project-inactive'
        with patch.object(atelier, 'worktrees', return_value=[{'worktree': str(p)} for p in (self.main, self.root, inactive)]), \
                patch.object(atelier, 'close') as close:
            atelier.close_all()
        close.assert_called_once_with(self.root)

    def test_close_all_without_open_worktree_does_nothing(self):
        with patch.object(atelier, 'close') as close:
            atelier.close_all()
        close.assert_not_called()

    def test_close_all_stops_on_refusal_after_previous_successes(self):
        second, third = self.main.parent / 'project-second', self.main.parent / 'project-third'
        rows = [{'worktree': str(p)} for p in (self.root, second, third)]
        with patch.object(atelier, 'worktrees', return_value=rows), \
                patch.object(atelier, 'open_roles', return_value=['vim']), \
                patch.object(atelier, 'close', side_effect=[None, c.ToolError('Buffers modifiés')]) as close:
            with self.assertRaisesRegex(c.ToolError, '1/3.*Buffers modifiés'):
                atelier.close_all()
        self.assertEqual([call.args[0] for call in close.call_args_list], [self.root, second])

    def status_record(self, role='vim', **changes):
        folder = atelier.directory(self.root)
        c.atomic(folder / 'atelier.json', {'generation': 'current', 'status': 'open'})
        record = {'pid': os.getpid(), 'start': atelier.identity(os.getpid()),
                  'generation': 'current', 'worktree': str(self.root), 'role': role, 'status': 'running'}
        record.update(changes)
        c.atomic(folder / (role + '.json'), record)

    def test_status_counts_an_open_shell_without_codex(self):
        import io
        self.status_record()
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            cli.status_all()
        text = output.getvalue()
        self.assertIn('atelier OUVERT (onglets : Bash)', text)
        self.assertIn('1 atelier(s) de worktree actuellement ouvert(s).', text)
        self.assertIn('refs/heads/feature/1234_réglage', text)
        self.assertIn('propre', text)

    def test_status_ignores_stale_foreign_closed_and_incomplete_records(self):
        cases = ({'start': 'previous-process'}, {'generation': 'old'},
                 {'worktree': str(self.main)}, {'status': 'closed'}, {'start': None})
        for changes in cases:
            with self.subTest(changes=changes):
                self.status_record(**changes)
                self.assertEqual(atelier.open_roles(self.root), [])
        (atelier.directory(self.root) / 'atelier.json').unlink()
        self.assertEqual(atelier.open_roles(self.root), [])

    def test_status_reports_no_open_worktree_and_keeps_git_changes(self):
        import io
        (self.root / 'tracked.txt').write_text('unfinished')
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            cli.status_all()
        self.assertIn('atelier fermé', output.getvalue())
        self.assertIn('Aucun atelier de worktree actuellement ouvert.', output.getvalue())
        self.assertIn('M tracked.txt', output.getvalue())

    def test_existing_branch_uses_worktree_without_switching_master(self):
        self.assertEqual(self.root.parent, self.main.parent)
        self.assertEqual(self.git(self.main, 'branch', '--show-current'), 'master')
        self.assertEqual(cli.ensure_worktree('1234_réglage'), self.root)

    def test_new_branch_uses_master_commit(self):
        root = cli.ensure_worktree('2345_nouveau', create=True)
        self.assertEqual(self.git(root, 'rev-parse', 'HEAD'), self.git(self.main, 'rev-parse', 'master'))
        with self.assertRaises(c.ToolError): cli.ensure_worktree('2345_nouveau', create=True)

    def reach(self, task, *options):
        args = cli.parser().parse_args(['worktree_reach', task, *options])
        with patch.object(cli, 'context', wraps=c.context) as context:
            context.side_effect = lambda *a, **kw: c.context(*(a or (self.main,)), **kw)
            cli.open_command(args)

    def test_reach_without_task_returns_to_main_terminal_without_preparation(self):
        previous = Path.cwd()
        try:
            os.chdir(self.root)
            (self.root / 'tracked.txt').write_text('unfinished work\n')
            with patch.object(sys, 'argv', ['worktree_tools.py', 'worktree_reach']), \
                    patch.object(atelier, 'worktree_switch') as switch, \
                    patch.object(cli, 'ensure_worktree') as ensure, \
                    patch.object(cli, 'update_from_master') as update, \
                    patch.object(sessions, 'prepare') as prepare, \
                    patch.object(atelier, 'preflight') as preflight, \
                    patch.object(atelier, 'open_atelier') as opened:
                cli.main()
            switch.assert_called_once_with()
            for operation in (ensure, update, prepare, preflight, opened):
                operation.assert_not_called()
            self.assertEqual((self.root / 'tracked.txt').read_text(), 'unfinished work\n')
            self.assertEqual(Path.cwd(), self.root)
        finally:
            os.chdir(previous)

    def test_reach_without_task_reports_missing_main_terminal(self):
        with patch.object(cli, 'context'), \
                patch.object(atelier, 'worktree_switch', side_effect=c.ToolError('Aucun terminal master trouvé.')):
            with self.assertRaisesRegex(c.ToolError, 'Aucun terminal master'):
                cli.open_command(cli.parser().parse_args(['worktree_reach']))

    def test_reach_creates_missing_branch_and_initializes_notes(self):
        self.reach('2345_nouveau', '--prepare-only')
        root = self.main.parent / 'project-2345_nouveau'
        self.assertEqual(self.git(root, 'branch', '--show-current'), 'feature/2345_nouveau')
        self.assertEqual(self.git(root, 'rev-parse', 'HEAD'), self.git(self.main, 'rev-parse', 'master'))
        self.assertTrue((root / 'doc/2345_nouveau_suivi.txt').is_file())
        self.assertEqual(self.git(self.main, 'branch', '--show-current'), 'master')

    def test_reach_attaches_existing_branch_preserving_its_commit(self):
        self.git(self.main, 'branch', 'feature/2345_existante', 'master')
        with patch.object(cli, 'update_from_master') as update, patch.object(sessions, 'prepare') as prepare:
            self.reach('2345_existante', '--prepare-only', '--no-update')
        root = self.main.parent / 'project-2345_existante'
        self.assertEqual(self.git(root, 'branch', '--show-current'), 'feature/2345_existante')
        prepare.assert_called_once_with(root, new=False)
        update.assert_not_called()

    def test_reach_attaches_remote_branch_with_tracking(self):
        self.git(self.main, 'update-ref', 'refs/remotes/origin/feature/2345_distante', 'HEAD')
        self.git(self.main, 'config', 'remote.origin.url', str(self.main))
        self.git(self.main, 'config', 'remote.origin.fetch', '+refs/heads/*:refs/remotes/origin/*')
        self.reach('2345_distante', '--prepare-only', '--no-update')
        root = self.main.parent / 'project-2345_distante'
        self.assertEqual(self.git(root, 'rev-parse', '--abbrev-ref', '@{upstream}'),
                         'origin/feature/2345_distante')

    def test_reach_opens_closed_atelier_and_updates_existing_task(self):
        with patch.object(atelier, 'preflight'), patch.object(cli, 'update_from_master') as update, \
                patch.object(sessions, 'prepare') as prepare, patch.object(atelier, 'open_atelier') as opened:
            self.reach('1234_réglage')
        update.assert_called_once_with(self.root)
        prepare.assert_called_once_with(self.root, new=False)
        opened.assert_called_once_with(self.root, 'http')

    def test_reach_switches_live_atelier_without_git_update_or_docker(self):
        with patch.object(atelier, 'alive', return_value=True), \
                patch.object(atelier, 'worktree_switch') as switch, \
                patch.object(cli, 'update_from_master') as update, \
                patch.object(sessions, 'prepare') as prepare, \
                patch.object(atelier, 'open_atelier') as opened, \
                patch.object(atelier, 'preflight') as preflight:
            self.reach('1234_réglage')
        switch.assert_called_once_with(self.root)
        for operation in (update, prepare, opened, preflight): operation.assert_not_called()

    def test_reach_reports_partial_atelier_without_opening_another(self):
        with patch.object(atelier, 'alive', side_effect=[False, True, False, False, False, False]), \
                patch.object(atelier, 'open_atelier') as opened, \
                patch.object(cli, 'update_from_master') as update:
            with self.assertRaisesRegex(c.ToolError, 'Aucun onglet Codex'):
                self.reach('1234_réglage')
        opened.assert_not_called()
        update.assert_not_called()

    def test_reach_refuses_occupied_destination_without_creating_branch(self):
        (self.main.parent / 'project-2345_nouveau').mkdir()
        with self.assertRaisesRegex(c.ToolError, 'Destination déjà occupée'):
            self.reach('2345_nouveau', '--prepare-only')
        self.assertEqual(c.git(self.main, 'show-ref', '--verify', '--quiet',
                             'refs/heads/feature/2345_nouveau', check=False).returncode, 1)

    def test_shell_commands_prepare_worktree_and_preserve_calling_directory(self):
        import shlex
        env = dict(os.environ, WT_MAIN_ROOT=str(self.main))
        for command, task in (('worktree_open.sh', '1234_réglage'), ('worktree_new.sh', '2345_nouveau'),
                              ('worktree_reach.sh', '3456_unifiée')):
            script = ('source ' + shlex.quote(str(TOOLS / 'worktree_shell.sh')) + '\n' +
                      command + ' ' + shlex.quote(task) + ' --prepare-only' +
                      (' --no-update' if command == 'worktree_open.sh' else '') + '\npwd -P\n')
            result = subprocess.run(['bash', '-e', '-c', script], cwd=self.main, env=env,
                                    capture_output=True, text=True, check=True, timeout=20)
            target = self.main.parent / ('project-' + task)
            self.assertEqual(result.stdout.splitlines()[-1], str(self.main))
            self.assertEqual(self.git(target, 'branch', '--show-current'), 'feature/' + task)
            if command in ('worktree_new.sh', 'worktree_reach.sh'):
                self.assertTrue((target / 'doc' / (task + '_suivi.txt')).is_file())
        self.assertEqual(self.git(self.main, 'branch', '--show-current'), 'master')

    def run_patch_without_action(self, answer=True, interactive=True):
        previous = Path.cwd()
        try:
            os.chdir(self.root)
            with patch.object(sys, 'argv', ['worktree_tools.py', 'patch']), \
                    patch.object(backup.os, 'isatty', return_value=interactive), \
                    patch.object(backup, 'ask', return_value=answer) as ask:
                cli.main()
            return ask
        finally:
            os.chdir(previous)

    def test_patch_no_action_restores_after_checkout_with_saved_untracked_files(self):
        (self.root / 'tracked.txt').write_text('saved change\n')
        new = self.root / 'new file.txt'; new.write_text('saved new file\n')
        archive = backup.make(self.root)
        self.assertEqual(archive.parent, self.main / 'doc')
        self.git(self.root, 'checkout', '.')
        ask = self.run_patch_without_action()
        ask.assert_called_once()
        self.assertEqual((self.root / 'tracked.txt').read_text(), 'saved change\n')
        self.assertEqual(new.read_text(), 'saved new file\n')
        self.assertFalse(archive.exists())
        self.assertTrue(Path(str(archive) + '.done').exists())

    def test_patch_no_action_decline_or_noninteractive_preserves_files(self):
        (self.root / 'tracked.txt').write_text('saved change\n')
        archive = backup.make(self.root)
        self.git(self.root, 'checkout', '.')
        for answer, interactive in ((False, True), (True, False)):
            with self.subTest(interactive=interactive):
                ask = self.run_patch_without_action(answer, interactive)
                self.assertEqual(ask.call_count, int(interactive))
                self.assertEqual((self.root / 'tracked.txt').read_text(), 'initial\n')
                self.assertTrue(archive.exists())

    def test_patch_no_action_rejects_changed_or_foreign_untracked_file(self):
        (self.root / 'tracked.txt').write_text('saved change\n')
        new = self.root / 'new.txt'; new.write_text('saved new file\n')
        archive = backup.make(self.root)
        self.git(self.root, 'checkout', '.')
        new.write_text('new work since backup\n')
        with self.assertRaisesRegex(c.ToolError, 'non identique'):
            self.run_patch_without_action()
        self.assertEqual(new.read_text(), 'new work since backup\n')
        self.assertTrue(archive.exists())
        new.write_text('saved new file\n')
        foreign = self.root / 'foreign.txt'; foreign.write_text('unrelated work')
        with self.assertRaisesRegex(c.ToolError, 'foreign.txt'):
            self.run_patch_without_action()
        self.assertEqual(foreign.read_text(), 'unrelated work')
        self.assertEqual((self.root / 'tracked.txt').read_text(), 'initial\n')

    def test_restore_rechecks_saved_untracked_file_after_confirmation(self):
        (self.root / 'tracked.txt').write_text('saved change\n')
        new = self.root / 'new.txt'; new.write_text('saved new file\n')
        archive = backup.make(self.root)
        self.git(self.root, 'checkout', '.')
        def answer(_):
            new.write_text('written during confirmation\n')
            return True
        with patch.object(backup, 'ask', side_effect=answer):
            with self.assertRaisesRegex(c.ToolError, 'non identique'):
                backup.restore(self.root, archive)
        self.assertEqual(new.read_text(), 'written during confirmation\n')
        self.assertEqual((self.root / 'tracked.txt').read_text(), 'initial\n')
        self.assertTrue(archive.exists())

    def test_patch_no_action_reports_no_backup(self):
        import io
        output = io.StringIO()
        with contextlib.redirect_stdout(output), patch.object(backup, 'restore') as restore:
            self.run_patch_without_action()
        self.assertIn('Aucune sauvegarde en attente', output.getvalue())
        restore.assert_not_called()

    def test_patch_storage_is_central_for_both_named_and_current_task(self):
        (self.root / 'tracked.txt').write_text('central backup\n')
        with patch.dict(os.environ, WT_DOCUMENTS_DIR='notes'):
            archive = backup.make(self.root)
            self.assertEqual(archive.parent, self.main / 'doc')
            self.assertEqual(backup.pending(self.root), [archive])
            self.assertEqual(backup.pending(task='1234_réglage'), [archive])
        self.assertFalse((self.root / 'doc').exists())

    def test_patch_restore_relative_doc_path_resolves_central_archive(self):
        (self.root / 'tracked.txt').write_text('central restore\n')
        archive = backup.make(self.root)
        self.git(self.root, 'checkout', '.')
        previous = Path.cwd()
        try:
            os.chdir(self.root)
            backup.restore(self.root, 'doc/' + archive.name, yes=True)
        finally:
            os.chdir(previous)
        self.assertEqual((self.root / 'tracked.txt').read_text(), 'central restore\n')
        self.assertTrue(Path(str(archive) + '.done').exists())

    def test_patch_listing_ignores_local_worktree_files_and_restore_refuses_them(self):
        (self.root / 'doc').mkdir()
        local = self.root / 'doc/patch_1234_réglage_old.txt'
        local.write_text('legacy')
        self.assertEqual(backup.pending(self.root), [])
        self.assertEqual(backup.pending(task='1234_réglage'), [])
        with self.assertRaisesRegex(c.ToolError, 'doivent se trouver'):
            backup.restore(self.root, local, yes=True)
        self.assertEqual(local.read_text(), 'legacy')

    def test_is_any_patch_named_task_lists_central_backups(self):
        (self.root / 'doc').mkdir(exist_ok=True)
        paths = [self.main / 'doc/patch_1234_réglage_new.wtpatch.zip',
                 self.main / 'doc/patch_auggie_1234_réglage_old.txt',
                 self.main / 'doc/patch_1234_réglage_restored.wtpatch.zip.done',
                 self.main / 'doc/patch_1234_réglage_restored.txt.done',
                 self.main / 'doc/patch_9999_other_new.txt']
        for index, path in enumerate(paths):
            path.write_text('fixture backup')
            os.utime(path, (100 + index, 100 + index))
        env = dict(os.environ, WT_MAIN_ROOT=str(self.main))
        for extra, expected in [((), [paths[1], paths[0]]), (('done',), [paths[3], paths[2]])]:
            with self.subTest(extra=extra):
                result = subprocess.run([str(TOOLS / 'is_any_patch.sh'), '1234_réglage', *extra],
                                        cwd=self.main, env=env, capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.splitlines(), [str(path) for path in expected])
        self.assertTrue(all(path.read_text() == 'fixture backup' for path in paths))
        self.assertEqual(self.git(self.main, 'branch', '--show-current'), 'master')

    def test_is_any_patch_current_worktree_preserves_done_argument(self):
        (self.root / 'doc').mkdir(exist_ok=True)
        pending = self.main / 'doc/patch_1234_réglage_new.txt'
        done = self.main / 'doc/patch_1234_réglage_restored.txt.done'
        pending.write_text('pending'); done.write_text('done')
        env = dict(os.environ, WT_MAIN_ROOT=str(self.main))
        for extra, expected in [((), pending), (('done',), done)]:
            with self.subTest(extra=extra):
                result = subprocess.run([str(TOOLS / 'is_any_patch.sh'), *extra],
                                        cwd=self.root, env=env, capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.strip(), str(expected))

    def test_is_any_patch_without_worktree_or_branch_searches_main(self):
        pending = self.main / 'doc/patch_9999_absente_new.txt'
        done = self.main / 'doc/patch_auggie_9999_absente_old.txt.done'
        pending.write_text('pending'); done.write_text('done')
        env = dict(os.environ, WT_MAIN_ROOT=str(self.main))
        before = self.git(self.main, 'worktree', 'list', '--porcelain')
        for extra, expected in [((), pending), (('done',), done)]:
            with self.subTest(extra=extra):
                result = subprocess.run([str(TOOLS / 'is_any_patch.sh'), '9999_absente', *extra],
                                        cwd=self.main, env=env, capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.strip(), str(expected))
        self.assertEqual(self.git(self.main, 'worktree', 'list', '--porcelain'), before)
        self.assertEqual(self.git(self.main, 'branch', '--list', 'feature/9999_absente'), '')
        self.assertEqual(pending.read_text(), 'pending')
        self.assertEqual(done.read_text(), 'done')

    def test_is_any_patch_with_branch_but_no_worktree_searches_main(self):
        self.git(self.main, 'branch', 'feature/9999_absente')
        path = self.main / 'doc/patch_9999_absente_new.wtpatch.zip'
        path.write_text('backup')
        with patch.object(cli, 'ensure_worktree') as create, patch.object(cli, 'existing_worktree') as existing:
            self.assertEqual(backup.pending(task='9999_absente'), [path])
            create.assert_not_called()
            existing.assert_not_called()

    def test_is_any_patch_without_worktree_no_backup_returns_empty(self):
        env = dict(os.environ, WT_MAIN_ROOT=str(self.main))
        result = subprocess.run([str(TOOLS / 'is_any_patch.sh'), '9999_absente'],
                                cwd=self.main, env=env, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), '')
        for invalid in ('../escape', '9999_a/b', 'invalid'):
            with self.subTest(invalid=invalid), self.assertRaises(c.ToolError):
                backup.pending(task=invalid)

    def test_desc_git_named_task_from_main_writes_report_in_central_doc(self):
        (self.root / 'tracked.txt').write_text('committed task change\n')
        self.git(self.root, 'add', 'tracked.txt')
        self.git(self.root, 'commit', '-m', '1234_réglage : task report fixture')
        env = dict(os.environ, WT_MAIN_ROOT=str(self.main))
        before = self.git(self.root, 'rev-parse', 'HEAD')
        result = subprocess.run([str(TOOLS / 'desc_git.sh'), '1234_réglage'],
                                cwd=self.main, env=env, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = self.main / 'doc/1234_réglage_git.txt'
        self.assertEqual(result.stdout.strip(), str(report))
        self.assertIn('Branche : feature/1234_réglage', report.read_text())
        self.assertIn('+committed task change', report.read_text())
        self.assertFalse((self.root / 'doc/1234_réglage_git.txt').exists())
        self.assertEqual(self.git(self.main, 'branch', '--show-current'), 'master')
        self.assertEqual(self.git(self.root, 'rev-parse', 'HEAD'), before)

    def test_desc_git_includes_task_history_already_integrated_in_main(self):
        (self.root / 'tracked.txt').write_text('task change\n')
        self.git(self.root, 'add', 'tracked.txt')
        self.git(self.root, 'commit', '-m', '1234_réglage : first change')
        (self.root / 'new file.txt').write_text('added\n')
        self.git(self.root, 'add', 'new file.txt')
        self.git(self.root, 'commit', '-m', '1234_réglage : second change')
        self.git(self.main, 'merge', '--ff-only', 'feature/1234_réglage')
        self.assertEqual(self.git(self.root, 'merge-base', 'master', 'HEAD'), self.git(self.root, 'rev-parse', 'HEAD'))
        cli.describe(self.root)
        report = (self.main / 'doc/1234_réglage_git.txt').read_text()
        self.assertTrue(report.startswith('Fichiers modifiés dans les commits de la tâche :\nnew file.txt\ntracked.txt\n\n'))
        self.assertIn('Commits sélectionnés : 2', report)
        self.assertIn('1234_réglage : first change', report)
        self.assertIn('1234_réglage : second change', report)
        self.assertIn('+task change', report)
        self.assertIn('+added', report)

    def test_desc_git_excludes_other_tasks_and_body_only_mentions(self):
        for subject, body in [('1234_réglage_other : another task', ''),
                              ('9999_other : change', '1234_réglage'),
                              ('fix: mention 1234_réglage', '')]:
            self.git(self.root, 'commit', '--allow-empty', '-m', subject, '-m', body)
        cli.describe(self.root)
        report = (self.main / 'doc/1234_réglage_git.txt').read_text()
        self.assertIn('Commits sélectionnés : 0', report)
        self.assertIn('Aucun commit dont le sujet commence', report)
        self.assertNotIn('another task', report)

    def test_desc_git_lists_deleted_and_renamed_paths_once(self):
        self.git(self.root, 'rm', 'delete.txt')
        self.git(self.root, 'mv', 'tracked.txt', 'renamed file.txt')
        self.git(self.root, 'commit', '-m', '1234_réglage : rename and delete')
        (self.root / 'renamed file.txt').write_text('modified again\n')
        self.git(self.root, 'add', 'renamed file.txt')
        self.git(self.root, 'commit', '-m', '1234_réglage : update renamed file')
        cli.describe(self.root)
        report = (self.main / 'doc/1234_réglage_git.txt').read_text()
        self.assertEqual(report.split('\n\n', 1)[0], 'Fichiers modifiés dans les commits de la tâche :\ndelete.txt\nrenamed file.txt\ntracked.txt')
        self.assertIn('deleted file mode', report)
        self.assertIn('+modified again', report)

    def test_desc_git_without_task_uses_current_worktree(self):
        previous = Path.cwd()
        try:
            os.chdir(self.root)
            with patch.object(sys, 'argv', ['worktree_tools.py', 'desc_git']):
                cli.main()
            self.assertTrue((self.main / 'doc/1234_réglage_git.txt').is_file())
            self.assertEqual(Path.cwd(), self.root)
        finally:
            os.chdir(previous)

    def test_desc_git_missing_or_locked_worktree_refuses_without_creation(self):
        previous = Path.cwd()
        try:
            os.chdir(self.main)
            with patch.object(sys, 'argv', ['worktree_tools.py', 'desc_git', '9999_absente']), \
                    patch.object(cli, 'ensure_worktree') as create:
                with self.assertRaisesRegex(c.ToolError, 'Aucun worktree'):
                    cli.main()
                create.assert_not_called()
            self.git(self.main, 'worktree', 'lock', str(self.root))
            with patch.object(sys, 'argv', ['worktree_tools.py', 'desc_git', '1234_réglage']):
                with self.assertRaisesRegex(c.ToolError, 'verrouillé'):
                    cli.main()
            self.assertFalse((self.main / 'doc/1234_réglage_git.txt').exists())
        finally:
            os.chdir(previous)

    def test_close_named_task_from_main_targets_existing_worktree(self):
        previous = Path.cwd()
        try:
            os.chdir(self.main)
            with patch.object(sys, 'argv', ['worktree_tools.py', 'worktree_close', '1234_réglage']), \
                    patch.object(atelier, 'close') as close:
                cli.main()
            close.assert_called_once_with(self.root)
        finally:
            os.chdir(previous)

    def test_close_without_task_keeps_current_worktree(self):
        previous = Path.cwd()
        try:
            os.chdir(self.root)
            with patch.object(sys, 'argv', ['worktree_tools.py', 'worktree_close']), \
                    patch.object(atelier, 'close') as close:
                cli.main()
            close.assert_called_once_with(self.root)
        finally:
            os.chdir(previous)

    def test_close_missing_worktree_never_creates_it(self):
        self.git(self.main, 'branch', 'feature/9999_absente')
        with patch.object(cli, 'ensure_worktree') as create:
            with self.assertRaisesRegex(c.ToolError, 'Aucun worktree'):
                cli.existing_worktree('9999_absente')
            create.assert_not_called()
        self.assertFalse((self.main.parent / 'project-9999_absente').exists())

    def test_absent_existing_branch_is_not_created(self):
        with self.assertRaises(c.ToolError): cli.ensure_worktree('9999_absente')
        self.assertEqual(self.git(self.main, 'branch', '--list', 'feature/9999_absente'), '')

    def test_occupied_destination_is_preserved(self):
        self.git(self.main, 'branch', 'feature/4567_occupé')
        target = self.main.parent / 'project-4567_occupé'; target.mkdir(); (target / 'mine').write_text('keep')
        with self.assertRaises(c.ToolError): cli.ensure_worktree('4567_occupé')
        self.assertEqual((target / 'mine').read_text(), 'keep')

    def test_path_escape_and_bad_task_rejected(self):
        for task in ('../escape', '1234_a/b', 'abc', '1234_$(echo bad)', '1234_a b'):
            with self.assertRaises(c.ToolError): cli.ensure_worktree(task)

    def test_nested_repo_cannot_be_target(self):
        nested = self.root / 'doc' / 'tools'; nested.mkdir(parents=True)
        self.git(nested, 'init')
        with self.assertRaises(c.ToolError): c.context(nested)

    def test_symlink_cwd_canonicalized(self):
        alias = Path(self.temp.name) / 'alias'; alias.symlink_to(self.root)
        self.assertEqual(c.context(alias)[0], self.root)

    def test_collision_includes_staged_untracked_and_unstaged(self):
        (self.main / 'tracked.txt').write_text('elsewhere')
        self.git(self.main, 'add', 'tracked.txt')
        (self.main / 'new.txt').write_text('new')
        (self.main / 'delete.txt').write_text('changed')
        self.assertEqual(len(c.collisions(self.root, ['tracked.txt', 'new.txt', 'delete.txt'])), 3)

    def test_collisions_batch_paths_and_preserve_unusual_names(self):
        names = ['file-%s.txt' % i for i in range(260)] + ['é space\nline.txt', 'literal*.txt']
        for name in (names[0], names[200], names[-2], 'literal-other.txt'):
            (self.main / name).write_text('new')
        with patch.object(c, 'git', wraps=c.git) as calls:
            found = c.collisions(self.root, names)
        self.assertEqual([item.split(' : ', 1)[0] for item in found],
                         [names[0], names[200], names[-2]])
        statuses = [call for call in calls.call_args_list if 'status' in call.args]
        self.assertEqual(len(statuses), 3)

    def test_collisions_include_both_sides_of_rename_and_directory(self):
        self.git(self.main, 'mv', 'tracked.txt', 'renamed.txt')
        (self.main / 'nested').mkdir()
        (self.main / 'nested' / 'new.txt').write_text('new')
        names = ['tracked.txt', 'renamed.txt', 'nested', 'unrelated.txt']
        found = c.collisions(self.root, names)
        self.assertEqual([item.split(' : ', 1)[0] for item in found], names[:3])

    def test_snapshot_roundtrip_index_working_binary_delete_and_new(self):
        (self.root / 'tracked.txt').write_text('staged\n')
        self.git(self.root, 'add', 'tracked.txt')
        (self.root / 'tracked.txt').write_text('unstaged\n')
        (self.root / 'delete.txt').unlink()
        binary = self.root / 'nouveau é space.bin'; binary.write_bytes(bytes(range(256)))
        (self.root / 'link').symlink_to('tracked.txt')
        path = backup.make(self.root)
        first = backup.inspect_archive(path)
        backup.restore(self.root, path, yes=True)
        self.assertTrue(path.exists())  # déjà présent, ne pas marquer .done
        self.git(self.root, 'reset', '--hard', 'HEAD')
        binary.unlink(); (self.root / 'link').unlink()
        backup.restore(self.root, path, yes=True)
        self.assertTrue(Path(str(path) + '.done').exists())
        self.assertEqual(self.git(self.root, 'show', ':tracked.txt'), 'staged')
        self.assertEqual((self.root / 'tracked.txt').read_text(), 'unstaged\n')
        self.assertFalse((self.root / 'delete.txt').exists())
        self.assertEqual(binary.read_bytes(), bytes(range(256)))
        self.assertTrue((self.root / 'link').is_symlink())

    def test_staged_change_cancelled_in_working_tree_is_preserved(self):
        (self.root / 'tracked.txt').write_text('staged'); self.git(self.root, 'add', 'tracked.txt')
        (self.root / 'tracked.txt').write_text('initial\n')
        path = backup.make(self.root)
        self.assertIsNotNone(path)
        self.assertIn('tracked.txt', [x['path'] for x in backup.inspect_archive(path)[0]['files']])

    def test_new_file_only_and_multiple_backups_per_day(self):
        (self.root / 'new.txt').write_text('new')
        a = backup.make(self.root); b = backup.make(self.root)
        self.assertNotEqual(a, b)
        self.assertEqual(len(backup.pending(self.root)), 2)

    def test_snapshot_does_not_include_ignored_secrets(self):
        (self.root / '.env').write_text('SECRET=private')
        (self.root / 'new.txt').write_text('public')
        path = backup.make(self.root)
        self.assertNotIn('.env', [r['path'] for r in backup.inspect_archive(path)[0]['files']])

    def test_restore_refuses_different_head_and_keeps_archive(self):
        (self.root / 'tracked.txt').write_text('backup')
        path = backup.make(self.root)
        self.git(self.root, 'add', 'tracked.txt'); self.git(self.root, 'commit', '-m', 'later')
        with self.assertRaises(c.ToolError): backup.restore(self.root, path, yes=True)
        self.assertTrue(path.exists())

    def test_restore_refuses_dirty_worktree(self):
        (self.root / 'tracked.txt').write_text('backup'); path = backup.make(self.root)
        (self.root / 'tracked.txt').write_text('new work')
        with self.assertRaises(c.ToolError): backup.restore(self.root, path, yes=True)
        self.assertEqual((self.root / 'tracked.txt').read_text(), 'new work')

    def test_prepare_is_idempotent_and_journal_central(self):
        (self.main / '.env').write_text('fixture')
        sessions.prepare(self.root)
        sessions.prepare(self.root)
        self.assertTrue((self.root / 'doc').is_symlink())
        self.assertEqual((self.root / 'doc').resolve(), self.main / 'doc')
        self.assertFalse((self.root / 'sessions_codex').exists())
        self.assertTrue((self.main / 'sessions_codex/1234_réglage.jsont').exists())
        self.assertEqual((self.root / '.env').stat().st_mode & 0o777, 0o600)
        self.assertEqual(os.readlink(self.root / 'tmp'), '../project/tmp')
        self.assertEqual((self.root / 'tmp').resolve(), self.main / 'tmp')

    def test_shared_documents_migrates_unique_files_and_preserves_original(self):
        (self.root / 'doc/nested').mkdir(parents=True)
        (self.root / 'doc/nested/local.txt').write_text('local notes')
        (self.root / 'doc/tools').symlink_to(TOOLS, target_is_directory=True)
        sessions.shared_documents(self.root)
        sessions.shared_documents(self.root)
        self.assertEqual((self.root / 'doc').resolve(), self.main / 'doc')
        self.assertEqual((self.main / 'doc/nested/local.txt').read_text(), 'local notes')
        backups = list((self.main / 'doc/worktree-doc-backups').glob('*/doc-*/doc/nested/local.txt'))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(), 'local notes')
        (self.root / 'doc/shared.txt').write_text('written through link')
        self.assertEqual((self.main / 'doc/shared.txt').read_text(), 'written through link')
        self.assertEqual(self.git(self.root, 'status', '--porcelain'), '')

    def test_shared_documents_refuses_conflicts_without_copying(self):
        (self.root / 'doc').mkdir()
        (self.root / 'doc/a-new.txt').write_text('unique')
        (self.root / 'doc/notes.txt').write_text('local')
        (self.main / 'doc/notes.txt').write_text('central')
        for path in (self.root / 'doc/notes.txt', self.main / 'doc/notes.txt'):
            os.utime(path, ns=(1000000000, 1000000000))
        with self.assertRaisesRegex(c.ToolError, 'Documents différents'):
            sessions.shared_documents(self.root)
        self.assertFalse((self.root / 'doc').is_symlink())
        self.assertFalse((self.main / 'doc/a-new.txt').exists())
        self.assertEqual((self.root / 'doc/notes.txt').read_text(), 'local')
        self.assertEqual((self.main / 'doc/notes.txt').read_text(), 'central')

    def test_shared_documents_keeps_newest_version_and_backs_up_both(self):
        (self.root / 'doc').mkdir()
        for name, local_time, central_time in (('local.txt', 200, 100), ('central.txt', 100, 200)):
            local, central = self.root / 'doc' / name, self.main / 'doc' / name
            local.write_text('local ' + name)
            central.write_text('central ' + name)
            os.utime(local, (local_time, local_time))
            os.utime(central, (central_time, central_time))
        sessions.shared_documents(self.root)
        self.assertEqual((self.main / 'doc/local.txt').read_text(), 'local local.txt')
        self.assertEqual((self.main / 'doc/central.txt').read_text(), 'central central.txt')
        archive = next((self.main / 'doc/worktree-doc-backups').glob('*/doc-*'))
        self.assertEqual((archive / 'doc/local.txt').read_text(), 'local local.txt')
        self.assertEqual((archive / 'doc/central.txt').read_text(), 'local central.txt')
        self.assertEqual((archive / 'central/local.txt').read_text(), 'central local.txt')

    def test_shared_documents_refuses_tracked_files_and_foreign_link(self):
        (self.root / 'doc').mkdir()
        (self.root / 'doc/tracked.txt').write_text('tracked')
        self.git(self.root, 'add', '-f', 'doc/tracked.txt')
        with self.assertRaisesRegex(c.ToolError, 'Documents suivis'):
            sessions.shared_documents(self.root)
        self.assertTrue((self.root / 'doc/tracked.txt').is_file())
        self.git(self.root, 'reset', '--', 'doc/tracked.txt')
        shutil.rmtree(self.root / 'doc')
        (self.root / 'doc').symlink_to(self.main / 'other')
        with self.assertRaisesRegex(c.ToolError, 'Lien local différent'):
            sessions.shared_documents(self.root)

    def test_shared_tmp_discards_local_contents_and_preserves_main(self):
        (self.main / 'tmp').mkdir()
        (self.main / 'tmp/shared.txt').write_text('shared')
        (self.root / 'tmp/nested').mkdir(parents=True)
        (self.root / 'tmp/nested/local.txt').write_text('discard')
        sessions.shared_tmp(self.root)
        sessions.shared_tmp(self.root)
        self.assertFalse((self.root / 'tmp/nested').exists())
        self.assertEqual((self.root / 'tmp/shared.txt').read_text(), 'shared')
        self.assertEqual(self.git(self.root, 'status', '--porcelain'), '')

    def test_shared_tmp_replaces_old_link_without_deleting_its_target(self):
        other = Path(self.temp.name) / 'other-tmp'
        other.mkdir(); (other / 'keep.txt').write_text('keep')
        (self.root / 'tmp').symlink_to(other)
        sessions.shared_tmp(self.root)
        self.assertEqual((other / 'keep.txt').read_text(), 'keep')
        self.assertEqual((self.root / 'tmp').resolve(), self.main / 'tmp')

    def test_shared_tmp_cleans_docker_owned_directory_with_only_tmp_mounted(self):
        (self.root / 'tmp').mkdir()
        config = {'services': {'shell': {'image': 'fixture-image'}}}
        with patch.object(sessions.shutil, 'rmtree', side_effect=PermissionError('fixture')), \
                patch.object(sessions, 'run', side_effect=[Mock(stdout=json.dumps(config)), Mock()]) as run:
            sessions.shared_tmp(self.root)
        args = run.call_args.args[0]
        self.assertEqual(args[args.index('--mount')+1], 'type=bind,source=%s,target=/worktree-tmp' % (self.root / 'tmp'))
        self.assertIn('--pull=never', args)
        self.assertIn('--network=none', args)
        self.assertEqual(args.count('--mount'), 1)
        self.assertEqual((self.root / 'tmp').resolve(), self.main / 'tmp')

    def test_shared_tmp_refuses_tracked_files(self):
        (self.root / 'tmp').mkdir(); (self.root / 'tmp/tracked').write_text('keep')
        self.git(self.root, 'add', '-f', 'tmp/tracked')
        with self.assertRaisesRegex(c.ToolError, 'suivis par Git'):
            sessions.shared_tmp(self.root)
        self.assertEqual((self.root / 'tmp/tracked').read_text(), 'keep')

    def test_tmp_mount_exposes_relative_link_destination_in_docker(self):
        mounts = sessions.tmp_mount(self.root)
        self.assertEqual(mounts, [{'type': 'bind', 'source': str(self.main / 'tmp'),
                                  'target': '/project/tmp', 'bind': {'create_host_path': False}}])
        self.assertEqual(sessions.tmp_mount(self.main), [])

    def test_prepare_does_not_overwrite_local_profile(self):
        (self.root / c.setting_json('PROFILE_FILES')[0]).write_text('custom')
        with self.assertRaises(c.ToolError): sessions.prepare(self.root)
        self.assertEqual((self.root / c.setting_json('PROFILE_FILES')[0]).read_text(), 'custom')

    def test_prepare_supports_legacy_hook_path(self):
        sessions.prepare(self.root)
        hook = self.root / '.codex/hooks/session_journal.py'
        self.assertTrue(hook.is_symlink())
        self.assertEqual(hook.resolve(), TOOLS / 'session_journal.py')
        payload = {'hook_event_name': 'UserPromptSubmit', 'cwd': str(self.root),
                   'prompt': 'Test chemin historique', 'session_id': 'fixture', 'turn_id': 'legacy'}
        subprocess.run([sys.executable, str(hook)], cwd=self.root,
                       input=json.dumps(payload), text=True, check=True, capture_output=True)
        data = json.loads((self.main / 'sessions_codex/1234_réglage.jsont').read_text())
        self.assertEqual(data['messages'][-1]['content'], payload['prompt'])
        self.assertFalse((self.root / 'sessions_codex').exists())

    def test_journal_hook_resolves_main_from_worktree_and_deduplicates(self):
        payload = {'hook_event_name': 'UserPromptSubmit', 'cwd': str(self.root),
                   'session_id': 's', 'turn_id': 't', 'prompt': 'bonjour'}
        journal.process_event(payload); journal.process_event(payload)
        path = self.main / 'sessions_codex/1234_réglage.jsont'
        self.assertEqual(len(json.loads(path.read_text())['messages']), 1)
        self.assertFalse((self.root / 'sessions_codex').exists())

    def test_journal_ticket_does_not_create_nested_directory(self):
        self.git(self.main, 'branch', 'tickets/t_5678_ticket')
        root = cli.ensure_worktree('t_5678_ticket')
        _, branch, path = journal.journal_location(str(root))
        self.assertEqual(path.name, 't_5678_ticket.jsont')

    def test_codex_resumes_with_worktree_and_central_write_access(self):
        sessions.prepare(self.root)
        transcript = self.main / 'transcript.jsonl'
        transcript.write_text(json.dumps({'type': 'session_meta', 'payload': {'id': 'abc', 'cwd': str(self.root)}})+'\n')
        (self.main / 'sessions_codex/1234_réglage.jsonl').symlink_to(transcript)
        with patch.object(sessions.shutil, 'which', return_value='/bin/codex'):
            args = sessions.codex_command(self.root)
        self.assertEqual(args[1:3], ['resume', 'abc'])
        self.assertEqual(args[args.index('-C')+1], str(self.root))
        self.assertEqual(args[args.index('--add-dir')+1], str(self.main / 'sessions_codex'))
        self.assertIn(str(self.main / 'doc'), [args[i+1] for i, arg in enumerate(args) if arg == '--add-dir'])

    def runtime_fixture(self, root=None, mode='http', port=3002, status='active'):
        root = root or self.root
        state = {'version': 2, 'project': docker.project_for(root), 'status': status,
                 'worktree': str(root), 'mode': mode, 'override': '/tmp/override.yml',
                 'port': port, 'activation_id': 'fixture-activation'}
        docker.write_state(state)
        return state

    def test_docker_stop_targets_own_project_and_preserves_other_worktree(self):
        other = self.runtime_fixture(self.main, port=3000)
        own = self.runtime_fixture()
        with patch.object(docker, 'app_mounts', return_value=[('console', self.main, 'other-id', True)]), \
                patch.object(docker, 'run', return_value=Mock(returncode=0)) as run:
            docker.stop(self.root)
        self.assertEqual(run.call_count, 1)
        args = run.call_args.args[0]
        self.assertEqual(args[args.index('-p')+1], own['project'])
        self.assertEqual(docker.read_state(self.main), other)
        self.assertEqual(docker.read_state(self.root)['status'], 'stopped')

    def test_docker_stop_infrastructure_refused_when_other_worktree_runs(self):
        self.runtime_fixture()
        with patch.object(docker, 'app_mounts', return_value=[('console', self.main, 'other-id', True)]), patch.object(docker, 'run') as run:
            with self.assertRaisesRegex(c.ToolError, 'Infrastructure utilisée'):
                docker.stop(self.root, infrastructure=True)
        run.assert_not_called()

    def test_main_stop_preserves_independent_task_servers(self):
        self.runtime_fixture(self.main, port=3000)
        def mounts(running=True, project=None):
            return [] if project == c.DOCKER_PROJECT else [('web_http', self.root, 'task-id', True)]
        with patch.object(docker, 'app_mounts', side_effect=mounts), patch.object(docker, 'run') as run:
            docker.stop(self.main)
        self.assertEqual(run.call_args.args[0][3], c.DOCKER_PROJECT)

    def test_main_stop_refuses_legacy_task_even_with_main_state(self):
        self.runtime_fixture(self.main, port=3000)
        with patch.object(docker, 'app_mounts', return_value=[('web_http', self.root, 'legacy-id', True)]), \
                patch.object(docker, 'run') as run:
            with self.assertRaisesRegex(c.ToolError, 'Ancien runtime'):
                docker.stop(self.main)
        run.assert_not_called()

    def test_master_stop_all_includes_ci_and_marks_other_worktree_stopped(self):
        c.atomic(docker.STATE / 'active.json', {'status': 'active', 'worktree': str(self.root)})
        self.runtime_fixture()
        with patch.object(docker, 'docker', side_effect=[Mock(stdout='app db'), Mock(stdout='ci'), Mock(stdout='task'), Mock()]) as run:
            docker.stop_all(self.main)
        self.assertEqual(run.call_args_list[0].args[-1], 'label=com.docker.compose.project='+c.DOCKER_PROJECT)
        self.assertEqual(run.call_args_list[1].args[-1], 'label=com.docker.compose.project='+c.setting('CI_PROJECT'))
        self.assertEqual(run.call_args_list[2].args[-1], 'label='+docker.RUNTIME_LABEL+'=worktree')
        self.assertEqual(run.call_args.args, ('stop', 'app', 'db', 'ci', 'task'))
        self.assertEqual(c.read_json(docker.STATE / 'active.json')['status'], 'stopped')

    def test_worktree_stop_all_remains_scoped(self):
        with patch.object(docker, 'stop') as stop, patch.object(docker, 'docker') as run:
            docker.stop_all(self.root)
        stop.assert_called_once_with(self.root, False)
        run.assert_not_called()

    def test_master_stop_all_handles_no_running_containers(self):
        with patch.object(docker, 'docker', return_value=Mock(stdout='')) as run:
            docker.stop_all(self.main)
        self.assertEqual(run.call_count, 3)

    def test_relaunch_checks_infrastructure_even_when_application_is_running(self):
        for role, service in (('console', 'console'), ('server', 'web')):
            with self.subTest(role=role):
                self.runtime_fixture(mode='https')
                with patch.object(docker, 'app_mounts', return_value=[(service, self.root, 'id', True)]), \
                        patch.object(docker, 'run', return_value=Mock(returncode=0)) as run, \
                        patch.object(docker, 'wait_for_server'):
                    docker.ensure_service(self.root, role)
                self.assertEqual(run.call_args_list[0].args[0][-6:], ['up', '-d', '--no-recreate', *docker.INFRA])
                self.assertIn('mysqladmin', run.call_args_list[1].args[0])
                self.assertEqual(run.call_args.args[0][-5:], ['up', '-d', '--no-deps', '--no-recreate', 'caddy'])
                self.assertEqual(run.call_count, 3)

    def test_infrastructure_failure_prevents_application_start(self):
        self.runtime_fixture()
        with patch.object(docker, 'app_mounts', return_value=[]), \
                patch.object(docker, 'run', side_effect=c.ToolError('infra indisponible')) as run:
            with self.assertRaisesRegex(c.ToolError, 'infra indisponible'):
                docker.ensure_service(self.root, 'console')
        run.assert_called_once()

    def test_activation_starts_own_project_without_stopping_others_or_infrastructure(self):
        other = self.runtime_fixture(self.main, port=3000)
        commands = []
        def execute(args, **kwargs):
            commands.append(args)
            return Mock(returncode=0)
        with patch.object(docker, 'app_mounts', return_value=[('web_http', self.main, 'old', True)]), \
                patch.object(docker, 'generate', return_value=self.main / 'override.yml'), \
                patch.object(docker, 'published_ports', return_value={}), \
                patch.object(docker, 'port_available', return_value=True), patch.object(docker, 'containers', return_value=[]), \
                patch.object(docker, 'run', side_effect=execute), \
                patch.object(docker, 'wait_for_server') as wait:
            state = docker.activate(self.root)
        stopped = next(args for args in commands if 'stop' in args)
        self.assertEqual(stopped[stopped.index('stop')+1:], docker.APPS + ['caddy'])
        started = next(args for args in commands if '--force-recreate' in args)
        self.assertEqual(stopped[stopped.index('-p')+1], docker.project_for(self.root))
        self.assertEqual(started[started.index('-p')+1], docker.project_for(self.root))
        self.assertEqual(docker.read_state(self.main), other)
        wait.assert_called_once_with(self.root, 'web_http')
        self.assertEqual(state['worktree'], str(self.root))
        self.assertEqual(state['status'], 'active')
        self.assertEqual(state['port'], 3002)
        self.assertTrue(state['activation_id'])

    def test_invalid_target_config_preserves_current_runtime(self):
        (self.main / c.setting('COMPOSE_FILE')).write_text('main')
        (self.root / c.setting('COMPOSE_FILE')).write_text('different')
        original = docker.setting_json
        with patch.object(docker, 'setting_json', side_effect=lambda name, default=None: {} if name == 'COMPOSE_CONFIG' else original(name, default)), \
                patch.object(docker, 'app_mounts', return_value=[('web_http', self.main, 'old', True)]), \
                patch.object(docker, 'run') as run, patch.object(docker, 'stop_legacy') as stop:
            with self.assertRaisesRegex(c.ToolError, 'Configuration différente'):
                docker.activate(self.root)
            run.assert_not_called(); stop.assert_not_called()

    def test_published_port_detection_targets_host_binding_not_container_port(self):
        rows = [
            {'Id': 'owner', 'Name': '/other-project', 'State': {'Running': True},
             'NetworkSettings': {'Ports': {'80/tcp': [{'HostPort': '3000'}]}}},
            {'Id': 'unrelated', 'Name': '/keep', 'State': {'Running': True},
             'NetworkSettings': {'Ports': {'3000/tcp': [{'HostPort': '13000'}]}}},
            {'Id': 'infra', 'Name': '/mysql', 'State': {'Running': True},
             'NetworkSettings': {'Ports': {'3306/tcp': None}}},
        ]
        with patch.object(docker, 'docker', side_effect=[Mock(stdout='owner unrelated infra'),
                          Mock(stdout=json.dumps(rows))]) as run:
            ports = docker.published_ports()
        self.assertEqual(set(ports), {3000, 13000})
        self.assertEqual(run.call_count, 2)

    def test_runtime_tabs_reconnect_when_worktree_becomes_active_again(self):
        folder = atelier.directory(self.root)
        state = {'status': 'active', 'worktree': str(self.root), 'activation_id': 'first', 'mode': 'http'}
        c.atomic(atelier.STATE / 'active.json', state)
        record = {'role': 'server'}
        waits = []
        def process_wait():
            waits.append(True)
            if len(waits) == 1:
                c.atomic(atelier.STATE / 'active.json', dict(state, worktree=str(self.main), activation_id='other'))
            else:
                (folder / 'close').touch()
            return 0
        def next_activation(_):
            c.atomic(atelier.STATE / 'active.json', dict(state, activation_id='back'))
        with patch.object(docker, 'service_command', return_value=['docker', 'attach', 'container']) as command, \
                patch.object(atelier.subprocess, 'Popen', return_value=Mock(pid=os.getpid(), wait=process_wait)) as spawn, \
                patch.object(atelier.time, 'sleep', side_effect=next_activation):
            atelier.runtime_tab(self.root, 'server', folder, record, folder / 'server.json')
        self.assertEqual(spawn.call_count, 2)
        self.assertEqual(command.call_count, 2)
        self.assertEqual(record['status'], 'waiting')
        self.assertNotIn('child_pid', record)

    def test_relance_docker_selects_named_existing_worktree(self):
        previous = Path.cwd()
        try:
            os.chdir(self.main)
            for command, role in (('relance_docker', 'server'), ('relance_serveur', 'server'), ('relance_console', 'console')):
                with self.subTest(command=command), \
                        patch.object(sys, 'argv', ['worktree_tools.py', command, '1234_réglage']), \
                        patch.object(atelier, 'relaunch_runtime') as relaunch:
                    cli.main()
                    relaunch.assert_called_once_with(self.root, role, None)
        finally:
            os.chdir(previous)

    def test_console_relaunch_keeps_server_running(self):
        self.runtime_fixture()
        server = [('web_http', self.root, 'server-id', True)]
        with patch.object(docker, 'app_mounts', side_effect=[server, server + [('console', self.root, 'console-id', True)]]), \
                patch.object(docker, 'run', return_value=Mock(returncode=0)) as run, \
                patch.object(docker, 'activate') as activate, \
                patch.object(docker, 'generate', return_value=self.main / 'updated.compose.yml') as generate:
            docker.ensure_service(self.root, 'console')
        generate.assert_called_once_with(self.root, 3002)
        self.assertIn(str(self.main / 'updated.compose.yml'), run.call_args.args[0])
        self.assertEqual(run.call_args.args[0][-4:], ['up', '-d', '--no-deps', 'console'])
        self.assertEqual(run.call_args_list[0].args[0][-6:], ['up', '-d', '--no-recreate', *docker.INFRA])
        self.assertIn('mysqladmin', run.call_args_list[1].args[0])
        activate.assert_not_called()

    def test_server_relaunch_does_not_start_closed_console(self):
        self.runtime_fixture()
        with patch.object(docker, 'app_mounts', return_value=[]), \
                patch.object(docker, 'run', return_value=Mock(returncode=0)) as run, \
                patch.object(docker, 'wait_for_server') as wait, \
                patch.object(docker, 'generate', return_value=self.main / 'updated.compose.yml') as generate:
            docker.ensure_service(self.root, 'server')
        generate.assert_called_once_with(self.root, 3002)
        self.assertIn(str(self.main / 'updated.compose.yml'), run.call_args.args[0])
        self.assertEqual(run.call_args.args[0][-4:], ['up', '-d', '--no-deps', 'web_http'])
        self.assertEqual(run.call_args_list[0].args[0][-6:], ['up', '-d', '--no-recreate', *docker.INFRA])
        self.assertIn('mysqladmin', run.call_args_list[1].args[0])
        wait.assert_called_once_with(self.root, 'web_http', require_console=False)

    def test_relaunch_notifies_existing_waiting_tab(self):
        folder = atelier.directory(self.root)
        c.atomic(folder / 'atelier.json', {'generation': 'gen'})
        c.atomic(folder / 'console.json', {'pid': os.getpid(), 'start': atelier.identity(os.getpid()),
                                          'generation': 'gen', 'worktree': str(self.root), 'runtime_input': True})
        with patch.object(docker, 'ensure_service'), patch.object(atelier, 'run') as run:
            atelier.relaunch_runtime(self.root, 'console')
        self.assertEqual(c.read_json(folder / 'console.attach.json')['generation'], 'gen')
        run.assert_not_called()

    def test_relaunch_without_managed_tab_attaches_in_calling_terminal(self):
        with patch.object(docker, 'ensure_service'), \
                patch.object(docker, 'service_command', return_value=['docker', 'attach', 'id']), \
                patch.object(atelier, 'run', return_value=Mock(returncode=0)) as run:
            atelier.relaunch_runtime(self.root, 'console')
        run.assert_called_once_with(['docker', 'attach', 'id'], cwd=self.root)

    def test_runtime_prompt_accepts_enter_and_service_command(self):
        for line in ('\n', 'relance_console.sh\n'):
            with patch.object(atelier.sys, 'stdin', Mock(isatty=lambda: True, readline=lambda: line)), \
                    patch.object(atelier.select, 'select', return_value=([1], [], [])):
                self.assertEqual(atelier.runtime_input('console'), 'restart')

    def test_runtime_tab_relaunches_manually_after_exit(self):
        folder = atelier.directory(self.root)
        c.atomic(atelier.STATE / 'active.json', {'status': 'active', 'worktree': str(self.root),
                                              'activation_id': 'same', 'mode': 'http'})
        waits = []
        def process_wait():
            waits.append(True)
            if len(waits) == 2: (folder / 'close').touch()
            return 0
        with patch.object(docker, 'service_command', return_value=['docker', 'attach', 'container']), \
                patch.object(docker, 'ensure_service') as ensure, \
                patch.object(atelier, 'runtime_input', return_value='restart'), \
                patch.object(atelier.subprocess, 'Popen', return_value=Mock(pid=os.getpid(), wait=process_wait)) as spawn:
            atelier.runtime_tab(self.root, 'console', folder, {}, folder / 'console.json')
        self.assertEqual(spawn.call_count, 2)
        ensure.assert_called_once_with(self.root, 'console')

    def test_runtime_tabs_do_not_reattach_repeatedly_after_detach(self):
        folder = atelier.directory(self.root)
        state = {'status': 'active', 'worktree': str(self.root), 'activation_id': 'same', 'mode': 'http'}
        c.atomic(atelier.STATE / 'active.json', state)
        with patch.object(docker, 'service_command', return_value=['docker', 'attach', 'container']), \
                patch.object(atelier.subprocess, 'Popen', return_value=Mock(pid=os.getpid(), wait=lambda: 0)) as spawn, \
                patch.object(atelier.time, 'sleep', side_effect=lambda _: (folder / 'close').touch()):
            atelier.runtime_tab(self.root, 'console', folder, {}, folder / 'console.json')
        spawn.assert_called_once()

    def test_closing_inactive_worktree_preserves_other_runtime(self):
        sessions.prepare(self.root)
        with patch.object(docker, 'app_mounts', return_value=[('web_http', self.main, 'other', True)]), \
                patch.object(docker, 'stop') as stop:
            atelier.close(self.root)
        stop.assert_not_called()
        self.assertTrue((atelier.directory(self.root) / 'close').exists())

    @unittest.skipUnless(shutil.which('docker'), 'Docker Compose requis, sans daemon')
    def test_profile_operates_without_any_project_compose_file(self):
        self.assertFalse((self.main / c.setting('COMPOSE_FILE')).exists())
        self.assertFalse((self.root / c.setting('CI_COMPOSE_FILE')).exists())
        for root, port in ((self.main, c.MAIN_PORT), (self.root, c.PORT_START)):
            generated = docker.generate(root, port)
            command = docker.base(generated, docker.project_for(root))
            config = json.loads(c.run(command + ['config', '--format', 'json'], capture=True).stdout)
            self.assertIn(c.CONSOLE_SERVICE, config['services'])
            for service in docker.APPS:
                mounts = [v for v in config['services'][service]['volumes'] if v['target'] == c.APP_PATH]
                self.assertEqual(mounts[0]['source'], str(root))
        ci = docker.compose_file(self.root, ci=True)
        config = json.loads(c.run(['docker', 'compose', '-p', c.setting('CI_PROJECT'),
                                  '--project-directory', str(self.root), '-f', str(ci),
                                  'config', '--format', 'json'], capture=True).stdout)
        self.assertIn(c.setting('CI_SERVICE'), config['services'])

    def test_compose_profile_is_self_contained_private_and_preserves_relative_paths(self):
        profile = {'services': {'custom': {'image': 'example.invalid/image:fixture',
                                         'env_file': ['.env'], 'volumes': ['./data:/data']}},
                   'volumes': {'storage': {}}, 'networks': {'private': {}}}
        original = docker.setting_json
        def settings(name, default=None):
            if name in ('COMPOSE_CONFIG', 'CI_COMPOSE_CONFIG'): return profile
            return original(name, default)
        with patch.object(docker, 'setting_json', side_effect=settings):
            main = docker.compose_file()
            ci = docker.compose_file(self.root, ci=True)
            self.assertEqual(json.loads(main.read_text()), profile)
            self.assertEqual(json.loads(ci.read_text()), profile)
            self.assertEqual(main.stat().st_mode & 0o777, 0o600)
            args = docker.base()
            self.assertEqual(args[args.index('--project-directory') + 1], str(self.main))
            self.assertEqual(args[args.index('-f') + 1], str(main))
            profile['services']['custom']['image'] = 'example.invalid/image:updated'
            updated = docker.compose_file()
            self.assertNotEqual(main, updated)
            self.assertEqual(json.loads(main.read_text())['services']['custom']['image'], 'example.invalid/image:fixture')
        self.assertNotEqual(main, ci)
        self.assertFalse((self.main / c.setting('COMPOSE_FILE')).exists())

    def test_empty_compose_profile_uses_configured_files(self):
        original = docker.setting_json
        with patch.object(docker, 'setting_json', side_effect=lambda name, default=None: {} if name in ('COMPOSE_CONFIG', 'CI_COMPOSE_CONFIG') else original(name, default)):
            self.assertEqual(docker.compose_file(), self.main / c.setting('COMPOSE_FILE'))
            self.assertEqual(docker.compose_file(self.root, ci=True), self.root / c.setting('CI_COMPOSE_FILE'))

    def test_ci_uses_profile_and_task_project_directory(self):
        config = self.main / 'profile-ci.compose.json'
        with patch.object(sessions, 'tmp_mount', return_value=[]), \
                patch.object(docker, 'compose_file', return_value=config) as source, \
                patch.object(cli, 'run', return_value=Mock(returncode=0)) as run:
            cli.test_run(self.root, ['unit'])
        source.assert_called_once_with(self.root, ci=True)
        args = run.call_args.args[0]
        self.assertEqual(args[args.index('--project-directory') + 1], str(self.root))
        self.assertEqual(args[args.index('-f') + 1], str(config))

    def test_application_commands_can_use_another_language(self):
        effective = {'services': {s: {'volumes': [{'target': '/app', 'source': str(self.main)}]} for s in docker.APPS}}
        original = docker.setting_json
        def settings(name, default=None):
            if name == 'SERVER_COMMANDS': return {'http': ['node', 'server.js', '--title', 'two words']}
            if name == 'CONSOLE_COMMAND': return ['node', '-i']
            return original(name, default)
        with patch.object(docker, 'setting_json', side_effect=settings), \
                patch.object(docker, 'run', return_value=Mock(stdout=json.dumps(effective))):
            config = json.loads(docker.generate(self.main).read_text())
        self.assertEqual(config['services'][docker.MODES['http']]['command'], ['node', 'server.js', '--title', 'two words'])
        self.assertEqual(config['services'][c.CONSOLE_SERVICE]['command'], ['node', '-i'])
        self.assertNotIn('command', config['services'][c.SHELL_SERVICE])

    def test_main_docker_config_has_six_targets_and_own_cache(self):
        effective = {'services': {s: {'volumes': [{'target': '/app', 'source': str(self.main)}]} for s in docker.APPS}}
        with patch.object(docker, 'run', return_value=Mock(stdout=json.dumps(effective))):
            path = docker.generate(self.main)
        data = json.loads(path.read_text())
        self.assertEqual(len(data['services']), 6)
        self.assertIn(c.key(self.main), data['volumes'][c.setting_json('VOLUMES')[0]]['name'])
        self.assertFalse(data['services']['web_http']['volumes'][0]['bind']['create_host_path'])

    def test_docker_leaves_boot_symlinks_unmounted(self):
        effective = {'services': {s: {'volumes': [{'target': '/app', 'source': str(self.main)}]} for s in docker.APPS}}
        # Simuler les dépendances présentes sur master sans créer de dossier lib.
        with patch.object(Path, 'is_dir', return_value=True), \
                patch.object(docker, 'run', return_value=Mock(stdout=json.dumps(effective))):
            path = docker.generate(self.main)
        data = json.loads(path.read_text())
        for service in docker.APPS:
            self.assertEqual([v['target'] for v in data['services'][service]['volumes']], ['/app'])

    @unittest.skipUnless(shutil.which('docker'), 'Docker Compose requis pour vérifier les configurations, sans daemon')
    def test_generated_task_configs_isolate_ports_networks_and_keep_base_unchanged(self):
        source = TOOLS.parent.parent / c.setting('COMPOSE_FILE')
        copied = self.main / source.name
        copied.write_bytes(source.read_bytes())
        original = copied.read_bytes()
        self.git(self.main, 'branch', 'feature/2345_other')
        other = cli.ensure_worktree('2345_other')
        effective = []
        for root, port in ((self.root, 3002), (other, 3009)):
            path = docker.generate(root, port)
            config = json.loads(c.run(docker.base(path, docker.project_for(root))+['config', '--format', 'json'], capture=True).stdout)
            effective.append(config)
            for mode, argv in c.setting_json('SERVER_COMMANDS', {}).items():
                self.assertEqual(config['services'][docker.MODES[mode]]['command'], argv)
            self.assertEqual(config['services'][c.CONSOLE_SERVICE]['command'], c.setting_json('CONSOLE_COMMAND'))
            self.assertEqual({int(p['published']) for p in config['services']['web_http']['ports']}, {port})
            self.assertEqual(config['services']['web'].get('ports', []), [])
            self.assertEqual({int(p['published']) for p in config['services']['debug']['ports']}, {docker.debugger_port(port)})
            self.assertEqual({int(p['published']) for p in config['services']['caddy']['ports']}, {port})
            for service in docker.APPS:
                app = config['services'][service]
                mounts = [v for v in app['volumes'] if v['target'] == '/app']
                self.assertEqual([v['source'] for v in mounts], [str(root)])
                self.assertFalse(mounts[0]['bind']['create_host_path'])
                shared = [v for v in app['volumes'] if v['target'] == '/project/tmp']
                self.assertEqual([v['source'] for v in shared], [str(self.main / 'tmp')])
                self.assertFalse(any(v['target'] in ('/app/lib', '/app/vendor') for v in app['volumes']))
                self.assertEqual(app.get('depends_on', {}), {})
                self.assertNotIn('app', (app['networks']['shared_frontend'] or {}).get('aliases', []))
            self.assertTrue(config['networks']['backend']['external'])
            self.assertEqual(config['networks']['backend']['name'], c.setting_json('EXTERNAL_NETWORKS')['backend'])
            self.assertEqual(config['networks']['shared_frontend']['name'], c.setting_json('EXTERNAL_NETWORKS')['shared_frontend'])
            self.assertEqual(config['services']['caddy']['networks'].keys(), {'frontend'})
            self.assertTrue(set(docker.INFRA).isdisjoint(config['services']))
            self.assertTrue(path.is_relative_to(self.main / 'doc'))
            caddy = next(v['source'] for v in config['services']['caddy']['volumes'] if v['target'] == '/etc/caddy/Caddyfile')
            self.assertIn('X-Forwarded-Port %d' % port, Path(caddy).read_text())
        self.assertNotEqual(effective[0]['name'], effective[1]['name'])
        self.assertNotEqual(effective[0]['networks']['frontend']['name'], effective[1]['networks']['frontend']['name'])
        self.assertNotEqual(effective[0]['volumes'][c.setting_json('VOLUMES')[0]]['name'], effective[1]['volumes'][c.setting_json('VOLUMES')[0]]['name'])
        self.assertEqual(copied.read_bytes(), original)
        self.assertEqual(source.read_bytes(), original)

    def test_port_assignment_is_distinct_for_simultaneous_worktrees(self):
        self.runtime_fixture(port=3002)
        other = self.main.parent / 'project-2345_other'
        with patch.object(docker, 'published_ports', return_value={3002: {docker.project_for(self.root)}}), \
                patch.object(docker, 'port_available', return_value=True):
            self.assertEqual(docker.choose_port(other, 'http'), 3003)
            self.assertEqual(docker.choose_port(self.root, 'http'), 3002)

    def test_port_assignment_skips_external_owners_without_stopping_them(self):
        with patch.object(docker, 'published_ports', return_value={3002: {'unrelated-project'}}), \
                patch.object(docker, 'port_available', return_value=True), patch.object(docker, 'docker') as mutation:
            self.assertEqual(docker.choose_port(self.root, 'http'), 3003)
        mutation.assert_not_called()

    def test_stopped_worktree_restarts_on_same_port(self):
        self.runtime_fixture(port=3006, status='stopped')
        with patch.object(docker, 'published_ports', return_value={}), \
                patch.object(docker, 'port_available', return_value=True):
            self.assertEqual(docker.choose_port(self.root, 'http'), 3006)
            self.assertEqual(docker.choose_port(self.root, 'debug'), 3006)

    def test_stopped_worktree_uses_next_port_when_previous_port_is_busy(self):
        self.runtime_fixture(port=3006, status='stopped')
        with patch.object(docker, 'published_ports', return_value={3006: {'external'}}), \
                patch.object(docker, 'port_available', return_value=True), patch.object(docker, 'docker') as mutation:
            self.assertEqual(docker.choose_port(self.root, 'http'), 3007)
        mutation.assert_not_called()
        with patch.object(docker, 'published_ports', return_value={}), \
                patch.object(docker, 'port_available', side_effect=lambda port: port != 3006):
            self.assertEqual(docker.choose_port(self.root, 'http'), 3007)

    def test_stopped_worktree_uses_next_port_if_reserved_or_debugger_busy(self):
        self.runtime_fixture(port=3006, status='stopped')
        with patch.object(docker, 'published_ports', return_value={docker.debugger_port(3006): {'external'}}), \
                patch.object(docker, 'port_available', return_value=True):
            self.assertEqual(docker.choose_port(self.root, 'debug'), 3007)
        self.runtime_fixture(self.main.parent / 'project-2345_other', port=3006)
        with patch.object(docker, 'published_ports', return_value={}), \
                patch.object(docker, 'port_available', return_value=True):
            self.assertEqual(docker.choose_port(self.root, 'http'), 3007)

    def test_port_fallback_skips_busy_pairs_and_wraps_at_end_of_range(self):
        self.runtime_fixture(port=3008, status='stopped')
        self.runtime_fixture(self.main.parent / 'project-2345_other', port=3003)
        busy = {3008: {'external'}, docker.debugger_port(3009): {'external'}}
        with patch.object(docker, 'published_ports', return_value=busy), \
                patch.object(docker, 'port_available', side_effect=lambda port: port != 3002):
            self.assertEqual(docker.choose_port(self.root, 'debug'), 3004)

    def test_previous_port_does_not_bypass_exhausted_range(self):
        self.runtime_fixture(port=3006, status='stopped')
        with patch.object(docker, 'published_ports', return_value={}), \
                patch.object(docker, 'port_available', return_value=False):
            with self.assertRaisesRegex(c.ToolError, 'Aucun port libre dans la plage configurée'):
                docker.choose_port(self.root, 'http')

    def test_port_assignment_refuses_ninth_task_and_reuses_stopped_slot(self):
        for number, port in enumerate(docker.PORTS):
            self.runtime_fixture(self.main.parent / ('project-%d_other' % number), port=port)
        with patch.object(docker, 'published_ports', return_value={}), patch.object(docker, 'port_available', return_value=True):
            with self.assertRaisesRegex(c.ToolError, 'Aucun port libre dans la plage configurée'):
                docker.choose_port(self.root, 'http')
            old = docker.all_states()[0]
            old['status'] = 'stopped'; docker.write_state(old)
            self.assertEqual(docker.choose_port(self.root, 'http'), old['port'])

    def test_debug_port_assignment_skips_busy_debugger_binding(self):
        with patch.object(docker, 'published_ports', return_value={1234: {'another-debugger'}}), \
                patch.object(docker, 'port_available', return_value=True):
            self.assertEqual(docker.choose_port(self.root, 'debug'), 3003)

    def test_legacy_port_is_reused_only_when_mounted_on_own_worktree(self):
        item = {'State': {'Running': True},
                'Config': {'Labels': {'com.docker.compose.project': c.DOCKER_PROJECT, 'com.docker.compose.service': 'debug'}},
                'Mounts': [{'Destination': '/app', 'Source': str(self.root)}],
                'NetworkSettings': {'Ports': {'3000/tcp': [{'HostPort': '3002'}], '1234/tcp': [{'HostPort': '1234'}]}}}
        with patch.object(docker, 'published_ports', return_value={3002: {c.DOCKER_PROJECT}, 1234: {c.DOCKER_PROJECT}}), \
                patch.object(docker, 'containers', return_value=[item]), patch.object(docker, 'port_available', return_value=True):
            self.assertEqual(docker.choose_port(self.root, 'debug'), 3002)
            item['Mounts'][0]['Source'] = str(self.main)
            self.assertEqual(docker.choose_port(self.root, 'debug'), 3003)

    def test_port_availability_rejects_listening_non_docker_process(self):
        import socket
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(('127.0.0.1', 0)); listener.listen()
            self.assertFalse(docker.port_available(listener.getsockname()[1]))

    def test_per_worktree_state_is_preserved_when_other_task_activates(self):
        own = self.runtime_fixture()
        other = self.runtime_fixture(self.main, port=3000)
        self.assertEqual(docker.read_state(self.root), own)
        self.assertEqual(docker.read_state(self.main), other)
        self.assertEqual(len(docker.all_states()), 2)

    def test_legacy_state_is_saved_before_new_runtime_state(self):
        old = {'worktree': str(self.main), 'status': 'active', 'mode': 'http'}
        c.atomic(docker.STATE / 'active.json', old)
        self.runtime_fixture()
        self.assertEqual(docker.read_state(self.main), old)

    def test_runtime_tab_stays_attached_to_own_state_when_another_activates(self):
        self.runtime_fixture()
        folder = atelier.directory(self.root)
        def process_wait():
            self.runtime_fixture(self.main, port=3000)
            return 0
        with patch.object(docker, 'service_command', return_value=['docker', 'attach', 'own-id']), \
                patch.object(atelier.subprocess, 'Popen', return_value=Mock(pid=os.getpid(), wait=process_wait)) as spawn, \
                patch.object(atelier, 'runtime_input', return_value='exit'):
            atelier.runtime_tab(self.root, 'console', folder, {}, folder / 'console.json')
        spawn.assert_called_once()

    def test_legacy_migration_stops_only_containers_mounted_on_own_task(self):
        old = {'worktree': str(self.root), 'status': 'active', 'mode': 'http'}
        c.atomic(docker.STATE / 'active.json', old)
        rows = [
            {'Id': 'own-old', 'State': {'Running': True}, 'Mounts': [{'Destination': '/app', 'Source': str(self.root)}],
             'Config': {'Labels': {'com.docker.compose.project': c.DOCKER_PROJECT, 'com.docker.compose.service': 'web_http'}}},
            {'Id': 'another-task', 'State': {'Running': True}, 'Mounts': [{'Destination': '/app', 'Source': str(self.main)}],
             'Config': {'Labels': {'com.docker.compose.project': c.DOCKER_PREFIX+'other', 'com.docker.compose.service': 'web_http'}}},
        ]
        with patch.object(docker, 'containers', return_value=rows), patch.object(docker, 'docker') as mutation, patch.object(docker, 'run') as run:
            docker.stop_legacy(self.root)
        mutation.assert_called_once_with('stop', 'own-old')
        self.assertEqual(run.call_args.args[0][-2:], ['stop', 'caddy'])

    def test_attach_rejects_container_mounted_on_another_worktree(self):
        state = self.runtime_fixture()
        other_info = [{'Mounts': [{'Destination': '/app', 'Source': str(self.main)}]}]
        with patch.object(docker, 'app_mounts', return_value=[]), \
                patch.object(docker, 'run', return_value=Mock(stdout='wrong-container')), \
                patch.object(docker, 'docker', return_value=Mock(stdout=json.dumps(other_info))):
            with self.assertRaisesRegex(c.ToolError, 'vise un autre dossier'):
                docker.service_command(self.root, 'attach')
        self.assertEqual(state['project'], docker.project_for(self.root))

    def test_server_readiness_rejects_crash_after_compose_started(self):
        live = [('console', self.root, 'console-id', True), ('web_http', self.root, 'web-id', True)]
        with patch.object(docker, 'app_mounts', side_effect=[live, live[:1]]), \
                patch.object(docker, 'docker', return_value=Mock(returncode=1)), \
                patch.object(docker.time, 'sleep'):
            with self.assertRaisesRegex(c.ToolError, 'web_http'):
                docker.wait_for_server(self.root, 'web_http')

    def test_server_readiness_waits_for_listening_port(self):
        live = [('console', self.root, 'console-id', True), ('web_http', self.root, 'web-id', True)]
        with patch.object(docker, 'app_mounts', return_value=live), \
                patch.object(docker, 'docker', side_effect=[Mock(returncode=1), Mock(returncode=0)]) as probe, \
                patch.object(docker.time, 'sleep'):
            docker.wait_for_server(self.root, 'web_http')
        self.assertEqual(probe.call_count, 2)

    def test_server_readiness_times_out(self):
        live = [('console', self.root, 'console-id', True), ('web_http', self.root, 'web-id', True)]
        with patch.object(docker, 'app_mounts', return_value=live), \
                patch.object(docker, 'docker', return_value=Mock(returncode=1)):
            with self.assertRaisesRegex(c.ToolError, 'n’est pas prêt'):
                docker.wait_for_server(self.root, 'web_http', timeout=0)

    def managed_codex_fixture(self, ignore_term=False):
        code = ('import signal, time, pathlib\n'
                'def finish(signum, frame):\n'
                ' pathlib.Path("codex-flushed").write_text("saved")\n'
                ' raise SystemExit(0)\n'
                'signal.signal(signal.SIGTERM, %s)\n'
                'print("ready", flush=True)\n'
                'while True: time.sleep(.01)\n') % ('signal.SIG_IGN' if ignore_term else 'finish')
        child = subprocess.Popen([sys.executable, '-c', code], cwd=self.root,
                                 stdout=subprocess.PIPE, text=True)
        def cleanup():
            if child.poll() is None: child.kill()
            child.wait(timeout=3)
            child.stdout.close()
        self.addCleanup(cleanup)
        self.assertEqual(child.stdout.readline().strip(), 'ready')
        folder = atelier.directory(self.root)
        c.atomic(folder / 'atelier.json', {'generation': 'test-generation'})
        c.atomic(folder / 'codex.json', {'pid': os.getpid(), 'start': atelier.identity(os.getpid()),
                                       'child_pid': child.pid, 'child_start': atelier.identity(child.pid),
                                       'role': 'codex', 'worktree': str(self.root),
                                       'generation': 'test-generation', 'status': 'running'})
        return child, folder

    def test_close_codex_waits_for_flush_and_exit(self):
        child, folder = self.managed_codex_fixture()
        atelier.close_codex(self.root, folder)
        self.assertEqual(child.wait(timeout=3), 0)
        self.assertEqual((self.root / 'codex-flushed').read_text(), 'saved')

    def test_close_codex_does_not_signal_another_generation(self):
        child, folder = self.managed_codex_fixture()
        c.atomic(folder / 'atelier.json', {'generation': 'other'})
        with patch.object(atelier.signal, 'pidfd_send_signal') as send:
            with self.assertRaisesRegex(c.ToolError, 'Identité'):
                atelier.close_codex(self.root, folder)
            send.assert_not_called()
        self.assertIsNone(child.poll())

    def test_close_codex_timeout_prevents_docker_stop_and_backup(self):
        sessions.prepare(self.root)
        child, folder = self.managed_codex_fixture(ignore_term=True)
        original = atelier.close_codex
        with patch.object(atelier, 'close_codex', side_effect=lambda r, f: original(r, f, timeout=.1)), \
                patch.object(docker, 'stop') as stop, patch.object(backup, 'make') as save:
            with self.assertRaisesRegex(c.ToolError, 'aucun arrêt forcé'):
                atelier.close(self.root)
            stop.assert_not_called()
            save.assert_not_called()
        self.assertIsNone(child.poll())
        self.assertFalse((folder / 'close').exists())

    def test_close_codex_stale_pid_is_ignored(self):
        child, folder = self.managed_codex_fixture()
        record = c.read_json(folder / 'codex.json')
        record['child_start'] = 'old-start'
        c.atomic(folder / 'codex.json', record)
        with patch.object(atelier.signal, 'pidfd_send_signal') as send:
            atelier.close_codex(self.root, folder)
            send.assert_not_called()
        self.assertIsNone(child.poll())

    def test_close_bash_command_waits_for_saved_work(self):
        child, _ = self.managed_codex_fixture()
        with patch.object(atelier, 'ask', return_value=True) as ask:
            atelier.stop_shell_commands(os.getpid(), self.root, 'vim')
        self.assertEqual(child.wait(timeout=3), 0)
        self.assertEqual((self.root / 'codex-flushed').read_text(), 'saved')
        self.assertIn('shell Bash', ask.call_args.args[0])

    def test_close_bash_command_cancellation_sends_no_signal(self):
        child, _ = self.managed_codex_fixture()
        with patch.object(atelier, 'ask', return_value=False), \
                patch.object(atelier.signal, 'pidfd_send_signal') as send:
            with self.assertRaisesRegex(c.ToolError, 'annulée'):
                atelier.stop_shell_commands(os.getpid(), self.root, 'vim')
        send.assert_not_called()
        self.assertIsNone(child.poll())

    def test_close_bash_command_noninteractive_preserves_process(self):
        child, _ = self.managed_codex_fixture()
        with patch.object(c.os, 'isatty', return_value=False), \
                patch.object(atelier.signal, 'pidfd_send_signal') as send:
            with self.assertRaisesRegex(c.ToolError, 'terminal interactif'):
                atelier.stop_shell_commands(os.getpid(), self.root, 'vim')
        send.assert_not_called()
        self.assertIsNone(child.poll())

    def test_close_bash_command_timeout_does_not_force_kill(self):
        child, _ = self.managed_codex_fixture(ignore_term=True)
        with patch.object(atelier, 'ask', return_value=True):
            with self.assertRaisesRegex(c.ToolError, 'aucun arrêt forcé'):
                atelier.stop_shell_commands(os.getpid(), self.root, 'vim', timeout=.1)
        self.assertIsNone(child.poll())

    def test_close_bash_command_refuses_process_in_other_worktree(self):
        child, _ = self.managed_codex_fixture()
        with patch.object(atelier, 'ask') as ask, \
                patch.object(atelier.signal, 'pidfd_send_signal') as send:
            with self.assertRaisesRegex(c.ToolError, 'travaille ailleurs'):
                atelier.stop_shell_commands(os.getpid(), self.main, 'vim')
        ask.assert_not_called()
        send.assert_not_called()
        self.assertIsNone(child.poll())

    def test_close_bash_command_refuses_new_child_after_confirmation(self):
        child, _ = self.managed_codex_fixture()
        original = atelier.shell_children
        def confirm(message):
            # Une nouvelle commande apparait dans Bash pendant le choix.
            patcher = patch.object(atelier, 'shell_children', side_effect=lambda pid:
                                   original(pid) + ([999999] if pid == os.getpid() else []))
            patcher.start(); self.addCleanup(patcher.stop)
            return True
        with patch.object(atelier, 'ask', side_effect=confirm), \
                patch.object(atelier.signal, 'pidfd_send_signal') as send:
            with self.assertRaisesRegex(c.ToolError, 'ont changé'):
                atelier.stop_shell_commands(os.getpid(), self.root, 'vim')
        send.assert_not_called()
        self.assertIsNone(child.poll())

    def test_close_bash_wrapper_also_stops_child_application(self):
        child_code = ('import signal,time,pathlib\n'
                      'def finish(s,f):\n'
                      ' pathlib.Path("nested-flushed").write_text("saved")\n'
                      ' raise SystemExit(0)\n'
                      'signal.signal(signal.SIGTERM,finish)\n'
                      'print("ready",flush=True)\n'
                      'while True: time.sleep(.01)\n')
        wrapper_code = ('import subprocess,sys,signal,time\n'
                        'child=subprocess.Popen([sys.executable,"-c",%r],stdout=subprocess.PIPE,text=True)\n'
                        'child.stdout.readline()\n'
                        'def finish(s,f):\n'
                        ' child.wait(timeout=3)\n'
                        ' raise SystemExit(0)\n'
                        'signal.signal(signal.SIGTERM,finish)\n'
                        'print(child.pid,flush=True)\n'
                        'while True: time.sleep(.01)\n') % child_code
        wrapper = subprocess.Popen([sys.executable, '-c', wrapper_code], cwd=self.root,
                                   stdout=subprocess.PIPE, text=True)
        child_pid = int(wrapper.stdout.readline().strip())
        def cleanup():
            for pid in (child_pid, wrapper.pid):
                try: os.kill(pid, signal.SIGKILL)
                except ProcessLookupError: pass
            wrapper.wait(timeout=3); wrapper.stdout.close()
        self.addCleanup(cleanup)
        with patch.object(atelier, 'ask', return_value=True):
            atelier.stop_shell_commands(os.getpid(), self.root, 'vim')
        self.assertEqual(wrapper.wait(timeout=3), 0)
        self.assertEqual((self.root / 'nested-flushed').read_text(), 'saved')

    def test_close_continues_after_confirmed_bash_command_stop(self):
        sessions.prepare(self.root)
        child, folder = self.managed_codex_fixture()
        # Le test est le superviseur ; son enfant joue le shell de l'atelier.
        record = c.read_json(folder / 'codex.json')
        record.update(role='vim', shell_fallback=True)
        c.atomic(folder / 'vim.json', record)
        def terminate_command(*args):
            child.terminate(); child.wait(timeout=3)
        with patch.object(atelier, 'shell_children', side_effect=lambda pid:
                          [child.pid] if pid == child.pid and child.poll() is None else []), \
                patch.object(atelier, 'stop_shell_commands', side_effect=terminate_command) as stop_commands, \
                patch.object(atelier, 'close_codex', side_effect=lambda *args: child.wait(timeout=3)), \
                patch.object(docker, 'app_mounts', return_value=[]), \
                patch.object(backup, 'make') as save:
            atelier.close(self.root)
        stop_commands.assert_called_once_with(child.pid, self.root, 'vim')
        save.assert_called_once_with(self.root)
        self.assertEqual(c.read_json(folder / 'atelier.json')['status'], 'closed')

    def test_five_tabs_order(self):
        self.assertEqual(atelier.ROLES, list(c.setting_json('ROLE_TITLES').items()))

    def test_bash_tab_ignores_existing_development_vim_session(self):
        task = c.context(self.root, task=True)[2]
        (self.root / ('vimsession_' + task)).write_text('ancienne session')
        folder = atelier.directory(self.root)
        c.atomic(folder / 'atelier.json', {'generation': 'test'})
        (folder / 'close').touch()
        child = Mock(pid=os.getpid())
        child.wait.return_value = 0
        with patch.object(atelier.subprocess, 'Popen', return_value=child) as spawn, \
                patch.object(atelier, 'context', return_value=(self.root, 'feature/' + task, task)), \
                patch.object(atelier, 'vim_command') as vim:
            atelier.tab(self.root, 'vim', 'test')
            vim.assert_not_called()
            self.assertEqual(spawn.call_args.args[0], ['bash', '--rcfile', str(TOOLS / 'worktree_bashrc.sh'), '-i'])
            self.assertEqual(spawn.call_args.kwargs['cwd'], self.root)
            self.assertEqual(spawn.call_args.kwargs['env']['WT_SHELL_CLOSE'], str(folder / 'vim.close'))
        self.assertTrue(c.read_json(folder / 'vim.json')['shell_fallback'])

    def test_new_task_doc_vim_opens_notes_and_saves_first_session(self):
        sessions.prepare(self.root, new=True)
        folder = atelier.directory(self.root)
        folder.mkdir(parents=True, exist_ok=True)
        command = atelier.vim_command(self.root, 'vim_doc', folder)
        task = c.context(self.root, task=True)[2]
        notes = [str(self.root / 'doc' / (task + suffix)) for suffix in ('_suivi.txt', '.txt')]
        self.assertEqual(command[1:3], notes)
        self.assertIsNone(atelier.vim_command(self.root, 'vim', folder))
        session = self.root / ('vimsession_' + task + '_doc')
        marker = folder / 'vim_doc.close'
        marker.touch()
        env = dict(os.environ, WT_VIM_SESSION=str(session), WT_VIM_CLOSE=str(marker),
                   WT_VIM_REFUSED=str(folder / 'vim_doc.refused'))
        subprocess.run(['vim', '-Nu', 'NONE', '-n', '-es'] + command[1:] +
                       ['-c', 'call WorktreeCloseCheck(0)'], cwd=self.root, env=env,
                       check=True, timeout=10, capture_output=True)
        self.assertTrue(session.is_file())
        self.assertIn(task + '_suivi.txt', session.read_text())
        self.assertIn(task + '.txt', session.read_text())
        self.assertFalse((folder / 'vim_doc.refused').exists())
        self.assertEqual(atelier.vim_command(self.root, 'vim_doc')[:3], ['vim', '-S', str(session)])

    def test_doc_vim_without_notes_or_session_keeps_fallback(self):
        sessions.prepare(self.root)
        self.assertIsNone(atelier.vim_command(self.root, 'vim_doc'))

    def test_terminal_profile_clones_once_and_preserves_default(self):
        profiles = ['personal-profile']
        writes = []
        def settings(args, **kwargs):
            if args[:2] == ['dconf', 'dump']:
                return Mock(stdout="[/]\nexit-action='hold'\n")
            if args[:2] == ['gsettings', 'get']:
                return Mock(stdout=repr(profiles if args[-1] == 'list' else
                                       'personal-profile' if args[-1] == 'default' else 'close'))
            writes.append(args)
            if args[:2] == ['gsettings', 'set'] and args[3] == 'list':
                profiles[:] = atelier.ast.literal_eval(args[4])
            return Mock(returncode=0)
        with patch.object(atelier, 'run', side_effect=settings):
            first = atelier.terminal_profile()
            self.assertEqual(atelier.terminal_profile(), first)
        self.assertEqual(len(profiles), 2)
        self.assertEqual(sum(args[:2] == ['dconf', 'load'] for args in writes), 1)
        self.assertFalse(any(args[:2] == ['gsettings', 'set'] and args[3] == 'default' for args in writes))
        self.assertFalse(any('personal-profile/' in arg for args in writes for arg in args))
        self.assertEqual(sum(args[-2:] == ['exit-action', 'close'] for args in writes), 2)

    def test_terminal_profile_refuses_unapplied_close_setting(self):
        identifier = str(atelier.uuid.uuid5(atelier.uuid.NAMESPACE_URL, c.setting('TERMINAL_PROFILE_NAMESPACE') + str(self.main)))
        def settings(args, **kwargs):
            return Mock(stdout=repr([identifier] if args[-1] == 'list' else 'hold'))
        with patch.object(atelier, 'run', side_effect=settings):
            with self.assertRaisesRegex(c.ToolError, 'ne ferme pas'):
                atelier.terminal_profile()

    def fallback_shell_fixture(self):
        folder = atelier.directory(self.root)
        folder.mkdir(parents=True, exist_ok=True)
        rc = Path(self.temp.name) / 'shell-rc'
        import shlex
        rc.write_text('source ' + shlex.quote(str(TOOLS / 'worktree_bashrc.sh')) + '\nPS1="WT_TEST_READY> "\n')
        pid, terminal = pty.fork()
        if pid == 0:
            os.chdir(self.root)
            os.environ['WT_SHELL_CLOSE'] = str(folder / 'vim.close')
            os.execvp('bash', ['bash', '--rcfile', str(rc), '-i'])
        reaped = []
        def cleanup():
            if not reaped:
                children = Path('/proc/%s/task/%s/children' % (pid, pid))
                for child in children.read_text().split() if children.exists() else []:
                    try: os.kill(int(child), signal.SIGKILL)
                    except ProcessLookupError: pass
                try: os.kill(pid, signal.SIGKILL)
                except ProcessLookupError: pass
                os.waitpid(pid, 0)
            os.close(terminal)
        self.addCleanup(cleanup)
        data = b''; deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not data.endswith(b'WT_TEST_READY> '):
            if select.select([terminal], [], [], .1)[0]:
                data += os.read(terminal, 65536)
        self.assertTrue(data.endswith(b'WT_TEST_READY> '), 'Bash doit atteindre son prompt')
        return pid, terminal, folder, reaped

    def test_fallback_shell_exits_from_readline_without_keyboard_input(self):
        pid, terminal, folder, reaped = self.fallback_shell_fixture()
        record = {'child_pid': pid, 'child_start': atelier.identity(pid)}
        atelier.close_idle_shell(record, folder, 'vim')
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            done, status = os.waitpid(pid, os.WNOHANG)
            if done:
                reaped.append(done)
                self.assertEqual(os.waitstatus_to_exitcode(status), 0)
                return
            time.sleep(.05)
        self.fail('Le shell reste bloqué dans readline')

    def test_extra_terminal_shell_exits_without_keyboard_input(self):
        pid, terminal, folder, reaped = self.fallback_shell_fixture()
        atelier.close_extra_shell({'pid': pid, 'start': atelier.identity(pid)}, self.root)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            done, status = os.waitpid(pid, os.WNOHANG)
            if done:
                reaped.append(done)
                return
            time.sleep(.05)
        self.fail('Le shell supplémentaire reste ouvert')

    def test_extra_terminal_shell_preserves_running_command(self):
        pid, terminal, folder, reaped = self.fallback_shell_fixture()
        os.write(terminal, b'sleep 30\n')
        deadline = time.monotonic() + 3
        while not atelier.shell_children(pid) and time.monotonic() < deadline: time.sleep(.02)
        self.assertTrue(atelier.shell_children(pid))
        with patch.object(atelier.signal, 'pidfd_send_signal') as send:
            with self.assertRaisesRegex(c.ToolError, 'commande a démarré'):
                atelier.close_extra_shell({'pid': pid, 'start': atelier.identity(pid)}, self.root)
            send.assert_not_called()

    def test_extra_terminal_shell_in_other_worktree_is_preserved(self):
        pid, terminal, folder, reaped = self.fallback_shell_fixture()
        with patch.object(atelier.signal, 'pidfd_send_signal') as send:
            atelier.close_extra_shell({'pid': pid, 'start': atelier.identity(pid)}, self.main)
            send.assert_not_called()

    def test_fallback_shell_preserves_running_command(self):
        pid, terminal, folder, reaped = self.fallback_shell_fixture()
        os.write(terminal, b'sleep 30\n')
        children = Path('/proc/%s/task/%s/children' % (pid, pid))
        deadline = time.monotonic() + 3
        while not children.read_text().strip() and time.monotonic() < deadline:
            time.sleep(.02)
        self.assertTrue(children.read_text().strip())
        record = {'child_pid': pid, 'child_start': atelier.identity(pid)}
        with patch.object(atelier.signal, 'pidfd_send_signal') as send:
            with self.assertRaisesRegex(c.ToolError, 'commande est active'):
                atelier.close_idle_shell(record, folder, 'vim')
            send.assert_not_called()
        self.assertFalse((folder / 'vim.close').exists())

    def log_follower_fixture(self, verb='logs', option='-f'):
        import shlex
        pid, terminal, folder, reaped = self.fallback_shell_fixture()
        # Simuler un client Docker sans Docker : argv identiques et SIGINT
        # cooperatif, dans un vrai shell interactif avec controle des jobs.
        (self.root / verb).write_text('import signal,time,sys\n'
                                     'signal.signal(signal.SIGINT, lambda *_: sys.exit(0))\n'
                                     'while True: time.sleep(.1)\n')
        command = '(exec -a docker %s %s %s fixture-container)\n' % (shlex.quote(sys.executable), verb, option)
        os.write(terminal, command.encode())
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            children = atelier.shell_children(pid)
            if children:
                args = Path('/proc/%s/cmdline' % children[0]).read_bytes().split(b'\0')
                if args[:2] == [b'docker', verb.encode()]:
                    return pid, terminal, folder, reaped, children[0]
            time.sleep(.02)
        self.fail('Le client de logs doit demarrer')

    def test_shell_log_follower_is_stopped_without_signalling_shell(self):
        pid, terminal, folder, reaped, child = self.log_follower_fixture()
        with patch.object(atelier.signal, 'pidfd_send_signal', wraps=atelier.signal.pidfd_send_signal) as send:
            atelier.stop_log_followers(pid, self.root)
        self.assertEqual(send.call_count, 1)
        self.assertEqual(send.call_args.args[1], signal.SIGINT)
        self.assertIsNotNone(atelier.identity(pid))
        self.assertFalse(atelier.shell_children(pid))
        atelier.close_idle_shell({'child_pid': pid, 'child_start': atelier.identity(pid)}, folder, 'vim')

    def test_shell_docker_other_command_is_preserved(self):
        pid, terminal, folder, reaped, child = self.log_follower_fixture('exec')
        with patch.object(atelier.signal, 'pidfd_send_signal') as send:
            atelier.stop_log_followers(pid, self.root)
            send.assert_not_called()
        self.assertIsNotNone(atelier.identity(child))

    def test_shell_log_follower_in_other_worktree_is_preserved(self):
        pid, terminal, folder, reaped, child = self.log_follower_fixture()
        with patch.object(atelier.signal, 'pidfd_send_signal') as send:
            atelier.stop_log_followers(pid, self.main)
            send.assert_not_called()
        self.assertIsNotNone(atelier.identity(child))

    def test_shell_log_follower_long_follow_option_is_stopped(self):
        pid, terminal, folder, reaped, child = self.log_follower_fixture(option='--follow')
        atelier.stop_log_followers(pid, self.root)
        self.assertFalse(atelier.shell_children(pid))

    def test_shell_log_follower_timeout_blocks_without_forced_stop(self):
        pid, terminal, folder, reaped, child = self.log_follower_fixture()
        with patch.object(atelier.signal, 'pidfd_send_signal') as send:
            with self.assertRaisesRegex(c.ToolError, 'ne s’est pas arrêté'):
                atelier.stop_log_followers(pid, self.root, timeout=0)
            self.assertEqual(send.call_count, 1)
            self.assertEqual(send.call_args.args[1], signal.SIGINT)
        self.assertIsNotNone(atelier.identity(child))

    def test_close_from_fallback_shell_saves_before_closing_caller(self):
        import shlex
        sessions.prepare(self.root)
        pid, terminal, folder, reaped = self.fallback_shell_fixture()
        c.atomic(folder / 'vim.json', {
            'pid': os.getpid(), 'start': atelier.identity(os.getpid()),
            'child_pid': pid, 'child_start': atelier.identity(pid), 'shell_fallback': True})
        helper = Path(self.temp.name) / 'close-fixture.py'
        helper.write_text(
            'import os, sys\nfrom pathlib import Path\nfrom unittest.mock import patch\n'
            'os.environ["WT_MAIN_ROOT"] = %r\n'
            'sys.path.insert(0, %r)\nimport _wt_atelier as a\n'
            'root = Path(%r)\n'
            'def save(root):\n'
            ' assert a.identity(%d) is not None\n'
            ' (root / "saved-before-close").touch()\n'
            'with patch.object(a.runtime, "app_mounts", return_value=[]), '
            'patch.object(a.patches, "make", side_effect=save):\n'
            ' a.close(root)\n'
            '(root / "close-returned").touch()\n' %
            (str(self.main), str(TOOLS), str(self.root), pid))
        os.write(terminal, (shlex.quote(sys.executable) + ' ' + shlex.quote(str(helper)) + '\n').encode())
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            done, status = os.waitpid(pid, os.WNOHANG)
            if done:
                reaped.append(done)
                break
            time.sleep(.05)
        else:
            self.fail('Le shell appelant reste ouvert')
        deadline = time.monotonic() + 3
        while not (self.root / 'close-returned').exists() and time.monotonic() < deadline:
            time.sleep(.05)
        self.assertTrue((self.root / 'saved-before-close').exists())
        self.assertTrue((self.root / 'close-returned').exists())
        self.assertTrue((folder / 'close').exists())

    def test_busy_fallback_shell_blocks_before_codex_shutdown(self):
        pid, terminal, folder, reaped = self.fallback_shell_fixture()
        c.atomic(folder / 'vim.json', {
            'pid': os.getpid(), 'start': atelier.identity(os.getpid()),
            'child_pid': pid, 'child_start': atelier.identity(pid), 'shell_fallback': True})
        os.write(terminal, b'sleep 30\n')
        deadline = time.monotonic() + 3
        while not atelier.shell_children(pid) and time.monotonic() < deadline:
            time.sleep(.02)
        self.assertTrue(atelier.shell_children(pid))
        with patch.object(atelier, 'close_codex') as close_codex, \
                patch.object(docker, 'stop') as stop, patch.object(backup, 'make') as save:
            with self.assertRaisesRegex(c.ToolError, r'commande est active.*sleep \(PID \d+\)'):
                atelier.close(self.root)
            close_codex.assert_not_called()
            stop.assert_not_called()
            save.assert_not_called()

    def test_commit_only_staged_content_and_message(self):
        (self.root / 'tracked.txt').write_text('staged'); self.git(self.root, 'add', 'tracked.txt')
        (self.root / 'tracked.txt').write_text('still working')
        cli.commit(self.root, 'fix: exemple')
        self.assertEqual(self.git(self.root, 'show', 'HEAD:tracked.txt'), 'staged')
        self.assertEqual((self.root / 'tracked.txt').read_text(), 'still working')
        self.assertEqual(self.git(self.root, 'log', '-1', '--format=%s'), '1234_réglage : fix: exemple')

    def test_commit_refuses_doc_even_forced_into_index(self):
        (self.root / 'doc').mkdir(); (self.root / 'doc/a').write_text('private')
        self.git(self.root, 'add', '-f', 'doc/a')
        with self.assertRaises(c.ToolError): cli.commit(self.root, 'docs: test')

    def test_commit_accepts_exact_to_prod_and_preserves_unstaged_work(self):
        helper = self.root / c.setting_json('SPECIAL_COMMITS')['to_prod'][0]
        helper.parent.mkdir(parents=True)
        helper.write_text('deployment change\n')
        (self.root / 'tracked.txt').write_text('staged')
        self.git(self.root, 'add', 'tracked.txt')
        (self.root / 'tracked.txt').write_text('unfinished')
        with patch.object(cli, 'run', wraps=cli.run) as run:
            cli.commit(self.root, 'to_prod')
        commands = [call.args[0] for call in run.call_args_list]
        add = ['git', '-C', self.root, 'add', '--', c.setting_json('SPECIAL_COMMITS')['to_prod'][0]]
        diff = ['git', '-C', self.root, '--no-pager', 'diff', '--cached']
        self.assertLess(commands.index(add), commands.index(diff))
        self.assertEqual(commands[-1][3], 'commit')
        self.assertEqual(self.git(self.root, 'show', 'HEAD:' + c.setting_json('SPECIAL_COMMITS')['to_prod'][0]), 'deployment change')
        self.assertEqual(self.git(self.root, 'log', '-1', '--format=%s'), '1234_réglage : to_prod')
        self.assertEqual(self.git(self.root, 'show', 'HEAD:tracked.txt'), 'staged')
        self.assertEqual((self.root / 'tracked.txt').read_text(), 'unfinished')

    def test_commit_accepts_exact_to_devb_and_preserves_unstaged_work(self):
        helper = self.root / c.setting_json('SPECIAL_COMMITS')['to_prod'][0]
        helper.parent.mkdir(parents=True)
        helper.write_text('deployment change\n')
        (self.root / 'tracked.txt').write_text('staged')
        self.git(self.root, 'add', 'tracked.txt')
        (self.root / 'tracked.txt').write_text('unfinished')
        with patch.object(cli, 'run', wraps=cli.run) as run:
            cli.commit(self.root, 'to_devb')
        commands = [call.args[0] for call in run.call_args_list]
        add = ['git', '-C', self.root, 'add', '--', c.setting_json('SPECIAL_COMMITS')['to_prod'][0]]
        diff = ['git', '-C', self.root, '--no-pager', 'diff', '--cached']
        self.assertLess(commands.index(add), commands.index(diff))
        self.assertEqual(commands[-1][3], 'commit')
        self.assertEqual(self.git(self.root, 'show', 'HEAD:' + c.setting_json('SPECIAL_COMMITS')['to_prod'][0]), 'deployment change')
        self.assertEqual(self.git(self.root, 'log', '-1', '--format=%s'), '1234_réglage : to_devb')
        self.assertEqual(self.git(self.root, 'show', 'HEAD:tracked.txt'), 'staged')
        self.assertEqual((self.root / 'tracked.txt').read_text(), 'unfinished')

    def test_commit_to_prod_keeps_index_and_personal_file_checks(self):
        helper = self.root / c.setting_json('SPECIAL_COMMITS')['to_prod'][0]
        helper.parent.mkdir(parents=True)
        helper.write_text('unchanged\n')
        self.git(self.root, 'add', c.setting_json('SPECIAL_COMMITS')['to_prod'][0])
        self.git(self.root, 'commit', '-m', 'fixture helper')
        with self.assertRaisesRegex(c.ToolError, 'Index vide'):
            cli.commit(self.root, 'to_prod')
        (self.root / 'doc').mkdir()
        (self.root / 'doc/private').write_text('private')
        self.git(self.root, 'add', '-f', 'doc/private')
        with self.assertRaisesRegex(c.ToolError, 'Fichier personnel'):
            cli.commit(self.root, 'to_prod')

    def test_deployment_commit_stages_helper_from_empty_index_only(self):
        helper = self.root / c.setting_json('SPECIAL_COMMITS')['to_prod'][0]
        helper.parent.mkdir(parents=True)
        (self.root / 'tracked.txt').write_text('unfinished')
        for message in ('to_prod', 'to_devb'):
            helper.write_text(message + '\n')
            self.assertEqual(self.git(self.root, 'diff', '--cached', '--name-only'), '')
            cli.commit(self.root, message)
            self.assertEqual(self.git(self.root, 'show', 'HEAD:' + c.setting_json('SPECIAL_COMMITS')['to_prod'][0]), message)
            self.assertEqual(self.git(self.root, 'show', 'HEAD:tracked.txt'), 'initial')
            self.assertEqual((self.root / 'tracked.txt').read_text(), 'unfinished')

    def test_push_refuses_master_before_network(self):
        with self.assertRaises(c.ToolError): cli.push(self.main, 'origin')

    def test_close_saves_before_final_tab_close(self):
        sessions.prepare(self.root)
        (self.root / 'tracked.txt').write_text('unfinished')
        with patch.object(docker, 'app_mounts', return_value=[]):
            atelier.close(self.root)
        self.assertTrue((atelier.directory(self.root) / 'close').exists())
        archives = backup.pending(self.root)
        self.assertEqual(len(archives), 1)
        self.assertEqual(archives[0].parent, self.main / 'doc')
        self.assertEqual(backup.inspect_archive(archives[0])[2], [b'unfinished'])
        self.assertEqual((self.root / 'tracked.txt').read_text(), 'unfinished')

    def test_close_uses_reassigned_port_and_preserves_other_worktree(self):
        sessions.prepare(self.root)
        other = self.main.parent / 'project-2345_other'
        other_state = self.runtime_fixture(other, port=3002)
        self.runtime_fixture(port=3002, status='stopped')
        with patch.object(docker, 'published_ports', return_value={3002: {docker.project_for(other)}}), \
                patch.object(docker, 'port_available', return_value=True):
            port = docker.choose_port(self.root, 'http')
        self.assertEqual(port, 3003)
        own = self.runtime_fixture(port=port)
        own['override'] = str(docker.STATE / 'configs' / 'own-port-3003.yml')
        docker.write_state(own)
        mounts = [('web_http', self.root, 'own-id', True), ('web_http', other, 'other-id', True)]
        with patch.object(docker, 'app_mounts', return_value=mounts), \
                patch.object(docker, 'run', return_value=Mock(returncode=0)) as run, \
                patch.object(atelier, 'extra_terminal_shells', return_value=[]):
            atelier.close(self.root)
        run.assert_called_once()
        command = run.call_args.args[0]
        self.assertEqual(command[command.index('-p') + 1], own['project'])
        self.assertIn(own['override'], command)
        self.assertEqual(command[-len(docker.APPS)-2:], ['stop', *docker.APPS, docker.PROXY_SERVICE])
        self.assertEqual(docker.read_state(self.root)['status'], 'stopped')
        self.assertEqual(docker.read_state(self.root)['port'], 3003)
        self.assertEqual(docker.read_state(other), other_state)
        self.assertTrue((atelier.directory(self.root) / 'close').exists())

    def test_backup_failure_keeps_tabs_open(self):
        sessions.prepare(self.root)
        with patch.object(docker, 'app_mounts', return_value=[]), patch.object(backup, 'make', side_effect=c.ToolError('disk full')):
            with self.assertRaises(c.ToolError): atelier.close(self.root)
        self.assertFalse((atelier.directory(self.root) / 'close').exists())

    def test_legacy_patch_restore_and_done(self):
        (self.root / 'tracked.txt').write_text('legacy change\n')
        diff = backup.diff_bytes(self.root)
        (self.root / 'tracked.txt').write_text('initial\n')
        (self.root / 'doc').mkdir()
        path = self.main / 'doc/patch_1234_réglage_old.txt'; path.write_bytes(diff)
        backup.restore(self.root, path, yes=True)
        self.assertEqual((self.root / 'tracked.txt').read_text(), 'legacy change\n')
        self.assertTrue(Path(str(path)+'.done').exists())

    def test_ci_conflicting_flags_refused_before_docker(self):
        with patch.object(cli, 'run') as run:
            with self.assertRaises(c.ToolError): cli.test_run(self.root, ['unit', '--db-setup', '--no-db-setup'])
            run.assert_not_called()

    def test_development_knock_precedes_lease_lookup(self):
        calls = []
        def fake_value(root, *args):
            calls.append(args[0]); return 'abc123 refs/heads/feature/1234_réglage' if args[0]=='ls-remote' else 'remote'
        def fake_run(args, **kw):
            calls.append(str(args[0])); return Mock(returncode=0)
        with patch.object(cli, 'value', side_effect=fake_value), patch.object(cli, 'run', side_effect=fake_run):
            cli.push(self.root, 'development', force=True)
        self.assertLess(calls.index('knock'), calls.index('ls-remote'))

    def test_staged_rename_restores_without_ghost_source(self):
        self.git(self.root, 'mv', 'tracked.txt', 'renamed.txt')
        archive = backup.make(self.root)
        self.git(self.root, 'reset', '--hard', 'HEAD')
        backup.restore(self.root, archive, yes=True)
        self.assertFalse((self.root / 'tracked.txt').exists())
        self.assertEqual((self.root / 'renamed.txt').read_text(), 'initial\n')
        self.assertEqual(self.git(self.root, 'ls-files', '--others', '--exclude-standard'), '')


class CodexResolutionTests(unittest.TestCase):
    def test_path_is_resolved_each_time_without_version_cache(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(sessions.shutil, 'which', side_effect=['/nvm/v22/bin/codex', '/nvm/v24/bin/codex']):
            self.assertEqual(sessions.codex_binary(), '/nvm/v22/bin/codex')
            self.assertEqual(sessions.codex_binary(), '/nvm/v24/bin/codex')

    def test_shell_fallback_keeps_node_bin_in_child_path(self):
        with patch.dict(os.environ, {'PATH':'/usr/bin'}, clear=True), patch.object(sessions.shutil, 'which', side_effect=[None, '/nvm/v24/bin/codex']), patch('subprocess.run', return_value=Mock(returncode=0, stdout='/nvm/v24/bin/codex\n')):
            binary = sessions.codex_binary()
            self.assertEqual(sessions.codex_environment(binary)['PATH'], '/nvm/v24/bin:/usr/bin')

    def test_explicit_invalid_binary_is_reported(self):
        with patch.dict(os.environ, {'CODEX_BIN':'/missing/codex'}), patch.object(sessions.shutil, 'which', return_value=None):
            with self.assertRaisesRegex(c.ToolError, 'CODEX_BIN'): sessions.codex_binary()

    def test_personal_files_include_env_and_vim(self):
        for name in ('.env', '.env.local', 'vimsession_1234_task', 'doc/private.txt', '.idea'):
            self.assertTrue(c.personal(name))


if __name__ == '__main__': unittest.main()
