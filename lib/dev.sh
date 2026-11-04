# shellcheck shell=bash
# 86server dev: copy manager/ to the host and restart the manager whenever a file changes.
# Open pages reload themselves (the manager is in dev mode while the .dev marker exists).

DEV_DEST=/opt/86server/manager

dev_sync() {
  sudo rsync -a --delete \
    --exclude=tests --exclude=__pycache__ --exclude=.dev \
    --chown=root:root --chmod=D755,F644 \
    "$REPO/manager/" "$DEV_DEST/"
}

dev_stamp() {
  find "$REPO/manager" -path '*/__pycache__' -prune -o -path '*/tests' -prune -o -type f \
    -printf '%p %T@ %s\n' | sort | cksum
}

dev_cleanup() {
  sudo rm -f "$DEV_DEST/.dev"
  echo
  echo "Dev mode off. The host still runs your last dev copy; 86server update deploys the committed state."
}

run_dev() {
  command -v rsync >/dev/null 2>&1 || die "rsync is required (sudo pacman -S rsync)"
  [ -d "$DEV_DEST" ] || die "$DEV_DEST not found. Run 86server update once first."
  sudo -v
  trap dev_cleanup EXIT
  trap 'exit 0' INT TERM
  dev_sync
  sudo touch "$DEV_DEST/.dev"
  sudo systemctl restart 86server-manager
  echo "Dev mode on. Watching $REPO/manager (Ctrl+C to stop)."
  local last now
  last=$(dev_stamp)
  while sleep 1; do
    now=$(dev_stamp)
    [ "$now" != "$last" ] || continue
    last=$now
    sleep 0.3  # let an editor finish writing
    dev_sync
    if sudo systemctl restart 86server-manager; then
      echo "$(date +%T) synced, manager restarted"
    else
      echo "$(date +%T) restart FAILED (journalctl -u 86server-manager)"
    fi
  done
}
