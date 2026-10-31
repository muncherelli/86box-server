"""The Network groups page (admin only): list, create, edit and delete private networks for boxes."""
import urllib.parse

import config
import netgroups_store as ns
import respond
import store
from netgroups_store import NetError

NAME = r"([a-z][a-z0-9]{0,11})"
EXPLAIN = ("A network group is a private virtual switch. Boxes in the same group can talk to each other and to "
           "nothing else, unless you allow more below.")


def _csrf(req):
    return '<input type="hidden" name="csrf" value="%s">' % respond.esc(req.session["csrf"])


def _form(req):
    data = urllib.parse.parse_qs(req.body().decode("utf-8", "replace"), keep_blank_values=True)
    return data


def _one(data, key):
    return (data.get(key) or [""])[0].strip()


def _banner(state, done=False):
    if state["state"] == "applying":
        return '<p id="net-apply" class="notice">Applying the change... this takes a few seconds.</p>'
    if state["state"] == "error":
        return '<p class="error">The last change failed: %s</p>' % respond.esc(state["message"])
    out = ""
    if done and state["state"] == "ok":
        out = '<p class="notice">Done. %s</p>' % respond.esc(state["message"])
    for w in state["warnings"] if done else []:
        out += '<p class="error">Note: %s</p>' % respond.esc(w)
    return out


# ---- list

def _list_page(req, msg="", done=False, code=200):
    groups = ns.groups()
    state = ns.status()
    body = '<p class="hint">%s</p>' % respond.esc(EXPLAIN)
    body += ('<p class="error">%s</p>' % respond.esc(msg)) if msg else ""
    body += _banner(state, done)
    rows = []
    for name, g in sorted(groups.items()):
        q = respond.esc(name)
        users = ns.vms_using(name)
        try:
            gateway = ns.guest_help(g["cidr"], False, [])["gateway"]
        except ValueError:
            gateway = "?"
        rows.append(
            "<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td>"
            '<td><a href="/manage/netgroups/edit/%s">Edit</a> <a href="/manage/netgroups/edit/%s#delete">Delete</a></td></tr>'
            % (q, respond.esc(g["cidr"]), respond.esc(gateway), "Through this host" if g["egress"] == "nat" else "Off",
               "Yes" if g["dhcp"] else "No", respond.esc(", ".join(ns.reaches(name)) or "none"),
               respond.esc(", ".join(users) or "none"),
               "ready" if ns.bridge_present(g["bridge"]) else "not created yet", q, q))
    if rows:
        body += ("<table><tr><th>Name</th><th>Network</th><th>Host address (guest gateway)</th><th>Internet</th>"
                 "<th>Automatic addresses (DHCP)</th><th>Can also reach</th><th>Boxes using it</th><th>Status</th>"
                 "<th></th></tr>%s</table>" % "".join(rows))
    else:
        body += "<p>No network groups yet. Boxes without a group have no network.</p>"
    body += '<p><a href="/manage/netgroups/new">New network group</a></p>'
    return respond.page(req, "Network groups", body, code)


def h_list(req, m):
    return _list_page(req, done=bool((getattr(req, "query", None) or {}).get("done")))


# ---- form

def _form_page(req, name="", values=None, msg="", code=200):
    groups = ns.groups()
    editing = name in groups
    v = values or {}
    others = {n: g for n, g in groups.items() if n != name}
    current = groups.get(name, {})
    pol_dns = config.netpolicy()["nat_dns"]
    suggestions = ns.suggest_subnets(others)
    choices = ([current["cidr"]] if editing else []) + [c for c in suggestions if c != current.get("cidr")]
    chosen = v.get("choice") or (choices[0] if choices else "custom")
    opts = "".join('<option value="%s"%s>%s</option>' % (respond.esc(c), " selected" if c == chosen else "",
                                                          respond.esc(c + (" (current)" if c == current.get("cidr") else "")))
                   for c in choices)
    opts += '<option value="custom"%s>Custom...</option>' % (" selected" if chosen == "custom" else "")
    reach_now = set(v["reach"]) if "reach" in v else set(ns.reaches(name))
    nat = v["nat"] if "nat" in v else current.get("egress") == "nat"
    dhcp = v["dhcp"] if "dhcp" in v else bool(current.get("dhcp"))
    reach_boxes = "".join('<label><input type="checkbox" name="reach" value="%s"%s> %s</label> '
                          % (respond.esc(n), " checked" if n in reach_now else "", respond.esc(n)) for n in sorted(others)) \
        or "<em>There are no other groups yet.</em>"
    use_cidr = chosen if chosen != "custom" else (v.get("custom") or (suggestions[0] if suggestions else "10.86.0.0/24"))
    try:
        h = ns.guest_help(use_cidr, nat, pol_dns)
    except ValueError:
        h = ns.guest_help("10.86.0.0/24", nat, pol_dns)
    body = ('<p class="error">%s</p>' % respond.esc(msg)) if msg else ""
    users = ns.vms_using(name) if editing else []
    if users:
        body += ('<p class="error">%s use this group. If you change its network, their guests need new addresses.</p>'
                 % respond.esc(", ".join(users)))
    name_field = ('<input name="name" value="%s" readonly> <span class="hint">Groups cannot be renamed. To rename one, '
                  'create a new group and delete this one.</span>' % respond.esc(name)) if editing else \
        ('<input name="name" value="%s" required pattern="[a-z][a-z0-9]{0,11}" placeholder="lab"> '
         '<span class="hint">Up to 12 letters and digits, starting with a letter.</span>' % respond.esc(v.get("name", "")))
    body += ('<p class="hint">%s</p><form method="post" action="/manage/netgroups/save">%s'
             '<input type="hidden" name="mode" value="%s">'
             '<p>Name %s</p>'
             '<p>Network <select name="choice">%s</select> <input name="custom" value="%s" placeholder="10.86.5.0/24"> '
             '<span class="hint">Pick one, or choose Custom and type a private network such as 10.86.5.0/24 '
             '(sizes /16 to /29).</span></p>'
             '<p><label><input type="checkbox" name="nat" value="1"%s> Let boxes in this group reach the internet through '
             'this host</label><br><span class="hint">Off by default. Guests then use the host as their gateway.</span></p>'
             '<p><label><input type="checkbox" name="dhcp" value="1"%s> Give boxes addresses automatically (DHCP)</label>'
             '<br><span class="hint">Otherwise type a fixed address inside each guest.</span></p>'
             '<p>Also let this group talk to: %s</p>'
             '<h3>Inside the guest</h3><p class="hint">For %s: address, for example <b>%s</b>; subnet mask <b>%s</b>; '
             'gateway <b>%s</b> (this host); DNS <b>%s</b>.</p>'
             '<input type="submit" value="%s"></form>'
             % (respond.esc(EXPLAIN), _csrf(req), "edit" if editing else "create", name_field, opts,
                respond.esc(v.get("custom", "")), " checked" if nat else "", " checked" if dhcp else "", reach_boxes,
                respond.esc(use_cidr), respond.esc(h["example"]), respond.esc(h["mask"]), respond.esc(h["gateway"]),
                respond.esc(h["dns"] or "none (no internet)"), "Save" if editing else "Create"))
    if editing:
        body += ('<h2 id="delete">Delete</h2>')
        if users:
            body += '<p>This group cannot be deleted while boxes use it: %s. Remove it from their settings first.</p>' \
                % respond.esc(", ".join(users))
        else:
            body += ('<form method="post" action="/manage/netgroups/delete/%s">%s'
                     '<p>Type the group name to confirm: <input name="confirm" autocomplete="off"></p>'
                     '<input type="submit" value="Delete group"></form>' % (respond.esc(name), _csrf(req)))
    return respond.page(req, ("Edit network group " + name) if editing else "New network group", body, code)


def h_new(req, m):
    return _form_page(req)


def h_edit(req, m):
    name = m.group(1)
    if name not in ns.groups():
        return respond.text("not found", 404)
    return _form_page(req, name)


# ---- changes

def h_save(req, m):
    data = _form(req)
    groups = ns.groups()
    mode, name = _one(data, "mode"), _one(data, "name")
    values = {"name": name, "choice": _one(data, "choice"), "custom": _one(data, "custom"),
              "nat": _one(data, "nat") == "1", "dhcp": _one(data, "dhcp") == "1", "reach": data.get("reach", [])}
    if values["choice"] == "custom":
        values["cidr"] = values["custom"]
    else:
        values["cidr"] = values["choice"]
    try:
        if mode == "create":
            if not ns.NAME_RE.fullmatch(name):
                raise NetError("The name must be 1 to 12 letters and digits, starting with a letter (a-z, 0-9).")
            if name in groups:
                raise NetError("A group called %s already exists." % name)
        elif name not in groups:
            raise NetError("No such group.")
        others = {n: g for n, g in groups.items() if n != name}
        cidr = ns.parse_cidr(values["cidr"], others)
        reach = values["reach"]
        for r in reach:
            if r not in others:
                raise NetError("Unknown group: %s" % r)
        body = ns.desired(groups, name, {"cidr": cidr, "egress": "nat" if values["nat"] else "none",
                                         "dhcp": values["dhcp"]}, reach)
        ns.submit(body)
    except NetError as e:
        return _form_page(req, name if name in groups else "", values, str(e), 400)
    store.audit(req.user["name"], req.ip, "netgroup.save", name)
    return respond.redirect("/manage/netgroups")


def h_delete(req, m):
    name = m.group(1)
    groups = ns.groups()
    if name not in groups:
        return respond.text("not found", 404)
    if _one(_form(req), "confirm") != name:
        return _form_page(req, name, None, "Type the group name exactly to delete it.", 400)
    try:
        users = ns.vms_using(name)
        if users:
            raise NetError("These boxes still use the group: %s." % ", ".join(users))
        ns.submit(ns.desired(groups, name, {}, [], remove=True))
    except NetError as e:
        return _form_page(req, name, None, str(e), 400)
    store.audit(req.user["name"], req.ip, "netgroup.delete", name)
    return respond.redirect("/manage/netgroups")


def h_status(req, m):
    return respond.json_resp(ns.status())


ROUTES = [
    ("GET", r"/manage/netgroups", h_list, "admin"),
    ("GET", r"/manage/netgroups/new", h_new, "admin"),
    ("GET", r"/manage/netgroups/edit/" + NAME, h_edit, "admin"),
    ("POST", r"/manage/netgroups/save", h_save, "admin"),
    ("POST", r"/manage/netgroups/delete/" + NAME, h_delete, "admin"),
    ("GET", r"/manage/api/netgroups/status", h_status, "admin"),
]
