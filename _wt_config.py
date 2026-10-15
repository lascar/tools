"""Configuration locale inerte : aucune exécution shell ni interpolation implicite."""
import json
import os
from pathlib import Path
import re
import shlex

TOOLS = Path(__file__).resolve().parent


def load_env(path):
    values = {}
    for number, line in enumerate(Path(path).read_text().splitlines(), 1):
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        match = re.fullmatch(r'(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)', line)
        if not match:
            raise ValueError('Configuration .env invalide, ligne %s.' % number)
        key, raw = match.groups()
        parts = shlex.split(raw, comments=True)
        if len(parts) > 1:
            raise ValueError('Valeur .env à mettre entre guillemets, ligne %s.' % number)
        values[key] = parts[0] if parts else ''
    return values


CONFIG_FILE = Path(os.environ.get('WT_CONFIG_FILE', TOOLS / '.env')).expanduser().resolve()
VALUES = load_env(CONFIG_FILE)


def setting(name, default=None):
    key = 'WT_' + name
    result = os.environ.get(key, VALUES.get(key, default))
    if result is None:
        raise ValueError('Paramètre obligatoire absent : %s dans .env.' % key)
    return result


def setting_json(name, default=None):
    raw = setting(name, json.dumps(default) if default is not None else None)
    try:
        return json.loads(raw)
    except ValueError:
        # Ne jamais afficher la valeur : elle peut contenir des données privées.
        raise ValueError('Paramètre JSON invalide : WT_%s.' % name)


def setting_int(name, default=None):
    return int(setting(name, default))


def configured_path(name, base=TOOLS, default=None):
    raw = Path(setting(name, default)).expanduser()
    return (raw if raw.is_absolute() else base / raw).resolve()


PROJECT_NAME = setting('PROJECT_NAME')
BASE_BRANCH = setting('BASE_BRANCH', 'main')
REMOTE = setting('REMOTE', 'origin')
FEATURE_PREFIX = setting('FEATURE_PREFIX', 'feature/')
TICKET_PREFIX = setting('TICKET_PREFIX', 'tickets/')
DEFAULT_MODE = setting('DEFAULT_MODE', 'http')
APP_PATH = setting('CONTAINER_PATH', '/app')
CONSOLE_SERVICE = setting('CONSOLE_SERVICE', 'console')
SHELL_SERVICE = setting('SHELL_SERVICE', 'shell')
PROXY_SERVICE = setting('PROXY_SERVICE', 'proxy')
MAIN_PORT = setting_int('MAIN_PORT', 3000)
PORT_START = setting_int('PORT_START', 3002)
PORT_END = setting_int('PORT_END', 3009)
DEBUG_PORT = setting_int('DEBUG_PORT', 1234)
DOCKER_PROJECT = setting('DOCKER_PROJECT')
DOCKER_PREFIX = setting('DOCKER_PREFIX', DOCKER_PROJECT + '-wt-')
RUNTIME_LABEL = setting('RUNTIME_LABEL', 'worktree.runtime')
WORKTREE_LABEL = setting('WORKTREE_LABEL', 'worktree.path')


def validate():
    if not PROJECT_NAME or not re.fullmatch(r'[A-Za-z0-9_.-]+', setting('WORKTREE_PREFIX', PROJECT_NAME + '-')):
        raise ValueError('WT_WORKTREE_PREFIX doit être un préfixe de nom de dossier, sans chemin.')
    if not (1 <= MAIN_PORT <= 65535 and 1 <= PORT_START <= PORT_END <= 65535 and
            1 <= DEBUG_PORT <= 65535 - (PORT_END - PORT_START)):
        raise ValueError('Plage de ports invalide dans .env.')
    for prefix in (FEATURE_PREFIX, TICKET_PREFIX):
        if not re.fullmatch(r'[A-Za-z0-9_.-]+/', prefix):
            raise ValueError('Les préfixes de branche doivent avoir la forme type/.')
    for name in ('APP_SERVICES', 'INFRA_SERVICES', 'VOLUMES', 'PROFILE_FILES', 'COPY_FILES',
                 'PERSONAL_NAMES', 'PERSONAL_PREFIXES', 'LEGACY_ROOT_ALIASES', 'RUNTIME_DIRS',
                 'SHARED_CONFIG_FILES', 'ALIAS_SERVICES', 'CI_SHARED_DIRS', 'AUDIT_TARGETS',
                 'FORCE_PUSH_REMOTES', 'LEGACY_ENV_PREFIXES', 'SERVER_PROBE', 'INFRA_PROBE', 'CI_COMMAND', 'CONSOLE_COMMAND'):
        value = setting_json(name, [])
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise ValueError('WT_%s doit être une liste JSON de chaînes.' % name)
    for name in ('MODES', 'ROLE_TITLES', 'MAIN_ROLE_TITLES', 'APP_NETWORKS', 'EXTERNAL_NETWORKS',
                 'SPECIAL_COMMITS', 'PUSH_TARGETS'):
        if not isinstance(setting_json(name, {}), dict):
            raise ValueError('WT_%s doit être un objet JSON.' % name)
    for name in ('PROFILE_OVERRIDE', 'PROXY_TEMPLATE'):
        if not isinstance(setting_json(name, ''), str):
            raise ValueError('WT_%s doit être une chaîne JSON.' % name)


    for name in ('COMPOSE_CONFIG', 'CI_COMPOSE_CONFIG'):
        config = setting_json(name, {})
        if not isinstance(config, dict) or (config and (not isinstance(config.get('services'), dict) or not config['services'])):
            raise ValueError('WT_%s doit être un objet Compose JSON avec services, ou {} pour utiliser un fichier.' % name)
        for section in ('services', 'volumes', 'networks', 'configs', 'secrets'):
            if section in config and not isinstance(config[section], dict):
                raise ValueError('WT_%s : section Compose invalide.' % name)

    commands = setting_json('SERVER_COMMANDS', {})
    if not isinstance(commands, dict):
        raise ValueError('WT_SERVER_COMMANDS doit être un objet JSON de commandes par mode.')
    modes = setting_json('MODES', {})
    for mode, command in commands.items():
        if mode not in modes or not isinstance(command, list) or not command or not all(isinstance(arg, str) for arg in command):
            raise ValueError('WT_SERVER_COMMANDS : mode inconnu ou commande vide/invalide.')
    app_services = setting_json('APP_SERVICES', [])
    for mode in commands:
        if modes[mode] not in app_services:
            raise ValueError('WT_SERVER_COMMANDS : service absent de WT_APP_SERVICES.')
    if setting_json('CONSOLE_COMMAND', []) and CONSOLE_SERVICE not in app_services:
        raise ValueError('WT_CONSOLE_COMMAND : service absent de WT_APP_SERVICES.')
    # Un service Compose ne peut recevoir deux commandes selon des modes distincts.
    by_service = {}
    for mode, command in commands.items():
        service = modes[mode]
        if service in by_service and by_service[service] != command:
            raise ValueError('WT_SERVER_COMMANDS : commandes différentes pour un même service.')
        by_service[service] = command
    if setting('CONSOLE_SERVICE', 'console') in by_service:
        raise ValueError('WT_CONSOLE_SERVICE doit être distinct des services serveur.')


    def relative_path(value, key):
        path = Path(value)
        if not value or value == '.' or path.is_absolute() or '..' in path.parts or '.git' in path.parts:
            raise ValueError('WT_%s doit être un chemin relatif sûr au projet.' % key)
    for name, default in (('DOCUMENTS_DIR','doc'), ('JOURNAL_DIR','sessions_codex'),
                          ('STATE_DIR','doc/worktree-state'), ('SHARED_TEMP_DIR','tmp')):
        relative_path(setting(name, default), name)
    temporary = Path(setting('SHARED_TEMP_DIR', 'tmp'))
    for protected in (Path(setting('DOCUMENTS_DIR', 'doc')), Path(setting('JOURNAL_DIR', 'sessions_codex')), Path('.codex')):
        if temporary == protected or temporary in protected.parents or protected in temporary.parents:
            raise ValueError('WT_SHARED_TEMP_DIR chevauche un dossier de documentation ou de sessions.')
    for name in ('PROFILE_FILES', 'COPY_FILES', 'SHARED_CONFIG_FILES', 'CI_SHARED_DIRS'):
        for path in setting_json(name, []):
            relative_path(path, name)
    for name, paths in setting_json('SPECIAL_COMMITS', {}).items():
        if not isinstance(paths, list) or not all(isinstance(path, str) for path in paths):
            raise ValueError('WT_SPECIAL_COMMITS doit associer des messages à des listes de chemins.')
        for path in paths:
            relative_path(path, 'SPECIAL_COMMITS')
    for destination in setting_json('PUSH_TARGETS', {}).values():
        if not isinstance(destination, dict) or not isinstance(destination.get('remote'), str):
            raise ValueError('Destination invalide dans WT_PUSH_TARGETS.')
        before = destination.get('before', [])
        if not isinstance(before, list) or not all(isinstance(arg, str) for arg in before):
            raise ValueError('La commande before de WT_PUSH_TARGETS doit être une liste JSON.')


validate()

if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true', help='Valider la configuration sans afficher ses valeurs')
    parser.add_argument('--session-env', choices=('SHELL_CLOSE', 'VIM_SESSION', 'VIM_CLOSE', 'VIM_REFUSED'), help='Lire une variable de session, avec les préfixes historiques configurés')
    parser.add_argument('--main-root', action='store_true', help='Afficher uniquement le chemin physique du dépôt principal')
    args = parser.parse_args()
    if args.session_env:
        prefixes = ['WT'] + setting_json('LEGACY_ENV_PREFIXES', [])
        print(next((os.environ[prefix + '_' + args.session_env] for prefix in prefixes if os.environ.get(prefix + '_' + args.session_env)), ''))
    elif args.main_root:
        print(configured_path('MAIN_ROOT'))
    else:
        print('Configuration worktree valide.')
