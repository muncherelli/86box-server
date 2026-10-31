"""Published sites: validated site records -> Caddy site files, validate, reload.

sites.json holds the records. render_site() turns one record into Caddy text and
re-checks every value first (never write a file from unchecked data).
apply_sites() renders all enabled sites, validates them with the real caddy,
writes /etc/caddy/sites.d/<host>.caddy and reloads caddy (rolling back on failure).
"""
import ipaddress
import os
import re
import shutil
import subprocess
import threading
import urllib.parse

import config
import respond
import store
import units

LOCK = threading.Lock()

HOST_RE = r"([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,}"
VM_RE = r"[a-z][a-z0-9-]{1,30}"
PATH_RE = r"/([A-Za-z0-9_~-][A-Za-z0-9._~-]*/)*"
METHODS = ["GET", "HEAD", "POST", "PUT", "DELETE", "OPTIONS"]
MAX_BODIES = ["64KB", "1MB", "10MB", "100MB"]

# IIS 4 hardening profile: item -> (kind, parts). Rendered in this order.
IIS_DIRS = {
    "admin_dirs": ["scripts", "iisadmin", "iisadmpwd", "iishelp", "iissamples", "msadc", "printers"],
    "frontpage": ["_vti_[a-z_]*", "_private"],
}
IIS_EXTS = {
    "ism_ext": ["htr", "ida", "idq", "printer", "htw", "idc", "htx"],
    "ssi_ext": ["stm", "shtm", "shtml"],
    "asa_ext": ["asa", "cdx", "cer"],
}
IIS_ITEMS = list(IIS_DIRS) + list(IIS_EXTS)

ENC_EXPR = ('{http.request.orig_uri}.matches("(?i)^[^?]*'
            '(%25|%2e|%2f|%5c|%c0|%c1|%u|\\\\.\\\\.|::|\\\\\\\\)")')

DEFAULT_RULES = {
    "methods": ["GET", "HEAD", "POST"],
    "allow_paths": ["/"],
    "strip_prefix": "",
    "block_encoded": True,
    "iis_profile": {k: True for k in IIS_ITEMS},
    "max_body": "1MB",
}


# ---------- validation and rendering ----------

def _netgroups():
    return config.netgroups()


def _vm_groups(vm):
    if not re.fullmatch(VM_RE, vm):
        raise ValueError("bad VM name")
    path = os.path.join(config.CFG["data_root"], "vmdefs", vm + ".json")
    rec = store.read_json(path)
    if rec is None:
        raise ValueError("unknown VM '%s'" % vm)
    return rec.get("allowed_groups", [])


def check_site(site):
    """Return the site with defaults filled in, or raise ValueError."""
    host = site.get("host", "")
    if not isinstance(host, str) or not re.fullmatch(HOST_RE, host):
        raise ValueError("bad host name")
    if not any(host == d or host.endswith("." + d) for d in config.CFG["publish_domains"]):
        raise ValueError("host is not under an approved domain")

    nets = _netgroups()
    cidrs = [nets[g]["cidr"] for g in _vm_groups(site.get("vm", "")) if g in nets]
    try:
        ip = ipaddress.IPv4Address(site.get("guest_ip", ""))
    except (ValueError, TypeError):
        raise ValueError("bad guest IP")
    for cidr in cidrs:
        net = ipaddress.IPv4Network(cidr)
        if ip in net and ip not in (net.network_address, net.broadcast_address, next(net.hosts())):
            break
    else:
        raise ValueError("guest IP is not inside one of the VM's network groups")

    port = site.get("port")
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
        raise ValueError("bad port")

    hh = site.get("host_header", "original")
    if hh != "original" and not (isinstance(hh, str) and re.fullmatch(r"[a-z0-9]([a-z0-9.-]{0,251}[a-z0-9])?", hh)):
        raise ValueError("bad host header")

    rules = dict(DEFAULT_RULES)
    rules.update(site.get("rules", {}))
    methods = rules["methods"]
    if not methods or not all(m in METHODS for m in methods):
        raise ValueError("bad method list")
    paths = rules["allow_paths"]
    if not paths or not all(isinstance(p, str) and re.fullmatch(PATH_RE, p) for p in paths):
        raise ValueError("bad allowed path")
    strip = rules["strip_prefix"]
    if strip and (strip == "/" or strip not in paths):
        raise ValueError("strip prefix must be one of the allowed paths (and not '/')")
    if rules["max_body"] not in MAX_BODIES:
        raise ValueError("bad body limit")
    iis = dict.fromkeys(IIS_ITEMS, False)
    iis.update(rules["iis_profile"])
    rules.update(iis_profile=iis, block_encoded=bool(rules["block_encoded"]))
    return dict(site, rules=rules, port=port, host_header=hh)


def iis_regex(iis):
    parts = []
    dirs = [p for item in IIS_DIRS for p in IIS_DIRS[item] if iis.get(item)]
    exts = [p for item in IIS_EXTS for p in IIS_EXTS[item] if iis.get(item)]
    if dirs:
        parts.append("^/(" + "|".join(dirs) + ")(/|$)")
    if exts:
        parts.append("\\.(" + "|".join(exts) + ")$")
    return "|".join(parts)


def render_site(site, edge_mode="acme"):
    s = check_site(site)
    r = s["rules"]
    iis = iis_regex(r["iis_profile"])
    deny = "^(" + "|".join(p.replace(".", "\\.") for p in r["allow_paths"]) + ")"
    methods = [m for m in METHODS if m in r["methods"]]
    out = ["# managed by 86box-server: do not edit", s["host"] + " {", "\timport b86_edge",
           "\trequest_body {", "\t\tmax_size " + r["max_body"], "\t}"]
    if r["block_encoded"]:
        out.append("\t@b86enc expression `" + ENC_EXPR + "`")
    out.append("\t@b86method not method " + " ".join(methods))
    if iis:
        out.append("\t@b86iis path_regexp (?i)" + iis)
    out.append("\t@b86deny not path_regexp " + deny)
    out.append("\troute {")
    if r["block_encoded"]:
        out.append("\t\trespond @b86enc 400")
    out.append("\t\trespond @b86method 405")
    if iis:
        out.append("\t\trespond @b86iis 404")
    out.append("\t\trespond @b86deny 404")
    if r["strip_prefix"]:
        out.append("\t\turi strip_prefix " + r["strip_prefix"].rstrip("/"))
    upstream = "%s:%d" % (s["guest_ip"], s["port"])
    if s["host_header"] == "original":
        out.append("\t\treverse_proxy " + upstream)
    else:
        out += ["\t\treverse_proxy %s {" % upstream, "\t\t\theader_up Host " + s["host_header"], "\t\t}"]
    out += ["\t}", "}"]
    return "\n".join(out) + "\n"


# ---------- storage ----------

def _sites_path():
    return os.path.join(config.CFG["data_root"], "manager", "sites.json")


def load_sites():
    return store.read_json(_sites_path(), {"sites": []})["sites"]


def save_sites(sites):
    store.write_json(_sites_path(), {"sites": sites}, mode=0o600)


# ---------- apply: render, validate, write, reload ----------

def _caddy_error(stderr):
    lines = [l for l in stderr.splitlines() if l.strip() and not l.startswith('{"level":"info"')]
    return "\n".join(lines[-5:]) or "caddy rejected the configuration"


def _snapshot(src, dst):
    shutil.rmtree(dst, ignore_errors=True)
    os.makedirs(dst)
    for name in os.listdir(src):
        if name.endswith(".caddy"):
            shutil.copy2(os.path.join(src, name), dst)


def _restore(src, dst):
    for name in os.listdir(dst):
        if name.endswith(".caddy"):
            os.unlink(os.path.join(dst, name))
    for name in os.listdir(src):
        shutil.copy2(os.path.join(src, name), dst)


def apply_sites(sites=None):
    """Return (ok, message). On any problem the live files stay as they were."""
    with LOCK:
        sites = load_sites() if sites is None else sites
        sites_dir = config.CFG.get("sites_dir", "/etc/caddy/sites.d")
        snippets = config.CFG.get("validate_snippets", "/etc/caddy/b86-validate-snippets.caddy")
        work = os.path.join(config.CFG["data_root"], "manager")
        try:
            files = {s["host"] + ".caddy": render_site(s, config.CFG["edge_mode"])
                     for s in sites if s.get("enabled", True)}
        except ValueError as e:
            return False, str(e)

        with open(snippets) as f:
            text = f.read()
        vfile = os.path.join(work, "validate.Caddyfile")
        store.write_text(vfile, text + "".join(files.values()), mode=0o600)
        env = dict(os.environ, HOME=work, XDG_DATA_HOME=work, XDG_CONFIG_HOME=work)
        r = subprocess.run([os.environ.get("B86_CADDY", "caddy"), "validate", "--adapter", "caddyfile",
                            "--config", vfile], capture_output=True, text=True, env=env, timeout=60)
        if r.returncode:
            return False, _caddy_error(r.stderr)

        prev = os.path.join(work, "sites-prev")
        _snapshot(sites_dir, prev)
        for name, body in files.items():
            store.write_text(os.path.join(sites_dir, name), body, mode=0o644)
        for name in os.listdir(sites_dir):
            if name.endswith(".caddy") and name not in files:
                os.unlink(os.path.join(sites_dir, name))
        ok, err = units.run("reload", "caddy.service")
        if not ok:
            _restore(prev, sites_dir)
            units.run("reload", "caddy.service")
            return False, "caddy reload failed: " + err
        return True, "applied"


# ---------- pages (admin) ----------

def _vm_names():
    d = os.path.join(config.CFG["data_root"], "vmdefs")
    try:
        return sorted(n[:-5] for n in os.listdir(d) if n.endswith(".json") and re.fullmatch(VM_RE, n[:-5]))
    except OSError:
        return []


def site_from_form(data):
    """data: parse_qs dict (name -> list). Raises ValueError on unparsable numbers."""
    def one(k, default=""):
        return data.get(k, [default])[0].strip()
    try:
        port = int(one("port", "80"))
    except ValueError:
        raise ValueError("port must be a number")
    rules = {
        "methods": data.get("methods", []),
        "allow_paths": [l.strip() for l in one("allow_paths", "/").splitlines() if l.strip()],
        "strip_prefix": one("strip_prefix"),
        "block_encoded": "block_encoded" in data,
        "iis_profile": {k: ("iis_" + k) in data for k in IIS_ITEMS},
        "max_body": one("max_body", "1MB"),
    }
    return {"host": one("host"), "vm": one("vm"), "guest_ip": one("guest_ip"), "port": port,
            "enabled": "enabled" in data, "host_header": one("host_header", "original") or "original",
            "rules": rules}


def _checked(name, on):
    return " checked" if on else ""


def _form(req, site, error=""):
    s = dict(site)
    rules = dict(DEFAULT_RULES, **s.get("rules", {}))
    e = respond.esc
    vms = "".join('<option%s>%s</option>' % (" selected" if v == s.get("vm") else "", e(v)) for v in _vm_names())
    meth = "".join('<label><input type="checkbox" name="methods" value="%s"%s> %s</label> '
                   % (m, _checked("", m in rules["methods"]), m) for m in METHODS)
    iis = "".join('<label><input type="checkbox" name="iis_%s"%s> %s</label> '
                  % (k, _checked("", rules["iis_profile"].get(k, True)), k) for k in IIS_ITEMS)
    bodies = "".join("<option%s>%s</option>" % (" selected" if b == rules["max_body"] else "", b) for b in MAX_BODIES)
    return (
        '<p class="error">%s</p><form method="post" action="/manage/sites/save">'
        '<input type="hidden" name="csrf" value="%s">'
        '<p>Host name <input name="host" value="%s" required></p>'
        '<p>VM <select name="vm">%s</select> Guest IP <input name="guest_ip" value="%s" required> '
        'Port <input name="port" value="%s" size="5"></p>'
        '<p>Host header <input name="host_header" value="%s"> (original, or a name)</p>'
        '<p>Methods: %s</p><p>Allowed paths (one per line)<br><textarea name="allow_paths" rows="3">%s</textarea></p>'
        '<p>Strip prefix <input name="strip_prefix" value="%s"> Body limit <select name="max_body">%s</select></p>'
        '<p><label><input type="checkbox" name="block_encoded"%s> Block encoded and traversal URLs</label></p>'
        '<p>IIS 4 profile: %s</p>'
        '<p><label><input type="checkbox" name="enabled"%s> Enabled</label> <input type="submit" value="Save"></p></form>'
    ) % (e(error), e(req.session["csrf"]), e(s.get("host", "")), vms, e(s.get("guest_ip", "")),
         e(s.get("port", 80)), e(s.get("host_header", "original")), meth,
         e("\n".join(rules["allow_paths"])), e(rules["strip_prefix"]), bodies,
         _checked("", rules["block_encoded"]), iis, _checked("", s.get("enabled", True)))


def _list(req, error=""):
    e = respond.esc
    csrf = '<input type="hidden" name="csrf" value="%s">' % e(req.session["csrf"])
    rows = ""
    for s in load_sites():
        h = e(s["host"])
        rows += ('<tr><td><a href="/manage/sites?edit=%s">%s</a></td><td>%s</td><td>%s:%s</td><td>%s</td><td>'
                 '<form method="post" action="/manage/sites/%s/toggle">%s<input type="submit" value="%s"></form> '
                 '<form method="post" action="/manage/sites/%s/delete">%s<input type="submit" value="Delete"></form></td></tr>'
                 % (h, h, e(s["vm"]), e(s["guest_ip"]), e(s["port"]), "on" if s.get("enabled", True) else "off",
                    h, csrf, "Disable" if s.get("enabled", True) else "Enable", h, csrf))
    edit = next((s for s in load_sites() if s["host"] == req.query.get("edit", [""])[0]), {})
    body = ('<table><tr><th>Host</th><th>VM</th><th>Upstream</th><th>State</th><th></th></tr>%s</table>'
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
    except ValueError as e:
        return _list(req, str(e))
    sites = [s for s in load_sites() if s["host"] != site["host"]] + [site]
    ok, msg = _commit(req, sites, "site_save", site["host"])
    return respond.redirect("/manage/sites") if ok else _list(req, msg)


def h_delete(req, m):
    host = m.group(1)
    ok, msg = _commit(req, [s for s in load_sites() if s["host"] != host], "site_delete", host)
    return respond.redirect("/manage/sites") if ok else _list(req, msg)


def h_toggle(req, m):
    host = m.group(1)
    sites = [dict(s, enabled=not s.get("enabled", True)) if s["host"] == host else s for s in load_sites()]
    ok, msg = _commit(req, sites, "site_toggle", host)
    return respond.redirect("/manage/sites") if ok else _list(req, msg)


ROUTES = [
    ("GET", r"/manage/sites", h_list, "admin"),
    ("POST", r"/manage/sites/save", h_save, "admin"),
    ("POST", r"/manage/sites/([a-z0-9.-]+)/delete", h_delete, "admin"),
    ("POST", r"/manage/sites/([a-z0-9.-]+)/toggle", h_toggle, "admin"),
]
