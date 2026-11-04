"""Published sites: validated site records -> Caddy site files, validate, reload.

A site is a set of forwarding rules (where traffic goes) and filtering rules (what may pass).
Sites in a Cloudflare zone (edge.py) use the zone's wildcard certificate and get a tunnel route and DNS record;
sites under publish_domains use Caddy's local CA (LAN only).
Nothing here is specific to one guest OS; optional presets (like iis4) add extra filters.

sites.json holds the records. render_site() turns one record into Caddy text and
re-checks every value first (never write a file from unchecked data).
apply_sites() renders all enabled sites, validates them with the real caddy,
writes <sites_dir>/<host>.caddy and reloads caddy (rolling back on failure).
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

# Optional filter presets. Each preset has named items the owner can switch on one by one.
# item -> list of regex parts. "dirs" match the start of the path, "exts" match the end.
PRESETS = {
    "iis4": {
        "label": "IIS 4 hardening",
        "dirs": {
            "admin_dirs": ["scripts", "iisadmin", "iisadmpwd", "iishelp", "iissamples", "msadc", "printers"],
            "frontpage": ["_vti_[a-z_]*", "_private"],
        },
        "exts": {
            "ism_ext": ["htr", "ida", "idq", "printer", "htw", "idc", "htx"],
            "ssi_ext": ["stm", "shtm", "shtml"],
            "asa_ext": ["asa", "cdx", "cer"],
        },
    },
}
SCHEMES = ["https", "http"]

ENC_EXPR = ('{http.request.orig_uri}.matches("(?i)^[^?]*'
            '(%25|%2e|%2f|%5c|%c0|%c1|%u|\\\\.\\\\.|::|\\\\\\\\)")')

DEFAULT_RULES = {
    "methods": ["GET", "HEAD", "POST"],
    "allow_paths": ["/"],
    "strip_prefix": "",
    "block_encoded": True,
    "profiles": {},          # preset name -> {item: True}; a preset with no items on is off
    "max_body": "1MB",
}


def preset_items(name):
    p = PRESETS[name]
    return list(p["dirs"]) + list(p["exts"])


# ---------- validation and rendering ----------

def _netgroups():
    return config.netgroups()


def _vm_groups(vm):
    if not re.fullmatch(VM_RE, vm):
        raise ValueError("bad box name")
    path = os.path.join(config.CFG["data_root"], "boxdefs", vm + ".json")
    rec = store.read_json(path)
    if rec is None:
        raise ValueError("unknown box '%s'" % vm)
    return rec.get("allowed_groups", [])


def _check_profiles(profiles):
    if not isinstance(profiles, dict):
        raise ValueError("bad presets")
    out = {}
    for name, items in profiles.items():
        if name not in PRESETS or not isinstance(items, dict):
            raise ValueError("unknown preset")
        known = preset_items(name)
        if not all(k in known and isinstance(v, bool) for k, v in items.items()):
            raise ValueError("bad item in preset '%s'" % name)
        out[name] = {k: items.get(k, False) for k in known}
    return out


def check_site(site):
    """Return the site with defaults filled in, or raise ValueError."""
    host = site.get("host", "")
    if not isinstance(host, str) or not re.fullmatch(HOST_RE, host):
        raise ValueError("bad host name")
    if not config.zone_for(host) and not any(host == d or host.endswith("." + d)
                                             for d in config.CFG["publish_domains"]):
        raise ValueError("host is not in a Cloudflare zone set up for 86server (one level below it) "
                         "or under publish_domains")

    scheme = site.get("scheme", "https")
    if scheme not in SCHEMES:
        raise ValueError("scheme must be https or http")

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
        raise ValueError("guest IP is not inside one of the box's network groups")

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
    rules.update(profiles=_check_profiles(rules["profiles"]), block_encoded=bool(rules["block_encoded"]))
    return dict(site, rules=rules, port=port, host_header=hh, scheme=scheme)


def preset_regex(name, items):
    """One regex for the enabled items of a preset, or '' when none is on."""
    p = PRESETS[name]
    dirs = [x for item in p["dirs"] if items.get(item) for x in p["dirs"][item]]
    exts = [x for item in p["exts"] if items.get(item) for x in p["exts"][item]]
    parts = []
    if dirs:
        parts.append("^/(" + "|".join(dirs) + ")(/|$)")
    if exts:
        parts.append("\\.(" + "|".join(exts) + ")$")
    return "|".join(parts)


def render_site(site, cert_zone=""):
    """cert_zone: the Cloudflare zone whose wildcard certificate covers the host; '' = Caddy's local CA."""
    s = check_site(site)
    r = s["rules"]
    presets = [(n, preset_regex(n, r["profiles"][n])) for n in PRESETS if n in r["profiles"]]
    presets = [(n, rx) for n, rx in presets if rx]
    deny = "^(" + "|".join(p.replace(".", "\\.") for p in r["allow_paths"]) + ")"
    methods = [m for m in METHODS if m in r["methods"]]
    if s["scheme"] == "http":
        out = ["# managed by 86server: do not edit", "http://" + s["host"] + " {"]
    else:
        certs = config.CFG["certs_dir"]
        tls = ("tls %s/%s.crt %s/%s.key" % (certs, cert_zone, certs, cert_zone)) if cert_zone else "tls internal"
        out = ["# managed by 86server: do not edit", s["host"] + " {", "\t" + tls]
    out += ["\trequest_body {", "\t\tmax_size " + r["max_body"], "\t}"]
    if r["block_encoded"]:
        out.append("\t@b86enc expression `" + ENC_EXPR + "`")
    out.append("\t@b86method not method " + " ".join(methods))
    for n, rx in presets:
        out.append("\t@b86p_%s path_regexp (?i)%s" % (n, rx))
    out.append("\t@b86deny not path_regexp " + deny)
    out.append("\troute {")
    if r["block_encoded"]:
        out.append("\t\trespond @b86enc 400")
    out.append("\t\trespond @b86method 405")
    for n, _ in presets:
        out.append("\t\trespond @b86p_%s 404" % n)
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


def caddy_validate(text):
    """Check site text with the real caddy, after the snippet stubs. Return (ok, message)."""
    work = os.path.join(config.CFG["data_root"], "manager")
    with open(config.CFG["validate_snippets"]) as f:
        snippets = f.read()
    vfile = os.path.join(work, "validate.Caddyfile")
    store.write_text(vfile, snippets + text, mode=0o600)
    env = dict(os.environ, HOME=work, XDG_DATA_HOME=work, XDG_CONFIG_HOME=work)
    r = subprocess.run([os.environ.get("B86_CADDY", "caddy"), "validate", "--adapter", "caddyfile",
                        "--config", vfile], capture_output=True, text=True, env=env, timeout=60)
    if r.returncode:
        return False, _caddy_error(r.stderr)
    return True, "valid"


def apply_sites(sites=None):
    """Return (ok, message). On any problem the live files stay as they were."""
    with LOCK:
        sites = load_sites() if sites is None else sites
        sites_dir = config.CFG["sites_dir"]
        work = os.path.join(config.CFG["data_root"], "manager")
        try:
            files = {s["host"] + ".caddy": render_site(s, config.zone_for(s["host"]))
                     for s in sites if s.get("enabled", True)}
        except ValueError as e:
            return False, str(e)

        ok, msg = caddy_validate("".join(files.values()))
        if not ok:
            return False, msg

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


# ---------- form parsing ----------

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
        "profiles": {n: {k: True for k in preset_items(n) if "%s_%s" % (n, k) in data} for n in PRESETS},
        "max_body": one("max_body", "1MB"),
    }
    return {"host": one("host"), "vm": one("vm"), "guest_ip": one("guest_ip"), "port": port,
            "enabled": "enabled" in data, "scheme": one("scheme", "https"), "host_header": one("host_header", "original") or "original",
            "rules": rules}


import sites_pages  # noqa: E402  (page handlers; must come last)

ROUTES = sites_pages.ROUTES
