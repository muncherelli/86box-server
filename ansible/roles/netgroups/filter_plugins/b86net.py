"""Address helpers for the network group templates (Python stdlib only)."""
import ipaddress


def b86_dhcp_pool(cidr):
    """Pool for a group's DHCP: .100-.199 when the network is big enough, else its upper half.

    Returns {first, last, netmask, router}; router is the host address (.1), never handed out.
    """
    net = ipaddress.ip_network(str(cidr))
    base = int(net.network_address)
    if net.num_addresses >= 256:
        first, last = base + 100, base + 199
    else:
        first, last = base + net.num_addresses // 2, base + net.num_addresses - 2
    return {
        "first": str(ipaddress.ip_address(first)),
        "last": str(ipaddress.ip_address(last)),
        "netmask": str(net.netmask),
        "router": str(next(net.hosts())),
    }


class FilterModule:
    def filters(self):
        return {"b86_dhcp_pool": b86_dhcp_pool}
