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
    cfg.setdefault("media_root", "/opt/86server/media")
    cfg.setdefault("emu86box_tag", "")
    cfg.setdefault("share_group", "86server")
    cfg.setdefault("manager_hosts", [])
    cfg.setdefault("publish_domains", [])
    cfg.setdefault("port_forward_limits", [])
    if os.environ.get("B86_DATA_ROOT"):
        cfg["data_root"] = os.environ["B86_DATA_ROOT"]
    # Everything 86server owns lives under /opt/86server, Caddy files included (Ansible writes these too).
    cfg.setdefault("sites_dir", "/opt/86server/etc/caddy/sites.d")
    cfg.setdefault("panel_dir", "/opt/86server/etc/caddy/panel.d")
    cfg.setdefault("validate_snippets", "/opt/86server/etc/caddy/validate-snippets.caddy")
    cfg.setdefault("certs_dir", os.path.join(cfg["data_root"], "certs"))
    cfg.setdefault("lego_bin", "/usr/bin/lego")
    # Update notice (role selfupdate): what was deployed, and the state the clone's owner writes for us.
    cfg.setdefault("deployed_file", "/opt/86server/etc/deployed.json")
    cfg.setdefault("update_state_dir", os.path.join(cfg["data_root"], "update"))
    return cfg


def media_dir(*parts):
    """The media library (/opt/86server/media): one folder per kind of 86Box drive (see media.KINDS)."""
    return os.path.join(CFG["media_root"], *parts)


def manager_hosts():
    """Every name the manager is served under: lan_hosts and the Cloudflare panel name."""
    hosts = list(CFG.get("manager_hosts") or [])
    panel = panel_hostname()
    if panel and panel not in hosts:
        hosts.append(panel)
    return hosts


def edge_state():
    """The Cloudflare setup (manager/edge.json, written by edge.py), read live."""
    return _read_cached(os.path.join(CFG["data_root"], "manager", "edge.json"), {})


def panel_hostname():
    """The control panel name set up for Cloudflare, if any."""
    st = edge_state()
    panel = st.get("panel") or ""
    return panel if panel and zone_for(panel, st.get("zones") or {}) else ""


def tunnel_on():
    st = edge_state()
    return bool(st.get("tunnel") and st.get("tunnel_id") and st.get("dns"))


def zone_for(host, zones=None):
    """The Cloudflare zone whose wildcard certificate covers host (the zone itself or one label below), or ''."""
    if zones is None:
        zones = edge_state().get("zones") or {}
    for zone in zones:
        if host == zone or (host.endswith("." + zone) and "." not in host[:-len(zone) - 1]):
            return zone
    return ""


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
