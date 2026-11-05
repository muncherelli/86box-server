"""Download a file from a link into the media library, on the server (admins). The browser sends a URL; a
background thread fetches it into <kind>/.<name>.part and moves it into place like an upload (never overwriting).

The manager sits on the LAN and next to the boxes, so a link must not be a way to reach the server itself or
the cloud's metadata service: every host is resolved first and refused when any of its addresses is loopback,
link-local (169.254.169.254 included), multicast, reserved, unspecified or assigned to this server; the
connection then goes to the address that was checked (no second lookup), and every redirect is checked again.
Other LAN addresses are allowed, so a NAS on the LAN works. Jobs live in memory: a manager restart ends them
(their .part files are removed at the next start)."""
import email.message
import http.client
import ipaddress
import os
import re
import shutil
import socket
import ssl
import threading
import time
import urllib.parse
import uuid

import config
import media
import respond
import store

MAX_REDIRECTS = 5
MAX_URL = 2048
TIMEOUT = 30            # seconds without data before giving up
KEEP_DONE = 3600        # finished jobs stay listed this long
MAX_JOBS = 30
PARALLEL = 2            # downloads at once; the rest wait
USER_AGENT = "86server-media-fetch/1"
CHUNK = 1024 * 1024
_TEST_ALLOW = set()     # tests add 127.0.0.1 here to reach their local server

JOBS = {}
_lock = threading.Lock()
_slots = threading.Semaphore(PARALLEL)


class FetchError(Exception):
    pass


# ---- addresses -----------------------------------------------------------

def _is_local(ip):
    """True when ip is assigned to this host (binding to it succeeds only then)."""
    fam = socket.AF_INET6 if ip.version == 6 else socket.AF_INET
    try:
        with socket.socket(fam, socket.SOCK_STREAM) as s:
            s.bind((str(ip), 0))
        return True
    except OSError:
        return False


def address_problem(addr):
    """Why this address may not be fetched from, or None when it may."""
    try:
        ip = ipaddress.ip_address(addr.split("%", 1)[0])
    except ValueError:
        return "not an IP address"
    if str(ip) in _TEST_ALLOW:
        return None
    if ip.version == 6 and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    if ip.is_unspecified:
        return "an unspecified address"
    if ip.is_loopback:
        return "this server (loopback)"
    if ip.is_link_local:
        return "a link-local address (cloud metadata lives there)"
    if ip.is_multicast:
        return "a multicast address"
    if ip.is_reserved:
        return "a reserved address"
    if _is_local(ip):
        return "one of this server's own addresses"
    return None


def _resolve(host, port):
    """The address to connect to, after checking every address the name has."""
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as e:
        raise FetchError("cannot look up %s: %s" % (host, e.strerror or e))
    addrs = []
    for info in infos:
        a = info[4][0]
        if a not in addrs:
            addrs.append(a)
    if not addrs:
        raise FetchError("%s has no address" % host)
    for a in addrs:
        why = address_problem(a)
        if why:
            raise FetchError("%s points at %s, which is not allowed" % (host, why))
    return addrs[0]


# ---- URLs and names ------------------------------------------------------

def check_url(url):
    """Parsed URL parts (scheme, host, port, path) or FetchError."""
    if not url or len(url) > MAX_URL:
        raise FetchError("give a link of at most %d characters" % MAX_URL)
    try:
        u = urllib.parse.urlsplit(url.strip())
        port = u.port
    except ValueError:
        raise FetchError("that is not a valid link")
    if u.scheme not in ("http", "https"):
        raise FetchError("only http:// and https:// links")
    if not u.hostname:
        raise FetchError("the link has no host")
    if u.username or u.password:
        raise FetchError("links with a user name or password are not supported")
    path = u.path or "/"
    if u.query:
        path += "?" + u.query
    return u.scheme, u.hostname, port or (443 if u.scheme == "https" else 80), path


def redact(url):
    """scheme://host/path for logs: no user info, no query (signed links carry tokens there)."""
    try:
        u = urllib.parse.urlsplit(url)
        return "%s://%s%s" % (u.scheme, u.hostname or "", u.path)
    except ValueError:
        return "(bad url)"


def clean_name(name):
    """A file name the library accepts, made from whatever the server or link called it ('' if nothing usable)."""
    name = os.path.basename((name or "").replace("\\", "/")).strip()
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", name)
    name = re.sub(r"^[^A-Za-z0-9]+", "", name)
    if len(name) > 121:
        stem, dot, ext = name.rpartition(".")
        name = (stem[:120 - len(ext)] + "." + ext) if dot and len(ext) < 12 else name[:121]
    return name


def name_from(headers, url):
    cd = headers.get("Content-Disposition")
    if cd:
        msg = email.message.Message()
        msg["Content-Disposition"] = cd
        fn = msg.get_filename()
        if fn:
            return clean_name(fn)
    return clean_name(urllib.parse.unquote(urllib.parse.urlsplit(url).path.rsplit("/", 1)[-1]))


def pick_kind(name, want):
    """The folder for this file: `want` when it fits; with want 'auto' (or a folder that does not fit) the one
    folder whose extensions fit. FetchError when none fits or the choice is ambiguous."""
    fits = [k for k in media.KINDS if media.ext_ok(k, name)]
    if want in fits:
        return want
    if len(fits) == 1:
        return fits[0]
    ext = name.rsplit(".", 1)[1].lower() if "." in name else ""
    if not fits:
        if ext in ("zip", "7z", "rar", "gz", "xz", "bz2", "tar", "tgz"):
            raise FetchError("%s is an archive; archives are not unpacked yet. Unpack it and upload the image." % name)
        return_ext = (".%s" % ext) if ext else "no extension"
        raise FetchError("%s (%s) is not a file type 86Box uses" % (name, return_ext))
    labels = ", ".join(media.TAB_LABELS.get(k, k) for k in fits)
    raise FetchError(".%s fits several folders (%s): pick one under 'into'" % (ext, labels))


# ---- the download --------------------------------------------------------

def _connect(scheme, host, port, ip):
    if scheme == "https":
        c = http.client.HTTPSConnection(host, port, timeout=TIMEOUT, context=ssl.create_default_context())
    else:
        c = http.client.HTTPConnection(host, port, timeout=TIMEOUT)
    # Dial the address that was checked; the name stays in Host, SNI and the certificate check.
    c._create_connection = lambda addr, timeout=TIMEOUT, source_address=None: \
        socket.create_connection((ip, port), timeout, source_address)
    return c


def _open(url):
    """Follow redirects (each hop checked) to a 200 response. Returns (conn, response, final url)."""
    for _ in range(MAX_REDIRECTS + 1):
        scheme, host, port, path = check_url(url)
        ip = _resolve(host, port)
        conn = _connect(scheme, host, port, ip)
        try:
            conn.request("GET", path, headers={"User-Agent": USER_AGENT, "Accept": "*/*",
                                               "Accept-Encoding": "identity"})
            resp = conn.getresponse()
        except (OSError, http.client.HTTPException, ssl.SSLError) as e:
            conn.close()
            raise FetchError("could not connect to %s: %s" % (host, getattr(e, "reason", None) or e))
        if resp.status in (301, 302, 303, 307, 308):
            loc = resp.getheader("Location")
            conn.close()
            if not loc:
                raise FetchError("redirect without a location")
            url = urllib.parse.urljoin(url, loc)
            continue
        if resp.status != 200:
            conn.close()
            raise FetchError("the server answered HTTP %d %s" % (resp.status, resp.reason))
        return conn, resp, url
    raise FetchError("too many redirects")


def _set(job, **kw):
    with _lock:
        job.update(kw)


def _run(job, want_kind, want_name):
    part = None
    conn = None
    with _slots:
        if job["cancel"]:
            _set(job, state="cancelled", finished=time.time())
            return
        _set(job, state="connecting")
        try:
            conn, resp, final = _open(job["url"])
            ctype = (resp.getheader("Content-Type") or "").split(";", 1)[0].strip().lower()
            if ctype in ("text/html", "application/xhtml+xml"):
                raise FetchError("the link returned a web page, not a file (sharing sites often do: use the "
                                 "direct download link)")
            enc = (resp.getheader("Content-Encoding") or "identity").lower()
            if enc not in ("identity", ""):
                raise FetchError("the server sent the file compressed (%s)" % enc)
            name = clean_name(want_name) if want_name else name_from(resp.headers, final)
            if not name:
                raise FetchError("could not work out a file name: type one")
            kind = pick_kind(name, want_kind)
            err = media.valid_name(kind, name)
            if err:
                raise FetchError("%s: %s" % (name, err))
            total = resp.getheader("Content-Length")
            total = int(total) if total and total.isdigit() else None
            folder = config.media_dir(kind)
            final_path = os.path.join(folder, name)
            part = os.path.join(folder, "." + name + ".part")
            if os.path.lexists(final_path):
                raise FetchError("%s already exists in %s" % (name, media.TAB_LABELS.get(kind, kind)))
            if total is not None and shutil.disk_usage(folder).free < total + media.GIB:
                raise FetchError("not enough free disk space for %s" % media._size(total))
            try:
                f = open(part, "xb")
            except FileExistsError:
                part = None
                raise FetchError("%s is being uploaded or downloaded already" % name)
            _set(job, state="downloading", name=name, kind=kind, total=total)
            done = 0
            with f:
                while True:
                    if job["cancel"]:
                        raise FetchError("cancelled")
                    chunk = resp.read(CHUNK)
                    if not chunk:
                        break
                    f.write(chunk)
                    done += len(chunk)
                    _set(job, bytes=done)
                    if done % (64 * CHUNK) < CHUNK and shutil.disk_usage(folder).free < media.GIB:
                        raise FetchError("the disk is nearly full; stopped")
                f.flush()
                os.fsync(f.fileno())
            if total is not None and done != total:
                raise FetchError("the download ended early (%s of %s)" % (media._size(done), media._size(total)))
            os.chmod(part, 0o664)
            media._chgrp(part)
            try:
                os.link(part, final_path)
            except FileExistsError:
                raise FetchError("%s appeared while downloading; kept the existing file" % name)
            os.unlink(part)
            part = None
            _set(job, state="done", finished=time.time())
            store.audit(job["user"], job["ip"], "media.fetch",
                              "%s/%s %d bytes from %s" % (kind, name, done, redact(final)))
        except (FetchError, OSError, http.client.HTTPException, ssl.SSLError) as e:
            msg = str(e) if isinstance(e, FetchError) else "download failed: %s" % (getattr(e, "strerror", None) or e)
            state = "cancelled" if job["cancel"] else "failed"
            _set(job, state=state, error="" if state == "cancelled" else msg, finished=time.time())
        finally:
            if conn is not None:
                conn.close()
            if part and os.path.lexists(part):
                try:
                    os.unlink(part)
                except OSError:
                    pass


def _prune():
    now = time.time()
    for jid in [j for j, v in JOBS.items() if v.get("finished") and now - v["finished"] > KEEP_DONE]:
        del JOBS[jid]


def active():
    with _lock:
        return any(j["state"] in ("queued", "connecting", "downloading") for j in JOBS.values())


def _public(job):
    return {k: job.get(k) for k in ("id", "state", "name", "kind", "bytes", "total", "error", "started", "finished", "label")}


def start(url, kind, name, user, ip):
    check_url(url)
    if kind != "auto" and kind not in media.KINDS:
        raise FetchError("unknown folder")
    if name and not clean_name(name):
        raise FetchError("that file name has no usable characters")
    with _lock:
        _prune()
        if len([j for j in JOBS.values() if not j.get("finished")]) >= MAX_JOBS:
            raise FetchError("too many downloads waiting; try again later")
        jid = uuid.uuid4().hex[:12]
        u = urllib.parse.urlsplit(url)
        label = (clean_name(name) if name else "") or urllib.parse.unquote(u.path.rsplit("/", 1)[-1]) or u.hostname
        job = {"id": jid, "url": url, "state": "queued", "name": "", "kind": "", "bytes": 0, "total": None,
               "error": "", "started": time.time(), "finished": None, "cancel": False, "user": user, "ip": ip,
               "label": label}
        JOBS[jid] = job
    threading.Thread(target=_run, args=(job, kind, name), daemon=True, name="fetch-" + jid).start()
    return _public(job)


# ---- routes --------------------------------------------------------------

def h_start(req, m):
    f = req.form()
    try:
        job = start((f.get("url") or "").strip(), f.get("kind") or "auto", (f.get("name") or "").strip(),
                    req.user["name"], req.ip)
    except FetchError as e:
        return respond.json_resp({"ok": False, "error": str(e)}, 400)
    return respond.json_resp({"ok": True, "job": job}, 202)


def h_list(req, m):
    with _lock:
        _prune()
        jobs = sorted((_public(j) for j in JOBS.values()), key=lambda j: j["started"])
    return respond.json_resp({"jobs": jobs})


def h_cancel(req, m):
    with _lock:
        job = JOBS.get(m.group(1))
        if job and not job.get("finished"):
            job["cancel"] = True
            if job["state"] == "queued":  # its thread is still waiting for a slot
                job.update(state="cancelled", finished=time.time())
    if not job:
        return respond.json_resp({"ok": False, "error": "no such download"}, 404)
    return respond.json_resp({"ok": True})


ROUTES = [
    ("POST", r"/manage/api/media/fetch", h_start, "admin"),
    ("GET", r"/manage/api/media/fetches", h_list, "admin"),
    ("POST", r"/manage/api/media/fetch/([0-9a-f]{12})/cancel", h_cancel, "admin"),
]
