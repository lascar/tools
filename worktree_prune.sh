#!/usr/bin/env bash
set -euo pipefail
tools_dir="$(dirname -- "$(readlink -f -- "${BASH_SOURCE[0]}")")"
exec "$tools_dir/worktree_prune" "$@"
