# Guest networking

Guests never get direct internet access by default. Each **network group** is one private Linux bridge on the host.

## Defining groups

In your inventory (`group_vars/all/main.yml`):

```yaml
netgroups:
  lab: {bridge: br-lab, cidr: 10.86.0.0/24, egress: none, dhcp: false}
  web: {bridge: br-web, cidr: 10.86.1.0/24, egress: none, dhcp: false}
```

- `bridge`: `br-` plus up to 12 of `a-z0-9`.
- `cidr`: IPv4, prefix `/16` to `/29`. Groups must not overlap.
- `egress` and `dhcp`: see the later network rows; the default is no internet and no DHCP.

`./update.sh` checks these before it changes anything.

## What the host does

- Creates each bridge and gives it the **first host address** of the CIDR (`10.86.0.1` for the example). The host's own network card is never added to a bridge.
- Tells NetworkManager to ignore the bridges and the `tap*` devices 86Box creates.
- Loads its own firewall table, `inet b86box`. It matches only the named bridges, never tap names (86Box picks those), so it cannot touch SSH or other traffic. It does not use Arch's `nftables.service`, which would delete ufw's rules; `./update.sh` refuses to run while that service is enabled.

Result: guests in the same group talk to each other. They cannot reach the host (except DHCP, when enabled), other groups, or the internet.

## DHCP (optional)

Set `dhcp: true` on a group and `./update.sh` runs one small `dnsmasq` for all such groups (`86box-dnsmasq.service`). It does DHCP only, no DNS server. Static addresses always work too, and NT4 is fine with them.

- **Range:** `.100` to `.199` of the group's network. Groups too small for that (`/25` and smaller) use the upper half of the network instead.
- **Lease time:** 12 hours. Leases are listed in `<vm_data_root>/dnsmasq.leases`.
- **Gateway handed out:** the host address (`.1`) only when the group has internet access (`egress: nat`) or is part of a group pair in `group_routes`. Otherwise guests get no gateway.
- **DNS handed out:** `nat_dns` (default `1.1.1.1`) for `nat` groups, none for the rest.
- **Firewall:** port 67/udp is opened on that group's bridge only (in the host's own table, and in ufw when it is active). Turning `dhcp` off closes it again and stops and removes the service.

Arch's own `dnsmasq.service` must stay off; `./update.sh` refuses to run while it is active.

## Static addresses inside a guest

- Address: any free address in the group's CIDR except the `.1` host address.
- Gateway: `.1` (only needed for groups with internet access or group-to-group routes).
- Netmask: from the CIDR (`255.255.255.0` for a `/24`).

## Not supported

`br_netfilter` (usually loaded by Docker) changes how bridged traffic is filtered. `./update.sh` stops if it is loaded.
