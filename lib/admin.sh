# shellcheck shell=bash
# ./update.sh admin-password [reset]: show the web manager's admin password, or set a new random one.
# The password lives in /opt/86server/secrets.yml as secret_admin_password. Ansible only uses it to create
# the first admin, so after a change in the web manager that copy goes stale: plain admin-password says so.

ADMIN_USER=admin

saved_admin_password() {
  sudo cat "$SECRETS_FILE" | awk '/^secret_admin_password:/ {sub(/^secret_admin_password:[ \t]*/, ""); gsub(/^["\x27]|["\x27]$/, ""); print; exit}'
}

manager_cli() {
  sudo -u 86server python3 /opt/86server/manager/cli.py "$@"
}

# Writes a new secret_admin_password into the secrets file (stdin = the password), keeping it root:root 600.
save_admin_password() {
  sudo python3 -c '
import os, re, sys
path = sys.argv[1]
line = "secret_admin_password: \x27%s\x27" % sys.stdin.readline().rstrip("\n")
with open(path) as f:
    text = f.read()
new, n = re.subn(r"(?m)^secret_admin_password:.*$", lambda m: line, text)
if not n:
    new = text.rstrip("\n") + "\n" + line + "\n"
tmp = path + ".new"
fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, "w") as f:
    f.write(new)
os.replace(tmp, path)' "$SECRETS_FILE"
}

admin_password() {
  check_secrets
  [ -f /opt/86server/manager/cli.py ] || die "86server is not installed yet: run ./update.sh first"
  case "${1:-}" in
    "")
      local pw
      pw=$(saved_admin_password)
      [ -n "$pw" ] || die "No secret_admin_password in $SECRETS_FILE"
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
      sudo systemctl restart 86server-manager
      printf '%s\n' "$pw" | save_admin_password || die "Password changed in the manager, but saving it in $SECRETS_FILE failed. New password: $pw"
      echo "user:     $ADMIN_USER"
      echo "password: $pw"
      echo "(saved in $SECRETS_FILE; existing $ADMIN_USER logins were signed out)"
      ;;
    *)
      die "usage: ./update.sh admin-password [reset]"
      ;;
  esac
}
