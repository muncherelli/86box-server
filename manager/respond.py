"""Response helpers. A handler returns (status, headers, body_bytes)."""
import html
import json
import os
import string

_DIR = os.path.dirname(os.path.abspath(__file__))


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
    out = tpl.substitute(title=esc(title), body=body_html, user=esc(user), csrf=esc(csrf))
    return status, {"Content-Type": "text/html; charset=utf-8"}, out.encode()
