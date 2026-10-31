"""Pages and handlers for published sites (admin only). Imported only by sites.py."""
import os
import re
import urllib.parse

import config
import respond
import store
import sites
from sites import METHODS, MAX_BODIES, PRESETS, SCHEMES, DEFAULT_RULES, apply_sites, check_site
from sites import VM_RE, load_sites, preset_items, save_sites, site_from_form

HTTP_WARNING = ("Traffic between the visitor and this server is not encrypted; "
                "use it only on a trusted network or behind a tunnel.")


def _vm_names():
    d = os.path.join(config.CFG["data_root"], "boxdefs")
    try:
        return sorted(n[:-5] for n in os.listdir(d) if n.endswith(".json") and re.fullmatch(VM_RE, n[:-5]))
    except OSError:
        return []


def _checked(on):
    return " checked" if on else ""


def _forwarding(s, e):
    vms = "".join('<option%s>%s</option>' % (" selected" if v == s.get("vm") else "", e(v)) for v in _vm_names())
    schemes = "".join('<option%s>%s</option>' % (" selected" if x == s.get("scheme", "https") else "", x)
                      for x in SCHEMES)
    return (
        '<fieldset><legend>Forwarding: where the traffic goes</legend>'
        '<p>Host name <input name="host" value="%s" required></p>'
        '<p>Box <select name="vm">%s</select> Guest IP <input name="guest_ip" value="%s" required> '
        'Port <input name="port" value="%s" size="5"></p>'
        '<p>Host header <input name="host_header" value="%s"> (original, or a name)</p>'
        '<p>Strip prefix <input name="strip_prefix" value="%s"> (one of the allowed paths)</p>'
        '<p>Public side <select name="scheme">%s</select> '
        '<span class="hint">https gets a Let\'s Encrypt certificate. http is plain text: %s</span></p>'
        '</fieldset>'
    ) % (e(s.get("host", "")), vms, e(s.get("guest_ip", "")), e(s.get("port", 80)),
         e(s.get("host_header", "original")), e(s.get("rules", {}).get("strip_prefix", "")), schemes,
         e(HTTP_WARNING))


def _filtering(rules, e):
    meth = "".join('<label><input type="checkbox" name="methods" value="%s"%s> %s</label> '
                   % (m, _checked(m in rules["methods"]), m) for m in METHODS)
    bodies = "".join("<option%s>%s</option>" % (" selected" if b == rules["max_body"] else "", b) for b in MAX_BODIES)
    presets = ""
    for name, p in PRESETS.items():
        on = rules["profiles"].get(name, {})
        items = "".join('<label><input type="checkbox" name="%s_%s"%s> %s</label> '
                        % (name, k, _checked(on.get(k)), k) for k in preset_items(name))
        presets += '<p>%s (off unless ticked): %s</p>' % (e(p["label"]), items)
    return (
        '<fieldset><legend>Filtering: what may pass</legend>'
        '<p>Allowed methods: %s</p>'
        '<p>Allowed paths (one per line)<br><textarea name="allow_paths" rows="3">%s</textarea></p>'
        '<p>Body limit <select name="max_body">%s</select></p>'
        '<p><label><input type="checkbox" name="block_encoded"%s> Block encoded and traversal URLs</label></p>'
        '%s</fieldset>'
    ) % (meth, e("\n".join(rules["allow_paths"])), bodies, _checked(rules["block_encoded"]), presets)


def _form(req, site, error=""):
    s = dict(site)
    rules = dict(DEFAULT_RULES, **s.get("rules", {}))
    e = respond.esc
    return (
        '<p class="error">%s</p><form method="post" action="/manage/sites/save">'
        '<input type="hidden" name="csrf" value="%s">%s%s'
        '<p><label><input type="checkbox" name="enabled"%s> Enabled</label> <input type="submit" value="Save"></p></form>'
    ) % (e(error), e(req.session["csrf"]), _forwarding(s, e), _filtering(rules, e), _checked(s.get("enabled", True)))


def _list(req, error=""):
    e = respond.esc
    csrf = '<input type="hidden" name="csrf" value="%s">' % e(req.session["csrf"])
    rows = ""
    for s in load_sites():
        h = e(s["host"])
        rows += ('<tr><td><a href="/manage/sites?edit=%s">%s</a></td><td>%s</td><td>%s:%s</td><td>%s</td><td>%s</td><td>'
                 '<form method="post" action="/manage/sites/%s/toggle">%s<input type="submit" value="%s"></form> '
                 '<form method="post" action="/manage/sites/%s/delete">%s<input type="submit" value="Delete"></form></td></tr>'
                 % (h, h, e(s["vm"]), e(s["guest_ip"]), e(s["port"]), e(s.get("scheme", "https")),
                    "on" if s.get("enabled", True) else "off",
                    h, csrf, "Disable" if s.get("enabled", True) else "Enable", h, csrf))
    edit = next((s for s in load_sites() if s["host"] == req.query.get("edit", [""])[0]), {})
    body = ('<table><tr><th>Host</th><th>Box</th><th>Upstream</th><th>Public side</th><th>State</th><th></th></tr>%s</table>'
            '<h2>%s</h2>%s') % (rows, "Edit site" if edit else "Publish a site", _form(req, edit, error))
    return respond.page(req, "Sites", body)


def _commit(req, new_sites, action, host):
    old = load_sites()
    save_sites(new_sites)
    ok, msg = apply_sites(new_sites)
    if not ok:
        save_sites(old)
        return False, msg
    store.audit(req.user["name"], req.ip, action, host)
    return True, msg


def h_list(req, m):
    return _list(req)


def h_save(req, m):
    data = urllib.parse.parse_qs(req.body().decode("utf-8", "replace"), keep_blank_values=True)
    try:
        site = check_site(site_from_form(data))
    except ValueError as err:
        return _list(req, str(err))
    new_sites = [s for s in load_sites() if s["host"] != site["host"]] + [site]
    ok, msg = _commit(req, new_sites, "site_save", site["host"])
    return respond.redirect("/manage/sites") if ok else _list(req, msg)


def h_delete(req, m):
    host = m.group(1)
    ok, msg = _commit(req, [s for s in load_sites() if s["host"] != host], "site_delete", host)
    return respond.redirect("/manage/sites") if ok else _list(req, msg)


def h_toggle(req, m):
    host = m.group(1)
    new_sites = [dict(s, enabled=not s.get("enabled", True)) if s["host"] == host else s for s in load_sites()]
    ok, msg = _commit(req, new_sites, "site_toggle", host)
    return respond.redirect("/manage/sites") if ok else _list(req, msg)


ROUTES = [
    ("GET", r"/manage/sites", h_list, "admin"),
    ("POST", r"/manage/sites/save", h_save, "admin"),
    ("POST", r"/manage/sites/([a-z0-9.-]+)/delete", h_delete, "admin"),
    ("POST", r"/manage/sites/([a-z0-9.-]+)/toggle", h_toggle, "admin"),
]
