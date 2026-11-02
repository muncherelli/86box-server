"""The control panel under a real hostname: a Let's Encrypt certificate (DNS-01 through Cloudflare, via lego),
a Caddy site for it, and optionally a Cloudflare Tunnel with a DNS record.

State: <data_root>/manager/panel.json
    {"active", "hostname", "zone_id", "zone_name", "account_id", "email", "staging", "tunnel",
     "tunnel_id", "tunnel_name", "cert": {"expires", "issuer"}}
Files written:
    <data_root>/manager/lego/                lego's account and certificate store (0700)
    <certs_dir>/<host>.crt, <host>.key       copies Caddy reads (0640; the folder is setgid group caddy)
    <panel_dir>/panel.caddy                  the Caddy site (0644)
    <data_root>/manager/cloudflared.env      TUNNEL_TOKEN=... for 86server-cloudflared.service (0600)
Set up and remove run as a background job, one at a time; status() reports its steps.
"""
import datetime
import os
import re
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
RENEW_DAYS = 30
STEPS = ("certificate", "caddy", "tunnel", "dns", "cloudflared")
REMOVE_STEPS = ("tunnel", "dns", "cloudflared", "caddy", "delete tunnel")

_lock = threading.Lock()
JOB = {"kind": "", "running": False, "steps": [], "done": "", "ok": None}


# ---------- state ----------

def _mgr(*parts):
    return os.path.join(config.CFG["data_root"], "manager", *parts)


def load():
    data = store.read_json(_mgr("panel.json"), {})
    return data if isinstance(data, dict) else {}


def save(state):
    store.write_json(_mgr("panel.json"), state, mode=0o600)


def hostname_for(name, zone):
    """'box' + 'example.com' -> 'box.example.com'; '@' -> the zone itself."""
    name, zone = name.strip().lower().rstrip("."), zone.strip().lower()
    if name in ("", "@"):
        host = zone
    else:
        if not all(LABEL.fullmatch(p) for p in name.split(".")):
            raise ValueError("The name may use a-z, 0-9 and '-' between dots.")
        host = name + "." + zone
    if len(host) > 253 or not HOST.fullmatch(host):
        raise ValueError("Not a valid hostname: %s" % host)
    return host


def tunnel_name(host):
    return "86server-" + host.replace(".", "-")


def render_caddy(host):
    if not HOST.fullmatch(host):
        raise ValueError("bad hostname")
    certs = config.CFG["certs_dir"]
    return ("# managed by 86server: do not edit\n"
            "%s {\n"
            "\ttls %s/%s.crt %s/%s.key\n"
            "\timport b86_manager {http.request.scheme}\n"
            "}\n") % (host, certs, host, certs, host)


# ---------- job plumbing ----------

def status():
    with _lock:
        return {"kind": JOB["kind"], "running": JOB["running"], "done": JOB["done"], "ok": JOB["ok"],
                "steps": [dict(s) for s in JOB["steps"]]}


def _set(step, state, msg=""):
    with _lock:
        for s in JOB["steps"]:
            if s["name"] == step:
                s["state"], s["msg"] = state, msg


def start(kind, fn, steps, *args):
    """Run fn(*args) in a thread. Return False when a job is already running."""
    with _lock:
        if JOB["running"]:
            return False
        JOB.update(kind=kind, running=True, done="", ok=None,
                   steps=[{"name": n, "state": "pending", "msg": ""} for n in steps])
    threading.Thread(target=_run, args=(fn,) + args, daemon=True).start()
    return True


def _run(fn, *args):
    ok = False
    try:
        ok = fn(*args)
    except Exception as e:  # report, never kill the manager
        with _lock:
            for s in JOB["steps"]:
                if s["state"] == "running":
                    s["state"], s["msg"] = "error", "unexpected error: %s" % e
    finally:
        with _lock:
            JOB.update(running=False, ok=bool(ok),
                       done=datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))


def _step(name, fn):
    """Run one step; return True on success. fn returns a message or raises."""
    _set(name, "running")
    try:
        msg = fn()
    except (cloudflare.CFError, ValueError, OSError, subprocess.SubprocessError) as e:
        _set(name, "error", str(e))
        return False
    _set(name, "ok", msg or "")
    return True


# ---------- certificate (lego) ----------

def _lego_cmd(state, renew=False):
    cmd = [config.CFG["lego_bin"], "run", "--log.format", "text", "--accept-tos",
           "--dns", "cloudflare", "--domains", state["hostname"], "--cert.name", state["hostname"],
           "--path", _mgr("lego"), "--server", "letsencrypt-staging" if state.get("staging") else "letsencrypt"]
    if state.get("email"):
        cmd += ["--email", state["email"]]
    if renew:
        cmd += ["--renew-days", str(RENEW_DAYS)]
    return cmd


def _lego_files(host):
    """(cert, key) paths in lego's store for this certificate name."""
    root = _mgr("lego")
    want = {host + ".crt": None, host + ".key": None}
    for d, _, files in os.walk(root):
        for f in files:
            if f in want and want[f] is None:
                want[f] = os.path.join(d, f)
    if not all(want.values()):
        raise OSError("lego finished but its certificate files for %s were not found" % host)
    return want[host + ".crt"], want[host + ".key"]


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


def days_left(state, now=None):
    exp = (state.get("cert") or {}).get("expires")
    if not exp:
        return None
    end = datetime.datetime.strptime(exp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc)
    now = now or datetime.datetime.now(datetime.timezone.utc)
    return (end - now).total_seconds() / 86400


def obtain_cert(state, token, renew=False):
    """Run lego, copy the files where Caddy reads them, record expiry. Return a message."""
    host = state["hostname"]
    os.makedirs(_mgr("lego"), mode=0o700, exist_ok=True)
    env = dict(os.environ, CLOUDFLARE_DNS_API_TOKEN=token, HOME=_mgr(), LEGO_LOG_FORMAT="text")
    r = subprocess.run(_lego_cmd(state, renew), capture_output=True, text=True, env=env, timeout=900)
    if r.returncode:
        lines = [l for l in (r.stderr + r.stdout).splitlines() if l.strip()]
        raise OSError("lego failed: " + ("\n".join(lines[-4:]) or "exit %d" % r.returncode).replace(token, "***"))
    crt, key = _lego_files(host)
    certs = config.CFG["certs_dir"]
    for src, dst in ((crt, host + ".crt"), (key, host + ".key")):
        with open(src) as f:
            store.write_text(os.path.join(certs, dst), f.read(), mode=0o640)
    state["cert"] = cert_info(os.path.join(certs, host + ".crt"))
    save(state)
    return "valid until %s (%s)" % (state["cert"]["expires"][:10], state["cert"]["issuer"] or "unknown issuer")


# ---------- caddy ----------

def _panel_file():
    return os.path.join(config.CFG["panel_dir"], "panel.caddy")


def write_caddy(text):
    """Validate, write (or delete when text is None), reload; put the old file back if reload fails."""
    path = _panel_file()
    if text is not None:
        ok, msg = sites.caddy_validate(text)
        if not ok:
            raise ValueError("caddy rejected the site: " + msg)
    old = None
    if os.path.exists(path):
        with open(path) as f:
            old = f.read()
    if text is None:
        if old is None:
            return "nothing to remove"
        os.unlink(path)
    else:
        store.write_text(path, text, mode=0o644)
    ok, err = units.run("reload", "caddy.service")
    if not ok:
        if old is None:
            if os.path.exists(path):
                os.unlink(path)
        else:
            store.write_text(path, old, mode=0o644)
        units.run("reload", "caddy.service")
        raise OSError("caddy reload failed: " + err)
    return "caddy reloaded"


# ---------- set up ----------

def setup(state, token, replace_dns=False):
    """The setup job. state carries the form's choices; it is saved as it progresses."""
    host = state["hostname"]
    state["active"] = False
    save(state)

    if not _step("certificate", lambda: obtain_cert(state, token)):
        return False

    def caddy():
        msg = write_caddy(render_caddy(host))
        state["active"] = True
        save(state)
        return msg
    if not _step("caddy", caddy):
        return False

    if not state.get("tunnel"):
        for s in ("tunnel", "dns", "cloudflared"):
            _set(s, "skipped", "no tunnel requested")
        return True

    def tunnel():
        acct, name = state["account_id"], tunnel_name(host)
        t = cloudflare.find_tunnel(token, acct, name)
        made = t is None
        if made:
            t = cloudflare.create_tunnel(token, acct, name)
        state.update(tunnel_id=t["id"], tunnel_name=name)
        save(state)
        cfg = cloudflare.get_config(token, acct, t["id"])
        rule = {"service": "https://localhost:443",
                "originRequest": {"originServerName": host, "httpHostHeader": host}}
        cloudflare.put_config(token, acct, t["id"], cloudflare.merge_ingress(cfg, host, rule))
        ttoken = cloudflare.tunnel_token(token, acct, t["id"])
        store.write_text(_mgr("cloudflared.env"), "TUNNEL_TOKEN=%s\n" % ttoken, mode=0o600)
        return "%s tunnel %s, route %s -> https://localhost:443" % ("created" if made else "reused", name, host)
    if not _step("tunnel", tunnel):
        return False

    if not _step("dns", lambda: point_dns(state, token, replace_dns)):
        return False

    def run_cloudflared():
        ok, err = units.run("restart", CLOUDFLARED)
        if not ok:
            raise OSError("could not start %s: %s" % (CLOUDFLARED, err))
        return "running"
    return _step("cloudflared", run_cloudflared)


def point_dns(state, token, replace=False):
    host, zone = state["hostname"], state["zone_id"]
    target = cloudflare.tunnel_target(state["tunnel_id"])
    records = cloudflare.find_records(token, zone, host)
    ours = [r for r in records if r.get("type") == "CNAME" and r.get("content", "").lower() == target]
    others = [r for r in records if r not in ours]
    if others and not replace:
        raise ValueError("%s already has DNS records (%s). Tick 'Replace existing DNS records' to replace them."
                         % (host, ", ".join("%s %s" % (r.get("type"), r.get("content")) for r in others)))
    for r in others:
        cloudflare.delete_record(token, zone, r["id"])
    if ours:
        cloudflare.update_cname(token, zone, ours[0]["id"], host, target, "managed by 86server")
        return "CNAME %s -> %s (updated)" % (host, target)
    cloudflare.create_cname(token, zone, host, target, "managed by 86server")
    return "CNAME %s -> %s (created)" % (host, target)


# ---------- remove ----------

def remove(state, token, delete_tunnel=False):
    host, acct, tid = state.get("hostname", ""), state.get("account_id", ""), state.get("tunnel_id", "")
    ok = True

    def tunnel():
        if not tid or not token:
            return "no tunnel"
        cfg = cloudflare.get_config(token, acct, tid)
        cloudflare.put_config(token, acct, tid, cloudflare.merge_ingress(cfg, host, None))
        return "route for %s removed" % host
    ok &= _step("tunnel", tunnel)

    def dns():
        if not tid or not token:
            return "no tunnel record"
        target = cloudflare.tunnel_target(tid)
        n = 0
        for r in cloudflare.find_records(token, state["zone_id"], host):
            if r.get("type") == "CNAME" and r.get("content", "").lower() == target:
                cloudflare.delete_record(token, state["zone_id"], r["id"])
                n += 1
        return "%d record(s) removed" % n
    ok &= _step("dns", dns)

    def stop():
        units.run("stop", CLOUDFLARED)
        if os.path.exists(_mgr("cloudflared.env")):
            os.unlink(_mgr("cloudflared.env"))
        return "stopped"
    ok &= _step("cloudflared", stop)

    def caddy():
        msg = write_caddy(None)
        state["active"] = False
        save(state)
        return msg
    ok &= _step("caddy", caddy)

    if delete_tunnel and tid and token:
        def gone():
            cloudflare.clean_connections(token, acct, tid)
            cloudflare.delete_tunnel(token, acct, tid)
            state.pop("tunnel_id", None)
            save(state)
            return "tunnel deleted"
        ok &= _step("delete tunnel", gone)
    else:
        _set("delete tunnel", "skipped", "tunnel kept")
    return bool(ok)


# ---------- renewal (cli.py renew-certs) ----------

def renew(now=None, log=print):
    """Renew the panel certificate when it has fewer than RENEW_DAYS left. Return an exit code."""
    state = load()
    if not state.get("active") or not state.get("hostname"):
        log("no panel certificate to renew")
        return 0
    left = days_left(state, now)
    if left is not None and left > RENEW_DAYS:
        log("%s: %d days left, nothing to do" % (state["hostname"], int(left)))
        return 0
    token = secrets_store.get("cf_api_token")
    if not token:
        log("%s: due for renewal but no Cloudflare API token is set" % state["hostname"])
        return 1
    try:
        msg = obtain_cert(state, token, renew=True)
    except (OSError, subprocess.SubprocessError) as e:
        log("%s: renewal failed: %s" % (state["hostname"], e))
        return 1
    ok, err = units.run("reload", "caddy.service")
    if not ok:
        log("%s: renewed, but caddy reload failed: %s" % (state["hostname"], err))
        return 1
    store.audit("system", "-", "cert-renew", state["hostname"])
    log("%s: renewed, %s" % (state["hostname"], msg))
    return 0


def wait(timeout=5.0):
    """Tests: wait for the running job to finish."""
    end = time.time() + timeout
    while time.time() < end:
        if not status()["running"]:
            return True
        time.sleep(0.02)
    return False
