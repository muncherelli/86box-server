"""Pages and handlers for the VM list, new/edit forms and the status API. Imported only by vms.py."""
import importlib
import os
import urllib.parse

import config
import respond
import store
import vms
from vms import CONSOLE, VmError, _p, _read_cfg, all_defs, busy, create_vm, delete_vm, edit_vm
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
        links = '<a href="%s">Console</a> <a href="/manage/vms/%s/settings">Settings</a>' % (respond.esc(CONSOLE % n), q)
        if admin:
            links += ' <a href="/manage/vms/%s">Edit</a>' % q
        rows.append('<tr data-vm="%s"><td>%s</td><td data-state>%s</td><td data-nic>%s</td>'
                    '<td data-guard class="error">%s</td><td>%s %s</td></tr>'
                    % (q, q, respond.esc(s["state"]), respond.esc(s["nic"]), respond.esc(s["guard"]), buttons, links))
    body = '<p class="error">%s</p>' % respond.esc(msg) if msg else ""
    body += ('<table id="vm-table"><tr><th>VM</th><th>State</th><th>NIC</th><th>Problems</th><th></th></tr>%s</table>'
             % "".join(rows)) if rows else "<p>No VMs yet.</p>"
    body += ('<dialog id="confirm"><p id="confirm-text"></p><button id="confirm-yes">Yes</button> '
             '<button id="confirm-no">Cancel</button></dialog>')
    if admin:
        mb = trash_size() / 1048576
        body += ('<p><a href="/manage/vms/new">New VM</a></p><form method="post" action="/manage/trash/empty">%s'
                 'Trash: %.0f MB <input type="submit" value="Empty trash"></form>' % (_csrf(req), mb))
    return respond.page(req, "Virtual machines", body)


def _checks(name, items, chosen, label):
    return "".join('<label><input type="checkbox" name="%s" value="%s"%s> %s</label> '
                   % (name, respond.esc(i), " checked" if i in chosen else "", respond.esc(i)) for i in items) \
        or "<em>none available</em>"


def _cd_select(current):
    opts = ['<option value="">(none)</option>'] + ['<option%s>%s</option>' % (" selected" if f == current else "", respond.esc(f))
                                                    for f in list_isos()]
    return '<select name="cd">%s</select>' % "".join(opts)


def _new_page(req, msg="", status_code=200):
    tpls = list_templates()
    opts = "".join('<option value="%s">%s</option>' % (respond.esc(k), respond.esc(v["name"])) for k, v in tpls.items())
    body = ('<p class="error">%s</p>' % respond.esc(msg)) if msg else ""
    body += ('<form method="post" action="/manage/vms/new">%s'
             '<p>Name <input name="name" required pattern="[a-z][a-z0-9-]{1,30}"></p>'
             '<p>Template <select name="template">%s</select></p>'
             '<p>Disk GB (1-7) <input name="disk_gb" type="number" min="1" max="7" placeholder="template default"></p>'
             '<p>CD image %s</p><p>Network groups %s</p><p>Console users %s</p>'
             '<p><label><input type="checkbox" name="autostart" value="1"> Start at boot</label></p>'
             '<input type="submit" value="Create"></form>'
             % (_csrf(req), opts, _cd_select(""), _checks("group", sorted(config.CFG["netgroups"]), (), "group"),
                _checks("user", sorted(vms._auth().console_users()), (), "user")))
    status_, headers, out = respond.page(req, "New VM", body, status_code)
    return status_, headers, out


def _edit_page(req, name, msg=""):
    data = all_defs().get(name)
    if data is None:
        return respond.text("not found", 404)
    users = [u for u, v in vms._auth().console_users().items() if name in v.get("vms", [])]
    cfg = _read_cfg(_p("vms", name, "86box.cfg")) if os.path.exists(_p("vms", name, "86box.cfg")) else None
    current = ""
    if cfg and cfg.has_option("Floppy and CD-ROM drives", "cdrom_01_image_path"):
        current = os.path.basename(cfg.get("Floppy and CD-ROM drives", "cdrom_01_image_path"))
    q = respond.esc(name)
    cd = ('<p>CD image %s</p>' % _cd_select(current)) if not busy(name) else \
        '<p>CD image: use the Media menu in the console while the VM is running.</p>'
    body = ('<p class="error">%s</p>' % respond.esc(msg)) if msg else ""
    body += ('<form method="post" action="/manage/vms/%s">%s%s<p>Network groups %s</p><p>Console users %s</p>'
             '<p><label><input type="checkbox" name="autostart" value="1"%s> Start at boot</label></p>'
             '<input type="submit" value="Save"></form>'
             '<h2>Delete</h2><form method="post" action="/manage/vms/%s/delete">%s'
             '<p>Type the name to confirm: <input name="confirm"> <input type="submit" value="Delete"></p></form>'
             % (q, _csrf(req), cd, _checks("group", sorted(config.CFG["netgroups"]), data.get("allowed_groups", []), "group"),
                _checks("user", sorted(vms._auth().console_users()), users, "user"),
                " checked" if data.get("autostart") else "", q, _csrf(req)))
    for mod, fn in (("snapshots", "html_section"), ("media", "html_attach_section"),
                    ("clone", "html_section"), ("importexp", "html_section")):
        body += getattr(importlib.import_module(mod), fn)(req, name)
    return respond.page(req, "VM " + name, body)


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
        name = create_vm(one("name"), one("template"), one("disk_gb"), one("cd"), f.get("group", []),
                         one("autostart") == "1", f.get("user", []))
    except VmError as e:
        return _new_page(req, str(e), 400)
    store.audit(req.user["name"], req.ip, "vm.create", name)
    return respond.redirect("/manage/")


def h_edit_get(req, m):
    return _edit_page(req, m.group(1))


def h_edit_post(req, m):
    name, f = m.group(1), _multi(req)
    try:
        edit_vm(name, f.get("group", []), (f.get("autostart") or [""])[0] == "1", f.get("user", []),
                cd=None if busy(name) else (f.get("cd") or [""])[0])
    except VmError as e:
        return _edit_page(req, name, str(e))
    store.audit(req.user["name"], req.ip, "vm.edit", name)
    return respond.redirect("/manage/")


def h_delete(req, m):
    name = m.group(1)
    if (_multi(req).get("confirm") or [""])[0] != name:
        return _edit_page(req, name, "Type the VM name exactly to delete it.")
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
    ("GET", r"/manage/vms/new", h_new_get, "admin"),
    ("POST", r"/manage/vms/new", h_new_post, "admin"),
    ("GET", r"/manage/vms/" + N, h_edit_get, "admin"),
    ("POST", r"/manage/vms/" + N, h_edit_post, "admin"),
    ("POST", r"/manage/vms/%s/delete" % N, h_delete, "admin"),
    ("POST", r"/manage/trash/empty", h_trash, "admin"),
    ("GET", r"/manage/api/vms", h_api_list, "any"),
    ("POST", r"/manage/api/vm/%s/(start|stop|reset)" % N, h_api_action, "any"),
]
