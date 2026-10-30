"""Checks the netgroups setting. Reads JSON from stdin, prints problems.

Exit code 0 = fine, 1 = problems found.
"""
import ipaddress
import json
import re
import sys


def main():
    groups = json.load(sys.stdin)
    problems = []
    nets = {}
    for name, g in groups.items():
        bridge = str(g.get("bridge", ""))
        if not re.fullmatch(r"br-[a-z0-9]{1,12}", bridge):
            problems.append(f"{name}: bridge name '{bridge}' must look like br-lab (br- plus 1-12 of a-z, 0-9)")
        try:
            net = ipaddress.ip_network(str(g.get("cidr", "")))
        except ValueError:
            problems.append(f"{name}: cidr '{g.get('cidr')}' is not a network")
            continue
        if net.version != 4 or not 16 <= net.prefixlen <= 29:
            problems.append(f"{name}: cidr {net} must be IPv4 with a prefix from /16 to /29")
        nets[name] = net
    names = list(nets)
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            if nets[a].overlaps(nets[b]):
                problems.append(f"{a} and {b}: their networks overlap")
    if problems:
        print("\n".join(problems))
        return 1
    return 0


sys.exit(main())
