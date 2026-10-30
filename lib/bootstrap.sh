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

# nano is the editor used by ./update.sh vault-edit.
ensure_nano() {
  command -v nano >/dev/null 2>&1 && return 0
  echo "Installing nano ..."
  sudo pacman -S --needed --noconfirm nano
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

# First run: copy the example inventory to the git-ignored local one and stop.
ensure_local_inventory() {
  local src="$REPO/ansible/inventories/example"
  local dst="$REPO/ansible/inventories/local"
  [ -d "$dst" ] && return 0
  cp -r "$src" "$dst"
  mv "$dst/group_vars/all/vault.yml.example" "$dst/group_vars/all/vault.yml"
  echo "Created ansible/inventories/local."
  echo "Edit it, encrypt vault.yml (./update.sh vault-edit), then run ./update.sh again."
  exit 0
}
