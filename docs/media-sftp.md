# SFTP access to the media library

The media library lives in `<vm_data_root>/media/{isos,floppies,disks}`. It is also managed from the web UI.

## Normal SSH users

Add the login name to `media_users` in the inventory and run `./update.sh`. The user joins the shared group (`vm_share_group`) and can use `sftp` or `scp` on the folders directly.

## SFTP-only accounts

For people who should upload images and nothing else, list them in the inventory:

```yaml
sftp_only_users:
  - name: uploader
    ssh_key: "ssh-ed25519 AAAA... comment"
```

`./update.sh` then creates each account with no shell and key-only login. Each one is locked into `/opt/86server/sftp`, which shows only the `media` folder (a bind mount of the library). New files are group-writable (umask 0002). Password login, port forwarding and tunnels are off for them.

The SSH settings go in `/etc/ssh/sshd_config.d/60-86box-sftp.conf`. `./update.sh` checks them with `sshd -t` before it reloads sshd, so a bad config is refused instead of applied. Leaving `sftp_only_users` empty changes nothing on the host.

Removing a name from the list does not delete the account; remove it by hand (`userdel`) and delete `/etc/ssh/sftp-keys/<name>`.
