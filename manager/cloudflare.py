"""Small Cloudflare API v4 client (urllib only). The token is passed per call and never put in errors or logs.

Every function names the doc page it follows. Errors raise CFError with Cloudflare's own messages.
"""
import json
import urllib.error
import urllib.parse
import urllib.request

BASE = "https://api.cloudflare.com/client/v4"
TIMEOUT = 20
CATCH_ALL = {"service": "http_status:404"}


class CFError(Exception):
    pass


def _call(token, method, path, query=None, body=None):
    url = BASE + path
    if query:
        url += "?" + urllib.parse.urlencode(query)
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", "Bearer " + token)
    req.add_header("Accept", "application/json")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            raw = r.read()
    except urllib.error.HTTPError as e:
        raw = e.read() or b""
        msg = _messages(raw) or "HTTP %d" % e.code
        raise CFError("%s %s: %s" % (method, path, msg)) from None
    except (urllib.error.URLError, OSError) as e:
        raise CFError("%s %s: cannot reach Cloudflare (%s)" % (method, path, getattr(e, "reason", e))) from None
    try:
        out = json.loads(raw.decode() or "{}")
    except ValueError:
        raise CFError("%s %s: Cloudflare sent something that is not JSON" % (method, path)) from None
    if not out.get("success", False):
        raise CFError("%s %s: %s" % (method, path, _messages(raw) or "request failed"))
    return out


def _messages(raw):
    try:
        errs = json.loads(raw.decode()).get("errors") or []
    except (ValueError, AttributeError, UnicodeDecodeError):
        return ""
    return "; ".join(str(e.get("message", e)) if isinstance(e, dict) else str(e) for e in errs)


# ---------- zones ----------

# https://developers.cloudflare.com/api/resources/zones/methods/list/  (Zone > Zone > Read; per_page max 50)
def list_zones(token):
    """[{id, name, account_id, account_name}] for every active zone the token can see."""
    zones, page = [], 1
    while True:
        out = _call(token, "GET", "/zones", {"per_page": 50, "page": page, "status": "active"})
        for z in out.get("result") or []:
            acct = z.get("account") or {}
            zones.append({"id": z["id"], "name": z["name"], "account_id": acct.get("id", ""),
                          "account_name": acct.get("name", "")})
        info = out.get("result_info") or {}
        if page >= int(info.get("total_pages") or 1):
            return zones
        page += 1


# ---------- tunnels ----------

# https://developers.cloudflare.com/api/resources/zero_trust/subresources/tunnels/subresources/cloudflared/methods/list/
def find_tunnel(token, account, name):
    out = _call(token, "GET", "/accounts/%s/cfd_tunnel" % account, {"name": name, "is_deleted": "false"})
    for t in out.get("result") or []:
        if t.get("name") == name and not t.get("deleted_at"):
            return t
    return None


# https://developers.cloudflare.com/api/resources/zero_trust/subresources/tunnels/subresources/cloudflared/methods/create/
def create_tunnel(token, account, name):
    """A remotely-managed tunnel (its routes live in Cloudflare, set with put_ingress)."""
    return _call(token, "POST", "/accounts/%s/cfd_tunnel" % account,
                 body={"name": name, "config_src": "cloudflare"})["result"]


# https://developers.cloudflare.com/api/resources/zero_trust/subresources/tunnels/subresources/cloudflared/methods/get/
def get_tunnel(token, account, tunnel_id):
    return _call(token, "GET", "/accounts/%s/cfd_tunnel/%s" % (account, tunnel_id))["result"]


# https://developers.cloudflare.com/api/resources/zero_trust/subresources/tunnels/subresources/cloudflared/subresources/token/methods/get/
def tunnel_token(token, account, tunnel_id):
    """The token cloudflared runs with (result is a plain string). A secret."""
    return _call(token, "GET", "/accounts/%s/cfd_tunnel/%s/token" % (account, tunnel_id))["result"]


# https://developers.cloudflare.com/api/resources/zero_trust/subresources/tunnels/subresources/cloudflared/subresources/connections/methods/get/
def tunnel_connections(token, account, tunnel_id):
    return _call(token, "GET", "/accounts/%s/cfd_tunnel/%s/connections" % (account, tunnel_id)).get("result") or []


# https://developers.cloudflare.com/api/resources/zero_trust/subresources/tunnels/subresources/cloudflared/subresources/connections/methods/delete/
def clean_connections(token, account, tunnel_id):
    _call(token, "DELETE", "/accounts/%s/cfd_tunnel/%s/connections" % (account, tunnel_id))


# https://developers.cloudflare.com/api/resources/zero_trust/subresources/tunnels/subresources/cloudflared/methods/delete/
def delete_tunnel(token, account, tunnel_id):
    _call(token, "DELETE", "/accounts/%s/cfd_tunnel/%s" % (account, tunnel_id))


# https://developers.cloudflare.com/api/resources/zero_trust/subresources/tunnels/subresources/cloudflared/subresources/configurations/methods/get/
def get_config(token, account, tunnel_id):
    """The tunnel's config dict ({} when none was set yet)."""
    out = _call(token, "GET", "/accounts/%s/cfd_tunnel/%s/configurations" % (account, tunnel_id))
    return (out.get("result") or {}).get("config") or {}


# https://developers.cloudflare.com/api/resources/zero_trust/subresources/tunnels/subresources/cloudflared/subresources/configurations/methods/update/
def put_config(token, account, tunnel_id, cfg):
    _call(token, "PUT", "/accounts/%s/cfd_tunnel/%s/configurations" % (account, tunnel_id), body={"config": cfg})


def merge_ingress(cfg, hostname, rule=None):
    """Return a new config: every other hostname's rule kept in order, ours replaced (or removed when rule is
    None), and exactly one catch-all at the end."""
    cfg = dict(cfg or {})
    rules = [r for r in cfg.get("ingress") or [] if r.get("hostname") and r.get("hostname") != hostname]
    if rule is not None:
        rules.append(dict(rule, hostname=hostname))
    cfg["ingress"] = rules + [dict(CATCH_ALL)]
    return cfg


# ---------- DNS records ----------

# https://developers.cloudflare.com/api/resources/dns/subresources/records/methods/list/
def find_records(token, zone_id, name):
    out = _call(token, "GET", "/zones/%s/dns_records" % zone_id, {"name.exact": name, "per_page": 100})
    return [r for r in out.get("result") or [] if r.get("name", "").lower() == name.lower()]


# https://developers.cloudflare.com/api/resources/dns/subresources/records/methods/create/
def create_cname(token, zone_id, name, target, comment=""):
    body = {"type": "CNAME", "name": name, "content": target, "proxied": True, "ttl": 1}
    if comment:
        body["comment"] = comment
    return _call(token, "POST", "/zones/%s/dns_records" % zone_id, body=body)["result"]


# https://developers.cloudflare.com/api/resources/dns/subresources/records/methods/edit/
def update_cname(token, zone_id, record_id, name, target, comment=""):
    body = {"type": "CNAME", "name": name, "content": target, "proxied": True, "ttl": 1}
    if comment:
        body["comment"] = comment
    return _call(token, "PATCH", "/zones/%s/dns_records/%s" % (zone_id, record_id), body=body)["result"]


# https://developers.cloudflare.com/api/resources/dns/subresources/records/methods/delete/
def delete_record(token, zone_id, record_id):
    _call(token, "DELETE", "/zones/%s/dns_records/%s" % (zone_id, record_id))


def tunnel_target(tunnel_id):
    return "%s.cfargotunnel.com" % tunnel_id
