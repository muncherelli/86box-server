# Web front (Caddy)

Caddy serves the web manager, the browser consoles and published sites, under two kinds of names:

| Names | Set with | Certificate | Reached |
| --- | --- | --- | --- |
| LAN names (`lan_hosts`) | `86server settings` | Caddy's local CA (self-signed) | on your LAN, `http://` and `https://` |
| Names in a Cloudflare zone | `86server cloudflare` or the Cloudflare page | Let's Encrypt wildcard per zone | through the Cloudflare Tunnel, and on your LAN with local DNS |

Use either or both. Ports 80 and 443 stay open for LAN visitors (when ufw is active, `86server update` opens
them); the tunnel itself needs no open port.

## LAN names (`lan_hosts`)

By default the manager answers on this machine's own name. To add others:

```yaml
lan_hosts: ["{{ ansible_facts['hostname'] }}", box.lan]
```

1. On your router or LAN DNS server, point the name at this host: an A record, or a CNAME to the host's existing name.
2. `http://box.lan/` works straight away. Login works over plain http: the session cookie is marked `Secure` only on https.
3. `https://box.lan/` uses a certificate from Caddy's own CA. Browsers warn until you trust that CA once per device. 86server keeps Caddy's data in `/opt/86server/data/caddy`, so the root certificate is:

   ```sh
   sudo cat /opt/86server/data/caddy/pki/authorities/local/root.crt
   ```

   If it is not there, find it with `sudo find /opt/86server/data/caddy -name root.crt`. Copy it to the device and import it as a trusted root CA: Keychain on macOS, "Trusted Root Certification Authorities" on Windows, or the browser's certificate settings. The file appears after Caddy has issued its first internal certificate.

Sites can also be published on LAN-only domains listed in `publish_domains`; they use the same local CA.

## Cloudflare names

See [cloudflare.md](cloudflare.md). The manager writes the control panel's site to
`/opt/86server/etc/caddy/panel.d/panel.caddy` and published sites to `/opt/86server/etc/caddy/sites.d/`; the
Caddyfile imports both.

Caddy treats requests from `cloudflared` (127.0.0.1) as proxied and takes the visitor's address from `Cf-Connecting-Ip`. Login rate limits therefore count each visitor separately. Visitors on the LAN connect directly, so their own address is used.
