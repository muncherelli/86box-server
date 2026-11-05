"""Shared media library (/opt/86server/media/<kind>, one folder per 86Box drive type): list, upload, delete, attach."""
import configparser
import fnmatch
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
import selfupdate
import store
import units

# One folder per kind of 86Box drive. Extensions are the ones 86Box v6.0 offers in its own file dialogs
# (src/qt/qt_mediamenu.cpp, qt_harddiskdialog.cpp), as fnmatch patterns on the lowercased extension.
KINDS = {
    "iso": ("CD/DVD images", ("iso", "cue", "bin", "mds", "mdf", "mdx")),
    # Floppies: the loader table in src/floppy/fdd.c (wider than the dialog filter, e.g. .vfd).
    "floppy": ("Floppy images", ("001", "002", "003", "004", "005", "006", "007", "008", "009", "010", "12", "144",
                                 "360", "720", "86f", "bin", "cq", "cqm", "ddi", "dsk", "fdi", "fdf", "flp", "hdm",
                                 "ima", "imd", "img", "json", "mfm", "td0", "vfd", "xdf")),
    "disk": ("Hard disk images", ("hd?", "im?", "vhd")),
    "zip": ("ZIP and removable disk images", ("im?", "rdi", "zdi")),
    "mo": ("Magneto-optical (MO) images", ("im?", "mdi")),
    "tape": ("Tape images", ("tap",)),
    "cassette": ("Cassette images", ("pcm", "raw", "wav", "cas")),
    "cartridge": ("Cartridge images (PCjr)", ("a", "b", "jrc")),
}
# Kinds the box page can attach while a box is stopped; the rest are picked in 86Box's Media menu in the console.
ATTACHABLE = ("iso", "floppy", "disk")
NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,120}")
VM_RE = re.compile(r"[a-z][a-z0-9-]{1,30}")
CHUNK = 1024 * 1024
GIB = 1024 ** 3
CDROM_RE = re.compile(r"^cdrom_\d\d_image_path\s*=\s*(.*?)\s*$", re.M)


def _root():
    return config.CFG["data_root"]


def _dir(kind):
    return config.media_dir(kind)


def ext_ok(kind, name):
    ext = name.rsplit(".", 1)[1].lower() if "." in name else ""
    return bool(ext) and any(fnmatch.fnmatchcase(ext, pat) for pat in KINDS[kind][1])


def _group():
    return config.CFG.get("share_group", "86server")


def _chgrp(path):
    try:
        shutil.chown(path, group=_group())
    except (PermissionError, LookupError, OSError):
        pass


def valid_name(kind, name):
    """Return an error string, or None when kind and name are acceptable."""
    if kind not in KINDS:
        return "unknown kind"
    if not NAME_RE.fullmatch(name) or not ext_ok(kind, name):
        return "bad file name or extension"
    return None


def cleanup_parts(max_age=86400):
    """Delete upload and download leftovers (.part) older than max_age seconds. At manager start max_age is 0:
    no request can still be writing one then."""
    for kind in KINDS:
        for p in glob.glob(os.path.join(_dir(kind), ".*.part")):
            try:
                if time.time() - os.lstat(p).st_mtime >= max_age:
                    os.unlink(p)
            except OSError:
                pass


def used_by():
    """Map absolute ISO path -> sorted list of box names whose cfg points at it."""
    used = {}
    for cfg in glob.glob(os.path.join(_root(), "boxes", "*", "86box.cfg")):
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


def list_media(kind, users=True):
    """Files of one kind, sorted by name. users=False skips reading every box's cfg (for counts)."""
    out = []
    users = used_by() if kind == "iso" and users else {}
    try:
        names = sorted(os.listdir(_dir(kind)), key=str.lower)
    except OSError:
        return out
    for name in names:
        if name.startswith(".") or valid_name(kind, name):
            continue
        path = os.path.join(_dir(kind), name)
        try:
            st = os.lstat(path)
        except OSError:   # deleted while we were listing
            continue
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

def _back(value):
    """Where a delete form returns to: the media page it came from (keeps tab, search and page), nothing else."""
    return value if value and (value == "/manage/media" or value.startswith("/manage/media?")) else "/manage/media"


def _delete_one(req, kind, name, users):
    """Delete one library file. Returns (error, status) or None."""
    err = valid_name(kind, name)
    if err:
        return err, 400
    path = os.path.join(_dir(kind), name)
    if not os.path.lexists(path):
        return "%s: no such file" % name, 404
    in_use = users.get(os.path.normpath(path), [])
    if in_use:
        return "%s is in use by: %s" % (name, ", ".join(in_use)), 409
    os.unlink(path)
    _audit(req, "media.delete", "%s/%s" % (kind, name))
    return None


def delete(req, m):
    kind, name = m.group(1), urllib.parse.unquote(m.group(2))
    form = req.form()
    back = _back(form.get("back"))
    err = valid_name(kind, name)
    if err:
        return _done(req, False, err, back)
    if form.get("confirm") != name:
        return _done(req, False, "Type the file name to confirm the delete.", back)
    failed = _delete_one(req, kind, name, used_by() if kind == "iso" else {})
    if failed:
        return _done(req, False, failed[0], back, failed[1])
    return _done(req, True, "", back)


def delete_many(req, m):
    """Delete several files of one kind. confirm must be the number of files, so a stray post deletes nothing."""
    kind = m.group(1)
    data = urllib.parse.parse_qs(req.body().decode("utf-8", "replace"), keep_blank_values=True)
    back = _back((data.get("back") or [""])[0])
    names = list(dict.fromkeys(n for n in data.get("name", []) if n))
    if kind not in KINDS:
        return _done(req, False, "unknown kind", back)
    if not names:
        return _done(req, False, "Select at least one file.", back)
    if (data.get("confirm") or [""])[0] != str(len(names)):
        return _done(req, False, "Confirm the delete first.", back)
    users = used_by() if kind == "iso" else {}
    errors = [e[0] for e in (_delete_one(req, kind, n, users) for n in names) if e]
    if errors:
        done = len(names) - len(errors)
        return _done(req, False, "Deleted %d of %d. Not deleted: %s" % (done, len(names), "; ".join(errors)), back, 409)
    return _done(req, True, "", back)


# ---- attach to a box -----------------------------------------------------

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
    """Attach a library file to a stopped box. Returns an error string, or None."""
    if not VM_RE.fullmatch(vm):
        return "bad box name"
    err = valid_name(kind, filename)
    if err:
        return err
    if kind not in ATTACHABLE:
        return "Attach %s files in the console: 86Box's Media menu opens %s" % (kind, _dir(kind))
    vmdir = os.path.join(_root(), "boxes", vm)
    cfg = os.path.join(vmdir, "86box.cfg")
    if not os.path.isdir(vmdir):
        return "no such box"
    if units.is_active("box@%s.service" % vm) or units.is_active("box-settings@%s.service" % vm):
        return "The box is running. Use the Media menu in the console."
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
    if kind == "iso":
        if "cdrom_01_parameters" not in section:  # a box made without a CD drive gets one
            section["cdrom_01_parameters"] = "1, atapi"
            section["cdrom_01_ide_channel"] = "0:1"
        section["cdrom_01_image_path"] = src
    else:
        sub = "floppies" if kind == "floppy" else "disks"
        folder = os.path.join(vmdir, sub)
        dest = os.path.join(folder, filename)
        if os.path.lexists(dest):
            return "A file with that name is already in the box folder."
        os.makedirs(folder, exist_ok=True)
        os.chmod(folder, 0o2770)
        r = subprocess.run(["cp", "--reflink=auto", "--sparse=always", src, dest], capture_output=True, text=True)
        if r.returncode:
            return "copy failed: " + r.stderr.strip()
        os.chmod(dest, 0o660)
        _chgrp(dest)
        if kind == "floppy":
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


def locations_html():
    """Where the library lives on the server, so files can also be copied in over SFTP or the shell."""
    return ('<p class="hint">The library is <code>%s</code> on this server, one folder per kind. Upload here, or copy '
            'files straight into those folders (SFTP or the shell); they show up on the next page load. Files need to '
            'be readable by the <code>%s</code> group.</p>'
            % (respond.esc(config.media_dir()), respond.esc(_group())))


PER_PAGE = (50, 100, 250)
TAB_LABELS = {"iso": "CD/DVD", "floppy": "Floppy", "disk": "Hard disk", "zip": "ZIP", "mo": "MO",
              "tape": "Tape", "cassette": "Cassette", "cartridge": "Cartridge"}
SORTS = {"name": ("Name", lambda e: e["name"].lower(), False), "size": ("Size", lambda e: e["size"], True),
         "modified": ("Modified", lambda e: e["modified"], True)}


def _ext_tags(kind):
    return '<span class="tags">%s</span>' % "".join('<span class="tag">.%s</span>' % respond.esc(e)
                                                     for e in KINDS[kind][1])


def _media_url(**params):
    clean = {k: v for k, v in params.items() if v not in (None, "", 1, "1")}
    return "/manage/media" + ("?" + urllib.parse.urlencode(clean) if clean else "")


def media_page(req, m):
    one = lambda k, d="": (req.query.get(k) or [d])[0]
    kind = one("kind", "iso")
    kind = kind if kind in KINDS else "iso"
    q = one("q").strip()
    sort = one("sort", "name")
    sort = sort if sort in SORTS else "name"
    desc = one("dir") == "desc" if one("dir") in ("asc", "desc") else SORTS[sort][2]
    try:
        per = int(one("per", "50"))
    except ValueError:
        per = 50
    per = per if per in PER_PAGE else 50
    try:
        page = max(1, int(one("page", "1")))
    except ValueError:
        page = 1

    entries = list_media(kind)
    counts = {k: (len(entries) if k == kind else len(list_media(k, users=False))) for k in KINDS}
    if q:
        entries = [e for e in entries if q.lower() in e["name"].lower()]
    entries.sort(key=SORTS[sort][1], reverse=desc)
    total = len(entries)
    pages = max(1, -(-total // per))
    page = min(page, pages)
    shown = entries[(page - 1) * per: page * per]
    state = {"kind": kind, "q": q, "sort": None if sort == "name" else sort,
             "dir": None if desc == SORTS[sort][2] else ("desc" if desc else "asc"),
             "per": None if per == 50 else per}
    here = _media_url(page=page, **state)
    iso = kind == "iso"

    tabs = "".join('<a href="%s"%s>%s <span class="count">%d</span></a>'
                   % (respond.esc(_media_url(kind=k)), ' aria-current="page"' if k == kind else "",
                      respond.esc(TAB_LABELS.get(k, KINDS[k][0])), counts[k]) for k in KINDS)

    def head(key):
        label, _, default_desc = SORTS[key]
        if key == sort:
            nxt, arrow = ("asc" if desc else "desc"), (" ↓" if desc else " ↑")
        else:
            nxt, arrow = ("desc" if default_desc else "asc"), ""
        st = dict(state, sort=None if key == "name" else key, dir=None if (nxt == "desc") == default_desc else nxt)
        cls = {"name": "", "size": ' class="num"', "modified": ' class="num col-mod"'}[key]
        return '<th%s><a class="sort" href="%s">%s%s</a></th>' % (cls, respond.esc(_media_url(**st)), label, arrow)

    rows = []
    for e in shown:
        name = respond.esc(e["name"])
        used = ", ".join(e["used_by"])
        rows.append(
            '<tr><td class="pick"><input type="checkbox" name="name" value="%s" aria-label="Select %s"%s></td>'
            '<td class="name">%s</td><td class="num">%s</td><td class="num col-mod">%s</td>%s'
            '<td class="actions"><button type="button" class="ghost small" data-delete="%s"%s>Delete</button></td></tr>'
            % (name, name, " disabled" if used else "", name, _size(e["size"]),
               time.strftime("%Y-%m-%d %H:%M", time.gmtime(e["modified"])),
               ('<td class="col-used">%s</td>' % (respond.esc(used) or '<span class="hint">-</span>')) if iso else "",
               name, (' disabled title="In use by %s"' % respond.esc(used)) if used else ""))
    cols = 6 if iso else 5
    if not rows:
        rows.append('<tr><td colspan="%d" class="empty">%s</td></tr>'
                    % (cols, "No files match <b>%s</b>." % respond.esc(q) if q else "Nothing here yet."))

    pager = ""
    if total:
        first, last = (page - 1) * per + 1, min(page * per, total)
        link = lambda p, label, on: ('<a class="button small" href="%s">%s</a>' % (respond.esc(_media_url(page=p, **state)), label)
                                     if on else '<span class="button small" aria-disabled="true">%s</span>' % label)
        per_opts = "".join('<option value="%d"%s>%d per page</option>' % (n, " selected" if n == per else "", n)
                           for n in PER_PAGE)
        pager = ('<div class="pager"><span class="hint">%d–%d of %d</span><span class="spacer"></span>'
                 '<form method="get" action="/manage/media" class="inline">%s<select name="per" data-autosubmit>%s</select></form>'
                 '%s<span class="hint">Page %d of %d</span>%s</div>'
                 % (first, last, total,
                    "".join('<input type="hidden" name="%s" value="%s">' % (k, respond.esc(v))
                            for k, v in state.items() if v and k != "per"),
                    per_opts, link(page - 1, "← Prev", page > 1), page, pages, link(page + 1, "Next →", page < pages)))

    hidden = "".join('<input type="hidden" name="%s" value="%s">' % (k, respond.esc(v))
                     for k, v in state.items() if v and k not in ("q",))
    parts = ['<nav class="tabs" aria-label="Kind of media">%s</nav>' % tabs]
    note = ('<p class="hint">Through the Cloudflare tunnel, uploads over 100 MB fail: use the LAN name or SFTP.</p>'
            if config.tunnel_on() else "")
    parts.append(
        '<form id="upload" class="upload"><div class="dropzone" id="drop">'
        '<span><b>Upload:</b> drop files here or <label class="link">choose files'
        '<input type="file" name="file" multiple hidden></label></span>'
        '<span class="hint">into <select name="kind" aria-label="Upload into">%s</select></span></div>'
        '</form>'
        '<form id="fetch" class="fetch"><label class="fetch-url"><b>Download from a link:</b>'
        '<input type="url" name="url" required placeholder="https://example.com/disk.iso" aria-label="Link to a file"></label>'
        '<input type="text" name="name" placeholder="file name (optional)" aria-label="Save as (optional)">'
        '<span class="hint">into <select name="kind" aria-label="Download into">'
        '<option value="auto">Automatic (by file type)</option>%s</select></span>'
        '<button type="submit">Download</button></form>'
        '<p class="hint fetch-hint">The server downloads it straight into the library: nothing goes through your '
        'browser, so big files and the tunnel limit are no problem. Archives (.zip, .7z) are not unpacked.</p>'
        '<ul id="queue" class="queue"></ul>%s'
        % ("".join('<option value="%s"%s>%s</option>' % (k, " selected" if k == kind else "", respond.esc(KINDS[k][0]))
                   for k in KINDS),
           "".join('<option value="%s">%s</option>' % (k, respond.esc(KINDS[k][0])) for k in KINDS), note))
    parts.append(
        '<section class="card">'
        '<div class="card-head"><h2>%s</h2>%s</div>'
        '<p class="path"><code>%s</code></p>'
        '<form method="get" action="/manage/media" class="toolbar" role="search">%s'
        '<input type="search" name="q" value="%s" placeholder="Search %d files" aria-label="Search file names">'
        '<button type="submit">Search</button>%s</form>'
        '<form method="post" action="/manage/api/media/%s/delete-many" id="bulk">%s'
        '<input type="hidden" name="back" value="%s"><input type="hidden" name="confirm" value="">'
        '<div class="table-wrap"><table class="media-table"><thead><tr>'
        '<th class="pick"><input type="checkbox" id="pick-all" aria-label="Select all on this page"></th>'
        '%s%s%s%s<th></th></tr></thead><tbody>%s</tbody></table></div>'
        '<div class="bulkbar" hidden><span id="picked"></span><button type="submit" class="danger small">Delete selected</button></div>'
        '</form>%s</section>%s'
        % (respond.esc(KINDS[kind][0]), _ext_tags(kind), respond.esc(_dir(kind)),
           hidden, respond.esc(q), counts[kind],
           ' <a href="%s">Clear</a>' % respond.esc(_media_url(**dict(state, q=None))) if q else "",
           kind, _csrf(req), respond.esc(here),
           head("name"), head("size"), head("modified"), '<th class="col-used">Used by</th>' if iso else "",
           "".join(rows), pager, locations_html()))

    parts.append('<form method="post" id="delete-one" hidden>%s<input type="hidden" name="confirm">'
                 '<input type="hidden" name="back" value="%s"></form>' % (_csrf(req), respond.esc(here)))
    parts.append('<dialog id="media-confirm"><p id="media-confirm-text"></p><div class="dialog-actions">'
                 '<button type="button" id="media-confirm-no">Cancel</button> '
                 '<button type="button" class="danger" id="media-confirm-yes">Delete</button></div></dialog>')
    parts.append('<script src="/manage/static/media.js?v=' + respond.esc(selfupdate.asset_version()) + '"></script>')
    return respond.page(req, "Media", "".join(parts))


def html_attach_section(req, vm):
    """Fragment for the box page: attach a library file to a stopped box."""
    opts = "".join('<option value="%s|%s">%s/%s</option>' % (k, respond.esc(e["name"]), k, respond.esc(e["name"]))
                   for k in ATTACHABLE for e in list_media(k))
    if units.is_active("box@%s.service" % vm):
        return "<h2>Media</h2><p>Use the Media menu in the console.</p>"
    return ('<h2>Media</h2><form method="post" action="/manage/api/box/%s/attach" id="attach">%s'
            '<select name="pick">%s</select> <input type="submit" value="Attach"></form>' % (vm, _csrf(req), opts))


def list_api(req, m):
    return respond.json_resp({k: list_media(k) for k in KINDS})


ROUTES = [
    ("GET", r"/manage/media", media_page, "admin"),
    ("GET", r"/manage/api/media", list_api, "admin"),
    ("PUT", r"/manage/api/media/([^/]+)/(.+)", upload, "admin"),
    ("POST", r"/manage/api/media/([^/]+)/([^/]+)/delete", delete, "admin"),
    ("POST", r"/manage/api/media/([^/]+)/delete-many", delete_many, "admin"),
    ("POST", r"/manage/api/box/([a-z][a-z0-9-]{1,30})/attach", attach, "admin"),
]
