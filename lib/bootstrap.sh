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
    -p "$INSTALL_DIR/collections" >/dev/null
}

# Sets BECOME_ARGS (used by lib/ui.sh): ask for the sudo password only when sudo needs one.
# shellcheck disable=SC2034
set_become_args() {
  BECOME_ARGS=()
  if [ "$(id -u)" -ne 0 ] && ! sudo -n true 2>/dev/null; then
    BECOME_ARGS=(--ask-become-pass)
  fi
}

# /opt/86server/install belongs to the person who runs ./update.sh (mode 700): settings and secrets.
ensure_install_dir() {
  if [ ! -d "$INSTALL_DIR" ] || [ ! -O "$INSTALL_DIR" ]; then
    sudo install -d -o root -g root -m 0755 /opt/86server
    sudo install -d -o "$(id -u)" -g "$(id -g)" -m 0700 "$INSTALL_DIR"
  fi
}

# Older checkouts kept the inventory in the repo and the vault password in ~/.config: move them once.
migrate_install_state() {
  local old_inv="$REPO/ansible/inventories/local" old_pass="$HOME/.config/86server/vault-pass"
  if [ -d "$old_inv" ] && [ ! -e "$INSTALL_DIR/inventory" ]; then
    mv "$old_inv" "$INSTALL_DIR/inventory"
    echo "Moved ansible/inventories/local to $INSTALL_DIR/inventory"
  fi
  if [ -z "${B86_VAULT_PASS_FILE:-}" ] && [ -f "$old_pass" ] && [ ! -e "$INSTALL_DIR/vault-pass" ]; then
    mv "$old_pass" "$INSTALL_DIR/vault-pass"
    rmdir "$(dirname "$old_pass")" 2>/dev/null || true
    echo "Moved the vault password to $INSTALL_DIR/vault-pass"
  fi
  rm -rf "$REPO/ansible/collections" "${XDG_STATE_HOME:-$HOME/.local/state}/86server"
}

# First run: copy the example inventory to $INSTALL_DIR/inventory and stop.
ensure_local_inventory() {
  local src="$REPO/ansible/inventories/example"
  local dst="$INSTALL_DIR/inventory"
  [ -d "$dst" ] && return 0
  cp -r "$src" "$dst"
  mv "$dst/group_vars/all/vault.yml.example" "$dst/group_vars/all/vault.yml"
  echo "Created $dst."
  echo "Edit it, encrypt vault.yml (./update.sh vault-edit), then run ./update.sh again."
  exit 0
}
