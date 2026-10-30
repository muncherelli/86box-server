# shellcheck shell=bash
# Makes sure Ansible and the pinned collections are installed.

ensure_ansible() {
  command -v ansible-playbook >/dev/null 2>&1 && return 0
  local id
  # shellcheck disable=SC1091
  id=$(. /etc/os-release && echo "${ID:-unknown}")
  [ "$id" = arch ] || die "Unsupported distro: $id"
  echo "Installing ansible-core ..."
  sudo pacman -S --needed --noconfirm ansible-core python
}

ensure_collections() {
  ansible-galaxy collection install \
    -r "$REPO/ansible/requirements.yml" \
    -p "$REPO/ansible/collections" >/dev/null
}

# Sets BECOME_ARGS (used by lib/ui.sh): ask for the sudo password only when sudo needs one.
# shellcheck disable=SC2034
set_become_args() {
  BECOME_ARGS=()
  if [ "$(id -u)" -ne 0 ] && ! sudo -n true 2>/dev/null; then
    BECOME_ARGS=(--ask-become-pass)
  fi
}
