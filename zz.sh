#!/usr/bin/env bash
set -euo pipefail

# Lanceur personnel : le dépôt principal reste toujours sur master.
project_dir=/home/pascal/development/bo.facile
tools_dir="$project_dir/doc/tools"
self="$tools_dir/zz.sh"

fail() { printf 'Erreur : %s\n' "$*" >&2; exit 1; }

if [[ ${1:-} == --help || ${1:-} == -h ]]; then
    printf 'Usage : zz.sh [--shell | --console | --server]\n'
    printf 'Exige que tous les ateliers de worktree soient fermés (hors master).\n'
    printf 'Actualise master, arrête les conteneurs du projet, redémarre Docker et ouvre quatre onglets GNOME : Codex, Docker, merge_to_master.sh et le rapport des tâches.\n'
    printf 'Le rapport démarre automatiquement dans une nouvelle session Codex, en dernier (sixième onglet si deux sont déjà ouverts).\n'
    exit 0
fi

cd -- "$project_dir"
branch=$(git branch --show-current)
[[ $branch == master ]] || fail "Le dépôt est sur ${branch:-HEAD détachée}, master est requis. Aucune branche changée."

# Les onglets revalident eux aussi la branche ; aucun pull dans les enfants.
if [[ ${1:-} == --_tab ]]; then
    [[ $# == 2 ]] || fail 'Rôle d’onglet manquant.'
    case $2 in
        codex)
            # Le shell interactif charge NVM. La mise à jour reste dans cet
            # onglet et ne bloque pas l’ouverture des autres.
            exec bash -ic '
                codex --add-dir "$1/doc" --add-dir "$1/sessions_codex" \
                    "Lis AGENTS.md puis AGENTS-pascal.md. Reprends le journal central $1/sessions_codex/master.jsont et tiens-le à jour."
                result=$?
                if (( result != 0 )); then printf "Codex terminé avec le code %s.\n" "$result" >&2; fi
                exec bash -i
            ' bash "$project_dir"
            ;;
        report)
            [[ -r "$project_dir/doc/build_rapport_taches.txt" ]] || fail 'Prompt du rapport introuvable : doc/build_rapport_taches.txt'
            # Un lancement sans resume ni fork cree une session dediee au rapport.
            # Le shell interactif charge NVM comme dans l'onglet Codex principal.
            exec bash -ic '
                codex --add-dir "$1/doc" --add-dir "$1/sessions_codex" \
                    "Cette nouvelle session est exclusivement consacrée au rapport de mes tâches Incwo. Lis et exécute les instructions de $1/doc/build_rapport_taches.txt."
                result=$?
                if (( result != 0 )); then printf "Rapport Codex terminé avec le code %s.\n" "$result" >&2; fi
                exec bash -i
            ' bash "$project_dir"
            ;;
        console|server)
            result=0
            python3 "$tools_dir/worktree_tools.py" _master_tab "$2" || result=$?
            if (( result != 0 )); then printf 'Connexion Docker terminée avec le code %s.\n' "$result" >&2; fi
            exec bash -i
            ;;
        shell)
            result=0
            bash "$tools_dir/worktree_docker.sh" shell || result=$?
            if (( result != 0 )); then printf 'Shell Docker terminé avec le code %s.\n' "$result" >&2; fi
            exec bash -i
            ;;
        merge)
            result=0
            bash "$tools_dir/merge_to_master.sh" || result=$?
            if (( result != 0 )); then printf 'merge_to_master.sh terminé avec le code %s.\n' "$result" >&2; fi
            exec bash -i
            ;;
        *) fail 'Rôle d’onglet inconnu.' ;;
    esac
fi

role=shell
case ${1:-} in
    --server) role=server ;;
    --console) role=console ;;
    ''|--shell) ;;
    *) fail 'Utilise zz.sh --help.' ;;
esac
(( $# <= 1 )) || fail 'Trop d’arguments.'

for binary in git python3 docker gnome-terminal; do
    command -v "$binary" >/dev/null || fail "Commande introuvable : $binary"
done
[[ -r "$project_dir/doc/build_rapport_taches.txt" ]] || fail 'Prompt du rapport introuvable : doc/build_rapport_taches.txt'
[[ -n ${DISPLAY:-}${WAYLAND_DISPLAY:-} ]] || fail 'Une session graphique est nécessaire.'

printf 'Vérification de la fermeture des worktrees…\n'
python3 - "$tools_dir" <<'PY'
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from _wt_common import MAIN, worktrees
from _wt_atelier import open_roles

# Utiliser la meme detection des onglets vivants que worktree_status.sh.
opened = []
for row in worktrees():
    root = Path(row['worktree'])
    if root != MAIN and open_roles(root):
        opened.append((root, row.get('branch', 'HEAD détachée')))
if opened:
    print('Erreur : ferme les ateliers suivants avant de relancer zz.sh :', file=sys.stderr)
    for root, branch in opened:
        print('  %s — %s\n    %s' % (root.name, branch, root), file=sys.stderr)
    sys.exit(1)
PY

printf 'Actualisation de master…\n'
# Refuser une fusion implicite si master et sa branche distante divergent.
git pull --ff-only

printf 'Arrêt des conteneurs du projet…\n'
bash "$tools_dir/docker_stop_all.sh"

printf 'Démarrage des services Docker fondamentaux et du runtime master…\n'
python3 - "$tools_dir" "$project_dir" "$role" <<'PY'
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import _wt_docker as runtime
runtime.ensure_service(Path(sys.argv[2]), 'console' if sys.argv[3] == 'shell' else sys.argv[3])
PY

# Une seule requête GNOME groupe les onglets sans attendre leurs processus.
# Depuis GNOME, ajouter à la fenêtre appelante ; sinon ouvrir une fenêtre.
terminal=(gnome-terminal)
if [[ -n ${GNOME_TERMINAL_SCREEN:-} ]]; then
    terminal+=(--tab)
else
    terminal+=(--window)
fi
terminal+=(--title='bo.facile/master' --working-directory="$project_dir" --command="bash '$self' --_tab codex"
    --tab --title='bo.facile/master' --working-directory="$project_dir" --command="bash '$self' --_tab $role"
    --tab --title='bo.facile/master' --working-directory="$project_dir" --command="bash '$self' --_tab merge"
    --tab --title='bo.facile/rapport tâches' --working-directory="$project_dir" --command="bash '$self' --_tab report")
"${terminal[@]}"
