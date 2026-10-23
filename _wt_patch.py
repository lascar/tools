"""Sauvegardes vérifiables : index, fichiers suivis et nouveaux fichiers."""
import hashlib
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import zipfile
import json
from _wt_common import *



def directory():
    """Emplacement unique des patchs, commun à toutes les tâches."""
    return MAIN / 'doc'


def patch_path(path):
    path = Path(path).expanduser()
    # Conserver la commande « restore doc/patch... » depuis un worktree.
    if not path.is_absolute() and len(path.parts) == 2 and path.parts[0] == 'doc':
        path = directory() / path.name
    path = path.resolve()
    if path.parent != directory().resolve():
        raise ToolError('Les patchs doivent se trouver dans %s.' % directory())
    return path


def names(root, *args):
    return [s for s in git(root, *args).stdout.split('\0') if s]


def changed(root):
    return sorted(set(names(root, 'diff', '--no-renames', '--name-only', '-z') +
                      names(root, 'diff', '--cached', '--no-renames', '--name-only', '-z') +
                      names(root, 'ls-files', '--others', '--exclude-standard', '-z')))


def file_record(root, name):
    p = root / safe_relative(name)
    # Ne jamais suivre un répertoire symbolique lors d'une sauvegarde/restauration.
    for parent in p.parents:
        if parent == root:
            break
        if parent.is_symlink():
            raise ToolError('Parent symbolique refusé : %s' % parent)
    if p.is_symlink():
        data = os.fsencode(os.readlink(p)); kind = 'symlink'; mode = 0o777
    elif p.is_file():
        data = p.read_bytes(); kind = 'file'; mode = stat.S_IMODE(p.stat().st_mode)
    elif not p.exists():
        data = b''; kind = 'missing'; mode = 0
    else:
        raise ToolError('Type de fichier non sauvegardable : %s' % p)
    return {'path': name, 'kind': kind, 'mode': mode,
            'sha256': hashlib.sha256(data).hexdigest()}, data


def diff_bytes(root, cached=False):
    return subprocess.check_output(['git', '-C', str(root), 'diff', '--binary',
                                    '--full-index', '--no-ext-diff', '--no-textconv'] +
                                   (['--cached'] if cached else []))


def make(root, extra=()):
    root, branch, task = context(root, task=True)
    paths = [x for x in changed(root) if not personal(x)]
    for name in extra:
        safe_relative(name)
        if name not in paths:
            paths.append(name)
    if not paths:
        print('Aucun changement à sauvegarder.'); return None
    # Les secrets ignorés ne sont jamais découverts implicitement.
    with locked(directory() / '.patch.lock'):
        head = value(root, 'rev-parse', 'HEAD')
        initial_paths = changed(root)
        staged, unstaged = diff_bytes(root, True), diff_bytes(root)
        tracked_changes = names(root, 'diff', '--cached', '--no-renames', '--name-only', '-z') + names(root, 'diff', '--no-renames', '--name-only', '-z')
        if any(personal(x) for x in tracked_changes):
            raise ToolError('Fichiers personnels suivis/modifiés : traite-les séparément avant la sauvegarde.')
        records, blobs = [], []
        for name in sorted(paths):
            record, data = file_record(root, name)
            if git(root, 'ls-files', '--stage', '--', name).stdout.startswith('160000'):
                raise ToolError('Sous-module modifié : sauvegarde dédiée nécessaire : ' + name)
            records.append(record); blobs.append(data)
        stamp = datetime.datetime.now().strftime('%Y%m%d-%H%M%S-%f')
        target = directory() / ('patch_' + task + '_' + stamp + '.wtpatch.zip')
        temp = target.with_suffix('.tmp')
        manifest = {'version': 1, 'branch': branch, 'head': head, 'worktree': str(root),
                    'created_at': now(), 'files': records,
                    'index_sha256': hashlib.sha256(staged).hexdigest()}
        try:
            with zipfile.ZipFile(temp, 'w', zipfile.ZIP_DEFLATED) as z:
                z.writestr('manifest.json', json.dumps(manifest, ensure_ascii=False))
                z.writestr('index.patch', staged); z.writestr('working.patch', unstaged)
                for i, data in enumerate(blobs):
                    z.writestr('files/%d' % i, data)
            with zipfile.ZipFile(temp) as z:
                if z.testzip():
                    raise ToolError('Archive de sauvegarde corrompue.')
            # Refuser un instantané incohérent si un éditeur a écrit pendant la lecture.
            if changed(root) != initial_paths or value(root, 'rev-parse', 'HEAD') != head or diff_bytes(root, True) != staged or diff_bytes(root) != unstaged:
                raise ToolError('Git a changé pendant la sauvegarde ; recommence après arrêt des écritures.')
            for record in records:
                if file_record(root, record['path'])[0] != record:
                    raise ToolError('Fichier modifié pendant la sauvegarde : ' + record['path'])
            os.replace(temp, target)
        finally:
            if temp.exists(): temp.unlink()
        print('Sauvegarde vérifiée : %s' % target)
        return target


def inspect_archive(path):
    with zipfile.ZipFile(path) as z:
        m = json.loads(z.read('manifest.json'))
        if m.get('version') != 1:
            raise ToolError('Version de sauvegarde inconnue.')
        index = z.read('index.patch')
        if hashlib.sha256(index).hexdigest() != m['index_sha256']:
            raise ToolError('Index de sauvegarde corrompu.')
        blobs = []
        for i, f in enumerate(m['files']):
            safe_relative(f['path'])
            if f['kind'] not in ('file', 'symlink', 'missing'):
                raise ToolError('Type d’entrée invalide.')
            data = z.read('files/%d' % i)
            if hashlib.sha256(data).hexdigest() != f['sha256']:
                raise ToolError('Contenu de sauvegarde corrompu : ' + f['path'])
            blobs.append(data)
    return m, index, blobs


def already_present(root, m, index):
    return (value(root, 'rev-parse', 'HEAD') == m['head'] and
            diff_bytes(root, True) == index and
            all(file_record(root, f['path'])[0] == f for f in m['files']))



def check_restore_worktree(root, manifest):
    # checkout . laisse les fichiers non suivis sur place. Ils sont compatibles
    # uniquement si leur contenu, type et mode correspondent a la sauvegarde.
    local = changed(root)
    if not local:
        return
    untracked = set(names(root, 'ls-files', '--others', '--exclude-standard', '-z'))
    saved = {record['path']: record for record in manifest['files']}
    for name in local:
        if name not in untracked or name not in saved or file_record(root, name)[0] != saved[name]:
            raise ToolError('Worktree non propre : changement local non identique à la sauvegarde : %s.' % name)


def restore(root, path, yes=False):
    root, branch, task = context(root, task=True)
    path = patch_path(path)
    if path.name.endswith('.done'):
        raise ToolError('Cette sauvegarde est déjà marquée .done.')
    if not path.name.endswith('.wtpatch.zip'):
        return restore_legacy(root, path, yes)
    m, index, blobs = inspect_archive(path)
    if m['branch'] != branch:
        raise ToolError('La sauvegarde appartient à une autre branche.')
    if already_present(root, m, index):
        print('Travail déjà présent : aucune réapplication.'); return
    if value(root, 'rev-parse', 'HEAD') != m['head']:
        raise ToolError('Commit de base différent : restauration manuelle requise, aucune fusion automatique.')
    check_restore_worktree(root, m)
    conflicts = collisions(root, [f['path'] for f in m['files']])
    if conflicts:
        raise ToolError('Collisions avec d’autres worktrees :\n' + '\n'.join(conflicts))
    for f in m['files']:
        file_record(root, f['path'])
        p = root / f['path']
        if git(root, 'check-ignore', '-q', '--', f['path'], check=False).returncode == 0 and (p.exists() or p.is_symlink()):
            raise ToolError('Fichier ignoré existant : restauration manuelle requise : ' + f['path'])
    if not yes and not ask('Restaurer %s ?' % path): return
    with locked(directory() / '.patch.lock'):
        if context(root, task=True)[1] != m['branch'] or value(root, 'rev-parse', 'HEAD') != m['head']:
            raise ToolError('La branche ou le commit a changé avant la restauration.')
        check_restore_worktree(root, m)
        if index:
            run(['git', '-C', root, 'apply', '--cached', '--check', '-'], input=index.decode('utf-8', 'surrogateescape'))
            run(['git', '-C', root, 'apply', '--cached', '-'], input=index.decode('utf-8', 'surrogateescape'))
        for f, data in zip(m['files'], blobs):
            p = root / f['path']; p.parent.mkdir(parents=True, exist_ok=True)
            if p.is_symlink() or (f['kind'] == 'missing' and p.exists()): p.unlink()
            if f['kind'] == 'symlink':
                if p.exists(): p.unlink()
                p.symlink_to(os.fsdecode(data))
            elif f['kind'] == 'file':
                p.write_bytes(data); p.chmod(f['mode'])
        if not already_present(root, m, index):
            raise ToolError('Vérification après restauration échouée ; sauvegarde conservée sans .done.')
        path.rename(Path(str(path) + '.done'))
        print('Restauration vérifiée ; sauvegarde marquée .done.')


def restore_legacy(root, path, yes):
    path = patch_path(path)
    task = context(root, task=True)[2]
    if not path.name.startswith(('patch_' + task + '_', 'patch_auggie_' + task + '_')):
        raise ToolError('Le nom du patch ne correspond pas à la tâche.')
    if changed(root): raise ToolError('Worktree non propre : restauration manuelle du patch ancien requise.')
    if git(root, 'apply', '--reverse', '--check', str(path), check=False).returncode == 0:
        print('Patch ancien déjà présent.'); return
    # git apply vérifie tous les chemins et refuse les traversées hors du worktree.
    git(root, 'apply', '--check', str(path))
    entries = git(root, 'apply', '--numstat', '-z', str(path)).stdout.split('\0')
    paths = [entry.split('\t', 2)[2] for entry in entries if entry and len(entry.split('\t', 2)) == 3]
    if any(personal(p) for p in paths): raise ToolError('Patch ancien visant des fichiers personnels : revue manuelle requise.')
    conflicts = collisions(root, paths)
    if conflicts: raise ToolError('Collisions avec d’autres worktrees :\n' + '\n'.join(conflicts))
    if not yes and not ask('Appliquer le patch ancien %s ?' % path): return
    git(root, 'apply', str(path))
    git(root, 'apply', '--reverse', '--check', str(path))
    path.rename(Path(str(path) + '.done'))


def pending(root=None, done=False, task=None):
    if task is None:
        task = context(root, task=True)[2]
    else:
        # Les sauvegardes centrales restent consultables sans branche ni worktree.
        task = task_branch(task).split('/', 1)[1]
    folder = directory()
    if not folder.exists():
        return []
    result = []
    for path in folder.iterdir():
        if (path.is_file() and not path.is_symlink() and
                path.name.startswith(('patch_' + task + '_', 'patch_auggie_' + task + '_')) and
                (path.name.endswith('.done') if done else path.name.endswith(('.txt', '.wtpatch.zip')))):
            result.append(path)
    return sorted(result, key=lambda path: path.stat().st_mtime, reverse=True)


def offer(root):
    for p in pending(root):
        # Les anciens .txt n’enregistrent pas la création : annoncer leur
        # dernière modification, sans la présenter comme une date de sauvegarde.
        date = datetime.datetime.fromtimestamp(p.stat().st_mtime).astimezone()
        label = 'Dernière modification du fichier'
        if p.name.endswith('.wtpatch.zip'):
            m, index, _ = inspect_archive(p)
            # Une actualisation peut changer HEAD avant la proposition des patchs.
            # Conserver ces archives sans proposer une restauration impossible.
            if m['branch'] != value(root, 'branch', '--show-current'):
                print('Sauvegarde conservée, non proposée automatiquement (autre branche) : %s' % p)
                continue
            if m['head'] != value(root, 'rev-parse', 'HEAD'):
                print('Sauvegarde conservée, non proposée automatiquement (commit de base différent) : %s' % p)
                continue
            if already_present(root, m, index):
                print('Sauvegarde déjà présente : %s' % p); continue
            if m.get('created_at'):
                date = datetime.datetime.fromisoformat(m['created_at']).astimezone()
                label = 'Sauvegarde créée le'
        print('Sauvegarde disponible : %s' % p)
        print('%s : %s' % (label, date.strftime('%d/%m/%Y à %H:%M:%S %Z (%z)')))
        if os.isatty(0) and ask('Examiner et restaurer cette sauvegarde ?'):
            restore(root, p, yes=True)
            break
