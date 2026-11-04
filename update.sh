#!/usr/bin/env bash
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export REPO
export ANSIBLE_CONFIG="$REPO/ansible/ansible.cfg"
# This host's settings (inventory), install logs and Ansible collections.
# Kept outside the repo, so a fresh clone (or a different checkout) installs with the same settings.
INVENTORY_DIR=/opt/86server/inventory
LOG_DIR=/opt/86server/log
COLLECTIONS_DIR=/opt/86server/cache/ansible-collections
export INVENTORY_DIR LOG_DIR COLLECTIONS_DIR

die() { echo "error: $*" >&2; exit 1; }

# shellcheck source=lib/bootstrap.sh
source "$REPO/lib/bootstrap.sh"
# shellcheck source=lib/ui.sh
source "$REPO/lib/ui.sh"
# shellcheck source=lib/preflight.sh
source "$REPO/lib/preflight.sh"
# shellcheck source=lib/log.sh
source "$REPO/lib/log.sh"
# shellcheck source=lib/secrets.sh
source "$REPO/lib/secrets.sh"
# shellcheck source=lib/status.sh
source "$REPO/lib/status.sh"
# shellcheck source=lib/dev.sh
source "$REPO/lib/dev.sh"
# shellcheck source=lib/admin.sh
source "$REPO/lib/admin.sh"

VERBOSE=0
ARGS=()
for a in "$@"; do
  case "$a" in
    --verbose) VERBOSE=1 ;;
    *) ARGS+=("$a") ;;
  esac
done

case "${ARGS[0]:-}" in
  vm|box)
    sudo -u 86server python3 /opt/86server/manager/cli.py "${ARGS[@]:1}"
    exit $?
    ;;
  status)
    show_status
    exit 0
    ;;
  dev)
    run_dev
    exit 0
    ;;
esac

preflight_basic
ensure_install_dirs
ensure_secrets
ensure_local_inventory
ensure_ansible
ensure_nano
export EDITOR=nano
ensure_collections
if [ "${ARGS[0]:-}" = secrets-edit ]; then
  secrets_edit
  exit 0
fi
if [ "${ARGS[0]:-}" = admin-password ]; then
  admin_password "${ARGS[@]:1}"
  exit 0
fi

check_secrets
copy_secrets       # asks for the sudo password if needed, so Ansible can usually skip asking
set_become_args
setup_log
run_playbook "${ARGS[@]}"
