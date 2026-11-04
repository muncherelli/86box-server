# shellcheck shell=bash
# Makes sure Ansible and the pinned collections are installed.

ensure_ansible() {
  command -v ansible-playbook >/dev/null 2>&1 && return 0
  local id
  # shellcheck disable=SC1091
  id=$(. /etc/os-release && echo "${ID:-unknown}")
  case "$id" in
    arch | archarm) ;;
    *) die "Unsupported distro: $id" ;;
  esac
  echo "Installing ansible-core ..."
  sudo pacman -S --needed --noconfirm ansible-core python
}

# nano is the default editor for 86server settings and secrets.
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
    # The web manager's Update button runs this with nobody to type a password.
    [ "${B86_NONINTERACTIVE:-0}" != 1 ] ||
      die "sudo asks $(id -un) for a password, so the update cannot run unattended. Run 86server update in a terminal, or let $(id -un) use sudo without a password."
    BECOME_ARGS=(--ask-become-pass)
  fi
}

# One update at a time: a manual run and the web manager's Update button must not overlap.
lock_update() {
  exec 9>"$LOG_DIR/.update.lock"
  flock -n 9 || die "Another 86server update is running."
}

# Tells Ansible which clone and commit it is deploying, for the web manager's update notice (role selfupdate).
# The files come from the working tree, so uncommitted changes are recorded as dirty.
deploy_args() {
  local commit dirty=false subject branch info
  commit=$(git -C "$REPO" rev-parse HEAD 2>/dev/null || true)
  if [ -n "$(git -C "$REPO" status --porcelain --untracked-files=no 2>/dev/null)" ]; then dirty=true; fi
  subject=$(git -C "$REPO" log -1 --format=%s 2>/dev/null || true)
  branch=$(git -C "$REPO" rev-parse --abbrev-ref HEAD 2>/dev/null || true)
  info=$(python3 -c 'import json,sys; print(json.dumps({"b86_repo": sys.argv[1], "b86_repo_owner": sys.argv[2],
    "b86_deploy": {"commit": sys.argv[3], "short": sys.argv[3][:7], "dirty": sys.argv[4] == "true",
                   "subject": sys.argv[5], "branch": sys.argv[6], "by": sys.argv[2]}}))' \
    "$REPO" "$(id -un)" "$commit" "$dirty" "$subject" "$branch")
  DEPLOY_ARGS=(-e "$info")
}

# The inventory (settings, mode 700), the install logs and the Ansible collections belong to
# the person who runs 86server, so it can write them without sudo.
ensure_install_dirs() {
  local d
  for d in "$INVENTORY_DIR" "$LOG_DIR" "$COLLECTIONS_DIR"; do
    [ -d "$d" ] && [ -O "$d" ] && continue
    sudo install -d -o root -g root -m 0755 /opt/86server /opt/86server/cache
    sudo install -d -o "$(id -u)" -g "$(id -g)" -m "$([ "$d" = "$INVENTORY_DIR" ] && echo 0700 || echo 0755)" "$d"
  done
}

# First run: copy the example settings to $INVENTORY_DIR. They work as they are; change them later.
ensure_local_inventory() {
  local src="$REPO/ansible/inventories/example"
  local dst="$INVENTORY_DIR"
  [ -f "$dst/hosts.yml" ] && return 0
  cp -r "$src/." "$dst/"
  echo "Created $dst with the default settings (change them with: 86server settings)."
}

settings_edit() {
  "$EDITOR" "$INVENTORY_DIR/group_vars/all/main.yml"
  echo "Run 86server update to apply the settings."
}

# Puts the 86server command on the PATH: /usr/local/bin/86server -> this clone.
link_command() {
  local link=/usr/local/bin/86server
  [ "$(readlink "$link" 2>/dev/null)" = "$REPO/86server" ] && return 0
  sudo ln -sfn "$REPO/86server" "$link"
  echo "Linked $link -> $REPO/86server"
}
