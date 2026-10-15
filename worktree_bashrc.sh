# Shell de secours d’un onglet géré, lorsque la session Vim n’existe pas.
[[ ! -f "$HOME/.bashrc" ]] || source "$HOME/.bashrc"
# Un ancien superviseur peut encore transmettre le préfixe historique.
if [[ -z "${WT_SHELL_CLOSE:-}" ]]; then
  _worktree_config_dir="$(dirname -- "$(readlink -f -- "${BASH_SOURCE[0]}")")"
  WT_SHELL_CLOSE="$(python3 "$_worktree_config_dir/_wt_config.py" --session-env SHELL_CLOSE)"
fi
_worktree_close_idle_shell() {
  if [[ -f "${WT_SHELL_CLOSE:-/nonexistent}" ]]; then
    # Inclure aussi les tâches suspendues : leur travail doit être conservé.
    if [[ -z "$(jobs -p)" ]]; then
      exit 0
    fi
    echo 'Des tâches tournent encore dans cet onglet ; fermeture suspendue.'
  fi
}
trap _worktree_close_idle_shell USR1
