#!/usr/bin/env bash
# Un exécutable ne peut pas déplacer le shell appelant : la fonction est requise.
tools_dir="$(dirname -- "$(readlink -f -- "${BASH_SOURCE[0]}")")"
if [[ "${1:-}" == --help || "${1:-}" == -h ]]; then
  echo 'Usage : change_branch.sh [numéro_nom | t_numéro_nom]'
  echo 'Sans argument : revenir dans le dépôt principal sur sa branche de référence.'
  echo 'Contrôles → actualisation de la branche de référence → cd → fusion.'
  printf 'Charge la fonction : source %q/worktree_shell.sh\n' "$tools_dir"
  exit 0
fi
echo 'Recharge les fonctions pour permettre le cd dans ton terminal :' >&2
printf 'source %q/worktree_shell.sh\n' "$tools_dir" >&2
exit 2
