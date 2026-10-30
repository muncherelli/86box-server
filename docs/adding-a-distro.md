# Adding a Linux distribution

Only Arch Linux is supported today. Every role keeps its distro-specific
names (packages, services, paths) in `ansible/roles/<role>/vars/<OS family>.yml`.
Tasks only use those variables, so adding a distro never means rewriting tasks.

1. In each role, copy `vars/Archlinux.yml` to `vars/<Family>.yml`
   (for example `Debian.yml`; the family name is Ansible's `ansible_os_family`).
2. Change the package names, service names and paths in the new files.
3. Add the family to the allow-list in the `preflight` role, then test the
   whole install on a scratch machine.
