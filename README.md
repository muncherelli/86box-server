# 86server

Run [86Box](https://86box.net) headless on a Linux server and host things on emulated vintage PCs.

- **Boxes**: each emulated PC runs as a systemd service with its own virtual screen, reached through a browser console (noVNC).
- **Web manager**: create, start, stop, clone, snapshot, import and export boxes; upload ISOs and floppies; manage users.
- **Network groups**: private bridges for guests, with optional NAT, DHCP and group-to-group routes. Guests get no internet unless a group allows it.
- **Publishing**: put a website running inside a box (for example IIS on NT4) on a hostname, behind Caddy, with per-site rules.

This repo is only the installer. Ansible sets up the host, so the same install can be repeated on any supported machine. Everything 86server installs, writes or keeps lives in `/opt/86server`: the programs, box disks, ISOs, settings, certificates, and the installer's own settings and logs (see [Install layout](#install-layout)).

Supported: Arch Linux (more distros later, see [docs/adding-a-distro.md](docs/adding-a-distro.md)).

## Install

```sh
git clone https://github.com/muncherelli/86server
cd 86server
./update.sh            # first run: creates /opt/86server/install/inventory and stops
$EDITOR /opt/86server/install/inventory/group_vars/all/main.yml
./update.sh vault-edit # set the admin password and any other secrets
./update.sh            # installs everything
./update.sh admin-password   # shows the web manager's admin login
```

`./update.sh` installs Ansible and the pinned collections when they are missing, then runs the playbook. Run it again any time: it only changes what differs. This host's settings and secrets live in `/opt/86server/install`, outside the repo, so your hostnames and secrets never end up in the repo, and a fresh clone picks up the same settings. Older checkouts kept them in `ansible/inventories/local` and `~/.config/86server/vault-pass`; the next run moves them.

To reach the manager from outside, or to give it a real certificate, use the Cloudflare page in the web manager: see [docs/cloudflare.md](docs/cloudflare.md).

## Commands

| Command | What it does |
| --- | --- |
| `./update.sh` | Install or update the host (`--verbose` for full Ansible output) |
| `./update.sh status` | Services, boxes and bridges, without an Ansible run |
| `./update.sh box list` | Boxes from the terminal; also `box start\|stop\|restart <name>` |
| `./update.sh vault-edit` | Edit the encrypted secrets file |
| `./update.sh admin-password` | Show the web manager's admin login (warns if it was changed in the web manager) |
| `./update.sh admin-password reset` | Set a new random admin password, save it in the vault and sign out old admin logins |
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
| `/opt/86server/data/boxes` | One folder per box: its `86box.cfg`, disk images (`disks/`), snapshots |
| `/opt/86server/media` | Media library, one folder per 86Box drive type: `iso/`, `floppy/`, `disk/`, `zip/`, `mo/`, `tape/`, `cassette/`, `cartridge/` (the Media page lists the file types for each) |
| `/opt/86server/data/manager` | Manager state: users, sessions, secrets (`secrets.json`), audit log |
| `/opt/86server/data/caddy` | Caddy's data: its local CA and the certificates it manages |
| `/opt/86server/data/certs` | Certificates the Cloudflare page gets from Let's Encrypt |
| `/opt/86server/etc` | Generated settings: `manager.json`, network groups, `cloudflared.env`, `ssh/sftp-keys/` |
| `/opt/86server/etc/caddy` | `Caddyfile`, `sites.d/` (published sites) and `panel.d/` (the control panel name) |
| `/opt/86server/system/etc` | The real files for systemd units, the Caddy drop-in, polkit, tmpfiles, sshd, sysctl and NetworkManager (see below) |
| `/opt/86server/install` | The installer's own state: `inventory/` (settings and `vault.yml`), `vault-pass`, `logs/`, `collections/`. Owned by the person who runs `./update.sh`, mode 700 |
| `/opt/86server/cache` | Downloads and caches |
| `/opt/86server/sftp` | Chroot for SFTP-only accounts |

Versions are pinned in `ansible/inventories/example/group_vars/all/pins.yml`, which the first run copies into your inventory. Old versions stay on disk next to the new one, so a rollback means pointing `current` back.

### Files in /etc are symlinks

systemd, polkit, sshd, systemd-tmpfiles, sysctl and NetworkManager only read their settings from fixed folders under `/etc`. 86server keeps the real files in `/opt/86server/system/etc/<same path>` and puts a symlink in the matching `/etc` folder. For example, `/etc/systemd/system/box@.service` points to `/opt/86server/system/etc/systemd/system/box@.service`. So:

- `ls -l` on those folders shows exactly what 86server added: every entry that points into `/opt/86server`.
- Deleting `/opt/86server` leaves only dangling links behind, nothing with content.

Caddy needs no link: a systemd drop-in points it at `/opt/86server/etc/caddy/Caddyfile` and `/opt/86server/data/caddy`.

The one exception is `/run/86server`: a small in-memory folder for runtime status that the OS clears at every boot. Nothing is kept there.

## Docs

- [docs/networking.md](docs/networking.md): network groups, NAT, DHCP, routes
- [docs/display.md](docs/display.md): box display size
- [docs/media-sftp.md](docs/media-sftp.md): SFTP access to the media library
- [docs/edge.md](docs/edge.md): how the manager and sites are served (LAN, HTTPS, Cloudflare)
- [docs/adding-a-distro.md](docs/adding-a-distro.md)
- [manager/README.md](manager/README.md): manager module contract and tests

## License

86server is licensed under the GNU General Public License v2.0 (SPDX: `GPL-2.0-only`), the same license as 86Box. See [LICENSE](LICENSE).

86server does not bundle 86Box or its ROM set: `./update.sh` downloads the pinned releases from their own projects at install time.
