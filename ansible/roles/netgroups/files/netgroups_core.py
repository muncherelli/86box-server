"""Network groups: check a request and render the config files. Pure functions, no root, no side effects.

Used by netgroups-apply (root) and by the unit tests. The request comes from the web manager, which is
less trusted than root, so everything is checked here before anything on the host changes.
"""
import ipaddress
import re

NAME_RE = re.compile(r"[a-z][a-z0-9]{0,11}")
BRIDGE_RE = re.compile(r"br-[a-z0-9]{1,12}")
ID_RE = re.compile(r"[0-9a-f]{8,64}")
DEST_RE = re.compile(r"[0-9]{1,3}(\.[0-9]{1,3}){3}(/[0-9]{1,2})?")
PRIVATE = [ipaddress.ip_network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")]
MAX_GROUPS = 32
MAX_ROUTES = 64
MAX_ALLOW = 32
REQUEST_KEYS = {"id", "netgroups", "group_routes", "nat_dns"}
GROUP_KEYS = {"cidr", "egress", "egress_allow", "dhcp", "bridge"}
EDGE_MODES = ("acme", "tunnel", "cf-origin", "internal")
DEFAULT_NAT_DNS = ["1.1.1.1"]


def _text(value):
    return isinstance(value, str)


def _ipv4(value):
    try:
        return ipaddress.IPv4Address(value) if _text(value) else None
    except ValueError:
        return None


def _group(name, g, problems):
    """Check one group; returns the full group (all keys present) or None."""
    if not isinstance(g, dict):
        problems.append("%s: must be an object" % name)
        return None
    bad = False
    for key in g:
        if key not in GROUP_KEYS:
            problems.append("%s: unknown setting '%s'" % (name, key))
            bad = True
    bridge = g.get("bridge", "br-" + name)
    if not _text(bridge) or not BRIDGE_RE.fullmatch(bridge):
        problems.append("%s: bridge name must be br- plus 1-12 of a-z, 0-9" % name)
        bad = True
    net = None
    try:
        net = ipaddress.ip_network(g["cidr"]) if _text(g.get("cidr")) else None
    except ValueError:
        net = None
    if net is None or net.version != 4:
        problems.append("%s: cidr must be an IPv4 network such as 10.86.0.0/24" % name)
        bad = True
    elif not 16 <= net.prefixlen <= 29:
        problems.append("%s: cidr %s must have a prefix from /16 to /29" % (name, net))
        bad = True
    elif not any(net.subnet_of(p) for p in PRIVATE):
        problems.append("%s: cidr %s must be a private range (10.x, 172.16-31.x or 192.168.x)" % (name, net))
        bad = True
    egress = g.get("egress", "none")
    if egress not in ("none", "nat"):
        problems.append("%s: egress must be none or nat" % name)
        bad = True
    allow = g.get("egress_allow", [])
    if not isinstance(allow, list) or len(allow) > MAX_ALLOW:
        problems.append("%s: egress_allow must be a list of at most %d addresses or networks" % (name, MAX_ALLOW))
        allow, bad = [], True
    for dest in allow:
        try:
            ok = _text(dest) and DEST_RE.fullmatch(dest) and ipaddress.ip_network(dest, strict=False).version == 4
        except ValueError:
            ok = False
        if not ok:
            problems.append("%s: egress_allow entry %r is not an IPv4 address or network" % (name, dest))
            bad = True
    dhcp = g.get("dhcp", False)
    if not isinstance(dhcp, bool):
        problems.append("%s: dhcp must be true or false" % name)
        bad = True
    if bad:
        return None
    return {"bridge": bridge, "cidr": str(net), "egress": egress, "egress_allow": list(allow), "dhcp": dhcp}


def _edge(edge, problems):
    if edge is None:
        return {}
    if not isinstance(edge, dict) or set(edge) - {"edge_mode", "cf4_ranges", "cf6_ranges"}:
        problems.append("edge: unknown settings")
        return {}
    out = {}
    if "edge_mode" in edge:
        if edge["edge_mode"] not in EDGE_MODES:
            problems.append("edge: edge_mode must be one of %s" % ", ".join(EDGE_MODES))
        out["edge_mode"] = edge["edge_mode"]
    for key, version in (("cf4_ranges", 4), ("cf6_ranges", 6)):
        ranges = edge.get(key, [])
        try:
            if not isinstance(ranges, list) or len(ranges) > 100:
                raise ValueError
            for r in ranges:
                if not _text(r) or ipaddress.ip_network(r).version != version:
                    raise ValueError
        except ValueError:
            problems.append("edge: %s must be a list of IPv%d networks" % (key, version))
            ranges = []
        out[key] = list(ranges)
    return out


def validate(req, host=None, seed=False):
    """Check a request. Returns (problems, normalized); normalized is None when there are problems.

    host (optional) describes this machine: {"nets": [IPv4Network, ...] on other interfaces,
    "devices": {name: is_bridge}, "managed": {bridge names we already own}}.
    seed=True is the first-time request from Ansible: it may carry "edge" and may adopt an existing bridge.
    """
    if not isinstance(req, dict):
        return ["the request is not a JSON object"], None
    problems = []
    for key in req:
        if key not in REQUEST_KEYS and not (seed and key == "edge"):
            problems.append("unknown key '%s'" % key)
    rid = "seed" if seed else req.get("id")
    if not seed and not (_text(rid) and ID_RE.fullmatch(rid)):
        problems.append("id must be 8-64 lowercase hex characters")
    groups_in = req.get("netgroups", {})
    if not isinstance(groups_in, dict):
        problems.append("netgroups must be an object")
        groups_in = {}
    if len(groups_in) > MAX_GROUPS:
        problems.append("at most %d groups" % MAX_GROUPS)
    groups = {}
    for name in sorted(groups_in, key=str):
        if not _text(name) or not NAME_RE.fullmatch(name):
            problems.append("group name %r must be 1-12 characters: a-z and 0-9, starting with a letter" % name)
            continue
        g = _group(name, groups_in[name], problems)
        if g:
            groups[name] = g
    names = list(groups)
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            if ipaddress.ip_network(groups[a]["cidr"]).overlaps(ipaddress.ip_network(groups[b]["cidr"])):
                problems.append("%s and %s: their networks overlap" % (a, b))
            if groups[a]["bridge"] == groups[b]["bridge"]:
                problems.append("%s and %s: they use the same bridge name" % (a, b))
    routes = req.get("group_routes", [])
    clean_routes = []
    if not isinstance(routes, list) or len(routes) > MAX_ROUTES:
        problems.append("group_routes must be a list of at most %d pairs" % MAX_ROUTES)
        routes = []
    seen = set()
    for pair in routes:
        if not (isinstance(pair, list) and len(pair) == 2 and all(_text(p) for p in pair)
                and pair[0] != pair[1] and all(p in groups_in for p in pair)):
            problems.append("group_routes entry %r needs two different existing group names" % (pair,))
        elif frozenset(pair) in seen:
            problems.append("group_routes lists %s and %s twice" % tuple(pair))
        else:
            seen.add(frozenset(pair))
            clean_routes.append(list(pair))
    dns = req.get("nat_dns", DEFAULT_NAT_DNS)
    if not isinstance(dns, list) or not 1 <= len(dns) <= 3 or not all(_ipv4(d) for d in dns):
        problems.append("nat_dns must be a list of 1-3 IPv4 addresses")
        dns = DEFAULT_NAT_DNS
    edge = _edge(req.get("edge"), problems) if seed else None
    if host is not None:
        problems += check_host(groups, host, seed)
    if problems:
        return problems, None
    norm = {"id": rid, "netgroups": groups, "group_routes": clean_routes, "nat_dns": list(dns)}
    if edge is not None:
        norm["edge"] = edge
    return [], norm


def check_host(groups, host, seed=False):
    problems = []
    for name, g in groups.items():
        net = ipaddress.ip_network(g["cidr"])
        for other in host.get("nets", []):
            if net.overlaps(other):
                problems.append("%s: cidr %s overlaps %s, which is already in use on this host" % (name, net, other))
                break
        bridge = g["bridge"]
        devices = host.get("devices", {})
        if bridge in devices:
            if not devices[bridge]:
                problems.append("%s: %s already exists and is not a bridge" % (name, bridge))
            elif bridge not in host.get("managed", ()) and not seed:
                problems.append("%s: a bridge called %s already exists and is not managed here" % (name, bridge))
    return problems


def host_nets(addr, routes, ignore=()):
    """IPv4 networks in use on this host, from `ip -j addr` and `ip -j route`, leaving out the ignored devices."""
    nets = []
    for iface in addr:
        if iface.get("ifname") in ignore:
            continue
        for a in iface.get("addr_info", []):
            if a.get("family") == "inet":
                try:
                    nets.append(ipaddress.ip_network("%s/%s" % (a["local"], a["prefixlen"]), strict=False))
                except (KeyError, ValueError):
                    pass
    for r in routes:
        if r.get("dev") in ignore or r.get("dst") in (None, "default"):
            continue
        try:
            net = ipaddress.ip_network(r["dst"], strict=False)
        except ValueError:
            continue
        if net.version == 4:
            nets.append(net)
    return nets


def dhcp_pool(cidr):
    """.100-.199 when the network is big enough, else its upper half. The router is the host's .1."""
    net = ipaddress.ip_network(str(cidr))
    base = int(net.network_address)
    if net.num_addresses >= 256:
        first, last = base + 100, base + 199
    else:
        first, last = base + net.num_addresses // 2, base + net.num_addresses - 2
    return {"first": str(ipaddress.ip_address(first)), "last": str(ipaddress.ip_address(last)),
            "netmask": str(net.netmask), "router": str(next(net.hosts()))}


def host_address(cidr):
    net = ipaddress.ip_network(cidr)
    return "%s/%d" % (next(net.hosts()), net.prefixlen)


def needs_forwarding(groups, routes):
    return bool(routes) or any(g["egress"] == "nat" for g in groups.values())


def render_nm(bridges):
    """NetworkManager must ignore every bridge of ours, including one kept only because a VM is plugged in."""
    names = "".join("interface-name:%s;" % b for b in bridges)
    return "# managed by 86box-server: do not edit\n[keyfile]\nunmanaged-devices=%sinterface-name:tap*\n" % names


def render_nft(groups, routes, edge=None):
    edge = edge or {}
    cf = edge.get("edge_mode") == "cf-origin" and bool(edge.get("cf4_ranges"))
    out = ["# managed by 86box-server: do not edit",
           "# The first two lines make loading atomic: the table is replaced in one step.",
           "table inet b86box", "delete table inet b86box", "table inet b86box {",
           "  set ourbr {", "    type ifname"]
    if groups:
        out.append("    elements = { %s }" % ", ".join('"%s"' % g["bridge"] for g in groups.values()))
    out.append("  }")
    if cf:
        out += ["  set cf4 {", "    type ipv4_addr", "    flags interval",
                "    elements = { %s }" % ", ".join(edge["cf4_ranges"]), "  }",
                "  set cf6 {", "    type ipv6_addr", "    flags interval",
                "    elements = { %s }" % ", ".join(edge.get("cf6_ranges", [])), "  }"]
    out += ["  chain input {", "    type filter hook input priority filter - 5; policy accept;"]
    if cf:
        out += ['    iifname "lo" accept', "    tcp dport { 80, 443 } ip saddr != @cf4 drop",
                "    tcp dport { 80, 443 } ip6 saddr != @cf6 drop"]
    out.append("    iifname @ourbr ct state established,related accept")
    out += ['    iifname "%s" udp dport 67 accept' % g["bridge"] for g in groups.values() if g["dhcp"]]
    out += ["    iifname @ourbr drop", "  }", "  chain forward {",
            "    type filter hook forward priority filter - 5; policy accept;",
            "    ct state established,related accept"]
    for a, b in routes:
        ba, bb = groups[a]["bridge"], groups[b]["bridge"]
        out.append('    iifname "%s" oifname "%s" accept' % (ba, bb))
        out.append('    iifname "%s" oifname "%s" accept' % (bb, ba))
    for g in groups.values():
        if g["egress"] != "nat":
            continue
        if g["egress_allow"]:
            out.append('    iifname "%s" oifname != @ourbr ip daddr { %s } accept' % (g["bridge"], ", ".join(g["egress_allow"])))
        else:
            out.append('    iifname "%s" oifname != @ourbr accept' % g["bridge"])
    out += ["    iifname @ourbr drop", "    oifname @ourbr drop", "  }", "  chain postrouting {",
            "    type nat hook postrouting priority srcnat; policy accept;"]
    out += ["    ip saddr %s oifname != @ourbr masquerade" % g["cidr"] for g in groups.values() if g["egress"] == "nat"]
    out += ["  }", "}"]
    return "\n".join(out) + "\n"


def render_dnsmasq(groups, routes, nat_dns, leasefile):
    routed = {n for pair in routes for n in pair}
    out = ["# managed by 86box-server: do not edit",
           "# DHCP only (no DNS server). One range per group that has dhcp: true.",
           "port=0", "bind-interfaces", "dhcp-leasefile=%s" % leasefile]
    for name, g in groups.items():
        if not g["dhcp"]:
            continue
        br, pool, nat = g["bridge"], dhcp_pool(g["cidr"]), g["egress"] == "nat"
        out += ["", "# %s" % name, "interface=%s" % br,
                "dhcp-range=set:%s,%s,%s,%s,12h" % (br, pool["first"], pool["last"], pool["netmask"])]
        gateway = nat or name in routed
        out.append("dhcp-option=tag:%s,option:router%s" % (br, "," + pool["router"] if gateway else ""))
        out.append("dhcp-option=tag:%s,option:dns-server%s" % (br, "," + ",".join(nat_dns) if nat else ""))
    return "\n".join(out) + "\n"


def desired_ufw(groups, routes, uplink):
    """The ufw rules these groups need, as argument lists for the ufw command."""
    rules = []
    for g in groups.values():
        if g["dhcp"]:
            rules.append(["allow", "in", "on", g["bridge"], "proto", "udp", "to", "any", "port", "67"])
    if uplink:
        for g in groups.values():
            if g["egress"] == "nat":
                rules.append(["route", "allow", "in", "on", g["bridge"], "out", "on", uplink])
    for a, b in routes:
        ba, bb = groups[a]["bridge"], groups[b]["bridge"]
        rules.append(["route", "allow", "in", "on", ba, "out", "on", bb])
        rules.append(["route", "allow", "in", "on", bb, "out", "on", ba])
    return [r for i, r in enumerate(rules) if r not in rules[:i]]


def ufw_diff(old, new):
    """(rules to add, rules to delete) to get from the old rule list to the new one."""
    return [r for r in new if r not in old], [r for r in old if r not in new]


def ufw_delete_args(rule):
    """The ufw arguments that remove a rule made from `rule`."""
    return ["route", "delete"] + rule[1:] if rule[0] == "route" else ["delete"] + rule
