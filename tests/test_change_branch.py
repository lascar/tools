"""Vrais dépôts Git temporaires ; aucun serveur ni dépôt réel modifié."""
import contextlib
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

TOOLS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS))
import change_branch as cb
import _wt_common as common
import _wt_atelier as atelier
import _wt_docker as runtime
import _wt_remove as removal


class ChangeBranchTests(unittest.TestCase):
    def git(self, root, *args):
        return subprocess.check_output(['git', '-C', str(root), *args], text=True, stderr=subprocess.PIPE).strip()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='change-branch-')
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        self.main = base / 'main'
        self.remote = base / 'remote'
        self.root = base / 'task with spaces'
        self.task = '1234_réglage'
        self.branch = 'feature/' + self.task
        subprocess.run(['git', 'init', '-q', '--initial-branch=master', self.main], check=True)
        self.git(self.main, 'config', 'user.name', 'Fixture')
        self.git(self.main, 'config', 'user.email', 'fixture@example.invalid')
        self.git(self.main, 'config', 'core.hooksPath', '/dev/null')
        (self.main / '.git/info/exclude').write_text('doc/\n')
        (self.main / 'tracked.txt').write_text('base\n')
        self.git(self.main, 'add', '-f', 'tracked.txt')
        self.git(self.main, 'commit', '-qm', 'base')
        subprocess.run(['git', 'clone', '-q', '--bare', self.main, self.remote], check=True)
        self.git(self.main, 'remote', 'add', 'origin', str(self.remote))
        self.git(self.main, 'fetch', '-q', 'origin')
        self.git(self.main, 'branch', '--set-upstream-to=origin/master', 'master')
        self.git(self.main, 'worktree', 'add', '-q', '-b', self.branch, str(self.root))
        state = self.main / 'doc/docker-worktrees'
        for module in (cb, common, atelier, runtime):
            for attr, val in (('MAIN', self.main), ('STATE', state)):
                p = patch.object(module, attr, val)
                p.start()
                self.addCleanup(p.stop)
        self.old_cwd = Path.cwd()
        os.chdir(self.main)
        self.addCleanup(os.chdir, self.old_cwd)
        original_iterdir = Path.iterdir
        def test_processes(path):
            if path == Path('/proc'):
                children = Path('/proc/%s/task/%s/children' % (os.getpid(), os.getpid()))
                return iter(Path('/proc') / pid for pid in children.read_text().split())
            return original_iterdir(path)
        proc_patch = patch.object(Path, 'iterdir', test_processes)
        proc_patch.start()
        self.addCleanup(proc_patch.stop)
        self.docker = patch.object(runtime, 'docker', return_value=subprocess.CompletedProcess([], 0, stdout=''))
        self.docker.start()
        self.addCleanup(self.docker.stop)

    def upstream(self, text='remote\n'):
        clone = Path(self.temp.name) / 'upstream'
        subprocess.run(['git', 'clone', '-q', self.remote, clone], check=True)
        self.git(clone, 'config', 'user.name', 'Fixture')
        self.git(clone, 'config', 'user.email', 'fixture@example.invalid')
        (clone / 'tracked.txt').write_text(text)
        self.git(clone, 'add', 'tracked.txt')
        self.git(clone, 'commit', '-qm', 'upstream')
        self.git(clone, 'push', '-q', 'origin', 'master')
        return self.git(clone, 'rev-parse', 'HEAD')

    def prepare(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            cb.prepare(self.task)
        self.assertEqual(output.getvalue().strip(), str(self.root))

    def test_pull_then_merge_and_preserve_branches(self):
        head = self.upstream()
        self.prepare()
        self.assertEqual(self.git(self.main, 'rev-parse', 'HEAD'), head)
        self.assertNotEqual(self.git(self.root, 'rev-parse', 'HEAD'), head)
        os.chdir(self.root)
        cb.merge(self.task)
        self.assertEqual(self.git(self.root, 'rev-parse', 'HEAD'), head)
        self.assertEqual(self.git(self.main, 'branch', '--show-current'), 'master')
        self.assertEqual(self.git(self.root, 'branch', '--show-current'), self.branch)

    def test_dirty_target_refused_before_pull(self):
        for kind in ('unstaged', 'staged', 'untracked'):
            with self.subTest(kind=kind), patch.object(cb, 'run') as run:
                path = self.root / ('new.txt' if kind == 'untracked' else 'tracked.txt')
                path.write_text('local\n')
                if kind == 'staged':
                    self.git(self.root, 'add', 'tracked.txt')
                with self.assertRaisesRegex(common.ToolError, 'non committés'):
                    cb.prepare(self.task)
                run.assert_not_called()
                if kind == 'untracked':
                    path.unlink()
                else:
                    self.git(self.root, 'restore', '--source=HEAD', '--staged', '--worktree', 'tracked.txt')

    def test_dirty_master_refused_before_pull(self):
        (self.main / 'new.txt').write_text('local\n')
        with patch.object(cb, 'run') as run, self.assertRaisesRegex(common.ToolError, 'non committés'):
            cb.prepare(self.task)
        run.assert_not_called()

    def test_active_container_and_docker_failure_refuse(self):
        item = {'Id': 'fixture', 'Name': '/server', 'Mounts': [{'Type': 'bind', 'Source': str(self.root)}]}
        import json
        with patch.object(runtime, 'docker', side_effect=[subprocess.CompletedProcess([], 0, stdout='id'), subprocess.CompletedProcess([], 0, stdout=json.dumps([item]))]), patch.object(cb, 'run') as run:
            with self.assertRaisesRegex(common.ToolError, 'Conteneur actif'):
                cb.prepare(self.task)
            run.assert_not_called()
        with patch.object(runtime, 'docker', side_effect=common.ToolError('Docker inaccessible')), patch.object(cb, 'run') as run:
            with self.assertRaisesRegex(common.ToolError, 'Docker inaccessible'):
                cb.prepare(self.task)
            run.assert_not_called()

    def test_live_atelier_refused(self):
        with patch.object(atelier, 'alive', return_value=True), patch.object(cb, 'run') as run:
            with self.assertRaisesRegex(common.ToolError, 'Atelier actif'):
                cb.prepare(self.task)
            run.assert_not_called()

    def test_real_process_in_target_refused(self):
        child = subprocess.Popen(['sleep', '30'], cwd=self.root)
        try:
            with self.assertRaisesRegex(common.ToolError, 'Commande active'):
                cb.idle(self.root)
        finally:
            child.terminate()
            child.wait(timeout=5)

    def protected_process(self):
        proc = Path(self.temp.name) / '2265'
        proc.mkdir()
        (proc / 'comm').write_text('sleep\n')
        original_resolve = Path.resolve
        original_iterdir = Path.iterdir
        def resolve(path, *args, **kwargs):
            if path == proc / 'cwd':
                raise PermissionError('protected cwd')
            return original_resolve(path, *args, **kwargs)
        def iterdir(path):
            return iter([proc]) if path == Path('/proc') else original_iterdir(path)
        self.enterContext(patch.object(Path, 'resolve', resolve))
        self.enterContext(patch.object(Path, 'iterdir', iterdir))
        return proc

    def test_protected_process_outside_worktree_allows_check_and_preserves_stdout(self):
        proc = self.protected_process()
        output, errors = io.StringIO(), io.StringIO()
        result = subprocess.CompletedProcess([], 0, str(self.main) + '\n', '')
        with patch.object(removal, 'run', return_value=result) as read, \
                contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            cb.idle(self.root)
        read.assert_called_once_with(['sudo', 'readlink', '-e', '--', proc / 'cwd'], capture=True, check=False)
        self.assertEqual(output.getvalue(), '')
        self.assertIn('2265 protégé', errors.getvalue())

    def test_protected_process_in_worktree_still_blocks_before_pull(self):
        self.protected_process()
        result = subprocess.CompletedProcess([], 0, str(self.root) + '\n', '')
        with patch.object(removal, 'run', return_value=result), patch.object(cb, 'run') as run:
            with self.assertRaisesRegex(common.ToolError, 'Commande active.*sleep'):
                cb.prepare(self.task)
            run.assert_not_called()

    def test_failed_privileged_process_check_blocks_before_pull(self):
        self.protected_process()
        result = subprocess.CompletedProcess([], 1, '', 'sudo refusé')
        with patch.object(removal, 'run', return_value=result), patch.object(cb, 'run') as run:
            with self.assertRaisesRegex(common.ToolError, 'sudo refusé'):
                cb.prepare(self.task)
            run.assert_not_called()

    def test_git_operation_refused_with_clean_index(self):
        marker = Path(self.git(self.root, 'rev-parse', '--path-format=absolute', '--git-path', 'CHERRY_PICK_HEAD'))
        marker.write_text(self.git(self.root, 'rev-parse', 'HEAD'))
        with patch.object(cb, 'run') as run, self.assertRaisesRegex(common.ToolError, 'Opération Git'):
            cb.prepare(self.task)
        run.assert_not_called()

    def test_dirty_during_pull_blocks_merge(self):
        def mutate(*args, **kwargs):
            (self.root / 'tracked.txt').write_text('new local work\n')
        with patch.object(cb, 'run', side_effect=mutate), self.assertRaisesRegex(common.ToolError, 'non committés'):
            cb.prepare(self.task)
        self.assertEqual((self.root / 'tracked.txt').read_text(), 'new local work\n')

    def test_merge_conflict_preserved(self):
        (self.root / 'tracked.txt').write_text('task\n')
        self.git(self.root, 'add', 'tracked.txt')
        self.git(self.root, 'commit', '-qm', 'task')
        self.upstream()
        self.prepare()
        os.chdir(self.root)
        with self.assertRaisesRegex(common.ToolError, 'Fusion interrompue'):
            cb.merge(self.task)
        self.assertIn('UU tracked.txt', self.git(self.root, 'status', '--porcelain'))
        self.assertEqual(Path.cwd(), self.root)

    def test_shell_cd_and_failed_prepare_keep_expected_directory(self):
        self.upstream()
        binaries = Path(self.temp.name) / 'bin'
        binaries.mkdir()
        docker = binaries / 'docker'
        docker.write_text('#!/bin/sh\nexit 0\n')
        docker.chmod(0o755)
        (binaries / 'sitecustomize.py').write_text("from pathlib import Path\nimport os\noriginal = Path.iterdir\ndef isolated(self):\n    if str(self) == '/proc':\n        return iter(())\n    return original(self)\nPath.iterdir = isolated\n")
        env = dict(os.environ, PYTHONPATH=str(binaries), WT_MAIN_ROOT=str(self.main), PATH=str(binaries)+':'+os.environ['PATH'])
        script = 'source "$1/worktree_shell.sh"; change_branch.sh "$2"; result=$?; printf "RESULT=%s\\nPWD=%s\\n" "$result" "$PWD"'
        first = subprocess.run(['bash', '--noprofile', '--norc', '-c', script, 'test', str(TOOLS), self.task], cwd=self.main, env=env, text=True, capture_output=True)
        self.assertIn('RESULT=0\nPWD='+str(self.root), first.stdout, first.stderr)
        (self.root / 'new.txt').write_text('local\n')
        second = subprocess.run(['bash', '--noprofile', '--norc', '-c', script, 'test', str(TOOLS), self.task], cwd=self.main, env=env, text=True, capture_output=True)
        self.assertIn('RESULT=1\nPWD='+str(self.main), second.stdout, second.stderr)
        (self.root / 'new.txt').unlink()
        (self.root / 'tracked.txt').write_text('task conflict\n')
        self.git(self.root, 'add', 'tracked.txt')
        self.git(self.root, 'commit', '-qm', 'task conflict')
        upstream = Path(self.temp.name) / 'upstream'
        (upstream / 'tracked.txt').write_text('another upstream change\n')
        self.git(upstream, 'add', 'tracked.txt')
        self.git(upstream, 'commit', '-qm', 'another upstream change')
        self.git(upstream, 'push', '-q', 'origin', 'master')
        third = subprocess.run(['bash', '--noprofile', '--norc', '-c', script, 'test', str(TOOLS), self.task], cwd=self.main, env=env, text=True, capture_output=True)
        self.assertIn('RESULT=1\nPWD='+str(self.root), third.stdout, third.stderr)
        self.assertIn('UU tracked.txt', self.git(self.root, 'status', '--porcelain'))

    def test_shell_without_argument_returns_to_main_and_preserves_dirty_task(self):
        head = self.git(self.main, 'rev-parse', 'HEAD')
        (self.root / 'tracked.txt').write_text('unfinished task\n')
        (self.main / 'untracked.txt').write_text('unfinished main\n')
        env = dict(os.environ, WT_MAIN_ROOT=str(self.main))
        script = 'source "$1/worktree_shell.sh"; change_branch.sh; result=$?; printf "RESULT=%s\\nPWD=%s\\n" "$result" "$PWD"'
        for start in (self.root, self.main):
            result = subprocess.run(['bash', '--noprofile', '--norc', '-c', script, 'test', str(TOOLS)],
                                    cwd=start, env=env, text=True, capture_output=True, timeout=10)
            self.assertIn('RESULT=0\nPWD=' + str(self.main), result.stdout, result.stderr)
        self.assertEqual(self.git(self.main, 'rev-parse', 'HEAD'), head)
        self.assertEqual(self.git(self.main, 'branch', '--show-current'), 'master')
        self.assertEqual(self.git(self.root, 'branch', '--show-current'), self.branch)
        self.assertEqual((self.root / 'tracked.txt').read_text(), 'unfinished task\n')
        self.assertEqual((self.main / 'untracked.txt').read_text(), 'unfinished main\n')

    def test_shell_without_argument_refuses_wrong_main_branch_and_keeps_directory(self):
        self.git(self.main, 'checkout', '-qb', 'other')
        env = dict(os.environ, WT_MAIN_ROOT=str(self.main))
        script = 'source "$1/worktree_shell.sh"; change_branch.sh; result=$?; printf "RESULT=%s\\nPWD=%s\\n" "$result" "$PWD"'
        result = subprocess.run(['bash', '--noprofile', '--norc', '-c', script, 'test', str(TOOLS)],
                                cwd=self.root, env=env, text=True, capture_output=True, timeout=10)
        self.assertIn('RESULT=1\nPWD=' + str(self.root), result.stdout)
        self.assertIn('doit déjà être sur master', result.stderr)
        self.assertEqual(self.git(self.main, 'branch', '--show-current'), 'other')

    def test_missing_worktree_and_wrong_main_branch_refuse_before_pull(self):
        with patch.object(cb, 'run') as run:
            with self.assertRaisesRegex(common.ToolError, 'Aucun worktree'):
                cb.prepare('9999_missing')
            self.git(self.main, 'checkout', '-qb', 'other')
            with self.assertRaisesRegex(common.ToolError, 'déjà être sur master'):
                cb.prepare(self.task)
            run.assert_not_called()
            self.assertEqual(self.git(self.main, 'branch', '--show-current'), 'other')


if __name__ == '__main__':
    unittest.main()
