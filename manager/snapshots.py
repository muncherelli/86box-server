"""Snapshots of a stopped box: disks, nvr, floppies, 86box.cfg and its record (vmdef.json)."""
import datetime
import os
import re
import shutil
import subprocess

import config
import respond
import store
import units

VM_RE = re.compile(r"[a-z][a-z0-9-]{1,30}")
LABEL_RE = re.compile(r"[a-z0-9-]{0,30}")
SNAP_RE = re.compile(r"\d{8}-\d{6}(-[a-z0-9-]{1,30})?")
ITEMS = ("disks", "nvr", "floppies", "86box.cfg")
GIB = 1024 ** 3


def _root():
    return config.CFG["data_root"]


def _vmdir(name):
    return os.path.join(_root(), "boxes", name)


def _snapdir(name):
    return os.path.join(_vmdir(name), "snapshots")


def _group():
    return config.CFG.get("share_group", "86box-data")


def _chgrp(path):
    try:
        shutil.chown(path, group=_group())
    except (PermissionError, LookupError, OSError):
        pass


def _cp(src, dest):
    r = subprocess.run(["cp", "-a", "--reflink=auto", "--sparse=always", src, dest], capture_output=True, text=True)
    if r.returncode:
        raise OSError(r.stderr.strip() or "copy failed")


def _du(path):
    r = subprocess.run(["du", "-sb", path], capture_output=True, text=True)
    return int(r.stdout.split()[0]) if r.returncode == 0 and r.stdout else 0


def _busy(name):
    return units.is_active("box@%s.service" % name) or units.is_active("box-settings@%s.service" % name)


def _check(name):
    """Return an error string if this box cannot be snapshotted or restored right now."""
    if not VM_RE.fullmatch(name):
        return "bad box name"
    if not os.path.isdir(_vmdir(name)):
        return "no such box"
    if _busy(name):
        return "Stop the box first."
    return None


def list_snapshots(name):
    out = []
    try:
        names = sorted(os.listdir(_snapdir(name)))
    except OSError:
        return out
    for snap in names:
        path = os.path.join(_snapdir(name), snap)
        if SNAP_RE.fullmatch(snap) and os.path.isdir(path) and not os.path.islink(path):
            out.append({"name": snap, "date": "%s-%s-%s %s:%s:%s" % (snap[0:4], snap[4:6], snap[6:8], snap[9:11], snap[11:13], snap[13:15]),
                        "size": _du(path)})
    return out


def create(name, label=""):
    """Return (error, snapshot_name)."""
    err = _check(name)
    if err:
        return err, None
    if not LABEL_RE.fullmatch(label):
        return "Label: up to 30 characters of a-z, 0-9 and dash.", None
    vm = _vmdir(name)
    need = sum(_du(os.path.join(vm, i)) for i in ("disks", "nvr", "floppies") if os.path.exists(os.path.join(vm, i))) + GIB
    os.makedirs(_snapdir(name), exist_ok=True)
    if shutil.disk_usage(_snapdir(name)).free < need:
        return "Not enough free disk space.", None
    snap = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d-%H%M%S") + ("-" + label if label else "")
    dest = os.path.join(_snapdir(name), snap)
    if os.path.exists(dest):
        return "A snapshot with that name exists. Try again in a second.", None
    os.mkdir(dest)
    try:
        for item in ITEMS:
            if os.path.exists(os.path.join(vm, item)):
                _cp(os.path.join(vm, item), os.path.join(dest, item))
        vmdef = os.path.join(_root(), "boxdefs", name + ".json")
        if os.path.exists(vmdef):
            _cp(vmdef, os.path.join(dest, "vmdef.json"))
    except OSError as e:
        shutil.rmtree(dest, ignore_errors=True)
        return "Snapshot failed: %s" % e, None
    _chgrp(dest)
    os.chmod(dest, 0o2770)
    return None, snap


def restore(name, snap):
    """Put a snapshot back. The current state is saved first as <ts>-before-restore."""
    err = _check(name)
    if err:
        return err
    if not SNAP_RE.fullmatch(snap) or not os.path.isdir(os.path.join(_snapdir(name), snap)):
        return "no such snapshot"
    err, _ = create(name, "before-restore")
    if err:
        return err
    src, vm = os.path.join(_snapdir(name), snap), _vmdir(name)
    try:
        for item in ITEMS:  # vmdef.json is not restored: name, display and groups stay as they are now
            if not os.path.exists(os.path.join(src, item)):
                continue
            new, cur = os.path.join(vm, item + ".restoring"), os.path.join(vm, item)
            shutil.rmtree(new, ignore_errors=True)
            _cp(os.path.join(src, item), new)
            if os.path.isdir(cur):
                shutil.rmtree(cur)
            elif os.path.lexists(cur):
                os.unlink(cur)
            os.rename(new, cur)
    except OSError as e:
        return "Restore failed: %s. The state before is in the before-restore snapshot." % e
    return None


def delete(name, snap, confirm):
    if not VM_RE.fullmatch(name) or not SNAP_RE.fullmatch(snap):
        return "bad name"
    path = os.path.join(_snapdir(name), snap)
    if not os.path.isdir(path):
        return "no such snapshot"
    if confirm != snap:
        return "Type the snapshot name to confirm the delete."
    shutil.rmtree(path)
    return None


# ---- HTTP ---------------------------------------------------------------

def _csrf(req):
    return '<input type="hidden" name="csrf" value="%s">' % respond.esc(req.session["csrf"] if req.session else "")


def html_section(req, name):
    """Fragment for the box page."""
    base = "/manage/api/box/%s/snapshots" % name
    rows = []
    for s in list_snapshots(name):
        rows.append(
            "<tr><td>%s</td><td>%s</td><td>%.1f MB</td><td>"
            '<form method="post" action="%s/%s/restore" style="display:inline">%s<input type="submit" value="Restore"></form> '
            '<form method="post" action="%s/%s/delete" style="display:inline">%s<input name="confirm" placeholder="type the name"> '
            '<input type="submit" value="Delete"></form></td></tr>'
            % (respond.esc(s["name"]), s["date"], s["size"] / 1048576.0, base, s["name"], _csrf(req), base, s["name"], _csrf(req)))
    return ('<h2>Snapshots</h2><p>The box must be stopped. Restoring saves the current state first.</p>'
            '<form method="post" action="%s">%s<input name="label" placeholder="label (optional)"> '
            '<input type="submit" value="Take snapshot"></form>'
            '<table><tr><th>Name</th><th>Date (UTC)</th><th>Size</th><th></th></tr>%s</table>'
            % (base, _csrf(req), "".join(rows) or "<tr><td colspan=4>No snapshots yet.</td></tr>"))


def _audit(req, action, detail):
    store.audit(req.user["name"] if req.user else "", req.ip, action, detail)


def _done(req, name, err, extra=None):
    if "json" in (req.headers.get("Accept") or ""):
        body = {"ok": not err, "error": err or ""}
        body.update(extra or {})
        return respond.json_resp(body, 400 if err else 200)
    back = "/manage/vm/" + name
    if not err:
        return respond.redirect(back)
    return respond.page(req, "Error", '<p class="error">%s</p><p><a href="%s">Back</a></p>'
                        % (respond.esc(err), respond.esc(back)), 400)


def h_list(req, m):
    return respond.json_resp({"snapshots": list_snapshots(m.group(1))})


def h_create(req, m):
    err, snap = create(m.group(1), req.form().get("label", ""))
    if not err:
        _audit(req, "snapshot.create", "%s %s" % (m.group(1), snap))
    return _done(req, m.group(1), err, {"snapshot": snap})


def h_restore(req, m):
    err = restore(m.group(1), m.group(2))
    if not err:
        _audit(req, "snapshot.restore", "%s %s" % (m.group(1), m.group(2)))
    return _done(req, m.group(1), err)


def h_delete(req, m):
    err = delete(m.group(1), m.group(2), req.form().get("confirm", ""))
    if not err:
        _audit(req, "snapshot.delete", "%s %s" % (m.group(1), m.group(2)))
    return _done(req, m.group(1), err)


_V = r"/manage/api/box/([a-z][a-z0-9-]{1,30})/snapshots"
ROUTES = [
    ("GET", _V, h_list, "admin"),
    ("POST", _V, h_create, "admin"),
    ("POST", _V + r"/([0-9a-z-]+)/restore", h_restore, "admin"),
    ("POST", _V + r"/([0-9a-z-]+)/delete", h_delete, "admin"),
]
