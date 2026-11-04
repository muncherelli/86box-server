"""Response helpers. A handler returns (status, headers, body_bytes)."""
import html
import json
import os
import string
import urllib.parse

_DIR = os.path.dirname(os.path.abspath(__file__))
# 86server dev creates this file; the normal deploy (rsync --delete) removes it.
DEV_MARKER = os.path.join(_DIR, ".dev")


def dev_mode():
    return os.path.exists(DEV_MARKER)


def scheme(req):
    """'https' or 'http' as the visitor sees it. Caddy sets X-B86-Proto; anything else counts as http."""
    return "https" if (req.headers.get("X-B86-Proto") or "").strip().lower() == "https" else "http"


def cookie_attrs(req):
    """Cookie flags: Secure only when the visitor is on https, so plain-http LAN logins still work."""
    return "Path=/; HttpOnly; Secure; SameSite=Strict" if scheme(req) == "https" else "Path=/; HttpOnly; SameSite=Strict"


def origin_ok(req, hosts):
    """A browser Origin header must name one of our hosts, over the scheme this request came in on."""
    origin = req.headers.get("Origin")
    if not origin:
        return True
    try:
        parts = urllib.parse.urlsplit(origin)
        port = parts.port
    except ValueError:
        return False
    if parts.scheme != scheme(req) or parts.path or port not in (None, 80, 443):
        return False
    return (parts.hostname or "") in [h.lower() for h in hosts if h]


def text(body, status=200, ctype="text/plain; charset=utf-8"):
    return status, {"Content-Type": ctype}, body.encode()


def json_resp(obj, status=200):
    return status, {"Content-Type": "application/json"}, json.dumps(obj).encode()


def redirect(location, status=303):
    return status, {"Location": location}, b""


def esc(value):
    return html.escape(str(value), quote=True)


def page(req, title, body_html, status=200):
    """Wrap body_html (already escaped by the caller) in layout.html."""
    with open(os.path.join(_DIR, "pages", "layout.html")) as f:
        tpl = string.Template(f.read())
    user = req.user["name"] if req.user else ""
    csrf = req.session["csrf"] if req.session else ""
    dev = '<meta name="dev-reload" content="1">' if dev_mode() else ""
    out = tpl.substitute(title=esc(title), body=body_html, user=esc(user), csrf=esc(csrf), dev=dev)
    return status, {"Content-Type": "text/html; charset=utf-8"}, out.encode()
