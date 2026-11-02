# shellcheck shell=bash
# './update.sh status': services, boxes and bridges. No Ansible run.

show_status() {
  local unit state
  echo "86server status"
  echo
  printf '%-24s %s\n' SERVICE STATE
  for unit in 86box-manager 86box-websockify 86box-guard 86box-nft 86box-bridges 86box-dnsmasq caddy 86box-cloudflared; do
    # Services that are not installed on this host (not in use) are skipped.
    systemctl cat "$unit" >/dev/null 2>&1 || continue
    state="$(systemctl is-active "$unit" 2>/dev/null || true)"
    printf '%-24s %s\n' "$unit" "$state"
  done
  echo
  sudo -u 86srvmgr python3 /opt/86server/manager/cli.py list || echo "(Box list unavailable)"
  echo
  echo "Bridges:"
  ip -br link show type bridge || true
}
