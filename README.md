# 86server

Run [86Box](https://86box.net) headless on a Linux server and host things on emulated vintage PCs.

- **Boxes**: each emulated PC runs as a systemd service with its own virtual screen, reached through a browser console (noVNC).
- **Web manager**: create, start, stop, clone, snapshot, import and export boxes; upload ISOs and floppies; manage users.
- **Network groups**: private bridges for guests, with optional NAT, DHCP and group-to-group routes. Guests get no internet unless a group allows it.
- **Publishing**: put a website running inside a box (for example IIS on NT4) on a hostname, behind Caddy, with per-site rules.

This repo is only the installer. Ansible sets up the host, so the same install can be repeated on any supported machine. Everything it installs lives in `/opt/86server`.

Supported: Arch Linux (more distros later, see [docs/adding-a-distro.md](docs/adding-a-distro.md)).

## Install

```sh
git clone https://github.com/muncherelli/86server
cd 86server
./update.sh            # first run: creates ansible/inventories/local and stops
$EDITOR ansible/inventories/local/group_vars/all/main.yml
./update.sh vault-edit # set the admin password and any other secrets
./update.sh            # installs everything
```

`./update.sh` installs Ansible and the pinned collections when they are missing, then runs the playbook. Run it again any time: it only changes what differs. The local inventory is git-ignored, so your hostnames and secrets never end up in the repo.

## Commands

| Command | What it does |
| --- | --- |
| `./update.sh` | Install or update the host (`--verbose` for full Ansible output) |
| `./update.sh status` | Services, boxes and bridges, without an Ansible run |
| `./update.sh box list` | Boxes from the terminal; also `box start\|stop\|restart <name>` |
| `./update.sh vault-edit` | Edit the encrypted secrets file |
| `./update.sh dev` | Copy `manager/` to the host on every change and restart the manager |

## Install layout

| Path | Contents |
| --- | --- |
| `/opt/86server/vendor/86box/<ver>` | Extracted 86Box AppImage; `current` points at the pinned version |
| `/opt/86server/vendor/roms/<ver>` | 86Box ROM set; `current` symlink |
| `/opt/86server/vendor/novnc/<ver>` | noVNC browser console; `current` symlink |
| `/opt/86server/vendor/websockify/<ver>` | websockify; `current` symlink |
| `/opt/86server/manager` | Web manager code |
| `/opt/86server/libexec` | Helper scripts used by the services |
| `/opt/86server/data` | Boxes, media library, manager state |
| `/opt/86server/etc` | Generated settings (manager, network groups) |
| `/opt/86server/cache` | Downloads and caches |
| `/opt/86server/log` | `./update.sh` run logs |
| `/opt/86server/sftp` | Chroot for SFTP-only accounts |

Versions are pinned in `ansible/inventories/*/group_vars/all/pins.yml`. Old versions stay on disk next to the new one, so a rollback means pointing `current` back.

Only things the OS expects elsewhere live outside `/opt/86server`: systemd units in `/etc/systemd/system`, Caddy config in `/etc/caddy`, the polkit rule, the sshd drop-in for SFTP accounts, the sysctl and NetworkManager drop-ins, and runtime state in `/run/86server`.

## Docs

- [docs/networking.md](docs/networking.md): network groups, NAT, DHCP, routes
- [docs/display.md](docs/display.md): box display size
- [docs/media-sftp.md](docs/media-sftp.md): SFTP access to the media library
- [docs/edge.md](docs/edge.md): how the manager and sites are served (LAN, HTTPS, Cloudflare)
- [docs/adding-a-distro.md](docs/adding-a-distro.md)
- [manager/README.md](manager/README.md): manager module contract and tests
