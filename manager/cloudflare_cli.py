"""`cli.py cloudflare ...`: the Cloudflare page for a terminal (used by `86server cloudflare`). Run as 86server.

token                 read an API token from stdin, check it with Cloudflare and save it
zones                 the zones the saved token can see, one per line
setup [options]       choose zones, the panel name and the tunnel, then apply (prints each step)
    --zones a.com,b.org   zones to use (each gets a wildcard certificate)
    --panel NAME          control panel name in --panel-zone ('@' = the zone itself); '' = none
    --panel-zone ZONE     zone for the panel name (added to --zones)
    --email ADDR          for Let's Encrypt     --staging     Let's Encrypt staging certificates
    --no-tunnel           no Cloudflare Tunnel (LAN access only, still with real certificates)
    --replace-dns         replace DNS records that already exist for these names
sync                  apply the saved setup again
off [--delete-tunnel] turn the tunnel off (DNS records removed, cloudflared stopped); certificates stay
status                what is set up
panel-url             https://<panel name>/ when one is set up, else nothing
"""
import argparse
import sys

import cloudflare
import config
import edge
import secrets_store


def _token():
    token = secrets_store.get(edge.TOKEN)
    if not token:
        raise SystemExit("No Cloudflare API token saved yet (86server cloudflare token).")
    return token


def cmd_token():
    value = sys.stdin.readline().strip()
    if not value or any(c.isspace() for c in value):
        print("Paste the token as one line.", file=sys.stderr)
        return 1
    try:
        zones = cloudflare.list_zones(value)
    except cloudflare.CFError as e:
        print("Token not saved: %s" % e, file=sys.stderr)
        return 1
    if not zones:
        print("Token not saved: it can see no active zones (needs Zone > Zone > Read).", file=sys.stderr)
        return 1
    secrets_store.put(edge.TOKEN, value, "cli")
    print("Token saved. It can see: %s" % ", ".join(z["name"] for z in zones))
    for z in zones:
        try:
            edge.check_dns_access(value, z["name"], z["id"])
        except ValueError as e:
            print("warning: %s" % e, file=sys.stderr)
    return 0


def cmd_zones():
    try:
        for z in cloudflare.list_zones(_token()):
            print(z["name"])
    except cloudflare.CFError as e:
        print(str(e), file=sys.stderr)
        return 1
    return 0


def cmd_setup(args):
    token = _token()
    try:
        zones = cloudflare.list_zones(token)
        state = edge.configure(edge.load(), zones, [z for z in args.zones.split(",") if z.strip()],
                               args.panel, args.panel_zone, args.email, args.staging, not args.no_tunnel)
    except (cloudflare.CFError, ValueError) as e:
        print(str(e), file=sys.stderr)
        return 1
    return 0 if edge.sync(state, token, args.replace_dns) else 1


def cmd_sync():
    state = edge.load()
    if not state["zones"]:
        print("Nothing set up yet (86server cloudflare setup).", file=sys.stderr)
        return 1
    return 0 if edge.sync(state, secrets_store.get(edge.TOKEN)) else 1


def cmd_off(delete):
    token = secrets_store.get(edge.TOKEN)
    if not edge.sync(dict(edge.load(), tunnel=False), token):
        return 1
    if delete:
        return 0 if edge._step(edge.PrintReport(), "tunnel", lambda: edge.delete_tunnel(edge.load(), token)) else 1
    return 0


def _print_status():
    s = edge.load()
    info = secrets_store.info(edge.TOKEN)
    print()
    print("API token:   %s" % ("ends in %s, set %s" % (info["last4"], info["set"]) if info else "none"))
    if not s["zones"]:
        print("Zones:       none")
        return
    for zone, z in sorted(s["zones"].items()):
        cert = z.get("cert") or {}
        print("Zone:        %s (certificate %s)" % (zone, "until %s%s" % (cert["expires"][:10],
                                                                         ", staging" if cert.get("staging") else "")
                                                     if cert.get("expires") else "not yet"))
    print("Panel:       %s" % ("https://%s/" % s["panel"] if s["panel"] else "none"))
    print("Tunnel:      %s" % ("off" if not s.get("tunnel") else "%s, routes: %s" % (
        s.get("tunnel_name") or "not created yet", ", ".join(s["dns"]) or "none")))
    print("Local DNS:   point %s at this server to skip the tunnel on your LAN"
          % " and ".join("%s, *.%s" % (z, z) for z in sorted(s["zones"])))


def main(argv):
    p = argparse.ArgumentParser(prog="86server cloudflare", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("token")
    sub.add_parser("zones")
    s = sub.add_parser("setup")
    s.add_argument("--zones", default="")
    s.add_argument("--panel", default="")
    s.add_argument("--panel-zone", default="")
    s.add_argument("--email", default="")
    s.add_argument("--staging", action="store_true")
    s.add_argument("--no-tunnel", action="store_true")
    s.add_argument("--replace-dns", action="store_true")
    sub.add_parser("sync")
    o = sub.add_parser("off")
    o.add_argument("--delete-tunnel", action="store_true")
    sub.add_parser("status")
    sub.add_parser("panel-url")
    args = p.parse_args(argv)
    if args.cmd == "token":
        return cmd_token()
    if args.cmd == "zones":
        return cmd_zones()
    if args.cmd == "setup":
        return cmd_setup(args)
    if args.cmd == "sync":
        return cmd_sync()
    if args.cmd == "off":
        return cmd_off(args.delete_tunnel)
    if args.cmd == "status":
        _print_status()
        return 0
    panel = config.panel_hostname()
    if panel:
        print("https://%s/" % panel)
    return 0
