"""Export a box as JSON (record + 86box.cfg, no disks) and import it under a new name."""
import configparser
import json
import os
import re
import shutil
import urllib.parse

import config
import respond
import store
import vms
from vms import NAME_RE, RESERVED, VmError

PATH_KEY = re.compile(r".*(_fn|_image_path)$")


def export_vm(name):
    data = store.read_json(vms._p("boxdefs", name + ".json"))
    if not isinstance(data, dict):
        raise VmError("No such box.")
    vm = {k: v for k, v in data.items() if k != "display"}
    try:
        fd = os.open(vms._p("boxes", name, "86box.cfg"), os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as f:
            cfg = f.read().decode("utf-8")
    except FileNotFoundError:
        cfg = ""
    except (OSError, UnicodeDecodeError) as e:
        raise VmError("Cannot read 86box.cfg: %s" % e)
    return {"format": 1, "vm": vm, "cfg": cfg}


def check_cfg(p, allowed_bridges):
    """Same rule as cfg-guard: only tap NICs on allowed bridges. Returns a list of problems."""
    problems = []
    if p.has_section("Network"):
        for key, value in p.items("Network"):
            m = re.fullmatch(r"net_(\d\d)_net_type", key)
            if not m or value.strip() in ("", "none"):
                continue
            nn = m.group(1)
            if value.strip() != "tap":
                problems.append("NIC %s uses network type %s (only tap is allowed)" % (nn, value.strip()))
            elif p.get("Network", "net_%s_host_device" % nn, fallback="").strip() not in allowed_bridges:
                problems.append("NIC %s uses a bridge this box may not use" % nn)
    return problems


def check_paths(p):
    """File settings must stay inside the box folder or the media library."""
    media = config.media_dir() + os.sep
    for section in p.sections():
        for key, value in p.items(section):
            v = value.strip()
            if not PATH_KEY.fullmatch(key) or not v:
                continue
            if os.path.isabs(v):
                if not os.path.normpath(v).startswith(media):
                    return "%s points outside the media library" % key
            elif ".." in v.split("/"):
                return "%s contains '..'" % key
    return None


def _disk_bytes(p):
    if not p.has_option("Hard disks", "hdd_01_parameters"):
        return None
    parts = [x.strip() for x in p.get("Hard disks", "hdd_01_parameters").split(",")]
    if len(parts) != 5 or parts[4] != "ide" or not all(x.isdigit() for x in parts[:3]):
        raise VmError("Only an IDE first hard disk with plain numbers is supported.")
    s, h, c = (int(x) for x in parts[:3])
    if not (1 <= s <= 63 and 1 <= h <= 16 and 1 <= c <= 16383):
        raise VmError("Hard disk geometry is out of range.")
    return s * h * c * 512


def import_vm(name, data, groups, autostart, users):
    with vms.LOCK:
        return _import(name, data, groups, autostart, users)


def _import(name, data, groups, autostart, users):
    if not NAME_RE.fullmatch(name or "") or name in RESERVED:
        raise VmError("Name must be 2-31 characters: a-z, 0-9 and '-', starting with a letter.")
    if not isinstance(data, dict) or data.get("format") != 1 or not isinstance(data.get("cfg"), str) \
            or not isinstance(data.get("vm"), dict):
        raise VmError("This is not a box export file (format 1).")
    vms._check_common(groups, users)
    p = configparser.ConfigParser(interpolation=None, strict=False)
    p.optionxform = str
    try:
        p.read_string(data["cfg"])
    except configparser.Error as e:
        raise VmError("Cannot parse the 86box.cfg: %s" % e)
    bridges = [config.netgroups()[g]["bridge"] for g in groups]
    problems = check_cfg(p, bridges)
    if problems:
        raise VmError("; ".join(problems))
    bad = check_paths(p)
    if bad:
        raise VmError(bad)
    size = _disk_bytes(p)
    if size is not None:
        p.set("Hard disks", "hdd_01_fn", "disks/hdd1.img")
    final, temp = vms._p("boxes", name), vms._p("boxes", ".new-" + name)
    if any(os.path.lexists(x) for x in (final, temp, vms._p("boxdefs", name + ".json"))):
        raise VmError("A box called %s already exists." % name)
    display = vms._free_display(vms.all_defs())
    template = data["vm"].get("template", "")
    if not isinstance(template, str):
        template = ""
    auth = vms._auth()
    users_set = False
    try:
        vms._mkdir(temp)
        for sub in ("disks", "floppies", "snapshots"):
            vms._mkdir(os.path.join(temp, sub))
        vms._write_cfg(p, os.path.join(temp, "86box.cfg"))
        if size is not None:
            disk = os.path.join(temp, "disks", "hdd1.img")
            with open(disk, "wb") as f:
                f.truncate(size)
            os.chmod(disk, 0o660)
            vms._group(disk)
        os.rename(temp, final)
        store.write_text(vms._p("boxdefs", name + ".env"),
                         "B86_DISPLAY=%d\nB86_VNC_PORT=%d\nDISPLAY=:%d\n" % (display, 5900 + display, display), mode=0o644)
        store.write_json(vms._p("boxdefs", name + ".json"), {
            "name": name, "display": display, "allowed_groups": sorted(groups), "autostart": bool(autostart),
            "template": template, "created": vms._stamp()}, mode=0o644)
        vms._write_token(name, display)
        if users:
            users_set = True
            auth.set_vm_users(name, users)
    except Exception as e:
        for path in (vms._p("vnc-tokens", name), vms._p("boxdefs", name + ".json"), vms._p("boxdefs", name + ".env")):
            if os.path.lexists(path):
                os.unlink(path)
        for path in (final, temp):
            shutil.rmtree(path, ignore_errors=True)
        if users_set:
            auth.remove_vm(name)
        raise VmError("Could not import the box: %s" % e)
    return name


# ---- HTTP

def h_export(req, m):
    name = m.group(1)
    try:
        data = export_vm(name)
    except VmError as e:
        return respond.json_resp({"ok": False, "error": str(e)}, 404)
    store.audit(req.user["name"], req.ip, "vm.export", name)
    body = json.dumps(data, indent=2).encode()
    return 200, {"Content-Type": "application/json",
                 "Content-Disposition": 'attachment; filename="%s.86box-vm.json"' % name}, body


def _page(req, msg="", status=200):
    csrf = respond.esc(req.session["csrf"])
    groups = "".join('<label><input type="checkbox" name="group" value="%s"> %s</label> ' % ((respond.esc(g),) * 2)
                     for g in sorted(config.netgroups())) or "<em>none available</em>"
    users = "".join('<label><input type="checkbox" name="user" value="%s"> %s</label> ' % ((respond.esc(u),) * 2)
                    for u in sorted(vms._auth().console_users())) or "<em>none</em>"
    body = '<p class="error">%s</p>' % respond.esc(msg) if msg else ""
    body += ('<p>Import a file made by "Export" on a box page. Disks are not included: a blank disk of the same size is made.</p>'
             '<form method="post" action="/manage/import"><input type="hidden" name="csrf" value="%s">'
             '<p>New name: <input name="name"></p>'
             '<p>Export file: <input type="file" id="file" accept=".json"></p>'
             '<p>or paste its contents:<br><textarea name="payload" id="payload" rows="8" cols="70"></textarea></p>'
             '<p>Network groups %s</p><p>Console users %s</p>'
             '<p><label><input type="checkbox" name="autostart" value="1"> Start at boot</label></p>'
             '<input type="submit" value="Import"></form>'
             '<script>document.getElementById("file").onchange=function(e){var r=new FileReader();'
             'r.onload=function(){document.getElementById("payload").value=r.result};r.readAsText(e.target.files[0]);};</script>'
             % (csrf, groups, users))
    return respond.page(req, "Import a box", body, status)


def h_page(req, m):
    return _page(req)


def h_import(req, m):
    f = urllib.parse.parse_qs(req.body().decode("utf-8", "replace"), keep_blank_values=True)
    one = lambda k: (f.get(k) or [""])[0]
    try:
        try:
            data = json.loads(one("payload"))
        except ValueError:
            raise VmError("The export file is not valid JSON.")
        name = import_vm(one("name"), data, f.get("group", []), one("autostart") == "1", f.get("user", []))
    except VmError as e:
        return _page(req, str(e), 400)
    store.audit(req.user["name"], req.ip, "vm.import", name)
    return respond.redirect("/manage/")


def html_section(req, name):
    """Fragment for the box edit page."""
    return ('<h2>Export</h2><p><a href="/manage/api/box/%s/export">Download this box as JSON</a> (settings only, no disks). '
            '<a href="/manage/import">Import a box</a></p>' % respond.esc(name))


ROUTES = [
    ("GET", r"/manage/api/box/([a-z][a-z0-9-]{1,30})/export", h_export, "admin"),
    ("GET", r"/manage/import", h_page, "admin"),
    ("POST", r"/manage/import", h_import, "admin"),
]
