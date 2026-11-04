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

# nano is the editor used by ./update.sh secrets-edit.
ensure_nano() {
  command -v nano >/dev/null 2>&1 && return 0
  echo "Installing nano ..."
  sudo pacman -S --needed --noconfirm nano
}

ensure_collections() {
  ansible-galaxy collection install \
    -r "$REPO/ansible/requirements.yml" \
    -p "$COLLECTIONS_DIR" >/dev/null
}

# Sets BECOME_ARGS (used by lib/ui.sh): ask for the sudo password only when sudo needs one.
# shellcheck disable=SC2034
set_become_args() {
  BECOME_ARGS=()
  if [ "$(id -u)" -ne 0 ] && ! sudo -n true 2>/dev/null; then
    BECOME_ARGS=(--ask-become-pass)
  fi
}

# The inventory (settings, mode 700), the install logs and the Ansible collections belong to
# the person who runs ./update.sh, so it can write them without sudo.
ensure_install_dirs() {
  local d
  for d in "$INVENTORY_DIR" "$LOG_DIR" "$COLLECTIONS_DIR"; do
    [ -d "$d" ] && [ -O "$d" ] && continue
    sudo install -d -o root -g root -m 0755 /opt/86server /opt/86server/cache
    sudo install -d -o "$(id -u)" -g "$(id -g)" -m "$([ "$d" = "$INVENTORY_DIR" ] && echo 0700 || echo 0755)" "$d"
  done
}

# First run: copy the example inventory to $INVENTORY_DIR and stop.
ensure_local_inventory() {
  local src="$REPO/ansible/inventories/example"
  local dst="$INVENTORY_DIR"
  [ -f "$dst/hosts.yml" ] && return 0
  cp -r "$src/." "$dst/"
  echo "Created $dst."
  echo "Edit it (and secrets with ./update.sh secrets-edit), then run ./update.sh again."
  exit 0
}
