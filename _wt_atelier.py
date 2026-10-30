"""Fenêtre GNOME à cinq onglets, fermeture coopérative sans kill global."""
import os
import ast
from pathlib import Path
import shutil
import select
import signal
import subprocess
import sys
import time
import uuid
from _wt_common import *
import _wt_docker as runtime
import _wt_patch as patches
import _wt_sessions as sessions

# Garder l'identifiant historique vim pour fermer aussi les anciens ateliers.
ROLES = list(setting_json('ROLE_TITLES', {'codex':'Codex', 'vim':'Bash', 'vim_doc':'Vim documentation'}).items())


def identity(pid):
    try:
        return Path('/proc/%s/stat' % pid).read_text().rsplit(')', 1)[1].split()[19]
    except (FileNotFoundError, ProcessLookupError):
        return None


def alive(record, prefix=''):
    return bool(record and record.get(prefix + 'pid') and
                identity(record[prefix + 'pid']) == record.get(prefix + 'start'))


def directory(root):
    return STATE / 'ateliers' / key(root)


def open_roles(root):
    """Ne pas confondre un ancien état sur disque avec un onglet encore vivant."""
    folder = directory(root)
    state = read_json(folder / 'atelier.json', {})
    generation = state.get('generation')
    if not generation:
        return []
    roles = []
    for role, _ in ROLES:
        record = read_json(folder / (role + '.json'), {})
        if (record.get('generation') == generation and record.get('worktree') == str(root)
                and record.get('role') == role and record.get('status') != 'closed'
                and record.get('start') and alive(record)):
            roles.append(role)
    return roles


def terminal_target(root):
    folder = directory(root)
    state = read_json(folder / 'atelier.json', {})
    record = read_json(folder / 'codex.json', {})
    if (not alive(record) or record.get('generation') != state.get('generation') or
            record.get('worktree') != str(root)):
        raise ToolError('Aucun onglet Codex ouvert pour %s.' % root.name)
    # Lire seulement ces deux variables ; les anciens superviseurs les ont aussi.
    try:
        env = dict(item.split(b'=', 1) for item in
                   Path('/proc/%s/environ' % record['pid']).read_bytes().split(b'\0') if b'=' in item)
        service = env[b'GNOME_TERMINAL_SERVICE'].decode()
        screen = env[b'GNOME_TERMINAL_SCREEN'].decode()
        identifier = str(uuid.UUID(screen.rsplit('/', 1)[-1].replace('_', '-')))
    except (OSError, KeyError, ValueError, UnicodeError) as error:
        raise ToolError('Impossible de retrouver le terminal de cet atelier : %s' % error)
    if not alive(record) or not service.startswith(':'):
        raise ToolError('Le terminal a changé pendant sa recherche ; relance la commande.')
    return service, identifier


def master_terminal_target():
    if context(MAIN)[1] != BASE_BRANCH:
        raise ToolError(f'Le dépôt principal n’est pas sur {BASE_BRANCH}.')
    targets = {}
    # Le terminal principal n’a pas de superviseur d’atelier : retrouver son
    # shell par son répertoire physique, jamais par un titre modifiable.
    for proc in Path('/proc').iterdir():
        if not proc.name.isdigit(): continue
        try:
            start = identity(int(proc.name))
            if (proc / 'comm').read_text().strip() not in ('bash', 'zsh', 'fish', 'sh', 'dash'):
                continue
            if (proc / 'cwd').resolve(strict=True) != MAIN: continue
            env = dict(item.split(b'=', 1) for item in
                       (proc / 'environ').read_bytes().split(b'\0') if b'=' in item)
            service = env[b'GNOME_TERMINAL_SERVICE'].decode()
            screen = env[b'GNOME_TERMINAL_SCREEN'].decode()
            identifier = str(uuid.UUID(screen.rsplit('/', 1)[-1].replace('_', '-')))
            if start and identity(int(proc.name)) == start and service.startswith(':'):
                target = (service, identifier)
                targets[target] = min(int(start), targets.get(target, int(start)))
        except (FileNotFoundError, ProcessLookupError, PermissionError, KeyError):
            # Processus terminé, d’un autre utilisateur ou sans terminal GNOME.
            continue
    if not targets:
        raise ToolError(f'Aucun terminal {BASE_BRANCH} trouvé. Ouvre un terminal dans %s.' % MAIN)
    # Plusieurs shells peuvent être dans master : préférer le terminal d’origine.
    return min(targets, key=lambda target: (targets[target], target))


def worktree_switch(root=None):
    service, identifier = master_terminal_target() if root is None else terminal_target(root)
    base = ['gdbus', 'call', '--session', '--dest', service, '--object-path',
            '/org/gnome/Terminal/SearchProvider', '--method',
            'org.gnome.Shell.SearchProvider2.']
    def call(method, *args):
        try:
            return run(base[:-1] + [base[-1] + method, *args], capture=True, timeout=10).stdout
        except (OSError, subprocess.TimeoutExpired) as error:
            raise ToolError('GNOME Terminal inaccessible : %s' % error)
    # Vérifier l’UUID vivant avant ActivateResult, qui sélectionne et présente
    # l’onglet existant sans créer de fenêtre ni envoyer de commande au shell.
    results = ast.literal_eval(call('GetInitialResultSet', '[]').replace('@as ', ''))[0]
    if identifier not in results:
        raise ToolError('L’onglet a été fermé ; aucune fenêtre créée.')
    call('ActivateResult', identifier, '[]', '0')
    print('Fenêtre demandée : %s.' % (root.name + ' — Codex' if root else BASE_BRANCH))


def preflight():
    for binary in ('gnome-terminal', 'gsettings', 'dconf', 'docker', 'vim'):
        if not shutil.which(binary): raise ToolError('Commande requise introuvable : ' + binary)
    sessions.codex_binary()
    if not (os.environ.get('DISPLAY') or os.environ.get('WAYLAND_DISPLAY')):
        raise ToolError('Pas de session graphique. Utilise --prepare-only pour préparer le worktree.')


def master_context(root):
    root, branch, _ = context(root)
    if root != MAIN or branch != BASE_BRANCH:
        raise ToolError(f'worktree_lance_master.sh doit être lancé depuis le dépôt principal sur {BASE_BRANCH}.')
    return root


def open_master(root, mode=None):
    root = master_context(root)
    for binary in ('gnome-terminal', 'docker'):
        if not shutil.which(binary): raise ToolError('Commande requise introuvable : ' + binary)
    if not (os.environ.get('DISPLAY') or os.environ.get('WAYLAND_DISPLAY')):
        raise ToolError(f'Pas de session graphique pour ouvrir les onglets {BASE_BRANCH}.')
    service = os.environ.get('GNOME_TERMINAL_SERVICE', '')
    screen = os.environ.get('GNOME_TERMINAL_SCREEN', '')
    if not service.startswith(':') or not screen.startswith('/org/gnome/Terminal/screen/'):
        raise ToolError(f'Lance cette commande dans le terminal GNOME de {BASE_BRANCH} pour y ajouter les deux onglets.')
    # Le mode existant est conserve afin de ne pas recreer inutilement un
    # serveur HTTPS/debug deja utilise. Docker n'est active qu'apres validation.
    selected_mode = mode or runtime.read_state(root).get('mode') or 'http'
    state = runtime.activate(root, selected_mode)
    for role, title in setting_json('MAIN_ROLE_TITLES', {'console': 'Console', 'server': 'Service'}).items():
        # GNOME utilise le screen herite pour cibler la fenetre appelante.
        # Les arguments apres -- restent des argv, sans interpolation shell.
        args = ['gnome-terminal', '--tab', '--title', PROJECT_NAME + '/' + BASE_BRANCH,
                '--working-directory', str(root), '--', sys.executable,
                str(TOOLS / 'worktree_tools.py'), '_master_tab', role]
        try:
            run(args, cwd=root, timeout=15)
        except (ToolError, OSError, subprocess.TimeoutExpired) as error:
            raise ToolError('Ouverture de l’onglet %s impossible : %s. '
                            'Docker et les onglets déjà ouverts sont conservés.' % (title, error))
    print('Onglets des services principaux ouverts — %s.' % runtime.runtime_url(state))


def master_tab(root, role):
    # Revalider la branche dans chaque onglet, apres le lancement graphique.
    root = master_context(root)
    if role not in ('console', 'server'): raise ToolError(f'Onglet {BASE_BRANCH} invalide.')
    print('Ctrl-P puis Ctrl-Q pour détacher sans arrêter le service Docker.', flush=True)
    return run(runtime.service_command(root, 'attach', CONSOLE_SERVICE if role == 'console' else None), cwd=root).returncode


def terminal_profile():
    # Le profil personnel peut garder les onglets après exit (exit-action=hold).
    # Cloner son apparence une seule fois, sans modifier le profil par défaut.
    schema = 'org.gnome.Terminal.ProfilesList'
    identifier = str(uuid.uuid5(uuid.NAMESPACE_URL, setting('TERMINAL_PROFILE_NAMESPACE', 'worktree-atelier:') + str(MAIN)))
    name = setting('TERMINAL_PROFILE_TITLE', PROJECT_NAME + ' ateliers ') + identifier[:8]
    path = '/org/gnome/terminal/legacy/profiles:/:' + identifier + '/'
    profile_schema = 'org.gnome.Terminal.Legacy.Profile:' + path
    with locked(STATE / 'terminal-profile.lock', wait=True):
        profiles = ast.literal_eval(run(['gsettings', 'get', schema, 'list'], capture=True).stdout)
        if identifier not in profiles:
            default = ast.literal_eval(run(['gsettings', 'get', schema, 'default'], capture=True).stdout)
            source = '/org/gnome/terminal/legacy/profiles:/:' + default + '/'
            settings = run(['dconf', 'dump', source], capture=True).stdout
            run(['dconf', 'load', path], input=settings)
        run(['gsettings', 'set', profile_schema, 'visible-name', name])
        run(['gsettings', 'set', profile_schema, 'exit-action', 'close'])
        if identifier not in profiles:
            # Relire avant ajout pour conserver un profil créé entre-temps.
            profiles = ast.literal_eval(run(['gsettings', 'get', schema, 'list'], capture=True).stdout)
            if identifier not in profiles:
                run(['gsettings', 'set', schema, 'list', repr(profiles + [identifier])])
        if ast.literal_eval(run(['gsettings', 'get', profile_schema, 'exit-action'], capture=True).stdout) != 'close':
            raise ToolError('Le profil GNOME de l’atelier ne ferme pas les onglets à la sortie.')
    return name


def open_atelier(root, mode=DEFAULT_MODE, offer_patches=True):
    root, branch, task = context(root, task=True)
    preflight()
    folder = directory(root)
    with locked(folder / 'atelier.lock'):
        state = read_json(folder / 'atelier.json', {})
        live = [role for role, _ in ROLES if alive(read_json(folder / (role + '.json')))]
        if live:
            if state.get('status') == 'open' and len(live) == len(ROLES):
                runtime.activate(root, mode)
                runtime_tabs_notice(root)
                print('Atelier déjà ouvert : Docker réactivé pour %s.' % task)
                return
            raise ToolError('Atelier déjà ouvert (%s). Ferme-le avant de le rouvrir.' % ', '.join(live))
        sessions.codex_command(root)  # vérifier la reprise avant de toucher à Docker
        if offer_patches:
            patches.offer(root)
        profile = terminal_profile()
        runtime.activate(root, mode)
        generation = uuid.uuid4().hex
        atomic(folder / 'atelier.json', {'worktree': str(root), 'branch': branch, 'generation': generation,
                                        'status': 'opening', 'created_at': now()})
        for name in ('close', 'runtime.close', 'vim.close', 'vim_doc.close', 'vim.refused', 'vim_doc.refused'):
            p = folder / name
            if p.exists(): p.unlink()
        # Placé avant --window, le profil s'applique aux cinq onglets.
        args = ['gnome-terminal', '--profile', profile]
        for i, (role, title) in enumerate(ROLES):
            # Pas de commande shell interpolée : chemins et noms accentués sont des argv.
            import shlex
            command = [sys.executable, str(TOOLS / 'worktree_tools.py'), '_tab',
                       str(root), role, generation]
            args += ['--window' if i == 0 else '--tab', '--title', task,
                     '--working-directory', str(root), '--command', shlex.join(command)]
            if i == 0: args += ['--active']
        run(args)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            records = [read_json(folder / (role + '.json'), {}) for role, _ in ROLES]
            if any(r.get('generation') == generation and (r.get('status') == 'error' or
                   (r.get('status') == 'ready' and r.get('exit_code'))) for r in records):
                raise ToolError('Une application de l’atelier a échoué ; consulte son onglet avant de fermer.')
            if all(r.get('generation') == generation and alive(r) and
                   r.get('status') in ('running', 'ready', 'waiting') for r in records):
                atomic(folder / 'atelier.json', {'worktree': str(root), 'branch': branch,
                                                'generation': generation, 'status': 'open', 'created_at': now()})
                print('Atelier ouvert : %s\n%s' % (task, root)); return
            time.sleep(.2)
        raise ToolError('Ouverture partielle des onglets : état conservé. Vérifie les terminaux avant worktree_close.sh.')


def vim_command(root, role, folder=None):
    task = context(root, task=True)[2]
    path = root / ('vimsession_' + task + ('_doc' if role == 'vim_doc' else ''))
    if path.exists():
        args = ['vim', '-S', str(path)]
    elif role == 'vim_doc':
        # Une nouvelle tâche a des notes, mais pas encore de session à reprendre.
        notes = [root / setting('DOCUMENTS_DIR', 'doc') / (task + suffix) for suffix in ('_suivi.txt', '.txt')]
        if not any(note.is_file() for note in notes): return None
        args = ['vim'] + [str(note) for note in notes]
    else:
        return None
    if folder:
        args += ['-S', str(TOOLS / 'worktree_session.vim')]
    return args


def runtime_tabs_notice(root):
    # Les superviseurs Python déjà lancés ne rechargent pas leur code. Les
    # anciens onglets restent visibles ; leurs remplaçants surveillent Docker.
    folder = directory(root)
    legacy = [role for role in ('console', 'server')
              if alive(read_json(folder / (role + '.json'))) and
              read_json(folder / (role + '.json'), {}).get('runtime_version') != 2]
    if legacy:
        print('Anciens onglets Docker avec runtime partagé : %s. '
              'Ferme puis rouvre cet atelier une fois pour mettre ses onglets à jour. '
              'En attendant, utilise worktree_docker.sh console ou worktree_docker.sh attach '
              'depuis ce worktree.' % ', '.join(legacy))


def relaunch_runtime(root, role, mode=None):
    runtime.ensure_service(root, role, mode)
    folder = directory(root)
    record = read_json(folder / (role + '.json'), {})
    state = read_json(folder / 'atelier.json', {})
    managed = (alive(record) and record.get('worktree') == str(root) and
               record.get('generation') == state.get('generation') and
               not (folder / 'close').exists() and not (folder / 'runtime.close').exists())
    if managed and alive(record, 'child_'):
        print('Le service est actif et son onglet est déjà attaché.')
        return
    if managed and record.get('runtime_input'):
        atomic(folder / (role + '.attach.json'), {'id': uuid.uuid4().hex, 'generation': record['generation']})
        print('Relance demandée dans l’onglet %s du worktree.' % role)
        return
    # Un onglet ancien ou fermé ne peut pas reprendre la commande : conserver
    # l'accès interactif dans le terminal appelant sans fermer Codex ou Vim.
    print('Connexion au service dans ce terminal (Ctrl-P puis Ctrl-Q pour détacher).')
    return run(runtime.service_command(root, 'attach', CONSOLE_SERVICE if role == 'console' else None), cwd=root).returncode


def runtime_input(role):
    if not sys.stdin.isatty():
        time.sleep(.3)
        return None
    try:
        if not select.select([sys.stdin], [], [], .3)[0]:
            return None
        line = sys.stdin.readline()
    except KeyboardInterrupt:
        return None
    if not line:
        return 'exit'
    command = 'relance_console.sh' if role == 'console' else 'relance_serveur.sh'
    if line.strip() in ('', command):
        return 'restart'
    print('Tape Entrée ou %s pour relancer ; Ctrl-D ferme cet onglet.' % command)
    return None


def runtime_tab(root, role, folder, record, path):
    last_activation = None
    record.update(runtime_monitor=True, runtime_input=True, runtime_version=2, status='waiting')
    atomic(path, record)
    while not (folder / 'close').exists():
        state = runtime.read_state(root)
        request = read_json(folder / (role + '.attach.json'), {})
        request_id = request.get('id') if request.get('generation') == record.get('generation') else None
        token = (state.get('activation_id') or state.get('updated_at'), state.get('mode'), request_id)
        if (folder / 'runtime.close').exists():
            time.sleep(.3)
            continue
        if (state.get('status') != 'active' or
                state.get('worktree') != str(root) or token == last_activation):
            action = runtime_input(role)
            if action == 'exit':
                return
            if action == 'restart':
                try:
                    runtime.ensure_service(root, role)
                    last_activation = None
                except ToolError as exc:
                    print('Relance impossible : %s' % exc, file=sys.stderr)
            continue
        try:
            args = runtime.service_command(root, 'attach', CONSOLE_SERVICE if role == 'console' else None)
        except ToolError as exc:
            # Une bascule a pu intervenir entre la lecture d'état et inspect.
            # Réessayer uniquement si l'état a réellement changé.
            if runtime.read_state(root) != state:
                continue
            print('Attach impossible : %s. Tape Entrée pour relancer.' % exc, file=sys.stderr)
            last_activation = token
            continue
        # Revalider sous le verrou avant spawn pour ne jamais attacher le
        # conteneur d'un autre worktree pendant une activation concurrente.
        with locked(STATE / 'runtime.lock', wait=True):
            if runtime.read_state(root) != state or (folder / 'runtime.close').exists():
                continue
            proc = subprocess.Popen(args, cwd=root)
            record.update(child_pid=proc.pid, child_start=identity(proc.pid), status='running')
            atomic(path, record)
        last_activation = token
        previous = signal.signal(signal.SIGINT, signal.SIG_IGN)
        try:
            code = proc.wait()
        finally:
            signal.signal(signal.SIGINT, previous)
        record.pop('child_pid', None); record.pop('child_start', None)
        record.update(status='waiting', exit_code=code)
        atomic(path, record)
        command = 'relance_console.sh' if role == 'console' else 'relance_serveur.sh'
        print('\nApplication terminée (%s). Entrée ou %s pour relancer ; '
              'Ctrl-D pour fermer cet onglet. Reconnexion automatique au retour de ce worktree.' % (code, command))


def tab(root, role, generation):
    root, _, task = context(root, task=True)
    folder = directory(root)
    state = read_json(folder / 'atelier.json')
    if not state or state['generation'] != generation: raise ToolError('Atelier périmé.')
    record = {'pid': os.getpid(), 'start': identity(os.getpid()), 'generation': generation,
              'worktree': str(root), 'role': role, 'status': 'starting'}
    path = folder / (role + '.json'); atomic(path, record)
    env = os.environ.copy()
    try:
        if role in ('console', 'server'):
            runtime_tab(root, role, folder, record, path)
            return
        if role == 'codex':
            args = sessions.codex_command(root)
            env = sessions.codex_environment(args[0])
        else:
            args = vim_command(root, role, folder) if role == 'vim_doc' else None
            if args:
                env.update(WT_VIM_SESSION=str(root / ('vimsession_' + task + ('_doc' if role == 'vim_doc' else ''))),
                           WT_VIM_CLOSE=str(folder / (role + '.close')),
                           WT_VIM_REFUSED=str(folder / (role + '.refused')))
            else:
                print('Bash disponible. La fermeture proposera l’arrêt des commandes en cours.' if role == 'vim' else
                      'Aucune session Vim ; shell disponible. La fermeture proposera l’arrêt des commandes en cours.')
                args = ['bash', '--rcfile', str(TOOLS / 'worktree_bashrc.sh'), '-i']
                env['WT_SHELL_CLOSE'] = str(folder / (role + '.close'))
                record['shell_fallback'] = True
        proc = subprocess.Popen(args, cwd=root, env=env)
        record.update(child_pid=proc.pid, child_start=identity(proc.pid), status='running')
        atomic(path, record)
        # Ctrl-C est destiné à l'application attachée, pas au superviseur de l'onglet.
        previous = signal.signal(signal.SIGINT, signal.SIG_IGN)
        code = proc.wait()
        signal.signal(signal.SIGINT, previous)
        record.update(status='ready', exit_code=code); atomic(path, record)
        print('\nApplication terminée (%s). Cet onglet sera fermé par worktree_close.sh.' % code)
        while not (folder / 'close').exists(): time.sleep(.25)
    except Exception as exc:
        record.update(status='error', error=str(exc)); atomic(path, record)
        print(str(exc), file=sys.stderr)
        while not (folder / 'close').exists(): time.sleep(.25)
    finally:
        record['status'] = 'closed'; atomic(path, record)


def close_codex(root, folder, timeout=30):
    deadline = time.monotonic() + timeout
    while True:
        record = read_json(folder / 'codex.json')
        if not record:
            return
        if record.get('status') == 'starting' and alive(record):
            if time.monotonic() >= deadline:
                raise ToolError('Codex démarre encore ; fermeture interrompue, relance la commande.')
            time.sleep(.2)
            continue
        if not alive(record, 'child_'):
            return
        state = read_json(folder / 'atelier.json', {})
        if (not alive(record) or record.get('worktree') != str(root) or
                record.get('role') != 'codex' or not record.get('generation') or
                record.get('generation') != state.get('generation')):
            raise ToolError('Identité de la session Codex incohérente ; aucun signal envoyé.')
        pid = record['child_pid']
        # Un pidfd cible le processus ouvert, même si son PID est réutilisé
        # ensuite. Ne jamais envoyer de signal global ou tuer un groupe.
        try:
            descriptor = os.pidfd_open(pid)
        except ProcessLookupError:
            return
        try:
            if not alive(record, 'child_'):
                return
            status = Path('/proc/%s/status' % pid).read_text()
            parent = next(int(line.split()[1]) for line in status.splitlines() if line.startswith('PPid:'))
            if parent != record['pid'] or Path('/proc/%s/cwd' % pid).resolve() != root:
                raise ToolError('Le processus Codex ne correspond plus à cet atelier ; aucun signal envoyé.')
            print('Fermeture de la session Codex de %s…' % root.name)
            signal.pidfd_send_signal(descriptor, signal.SIGTERM)
            poller = select.poll()
            poller.register(descriptor, select.POLLIN)
            while time.monotonic() < deadline:
                if poller.poll(200):
                    return
            raise ToolError('Codex ne s’est pas arrêté après %s secondes ; fermeture interrompue, '
                            'aucun arrêt forcé. Vérifie son onglet puis relance la commande.' % timeout)
        except ProcessLookupError:
            return
        finally:
            os.close(descriptor)


def busy_shell_error(role, children):
    commands = []
    for child in children:
        try:
            name = Path('/proc/%s/comm' % child).read_text().strip()
        except (FileNotFoundError, ProcessLookupError):
            name = 'processus terminé'
        # Le nom suffit au diagnostic ; ne pas exposer les arguments privés.
        commands.append('%s (PID %s)' % (name, child))
    label = dict(ROLES).get(role, role)
    return ToolError('Une commande est active dans le shell %s : %s ; '
                     'termine-la dans cet onglet avant la fermeture.' % (label, ', '.join(commands)))


def stop_shell_commands(shell_pid, root, role, timeout=10):
    children = [pid for pid in shell_children(shell_pid) if pid != os.getpid()]
    if not children:
        return
    # Un Node peut encapsuler Codex : ne pas deduire son identite du nom,
    # et ne pas laisser son application enfant tourner apres sa fermeture.
    records, descriptors = [], {}
    shell_start = identity(shell_pid)
    def collect(pid, parent):
        if len(records) >= 128:
            raise ToolError('Trop de processus dans cet onglet ; fermeture manuelle nécessaire.')
        proc = Path('/proc/%s' % pid)
        try:
            start = identity(pid)
            if not start: return
            descriptor = os.pidfd_open(pid)
            descriptors[pid] = descriptor
            actual_parent = int(proc.joinpath('stat').read_text().rsplit(')', 1)[1].split()[1])
            cwd = proc.joinpath('cwd').resolve(strict=True)
            if identity(pid) != start or actual_parent != parent or (cwd != root and root not in cwd.parents):
                raise ToolError('Une commande ne correspond plus à cet onglet ou travaille ailleurs ; aucun arrêt effectué.')
            descendants = shell_children(pid)
            records.append({'pid': pid, 'start': start, 'parent': parent,
                            'cwd': cwd, 'children': descendants,
                            'cmdline': proc.joinpath('cmdline').read_bytes(),
                            'name': proc.joinpath('comm').read_text().strip()})
            for child in descendants:
                collect(child, pid)
        except (FileNotFoundError, ProcessLookupError):
            return
    try:
        for pid in children:
            collect(pid, shell_pid)
        if not records:
            return
        description = ', '.join('%s (PID %s)' % (r['name'], r['pid']) for r in records)
        if not ask(str(busy_shell_error(role, children)) + '\n'
                   'Arrêter proprement ces commandes et leurs enfants : %s ? '
                   'Le travail non enregistré peut être perdu.' % description):
            raise ToolError('Fermeture annulée ; les commandes restent actives.')
        # Revalider tout l'arbre apres le choix, avant le premier signal.
        # Les pidfd evitent de viser un autre processus si un PID est reutilise.
        if identity(shell_pid) != shell_start:
            raise ToolError('Le shell a changé ; aucun arrêt effectué.')
        current = [pid for pid in shell_children(shell_pid) if pid != os.getpid()]
        if set(current) != set(pid for pid in children if identity(pid)):
            raise ToolError('Les commandes du shell ont changé ; relance la fermeture.')
        for record in records:
            if not alive(record):
                if identity(record['pid']):
                    raise ToolError('Un processus a changé ; relance la fermeture.')
                continue
            proc = Path('/proc/%s' % record['pid'])
            parent = int(proc.joinpath('stat').read_text().rsplit(')', 1)[1].split()[1])
            if (parent != record['parent'] or proc.joinpath('cwd').resolve(strict=True) != record['cwd'] or
                    proc.joinpath('cmdline').read_bytes() != record['cmdline'] or
                    set(shell_children(record['pid'])) - set(record['children'])):
                raise ToolError('Une commande a changé ; relance la fermeture.')
        print('Arrêt propre des commandes du shell %s…' % dict(ROLES).get(role, role), flush=True)
        for record in reversed(records):
            if alive(record):
                try:
                    signal.pidfd_send_signal(descriptors[record['pid']], signal.SIGTERM)
                except ProcessLookupError:
                    continue
        poller = select.poll()
        for descriptor in descriptors.values(): poller.register(descriptor, select.POLLIN)
        pending = set(descriptors.values())
        deadline = time.monotonic() + timeout
        while pending and time.monotonic() < deadline:
            for descriptor, _ in poller.poll(100):
                pending.discard(descriptor); poller.unregister(descriptor)
        if pending:
            raise ToolError('Une commande ne s’est pas arrêtée après %s secondes ; '
                            'fermeture interrompue, aucun arrêt forcé.' % timeout)
        # Attendre aussi que Bash recolte les enfants termines.
        reap_deadline = min(deadline, time.monotonic() + 1)
        while shell_children(shell_pid) and time.monotonic() < reap_deadline:
            if shell_children(shell_pid) == [os.getpid()]: return
            time.sleep(.02)
    finally:
        for descriptor in descriptors.values(): os.close(descriptor)


def close_idle_shell(record, folder, role):
    pid = record['child_pid']
    if not alive(record, 'child_'):
        return
    children_path = Path('/proc/%s/task/%s/children' % (pid, pid))
    children = shell_children(pid)
    if children:
        raise busy_shell_error(role, children)
    # Bash peut différer USR1 pendant readline. INT, envoyé au seul shell
    # après une nouvelle vérification, réveille readline et exécute le trap.
    # Le pidfd évite de toucher un autre processus en cas de réutilisation du PID.
    try:
        descriptor = os.pidfd_open(pid)
    except ProcessLookupError:
        return
    marker = folder / (role + '.close')
    try:
        if not alive(record, 'child_'):
            return
        marker.touch()
        signal.pidfd_send_signal(descriptor, signal.SIGUSR1)
        time.sleep(.1)
        if not alive(record, 'child_'):
            return
        if children_path.read_text().strip():
            marker.unlink(missing_ok=True)
            raise ToolError('Une commande a démarré dans le shell %s ; fermeture suspendue.' % role)
        signal.pidfd_send_signal(descriptor, signal.SIGINT)
    except ProcessLookupError:
        return
    finally:
        os.close(descriptor)


def shell_children(pid):
    return [int(child) for child in Path('/proc/%s/task/%s/children' % (pid, pid)).read_text().split()]


def stop_log_followers(shell_pid, root, timeout=3):
    # Seul le client de lecture des logs est interrompu, jamais le conteneur
    # ni un groupe de processus. Les autres commandes restent bloquantes.
    for pid in shell_children(shell_pid):
        if pid == os.getpid():
            continue
        descriptor = None
        try:
            descriptor = os.pidfd_open(pid)
            proc = Path('/proc/%s' % pid)
            args = proc.joinpath('cmdline').read_bytes().split(b'\0')[:-1]
            if (len(args) < 4 or Path(os.fsdecode(args[0])).name != 'docker' or
                    args[1] != b'logs' or not any(a in (b'-f', b'--follow', b'--follow=true') for a in args[2:])):
                continue
            parent = int(proc.joinpath('stat').read_text().rsplit(')', 1)[1].split()[1])
            cwd = proc.joinpath('cwd').resolve(strict=True)
            if parent != shell_pid or (cwd != root and root not in cwd.parents):
                continue
            # Relire avant le signal pour refuser une commande qui aurait change.
            if proc.joinpath('cmdline').read_bytes().split(b'\0')[:-1] != args:
                raise ToolError('Le suivi de logs a changé ; fermeture interrompue.')
            print('Arrêt du suivi des logs Docker dans cet onglet (PID %s).' % pid, flush=True)
            signal.pidfd_send_signal(descriptor, signal.SIGINT)
            poller = select.poll()
            poller.register(descriptor, select.POLLIN)
            if not poller.poll(timeout * 1000):
                raise ToolError('Le suivi de logs Docker ne s’est pas arrêté ; '
                                'termine-le dans cet onglet avant de relancer.')
            # Le shell doit recolter son enfant avant le controle des commandes.
            deadline = time.monotonic() + 1
            while pid in shell_children(shell_pid) and time.monotonic() < deadline:
                time.sleep(.02)
        except (FileNotFoundError, ProcessLookupError):
            # Le client peut terminer entre l'inventaire et l'ouverture du pidfd.
            continue
        finally:
            if descriptor is not None:
                os.close(descriptor)


def extra_terminal_shells(root):
    records = []
    for proc in Path('/proc').iterdir():
        if not proc.name.isdigit(): continue
        try:
            pid = int(proc.name)
            start = identity(pid)
            if (proc / 'comm').read_text().strip() not in ('bash', 'zsh', 'fish', 'sh', 'dash'): continue
            if (proc / 'cwd').resolve(strict=True) != root: continue
            parent = int((proc / 'stat').read_text().rsplit(')', 1)[1].split()[1])
            # Seulement les shells directement lancés par GNOME : exclure les
            # shells imbriqués et ceux des onglets supervisés.
            if Path('/proc/%s/exe' % parent).resolve(strict=True).name != 'gnome-terminal-server': continue
            record = {'pid': pid, 'start': start}
            if not alive(record): continue
            stop_log_followers(pid, root)
            children = shell_children(pid)
            if any(child != os.getpid() for child in children):
                raise ToolError('Une commande est active dans le terminal supplémentaire %s ; termine-la avant la fermeture.' % pid)
            records.append(record)
        except (FileNotFoundError, ProcessLookupError, PermissionError):
            continue
    return records


def close_extra_shell(record, root):
    if not alive(record): return
    pid = record['pid']
    try:
        descriptor = os.pidfd_open(pid)
        try:
            if not alive(record) or Path('/proc/%s/cwd' % pid).resolve() != root: return
            children = shell_children(pid)
            if any(child != os.getpid() for child in children):
                raise ToolError('Une commande a démarré dans le terminal %s ; sa fermeture est refusée.' % pid)
            # Le shell appelant peut propager HUP à cette commande. Tout le
            # travail est terminé ; laisser la commande achever sa sortie.
            if os.getpid() in children:
                signal.signal(signal.SIGHUP, signal.SIG_IGN)
            sys.stdout.flush(); sys.stderr.flush()
            signal.pidfd_send_signal(descriptor, signal.SIGHUP)
        finally:
            os.close(descriptor)
    except ProcessLookupError:
        return


def close_all():
    # Même détection que worktree_status : un fichier d'état ancien ne suffit pas.
    targets = [Path(row['worktree']) for row in worktrees()
               if Path(row['worktree']) != MAIN and open_roles(Path(row['worktree']))]
    if not targets:
        print('Aucun atelier de worktree actuellement ouvert.')
        return
    print('Fermeture de %s atelier(s) : %s.' % (len(targets), ', '.join(root.name for root in targets)), flush=True)
    for index, root in enumerate(targets):
        try:
            close(root)
        except ToolError as error:
            # Une annulation ou un refus doit interrompre aussi la fermeture collective.
            raise ToolError('Fermeture collective interrompue sur %s (%s/%s atelier(s) fermé(s)) : %s' %
                            (root.name, index, len(targets), error)) from error
    print('%s atelier(s) fermé(s) ; worktrees et fichiers conservés.' % len(targets))


def close(root):
    root, branch, task = context(root, task=True)
    folder = directory(root)
    with locked(folder / 'atelier.lock'):
        extra_shells = extra_terminal_shells(root)
        caller_roles = set()
        # Le shell qui attend cette commande doit rester vivant jusqu'à la
        # sauvegarde. Les autres tâches, même suspendues, restent bloquantes.
        for role in ('vim', 'vim_doc'):
            record = read_json(folder / (role + '.json'))
            if alive(record) and alive(record, 'child_') and record.get('shell_fallback'):
                stop_log_followers(record['child_pid'], root)
                children = shell_children(record['child_pid'])
                blockers = [child for child in children if child != os.getpid()]
                if blockers:
                    stop_shell_commands(record['child_pid'], root, role)
                    children = shell_children(record['child_pid'])
                    blockers = [child for child in children if child != os.getpid()]
                    if blockers: raise busy_shell_error(role, blockers)
                if os.getpid() in children:
                    caller_roles.add(role)
                    extra_shells.append({'pid': record['child_pid'], 'start': record['child_start']})
        journal = MAIN / setting('JOURNAL_DIR', 'sessions_codex') / (task + '.jsont')
        with locked(str(journal) + '.lock'):
            data = read_json(journal)
            if not data or data.get('branch') != branch or not isinstance(data.get('messages'), list):
                raise ToolError('Journal central absent ou invalide : fermeture interrompue.')
        close_codex(root, folder)
        for role in ('vim', 'vim_doc'):
            if role in caller_roles:
                continue
            record = read_json(folder / (role + '.json'))
            if alive(record) and alive(record, 'child_'):
                if record.get('shell_fallback'):
                    close_idle_shell(record, folder, role)
                    continue
                (folder / (role + '.close')).touch()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if all(not alive(read_json(folder / (r + '.json')), 'child_')
                   for r in ('vim', 'vim_doc') if r not in caller_roles): break
            time.sleep(.2)
        for role in ('vim', 'vim_doc'):
            if role in caller_roles:
                continue
            record = read_json(folder / (role + '.json'))
            if alive(record) and alive(record, 'child_'):
                if record.get('shell_fallback'):
                    raise ToolError('Le shell de secours %s reste ouvert ; vérifie cet onglet puis relance.' % role)
                refused = folder / (role + '.refused')
                if refused.exists():
                    raise ToolError('Vim %s refuse la fermeture : %s' % (role, refused.read_text().strip()))
                raise ToolError('Vim %s reste ouvert : enregistre les buffers (:wall) ou termine l’opération, puis relance.' % role)
        # Empêcher les onglets en attente de se reconnecter pendant la fermeture.
        (folder / 'runtime.close').touch()
        mounts = runtime.app_mounts(True)
        if (any(p == root for _, p, _, _ in mounts) or
                runtime.read_state(root).get('status') in ('active', 'activating')):
            runtime.stop(root)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if all(not alive(read_json(folder / (r + '.json')), 'child_') for r in ('console', 'server')): break
            time.sleep(.2)
        if any(alive(read_json(folder / (r + '.json')), 'child_') for r in ('console', 'server')):
            raise ToolError('Un attach est encore actif ; vérifie-le avant de fermer.')
        patches.make(root)
        (folder / 'close').touch()
        state = read_json(folder / 'atelier.json', {'worktree': str(root)})
        state.update(status='closed', updated_at=now()); atomic(folder / 'atelier.json', state)
        print('Atelier fermé ; worktree, fichiers et journal conservés : %s' % root)
    # Le verrou doit être libéré avant la sortie du terminal appelant.
    for record in extra_shells:
        close_extra_shell(record, root)
