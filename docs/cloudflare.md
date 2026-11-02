# Cloudflare: a real hostname for the control panel

The **Cloudflare** page in the web manager (admins only) puts the control panel under a hostname in a domain you
keep on Cloudflare, for example `panel.example.com`:

- a **Let's Encrypt certificate** for that name, obtained with a DNS challenge through Cloudflare. Nothing has to
  reach the server from the internet to get it, so it works on a LAN-only machine too;
- a **Caddy site** for that name using the certificate;
- optionally a **Cloudflare Tunnel** with a DNS record, so the panel is reachable from the internet with no open
  ports.

Because the certificate is real, the same name also works inside your network: point your local DNS (router,
Pi-hole, hosts file) for that name at the server's LAN address and browsers trust it there as well.

## 1. Create an API token

In the Cloudflare dashboard go to **My Profile > API Tokens** (a user token) or **Manage Account > API Tokens** (an
account token), select **Create Token** and make a custom token with these permissions:

| Group | Permission | Access |
| --- | --- | --- |
| Account | Cloudflare Tunnel | Edit |
| Zone | DNS | Edit |
| Zone | Zone | Read |

Under **Zone Resources**, include only the zones (domains) 86server may use. The token never needs anything else.
Cloudflare's permission list calls Edit "Write" in some places; it is the same permission.

## 2. Save it in the manager

Open **Cloudflare** in the manager menu, paste the token and select **Save**. The manager first asks Cloudflare
which zones the token can see and refuses a token that fails or sees none. The token is stored in
`/opt/86server/data/manager/secrets.json` (readable only by the manager's account). The page only ever shows the
date it was set and its last four characters. **Remove token** deletes it.

## 3. Set up the hostname

Under **Control panel hostname**:

1. Type a name and pick one of the token's zones: `panel` + `example.com` gives `panel.example.com`; `@` uses the
   zone itself.
2. Optionally give an email for Let's Encrypt.
3. Tick **Let's Encrypt staging** while you are trying things out. Staging certificates are not trusted by browsers
   but have very generous rate limits. Untick it and run Set up again for the real certificate.
4. Leave **Also expose through a Cloudflare Tunnel** ticked to reach the panel from the internet.
5. Select **Set up**. The page shows each step as it runs:
   - **certificate**: lego creates a temporary TXT record and waits for it to spread (a minute or two);
   - **caddy**: the site is checked with `caddy validate`, written, and Caddy reloaded;
   - **tunnel**: a tunnel named `86server-<hostname>` is created (or reused) and given a route for the hostname
     to `https://localhost:443`; routes for other hostnames on that tunnel are kept;
   - **dns**: a proxied CNAME `<hostname> -> <tunnel id>.cfargotunnel.com`. If the name already has other
     records, the step stops and lists them; tick **Replace existing DNS records** and run Set up again to
     replace them;
   - **cloudflared**: `86server-cloudflared.service` is restarted with the tunnel's token.

To change the hostname, select **Remove** first. Remove deletes the tunnel route, the DNS record (only if it still
points at our tunnel), stops cloudflared and deletes the Caddy site. The tunnel itself and the certificate files
stay unless you tick **Also delete the Cloudflare Tunnel**.

## Renewal

A daily timer runs `cli.py renew-certs` as the manager's account. When the certificate has fewer than 30 days left
it renews it the same way (DNS challenge through Cloudflare) and reloads Caddy. The API token must still be saved
for that to work. Run it by hand with:

```
sudo -u 86boxmgr python3 /opt/86server/manager/cli.py renew-certs
```

## Files

| Path | What |
| --- | --- |
| `/opt/86server/data/manager/secrets.json` | the API token (0600) |
| `/opt/86server/data/manager/panel.json` | the hostname, zone, tunnel id and certificate expiry |
| `/opt/86server/data/manager/lego/` | lego's ACME account and certificate store |
| `/opt/86server/data/certs/<hostname>.crt` and `.key` | the certificate Caddy uses |
| `/opt/86server/etc/caddy/panel.d/panel.caddy` | the Caddy site |
| `/opt/86server/data/manager/cloudflared.env` | the tunnel token cloudflared runs with (0600) |
