"""Pages and handlers for the box list, new/edit forms and the status API. Imported only by vms.py."""
import importlib
import os
import urllib.parse

import config
import respond
import store
import vms
from vms import VmError, _p, _read_cfg, all_defs, busy, console_url, create_vm, delete_vm, edit_vm
from vms import empty_trash, list_isos, list_templates, status, trash_size, visible, do_action


# ---- pages

def _csrf(req):
    return '<input type="hidden" name="csrf" value="%s">' % respond.esc(req.session["csrf"])


def _list_page(req, msg=""):
    names = visible(req.user)
    admin = req.user["role"] == "admin"
    rows = []
    for n in names:
        s = status(n)
        q = respond.esc(n)
        buttons = "".join('<button data-action="%s" data-vm="%s">%s</button> ' % (a, q, a.title())
                          for a in ("start", "stop", "reset"))
        links = '<a href="%s" target="_blank" rel="noopener">Console</a> <a href="/manage/boxes/%s/settings">Settings</a>' % (respond.esc(console_url(n)), q)
        if admin:
            links += ' <a href="/manage/boxes/%s">Edit</a>' % q
        rows.append('<tr data-vm="%s"><td>%s</td><td data-state>%s</td><td data-nic>%s</td>'
                    '<td data-cpu></td><td data-ram></td><td data-disk></td><td data-guard class="error">%s</td><td>%s %s</td></tr>'
                    % (q, q, respond.esc(s["state"]), respond.esc(s["nic"]), respond.esc(s["guard"]), buttons, links))
    body = '<p class="error">%s</p>' % respond.esc(msg) if msg else ""
    body += ('<table id="vm-table"><tr><th>Box</th><th>State</th><th>NIC</th><th>CPU</th><th>RAM</th><th>Disk</th><th>Problems</th><th></th></tr>%s</table>'
             % "".join(rows)) if rows else "<p>No boxes yet.</p>"
    body += ('<dialog id="confirm"><p id="confirm-text"></p><button id="confirm-yes">Yes</button> '
             '<button id="confirm-no">Cancel</button></dialog>')
    if admin:
        mb = trash_size() / 1048576
        body += ('<p><a href="/manage/boxes/new">New box</a></p><form method="post" action="/manage/trash/empty">%s'
                 'Trash: %.0f MB <input type="submit" value="Empty trash"></form>' % (_csrf(req), mb))
    return respond.page(req, "Boxes", body)


def _checks(name, items, chosen, label):
    return "".join('<label><input type="checkbox" name="%s" value="%s"%s> %s</label> '
                   % (name, respond.esc(i), " checked" if i in chosen else "", respond.esc(i)) for i in items) \
        or "<em>none available</em>"


def _group_checks(chosen):
    """One checkbox per network group, with its subnet, and a link to manage them."""
    groups = config.netgroups()
    if not groups:
        return '<em>No groups yet</em> - <a href="/manage/netgroups/new">create one</a>'
    boxes = "".join('<label><input type="checkbox" name="group" value="%s"%s> %s <small>%s</small></label> '
                    % (respond.esc(n), " checked" if n in chosen else "", respond.esc(n), respond.esc(g.get("cidr", "")))
                    for n, g in sorted(groups.items()))
    return boxes + ' <a href="/manage/netgroups">Manage network groups</a>'


def _cd_select(current):
    opts = ['<option value="">(none)</option>'] + ['<option%s>%s</option>' % (" selected" if f == current else "", respond.esc(f))
                                                    for f in list_isos()]
    return '<select name="cd">%s</select>' % "".join(opts)


def _new_page(req, msg="", status_code=200):
    tpls = list_templates()
    opts = '<option value="">Blank: no template, set everything in Settings</option>' + "".join(
        '<option value="%s">%s</option>' % (respond.esc(k), respond.esc(v["name"])) for k, v in tpls.items())
    body = ('<p class="error">%s</p>' % respond.esc(msg)) if msg else ""
    body += ('<form method="post" action="/manage/boxes/new">%s'
             '<p>Name <input name="name" required pattern="[a-z][a-z0-9-]{1,30}"></p>'
             '<p>Template <select name="template">%s</select></p>'
             '<p class="hint">A template only picks the board and CPU. Disks, video, sound and the rest are set in '
             'the 86Box Settings window.</p>'
             '<p>Network groups %s</p>'
             '<p class="hint">A network group is a private virtual switch. Boxes in the same group can talk to each other '
             'and nothing else; they get no internet unless the group allows it. With no group the box has no network: '
             'a card set to network type None does nothing, and type TAP needs a group.</p>'
             '<p>Console users %s</p>'
             '<p><label><input type="checkbox" name="autostart" value="1"> Start at boot</label></p>'
             '<input type="submit" value="Create"></form>'
             % (_csrf(req), opts, _group_checks(()),
                _checks("user", sorted(vms._auth().console_users()), (), "user")))
    status_, headers, out = respond.page(req, "New box", body, status_code)
    return status_, headers, out


def _resolution_field(current):
    """Display size: a dropdown of Auto, the presets and Custom; Custom shows a plain WxH text box."""
    custom = current not in ["auto"] + vms.RESOLUTION_PRESETS

    def opt(value, label):
        sel = " selected" if value == ("custom" if custom else current) else ""
        return '<option value="%s"%s>%s</option>' % (value, sel, label)

    def group(label, sizes):
        return '<optgroup label="%s">%s</optgroup>' % (label, "".join(opt(r, r.replace("x", " × ")) for r in sizes))

    options = (opt("auto", "Auto (follow the browser window)") + group("4:3 and classic", vms.RESOLUTION_PRESETS[:8])
               + group("Widescreen", vms.RESOLUTION_PRESETS[8:]) + opt("custom", "Custom…"))
    return ('<p><label>Display size <select name="resolution" id="b86res">%s</select></label> '
            '<input name="resolution_custom" id="b86res-custom" value="%s" placeholder="WxH, e.g. 1280x960" size="16"'
            ' aria-label="Custom display size"%s>'
            '<br><small><b>Auto</b> makes the display follow your browser window, live. A fixed size is scaled to fit '
            'the window and applies the next time the box starts. Custom takes any WxH from 640x400 to 2560x1600. '
            '86Box stretches whatever the guest shows to fill this display.</small></p>'
            '<script>(function(){var s=document.getElementById("b86res"),c=document.getElementById("b86res-custom");'
            's.onchange=function(){c.hidden=s.value!=="custom";if(!c.hidden)c.focus();};})();</script>'
            % (options, respond.esc(current if custom else ""), "" if custom else " hidden"))


def _edit_page(req, name, msg=""):
    data = all_defs().get(name)
    if data is None:
        return respond.text("not found", 404)
    users = [u for u, v in vms._auth().console_users().items() if name in v.get("vms", [])]
    cfg = _read_cfg(_p("boxes", name, "86box.cfg")) if os.path.exists(_p("boxes", name, "86box.cfg")) else None
    current = ""
    if cfg and cfg.has_option("Floppy and CD-ROM drives", "cdrom_01_image_path"):
        current = os.path.basename(cfg.get("Floppy and CD-ROM drives", "cdrom_01_image_path"))
    q = respond.esc(name)
    cd = ""
    body = ('<p class="error">%s</p>' % respond.esc(msg)) if msg else ""
    body += ('<form method="post" action="/manage/boxes/%s">%s%s<p>Network groups %s</p><p>Console users %s</p>'
             '<p><label><input type="checkbox" name="autostart" value="1"%s> Start at boot</label></p>'
             '%s'
             '<input type="submit" value="Save"></form>'
             '<h2>Delete</h2><form method="post" action="/manage/boxes/%s/delete">%s'
             '<p>Type the name to confirm: <input name="confirm"> <input type="submit" value="Delete"></p></form>'
             % (q, _csrf(req), cd, _group_checks(data.get("allowed_groups", [])),
                _checks("user", sorted(vms._auth().console_users()), users, "user"),
                " checked" if data.get("autostart") else "", _resolution_field(data.get("resolution", "auto")),
                q, _csrf(req)))
    for mod, fn in (("snapshots", "html_section"),
                    ("clone", "html_section"), ("importexp", "html_section")):
        body += getattr(importlib.import_module(mod), fn)(req, name)
    return respond.page(req, "Box " + name, body)


def _multi(req):
    return urllib.parse.parse_qs(req.body().decode("utf-8", "replace"), keep_blank_values=True)


# ---- handlers

def h_list(req, m):
    return _list_page(req)


def h_root(req, m):
    return respond.redirect("/manage/")


def h_new_get(req, m):
    return _new_page(req)


def h_new_post(req, m):
    f = _multi(req)
    one = lambda k: (f.get(k) or [""])[0]
    try:
        name = create_vm(one("name"), one("template"), "", "", f.get("group", []),
                         one("autostart") == "1", f.get("user", []))
    except VmError as e:
        return _new_page(req, str(e), 400)
    store.audit(req.user["name"], req.ip, "vm.create", name)
    return respond.redirect("/manage/")


def h_edit_get(req, m):
    return _edit_page(req, m.group(1))


def _posted_resolution(f):
    """The dropdown's value, or the custom text box when the dropdown says Custom."""
    res = (f.get("resolution") or [""])[0]
    if res != "custom":
        return res
    res = (f.get("resolution_custom") or [""])[0].strip()
    if not res:
        raise VmError("Type a custom display size as WxH, for example 1280x960.")
    return res


def h_edit_post(req, m):
    name, f = m.group(1), _multi(req)
    try:
        edit_vm(name, f.get("group", []), (f.get("autostart") or [""])[0] == "1", f.get("user", []),
                resolution=_posted_resolution(f))
    except VmError as e:
        return _edit_page(req, name, str(e))
    store.audit(req.user["name"], req.ip, "vm.edit", name)
    return respond.redirect("/manage/")


def h_delete(req, m):
    name = m.group(1)
    if (_multi(req).get("confirm") or [""])[0] != name:
        return _edit_page(req, name, "Type the box name exactly to delete it.")
    try:
        delete_vm(name)
    except VmError as e:
        return _edit_page(req, name, str(e))
    store.audit(req.user["name"], req.ip, "vm.delete", name)
    return respond.redirect("/manage/")


def h_trash(req, m):
    empty_trash()
    store.audit(req.user["name"], req.ip, "trash.empty")
    return respond.redirect("/manage/")


def h_api_list(req, m):
    return respond.json_resp({"vms": [status(n) for n in visible(req.user)]})


def h_api_action(req, m):
    name, action = m.group(1), m.group(2)
    if name not in all_defs() or not vms._auth().can_use_vm(req.user, name):
        return respond.json_resp({"ok": False, "error": "forbidden"}, 403)
    ok, err = do_action(name, action)
    store.audit(req.user["name"], req.ip, "vm." + action, name)
    return respond.json_resp({"ok": ok, "error": err})


N = r"([a-z][a-z0-9-]{1,30})"
ROUTES = [
    ("GET", r"/manage", h_root, None),
    ("GET", r"/manage/", h_list, "any"),
    ("GET", r"/manage/boxes/new", h_new_get, "admin"),
    ("POST", r"/manage/boxes/new", h_new_post, "admin"),
    ("GET", r"/manage/boxes/" + N, h_edit_get, "admin"),
    ("POST", r"/manage/boxes/" + N, h_edit_post, "admin"),
    ("POST", r"/manage/boxes/%s/delete" % N, h_delete, "admin"),
    ("POST", r"/manage/trash/empty", h_trash, "admin"),
    ("GET", r"/manage/api/boxes", h_api_list, "any"),
    ("POST", r"/manage/api/box/%s/(start|stop|reset)" % N, h_api_action, "any"),
]
