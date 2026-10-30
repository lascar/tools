"""Runtimes applicatifs indépendants ; infrastructure de développement commune."""
import errno
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import uuid
from _wt_common import *
import _wt_sessions as sessions

APPS = setting_json('APP_SERVICES', [])
INFRA = setting_json('INFRA_SERVICES', [])
MODES = setting_json('MODES', {})
PORTS = range(PORT_START, PORT_END + 1)
INTERNAL_PORT = setting_int('INTERNAL_PORT', MAIN_PORT)
INTERNAL_DEBUG_PORT = setting_int('INTERNAL_DEBUG_PORT', DEBUG_PORT)
HTTP_SERVICE = setting('HTTP_SERVICE', 'http')
DEBUG_SERVICE = setting('DEBUG_SERVICE', 'debug')
TLS_SERVICE = setting('TLS_SERVICE', 'tls')
PRIVATE_NETWORK = setting('PRIVATE_NETWORK', 'private')
SHARED_NETWORK = setting('SHARED_NETWORK', 'shared')
APP_ALIAS = setting('APP_ALIAS', 'app')


def docker(*args, **kw):
    return run(['docker', *args], **kw)


def prune_unused_worktree_networks():
    # Les labels limitent le nettoyage aux reseaux prives de nos ateliers.
    projects = docker('network', 'ls', '--filter',
                      'label=com.docker.compose.network=' + PRIVATE_NETWORK,
                      '--format', '{{.Label "com.docker.compose.project"}}', capture=True).stdout.split()
    for project in sorted(set(projects)):
        if project == DOCKER_PROJECT or not project.startswith(DOCKER_PREFIX) or project == DOCKER_PREFIX:
            continue
        # Prune conserve aussi les reseaux references par des conteneurs arretes.
        # Docker verifie leur utilisation au moment de la suppression.
        docker('network', 'prune', '--force', '--filter',
               'label=com.docker.compose.project=' + project, '--filter',
               'label=com.docker.compose.network=' + PRIVATE_NETWORK)


def compose_up(command, *options):
    for attempt in range(2):
        # Garder stdout visible et lire stderr sans pipe susceptible de se bloquer.
        with tempfile.TemporaryFile(mode='w+') as errors:
            result = run(command + ['up', *options], check=False, stderr=errors)
            errors.seek(0)
            diagnostic = errors.read()
        if diagnostic:
            print(diagnostic, end='', file=sys.stderr, flush=True)
        if result.returncode == 0:
            return result
        if attempt or 'all predefined address pools have been fully subnetted' not in diagnostic.lower():
            raise ToolError('Démarrage Docker en échec (%s) ; consulte les erreurs ci-dessus.' % result.returncode)
        print('Plages réseau Docker épuisées : nettoyage des réseaux privés d’ateliers inutilisés, '
              'puis une nouvelle tentative. Les réseaux utilisés sont conservés.', flush=True)
        prune_unused_worktree_networks()


def project_for(root):
    return DOCKER_PROJECT if root == MAIN else DOCKER_PREFIX + key(root)


def compose_file(root=None, ci=False):
    root = root or MAIN
    config = setting_json('CI_COMPOSE_CONFIG' if ci else 'COMPOSE_CONFIG', {})
    if not config:
        return root / setting('CI_COMPOSE_FILE' if ci else 'COMPOSE_FILE')
    # Chaque profil a son fichier immuable pour ne pas remplacer celui d'un atelier actif.
    import hashlib
    digest = hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()[:16]
    path = STATE / 'configs' / (key(root) + ('.ci' if ci else '') + '.base-' + digest + '.compose.json')
    if not path.exists():
        atomic(path, config)  # mkstemp conserve des permissions privées (0600).
    return path


def base(override=None, project=None):
    args = ['docker', 'compose', '-p', project or DOCKER_PROJECT, '--project-directory', str(MAIN),
            '--env-file', str(MAIN / setting('COMPOSE_ENV_FILE', '.env')) if (MAIN / setting('COMPOSE_ENV_FILE', '.env')).exists() else '/dev/null',
            '-f', str(compose_file())]
    if override: args += ['-f', str(override)]
    return args


def state_path(root):
    return STATE / 'runtimes' / (key(root) + '.json')


def read_state(root):
    state = read_json(state_path(root))
    if state:
        if state.get('worktree') != str(root):
            raise ToolError('État Docker associé à un autre worktree.')
        if state.get('version') == 2 and state.get('project') != project_for(root):
            raise ToolError('Projet Docker incohérent avec le worktree.')
        return state
    # Compatibilité avec l'ancien runtime unique, jusqu'à sa prochaine activation.
    old = read_json(STATE / 'active.json', {})
    return old if old.get('worktree') == str(root) else {}


def all_states():
    states = {}
    for path in sorted((STATE / 'runtimes').glob('*.json')):
        state = read_json(path)
        if state and state.get('worktree'):
            states[state['worktree']] = state
    old = read_json(STATE / 'active.json', {})
    if old.get('worktree') and old['worktree'] not in states:
        states[old['worktree']] = old
    return list(states.values())


def write_state(state):
    old = read_json(STATE / 'active.json', {})
    if old.get('worktree') and not state_path(Path(old['worktree'])).exists():
        atomic(state_path(Path(old['worktree'])), old)
    atomic(state_path(Path(state['worktree'])), state)
    # Les vieux onglets liront encore ce fichier ; les nouveaux utilisent leur état dédié.
    atomic(STATE / 'active.json', state)


def runtime_url(state):
    return '%s://localhost:%s' % ('http' if state.get('mode') == 'http' else 'https', state.get('port', MAIN_PORT))


def command_for(state):
    return base(state.get('override'), state.get('project', DOCKER_PROJECT))


def atomic_text(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix='.' + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            stream.write(text); stream.flush(); os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp): os.unlink(tmp)


def debugger_port(port):
    return DEBUG_PORT + port - PORT_START


def application_commands():
    """Les commandes du profil remplacent celles de Compose, sans shell implicite."""
    commands = {MODES[mode]: argv for mode, argv in setting_json('SERVER_COMMANDS', {}).items()}
    console = setting_json('CONSOLE_COMMAND', [])
    if console:
        commands[CONSOLE_SERVICE] = console
    return commands


def generate(root, port=MAIN_PORT):
    identifier = key(root)
    commands = application_commands()
    mount = {'type': 'bind', 'source': str(root), 'target': APP_PATH, 'bind': {'create_host_path': False}}
    shared_mounts = sessions.tmp_mount(root)
    # Les volumes persistants sont propres à chaque worktree.
    volumes = {v: {'name': setting('VOLUME_PREFIX', DOCKER_PREFIX) + identifier + '_' + v} for v in setting_json('VOLUMES', [])}
    if root == MAIN:
        path = STATE / 'configs' / (identifier + '.compose.json')
        atomic(path, {'services': {name: dict({'volumes': [mount]}, **({'command': commands[name]} if name in commands else {})) for name in APPS}, 'volumes': volumes})
    else:
        if port not in PORTS: raise ToolError('Port de tâche hors de la plage configurée.')
        proxy = STATE / 'configs' / (identifier + '.proxy.conf')
        atomic_text(proxy, setting_json('PROXY_TEMPLATE', '').replace('@PORT@', str(port)))
        # !override remplace ports/réseaux au lieu d'ajouter les ports du fichier de base.
        lines = ['services:']
        for name in APPS:
            networks = setting_json('APP_NETWORKS', {})
            if name in setting_json('ALIAS_SERVICES', []):
                networks[PRIVATE_NETWORK] = {'aliases': [APP_ALIAS]}
            labels = {RUNTIME_LABEL: 'worktree', WORKTREE_LABEL: str(root)}
            lines += ['  '+name+':', '    volumes: '+json.dumps([mount] + shared_mounts),
                      '    labels: '+json.dumps(labels), '    depends_on: !reset {}',
                      '    networks: !override '+json.dumps(networks)]
            if name in commands:
                lines.append('    command: '+json.dumps(commands[name]))
            if name in (TLS_SERVICE, HTTP_SERVICE, DEBUG_SERVICE):
                published = ([] if name == TLS_SERVICE else
                             ['127.0.0.1:%d:%d' % (port, INTERNAL_PORT)] if name == HTTP_SERVICE else
                             ['127.0.0.1:%d:%d' % (debugger_port(port), INTERNAL_DEBUG_PORT)])
                lines.append('    ports: !override '+json.dumps(published))
        proxy_mount = {'type': 'bind', 'source': str(proxy), 'target': setting('PROXY_CONFIG_PATH'),
                       'read_only': True, 'bind': {'create_host_path': False}}
        lines += ['  '+PROXY_SERVICE+':', '    labels: '+json.dumps({RUNTIME_LABEL: 'worktree', WORKTREE_LABEL: str(root)}),
                  '    ports: !override '+json.dumps(['127.0.0.1:%d:%d' % (port, INTERNAL_PORT)]),
                  '    volumes: '+json.dumps([proxy_mount])]
        for name in INFRA:
            lines += ['  '+name+':', '    profiles: !override [shared-infrastructure]']
        volumes[setting('PROXY_VOLUME', 'proxy_config')] = {'name': setting('VOLUME_PREFIX', DOCKER_PREFIX) + identifier + '_' + setting('PROXY_VOLUME', 'proxy_config')}
        networks = {name: {'external': True, 'name': external}
                    for name, external in setting_json('EXTERNAL_NETWORKS', {}).items()}
        lines += ['volumes: '+json.dumps(volumes), 'networks: '+json.dumps(networks)]
        path = STATE / 'configs' / (identifier + '.compose.yml')
        atomic_text(path, '\n'.join(lines)+'\n')
    # Ne pas afficher compose config : sa sortie contient les secrets interpolés.
    effective = json.loads(run(base(path, project_for(root)) + ['config', '--format', 'json'], capture=True).stdout)
    for service in APPS:
        selected = [v for v in effective['services'][service]['volumes'] if v['target'] == APP_PATH]
        if len(selected) != 1 or Path(selected[0]['source']).resolve() != root:
            raise ToolError('Montage Compose incorrect : ' + service)
        if root != MAIN:
            ports = {int(p['published']) for p in effective['services'][service].get('ports', [])}
            expected = {port} if service == HTTP_SERVICE else {debugger_port(port)} if service == DEBUG_SERVICE else set()
            if ports != expected: raise ToolError('Ports Compose incorrects : ' + service)
            shared = effective['services'][service]['networks'].get(SHARED_NETWORK, {}) or {}
            if APP_ALIAS in shared.get('aliases', []): raise ToolError('Alias app partagé refusé : ' + service)
    if root != MAIN:
        if {int(p['published']) for p in effective['services'][PROXY_SERVICE].get('ports', [])} != {port}:
            raise ToolError('Port Compose incorrect : ' + PROXY_SERVICE)
        for name, expected in setting_json('EXTERNAL_NETWORKS', {}).items():
            network = effective['networks'][name]
            if not network.get('external') or network.get('name') != expected:
                raise ToolError('Réseau commun Compose incorrect : ' + name)
    return path


def containers():
    # Le projet principal et les projets de tâche sont reconnus par leurs labels.
    ids = docker('ps', '-a', '--filter', 'label=com.docker.compose.project',
                 '--format', '{{.ID}}', capture=True).stdout.split()
    if not ids: return []
    inspected = json.loads(docker('inspect', *ids, capture=True).stdout)
    result = []
    for item in inspected:
        labels = item.get('Config', {}).get('Labels', {})
        project = labels.get('com.docker.compose.project', '')
        if project == DOCKER_PROJECT or (project.startswith(DOCKER_PREFIX) and labels.get(RUNTIME_LABEL) == 'worktree'):
            result.append(item)
    return result


def app_mounts(running_only=False, project=None):
    result = []
    for item in containers():
        if project and item.get('Config', {}).get('Labels', {}).get('com.docker.compose.project') != project:
            continue
        service = item.get('Config', {}).get('Labels', {}).get('com.docker.compose.service')
        if service not in APPS: continue
        if running_only and not item['State']['Running']: continue
        mounted = [m['Source'] for m in item['Mounts'] if m['Destination'] == APP_PATH]
        if len(mounted) != 1: raise ToolError('Montage applicatif absent ou ambigu : ' + item['Name'])
        result.append((service, Path(mounted[0]).resolve(), item['Id'], item['State']['Running']))
    return result


def published_ports():
    ids = docker('ps', '-q', capture=True).stdout.split()
    if not ids: return {}
    result = {}
    for item in json.loads(docker('inspect', *ids, capture=True).stdout):
        project = item.get('Config', {}).get('Labels', {}).get('com.docker.compose.project', '')
        for bindings in (item.get('NetworkSettings', {}).get('Ports') or {}).values():
            for binding in bindings or []:
                result.setdefault(int(binding['HostPort']), set()).add(project)
    return result


def port_available(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try: probe.bind(('127.0.0.1', port))
        except OSError as exc:
            if exc.errno in (errno.EADDRINUSE, errno.EACCES): return False
            raise
    return True


def choose_port(root, mode):
    if root == MAIN: return MAIN_PORT
    project = project_for(root)
    published = published_ports()
    legacy_ports = set()
    if any(DOCKER_PROJECT in owners for port, owners in published.items() if port in PORTS or DEBUG_PORT <= port < DEBUG_PORT + len(PORTS)):
        for item in containers():
            labels = item.get('Config', {}).get('Labels', {})
            if (labels.get('com.docker.compose.project') == DOCKER_PROJECT and item['State']['Running']
                    and labels.get('com.docker.compose.service') in APPS and any(
                        m['Destination'] == APP_PATH and Path(m['Source']).resolve() == root for m in item['Mounts'])):
                for bindings in (item.get('NetworkSettings', {}).get('Ports') or {}).values():
                    legacy_ports.update(int(b['HostPort']) for b in bindings or [])
    old = read_state(root)
    preferred = old.get('port')
    # Garder l'adresse si possible, puis chercher les ports suivants et
    # revenir au debut de la plage pour ne pas refuser une place libre.
    candidates = list(PORTS)
    if preferred in PORTS:
        offset = candidates.index(preferred)
        candidates = candidates[offset:] + candidates[:offset]
    reserved = {s['port'] for s in all_states() if s.get('worktree') != str(root)
                and s.get('status') in ('active', 'activating') and s.get('port') in PORTS}
    for port in candidates:
        if port in reserved: continue
        ports = [port] + ([debugger_port(port)] if mode == 'debug' else [])
        if all((published.get(p) == {project}) or
               (published.get(p) == {DOCKER_PROJECT} and p in legacy_ports) or
               (p not in published and port_available(p)) for p in ports):
            return port
    raise ToolError('Aucun port libre dans la plage configurée ; arrête un service de tâche avant d’en ouvrir un autre.')


def active_for(root):
    state = read_state(root)
    if not state or state.get('status') != 'active':
        raise ToolError('Ce worktree n’est pas actif dans Docker. Utilise worktree_docker.sh status/activate.')
    if any(p != root for _, p, _, _ in app_mounts(True, state.get('project', DOCKER_PROJECT))):
        raise ToolError('Le projet Docker exécute un autre worktree ; relance activate depuis la tâche.')
    return state


def wait_for_server(root, service, timeout=60, require_console=True):
    deadline = time.monotonic() + timeout
    probe = setting_json('SERVER_PROBE', [])
    project = read_state(root).get('project', project_for(root))
    while True:
        live = {s: cid for s, p, cid, running in app_mounts(True) if p == root and running}
        missing = ({CONSOLE_SERVICE, service} if require_console else {service}) - set(live)
        if missing:
            raise ToolError('Services applicatifs arrêtés ou absents après activation : %s. '
                            'Consulte docker logs --tail 80 %s-<service>-1.' % (', '.join(sorted(missing)), project))
        if not probe or docker('exec', live[service], *probe,
                  capture=True, check=False).returncode == 0: return
        if time.monotonic() >= deadline:
            raise ToolError('Le service n’est pas prêt après %s secondes ; '
                            'consulte docker logs --tail 80 %s-%s-1.' % (timeout, project, service))
        time.sleep(1)


def ensure_infrastructure(command):
    if INFRA: compose_up(command, '-d', '--no-recreate', *INFRA)
    if not setting_json('INFRA_PROBE', []): return
    deadline = time.monotonic() + 300
    while run(command + setting_json('INFRA_PROBE', []),
              capture=True, check=False).returncode:
        if time.monotonic() > deadline: raise ToolError('Infrastructure non prête après 300 secondes.')
        time.sleep(1)


def check_config(root):
    for file in setting_json('SHARED_CONFIG_FILES', []):
        if setting_json('COMPOSE_CONFIG', {}) and file == setting('COMPOSE_FILE', ''):
            continue
        a, b = root / file, MAIN / file
        if a.exists() and b.exists() and a.read_bytes() != b.read_bytes():
            raise ToolError('Configuration différente du dépôt principal : %s. Tester cette infrastructure séparément.' % file)


def stop_legacy(root):
    # Migrer uniquement les anciens conteneurs montés sur cette tâche.
    owned = []
    for item in containers():
        labels = item.get('Config', {}).get('Labels', {})
        if labels.get('com.docker.compose.project') != DOCKER_PROJECT or not item['State']['Running']: continue
        if labels.get('com.docker.compose.service') in APPS and any(
                m['Destination'] == APP_PATH and Path(m['Source']).resolve() == root for m in item['Mounts']):
            owned.append(item['Id'])
    if owned: docker('stop', *owned)
    old = read_state(root)
    if owned and old.get('worktree') == str(root) and not old.get('project'):
        run(base() + ['stop', PROXY_SERVICE])


def activate(root, mode=DEFAULT_MODE):
    root, branch, task = context(root)
    if mode not in MODES: raise ToolError('Mode Docker inconnu : ' + mode)
    with locked(STATE / 'runtime.lock'):
        check_config(root)
        if root == MAIN and any(p != MAIN for _, p, _, _ in app_mounts(True, DOCKER_PROJECT)):
            raise ToolError('Ancien runtime partagé associé à une tâche ; active d’abord son serveur indépendant depuis son worktree.')
        port = choose_port(root, mode)
        override = generate(root, port)
        for directory in setting_json('RUNTIME_DIRS', []):
            (root / directory).mkdir(parents=True, exist_ok=True)
        ensure_infrastructure(base())
        old = read_state(root)
        command = base(override, project_for(root))
        live = {s for s, p, _, running in app_mounts(True) if p == root and running}
        if (old.get('status') == 'active' and old.get('project') == project_for(root)
                and old.get('mode') == mode and old.get('port') == port and {CONSOLE_SERVICE, MODES[mode]} <= live):
            wait_for_server(root, MODES[mode])
            print('Docker est déjà actif pour %s : %s.' % (task, runtime_url(old)))
            return old
        print('Activation Docker : %s — %s (%s), %s://localhost:%d' %
              (root, branch, mode, 'http' if mode == 'http' else 'https', port), flush=True)
        if root != MAIN: stop_legacy(root)
        candidate = {'version': 2, 'project': project_for(root), 'port': port, 'worktree': str(root),
                     'branch': branch, 'mode': mode, 'override': str(override),
                     'status': 'activating', 'activation_id': uuid.uuid4().hex}
        write_state(candidate)
        # Le projet ciblé possède uniquement les services applicatifs de ce worktree.
        run(command + ['stop', *APPS, PROXY_SERVICE])
        services = [CONSOLE_SERVICE, MODES[mode]] + ([] if mode == 'http' else [PROXY_SERVICE])
        compose_up(command, '-d', '--no-deps', '--force-recreate', *services)
        wait_for_server(root, MODES[mode])
        candidate.update(status='active', updated_at=now())
        write_state(candidate)
        return candidate


def ensure_service(root, role, mode=None):
    root, _, _ = context(root)
    if role not in ('console', 'server'): raise ToolError('Service à relancer invalide.')
    with locked(STATE / 'runtime.lock'):
        state = read_state(root)
        if root == MAIN and any(p != MAIN for _, p, _, _ in app_mounts(True, DOCKER_PROJECT)):
            raise ToolError('Ancien runtime partagé associé à une tâche ; active d’abord son serveur indépendant depuis son worktree.')
        selected_mode = mode or state.get('mode') or DEFAULT_MODE
        if selected_mode not in MODES: raise ToolError('Mode Docker inconnu : ' + selected_mode)
        if (state.get('status') == 'active' and state.get('project') == project_for(root)
                and state.get('mode') == selected_mode):
            check_config(root)
            ensure_infrastructure(base())
            command = command_for(state)
            service = CONSOLE_SERVICE if role == 'console' else MODES[selected_mode]
            live = {s for s, p, _, running in app_mounts(True) if p == root and running}
            if selected_mode != 'http':
                compose_up(command, '-d', '--no-deps', '--no-recreate', PROXY_SERVICE)
            if service not in live:
                # Relire le profil pour la prochaine création du service arrêté.
                override = generate(root, state.get('port', MAIN_PORT))
                command = base(override, project_for(root))
                compose_up(command, '-d', '--no-deps', service)
            if role == 'server': wait_for_server(root, service, require_console=False)
            elif not any(s == CONSOLE_SERVICE and p == root and running for s, p, _, running in app_mounts(True)):
                raise ToolError('La console s’est arrêtée au démarrage ; consulte docker logs --tail 80 %s-console-1.' % state['project'])
            return state
    return activate(root, selected_mode)


def stop_all(root, infrastructure=False):
    root, branch, _ = context(root)
    if branch != BASE_BRANCH: return stop(root, infrastructure)
    with locked(STATE / 'ci.lock'), locked(STATE / 'runtime.lock'):
        ids = []
        for selector in ('com.docker.compose.project='+DOCKER_PROJECT, 'com.docker.compose.project='+setting('CI_PROJECT'),
                         RUNTIME_LABEL+'=worktree'):
            ids.extend(docker('ps', '-q', '--filter', 'label='+selector, capture=True).stdout.split())
        if ids: docker('stop', *dict.fromkeys(ids))
        else: print('Aucun conteneur du projet actif.')
        for state in all_states():
            state.update(status='stopped', updated_at=now()); write_state(state)


def stop(root, infrastructure=False):
    root, _, _ = context(root)
    with locked(STATE / 'runtime.lock'):
        mounts = app_mounts(True)
        if infrastructure and any(p != root for _, p, _, _ in mounts):
            raise ToolError('Infrastructure utilisée par un autre worktree ; arrêt commun refusé.')
        state = read_state(root)
        if root == MAIN and any(p != MAIN for _, p, _, _ in app_mounts(True, DOCKER_PROJECT)):
            raise ToolError('Ancien runtime associé à une tâche ; utilise son worktree pour l’arrêter.')
        if state.get('project') == project_for(root):
            print('Arrêt des services de %s (port attribué : %s).' %
                  (root.name, state.get('port', 'inconnu')), flush=True)
            run(command_for(state) + ['stop', *APPS, PROXY_SERVICE])
        elif root == MAIN:
            run(base() + ['stop', *APPS, PROXY_SERVICE])
        else:
            stop_legacy(root)
        if infrastructure: run(base() + ['stop', *INFRA])
        if state:
            state.update(status='stopped', updated_at=now()); write_state(state)


def service_command(root, action, service=None):
    state = active_for(root)
    service = service or MODES[state['mode']]
    if service not in APPS: raise ToolError('Service applicatif invalide.')
    args = command_for(state)
    if action == 'shell': return args + ['run', '--rm', '--no-deps', SHELL_SERVICE]
    if action == 'logs': return args + ['logs', '-f', service]
    if action == 'restart': return args + ['restart', service]
    ids = run(args + ['ps', '-q', service], capture=True).stdout.split()
    if len(ids) != 1: raise ToolError('Conteneur actif introuvable : ' + service)
    info = json.loads(docker('inspect', ids[0], capture=True).stdout)[0]
    if not any(m['Destination'] == APP_PATH and Path(m['Source']).resolve() == root for m in info['Mounts']):
        raise ToolError('Le conteneur vise un autre dossier.')
    return ['docker', 'attach', ids[0]]


def status():
    for state in all_states():
        current = state.get('status')
        # Une adresse n'est utile que lorsque le serveur est actif ; le
        # port memorise pour la prochaine activation reste interne.
        if current == 'active':
            detail = '%s — %s (actif)' % (runtime_url(state), state.get('mode', 'http'))
        else:
            detail = {'stopped': 'serveur arrêté',
                      'activating': 'serveur en cours de démarrage'}.get(current, 'état du serveur inconnu')
        print('%s : %s' % (state['worktree'], detail))
    for service, path, cid, live in app_mounts():
        print('%s %s %s %s' % (service, path, cid[:12], 'actif' if live else 'arrêté'))


