"""Cloudflare edge: one wildcard Let's Encrypt certificate per zone (DNS-01 through Cloudflare, via lego), the
control panel under a name in one of those zones, and one Cloudflare Tunnel for this server that routes the panel
and every published site in those zones, each with a proxied CNAME.

Because the certificates are real, the same names work on the LAN without the tunnel: point local DNS at this
server and Caddy answers on 443 itself.

State: <data_root>/manager/edge.json
    {"zones": {"example.com": {"id", "account_id", "cert": {"expires", "issuer", "staging"}}},
     "panel": "panel.example.com" or "", "tunnel": true, "email": "", "staging": false,
     "tunnel_id", "tunnel_name", "dns": [names whose CNAME we made]}
sync() makes Caddy, Cloudflare and cloudflared match that state. It is safe to run again at any time: the web
page, `86server cloudflare` and publishing a site all just change the state and sync.

Files written:
    <data_root>/manager/lego/                lego's account and certificate store (0700)
    <certs_dir>/<zone>.crt, <zone>.key       copies Caddy reads (0640; the folder is setgid group caddy)
    <panel_dir>/panel.caddy                  the control panel's Caddy site (0644)
    <data_root>/manager/cloudflared.env      TUNNEL_TOKEN=... for 86server-cloudflared.service (0600)
"""
import contextlib
import datetime
import fcntl
import os
import re
import socket
import ssl
import subprocess
import threading
import time

import cloudflare
import config
import secrets_store
import sites
import store
import units

LABEL = re.compile(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?")
HOST = re.compile(r"([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}")
CLOUDFLARED = "86server-cloudflared.service"
TOKEN = "cf_api_token"
RENEW_DAYS = 30
STEPS = ("certificates", "caddy", "tunnel", "dns", "cloudflared")

_lock = threading.Lock()
JOB = {"kind": "", "running": False, "steps": [], "done": "", "ok": None, "again": False}


# ---------- state ----------

def _mgr(*parts):
    return os.path.join(config.CFG["data_root"], "manager", *parts)


def load():
    data = store.read_json(_mgr("edge.json"), {})
    if not isinstance(data, dict):
        data = {}
    data.setdefault("zones", {})
    data.setdefault("panel", "")
    data.setdefault("tunnel", True)
    data.setdefault("dns", [])
    return data


def save(state):
    store.write_json(_mgr("edge.json"), state, mode=0o600)


def hostname_for(name, zone):
    """'box' + 'example.com' -> 'box.example.com'; '@' or '' -> the zone itself. One label only, so the zone's
    wildcard certificate covers it."""
    name, zone = name.strip().lower().rstrip("."), zone.strip().lower()
    if name in ("", "@"):
        host = zone
    else:
        if not LABEL.fullmatch(name):
            raise ValueError("The name may use a-z, 0-9 and '-' (one label, no dots: the zone's wildcard "
                             "certificate covers one level).")
        host = name + "." + zone
    if len(host) > 253 or not HOST.fullmatch(host):
        raise ValueError("Not a valid hostname: %s" % host)
    return host


def configure(old, token_zones, zone_names, panel_name="", panel_zone="", email="", staging=False, tunnel=True):
    """The new state for the choices made on the web page or in `86server cloudflare setup`. Raises ValueError.

    token_zones: list_zones() for the saved token. zone_names: the zones to use. The panel's zone is always used.
    """
    by_name = {z["name"]: z for z in token_zones}
    names = set(zone_names)
    if panel_zone:
        names.add(panel_zone)
    unknown = sorted(n for n in names if n not in by_name)
    if unknown:
        raise ValueError("The token cannot see %s." % ", ".join(unknown))
    panel = hostname_for(panel_name, panel_zone) if panel_zone else ""
    email = email.strip()
    if email and (len(email) > 254 or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email)):
        raise ValueError("That email address does not look right.")
    dropped = set(old.get("zones") or {}) - names
    used = sorted(s["host"] for s in sites.load_sites() if config.zone_for(s.get("host", ""), dropped))
    if used:
        raise ValueError("Published sites still use the zone(s) you left out: %s." % ", ".join(used))
    zones = {}
    for n in sorted(names):
        z = dict((old.get("zones") or {}).get(n) or {})
        z.update(id=by_name[n]["id"], account_id=by_name[n]["account_id"])
        zones[n] = z
    return dict(old, zones=zones, panel=panel, email=email, staging=bool(staging), tunnel=bool(tunnel))


def tunnel_name():
    """One tunnel per server, named after it."""
    host = re.sub(r"[^a-z0-9-]+", "-", socket.gethostname().split(".")[0].lower()).strip("-") or "host"
    return "86server-" + host


def render_panel(host, zone):
    if not HOST.fullmatch(host) or not HOST.fullmatch(zone):
        raise ValueError("bad hostname")
    certs = config.CFG["certs_dir"]
    return ("# managed by 86server: do not edit\n"
            "%s {\n"
            "\ttls %s/%s.crt %s/%s.key\n"
            "\timport b86_manager {http.request.scheme}\n"
            "}\n") % (host, certs, zone, certs, zone)


def routes(state):
    """[(hostname, zone, ingress rule)] for the panel and every enabled site in our zones."""
    zones = state.get("zones") or {}
    out = []
    if state.get("panel"):
        zone = config.zone_for(state["panel"], zones)
        if zone:
            out.append((state["panel"], zone, "https"))
    for s in sites.load_sites():
        zone = config.zone_for(s.get("host", ""), zones)
        if zone and s.get("enabled", True):
            out.append((s["host"], zone, s.get("scheme", "https")))
    rules = []
    for host, zone, scheme in out:
        if scheme == "http":
            rule = {"service": "http://localhost:80", "originRequest": {"httpHostHeader": host}}
        else:
            rule = {"service": "https://localhost:443",
                    "originRequest": {"originServerName": host, "httpHostHeader": host}}
        rules.append((host, zone, dict(rule, hostname=host)))
    return rules


# ---------- reporting: a background job for the web page, or lines on a terminal ----------

class JobReport:
    def __call__(self, step, state, msg=""):
        with _lock:
            for s in JOB["steps"]:
                if s["name"] == step:
                    s["state"], s["msg"] = state, msg


class PrintReport:
    def __init__(self, out=print):
        self.out = out

    def __call__(self, step, state, msg=""):
        if state != "running":
            self.out("%-13s %-8s %s" % (step, state, msg))


def _step(report, name, fn):
    """Run one step; return True on success. fn returns a message or raises."""
    report(name, "running")
    try:
        msg = fn()
    except (cloudflare.CFError, ValueError, OSError, subprocess.SubprocessError) as e:
        report(name, "error", str(e))
        return False
    report(name, "ok", msg or "")
    return True


@contextlib.contextmanager
def _exclusive():
    """One sync at a time across processes (the manager's job and `86server cloudflare`)."""
    os.makedirs(_mgr(), exist_ok=True)
    with open(_mgr("edge.lock"), "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def status():
    with _lock:
        return {"kind": JOB["kind"], "running": JOB["running"], "done": JOB["done"], "ok": JOB["ok"],
                "steps": [dict(s) for s in JOB["steps"]]}


def start(kind, fn, *args):
    """Run fn(*args, report=JobReport()) in a thread. Return False when a job is already running."""
    with _lock:
        if JOB["running"]:
            return False
        JOB.update(kind=kind, running=True, done="", ok=None, again=False,
                   steps=[{"name": n, "state": "pending", "msg": ""} for n in STEPS])
    threading.Thread(target=_run, args=(kind, fn) + args, daemon=True).start()
    return True


def _run(kind, fn, *args):
    ok = False
    try:
        ok = fn(*args, report=JobReport())
    except Exception as e:  # report, never kill the manager
        with _lock:
            for s in JOB["steps"]:
                if s["state"] == "running":
                    s["state"], s["msg"] = "error", "unexpected error: %s" % e
    finally:
        with _lock:
            again = JOB["again"]
            JOB.update(running=False, ok=bool(ok), again=False,
                       done=datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
    if again:
        request_sync()


def request_sync():
    """After a site change: bring the tunnel routes and DNS up to date in the background (or right after the
    running job). Does nothing when Cloudflare is not set up."""
    state, token = load(), secrets_store.get(TOKEN)
    if not state["zones"] or not token:
        return False
    with _lock:
        if JOB["running"]:
            JOB["again"] = True
            return True
    return start("sync", sync, state, token)


# ---------- certificates (lego) ----------

def _lego_cmd(zone, state):
    return [config.CFG["lego_bin"], "run", "--log.format", "text", "--accept-tos", "--dns", "cloudflare",
            "--domains", "*." + zone, "--domains", zone, "--cert.name", zone, "--path", _mgr("lego"),
            "--server", "letsencrypt-staging" if state.get("staging") else "letsencrypt"] + \
        (["--email", state["email"]] if state.get("email") else [])


def _lego_files(name):
    """(cert, key) paths in lego's store for this certificate name."""
    want = {name + ".crt": None, name + ".key": None}
    for d, _, files in os.walk(_mgr("lego")):
        for f in files:
            if f in want and want[f] is None:
                want[f] = os.path.join(d, f)
    if not all(want.values()):
        raise OSError("lego finished but its certificate files for %s were not found" % name)
    return want[name + ".crt"], want[name + ".key"]


def cert_info(path):
    """{"expires": iso utc, "issuer": text} for a PEM certificate."""
    r = subprocess.run(["openssl", "x509", "-noout", "-enddate", "-issuer", "-nameopt", "oneline", "-in", path],
                       capture_output=True, text=True, timeout=10)
    if r.returncode:
        raise OSError("cannot read the certificate: " + (r.stderr.strip() or "openssl failed"))
    out = dict(l.split("=", 1) for l in r.stdout.splitlines() if "=" in l)
    ts = ssl.cert_time_to_seconds(out["notAfter"].strip())
    return {"expires": datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "issuer": out.get("issuer", "").strip()}


def days_left(cert, now=None):
    exp = (cert or {}).get("expires")
    if not exp:
        return None
    end = datetime.datetime.strptime(exp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc)
    now = now or datetime.datetime.now(datetime.timezone.utc)
    return (end - now).total_seconds() / 86400


def cert_due(state, zone, now=None):
    """True when the zone has no usable certificate: none yet, the files are gone, staging changed, or it ends
    within RENEW_DAYS."""
    cert = state["zones"][zone].get("cert") or {}
    if not os.path.exists(os.path.join(config.CFG["certs_dir"], zone + ".crt")):
        return True
    if bool(cert.get("staging")) != bool(state.get("staging")):
        return True
    left = days_left(cert, now)
    return left is None or left <= RENEW_DAYS


def obtain_cert(state, zone, token):
    """Run lego for *.zone and zone, copy the files where Caddy reads them, record expiry. Return a message."""
    os.makedirs(_mgr("lego"), mode=0o700, exist_ok=True)
    env = dict(os.environ, CLOUDFLARE_DNS_API_TOKEN=token, HOME=_mgr(), LEGO_LOG_FORMAT="text")
    r = subprocess.run(_lego_cmd(zone, state), capture_output=True, text=True, env=env, timeout=900)
    if r.returncode:
        lines = [l for l in (r.stderr + r.stdout).splitlines() if l.strip()]
        raise OSError("lego failed for %s: %s" % (zone, ("\n".join(lines[-4:]) or "exit %d" % r.returncode)
                                                  .replace(token, "***")))
    crt, key = _lego_files(zone)
    certs = config.CFG["certs_dir"]
    for src, dst in ((crt, zone + ".crt"), (key, zone + ".key")):
        with open(src) as f:
            store.write_text(os.path.join(certs, dst), f.read(), mode=0o640)
    cert = dict(cert_info(os.path.join(certs, zone + ".crt")), staging=bool(state.get("staging")))
    state["zones"][zone]["cert"] = cert
    save(state)
    return "*.%s valid until %s" % (zone, cert["expires"][:10])


# ---------- caddy ----------

def _panel_file():
    return os.path.join(config.CFG["panel_dir"], "panel.caddy")


def write_panel(text):
    """Validate, write (or delete when text is None), reload; put the old file back if reload fails."""
    path = _panel_file()
    old = None
    if os.path.exists(path):
        with open(path) as f:
            old = f.read()
    if text == old:
        return "panel site unchanged"
    if text is not None:
        ok, msg = sites.caddy_validate(text)
        if not ok:
            raise ValueError("caddy rejected the panel site: " + msg)
        store.write_text(path, text, mode=0o644)
    else:
        os.unlink(path)
    ok, err = units.run("reload", "caddy.service")
    if not ok:
        if old is None:
            if os.path.exists(path):
                os.unlink(path)
        else:
            store.write_text(path, old, mode=0o644)
        units.run("reload", "caddy.service")
        raise OSError("caddy reload failed: " + err)
    return "panel site written" if text is not None else "panel site removed"


# ---------- sync ----------

def sync(state, token, replace_dns=False, report=None):
    """Make everything match state. Returns True when every step worked; stops at the first failing step."""
    report = report or PrintReport()
    with _exclusive():
        return _sync(state, token, replace_dns, report)


def _sync(state, token, replace_dns, report):
    save(state)

    def certificates():
        if not state["zones"]:
            return "no zones"
        done = []
        for zone in sorted(state["zones"]):
            if cert_due(state, zone):
                if not token:
                    raise ValueError("certificate for %s is due but no Cloudflare API token is saved" % zone)
                done.append(obtain_cert(state, zone, token))
            else:
                done.append("*.%s valid until %s" % (zone, state["zones"][zone]["cert"]["expires"][:10]))
        return "; ".join(done)
    if not _step(report, "certificates", certificates):
        return False

    def caddy():
        panel_zone = config.zone_for(state["panel"], state["zones"]) if state["panel"] else ""
        if state["panel"] and not panel_zone:
            raise ValueError("%s is not in one of the zones" % state["panel"])
        msg = write_panel(render_panel(state["panel"], panel_zone) if state["panel"] else None)
        ok, smsg = sites.apply_sites()
        if not ok:
            raise ValueError("published sites: " + smsg)
        return msg + "; sites applied"
    if not _step(report, "caddy", caddy):
        return False

    wanted = routes(state) if state.get("tunnel") else []

    def tunnel():
        if not wanted:
            if state.get("tunnel_id") and token:
                acct = _account(state)
                cfg = cloudflare.get_config(token, acct, state["tunnel_id"])
                cloudflare.put_config(token, acct, state["tunnel_id"], dict(cfg or {}, ingress=[dict(cloudflare.CATCH_ALL)]))
                return "no routes (tunnel %s kept)" % state.get("tunnel_name", "")
            return "tunnel off" if not state.get("tunnel") else "nothing to route yet"
        if not token:
            raise ValueError("no Cloudflare API token is saved")
        acct, name = _account(state), state.get("tunnel_name") or tunnel_name()
        t = cloudflare.find_tunnel(token, acct, name)
        made = t is None
        if made:
            t = cloudflare.create_tunnel(token, acct, name)
        state.update(tunnel_id=t["id"], tunnel_name=t.get("name", name))
        save(state)
        cfg = dict(cloudflare.get_config(token, acct, t["id"]) or {})
        cfg["ingress"] = [rule for _, _, rule in wanted] + [dict(cloudflare.CATCH_ALL)]
        cloudflare.put_config(token, acct, t["id"], cfg)
        store.write_text(_mgr("cloudflared.env"), "TUNNEL_TOKEN=%s\n" % cloudflare.tunnel_token(token, acct, t["id"]),
                         mode=0o600)
        return "%s tunnel %s: %s" % ("created" if made else "using", state["tunnel_name"],
                                     ", ".join(h for h, _, _ in wanted))
    if not _step(report, "tunnel", tunnel):
        return False

    def dns():
        want = {h: z for h, z, _ in wanted}
        done = []
        if want:
            target = cloudflare.tunnel_target(state["tunnel_id"])
            for host, zone in sorted(want.items()):
                done.append(point_dns(token, state["zones"][zone]["id"], host, target, replace_dns))
        for host in [h for h in state.get("dns", []) if h not in want]:
            zone = config.zone_for(host, state["zones"])
            if zone and token and state.get("tunnel_id"):
                n = drop_dns(token, state["zones"][zone]["id"], host, cloudflare.tunnel_target(state["tunnel_id"]))
                done.append("%s removed (%d)" % (host, n))
        state["dns"] = sorted(want)
        save(state)
        return "; ".join(done) or "no records"
    if not _step(report, "dns", dns):
        return False

    def run_cloudflared():
        if wanted:
            ok, err = units.run("restart", CLOUDFLARED)
            if not ok:
                raise OSError("could not start %s: %s" % (CLOUDFLARED, err))
            return "running"
        units.run("stop", CLOUDFLARED)
        if os.path.exists(_mgr("cloudflared.env")):
            os.unlink(_mgr("cloudflared.env"))
        return "stopped"
    return _step(report, "cloudflared", run_cloudflared)


def _account(state):
    accts = {z.get("account_id") for z in state["zones"].values() if z.get("account_id")}
    if not accts:
        raise ValueError("no Cloudflare account known for these zones")
    return sorted(accts)[0]


def point_dns(token, zone_id, host, target, replace=False):
    records = cloudflare.find_records(token, zone_id, host)
    ours = [r for r in records if r.get("type") == "CNAME" and r.get("content", "").lower() == target]
    others = [r for r in records if r not in ours]
    if others and not replace:
        raise ValueError("%s already has DNS records (%s). Choose 'replace existing DNS records' to replace them."
                         % (host, ", ".join("%s %s" % (r.get("type"), r.get("content")) for r in others)))
    for r in others:
        cloudflare.delete_record(token, zone_id, r["id"])
    if ours:
        return "%s ok" % host
    cloudflare.create_cname(token, zone_id, host, target, "managed by 86server")
    return "%s created" % host


def drop_dns(token, zone_id, host, target):
    n = 0
    for r in cloudflare.find_records(token, zone_id, host):
        if r.get("type") == "CNAME" and r.get("content", "").lower() == target:
            cloudflare.delete_record(token, zone_id, r["id"])
            n += 1
    return n


def delete_tunnel(state, token):
    """After a sync with the tunnel off: delete the tunnel itself at Cloudflare."""
    if not state.get("tunnel_id"):
        return "no tunnel"
    acct = _account(state)
    cloudflare.clean_connections(token, acct, state["tunnel_id"])
    cloudflare.delete_tunnel(token, acct, state["tunnel_id"])
    state.pop("tunnel_id", None)
    state.pop("tunnel_name", None)
    save(state)
    return "tunnel deleted"


# ---------- renewal (cli.py renew-certs, daily) ----------

def renew(now=None, log=print):
    """Renew every zone certificate with fewer than RENEW_DAYS left. Return an exit code."""
    state = load()
    due = [z for z in sorted(state["zones"]) if cert_due(state, z, now)]
    if not due:
        log("no certificate is due" if state["zones"] else "no certificates to renew")
        return 0
    token = secrets_store.get(TOKEN)
    if not token:
        log("%s: due for renewal but no Cloudflare API token is saved" % ", ".join(due))
        return 1
    rc = 0
    with _exclusive():
        for zone in due:
            try:
                log(obtain_cert(state, zone, token))
                store.audit("system", "-", "cert-renew", zone)
            except (OSError, subprocess.SubprocessError) as e:
                log("%s: renewal failed: %s" % (zone, e))
                rc = 1
        ok, err = units.run("reload", "caddy.service")
        if not ok:
            log("caddy reload failed: %s" % err)
            rc = 1
    return rc


def wait(timeout=5.0):
    """Tests: wait for the running job to finish."""
    end = time.time() + timeout
    while time.time() < end:
        if not status()["running"]:
            return True
        time.sleep(0.02)
    return False
