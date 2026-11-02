"""Settings: /opt/86server/etc/manager.json (written by Ansible). Tests set B86_CONFIG / B86_DATA_ROOT."""
import json
import os

_PATH = os.environ.get("B86_CONFIG", "/opt/86server/etc/manager.json")
# Network groups are changed from the web UI, so they are read live from what the root helper applied.
ETC = os.environ.get("B86_ETC", "/opt/86server/etc")
RUN = os.environ.get("B86_RUN", "/run/86server")
NETGROUPS_PATH = os.path.join(ETC, "netgroups.json")
NETPOLICY_PATH = os.path.join(ETC, "netpolicy.json")
NETAPPLY_RESULT = os.path.join(RUN, "netapply.json")
_cache = {}


def load():
    try:
        with open(_PATH) as f:
            cfg = json.load(f)
    except OSError:
        cfg = {}
    cfg.setdefault("data_root", "/opt/86server/data")
    cfg.setdefault("emu86box_tag", "")
    cfg.setdefault("share_group", "86box-data")
    cfg.pop("netgroups", None)  # an old manager.json may still carry it; the live file wins
    cfg.setdefault("edge_mode", "acme")
    cfg.setdefault("manager_host", "localhost")
    cfg.setdefault("manager_hosts", [])
    cfg.setdefault("publish_domains", [])
    cfg.setdefault("port_forward_limits", [])
    cfg.setdefault("public_ipv4", "")
    if os.environ.get("B86_DATA_ROOT"):
        cfg["data_root"] = os.environ["B86_DATA_ROOT"]
    # Everything 86server owns lives under /opt/86server, Caddy files included (Ansible writes these too).
    cfg.setdefault("sites_dir", "/opt/86server/etc/caddy/sites.d")
    cfg.setdefault("panel_dir", "/opt/86server/etc/caddy/panel.d")
    cfg.setdefault("validate_snippets", "/opt/86server/etc/caddy/validate-snippets.caddy")
    cfg.setdefault("certs_dir", os.path.join(cfg["data_root"], "certs"))
    cfg.setdefault("lego_bin", "/usr/bin/lego")
    return cfg


def manager_hosts():
    """Every name the manager is served under (manager_host, lan_hosts, tunnel_hosts, the Cloudflare panel name)."""
    hosts = list(CFG.get("manager_hosts") or [])
    extra = [CFG.get("manager_host"), panel_hostname()]
    hosts += [h for h in extra if h and h not in hosts]
    return hosts


def panel_hostname():
    """The control panel name set up on the Cloudflare page, if any (read live from panel.json)."""
    path = os.path.join(CFG["data_root"], "manager", "panel.json")
    panel = _read_cached(path, {})
    return (panel.get("hostname") or "") if panel.get("active") else ""


def _read_cached(path, default):
    """Re-read a small JSON file only when its mtime or size changes."""
    try:
        st = os.stat(path)
        key = (st.st_mtime_ns, st.st_size)
    except OSError:
        return default
    hit = _cache.get(path)
    if hit and hit[0] == key:
        return hit[1]
    try:
        with open(path) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return default
    if not isinstance(data, type(default)):
        return default
    _cache[path] = (key, data)
    return data


def netgroups():
    """{name: {bridge, cidr, egress, egress_allow, dhcp}}. Tests may set CFG["netgroups"] to override."""
    if "netgroups" in CFG:
        return CFG["netgroups"]
    return _read_cached(NETGROUPS_PATH, {})


def netpolicy():
    """{"group_routes": [[a, b], ...], "nat_dns": [...]} as applied."""
    pol = _read_cached(NETPOLICY_PATH, {})
    return {"group_routes": pol.get("group_routes", []), "nat_dns": pol.get("nat_dns", ["1.1.1.1"])}


CFG = load()
