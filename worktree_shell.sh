# À charger avec : source /chemin/vers/tools/worktree_shell.sh
# Ouvrir la fenêtre de tâche sans déplacer le terminal d'origine.
_worktree_tools_dir="$(dirname -- "$(readlink -f -- "${BASH_SOURCE[0]}")")"
_worktree_open() {
  local command_name="$1"
  shift
  "$_worktree_tools_dir/$command_name.sh" "$@"
}
# Retirer les anciennes fonctions des terminaux qui rechargent ce fichier.
unset -f change_branch.sh make_new_branch.sh change_worktree.sh new_worktree.sh close_worktree.sh
worktree_open.sh() { _worktree_open worktree_open "$@"; }
worktree_new.sh() { _worktree_open worktree_new "$@"; }
worktree_reach.sh() { _worktree_open worktree_reach "$@"; }
worktree_close.sh() {
  "$_worktree_tools_dir/worktree_close.sh" "$@" || return
  local main_root
  main_root="$(python3 "$_worktree_tools_dir/_wt_config.py" --main-root)" || return
  cd -- "$main_root"
}

# Le Python prépare la cible avant de déplacer ce shell ; en cas de conflit,
# rester dans la cible pour permettre sa résolution manuelle.
change_branch.sh() {
  local target
  if [[ "${1:-}" == --help || "${1:-}" == -h ]]; then
    "$_worktree_tools_dir/change_branch.sh" --help
    return
  fi
  if [[ $# == 0 ]]; then
    target="$(python3 "$_worktree_tools_dir/change_branch.py" home)" || return
    cd -- "$target"
    return
  fi
  if [[ $# != 1 ]]; then
    echo 'Usage : change_branch.sh [numéro_nom | t_numéro_nom]' >&2
    return 2
  fi
  target="$(python3 "$_worktree_tools_dir/change_branch.py" select "$1")" || return
  if [[ -n "$target" ]]; then
    cd -- "$target"
    return
  fi
  target="$(python3 "$_worktree_tools_dir/change_branch.py" prepare "$1")" || return
  cd -- "$target" || return
  python3 "$_worktree_tools_dir/change_branch.py" merge "$1"
}
