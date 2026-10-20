"""Retirer les worktrees de tâches sans activité locale récente, branches conservées."""
import argparse
import datetime
import os
from pathlib import Path
import sys
import time

import _wt_remove as removal
from _wt_common import MAIN, ToolError, context, git, setting, task_branch, value, worktrees


def newest(path, nested=False):
    """Ne suivre aucun lien : doc pointe vers des documents partagés entre tâches."""
    stamp = path.lstat().st_mtime
    if path.is_symlink() or not path.is_dir():
        return stamp
    if nested and (os.path.ismount(path) or (path / '.git').exists()):
        raise ToolError('Montage ou dépôt imbriqué à vérifier : %s' % path)
    for child in path.iterdir():
        stamp = max(stamp, newest(child, nested=True))
    return stamp


def last_activity(root, branch):
    latest = newest(root)
    admin = Path(value(root, 'rev-parse', '--path-format=absolute', '--git-dir'))
    for relative in ('HEAD', 'index', 'logs/HEAD'):
        path = admin / relative
        if path.exists(): latest = max(latest, path.stat().st_mtime)
    task = branch.split('/', 1)[1]
    journals = MAIN / setting('JOURNAL_DIR', 'sessions_codex')
    for suffix in ('.jsont', '.jsonl'):
        path = journals / (task + suffix)
        if path.exists(): latest = max(latest, path.stat().st_mtime)
    folder = removal.atelier.directory(root)
    if folder.exists():
        # Les verrous sont ouverts par la suppression elle-même, pas par l'utilisateur.
        for path in folder.iterdir():
            if not path.name.endswith('.lock'): latest = max(latest, newest(path))
    return latest


def eligible(root, branch, cutoff, current):
    if root in (MAIN.resolve(), current) or current.is_relative_to(root):
        raise ToolError('Worktree principal ou courant protégé.')
    if last_activity(root, branch) >= cutoff:
        raise ToolError('Activité plus récente que le seuil.')
    removal.clean(root)
    removal.inactive(root)


def prune(days=20, dry_run=False):
    if days < 1:
        raise ToolError('Le nombre de jours doit être strictement positif.')
    current, _, _ = context()
    cutoff = time.time() - days * 86400
    count = failures = 0
    for row in worktrees():
        root = Path(row['worktree'])
        branch = row.get('branch', '').removeprefix('refs/heads/')
        if root == MAIN.resolve() or root == current:
            print('CONSERVÉ %s : worktree principal ou courant.' % root)
            continue
        try:
            task = branch.split('/', 1)[1] if '/' in branch else ''
            if task_branch(task) != branch:
                raise ToolError('Branche hors convention de tâche.')
            # target vérifie aussi les chemins, associations, verrous et chemins manquants.
            checked, _, _, registered = removal.target(task)
            if checked != root or not registered:
                raise ToolError('Association Git incohérente.')
            eligible(root, branch, cutoff, current)
        except (ToolError, OSError) as error:
            print('CONSERVÉ %s : %s' % (root, error), flush=True)
            continue
        date = datetime.datetime.fromtimestamp(last_activity(root, branch)).astimezone().isoformat(timespec='seconds')
        print('%s %s (dernière activité : %s)' %
              ('CANDIDAT' if dry_run else 'SUPPRESSION', root, date), flush=True)
        if not dry_run:
            try:
                removal.remove(task, guard=lambda root, branch: eligible(root, branch, cutoff, current))
            except (ToolError, OSError) as error:
                print('ÉCHEC %s : %s' % (root, error), file=sys.stderr, flush=True)
                failures += 1
                continue
        count += 1
    print('%s worktree(s) %s ; %s échec(s).' % (count, 'candidat(s)' if dry_run else 'supprimé(s)', failures))
    return 1 if failures else 0


def main():
    parser = argparse.ArgumentParser(prog='worktree_prune', description=__doc__)
    parser.add_argument('days', nargs='?', type=int, default=20, help='Jours sans activité (défaut : 20)')
    parser.add_argument('--dry-run', action='store_true', help='Afficher les candidats sans sauvegarde ni suppression')
    args = parser.parse_args()
    if args.days < 1: parser.error('days doit être strictement positif')
    # git status ne doit pas rafraîchir l'index et créer une fausse activité.
    os.environ['GIT_OPTIONAL_LOCKS'] = '0'
    try:
        return prune(args.days, args.dry_run)
    except (ToolError, OSError) as error:
        print('ERREUR : %s' % error, file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
