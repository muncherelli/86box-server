# shellcheck shell=bash
# One log file per run in $LOG_DIR. Sets LOG.

PACKAGES_TO_LOG="ansible-core caddy tigervnc dnsmasq nftables cloudflared lego python polkit openssl rsync ttf-dejavu shellcheck"

setup_log() {
  mkdir -p "$LOG_DIR"
  LOG="$LOG_DIR/$(date +%Y-%m-%dT%H-%M-%S).log"
  ln -sfn "$LOG" "$LOG_DIR/latest.log"
  write_log_header
  prune_logs
  # No color codes in log files.
  export ANSIBLE_NOCOLOR=1 ANSIBLE_FORCE_COLOR=0
}

write_log_header() {
  {
    echo "date:    $(date -Is)"
    echo "commit:  $(git -C "$REPO" rev-parse --short HEAD 2>/dev/null || echo unknown)"
    echo "kernel:  $(uname -r)"
    echo "ansible: $(ansible --version | head -1)"
    if command -v pacman >/dev/null 2>&1; then
      # shellcheck disable=SC2086
      pacman -Q $PACKAGES_TO_LOG 2>/dev/null | sed 's/^/package: /' || true
    fi
    echo "----"
  } >"$LOG"
}

# Keep the newest 30 logs.
prune_logs() {
  # shellcheck disable=SC2012
  ls -1t "$LOG_DIR"/2*.log | tail -n +31 | xargs -r rm --
}
