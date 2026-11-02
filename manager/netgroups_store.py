"""Network groups: checks, and the request/result handshake with the root helper.

The manager has no network privileges. To change groups it writes the complete desired state to
<data_root>/manager/netgroups-request.json; the root helper validates and applies it, writes the applied
state to /opt/86server/etc/netgroups.json and netpolicy.json, and reports in /run/86server/netapply.json.
"""
import ipaddress
import os
import re
import time
import uuid

import config
import store

NAME_RE = re.compile(r"[a-z][a-z0-9]{0,11}")
PRIVATE = [ipaddress.ip_network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")]
MIN_PREFIX, MAX_PREFIX = 16, 29
APPLY_TIMEOUT = 30        # seconds to wait for the helper before giving up
SYSNET = "/sys/class/net"  # tests point this at a temp folder


class NetError(Exception):
    """A message that is safe to show to the admin."""


# ---- reading the applied state

def groups():
    """Applied groups, each with every field filled in. Example: {'lab': {'bridge': 'br-lab', 'cidr': ..., ...}}."""
    out = {}
    for name, g in config.netgroups().items():
        if isinstance(g, dict):
            out[name] = {"bridge": g.get("bridge", ""), "cidr": g.get("cidr", ""),
                         "egress": "nat" if g.get("egress") == "nat" else "none",
                         "egress_allow": list(g.get("egress_allow") or []), "dhcp": bool(g.get("dhcp"))}
    return out


def routes():
    """Applied group pairs as sorted tuples, without duplicates."""
    seen = []
    for pair in config.netpolicy()["group_routes"]:
        if isinstance(pair, (list, tuple)) and len(pair) == 2 and pair[0] != pair[1]:
            t = tuple(sorted(pair))
            if t not in seen:
                seen.append(t)
    return seen


def reaches(name):
    """Groups that `name` can talk to through a pair."""
    return sorted({a if b == name else b for a, b in routes() if name in (a, b)})


def vms_using(name):
    import vms
    return sorted(v for v, d in vms.all_defs().items() if name in d.get("allowed_groups", []))


def bridge_present(bridge):
    return bool(bridge) and os.path.isdir(os.path.join(SYSNET, bridge, "bridge"))


# ---- checks and suggestions

def parse_cidr(text, others):
    """A valid private IPv4 network that does not overlap `others` (dict of other groups). Returns 'a.b.c.d/n'."""
    try:
        net = ipaddress.ip_network((text or "").strip(), strict=True)
    except ValueError:
        raise NetError("'%s' is not a network like 10.86.0.0/24 (the address must be the first one of the network)."
                       % (text or "").strip()[:40])
    if net.version != 4 or not any(net.subnet_of(p) for p in PRIVATE):
        raise NetError("Use a private range: 10.x.x.x, 172.16-31.x.x or 192.168.x.x.")
    if not MIN_PREFIX <= net.prefixlen <= MAX_PREFIX:
        raise NetError("The network size must be between /%d and /%d." % (MIN_PREFIX, MAX_PREFIX))
    for name, g in others.items():
        try:
            if net.overlaps(ipaddress.ip_network(g["cidr"])):
                raise NetError("That network overlaps the group '%s' (%s)." % (name, g["cidr"]))
        except ValueError:
            continue
    return str(net)


def suggest_subnets(existing, count=3):
    """The first free 10.86.N.0/24 networks."""
    out = []
    for n in range(256):
        cand = "10.86.%d.0/24" % n
        try:
            parse_cidr(cand, existing)
        except NetError:
            continue
        out.append(cand)
        if len(out) == count:
            break
    return out


def guest_help(cidr, nat, dns):
    """What to type in the guest for this network: an example address, mask, gateway and DNS."""
    net = ipaddress.ip_network(cidr)
    hosts = list(net.hosts()) if net.num_addresses <= 256 else [net.network_address + i for i in range(1, 12)]
    return {"gateway": str(hosts[0]), "example": str(hosts[min(9, len(hosts) - 1)]), "mask": str(net.netmask),
            "dns": dns[0] if (nat and dns) else ""}


# ---- building and sending a change

def desired(current, name, record, reach, remove=False):
    """The complete new state after changing `name`. `record` = {cidr, egress, dhcp}; `reach` = groups to pair with it."""
    out = {n: {k: v for k, v in g.items() if v != "" or k != "bridge"} for n, g in current.items()}
    if remove:
        out.pop(name, None)
    else:
        old = out.get(name, {})
        out[name] = {"cidr": record["cidr"], "egress": record["egress"],
                     "egress_allow": old.get("egress_allow", []), "dhcp": bool(record["dhcp"])}
        if old.get("bridge"):
            out[name]["bridge"] = old["bridge"]
    pairs = [t for t in routes() if name not in t and t[0] in out and t[1] in out]
    if not remove:
        pairs += [tuple(sorted((name, r))) for r in reach if r in out and r != name]
    return {"netgroups": out, "group_routes": [list(t) for t in sorted(set(pairs))],
            "nat_dns": config.netpolicy()["nat_dns"]}


def _request_path():
    return os.path.join(config.CFG["data_root"], "manager", "netgroups-request.json")


def status(now=None):
    """{state: applying|ok|error|idle, message, warnings}. Based on the newest request and the helper's answer."""
    now = time.time() if now is None else now
    req = store.read_json(_request_path(), None)
    res = store.read_json(config.NETAPPLY_RESULT, None)
    req_id = req.get("id") if isinstance(req, dict) else None
    answered = isinstance(res, dict) and req_id is not None and res.get("id") == req_id
    if req_id and not answered:
        try:
            age = now - os.stat(_request_path()).st_mtime
        except OSError:
            age = APPLY_TIMEOUT + 1
        if age <= APPLY_TIMEOUT:
            return {"state": "applying", "message": "Applying the change...", "warnings": []}
        return {"state": "error", "warnings": [],
                "message": "The network helper did not answer within %d seconds, so the change was not applied. "
                           "Check that the 86server-netapply service is running." % APPLY_TIMEOUT}
    if isinstance(res, dict) and "ok" in res:
        return {"state": "ok" if res["ok"] else "error", "message": str(res.get("message", "")),
                "warnings": [str(w) for w in (res.get("warnings") or [])]}
    return {"state": "idle", "message": "", "warnings": []}


def submit(body):
    """Send a complete desired state to the helper. Returns the request id."""
    if status()["state"] == "applying":
        raise NetError("Another change is still being applied. Wait a few seconds and try again.")
    rid = uuid.uuid4().hex
    store.write_json(_request_path(), dict({"id": rid}, **body), mode=0o600)
    return rid
