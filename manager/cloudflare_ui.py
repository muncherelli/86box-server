"""Cloudflare page (admin): API token, zones with wildcard Let's Encrypt certificates, the control panel name and
the Cloudflare Tunnel. The work is done by edge.py; `86server cloudflare` does the same from a terminal."""
import time
import urllib.parse

import cloudflare
import config
import edge
import respond
import secrets_store
import store
import units

TOKEN = edge.TOKEN
_zones_cache = {"at": 0.0, "last4": "", "zones": None, "error": ""}


def _zones(refresh=False):
    """(zones, error) for the saved token, cached for a minute."""
    token = secrets_store.get(TOKEN)
    if not token:
        return [], ""
    c = _zones_cache
    if refresh or c["last4"] != token[-4:] or time.time() - c["at"] > 60 or c["zones"] is None:
        try:
            c.update(zones=cloudflare.list_zones(token), error="")
        except cloudflare.CFError as e:
            c.update(zones=[], error=str(e))
        c.update(at=time.time(), last4=token[-4:])
    return c["zones"], c["error"]


def _csrf(req):
    return '<input type="hidden" name="csrf" value="%s">' % respond.esc(req.session["csrf"])


def _token_section(req, error=""):
    e, info = respond.esc, secrets_store.info(TOKEN)
    if info:
        zones, zerr = _zones()
        state = ("Set on %s, ends in <code>%s</code>. %s" %
                 (e(info["set"]), e(info["last4"]),
                  '<span class="error">%s</span>' % e(zerr) if zerr else "It can see %d zone(s)." % len(zones)))
    else:
        state = "No token yet."
    remove = ('<form method="post" action="/manage/cloudflare/token/delete">%s<input type="submit" value="Remove token">'
              '</form>' % _csrf(req)) if info else ""
    return ('<h2>API token</h2><p>%s</p><p class="error">%s</p>'
            '<form method="post" action="/manage/cloudflare/token">%s'
            '<p><label>%s <input type="password" name="token" autocomplete="off" required size="48"></label> '
            '<input type="submit" value="Save"></p></form>%s'
            '<p>Permissions: Account &gt; Cloudflare Tunnel &gt; Edit, Zone &gt; DNS &gt; Edit, Zone &gt; Zone &gt; Read. '
            'See docs/cloudflare.md.</p>') % (state, e(error), _csrf(req), "Replace token" if info else "Token", remove)


def _job_section():
    st = edge.status()
    if not st["steps"]:
        return ""
    e = respond.esc
    rows = "".join('<tr><td>%s</td><td class="step-%s">%s</td><td><pre>%s</pre></td></tr>'
                   % (e(s["name"]), e(s["state"]), e(s["state"]), e(s["msg"])) for s in st["steps"])
    head = ("Working..." if st["running"] else
            "%s finished %s at %s." % (e(st["kind"].capitalize()), "OK" if st["ok"] else "with errors", e(st["done"])))
    return ('<h2>Last run</h2><p id="cf-job" data-running="%s">%s</p><table><tr><th>Step</th><th>State</th>'
            '<th>Details</th></tr>%s</table>') % ("1" if st["running"] else "0", head, rows)


def _status_section(req):
    s = edge.load()
    if not s["zones"]:
        return ""
    e = respond.esc
    lines = []
    for zone, z in sorted(s["zones"].items()):
        cert = z.get("cert") or {}
        lines.append("Zone <code>%s</code>: %s" % (e(zone), (
            "certificate for *.%s valid until %s%s" % (e(zone), e(cert.get("expires", "")[:10]),
                                                      " (staging, browsers will not trust it)" if cert.get("staging")
                                                      else "")) if cert else "no certificate yet"))
    if s["panel"]:
        lines.append('Control panel: <a href="https://%s/manage/">https://%s/</a>' % (e(s["panel"]), e(s["panel"])))
    if s.get("tunnel_id"):
        conns = "unknown"
        token = secrets_store.get(TOKEN)
        if token and s["dns"]:
            try:
                n = sum(len(c.get("conns") or []) for c in
                        cloudflare.tunnel_connections(token, edge._account(s), s["tunnel_id"]))
                conns = "%d active connection(s)" % n
            except (cloudflare.CFError, ValueError) as err:
                conns = "could not ask Cloudflare: %s" % err
        lines.append("Tunnel <code>%s</code>: %s; routes: %s" % (
            e(s.get("tunnel_name", "")), e(conns), ", ".join("<code>%s</code>" % e(h) for h in s["dns"]) or "none"))
    lines.append("Tunnel: %s; cloudflared %s" % ("on" if s.get("tunnel") else "off",
                                                 "running" if units.is_active(edge.CLOUDFLARED) else "not running"))
    lines.append("On your LAN, point local DNS for %s at this server's address to skip the tunnel: the certificates "
                 "are real, so browsers trust them there too." %
                 " and ".join("<code>%s</code> and <code>*.%s</code>" % (e(z), e(z)) for z in sorted(s["zones"])))
    buttons = ('<form method="post" action="/manage/cloudflare/sync">%s<input type="submit" value="Run again"></form>'
               % _csrf(req))
    if s.get("tunnel_id"):
        buttons += ('<form method="post" action="/manage/cloudflare/off">%s'
                    '<label><input type="checkbox" name="delete_tunnel"> and delete the tunnel at Cloudflare</label> '
                    '<input type="submit" value="Turn the tunnel off"></form>' % _csrf(req))
    return "<h2>Current setup</h2><ul>%s</ul>%s" % ("".join("<li>%s</li>" % l for l in lines), buttons)


def _setup_section(req, error=""):
    if not secrets_store.info(TOKEN):
        return "<h2>Set up</h2><p>Save an API token first.</p>"
    zones, zerr = _zones()
    if not zones:
        return "<h2>Set up</h2><p>The token sees no zones. %s</p>" % respond.esc(zerr)
    e, s = respond.esc, edge.load()
    panel_zone = config.zone_for(s["panel"], s["zones"]) if s["panel"] else ""
    name = "@" if s["panel"] and s["panel"] == panel_zone else (s["panel"][:-len(panel_zone) - 1] if panel_zone else "")
    checks = "".join('<label><input type="checkbox" name="zones" value="%s"%s> %s</label><br>'
                     % (e(z["name"]), " checked" if z["name"] in s["zones"] else "", e(z["name"])) for z in zones)
    opts = '<option value="">(none)</option>' + "".join(
        '<option value="%s"%s>%s</option>' % (e(z["name"]), " selected" if z["name"] == panel_zone else "", e(z["name"]))
        for z in zones)
    return ('<h2>Set up</h2><p class="error">%s</p>'
            '<form method="post" action="/manage/cloudflare/setup">%s'
            '<p>Zones to use (each gets a wildcard certificate; sites can be published one level below them):<br>%s</p>'
            '<p><label>Control panel name <input name="name" value="%s" placeholder="panel"></label> . '
            '<select name="zone" aria-label="Zone">%s</select> (use @ for the zone itself)</p>'
            '<p><label><input type="checkbox" name="tunnel"%s> Reach the panel and sites through a Cloudflare Tunnel '
            '(no open ports)</label></p>'
            '<p><label>Email for Let\'s Encrypt (optional) <input type="email" name="email" value="%s"></label></p>'
            '<p><label><input type="checkbox" name="staging"%s> Let\'s Encrypt staging (testing; browsers will not '
            'trust the certificates)</label></p>'
            '<p><label><input type="checkbox" name="replace_dns"> Replace existing DNS records for these names</label></p>'
            '<p><input type="submit" value="Save and apply"></p></form>'
            '<p>A new certificate takes a minute or two (a DNS record has to spread).</p>') % (
        e(error), _csrf(req), checks, e(name), opts, " checked" if s.get("tunnel", True) else "",
        e(s.get("email", "")), " checked" if s.get("staging") else "")


def _page(req, token_error="", setup_error=""):
    body = (_job_section() + _status_section(req) + _token_section(req, token_error) + _setup_section(req, setup_error)
            + '<script src="/manage/static/cloudflare.js"></script>')
    return respond.page(req, "Cloudflare", body)


# ---------- handlers ----------

def h_page(req, m):
    return _page(req)


def h_token(req, m):
    value = req.form().get("token", "").strip()
    try:
        if not value or any(c.isspace() for c in value):
            raise ValueError("Paste the token as one line.")
        zones = cloudflare.list_zones(value)
    except (ValueError, cloudflare.CFError) as e:
        return _page(req, token_error="Token not saved: %s" % e)
    if not zones:
        return _page(req, token_error="Token not saved: it can see no active zones (needs Zone > Zone > Read).")
    secrets_store.put(TOKEN, value, req.user["name"], req.ip)
    _zones(refresh=True)
    return respond.redirect("/manage/cloudflare")


def h_token_delete(req, m):
    secrets_store.delete(TOKEN, req.user["name"], req.ip)
    return respond.redirect("/manage/cloudflare")


def h_setup(req, m):
    token = secrets_store.get(TOKEN)
    if not token:
        return _page(req, setup_error="Save an API token first.")
    single = req.form()
    picked = urllib.parse.parse_qs(req.body().decode("utf-8", "replace")).get("zones", [])
    zones, zerr = _zones()
    if not zones:
        return _page(req, setup_error="The token sees no zones. %s" % zerr)
    try:
        state = edge.configure(edge.load(), zones, picked, single.get("name", ""), single.get("zone", ""),
                               single.get("email", ""), "staging" in single, "tunnel" in single)
    except ValueError as e:
        return _page(req, setup_error=str(e))
    if not edge.start("set up", edge.sync, state, token, "replace_dns" in single):
        return _page(req, setup_error="Another run is still going.")
    store.audit(req.user["name"], req.ip, "cloudflare-setup",
                ",".join(sorted(state["zones"])) + (" panel " + state["panel"] if state["panel"] else ""))
    return respond.redirect("/manage/cloudflare")


def h_sync(req, m):
    state = edge.load()
    if not state["zones"]:
        return respond.redirect("/manage/cloudflare")
    if not edge.start("run", edge.sync, state, secrets_store.get(TOKEN)):
        return _page(req, setup_error="Another run is still going.")
    return respond.redirect("/manage/cloudflare")


def _off(state, token, delete, report):
    if not edge.sync(dict(state, tunnel=False), token, report=report):
        return False
    if delete:
        return edge._step(report, "tunnel", lambda: edge.delete_tunnel(edge.load(), token))
    return True


def h_off(req, m):
    state = edge.load()
    if not edge.start("tunnel off", _off, state, secrets_store.get(TOKEN), "delete_tunnel" in req.form()):
        return _page(req, setup_error="Another run is still going.")
    store.audit(req.user["name"], req.ip, "cloudflare-tunnel-off", state.get("tunnel_name", ""))
    return respond.redirect("/manage/cloudflare")


def h_status(req, m):
    return respond.json_resp(edge.status())


ROUTES = [
    ("GET", r"/manage/cloudflare", h_page, "admin"),
    ("POST", r"/manage/cloudflare/token", h_token, "admin"),
    ("POST", r"/manage/cloudflare/token/delete", h_token_delete, "admin"),
    ("POST", r"/manage/cloudflare/setup", h_setup, "admin"),
    ("POST", r"/manage/cloudflare/sync", h_sync, "admin"),
    ("POST", r"/manage/cloudflare/off", h_off, "admin"),
    ("GET", r"/manage/api/cloudflare/status", h_status, "admin"),
]
