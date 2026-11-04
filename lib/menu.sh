# shellcheck shell=bash
# `86server` with no arguments: a menu over the same commands. Each choice runs in a subshell,
# so a command that stops with an error (or exits when it is done) returns to the menu.

installed() { [ -f /opt/86server/manager/cli.py ]; }

box_cli() {
  [ $# -gt 0 ] || set -- list
  installed || die "86server is not installed yet: run 86server update first"
  manager_cli "$@"
}

show_log() {
  [ -e "$LOG_DIR/latest.log" ] || die "No update log yet."
  ${PAGER:-less} "$LOG_DIR/latest.log"
}

pause() {
  echo
  read -rsn1 -p "Press a key to go back to the menu." || true
  echo
}

ask_yes() {
  local a
  read -rp "$1 [y/N] " a || return 1
  [[ $a == [yY]* ]]
}

menu_boxes() {
  box_cli list || return
  echo
  local verb name
  read -rp "start, stop or restart a box? (Enter to go back) " verb name || return 0
  [ -n "$verb" ] || return 0
  case $verb in
    start | stop | restart) ;;
    *) echo "Type e.g.: start win98"; return 1 ;;
  esac
  [ -n "$name" ] || read -rp "Box name: " name
  box_cli "$verb" "$name"
}

menu_admin() {
  prepare
  admin_password
  echo
  if ask_yes "Set a new random password (signs the admin out everywhere)?"; then
    admin_password reset
  fi
}

menu_header() {
  local state="not installed yet"
  if installed; then
    state="installed"
    local panel
    panel=$(sudo -n -u 86server python3 /opt/86server/manager/cli.py cloudflare panel-url 2>/dev/null || true)
    [ -z "$panel" ] || state="installed, panel at $panel"
  fi
  echo
  echo "  86server on $(uname -n) ($state)"
  echo
}

run_menu() {
  [ -t 0 ] || { usage; exit 2; }
  local choice
  while true; do
    menu_header
    echo "  1) Install / update"
    echo "  2) Status"
    echo "  3) Boxes"
    echo "  4) Cloudflare (tunnel + certificates)"
    echo "  5) Admin password"
    echo "  6) Edit settings"
    echo "  7) Edit secrets"
    echo "  8) Last update log"
    echo "  q) Quit"
    echo
    read -rp "Choose: " choice || exit 0
    echo
    case $choice in
      1) (do_update) || true ;;
      2) (show_status) || true ;;
      3) (menu_boxes) || true ;;
      4) (cloudflare_menu) || true ;;
      5) (menu_admin) || true ;;
      6) (prepare && settings_edit) || true ;;
      7) (ensure_install_dirs && secrets_edit) || true ;;
      8) (show_log) || true; continue ;;
      q | Q | "") exit 0 ;;
      *) echo "No such choice: $choice"; continue ;;
    esac
    pause
  done
}
