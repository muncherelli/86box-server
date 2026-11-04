# 86server

Run [86Box](https://86box.net) headless on a Linux server and host things on emulated vintage PCs.

- **Boxes**: each emulated PC runs as a systemd service with its own virtual screen, reached through a browser console (noVNC).
- **Web manager**: create, start, stop, clone, snapshot, import and export boxes; upload ISOs and floppies; manage users.
- **Network groups**: private bridges for guests, with optional NAT, DHCP and group-to-group routes. Guests get no internet unless a group allows it.
- **Publishing**: put a website running inside a box (for example IIS on NT4) on a hostname, behind Caddy, with per-site rules.

This repo is only the installer. Ansible sets up the host, so the same install can be repeated on any supported machine. Everything 86server installs, writes or keeps lives in `/opt/86server`: the programs, box disks, ISOs, settings, certificates, and the installer's own settings and logs (see [Install layout](#install-layout)).

Supported: Arch Linux (more distros later, see [docs/adding-a-distro.md](docs/adding-a-distro.md)), on x86_64 or aarch64.
On aarch64 86server uses 86Box's NDR build, which has no dynamic recompiler: boxes work but run much slower than on x86_64.

## Install

```sh
git clone https://github.com/muncherelli/86server
cd 86server
./86server             # menu: choose 1) Install / update
```

Or without the menu: `./86server update`. The first run puts the `86server` command on your PATH
(`/usr/local/bin/86server`, a link to this clone), so after that you just type `86server` from anywhere.

The defaults work as they are: the manager is the server's default site, so it answers on its IP address and on
any name that points at it (`http://<ip or name>/`, and `https://` with Caddy's local CA), with a random admin
password (`86server admin-password` shows it). Change the settings with `86server settings`, and add other secrets
with `86server secrets`.

`86server update` installs Ansible and the pinned collections when they are missing, then runs the playbook. Run it again any time: it only changes what differs. This host's settings live in `/opt/86server/inventory` and its secrets in `/opt/86server/secrets.yml`, both outside the repo, so your hostnames and secrets never end up in the repo, and a fresh clone picks up the same settings.

Secrets are not encrypted and need no extra password: the file belongs to root with mode 600, so reading or editing it takes sudo. For each run `86server update` copies it with sudo into a private, memory-only folder (`$XDG_RUNTIME_DIR`) for Ansible and deletes the copy when the run ends.

## Cloudflare: tunnel and real certificates

Give 86server a Cloudflare API token and it puts the control panel and every published site on your own domain:
a Let's Encrypt wildcard certificate per zone (DNS challenge, so nothing has to reach the server), one Cloudflare
Tunnel for this server (no open ports) and the DNS records. Because the certificates are real, the same names work
on your LAN without the tunnel: point local DNS for `example.com` and `*.example.com` at the server. Run
`86server cloudflare` (or use the Cloudflare page in the web manager); see [docs/cloudflare.md](docs/cloudflare.md).

## Commands

`86server` with no command shows a menu with the common ones.

| Command | What it does |
| --- | --- |
| `86server update` | Install or update the host (`--verbose` for full Ansible output) |
| `86server status` | Services, boxes and bridges, without an Ansible run |
| `86server box list` | Boxes from the terminal; also `box start\|stop\|restart <name>` |
| `86server cloudflare` | Guided setup of the Cloudflare token, zones, panel name and tunnel; `86server cloudflare help` for the rest |
| `86server admin-password` | Show the web manager's admin login (warns if it was changed in the web manager) |
| `86server admin-password reset` | Set a new random admin password, save it in the secrets file and sign out old admin logins |
| `86server settings` | Edit this host's settings, then run `86server update` |
| `86server secrets` | Edit the secrets file (`sudoedit`; root only) |
| `86server log` | Show the last update log |
| `86server dev` | Copy `manager/` to the host on every change and restart the manager |

## The 86server account

Everything runs as one service account, `86server`, with UID and GID 86: the boxes, the web manager,
the console proxy and the certificate timer. It has no shell and no password. Install as any other
login user (for example `linuxuser`); `86server update` stops if a different account already has the
name `86server` or ID 86. Login users listed in `media_users` join the `86server` group to read and
write the media library.

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
| `/opt/86server/media` | Media library, one folder per 86Box drive type: `iso/`, `floppy/`, `disk/`, `zip/`, `mo/`, `tape/`, `cassette/`, `cartridge/` (the Media page shows the file types on each tab) |
| `/opt/86server/data/manager` | Manager state: users, sessions, secrets (`secrets.json`: the Cloudflare token), the Cloudflare setup (`edge.json`), audit log |
| `/opt/86server/data/caddy` | Caddy's data: its local CA and the certificates it manages |
| `/opt/86server/data/certs` | Let's Encrypt wildcard certificates, one per Cloudflare zone (`<zone>.crt`, `.key`) |
| `/opt/86server/etc` | Generated settings: `manager.json`, network groups, `ssh/sftp-keys/` |
| `/opt/86server/etc/caddy` | `Caddyfile`, `sites.d/` (published sites) and `panel.d/` (the control panel name) |
| `/opt/86server/system/etc` | The real files for systemd units, the Caddy drop-in, polkit, tmpfiles, sshd, sysctl and NetworkManager (see below) |
| `/opt/86server/inventory` | This host's settings: `hosts.yml`, `group_vars/all/` (`main.yml`, `pins.yml`). Owned by the person who runs `86server`, mode 700. No secrets |
| `/opt/86server/secrets.yml` | This host's secrets (the admin password). Plain YAML, `root:root` mode 600: reading or editing it takes sudo |
| `/opt/86server/log` | `86server update` run logs (newest 30) |
| `/opt/86server/cache/ansible-collections` | Ansible collections the installer downloads |
| `/opt/86server/cache` | Downloads and caches |
| `/opt/86server/sftp` | Chroot for SFTP-only accounts |

Versions are pinned in `ansible/inventories/example/group_vars/all/pins.yml`, which the first run copies into your inventory. The 86Box AppImage is pinned once per CPU architecture (`pins.emu86box.builds`). Old versions stay on disk next to the new one, so a rollback means pointing `current` back.

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

86server does not bundle 86Box or its ROM set: `86server update` downloads the pinned releases from their own projects at install time.
