#!/usr/bin/env python3
"""TEST ONLY. Stands in for the root helper that applies network group changes.

Usage: fake-netapply.py <request.json> <etc dir> <run dir>
Reads the request, checks it a little, then writes <etc>/netgroups.json, <etc>/netpolicy.json and
<run>/netapply.json in the format the real helper uses. It creates no bridges and needs no root.
"""
import ipaddress
import json
import os
import re
import sys
import time


def write(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def main(request_path, etc, run):
    with open(request_path) as f:
        req = json.load(f)
    result = {"id": req.get("id"), "ok": True, "message": "Applied (fake)", "warnings": [], "ts": int(time.time())}
    groups = {}
    try:
        nets = []
        for name, g in req["netgroups"].items():
            if not re.fullmatch(r"[a-z][a-z0-9]{0,11}", name):
                raise ValueError("bad group name %s" % name)
            net = ipaddress.ip_network(g["cidr"])
            if any(net.overlaps(o) for o in nets):
                raise ValueError("%s overlaps another group" % name)
            nets.append(net)
            groups[name] = {"bridge": g.get("bridge") or "br-" + name, "cidr": g["cidr"],
                            "egress": g.get("egress", "none"), "egress_allow": g.get("egress_allow", []),
                            "dhcp": bool(g.get("dhcp"))}
        for a, b in req.get("group_routes", []):
            if a not in groups or b not in groups:
                raise ValueError("route %s-%s names an unknown group" % (a, b))
    except (KeyError, ValueError, TypeError) as e:
        result.update(ok=False, message="Refused: %s" % e)
    else:
        write(os.path.join(etc, "netgroups.json"), groups)
        write(os.path.join(etc, "netpolicy.json"),
              {"group_routes": req.get("group_routes", []), "nat_dns": req.get("nat_dns", ["1.1.1.1"])})
    write(os.path.join(run, "netapply.json"), result)
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    if len(sys.argv) != 4:
        print(__doc__, file=sys.stderr)
        sys.exit(2)
    sys.exit(main(*sys.argv[1:]))
