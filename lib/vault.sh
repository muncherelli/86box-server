# shellcheck shell=bash
# Ansible Vault handling. Sets VAULT_ARGS for ansible-playbook.

VAULT_FILE="$INVENTORY_DIR/group_vars/all/vault.yml"

vault_password_file() {
  echo "${B86_VAULT_PASS_FILE:-$INVENTORY_DIR/vault-pass}"
}

# Chooses how Ansible gets the vault password.
setup_vault_password() {
  local pass
  pass=$(vault_password_file)
  VAULT_ARGS=()
  if [ -f "$pass" ]; then
    [ "$(stat -c %a "$pass")" = 600 ] || die "Vault password file must have mode 600: chmod 600 $pass"
    VAULT_ARGS=(--vault-password-file "$pass")
  elif [ -t 0 ]; then
    VAULT_ARGS=(--ask-vault-pass)
  else
    die "No vault password file at $pass"
  fi
}

vault_is_encrypted() {
  # shellcheck disable=SC2016
  head -1 "$VAULT_FILE" | grep -q '^\$ANSIBLE_VAULT'
}

# Refuses to run when secrets could be sitting in plain text.
check_vault() {
  [ -f "$VAULT_FILE" ] || die "Missing $VAULT_FILE"
  vault_is_encrypted || die "vault.yml is not encrypted. Run: ./update.sh vault-edit"
  local bad
  bad=$(grep -rlE '^\s*vault_[a-z0-9_]+:' "$INVENTORY_DIR" \
    --include='*.yml' --exclude=vault.yml || true)
  [ -z "$bad" ] || die "Secrets (vault_*) found outside vault.yml: $bad"
}

# ./update.sh vault-edit: encrypts vault.yml the first time, then opens it.
vault_edit() {
  [ -f "$VAULT_FILE" ] || die "Missing $VAULT_FILE (run ./update.sh once first)"
  setup_vault_password
  if ! vault_is_encrypted; then
    ansible-vault encrypt "${VAULT_ARGS[@]}" "$VAULT_FILE"
  fi
  ansible-vault edit "${VAULT_ARGS[@]}" "$VAULT_FILE"
}
