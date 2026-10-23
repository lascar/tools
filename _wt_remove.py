"""Suppression ciblée d'un worktree fermé, avec archive locale vérifiée."""
import argparse
import hashlib
import os
from pathlib import Path
import shutil
import stat
import sys
import tarfile
import tempfile
from _wt_common import *
import _wt_atelier as atelier
import _wt_docker as runtime


def target(task):
    branch = task_branch(task)
    task = branch.split('/', 1)[1]
    root = MAIN.parent / (setting('WORKTREE_PREFIX', PROJECT_NAME + '-') + task)
    if root.is_symlink() or not root.is_dir() or root.resolve() != root:
        raise ToolError('Dossier absent ou lien symbolique refusé : %s' % root)
    if Path.cwd().resolve().is_relative_to(root):
        raise ToolError('Lance worktree_remove.sh depuis le dépôt principal, hors du dossier à supprimer.')
    rows = worktrees()
    matches = [r for r in rows if Path(r['worktree']) == root]
    if any(r.get('branch') == 'refs/heads/' + branch and Path(r['worktree']) != root for r in rows):
        raise ToolError('La branche utilise un autre dossier ; suppression refusée.')
    if matches:
        row = matches[0]
        if row.get('branch') != 'refs/heads/' + branch or 'locked' in row or 'prunable' in row:
            raise ToolError('Association Git incohérente ou worktree verrouillé.')
        context(root, task=True)
    else:
        # Un nom de dossier seul ne prouve pas qu'il s'agit d'un ancien worktree.
        marker = root / '.git'
        common = Path(value(MAIN, 'rev-parse', '--path-format=absolute', '--git-common-dir'))
        if marker.is_symlink() or not marker.is_file():
            raise ToolError('Dossier résiduel sans marqueur Git vérifiable ; suppression refusée.')
        text = marker.read_text().strip()
        gitdir = Path(text[8:]) if text.startswith('gitdir: ') else Path('/')
        if not gitdir.is_absolute() or gitdir.parent.resolve() != (common / 'worktrees').resolve() or gitdir.exists():
            raise ToolError('Le dossier ne correspond pas à un worktree désenregistré de ce dépôt.')
    sha = value(MAIN, 'rev-parse', '--verify', 'refs/heads/' + branch)
    return root, branch, sha, bool(matches)


def process_directory(proc):
    try:
        return (proc / 'cwd').resolve(strict=True)
    except PermissionError:
        # Certains processus du même UID sont protégés (non dumpables).
        # Ne pas les ignorer : limiter l'élévation à la lecture de leur cwd.
        print('Processus %s protégé : vérification de son dossier avec sudo.' % proc.name, flush=True)
        result = run(['sudo', 'readlink', '-e', '--', proc / 'cwd'], capture=True, check=False)
        if result.returncode == 0 and result.stdout.strip().startswith('/'):
            return Path(result.stdout.strip())
        if not proc.exists():
            return None
        raise ToolError('Impossible de vérifier le dossier du processus %s avec sudo : %s' %
                        (proc.name, result.stderr.strip() or 'lecture refusée'))


def inactive(root):
    folder = atelier.directory(root)
    for role, _ in atelier.ROLES:
        record = read_json(folder / (role + '.json'), {})
        if atelier.alive(record) or atelier.alive(record, 'child_'):
            raise ToolError('Atelier encore ouvert : lance worktree_close.sh avant la suppression.')
    if any(path == root for _, path, _, _ in runtime.app_mounts(True)):
        raise ToolError('Un conteneur applicatif utilise encore ce worktree.')
    for proc in Path('/proc').iterdir():
        if not proc.name.isdigit(): continue
        try:
            # Les autres utilisateurs ne sont pas inspectables ; Docker est vérifié séparément.
            if proc.stat().st_uid != os.getuid(): continue
            cwd = process_directory(proc)
            if cwd is not None and cwd.is_relative_to(root):
                raise ToolError('Le processus %s utilise encore ce dossier ; ferme son terminal ou application.' % proc.name)
        except (FileNotFoundError, ProcessLookupError):
            continue
        except PermissionError as error:
            raise ToolError('Impossible de vérifier un processus : %s' % error)


def clean(root):
    if value(root, 'status', '--porcelain', '--untracked-files=all'):
        raise ToolError('Modifications Git ou fichiers non suivis : sauvegarde-les avant de supprimer le worktree.')
    flags = git(root, 'ls-files', '-v', '-z').stdout.split('\0')
    if any(line and (line[0].islower() or line[0] == 'S') for line in flags):
        raise ToolError('Indicateurs assume-unchanged/skip-worktree présents ; suppression refusée.')


def digest(stream):
    return hashlib.file_digest(stream, 'sha256').hexdigest()


def inventory(root):
    entries = {}
    device = root.stat().st_dev
    def visit(path):
        info = path.lstat()
        name = str(path.relative_to(root))
        if stat.S_ISLNK(info.st_mode):
            entries[name] = ['link', os.readlink(path)]
        elif stat.S_ISDIR(info.st_mode):
            if info.st_dev != device or (path != root and os.path.ismount(path)):
                raise ToolError('Montage imbriqué refusé : %s' % path)
            entries[name] = ['dir', stat.S_IMODE(info.st_mode)]
            for child in sorted(path.iterdir()): visit(child)
        elif stat.S_ISREG(info.st_mode):
            with path.open('rb') as stream: checksum = digest(stream)
            entries[name] = ['file', info.st_size, checksum, stat.S_IMODE(info.st_mode)]
        else:
            raise ToolError('Fichier spécial à traiter avant suppression : %s' % path)
    visit(root)
    return entries


def backup(root, branch, sha):
    base = MAIN / setting('DOCUMENTS_DIR', 'doc') / 'worktree-backups'
    base.mkdir(parents=True, exist_ok=True)
    dest = Path(tempfile.mkdtemp(prefix=root.name + '-', dir=base))
    before = inventory(root)
    archive = dest / 'worktree.tar'
    # Tout le contenu est conservé, y compris les secrets locaux ; dossier privé 0700.
    with tarfile.open(archive, 'w', dereference=False) as tar:
        for name in before: tar.add(root / name, arcname=name, recursive=False)
    with tarfile.open(archive, 'r') as tar:
        checked = {}
        for member in tar:
            if member.isdir(): checked[member.name] = ['dir', member.mode]
            elif member.issym(): checked[member.name] = ['link', member.linkname]
            elif member.isfile():
                with tar.extractfile(member) as stream: checksum = digest(stream)
                checked[member.name] = ['file', member.size, checksum, member.mode]
            else: raise ToolError('Entrée archive non prise en charge : %s' % member.name)
    if checked != before or inventory(root) != before:
        raise ToolError('Contenu modifié pendant la sauvegarde ou archive incohérente ; aucune suppression.')
    atomic(dest / 'manifest.json', {'worktree': str(root), 'branch': branch, 'sha': sha,
                                  'created_at': now(), 'entries': before})
    print('Sauvegarde vérifiée : %s' % archive, flush=True)
    return before


def remove(task, guard=None, create_backup=True):
    context()
    with locked(STATE / 'worktree-create.lock'):
        root, branch, sha, registered = target(task)
        with locked(atelier.directory(root) / 'atelier.lock'), locked(STATE / 'runtime.lock'):
            # Les suppressions collectives revalident leur critère sous les verrous.
            if guard: guard(root, branch)
            inactive(root)
            if registered: clean(root)
            identity = (root.stat().st_dev, root.stat().st_ino)
            # Le prune sans archive conserve les controles de stabilite du contenu.
            saved = backup(root, branch, sha) if create_backup else inventory(root)
            if target(task) != (root, branch, sha, registered) or inventory(root) != saved:
                raise ToolError('La cible a changé ; suppression annulée.')
            inactive(root)
            if guard: guard(root, branch)
            if registered:
                clean(root)
                result = git(MAIN, 'worktree', 'remove', str(root), check=False)
                if result.returncode and any(Path(r['worktree']) == root for r in worktrees()):
                    raise ToolError('Git refuse la suppression : ' + result.stderr)
            if root.exists():
                if root.is_symlink() or (root.stat().st_dev, root.stat().st_ino) != identity:
                    raise ToolError('Le dossier a été remplacé ; nettoyage annulé.')
                try:
                    shutil.rmtree(root)
                except PermissionError:
                    # Git peut avoir désenregistré le worktree avant son échec de suppression.
                    if root.is_symlink() or (root.stat().st_dev, root.stat().st_ino) != identity:
                        raise ToolError('Le dossier a été remplacé ; nettoyage privilégié annulé.')
                    print('Permissions insuffisantes : sudo est requis pour supprimer ce dossier uniquement.', flush=True)
                    run(['sudo', 'rm', '-r', '--', root])
            if root.exists() or any(Path(r['worktree']) == root for r in worktrees()):
                raise ToolError('Suppression incomplète.' + (' La sauvegarde est conservée.' if create_backup else ''))
            print('Worktree supprimé ; branche conservée : %s' % branch)


def main():
    parser = argparse.ArgumentParser(prog='worktree_remove.sh', description=__doc__)
    parser.add_argument('task', help='Nom de tâche, sans feature/ ni chemin')
    args = parser.parse_args()
    try:
        remove(args.task)
    except (ToolError, OSError) as error:
        print('ERREUR : %s' % error, file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
