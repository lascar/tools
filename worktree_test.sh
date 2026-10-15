#!/usr/bin/env bash
set -euo pipefail
# Le contexte vient du répertoire appelant, pas du dépôt imbriqué des outils.
tools_dir="$(dirname -- "$(readlink -f -- "${BASH_SOURCE[0]}")")"
exec python3 "$tools_dir/worktree_tools.py" worktree_test "$@"
