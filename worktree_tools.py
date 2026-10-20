#!/usr/bin/env python3
"""Workflow worktree : ouverture et actualisation depuis la branche de référence configurée."""
import argparse
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from _wt_common import *
import _wt_patch as patches
import _wt_sessions as sessions
import _wt_docker as runtime
import _wt_atelier as atelier


def ensure_worktree(task, create=False, return_new=False):
    branch = task_branch(task); task = branch.split('/', 1)[1]
    with locked(STATE / 'worktree-create.lock'):
        rows = [r for r in worktrees() if r.get('branch') == 'refs/heads/' + branch]
        if rows:
            if create: raise ToolError('Branche déjà ouverte ; utilise worktree_open.sh.')
            root = Path(rows[0]['worktree'])
            if root == MAIN: raise ToolError('Cette branche occupe le dépôt principal. Libère-la manuellement avant de créer son worktree.')
            if 'locked' in rows[0] or 'prunable' in rows[0]: raise ToolError('Worktree verrouillé ou indisponible ; aucune réparation automatique.')
            if root.parent != MAIN.parent: raise ToolError('Worktree hors du répertoire development attendu.')
            context(root, task=True)
            if root.stat().st_dev != MAIN.stat().st_dev:
                raise ToolError('Worktree sur une autre partition que le dépôt principal.')
            return (root, False) if return_new else root
        local = git(MAIN, 'show-ref', '--verify', '--quiet', 'refs/heads/' + branch, check=False).returncode == 0
        remote = git(MAIN, 'show-ref', '--verify', '--quiet', 'refs/remotes/' + REMOTE + '/' + branch, check=False).returncode == 0
        # Le choix automatique reste sous le verrou de creation : reutiliser
        # une branche connue, y compris distante, avant d'en creer une.
        if create is None:
            create = not (local or remote)
        if create and (local or remote): raise ToolError('Branche existante : utilise worktree_open.sh.')
        if not create and not (local or remote): raise ToolError('Branche absente : utilise worktree_new.sh (ou actualise les références distantes).')
        root = MAIN.parent / (setting('WORKTREE_PREFIX', PROJECT_NAME + '-') + task)
        if root.exists() or root.is_symlink(): raise ToolError('Destination déjà occupée : %s' % root)
        if MAIN.stat().st_dev != MAIN.parent.stat().st_dev: raise ToolError('Le répertoire des worktrees est sur une autre partition.')
        if create:
            if value(MAIN, 'branch', '--show-current') != BASE_BRANCH: raise ToolError(f'Le dépôt principal doit être sur {BASE_BRANCH} pour créer une tâche.')
            args = ['worktree', 'add', '-b', branch, str(root), BASE_BRANCH]
        elif local: args = ['worktree', 'add', str(root), branch]
        else: args = ['worktree', 'add', '--track', '-b', branch, str(root), REMOTE + '/' + branch]
        run(['git', '-C', MAIN, *args])
        return (root, create) if return_new else root


def existing_worktree(task):
    # Cibler une tâche existante ne doit jamais créer son worktree ni sa branche.
    branch = task_branch(task)
    rows = [r for r in worktrees() if r.get('branch') == 'refs/heads/' + branch]
    if len(rows) != 1:
        raise ToolError('Aucun worktree unique pour %s ; opération annulée.' % branch)
    row = rows[0]
    if 'locked' in row or 'prunable' in row:
        raise ToolError('Worktree verrouillé ou indisponible ; opération annulée.')
    root, actual, _ = context(Path(row['worktree']), task=True)
    if actual != branch:
        raise ToolError('La branche du worktree a changé ; opération annulée.')
    return root


def local_changes_choice(root):
    if not os.isatty(0):
        raise ToolError('Modifications locales dans %s ; relance dans un terminal pour choisir, ou utilise --no-update.' % root)
    print('Modifications locales dans %s :' % root)
    print(git(root, 'status', '--short', '--untracked-files=all').stdout, end='')
    print(f'1. Fusionner {REMOTE}/{BASE_BRANCH} en conservant les changements locaux (mise de côté temporaire).')
    print('2. Sauvegarder en patch puis nettoyer et ouvrir sans fusion.')
    print('3. Éliminer les changements locaux puis ouvrir sans fusion.')
    print('0. Annuler (par défaut).')
    while True:
        try:
            choice = input('Ton choix [0/1/2/3] : ').strip()
        except (EOFError, KeyboardInterrupt):
            choice = '0'
        if choice in ('', '0'):
            raise ToolError('Ouverture annulée ; changements locaux conservés.')
        if choice in ('1', '2', '3'):
            return {'1': 'merge', '2': 'patch', '3': 'discard'}[choice]
        print('Choisis 0, 1, 2 ou 3.')


def changes_snapshot(root):
    paths = patches.changed(root)
    if any(personal(p) for p in paths):
        raise ToolError('Fichiers personnels modifiés : traite-les séparément, ou ouvre avec --no-update.')
    for name in paths:
        if git(root, 'ls-files', '--stage', '--', name).stdout.startswith('160000'):
            raise ToolError('Sous-module modifié : traitement manuel nécessaire : ' + name)
    return (value(root, 'rev-parse', 'HEAD'), patches.diff_bytes(root, True),
            patches.diff_bytes(root), [patches.file_record(root, p)[0] for p in paths])


def clear_local_changes(root, snapshot):
    # Comparer aussi les contenus : status seul ne detecte pas une nouvelle edition.
    if changes_snapshot(root) != snapshot:
        raise ToolError('Le travail a changé avant le nettoyage ; aucune suppression effectuée.')
    tracked = sorted(set(patches.names(root, 'diff', '--name-only', '--no-renames', '-z') +
                         patches.names(root, 'diff', '--cached', '--name-only', '--no-renames', '-z')))
    untracked = patches.names(root, 'ls-files', '--others', '--exclude-standard', '-z')
    if tracked:
        # checkout . seul conserverait les changements indexes et les ajouts.
        run(['git', '-C', root, '--literal-pathspecs', 'restore', '--source=HEAD',
             '--staged', '--worktree', '--', *tracked])
    for name in untracked:
        (root / safe_relative(name)).unlink()


def resolve_open_conflicts(root):
    root, branch, _ = context(root, task=True)
    paths = sorted(set(patches.names(root, 'diff', '--name-only', '--diff-filter=U', '-z')))
    if not paths:
        raise ToolError('Aucun conflit de fichier à traiter avec git mergetool ; vérifie git status.')
    if not os.isatty(0):
        raise ToolError('Conflits présents ; relance worktree_open.sh dans un terminal pour lancer git mergetool.')
    found = collisions(root, paths)
    if found:
        raise ToolError('Fichiers modifiés dans un autre worktree ; mergetool refusé :\n' + '\n'.join(found))
    head = value(root, 'rev-parse', 'HEAD')
    print('Résolution des conflits avec git mergetool dans %s...' % branch, flush=True)
    result = run(['git', '-C', root, '--literal-pathspecs', 'mergetool', '--no-prompt', '--', *paths],
                 cwd=root, check=False)
    if value(root, 'branch', '--show-current') != branch or value(root, 'rev-parse', 'HEAD') != head:
        raise ToolError('La branche ou HEAD a changé pendant mergetool ; reprise refusée.')
    remaining = patches.names(root, 'diff', '--name-only', '--diff-filter=U', '-z')
    if result.returncode or remaining:
        raise ToolError('Résolution interrompue ou conflits encore présents ; relance worktree_open.sh après résolution. Aucun atelier lancé.')
    print('Conflits résolus ; reprise de l’ouverture.', flush=True)


def restore_open_stash(root, stash):
    result = run(['git', '-C', root, 'stash', 'apply', '--index', stash], check=False)
    if result.returncode:
        try:
            resolve_open_conflicts(root)
        except ToolError as exc:
            raise ToolError('Réapplication interrompue dans %s : %s Sauvegarde conservée : %s.' % (root, exc, stash))
        # Apres une resolution manuelle, conserver la sauvegarde d'origine par precaution.
        print('Sauvegarde conservée après résolution : %s.' % stash)
        return
    # Ne jamais supprimer le stash d'une autre session qui aurait travaille entre-temps.
    if value(root, 'rev-parse', 'refs/stash') == stash:
        run(['git', '-C', root, 'stash', 'drop', 'stash@{0}'])
    else:
        print('Changements réappliqués ; sauvegarde temporaire conservée : %s.' % stash)


def refresh_master(root):
    # Le verrou est commun aux taches : elles partagent le worktree master.
    with locked(STATE / 'master-update.lock'):
        def ready():
            if context(MAIN)[1] != BASE_BRANCH:
                raise ToolError(f'Le dépôt principal doit être sur {BASE_BRANCH} ; aucune bascule automatique.')
            for marker in ('MERGE_HEAD', 'CHERRY_PICK_HEAD', 'REVERT_HEAD', 'rebase-merge', 'rebase-apply', 'sequencer'):
                path = Path(value(MAIN, 'rev-parse', '--path-format=absolute', '--git-path', marker))
                if path.exists():
                    raise ToolError(f'Opération Git en cours sur {BASE_BRANCH} local ; termine-la avant la mise à jour.')
            if value(MAIN, 'status', '--porcelain', '--untracked-files=all'):
                raise ToolError(f'Modifications locales sur {BASE_BRANCH} ; conserve-les et traite-les avant la mise à jour, ou utilise --no-update.')
        ready()
        # Refspec explicite : ne pas utiliser une ref ancienne si le fetch est restreint.
        run(['git', '-C', root, 'fetch', REMOTE, '+refs/heads/' + BASE_BRANCH + ':refs/remotes/' + REMOTE + '/' + BASE_BRANCH])
        target = value(root, 'rev-parse', '--verify', 'refs/remotes/' + REMOTE + '/' + BASE_BRANCH + '^{commit}')
        ready()
        head = value(MAIN, 'rev-parse', 'HEAD')
        if git(MAIN, 'merge-base', '--is-ancestor', target, head, check=False).returncode == 0:
            print(f'Le {BASE_BRANCH} local contient déjà {REMOTE}/{BASE_BRANCH}.')
            return target
        if git(MAIN, 'merge-base', '--is-ancestor', head, target, check=False).returncode != 0:
            raise ToolError(f'Le {BASE_BRANCH} local a divergé de {REMOTE}/{BASE_BRANCH} ; aucune fusion automatique dans {BASE_BRANCH}.')
        paths = patches.names(MAIN, 'diff', '--name-only', '--no-renames', '-z', head, target)
        # La tache courante participe a cette operation ; verifier les autres taches.
        found = collisions(root, paths)
        if found:
            raise ToolError(f'Fichiers modifiés dans un autre worktree ; actualisation du {BASE_BRANCH} local refusée :\n' + '\n'.join(found))
        ready()
        if value(MAIN, 'rev-parse', 'HEAD') != head:
            raise ToolError(f'Le {BASE_BRANCH} local a changé pendant la vérification ; relance l’ouverture.')
        print(f'Actualisation du {BASE_BRANCH} local en avance rapide...', flush=True)
        result = run(['git', '-C', MAIN, 'merge', '--ff-only', '--no-autostash',
                      '--no-overwrite-ignore', target], check=False)
        if result.returncode:
            raise ToolError(f'Actualisation du {BASE_BRANCH} local interrompue ; aucune fusion dans la tâche ni ouverture effectuée.')
        return target


def update_from_master(root):
    root, branch, _ = context(root, task=True)
    with locked(STATE / ('update-' + key(root) + '.lock')):
        def ready(clean=True):
            if value(root, 'branch', '--show-current') != branch:
                raise ToolError('La branche du worktree a changé ; mise à jour refusée.')
            for marker in ('MERGE_HEAD', 'CHERRY_PICK_HEAD', 'REVERT_HEAD', 'rebase-merge', 'rebase-apply', 'sequencer'):
                path = Path(value(root, 'rev-parse', '--path-format=absolute', '--git-path', marker))
                if path.exists():
                    raise ToolError('Opération Git en cours dans %s ; termine-la avant de rouvrir la tâche.' % root)
            if clean and value(root, 'status', '--porcelain', '--untracked-files=all'):
                raise ToolError('Modifications locales dans %s ; conserve-les et utilise --no-update pour ouvrir sans fusion.' % root)
        ready(clean=False)
        if patches.names(root, 'diff', '--name-only', '--diff-filter=U', '-z'):
            # Un stash deja partiellement applique ne doit jamais etre reapplique.
            resolve_open_conflicts(root)
            print('Les stash existants sont conservés ; aucune seconde réapplication.')
            return
        snapshot = None
        if value(root, 'status', '--porcelain', '--untracked-files=all'):
            snapshot = changes_snapshot(root)
            action = local_changes_choice(root)
            ready(clean=False)
            if changes_snapshot(root) != snapshot:
                raise ToolError('Le travail a changé pendant le choix ; relance l’ouverture.')
            found = collisions(root, [f['path'] for f in snapshot[3]])
            if found:
                raise ToolError('Fichiers modifiés dans un autre worktree ; traitement refusé :\n' + '\n'.join(found))
            if action in ('patch', 'discard'):
                if action == 'patch':
                    archive = patches.make(root)
                    if not archive:
                        raise ToolError('Aucune sauvegarde créée ; nettoyage refusé.')
                    patches.inspect_archive(archive)
                clear_local_changes(root, snapshot)
                ready()
                print(f'Branche nettoyée ; ouverture sans fusion de {REMOTE}/{BASE_BRANCH}.')
                return action
        print(f'Actualisation de {REMOTE}/{BASE_BRANCH} et du {BASE_BRANCH} local, puis fusion dans %s...' % branch, flush=True)
        target = refresh_master(root)
        ready(clean=snapshot is None)
        if snapshot is not None and changes_snapshot(root) != snapshot:
            raise ToolError('Le travail a changé pendant le fetch ; aucune mise de côté effectuée.')
        included = git(root, 'merge-base', '--is-ancestor', target, 'HEAD', check=False).returncode
        if included == 0:
            print(f'La branche contient déjà {REMOTE}/{BASE_BRANCH}.')
            return
        if included != 1: raise ToolError(f'Impossible de comparer la tâche à {REMOTE}/{BASE_BRANCH}.')
        base = value(root, 'merge-base', 'HEAD', target)
        paths = git(root, 'diff', '--name-only', '--no-renames', '-z', base, target).stdout.split('\0')
        print('Vérification des collisions dans les autres worktrees (%s fichiers)...' %
              len([p for p in paths if p]), flush=True)
        found = collisions(root, [p for p in paths if p])
        if found:
            raise ToolError('Fichiers modifiés dans un autre worktree ; fusion refusée :\n' + '\n'.join(found))
        ready(clean=snapshot is None)
        stash = None
        if snapshot is not None:
            if changes_snapshot(root) != snapshot:
                raise ToolError('Le travail a changé avant la fusion ; relance l’ouverture.')
            # Le choix explicite de fusion autorise cette sauvegarde temporaire.
            with locked(STATE / 'open-stash.lock'):
                run(['git', '-C', root, 'stash', 'push', '--include-untracked',
                     '-m', 'worktree_open : ' + branch])
                stash = value(root, 'rev-parse', 'refs/stash')
            print('Changements mis de côté : %s (réapplication après fusion).' % stash, flush=True)
        ready()
        # Garder les conflits visibles et proteger les fichiers ignores.
        result = run(['git', '-C', root, 'merge', '--no-edit', '--no-autostash',
                      '--no-overwrite-ignore', '--ff', '--commit', target], check=False)
        if result.returncode:
            saved = (' Changements locaux conservés dans le stash %s ; après résolution ou abandon, '
                     'réapplique-les avec git stash apply --index %s.' % (stash, stash)) if stash else ''
            raise ToolError('Fusion interrompue dans %s. Vérifie git status et résous ou abandonne la fusion avant de relancer ; aucun atelier lancé.%s' % (root, saved))
        if stash:
            restore_open_stash(root, stash)


def open_command(args):
    context()  # refuser un appel depuis le dépôt imbriqué doc/tools
    reach = args.command == 'worktree_reach'
    if reach and args.task is None:
        # Revenir au terminal existant sans preparer ni actualiser de worktree.
        return atelier.worktree_switch()
    if reach:
        # Ne jamais actualiser Git ni preparer les fichiers d'un atelier actif.
        branch = task_branch(args.task)
        rows = [r for r in worktrees() if r.get('branch') == 'refs/heads/' + branch]
        if rows:
            root = ensure_worktree(args.task)
            folder = atelier.directory(root)
            live = [role for role, _ in atelier.ROLES
                    if atelier.alive(read_json(folder / (role + '.json'), {}))]
            if live:
                if not args.prepare_only:
                    atelier.worktree_switch(root)
                if args.result_file: Path(args.result_file).write_text(str(root) + '\n')
                return
    if not args.prepare_only: atelier.preflight()
    if reach:
        root, new = ensure_worktree(args.task, create=None, return_new=True)
    else:
        new = args.command == 'worktree_new'
        root = ensure_worktree(args.task, new)
    print('Worktree : %s\nBranche : %s' % (root, value(root, 'branch', '--show-current')))
    action = None
    if not new and not args.no_update:
        action = update_from_master(root)
    sessions.prepare(root, new=new)
    if not args.prepare_only:
        if action == 'patch':
            # Ne pas proposer immediatement de restaurer le travail qui vient d’etre range.
            atelier.open_atelier(root, args.mode, offer_patches=False)
        else:
            atelier.open_atelier(root, args.mode)
    if args.result_file: Path(args.result_file).write_text(str(root) + '\n')


def describe(root, task=None):
    report_worktree = None
    if task is None:
        root, branch, task = context(root, task=True)
        report_worktree = root
        head = value(root, 'rev-parse', 'HEAD')
    else:
        branch = task_branch(task)
        task = branch.split('/', 1)[1]
        if any(row.get('branch') == 'refs/heads/' + branch for row in worktrees()):
            root = existing_worktree(task)
            report_worktree = root
            head = value(root, 'rev-parse', 'HEAD')
        else:
            # Les objets Git restent accessibles après le retrait d'un worktree.
            # Préférer la branche locale, sinon sa référence distante déjà connue.
            root = MAIN
            for ref in ('refs/heads/' + branch, 'refs/remotes/' + REMOTE + '/' + branch):
                result = git(root, 'rev-parse', '--verify', ref + '^{commit}', check=False)
                if result.returncode == 0:
                    head = result.stdout.strip()
                    break
            else:
                raise ToolError('Branche locale ou référence distante absente pour %s ; rapport non créé.' % branch)
    # Une tâche déjà intégrée reste présente dans l'historique de HEAD :
    # sélectionner ses sujets, pas la différence avec la branche principale.
    prefix = re.compile(r'^' + re.escape(task) + r'(?:$|[\s:])')
    commits = []
    for line in git(root, 'log', '--format=%H%x09%s', head).stdout.splitlines():
        sha, subject = line.split('\t', 1)
        if prefix.match(subject):
            commits.append(sha)
    paths, descriptions = set(), []
    for sha in commits:
        # Même sélection de différences pour les fichiers et les patches.
        # Les fusions combinées évitent de lister tout le travail importé.
        paths.update(name for name in git(root, 'show', '--format=', '--name-only',
                     '--no-renames', '--diff-merges=combined', '-z', sha).stdout.split('\0') if name)
        descriptions.append(git(root, 'show', '--format=fuller', '--stat', '--patch',
                            '--no-renames', '--diff-merges=combined', '--no-color',
                            '--no-ext-diff', '--no-textconv', sha).stdout)
    text = 'Fichiers modifiés dans les commits de la tâche :\n'
    text += ('\n'.join(sorted(paths)) if paths else '(aucun)') + '\n\n'
    text += 'Tâche : %s\nBranche : %s\nWorktree : %s\nHEAD analysé : %s\nCommits sélectionnés : %s\n\n' % (task, branch, report_worktree or '(absent ; lecture directe de la branche)', head, len(commits))
    if commits:
        text += '\n\n'.join(descriptions)
    else:
        text += 'Aucun commit dont le sujet commence par le nom de cette tâche.\n'
    # Conserver un rapport dédié pour ne pas écraser les notes de la tâche.
    dest = MAIN / setting('DOCUMENTS_DIR', 'doc') / (task + '_git.txt')
    dest.parent.mkdir(exist_ok=True)
    dest.write_text(text)
    print(dest)


def commit(root, message, no_verify=False):
    root, branch, task = context(root, task=True)
    # Les messages speciaux et leurs fichiers sont definis par la configuration.
    if message not in setting_json('SPECIAL_COMMITS', {}) and not re.match(r'^(feat|fix|docs|style|refactor|perf|test|build|ci|chore|revert)(\([^\n]+\))?!?: .+', message):
        raise ToolError('Message attendu : to_prod, to_devb ou fix: description (ou autre type Conventional Commits).')
    files = patches.names(root, 'diff', '--cached', '--name-only', '-z')
    if any(personal(f) for f in files): raise ToolError('Fichier personnel dans l’index ; commit refusé.')
    if git(root, 'diff', '--name-only', '--diff-filter=U').stdout: raise ToolError('Conflits non résolus.')
    if message in setting_json('SPECIAL_COMMITS', {}):
        # Seul ce fichier est prepare automatiquement pour ces deux messages.
        run(['git', '-C', root, 'add', '--', *setting_json('SPECIAL_COMMITS', {})[message]])
        run(['git', '-C', root, '--no-pager', 'diff', '--cached'])
        files = patches.names(root, 'diff', '--cached', '--name-only', '-z')
    if any(personal(f) for f in files): raise ToolError('Fichier personnel dans l’index ; commit refusé.')
    if not files: raise ToolError('Index vide : prépare explicitement les fichiers avant commit.sh.')
    run(['git', '-C', root, 'diff', '--cached', '--check'])
    run(['git', '-C', root, 'diff', '--cached', '--stat'])
    return run(['git', '-C', root, 'commit', *(['--no-verify'] if no_verify else []), '-m', task + ' : ' + message]).returncode


def push(root, target, force=False):
    root, branch, _ = context(root, task=True)
    destinations = setting_json('PUSH_TARGETS', {REMOTE: {'remote': REMOTE}})
    if target not in destinations: raise ToolError('Destination inconnue.')
    destination = destinations[target]
    remote = destination['remote']
    if force and remote not in setting_json('FORCE_PUSH_REMOTES', []):
        raise ToolError('--force-with-lease est interdit pour cette destination.')
    value(root, 'remote', 'get-url', remote)
    print('Push %s : %s -> %s' % (root, branch, remote))
    if destination.get('before'): run(destination['before'], timeout=30)
    lease = []
    if force:
        # Attente explicite du SHA distant, pas de confiance dans une ref fetchée en arrière-plan.
        ref = 'refs/heads/' + branch
        line = value(root, 'ls-remote', '--heads', remote, ref)
        expected = line.split()[0] if line else ''
        lease = ['--force-with-lease=' + ref + ':' + expected]
    return run(['git', '-C', root, 'push', *lease, remote, 'HEAD:refs/heads/' + branch]).returncode


def status_all():
    opened = 0
    for row in worktrees():
        root = Path(row['worktree'])
        print('%s\n  %s' % (root, row.get('branch', 'detached')))
        if root != MAIN:
            roles = atelier.open_roles(root)
            if roles:
                opened += 1
                titles = dict(atelier.ROLES)
                print('  atelier OUVERT (onglets : %s)' % ', '.join(titles[role] for role in roles))
            else:
                print('  atelier fermé')
        result = git(root, 'status', '--short', '--untracked-files=all', check=False)
        print(result.stdout or ('  propre\n' if not result.returncode else '  inaccessible\n'))
    print('%s atelier(s) de worktree actuellement ouvert(s).' % opened if opened else
          'Aucun atelier de worktree actuellement ouvert.')


def audit(root, target, days, author):
    value(root, 'rev-parse', '--verify', target + '^{commit}')
    author = author or value(root, 'config', 'user.email')
    # Les stash contiennent des commits techniques non-merge : --all les inclurait.
    commits = value(root, 'log', '--branches', '--remotes', '--no-merges', '--since=' + str(days) + ' days ago',
                    '--author=' + author, '--format=%H %h %as %s').splitlines()
    for line in commits:
        sha, label = line.split(' ', 1)
        code = git(root, 'merge-base', '--is-ancestor', sha, target, check=False).returncode
        if code not in (0, 1): raise ToolError('Audit de fusion en échec.')
        print(('INTÉGRÉ ' if code == 0 else 'À INTÉGRER ') + label)
    print('\nCommits des branches locales absents des références origin connues :')
    for branch in value(root, 'for-each-ref', '--format=%(refname:short)', 'refs/heads/').splitlines():
        result = value(root, 'log', branch, '--not', '--remotes=' + REMOTE, '--format=%h %as %s')
        if result: print('\n%s\n%s' % (branch, result))


def test_run(root, args):
    context(root, task=True)
    if not args: raise ToolError('Indique le scope CI : file test/unit/mon_test.rb, unit, etc.')
    if '--db-setup' in args and '--no-db-setup' in args:
        raise ToolError('Choisis une seule option de préparation de la base CI.')
    if '--db-setup' not in args and '--no-db-setup' not in args: args += ['--no-db-setup']
    if '--db-setup' in args: print('Préparation explicite : la base de test sera réinitialisée.')
    print('Attente du verrou CI commun...')
    with locked(STATE / 'ci.lock', wait=True):
        import hashlib
        digest = hashlib.sha256()
        for file in sorted(root.glob(setting('CI_SCHEMA_GLOB', '*.sql'))):
            digest.update(file.name.encode()); digest.update(file.read_bytes())
        schema = digest.hexdigest()
        recorded = read_json(STATE / 'ci-schema.json')
        if '--db-setup' not in args and recorded and recorded['sha256'] != schema:
            raise ToolError('Schéma CI différent de la dernière préparation connue. Lance explicitement --db-setup.')
        if '--db-setup' not in args and not recorded:
            print('Schéma CI non enregistré par ces outils : --no-db-setup suppose une base déjà compatible.')
        mounts = sessions.tmp_mount(root)
        for folder in setting_json('CI_SHARED_DIRS', []):
            if not (root / folder).exists() and (MAIN / folder).is_dir():
                mounts.append({'type': 'bind', 'source': str(MAIN / folder), 'target': APP_PATH + '/' + folder,
                               'read_only': True, 'bind': {'create_host_path': False}})
        override = STATE / 'configs' / (key(root) + '.ci.json')
        atomic(override, {'services': {setting('CI_SERVICE', 'test'): {'volumes': mounts}},
                          'volumes': {setting('CI_CACHE_KEY', 'cache'): {'name': setting('CI_CACHE_PREFIX', DOCKER_PREFIX + 'ci-') + key(root) + '_cache'}}})
        # Le code et le build viennent de la tâche ; les données CI sont partagées et sérialisées.
        code = run(['docker', 'compose', '-p', setting('CI_PROJECT'), '--project-directory', str(root), '-f', str(runtime.compose_file(root, ci=True)), '-f', str(override),
                    'run', '--rm', setting('CI_SERVICE', 'test'), *setting_json('CI_COMMAND'), *args], cwd=root).returncode
        if '--db-setup' in args: atomic(STATE / 'ci-schema.json', {'sha256': schema, 'prepared_at': now()})
        return code


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest='command', required=True)
    for name in ('worktree_open', 'worktree_new', 'worktree_reach'):
        q = sub.add_parser(name)
        if name == 'worktree_reach':
            q.add_argument('task', nargs='?', help='Tâche à rejoindre ; sans argument, retour au terminal principal')
        else:
            q.add_argument('task')
        q.add_argument('--prepare-only', action='store_true')
        q.add_argument('--mode', choices=list(runtime.MODES), default=DEFAULT_MODE); q.add_argument('--result-file')
        if name in ('worktree_open', 'worktree_reach'):
            q.add_argument('--no-update', action='store_true', help=f'Ouvrir sans fetch ni fusion de {REMOTE}/{BASE_BRANCH}')
    q = sub.add_parser('worktree_close')
    q.add_argument('task', nargs='?', help='Tâche à fermer ; sans argument, le worktree courant ou tous les ateliers ouverts depuis le dépôt principal')
    q = sub.add_parser('worktree_switch')
    q.add_argument('task', nargs='?', help=f'Tâche à afficher ; sans argument, retour au terminal {BASE_BRANCH}')
    q = sub.add_parser('worktree_lance_master', help=f'Ouvrir la console et le serveur {BASE_BRANCH} dans deux onglets du terminal courant')
    q.add_argument('--mode', choices=list(runtime.MODES), default=None,
                   help='Mode serveur ; par défaut, conserver le mode actif, sinon http')
    q = sub.add_parser('_master_tab'); q.add_argument('role', choices=['console', 'server'])
    q = sub.add_parser('desc_git')
    q.add_argument('task', nargs='?', help='Tâche à décrire, avec ou sans worktree ; par défaut, le worktree courant')
    for name in ('worktree_status', 'worktree_port', 'worktree_open_current', 'put_branch_in_idea'):
        sub.add_parser(name)
    q = sub.add_parser('worktree_check_conflicts'); q.add_argument('paths', nargs='*'); q.add_argument('--against')
    q = sub.add_parser('patch'); q.add_argument('action', nargs='?', help='Sans argument : proposer une restauration ; sinon make, list, restore ou <nom_tâche> pour sauvegarder son worktree'); q.add_argument('file', nargs='?')
    q.add_argument('--yes', action='store_true'); q.add_argument('--include', action='append', default=[])
    q = sub.add_parser('is_any_patch')
    q.add_argument('task', nargs='?', help='Tâche existante ; sans argument, le worktree courant. done seul liste ses sauvegardes traitées.')
    q.add_argument('done', nargs='?', choices=['done'], help='Lister les sauvegardes déjà traitées de la tâche indiquée')
    q = sub.add_parser('commit'); q.add_argument('message'); q.add_argument('--no-verify', action='store_true')
    q = sub.add_parser('push'); q.add_argument('target', nargs='?', default=REMOTE); q.add_argument('--force-with-lease', action='store_true')
    for name in ('merge_to_master', 'merge_to_dev'):
        q = sub.add_parser(name); q.add_argument('days', nargs='?', type=int, default=7); q.add_argument('author', nargs='?')
        q.add_argument('target', nargs='?')
        refresh = q.add_mutually_exclusive_group()
        refresh.add_argument('--fetch', dest='fetch', action='store_true', help='Actualiser origin avant l’audit (par défaut)')
        refresh.add_argument('--no-fetch', dest='fetch', action='store_false', help='Utiliser les références locales sans accès réseau')
        q.set_defaults(fetch=True)
    q = sub.add_parser('worktree_docker'); q.add_argument('action', choices=['activate','status','stop','console','attach','shell','logs','restart'])
    q.add_argument('--mode', choices=list(runtime.MODES), default=DEFAULT_MODE); q.add_argument('--worktree'); q.add_argument('--infrastructure', action='store_true')
    for name in ('relance_serveur', 'relance_console', 'relance_docker'):
        q = sub.add_parser(name)
        q.add_argument('task', nargs='?', help='Tâche existante ; par défaut, le worktree courant')
        q.add_argument('--mode', choices=list(runtime.MODES), default=None)
    q = sub.add_parser('docker_stop_all'); q.add_argument('--infrastructure', action='store_true')
    q = sub.add_parser('vim_session'); q.add_argument('kind', nargs='?', choices=['doc'])
    q = sub.add_parser('relance_codex'); q.add_argument('--new-session', action='store_true'); q.add_argument('extra', nargs=argparse.REMAINDER)
    q = sub.add_parser('worktree_test'); q.add_argument('args', nargs=argparse.REMAINDER)
    q = sub.add_parser('_tab'); q.add_argument('root'); q.add_argument('role', choices=[x[0] for x in atelier.ROLES]); q.add_argument('generation')
    return p


def main():
    a = parser().parse_args()
    if a.command in ('worktree_open', 'worktree_new', 'worktree_reach'): return open_command(a)
    if a.command == '_tab': return atelier.tab(Path(a.root), a.role, a.generation)
    root, branch, task = context(getattr(a, 'worktree', None))
    if a.command == 'worktree_lance_master': return atelier.open_master(root, a.mode)
    if a.command == '_master_tab': return atelier.master_tab(root, a.role)
    if a.command == 'worktree_switch':
        return atelier.worktree_switch(existing_worktree(a.task) if a.task else None)
    if a.command == 'worktree_close':
        if not a.task and root == MAIN:
            return atelier.close_all()
        return atelier.close(existing_worktree(a.task) if a.task else root)
    if a.command in ('relance_serveur', 'relance_console', 'relance_docker'):
        target = existing_worktree(a.task) if a.task else root
        return atelier.relaunch_runtime(target, 'console' if a.command == 'relance_console' else 'server', a.mode)
    if a.command == 'worktree_status': return status_all()
    if a.command == 'worktree_port':
        state = runtime.read_state(root)
        # Le port conserve apres arret n'est pas un port utilise actuellement.
        if state.get('status') == 'active':
            print(state.get('port', MAIN_PORT if root == MAIN else 'port inconnu'))
        elif state.get('status') == 'activating':
            print('serveur en cours de démarrage')
        else:
            print('serveur arrêté')
        return
    if a.command == 'desc_git': return describe(root, a.task)
    if a.command == 'worktree_open_current': return atelier.open_atelier(root)
    if a.command == 'worktree_check_conflicts':
        paths = a.paths or patches.changed(root)
        found = collisions(root, paths)
        print('\n'.join(found) if found else 'Aucune collision locale détectée pour les chemins examinés.')
        if a.against:
            code = run(['git', '-C', root, 'merge-tree', '--write-tree', '--name-only', 'HEAD', a.against], check=False).returncode
            if code not in (0, 1): raise ToolError('Simulation de fusion en échec.')
            return code
        return 1 if found else 0
    if a.command == 'patch':
        if a.action is None:
            if a.yes or a.include:
                raise ToolError('Sans action, utilise patch.sh seul ; --yes exige restore <fichier> et --include exige make.')
            if patches.pending(root):
                patches.offer(root)
            else:
                print('Aucune sauvegarde en attente pour cette tâche.')
        elif a.action == 'make': patches.make(root, a.include)
        elif a.action == 'list': print('\n'.join(map(str, patches.pending(root))))
        elif a.action == 'restore':
            if not a.file: raise ToolError('Indique le fichier à restaurer.')
            patches.restore(root, a.file, a.yes)
        else:
            if a.file: raise ToolError('Aucun deuxième argument attendu après le nom de tâche.')
            patches.make(existing_worktree(a.action), a.include)
        return
    if a.command == 'is_any_patch':
        # Conserver la forme historique « is_any_patch.sh done ».
        if a.task == 'done':
            if a.done: raise ToolError('Usage : is_any_patch.sh [tâche] [done].')
            found = patches.pending(root, True)
        elif a.task:
            found = patches.pending(done=bool(a.done), task=a.task)
        else:
            found = patches.pending(root, bool(a.done))
        print('\n'.join(map(str, found)))
        return
    if a.command == 'commit': return commit(root, a.message, a.no_verify)
    if a.command == 'push': return push(root, a.target, a.force_with_lease)
    if a.command.startswith('merge_to_'):
        # Actualiser avant de choisir la cible : les refs connues peuvent etre obsoletes.
        if a.fetch: run(['git', '-C', root, 'fetch', '--prune', REMOTE])
        target = a.target
        if not target:
            candidates = [REMOTE + '/' + BASE_BRANCH, BASE_BRANCH] if a.command.endswith('master') else setting_json('AUDIT_TARGETS', [])
            target = next((c for c in candidates if git(root, 'rev-parse', '--verify', c, check=False).returncode == 0), None)
        if not target: raise ToolError('Aucune branche cible trouvée ; précise-la.')
        return audit(root, target, a.days, a.author)
    if a.command == 'worktree_docker':
        if a.action == 'status':
            runtime.status()
        elif a.action == 'activate': runtime.activate(root, a.mode)
        elif a.action == 'stop': runtime.stop(root, a.infrastructure)
        else: return run(runtime.service_command(root, a.action, CONSOLE_SERVICE if a.action == 'console' else None), cwd=root).returncode
        return
    if a.command == 'docker_stop_all': return runtime.stop_all(root, a.infrastructure)
    if a.command == 'vim_session':
        cmd = atelier.vim_command(root, 'vim_doc' if a.kind else 'vim')
        if not cmd: print('Aucune session Vim existante.'); return
        return run(cmd, cwd=root).returncode
    if a.command == 'relance_codex':
        sessions.prepare(root)
        extra = a.extra[1:] if a.extra[:1] == ['--'] else a.extra
        cmd = sessions.codex_command(root, extra, a.new_session)
        return run(cmd, cwd=root, env=sessions.codex_environment(cmd[0])).returncode
    if a.command == 'worktree_test': return test_run(root, a.args)
    if a.command == 'put_branch_in_idea':
        dest = root / setting('BRANCH_MARKER', '.idea')
        if dest.is_dir(): raise ToolError('.idea est un répertoire IDE ; aucun écrasement.')
        dest.write_text(branch + '\n'); print(dest)


if __name__ == '__main__':
    try:
        sys.exit(main() or 0)
    except (ToolError, OSError, ValueError, subprocess.SubprocessError) as exc:
        print('ERREUR : %s' % exc, file=sys.stderr)
        sys.exit(1)
