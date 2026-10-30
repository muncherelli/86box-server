#!/usr/bin/env bash
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export REPO
export ANSIBLE_CONFIG="$REPO/ansible/ansible.cfg"

die() { echo "error: $*" >&2; exit 1; }

# shellcheck source=lib/bootstrap.sh
source "$REPO/lib/bootstrap.sh"
# shellcheck source=lib/log.sh
source "$REPO/lib/log.sh"
# shellcheck source=lib/vault.sh
source "$REPO/lib/vault.sh"

ensure_local_inventory
ensure_ansible
ensure_collections
if [ "${1:-}" = vault-edit ]; then
  vault_edit
  exit 0
fi

set_become_args
setup_vault_password
check_vault
setup_log
echo "not implemented yet"
echo "log: $LOG"
