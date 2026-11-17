"""Contexte et primitives des outils personnels (Python 3, indépendants du projet)."""
import contextlib
import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time

from _wt_config import *
MAIN = configured_path('MAIN_ROOT')
STATE = MAIN / setting('STATE_DIR', 'doc/worktree-state')


class ToolError(RuntimeError):
    pass


def run(args, cwd=None, capture=False, check=True, **kw):
    result = subprocess.run([str(x) for x in args], cwd=cwd, text=True,
                            stdout=kw.pop('stdout', subprocess.PIPE if capture else None),
                            stderr=kw.pop('stderr', subprocess.PIPE if capture else None), **kw)
    if check and result.returncode:
        raise ToolError('Commande en échec (%s) : %s\n%s' %
                        (result.returncode, args[0], result.stderr or ''))
    return result


def git(root, *args, check=True):
    return run(['git', '-C', root, *args], capture=True, check=check)


def value(root, *args):
    return git(root, *args).stdout.strip()


def worktrees():
    rows = []
    for block in value(MAIN, 'worktree', 'list', '--porcelain').split('\n\n'):
        row = dict(line.split(' ', 1) if ' ' in line else (line, True)
                   for line in block.splitlines())
        if 'worktree' in row:
            row['worktree'] = str(Path(row['worktree']).resolve())
            rows.append(row)
    return rows


def context(path=None, task=False, allow_main=False):
    root = Path(value(path or Path.cwd(), 'rev-parse', '--show-toplevel')).resolve()
    common = Path(value(root, 'rev-parse', '--path-format=absolute', '--git-common-dir')).resolve()
    expected = Path(value(MAIN, 'rev-parse', '--path-format=absolute', '--git-common-dir')).resolve()
    if common != expected or str(root) not in [x['worktree'] for x in worktrees()]:
        raise ToolError('Ce dossier ne correspond pas à un worktree du projet : %s' % root)
    branch = value(root, 'branch', '--show-current')
    if not branch:
        raise ToolError('HEAD détachée : choisis un worktree de tâche.')
    if task and ((root == MAIN and not allow_main) or not branch.startswith((FEATURE_PREFIX, TICKET_PREFIX))):
        raise ToolError('Cette commande nécessite un worktree de tâche dédié, hors dépôt principal.')
    name = branch.split('/', 1)[1] if branch.startswith((FEATURE_PREFIX, TICKET_PREFIX)) else branch
    if '/' in name or name in ('.', '..'):
        raise ToolError('Nom de tâche non pris en charge : %s' % name)
    return root, branch, name


def task_branch(name):
    name = name.removeprefix('#')
    if not re.fullmatch(r'(?:t_)?[0-9]+_[\w.-]+', name, re.UNICODE):
        raise ToolError('Utilise <numéro>_<nom> (accents acceptés), ou t_<numéro>_<nom>.')
    return (TICKET_PREFIX if name.startswith('t_') else FEATURE_PREFIX) + name


def key(root):
    return hashlib.sha256(str(Path(root).resolve()).encode()).hexdigest()[:16]


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def atomic(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix='.' + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.write('\n'); stream.flush(); os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def read_json(path, default=None):
    return json.loads(Path(path).read_text()) if Path(path).exists() else default


@contextlib.contextmanager
def locked(path, wait=False):
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a') as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))
        except BlockingIOError:
            raise ToolError('Opération déjà en cours : %s' % path)
        yield


def ask(message):
    if not os.isatty(0):
        raise ToolError(message + ' Relance dans un terminal interactif.')
    return input(message + ' [o/N] ').lower() in ('o', 'oui', 'y', 'yes')


def safe_relative(name):
    p = Path(name)
    if p.is_absolute() or '..' in p.parts or '.git' in p.parts:
        raise ToolError('Chemin de sauvegarde interdit : %s' % name)
    return p


def personal(name):
    first = name.split('/', 1)[0]
    return first.startswith(tuple(setting_json('PERSONAL_PREFIXES', ['.env']))) or first in setting_json('PERSONAL_NAMES', [])


def worktree_modified_since(root, cutoff):
    # Ne pas suivre doc/ et les autres liens vers des dossiers partages.
    def recent(path):
        if path.lstat().st_mtime >= cutoff:
            return True
        if path.is_symlink() or not path.is_dir():
            return False
        return any(recent(child) for child in path.iterdir() if child.name != '.git')

    if recent(root):
        return True
    admin = Path(value(root, 'rev-parse', '--path-format=absolute', '--git-dir'))
    # HEAD et son reflog couvrent les commits et changements de branche.
    # Ne pas utiliser index : git status peut en rafraichir la date sans edition.
    return any(path.exists() and path.stat().st_mtime >= cutoff
               for path in (admin / 'HEAD', admin / 'logs/HEAD'))


def collisions(root, paths, max_age_days=None):
    paths = list(dict.fromkeys(paths))
    if not paths:
        return []
    conflicts = []
    cutoff = None if max_age_days is None else time.time() - max_age_days * 86400
    for row in worktrees():
        other = Path(row['worktree'])
        if other == root:
            continue
        if cutoff is not None and not worktree_modified_since(other, cutoff):
            continue
        changed = set()
        # Regrouper les chemins pour eviter des milliers de processus Git ;
        # borner les argv et utiliser NUL pour les noms contenant des retours ligne.
        for offset in range(0, len(paths), 128):
            entries = iter(git(other, '--literal-pathspecs', 'status', '--porcelain',
                               '--untracked-files=all', '-z', '--',
                               *paths[offset:offset + 128]).stdout.split('\0'))
            for entry in entries:
                if not entry:
                    continue
                changed.add(entry[3:])
                if 'R' in entry[:2] or 'C' in entry[:2]:
                    # Avec -z, le renommage fournit aussi le chemin d'origine.
                    changed.add(next(entries))
        for path in paths:
            if any(name == path or name.startswith(path.rstrip('/') + '/') for name in changed):
                conflicts.append('%s : %s (%s)' % (path, other, row.get('branch', 'detached')))
    return conflicts
