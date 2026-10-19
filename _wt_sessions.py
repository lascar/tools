"""Préparation locale et reprise de Codex sans changer de worktree."""
import os
from pathlib import Path
import shutil
from _wt_common import *


def link_or_check(source, target):
    source, target = Path(source).resolve(), Path(target)
    if target.is_symlink():
        if target.resolve() == source: return
        raise ToolError('Lien local différent, aucun écrasement : %s' % target)
    if target.exists():
        if source.is_file() and target.is_file() and source.read_bytes() == target.read_bytes(): return
        raise ToolError('Fichier local déjà présent, à examiner : %s' % target)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.symlink_to(source, target_is_directory=source.is_dir())


def shared_tmp(root):
    root, _, _ = context(root, task=True)
    source, target = MAIN / setting('SHARED_TEMP_DIR', 'tmp'), root / setting('SHARED_TEMP_DIR', 'tmp')
    with locked(STATE / 'tmp.lock', wait=True):
        source.mkdir(exist_ok=True)
        relative = os.path.relpath(source, root)
        if target.is_symlink() and os.readlink(target) == relative:
            return
        if git(root, 'ls-files', '--', setting('SHARED_TEMP_DIR', 'tmp')).stdout:
            raise ToolError('tmp contient des fichiers suivis par Git ; remplacement refusé : %s' % target)
        # Le workflow autorise le remplacement du contenu temporaire. Ne jamais
        # suivre un ancien lien : seule son entrée est remplacée.
        if target.is_symlink() or (target.exists() and not target.is_dir()):
            target.unlink()
        elif target.is_dir():
            if os.path.ismount(target): raise ToolError('tmp est un point de montage : %s' % target)
            try:
                shutil.rmtree(target)
            except PermissionError:
                # Les fichiers générés par Docker peuvent appartenir à nobody.
                # Le conteneur de nettoyage ne monte que ce tmp et ne télécharge rien.
                import _wt_docker as runtime
                config = json.loads(run(runtime.base() + ['config', '--format', 'json'], capture=True).stdout)
                image = config['services'][SHELL_SERVICE]['image']
                if target.is_symlink() or not target.is_dir():
                    raise ToolError('tmp a changé pendant son nettoyage : %s' % target)
                print('Nettoyage du tmp Docker : %s' % target, flush=True)
                run(['docker', 'run', '--rm', '--pull=never', '--network=none', '--user=0',
                     '--entrypoint=/bin/sh', '--mount', 'type=bind,source=%s,target=/worktree-tmp' % target,
                     image, '-c', 'find /worktree-tmp -mindepth 1 -maxdepth 1 -exec rm -rf -- {} +'])
                target.rmdir()
        target.symlink_to(relative, target_is_directory=True)
        print('tmp partagé : %s -> %s' % (target, relative))


def tmp_mount(root):
    if root == MAIN: return []
    shared_tmp(root)
    # Le montage applicatif contient le worktree : le lien relatif mène à ce chemin
    # dans le conteneur, où le tmp principal doit donc aussi être monté.
    destination = os.path.normpath(APP_PATH + '/' + os.readlink(root / setting('SHARED_TEMP_DIR', 'tmp')))
    return [{'type': 'bind', 'source': str(MAIN / setting('SHARED_TEMP_DIR', 'tmp')), 'target': destination,
             'bind': {'create_host_path': False}}]


def shared_documents(root):
    root, _, _ = context(root, task=True)
    source = MAIN / setting('DOCUMENTS_DIR', 'doc')
    target = root / setting('DOCUMENTS_DIR', 'doc')
    with locked(STATE / 'documents.lock', wait=True):
        source.mkdir(parents=True, exist_ok=True)
        # Une règle doc/ ignore un dossier, mais pas le lien qui le remplace.
        exclude = Path(value(root, 'rev-parse', '--path-format=absolute', '--git-path', 'info/exclude'))
        pattern = '/' + setting('DOCUMENTS_DIR', 'doc').strip('/')
        text = exclude.read_text() if exclude.exists() else ''
        if pattern not in text.splitlines():
            exclude.parent.mkdir(parents=True, exist_ok=True)
            with exclude.open('a') as stream:
                stream.write(('\n' if text and not text.endswith('\n') else '') + pattern + '\n')
        if target.is_symlink():
            link_or_check(source, target)
            return
        if git(root, 'ls-files', '--', setting('DOCUMENTS_DIR', 'doc')).stdout:
            raise ToolError('Documents suivis par Git ; partage refusé : %s' % target)
        if target.exists():
            if not target.is_dir() or os.path.ismount(target):
                raise ToolError('Dossier de documents local à examiner : %s' % target)
            # Vérifier tous les conflits avant toute copie. Les liens existants
            # sont comparés par cible sans parcourir leurs répertoires.
            missing, newer = [], []
            for folder, dirs, files in os.walk(target, followlinks=False):
                for name in list(dirs) + files:
                    local = Path(folder) / name
                    dest = source / local.relative_to(target)
                    if local.is_symlink():
                        if name in dirs: dirs.remove(name)
                        same = dest.resolve() == local.resolve()
                    elif local.is_dir():
                        same = dest.is_dir() and not dest.is_symlink()
                    else:
                        same = dest.is_file() and not dest.is_symlink() and dest.read_bytes() == local.read_bytes()
                    if dest.exists() or dest.is_symlink():
                        if not same:
                            if (local.is_file() and not local.is_symlink() and
                                    dest.is_file() and not dest.is_symlink() and
                                    local.stat().st_mtime_ns != dest.stat().st_mtime_ns):
                                if local.stat().st_mtime_ns > dest.stat().st_mtime_ns:
                                    newer.append((local, dest))
                            else:
                                raise ToolError('Documents différents, migration à décider : %s et %s' % (local, dest))
                    else:
                        missing.append((local, dest))
            archive = source / 'worktree-doc-backups' / root.name
            archive.mkdir(parents=True, exist_ok=True)
            backup = Path(tempfile.mkdtemp(prefix='doc-', dir=archive))
            # La version la plus récente devient commune. Conserver aussi
            # l'ancienne version centrale avant de la remplacer.
            for local, dest in newer:
                previous = backup / 'central' / local.relative_to(target)
                previous.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(dest, previous)
                shutil.copy2(local, dest)
                print('Version locale plus récente conservée : %s' % dest)
            for local, dest in missing:
                if local.is_symlink():
                    dest.symlink_to(local.resolve(), target_is_directory=local.is_dir())
                elif local.is_dir():
                    dest.mkdir(exist_ok=True)
                else:
                    shutil.copy2(local, dest)
            # Conserver le dossier original complet avant de le remplacer.
            backup = backup / 'doc'
            target.rename(backup)
            print('Documents locaux sauvegardés : %s' % backup)
        target.symlink_to(source, target_is_directory=True)


def prepare(root, new=False):
    root, branch, task = context(root, task=True)
    shared_documents(root)
    if (MAIN / setting('DOCUMENTS_DIR', 'doc') / 'tools').resolve() != TOOLS:
        link_or_check(TOOLS, MAIN / setting('DOCUMENTS_DIR', 'doc') / 'tools')
    for profile in setting_json('PROFILE_FILES', []):
        link_or_check(MAIN / profile, root / profile)
    override = root / 'AGENTS.override.md'
    instructions = setting_json('PROFILE_OVERRIDE', '')
    if not override.exists(): override.write_text(instructions)
    elif override.read_text() != instructions:
        print('Profil override existant conservé, vérifie les instructions configurées : %s.' % override)
    # Les sessions reprises peuvent conserver l'ancienne commande relative
    # .codex/hooks/session_journal.py : fournir aussi cette entrée, sans copie
    # du journal et sans écraser un éventuel hook personnalisé.
    link_or_check(TOOLS / 'session_journal.py', root / '.codex' / 'hooks' / 'session_journal.py')
    # Le hook projet est indépendant du cwd de son installation.
    hooks = root / '.codex' / 'hooks.json'
    if not hooks.exists():
        hooks.parent.mkdir(exist_ok=True)
        import shlex
        command = 'python3 ' + shlex.quote(str(TOOLS / 'session_journal.py'))
        atomic(hooks, {'hooks': {e: [{'hooks': [{'type': 'command', 'command': command, 'timeout': 10}]}]
                               for e in ('SessionStart', 'UserPromptSubmit', 'Stop')}})
    else:
        print('Hooks existants conservés : %s' % hooks)
    # .env contient des paramètres locaux ; copie privée initiale, jamais d’écrasement.
    for relative in setting_json('COPY_FILES', []):
        source, dest = MAIN / relative, root / relative
        if source.is_file() and not dest.exists():
            shutil.copy2(source, dest); dest.chmod(0o600)
    for suffix in ('', '_doc'):
        source = MAIN / ('vimsession_' + task + suffix)
        dest = root / source.name
        if source.is_file() and not dest.exists():
            # Les anciennes sessions contiennent des chemins absolus vers master.
            text = source.read_text()
            for old in (str(MAIN), *setting_json('LEGACY_ROOT_ALIASES', [])):
                text = text.replace(old + '/', str(root) + '/').replace('cd ' + old + '\n', 'cd ' + str(root) + '\n')
            dest.write_text(text)
    if new:
        for suffix in ('.txt', '_suivi.txt'):
            dest = root / setting('DOCUMENTS_DIR', 'doc') / (task + suffix)
            if not dest.exists():
                template = MAIN / setting('NOTES_TEMPLATE', 'doc/notes-template.txt')
                text = template.read_text() if suffix == '_suivi.txt' and template.exists() else ''
                dest.write_text(text.replace('<nom de la tâche>', task).replace('<num de la tâche>', task.removeprefix('t_').split('_')[0]))
    journal = MAIN / setting('JOURNAL_DIR', 'sessions_codex') / (task + '.jsont')
    local = root / setting('JOURNAL_DIR', 'sessions_codex') / journal.name
    if local.exists():
        raise ToolError('Journal local concurrent à examiner : %s' % local)
    with locked(str(journal) + '.lock', wait=True):
        if not journal.exists(): atomic(journal, {'branch': branch, 'created_at': now(), 'updated_at': now(), 'messages': []})
        elif read_json(journal).get('branch') != branch:
            raise ToolError('Journal central associé à une autre branche.')
    shared_tmp(root)


def metadata(path):
    with Path(path).open() as stream:
        for line in stream:
            data = json.loads(line)
            if data.get('type') == 'session_meta': return data['payload']
    raise ToolError('Transcript sans métadonnées : %s' % path)


def codex_binary():
    """Résoudre à chaque lancement ; les chemins NVM changent avec Node."""
    explicit = os.environ.get('CODEX_BIN')
    binary = shutil.which(explicit or 'codex')
    if binary:
        return os.path.abspath(binary)
    if explicit:
        raise ToolError('CODEX_BIN ne désigne pas un exécutable accessible : ' + explicit)
    # GNOME peut hériter d'un PATH antérieur au chargement de NVM. Interroger
    # le shell interactif recharge la configuration personnelle sans figer Node.
    import subprocess
    result = subprocess.run(['bash', '-ic', 'type -P codex'], capture_output=True,
                            text=True, timeout=15)
    candidate = result.stdout.strip().splitlines()[-1] if result.stdout.strip() else ''
    if result.returncode == 0 and Path(candidate).is_absolute() and shutil.which(candidate):
        return candidate
    raise ToolError('Codex CLI introuvable dans le PATH, même après chargement de Bash. '
                    'Vérifie which codex dans ton terminal ou définis CODEX_BIN.')


def codex_environment(binary):
    env = os.environ.copy()
    # Garder le dossier bin de NVM (pas la cible node_modules du lien codex) :
    # le shebang /usr/bin/env node doit trouver la version Node correspondante.
    env['PATH'] = str(Path(binary).absolute().parent) + os.pathsep + env.get('PATH', '')
    return env


def codex_command(root, extra=(), new=False):
    root, branch, task = context(root, task=True)
    binary = codex_binary()
    if any(a in ('-C', '--cd', '--worktree') or a.startswith(('--cd=', '-C')) for a in extra):
        raise ToolError('Le répertoire Codex est fixé au worktree ; option --cd interdite ici.')
    alias = MAIN / setting('JOURNAL_DIR', 'sessions_codex') / (task + '.jsonl')
    args = [binary]
    if not new and (alias.exists() or alias.is_symlink()):
        if not alias.exists(): raise ToolError('Lien de reprise cassé ; utilise --new-session ou répare le lien.')
        meta = metadata(alias)
        old_root = Path(meta.get('cwd', '')).resolve()
        if old_root != root and not (old_root == MAIN and meta.get('git', {}).get('branch') == branch):
            raise ToolError('Transcript d’un autre dossier/tâche ; utilise --new-session après vérification.')
        session_id = meta.get('session_id') or meta.get('id')
        if not session_id: raise ToolError('Identifiant de session absent.')
        args += ['resume', session_id]
    args += ['-C', str(root), '--add-dir', str(MAIN / setting('JOURNAL_DIR', 'sessions_codex')),
             '--add-dir', str(MAIN / setting('DOCUMENTS_DIR', 'doc')), *extra]
    args += [setting('AGENT_INSTRUCTIONS', 'Lis AGENTS.md.') + ' Reprends le journal central ' +
             str(MAIN / setting('JOURNAL_DIR', 'sessions_codex') / (task + '.jsont')) +
             ' et tiens-le à jour. Écris les documents exclusivement dans ' +
             str(MAIN / setting('DOCUMENTS_DIR', 'doc')) +
             ' ; doc dans le worktree doit pointer vers ce dossier central. Le code et Git restent dans ce worktree.']
    return args
