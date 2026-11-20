"""Portabilité et chargement sûr du profil, sans dépendre d’un projet réel."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

TOOLS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS))
import _wt_config as config


class ConfigTests(unittest.TestCase):
    def test_aliases_expand_without_changing_system_home_or_running_shell(self):
        system_home = os.environ.get('HOME')
        with patch.object(config, 'CONFIG_HOME', '/tmp/configured home'), \
                patch.object(config, 'CONFIG_APPLICATION', 'another-app'), \
                patch.object(config, 'VALUES', {'WT_MAIN_ROOT': '${HOME}/development/${APPLICATION}',
                                              'WT_TITLE': '${APPLICATION}/master',
                                              'PASSWORD': '${HOME} $(ignored)'}):
            self.assertEqual(str(config.configured_path('MAIN_ROOT')), '/tmp/configured home/development/another-app')
            self.assertEqual(config.setting('TITLE'), 'another-app/master')
            self.assertEqual(config.VALUES['PASSWORD'], '${HOME} $(ignored)')
            self.assertEqual(config.expand_config_value('$HOME ${OTHER} $(ignored)'), '$HOME ${OTHER} $(ignored)')
        self.assertEqual(os.environ.get('HOME'), system_home)

    def test_aliases_in_nested_json_preserve_quotes_and_backslashes(self):
        with patch.object(config, 'CONFIG_HOME', '/tmp/a "quoted" home\\path'), \
                patch.object(config, 'CONFIG_APPLICATION', 'sample'), \
                patch.object(config, 'VALUES', {'WT_DATA': '{"${APPLICATION}":["${HOME}/src",{"command":"echo ${OTHER}"}]}'}):
            self.assertEqual(config.setting_json('DATA'),
                             {'sample': ['/tmp/a "quoted" home\\path/src', {'command': 'echo ${OTHER}'}]})

    def test_missing_or_recursive_aliases_are_rejected_without_values(self):
        for value in ('', '${APPLICATION}', '${HOME}/private'):
            with self.subTest(value=value), patch.object(config, 'CONFIG_APPLICATION', value):
                with self.assertRaises(ValueError) as caught:
                    config.expand_config_value('${APPLICATION}')
                self.assertNotIn('private', str(caught.exception))

    def test_aliases_come_from_profile_and_keep_environment_overrides_for_wt(self):
        with tempfile.TemporaryDirectory() as folder:
            profile = Path(folder) / '.env'
            profile.write_text((TOOLS / '.env.example').read_text() +
                               '\nHOME=/tmp/profile-home\nAPPLICATION=profile-app\n')
            env = {key: value for key, value in os.environ.items() if not key.startswith('WT_')}
            env.update(WT_CONFIG_FILE=str(profile), WT_PROJECT_NAME='${APPLICATION}-override')
            result = subprocess.run([sys.executable, str(TOOLS / '_wt_config.py'), '--project-name'],
                                    env=env, capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), 'profile-app-override')
            result = subprocess.run([sys.executable, str(TOOLS / '_wt_config.py'), '--main-root'],
                                    env=env, capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), '/tmp/profile-home/development/profile-app')

    def test_zz_uses_configured_root_and_titles_without_real_git_or_docker(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            main = root / 'development' / 'another-app'
            main.mkdir(parents=True)
            binaries = root / 'bin'
            binaries.mkdir()
            profile = root / '.env'
            profile.write_text((TOOLS / '.env.example').read_text() +
                               '\nHOME=%s\nAPPLICATION=another-app\n' % root)
            scripts = {
                'git': '#!/bin/bash\nif [[ $1 == branch ]]; then echo master; fi\n',
                'docker': '#!/bin/bash\nexit 0\n',
                'python3': '#!/bin/bash\nif [[ $1 == */_wt_config.py ]]; then exec "%s" "$@"; fi\n'
                           'if [[ $1 == - ]]; then cat >/dev/null; fi\nexit 0\n' % sys.executable,
                'gnome-terminal': '#!%s\nimport json,os,sys\nprint(json.dumps({"args":sys.argv[1:],"home":os.environ.get("HOME")}))\n' % sys.executable,
            }
            for name, body in scripts.items():
                path = binaries / name
                path.write_text(body)
                path.chmod(0o755)
            env = {key: value for key, value in os.environ.items() if not key.startswith('WT_')}
            env.update(WT_CONFIG_FILE=str(profile), PATH=str(binaries) + ':' + os.environ['PATH'], DISPLAY=':fixture')
            env.pop('GNOME_TERMINAL_SCREEN', None)
            result = subprocess.run(['bash', str(TOOLS / 'zz.sh'), '--no-report'], cwd=root,
                                    env=env, capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            actual = json.loads(result.stdout.splitlines()[-1])
            self.assertEqual(actual['home'], os.environ.get('HOME'))
            self.assertEqual(actual['args'].count('--title=another-app/master'), 3)
            self.assertEqual(actual['args'].count('--working-directory=' + str(main)), 3)

    def test_env_values_are_inert_and_support_json_and_spaces(self):
        with tempfile.TemporaryDirectory() as folder:
            marker = Path(folder) / 'must-not-exist'
            path = Path(folder) / '.env'
            path.write_text("WT_NAME='$(touch %s)'\nWT_ARGS='[\"one two\",\"three\"]'\nexport WT_EMPTY=\n" % marker)
            values = config.load_env(path)
            self.assertEqual(values['WT_NAME'], '$(touch %s)' % marker)
            self.assertEqual(json.loads(values['WT_ARGS']), ['one two', 'three'])
            self.assertEqual(values['WT_EMPTY'], '')
            self.assertFalse(marker.exists())

    def test_invalid_env_reports_line_without_disclosing_value(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / '.env'
            path.write_text('WT_SECRET=private value\n')
            with self.assertRaisesRegex(ValueError, 'ligne 1') as error:
                config.load_env(path)
            self.assertNotIn('private', str(error.exception))

    def test_other_profile_controls_git_sessions_runtime_and_probes(self):
        with tempfile.TemporaryDirectory(prefix='portable-workflow-') as folder:
            base = Path(folder)
            main, remote, upstream = base / 'repository', base / 'remote', base / 'upstream'
            def git(root, *args):
                return subprocess.check_output(['git', '-C', str(root), *args], text=True, stderr=subprocess.PIPE).strip()
            subprocess.run(['git', 'init', '-q', '--initial-branch=trunk', main], check=True)
            git(main, 'config', 'user.name', 'Fixture')
            git(main, 'config', 'user.email', 'fixture@example.invalid')
            git(main, 'config', 'core.hooksPath', '/dev/null')
            (main / '.gitignore').write_text('doc/\nsessions_codex/\ntmp\nAGENTS-local.md\nAGENTS.override.md\n.codex/\n')
            (main / 'tracked').write_text('base\n')
            (main / 'AGENTS-local.md').write_text('Instructions génériques')
            git(main, 'add', '.gitignore', 'tracked')
            git(main, 'commit', '-qm', 'base')
            subprocess.run(['git', 'clone', '-q', '--bare', main, remote], check=True)
            git(main, 'remote', 'add', 'upstream', str(remote))
            subprocess.run(['git', 'clone', '-q', remote, upstream], check=True)
            git(upstream, 'config', 'user.name', 'Fixture')
            git(upstream, 'config', 'user.email', 'fixture@example.invalid')
            (upstream / 'tracked').write_text('updated\n')
            git(upstream, 'add', 'tracked')
            git(upstream, 'commit', '-qm', 'remote update')
            git(upstream, 'push', '-q', 'origin', 'trunk')
            profile = base / 'profile.env'
            profile.write_text((TOOLS / '.env.example').read_text())
            env = {k: v for k, v in os.environ.items() if not k.startswith('WT_')}
            env.update(WT_CONFIG_FILE=str(profile), WT_MAIN_ROOT=str(main), WT_PROJECT_NAME='another-project',
                       WT_WORKTREE_PREFIX='task-', WT_BASE_BRANCH='trunk', WT_REMOTE='upstream',
                       WT_FEATURE_PREFIX='topic/', WT_TICKET_PREFIX='support/',
                       WT_DOCKER_PROJECT='another-runtime', WT_DOCKER_PREFIX='another-task-',
                       WT_CONTAINER_PATH='/workspace', WT_MAIN_PORT='4100', WT_PORT_START='4101', WT_PORT_END='4103',
                       WT_DEBUG_PORT='5101', WT_CONSOLE_SERVICE='repl', WT_SHELL_SERVICE='toolbox',
                       WT_APP_SERVICES='["api","repl","toolbox"]', WT_MODES='{"http":"api"}',
                       WT_ROLE_TITLES='{"codex":"Agent","console":"REPL","server":"API","vim":"Shell"}',
                       WT_SERVER_PROBE='["check-ready","--port","4100"]',
                       WT_SERVER_COMMANDS='{"http":["generic-api","--serve"]}', WT_CONSOLE_COMMAND='["generic-repl"]', WT_INFRA_SERVICES='[]', WT_INFRA_PROBE='[]')
            code = r'''
import json, sys
from pathlib import Path
from unittest.mock import patch, Mock
sys.path.insert(0, sys.argv[1])
import _wt_common as c
import _wt_sessions as sessions
import _wt_docker as runtime
import _wt_atelier as atelier
import worktree_tools as cli
root = cli.ensure_worktree('9876_sample', create=True)
cli.update_from_master(root)
sessions.prepare(root, new=True)
effective = {'services': {name: {'volumes': [{'target': '/workspace', 'source': str(c.MAIN)}]} for name in runtime.APPS}}
with patch.object(runtime, 'run', return_value=Mock(stdout=json.dumps(effective))):
    generated = json.loads(runtime.generate(c.MAIN).read_text())
runtime.write_state({'worktree': str(root), 'project': runtime.project_for(root), 'status': 'active'})
with patch.object(runtime, 'app_mounts', return_value=[('api', root, 'api-id', True), ('repl', root, 'repl-id', True)]), patch.object(runtime, 'docker', return_value=Mock(returncode=0)) as docker:
    runtime.wait_for_server(root, 'api')
    probe = docker.call_args.args
print(json.dumps({'root': str(root), 'branch': c.value(root, 'branch', '--show-current'),
 'project': runtime.project_for(root), 'main_port': runtime.choose_port(c.MAIN, 'http'),
 'debug_port': runtime.debugger_port(4102), 'services': sorted(generated['services']),
 'mount': generated['services']['api']['volumes'][0]['target'], 'probe': probe,
 'server_command': generated['services']['api']['command'],
 'console_command': generated['services']['repl']['command'],
 'roles': atelier.ROLES, 'updated': (root/'tracked').read_text(),
 'journal': (c.MAIN/'sessions_codex/9876_sample.jsont').exists()}))
'''
            result = subprocess.run([sys.executable, '-c', code, str(TOOLS)], cwd=main, env=env, capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            actual = json.loads(result.stdout.splitlines()[-1])
            self.assertEqual(actual['root'], str(base / 'task-9876_sample'))
            self.assertEqual(actual['branch'], 'topic/9876_sample')
            self.assertTrue(actual['project'].startswith('another-task-'))
            self.assertEqual(actual['server_command'], ['generic-api', '--serve'])
            self.assertEqual(actual['console_command'], ['generic-repl'])
            self.assertEqual(actual['main_port'], 4100)
            self.assertEqual(actual['debug_port'], 5102)
            self.assertEqual(actual['services'], ['api', 'repl', 'toolbox'])
            self.assertEqual(actual['mount'], '/workspace')
            self.assertEqual(actual['probe'], ['exec', 'api-id', 'check-ready', '--port', '4100'])
            self.assertEqual(actual['updated'], 'updated\n')
            self.assertTrue(actual['journal'])
            self.assertIn(['server', 'API'], actual['roles'])

    def test_invalid_application_commands_are_refused(self):
        for key, value in (
                ('WT_SERVER_COMMANDS', '[]'),
                ('WT_SERVER_COMMANDS', '{"http":"shell string"}'),
                ('WT_SERVER_COMMANDS', '{"http":[]}'),
                ('WT_SERVER_COMMANDS', '{"unknown":["serve"]}'),
                ('WT_CONSOLE_COMMAND', '[42]'),
                ('WT_CONSOLE_COMMAND', '"shell string"')):
            with self.subTest(key=key, value=value):
                result = subprocess.run([sys.executable, str(TOOLS / '_wt_config.py'), '--check'],
                                        env=dict(os.environ, **{key: value}), capture_output=True, text=True, timeout=10)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(key, result.stderr)

    def test_invalid_complete_compose_profiles_are_refused(self):
        for name in ('WT_COMPOSE_CONFIG', 'WT_CI_COMPOSE_CONFIG'):
            for value in ('[]', '"yaml text"', '{"services":[]}', '{"services":{}}', '{"services":{"app":{}},"volumes":[]}'):
                with self.subTest(name=name, value=value):
                    result = subprocess.run([sys.executable, str(TOOLS / '_wt_config.py'), '--check'],
                                            env=dict(os.environ, **{name: value}), capture_output=True, text=True, timeout=10)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn(name, result.stderr)

    def test_legacy_session_prefix_is_configured(self):
        env = dict(os.environ, WT_LEGACY_ENV_PREFIXES='["HISTORIC"]', HISTORIC_VIM_CLOSE='/tmp/legacy-close')
        env.pop('WT_VIM_CLOSE', None)
        result = subprocess.run([sys.executable, str(TOOLS / '_wt_config.py'), '--session-env', 'VIM_CLOSE'], env=env, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), '/tmp/legacy-close')

    def test_dangerous_shared_temp_paths_refused_before_any_work(self):
        for path in ('/', '../outside', '.', '.git', 'doc', 'sessions_codex', '.codex'):
            with self.subTest(path=path):
                env = dict(os.environ, WT_SHARED_TEMP_DIR=path)
                result = subprocess.run([sys.executable, str(TOOLS / '_wt_config.py'), '--check'], env=env, capture_output=True, text=True, timeout=10)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('WT_SHARED_TEMP_DIR', result.stderr)

    def test_invalid_ports_refused_before_any_work(self):
        env = dict(os.environ, WT_PORT_START='6000', WT_PORT_END='5000')
        result = subprocess.run([sys.executable, str(TOOLS / '_wt_config.py'), '--check'], env=env, capture_output=True, text=True, timeout=10)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Plage de ports invalide', result.stderr)


if __name__ == '__main__':
    unittest.main()
