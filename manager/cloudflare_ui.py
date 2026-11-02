"""Cloudflare page (admin): API token, control panel hostname with a Let's Encrypt certificate, Cloudflare Tunnel."""
import re
import time

import cloudflare
import panel
import respond
import secrets_store
import store
import units

TOKEN = "cf_api_token"
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
    st = panel.status()
    if not st["steps"]:
        return ""
    e = respond.esc
    rows = "".join('<tr><td>%s</td><td class="step-%s">%s</td><td><pre>%s</pre></td></tr>'
                   % (e(s["name"]), e(s["state"]), e(s["state"]), e(s["msg"])) for s in st["steps"])
    head = ("Working..." if st["running"] else
            "%s finished %s at %s." % (e(st["kind"].capitalize()), "OK" if st["ok"] else "with errors", e(st["done"])))
    return ('<h2>Last job</h2><p id="cf-job" data-running="%s">%s</p><table><tr><th>Step</th><th>State</th>'
            '<th>Details</th></tr>%s</table>') % ("1" if st["running"] else "0", head, rows)


def _status_section(req):
    s = panel.load()
    if not s.get("hostname"):
        return ""
    e = respond.esc
    cert = s.get("cert") or {}
    lines = ["Hostname: <code>%s</code> (%s)" % (e(s["hostname"]), "active" if s.get("active") else "not active"),
             "Certificate: %s" % (("valid until %s, issuer %s%s" % (e(cert.get("expires", "")), e(cert.get("issuer", "")),
                                   " (staging, browsers will not trust it)" if s.get("staging") else ""))
                                  if cert else "none yet")]
    if s.get("tunnel_id"):
        conns = "unknown"
        token = secrets_store.get(TOKEN)
        if token and s.get("active"):
            try:
                n = sum(len(c.get("conns") or []) for c in
                        cloudflare.tunnel_connections(token, s["account_id"], s["tunnel_id"]))
                conns = "%d active connection(s)" % n
            except cloudflare.CFError as err:
                conns = "could not ask Cloudflare: %s" % err
        lines.append("Tunnel: <code>%s</code> (%s), %s" % (e(s.get("tunnel_name", "")), e(s["tunnel_id"]), e(conns)))
    lines.append("cloudflared: %s" % ("running" if units.is_active(panel.CLOUDFLARED) else "not running"))
    if s.get("active"):
        lines.append("On your LAN, point your local DNS for <code>%s</code> at this server's address: "
                     "the certificate is real, so it works there too." % e(s["hostname"]))
    remove = ('<form method="post" action="/manage/cloudflare/remove">%s'
              '<label><input type="checkbox" name="delete_tunnel"> Also delete the Cloudflare Tunnel</label> '
              '<input type="submit" value="Remove"></form>' % _csrf(req))
    return "<h2>Current setup</h2><ul>%s</ul>%s" % ("".join("<li>%s</li>" % l for l in lines), remove)


def _setup_section(req, error=""):
    if not secrets_store.info(TOKEN):
        return "<h2>Control panel hostname</h2><p>Save an API token first.</p>"
    zones, zerr = _zones()
    if not zones:
        return "<h2>Control panel hostname</h2><p>The token sees no zones. %s</p>" % respond.esc(zerr)
    e, s = respond.esc, panel.load()
    opts = "".join('<option value="%s"%s>%s</option>' % (e(z["id"]), " selected" if z["id"] == s.get("zone_id") else "",
                                                        e(z["name"])) for z in zones)
    name = s["hostname"][:-len(s["zone_name"]) - 1] if s.get("hostname", "").endswith("." + s.get("zone_name", "-")) \
        else ("@" if s.get("hostname") and s.get("hostname") == s.get("zone_name") else "")
    tunnel_on = s.get("tunnel", True)
    return ('<h2>Control panel hostname</h2><p class="error">%s</p>'
            '<form method="post" action="/manage/cloudflare/setup">%s'
            '<p><label>Name <input name="name" value="%s" placeholder="panel" required></label> . '
            '<select name="zone" aria-label="Zone">%s</select> (use @ for the zone itself)</p>'
            '<p><label>Email for Let\'s Encrypt (optional) <input type="email" name="email" value="%s"></label></p>'
            '<p><label><input type="checkbox" name="staging"%s> Let\'s Encrypt staging (testing; browsers will not '
            'trust the certificate)</label></p>'
            '<p><label><input type="checkbox" name="tunnel"%s> Also expose through a Cloudflare Tunnel</label></p>'
            '<p><label><input type="checkbox" name="replace_dns"> Replace existing DNS records for this name</label></p>'
            '<p><input type="submit" value="Set up"></p></form>'
            '<p>Getting the certificate takes a minute or two (a DNS record has to spread).</p>') % (
        e(error), _csrf(req), e(name), opts, e(s.get("email", "")), " checked" if s.get("staging") else "",
        " checked" if tunnel_on else "")


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
    f, token = req.form(), secrets_store.get(TOKEN)
    if not token:
        return _page(req, setup_error="Save an API token first.")
    zones, zerr = _zones()
    zone = next((z for z in zones if z["id"] == f.get("zone")), None)
    if zone is None:
        return _page(req, setup_error="Pick one of the token's zones. %s" % zerr)
    try:
        host = panel.hostname_for(f.get("name", ""), zone["name"])
    except ValueError as e:
        return _page(req, setup_error=str(e))
    email = f.get("email", "").strip()
    if email and (len(email) > 254 or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email)):
        return _page(req, setup_error="That email address does not look right.")
    old = panel.load()
    if old.get("active") and old.get("hostname") != host:
        return _page(req, setup_error="Remove the current setup (%s) first." % old["hostname"])
    state = dict(old, hostname=host, zone_id=zone["id"], zone_name=zone["name"], account_id=zone["account_id"],
                 email=email, staging="staging" in f, tunnel="tunnel" in f)
    if not panel.start("set up", panel.setup, panel.STEPS, state, token, "replace_dns" in f):
        return _page(req, setup_error="Another job is still running.")
    store.audit(req.user["name"], req.ip, "panel-setup", host)
    return respond.redirect("/manage/cloudflare")


def h_remove(req, m):
    state = panel.load()
    if not state.get("hostname"):
        return respond.redirect("/manage/cloudflare")
    if not panel.start("remove", panel.remove, panel.REMOVE_STEPS, state, secrets_store.get(TOKEN),
                       "delete_tunnel" in req.form()):
        return _page(req, setup_error="Another job is still running.")
    store.audit(req.user["name"], req.ip, "panel-remove", state["hostname"])
    return respond.redirect("/manage/cloudflare")


def h_status(req, m):
    return respond.json_resp(panel.status())


ROUTES = [
    ("GET", r"/manage/cloudflare", h_page, "admin"),
    ("POST", r"/manage/cloudflare/token", h_token, "admin"),
    ("POST", r"/manage/cloudflare/token/delete", h_token_delete, "admin"),
    ("POST", r"/manage/cloudflare/setup", h_setup, "admin"),
    ("POST", r"/manage/cloudflare/remove", h_remove, "admin"),
    ("GET", r"/manage/api/cloudflare/status", h_status, "admin"),
]
