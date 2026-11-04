# shellcheck shell=bash
# Checks that must pass before Ansible can even start.

preflight_basic() {
  [ "$(uname -m)" = x86_64 ] || die "Only x86_64 is supported."
  [ -d "/usr/lib/modules/$(uname -r)" ] ||
    die "The kernel was upgraded but this machine was not rebooted. Reboot, then run 86server update again."
}
