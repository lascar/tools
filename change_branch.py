#!/usr/bin/env python3
"""Préparation et fusion pour la fonction Bash change_branch.sh."""
import argparse
import contextlib
import json
import os
from pathlib import Path
import subprocess
import sys

from _wt_common import MAIN, STATE, ToolError, context, git, key, locked, read_json, run, task_branch, value, worktrees, BASE_BRANCH, WORKTREE_LABEL
import _wt_atelier as atelier
import _wt_docker as runtime
from _wt_remove import process_directory


def target_for(task):
    branch = task_branch(task)
    rows = [row for row in worktrees() if row.get('branch') == 'refs/heads/' + branch]
    if len(rows) != 1:
        raise ToolError('Aucun worktree unique pour %s. Rattache d’abord cette branche à un worktree.' % branch)
    row = rows[0]
    if 'locked' in row or 'prunable' in row:
        raise ToolError('Worktree verrouillé ou indisponible.')
    root = Path(row['worktree'])
    if root == MAIN or context(root, task=True)[1] != branch:
        raise ToolError('La branche ne correspond pas au worktree de tâche attendu.')
    return root, branch


def clean(root):
    changes = git(root, 'status', '--porcelain', '--untracked-files=all').stdout
    if changes:
        raise ToolError('Changements non committés dans %s :\n%s' % (root, changes))
    # Certaines opérations (rebase, cherry-pick vide) laissent un index propre.
    for marker in ('MERGE_HEAD', 'CHERRY_PICK_HEAD', 'REVERT_HEAD', 'rebase-merge', 'rebase-apply', 'sequencer', 'BISECT_LOG'):
        if Path(value(root, 'rev-parse', '--path-format=absolute', '--git-path', marker)).exists():
            raise ToolError('Opération Git en cours dans %s : %s.' % (root, marker))


def caller_pids():
    pids = set()
    pid = os.getpid()
    while pid and pid not in pids:
        pids.add(pid)
        try:
            pid = int(Path('/proc/%s/stat' % pid).read_text().rsplit(')', 1)[1].split()[1])
        except FileNotFoundError:
            break
    return pids


def idle(root):
    for role, title in atelier.ROLES:
        record = read_json(atelier.directory(root) / (role + '.json'), {})
        if atelier.alive(record) or atelier.alive(record, 'child_'):
            raise ToolError('Atelier actif dans %s : %s. Ferme-le avant de continuer.' % (root, title))
    if runtime.read_state(root).get('status') == 'activating':
        raise ToolError('Démarrage Docker en cours dans %s.' % root)
    # Inspecter les montages réels, y compris un serveur lancé hors des scripts.
    ids = runtime.docker('ps', '-q', capture=True, timeout=30).stdout.split()
    items = json.loads(runtime.docker('inspect', *ids, capture=True, timeout=30).stdout) if ids else []
    for item in items:
        labels = item.get('Config', {}).get('Labels') or {}
        mounts = [Path(m['Source']).resolve() for m in item.get('Mounts', []) if m.get('Type') == 'bind']
        if (root in mounts or labels.get(WORKTREE_LABEL) == str(root) or
                labels.get('com.docker.compose.project') == runtime.project_for(root)):
            raise ToolError('Conteneur actif dans %s : %s.' % (root, item.get('Name', item['Id'])))
    # Les shells seuls ne prouvent pas une activité ; éditeurs, agents, serveurs
    # et commandes dans ce dossier bloquent. Exclure notre propre chaîne d’appel.
    excluded = caller_pids()
    for proc in Path('/proc').iterdir():
        if not proc.name.isdigit() or int(proc.name) in excluded:
            continue
        try:
            if proc.stat().st_uid != os.getuid():
                continue
            # Reutiliser la lecture privilegiee ciblee des processus proteges.
            # stdout reste reserve au chemin retourne a la fonction Bash.
            with contextlib.redirect_stdout(sys.stderr):
                cwd = process_directory(proc)
            if cwd is None or (cwd != root and root not in cwd.parents):
                continue
            name = (proc / 'comm').read_text().strip()
            if name not in ('bash', 'sh', 'dash', 'zsh', 'fish'):
                raise ToolError('Commande active dans %s : %s (PID %s).' % (root, name, proc.name))
        except (FileNotFoundError, ProcessLookupError):
            continue
        except PermissionError as error:
            raise ToolError('Impossible de vérifier l’activité du processus %s : %s' % (proc.name, error))


@contextlib.contextmanager
def guarded(root):
    # Même ordre que l’ouverture d’atelier : atelier puis runtime.
    with locked(atelier.directory(root) / 'atelier.lock'), locked(STATE / 'runtime.lock'):
        yield


def prepare(task):
    source = context()[0]
    root, branch = target_for(task)
    with guarded(root):
        if context(MAIN)[1] != BASE_BRANCH:
            raise ToolError(f'Le dépôt principal doit déjà être sur {BASE_BRANCH} ; aucun changement de branche automatique.')
        for path in set((source, MAIN, root)):
            clean(path)
        idle(root)
        # stdout est réservé au chemin pour la fonction shell, pas aux sorties Git.
        run(['git', '-C', MAIN, 'pull', '--ff-only'], stdout=sys.stderr, timeout=120)
        if target_for(task) != (root, branch):
            raise ToolError('Le worktree a changé pendant le pull.')
        clean(root)
        idle(root)
    print(root)


def merge(task):
    root, branch = target_for(task)
    if Path.cwd().resolve() != root:
        raise ToolError('La fusion doit être lancée depuis la racine du worktree cible.')
    with guarded(root):
        if context(MAIN)[1] != BASE_BRANCH:
            raise ToolError(f'Le dépôt principal n’est plus sur {BASE_BRANCH} ; fusion refusée.')
        clean(MAIN)
        clean(root)
        idle(root)
        print(f'Fusion de {BASE_BRANCH} dans %s (%s).' % (branch, root), flush=True)
        result = run(['git', '-C', root, 'merge', '--no-edit', BASE_BRANCH], check=False)
        if result.returncode:
            raise ToolError('Fusion interrompue dans %s. Consulte git status ; aucun conflit résolu automatiquement.' % root)


def home():
    # Revenir dans le worktree principal sans toucher aux branches des taches.
    if context(MAIN)[1] != BASE_BRANCH:
        raise ToolError(f'Le dépôt principal doit déjà être sur {BASE_BRANCH} ; aucun changement de branche automatique.')
    print(MAIN)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('phase', choices=('prepare', 'merge', 'home'))
    parser.add_argument('task', nargs='?')
    args = parser.parse_args()
    if args.phase != 'home' and not args.task:
        parser.error('la tâche est obligatoire pour prepare et merge')
    if args.phase == 'home' and args.task:
        parser.error('home ne prend pas de tâche')
    try:
        if args.phase == 'home':
            home()
        else:
            (prepare if args.phase == 'prepare' else merge)(args.task)
    except (ToolError, OSError, subprocess.TimeoutExpired, ValueError) as error:
        print('ERREUR : %s' % error, file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
