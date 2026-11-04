# Web front (Caddy)

Caddy serves the web manager, the browser consoles and published sites. It can do this under three kinds of names, and you can use any mix of them. Set them in `/opt/86server/inventory/group_vars/all/main.yml`, then run `./update.sh`. Each name may appear only once across the three lists.

| Setting | Served as | Certificate |
| --- | --- | --- |
| `manager_host` | depends on `edge_mode` (below) | depends on `edge_mode` |
| `lan_hosts` | `http://name` and `https://name`, with no redirect between them | Caddy's local CA (self-signed) |
| `tunnel_hosts` | `http://name` on port 80, for `cloudflared` | Cloudflare's, at the edge |

`edge_mode` covers `manager_host` and the sites published from the manager:

| `edge_mode` | Meaning |
| --- | --- |
| `acme` | Let's Encrypt. The name's DNS must point at this host, with ports 80 and 443 open. |
| `internal` | Caddy's local CA (`tls internal`), for LAN-only hosts. |
| `tunnel` | Plain http on port 80 for `cloudflared`. No inbound ports are opened. |
| `cf-origin` | Cloudflare Origin CA certificate plus authenticated origin pulls (not built yet). |

When ufw is active, `./update.sh` opens 80/443. The one exception is `edge_mode: tunnel` with no `lan_hosts`.

## LAN names (`lan_hosts`)

```yaml
lan_hosts: [box.lan]
```

1. On your router or LAN DNS server, point the name at this host: an A record, or a CNAME to the host's existing name.
2. `http://box.lan/` works straight away. Login works over plain http: the session cookie is marked `Secure` only on https.
3. `https://box.lan/` uses a certificate from Caddy's own CA. Browsers warn until you trust that CA once per device. 86server keeps Caddy's data in `/opt/86server/data/caddy`, so the root certificate is:

   ```sh
   sudo cat /opt/86server/data/caddy/pki/authorities/local/root.crt
   ```

   If it is not there, find it with `sudo find /opt/86server/data/caddy -name root.crt`. Copy it to the device and import it as a trusted root CA: Keychain on macOS, "Trusted Root Certification Authorities" on Windows, or the browser's certificate settings. The file appears after Caddy has issued its first internal certificate.

## Cloudflare Tunnel (`tunnel_hosts`)

The tunnel lets people reach the manager from outside without opening any port. `cloudflared` dials out to Cloudflare, and Cloudflare sends visitors back down it.

The easier way is the Cloudflare page in the web manager. You paste an API token and pick a name. The page gets a Let's Encrypt certificate, creates the tunnel and its DNS record, and starts `cloudflared` for you. See [cloudflare.md](cloudflare.md). The steps below are the manual way, with a tunnel you make yourself.

1. In the Cloudflare dashboard, go to Zero Trust > Networks > Tunnels and create a tunnel of type Cloudflared. This makes a remotely-managed tunnel. Copy its token: it is the long string after `--token` in the install command shown there.
2. In the tunnel, add a public hostname such as `box.example.com`, with service type `HTTP` and URL `localhost:80`.
3. Run `./update.sh secrets-edit` and set `secret_cf_tunnel_token: <token>`. You can also set `secret_cf_tunnel_id` to the tunnel's id; later automation uses it.
4. Add the same name to the inventory:

   ```yaml
   tunnel_hosts: [box.example.com]
   ```

5. Run `./update.sh`. It installs `cloudflared` and starts `86server-cloudflared.service`, which reads the token from `/opt/86server/etc/cloudflared.env` (root, mode 0600).

Caddy treats requests from `cloudflared` (127.0.0.1) as proxied and takes the visitor's address from `Cf-Connecting-Ip`. Login rate limits therefore count each visitor separately. To turn the manual tunnel off, empty `secret_cf_tunnel_token` and run `./update.sh`. The token file `/opt/86server/etc/cloudflared.env` is removed, and `86server-cloudflared` stays off unless the Cloudflare page has set up a tunnel of its own.

Check it:

```sh
systemctl status 86server-cloudflared
journalctl -u 86server-cloudflared -n 50   # look for "Registered tunnel connection"
```
