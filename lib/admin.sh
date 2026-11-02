# shellcheck shell=bash
# ./update.sh admin-password [reset]: show the web manager's admin password, or set a new random one.
# The password lives in the vault as vault_admin_password. Ansible only uses it to create the first admin,
# so after a change in the web manager the vault copy goes stale: plain admin-password says so.

ADMIN_USER=admin

vault_admin_password() {
  ansible-vault view "${VAULT_ARGS[@]}" "$VAULT_FILE" | awk '/^vault_admin_password:/ {sub(/^vault_admin_password:[ \t]*/, ""); gsub(/^["\x27]|["\x27]$/, ""); print; exit}'
}

manager_cli() {
  sudo -u 86server python3 /opt/86server/manager/cli.py "$@"
}

# Writes a new vault_admin_password into the encrypted vault (stdin = the password).
vault_set_admin_password() {
  local pw tmp
  IFS= read -r pw
  tmp=$(mktemp)
  chmod 600 "$tmp"
  # shellcheck disable=SC2064
  trap "rm -f '$tmp'" RETURN
  ansible-vault view "${VAULT_ARGS[@]}" "$VAULT_FILE" |
    PW="$pw" python3 -c '
import os, re, sys
line = "vault_admin_password: \x27%s\x27" % os.environ["PW"]
text = sys.stdin.read()
new, n = re.subn(r"(?m)^vault_admin_password:.*$", lambda m: line, text)
sys.stdout.write(new if n else text.rstrip("\n") + "\n" + line + "\n")' >"$tmp"
  ansible-vault encrypt "${VAULT_ARGS[@]}" --output "$VAULT_FILE" "$tmp" >/dev/null
}

admin_password() {
  check_vault
  setup_vault_password
  [ -f /opt/86server/manager/cli.py ] || die "86server is not installed yet: run ./update.sh first"
  case "${1:-}" in
    "")
      local pw
      pw=$(vault_admin_password)
      [ -n "$pw" ] || die "No vault_admin_password in the vault"
      echo "user:     $ADMIN_USER"
      echo "password: $pw"
      if ! printf '%s\n' "$pw" | manager_cli check-password "$ADMIN_USER" >/dev/null 2>&1; then
        echo
        echo "warning: the manager does not accept this password (it was changed in the web manager?)."
        echo "Set a new one with: ./update.sh admin-password reset"
      fi
      ;;
    reset)
      local pw
      pw=$(python3 -c 'import secrets; print(secrets.token_urlsafe(15))')
      printf '%s\n' "$pw" | manager_cli set-password "$ADMIN_USER" >/dev/null || die "Could not set the password"
      sudo systemctl restart 86box-manager
      printf '%s\n' "$pw" | vault_set_admin_password || die "Password changed in the manager, but saving it in the vault failed. New password: $pw"
      echo "user:     $ADMIN_USER"
      echo "password: $pw"
      echo "(saved in the vault; existing $ADMIN_USER logins were signed out)"
      ;;
    *)
      die "usage: ./update.sh admin-password [reset]"
      ;;
  esac
}
