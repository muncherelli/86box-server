"""Box records: create, edit, delete, list, start/stop/reset. Hardware is never edited here (86Box Settings does that)."""
import configparser
import datetime
import glob
import grp
import json
import math
import os
import re
import shutil
import stat
import threading
import urllib.parse

import config
import respond
import store
import units

NAME_RE = re.compile(r"[a-z][a-z0-9-]{1,30}")
RESERVED = {"new", "api", "trash"}
MEDIA_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,120}")
LOCK = threading.Lock()
# noVNC resolves path against the page URL (/novnc/vnc.html), so it must start with "/" to reach /websockify.
# resize=remote makes the box's display follow the browser window; resize=scale keeps a fixed display size.
CONSOLE_FMT = "/novnc/vnc.html?path=%%2Fwebsockify%%3Ftoken%%3D%s&autoconnect=1&resize=%s&reconnect=1"
DEFAULT_GEOMETRY = "1024x768"   # start size of every display; "auto" boxes then follow the browser
RES_RE = re.compile(r"([0-9]{3,4})x([0-9]{3,4})")
RESOLUTION_PRESETS = ["640x400", "640x480", "720x400", "800x600", "1024x768", "1152x864", "1280x1024", "1600x1200",
                      "1280x720", "1366x768", "1440x900", "1600x900", "1680x1050", "1920x1080", "1920x1200", "2560x1440"]


class VmError(Exception):
    pass


def parse_resolution(value):
    """'' or 'auto' -> 'auto'; 'WxH' between 640x400 and 2560x1600 -> 'WxH'; anything else is refused."""
    v = (value or "").strip().lower().replace(" ", "")
    if v in ("", "auto"):
        return "auto"
    m = RES_RE.fullmatch(v)
    if not m or not (640 <= int(m.group(1)) <= 2560 and 400 <= int(m.group(2)) <= 1600):
        raise VmError("Display size must be 'auto' or WxH between 640x400 and 2560x1600.")
    return "%dx%d" % (int(m.group(1)), int(m.group(2)))


def console_url(name):
    """Console link for a box: 'auto' (the default) follows the browser window, a fixed size is scaled to fit."""
    data = store.read_json(_p("vmdefs", name + ".json"), {})
    res = data.get("resolution", "auto") if isinstance(data, dict) else "auto"
    return CONSOLE_FMT % (name, "remote" if res == "auto" else "scale")


def _env_text(display, resolution):
    geometry = DEFAULT_GEOMETRY if resolution == "auto" else resolution
    return ("B86_DISPLAY=%d\nB86_VNC_PORT=%d\nDISPLAY=:%d\nB86_GEOMETRY=%s\n"
            % (display, 5900 + display, display, geometry))


def _auth():
    import auth
    return auth


def _root():
    return config.CFG["data_root"]


def _p(*parts):
    return os.path.join(_root(), *parts)


def _group(path):
    """Give a path to the shared group. Ignored when we are not allowed (tests, other hosts)."""
    try:
        os.chown(path, -1, grp.getgrnam(config.CFG["share_group"]).gr_gid)
    except (KeyError, OSError):
        pass


def _mkdir(path):
    os.mkdir(path)
    os.chmod(path, 0o2770)
    _group(path)


def _now():
    return datetime.datetime.now(datetime.timezone.utc)


def _stamp():
    return _now().strftime("%Y-%m-%dT%H:%M:%SZ")


# ---- templates

def _tpl_dir():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates", config.CFG["emu86box_tag"])


def list_templates():
    out = {}
    for path in sorted(glob.glob(os.path.join(_tpl_dir(), "*.json"))):
        data = store.read_json(path)
        if data and os.path.exists(path[:-5] + ".cfg"):
            out[data["id"]] = data
    return out


def _read_cfg(path):
    p = configparser.ConfigParser(interpolation=None, strict=False)
    p.optionxform = str
    try:
        with open(path, encoding="utf-8") as f:
            p.read_file(f)
    except (OSError, UnicodeDecodeError, configparser.Error) as e:
        raise VmError("cannot read %s: %s" % (os.path.basename(path), e))
    return p


def _write_cfg(p, path):
    tmp = path + ".part"
    with open(tmp, "w") as f:
        p.write(f)
    os.chmod(tmp, 0o660)
    _group(tmp)
    os.replace(tmp, path)


# ---- records

def all_defs():
    out = {}
    for path in sorted(glob.glob(_p("vmdefs", "*.json"))):
        name = os.path.basename(path)[:-5]
        data = store.read_json(path)
        if NAME_RE.fullmatch(name) and isinstance(data, dict):
            out[name] = data
    return out


def busy(name):
    return units.is_active("vm@%s.service" % name) or units.is_active("vm-settings@%s.service" % name)


def _iso_ok(filename):
    if not MEDIA_RE.fullmatch(filename):
        return False
    try:
        return stat.S_ISREG(os.lstat(_p("media", "isos", filename)).st_mode)
    except OSError:
        return False


def list_isos():
    try:
        return sorted(f for f in os.listdir(_p("media", "isos")) if _iso_ok(f))
    except OSError:
        return []


def _free_display(defs):
    used = {d.get("display") for d in defs.values()}
    for n in range(10, 100):
        if n not in used:
            return n
    raise VmError("No free display numbers")


def _check_common(groups, users):
    for g in groups:
        if g not in config.netgroups():
            raise VmError("Unknown network group: %s" % g)
    known = _auth().console_users()
    for u in users:
        if u not in known:
            raise VmError("Unknown console user: %s" % u)


def _write_token(name, display):
    store.write_text(_p("vnc-tokens", name), "%s: 127.0.0.1:%d\n" % (name, 5900 + display), mode=0o600)


def create_vm(name, template, disk_gb, cd, groups, autostart, users):
    with LOCK:
        return _create(name, template, disk_gb, cd, groups, autostart, users)


def ensure_cdrom(p):
    """Give the config a CD-ROM drive (ATAPI, secondary slot) unless it already has one."""
    section = "Floppy and CD-ROM drives"
    if not p.has_section(section):
        p.add_section(section)
    if not p.has_option(section, "cdrom_01_parameters"):
        p.set(section, "cdrom_01_parameters", "1, atapi")
        p.set(section, "cdrom_01_ide_channel", "0:1")


def _parse_disk_gb(disk_gb):
    """Empty or 0 means no hard disk; otherwise a whole number of GB from 1 to 7."""
    try:
        gb = int(disk_gb) if str(disk_gb or "").strip() else 0
    except ValueError:
        raise VmError("Disk size must be a whole number of GB.")
    if gb != 0 and not 1 <= gb <= 7:
        raise VmError("Disk size must be 1-7 GB, or empty for no hard disk.")
    return gb


def _create(name, template, disk_gb, cd, groups, autostart, users):
    """template '' makes a blank box (86Box's own defaults). Templates only choose the board and CPU.
    No hard disk and no CD drive are added unless asked for."""
    if not NAME_RE.fullmatch(name or "") or name in RESERVED:
        raise VmError("Name must be 2-31 characters: a-z, 0-9 and '-', starting with a letter.")
    template = template or ""
    if template and template not in list_templates():
        raise VmError("Unknown template.")
    gb = _parse_disk_gb(disk_gb)
    if cd and not _iso_ok(cd):
        raise VmError("That CD image is not in the media library.")
    _check_common(groups, users)
    final, temp = _p("vms", name), _p("vms", ".new-" + name)
    if any(os.path.lexists(x) for x in (final, temp, _p("vmdefs", name + ".json"))):
        raise VmError("A box called %s already exists." % name)
    display = _free_display(all_defs())

    if template:
        p = _read_cfg(os.path.join(_tpl_dir(), template + ".cfg"))
    else:
        p = configparser.ConfigParser(interpolation=None, strict=False)
        p.optionxform = str
        p.add_section("General")
        p.set("General", "vid_resize", "1")
    cyl = min(16383, math.floor(gb * 1024 ** 3 / 516096)) if gb else 0
    if gb:
        if not p.has_section("Hard disks"):
            p.add_section("Hard disks")
        p.set("Hard disks", "hdd_01_parameters", "63, 16, %d, 0, ide" % cyl)
        p.set("Hard disks", "hdd_01_fn", "disks/hdd1.img")
        p.set("Hard disks", "hdd_01_ide_channel", "0:0")
    if cd:
        ensure_cdrom(p)
        p.set("Floppy and CD-ROM drives", "cdrom_01_image_path", _p("media", "isos", cd))

    users_set = False
    try:
        _mkdir(temp)
        for sub in ("disks", "floppies", "snapshots"):
            _mkdir(os.path.join(temp, sub))
        _write_cfg(p, os.path.join(temp, "86box.cfg"))
        if gb:
            disk = os.path.join(temp, "disks", "hdd1.img")
            with open(disk, "wb") as f:
                f.truncate(63 * 16 * cyl * 512)
            os.chmod(disk, 0o660)
            _group(disk)
        os.rename(temp, final)
        store.write_text(_p("vmdefs", name + ".env"), _env_text(display, "auto"), mode=0o644)
        store.write_json(_p("vmdefs", name + ".json"), {
            "name": name, "display": display, "allowed_groups": sorted(groups), "autostart": bool(autostart),
            "template": template, "created": _stamp()}, mode=0o644)
        _write_token(name, display)
        if users:
            users_set = True
            _auth().set_vm_users(name, users)
    except Exception as e:
        for path in (_p("vnc-tokens", name), _p("vmdefs", name + ".json"), _p("vmdefs", name + ".env")):
            if os.path.lexists(path):
                os.unlink(path)
        for path in (final, temp):
            shutil.rmtree(path, ignore_errors=True)
        if users_set:
            _auth().remove_vm(name)
        raise VmError("Could not create the box: %s" % e)
    return name


def edit_vm(name, groups, autostart, users, cd=None, resolution=None):
    """cd=None leaves the CD image alone; '' removes it. resolution=None leaves the display size alone;
    '' or 'auto' follows the browser window; 'WxH' is a fixed size that applies at the next start."""
    with LOCK:
        path = _p("vmdefs", name + ".json")
        data = store.read_json(path)
        if not isinstance(data, dict):
            raise VmError("No such box.")
        _check_common(groups, users)
        res = None if resolution is None else parse_resolution(resolution)
        if cd is not None:
            if busy(name):
                raise VmError("Use the Media menu in the console while the box is running.")
            if cd and not _iso_ok(cd):
                raise VmError("That CD image is not in the media library.")
            cfg = _p("vms", name, "86box.cfg")
            p = _read_cfg(cfg)
            section = "Floppy and CD-ROM drives"
            if cd:
                ensure_cdrom(p)
                p.set(section, "cdrom_01_image_path", _p("media", "isos", cd))
            elif p.has_option(section, "cdrom_01_image_path"):
                p.remove_option(section, "cdrom_01_image_path")
            _write_cfg(p, cfg)
        data["allowed_groups"] = sorted(groups)
        data["autostart"] = bool(autostart)
        if res is not None:
            data["resolution"] = res
            store.write_text(_p("vmdefs", name + ".env"), _env_text(data["display"], res), mode=0o644)
        store.write_json(path, data, mode=0o644)
        _auth().set_vm_users(name, users)


def delete_vm(name):
    with LOCK:
        if not os.path.exists(_p("vmdefs", name + ".json")):
            raise VmError("No such box.")
        if busy(name):
            raise VmError("Stop the box first.")
        os.makedirs(_p("trash"), mode=0o700, exist_ok=True)
        dest = _p("trash", "%s-%s" % (name, _now().strftime("%Y%m%d-%H%M%S")))
        if os.path.exists(_p("vms", name)):
            os.rename(_p("vms", name), dest)
        for path in (_p("vmdefs", name + ".json"), _p("vmdefs", name + ".env"), _p("vnc-tokens", name)):
            if os.path.lexists(path):
                os.unlink(path)
        _auth().remove_vm(name)


def trash_size():
    total = 0
    for root, _dirs, files in os.walk(_p("trash")):
        for f in files:
            try:
                total += os.lstat(os.path.join(root, f)).st_size
            except OSError:
                pass
    return total


def empty_trash():
    with LOCK:
        for entry in glob.glob(_p("trash", "*")):
            shutil.rmtree(entry, ignore_errors=True)


# ---- status and actions

def status(name):
    if units.is_active("vm@%s.service" % name):
        state = "running"
    elif units.is_active("vm-settings@%s.service" % name):
        state = "settings"
    else:
        state = "stopped"
    nic = store.read_json("/run/86box-server/nic/%s.json" % name, {})
    guard = store.read_json("/run/86box-server/guard/%s.json" % name, {})
    nic_bad = [n for n in nic.get("nics", []) if not n.get("ok")]
    return {"name": name, "state": state,
            "nic": "no data" if not nic else ("problem" if nic_bad else "ok"),
            "guard": "; ".join(guard.get("problems", [])) if guard.get("ok") is False else ""}


def visible(user):
    defs = all_defs()
    return [n for n in sorted(defs) if _auth().can_use_vm(user, n)]


def do_action(name, action):
    verb = {"start": "start", "stop": "stop", "reset": "restart"}[action]
    return units.run(verb, "vm@%s.service" % name)


import vms_pages  # noqa: E402  (page handlers; must come last)

ROUTES = vms_pages.ROUTES
