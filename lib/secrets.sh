# shellcheck shell=bash
# This host's secrets: one plain YAML file owned by root, mode 600, so reading or editing it takes sudo.
# Ansible gets a copy for the length of a run, in a private memory-only folder, deleted on exit.

SECRETS_FILE=/opt/86server/secrets.yml
SECRETS_COPY=""

# First run: create the secrets file from the example, with a random admin password.
ensure_secrets() {
  sudo test -f "$SECRETS_FILE" && return 0
  local pw
  pw=$(head -c 15 /dev/urandom | base64 | tr '+/' '-_')
  sudo install -o root -g root -m 0600 /dev/null "$SECRETS_FILE"
  sed "s|^secret_admin_password:.*|secret_admin_password: '$pw'|" "$REPO/ansible/secrets.example.yml" |
    sudo tee "$SECRETS_FILE" >/dev/null
  echo "Created $SECRETS_FILE (root only) with a random admin password."
}

# Refuses to run when the secrets file is readable by others or secrets sit in the inventory.
check_secrets() {
  sudo test -f "$SECRETS_FILE" || die "Missing $SECRETS_FILE (run 86server update once first)"
  [ "$(sudo stat -c '%U:%G %a' "$SECRETS_FILE")" = "root:root 600" ] ||
    die "$SECRETS_FILE must be root:root, mode 600: sudo chown root:root $SECRETS_FILE && sudo chmod 600 $SECRETS_FILE"
  local bad
  bad=$(grep -rlE '^\s*(secret|vault)_[a-z0-9_]+:' "$INVENTORY_DIR" --include='*.yml' || true)
  [ -z "$bad" ] || die "Secrets found in the inventory: $bad. Move them into $SECRETS_FILE (86server secrets), then delete them there."
}

# 86server secrets
secrets_edit() {
  ensure_secrets
  sudoedit "$SECRETS_FILE"
}

# Sets SECRETS_ARGS for ansible-playbook. remove_secrets_copy deletes the copy on exit.
copy_secrets() {
  local dir="${XDG_RUNTIME_DIR:-}"
  { [ -n "$dir" ] && [ -d "$dir" ] && [ -O "$dir" ]; } || dir=/dev/shm
  SECRETS_COPY=$(mktemp "$dir/86server-secrets.XXXXXX")
  trap remove_secrets_copy EXIT
  sudo cat "$SECRETS_FILE" >"$SECRETS_COPY" || die "Could not read $SECRETS_FILE"
  SECRETS_ARGS=(-e "@$SECRETS_COPY")
}

remove_secrets_copy() {
  [ -z "$SECRETS_COPY" ] || rm -f "$SECRETS_COPY"
}
