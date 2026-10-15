#!/usr/bin/env bash
set -euo pipefail
# Ancien nom conservé ; la commande s'appelle maintenant relance_serveur.sh.
tools_dir="$(dirname -- "$(readlink -f -- "${BASH_SOURCE[0]}")")"
exec "$tools_dir/relance_serveur.sh" "$@"
