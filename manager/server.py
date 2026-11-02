"""86server web manager: stdlib HTTP app on 127.0.0.1:8686. See README.md for the module contract."""
import importlib
import json
import os
import re
import sys
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import config
import respond

HERE = os.path.dirname(os.path.abspath(__file__))
MODULES = ["auth", "vms", "media", "snapshots", "stats", "sites", "secrets_ui", "importexp", "clone", "settings", "netgroups_ui", "cloudflare_ui"]
STATIC = {"style.css": "text/css", "app.js": "application/javascript",
          "media.js": "application/javascript", "cloudflare.js": "application/javascript"}
MAX_FORM = 1024 * 1024
BOOT_ID = str(time.time())  # changes on every restart; the dev page reload watches it

ROUTES = []  # (method, compiled regex, handler, role)
auth = None


def health(req, m):
    return respond.text("ok")


def dev_id(req, m):
    if not respond.dev_mode():
        return respond.text("not found", 404)
    return respond.text(BOOT_ID)


def static_file(req, m):
    name = m.group(1)
    if name not in STATIC:
        return respond.text("not found", 404)
    with open(os.path.join(HERE, "static", name), "rb") as f:
        cache = "no-store" if respond.dev_mode() else "max-age=300"
        return 200, {"Content-Type": STATIC[name], "Cache-Control": cache}, f.read()


def load_routes():
    global auth
    ROUTES.clear()
    ROUTES.append(("GET", re.compile(r"/manage/health"), health, None))
    ROUTES.append(("GET", re.compile(r"/manage/dev-id"), dev_id, None))
    ROUTES.append(("GET", re.compile(r"/manage/static/([a-z.]+)"), static_file, None))
    for name in MODULES:
        try:
            mod = importlib.import_module(name)
        except ModuleNotFoundError as e:
            if e.name != name:
                raise
            continue
        if name == "auth":
            auth = mod
        for method, pattern, handler, role in getattr(mod, "ROUTES", []):
            ROUTES.append((method, re.compile(pattern), handler, role))


class Request:
    def __init__(self, h):
        parts = urllib.parse.urlsplit(h.path)
        self.method = h.command
        self.path = parts.path
        self.query = urllib.parse.parse_qs(parts.query)
        self.headers = h.headers
        self.rfile = h.rfile
        self.length = int(h.headers.get("Content-Length") or 0)
        self._body = None
        self.ip = h.headers.get("X-B86-Client-IP") or "127.0.0.1"
        self.user = None     # set by auth.authenticate: {"name", "role", "vms"}
        self.session = None  # set by auth.authenticate: {"csrf", ...}

    def body(self):
        """Small bodies only (forms, JSON). Cached, so csrf_ok and the handler can both read it."""
        if self._body is None:
            if self.length > MAX_FORM:
                raise ValueError("body too large")
            self._body = self.rfile.read(self.length)
        return self._body

    def form(self):
        data = urllib.parse.parse_qs(self.body().decode("utf-8", "replace"), keep_blank_values=True)
        return {k: v[0] for k, v in data.items()}

    def json(self):
        return json.loads(self.body().decode("utf-8"))


def dispatch(req):
    handler = role = match = None
    path_known = False
    for method, pattern, h, r in ROUTES:
        m = pattern.fullmatch(req.path)
        if m:
            path_known = True
            if method == req.method:
                handler, role, match = h, r, m
                break
    if handler is None:
        return respond.text("method not allowed" if path_known else "not found", 405 if path_known else 404)

    if req.method in ("POST", "PUT"):
        if not respond.origin_ok(req, config.manager_hosts()):
            return respond.text("bad origin", 403)

    if role not in (None, "login"):
        if auth is None:
            return respond.text("login is not installed", 503)
        user = auth.authenticate(req)
        if user is None:
            if req.path.startswith("/manage/api/"):
                return respond.json_resp({"ok": False, "error": "login required"}, 401)
            return respond.redirect("/manage/login?next=" + urllib.parse.quote(req.path))
        if role == "admin" and user["role"] != "admin":
            return respond.text("forbidden", 403)
        if req.method in ("POST", "PUT") and not auth.csrf_ok(req):
            return respond.text("bad csrf token", 403)
    return handler(req, match)


class Handler(BaseHTTPRequestHandler):
    server_version = "86server-manager"

    def _go(self):
        try:
            status, headers, body = dispatch(Request(self))
        except ValueError as e:
            status, headers, body = respond.text(str(e), 400)
        except Exception as e:  # never leak details to the browser
            print("error handling %s %s: %r" % (self.command, self.path, e), file=sys.stderr, flush=True)
            status, headers, body = respond.text("server error", 500)
        self.send_response(status)
        headers.setdefault("Cache-Control", "no-store")
        headers["Content-Length"] = str(len(body))
        headers["X-Content-Type-Options"] = "nosniff"
        headers["X-Frame-Options"] = "DENY"
        for k, v in headers.items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    do_GET = do_POST = do_PUT = _go

    def log_message(self, fmt, *args):
        pass


def main():
    load_routes()
    if auth is not None and hasattr(auth, "init"):
        auth.init()
    port = int(os.environ.get("B86_PORT", "8686"))
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
