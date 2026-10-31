"""Shared media library (isos, floppies, disks): list, upload, delete, attach to a VM."""
import configparser
import glob
import os
import re
import shutil
import stat
import subprocess
import time
import urllib.parse

import config
import respond
import store
import units

KINDS = {
    "isos": (".iso", ".cue", ".bin"),
    "floppies": (".img", ".ima", ".vfd", ".86f", ".imd", ".td0"),
    "disks": (".img", ".vhd", ".hdi"),
}
NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,120}")
VM_RE = re.compile(r"[a-z][a-z0-9-]{1,30}")
CHUNK = 1024 * 1024
GIB = 1024 ** 3
CDROM_RE = re.compile(r"^cdrom_\d\d_image_path\s*=\s*(.*?)\s*$", re.M)


def _root():
    return config.CFG["data_root"]


def _dir(kind):
    return os.path.join(_root(), "media", kind)


def _group():
    return config.CFG.get("share_group", "86box-data")


def _chgrp(path):
    try:
        shutil.chown(path, group=_group())
    except (PermissionError, LookupError, OSError):
        pass


def valid_name(kind, name):
    """Return an error string, or None when kind and name are acceptable."""
    if kind not in KINDS:
        return "unknown kind"
    if not NAME_RE.fullmatch(name) or not name.lower().endswith(KINDS[kind]):
        return "bad file name or extension"
    return None


def cleanup_parts():
    """Delete upload leftovers (.part) older than 24 hours."""
    for kind in KINDS:
        for p in glob.glob(os.path.join(_dir(kind), ".*.part")):
            try:
                if time.time() - os.lstat(p).st_mtime > 86400:
                    os.unlink(p)
            except OSError:
                pass


def used_by():
    """Map absolute ISO path -> sorted list of VM names whose cfg points at it."""
    used = {}
    for cfg in glob.glob(os.path.join(_root(), "vms", "*", "86box.cfg")):
        vmdir = os.path.dirname(cfg)
        try:
            fd = os.open(cfg, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(fd, "rb") as f:
                text = f.read().decode("utf-8", "replace")
        except OSError:
            continue
        for value in CDROM_RE.findall(text):
            if not value:
                continue
            path = os.path.normpath(os.path.join(vmdir, value))  # 86Box may save it relative
            used.setdefault(path, set()).add(os.path.basename(vmdir))
    return {k: sorted(v) for k, v in used.items()}


def list_media(kind):
    out = []
    users = used_by() if kind == "isos" else {}
    try:
        names = sorted(os.listdir(_dir(kind)))
    except OSError:
        return out
    for name in names:
        if name.startswith(".") or valid_name(kind, name):
            continue
        path = os.path.join(_dir(kind), name)
        st = os.lstat(path)
        if not stat.S_ISREG(st.st_mode):
            continue
        out.append({"name": name, "size": st.st_size, "modified": int(st.st_mtime),
                    "used_by": users.get(os.path.normpath(path), [])})
    return out


def _audit(req, action, detail):
    store.audit(req.user["name"] if req.user else "", req.ip, action, detail)


def _done(req, ok, msg, back, status=None):
    """JSON for API callers, redirect or small error page for plain forms."""
    if "json" in (req.headers.get("Accept") or ""):
        return respond.json_resp({"ok": ok, "error": "" if ok else msg}, status or (200 if ok else 400))
    if ok:
        return respond.redirect(back)
    return respond.page(req, "Error", "<p class=\"error\">%s</p><p><a href=\"%s\">Back</a></p>"
                        % (respond.esc(msg), respond.esc(back)), status or 400)


# ---- upload -------------------------------------------------------------

def upload(req, m):
    kind = m.group(1)
    name = urllib.parse.unquote(m.group(2))
    err = valid_name(kind, name)
    if err:
        return respond.json_resp({"ok": False, "error": err}, 400)
    if req.headers.get("Content-Length") is None:
        return respond.json_resp({"ok": False, "error": "Content-Length is required"}, 411)
    if req.length <= 0:
        return respond.json_resp({"ok": False, "error": "empty upload"}, 400)
    folder = _dir(kind)
    final = os.path.join(folder, name)
    part = os.path.join(folder, "." + name + ".part")
    if os.path.lexists(final):
        return respond.json_resp({"ok": False, "error": "a file with that name exists"}, 409)
    try:
        if shutil.disk_usage(folder).free < req.length + GIB:
            return respond.json_resp({"ok": False, "error": "not enough free disk space"}, 507)
        cleanup_parts()
        f = open(part, "xb")
    except FileExistsError:
        return respond.json_resp({"ok": False, "error": "that file is being uploaded"}, 409)
    except OSError as e:
        return respond.json_resp({"ok": False, "error": "cannot write: %s" % e.strerror}, 500)
    try:
        with f:
            remaining = req.length
            while remaining:
                chunk = req.rfile.read(min(CHUNK, remaining))
                if not chunk:
                    raise EOFError
                f.write(chunk)
                remaining -= len(chunk)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(part, 0o664)
        _chgrp(part)
        os.link(part, final)  # fails if the name appeared meanwhile: never overwrite
    except EOFError:
        os.unlink(part)
        return respond.json_resp({"ok": False, "error": "upload ended early"}, 400)
    except FileExistsError:
        os.unlink(part)
        return respond.json_resp({"ok": False, "error": "a file with that name exists"}, 409)
    except OSError as e:
        if os.path.lexists(part):
            os.unlink(part)
        return respond.json_resp({"ok": False, "error": "cannot write: %s" % e.strerror}, 500)
    os.unlink(part)
    _audit(req, "media.upload", "%s/%s %d bytes" % (kind, name, req.length))
    return respond.json_resp({"ok": True}, 201)


# ---- delete -------------------------------------------------------------

def delete(req, m):
    kind, name = m.group(1), urllib.parse.unquote(m.group(2))
    back = "/manage/media"
    err = valid_name(kind, name)
    if err:
        return _done(req, False, err, back)
    if req.form().get("confirm") != name:
        return _done(req, False, "Type the file name to confirm the delete.", back)
    path = os.path.join(_dir(kind), name)
    if not os.path.lexists(path):
        return _done(req, False, "no such file", back, 404)
    if kind == "isos":
        users = used_by().get(os.path.normpath(path), [])
        if users:
            return _done(req, False, "In use by: " + ", ".join(users), back, 409)
    os.unlink(path)
    _audit(req, "media.delete", "%s/%s" % (kind, name))
    return _done(req, True, "", back)


# ---- attach to a VM -----------------------------------------------------

def attach(req, m):
    vm = m.group(1)
    form = req.form()
    back = "/manage/vm/" + vm
    kind, _, filename = form.get("pick", "").partition("|")
    kind, filename = kind or form.get("kind", ""), filename or form.get("file", "")
    err = attach_file(vm, kind, filename)
    if err:
        return _done(req, False, err, back)
    _audit(req, "media.attach", "%s %s/%s" % (vm, kind, filename))
    return _done(req, True, "", back)


def attach_file(vm, kind, filename):
    """Attach a library file to a stopped VM. Returns an error string, or None."""
    if not VM_RE.fullmatch(vm):
        return "bad VM name"
    err = valid_name(kind, filename)
    if err:
        return err
    vmdir = os.path.join(_root(), "vms", vm)
    cfg = os.path.join(vmdir, "86box.cfg")
    if not os.path.isdir(vmdir):
        return "no such VM"
    if units.is_active("vm@%s.service" % vm) or units.is_active("vm-settings@%s.service" % vm):
        return "The VM is running. Use the Media menu in the console."
    src = os.path.join(_dir(kind), filename)
    try:
        if not stat.S_ISREG(os.lstat(src).st_mode):
            return "not a regular file"
    except OSError:
        return "no such file"
    p = configparser.ConfigParser(interpolation=None, strict=False)
    p.optionxform = str
    try:
        fd = os.open(cfg, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as f:
            p.read_string(f.read().decode("utf-8"))
    except (OSError, UnicodeDecodeError, configparser.Error) as e:
        return "cannot read 86box.cfg: %s" % e
    if not p.has_section("Floppy and CD-ROM drives"):
        p.add_section("Floppy and CD-ROM drives")
    section = p["Floppy and CD-ROM drives"]
    if kind == "isos":
        if "cdrom_01_parameters" not in section:  # a VM made without a CD drive gets one
            section["cdrom_01_parameters"] = "1, atapi"
            section["cdrom_01_ide_channel"] = "0:1"
        section["cdrom_01_image_path"] = src
    else:
        sub = "floppies" if kind == "floppies" else "disks"
        folder = os.path.join(vmdir, sub)
        dest = os.path.join(folder, filename)
        if os.path.lexists(dest):
            return "A file with that name is already in the VM folder."
        os.makedirs(folder, exist_ok=True)
        os.chmod(folder, 0o2770)
        r = subprocess.run(["cp", "--reflink=auto", "--sparse=always", src, dest], capture_output=True, text=True)
        if r.returncode:
            return "copy failed: " + r.stderr.strip()
        os.chmod(dest, 0o660)
        _chgrp(dest)
        if kind == "floppies":
            section["fdd_01_fn"] = dest
    from io import StringIO
    out = StringIO()
    p.write(out)
    store.write_text(cfg, out.getvalue(), 0o660)
    return None


# ---- pages --------------------------------------------------------------

def _size(n):
    if n < 1024:
        return "%d B" % n
    for unit in ("KB", "MB", "GB"):
        n /= 1024.0
        if n < 1024 or unit == "GB":
            return "%.1f %s" % (n, unit)


def _csrf(req):
    return '<input type="hidden" name="csrf" value="%s">' % respond.esc(req.session["csrf"] if req.session else "")


def media_page(req, m):
    parts = []
    if config.CFG.get("edge_mode") in ("tunnel", "cf-origin"):
        parts.append("<p>Files over 100 MB: use SFTP (Cloudflare limits uploads).</p>")
    parts.append('<h2>Upload</h2><form id="upload"><select name="kind">%s</select> '
                 '<input type="file" name="file"> <input type="submit" value="Upload"> '
                 '<progress id="bar" value="0" max="100" hidden></progress> <span id="msg"></span></form>'
                 % "".join("<option>%s</option>" % k for k in KINDS))
    for kind in KINDS:
        rows = []
        for e in list_media(kind):
            use = ", ".join(e["used_by"])
            rows.append(
                "<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>"
                '<form method="post" action="/manage/api/media/%s/%s/delete">%s'
                '<input name="confirm" placeholder="type the name"> <input type="submit" value="Delete"></form></td></tr>'
                % (respond.esc(e["name"]), _size(e["size"]), time.strftime("%Y-%m-%d %H:%M", time.gmtime(e["modified"])),
                   respond.esc(use), kind, urllib.parse.quote(e["name"]), _csrf(req)))
        parts.append("<h2>%s</h2><table><tr><th>Name</th><th>Size</th><th>Modified</th><th>Used by</th><th></th></tr>%s</table>"
                     % (kind, "".join(rows) or "<tr><td colspan=5>Nothing here yet.</td></tr>"))
    parts.append('<script src="/manage/static/media.js"></script>')
    return respond.page(req, "Media", "".join(parts))


def html_attach_section(req, vm):
    """Fragment for the VM page: attach a library file to a stopped VM."""
    opts = "".join('<option value="%s|%s">%s/%s</option>' % (k, respond.esc(e["name"]), k, respond.esc(e["name"]))
                   for k in KINDS for e in list_media(k))
    if units.is_active("vm@%s.service" % vm):
        return "<h2>Media</h2><p>Use the Media menu in the console.</p>"
    return ('<h2>Media</h2><form method="post" action="/manage/api/vm/%s/attach" id="attach">%s'
            '<select name="pick">%s</select> <input type="submit" value="Attach"></form>' % (vm, _csrf(req), opts))


def list_api(req, m):
    return respond.json_resp({k: list_media(k) for k in KINDS})


ROUTES = [
    ("GET", r"/manage/media", media_page, "admin"),
    ("GET", r"/manage/api/media", list_api, "admin"),
    ("PUT", r"/manage/api/media/([^/]+)/(.+)", upload, "admin"),
    ("POST", r"/manage/api/media/([^/]+)/([^/]+)/delete", delete, "admin"),
    ("POST", r"/manage/api/vm/([a-z][a-z0-9-]{1,30})/attach", attach, "admin"),
]
