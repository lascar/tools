#!/usr/bin/env bash
set -euo pipefail

if [[ $# -eq 1 && "$1" == "--help" ]]; then
    printf '%s\n' 'Usage : pull.sh' 'Exécute git pull --ff-only dans le répertoire courant.'
    exit 0
fi
if [[ $# -ne 0 ]]; then
    printf '%s\n' 'Usage : pull.sh (sans argument, ou --help)' >&2
    exit 2
fi

# Conserver le répertoire appelant pour viser son dépôt ou worktree.
exec git pull --ff-only
