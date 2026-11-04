# Cloudflare: your own domain, real certificates and a tunnel

With a Cloudflare API token, 86server puts the control panel and the sites you publish from boxes on a domain you
keep on Cloudflare (a **zone**, for example `example.com`):

- one **Let's Encrypt wildcard certificate per zone** (`example.com` and `*.example.com`), obtained with a DNS
  challenge through Cloudflare. Nothing has to reach the server to get it, so it works on a LAN-only machine too.
  New sites need no new certificate;
- the **control panel** under a name in that zone, for example `panel.example.com`;
- one **Cloudflare Tunnel** for this server (named `86server-<hostname>`), with a route and a proxied DNS record
  for the panel and for every published site in the zone. No port has to be open: `cloudflared` dials out.

**Skipping the tunnel on your LAN.** Because the certificates are real, the same names also work inside your
network: on your router, Pi-hole or local DNS server, point `example.com` and `*.example.com` (or just the names
you use) at the server's LAN address. Browsers then talk straight to Caddy on the server, with a trusted
certificate, and never go through Cloudflare. Big uploads and the browser console are faster that way, and
Cloudflare's 100 MB upload limit does not apply.

Names must be the zone itself or **one level below it** (`panel.example.com`, `www.example.com`), because that is
what a wildcard certificate covers.

## 1. Create an API token

In the Cloudflare dashboard go to **My Profile > API Tokens** (a user token) or **Manage Account > API Tokens** (an
account token), select **Create Token** and make a custom token with these permissions:

| Group | Permission | Access |
| --- | --- | --- |
| Account | Cloudflare Tunnel | Edit |
| Zone | DNS | Edit |
| Zone | Zone | Read |

Under **Zone Resources**, include only the zones 86server may use. The token never needs anything else.
Cloudflare's permission list calls Edit "Write" in some places; it is the same permission.

## 2. Set it up

From a terminal on the server:

```sh
86server cloudflare
```

It asks for the token (hidden as you paste it; it is checked with Cloudflare before it is saved), then:

1. which zones to use;
2. the control panel's name (`panel` gives `panel.example.com`; `@` uses the zone itself; `-` for none);
3. whether to use the tunnel (no = LAN only, still with real certificates);
4. an optional email for Let's Encrypt, and whether to use Let's Encrypt **staging** while trying things out
   (staging certificates are not trusted by browsers but have very generous rate limits; set it up again
   without staging for the real ones).

Then it applies the setup and prints each step:

| Step | What happens |
| --- | --- |
| certificates | lego gets `*.zone` + `zone` for every zone that has no valid certificate (a minute or two: a TXT record has to spread) |
| caddy | the panel's site is checked with `caddy validate`, written, and Caddy reloaded; published sites are re-written to use the zone certificate |
| tunnel | the tunnel `86server-<hostname>` is created (or reused) and its routes set to exactly the panel and the published sites |
| dns | a proxied CNAME `<name> -> <tunnel id>.cfargotunnel.com` for each route; records 86server made for names no longer routed are removed |
| cloudflared | `86server-cloudflared.service` is restarted with the tunnel's token (or stopped when nothing is routed) |

If a name already has other DNS records, the dns step stops and lists them; the guided setup then offers to
replace them.

The **Cloudflare** page in the web manager (admins only) does exactly the same: save the token, tick zones, type
the panel name, **Save and apply**. Both use the manager's own code, so they always agree.

Without the menu:

```sh
86server cloudflare token                       # asks for the token
86server cloudflare zones                       # zones the token can see
86server cloudflare setup --zones example.com --panel panel --panel-zone example.com [--email a@b.c]
                          [--staging] [--no-tunnel] [--replace-dns]
86server cloudflare status
86server cloudflare sync                        # apply the saved setup again (repairs drift)
86server cloudflare off [--delete-tunnel]       # tunnel off: routes and DNS records removed; certificates stay
```

## Publishing sites

Once a zone is set up, the **Sites** page accepts names one level below it. Saving, disabling or deleting a site
updates Caddy right away, then updates the tunnel's routes and DNS records in the background (the Cloudflare page
shows that run). A site's **public side** `https` is served by Caddy with the zone certificate (the tunnel sends it
to `https://localhost:443`); `http` is plain http on port 80 (the tunnel sends it to `http://localhost:80`, and
Cloudflare still shows visitors https).

## Renewal

A daily timer runs `cli.py renew-certs` as the `86server` account. Every zone certificate with 30 days or fewer
left is renewed the same way and Caddy reloaded. The API token must still be saved for that to work. Run it by
hand with:

```
sudo -u 86server python3 /opt/86server/manager/cli.py renew-certs
```

## Files

| Path | What |
| --- | --- |
| `/opt/86server/data/manager/secrets.json` | the API token (0600, readable only by the `86server` account) |
| `/opt/86server/data/manager/edge.json` | zones, panel name, tunnel id and name, the DNS names 86server made, certificate expiry |
| `/opt/86server/data/manager/lego/` | lego's ACME account and certificate store |
| `/opt/86server/data/certs/<zone>.crt` and `.key` | the certificates Caddy uses |
| `/opt/86server/etc/caddy/panel.d/panel.caddy` | the control panel's Caddy site |
| `/opt/86server/etc/caddy/sites.d/<name>.caddy` | published sites |
| `/opt/86server/data/manager/cloudflared.env` | the tunnel token cloudflared runs with (0600) |

## Check it

```sh
86server cloudflare status
systemctl status 86server-cloudflared
journalctl -u 86server-cloudflared -n 50   # look for "Registered tunnel connection"
```
