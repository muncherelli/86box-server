# shellcheck shell=bash
# '86server status': services, boxes and bridges. No Ansible run.

show_status() {
  local unit state
  echo "86server status"
  echo
  installed || { echo "Not installed yet: run 86server update."; return 0; }
  printf '%-24s %s\n' SERVICE STATE
  for unit in 86server-manager 86server-websockify 86server-guard 86server-nft 86server-bridges 86server-dnsmasq caddy 86server-cloudflared; do
    # Services that are not installed on this host (not in use) are skipped.
    systemctl cat "$unit" >/dev/null 2>&1 || continue
    state="$(systemctl is-active "$unit" 2>/dev/null || true)"
    printf '%-24s %s\n' "$unit" "$state"
  done
  echo
  sudo -u 86server python3 /opt/86server/manager/cli.py list || echo "(Box list unavailable)"
  echo
  echo "Bridges:"
  ip -br link show type bridge || true
}
