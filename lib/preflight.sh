# shellcheck shell=bash
# Checks that must pass before Ansible can even start.

preflight_basic() {
  case "$(uname -m)" in
    x86_64 | aarch64) ;;
    *) die "Only x86_64 and aarch64 are supported." ;;
  esac
  [ -d "/usr/lib/modules/$(uname -r)" ] ||
    die "The kernel was upgraded but this machine was not rebooted. Reboot, then run 86server update again."
}
