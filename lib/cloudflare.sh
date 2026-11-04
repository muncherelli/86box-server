# shellcheck shell=bash
# `86server cloudflare`: the web manager's Cloudflare page, from a terminal. The work is done by the manager's
# own code (cli.py cloudflare, run as 86server), so the web page and this always agree.
#
#   86server cloudflare              guided setup (menu)
#   86server cloudflare token        save an API token (asked for, never on the command line)
#   86server cloudflare <other>      passed on: zones, setup ..., sync, off, status (see: 86server cloudflare help)

CF_PERMISSIONS="Account > Cloudflare Tunnel > Edit, Zone > DNS > Edit, Zone > Zone > Read"

cf() {
  installed || die "86server is not installed yet: run 86server update first"
  manager_cli cloudflare "$@"
}

cf_ask_token() {
  local token
  echo "Create a Cloudflare API token with: $CF_PERMISSIONS"
  echo "(dash.cloudflare.com > My Profile > API Tokens > Create Token > Custom token; see docs/cloudflare.md)"
  read -rsp "Paste the token (hidden): " token || true
  echo
  if [ -z "$token" ]; then
    [ -t 0 ] || echo "No token saved: this needs a terminal to type into (run it over SSH)." >&2
    echo "No token given; nothing saved." >&2
    return 1
  fi
  printf '%s\n' "$token" | cf token
}

# Guided setup: pick zones, the panel name and the tunnel; then apply.
cf_setup() {
  local zones=() picks=() z i n panel_zone panel tunnel email staging extra=()
  mapfile -t zones < <(cf zones) || return 1
  [ ${#zones[@]} -gt 0 ] || { echo "The token sees no zones."; return 1; }
  echo
  echo "Zones the token can see:"
  for i in "${!zones[@]}"; do echo "  $((i + 1))) ${zones[i]}"; done
  read -rp "Use which zones? Numbers, e.g. 1 3 [1]: " -a n
  [ ${#n[@]} -gt 0 ] || n=(1)
  for i in "${n[@]}"; do
    [[ $i =~ ^[0-9]+$ ]] && [ "$i" -ge 1 ] && [ "$i" -le ${#zones[@]} ] || { echo "No zone $i"; return 1; }
    picks+=("${zones[i - 1]}")
  done
  panel_zone=${picks[0]}
  if [ ${#picks[@]} -gt 1 ]; then
    for i in "${!picks[@]}"; do echo "  $((i + 1))) ${picks[i]}"; done
    read -rp "Zone for the control panel [1]: " z
    z=${z:-1}
    [[ $z =~ ^[0-9]+$ ]] && [ "$z" -ge 1 ] && [ "$z" -le ${#picks[@]} ] || { echo "No zone $z"; return 1; }
    panel_zone=${picks[z - 1]}
  fi
  read -rp "Control panel name in $panel_zone ('@' = $panel_zone itself, '-' = no panel name) [panel]: " panel
  panel=${panel:-panel}
  if [ "$panel" = - ]; then panel=""; panel_zone=""; fi
  tunnel=y
  ask_yes "Reach the panel and sites from the internet through a Cloudflare Tunnel? (n = LAN only)" || tunnel=n
  read -rp "Email for Let's Encrypt (optional): " email
  staging=n
  ask_yes "Use Let's Encrypt staging (test certificates browsers do not trust)?" && staging=y
  [ "$tunnel" = y ] || extra+=(--no-tunnel)
  [ "$staging" = n ] || extra+=(--staging)
  local IFS=,
  echo
  echo "Applying (a new certificate takes a minute or two) ..."
  if ! cf setup --zones "${picks[*]}" --panel "$panel" --panel-zone "$panel_zone" --email "$email" "${extra[@]}"; then
    echo
    if ask_yes "If a step said the name already has DNS records: replace them and try again?"; then
      cf setup --zones "${picks[*]}" --panel "$panel" --panel-zone "$panel_zone" --email "$email" "${extra[@]}" \
        --replace-dns
    else
      return 1
    fi
  fi
}

cloudflare_menu() {
  installed || die "86server is not installed yet: run 86server update first"
  local choice
  while true; do
    cf status || return 1
    if ! cf zones >/dev/null 2>&1; then
      echo
      cf_ask_token || return 1
      continue
    fi
    echo
    echo "  1) Set up or change (zones, panel name, tunnel)"
    echo "  2) Run again (repair, after changes made at Cloudflare)"
    echo "  3) Turn the tunnel off"
    echo "  4) Replace the API token"
    echo "  b) Back"
    read -rp "Choose: " choice || return 0
    case $choice in
      1) cf_setup || true ;;
      2) cf sync || true ;;
      3) if ask_yes "Also delete the tunnel at Cloudflare?"; then cf off --delete-tunnel || true; else cf off || true; fi ;;
      4) cf_ask_token || true ;;
      *) return 0 ;;
    esac
    echo
  done
}

cloudflare_cmd() {
  case "${1:-}" in
    "") cloudflare_menu ;;
    token) cf_ask_token ;;
    help | -h | --help) cf --help ;;
    *) cf "$@" ;;
  esac
}
