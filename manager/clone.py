"""Clone a stopped box under a new name, with new MAC addresses."""
import os
import random
import shutil
import subprocess
import urllib.parse

import respond
import store
import vms
from vms import NAME_RE, RESERVED, VmError

SKIP = ("snapshots", "86box.log")


def new_macs(cfg):
    """Give every key named exactly 'mac' three new random bytes (86Box keeps only the last three)."""
    for section in cfg.sections():
        if cfg.has_option(section, "mac"):
            cfg.set(section, "mac", "%02x:%02x:%02x" % tuple(random.randrange(256) for _ in range(3)))


def _copy(src, dest):
    r = subprocess.run(["cp", "-a", "--reflink=auto", "--sparse=always", src, dest], capture_output=True, text=True)
    if r.returncode:
        raise OSError(r.stderr.strip() or "copy failed")


def clone_vm(src, new):
    with vms.LOCK:
        defs = vms.all_defs()
        if src not in defs:
            raise VmError("No such box.")
        if not NAME_RE.fullmatch(new or "") or new in RESERVED:
            raise VmError("Name must be 2-31 characters: a-z, 0-9 and '-', starting with a letter.")
        if vms.busy(src):
            raise VmError("Stop the box first.")
        final, temp = vms._p("boxes", new), vms._p("boxes", ".new-" + new)
        if any(os.path.lexists(x) for x in (final, temp, vms._p("boxdefs", new + ".json"))):
            raise VmError("A box called %s already exists." % new)
        display = vms._free_display(defs)
        auth = vms._auth()
        users = [u for u, v in auth.console_users().items() if src in v.get("vms", [])]
        srcdir = vms._p("boxes", src)
        users_set = False
        try:
            vms._mkdir(temp)
            for entry in sorted(os.listdir(srcdir)):
                if entry not in SKIP and not entry.startswith("."):
                    _copy(os.path.join(srcdir, entry), os.path.join(temp, entry))
            vms._mkdir(os.path.join(temp, "snapshots"))
            cfg_path = os.path.join(temp, "86box.cfg")
            if os.path.exists(cfg_path):
                cfg = vms._read_cfg(cfg_path)
                new_macs(cfg)
                vms._write_cfg(cfg, cfg_path)
            os.rename(temp, final)
            store.write_text(vms._p("boxdefs", new + ".env"),
                             "B86_DISPLAY=%d\nB86_VNC_PORT=%d\nDISPLAY=:%d\n" % (display, 5900 + display, display), mode=0o644)
            store.write_json(vms._p("boxdefs", new + ".json"), {
                "name": new, "display": display, "allowed_groups": defs[src].get("allowed_groups", []),
                "autostart": False, "template": defs[src].get("template", ""), "cloned_from": src,
                "created": vms._stamp()}, mode=0o644)
            vms._write_token(new, display)
            if users:
                users_set = True
                auth.set_vm_users(new, users)
        except Exception as e:
            for path in (vms._p("vnc-tokens", new), vms._p("boxdefs", new + ".json"), vms._p("boxdefs", new + ".env")):
                if os.path.lexists(path):
                    os.unlink(path)
            for path in (final, temp):
                shutil.rmtree(path, ignore_errors=True)
            if users_set:
                auth.remove_vm(new)
            raise VmError("Could not clone the box: %s" % e)
    return new


def html_section(req, name):
    """Fragment for the box edit page."""
    return ('<h2>Clone</h2><p>The box must be stopped. The copy gets new network card addresses and no snapshots. '
            'Windows NT clones share the same machine SID; fix that inside the guest if it matters.</p>'
            '<form method="post" action="/manage/boxes/%s/clone"><input type="hidden" name="csrf" value="%s">'
            'New name: <input name="newname"> <input type="submit" value="Clone"></form>'
            % (respond.esc(name), respond.esc(req.session["csrf"] if req.session else "")))


def h_clone(req, m):
    name = m.group(1)
    new = urllib.parse.parse_qs(req.body().decode("utf-8", "replace")).get("newname", [""])[0]
    try:
        clone_vm(name, new)
    except VmError as e:
        return respond.page(req, "Clone failed", '<p class="error">%s</p><p><a href="/manage/boxes/%s">Back</a></p>'
                            % (respond.esc(str(e)), respond.esc(name)), 400)
    store.audit(req.user["name"], req.ip, "vm.clone", "%s -> %s" % (name, new))
    return respond.redirect("/manage/")


ROUTES = [("POST", r"/manage/boxes/([a-z][a-z0-9-]{1,30})/clone", h_clone, "admin")]
